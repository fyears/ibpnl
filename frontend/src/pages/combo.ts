// Option-combo detail: a combined candlestick chart for several option legs of
// one underlying, plus the leg breakdown, net cost and combined P&L.
//
// The combo value is sum(ratio_i * price_i) in per-share points, signed by each
// leg's direction (long +, short −), so a net-credit combo reads negative. The
// URL is canonical — "#/combo/-5@2006,5@2007" — and reconstructable cold, so a
// shared link rebuilds the same combo from the con_ids and multiples alone.

import { api } from "../api/client";
import { stream } from "../api/stream";
import type {
  ComboBarSet,
  ComboLegInfo,
  Position,
  Quote,
  SessionWindow,
} from "../api/types";
import { CandleChart } from "../components/candleChart";
import { mountSearchBox } from "../components/searchBox";
import {
  fmtExpiry,
  fmtMoney,
  fmtPnl,
  fmtPrice,
  fmtQty,
  pnlClass,
} from "../lib/format";
import { getSettings, onSettingsChange } from "../state/settings";

interface Grain {
  key: string;
  label: string;
  barSize: string;
  intraday: boolean;
  seconds: number;
}
const GRAINS: Grain[] = [
  { key: "1m", label: "1m", barSize: "1 min", intraday: true, seconds: 60 },
  { key: "5m", label: "5m", barSize: "5 mins", intraday: true, seconds: 300 },
  { key: "15m", label: "15m", barSize: "15 mins", intraday: true, seconds: 900 },
  { key: "30m", label: "30m", barSize: "30 mins", intraday: true, seconds: 1800 },
  { key: "1h", label: "1h", barSize: "1 hour", intraday: true, seconds: 3600 },
  { key: "1d", label: "1D", barSize: "1 day", intraday: false, seconds: 86400 },
];
const GRAIN_1H = 4;

interface Range {
  key: string;
  label: string;
  duration: string;
  minGrain: number;
}
const RANGES: Range[] = [
  { key: "1d", label: "1D", duration: "1 D", minGrain: 0 },
  { key: "1w", label: "1W", duration: "1 W", minGrain: 0 },
  { key: "1m", label: "1M", duration: "1 M", minGrain: 2 },
  { key: "3m", label: "3M", duration: "3 M", minGrain: 4 },
  { key: "1y", label: "1Y", duration: "1 Y", minGrain: 5 },
];

export function renderCombo(outlet: HTMLElement, spec: string): () => void {
  outlet.innerHTML = `
    <div class="page">
      <div class="inst-topline">
        <p class="eyebrow"><a href="#/">← Positions</a></p>
        <div class="inst-search" id="inst-search"></div>
      </div>
      <div class="inst-header">
        <h1 id="combo-name" class="skeleton">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</h1>
        <span class="price" id="combo-price"></span>
        <span class="chg" id="combo-chg"></span>
        <span class="badge sess" id="combo-net" hidden></span>
      </div>
      <div class="inst-sub" id="combo-sub"></div>
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
            <div class="loading-note"><span class="spin"></span>Loading combo history…</div>
          </div>
        </div>
        <aside>
          <div class="side-card" id="legs-card">
            <h2>Combo legs</h2>
            <div id="legs-body"></div>
          </div>
          <div class="side-card" id="combo-pos-card" hidden>
            <h2>Combined position</h2>
            <div id="combo-pos-body"></div>
          </div>
        </aside>
      </div>
    </div>
  `;

  let disposed = false;
  let chart: CandleChart | null = null;
  let meta: ComboBarSet | null = null;
  let range = RANGES[1]; // 1W
  let grain = GRAINS[GRAIN_1H]; // 1h
  let rthOnly = false;
  let reqSeq = 0;
  let sessions: SessionWindow[] = [];
  let loadingOlder = false;
  let reachedStart = false;
  // live combo mark computed from per-leg quotes
  const legLast = new Map<number, number>();
  const legClose = new Map<number, number>();
  const held = new Map<number, Position>();
  let formingBar: { time: number; open: number; high: number; low: number; close: number } | null =
    null;

  const $ = <T extends HTMLElement = HTMLElement>(sel: string) => outlet.querySelector<T>(sel);

  const teardownSearch = mountSearchBox($("#inst-search")!, {
    compact: true,
    placeholder: "Search another symbol…",
  });

  const legConIds = () => meta?.legs.map((l) => l.instrument.con_id) ?? [];
  const ratioOf = (conId: number) =>
    meta?.legs.find((l) => l.instrument.con_id === conId)?.ratio ?? 0;

  // ---------- header / cards ----------

  const legText = (leg: ComboLegInfo): string => {
    const inst = leg.instrument;
    const sign = leg.ratio > 0 ? "+" : "−";
    const right = inst.right === "C" ? "C" : "P";
    return `${sign}${fmtQty(Math.abs(leg.ratio))} × ${fmtPrice(inst.strike)} ${right} · ${fmtExpiry(inst.expiry)}`;
  };

  const renderHeader = () => {
    if (!meta) return;
    const name = $("#combo-name")!;
    name.classList.remove("skeleton");
    name.textContent = `${meta.symbol} combo`;
    const legs = meta.legs.map(legText).join("  /  ");
    const mult = meta.multiplier && meta.multiplier !== 1 ? ` · ×${fmtQty(meta.multiplier)}` : "";
    $("#combo-sub")!.innerHTML =
      `${escapeHtml(legs)}${mult}` +
      `<span class="combo-canon" title="Canonical combo">${escapeHtml(meta.canonical)}</span>`;
    document.title = `${meta.symbol} combo — IBKR Deck`;
  };

  const renderLegs = () => {
    if (!meta) return;
    const rows = meta.legs
      .map((leg) => {
        const held0 = held.get(leg.instrument.con_id);
        const price = legLast.get(leg.instrument.con_id);
        const side = leg.ratio > 0 ? "long" : "short";
        return `
          <div class="kv">
            <span class="k">
              <a href="#/i/${leg.instrument.con_id}" class="mono">${escapeHtml(legText(leg))}</a>
              <span class="badge ${side}">${side.toUpperCase()}</span>
            </span>
            <span class="v mono">${price != null ? fmtPrice(price) : held0 ? fmtPrice(held0.avg_price ?? held0.avg_cost) : "—"}</span>
          </div>`;
      })
      .join("");
    $("#legs-body")!.innerHTML = rows;
  };

  const netCost = (): number | null => {
    // Σ ratio × avg_price, only when every leg is held (so it's meaningful).
    if (!meta) return null;
    let sum = 0;
    for (const leg of meta.legs) {
      const p = held.get(leg.instrument.con_id);
      const avg = p?.avg_price ?? p?.avg_cost;
      if (avg == null) return null;
      sum += leg.ratio * avg;
    }
    return sum;
  };

  const renderComboPosition = () => {
    if (!meta) return;
    const card = $("#combo-pos-card")!;
    // P&L is only trustworthy when the combo's ratios match the actual holdings.
    const legsHeld = meta.legs.filter((l) => held.has(l.instrument.con_id));
    const ratioMatchesQty =
      legsHeld.length === meta.legs.length &&
      meta.legs.every((l) => {
        const p = held.get(l.instrument.con_id)!;
        return Math.round(p.quantity) === l.ratio;
      });
    const cost = netCost();
    if (cost == null && !ratioMatchesQty) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const cur = meta.legs[0]?.instrument.currency ?? "USD";
    const rows: [string, string][] = [];
    if (cost != null) {
      const sign = cost > 0 ? "debit" : cost < 0 ? "credit" : "even";
      rows.push(["Net cost", `${fmtPrice(cost)} <span class="mul">(${sign})</span>`]);
    }
    if (ratioMatchesQty) {
      const day = sumHeld((p) => p.daily_pnl);
      const unreal = sumHeld((p) => p.unrealized_pnl);
      const mv = sumHeld((p) => p.market_value);
      rows.push([
        "Day P&L",
        `<span class="${pnlClass(day)}">${fmtPnl(day, cur)}</span>`,
      ]);
      rows.push([
        "Unrealized P&L",
        `<span class="${pnlClass(unreal)}">${fmtPnl(unreal, cur)}</span>`,
      ]);
      rows.push(["Market value", fmtMoney(mv, cur)]);
    }
    $("#combo-pos-body")!.innerHTML = kv(rows);
  };

  const sumHeld = (pick: (p: Position) => number | null | undefined): number | null => {
    if (!meta) return null;
    let sum = 0;
    let any = false;
    for (const leg of meta.legs) {
      const p = held.get(leg.instrument.con_id);
      const v = p ? pick(p) : null;
      if (v != null) {
        sum += v;
        any = true;
      }
    }
    return any ? sum : null;
  };

  const comboAt = (src: Map<number, number>): number | null => {
    if (!meta || meta.legs.length === 0) return null;
    let sum = 0;
    for (const leg of meta.legs) {
      const v = src.get(leg.instrument.con_id);
      if (v == null) return null; // need every leg to have a price
      sum += leg.ratio * v;
    }
    return sum;
  };

  const renderPrice = () => {
    if (!meta) return;
    const mark = comboAt(legLast);
    const barLast = meta.bars.length ? meta.bars[meta.bars.length - 1].close : null;
    const show = mark ?? barLast;
    $("#combo-price")!.textContent = show != null ? fmtPrice(show) : "—";
    const prev = comboAt(legClose);
    const chg = $("#combo-chg")!;
    if (show != null && prev != null && prev !== show) {
      const d = show - prev;
      chg.className = `chg ${pnlClass(d)}`;
      chg.textContent = `${d >= 0 ? "+" : "−"}${fmtPrice(Math.abs(d))}`;
    } else {
      chg.textContent = "";
    }
    const net = $("#combo-net")!;
    if (show != null) {
      net.hidden = false;
      net.textContent = show > 0 ? "NET DEBIT" : show < 0 ? "NET CREDIT" : "EVEN";
    } else {
      net.hidden = true;
    }
  };

  // ---------- chart ----------

  const applyTimezone = () => {
    if (!chart || !meta) return;
    const s = getSettings();
    const tz =
      s.timezone === "exchange"
        ? meta.exchange_tz
        : Intl.DateTimeFormat().resolvedOptions().timeZone;
    chart.setTimezone(tz);
    $("#tz-note")!.textContent = s.timezone === "exchange" ? meta.exchange_tz : `Local (${tz})`;
  };

  const applyCostLine = () => {
    if (!chart) return;
    const cost = netCost();
    if (cost != null) chart.setCostLine(cost, "Net cost");
    else chart.setCostLine(null);
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
    formingBar = null;
    try {
      const bs = await api.comboHistory(spec, {
        duration: range.duration,
        barSize: grain.barSize,
        rthOnly,
      });
      if (disposed || !chart || mine !== reqSeq) return;
      meta = bs;
      // Canonicalize the URL so shared/back links are stable and deduplicated.
      canonicalizeUrl(bs.canonical);
      renderHeader();
      renderLegs();
      renderComboPosition();
      chart.setBars(bs.bars);
      sessions = bs.rth_only ? [] : bs.sessions;
      chart.setSessions(sessions);
      $("#chart-legend")!.hidden = sessions.length === 0;
      applyTimezone();
      applyCostLine();
      renderPrice();
      // Live per-leg quotes drive the combo mark + a client-side forming bar.
      stream.subscribeQuotes(legConIds());
    } catch (e) {
      if (mine !== reqSeq) return;
      const detail = e instanceof Error ? e.message : "";
      host.innerHTML = `<div class="error-note"><strong>Couldn't load combo data.</strong>
        ${escapeHtml(detail)} The legs may lack historical data for this range, or the
        combo spans more than one underlying.</div>`;
      chart = null;
    }
  };

  const loadOlder = async (oldestSec: number) => {
    if (loadingOlder || reachedStart || !chart || disposed) return;
    loadingOlder = true;
    const mine = reqSeq;
    try {
      const bs = await api.comboHistory(spec, {
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
      /* transient; a later scroll retries */
    } finally {
      loadingOlder = false;
    }
  };

  // ---------- live forming bar ----------

  const updateForming = () => {
    if (!chart || !grain.intraday || !meta) return;
    const mark = comboAt(legLast);
    if (mark == null) return;
    const now = Date.now() / 1000;
    const bucket = Math.floor(now / grain.seconds) * grain.seconds;
    if (!formingBar || bucket > formingBar.time) {
      formingBar = { time: bucket, open: mark, high: mark, low: mark, close: mark };
    } else {
      formingBar.close = mark;
      formingBar.high = Math.max(formingBar.high, mark);
      formingBar.low = Math.min(formingBar.low, mark);
    }
    chart.updateBar({ ...formingBar, volume: 0 });
  };

  // ---------- toolbar ----------

  const syncSegs = () => {
    outlet.querySelectorAll<HTMLButtonElement>("[data-range]").forEach((b) => {
      b.classList.toggle("active", b.dataset.range === range.key);
    });
    outlet.querySelectorAll<HTMLButtonElement>("[data-grain]").forEach((b) => {
      const idx = GRAINS.findIndex((g) => g.key === b.dataset.grain);
      b.classList.toggle("active", b.dataset.grain === grain.key);
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
      const gi = GRAINS.findIndex((g) => g.key === grain.key);
      if (gi < range.minGrain) grain = GRAINS[range.minGrain];
      syncSegs();
      void loadBars();
    });
  });
  outlet.querySelectorAll<HTMLButtonElement>("[data-grain]").forEach((b) => {
    b.addEventListener("click", () => {
      const idx = GRAINS.findIndex((g) => g.key === b.dataset.grain);
      if (idx < range.minGrain) return;
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
    // Held positions give us avg cost (cost line) and per-leg P&L.
    try {
      const groups = await api.positions();
      for (const g of groups) {
        for (const p of g.positions) held.set(p.instrument.con_id, p);
      }
    } catch {
      /* not critical */
    }
    if (disposed) return;
    await loadBars();
  };

  const unsub = stream.onMessage((msg) => {
    if (disposed) return;
    if (msg.type === "quote") {
      const cid = msg.quote.con_id;
      if (ratioOf(cid) === 0) return; // not one of our legs
      applyLegQuote(msg.quote);
    } else if (msg.type === "pnl") {
      const p = held.get(msg.con_id);
      if (p && ratioOf(msg.con_id) !== 0) {
        p.daily_pnl = msg.daily_pnl;
        p.unrealized_pnl = msg.unrealized_pnl;
        if (msg.market_value != null) p.market_value = msg.market_value;
        renderComboPosition();
      }
    }
  });

  const applyLegQuote = (q: Quote) => {
    const price = q.last ?? q.close;
    if (price != null) legLast.set(q.con_id, price);
    if (q.close != null) legClose.set(q.con_id, q.close);
    renderPrice();
    renderLegs();
    updateForming();
  };

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

/** Replace the URL's combo spec with its canonical form (no reload). */
function canonicalizeUrl(canonical: string): void {
  if (!canonical) return;
  const want = `#/combo/${canonical}`;
  if (location.hash !== want) {
    history.replaceState(null, "", want);
  }
}

function kv(pairs: [string, string][]): string {
  return pairs
    .map(
      ([k, v]) => `<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`,
    )
    .join("");
}

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

function escapeHtml(s: string): string {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
