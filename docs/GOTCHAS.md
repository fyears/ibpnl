# Gotchas & hard-won lessons

Non-obvious things that bit us, so they don't bite you. Skim the section for the
area you're touching before you start. Paired with `AGENTS.md` (orientation) and
`ARCHITECTURE.md` (the map).

Rule of thumb behind almost everything here: **IB's live/frozen data is
unreliable at the edges (illiquid instruments, off-hours, weekends); its
historical bars and contract details are reliable.** When in doubt, trust
`reqHistoricalData` and `reqContractDetails` over `reqMktData`.

---

## IB / `ib_async` (`backend/app/ib/ib_provider.py`)

### `qualifyContractsAsync` does not raise on an unknown contract
It logs `Error 200` and returns a list whose element is **`None`** (e.g.
`[None]`), so `(qualified,) = await ...` silently binds `None`. Always check the
element, not just `try/except`. See `_qualify()` — it tries `SMART` then a bare
(no-exchange) contract and treats a `None`/empty result as failure. Regression
covered by `tests/test_ib_provider.py`.

### Indices (and some futures) reject `SMART` routing
`SMART` gives `Error 200` for `SPX`, index continuous futures, etc. The
bare-exchange fallback in `_qualify()` resolves them. Symptom if you get this
wrong: "search SPX → open detail → Unknown instrument / not supported".

### Portfolio contracts arrive with `exchange=''` → `Error 321`
IB's `portfolio()`/`position()` contracts have no exchange, and
`reqMktData`/`reqHistoricalData` reject them ("please enter exchange"), silently
killing live quotes, greeks, and charts for held positions. `_make_routable()`
fills a routing exchange (`SMART` for US SMART-eligible stocks/options, the
listing exchange otherwise). Route everything through it before an IB request.

### `reqMatchingSymbols` never returns futures — only the underlying/index
Searching `MES`/`ES` returns the **index** (with `derivativeSecTypes` containing
`FUT`) plus unrelated stocks, never the future. To offer a chartable future we
qualify a `ContFuture(symbol, exchange)` (`_continuous_futures()`), which
resolves to `CONTFUT` → a real front-month `FUT` contract. Notes:
- Cache the qualified contract in `self._contracts` so the detail page resolves
  the con_id later.
- Map `CONTFUT` → `SecType.FUT` / `us_future` in `_sec_type` / `_asset_class`.
- Continuous futures chart fine (history works); live market data may not.

### `averageCost` includes the multiplier for derivatives
IB's `PortfolioItem.averageCost` is per-contract (already × multiplier) for
options/futures. Divide by `instrument.multiplier` to get a per-unit `avg_price`
comparable to the quote and the chart axis (see `_portfolio_item_to_position`).

### `-1` and `NaN` are "no value" sentinels
IB reports `-1` for an absent quote (illiquid / after-hours) and `NaN` for
uninitialized ticks. `_fp()` collapses **both** to `None`; use it for every
float off a ticker, or the UI shows `-1.00` / `NaN`.

### Market-data types: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
`reqMarketDataType` requests a type; the achieved type arrives on the
`marketDataType` tick and can differ (`auto` → 4 when there's no realtime
subscription). No-data errors `{354, 10089, 10167, 10168, 10197}` mark the
instrument `NONE`. The UI badge reflects the **achieved** type, not the request.

### `liquidHours` / `tradingHours` cover only a rolling few days — and which days is arbitrary
Format: `YYYYMMDD:HHMM-YYYYMMDD:HHMM;...;YYYYMMDD:CLOSED`, in **exchange-local**
tz. The window is short and its contents depend on *when you ask*: on a weekend
`HOOD` returns only the upcoming **Mon–Thu** (no Friday). So you cannot assume
any given weekday appears. `expand_windows()` (`services/trading_hours.py`)
learns the **modal** intraday RTH interval from whatever days IB gives and
replays it across every trading day in the bar range. Symptom if you assume the
sample is complete: whole weekdays (e.g. every Friday) shaded as if closed.

### IB folds the overnight electronic session onto the LAST day of `liquidHours` — the "previous trading day" anomaly
For futures, IB tacks the full overnight session onto the **last day of its
rolling `liquidHours` sample** as one long segment. ES, sampled on a Saturday,
returns `...;20260723:0830-20260723:1600;20260723:1700-20260724:1600` — that
final `1700-<next>1600` is Thu 17:00 -> Fri 16:00 (**~23h**), the electronic
session, **not** a regular/liquid daytime one. `liquidHours` is otherwise pure
RTH, so this segment is the odd one out.
- **Which weekday it lands on is arbitrary** — it's whatever day ends IB's
  sample, and that depends on *when you ask*. On a weekend the sample ends
  **Thursday**, so the block is Thu->Fri; ask another day and it moves.
- `expand_windows` learns per-weekday and **replays** it, so the block recurs
  **every** matching weekday (every Thu here), each spilling into the next
  morning. Left as "regular" it makes overnight bars (e.g. 02:00 Fri) read as
  regular hours — **no pre/post shading on the chart**, and `classify_session`
  reports REGULAR when it's really PRE.
- **Fix:** interpret RTH via `regular_windows()` (`services/trading_hours.py`),
  which drops any `liquidHours` interval that **crosses exchange-local midnight
  or runs > ~14h**. `tradingHours` (the extended session) legitimately spans
  midnight — parse it with `parse_hours`, *not* `regular_windows`. Regression:
  `tests/test_trading_hours.py::test_expand_windows_overnight_bar_is_not_regular`.

### Weekend / frozen quotes are empty or a day stale
Over a weekend `reqMktData` returns **all-null** for an illiquid stock (DRAM) and
a **day-stale** close for a thinly-traded future (SPXESUP shows Thursday, not
Friday). The chart's most recent historical bar is always the last real print,
so the instrument header falls back to the last bar's close when
`market_session === "closed"` or there's no live `last` (see
`renderPrice()` in `frontend/src/pages/instrument.ts`). Held positions have a
second price source — IB's `PortfolioItem.marketPrice` — which
`_portfolio_item_to_position` uses but `get_quote` does not, so the same
instrument can show a price on Home yet null on its detail page.

### One persistent connection; keep a stable client id
A single IB connection is shared by all browser clients. IB Gateway/TWS misbehave
if you reconnect with a new client id (stale connections linger), so the CLI
generates a random id once and persists it to `~/.ibpnl/client_id`, reusing it
after (`app/cli.py`). Pass `--client-id N` to pin one.

### Connect **read-only** — always, including throwaway scripts
The app connects with `readonly=True` (`settings.ib_readonly`). When you write an
ad-hoc test/debug script that hits a live Gateway/TWS, **pass `readonly=True` to
`connectAsync`** too — a read-write API session can place/modify orders and IB
may also refuse the connection depending on TWS settings. Use a client id
distinct from the running app's (e.g. `clientId=99`) so you don't collide.
```python
ib = IB()
await ib.connectAsync("127.0.0.1", 4001, clientId=99, readonly=True)
```

### Benign IB log noise (safe to ignore)
`2104/2106/2158` (market-data farm OK), `162` (historical pacing / cancelled
query), `300` (can't find EId — from cancelling a completed request), `354` (not
subscribed — expected for exchanges the account lacks), `10090` (delayed-data
notification), `200` (no security definition — we use it as the qualify-fallback
signal). Don't treat these as failures.

### Windows event loop
`ib_async`'s socket transport is most reliable on the selector loop; the CLI sets
`WindowsSelectorEventLoopPolicy` on win32 before starting uvicorn.

---

## Charts & timezones (`frontend/src/components/`)

### lightweight-charts renders every timestamp in UTC
To show "exchange time" or "viewer local time" we shift each bar's epoch by the
target zone's UTC offset **before** handing it to the chart (`shift()` in
`candleChart.ts`), computing the offset DST-correctly via `Intl.DateTimeFormat`.
Don't pass a tz-aware Date; the library ignores it.

### IB intraday bars are stamped at the bar's START and hour-aligned
An hourly bar is `09:00` (covering 09:00–10:00), not `09:30`, so the bar that
**straddles the open** (09:00 when RTH begins 09:30) would look pre-market by a
naive point-in-window test. Classify a bar as regular when its whole
`[t, t+barLen)` span **overlaps** a regular window — this also keeps the bar
straddling a non-standard close (e.g. 16:00 when the close is 16:30) on the
regular side. See `classifyBar()` in `candleChart.ts`.

### `setData` does not preserve scroll position on prepend
When lazy-loading older bars, snapshot the visible **logical** range, prepend,
`setData`, then restore the range shifted right by the number of prepended bars
(`prependBars()`), or the viewport jumps.

### The default per-series last-value line is meaningless on the volume histogram
It draws a dashed line at "the last bar's volume". Disable with
`lastValueVisible:false, priceLineVisible:false` on the volume series.

### Pane primitives: `timeToCoordinate` returns null for non-bar times
So ext-hours shading is computed **per bar** (each bar is always a valid
time-scale point), not per arbitrary session-boundary timestamp. The
`ExtHoursShade` primitive is attached to **both** the price and volume panes so
the shading stays aligned; pre vs post are distinct colors, split by whichever
regular session is nearer (no calendar-day math). See `extHoursShade.ts`.

---

## Backend / data conventions

- **Never leak `ib_async` objects past `ib_provider.py`.** The API, services and
  frontend only see our Pydantic models (`app/models.py`).
- **Times on the wire are epoch seconds, UTC.** The frontend converts for
  display. `SessionWindow.start/end` are epoch seconds UTC too.
- **Anything IB can tell us authoritatively, take from IB** (greeks, per-position
  PnL, live bars, exchange tz). The mock's local math exists only to exercise the
  UI without a Gateway. See the table in `ARCHITECTURE.md`.
- **The mock provider is deterministic and now honours `bar_size` / `duration` /
  `end`** with a continuous price path (`_level()` + `_hash01()` are pure
  functions of the timestamp, so lazily loaded older chunks line up). `LCLZ` is a
  fake instrument with a 16:30 close, used to exercise the close-straddle case.

---

## Build / run / test workflow

### The frontend builds into `backend/app/static` (git-ignored)
The backend serves that directory. **You must `npm run build` before the backend
serves an updated UI.** `npm run dev` (Vite @ :5173, proxying to :8000) is for
live-reload during development.

### The browser caches the bundle
After a rebuild, an ordinary reload can serve the old JS. Hard-reload
(bypass cache) when verifying a frontend change, or you'll debug a stale bundle.

### Backend tests fake the IB object
Provider logic is unit-tested without a live Gateway by constructing
`IBProvider.__new__(IBProvider)` (skipping `__init__`) and assigning a fake `ib`
with just the methods under test (see `tests/test_ib_provider.py`). The mock
provider is exercised directly (`tests/test_mock_provider.py`).

### There is no frontend test harness
Verify UI changes by driving the running **mock** backend through the
**`chrome-devtools-cli` skill** (navigate, snapshot, screenshot, read the
console). If it is not installed, tell the user and guide them to install it.
Keep the browser console clean — frontend errors are logged
with an `[ibpnl]` prefix.

### Screenshots for docs/README MUST use mock data
Never commit a screenshot of a real account/positions. Run `ibpnl --provider
mock` (account `DU-MOCK-001`) and capture from that. PNGs are stored via **Git
LFS** (`.gitattributes` tracks `*.png`); run `git lfs install` / `git lfs pull`
after cloning.

### Windows / Git Bash specifics
- Python: `backend/.venv/Scripts/python.exe`; console script:
  `backend/.venv/Scripts/ibpnl.exe`.
- Kill a server: `taskkill //F //PID <pid>` (find it via `netstat -ano | grep
  ':8000 .*LISTEN'`).
- Use **absolute paths** when launching background processes; a `cd` inside a
  compound command can trip permission prompts.

---

## Coding conventions

### Python
- **Type hints everywhere.** Annotate every function/method — parameters and
  return type. `from __future__ import annotations` is at the top of modules so
  newer syntax (`X | None`, builtin generics) works on 3.11. Ruff enforces style
  (`ruff check`); keep it green.
- **UTF-8, explicitly.** Source files are UTF-8; new modules carry a
  `# -*- coding: utf-8 -*-` hint. **Always pass `encoding="utf-8"`** to
  `open()` / `Path.read_text` / `Path.write_text` — on Windows the default is
  cp1252 and will corrupt or crash on non-ASCII.
- **Keep console/CLI output ASCII.** Windows consoles are often cp1252, so an em
  dash or fancy punctuation in `--help`/log strings renders as mojibake. Use
  plain ASCII (`-`, `->`) in terminal-facing text; save the nice glyphs for the
  web UI.

### TypeScript
- All UI code is TypeScript and compiled; no hand-written JS is committed. Keep
  `npm run check` (tsc `--noEmit`) green. Do not leave `window.__x` debug hooks
  in a committed bundle.

## Hard product rules (do not violate)

- **Read-only, always.** The IB connection is opened `readonly=True` and there is
  no order-placement code path. Never add one — this extends to test scripts
  (see the read-only note above).
- **English + light mode only**, and **mobile-friendly** — per the brief.
- **Config is CLI flags only** — no environment variables, no `.env`.
- **Never hardcode a real account.** No live IBKR account id (`U`/`DU` + digits),
  real positions, or account PII in code, docs, tests, commit messages, or
  screenshots (this repo is public). The account is supplied only via `--account`
  at runtime. In docs say "a real IB account", not the number; for examples use a
  fake placeholder like `U1234567`. `server*.log` holds the connected account and
  stays git-ignored — don't paste it into tracked files.
