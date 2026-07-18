"""Tests for position grouping logic."""

from app.models import (
    AssetClass,
    Greeks,
    Instrument,
    OptionRight,
    Position,
    SecType,
)
from app.services.grouping import group_positions


def _stk(con_id: int, symbol: str, qty: float, mv: float) -> Position:
    return Position(
        instrument=Instrument(
            con_id=con_id, symbol=symbol, sec_type=SecType.STK,
            underlying=symbol, asset_class=AssetClass.US_STOCK,
        ),
        quantity=qty, avg_cost=10.0, market_value=mv,
        unrealized_pnl=5.0, daily_pnl=1.0,
    )


def _opt(con_id: int, underlying: str, strike: float, expiry: str,
         right: OptionRight, qty: float, mv: float, delta: float | None = 0.5) -> Position:
    return Position(
        instrument=Instrument(
            con_id=con_id, symbol=underlying, sec_type=SecType.OPT,
            underlying=underlying, asset_class=AssetClass.US_OPTION,
            right=right, strike=strike, expiry=expiry, multiplier=100,
        ),
        quantity=qty, avg_cost=2.0, market_value=mv,
        unrealized_pnl=2.0, daily_pnl=0.5,
        greeks=None if delta is None else Greeks(con_id=con_id, delta=delta),
    )


def test_options_group_under_underlying():
    positions = [
        _stk(1, "AAPL", 100, 20000.0),
        _opt(2, "SPX", 5500, "20260801", OptionRight.PUT, -5, -2500.0),
        _opt(3, "SPX", 5400, "20260801", OptionRight.PUT, 5, 1500.0),
        _opt(4, "SPX", 5750, "20260901", OptionRight.CALL, -3, -900.0),
    ]
    groups = group_positions(positions)
    by_symbol = {g.symbol: g for g in groups}
    assert set(by_symbol) == {"AAPL", "SPX"}
    assert by_symbol["SPX"].leg_count == 3
    assert by_symbol["AAPL"].leg_count == 1


def test_group_totals_sum_legs():
    positions = [
        _opt(2, "SPX", 5500, "20260801", OptionRight.PUT, -5, -2500.0),
        _opt(3, "SPX", 5400, "20260801", OptionRight.PUT, 5, 1500.0),
    ]
    (g,) = group_positions(positions)
    assert g.total_market_value == -1000.0
    assert g.total_unrealized_pnl == 4.0
    assert g.total_daily_pnl == 1.0


def test_net_delta_uses_multiplier():
    positions = [
        _opt(2, "SPX", 5500, "20260801", OptionRight.PUT, -5, -2500.0, delta=-0.4),
    ]
    (g,) = group_positions(positions)
    # -0.4 delta * -5 qty * 100 multiplier = +200 share-equivalents
    assert g.net_delta == 200.0


def test_missing_delta_does_not_fake_zero():
    positions = [
        _opt(2, "SPX", 5500, "20260801", OptionRight.PUT, -5, -2500.0, delta=None),
    ]
    (g,) = group_positions(positions)
    assert g.net_delta is None


def test_stock_leg_sorts_before_options():
    positions = [
        _opt(2, "AAPL", 240, "20260801", OptionRight.CALL, -3, -1800.0),
        _stk(1, "AAPL", 100, 20000.0),
    ]
    (g,) = group_positions(positions)
    assert g.positions[0].instrument.sec_type == SecType.STK


def test_groups_sorted_by_abs_market_value():
    positions = [
        _stk(1, "AAPL", 100, 5000.0),
        _stk(2, "NVDA", -100, -30000.0),
    ]
    groups = group_positions(positions)
    assert [g.symbol for g in groups] == ["NVDA", "AAPL"]
