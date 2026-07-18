"""Tests for option-combo spec parsing and bar combination."""

import httpx
import pytest

from app.ib.mock_provider import _LEGS
from app.main import create_app
from app.models import Bar, BarSet
from app.services.combo import (
    ComboLeg,
    canonical_spec,
    combine_barsets,
    parse_combo_spec,
)


# ---- spec parsing / canonicalization -------------------------------------

def test_parse_basic():
    legs = parse_combo_spec("5@2007,-5@2006")
    # canonical order is by con_id ascending
    assert legs == [ComboLeg(2006, -5), ComboLeg(2007, 5)]


def test_parse_canonical_is_order_independent():
    a = canonical_spec(parse_combo_spec("5@2007,-5@2006"))
    b = canonical_spec(parse_combo_spec("-5@2006,5@2007"))
    assert a == b == "-5@2006,5@2007"


def test_parse_single_leg_keeps_sign():
    assert parse_combo_spec("-3@2008") == [ComboLeg(2008, -3)]
    assert parse_combo_spec("3@2008") == [ComboLeg(2008, 3)]


def test_parse_merges_duplicates_and_drops_zero():
    assert parse_combo_spec("2@10,-2@10,5@11") == [ComboLeg(11, 5)]


def test_parse_rejects_garbage_and_empty():
    for bad in ("", "abc", "5*2007", "@2007", "5@", "0@10"):
        with pytest.raises(ValueError):
            parse_combo_spec(bad)


# ---- combine_barsets ------------------------------------------------------

def _bs(con_id: int, bars: list[Bar]) -> BarSet:
    return BarSet(con_id=con_id, symbol=str(con_id), bars=bars)


def test_combine_single_long_leg_is_scaled_price():
    bars = [Bar(time=t, open=1.0, high=2.0, low=0.5, close=1.5, volume=10) for t in (60, 120)]
    out = combine_barsets([ComboLeg(1, 3)], {1: _bs(1, bars)})
    assert [b.time for b in out] == [60, 120]
    assert out[0].open == 3.0 and out[0].close == 4.5
    assert out[0].high == 6.0 and out[0].low == 1.5  # scaled, order preserved


def test_combine_single_short_leg_is_negative_and_high_low_flip():
    bars = [Bar(time=60, open=1.0, high=2.0, low=0.5, close=1.5, volume=10)]
    out = combine_barsets([ComboLeg(1, -2)], {1: _bs(1, bars)})
    b = out[0]
    assert b.open == -2.0 and b.close == -3.0
    # short leg: combo high uses the leg's LOW, low uses the leg's HIGH
    assert b.high == -1.0 and b.low == -4.0
    assert b.high >= b.open >= b.low and b.high >= b.close >= b.low


def test_combine_credit_spread_goes_negative():
    """Long cheap leg + short expensive leg => net credit => negative value."""
    short = [Bar(time=60, open=40, high=41, low=39, close=40, volume=5)]   # 5500P
    long_ = [Bar(time=60, open=22, high=23, low=21, close=22, volume=7)]   # 5400P
    out = combine_barsets(
        [ComboLeg(1, -5), ComboLeg(2, 5)],
        {1: _bs(1, short), 2: _bs(2, long_)},
    )
    # -5*40 + 5*22 = -90
    assert out[0].close == -90.0


def test_combine_forward_fills_sparse_legs():
    """Leg 2 only prints at t=60; leg 1 prints at 60 and 120. At 120 leg 2 is
    forward-filled at its last close, so a combined bar still exists."""
    leg1 = [
        Bar(time=60, open=10, high=10, low=10, close=10, volume=1),
        Bar(time=120, open=12, high=12, low=12, close=12, volume=1),
    ]
    leg2 = [Bar(time=60, open=5, high=5, low=5, close=5, volume=3)]
    out = combine_barsets([ComboLeg(1, 1), ComboLeg(2, 1)], {1: _bs(1, leg1), 2: _bs(2, leg2)})
    assert [b.time for b in out] == [60, 120]
    assert out[0].close == 15.0  # 10 + 5
    assert out[1].close == 17.0  # 12 + 5 (leg2 forward-filled)
    # forward-filled leg contributes no volume at t=120
    assert out[1].volume == 1.0


def test_combine_starts_at_latest_leg_inception():
    """Combo can't be valued before every leg has printed once."""
    leg1 = [Bar(time=60, open=10, high=10, low=10, close=10, volume=1),
            Bar(time=120, open=11, high=11, low=11, close=11, volume=1)]
    leg2 = [Bar(time=120, open=5, high=5, low=5, close=5, volume=1)]  # starts later
    out = combine_barsets([ComboLeg(1, 1), ComboLeg(2, 1)], {1: _bs(1, leg1), 2: _bs(2, leg2)})
    assert [b.time for b in out] == [120]  # t=60 dropped (leg2 not yet priced)


def test_combine_empty_when_a_leg_has_no_bars():
    leg1 = [Bar(time=60, open=10, high=10, low=10, close=10, volume=1)]
    out = combine_barsets([ComboLeg(1, 1), ComboLeg(2, 1)], {1: _bs(1, leg1), 2: _bs(2, [])})
    assert out == []


# ---- API integration (mock provider) -------------------------------------

@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _spx_option_con_ids() -> list[int]:
    return [leg.con_id for leg in _LEGS if leg.underlying == "SPX"]


async def test_combo_history_endpoint(client: httpx.AsyncClient):
    ids = _spx_option_con_ids()
    spec = f"-5@{ids[0]},5@{ids[1]}"
    r = await client.get("/api/combo/history", params={"legs": spec, "bar_size": "1 hour"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPX"
    assert len(body["legs"]) == 2
    assert body["multiplier"] == 100
    assert body["exchange_tz"] == "America/New_York"
    assert body["bars"], "expected combined bars"
    # canonical spec is con_id-sorted (ids[0]=2006 < ids[1]=2007)
    assert body["canonical"] == f"-5@{ids[0]},5@{ids[1]}"


async def test_combo_history_single_leg(client: httpx.AsyncClient):
    con_id = _spx_option_con_ids()[0]
    r = await client.get("/api/combo/history", params={"legs": f"-1@{con_id}", "bar_size": "1 hour"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["legs"]) == 1
    # short single leg -> values negative
    assert all(b["close"] <= 0 for b in body["bars"])


async def test_combo_history_rejects_cross_underlying(client: httpx.AsyncClient):
    spx = _spx_option_con_ids()[0]
    aapl_opt = next(leg.con_id for leg in _LEGS if leg.underlying == "AAPL"
                    and leg.sec_type.value == "OPT")
    r = await client.get("/api/combo/history", params={"legs": f"1@{spx},1@{aapl_opt}"})
    assert r.status_code == 400


async def test_combo_history_rejects_non_option(client: httpx.AsyncClient):
    stk = next(leg.con_id for leg in _LEGS if leg.sec_type.value == "STK")
    r = await client.get("/api/combo/history", params={"legs": f"1@{stk}"})
    assert r.status_code == 400
