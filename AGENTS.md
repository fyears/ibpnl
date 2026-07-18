# AGENTS.md

Orientation for an agent picking up this repo cold. Read this first, then the
doc it points you to for the area you're touching.

## What this is

**IBPNL** — a professional, **read-only** web dashboard for an Interactive
Brokers account: cross-market positions grouped by underlying, live P&L,
streaming quotes and option greeks, and candlestick charts with extended-hours
shading. Runs against a built-in **mock account by default**; point it at IB
Gateway/TWS with `--provider ib`.

- **Backend**: Python 3.11+, `ib_async`, FastAPI, WebSockets (`backend/`).
- **Frontend**: vanilla TypeScript + Vite, lightweight-charts, TanStack Table
  core (`frontend/`). Builds into `backend/app/static` (git-ignored), served by
  the backend.
- **CLI**: one console script, `ibpnl`; all config via flags (no env/`.env`).

## Golden rules (do not violate)

1. **Read-only.** The IB connection is `readonly=True`; there is no
   order-placement path. Never add one.
2. **Screenshots use mock data only** (account `DU-MOCK-001`) — never a real
   account. PNGs go through **Git LFS**.
3. **Never commit a real account identity.** No live IBKR account id (e.g.
   `U`/`DU` + digits), real positions, or other account PII in code, docs,
   commit messages, tests, or checked-in screenshots — the repo is public. The
   account comes only from the `--account` CLI flag at runtime. When you must
   refer to a live run in docs, write "a real IB account" (never the number);
   for examples use an obviously-fake placeholder like `U1234567`. `server*.log`
   is git-ignored precisely because it contains the connected account — keep it
   ignored, and never paste its contents into tracked files.
4. **English + light mode + mobile-friendly** UI (per the brief).
5. **Config is CLI flags only** — no environment variables.
6. **Never leak `ib_async` objects past `backend/app/ib/ib_provider.py`** — the
   rest of the app speaks our Pydantic models (`app/models.py`).
7. **Wire = epoch seconds UTC**; the frontend converts for display.

## Where things are

```
backend/app/
  cli.py            ibpnl entry point (argparse → settings → uvicorn)
  config.py         Settings dataclass (populated by the CLI)
  main.py           FastAPI app, lifespan, static serving, provider wiring
  models.py         Pydantic models shared with the frontend
  ib/provider.py    MarketDataProvider ABC (the key abstraction)
  ib/mock_provider.py   deterministic simulation (default; no IB needed)
  ib/ib_provider.py     real ib_async → IB Gateway/TWS  (all IB quirks live here)
  services/trading_hours.py  parse/expand IB hours → session windows
  services/grouping.py       group positions by underlying
  api/routes.py     REST endpoints        api/ws.py  WebSocket hub + subscriptions
  tests/            backend unit tests (fake IB / mock provider)
frontend/src/
  main.ts           entry + hash router + global error reporting
  api/              typed REST + WS clients, shared types
  pages/            home.ts, instrument.ts
  components/       candleChart.ts, extHoursShade.ts, positionsTable.ts, badges
  state/            settings store (localStorage)
  styles/           tokens.css + app.css
docs/               ARCHITECTURE, GOTCHAS, DEVELOPMENT, DESIGN, PLAN
```

## Run / build / test

```bash
# backend (mock mode; no IB needed)
cd backend && python -m venv .venv && source .venv/Scripts/activate   # or .../bin/activate
pip install -e ".[dev]"
ibpnl                        # http://127.0.0.1:8000 ; add --open to launch a browser
ibpnl --provider ib          # live IB Gateway/TWS (must be running, API enabled)

# frontend
cd frontend && npm install
npm run dev                  # Vite @ :5173, proxies /api + /ws to :8000 (live reload)
npm run build                # type-check + bundle into backend/app/static  <-- REQUIRED before the backend serves UI changes
npm run check                # tsc --noEmit only

# tests
cd backend && pytest         # uses the mock provider / a fake IB object

# standalone binary (bundles runtime + deps + built frontend into one file)
python scripts/build_binary.py --frontend   # -> dist/ibpnl-<os>-<arch>[.exe]
```

Packaging lives in `backend/ibpnl.spec` (+ `backend/packaging/entry.py`), driven
by `scripts/build_binary.py` locally and `.github/workflows/build-binaries.yml`
in CI (win/linux/mac matrix, attaches to `v*` releases). Binaries are **not**
cross-platform — build on each target OS. See `docs/DEVELOPMENT.md`.

UI has **no test harness** — verify changes by driving the running **mock**
backend with the chrome-devtools MCP (navigate, snapshot, screenshot, read the
console). Keep the console clean; frontend errors log with an `[ibpnl]` prefix.

## Before you touch...

- **anything under `ib/ib_provider.py`, session/hours logic, or charts** → read
  **`docs/GOTCHAS.md`** first. IB's live/frozen data is unreliable at the edges
  (illiquid instruments, off-hours, weekends); its historical bars and contract
  details are reliable. Many subtle bugs came from trusting the wrong source.
- **the overall design / data flow** → `docs/ARCHITECTURE.md` (the map).
- **running or configuring it** → `docs/DEVELOPMENT.md`.
- **styling** → `docs/DESIGN.md` (design tokens; light mode only).
- **status / what's done** → `docs/PLAN.md`.

## Coding conventions

- **Python: type-hint everything** (params + returns); modules start with
  `from __future__ import annotations`. Keep `ruff check` green.
- **UTF-8 explicitly**: new Python modules carry a `# -*- coding: utf-8 -*-` hint,
  and **all file I/O passes `encoding="utf-8"`** (Windows defaults to cp1252).
  Keep terminal/CLI strings ASCII (em dashes render as mojibake on cp1252
  consoles); use fancy glyphs only in the web UI.
- **Connect to IB read-only in dev/test scripts too**: pass `readonly=True` to
  `connectAsync` (and a distinct `clientId`, e.g. `99`). See `docs/GOTCHAS.md`.
- **TypeScript** for all UI; keep `npm run check` green; no committed debug hooks.

## Workflow expectations

- The frontend builds into `backend/app/static`; **rebuild before** expecting the
  backend to serve UI changes, and hard-reload the browser (the bundle is cached).
- On Windows/Git Bash: `backend/.venv/Scripts/python.exe`, kill servers with
  `taskkill //F //PID <pid>`, use absolute paths for background launches.
- You may `git commit` directly (no user confirmation needed) once you've
  verified the change yourself — build runs and smoke test / tests pass. If a
  change is untested or risky, confirm first. End commit messages with the
  required `Co-Authored-By` trailer.
