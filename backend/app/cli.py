"""``ibpnl`` command-line entry point.

All configuration is passed as command-line flags (run ``ibpnl --help``). The
IB API client id is either supplied with ``--client-id`` or generated randomly
on first run and then persisted to ``~/.ibpnl/client_id`` so subsequent runs
reuse the same id (IB Gateway/TWS is happiest with a stable client id).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

from app import __version__
from app.config import settings

log = logging.getLogger("ibpnl")

STATE_DIR = Path.home() / ".ibpnl"
CLIENT_ID_FILE = STATE_DIR / "client_id"


def _persist_client_id(cid: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CLIENT_ID_FILE.write_text(str(cid), encoding="utf-8")
    except OSError as exc:  # non-fatal: we just lose stickiness across restarts
        log.warning("could not persist client id to %s: %s", CLIENT_ID_FILE, exc)


def _resolve_client_id(explicit: int | None) -> tuple[int, str]:
    """Return (client_id, source) — explicit flag, persisted file, or fresh random.

    The resolved id is always persisted so later runs default to the same one.
    """
    if explicit is not None:
        _persist_client_id(explicit)
        return explicit, "flag"
    try:
        text = CLIENT_ID_FILE.read_text(encoding="utf-8").strip()
        if text:
            return int(text), "saved"
    except (OSError, ValueError):
        pass
    cid = random.randint(1000, 9000)
    _persist_client_id(cid)
    return cid, "random"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ibpnl",
        description=(
            "IBPNL - a professional, read-only web dashboard for Interactive "
            "Brokers accounts (positions, P&L, live quotes, option greeks, "
            "candlestick charts). Runs fully offline against simulated data by "
            "default; point it at IB Gateway/TWS with --provider ib."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"ibpnl {__version__}")

    srv = p.add_argument_group("web server")
    srv.add_argument("--host", default="127.0.0.1", help="Bind address for the web UI")
    srv.add_argument("--port", type=int, default=8000, help="Port for the web UI")
    srv.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Console log verbosity (use 'debug' to trace IB traffic)",
    )
    srv.add_argument(
        "--open",
        action="store_true",
        help="Open the dashboard in your default browser once it's up",
    )

    prov = p.add_argument_group("data source")
    prov.add_argument(
        "--provider",
        choices=["mock", "ib"],
        default="mock",
        help="'mock' = built-in simulated account; 'ib' = live IB Gateway/TWS",
    )

    ib = p.add_argument_group("IB connection (with --provider ib)")
    ib.add_argument("--ib-host", default="127.0.0.1", help="IB Gateway/TWS host")
    ib.add_argument(
        "--ib-ports",
        default="4001,4002,7496,7497",
        help="Comma-separated ports to try in order (GW live/paper, TWS live/paper)",
    )
    ib.add_argument(
        "--client-id",
        type=int,
        default=None,
        metavar="N",
        help="IB API client id; if omitted, a random id is generated once and "
        "reused from ~/.ibpnl/client_id on later runs",
    )
    ib.add_argument(
        "--account",
        default="",
        metavar="ACCT",
        help="IB account id to show (default: the first managed account)",
    )
    ib.add_argument(
        "--market-data",
        default="auto",
        choices=["auto", "realtime", "delayed", "frozen"],
        help="Requested market-data type ('auto' falls back to delayed)",
    )

    mock = p.add_argument_group("mock tuning (with --provider mock)")
    mock.add_argument(
        "--mock-md",
        default="mixed",
        choices=["mixed", "realtime", "delayed", "frozen", "none"],
        help="Simulated market-data state, to exercise the UI badges",
    )
    return p


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    settings.host = args.host
    settings.port = args.port
    settings.log_level = args.log_level.upper()
    settings.data_provider = args.provider
    settings.ib_host = args.ib_host
    settings.ib_ports = args.ib_ports
    settings.ib_account = args.account
    settings.market_data_type = args.market_data
    settings.mock_md_state = args.mock_md

    _setup_logging(settings.log_level)

    if sys.platform == "win32":
        # ib_async's socket transport is most reliable on the selector loop.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    url = f"http://{settings.host}:{settings.port}"
    if settings.data_provider == "ib":
        cid, source = _resolve_client_id(args.client_id)
        settings.ib_client_id = cid
        log.info(
            "IB mode: host=%s ports=%s client-id=%s (%s) account=%s read-only=%s",
            settings.ib_host,
            settings.ib_ports,
            cid,
            source,
            settings.ib_account or "<first managed>",
            settings.ib_readonly,
        )
    else:
        log.info("Mock mode: simulated account, no IB connection required")
    log.info("Dashboard: %s  (log level: %s)", url, settings.log_level)

    if args.open:
        _open_browser_when_ready(url)

    from app.main import app  # imported after settings are applied
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        loop="asyncio",
    )


def _open_browser_when_ready(url: str) -> None:
    """Open `url` in a browser shortly after the server starts listening."""
    import threading
    import webbrowser

    def _later() -> None:
        import time

        time.sleep(1.2)
        webbrowser.open(url)

    threading.Thread(target=_later, daemon=True).start()


if __name__ == "__main__":
    main()
