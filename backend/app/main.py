"""FastAPI application entry point.

The application object (`app`) is created here; configuration lives in
`app.config.settings` and is populated by the `ibpnl` CLI. To run the server,
use the `ibpnl` command (see `app/cli.py`) — `python -m app.main` simply
delegates to it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.api.ws import StreamHub, ws_router
from app.config import settings

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _make_provider():
    """Construct the configured data provider (imported lazily so mock mode
    doesn't require ib_async to be importable/working)."""
    if settings.data_provider == "ib":
        from app.ib.ib_provider import IBProvider

        return IBProvider()
    from app.ib.mock_provider import MockProvider

    return MockProvider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    provider = _make_provider()
    app.state.provider = provider
    app.state.hub = StreamHub(provider)
    await provider.start()
    log.info("provider '%s' started", settings.data_provider)
    try:
        yield
    finally:
        await provider.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="IBKR Dashboard", lifespan=lifespan)
    app.include_router(router)
    app.include_router(ws_router)

    if STATIC_DIR.is_dir():
        # Serve built frontend. SPA fallback: unknown non-API paths -> index.html
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):  # pragma: no cover - trivial
            candidate = STATIC_DIR / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()


def main() -> None:
    """Delegate to the `ibpnl` CLI so `python -m app.main` behaves identically."""
    from app.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
