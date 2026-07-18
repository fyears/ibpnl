// Small badge builders shared by pages.

import type { MarketDataType } from "../api/types";
import { mdtLabel } from "../lib/format";

export function mdtBadge(t: MarketDataType): string {
  const cls =
    t === "realtime" ? "live" : t === "none" ? "nodata" : "";
  return `<span class="badge ${cls}" title="Market data: ${mdtLabel(t)}">${mdtLabel(t)}</span>`;
}

export function sideBadge(quantity: number): string {
  const side = quantity < 0 ? "short" : "long";
  return `<span class="badge ${side}">${side.toUpperCase()}</span>`;
}
