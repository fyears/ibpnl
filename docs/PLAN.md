# Plan & Status

Living checklist. Update the status boxes as work lands so any agent can see
where things stand at a glance.

Legend: ✅ done · 🚧 in progress · ⬜ not started

## Current status (2026-07-18)

**The app is feature-complete and verified end-to-end against BOTH the mock
backend and a live IB Gateway.** All 14 brief requirements are implemented and
tested (grouping, price/greeks/pnl columns, cross-market portfolio, all four
market-data states LIVE/DELAYED/FROZEN/NO DATA, candlestick chart with
volume/HLOC-hover/wheel-zoom/live-updates, position PnL + live Greeks panels,
red-up⇄green-up color convention, exchange⇄local tz toggle, loading skeletons,
sortable/filterable table with sticky first column, mobile 375px).

M4 was verified against a live IB Gateway account (26 real positions across HK/US/
JP/DE/SE markets) — live quotes, IB-supplied option Greeks, and historical +
live minute bars all flow correctly.

### Known-good fixes from the live-Gateway pass
- **Routing exchange (Error 321).** IB portfolio contracts arrive with
  `exchange=''`; `reqMktData`/`reqHistoricalData` reject those ("please enter
  exchange"), which silently killed live quotes, option Greeks, and charts.
  `_make_routable()` in `ib_provider.py` now fills a routing exchange (SMART for
  US SMART-eligible stocks/options, listing exchange otherwise).
- **Fractional volume.** Real IB tick volume can be fractional; the instrument
  quote card now uses `fmtVolume` (e.g. "21.26M") instead of raw floats.

## Round 2 — design refinements (2026-07-18) ✅

A second design pass, verified live against a real IB account (after-hours).

**Home / positions**
- ✅ Account tape now updates live over WebSocket (`account` message from a new
  provider `on_account` callback → `reqPnL` event in IB, per-tick in mock), not
  just the 12s poll. Day/Unrealized P&L and Net Liq pulse on change.
- ✅ Single-leg underlyings render as one flat clickable row (no needless
  expand caret); multi-leg groups still expand. (`FlatRow` in positionsTable.)
- ✅ Whole leg/flat row navigates to the chart (not just the symbol link).
- ✅ Symbol search box (`/api/search` → `reqMatchingSymbols`) jumps to any
  stock/future/index chart, held or not. Debounced, keyboard-navigable.

**Chart / instrument**
- ✅ Range (1D–1Y) and bar granularity (1m–1D) are independent selectors;
  default 1W / 1h. Too-fine grains auto-disable per range.
- ✅ Dashed position-cost line drawn when held (`avg_price` = IB averageCost
  normalized by multiplier).
- ✅ Thicker, bordered candles + wider default `barSpacing` for sparse options.
- ✅ Live tail auto-appends the forming bar at the selected granularity and
  follows the edge only when already pinned there.
- ✅ Extended-hours bars fetched & drawn (verified: 146 ext vs 78 RTH bars).
- ✅ Option "underlying price" shows correctly (was a symptom of the Error-321
  greek suppression; confirmed live).
- ✅ Richer quote card: stocks show Open / VWAP / prev-close / bid×size /
  ask×size / volume; options show volume / open-interest / prev-close.
- ✅ Mobile: chart shrinks correctly on narrow viewports (`min-width:0` on the
  grid cell + explicit `chart.resize()` in the ResizeObserver). Verified 400px.

**Backend touch-ups**
- `Quote` gained `bid_size`, `ask_size`, `vwap`, `open_interest`.
- `Position` gained `avg_price`. New `SearchResult` model + `/api/search`.
- New `WsAccount` message + `on_account` provider callback + hub broadcast.
- `subscribe_bars(con_id, bar_size)` is now granularity-aware; the WS hub
  restarts the provider stream when a lone watcher changes granularity.
- Generic ticks `100,101,104,106,165,233,295` opened with market data for
  option volume / OI and stock VWAP.
- `_fp()` collapses IB's `-1` no-quote sentinel (illiquid/after-hours) to `—`.

## Round 3 — chart & quote polish (2026-07-18) ✅

Verified live against a real IB account during after-hours.

- Removed the meaningless last-value price line/label from the volume series
  (`lastValueVisible:false`, `priceLineVisible:false`).
- Quote card: Bid before Ask; bid/ask show resting size as `size * price`
  (e.g. `200 * 99.90`), not a notional product.
- Session awareness: `Quote.market_session` (regular/pre/post/closed) derived
  from cached IB `liquidHours`/`tradingHours` (`services/trading_hours.py`).
  Off-hours the quote card shows a PRE-MARKET / AFTER HOURS / CLOSED badge and
  relabels prev-close as "Reg. close".
- Extended-hours shading: `BarSet.sessions` carries regular-hours windows; a
  chart pane primitive (`extHoursShade.ts`) shades ext-hours bars on BOTH the
  price and volume panes (per-bar, so it stays aligned), skipped in RTH-only.
- `.prompt.txt` removed from git tracking and gitignored (session artifact).


## Round 4 — session shading fixes & chart lazy-load (2026-07-18) ✅

Verified live against a real IB account and in mock mode.

- Ext-hours shading correctness:
  - `expand_windows()` (`services/trading_hours.py`) replicates the RTH
    time-of-day pattern across every trading day in the bar range. IB's
    `liquidHours` only spans a rolling, arbitrary few days, so historical days
    (and whole weekdays absent from the sample — e.g. Fridays on a weekend) were
    previously shaded as if closed.
  - Shading now classifies by whole-bar overlap: a bar is regular when its
    `[t, t+barLen)` span overlaps a regular window, so the hourly bars that
    straddle the open (09:00 vs 09:30) and the close (16:00 vs a 16:30 close)
    read as regular.
  - Pre-market and after-hours are shaded in DISTINCT colors (cool blue vs warm
    amber), split by whichever regular session is nearer (no calendar-day math).
    A toolbar legend explains the colors; both hidden in RTH-only mode.
- Chart lazy-load: scrolling back past the oldest bar auto-fetches an older
  `range`-sized window (`end` param on `/api/history`, threaded through both
  providers) and prepends it with the viewport preserved, paging until the
  series start. `CandleChart.prependBars` / `setLoadMore`.
- Mock provider now honours `bar_size` / `duration` / `end` with a deterministic,
  continuous price path (so lazily loaded chunks line up), and gained a
  16:30-close test instrument (`LCLZ`) to exercise the close-straddle case.
- Tests: `tests/test_trading_hours.py` (weekend-sample coverage, historical
  Friday, weekend futures) and mock `bar_size`/`end`/late-close coverage.


## Round 5 — CLI, error surfacing & GitHub-ready docs (2026-07-18) ✅

- `ibpnl` console script (`app/cli.py`) is the entry point; all config is passed
  as CLI flags (`--provider`, `--host/--port`, `--ib-host/--ib-ports`,
  `--client-id`, `--account`, `--market-data`, `--mock-md`, `--log-level`,
  `--open`). Dropped env/.env (`pydantic-settings`, `python-dotenv`); `config.py`
  is now a plain dataclass the CLI populates. `python -m app.main` delegates to
  the CLI.
- IB API client id: `--client-id` pins one; otherwise a random id is generated on
  first run and persisted to `~/.ibpnl/client_id`, reused on later runs.
- Frontend errors surface to the browser console (`[ibpnl]` prefix) + a toast;
  global error/unhandledrejection handlers, plus explicit WS/network logging.
- README rewritten for GitHub promotion (features, usage, full flag reference)
  with three screenshots captured from the **mock** account (`DU-MOCK-001`),
  stored via Git LFS. ARCHITECTURE/DEVELOPMENT updated for the CLI.


## Round 6 — search fixes, weekend prices & dev docs (2026-07-18) ✅

- Search/qualify fixes for `IBProvider`: `_qualify` handles the `[None]` result
  from `qualifyContractsAsync` (SMART fails for indices like SPX → bare fallback);
  search adds a **continuous future** for futures roots (MES/ES) since
  `reqMatchingSymbols` returns only the index. Covered by `tests/test_ib_provider.py`.
- Weekend headline price: the instrument header falls back to the last chart
  bar's close when the market is closed / has no live tick, since IB's frozen
  quote is empty (illiquid stock) or a day stale (thin future).
- **New docs for cold-start contributors/agents**: `AGENTS.md` (orientation +
  golden rules + coding conventions) and `docs/GOTCHAS.md` (the IB/`ib_async`,
  chart/timezone, weekend-data, and build/test pitfalls). Conventions: type hints
  everywhere, explicit UTF-8 (`encoding="utf-8"`, ASCII in CLI strings),
  read-only IB connections in dev/test scripts too.


## Round 7 — option-combo history (2026-07-19) ✅

Multi-select option legs in the positions table and chart the **combined** price.
Verified in mock mode and live against a real IB account (SPX 8-leg condor, MU call spread).

- **Combo value = `Σ ratio_i * price_i`** in per-share points, where `ratio_i`
  is the leg's *signed held quantity* (long +, short −). Net-debit combos read
  positive, net-credit negative; a single-leg combo obeys the same rule. Short
  legs invert the per-bar high/low. Contract multiplier (×100) shown as context.
- **Sparse-safe combination** (`services/combo.py::combine_barsets`): union of
  leg bar-timestamps, forward-filling each leg's last close, emitting a combined
  bar only where ≥1 leg printed and once every leg has an initial price. Provider-
  agnostic — mock and IB feed through unchanged.
- **Canonical URL** `#/combo/-5@2006,5@2007` (legs con_id-sorted, `ratio@con_id`),
  normalized client-side via `history.replaceState`; reconstructable cold from
  the link alone. The spec is also shown on the page.
- **Single-underlying only** — enforced in the table (other-underlying option
  checkboxes disable while a selection is active) and the backend (`400` on
  cross-underlying or non-option legs).
- New: `GET /api/combo/history`, `ComboBarSet`/`ComboLegInfo` models,
  `pages/combo.ts` (reuses `CandleChart` — range/grain/RTH/tz/ext-shading, net-
  cost line, live combo mark + forming bar from per-leg quotes), positions-table
  multi-select + floating action bar. Tests in `tests/test_combo.py`.

## Milestones

### M1 — Scaffold & docs ✅
- [x] git repo, directory layout, `.gitignore`
- [x] README, ARCHITECTURE, PLAN, DEVELOPMENT, DESIGN docs

### M2 — Backend core (mock) ✅
- [x] `config.py` (env-driven settings, provider selection)
- [x] `models.py` (Instrument, Quote, Greeks, Position, PositionGroup, AccountSummary, Bar)
- [x] `ib/provider.py` (abstract `MarketDataProvider`)
- [x] `ib/mock_provider.py` (cross-market portfolio, ticks, greeks, bars)
- [x] `services/grouping.py` (group positions by underlying + aggregate)
- [x] `main.py` + `api/routes.py` (REST: account, positions, contract, history)
- [x] `api/ws.py` (WebSocket stream + subscription manager)
- [x] backend unit tests (grouping, mock provider, API smoke) — 20 passing

### M3 — Frontend ✅
- [x] Vite + TS scaffold, build into backend static dir
- [x] REST + WS typed clients
- [x] Global settings store (color convention, timezone) + settings UI
- [x] Home: account cards + grouped positions table (sort/filter/sticky col)
- [x] Instrument: candlestick + volume, HLOC hover, wheel zoom, live updates
- [x] Instrument: Greeks panel (options), position PnL panel (if held)
- [x] Timezone toggle (exchange-local ⇄ user-local) on chart axis
- [x] Loading skeletons / onboarding so nothing looks hung
- [x] Design polish pass (frontend-design skill)

### M4 — Real IB provider ✅
- [x] `ib/ib_provider.py`: connect gateway→tws, account, positions, quotes,
      greeks, market-data-type detection, historical + live bars
- [x] Manual verification against a live IB Gateway (a real account, port
      4001): account summary, 26 positions across 21 underlyings (HK/US/JP/DE/SE
      stocks, US/HK options), live quotes, live option Greeks, historical +
      live minute bars, and correct NO-DATA handling for exchanges the account
      isn't subscribed to (TSEJ/FWB2/SFB). Fixed a routing-exchange bug found
      during this pass (see note below).

### M5 — Testing & hardening ✅
- [x] chrome-devtools MCP E2E pass (home, detail, mobile viewport, toggles) —
      verified: grouping, sort, filter, sticky col, color swap, tz note, chart,
      live greeks/pnl, HLOC hover, all four market-data badges
- [x] Error/empty/disconnected states (error-note, NO DATA badge, backend-
      unreachable topbar state, WS auto-reconnect)
- [x] Final docs sync

## Open decisions / notes

- Provider selected at startup via the `--provider` CLI flag. Mock is default so
  the app runs with zero external dependencies.
- Frontend is served by the backend in production (`npm run build` →
  `backend/app/static`), and via Vite dev server with a proxy in development.
- Grouping key = underlying symbol (options/FOPs group under their underlying;
  stocks/futures group under their own symbol).

## How to resume this project (for a future agent)

1. Read `docs/ARCHITECTURE.md`.
2. Skim this file's checklist to see what's done.
3. `cd backend && pip install -e ".[dev]" && ibpnl` (mock mode).
4. `cd frontend && npm install && npm run dev`.
5. Pick the next ⬜ item.
