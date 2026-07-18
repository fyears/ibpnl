// Thin typed REST client.

import type {
  AccountSummary,
  BarSet,
  ComboBarSet,
  ConnectionStatus,
  Greeks,
  Instrument,
  PositionGroup,
  Quote,
  SearchResult,
} from "./types";
import { reportError } from "../lib/errors";

async function get<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (err) {
    // Network-level failure (backend down, DNS, offline). Make it loud.
    reportError(`network error on GET ${path}`, err);
    throw err;
  }
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => get<ConnectionStatus>("/api/status"),
  account: () => get<AccountSummary>("/api/account"),
  positions: () => get<PositionGroup[]>("/api/positions"),
  instrument: (conId: number) => get<Instrument>(`/api/instrument/${conId}`),
  quote: (conId: number) => get<Quote>(`/api/quote/${conId}`),
  greeks: (conId: number) => get<Greeks | null>(`/api/greeks/${conId}`),
  search: (q: string) =>
    get<SearchResult[]>(`/api/search?q=${encodeURIComponent(q)}`),
  history: (
    conId: number,
    opts: {
      duration?: string;
      barSize?: string;
      rthOnly?: boolean;
      end?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.duration) params.set("duration", opts.duration);
    if (opts.barSize) params.set("bar_size", opts.barSize);
    if (opts.rthOnly !== undefined) params.set("rth_only", String(opts.rthOnly));
    if (opts.end) params.set("end", String(opts.end));
    const qs = params.toString();
    return get<BarSet>(`/api/history/${conId}${qs ? `?${qs}` : ""}`);
  },
  comboHistory: (
    spec: string,
    opts: {
      duration?: string;
      barSize?: string;
      rthOnly?: boolean;
      end?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    params.set("legs", spec);
    if (opts.duration) params.set("duration", opts.duration);
    if (opts.barSize) params.set("bar_size", opts.barSize);
    if (opts.rthOnly !== undefined) params.set("rth_only", String(opts.rthOnly));
    if (opts.end) params.set("end", String(opts.end));
    return get<ComboBarSet>(`/api/combo/history?${params.toString()}`);
  },
};
