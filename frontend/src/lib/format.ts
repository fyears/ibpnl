// Number and label formatting helpers. All display formatting lives here so
// the terminal "numeric voice" is consistent everywhere.

import type { Instrument, MarketDataType } from "../api/types";

const nf2 = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const nf0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** Price with sensible precision for its magnitude. */
export function fmtPrice(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (Math.abs(v) >= 10000) return nf0.format(v);
  if (Math.abs(v) >= 1000) return nf2.format(v);
  if (Math.abs(v) < 1) return v.toFixed(4);
  return nf2.format(v);
}

/** Money with currency prefix, no sign forcing. */
export function fmtMoney(v: number | null | undefined, currency = "USD"): string {
  if (v == null || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  const body = abs >= 100000 ? nf0.format(v) : nf2.format(v);
  return `${currencySymbol(currency)}${body}`;
}

/** Signed PnL: +$1,234.56 / -$987.65. */
export function fmtPnl(v: number | null | undefined, currency = "USD"): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  const abs = Math.abs(v);
  const body = abs >= 100000 ? nf0.format(abs) : nf2.format(abs);
  return `${sign}${currencySymbol(currency)}${body}`;
}

export function pnlClass(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return "flat";
  return v > 0 ? "gain" : "loss";
}

export function fmtQty(v: number): string {
  return Number.isInteger(v) ? nf0.format(v) : String(v);
}

/** Bid/ask size — usually small integers; round fractional IB sizes. */
export function fmtSize(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (Math.abs(v) >= 10000) return fmtVolume(v);
  return nf0.format(Math.round(v));
}

export function fmtGreek(v: number | null | undefined, digits = 3): string {
  if (v == null || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export function fmtIv(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

export function fmtVolume(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return nf0.format(v);
}

function currencySymbol(currency: string): string {
  switch (currency) {
    case "USD": return "$";
    case "HKD": return "HK$";
    case "KRW": return "₩";
    case "JPY": return "¥";
    case "EUR": return "€";
    case "GBP": return "£";
    default: return `${currency} `;
  }
}

/** "20260816" -> "16 Aug 26" */
export function fmtExpiry(expiry: string | null): string {
  if (!expiry || expiry.length < 6) return expiry ?? "";
  const y = expiry.slice(0, 4);
  const m = Number(expiry.slice(4, 6));
  const d = expiry.length >= 8 ? expiry.slice(6, 8) : "";
  const months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return d ? `${Number(d)} ${months[m]} ${y.slice(2)}` : `${months[m]} ${y.slice(2)}`;
}

/** Compact one-line leg label, e.g. "5500 P · 26 Jul 26" or "FUT · Sep 26". */
export function legLabel(inst: Instrument): string {
  if (inst.sec_type === "OPT" || inst.sec_type === "FOP") {
    const r = inst.right === "C" ? "C" : "P";
    return `${fmtPrice(inst.strike)} ${r} · ${fmtExpiry(inst.expiry)}`;
  }
  if (inst.sec_type === "FUT") {
    return `FUT · ${fmtExpiry(inst.expiry)}`;
  }
  return inst.sec_type;
}

export function mdtLabel(t: MarketDataType): string {
  switch (t) {
    case "realtime": return "LIVE";
    case "delayed": return "DELAYED";
    case "frozen": return "FROZEN";
    case "delayed_frozen": return "DLY·FRZ";
    case "none": return "NO DATA";
  }
}

export function assetClassLabel(ac: string): string {
  const map: Record<string, string> = {
    us_stock: "US Stock",
    us_future: "US Future",
    us_option: "US Option",
    us_index: "US Index",
    us_index_option: "US Index Opt",
    us_future_option: "US Fut Opt",
    hk_stock: "HK Stock",
    hk_option: "HK Option",
    kr_stock: "KR Stock",
    kr_option: "KR Option",
    other: "Other",
  };
  return map[ac] ?? ac;
}
