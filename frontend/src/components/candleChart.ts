// Candlestick + volume chart built on lightweight-charts v5.
//
// Timezone handling: lightweight-charts formats UNIX timestamps in UTC. To
// display "exchange time" or "viewer local time" we shift each bar's epoch by
// the target zone's UTC offset before handing it to the chart (the standard
// approach recommended by the library docs), and shift back when needed.

import {
  CandlestickSeries,
  ColorType,
  createChart,
  CrosshairMode,
  HistogramSeries,
  LineStyle,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type MouseEventParams,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import type { Bar, SessionWindow } from "../api/types";
import { fmtPrice, fmtVolume } from "../lib/format";
import { ExtHoursShade, type ShadeBar, type ShadeKind } from "./extHoursShade";

export interface ChartColors {
  up: string;
  down: string;
}

function cssColors(): ChartColors {
  const cs = getComputedStyle(document.documentElement);
  return {
    up: cs.getPropertyValue("--up").trim() || "#0E7C4A",
    down: cs.getPropertyValue("--down").trim() || "#C93B36",
  };
}

/** Translucent fills for pre-market / after-hours shading, read from CSS. */
function shadeColors(): { pre: string; post: string } {
  const cs = getComputedStyle(document.documentElement);
  return {
    pre: cs.getPropertyValue("--shade-pre").trim() || "rgba(64,120,200,0.09)",
    post: cs.getPropertyValue("--shade-post").trim() || "rgba(196,142,36,0.11)",
  };
}

/** UTC offset (seconds) of `tz` at instant `utcSeconds`. DST-correct. */
function tzOffsetSeconds(tz: string, utcSeconds: number): number {
  const date = new Date(utcSeconds * 1000);
  try {
    const dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: tz,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    });
    const parts = dtf.formatToParts(date);
    const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
    const asUtc = Date.UTC(
      get("year"), get("month") - 1, get("day"),
      get("hour") % 24, get("minute"), get("second"),
    );
    return Math.round((asUtc - date.getTime()) / 1000);
  } catch {
    return 0; // unknown tz -> UTC
  }
}

export class CandleChart {
  private chart: IChartApi;
  private candles: ISeriesApi<"Candlestick">;
  private volume: ISeriesApi<"Histogram">;
  private tip: HTMLElement;
  private colors: ChartColors;
  private tz = "UTC"; // display timezone (IANA name), "UTC" = raw
  private bars: Bar[] = []; // source-of-truth in true epoch seconds
  private resizeObs: ResizeObserver;
  private costLine: IPriceLine | null = null;
  private costPrice: number | null = null;
  private costLabel = "";
  private shade: ExtHoursShade;
  private shadeVol: ExtHoursShade;
  private sessions: SessionWindow[] = [];
  private loadMore: ((oldestSec: number) => void) | null = null;

  constructor(host: HTMLElement) {
    this.colors = cssColors();
    host.innerHTML = `<div class="chart-tip"></div>`;
    this.tip = host.querySelector(".chart-tip")!;

    this.chart = createChart(host, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "#FFFFFF" },
        textColor: "#5D6673",
        fontFamily:
          '"Cascadia Mono", "SF Mono", "Roboto Mono", Consolas, monospace',
        fontSize: 11,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#EEF1F5" },
        horzLines: { color: "#EEF1F5" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: "#E2E6EC",
        rightOffset: 6,
        barSpacing: 9, // wider default so candles read clearly (esp. sparse options)
        minBarSpacing: 2,
      },
      rightPriceScale: { borderColor: "#E2E6EC" },
      localization: { locale: "en-US" },
      handleScale: { axisPressedMouseMove: true },
    });

    this.candles = this.chart.addSeries(CandlestickSeries, {
      upColor: this.colors.up,
      downColor: this.colors.down,
      wickUpColor: this.colors.up,
      wickDownColor: this.colors.down,
      borderVisible: true,
      borderUpColor: this.colors.up,
      borderDownColor: this.colors.down,
    });
    this.volume = this.chart.addSeries(
      HistogramSeries,
      {
        priceFormat: { type: "volume" },
        priceScaleId: "vol",
        // The default per-series last-value dashed line + axis label is
        // meaningless on a volume histogram (it marks the last bar's volume),
        // so hide it — only the candlestick series shows a price line.
        lastValueVisible: false,
        priceLineVisible: false,
      },
      1, // separate pane below
    );
    this.chart.panes()[1]?.setHeight(90);

    // Ext-hours shading on both pane backgrounds (price + volume) so the two
    // stay aligned. Pre-market and after-hours use distinct colors. Recompute
    // the shaded x-ranges as the user scrolls/zooms.
    const sc = shadeColors();
    this.shade = new ExtHoursShade(sc.pre, sc.post);
    this.shadeVol = new ExtHoursShade(sc.pre, sc.post);
    this.candles.attachPrimitive(this.shade);
    this.volume.attachPrimitive(this.shadeVol);
    this.chart.timeScale().subscribeVisibleTimeRangeChange(() => this.pushSessions());

    // Lazy-load older bars: when the viewport scrolls near the left edge, ask
    // the page for an older chunk. ~15 bars of lookahead so data arrives before
    // the user hits the very first bar.
    this.chart.timeScale().subscribeVisibleLogicalRangeChange((lr) => {
      if (!lr || !this.loadMore || this.bars.length === 0) return;
      if (lr.from < 15) this.loadMore(this.bars[0].time);
    });

    this.chart.subscribeCrosshairMove((p) => this.onCrosshair(p));

    // autoSize handles width/height, but a ResizeObserver makes the chart
    // re-layout promptly when the container itself changes (mobile rotate /
    // sidebar collapse), which autoSize can otherwise miss.
    this.resizeObs = new ResizeObserver(() => {
      const w = host.clientWidth;
      const h = host.clientHeight;
      if (w > 0 && h > 0) this.chart.resize(w, h);
    });
    this.resizeObs.observe(host);
  }

  destroy(): void {
    this.resizeObs.disconnect();
    this.chart.remove();
  }

  /** Set the display timezone; "UTC" or an IANA name. Re-renders data. */
  setTimezone(tz: string): void {
    this.tz = tz;
    this.pushData();
    this.refreshCostLine();
    this.pushSessions();
  }

  setColors(colors: ChartColors): void {
    this.colors = colors;
    this.candles.applyOptions({
      upColor: colors.up,
      downColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
    });
    this.pushData(); // volume bar tints
  }

  refreshColorsFromCss(): void {
    this.setColors(cssColors());
  }

  setBars(bars: Bar[]): void {
    this.bars = [...bars];
    this.pushData();
    this.chart.timeScale().fitContent();
    this.refreshCostLine();
    this.pushSessions();
  }

  /** Regular-session windows (true epoch seconds) for ext-hours shading. */
  setSessions(sessions: SessionWindow[]): void {
    this.sessions = sessions;
    this.pushSessions();
  }

  /**
   * Register a callback invoked (with the oldest loaded bar's true-epoch time)
   * when the viewport nears the left edge, so the page can fetch older bars.
   */
  setLoadMore(cb: ((oldestSec: number) => void) | null): void {
    this.loadMore = cb;
  }

  /** True-epoch seconds of the oldest loaded bar, or null when empty. */
  oldestTime(): number | null {
    return this.bars.length ? this.bars[0].time : null;
  }

  /**
   * Prepend older bars, keeping the viewport fixed on what the user is looking
   * at. Returns how many genuinely-older bars were added (0 = nothing new, e.g.
   * the series start was reached), which the caller uses to stop paging.
   */
  prependBars(older: Bar[]): number {
    if (!older.length) return 0;
    const oldestExisting = this.bars.length ? this.bars[0].time : Infinity;
    const fresh = older
      .filter((b) => b.time < oldestExisting)
      .sort((a, b) => a.time - b.time);
    if (!fresh.length) return 0;

    const ts = this.chart.timeScale();
    const before = ts.getVisibleLogicalRange();
    this.bars = [...fresh, ...this.bars];
    this.pushData(); // setData preserves nothing about position
    this.refreshCostLine();
    this.pushSessions();
    // Shift the visible logical range right by the number of prepended bars so
    // the same candles stay under the cursor (no visual jump).
    if (before) {
      ts.setVisibleLogicalRange({
        from: before.from + fresh.length,
        to: before.to + fresh.length,
      });
    }
    return fresh.length;
  }

  /** Flag each bar as regular/pre/post and hand display-time bars to both shades. */
  private pushSessions(): void {
    const dur = this.nominalBarSec();
    const shadeBars: ShadeBar[] = this.bars.map((b) => ({
      time: this.shift(b.time),
      kind: this.classifyBar(b.time, dur),
    }));
    this.shade.setBars(shadeBars);
    this.shadeVol.setBars(shadeBars);
  }

  /** Nominal bar length in seconds: the smallest gap between adjacent bars. */
  private nominalBarSec(): number {
    let min = Infinity;
    for (let i = 1; i < this.bars.length; i++) {
      const d = this.bars[i].time - this.bars[i - 1].time;
      if (d > 0 && d < min) min = d;
    }
    return Number.isFinite(min) ? min : 0;
  }

  /**
   * Classify a bar as regular (null, unshaded), pre-market, or after-hours.
   *
   * A bar covers the span [time, time + dur); it's regular when that span
   * overlaps a regular window (so the bars straddling the open/close read as
   * regular). Otherwise it's pre vs post by whichever regular session is nearer:
   * closer to the *next* open → pre-market; closer to the *previous* close →
   * after-hours. This correctly splits the overnight gap without needing
   * calendar-day math.
   */
  private classifyBar(epochSec: number, dur: number): ShadeKind {
    if (this.sessions.length === 0) return null;
    const end = epochSec + (dur > 0 ? dur : 1);
    for (const s of this.sessions) {
      if (epochSec < s.end && end > s.start) return null; // overlaps regular
    }
    const mid = epochSec + (dur > 0 ? dur : 0) / 2;
    let prevEnd = -Infinity;
    let nextStart = Infinity;
    for (const s of this.sessions) {
      if (s.end <= mid && s.end > prevEnd) prevEnd = s.end;
      if (s.start >= mid && s.start < nextStart) nextStart = s.start;
    }
    return nextStart - mid <= mid - prevEnd ? "pre" : "post";
  }

  /** Draw (or clear) a dashed horizontal line at the position's cost basis. */
  setCostLine(price: number | null, label = "Cost"): void {
    this.costPrice = price != null && Number.isFinite(price) ? price : null;
    this.costLabel = label;
    this.refreshCostLine();
  }

  private refreshCostLine(): void {
    if (this.costLine) {
      this.candles.removePriceLine(this.costLine);
      this.costLine = null;
    }
    if (this.costPrice == null) return;
    this.costLine = this.candles.createPriceLine({
      price: this.costPrice,
      color: "#8A93A2",
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: this.costLabel,
    });
  }

  /** Is the viewport currently pinned to the right edge (latest bars)? */
  private atRightEdge(): boolean {
    const range = this.chart.timeScale().scrollPosition();
    // scrollPosition ~0 when at the newest bar; small positive = right-offset gap
    return range >= -2;
  }

  /** Live update: replace-or-append the tail bar; follow the edge if pinned. */
  updateBar(bar: Bar): void {
    const wasAtEdge = this.atRightEdge();
    const lastIdx = this.bars.length - 1;
    const appended = lastIdx < 0 || bar.time > this.bars[lastIdx].time;
    if (lastIdx >= 0 && this.bars[lastIdx].time === bar.time) {
      this.bars[lastIdx] = bar;
    } else if (appended) {
      this.bars.push(bar);
    } else {
      return; // out-of-order; ignore
    }
    this.candles.update(this.candleDatum(bar));
    this.volume.update(this.volumeDatum(bar));
    if (appended) this.pushSessions(); // flag the new bar reg/ext
    if (wasAtEdge) this.chart.timeScale().scrollToRealTime();
  }

  private shift(epochSec: number): UTCTimestamp {
    if (this.tz === "UTC") return epochSec as UTCTimestamp;
    return (epochSec + tzOffsetSeconds(this.tz, epochSec)) as UTCTimestamp;
  }

  private candleDatum(b: Bar): CandlestickData {
    return {
      time: this.shift(b.time),
      open: b.open, high: b.high, low: b.low, close: b.close,
    };
  }

  private volumeDatum(b: Bar): HistogramData {
    const up = b.close >= b.open;
    return {
      time: this.shift(b.time),
      value: b.volume,
      color: up ? `${this.colors.up}55` : `${this.colors.down}55`,
    };
  }

  private pushData(): void {
    this.candles.setData(this.bars.map((b) => this.candleDatum(b)));
    this.volume.setData(this.bars.map((b) => this.volumeDatum(b)));
  }

  private onCrosshair(p: MouseEventParams<Time>): void {
    if (!p.time || !p.point || p.point.x < 0 || p.point.y < 0) {
      this.tip.style.display = "none";
      return;
    }
    const c = p.seriesData.get(this.candles) as CandlestickData | undefined;
    const v = p.seriesData.get(this.volume) as HistogramData | undefined;
    if (!c) {
      this.tip.style.display = "none";
      return;
    }
    const t = new Date((p.time as number) * 1000);
    // p.time is already shifted for display; format as UTC to show target-zone wall time
    const dateStr = t.toISOString().slice(0, 16).replace("T", " ");
    const chg = c.close - c.open;
    const cls = chg >= 0 ? "gain" : "loss";
    this.tip.innerHTML =
      `<div>${dateStr}</div>` +
      `<div>O <span class="${cls}">${fmtPrice(c.open)}</span> ` +
      `H <span class="${cls}">${fmtPrice(c.high)}</span> ` +
      `L <span class="${cls}">${fmtPrice(c.low)}</span> ` +
      `C <span class="${cls}">${fmtPrice(c.close)}</span></div>` +
      (v ? `<div>Vol ${fmtVolume(v.value)}</div>` : "");
    this.tip.style.display = "block";
  }
}
