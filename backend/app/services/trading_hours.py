"""Parsing of IB `tradingHours` / `liquidHours` strings into UTC windows.

IB reports hours per contract in the exchange-local timezone, e.g.::

    20260717:0400-20260717:2000;20260718:CLOSED;20260720:0930-20260720:1600

`tradingHours` covers the full (extended) session; `liquidHours` covers only
regular / RTH. We convert both to lists of UTC [start, end) epoch-second windows
and use them to (a) classify the current market session and (b) shade the
non-regular regions on the chart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import MarketSession, SessionWindow


def _safe_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def parse_hours(spec: str, tz_name: str) -> list[SessionWindow]:
    """Parse an IB hours string into UTC [start, end) windows.

    Unknown/blank specs and CLOSED days yield no window.
    """
    if not spec:
        return []
    tz = _safe_tz(tz_name)
    windows: list[SessionWindow] = []
    for token in spec.split(";"):
        token = token.strip()
        if not token or "CLOSED" in token.upper():
            continue
        # "YYYYMMDD:HHMM-YYYYMMDD:HHMM"
        try:
            left, right = token.split("-")
            sd, st = left.split(":")
            ed, et = right.split(":")
            start = _to_utc(sd, st, tz)
            end = _to_utc(ed, et, tz)
        except (ValueError, KeyError):
            continue
        if end > start:
            windows.append(SessionWindow(start=start, end=end))
    return windows


def _to_utc(date_str: str, hm: str, tz: ZoneInfo) -> int:
    y, m, d = int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8])
    hh, mm = int(hm[0:2]), int(hm[2:4])
    local = datetime(y, m, d, hh, mm, tzinfo=tz)
    return int(local.timestamp())


# A regular / RTH session is a daytime block inside one exchange-local day.
# Anything longer is IB folding the overnight electronic session into
# `liquidHours` (see below); 14h clears the longest real cash session while
# rejecting the ~23h overnight block.
_MAX_REGULAR_SECONDS = 14 * 3600


def regular_windows(spec: str, tz_name: str) -> list[SessionWindow]:
    """Like `parse_hours`, but for RTH/`liquidHours` — drops overnight blocks.

    IB tacks the full overnight electronic session onto the LAST day of its
    rolling `liquidHours` sample, e.g. ES returns
    ``...;20260723:0830-20260723:1600;20260723:1700-20260724:1600`` — that final
    ~23h segment (Thu 17:00 -> Fri 16:00) is the trading session, not a regular
    daytime one. Left in, it makes overnight bars read as regular hours (no
    pre/post shading) and misclassifies the session badge. A genuine RTH window
    sits within a single exchange-local calendar day, so drop any window that
    crosses local midnight or runs implausibly long. `tradingHours` (the
    extended session) legitimately spans midnight and must NOT be filtered — use
    `parse_hours` for that.
    """
    tz = _safe_tz(tz_name)
    out: list[SessionWindow] = []
    for w in parse_hours(spec, tz_name):
        start_local = datetime.fromtimestamp(w.start, tz)
        end_local = datetime.fromtimestamp(w.end, tz)
        if end_local.date() > start_local.date():
            continue  # crosses exchange-local midnight -> overnight block
        if w.end - w.start > _MAX_REGULAR_SECONDS:
            continue  # implausibly long for an RTH day
        out.append(w)
    return out


def expand_windows(
    spec: str,
    tz_name: str,
    lo: int,
    hi: int,
) -> list[SessionWindow]:
    """Regular-session windows covering the whole [lo, hi] epoch-second range.

    IB's `liquidHours` / `tradingHours` strings only span a rolling handful of
    days around "now" — and *which* days is arbitrary. On a weekend, HOOD's
    liquidHours lists only the upcoming Mon–Thu; on other days it may show a
    different, equally incomplete slice. So we must NOT assume any particular
    weekday is present.

    Instead we learn the recurring intraday RTH interval(s) — open/close
    *time-of-day* — from whatever days IB does give, take the modal pattern, and
    replicate it across every trading day in [lo, hi]. Trading days are Mon–Fri
    plus any weekend day IB explicitly shows a daytime session. This keeps
    ext-hours shading correct for all historical bars, including weekdays IB
    never listed.

    We parse via `regular_windows`, which drops IB's overnight electronic blocks
    (the ~23h span-midnight segment IB tacks onto the last sampled day). Left in,
    that block would be learned as a recurring weekday session and replayed —
    making every overnight span (e.g. Thu-night into Fri) read as regular hours.
    """
    parsed = regular_windows(spec, tz_name)
    if not parsed:
        return []
    tz = _safe_tz(tz_name)

    Interval = tuple[int, int, bool]  # (open_minute, close_minute, spans_midnight)

    # Learn the distinct intraday interval set for each weekday that appears.
    by_weekday: dict[int, list[Interval]] = {}
    for w in parsed:
        start_local = datetime.fromtimestamp(w.start, tz)
        end_local = datetime.fromtimestamp(w.end, tz)
        wd = start_local.weekday()
        sig: Interval = (
            start_local.hour * 60 + start_local.minute,
            end_local.hour * 60 + end_local.minute,
            end_local.date() > start_local.date(),
        )
        ivs = by_weekday.setdefault(wd, [])
        if sig not in ivs:
            ivs.append(sig)
    if not by_weekday:
        return []

    # Modal interval set: the day-shape that occurs on the most sampled days.
    # Used for weekdays IB didn't include in its short window.
    tally: dict[tuple[Interval, ...], int] = {}
    for ivs in by_weekday.values():
        key = tuple(sorted(ivs))
        tally[key] = tally.get(key, 0) + 1
    modal = list(max(tally.items(), key=lambda kv: kv[1])[0])

    # Trading weekdays: Mon–Fri by default, plus any weekend day IB shows open.
    trading = set(range(5)) | {wd for wd in by_weekday if wd >= 5}

    def intervals_for(weekday: int) -> list[Interval]:
        if weekday in by_weekday:  # IB gave this exact weekday — trust it
            return by_weekday[weekday]
        if weekday in trading:  # weekday IB omitted — use the modal shape
            return modal
        return []

    # Walk every calendar day in range (pad by one on each side for midnight
    # spans and tz edges) and emit each trading day's windows.
    lo_date = datetime.fromtimestamp(lo, tz).date() - timedelta(days=1)
    hi_date = datetime.fromtimestamp(hi, tz).date() + timedelta(days=1)
    out: list[SessionWindow] = []
    day = lo_date
    while day <= hi_date:
        for open_min, close_min, spans_midnight in intervals_for(day.weekday()):
            start_dt = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(
                minutes=open_min
            )
            end_day = day + timedelta(days=1) if spans_midnight else day
            end_dt = datetime(
                end_day.year, end_day.month, end_day.day, tzinfo=tz
            ) + timedelta(minutes=close_min)
            start = int(start_dt.timestamp())
            end = int(end_dt.timestamp())
            if end > start and end >= lo and start <= hi:
                out.append(SessionWindow(start=start, end=end))
        day += timedelta(days=1)
    out.sort(key=lambda w: w.start)
    return out


def classify_session(
    now_utc: float,
    regular: list[SessionWindow],
    extended: list[SessionWindow],
) -> MarketSession:
    """Classify the current instant relative to regular/extended windows."""
    now = int(now_utc)
    in_regular = any(w.start <= now < w.end for w in regular)
    if in_regular:
        return MarketSession.REGULAR
    ext = next((w for w in extended if w.start <= now < w.end), None)
    if ext is None:
        if not regular and not extended:
            return MarketSession.UNKNOWN
        return MarketSession.CLOSED
    # Inside an extended window but not regular: pre if before that day's regular
    # open, post if after its close. Find the regular window sharing this day.
    same_day_reg = [
        w for w in regular if w.start >= ext.start and w.end <= ext.end
    ]
    if same_day_reg:
        reg = same_day_reg[0]
        if now < reg.start:
            return MarketSession.PRE
        if now >= reg.end:
            return MarketSession.POST
    # extended window with no matching regular block (e.g. futures) -> regular-ish
    return MarketSession.REGULAR
