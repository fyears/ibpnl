"""Tests for the mock provider: portfolio shape, quotes, greeks, bars."""

import pytest

from app.ib.mock_provider import MockProvider
from app.models import MarketDataType, SecType


@pytest.fixture
async def provider():
    p = MockProvider()
    await p.start()
    yield p
    await p.stop()


async def test_positions_cover_multiple_markets(provider: MockProvider):
    positions = await provider.get_positions()
    currencies = {p.instrument.currency for p in positions}
    sec_types = {p.instrument.sec_type for p in positions}
    assert {"USD", "HKD", "KRW"} <= currencies
    assert {SecType.STK, SecType.FUT, SecType.OPT, SecType.FOP} <= sec_types
    # has both long and short legs
    assert any(p.quantity > 0 for p in positions)
    assert any(p.quantity < 0 for p in positions)


async def test_spx_group_has_multiple_option_legs(provider: MockProvider):
    groups = await provider.get_position_groups()
    spx = next(g for g in groups if g.symbol == "SPX")
    assert spx.leg_count >= 3
    assert all(p.instrument.is_option for p in spx.positions)


async def test_options_have_greeks_and_stocks_do_not(provider: MockProvider):
    positions = await provider.get_positions()
    for p in positions:
        if p.instrument.is_option:
            assert p.greeks is not None
            assert p.greeks.delta is not None
            # call deltas in (0,1), put deltas in (-1,0)
            if p.instrument.right and p.instrument.right.value == "C":
                assert 0 < p.greeks.delta < 1
            else:
                assert -1 < p.greeks.delta < 0
        else:
            assert p.greeks is None


async def test_mixed_market_data_states(provider: MockProvider):
    positions = await provider.get_positions()
    states = {p.quote.market_data_type for p in positions if p.quote}
    # mixed mode must exercise several UI states, including no-permission
    assert MarketDataType.REALTIME in states
    assert MarketDataType.DELAYED in states
    assert MarketDataType.NONE in states


async def test_no_data_position_has_no_last_price(provider: MockProvider):
    positions = await provider.get_positions()
    kr = next(p for p in positions if p.instrument.currency == "KRW")
    assert kr.quote is not None
    assert kr.quote.market_data_type == MarketDataType.NONE
    assert kr.quote.last is None
    # but still has pnl computed off close so the UI can show something
    assert kr.market_value is not None


async def test_account_summary_totals(provider: MockProvider):
    acct = await provider.get_account_summary()
    assert acct.account
    assert acct.net_liquidation and acct.net_liquidation > 0
    assert acct.market_data.note


async def test_history_minute_bars_one_week(provider: MockProvider):
    positions = await provider.get_positions()
    aapl = next(p for p in positions if p.instrument.symbol == "AAPL"
                and p.instrument.sec_type == SecType.STK)
    bars = await provider.get_history(aapl.instrument.con_id)
    assert bars.exchange_tz == "America/New_York"
    assert len(bars.bars) > 500
    # strictly increasing minute timestamps
    times = [b.time for b in bars.bars]
    assert times == sorted(times)
    assert all(t2 - t1 >= 60 for t1, t2 in zip(times, times[1:]))
    # OHLC sanity
    for b in bars.bars[:100]:
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high


async def test_rth_only_produces_fewer_bars(provider: MockProvider):
    positions = await provider.get_positions()
    aapl = next(p for p in positions if p.instrument.symbol == "AAPL"
                and p.instrument.sec_type == SecType.STK)
    all_hours = await provider.get_history(aapl.instrument.con_id, rth_only=False)
    rth = await provider.get_history(aapl.instrument.con_id, rth_only=True)
    assert len(rth.bars) < len(all_hours.bars)


async def test_history_honors_bar_size(provider: MockProvider):
    from app.ib.mock_provider import _UND_BY_SYMBOL

    con_id = _UND_BY_SYMBOL["AAPL"].con_id
    minute = await provider.get_history(con_id, duration="1 W", bar_size="1 min")
    hourly = await provider.get_history(con_id, duration="1 W", bar_size="1 hour")
    # coarser bars -> far fewer of them, spaced ~an hour apart within a session
    assert len(hourly.bars) < len(minute.bars) / 10
    gaps = [
        b2.time - b1.time
        for b1, b2 in zip(hourly.bars, hourly.bars[1:])
        if b2.time - b1.time == 3600
    ]
    assert gaps  # at least some adjacent hourly bars


async def test_history_end_returns_older_contiguous_window(provider: MockProvider):
    from app.ib.mock_provider import _UND_BY_SYMBOL

    con_id = _UND_BY_SYMBOL["AAPL"].con_id
    recent = await provider.get_history(con_id, duration="1 W", bar_size="1 hour")
    older = await provider.get_history(
        con_id, duration="1 W", bar_size="1 hour", end=recent.bars[0].time
    )
    assert older.bars, "expected an older window"
    assert older.bars[-1].time < recent.bars[0].time  # strictly before
    # deterministic: identical request yields identical bars (so lazy-loaded
    # chunks line up seamlessly)
    again = await provider.get_history(
        con_id, duration="1 W", bar_size="1 hour", end=recent.bars[0].time
    )
    assert [b.time for b in older.bars] == [b.time for b in again.bars]
    assert [b.close for b in older.bars] == [b.close for b in again.bars]


async def test_late_close_straddle_bar_reads_regular(provider: MockProvider):
    """LCLZ closes 16:30, so the hourly 16:00 bar (16:00-17:00) is regular."""
    import datetime
    from zoneinfo import ZoneInfo

    from app.ib.mock_provider import _UND_BY_SYMBOL

    con_id = _UND_BY_SYMBOL["LCLZ"].con_id
    bs = await provider.get_history(con_id, duration="1 W", bar_size="1 hour")
    et = ZoneInfo("America/New_York")
    dur = min(
        b2.time - b1.time
        for b1, b2 in zip(bs.bars, bs.bars[1:])
        if b2.time - b1.time > 0
    )

    def in_regular(t: int) -> bool:  # mirrors the frontend span-overlap rule
        end = t + dur
        return any(t < w.end and end > w.start for w in bs.sessions)

    def bar_at(day: str, hh: int) -> int | None:
        for b in bs.bars:
            lt = datetime.datetime.fromtimestamp(b.time, et)
            if lt.strftime("%Y-%m-%d") == day and lt.hour == hh and lt.minute == 0:
                return b.time
        return None

    # pick the most recent weekday that has a 16:00 bar
    day = None
    for b in reversed(bs.bars):
        lt = datetime.datetime.fromtimestamp(b.time, et)
        if lt.weekday() < 5 and lt.hour == 16:
            day = lt.strftime("%Y-%m-%d")
            break
    assert day is not None

    b16 = bar_at(day, 16)
    b17 = bar_at(day, 17)
    assert b16 is not None and in_regular(b16), "16:00 bar must be regular (closes 16:30)"
    if b17 is not None:
        assert not in_regular(b17), "17:00 bar is after the 16:30 close -> ext"


async def test_streaming_emits_quotes(provider: MockProvider):
    import asyncio

    received: list = []

    async def on_quote(q):
        received.append(q)

    provider.on_quote(on_quote)
    positions = await provider.get_positions()
    await provider.subscribe([positions[0].instrument.con_id])
    await asyncio.sleep(2.5)
    assert received, "expected at least one streamed quote within 2.5s"
