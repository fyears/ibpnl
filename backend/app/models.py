"""Pydantic models shared across the backend and mirrored by the frontend.

These are the *only* shapes the API and frontend deal with. Provider
implementations translate their native data (ib_async objects or simulated
values) into these. Times on the wire are epoch **seconds, UTC**.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SecType(str, Enum):
    """Normalized security types (superset of IB's secType we care about)."""

    STK = "STK"  # stock / ETF
    FUT = "FUT"  # future
    OPT = "OPT"  # equity / index option
    FOP = "FOP"  # future option
    IND = "IND"  # index
    CASH = "CASH"  # forex
    CFD = "CFD"
    BOND = "BOND"
    FUND = "FUND"
    CRYPTO = "CRYPTO"
    OTHER = "OTHER"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class MarketDataType(str, Enum):
    """Achieved market-data state for an instrument.

    Mirrors IB's reqMarketDataType outcomes plus an explicit 'none' for
    accounts lacking the subscription entirely.
    """

    REALTIME = "realtime"  # IB type 1
    FROZEN = "frozen"  # IB type 2 (last close snapshot; markets closed)
    DELAYED = "delayed"  # IB type 3 (~15 min)
    DELAYED_FROZEN = "delayed_frozen"  # IB type 4
    NONE = "none"  # no data permission / unavailable


class MarketSession(str, Enum):
    """Which trading session the instrument is currently in, exchange-local."""

    REGULAR = "regular"  # regular / liquid hours
    PRE = "pre"  # pre-market (before the regular open, still in trading hours)
    POST = "post"  # after-hours (after the regular close, still in trading hours)
    CLOSED = "closed"  # outside all trading hours
    UNKNOWN = "unknown"  # hours not known


class AssetClass(str, Enum):
    """Coarse grouping used for the UI 'market' badge on positions."""

    US_STOCK = "us_stock"
    US_FUTURE = "us_future"
    US_OPTION = "us_option"
    US_INDEX = "us_index"
    US_INDEX_OPTION = "us_index_option"
    US_FUTURE_OPTION = "us_future_option"
    HK_STOCK = "hk_stock"
    HK_OPTION = "hk_option"
    KR_STOCK = "kr_stock"
    KR_OPTION = "kr_option"
    OTHER = "other"


class Instrument(BaseModel):
    """A normalized tradable contract."""

    con_id: int
    symbol: str  # local/trading symbol, e.g. "AAPL", "ES", "SPX"
    sec_type: SecType
    exchange: str = ""
    currency: str = "USD"
    # Underlying used for grouping. For options/FOPs this is the underlying
    # symbol; for stocks/futures it is the symbol itself.
    underlying: str = ""
    asset_class: AssetClass = AssetClass.OTHER

    # Option / FOP fields (None for non-options)
    right: OptionRight | None = None
    strike: float | None = None
    expiry: str | None = None  # YYYYMMDD
    multiplier: float = 1.0

    # Display
    local_symbol: str = ""
    long_name: str = ""

    @property
    def is_option(self) -> bool:
        return self.sec_type in (SecType.OPT, SecType.FOP)

    def display_name(self) -> str:
        """Human label for a single leg, e.g. 'SPX 20250718 5600 C'."""
        if self.is_option and self.strike is not None and self.expiry and self.right:
            return f"{self.symbol} {self.expiry} {self.strike:g} {self.right.value}"
        if self.sec_type == SecType.FUT and self.expiry:
            return f"{self.symbol} {self.expiry}"
        return self.symbol


class Quote(BaseModel):
    """Latest pricing snapshot for an instrument."""

    con_id: int
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    close: float | None = None  # previous close
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    vwap: float | None = None
    # Option/future open interest (contracts). None for instruments without it.
    open_interest: float | None = None
    market_data_type: MarketDataType = MarketDataType.NONE
    # Which session the exchange is in right now (regular / pre / post / closed).
    market_session: MarketSession = MarketSession.UNKNOWN
    # epoch seconds UTC of last update
    timestamp: float | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.last


class Greeks(BaseModel):
    con_id: int
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    iv: float | None = None  # implied vol (fraction, e.g. 0.23)
    und_price: float | None = None
    option_price: float | None = None
    timestamp: float | None = None


class Position(BaseModel):
    """A single held leg."""

    instrument: Instrument
    quantity: float  # signed: negative = short
    avg_cost: float  # per unit incl. multiplier convention from IB
    # Per-unit average price in the same units as the price chart / quotes,
    # i.e. avg_cost normalized by the contract multiplier. Used to draw the
    # position cost line on the chart and show a comparable "Avg price".
    avg_price: float | None = None
    quote: Quote | None = None
    greeks: Greeks | None = None

    market_value: float | None = None
    unrealized_pnl: float | None = None
    daily_pnl: float | None = None
    realized_pnl: float | None = None

    @property
    def side(self) -> str:
        return "SHORT" if self.quantity < 0 else "LONG"


class PositionGroup(BaseModel):
    """Positions sharing an underlying, e.g. all SPX legs.

    `symbol` is the underlying. Totals are summed across legs (base currency
    assumed already-normalized by the provider for aggregation display).
    """

    symbol: str
    asset_class: AssetClass = AssetClass.OTHER
    currency: str = "USD"
    positions: list[Position] = Field(default_factory=list)

    total_market_value: float | None = None
    total_unrealized_pnl: float | None = None
    total_daily_pnl: float | None = None
    net_delta: float | None = None  # delta-adjusted, in underlying shares

    @property
    def leg_count(self) -> int:
        return len(self.positions)


class MarketDataCapability(BaseModel):
    """What kind of market data the account/session can see, overall."""

    default_type: MarketDataType = MarketDataType.NONE
    # human note, e.g. "Delayed data (no real-time subscription)"
    note: str = ""


class AccountValue(BaseModel):
    tag: str
    value: float | None = None
    currency: str = "USD"


class AccountSummary(BaseModel):
    account: str
    base_currency: str = "USD"
    net_liquidation: float | None = None
    total_cash: float | None = None
    buying_power: float | None = None
    gross_position_value: float | None = None
    maintenance_margin: float | None = None
    available_funds: float | None = None
    excess_liquidity: float | None = None
    day_pnl: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    market_data: MarketDataCapability = Field(default_factory=MarketDataCapability)
    # epoch seconds UTC
    updated_at: float | None = None


class Bar(BaseModel):
    """One OHLCV bar. `time` is epoch seconds UTC (start of bar)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class SessionWindow(BaseModel):
    """One regular-trading-hours window, as epoch seconds UTC [start, end)."""

    start: int
    end: int


class BarSet(BaseModel):
    con_id: int
    symbol: str
    bar_size: str = "1 min"
    # exchange timezone name, e.g. "America/New_York", for local-time display
    exchange_tz: str = "UTC"
    rth_only: bool = False
    bars: list[Bar] = Field(default_factory=list)
    # Regular-hours windows spanning the bar range, so the chart can shade the
    # extended-hours (pre/post) regions. Empty when unknown or rth_only.
    sessions: list[SessionWindow] = Field(default_factory=list)


class ComboLegInfo(BaseModel):
    """One leg of an option combo: the contract plus its signed multiple."""

    instrument: Instrument
    ratio: int  # signed: long positive, short negative


class ComboBarSet(BaseModel):
    """Combined OHLCV series for a multi-leg option combo.

    `bars`/`sessions`/`exchange_tz`/`rth_only` mirror `BarSet` so the same chart
    renders it. `bars` are in per-share price *points* (signed by leg direction);
    net-credit combos go negative. `multiplier` is the shared contract multiplier
    (points -> currency) shown as context, not applied to the series.
    """

    symbol: str  # the shared underlying
    legs: list[ComboLegInfo] = Field(default_factory=list)
    multiplier: float = 1.0
    canonical: str = ""  # canonical combo spec, "ratio@con_id,..."
    bar_size: str = "1 min"
    exchange_tz: str = "UTC"
    rth_only: bool = False
    bars: list[Bar] = Field(default_factory=list)
    sessions: list[SessionWindow] = Field(default_factory=list)


class ConnectionStatus(BaseModel):
    provider: str  # "mock" | "ib"
    connected: bool
    detail: str = ""
    account: str = ""
    server_time: float | None = None


class SearchResult(BaseModel):
    """A symbol-search hit, enough to open an instrument chart page."""

    con_id: int
    symbol: str
    sec_type: SecType
    exchange: str = ""
    currency: str = "USD"
    description: str = ""
    asset_class: AssetClass = AssetClass.OTHER


# ---- WebSocket message envelopes (server -> client) ----


class WsQuote(BaseModel):
    type: str = "quote"
    quote: Quote


class WsGreeks(BaseModel):
    type: str = "greeks"
    greeks: Greeks


class WsPnl(BaseModel):
    type: str = "pnl"
    con_id: int
    daily_pnl: float | None = None
    unrealized_pnl: float | None = None
    market_value: float | None = None


class WsBar(BaseModel):
    type: str = "bar"
    con_id: int
    bar: Bar
    # true if this bar replaces the last one (still forming), false if new bar
    update: bool = True


class WsAccount(BaseModel):
    """Live account-level totals, broadcast to every connected client."""

    type: str = "account"
    day_pnl: float | None = None
    unrealized_pnl: float | None = None
    net_liquidation: float | None = None
