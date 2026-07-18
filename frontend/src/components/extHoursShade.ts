// A chart pane-background primitive that shades the extended-hours bars —
// everything OUTSIDE the regular trading session — so ext-hours prints are
// visually distinct. Pre-market and after-hours get DIFFERENT colors.
//
// It works per-BAR (bars are always valid time-scale points, unlike arbitrary
// session-boundary timestamps, which `timeToCoordinate` returns null for). Each
// shaded bar contributes a half-bar-wide band on each side of its center;
// adjacent bands of the same kind merge into contiguous shaded spans. The same
// primitive is attached to both the price pane and the volume pane so the
// shading lines up.

import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  Time,
} from "lightweight-charts";

/** null = regular hours (not shaded); otherwise which ext-hours side. */
export type ShadeKind = "pre" | "post" | null;

export interface ShadeBar {
  time: Time; // display-time value (already tz-shifted), a real bar point
  kind: ShadeKind;
}

interface Band {
  from: number;
  to: number;
  color: string;
}

class ShadeRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly bands: Band[]) {}

  draw(): void {
    /* foreground: nothing */
  }

  drawBackground(target: CanvasRenderingTarget2D): void {
    if (!this.bands.length) return;
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const ratio = scope.horizontalPixelRatio;
      for (const b of this.bands) {
        const x1 = b.from * ratio;
        const x2 = b.to * ratio;
        if (x2 > x1) {
          ctx.fillStyle = b.color;
          ctx.fillRect(x1, 0, x2 - x1, scope.bitmapSize.height);
        }
      }
    });
  }
}

class ShadeView implements IPrimitivePaneView {
  private bands: Band[] = [];

  setBands(bands: Band[]): void {
    this.bands = bands;
  }

  zOrder(): "bottom" {
    return "bottom";
  }

  renderer(): IPrimitivePaneRenderer {
    return new ShadeRenderer(this.bands);
  }
}

/** Shades pre-market and after-hours bars (distinct colors) on a pane background. */
export class ExtHoursShade implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null;
  private requestUpdate: (() => void) | null = null;
  private view = new ShadeView();
  private bars: ShadeBar[] = [];

  constructor(
    private readonly preColor = "rgba(64, 120, 200, 0.09)",
    private readonly postColor = "rgba(196, 142, 36, 0.11)",
  ) {}

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.requestUpdate = null;
  }

  /** Bars with a pre/post/regular flag, in display time. */
  setBars(bars: ShadeBar[]): void {
    this.bars = bars;
    this.requestUpdate?.();
  }

  updateAllViews(): void {
    if (!this.chart || !this.bars.length) {
      this.view.setBands([]);
      return;
    }
    const ts = this.chart.timeScale();
    let half = 4;
    try {
      half = Math.max(1, ts.options().barSpacing / 2);
    } catch {
      /* keep default */
    }
    const bands: Band[] = [];
    for (const b of this.bars) {
      if (b.kind == null) continue;
      const color = b.kind === "pre" ? this.preColor : this.postColor;
      const x = ts.timeToCoordinate(b.time);
      if (x == null) continue;
      const from = x - half;
      const to = x + half;
      const last = bands[bands.length - 1];
      // Merge only with an adjacent band of the same color (pre never touches
      // post — the regular session sits between them — but guard anyway).
      if (last && last.color === color && from <= last.to + 0.5) {
        last.to = Math.max(last.to, to);
      } else {
        bands.push({ from, to, color });
      }
    }
    this.view.setBands(bands);
  }

  paneViews(): IPrimitivePaneView[] {
    return [this.view];
  }
}
