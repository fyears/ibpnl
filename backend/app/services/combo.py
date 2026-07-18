# -*- coding: utf-8 -*-
"""Option-combo pricing: parse a combo spec and combine per-leg bars.

A *combo* is a set of option legs, each with a signed integer **ratio** (the
"multiple"): long legs positive, short legs negative. Its value in per-share
price points is

    combo(t) = sum over legs of  ratio_i * price_i(t)

so a net-debit position (you paid premium) reads positive and a net-credit
position (you received premium) reads negative. The change of this series over
time is the position's mark-to-market (in points; multiply by the contract
multiplier for currency). A single-leg "combo" is just ``ratio * price`` and
obeys the same sign rule.

Combining bars is deliberately robust to **sparse option data**: option legs
trade thinly, so their bar timestamps rarely line up. We take the union of all
legs' bar times and, at each timestamp, use a leg's real bar when it has one and
otherwise forward-fill its last known close (a flat, zero-volume point). A
combined bar is emitted only at timestamps where at least one leg actually
printed, and only once every leg has an initial price (the series starts at the
latest leg inception), so we never fabricate a value we can't support.

This module is provider-agnostic: it operates on our own ``BarSet`` models, so
both the mock and the real IB provider feed through it unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import Bar, BarSet


@dataclass(frozen=True)
class ComboLeg:
    """One combo leg: a contract and its signed multiple."""

    con_id: int
    ratio: int


# One leg token: "<signed int>@<con_id>", e.g. "5@2007" or "-5@2006".
_LEG_RE = re.compile(r"^([+-]?\d+)@(\d+)$")


def parse_combo_spec(spec: str) -> list[ComboLeg]:
    """Parse a combo spec string into canonical legs.

    Accepts ``"-5@2006,5@2007"`` (comma-separated ``ratio@con_id`` tokens).
    Duplicate con_ids are merged by summing ratios; zero-ratio legs are dropped.
    Raises ``ValueError`` on a malformed token or an empty result.
    """
    merged: dict[int, int] = {}
    for raw in spec.split(","):
        tok = raw.strip()
        if not tok:
            continue
        m = _LEG_RE.match(tok)
        if not m:
            raise ValueError(f"bad combo leg token: {tok!r}")
        ratio = int(m.group(1))
        con_id = int(m.group(2))
        merged[con_id] = merged.get(con_id, 0) + ratio
    legs = [ComboLeg(con_id=c, ratio=r) for c, r in merged.items() if r != 0]
    if not legs:
        raise ValueError("empty combo (no non-zero legs)")
    return canonical_order(legs)


def canonical_order(legs: list[ComboLeg]) -> list[ComboLeg]:
    """Deterministic leg order (by con_id) so a combo has one canonical form."""
    return sorted(legs, key=lambda leg: leg.con_id)


def canonical_spec(legs: list[ComboLeg]) -> str:
    """Serialize legs to their canonical spec string ``ratio@con_id,...``."""
    return ",".join(f"{leg.ratio}@{leg.con_id}" for leg in canonical_order(legs))


def combine_barsets(legs: list[ComboLeg], barsets: dict[int, BarSet]) -> list[Bar]:
    """Combine per-leg bars into the combo's OHLCV series (see module docstring).

    `barsets` maps con_id -> that leg's BarSet (bars sorted ascending by time),
    all fetched with the same duration / bar size / range so timestamps align.
    Returns an empty list if any leg has no bars in the window.
    """
    series: dict[int, list[Bar]] = {}
    for leg in legs:
        bs = barsets.get(leg.con_id)
        series[leg.con_id] = bs.bars if bs else []

    # A combo needs every leg to have priced at least once in the window.
    if any(not series[leg.con_id] for leg in legs):
        return []

    # Combined series can only start once every leg has an initial price.
    start = max(bars[0].time for bars in series.values())
    times = sorted({b.time for bars in series.values() for b in bars})

    idx: dict[int, int] = {leg.con_id: 0 for leg in legs}
    last: dict[int, Bar | None] = {leg.con_id: None for leg in legs}
    out: list[Bar] = []

    for t in times:
        # Advance each leg's forward-fill pointer to its most recent bar <= t.
        cur: dict[int, tuple[Bar | None, bool]] = {}
        for leg in legs:
            bars = series[leg.con_id]
            i = idx[leg.con_id]
            while i < len(bars) and bars[i].time <= t:
                last[leg.con_id] = bars[i]
                i += 1
            idx[leg.con_id] = i
            lb = last[leg.con_id]
            cur[leg.con_id] = (lb, lb is not None and lb.time == t)

        if t < start:
            continue  # not all legs initialized yet; pointers already advanced

        o = h = lo = c = 0.0
        vol = 0.0
        for leg in legs:
            lb, real = cur[leg.con_id]
            if lb is None:  # unreachable once t >= start, but keeps types honest
                break
            r = leg.ratio
            if real:
                bo, bh, bl, bc, bv = lb.open, lb.high, lb.low, lb.close, lb.volume
            else:
                # forward-filled: a flat point at the last close, no volume
                bo = bh = bl = bc = lb.close
                bv = 0.0
            o += r * bo
            c += r * bc
            # A short leg (r < 0) inverts high/low: the combo is highest when a
            # short leg is at its low, and lowest when it's at its high.
            if r >= 0:
                h += r * bh
                lo += r * bl
            else:
                h += r * bl
                lo += r * bh
            vol += bv
        else:
            out.append(
                Bar(
                    time=t,
                    open=round(o, 4),
                    high=round(h, 4),
                    low=round(lo, 4),
                    close=round(c, 4),
                    volume=vol,
                )
            )
    return out
