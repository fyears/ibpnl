"""Runtime configuration.

Populated by the `ibpnl` CLI (see ``app/cli.py``) from command-line arguments.
There is intentionally no environment-variable / ``.env`` support: everything is
passed explicitly on the command line. Import ``settings`` anywhere you need
configuration; the CLI mutates this singleton once, before the server starts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Settings:
    # --- Web server ---
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # --- Provider selection ---
    # "mock" -> deterministic simulated data (default; no IB needed)
    # "ib"   -> real ib_async connection to IB Gateway / TWS
    data_provider: str = "mock"

    # --- IB connection (used when data_provider == "ib") ---
    ib_host: str = "127.0.0.1"
    # Ports tried in order: GW live, GW paper, TWS live, TWS paper.
    ib_ports: str = "4001,4002,7496,7497"
    ib_client_id: int = 17
    ib_account: str = ""  # empty -> first managed account
    ib_readonly: bool = True  # the dashboard never places orders
    # Requested market-data type: auto | realtime | delayed | frozen
    market_data_type: str = "auto"

    # --- Mock provider tuning ---
    # mixed | realtime | delayed | frozen | none
    mock_md_state: str = "mixed"

    @property
    def ib_port_list(self) -> list[int]:
        return [int(p.strip()) for p in self.ib_ports.split(",") if p.strip()]


settings = Settings()
