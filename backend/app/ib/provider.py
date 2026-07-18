"""The abstract market-data provider.

This is the seam between the app and the data source. Everything above this
layer (services, API, frontend) depends only on this interface and the models in
`app.models` — never on ib_async directly.

Two implementations exist:
  * MockProvider (app.ib.mock_provider)  — simulated, no network
  * IBProvider   (app.ib.ib_provider)    — real ib_async connection

Streaming model: a caller `subscribe(con_ids)` to receive live updates, and the
provider invokes the registered async callbacks as data arrives. The API layer's
subscription manager refcounts these so IB market-data lines are only open while
something is actually watching.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable, Iterable

from app.models import (
    AccountSummary,
    BarSet,
    ConnectionStatus,
    Greeks,
    Instrument,
    Position,
    PositionGroup,
    Quote,
    SearchResult,
)

# Async callbacks the provider calls when live data changes.
QuoteCallback = Callable[[Quote], Awaitable[None]]
GreeksCallback = Callable[[Greeks], Awaitable[None]]
PnlCallback = Callable[[int, float | None, float | None, float | None], Awaitable[None]]
BarCallback = Callable[[int, object, bool], Awaitable[None]]  # (con_id, Bar, is_update)
# (day_pnl, unrealized_pnl, net_liquidation)
AccountCallback = Callable[[float | None, float | None, float | None], Awaitable[None]]


class MarketDataProvider(abc.ABC):
    """Interface every data source must implement."""

    # --- lifecycle ---

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect / initialize. Safe to call once at app startup."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Disconnect / clean up. Called at app shutdown."""

    @abc.abstractmethod
    def status(self) -> ConnectionStatus:
        """Current connection status (never raises)."""

    # --- snapshots (REST) ---

    @abc.abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        ...

    @abc.abstractmethod
    async def get_positions(self) -> list[Position]:
        """All held positions, each with a best-effort quote/greeks snapshot."""

    @abc.abstractmethod
    async def get_position_groups(self) -> list[PositionGroup]:
        """Positions grouped by underlying with aggregated totals."""

    @abc.abstractmethod
    async def get_instrument(self, con_id: int) -> Instrument | None:
        ...

    @abc.abstractmethod
    async def get_quote(self, con_id: int) -> Quote | None:
        ...

    @abc.abstractmethod
    async def get_greeks(self, con_id: int) -> Greeks | None:
        ...

    @abc.abstractmethod
    async def search(self, query: str) -> list[SearchResult]:
        """Resolve a free-text query to instruments (for the symbol search box)."""

    @abc.abstractmethod
    async def get_history(
        self,
        con_id: int,
        *,
        duration: str = "1 W",
        bar_size: str = "1 min",
        rth_only: bool = False,
        end: int | None = None,
    ) -> BarSet:
        """Historical bars. Default: last week of 1-minute bars incl. ext hours.

        `end` (epoch seconds, UTC) fetches a window ending at that instant rather
        than "now" — used to lazily load older bars when the chart scrolls back.
        """

    # --- streaming (WebSocket) ---

    @abc.abstractmethod
    async def subscribe(self, con_ids: Iterable[int]) -> None:
        """Begin streaming quotes/greeks/pnl for these instruments."""

    @abc.abstractmethod
    async def unsubscribe(self, con_ids: Iterable[int]) -> None:
        """Stop streaming for these instruments (when no one is watching)."""

    @abc.abstractmethod
    async def subscribe_bars(self, con_id: int, bar_size: str = "1 min") -> None:
        """Begin streaming live (updating) bars for a single instrument."""

    @abc.abstractmethod
    async def unsubscribe_bars(self, con_id: int) -> None:
        ...

    # --- callback registration ---

    def on_quote(self, cb: QuoteCallback) -> None:
        self._quote_cbs.append(cb)

    def on_greeks(self, cb: GreeksCallback) -> None:
        self._greeks_cbs.append(cb)

    def on_pnl(self, cb: PnlCallback) -> None:
        self._pnl_cbs.append(cb)

    def on_bar(self, cb: BarCallback) -> None:
        self._bar_cbs.append(cb)

    def on_account(self, cb: AccountCallback) -> None:
        self._account_cbs.append(cb)

    # subclasses must initialize these lists in __init__
    _quote_cbs: list[QuoteCallback]
    _greeks_cbs: list[GreeksCallback]
    _pnl_cbs: list[PnlCallback]
    _bar_cbs: list[BarCallback]
    _account_cbs: list[AccountCallback]

    async def _emit_quote(self, quote: Quote) -> None:
        for cb in self._quote_cbs:
            await cb(quote)

    async def _emit_greeks(self, greeks: Greeks) -> None:
        for cb in self._greeks_cbs:
            await cb(greeks)

    async def _emit_pnl(
        self, con_id: int, daily: float | None, unreal: float | None, mv: float | None
    ) -> None:
        for cb in self._pnl_cbs:
            await cb(con_id, daily, unreal, mv)

    async def _emit_bar(self, con_id: int, bar: object, is_update: bool) -> None:
        for cb in self._bar_cbs:
            await cb(con_id, bar, is_update)

    async def _emit_account(
        self, daily: float | None, unreal: float | None, net_liq: float | None
    ) -> None:
        for cb in self._account_cbs:
            await cb(daily, unreal, net_liq)
