// Instrument detail: candlestick chart (live), position PnL, live Greeks.
//
// Two independent selectors drive the chart: a time RANGE (how far back) and a
// bar GRANULARITY (how coarse). Not every pair is valid for IB, so each range
// declares the finest granularity it permits and coarser grains are auto-picked
// when a range can't support the current one.

import { api } from "../api/client";
import { stream } from "../api/stream";
import type { BarSet, Greeks, Instrument, Position, Quote, SessionWindow } from "../api/types";
import { CandleChart } from "../components/candleChart";
import { mdtBadge, sideBadge } from "../components/badges";
import { mountSearchBox } from "../components/searchBox";
import {
  assetClassLabel,
  fmtExpiry,
  fmtGreek,
  fmtIv,
  fmtMoney,
  fmtPnl,
  fmtPrice,
  fmtQty,
  fmtSize,
  fmtVolume,
  pnlClass,
} from "../lib/format";
import { getSettings, onSettingsChange } from "../state/settings";

interface Grain {
  key: string;
  label: string;
  barSize: string;
  intraday: boolean;
}
// Ordered fine -> coarse; `minGrain` on a range indexes into this list.
const GRAINS: Grain[] = [
  { key: "1m", label: "1m", barSize: "1 min", intraday: true },
  { key: "5m", label: "5m", barSize: "5 mins", intraday: true },
  { key: "15m", label: "15m", barSize: "15 mins", intraday: true },
  { key: "30m", label: "30m", barSize: "30 mins", intraday: true },
  { key: "1h", label: "1h", barSize: "1 hour", intraday: true },
  { key: "1d", label: "1D", barSize: "1 day", intraday: false },
];
const GRAIN_1H = 4;

interface Range {
  key: string;
  label: string;
  duration: string;
  minGrain: number; // finest grain index this range may use
}
const RANGES: Range[] = [
  { key: "1d", label: "1D", duration: "1 D", minGrain: 0 },
  { key: "1w", label: "1W", duration: "1 W", minGrain: 0 },
  { key: "1m", label: "1M", duration: "1 M", minGrain: 2 },
  { key: "3m", label: "3M", duration: "3 M", minGrain: 4 },
  { key: "1y", label: "1Y", duration: "1 Y", minGrain: 5 },
];

export function renderInstrument(outlet: HTMLElement, conId: number): () => void {
  outlet.innerHTML = `
    <div class="page">
      <div class="inst-topline">
        <p class="eyebrow"><a href="#/">← Positions</a></p>
        <div class="inst-search" id="inst-search"></div>
      </div>
      <div class="inst-header">
        <h1 id="inst-name" class="skeleton">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</h1>
        <span class="price" id="inst-price"></span>
        <span class="chg" id="inst-chg"></span>
        <span id="inst-badges"></span>
      </div>
      <div class="inst-sub" id="inst-sub"></div>
      <div class="detail-grid">
        <div class="chart-card">
          <div class="chart-toolbar">
            <div class="seg" id="range-seg">
              ${RANGES.map(
                (r) => `<button type="button" data-range="${r.key}">${r.label}</button>`,
              ).join("")}
            </div>
            <div class="seg" id="grain-seg">
              ${GRAINS.map(
                (g) => `<button type="button" data-grain="${g.key}">${g.label}</button>`,
              ).join("")}
            </div>
            <div class="seg" id="rth-seg">
              <button type="button" data-rth="false">Ext hours</button>
              <button type="button" data-rth="true">RTH only</button>
            </div>
            <span class="spacer"></span>
            <span class="chart-legend" id="chart-legend" hidden>
              <span class="lg"><i class="sw pre"></i>Pre-market</span>
              <span class="lg"><i class="sw post"></i>After-hours</span>
            </span>
            <span class="eyebrow" id="tz-note"></span>
          </div>
          <div class="chart-host" id="chart-host">
            <div class="loading-note"><span class="spin"></span>Loading price history…</div>
          </div>
        </div>
        <aside>
          <div class="side-card" id="pos-card" hidden>
            <h2>Your position</h2>
            <div id="pos-body"></div>
          </div>
          <div class="side-card" id="greeks-card" hidden>
            <h2>Greeks <span class="badge live" id="greeks-live" hidden>LIVE</span></h2>
            <div id="greeks-body"></div>
          </div>
          <div class="side-card" id="quote-card" hidden>
            <h2>Quote <span id="quote-title-extra"></span></h2>
            <div id="quote-body"></div>
          </div>
        </aside>
      </div>
    </div>
  `;

  let disposed = false;
  let chart: CandleChart | null = null;
  let inst: Instrument | null = null;
  let barSet: BarSet | null = null;
  let held: Position | null = null;
  let lastQuote: Quote | null = null;
  let range = RANGES[1]; // default 1W
  let grain = GRAINS[GRAIN_1H]; // default 1h
  let rthOnly = false;
  let reqSeq = 0; // guards against out-of-order history responses
  let sessions: SessionWindow[] = []; // accumulated across lazy-loaded chunks
  let loadingOlder = false;
  let reachedStart = false; // no older bars remain (series inception)

  const $ = <T extends HTMLElement = HTMLElement>(sel: string) =>
    outlet.querySelector<T>(sel);

  // Banner search: jump to another symbol's chart without going home first.
  const teardownSearch = mountSearchBox($("#inst-search")!, {
    compact: true,
    placeholder: "Search another symbol…",
  });

  const isOption = () => !!inst && (inst.sec_type === "OPT" || inst.sec_type === "FOP");

  // ---------- header ----------

  const renderHeader = () => {
    if (!inst) return;
    const name = $("#inst-name")!;
    name.classList.remove("skeleton");
    name.textContent = displayName(inst);
    const sub = [inst.long_name, assetClassLabel(inst.asset_class), inst.exchange, inst.currency]
      .filter(Boolean)
      .join(" · ");
    $("#inst-sub")!.textContent = sub;
    document.title = `${displayName(inst)} — IBKR Deck`;
  };

  const renderPrice = () => {
    if (!lastQuote || !inst) return;
    const q = lastQuote;
    // IB's frozen weekend/overnight tick is unreliable — it can be empty (an
    // illiquid stock) or a day stale (a lightly-traded future over a weekend).
    // The most recent chart bar is always the last real print, so use it for the
    // headline price when the market is closed or there's no live last.
    const barLast = barSet?.bars.length
      ? barSet.bars[barSet.bars.length - 1].close
      : null;
    const useBar = (q.market_session === "closed" || q.last == null) && barLast != null;
    const showLast = useBar ? barLast : q.last;

    $("#inst-price")!.textContent = fmtPrice(showLast);
    $("#inst-badges")!.innerHTML =
      mdtBadge(q.market_data_type) + (held ? " " + sideBadge(held.quantity) : "");
    const chg = $("#inst-chg")!;
    if (showLast != null && q.close != null && q.close !== 0 && showLast !== q.close) {
      const d = showLast - q.close;
      const pct = (d / q.close) * 100;
      chg.className = `chg ${pnlClass(d)}`;
      chg.textContent = `${d >= 0 ? "+" : "−"}${fmtPrice(Math.abs(d))} (${d >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)`;
    } else {
      chg.textContent = "";
    }

    // quote card — indicators depend on instrument type
    $("#quote-card")!.hidden = false;
    // Bid/Ask with resting size shown as "15 * 333.81" (size × price, not the
    // meaningless product). Bid is listed before Ask.
    const sizeVal = (size: number | null, price: number | null): string => {
      if (price == null) return "—";
      if (size == null) return fmtPrice(price);
      return `${fmtSize(size)} <span class="mul">*</span> ${fmtPrice(price)}`;
    };

    // After-hours / pre-market marker + regular-session close alongside.
    const sess = q.market_session;
    const sessTag =
      sess === "pre"
        ? `<span class="badge sess">PRE-MARKET</span>`
        : sess === "post"
          ? `<span class="badge sess">AFTER HOURS</span>`
          : sess === "closed"
            ? `<span class="badge sess">CLOSED</span>`
            : "";
    $("#quote-title-extra")!.innerHTML = sessTag;
    // Off-hours: the live mark is an ext-hours print, so label the regular close
    // explicitly as "Reg. close"; in regular hours it's the prior close.
    const offHours = sess === "pre" || sess === "post" || sess === "closed";
    const closeLabel = offHours ? "Reg. close" : "Prev close";

    let rows: [string, string][];
    if (isOption()) {
      rows = [
        ["Bid", fmtPrice(q.bid)],
        ["Ask", fmtPrice(q.ask)],
        [closeLabel, fmtPrice(q.close)],
        ["Volume", q.volume != null ? fmtVolume(q.volume) : "—"],
        ["Open interest", q.open_interest != null ? fmtVolume(q.open_interest) : "—"],
      ];
    } else {
      rows = [
        ["Bid", sizeVal(q.bid_size, q.bid)],
        ["Ask", sizeVal(q.ask_size, q.ask)],
        ["Open", fmtPrice(q.open)],
        ["VWAP", fmtPrice(q.vwap)],
        [closeLabel, fmtPrice(q.close)],
        ["Volume", q.volume != null ? fmtVolume(q.volume) : "—"],
      ];
    }
    $("#quote-body")!.innerHTML = kv(rows);
  };

  // ---------- side cards ----------

  const renderPosition = () => {
    const card = $("#pos-card")!;
    if (!held) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const cur = held.instrument.currency;
    const avg = held.avg_price ?? held.avg_cost;
    $("#pos-body")!.innerHTML = kv([
      ["Side / Qty", `${held.quantity < 0 ? "Short" : "Long"} ${fmtQty(Math.abs(held.quantity))}`],
      ["Avg price", fmtPrice(avg)],
      ["Market value", fmtMoney(held.market_value, cur)],
      ["Day P&L", `<span class="${pnlClass(held.daily_pnl)}">${fmtPnl(held.daily_pnl, cur)}</span>`],
      ["Unrealized P&L", `<span class="${pnlClass(held.unrealized_pnl)}">${fmtPnl(held.unrealized_pnl, cur)}</span>`],
    ]);
  };

  const renderGreeks = (g: Greeks | null) => {
    const card = $("#greeks-card")!;
    if (!isOption() || !g) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    $("#greeks-live")!.hidden = false;
    $("#greeks-body")!.innerHTML = kv([
      ["Delta", fmtGreek(g.delta)],
      ["Gamma", fmtGreek(g.gamma, 4)],
      ["Vega", fmtGreek(g.vega, 3)],
      ["Theta", fmtGreek(g.theta, 3)],
      ["IV", fmtIv(g.iv)],
      ["Underlying", fmtPrice(g.und_price)],
    ]);
  };

  // ---------- chart ----------

  const applyTimezone = () => {
    if (!chart || !barSet) return;
    const s = getSettings();
    const tz =
      s.timezone === "exchange"
        ? barSet.exchange_tz
        : Intl.DateTimeFormat().resolvedOptions().timeZone;
    chart.setTimezone(tz);
    $("#tz-note")!.textContent =
      s.timezone === "exchange" ? `${barSet.exchange_tz}` : `Local (${tz})`;
  };

  const applyCostLine = () => {
    if (!chart) return;
    if (held) {
      const avg = held.avg_price ?? held.avg_cost;
      chart.setCostLine(avg, "Cost");
    } else {
      chart.setCostLine(null);
    }
  };

  const loadBars = async () => {
    const host = $("#chart-host")!;
    if (!chart) {
      host.innerHTML = "";
      chart = new CandleChart(host);
      chart.setLoadMore(loadOlder);
    }
    const mine = ++reqSeq;
    reachedStart = false;
    loadingOlder = false;
    try {
      const bs = await api.history(conId, {
        duration: range.duration,
        barSize: grain.barSize,
        rthOnly,
      });
      if (disposed || !chart || mine !== reqSeq) return;
      barSet = bs;
      chart.setBars(bs.bars);
      sessions = bs.rth_only ? [] : bs.sessions;
      chart.setSessions(sessions);
      // Legend explains the pre/post shading colors; only meaningful when
      // ext-hours shading is actually drawn.
      $("#chart-legend")!.hidden = sessions.length === 0;
      applyTimezone();
      applyCostLine();
      // Bars just arrived — refresh the headline price, which falls back to the
      // last bar's close when the market is closed / has no live tick.
      renderPrice();
      // Stream a forming bar at the same granularity for intraday grains; the
      // chart appends new-time bars as time passes (auto-draw at the tail).
      stream.subscribeBars(grain.intraday ? conId : null, grain.barSize);
    } catch {
      if (mine !== reqSeq) return;
      host.innerHTML = `<div class="error-note"><strong>Couldn't load chart data.</strong>
        The instrument may have no historical data permission for this range.</div>`;
      chart = null;
    }
  };

  // Lazy-load older history when the chart is scrolled back past its oldest bar.
  // Fetches another `range`-sized window ending at the current oldest bar and
  // prepends it, until the series start is reached.
  const loadOlder = async (oldestSec: number) => {
    if (loadingOlder || reachedStart || !chart || disposed) return;
    loadingOlder = true;
    const mine = reqSeq;
    try {
      const bs = await api.history(conId, {
        duration: range.duration,
        barSize: grain.barSize,
        rthOnly,
        end: oldestSec,
      });
      if (disposed || !chart || mine !== reqSeq) return;
      const added = chart.prependBars(bs.bars);
      if (added === 0) {
        reachedStart = true;
      } else if (!rthOnly && bs.sessions.length) {
        sessions = mergeSessions(sessions, bs.sessions);
        chart.setSessions(sessions);
      }
    } catch {
      /* transient; a later scroll will retry */
    } finally {
      loadingOlder = false;
    }
  };

  const syncSegs = () => {
    outlet.querySelectorAll<HTMLButtonElement>("[data-range]").forEach((b) => {
      b.classList.toggle("active", b.dataset.range === range.key);
    });
    outlet.querySelectorAll<HTMLButtonElement>("[data-grain]").forEach((b) => {
      const idx = GRAINS.findIndex((g) => g.key === b.dataset.grain);
      b.classList.toggle("active", b.dataset.grain === grain.key);
      // dim grains too fine for the selected range
      b.disabled = idx < range.minGrain;
      b.classList.toggle("disabled", idx < range.minGrain);
    });
    outlet.querySelectorAll<HTMLButtonElement>("[data-rth]").forEach((b) => {
      b.classList.toggle("active", b.dataset.rth === String(rthOnly));
    });
  };

  outlet.querySelectorAll<HTMLButtonElement>("[data-range]").forEach((b) => {
    b.addEventListener("click", () => {
      range = RANGES.find((r) => r.key === b.dataset.range) ?? range;
      // bump granularity coarser if this range can't support the current one
      const gi = GRAINS.findIndex((g) => g.key === grain.key);
      if (gi < range.minGrain) grain = GRAINS[range.minGrain];
      syncSegs();
      void loadBars();
    });
  });
  outlet.querySelectorAll<HTMLButtonElement>("[data-grain]").forEach((b) => {
    b.addEventListener("click", () => {
      const idx = GRAINS.findIndex((g) => g.key === b.dataset.grain);
      if (idx < range.minGrain) return; // disabled for this range
      grain = GRAINS[idx] ?? grain;
      syncSegs();
      void loadBars();
    });
  });
  outlet.querySelectorAll<HTMLButtonElement>("[data-rth]").forEach((b) => {
    b.addEventListener("click", () => {
      rthOnly = b.dataset.rth === "true";
      syncSegs();
      void loadBars();
    });
  });
  syncSegs();

  // ---------- data flow ----------

  const load = async () => {
    try {
      inst = await api.instrument(conId);
    } catch {
      outlet.querySelector(".page")!.innerHTML = `
        <div class="error-note"><strong>Unknown instrument.</strong>
        <a href="#/">Back to positions</a></div>`;
      return;
    }
    if (disposed) return;
    renderHeader();
    // options default their finest grain to 1h too, but 1-day for very old data
    // is available via the range buttons.

    // find our position in this instrument, if any
    try {
      const groups = await api.positions();
      for (const g of groups) {
        for (const p of g.positions) {
          if (p.instrument.con_id === conId) held = p;
        }
      }
    } catch {
      /* not critical */
    }
    if (disposed) return;
    renderPosition();

    // Open the live market-data line first so greeks/underlying populate, then
    // pull an initial snapshot for immediate paint.
    stream.subscribeQuotes([conId]);
    api.quote(conId)
      .then((q) => {
        if (!disposed) {
          lastQuote = q;
          renderPrice();
        }
      })
      .catch(() => {});
    api.greeks(conId)
      .then((g) => {
        if (!disposed) renderGreeks(g);
      })
      .catch(() => {});

    await loadBars();
  };

  const unsub = stream.onMessage((msg) => {
    if (disposed) return;
    switch (msg.type) {
      case "quote":
        if (msg.quote.con_id === conId) {
          lastQuote = msg.quote;
          renderPrice();
        }
        break;
      case "greeks":
        if (msg.greeks.con_id === conId) renderGreeks(msg.greeks);
        break;
      case "pnl":
        if (held && msg.con_id === conId) {
          held.daily_pnl = msg.daily_pnl;
          held.unrealized_pnl = msg.unrealized_pnl;
          if (msg.market_value != null) held.market_value = msg.market_value;
          renderPosition();
        }
        break;
      case "bar":
        if (msg.con_id === conId && chart) chart.updateBar(msg.bar);
        break;
    }
  });

  const unsubSettings = onSettingsChange(() => {
    applyTimezone();
    chart?.refreshColorsFromCss();
  });

  void load();

  return () => {
    disposed = true;
    teardownSearch();
    unsub();
    unsubSettings();
    stream.subscribeQuotes([]);
    stream.subscribeBars(null);
    chart?.destroy();
    chart = null;
    document.title = "IBKR Deck";
  };
}

function displayName(inst: Instrument): string {
  if ((inst.sec_type === "OPT" || inst.sec_type === "FOP") && inst.strike != null) {
    return `${inst.symbol} ${fmtPrice(inst.strike)} ${inst.right === "C" ? "Call" : "Put"} · ${fmtExpiry(inst.expiry)}`;
  }
  if (inst.sec_type === "FUT") {
    return `${inst.symbol} Future · ${fmtExpiry(inst.expiry)}`;
  }
  return inst.symbol;
}

function kv(pairs: [string, string][]): string {
  return pairs
    .map(
      ([k, v]) =>
        `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`,
    )
    .join("");
}

/** Union of two session-window lists, de-duplicated by start and time-sorted. */
function mergeSessions(a: SessionWindow[], b: SessionWindow[]): SessionWindow[] {
  const seen = new Set(a.map((w) => w.start));
  const out = [...a];
  for (const w of b) {
    if (!seen.has(w.start)) {
      seen.add(w.start);
      out.push(w);
    }
  }
  out.sort((x, y) => x.start - y.start);
  return out;
}
