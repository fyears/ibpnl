"""REST endpoints. All snapshot-style; live updates go over the WebSocket.

The provider is injected via `request.app.state.provider` (set in main.py).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.models import (
    AccountSummary,
    BarSet,
    ComboBarSet,
    ComboLegInfo,
    ConnectionStatus,
    Greeks,
    Instrument,
    PositionGroup,
    Quote,
    SearchResult,
)
from app.services.combo import canonical_spec, combine_barsets, parse_combo_spec

router = APIRouter(prefix="/api")


def _provider(request: Request):
    return request.app.state.provider


@router.get("/status", response_model=ConnectionStatus)
async def get_status(request: Request) -> ConnectionStatus:
    return _provider(request).status()


@router.get("/account", response_model=AccountSummary)
async def get_account(request: Request) -> AccountSummary:
    return await _provider(request).get_account_summary()


@router.get("/positions", response_model=list[PositionGroup])
async def get_positions(request: Request) -> list[PositionGroup]:
    """Positions grouped by underlying, aggregates included."""
    return await _provider(request).get_position_groups()


@router.get("/instrument/{con_id}", response_model=Instrument)
async def get_instrument(con_id: int, request: Request) -> Instrument:
    inst = await _provider(request).get_instrument(con_id)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"Unknown instrument {con_id}")
    return inst


@router.get("/quote/{con_id}", response_model=Quote)
async def get_quote(con_id: int, request: Request) -> Quote:
    quote = await _provider(request).get_quote(con_id)
    if quote is None:
        raise HTTPException(status_code=404, detail=f"No quote for {con_id}")
    return quote


@router.get("/greeks/{con_id}", response_model=Greeks | None)
async def get_greeks(con_id: int, request: Request) -> Greeks | None:
    """Greeks for an option; null for non-options."""
    return await _provider(request).get_greeks(con_id)


@router.get("/search", response_model=list[SearchResult])
async def search(
    request: Request,
    q: str = Query("", description="Symbol or name fragment, e.g. 'AAPL'"),
) -> list[SearchResult]:
    """Symbol search for the home-page jump box (stocks / futures / indices)."""
    q = q.strip()
    if len(q) < 1:
        return []
    return await _provider(request).search(q)


@router.get("/history/{con_id}", response_model=BarSet)
async def get_history(
    con_id: int,
    request: Request,
    duration: str = Query("1 W", description="IB duration string, e.g. '1 W', '2 D'"),
    bar_size: str = Query("1 min", description="IB bar size, e.g. '1 min', '5 mins'"),
    rth_only: bool = Query(False, description="Regular trading hours only"),
    end: int = Query(0, description="Epoch seconds (UTC) to end the window at; 0 = now"),
) -> BarSet:
    return await _provider(request).get_history(
        con_id, duration=duration, bar_size=bar_size, rth_only=rth_only,
        end=end or None,
    )


@router.get("/combo/history", response_model=ComboBarSet)
async def get_combo_history(
    request: Request,
    legs: str = Query(..., description="Combo spec, e.g. '-5@2006,5@2007'"),
    duration: str = Query("1 W", description="IB duration string, e.g. '1 W', '2 D'"),
    bar_size: str = Query("1 min", description="IB bar size, e.g. '1 min', '5 mins'"),
    rth_only: bool = Query(False, description="Regular trading hours only"),
    end: int = Query(0, description="Epoch seconds (UTC) to end the window at; 0 = now"),
) -> ComboBarSet:
    """Combined candlestick series for a single-underlying option combo.

    The spec lists signed multiples per contract; the value is
    ``sum(ratio_i * price_i)`` in per-share points (net-credit combos go
    negative). All legs must be options on the same underlying.
    """
    try:
        combo_legs = parse_combo_spec(legs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid combo: {exc}") from exc

    provider = _provider(request)

    infos: list[ComboLegInfo] = []
    underlyings: set[str] = set()
    multiplier = 1.0
    for leg in combo_legs:
        inst = await provider.get_instrument(leg.con_id)
        if inst is None:
            raise HTTPException(status_code=404, detail=f"Unknown instrument {leg.con_id}")
        if not inst.is_option:
            raise HTTPException(
                status_code=400,
                detail=f"{inst.display_name()} is not an option; combos are option-only",
            )
        underlyings.add(inst.underlying)
        multiplier = inst.multiplier
        infos.append(ComboLegInfo(instrument=inst, ratio=leg.ratio))
    if len(underlyings) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"combo legs span multiple underlyings: {sorted(underlyings)}",
        )

    barsets = {}
    exchange_tz = "UTC"
    sessions = []
    for i, leg in enumerate(combo_legs):
        bs = await provider.get_history(
            leg.con_id, duration=duration, bar_size=bar_size,
            rth_only=rth_only, end=end or None,
        )
        barsets[leg.con_id] = bs
        if i == 0:  # all legs share the underlying's exchange/tz/sessions
            exchange_tz = bs.exchange_tz
            sessions = bs.sessions

    bars = combine_barsets(combo_legs, barsets)
    return ComboBarSet(
        symbol=next(iter(underlyings)) if underlyings else "?",
        legs=infos,
        multiplier=multiplier,
        canonical=canonical_spec(combo_legs),
        bar_size=bar_size,
        exchange_tz=exchange_tz,
        rth_only=rth_only,
        bars=bars,
        sessions=[] if rth_only else sessions,
    )
