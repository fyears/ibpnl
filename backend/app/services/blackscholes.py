"""Minimal Black-Scholes pricing/greeks, used by the mock provider so that
simulated option prices and Greeks move coherently with the underlying.

Not used by the real IB provider (IB supplies its own model Greeks).
"""

from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price_greeks(
    *,
    spot: float,
    strike: float,
    t_years: float,
    vol: float,
    rate: float = 0.045,
    is_call: bool,
) -> dict[str, float]:
    """Return price + per-share greeks for a European option.

    Greeks are per 1.0 change (delta), per year (theta scaled to per-day below),
    per 1 vol point for vega (i.e. vega * 0.01).
    """
    t = max(t_years, 1e-6)
    vol = max(vol, 1e-4)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t

    if is_call:
        price = spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta_year = (
            -(spot * _norm_pdf(d1) * vol) / (2 * sqrt_t)
            - rate * strike * math.exp(-rate * t) * _norm_cdf(d2)
        )
    else:
        price = strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1.0
        theta_year = (
            -(spot * _norm_pdf(d1) * vol) / (2 * sqrt_t)
            + rate * strike * math.exp(-rate * t) * _norm_cdf(-d2)
        )

    gamma = _norm_pdf(d1) / (spot * vol * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t * 0.01  # per 1 vol point
    theta = theta_year / 365.0  # per calendar day

    return {
        "price": max(price, 0.0),
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "iv": vol,
    }
