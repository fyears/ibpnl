// P&L payoff curve analysis — uPlot + Black-Scholes.
//
// Renders: today's curve (blue, bold), N ghost decay curves (faint), a
// slider-selected curve (amber highlight), the expiry hockey-stick (red
// dashed), profit/loss fills, and a live vertical spot line.

import uPlot from "uplot";
import type { ComboLegInfo } from "../api/types";
import { bsPrice } from "../lib/blackscholes";

// ── constants ──────────────────────────────────────────────────────────────

const N_GHOST  = 4;        // intermediate ghost curves
const N_PTS    = 150;      // x-axis resolution
const DEF_IV   = 0.25;     // fallback IV when greeks not yet received
const RATE     = 0.045;    // risk-free rate (mirrors backend)
const CHT_H    = 320;      // chart height in px

// series indices in uPlot data[] (data[0] = x):
//   0 = x, 1 = today, 2..5 = ghosts, 6 = selected, 7 = expiry
const SI_GHOST0 = 2;
const SI_SEL    = SI_GHOST0 + N_GHOST; // = 6

export type CostBasis = "mark" | "position";

// ── helpers ────────────────────────────────────────────────────────────────

function dteDays(expiry: string): number {
  const y = +expiry.slice(0, 4), m = +expiry.slice(4, 6) - 1, d = +expiry.slice(6, 8);
  return Math.max(0, Math.ceil((Date.UTC(y, m, d) - Date.now()) / 86_400_000));
}

function numFmt(v: number): string {
  if (Math.abs(v) >= 10_000) return v.toFixed(0);
  if (Math.abs(v) >= 1_000)  return v.toFixed(2);
  if (Math.abs(v) < 1)       return v.toFixed(4);
  return v.toFixed(2);
}

// ── class ──────────────────────────────────────────────────────────────────

export class PayoffChart {
  private chart: uPlot | null = null;
  private readonly host: HTMLElement;
  private readonly legs: ComboLegInfo[];
  private readonly mult: number;         // shared contract multiplier
  private readonly nearestDte: number;   // calendar days to nearest expiry
  private readonly ghostDays: number[];  // intermediate curve days
  private readonly xArr: number[];       // underlying price grid (N_PTS points)

  // live per-leg state
  private legIv   = new Map<number, number>(); // implied vol
  private legMark = new Map<number, number>(); // current option price (mark basis)
  private legAvg  = new Map<number, number>(); // avg fill price (position basis)

  private spot: number | null = null;
  private costBasis: CostBasis = "mark";
  private daysElapsed = 0;
  private ro: ResizeObserver | null = null;

  constructor(host: HTMLElement, legs: ComboLegInfo[]) {
    this.host = host;
    this.legs = legs;
    this.mult = legs[0]?.instrument.multiplier ?? 1;

    // slider max = nearest leg expiry
    const dtes = legs
      .filter((l) => l.instrument.expiry)
      .map((l) => dteDays(l.instrument.expiry!));
    this.nearestDte = dtes.length ? Math.min(...dtes) : 0;

    // N_GHOST evenly-spaced intermediate days (excluding today and expiry)
    this.ghostDays = [];
    if (this.nearestDte > 1) {
      for (let k = 1; k <= N_GHOST; k++) {
        const d = Math.round((k / (N_GHOST + 1)) * this.nearestDte);
        this.ghostDays.push(Math.max(1, Math.min(d, this.nearestDte - 1)));
      }
    } else {
      // pad with zeros so the data array shape stays constant
      for (let k = 0; k < N_GHOST; k++) this.ghostDays.push(0);
    }

    // x-axis: strike range ± 22%
    const strikes = legs.map((l) => l.instrument.strike ?? 0).filter(Boolean);
    const sMin = Math.min(...strikes), sMax = Math.max(...strikes);
    const center = (sMin + sMax) / 2 || 100;
    const pad = center * 0.22;
    const lo = Math.max(sMin - pad, 1), hi = sMax + pad;
    this.xArr = Array.from({ length: N_PTS }, (_, i) => lo + (i / (N_PTS - 1)) * (hi - lo));

    this.buildChart();
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

  private buildAllData(): uPlot.AlignedData {
    const d: number[][] = [this.xArr];
    d.push(this.computeSeries(0));                                // today
    for (const gd of this.ghostDays) d.push(this.computeSeries(gd)); // ghosts
    d.push(this.computeSeries(this.daysElapsed));                // selected
    d.push(this.computeSeries(this.nearestDte));                 // expiry
    return d as uPlot.AlignedData;
  }

  // ── uPlot construction ───────────────────────────────────────────────────

  private buildChart(): void {
    if (this.chart) { this.chart.destroy(); this.chart = null; }
    this.host.innerHTML = "";

    const self = this;

    // Combined plugin: fills + reference lines drawn in a single draw pass.
    const plugin: uPlot.Plugin = {
      hooks: {
        draw: (u: uPlot) => {
          const ctx  = u.ctx;
          const left = u.bbox.left   / devicePixelRatio;
          const top  = u.bbox.top    / devicePixelRatio;
          const w    = u.bbox.width  / devicePixelRatio;
          const h    = u.bbox.height / devicePixelRatio;
          const zeroY = u.valToPos(0, "y", true);

          ctx.save();

          // ── zero reference line ──
          ctx.strokeStyle = "rgba(93,102,115,0.45)";
          ctx.lineWidth   = 1;
          ctx.setLineDash([4, 4]);
          ctx.beginPath();
          ctx.moveTo(left, zeroY); ctx.lineTo(left + w, zeroY);
          ctx.stroke();

          // ── strike verticals ──
          ctx.lineWidth   = 1;
          ctx.setLineDash([3, 3]);
          for (const leg of self.legs) {
            const k = leg.instrument.strike;
            if (!k) continue;
            const kx = u.valToPos(k, "x", true);
            ctx.strokeStyle = "rgba(93,102,115,0.35)";
            ctx.beginPath();
            ctx.moveTo(kx, top); ctx.lineTo(kx, top + h); ctx.stroke();
          }

          // ── live spot line ──
          if (self.spot != null) {
            const sx = u.valToPos(self.spot, "x", true);
            ctx.strokeStyle = "#16508f";
            ctx.lineWidth   = 1.5;
            ctx.setLineDash([]);
            ctx.beginPath();
            ctx.moveTo(sx, top); ctx.lineTo(sx, top + h); ctx.stroke();
          }

          ctx.restore();
        },

        // profit/loss fill for the selected curve only
        drawSeries: (u: uPlot, si: number) => {
          if (si !== SI_SEL) return;
          const ctx  = u.ctx;
          const left = u.bbox.left   / devicePixelRatio;
          const top  = u.bbox.top    / devicePixelRatio;
          const w    = u.bbox.width  / devicePixelRatio;
          const h    = u.bbox.height / devicePixelRatio;
          const zeroY = u.valToPos(0, "y", true);
          const ys = u.data[SI_SEL] as number[];
          const xs = u.data[0] as number[];
          const n  = xs.length;

          const drawFill = (above: boolean) => {
            ctx.save();
            ctx.beginPath();
            if (above) ctx.rect(left, top, w, Math.max(0, zeroY - top));
            else       ctx.rect(left, zeroY, w, Math.max(0, top + h - zeroY));
            ctx.clip();
            ctx.beginPath();
            for (let i = 0; i < n; i++) {
              const px = u.valToPos(xs[i], "x", true);
              const py = u.valToPos(ys[i], "y", true);
              i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
            }
            ctx.lineTo(u.valToPos(xs[n - 1], "x", true), zeroY);
            ctx.lineTo(u.valToPos(xs[0],     "x", true), zeroY);
            ctx.closePath();
            ctx.fillStyle = above ? "rgba(14,124,74,0.12)" : "rgba(201,59,54,0.12)";
            ctx.fill();
            ctx.restore();
          };
          drawFill(true);
          drawFill(false);
        },
      },
    };

    const ghostSeries: uPlot.Series[] = Array.from({ length: N_GHOST }, (_, i) => ({
      stroke: `rgba(22,80,143,${i < this.ghostDays.filter(d => d > 0).length ? "0.20" : "0"})`,
      width: 1, points: { show: false }, label: "",
    }));

    const opts: uPlot.Options = {
      width:  Math.max(this.host.clientWidth || 600, 300),
      height: CHT_H,
      scales: { x: { time: false }, y: {} },
      axes: [
        {
          label: "Underlying price",
          values: (_u: uPlot, vals: number[]) => vals.map((v) => numFmt(v)),
          gap: 4, size: 36, labelSize: 16,
        },
        {
          label: "P&L",
          values: (_u: uPlot, vals: number[]) =>
            vals.map((v) => (v > 0 ? "+" : "") + numFmt(v)),
          gap: 4, size: 72, labelSize: 16,
        },
      ],
      series: [
        {},
        // today
        { stroke: "#16508f", width: 2.0, points: { show: false }, label: "Today" },
        // ghosts
        ...ghostSeries,
        // selected (slider)
        { stroke: "#f5a623", width: 2.5, points: { show: false },
          label: `+${this.daysElapsed}d` },
        // expiry
        { stroke: "#c93b36", width: 1.5, points: { show: false },
          label: "At expiry", dash: [5, 3] },
      ],
      legend: { show: false },
      cursor:  { points: { show: false } },
      plugins: [plugin],
    };

    this.chart = new uPlot(opts, this.buildAllData(), this.host);

    // auto-resize with container
    this.ro = new ResizeObserver(() => {
      const w = this.host.clientWidth;
      if (w > 0 && this.chart) this.chart.setSize({ width: w, height: CHT_H });
    });
    this.ro.observe(this.host);
  }

  // ── public setters ────────────────────────────────────────────────────────

  /** Update implied vol for one leg → recompute and redraw. */
  setIv(conId: number, iv: number): void {
    this.legIv.set(conId, iv);
    this.rerender();
  }

  /** Update the current option price used as the mark cost basis. */
  setMarkPrice(conId: number, price: number): void {
    this.legMark.set(conId, price);
    this.rerender();
  }

  /** Set the position average fill price for one leg (position basis). */
  setAvgPrice(conId: number, avgPrice: number): void {
    this.legAvg.set(conId, avgPrice);
  }

  /** Update the live underlying spot price (moves the vertical line). */
  setSpot(spot: number): void {
    this.spot = spot;
    this.rerender();
  }

  /** Slider: days elapsed from today (0 = today, max = nearestDte). */
  setDaysElapsed(days: number): void {
    this.daysElapsed = Math.max(0, Math.min(days, this.nearestDte));
    if (!this.chart) return;
    // update only the selected series label + its data
    const data = this.chart.data as number[][];
    data[SI_SEL] = this.computeSeries(this.daysElapsed);
    this.chart.setData(data as uPlot.AlignedData, false);
    // update series label shown in the combo page slider readout (external)
    this.chart.series[SI_SEL].label = `+${this.daysElapsed}d`;
  }

  /** Switch cost basis and redraw all curves. */
  setCostBasis(basis: CostBasis): void {
    this.costBasis = basis;
    this.rerender();
  }

  get maxDte(): number { return this.nearestDte; }

  /** Full redraw of all series. */
  private rerender(): void {
    if (!this.chart) return;
    this.chart.setData(this.buildAllData(), false);
  }

  destroy(): void {
    this.ro?.disconnect();
    this.chart?.destroy();
    this.chart = null;
  }
}

