"""Tests for trading-hours parsing / session-window expansion.

Regression coverage for the ext-hours shading bug: IB's liquidHours string
only spans a rolling, arbitrary handful of days, so expand_windows must not
assume any particular weekday is present in the sample.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import MarketSession
from app.services.trading_hours import (
    classify_session,
    expand_windows,
    parse_hours,
    regular_windows,
)

ET = ZoneInfo("US/Eastern")
CT = ZoneInfo("US/Central")


def _et(y: int, m: int, d: int, hh: int, mm: int) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


# A real HOOD liquidHours string captured on a Saturday: IB returns only the
# upcoming Mon–Thu — no Friday, no past days.
HOOD_WEEKEND_LIQUID = (
    "20260718:CLOSED;20260719:CLOSED;"
    "20260720:0930-20260720:1600;20260721:0930-20260721:1600;"
    "20260722:0930-20260722:1600;20260723:0930-20260723:1600"
)


def test_parse_hours_skips_closed() -> None:
    windows = parse_hours(HOOD_WEEKEND_LIQUID, "US/Eastern")
    assert len(windows) == 4  # the two CLOSED days are dropped
    first = datetime.fromtimestamp(windows[0].start, ET)
    assert (first.hour, first.minute) == (9, 30)


def test_expand_windows_covers_weekdays_absent_from_sample() -> None:
    """Fridays (and any weekday IB omitted) must still get a regular session."""
    lo, hi = _et(2026, 6, 18, 4, 0), _et(2026, 7, 17, 20, 0)
    windows = expand_windows(HOOD_WEEKEND_LIQUID, "US/Eastern", lo, hi)

    starts = [datetime.fromtimestamp(w.start, ET) for w in windows]
    weekdays = {d.weekday() for d in starts}
    assert weekdays == {0, 1, 2, 3, 4}, "expected Mon–Fri, incl. Friday (4)"

    fridays = sorted(d.strftime("%m-%d") for d in starts if d.weekday() == 4)
    assert fridays == ["06-19", "06-26", "07-03", "07-10", "07-17"]

    # Every window is a full 09:30–16:00 RTH block; no weekend windows.
    for w in windows:
        s = datetime.fromtimestamp(w.start, ET)
        e = datetime.fromtimestamp(w.end, ET)
        assert (s.hour, s.minute) == (9, 30)
        assert (e.hour, e.minute) == (16, 0)
        assert s.weekday() < 5


def test_expand_windows_classifies_historical_friday() -> None:
    """A bar on an omitted Friday classifies pre/regular/post correctly."""
    lo, hi = _et(2026, 6, 18, 4, 0), _et(2026, 7, 17, 20, 0)
    windows = expand_windows(HOOD_WEEKEND_LIQUID, "US/Eastern", lo, hi)

    def in_regular(ts: int) -> bool:
        return any(w.start <= ts < w.end for w in windows)

    # Friday 2026-07-10
    assert not in_regular(_et(2026, 7, 10, 8, 0))  # pre-market -> shaded
    assert in_regular(_et(2026, 7, 10, 10, 0))  # regular -> not shaded
    assert not in_regular(_et(2026, 7, 10, 17, 0))  # after-hours -> shaded


# Real ES liquidHours captured on a Saturday. RTH is 08:30-16:00 CT on each
# sampled weekday, and IB tacks its overnight electronic block onto the LAST
# sampled day (Thu): 20260723:1700-20260724:1600 (Thu 17:00 -> Fri 16:00, ~23h).
# That block is NOT a regular session.
ES_LIQUID = (
    "20260718:CLOSED;20260719:CLOSED;"
    "20260720:0830-20260720:1600;20260721:0830-20260721:1600;"
    "20260722:0830-20260722:1600;20260723:0830-20260723:1600;"
    "20260723:1700-20260724:1600"
)


def test_regular_windows_drops_overnight_block() -> None:
    """The ~23h overnight segment IB folds into liquidHours is dropped."""
    windows = regular_windows(ES_LIQUID, "US/Central")
    assert len(windows) == 4  # 4 daytime RTH windows; overnight block gone
    for w in windows:
        s = datetime.fromtimestamp(w.start, CT)
        e = datetime.fromtimestamp(w.end, CT)
        assert (s.hour, s.minute) == (8, 30)
        assert (e.hour, e.minute) == (16, 0)
        assert e.date() == s.date()  # never crosses exchange-local midnight


def test_expand_windows_overnight_bar_is_not_regular() -> None:
    """Regression: 2am-ET on the overnight span must shade, not read regular.

    IB attributes the overnight block to whatever weekday ends its rolling
    sample (here Thu), so expand_windows would otherwise replay a bogus 23h
    'regular' window every Thu that swallows the following Fri morning.
    """
    lo, hi = _et(2026, 7, 13, 0, 0), _et(2026, 7, 18, 0, 0)
    windows = expand_windows(ES_LIQUID, "US/Central", lo, hi)

    def in_regular(ts: int) -> bool:
        return any(w.start <= ts < w.end for w in windows)

    # No emitted window spans local midnight or runs implausibly long.
    for w in windows:
        s = datetime.fromtimestamp(w.start, CT)
        e = datetime.fromtimestamp(w.end, CT)
        assert e.date() == s.date()
        assert w.end - w.start <= 14 * 3600

    # Fri 2026-07-17 02:00 ET (overnight, = 01:00 CT) -> shaded, NOT regular.
    assert not in_regular(_et(2026, 7, 17, 2, 0))
    # RTH on the same Friday and the prior Thursday still classify as regular.
    assert in_regular(_et(2026, 7, 17, 11, 0))
    assert in_regular(_et(2026, 7, 16, 11, 0))


def test_expand_windows_keeps_same_day_weekend_session() -> None:
    """A weekend day with a real DAYTIME session (e.g. Tadawul, Sun) is kept."""
    spec = (
        "20260712:1000-20260712:1500;"  # Sun 10:00-15:00 (same-day RTH)
        "20260713:1000-20260713:1500"  # Mon 10:00-15:00
    )
    lo, hi = _et(2026, 7, 5, 0, 0), _et(2026, 7, 12, 23, 0)
    windows = expand_windows(spec, "US/Eastern", lo, hi)
    weekdays = {datetime.fromtimestamp(w.start, ET).weekday() for w in windows}
    assert 6 in weekdays  # Sunday retained


def test_expand_windows_drops_overnight_only_weekend_block() -> None:
    """A spans-midnight-only sample yields no regular windows (all shaded)."""
    spec = (
        "20260719:1800-20260720:1700;"  # Sun 18:00 -> Mon 17:00 (overnight)
        "20260720:1800-20260721:1700"
    )
    lo, hi = _et(2026, 7, 5, 0, 0), _et(2026, 7, 21, 23, 0)
    assert expand_windows(spec, "US/Eastern", lo, hi) == []


def test_classify_session_uses_live_windows() -> None:
    regular = parse_hours(
        "20260717:0930-20260717:1600", "US/Eastern"
    )
    extended = parse_hours(
        "20260717:0400-20260717:2000", "US/Eastern"
    )
    assert (
        classify_session(_et(2026, 7, 17, 10, 0), regular, extended)
        == MarketSession.REGULAR
    )
    assert (
        classify_session(_et(2026, 7, 17, 8, 0), regular, extended)
        == MarketSession.PRE
    )
    assert (
        classify_session(_et(2026, 7, 17, 17, 0), regular, extended)
        == MarketSession.POST
    )
    assert (
        classify_session(_et(2026, 7, 17, 23, 0), regular, extended)
        == MarketSession.CLOSED
    )
