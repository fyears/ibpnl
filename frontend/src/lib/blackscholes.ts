// TypeScript port of backend/app/services/blackscholes.py
// European Black-Scholes pricing for the frontend payoff-curve calculator.
// Mirrors the backend function exactly so results are consistent.

/**
 * Standard normal CDF — Abramowitz & Stegun 26.2.17 (6-term rational).
 * Max absolute error < 7.5e-8. Matches Python's math.erf(x/sqrt(2)) route
 * to the precision needed for option pricing.
 */
function normCdf(x: number): number {
  const b = [0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429];
  const k = 1.0 / (1.0 + 0.2316419 * Math.abs(x));
  const poly = ((((b[4] * k + b[3]) * k + b[2]) * k + b[1]) * k + b[0]) * k;
  const phi = poly * Math.exp(-0.5 * x * x) / Math.sqrt(2.0 * Math.PI);
  return x >= 0 ? 1.0 - phi : phi;
}

/** Standard normal PDF. */
function normPdf(x: number): number {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2.0 * Math.PI);
}

export interface BsResult {
  price: number;
  delta: number;
  gamma: number;
  vega: number;
  theta: number; // per calendar day
}

/**
 * Price and greeks for a European option (Black-Scholes).
 *
 * @param spot       Current underlying price
 * @param strike     Option strike
 * @param tYears     Time to expiry in years (clamped to ≥ 1e-6)
 * @param vol        Implied volatility (fraction, e.g. 0.23)
 * @param rate       Risk-free rate (default 0.045 to match backend)
 * @param isCall     true = call, false = put
 */
export function bsPriceGreeks(
  spot: number,
  strike: number,
  tYears: number,
  vol: number,
  isCall: boolean,
  rate = 0.045,
): BsResult {
  const t = Math.max(tYears, 1e-6);
  const v = Math.max(vol, 1e-4);
  const sqrtT = Math.sqrt(t);
  const d1 = (Math.log(spot / strike) + (rate + 0.5 * v * v) * t) / (v * sqrtT);
  const d2 = d1 - v * sqrtT;

  let price: number;
  let delta: number;
  let thetaYear: number;

  if (isCall) {
    price = spot * normCdf(d1) - strike * Math.exp(-rate * t) * normCdf(d2);
    delta = normCdf(d1);
    thetaYear =
      -(spot * normPdf(d1) * v) / (2 * sqrtT) -
      rate * strike * Math.exp(-rate * t) * normCdf(d2);
  } else {
    price = strike * Math.exp(-rate * t) * normCdf(-d2) - spot * normCdf(-d1);
    delta = normCdf(d1) - 1.0;
    thetaYear =
      -(spot * normPdf(d1) * v) / (2 * sqrtT) +
      rate * strike * Math.exp(-rate * t) * normCdf(-d2);
  }

  const gamma = normPdf(d1) / (spot * v * sqrtT);
  const vega = spot * normPdf(d1) * sqrtT * 0.01; // per 1 vol point
  const theta = thetaYear / 365.0; // per calendar day

  return {
    price: Math.max(price, 0.0),
    delta,
    gamma,
    vega,
    theta,
  };
}

/**
 * Option price only — faster than the full greeks calculation when only
 * pricing is needed (payoff curve loops over many spot values).
 */
export function bsPrice(
  spot: number,
  strike: number,
  tYears: number,
  vol: number,
  isCall: boolean,
  rate = 0.045,
): number {
  const t = Math.max(tYears, 1e-6);
  const v = Math.max(vol, 1e-4);
  const sqrtT = Math.sqrt(t);
  const d1 = (Math.log(spot / strike) + (rate + 0.5 * v * v) * t) / (v * sqrtT);
  const d2 = d1 - v * sqrtT;
  if (isCall) {
    return Math.max(
      spot * normCdf(d1) - strike * Math.exp(-rate * t) * normCdf(d2),
      0,
    );
  }
  return Math.max(
    strike * Math.exp(-rate * t) * normCdf(-d2) - spot * normCdf(-d1),
    0,
  );
}
