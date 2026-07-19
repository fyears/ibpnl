// P&L payoff curve analysis — Plotly + Black-Scholes.
//
// Draws the combo's profit/loss across underlying price for several
// time-to-expiry slices: today's curve, N faint time-decay "ghost" curves, a
// slider-selected highlight that converges onto the at-expiry hockey-stick,
// plus a live vertical spot line, strike lines and a zero reference line.
//
// Every curve is priced with Black-Scholes (lib/blackscholes.ts) — the shape
// is computed, never faked. Plotly gives us native fills and unified hover
// tooltips (underlying price + P&L for today / selected day / expiry).

import Plotly from "plotly.js";
import type { ComboLegInfo } from "../api/types";
import { bsPrice } from "../lib/blackscholes";

// ── constants ──────────────────────────────────────────────────────────────

const N_GHOST = 4;      // intermediate time-decay ghost curves
const N_PTS   = 160;    // x-axis resolution
const DEF_IV  = 0.25;   // fallback IV when greeks not yet received
const RATE    = 0.045;  // risk-free rate (mirrors backend)

// design-token colors (light mode)
const C_TODAY  = "#16508f"; // accent blue
const C_SEL    = "#f5a623"; // amber
const C_EXPIRY = "#c93b36"; // down/red
const C_GHOST  = "rgba(22,80,143,0.22)";
const C_MUTED  = "#5d6673";

export type CostBasis = "mark" | "position";

// ── helpers ──────────────────────────────────────────────────────────────

function dteDays(expiry: string): number {
  const y = +expiry.slice(0, 4), m = +expiry.slice(4, 6) - 1, d = +expiry.slice(6, 8);
  return Math.max(0, Math.ceil((Date.UTC(y, m, d) - Date.now()) / 86_400_000));
}

// ── class ────────────────────────────────────────────────────────────────

export class PayoffChart {
  private readonly host: HTMLElement;
  private readonly legs: ComboLegInfo[];
  private readonly mult: number;         // shared contract multiplier
  private readonly nearestDte: number;   // calendar days to nearest expiry
  private readonly ghostDays: number[];  // intermediate curve days
  private readonly xArr: number[];       // underlying price grid

  // live per-leg state
  private legIv   = new Map<number, number>(); // implied vol
  private legMark = new Map<number, number>(); // current option price (mark basis)
  private legAvg  = new Map<number, number>(); // avg fill price (position basis)

  private spot: number | null = null;
  private costBasis: CostBasis = "mark";
  private daysElapsed = 0;
  private built = false;
  private currency = "USD";

  constructor(host: HTMLElement, legs: ComboLegInfo[]) {
    this.host = host;
    this.legs = legs;
    this.mult = legs[0]?.instrument.multiplier ?? 1;
    this.currency = legs[0]?.instrument.currency ?? "USD";

    // slider max = nearest leg expiry
    const dtes = legs
      .filter((l) => l.instrument.expiry)
      .map((l) => dteDays(l.instrument.expiry!));
    this.nearestDte = dtes.length ? Math.min(...dtes) : 0;

    // N_GHOST evenly-spaced intermediate days (between today and nearest expiry)
    this.ghostDays = [];
    if (this.nearestDte > 1) {
      for (let k = 1; k <= N_GHOST; k++) {
        const d = Math.round((k / (N_GHOST + 1)) * this.nearestDte);
        this.ghostDays.push(Math.max(1, Math.min(d, this.nearestDte - 1)));
      }
    }

    // x-axis: strike range ± 22%
    const strikes = legs.map((l) => l.instrument.strike ?? 0).filter(Boolean);
    const sMin = Math.min(...strikes), sMax = Math.max(...strikes);
    const center = (sMin + sMax) / 2 || 100;
    const pad = center * 0.22;
    const lo = Math.max(sMin - pad, 1), hi = sMax + pad;
    this.xArr = Array.from({ length: N_PTS }, (_, i) => lo + (i / (N_PTS - 1)) * (hi - lo));
  }

  // ── P&L computation ──────────────────────────────────────────────────────

  private entryCost(): number {
    let cost = 0;
    for (const leg of this.legs) {
      const cid = leg.instrument.con_id;
      const px = this.costBasis === "position"
        ? (this.legAvg.get(cid) ?? this.legMark.get(cid) ?? 0)
        : (this.legMark.get(cid) ?? 0);
      cost += leg.ratio * px;
    }
    return cost;
  }

  /** P&L (in currency) across the x grid at `days` elapsed from today. */
  private computeSeries(days: number): number[] {
    const cost = this.entryCost();
    const mult = this.mult;
    return this.xArr.map((s) => {
      let combo = 0;
      for (const leg of this.legs) {
        const inst = leg.instrument;
        if (!inst.strike || !inst.expiry || !inst.right) continue;
        const iv  = this.legIv.get(inst.con_id) ?? DEF_IV;
        const rem = Math.max(dteDays(inst.expiry) - days, 0);
        const px = rem <= 0
          ? (inst.right === "C"
            ? Math.max(s - inst.strike, 0)
            : Math.max(inst.strike - s, 0))
          : bsPrice(s, inst.strike, rem / 365, iv, inst.right === "C", RATE);
        combo += leg.ratio * px;
      }
      return (combo - cost) * mult;
    });
  }

  // ── Plotly construction ────────────────────────────────────────────────

  private traces(): Partial<Plotly.PlotData>[] {
    const x = this.xArr;
    const out: Partial<Plotly.PlotData>[] = [];

    // ghost decay curves (faint, no hover — context only)
    for (const gd of this.ghostDays) {
      out.push({
        x, y: this.computeSeries(gd),
        mode: "lines", type: "scatter",
        line: { color: C_GHOST, width: 1 },
        hoverinfo: "skip",
        showlegend: false,
        name: `+${gd}d`,
      });
    }

    // today (blue)
    out.push({
      x, y: this.computeSeries(0),
      mode: "lines", type: "scatter",
      line: { color: C_TODAY, width: 2 },
      name: "Today",
      hovertemplate: "Today: %{y:,.0f}<extra></extra>",
    });

    // at-expiry hockey-stick (red dashed)
    out.push({
      x, y: this.computeSeries(this.nearestDte),
      mode: "lines", type: "scatter",
      line: { color: C_EXPIRY, width: 1.5, dash: "dash" },
      name: "At expiry",
      hovertemplate: "At expiry: %{y:,.0f}<extra></extra>",
    });

    // slider-selected curve (amber, bold) — drawn last so it sits on top
    out.push({
      x, y: this.computeSeries(this.daysElapsed),
      mode: "lines", type: "scatter",
      line: { color: C_SEL, width: 2.5 },
      name: this.daysElapsed === 0 ? "Today (selected)" : `+${this.daysElapsed}d`,
      hovertemplate: `+${this.daysElapsed}d: %{y:,.0f}<extra></extra>`,
    });

    return out;
  }

  /** Vertical strike/spot lines + horizontal zero line as Plotly shapes. */
  private shapes(): Partial<Plotly.Shape>[] {
    const shapes: Partial<Plotly.Shape>[] = [
      // zero P&L reference
      {
        type: "line", xref: "paper", yref: "y",
        x0: 0, x1: 1, y0: 0, y1: 0,
        line: { color: C_MUTED, width: 1, dash: "dot" },
        layer: "below",
      },
    ];
    // strike verticals
    const seen = new Set<number>();
    for (const leg of this.legs) {
      const k = leg.instrument.strike;
      if (!k || seen.has(k)) continue;
      seen.add(k);
      shapes.push({
        type: "line", xref: "x", yref: "paper",
        x0: k, x1: k, y0: 0, y1: 1,
        line: { color: "rgba(93,102,115,0.35)", width: 1, dash: "dot" },
        layer: "below",
      });
    }
    // live spot line
    if (this.spot != null) {
      shapes.push({
        type: "line", xref: "x", yref: "paper",
        x0: this.spot, x1: this.spot, y0: 0, y1: 1,
        line: { color: C_TODAY, width: 1.5 },
        layer: "below",
      });
    }
    return shapes;
  }

  private annotations(): Partial<Plotly.Annotations>[] {
    if (this.spot == null) return [];
    return [{
      x: this.spot, xref: "x", yref: "paper", y: 1,
      text: `Spot ${this.spot.toFixed(0)}`,
      showarrow: false, font: { size: 10, color: C_TODAY },
      bgcolor: "rgba(255,255,255,0.75)",
      xanchor: "left", yanchor: "bottom",
    }];
  }

  private layout(): Partial<Plotly.Layout> {
    return {
      margin: { l: 64, r: 12, t: 10, b: 40 },
      height: 340,
      paper_bgcolor: "#ffffff",
      plot_bgcolor: "#ffffff",
      font: { family: "Segoe UI, system-ui, sans-serif", size: 11, color: "#131820" },
      xaxis: {
        title: { text: "Underlying price", font: { size: 11, color: C_MUTED } },
        gridcolor: "#eef1f5", zeroline: false, tickformat: ",d",
      },
      yaxis: {
        title: { text: `P&L (${this.currency})`, font: { size: 11, color: C_MUTED } },
        gridcolor: "#eef1f5", zeroline: false, tickformat: ",d",
      },
      hovermode: "x unified",
      hoverlabel: { bgcolor: "#ffffff", bordercolor: "#e2e6ec", font: { size: 11 } },
      shapes: this.shapes(),
      annotations: this.annotations(),
      showlegend: false,
      dragmode: false,
    };
  }

  private readonly config: Partial<Plotly.Config> = {
    displayModeBar: false,
    responsive: true,
    scrollZoom: false,
    doubleClick: false,
  };

  private render(): void {
    if (!this.built) {
      void Plotly.newPlot(this.host, this.traces(), this.layout(), this.config);
      this.built = true;
    } else {
      void Plotly.react(this.host, this.traces(), this.layout(), this.config);
    }
  }

  // ── public API ─────────────────────────────────────────────────────────

  /** Draw the chart for the first time. Call once the host has a real width. */
  mount(): void {
    this.render();
  }

  setIv(conId: number, iv: number): void {
    this.legIv.set(conId, iv);
    if (this.built) this.render();
  }

  setMarkPrice(conId: number, price: number): void {
    this.legMark.set(conId, price);
    if (this.built) this.render();
  }

  setAvgPrice(conId: number, avgPrice: number): void {
    this.legAvg.set(conId, avgPrice);
  }

  setSpot(spot: number): void {
    this.spot = spot;
    if (this.built) this.render();
  }

  /** Slider: days elapsed from today (0 = today, max = nearestDte). */
  setDaysElapsed(days: number): void {
    this.daysElapsed = Math.max(0, Math.min(days, this.nearestDte));
    if (this.built) this.render();
  }

  setCostBasis(basis: CostBasis): void {
    this.costBasis = basis;
    if (this.built) this.render();
  }

  get maxDte(): number { return this.nearestDte; }

  destroy(): void {
    if (this.built) Plotly.purge(this.host);
    this.built = false;
  }
}
