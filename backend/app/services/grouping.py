"""Group positions by underlying and compute aggregate totals.

Grouping key is the instrument's `underlying` (which the provider sets to the
underlying symbol for options/FOPs and to the symbol itself for stocks/futures).
This yields the brief's "one SPX header with all SPX legs beneath it" layout.
"""

from __future__ import annotations

from app.models import AssetClass, Position, PositionGroup


def _group_delta(p: Position) -> float | None:
    """Delta contribution in underlying-share equivalents.

    For options: delta * qty * multiplier. For stocks: qty (delta 1).
    Returns None if we can't compute it (so we don't fake a number).
    """
    inst = p.instrument
    if inst.is_option:
        if p.greeks is None or p.greeks.delta is None:
            return None
        return p.greeks.delta * p.quantity * inst.multiplier
    if inst.sec_type.value in ("STK", "FUT", "IND"):
        return p.quantity * (inst.multiplier if inst.sec_type.value == "FUT" else 1.0)
    return None


def _sum_optional(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def group_positions(
    positions: list[Position],
    fx_to_base: dict[str, float] | None = None,
) -> list[PositionGroup]:
    """Return position groups sorted by absolute market value (desc).

    `fx_to_base` maps currency code -> rate to the account base currency, used
    only for cross-currency sorting (group totals stay in the group currency).
    Unknown currencies fall back to rate 1.0.
    """
    fx = fx_to_base or {}
    buckets: dict[str, list[Position]] = {}
    for p in positions:
        key = p.instrument.underlying or p.instrument.symbol
        buckets.setdefault(key, []).append(p)

    groups: list[PositionGroup] = []
    for symbol, legs in buckets.items():
        # Sort legs: stock/future first, then options by expiry then strike.
        legs.sort(key=_leg_sort_key)
        deltas = [_group_delta(p) for p in legs]
        group = PositionGroup(
            symbol=symbol,
            asset_class=_dominant_asset_class(legs),
            currency=legs[0].instrument.currency,
            positions=legs,
            total_market_value=_sum_optional([p.market_value for p in legs]),
            total_unrealized_pnl=_sum_optional([p.unrealized_pnl for p in legs]),
            total_daily_pnl=_sum_optional([p.daily_pnl for p in legs]),
            net_delta=_sum_optional(deltas),
        )
        groups.append(group)

    groups.sort(
        key=lambda g: abs((g.total_market_value or 0.0) * fx.get(g.currency, 1.0)),
        reverse=True,
    )
    return groups


def _leg_sort_key(p: Position) -> tuple:
    inst = p.instrument
    # non-options (stock/future) come first (0), options after (1)
    is_opt = 1 if inst.is_option else 0
    expiry = inst.expiry or ""
    strike = inst.strike or 0.0
    right = inst.right.value if inst.right else ""
    return (is_opt, expiry, strike, right)


def _dominant_asset_class(legs: list[Position]) -> AssetClass:
    """Pick a representative asset class for the group badge.

    Prefer the underlying instrument's class (stock/future) if present,
    otherwise the first leg's class.
    """
    for p in legs:
        if not p.instrument.is_option:
            return p.instrument.asset_class
    return legs[0].instrument.asset_class
