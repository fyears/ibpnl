"""WebSocket streaming hub.

Protocol (JSON messages):

  client -> server:
    {"action": "subscribe",   "con_ids": [1, 2, 3]}   # replaces this client's quote set
    {"action": "subscribe_bars", "con_id": 42}         # at most one bar stream per client
    {"action": "unsubscribe_bars"}
    {"action": "ping"}

  server -> client:
    {"type": "quote",  "quote": {...}}
    {"type": "greeks", "greeks": {...}}
    {"type": "pnl",    "con_id": N, "daily_pnl": ..., "unrealized_pnl": ..., "market_value": ...}
    {"type": "bar",    "con_id": N, "bar": {...}, "update": true|false}
    {"type": "account", "day_pnl": ..., "unrealized_pnl": ..., "net_liquidation": ...}
    {"type": "pong"}

The hub refcounts con_ids across all connected clients so the provider only
streams what someone is actually watching (important for IB market-data lines,
which are a limited resource).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.ib.provider import MarketDataProvider
from app.models import Bar, Greeks, Quote

log = logging.getLogger(__name__)

ws_router = APIRouter()


class StreamHub:
    """Fans provider callbacks out to WebSocket clients, with refcounting."""

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider
        # per-client subscriptions
        self._client_quotes: dict[WebSocket, set[int]] = {}
        self._client_bars: dict[WebSocket, int | None] = {}
        self._client_bar_size: dict[WebSocket, str] = {}
        # refcounts across clients
        self._quote_refs: Counter[int] = Counter()
        self._bar_refs: Counter[int] = Counter()
        self._lock = asyncio.Lock()
        # register provider callbacks once
        provider.on_quote(self._on_quote)
        provider.on_greeks(self._on_greeks)
        provider.on_pnl(self._on_pnl)
        provider.on_bar(self._on_bar)
        provider.on_account(self._on_account)

    # --- client lifecycle ---

    async def attach(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._client_quotes[ws] = set()
            self._client_bars[ws] = None
            self._client_bar_size[ws] = "1 min"

    async def detach(self, ws: WebSocket) -> None:
        async with self._lock:
            quotes = self._client_quotes.pop(ws, set())
            bar = self._client_bars.pop(ws, None)
            self._client_bar_size.pop(ws, None)
            await self._release_quotes(quotes)
            if bar is not None:
                await self._release_bar(bar)

    # --- subscription management ---

    async def set_quotes(self, ws: WebSocket, con_ids: set[int]) -> None:
        async with self._lock:
            old = self._client_quotes.get(ws, set())
            added = con_ids - old
            removed = old - con_ids
            self._client_quotes[ws] = con_ids
            newly_active = [c for c in added if self._quote_refs[c] == 0]
            for c in added:
                self._quote_refs[c] += 1
            if newly_active:
                await self.provider.subscribe(newly_active)
            await self._release_quotes(removed)

    async def set_bars(self, ws: WebSocket, con_id: int | None, bar_size: str = "1 min") -> None:
        async with self._lock:
            old = self._client_bars.get(ws)
            old_size = self._client_bar_size.get(ws, "1 min")
            self._client_bar_size[ws] = bar_size
            if old == con_id:
                # Same instrument: if only the granularity changed and this client
                # is the sole watcher, restart the provider stream at the new size.
                if con_id is not None and bar_size != old_size and self._bar_refs[con_id] == 1:
                    await self.provider.subscribe_bars(con_id, bar_size)
                return
            self._client_bars[ws] = con_id
            if con_id is not None:
                if self._bar_refs[con_id] == 0:
                    await self.provider.subscribe_bars(con_id, bar_size)
                self._bar_refs[con_id] += 1
            if old is not None:
                await self._release_bar(old)

    async def _release_quotes(self, con_ids: set[int]) -> None:
        """Caller must hold self._lock."""
        to_drop = []
        for c in con_ids:
            self._quote_refs[c] -= 1
            if self._quote_refs[c] <= 0:
                del self._quote_refs[c]
                to_drop.append(c)
        if to_drop:
            await self.provider.unsubscribe(to_drop)

    async def _release_bar(self, con_id: int) -> None:
        """Caller must hold self._lock."""
        self._bar_refs[con_id] -= 1
        if self._bar_refs[con_id] <= 0:
            del self._bar_refs[con_id]
            await self.provider.unsubscribe_bars(con_id)

    # --- provider callbacks -> fan out ---

    async def _send_to_watchers(self, con_id: int, payload: dict, bars: bool = False) -> None:
        # snapshot clients under lock, send outside it
        async with self._lock:
            if bars:
                targets = [ws for ws, c in self._client_bars.items() if c == con_id]
            else:
                targets = [ws for ws, s in self._client_quotes.items() if con_id in s]
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                # client is gone; detach cleans up on the reader side
                pass

    async def _on_quote(self, quote: Quote) -> None:
        await self._send_to_watchers(
            quote.con_id, {"type": "quote", "quote": quote.model_dump()}
        )

    async def _on_greeks(self, greeks: Greeks) -> None:
        await self._send_to_watchers(
            greeks.con_id, {"type": "greeks", "greeks": greeks.model_dump()}
        )

    async def _on_pnl(
        self, con_id: int, daily: float | None, unreal: float | None, mv: float | None
    ) -> None:
        await self._send_to_watchers(
            con_id,
            {
                "type": "pnl",
                "con_id": con_id,
                "daily_pnl": daily,
                "unrealized_pnl": unreal,
                "market_value": mv,
            },
        )

    async def _on_bar(self, con_id: int, bar: object, is_update: bool) -> None:
        assert isinstance(bar, Bar)
        await self._send_to_watchers(
            con_id,
            {"type": "bar", "con_id": con_id, "bar": bar.model_dump(), "update": is_update},
            bars=True,
        )

    async def _on_account(
        self, daily: float | None, unreal: float | None, net_liq: float | None
    ) -> None:
        payload = {
            "type": "account",
            "day_pnl": daily,
            "unrealized_pnl": unreal,
            "net_liquidation": net_liq,
        }
        async with self._lock:
            targets = list(self._client_quotes.keys())
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                pass


@ws_router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    hub: StreamHub = ws.app.state.hub
    await hub.attach(ws)
    try:
        while True:
            msg = await ws.receive_json()
            action = msg.get("action")
            if action == "subscribe":
                con_ids = {int(c) for c in msg.get("con_ids", [])}
                await hub.set_quotes(ws, con_ids)
            elif action == "subscribe_bars":
                await hub.set_bars(ws, int(msg["con_id"]), str(msg.get("bar_size", "1 min")))
            elif action == "unsubscribe_bars":
                await hub.set_bars(ws, None)
            elif action == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        await hub.detach(ws)
