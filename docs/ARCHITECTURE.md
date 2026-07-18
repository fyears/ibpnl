# Architecture

This document is the map of the project. Read it before changing code.

## Goals (from the brief)

A clean, English, light-mode, mobile-friendly dashboard for an IBKR account:

1. Home: account summary + positions grouped by underlying (e.g. one `SPX`
   group containing many SPX option legs). Positions may be long or short.
2. Positions show price, Greeks, daily PnL, unrealized PnL.
3. Positions are **cross-market**: US stock / future / equity option / index
   option / futures option, HK stock / option, KR stock, etc.
4. Market data may be **real-time / delayed / frozen / unavailable** depending on
   the account's subscriptions — the UI reflects the actual state per instrument.
5. Instrument pages show a candlestick chart (default: last week, 1-minute bars)
   with pre/post-market and volume, live-updating intraday, HLOC hover, wheel
   zoom, and an exchange-local ⇄ user-local timezone toggle.
6. Options show live Greeks; held instruments show that position's PnL.
7. Global settings: red-up/green-down ⇄ red-down/green-up color convention.

## High-level shape

```
┌────────────────────────────────────────────────────────────────┐
│  Browser (TypeScript + Vite build)                               │
│   • Home page: account cards + grouped positions table           │
│   • Instrument page: lightweight-charts candles + Greeks/PnL      │
│   • Talks to backend over REST (snapshots) + WebSocket (streams)  │
└───────────────▲───────────────────────────────┬──────────────────┘
                │ REST /api/*                     │ WS /ws
                │ (JSON snapshots)                │ (live tick/greek/pnl deltas)
┌───────────────┴───────────────────────────────▼──────────────────┐
│  FastAPI backend  (backend/app)                                   │
│                                                                   │
│   api/routes.py ── REST endpoints ─┐                              │
│   api/ws.py     ── WebSocket hub  ─┤                              │
│                                     ▼                             │
│   services/  (grouping, formatting, subscription manager)         │
│                                     │                             │
│   ib/provider.py  ── abstract MarketDataProvider ────┐            │
│        ├── ib/mock_provider.py   (simulated data)    │            │
│        └── ib/ib_provider.py     (ib_async → IB)     │            │
└──────────────────────────────────────────────────────┼───────────┘
                                                         │
                                          ┌──────────────▼───────────┐
                                          │ IB Gateway (:4001/:4002)  │
                                          │  fallback TWS (:7496/:7497)│
                                          └───────────────────────────┘
```

## The provider abstraction (the most important design choice)

Everything the UI needs is defined by one interface,
[`app/ib/provider.py`](../backend/app/ib/provider.py) → `MarketDataProvider`.

Two implementations satisfy it:

- **`MockProvider`** — generates a realistic, deterministic cross-market
  portfolio plus simulated live ticks / Greeks / minute bars. No network. This
  is what runs in tests and during UI development.
- **`IBProvider`** — the real thing, using `ib_async`. Connects to IB Gateway
  first, falls back to TWS. Translates IB objects into our own models.

The rest of the app (API, services, frontend) only ever sees our own Pydantic
models — never raw `ib_async` objects. This keeps IB quirks in one file and lets
us develop/test with zero dependency on a running Gateway.

Selection is via the `--provider` CLI flag (`ib` default | `mock`), applied to
[`app/config.py`](../backend/app/config.py) by the
[`ibpnl` CLI](../backend/app/cli.py) and constructed in
[`app/main.py`](../backend/app/main.py) at startup (stored on `app.state`).

## Data models (`app/models.py`)

- `Instrument` — normalized contract: `con_id`, `symbol`, `sec_type`
  (`STK/FUT/OPT/FOP/IND/...`), `exchange`, `currency`, `underlying`,
  option fields (`right`, `strike`, `expiry`, `multiplier`), display helpers.
- `Quote` — `last/bid/ask/close/volume`, `market_data_type`
  (`realtime/delayed/frozen/none`), timestamps.
- `Greeks` — `delta/gamma/vega/theta/iv/undPrice`.
- `Position` — an `Instrument` + `quantity` (signed), `avg_cost`, `Quote`,
  optional `Greeks`, `daily_pnl`, `unrealized_pnl`, `market_value`.
- `PositionGroup` — an underlying `symbol` + aggregated totals + child
  `Position`s (the "SPX header with legs below" requirement).
- `AccountSummary` — net liq, cash, buying power, maintenance margin, day PnL,
  base currency, and the resolved market-data capability.
- `Bar` — `time` (epoch seconds, UTC) + `open/high/low/close/volume`.

## Real-time model

- REST gives **snapshots** (account, positions, history) so the page renders
  immediately even before any stream connects (requirement: no "frozen"/dead
  loading state).
- The **WebSocket** (`/ws`) carries **deltas**: the client subscribes to a set
  of `con_id`s (all held positions on Home, or the single instrument on a detail
  page), and the server pushes `quote` / `greeks` / `pnl` / `bar` messages as
  they change. A `subscription manager` in the backend refcounts IB market-data
  lines so we only request what's actually being viewed.

## Where numbers come from (important)

| Datum | Real `IBProvider` | `MockProvider` |
| --- | --- | --- |
| Option Greeks | **IB-supplied** `ticker.modelGreeks` (tickOptionComputation) — never computed locally | Local Black-Scholes (`services/blackscholes.py`) so simulated greeks move coherently with the simulated spot |
| Daily / unrealized PnL per position | **IB-supplied** `reqPnLSingle` | Estimated from simulated prev close |
| Account daily PnL | **IB-supplied** `reqPnL` / account summary tags | Summed from simulated legs |
| Live chart bars | `reqHistoricalData(keepUpToDate=True)` — one call gives history **and** streaming bar updates | Simulated random walk |
| Exchange timezone | `ContractDetails.timeZoneId` | Hardcoded per mock underlying |

Rule of thumb: anything IB can tell us authoritatively, we take from IB. The
mock's local math exists only so the UI can be exercised without a Gateway.

## Market-data-type handling

IB market data can be live(1), frozen(2), delayed(3), delayed-frozen(4), or the
account may lack the subscription entirely. `IBProvider` calls
`reqMarketDataType`, watches the `marketDataType` tick, and records the achieved
type per instrument. The UI shows a badge (LIVE / DELAYED / FROZEN / NO DATA)
and greys stale figures accordingly. `MockProvider` can simulate any of these
via config to exercise the UI.

## Frontend structure (`frontend/src`)

- `api/` — typed REST client + WebSocket client (mirrors backend models).
- `pages/home.ts` — account cards + grouped positions table.
- `pages/instrument.ts` — chart + Greeks/PnL panels.
- `components/` — positions table (TanStack Table core), chart wrapper
  (lightweight-charts), badges, loading skeletons.
- `state/` — global settings (color convention, timezone) persisted to
  `localStorage`.
- `styles/` — design tokens + component CSS (see `docs/DESIGN.md`).

## Why these choices

- **FastAPI**: async-native (matches `ib_async`'s asyncio core), first-class
  WebSockets, Pydantic models shared shape with the frontend, serves static files.
- **Vanilla TS + Vite** (no React): keeps the bundle and mental model small and
  easy for another agent to follow; the interactivity we need (a table, a chart,
  a WS feed) doesn't justify a framework.
- **lightweight-charts**: required by the brief; purpose-built for financial
  candles with volume, crosshair HLOC, and wheel zoom out of the box.
- **TanStack Table (core)**: framework-agnostic table logic (sorting, filtering,
  grouping/expanding) that we render ourselves so we control the sticky-column
  and mobile behavior.
