# IBPNL — a read-only trading dashboard for Interactive Brokers

**IBPNL** turns your Interactive Brokers account into a fast, clean web
dashboard: cross-market positions grouped by underlying, live P&L, streaming
quotes and option greeks, and professional candlestick charts with
extended-hours shading — all **read-only** (it never places or modifies orders).

It connects to your **live IB Gateway / TWS by default**. Start Gateway/TWS with
the API enabled first, then launch IBPNL in one command. No IB running? Add
`--provider mock` to explore the whole thing against a built-in simulated
account — no connection or real data required.

```bash
git clone <this-repo> && cd ibpnl
cd frontend && npm install && npm run build && cd ../backend
pip install -e .
ibpnl --open                    # live IB account — opens http://127.0.0.1:8000
ibpnl --provider mock --open    # ...or a built-in simulated account, no IB needed
```

> All screenshots below use the built-in **simulated** account (`DU-MOCK-001`).

![Positions dashboard](docs/assets/home.png)

---

## Why you might like it

- **Everything on one screen.** Account equity, buying power, and every position
  grouped by underlying — stocks, futures, and options (equity / index /
  futures) across US, HK, and KR markets — with live marks and P&L.
- **Read-only by design.** The IB connection is opened with `readonly=True`; the
  app has no order-placement code path at all. Safe to leave running.
- **Proper charts.** Candlesticks + volume via
  [lightweight-charts](https://tradingview.github.io/lightweight-charts/), with
  selectable range and bar size, your cost basis drawn as a dashed line, and
  **pre-market vs after-hours shaded in distinct colors** (derived from IB's own
  trading-hours calendar).
- **Scroll back in time.** Drag the chart left and older history streams in
  automatically — no "load more" button.
- **Live option greeks.** Delta / gamma / vega / theta / IV and the underlying
  price, updating in real time.
- **Search anything.** Jump to a chart for any stock, future, or index —
  including instruments you don't hold.
- **Zero-dependency demo mode.** A deterministic mock provider simulates a
  realistic multi-market portfolio with moving prices, greeks, and bars, so the
  entire UI works offline — ideal for trying it out, screenshots, and tests.
- **Your preferences.** Green-up or red-up P&L coloring, exchange or local chart
  time, and a mobile-friendly responsive layout.

---

## Screenshots

| Instrument chart (held position) | Option with live greeks |
| --- | --- |
| ![Candlestick chart with ext-hours shading and cost line](docs/assets/chart.png) | ![Option detail with greeks](docs/assets/option.png) |

The chart shades **pre-market** (cool blue) and **after-hours** (warm amber)
distinctly from the regular session, with a legend; the dashed line marks your
average cost. Everything shown is simulated data.

---

## Install

IBPNL is a Python package that bundles the built web UI. Until it's published to
PyPI, install it from source (this also builds the frontend):

```bash
git clone <this-repo> && cd ibpnl

# 1. build the web UI (outputs into backend/app/static, served by the backend)
cd frontend
npm install
npm run build

# 2. install the backend + the `ibpnl` command
cd ../backend
python -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev]"           # drop [dev] if you don't need tests/linters

# 3. run it (needs IB Gateway/TWS running with the API enabled;
#    or add --provider mock to try it with no IB connection)
ibpnl --open
```

The repo uses [Git LFS](https://git-lfs.com/) for the PNG screenshots — run
`git lfs install` once before cloning, or `git lfs pull` afterwards, to fetch
them.

Running against a **real** account (the default) needs
[IB Gateway or Trader Workstation](https://www.interactivebrokers.com/en/trading/ibgateway-stable.php)
running locally with the API enabled.

---

## Usage

### Connect to your IB account (default)

Start IB Gateway / TWS first, enable the API (Gateway: *Configure → Settings →
API → Enable ActiveX and Socket Clients*), then:

```bash
ibpnl                            # tries the usual ports, first managed account
ibpnl --account U1234567         # a specific account
ibpnl --ib-ports 7496            # only TWS live
ibpnl --log-level debug          # trace the IB conversation in the terminal
```

By default the ports `4001,4002,7496,7497` (Gateway live/paper, TWS live/paper)
are tried in order and the first that answers is used.

### Try it with simulated data (no IB needed)

```bash
ibpnl --provider mock              # mock account, http://127.0.0.1:8000
ibpnl --provider mock --open       # ...and open it in your browser
ibpnl --provider mock --mock-md delayed   # simulate a delayed-data account to see the badges
```

#### About the client id

IB identifies each API connection by a numeric **client id**. If you don't pass
`--client-id`, IBPNL generates a random one on first run and saves it to
`~/.ibpnl/client_id`, then **reuses that same id** on later runs (IB Gateway/TWS
behaves best with a stable client id). Pass `--client-id N` to pin a specific
one; it's saved too.

### All options

Run `ibpnl --help` for the authoritative list. Summary:

| Flag | Default | Description |
| --- | --- | --- |
| `--provider {mock,ib}` | `ib` | `ib` = live IB Gateway/TWS; `mock` = built-in simulated account |
| `--host` | `127.0.0.1` | Bind address for the web UI |
| `--port` | `8000` | Port for the web UI |
| `--open` | off | Open the dashboard in your browser once it's up |
| `--log-level {debug,info,warning,error}` | `info` | Console verbosity; `debug` traces IB traffic |
| `--ib-host` | `127.0.0.1` | IB Gateway/TWS host (with `--provider ib`) |
| `--ib-ports` | `4001,4002,7496,7497` | Comma-separated ports to try in order |
| `--client-id N` | random, then sticky | IB API client id (see above) |
| `--account ACCT` | first managed | IB account id to display |
| `--market-data {auto,realtime,delayed,frozen}` | `auto` | Requested market-data type (`auto` falls back to delayed) |
| `--mock-md {mixed,realtime,delayed,frozen,none}` | `mixed` | Simulated market-data state (with `--provider mock`) |

There is **no** environment-variable or config-file setup — every setting is a
command-line flag.

---

## Logs & troubleshooting

- **Backend / IB traffic** goes to the terminal you launched `ibpnl` from. Use
  `--log-level debug` to see the full IB conversation (connections, market-data
  farm status, request/tick flow).
- **Frontend errors** are logged to the browser's DevTools console with an
  `[ibpnl]` prefix (open DevTools with F12), and surfaced as a small toast in
  the UI so nothing fails silently. Lost-backend and WebSocket reconnects are
  reported there too.
- **Can't connect to IB?** Confirm Gateway/TWS is running, the API is enabled,
  and the port matches `--ib-ports`. The top-bar status pill shows the live
  connection state and the port/account it connected on.

---

## How it works

```
                 ┌──────────────┐    WebSocket + REST    ┌─────────────────┐
  IB Gateway ◄───┤  IBProvider  │◄──────────────────────┤  TypeScript UI  │
     / TWS       │  (ib_async)  │                        │  (Vite build)   │
                 └──────┬───────┘                        └─────────────────┘
                        │  implements
                 ┌──────▼─────────────────┐
                 │  MarketDataProvider     │   ← swappable interface
                 └──────▲─────────────────┘
                        │  implements
                 ┌──────┴───────┐
                 │ MockProvider │   (deterministic simulation, no IB needed)
                 └──────────────┘
```

- **Backend:** Python 3.11+, [`ib_async`](https://github.com/ib-api-reloaded/ib_async),
  FastAPI, WebSockets. A single persistent IB connection is shared by all
  browser clients.
- **Frontend:** TypeScript + Vite,
  [lightweight-charts](https://tradingview.github.io/lightweight-charts/),
  [TanStack Table](https://tanstack.com/table). Built into `backend/app/static`
  and served by the same process.
- **Provider abstraction:** everything the UI needs is defined by one
  `MarketDataProvider` interface, with a live `IBProvider` and an offline
  `MockProvider` behind it.

For a deeper tour see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md);
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) covers running from source and
extending it; [`AGENTS.md`](AGENTS.md) orients a fresh contributor (or AI agent)
and [`docs/GOTCHAS.md`](docs/GOTCHAS.md) collects the non-obvious IB / chart /
weekend pitfalls.

---

## Development

```bash
# backend tests
cd backend && pytest

# frontend: type-check + build, or live-reload dev server
cd frontend
npm run build            # -> backend/app/static
npm run dev              # Vite on :5173, proxies /api and /ws to :8000
```

In dev you can run the Vite server (`npm run dev`) alongside `ibpnl` for
hot-reloading the UI while the backend streams real or mock data.

---

## License

See [`LICENSE`](LICENSE) (PolyForm Strict License 1.0.0).

IBPNL is an independent project and is not affiliated with or endorsed by
Interactive Brokers. It is **read-only** and provided as-is; verify any figure
against official IB tools before relying on it.
