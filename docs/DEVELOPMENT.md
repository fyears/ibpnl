# Development guide

## Prerequisites

- Python 3.11+ (repo developed on 3.13)
- Node 18+ (repo developed on 26)
- For live data: Interactive Brokers **IB Gateway** or **TWS** running and
  logged in, with API access enabled.

## Backend

```bash
cd backend
python -m venv .venv
# Windows (Git Bash): source .venv/Scripts/activate
# Windows (cmd):      .venv\Scripts\activate.bat
# macOS/Linux:        source .venv/bin/activate
pip install -e ".[dev]"
ibpnl                 # live IB mode (add --provider mock for simulated data); `python -m app.main` is an alias
```

### Configuration (command-line flags)

All configuration is passed as flags to the `ibpnl` command — there is no
environment-variable or `.env` support. Run `ibpnl --help` for the full list.

| Flag                | Default     | Meaning                                                        |
| ------------------- | ----------- | -------------------------------------------------------------- |
| `--provider`        | `ib`        | `ib` or `mock`                                                 |
| `--host`            | `127.0.0.1` | Bind host                                                      |
| `--port`            | `8000`      | Bind port                                                      |
| `--open`            | off         | Open the dashboard in a browser once it's up                  |
| `--ib-host`         | `127.0.0.1` | IB Gateway/TWS host                                            |
| `--ib-ports`        | `4001,4002,7496,7497` | Ports tried in order (GW live, GW paper, TWS live, TWS paper) |
| `--client-id`       | random, then sticky | API client id; random on first run, saved to `~/.ibpnl/client_id`, reused after |
| `--account`         | *(empty)*   | Account id; empty = first managed account                     |
| `--market-data`     | `auto`      | `auto`/`realtime`/`delayed`/`frozen` — requested MD type      |
| `--mock-md`         | `mixed`     | Mock only: `realtime`/`delayed`/`frozen`/`none`/`mixed`       |
| `--log-level`       | `info`      | `debug`/`info`/`warning`/`error`                              |

The connection is always opened read-only; the app has no order-placement path.

`auto` market-data type asks for real-time and lets IB downgrade to frozen when
markets are closed; if the account lacks a subscription the instrument is marked
`NO DATA`.

### Connecting to IB Gateway / TWS

1. In IB Gateway/TWS: **Configure → API → Settings**, enable *ActiveX and Socket
   Clients*, add `127.0.0.1` to trusted IPs, note the socket port.
2. Default ports: Gateway live `4001`, Gateway paper `4002`, TWS live `7496`,
   TWS paper `7497`. The backend tries `--ib-ports` in order.
3. `ibpnl --provider ib`. Watch the log for the chosen port and the resolved
   market-data capability. Add `--log-level debug` to trace the IB traffic.

## Frontend

```bash
cd frontend
npm install
npm run dev      # dev server @ :5173, proxies /api and /ws to backend :8000
npm run build    # type-check + bundle into ../backend/app/static
npm run check    # tsc --noEmit type check only
```

During development run the backend (mock) and `npm run dev` together, then open
<http://localhost:5173>. For a production-like run, `npm run build` then open the
backend at <http://localhost:8000>.

## Building a standalone binary

You can package everything (Python runtime, dependencies, and the built
frontend) into a **single self-contained executable** that runs on a machine
with no Python or Node installed — copy the one file over and run it.

```bash
# from the repo root, inside backend/.venv (so ib_async etc. are importable)
python scripts/build_binary.py --frontend
# -> dist/ibpnl-<os>-<arch>[.exe]
./dist/ibpnl-windows-x64.exe --provider ib      # runs like the ibpnl command
```

The script builds the frontend, ensures PyInstaller is installed, runs
`backend/ibpnl.spec`, and stages a platform-named binary into `dist/`.

- **Not cross-platform.** A binary is tied to the OS and CPU arch it was built
  on. Build on each target, or use the `Build binaries` GitHub Actions workflow
  (`.github/workflows/build-binaries.yml`) — it builds Windows/Linux/macOS on a
  matrix and, on a `v*` tag, attaches them to a Release. Trigger it manually via
  *Actions -> Build binaries -> Run workflow*.
- **macOS** binaries are unsigned; first run needs a right-click -> Open (or
  `xattr -d com.apple.quarantine ./ibpnl-macos-arm64`).
- The frozen binary keeps the same read-only IB behavior and the sticky
  `~/.ibpnl/client_id` file. `zoneinfo` data (`tzdata`) is bundled, so trading
  hours resolve correctly on Windows.

## Tests

```bash
cd backend && pytest            # backend unit tests (uses mock provider)
```

End-to-end UI testing is done through a **chrome-devtools skill** against the
running mock backend — invoke whichever is available (`chrome-devtools`,
`chrome-devtools-mcp`, or `chrome-devtools-cli`); if none is installed, guide the
user to install one first. See `docs/PLAN.md` M5.

## Project layout

```
backend/
  app/
    cli.py            `ibpnl` entry point (argparse → settings → uvicorn)
    main.py           FastAPI app, lifespan, static serving, provider wiring
    config.py         Settings dataclass (populated by the CLI; no env vars)
    models.py         Pydantic models shared with the frontend
    ib/
      provider.py     MarketDataProvider ABC + shared helpers
      mock_provider.py
      ib_provider.py
    api/
      routes.py       REST endpoints
      ws.py           WebSocket hub + subscription manager
    services/
      grouping.py     group positions by underlying
    static/           built frontend (generated; git-ignored)
  tests/
  pyproject.toml
frontend/
  src/
    main.ts           entry + router
    api/              REST + WS clients, shared types
    pages/            home.ts, instrument.ts
    components/       table, chart, badges, skeletons
    state/            settings store
    styles/           tokens.css + components
  index.html
  package.json
  tsconfig.json
  vite.config.ts
docs/
```

## Conventions

- **Python: type hints on everything** (params + returns); modules start with
  `from __future__ import annotations`. Keep `ruff check` green.
- **UTF-8, explicitly**: new Python modules carry a `# -*- coding: utf-8 -*-`
  hint, and **all file I/O passes `encoding="utf-8"`** (Windows defaults to
  cp1252). Keep terminal/CLI strings ASCII — em dashes become mojibake on cp1252
  consoles; use nice glyphs only in the web UI.
- **Connect to IB read-only, always** — including ad-hoc dev/test scripts: pass
  `readonly=True` (and a distinct `clientId`) to `connectAsync`.
- All JS is authored in **TypeScript** and compiled; no hand-written JS in the
  build output is committed. Keep `npm run check` green; no committed debug hooks.
- Backend never leaks `ib_async` objects past `ib_provider.py`; everything else
  speaks our Pydantic models.
- Money/quantities are numbers in base or instrument currency; the frontend owns
  display formatting and the red/green color convention.
- Times on the wire are **epoch seconds UTC**; the frontend converts for display.

For the non-obvious IB/chart/weekend pitfalls, read **`docs/GOTCHAS.md`**.
