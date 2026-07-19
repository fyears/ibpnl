// Grouped positions table.
//
// Built on @tanstack/table-core (v8, headless) for sorting / global filter /
// expand-collapse state, with hand-rolled DOM rendering (no framework).
// Top-level rows are one of:
//   * group — an underlying with >1 leg (expandable header + aggregate totals)
//   * flat  — an underlying with exactly one leg (rendered inline, no expand)
//   * leg   — a single leg beneath a group
// Group/flat rows carry the underlying's accent rail; flat and leg rows are
// clickable and navigate to the instrument chart. Live quote/pnl/greeks updates
// mutate the data and re-render the tbody with decaying flash tints.

import {
  createTable,
  getCoreRowModel,
  getExpandedRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  type ColumnDef,
  type ExpandedState,
  type Row,
  type SortingState,
  type TableOptionsResolved,
} from "@tanstack/table-core";
import type { Greeks, Instrument, Position, PositionGroup, Quote } from "../api/types";
import {
  assetClassLabel,
  fmtGreek,
  fmtIv,
  fmtMoney,
  fmtPnl,
  fmtPrice,
  fmtQty,
  legLabel,
  pnlClass,
} from "../lib/format";
import { mdtBadge, sideBadge } from "./badges";

interface GroupRow {
  kind: "group";
  symbol: string;
  group: PositionGroup;
  subRows: LegRow[];
}
interface FlatRow {
  kind: "flat";
  symbol: string;
  group: PositionGroup;
  leg: Position;
  subRows: never[];
}
interface LegRow {
  kind: "leg";
  groupSymbol: string;
  leg: Position;
  subRows: never[];
}
type RowData = GroupRow | FlatRow | LegRow;

/** A multi-leg combo selection, restricted to option legs of one underlying. */
export interface ComboSelection {
  underlying: string;
  legs: { conId: number; quantity: number; instrument: Instrument }[];
}

/** Quick per-underlying bulk-select buckets for the combo chart. */
type QuickKind = "all" | "call" | "put" | "long" | "short";

function isOptionLeg(p: Position): boolean {
  return p.instrument.sec_type === "OPT" || p.instrument.sec_type === "FOP";
}

function matchesKind(p: Position, kind: QuickKind): boolean {
  switch (kind) {
    case "all":
      return true;
    case "call":
      return p.instrument.right === "C";
    case "put":
      return p.instrument.right === "P";
    case "long":
      return p.quantity > 0;
    case "short":
      return p.quantity < 0;
  }
}

function toRows(groups: PositionGroup[]): RowData[] {
  return groups.map((g): RowData => {
    if (g.positions.length === 1) {
      return { kind: "flat", symbol: g.symbol, group: g, leg: g.positions[0], subRows: [] };
    }
    return {
      kind: "group",
      symbol: g.symbol,
      group: g,
      subRows: g.positions.map((leg) => ({
        kind: "leg" as const,
        groupSymbol: g.symbol,
        leg,
        subRows: [],
      })),
    };
  });
}

// Column accessors exist for sorting/filtering; rendering is manual below.
const columns: ColumnDef<RowData>[] = [
  {
    id: "symbol",
    accessorFn: (r) => (r.kind === "leg" ? r.groupSymbol : r.symbol),
    enableGlobalFilter: true,
  },
  {
    id: "qty",
    accessorFn: (r) =>
      r.kind === "group" ? r.group.positions.length : Math.abs(r.leg.quantity),
    enableGlobalFilter: false,
  },
  {
    id: "price",
    accessorFn: (r) => (r.kind === "group" ? undefined : r.leg.quote?.last ?? undefined),
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  {
    id: "delta",
    accessorFn: (r) =>
      r.kind === "group"
        ? r.group.net_delta ?? undefined
        : r.leg.greeks?.delta ?? undefined,
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  {
    id: "theta",
    accessorFn: (r) => (r.kind === "group" ? undefined : r.leg.greeks?.theta ?? undefined),
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  {
    id: "iv",
    accessorFn: (r) => (r.kind === "group" ? undefined : r.leg.greeks?.iv ?? undefined),
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  {
    id: "dayPnl",
    accessorFn: (r) =>
      r.kind === "group"
        ? r.group.total_daily_pnl ?? undefined
        : r.leg.daily_pnl ?? undefined,
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  {
    id: "unrealPnl",
    accessorFn: (r) =>
      r.kind === "group"
        ? r.group.total_unrealized_pnl ?? undefined
        : r.leg.unrealized_pnl ?? undefined,
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
  {
    id: "marketValue",
    accessorFn: (r) =>
      r.kind === "group"
        ? r.group.total_market_value ?? undefined
        : r.leg.market_value ?? undefined,
    sortUndefined: "last",
    enableGlobalFilter: false,
  },
];

const HEADERS: { id: string; label: string; numeric: boolean }[] = [
  { id: "symbol", label: "Symbol", numeric: false },
  { id: "qty", label: "Qty", numeric: true },
  { id: "price", label: "Price", numeric: true },
  { id: "delta", label: "Delta", numeric: true },
  { id: "theta", label: "Theta", numeric: true },
  { id: "iv", label: "IV", numeric: true },
  { id: "dayPnl", label: "Day P&L", numeric: true },
  { id: "unrealPnl", label: "Unreal P&L", numeric: true },
  { id: "marketValue", label: "Mkt Value", numeric: true },
];

export class PositionsTable {
  private host: HTMLElement;
  private data: RowData[] = [];
  private byConId = new Map<number, { groupSymbol: string; leg: Position }>();
  private table;
  private state: { sorting: SortingState; globalFilter: string; expanded: ExpandedState };
  private renderQueued = false;
  // previous values per con_id for flash detection
  private prev = new Map<number, { last?: number | null; day?: number | null; unreal?: number | null }>();
  private flash = new Map<number, { price?: "up" | "down"; day?: "up" | "down"; unreal?: "up" | "down" }>();
  // Combo multi-select: option con_ids chosen, constrained to one underlying.
  private selected = new Set<number>();
  private selUnderlying: string | null = null;
  private onSel: ((sel: ComboSelection | null) => void) | null = null;

  constructor(host: HTMLElement) {
    this.host = host;
    this.state = { sorting: [], globalFilter: "", expanded: true };

    const options: TableOptionsResolved<RowData> = {
      data: [],
      columns,
      state: this.state,
      onStateChange: () => {},
      renderFallbackValue: null,
      getCoreRowModel: getCoreRowModel(),
      getSortedRowModel: getSortedRowModel(),
      getFilteredRowModel: getFilteredRowModel(),
      getExpandedRowModel: getExpandedRowModel(),
      // No pagination row model is wired up (all rows are shown), and the
      // controlled `state` carries no `pagination` slice. Leaving the default
      // on makes filtering/sorting fire `_autoResetPageIndex()` → `setPageIndex(0)`,
      // which reads `state.pagination.pageIndex` and throws on the undefined slice.
      autoResetPageIndex: false,
      getSubRows: (r) => (r.kind === "group" ? r.subRows : undefined),
      getRowId: (r) =>
        r.kind === "group"
          ? `g:${r.symbol}`
          : r.kind === "flat"
            ? `f:${r.leg.instrument.con_id}`
            : `l:${r.leg.instrument.con_id}`,
      filterFromLeafRows: true,
      globalFilterFn: (row: Row<RowData>, _columnId, value: string) => {
        const q = value.trim().toLowerCase();
        if (!q) return true;
        const r = row.original;
        if (r.kind === "group") {
          const g = r.group;
          return (
            r.symbol.toLowerCase().includes(q) ||
            assetClassLabel(g.asset_class).toLowerCase().includes(q) ||
            g.positions.some((p) => matchesLeg(p, q))
          );
        }
        if (r.kind === "flat") {
          return r.symbol.toLowerCase().includes(q) || matchesLeg(r.leg, q);
        }
        return r.groupSymbol.toLowerCase().includes(q) || matchesLeg(r.leg, q);
      },
    };
    this.table = createTable<RowData>(options);
    this.table.setOptions((prev) => ({
      ...prev,
      state: this.state,
      onStateChange: (updater) => {
        this.state =
          typeof updater === "function" ? updater(this.state as never) : updater as never;
        this.table.setOptions((p) => ({ ...p, state: this.state }));
        this.render();
      },
    }));

    this.host.innerHTML = `
      <div class="toolbar">
        <input class="filter-input" type="search" placeholder="Filter symbol, strike, market…"
               aria-label="Filter positions" />
        <span class="spacer"></span>
        <span class="eyebrow" id="pos-count"></span>
      </div>
      <div class="table-wrap">
        <table class="positions">
          <thead><tr>
            ${HEADERS.map(
              (h) => `<th data-col="${h.id}" scope="col">${h.label}<span class="arrow"></span></th>`,
            ).join("")}
            <th>Data</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    `;

    const input = this.host.querySelector<HTMLInputElement>(".filter-input")!;
    input.addEventListener("input", () => {
      this.table.setGlobalFilter(input.value);
    });

    this.host.querySelectorAll<HTMLElement>("th[data-col]").forEach((th) => {
      th.addEventListener("click", () => {
        const col = this.table.getColumn(th.dataset.col!);
        col?.toggleSorting(undefined, false);
      });
    });
  }

  /** Full snapshot replace (initial load / refresh). */
  setGroups(groups: PositionGroup[]): void {
    this.data = toRows(groups);
    this.byConId.clear();
    for (const r of this.data) {
      if (r.kind === "group") {
        for (const l of r.subRows) {
          this.byConId.set(l.leg.instrument.con_id, { groupSymbol: r.symbol, leg: l.leg });
        }
      } else if (r.kind === "flat") {
        this.byConId.set(r.leg.instrument.con_id, { groupSymbol: r.symbol, leg: r.leg });
      }
    }
    this.table.setOptions((p) => ({ ...p, data: this.data }));
    // Drop any selected legs that no longer exist after a refresh.
    let pruned = false;
    for (const conId of [...this.selected]) {
      if (!this.byConId.has(conId)) {
        this.selected.delete(conId);
        pruned = true;
      }
    }
    if (this.selected.size === 0) this.selUnderlying = null;
    this.snapshotPrev();
    this.render();
    if (pruned) this.emitSelection();
  }

  conIds(): number[] {
    return [...this.byConId.keys()];
  }

  // ---- combo multi-select --------------------------------------------------

  /** Register a listener notified whenever the combo selection changes. */
  onSelectionChange(cb: (sel: ComboSelection | null) => void): void {
    this.onSel = cb;
  }

  clearSelection(): void {
    if (this.selected.size === 0) return;
    this.selected.clear();
    this.selUnderlying = null;
    this.render();
    this.emitSelection();
  }

  private toggleLeg(conId: number, underlying: string): void {
    if (this.selected.has(conId)) {
      this.selected.delete(conId);
      if (this.selected.size === 0) this.selUnderlying = null;
    } else {
      // Only one underlying at a time; other-underlying boxes render disabled,
      // but guard here too in case of a stale click.
      if (this.selUnderlying && this.selUnderlying !== underlying) return;
      this.selected.add(conId);
      this.selUnderlying = underlying;
    }
    this.render();
    this.emitSelection();
  }

  /** Option legs of a group, by underlying symbol. */
  private optionLegsOf(underlying: string): Position[] {
    const g = this.data.find(
      (r): r is GroupRow | FlatRow =>
        (r.kind === "group" || r.kind === "flat") && r.symbol === underlying,
    );
    if (!g) return [];
    const legs = g.kind === "group" ? g.subRows.map((l) => l.leg) : [g.leg];
    return legs.filter(isOptionLeg);
  }

  /**
   * Replace the selection with every option leg of `underlying` matching
   * `kind` (all / calls / puts / longs / shorts). Clicking the already-active
   * bucket clears it. Blocked when another underlying is already selected.
   */
  private quickSelect(underlying: string, kind: QuickKind): void {
    if (this.selUnderlying && this.selUnderlying !== underlying) return;
    const conIds = this.optionLegsOf(underlying)
      .filter((p) => matchesKind(p, kind))
      .map((p) => p.instrument.con_id);
    if (conIds.length === 0) return;
    // Toggle off if this exact bucket is already the active selection.
    const isActive =
      this.selUnderlying === underlying &&
      this.selected.size === conIds.length &&
      conIds.every((c) => this.selected.has(c));
    this.selected = new Set(isActive ? [] : conIds);
    this.selUnderlying = this.selected.size ? underlying : null;
    this.render();
    this.emitSelection();
  }

  private emitSelection(): void {
    if (!this.onSel) return;
    if (this.selected.size === 0 || !this.selUnderlying) {
      this.onSel(null);
      return;
    }
    const legs = [...this.selected]
      .map((conId) => this.byConId.get(conId))
      .filter((hit): hit is { groupSymbol: string; leg: Position } => !!hit)
      .map((hit) => ({
        conId: hit.leg.instrument.con_id,
        quantity: hit.leg.quantity,
        instrument: hit.leg.instrument,
      }))
      .sort((a, b) => a.conId - b.conId);
    this.onSel({ underlying: this.selUnderlying, legs });
  }

  // ---- live updates --------------------------------------------------------

  applyQuote(q: Quote): void {
    const hit = this.byConId.get(q.con_id);
    if (!hit) return;
    const old = this.prev.get(q.con_id);
    if (old && old.last != null && q.last != null && q.last !== old.last) {
      this.flashSet(q.con_id, "price", q.last > old.last ? "up" : "down");
    }
    hit.leg.quote = q;
    this.queueRender();
  }

  applyGreeks(g: Greeks): void {
    const hit = this.byConId.get(g.con_id);
    if (!hit) return;
    hit.leg.greeks = g;
    this.queueRender();
  }

  applyPnl(conId: number, daily: number | null, unreal: number | null, mv: number | null): void {
    const hit = this.byConId.get(conId);
    if (!hit) return;
    const old = this.prev.get(conId);
    if (old && old.day != null && daily != null && daily !== old.day) {
      this.flashSet(conId, "day", daily > old.day ? "up" : "down");
    }
    if (old && old.unreal != null && unreal != null && unreal !== old.unreal) {
      this.flashSet(conId, "unreal", unreal > old.unreal ? "up" : "down");
    }
    hit.leg.daily_pnl = daily;
    hit.leg.unrealized_pnl = unreal;
    if (mv != null) hit.leg.market_value = mv;
    this.recomputeAggregates();
    this.queueRender();
  }

  private flashSet(conId: number, key: "price" | "day" | "unreal", dir: "up" | "down"): void {
    const f = this.flash.get(conId) ?? {};
    f[key] = dir;
    this.flash.set(conId, f);
  }

  private recomputeAggregates(): void {
    for (const r of this.data) {
      if (r.kind !== "group") continue;
      const g = r.group;
      const legs = r.subRows.map((l) => l.leg);
      g.total_daily_pnl = sumOrNull(legs.map((l) => l.daily_pnl));
      g.total_unrealized_pnl = sumOrNull(legs.map((l) => l.unrealized_pnl));
      g.total_market_value = sumOrNull(legs.map((l) => l.market_value));
      g.net_delta = sumOrNull(
        legs.map((l) => {
          const inst = l.instrument;
          if (inst.sec_type === "OPT" || inst.sec_type === "FOP") {
            if (l.greeks?.delta == null) return null;
            return l.greeks.delta * l.quantity * inst.multiplier;
          }
          if (inst.sec_type === "STK") return l.quantity;
          if (inst.sec_type === "FUT") return l.quantity * inst.multiplier;
          return null;
        }),
      );
    }
  }

  private snapshotPrev(): void {
    this.prev.clear();
    for (const [conId, { leg }] of this.byConId) {
      this.prev.set(conId, {
        last: leg.quote?.last,
        day: leg.daily_pnl,
        unreal: leg.unrealized_pnl,
      });
    }
  }

  private queueRender(): void {
    if (this.renderQueued) return;
    this.renderQueued = true;
    window.setTimeout(() => {
      this.renderQueued = false;
      this.render();
    }, 400); // batch bursts of ws messages
  }

  // ---- rendering -----------------------------------------------------------

  render(): void {
    const tbody = this.host.querySelector("tbody");
    if (!tbody) return;

    // header sort arrows
    const sorting = this.state.sorting;
    this.host.querySelectorAll<HTMLElement>("th[data-col]").forEach((th) => {
      const s = sorting.find((x) => x.id === th.dataset.col);
      th.querySelector(".arrow")!.textContent = s ? (s.desc ? " ↓" : " ↑") : "";
      th.setAttribute("aria-sort", s ? (s.desc ? "descending" : "ascending") : "none");
    });

    const rows = this.table.getRowModel().rows;
    tbody.innerHTML = rows.map((row) => this.rowHtml(row)).join("");

    // group expand/collapse
    tbody.querySelectorAll<HTMLElement>("tr.group-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        const row = this.table.getRowModel().rows.find((r) => r.id === tr.dataset.rowId);
        row?.toggleExpanded();
      });
    });
    // whole-row navigation to the instrument chart
    tbody.querySelectorAll<HTMLElement>("tr[data-nav]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        const t = e.target as HTMLElement;
        if (t.closest("a")) return; // let real links work
        if (t.closest("input.leg-select")) return; // combo checkbox handles itself
        location.hash = `#/i/${tr.dataset.nav}`;
      });
    });
    // combo multi-select checkboxes
    tbody.querySelectorAll<HTMLInputElement>("input.leg-select").forEach((cb) => {
      cb.addEventListener("click", (e) => e.stopPropagation()); // don't nav the row
      cb.addEventListener("change", () => {
        this.toggleLeg(Number(cb.dataset.con), cb.dataset.und ?? "");
      });
    });
    // combo quick-select chips on group headers (don't toggle expand/collapse)
    tbody.querySelectorAll<HTMLButtonElement>("button.quick-chip").forEach((chip) => {
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        if (chip.disabled) return;
        this.quickSelect(chip.dataset.und ?? "", chip.dataset.kind as QuickKind);
      });
    });

    const count = this.host.querySelector("#pos-count");
    if (count) {
      count.textContent = `${this.data.length} underlyings · ${this.byConId.size} legs`;
    }

    this.snapshotPrev();
    this.flash.clear();
  }

  private flashCls(conId: number, k: "price" | "day" | "unreal"): string {
    const f = this.flash.get(conId) ?? {};
    return f[k] ? ` flash-${f[k]}` : "";
  }

  /** Combo-select checkbox for an option leg (disabled if another underlying is active). */
  private selectBox(conId: number, underlying: string): string {
    const checked = this.selected.has(conId) ? " checked" : "";
    const disabled =
      this.selUnderlying && this.selUnderlying !== underlying ? " disabled" : "";
    const title = disabled
      ? "Combo legs must share one underlying"
      : "Select for combo chart";
    return `<input type="checkbox" class="leg-select" data-con="${conId}" data-und="${escapeHtml(underlying)}"${checked}${disabled} title="${title}" aria-label="Select option leg for combo">`;
  }

  /**
   * A full-width row of quick bulk-select chips (All / Calls / Puts / Long /
   * Short) beneath a group header. A filter chip is shown only when it selects
   * a non-empty proper subset; each is a toggle that replaces the selection.
   * Rendered as its own row so it isn't clipped by the sticky first column.
   */
  private quickRow(underlying: string): string {
    const legs = this.optionLegsOf(underlying);
    if (legs.length < 2) return ""; // single leg → the checkbox is enough
    const total = legs.length;
    const count = (k: QuickKind) => legs.filter((p) => matchesKind(p, k)).length;
    const buckets: { kind: QuickKind; label: string }[] = [
      { kind: "all", label: "All" },
      { kind: "call", label: "Calls" },
      { kind: "put", label: "Puts" },
      { kind: "long", label: "Long" },
      { kind: "short", label: "Short" },
    ];
    const disabled = this.selUnderlying != null && this.selUnderlying !== underlying;
    const chips = buckets
      .map((b) => ({ ...b, n: count(b.kind) }))
      // "all" always shown; filters only when they carve a proper, non-empty subset
      .filter((b) => (b.kind === "all" ? true : b.n > 0 && b.n < total))
      .map((b) => {
        const active =
          !disabled &&
          this.selUnderlying === underlying &&
          this.selected.size === b.n &&
          legs
            .filter((p) => matchesKind(p, b.kind))
            .every((p) => this.selected.has(p.instrument.con_id));
        return `<button type="button" class="quick-chip${active ? " active" : ""}" data-und="${escapeHtml(underlying)}" data-kind="${b.kind}"${disabled ? " disabled" : ""} title="Select all ${b.label.toLowerCase()} option legs for a combo chart">${b.label}</button>`;
      })
      .join("");
    const tip = disabled ? "Clear the current combo selection first" : "Combo quick-select";
    return `<tr class="quick-row"><td colspan="${HEADERS.length + 1}">
      <div class="quick-inner"><span class="quick-lead" title="${tip}">Combo:</span>${chips}</div>
    </td></tr>`;
  }

  private rowHtml(row: Row<RowData>): string {
    const r = row.original;
    if (r.kind === "group") {
      const g = r.group;
      const expanded = row.getIsExpanded();
      return `
        <tr class="group-row ${expanded ? "" : "collapsed"}" data-row-id="${row.id}"
            aria-expanded="${expanded}">
          <td><span class="caret">▾</span>${escapeHtml(r.symbol)}
            <span class="group-meta">${assetClassLabel(g.asset_class)} · ${g.positions.length} legs</span>
          </td>
          <td class="num">${g.positions.length}</td>
          <td class="num"></td>
          <td class="num">${g.net_delta != null ? fmtGreek(g.net_delta, 0) : "—"}</td>
          <td class="num"></td>
          <td class="num"></td>
          <td class="num"><span class="${pnlClass(g.total_daily_pnl)}">${fmtPnl(g.total_daily_pnl, g.currency)}</span></td>
          <td class="num"><span class="${pnlClass(g.total_unrealized_pnl)}">${fmtPnl(g.total_unrealized_pnl, g.currency)}</span></td>
          <td class="num">${fmtMoney(g.total_market_value, g.currency)}</td>
          <td></td>
        </tr>${this.quickRow(r.symbol)}`;
    }

    if (r.kind === "flat") {
      const leg = r.leg;
      const inst = leg.instrument;
      const conId = inst.con_id;
      const q = leg.quote;
      return `
        <tr class="flat-row" data-nav="${conId}">
          <td>
            ${isOptionLeg(leg) ? this.selectBox(conId, r.symbol) : `<span class="sel-pad"></span>`}
            <a class="flat-sym mono" href="#/i/${conId}">${escapeHtml(r.symbol)}</a>
            <span class="group-meta">${assetClassLabel(r.group.asset_class)} · ${escapeHtml(legLabel(inst))}</span>
          </td>
          <td class="num">${sideBadge(leg.quantity)} ${fmtQty(Math.abs(leg.quantity))}</td>
          <td class="num${this.flashCls(conId, "price")}">${fmtPrice(q?.last)}</td>
          <td class="num">${fmtGreek(leg.greeks?.delta)}</td>
          <td class="num">${fmtGreek(leg.greeks?.theta, 2)}</td>
          <td class="num">${fmtIv(leg.greeks?.iv)}</td>
          <td class="num${this.flashCls(conId, "day")}"><span class="${pnlClass(leg.daily_pnl)}">${fmtPnl(leg.daily_pnl, inst.currency)}</span></td>
          <td class="num${this.flashCls(conId, "unreal")}"><span class="${pnlClass(leg.unrealized_pnl)}">${fmtPnl(leg.unrealized_pnl, inst.currency)}</span></td>
          <td class="num">${fmtMoney(leg.market_value, inst.currency)}</td>
          <td>${q ? mdtBadge(q.market_data_type) : ""}</td>
        </tr>`;
    }

    const leg = r.leg;
    const inst = leg.instrument;
    const conId = inst.con_id;
    const q = leg.quote;
    return `
      <tr class="leg-row" data-nav="${conId}">
        <td>
          ${isOptionLeg(leg) ? this.selectBox(conId, r.groupSymbol) : `<span class="sel-pad"></span>`}
          <a class="leg-link mono" href="#/i/${conId}">${escapeHtml(legLabel(inst))}</a>
          <span class="leg-kind">${escapeHtml(inst.exchange)}</span>
        </td>
        <td class="num">${sideBadge(leg.quantity)} ${fmtQty(Math.abs(leg.quantity))}</td>
        <td class="num${this.flashCls(conId, "price")}">${fmtPrice(q?.last)}</td>
        <td class="num">${fmtGreek(leg.greeks?.delta)}</td>
        <td class="num">${fmtGreek(leg.greeks?.theta, 2)}</td>
        <td class="num">${fmtIv(leg.greeks?.iv)}</td>
        <td class="num${this.flashCls(conId, "day")}"><span class="${pnlClass(leg.daily_pnl)}">${fmtPnl(leg.daily_pnl, inst.currency)}</span></td>
        <td class="num${this.flashCls(conId, "unreal")}"><span class="${pnlClass(leg.unrealized_pnl)}">${fmtPnl(leg.unrealized_pnl, inst.currency)}</span></td>
        <td class="num">${fmtMoney(leg.market_value, inst.currency)}</td>
        <td>${q ? mdtBadge(q.market_data_type) : ""}</td>
      </tr>`;
  }
}

function matchesLeg(p: Position, q: string): boolean {
  return (
    legLabel(p.instrument).toLowerCase().includes(q) ||
    p.instrument.local_symbol.toLowerCase().includes(q) ||
    p.instrument.exchange.toLowerCase().includes(q) ||
    assetClassLabel(p.instrument.asset_class).toLowerCase().includes(q)
  );
}

function sumOrNull(values: (number | null | undefined)[]): number | null {
  const present = values.filter((v): v is number => v != null);
  return present.length ? present.reduce((a, b) => a + b, 0) : null;
}

function escapeHtml(s: string): string {
  return s
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
