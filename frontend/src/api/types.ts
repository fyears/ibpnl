// Mirrors backend/app/models.py. Keep in sync when the backend changes.

export type SecType =
  | "STK" | "FUT" | "OPT" | "FOP" | "IND"
  | "CASH" | "CFD" | "BOND" | "FUND" | "CRYPTO" | "OTHER";

export type MarketDataType =
  | "realtime" | "frozen" | "delayed" | "delayed_frozen" | "none";

export type MarketSession =
  | "regular" | "pre" | "post" | "closed" | "unknown";

export type AssetClass =
  | "us_stock" | "us_future" | "us_option" | "us_index" | "us_index_option"
  | "us_future_option" | "hk_stock" | "hk_option" | "kr_stock" | "kr_option"
  | "other";

export interface Instrument {
  con_id: number;
  symbol: string;
  sec_type: SecType;
  exchange: string;
  currency: string;
  underlying: string;
  asset_class: AssetClass;
  right: "C" | "P" | null;
  strike: number | null;
  expiry: string | null; // YYYYMMDD
  multiplier: number;
  local_symbol: string;
  long_name: string;
}

export interface Quote {
  con_id: number;
  last: number | null;
  bid: number | null;
  ask: number | null;
  bid_size: number | null;
  ask_size: number | null;
  close: number | null;
  open: number | null;
  high: number | null;
  low: number | null;
  volume: number | null;
  vwap: number | null;
  open_interest: number | null;
  market_data_type: MarketDataType;
  market_session: MarketSession;
  timestamp: number | null;
}

export interface Greeks {
  con_id: number;
  delta: number | null;
  gamma: number | null;
  vega: number | null;
  theta: number | null;
  iv: number | null;
  und_price: number | null;
  option_price: number | null;
  timestamp: number | null;
}

export interface Position {
  instrument: Instrument;
  quantity: number;
  avg_cost: number;
  avg_price: number | null;
  quote: Quote | null;
  greeks: Greeks | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  daily_pnl: number | null;
  realized_pnl: number | null;
}

export interface PositionGroup {
  symbol: string;
  asset_class: AssetClass;
  currency: string;
  positions: Position[];
  total_market_value: number | null;
  total_unrealized_pnl: number | null;
  total_daily_pnl: number | null;
  net_delta: number | null;
}

export interface MarketDataCapability {
  default_type: MarketDataType;
  note: string;
}

export interface AccountSummary {
  account: string;
  base_currency: string;
  net_liquidation: number | null;
  total_cash: number | null;
  buying_power: number | null;
  gross_position_value: number | null;
  maintenance_margin: number | null;
  available_funds: number | null;
  excess_liquidity: number | null;
  day_pnl: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number | null;
  market_data: MarketDataCapability;
  updated_at: number | null;
}

export interface Bar {
  time: number; // epoch seconds UTC
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface SessionWindow {
  start: number; // epoch seconds UTC
  end: number;
}

export interface BarSet {
  con_id: number;
  symbol: string;
  bar_size: string;
  exchange_tz: string;
  rth_only: boolean;
  bars: Bar[];
  sessions: SessionWindow[];
}

export interface ComboLegInfo {
  instrument: Instrument;
  ratio: number; // signed: long positive, short negative
}

export interface ComboBarSet {
  symbol: string; // shared underlying
  legs: ComboLegInfo[];
  multiplier: number;
  canonical: string; // canonical combo spec "ratio@con_id,..."
  bar_size: string;
  exchange_tz: string;
  rth_only: boolean;
  bars: Bar[];
  sessions: SessionWindow[];
}

export interface ConnectionStatus {
  provider: string;
  connected: boolean;
  detail: string;
  account: string;
  server_time: number | null;
}

export interface SearchResult {
  con_id: number;
  symbol: string;
  sec_type: SecType;
  exchange: string;
  currency: string;
  description: string;
  asset_class: AssetClass;
}

// WebSocket server->client messages
export type WsMessage =
  | { type: "quote"; quote: Quote }
  | { type: "greeks"; greeks: Greeks }
  | {
      type: "pnl";
      con_id: number;
      daily_pnl: number | null;
      unrealized_pnl: number | null;
      market_value: number | null;
    }
  | { type: "bar"; con_id: number; bar: Bar; update: boolean }
  | {
      type: "account";
      day_pnl: number | null;
      unrealized_pnl: number | null;
      net_liquidation: number | null;
    }
  | { type: "pong" };
