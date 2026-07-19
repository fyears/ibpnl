// Home page: account tape + grouped positions table, live via WebSocket.

import { api } from "../api/client";
import { stream } from "../api/stream";
import type { AccountSummary } from "../api/types";
import { fmtMoney, fmtPnl, pnlClass } from "../lib/format";
import { PositionsTable, type ComboSelection } from "../components/positionsTable";

const TAPE_CELLS: {
  key: keyof AccountSummary;
  label: string;
  pnl?: boolean;
}[] = [
  { key: "net_liquidation", label: "Net Liq" },
  { key: "day_pnl", label: "Day P&L", pnl: true },
  { key: "unrealized_pnl", label: "Unrealized P&L", pnl: true },
  { key: "total_cash", label: "Cash" },
  { key: "buying_power", label: "Buying Power" },
  { key: "maintenance_margin", label: "Maint Margin" },
];

export function renderHome(outlet: HTMLElement): () => void {
  outlet.innerHTML = `
    <section class="tape" id="tape" aria-label="Account summary">
      ${TAPE_CELLS.map(
        (c) => `
        <div class="tape-cell">
          <div class="eyebrow">${c.label}</div>
          <div class="value skeleton" data-key="${c.key}">&nbsp;</div>
        </div>`,
      ).join("")}
    </section>
    <div class="md-note" id="md-note" hidden></div>
    <div class="page">
      <div id="positions-host">
        <div class="loading-note"><span class="spin"></span>Loading positions from your account…</div>
      </div>
    </div>
    <div class="combo-bar" id="combo-bar" hidden></div>
  `;

  let disposed = false;
  let table: PositionsTable | null = null;
  let accountTimer: number | undefined;
  let baseCurrency = "USD";
  // last polled values, so live account deltas can be flashed
  let lastNetLiq: number | null = null;
  let lastDay: number | null = null;
  let lastUnreal: number | null = null;

  const setCell = (key: keyof AccountSummary, v: number | null, pnl: boolean, flash: boolean) => {
    const tape = outlet.querySelector("#tape");
    const el = tape?.querySelector<HTMLElement>(`[data-key="${key}"]`);
    if (!el) return;
    el.classList.remove("skeleton", "gain", "loss");
    if (pnl) {
      el.textContent = fmtPnl(v, baseCurrency);
      const cls = pnlClass(v);
      if (cls !== "flat") el.classList.add(cls);
    } else {
      el.textContent = fmtMoney(v, baseCurrency);
    }
    if (flash) {
      el.classList.remove("pulse");
      void el.offsetWidth; // restart animation
      el.classList.add("pulse");
    }
  };

  const renderAccount = (acct: AccountSummary) => {
    baseCurrency = acct.base_currency;
    for (const cell of TAPE_CELLS) {
      setCell(cell.key, acct[cell.key] as number | null, !!cell.pnl, false);
    }
    lastNetLiq = acct.net_liquidation;
    lastDay = acct.day_pnl;
    lastUnreal = acct.unrealized_pnl;
    const note = outlet.querySelector<HTMLElement>("#md-note");
    if (note && acct.market_data.note) {
      note.hidden = false;
      note.textContent = `Market data — ${acct.market_data.note}`;
    }
  };

  // Live account push (WebSocket): tick the P&L cells + net liq between polls.
  const applyAccountLive = (
    day: number | null,
    unreal: number | null,
    netLiq: number | null,
  ) => {
    if (day !== null && day !== lastDay) {
      setCell("day_pnl", day, true, true);
      lastDay = day;
    }
    if (unreal !== null && unreal !== lastUnreal) {
      setCell("unrealized_pnl", unreal, true, true);
      lastUnreal = unreal;
    }
    // Prefer a fresh net liq; otherwise nudge the last one by the unrealized delta.
    const nl =
      netLiq !== null
        ? netLiq
        : lastNetLiq !== null && unreal !== null && lastUnreal !== null
          ? lastNetLiq + (unreal - lastUnreal)
          : null;
    if (nl !== null && nl !== lastNetLiq) {
      setCell("net_liquidation", nl, false, true);
      lastNetLiq = nl;
    }
  };

  const loadAccount = async () => {
    try {
      const acct = await api.account();
      if (!disposed) renderAccount(acct);
    } catch {
      /* topbar shows backend-unreachable state */
    }
  };

  const comboBar = outlet.querySelector<HTMLElement>("#combo-bar")!;
  const renderComboBar = (sel: ComboSelection | null) => {
    if (!sel || sel.legs.length === 0) {
      comboBar.hidden = true;
      comboBar.innerHTML = "";
      return;
    }
    // ratio = the leg's signed held quantity (long +, short −); backend combines
    // as sum(ratio * price), so net-credit combos read negative.
    const spec = sel.legs
      .map((l) => `${Math.round(l.quantity)}@${l.conId}`)
      .join(",");
    const n = sel.legs.length;
    comboBar.hidden = false;
    comboBar.innerHTML = `
      <span class="combo-info"><strong>${escape(sel.underlying)}</strong> combo · ${n} leg${n > 1 ? "s" : ""}</span>
      <span class="spacer"></span>
      <button type="button" class="combo-clear" id="combo-clear">Clear</button>
      <button type="button" class="combo-go" id="combo-go">View combo chart →</button>`;
    comboBar.querySelector("#combo-go")!.addEventListener("click", () => {
      location.hash = `#/combo/${spec}`;
    });
    comboBar.querySelector("#combo-clear")!.addEventListener("click", () => {
      table?.clearSelection();
    });
  };

  const load = async () => {
    await loadAccount();
    try {
      const groups = await api.positions();
      if (disposed) return;
      const host = outlet.querySelector<HTMLElement>("#positions-host")!;
      host.innerHTML = "";
      table = new PositionsTable(host);
      table.onSelectionChange(renderComboBar);
      table.setGroups(groups);
      stream.subscribeQuotes(table.conIds());
    } catch (e) {
      if (disposed) return;
      const host = outlet.querySelector<HTMLElement>("#positions-host")!;
      host.innerHTML = `
        <div class="error-note">
          <strong>Couldn't load positions.</strong>
          ${e instanceof Error ? escape(e.message) : "Unknown error."}
          Check that the backend is running, then
          <a href="javascript:location.reload()">reload</a>.
        </div>`;
    }
  };

  const unsub = stream.onMessage((msg) => {
    if (msg.type === "account") {
      applyAccountLive(msg.day_pnl, msg.unrealized_pnl, msg.net_liquidation);
      return;
    }
    if (!table) return;
    switch (msg.type) {
      case "quote":
        table.applyQuote(msg.quote);
        break;
      case "greeks":
        table.applyGreeks(msg.greeks);
        break;
      case "pnl":
        table.applyPnl(msg.con_id, msg.daily_pnl, msg.unrealized_pnl, msg.market_value);
        break;
    }
  });

  void load();
  // periodic full refresh of the slower balance-sheet cells (P&L ticks via ws)
  accountTimer = window.setInterval(loadAccount, 12000);

  return () => {
    disposed = true;
    unsub();
    window.clearInterval(accountTimer);
    stream.subscribeQuotes([]);
  };
}

function escape(s: string): string {
  return s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
