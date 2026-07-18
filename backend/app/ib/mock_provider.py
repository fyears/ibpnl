"""MockProvider — a deterministic, realistic simulation of an IBKR account.

Purpose: let the entire dashboard (API, WebSocket streams, and frontend) be
developed and tested without a live IB Gateway. It produces:

  * a cross-market portfolio (US stock/future/option/index-option/future-option,
    HK stock/option, KR stock), with long and short legs;
  * account summary numbers;
  * per-instrument quotes with a configurable market-data state
    (realtime/delayed/frozen/none/mixed) to exercise the UI;
  * Black-Scholes Greeks for options that move with the underlying;
  * a week of 1-minute OHLCV bars with regular + extended sessions;
  * a background loop that random-walks spot prices and pushes live quote /
    greeks / pnl / bar updates to subscribers.

Everything is seeded so a given run is reproducible, while still "moving".
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings
from app.models import (
    AccountSummary,
    AssetClass,
    Bar,
    BarSet,
    ConnectionStatus,
    Greeks,
    Instrument,
    MarketDataCapability,
    MarketDataType,
    MarketSession,
    OptionRight,
    Position,
    PositionGroup,
    Quote,
    SearchResult,
    SecType,
    SessionWindow,
)
from app.ib.provider import MarketDataProvider
from app.services.blackscholes import bs_price_greeks
from app.services.grouping import group_positions

log = logging.getLogger(__name__)


# --- Static portfolio specification -------------------------------------------------

class _Underlying:
    """A tradable underlying with a base spot and volatility, plus session info."""

    def __init__(
        self,
        con_id: int,
        symbol: str,
        sec_type: SecType,
        exchange: str,
        currency: str,
        asset_class: AssetClass,
        spot: float,
        vol: float,
        tz: str,
        long_name: str,
        multiplier: float = 1.0,
        session: tuple[str, str] = ("09:30", "16:00"),
        ext_session: tuple[str, str] | None = ("04:00", "20:00"),
    ):
        self.con_id = con_id
        self.symbol = symbol
        self.sec_type = sec_type
        self.exchange = exchange
        self.currency = currency
        self.asset_class = asset_class
        self.spot = spot
        self.vol = vol
        self.tz = tz
        self.long_name = long_name
        self.multiplier = multiplier
        self.session = session
        self.ext_session = ext_session


# Underlyings across markets. con_ids are arbitrary but stable.
_UNDERLYINGS: list[_Underlying] = [
    _Underlying(1001, "AAPL", SecType.STK, "NASDAQ", "USD", AssetClass.US_STOCK,
                229.50, 0.24, "America/New_York", "Apple Inc."),
    _Underlying(1002, "NVDA", SecType.STK, "NASDAQ", "USD", AssetClass.US_STOCK,
                171.20, 0.42, "America/New_York", "NVIDIA Corp."),
    _Underlying(1003, "ES", SecType.FUT, "CME", "USD", AssetClass.US_FUTURE,
                5638.0, 0.16, "America/New_York", "E-mini S&P 500 Future",
                multiplier=50, session=("09:30", "16:00"), ext_session=("18:00", "17:00")),
    _Underlying(1004, "SPX", SecType.IND, "CBOE", "USD", AssetClass.US_INDEX,
                5632.0, 0.15, "America/New_York", "S&P 500 Index",
                ext_session=None),
    _Underlying(1005, "0700", SecType.STK, "SEHK", "HKD", AssetClass.HK_STOCK,
                412.4, 0.30, "Asia/Hong_Kong", "Tencent Holdings Ltd.",
                session=("09:30", "16:00"), ext_session=None),
    _Underlying(1006, "005930", SecType.STK, "KSE", "KRW", AssetClass.KR_STOCK,
                81300.0, 0.28, "Asia/Seoul", "Samsung Electronics Co.",
                session=("09:00", "15:30"), ext_session=None),
    # A market whose regular session closes at 16:30 — exercises the chart's
    # close-straddling bar (an hourly 16:00 bar must read as regular, not ext).
    _Underlying(1007, "LCLZ", SecType.STK, "ARCA", "USD", AssetClass.US_STOCK,
                48.0, 0.35, "America/New_York", "Late-Close Test Corp.",
                session=("09:30", "16:30")),
]

_UND_BY_SYMBOL = {u.symbol: u for u in _UNDERLYINGS}


def _to_minutes(session: tuple[str, str]) -> tuple[int, int]:
    """('09:30','16:00') -> (570, 960) minutes-since-midnight."""
    sh, sm = (int(x) for x in session[0].split(":"))
    eh, em = (int(x) for x in session[1].split(":"))
    return (sh * 60 + sm, eh * 60 + em)


def _bar_seconds(bar_size: str) -> int:
    """Seconds per bar for the given IB bar-size string (for live bucketing)."""
    s = bar_size.lower()
    num = "".join(ch for ch in s if ch.isdigit())
    n = int(num) if num else 1
    if "sec" in s:
        return n
    if "min" in s:
        return n * 60
    if "hour" in s:
        return n * 3600
    if "day" in s:
        return n * 86400
    if "week" in s:
        return n * 604800
    return 60


def _duration_seconds(duration: str) -> int:
    """Seconds spanned by an IB duration string like '1 W', '3 M', '1 Y'."""
    try:
        n_str, unit = duration.split()
        n = int(n_str)
    except ValueError:
        return 7 * 86400
    u = unit.upper()[:1]
    return {
        "S": n,
        "D": n * 86400,
        "W": n * 7 * 86400,
        "M": n * 30 * 86400,
        "Y": n * 365 * 86400,
    }.get(u, 7 * 86400)


def _hash01(a: int, b: int) -> float:
    """Deterministic pseudo-random value in [0, 1) from two integers.

    Pure integer mixing (no reliance on Python's salted str hashing), so a bar's
    value depends only on its timestamp — lazily loaded older chunks line up
    seamlessly with what's already on the chart.
    """
    n = (a * 2654435761 + b * 40503 + 12345) & 0xFFFFFFFF
    n = ((n ^ (n >> 15)) * 2246822519) & 0xFFFFFFFF
    n = ((n ^ (n >> 13)) * 3266489917) & 0xFFFFFFFF
    n = (n ^ (n >> 16)) & 0xFFFFFFFF
    return n / 0x100000000


# Rough FX rates to USD (account base currency) — for cross-currency sorting.
_FX_TO_USD = {"USD": 1.0, "HKD": 0.128, "KRW": 0.00072}


class _Leg:
    """A held position spec, resolved into an Instrument at build time."""

    def __init__(
        self,
        con_id: int,
        underlying: str,
        sec_type: SecType,
        quantity: float,
        avg_cost: float,
        *,
        right: OptionRight | None = None,
        strike: float | None = None,
        expiry: str | None = None,
        multiplier: float | None = None,
        asset_class: AssetClass | None = None,
        exchange: str | None = None,
        symbol: str | None = None,
    ):
        self.con_id = con_id
        self.underlying = underlying
        self.sec_type = sec_type
        self.quantity = quantity
        self.avg_cost = avg_cost
        self.right = right
        self.strike = strike
        self.expiry = expiry
        self.multiplier = multiplier
        self.asset_class = asset_class
        self.exchange = exchange
        self.symbol = symbol


def _near_expiry(days: int) -> str:
    # Deterministic expiry relative to a fixed anchor so the mock is stable.
    anchor = datetime(2026, 7, 17, tzinfo=timezone.utc)
    return (anchor + timedelta(days=days)).strftime("%Y%m%d")


# The held portfolio. Multiple SPX option legs => the "SPX group" requirement.
_LEGS: list[_Leg] = [
    # US stocks (long + a short)
    _Leg(2001, "AAPL", SecType.STK, 300, 198.4),
    _Leg(2002, "NVDA", SecType.STK, -150, 178.9),  # short
    # US equity options on AAPL (a vertical-ish pair)
    _Leg(2003, "AAPL", SecType.OPT, -3, 6.10, right=OptionRight.CALL,
         strike=240, expiry=_near_expiry(30), multiplier=100,
         asset_class=AssetClass.US_OPTION, exchange="CBOE"),
    _Leg(2004, "AAPL", SecType.OPT, 3, 2.35, right=OptionRight.CALL,
         strike=250, expiry=_near_expiry(30), multiplier=100,
         asset_class=AssetClass.US_OPTION, exchange="CBOE"),
    # US future
    _Leg(2005, "ES", SecType.FUT, 2, 5555.0, expiry=_near_expiry(60),
         multiplier=50, exchange="CME"),
    # US index options on SPX (several legs -> the grouping showcase)
    _Leg(2006, "SPX", SecType.OPT, -5, 41.2, right=OptionRight.PUT,
         strike=5500, expiry=_near_expiry(9), multiplier=100,
         asset_class=AssetClass.US_INDEX_OPTION, exchange="CBOE"),
    _Leg(2007, "SPX", SecType.OPT, 5, 22.8, right=OptionRight.PUT,
         strike=5400, expiry=_near_expiry(9), multiplier=100,
         asset_class=AssetClass.US_INDEX_OPTION, exchange="CBOE"),
    _Leg(2008, "SPX", SecType.OPT, -3, 55.0, right=OptionRight.CALL,
         strike=5750, expiry=_near_expiry(37), multiplier=100,
         asset_class=AssetClass.US_INDEX_OPTION, exchange="CBOE"),
    # US future option on ES (FOP)
    _Leg(2009, "ES", SecType.FOP, -2, 30.5, right=OptionRight.CALL,
         strike=5800, expiry=_near_expiry(30), multiplier=50,
         asset_class=AssetClass.US_FUTURE_OPTION, exchange="CME"),
    # HK stock + HK option
    _Leg(2010, "0700", SecType.STK, 500, 380.0, exchange="SEHK"),
    _Leg(2011, "0700", SecType.OPT, 5, 12.5, right=OptionRight.CALL,
         strike=420, expiry=_near_expiry(44), multiplier=100,
         asset_class=AssetClass.HK_OPTION, exchange="SEHK"),
    # KR stock
    _Leg(2012, "005930", SecType.STK, 100, 74000.0, exchange="KSE"),
]


class MockProvider(MarketDataProvider):
    def __init__(self) -> None:
        self._quote_cbs = []
        self._greeks_cbs = []
        self._pnl_cbs = []
        self._bar_cbs = []
        self._account_cbs = []

        self._rng = random.Random(42)
        # Live spot state per underlying symbol (walks over time).
        self._spot: dict[str, float] = {u.symbol: u.spot for u in _UNDERLYINGS}
        self._day_open_spot: dict[str, float] = {}
        self._instruments: dict[int, Instrument] = {}
        self._positions: dict[int, Position] = {}
        self._subscribed: set[int] = set()
        self._bar_subscribed: set[int] = set()
        self._bar_size: dict[int, str] = {}  # con_id -> requested live bar size
        # last streamed bar per con_id, for live-bar updates
        self._live_bar: dict[int, Bar] = {}
        self._task: asyncio.Task | None = None
        self._connected = False

    # --- lifecycle ---

    async def start(self) -> None:
        self._build_portfolio()
        self._connected = True
        self._task = asyncio.create_task(self._run_ticks(), name="mock-ticks")
        log.info("MockProvider started with %d positions", len(self._positions))

    async def stop(self) -> None:
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("MockProvider stopped")

    def status(self) -> ConnectionStatus:
        return ConnectionStatus(
            provider="mock",
            connected=self._connected,
            detail="Simulated data (no IB connection)",
            account="DU-MOCK-001",
            server_time=self._now(),
        )

    # --- build ---

    def _market_data_type_for(self, asset_class: AssetClass) -> MarketDataType:
        state = settings.mock_md_state.lower()
        if state == "mixed":
            # Give different markets different states to exercise the UI.
            mapping = {
                AssetClass.US_STOCK: MarketDataType.REALTIME,
                AssetClass.US_FUTURE: MarketDataType.REALTIME,
                AssetClass.US_OPTION: MarketDataType.REALTIME,
                AssetClass.US_INDEX: MarketDataType.DELAYED,
                AssetClass.US_INDEX_OPTION: MarketDataType.REALTIME,
                AssetClass.US_FUTURE_OPTION: MarketDataType.DELAYED,
                AssetClass.HK_STOCK: MarketDataType.DELAYED,
                AssetClass.HK_OPTION: MarketDataType.DELAYED,
                AssetClass.KR_STOCK: MarketDataType.NONE,  # no permission
            }
            return mapping.get(asset_class, MarketDataType.DELAYED)
        return {
            "realtime": MarketDataType.REALTIME,
            "delayed": MarketDataType.DELAYED,
            "frozen": MarketDataType.FROZEN,
            "none": MarketDataType.NONE,
        }.get(state, MarketDataType.REALTIME)

    def _build_portfolio(self) -> None:
        for leg in _LEGS:
            und = _UND_BY_SYMBOL[leg.underlying]
            symbol = leg.symbol or leg.underlying
            inst = Instrument(
                con_id=leg.con_id,
                symbol=symbol,
                sec_type=leg.sec_type,
                exchange=leg.exchange or und.exchange,
                currency=und.currency,
                underlying=leg.underlying,
                asset_class=leg.asset_class or und.asset_class,
                right=leg.right,
                strike=leg.strike,
                expiry=leg.expiry,
                multiplier=leg.multiplier or und.multiplier,
                long_name=und.long_name,
                local_symbol=symbol,
            )
            self._instruments[leg.con_id] = inst
            # seed day open near current spot for daily pnl
            self._day_open_spot.setdefault(
                leg.underlying, self._spot[leg.underlying] * (1 - 0.004)
            )
            pos = Position(
                instrument=inst,
                quantity=leg.quantity,
                avg_cost=leg.avg_cost,
                avg_price=leg.avg_cost,  # mock avg_cost is already per-unit
            )
            self._positions[leg.con_id] = pos
            self._revalue(pos)

        # Also register the pure underlyings as instruments (for detail pages
        # of things we hold options on but not the underlying itself, e.g. SPX).
        for und in _UNDERLYINGS:
            if und.con_id not in self._instruments:
                self._instruments[und.con_id] = Instrument(
                    con_id=und.con_id,
                    symbol=und.symbol,
                    sec_type=und.sec_type,
                    exchange=und.exchange,
                    currency=und.currency,
                    underlying=und.symbol,
                    asset_class=und.asset_class,
                    multiplier=und.multiplier,
                    long_name=und.long_name,
                    local_symbol=und.symbol,
                )

    # --- pricing helpers ---

    def _price_for(self, inst: Instrument) -> tuple[float, Greeks | None]:
        """Compute a current price (and greeks for options) from live spot."""
        und = _UND_BY_SYMBOL.get(inst.underlying)
        spot = self._spot.get(inst.underlying, 100.0)
        if inst.is_option and inst.strike and inst.expiry:
            expiry_dt = datetime.strptime(inst.expiry, "%Y%m%d").replace(tzinfo=timezone.utc)
            t_years = max((expiry_dt - datetime.now(timezone.utc)).total_seconds(), 3600) / (
                365 * 24 * 3600
            )
            vol = (und.vol if und else 0.25) + 0.05
            g = bs_price_greeks(
                spot=spot,
                strike=inst.strike,
                t_years=t_years,
                vol=vol,
                is_call=inst.right == OptionRight.CALL,
            )
            greeks = Greeks(
                con_id=inst.con_id,
                delta=g["delta"],
                gamma=g["gamma"],
                vega=g["vega"],
                theta=g["theta"],
                iv=g["iv"],
                und_price=spot,
                option_price=g["price"],
                timestamp=self._now(),
            )
            return g["price"], greeks
        # non-option: price is the spot
        return spot, None

    def _revalue(self, pos: Position) -> None:
        inst = pos.instrument
        price, greeks = self._price_for(inst)
        mdt = self._market_data_type_for(inst.asset_class)
        day_open = self._day_open_spot.get(inst.underlying, self._spot[inst.underlying])
        # previous close approximated by day open for the underlying
        prev_close_spot = day_open
        if inst.is_option:
            # recompute an approximate previous close price for the option
            prev_option_price, _ = self._price_at_spot(inst, prev_close_spot)
            close_price = prev_option_price
        else:
            close_price = prev_close_spot

        no_data = mdt == MarketDataType.NONE
        oi = None
        if inst.is_option:
            oi = float(self._rng.randint(200, 60000))
        elif inst.sec_type == SecType.FUT:
            oi = float(self._rng.randint(50000, 2000000))
        quote = Quote(
            con_id=inst.con_id,
            last=None if no_data else round(price, 4),
            bid=None if no_data else round(price * 0.999, 4),
            ask=None if no_data else round(price * 1.001, 4),
            bid_size=None if no_data else float(self._rng.randint(1, 40) * 100),
            ask_size=None if no_data else float(self._rng.randint(1, 40) * 100),
            close=round(close_price, 4),
            open=round(close_price, 4),
            vwap=None if no_data else round(price * (1 + self._rng.uniform(-0.002, 0.002)), 4),
            open_interest=oi,
            volume=None if no_data else float(self._rng.randint(1000, 500000)),
            market_data_type=mdt,
            market_session=self._session_for(inst),
            timestamp=self._now(),
        )
        pos.quote = quote
        pos.greeks = greeks
        mult = inst.multiplier
        signed_mult = mult
        mkt_price = price if not no_data else close_price
        pos.market_value = round(pos.quantity * mkt_price * signed_mult, 2)
        cost_basis = pos.quantity * pos.avg_cost * signed_mult
        pos.unrealized_pnl = round(pos.market_value - cost_basis, 2)
        prev_val = pos.quantity * close_price * signed_mult
        pos.daily_pnl = round(pos.market_value - prev_val, 2)
        pos.realized_pnl = 0.0

    def _price_at_spot(self, inst: Instrument, spot: float) -> tuple[float, float]:
        """Return (price, price) for an instrument at a given spot (for close calc)."""
        if inst.is_option and inst.strike and inst.expiry:
            expiry_dt = datetime.strptime(inst.expiry, "%Y%m%d").replace(tzinfo=timezone.utc)
            t_years = max((expiry_dt - datetime.now(timezone.utc)).total_seconds(), 3600) / (
                365 * 24 * 3600
            )
            und = _UND_BY_SYMBOL.get(inst.underlying)
            vol = (und.vol if und else 0.25) + 0.05
            g = bs_price_greeks(
                spot=spot, strike=inst.strike, t_years=t_years, vol=vol,
                is_call=inst.right == OptionRight.CALL,
            )
            return g["price"], g["price"]
        return spot, spot

    # --- snapshots ---

    async def get_account_summary(self) -> AccountSummary:
        net_liq = 1_000_000.0

        def to_usd(value: float | None, currency: str) -> float:
            return (value or 0.0) * _FX_TO_USD.get(currency, 1.0)

        legs = list(self._positions.values())
        total_unreal = sum(to_usd(p.unrealized_pnl, p.instrument.currency) for p in legs)
        total_day = sum(to_usd(p.daily_pnl, p.instrument.currency) for p in legs)
        gross = sum(abs(to_usd(p.market_value, p.instrument.currency)) for p in legs)
        cap = self._capability()
        return AccountSummary(
            account="DU-MOCK-001",
            base_currency="USD",
            net_liquidation=round(net_liq + total_unreal, 2),
            total_cash=round(net_liq * 0.35, 2),
            buying_power=round(net_liq * 3.2, 2),
            gross_position_value=round(gross, 2),
            maintenance_margin=round(gross * 0.18, 2),
            available_funds=round(net_liq * 0.55, 2),
            excess_liquidity=round(net_liq * 0.6, 2),
            day_pnl=round(total_day, 2),
            unrealized_pnl=round(total_unreal, 2),
            realized_pnl=0.0,
            market_data=cap,
            updated_at=self._now(),
        )

    def _capability(self) -> MarketDataCapability:
        state = settings.mock_md_state.lower()
        if state == "mixed":
            return MarketDataCapability(
                default_type=MarketDataType.REALTIME,
                note="Mixed subscriptions: US real-time, HK delayed, KR unavailable",
            )
        mapping = {
            "realtime": (MarketDataType.REALTIME, "Real-time data across markets"),
            "delayed": (MarketDataType.DELAYED, "Delayed data (~15 min)"),
            "frozen": (MarketDataType.FROZEN, "Frozen data (markets closed)"),
            "none": (MarketDataType.NONE, "No market-data subscription"),
        }
        dt, note = mapping.get(state, (MarketDataType.REALTIME, ""))
        return MarketDataCapability(default_type=dt, note=note)

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_position_groups(self) -> list[PositionGroup]:
        return group_positions(list(self._positions.values()), fx_to_base=_FX_TO_USD)

    async def get_instrument(self, con_id: int) -> Instrument | None:
        return self._instruments.get(con_id)

    async def get_quote(self, con_id: int) -> Quote | None:
        pos = self._positions.get(con_id)
        if pos and pos.quote:
            return pos.quote
        inst = self._instruments.get(con_id)
        if not inst:
            return None
        price, _ = self._price_for(inst)
        mdt = self._market_data_type_for(inst.asset_class)
        no_data = mdt == MarketDataType.NONE
        oi = None
        if inst.is_option:
            oi = float(self._rng.randint(200, 60000))
        elif inst.sec_type == SecType.FUT:
            oi = float(self._rng.randint(50000, 2000000))
        return Quote(
            con_id=con_id,
            last=None if no_data else round(price, 4),
            bid=None if no_data else round(price * 0.999, 4),
            ask=None if no_data else round(price * 1.001, 4),
            bid_size=None if no_data else float(self._rng.randint(1, 40) * 100),
            ask_size=None if no_data else float(self._rng.randint(1, 40) * 100),
            close=round(self._day_open_spot.get(inst.underlying, price), 4),
            open=round(self._day_open_spot.get(inst.underlying, price), 4),
            vwap=None if no_data else round(price * (1 + self._rng.uniform(-0.002, 0.002)), 4),
            open_interest=oi,
            volume=None if no_data else float(self._rng.randint(1000, 500000)),
            market_data_type=mdt,
            market_session=self._session_for(inst),
            timestamp=self._now(),
        )

    def _session_for(self, inst: Instrument) -> MarketSession:
        """Classify the mock underlying's current session from its wall clock."""
        und = _UND_BY_SYMBOL.get(inst.underlying)
        if und is None:
            return MarketSession.UNKNOWN
        try:
            local = datetime.now(ZoneInfo(und.tz))
        except Exception:
            return MarketSession.UNKNOWN
        if local.weekday() >= 5:
            return MarketSession.CLOSED
        cur = local.hour * 60 + local.minute
        reg = _to_minutes(und.session)
        if reg and reg[0] <= cur < reg[1]:
            return MarketSession.REGULAR
        if und.ext_session:
            ext = _to_minutes(und.ext_session)
            in_ext = (ext[0] <= cur or cur < ext[1]) if ext[1] <= ext[0] else (ext[0] <= cur < ext[1])
            if in_ext:
                if reg and cur < reg[0]:
                    return MarketSession.PRE
                if reg and cur >= reg[1]:
                    return MarketSession.POST
                return MarketSession.REGULAR
        return MarketSession.CLOSED

    async def search(self, query: str) -> list[SearchResult]:
        q = query.strip().lower()
        if not q:
            return []
        results: list[SearchResult] = []
        for u in _UNDERLYINGS:
            if q in u.symbol.lower() or q in u.long_name.lower():
                results.append(
                    SearchResult(
                        con_id=u.con_id,
                        symbol=u.symbol,
                        sec_type=u.sec_type,
                        exchange=u.exchange,
                        currency=u.currency,
                        description=u.long_name,
                        asset_class=u.asset_class,
                    )
                )
        return results[:12]

    async def get_greeks(self, con_id: int) -> Greeks | None:
        inst = self._instruments.get(con_id)
        if not inst or not inst.is_option:
            return None
        _, greeks = self._price_for(inst)
        return greeks

    async def get_history(
        self, con_id: int, *, duration: str = "1 W", bar_size: str = "1 min",
        rth_only: bool = False, end: int | None = None,
    ) -> BarSet:
        inst = self._instruments.get(con_id)
        if not inst:
            return BarSet(con_id=con_id, symbol="?", bars=[])
        und = _UND_BY_SYMBOL.get(inst.underlying)
        tz = und.tz if und else "UTC"
        bars = self._generate_bars(
            inst, und, rth_only, bar_size=bar_size, duration=duration, end=end
        )
        sessions: list[SessionWindow] = []
        if bars and not rth_only and und is not None:
            sessions = self._regular_windows(und, bars[0].time, bars[-1].time)
        return BarSet(
            con_id=con_id,
            symbol=inst.display_name(),
            bar_size=bar_size,
            exchange_tz=tz,
            rth_only=rth_only,
            bars=bars,
            sessions=sessions,
        )

    def _regular_windows(
        self, und: _Underlying, lo: int, hi: int
    ) -> list[SessionWindow]:
        """Regular-session [start,end) UTC windows spanning [lo, hi]."""
        tz = ZoneInfo(und.tz)
        sh, sm = (int(x) for x in und.session[0].split(":"))
        eh, em = (int(x) for x in und.session[1].split(":"))
        windows: list[SessionWindow] = []
        day = datetime.fromtimestamp(lo, tz).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_day = datetime.fromtimestamp(hi, tz)
        while day <= end_day:
            if day.weekday() < 5:
                s = int(day.replace(hour=sh, minute=sm).timestamp())
                e = int(day.replace(hour=eh, minute=em).timestamp())
                if e >= lo and s <= hi:
                    windows.append(SessionWindow(start=s, end=e))
            day += timedelta(days=1)
        return windows

    def _generate_bars(
        self,
        inst: Instrument,
        und: _Underlying | None,
        rth_only: bool,
        *,
        bar_size: str = "1 min",
        duration: str = "1 W",
        end: int | None = None,
    ) -> list[Bar]:
        """OHLCV bars for a window of `duration` ending at `end` (or now).

        Bars are a deterministic function of their timestamp, so lazily loaded
        older chunks connect seamlessly with bars already on the chart. A bar is
        emitted when its [t, t+step) interval touches the session, so the bars
        straddling the open and the close are present (and read as regular).
        """
        if und is None:
            return []
        tz = ZoneInfo(und.tz)
        step = _bar_seconds(bar_size)
        span = _duration_seconds(duration)
        now = int(datetime.now(timezone.utc).timestamp())
        end_ts = min(end, now) if end else now
        start_ts = end_ts - span
        daily = step >= 86400

        bars: list[Bar] = []
        if daily:
            # One bar per weekday, timestamped at local midnight.
            day = datetime.fromtimestamp(start_ts, tz).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_dt = datetime.fromtimestamp(end_ts, tz)
            while day <= end_dt:
                if day.weekday() < 5:
                    t = int(day.timestamp())
                    bars.append(self._bar_at(inst, und, t, step, in_reg=True))
                day += timedelta(days=1)
            return bars

        # Intraday: step across the window, keep bars that touch a session.
        t = (start_ts // step) * step
        while t < end_ts:
            local = datetime.fromtimestamp(t, tz)
            if local.weekday() < 5:
                in_reg = self._in_session(local, und.session)
                if self._bar_touches_session(local, step, und, rth_only):
                    bars.append(self._bar_at(inst, und, t, step, in_reg=in_reg))
            t += step
        return bars

    def _bar_touches_session(
        self, local: datetime, step: int, und: _Underlying, rth_only: bool
    ) -> bool:
        """Does the bar interval [local, local+step) overlap a session?

        Checks both ends so the open- and close-straddling bars are included.
        """
        end_local = local + timedelta(seconds=max(step - 60, 0))
        if self._in_session(local, und.session) or self._in_session(
            end_local, und.session
        ):
            return True
        if not rth_only and und.ext_session is not None:
            if self._in_session(
                local, und.ext_session, extended=True
            ) or self._in_session(end_local, und.ext_session, extended=True):
                return True
        return False

    def _level(self, und: _Underlying, t: int) -> float:
        """Deterministic, continuous spot level at epoch `t`."""
        spot = self._spot.get(und.symbol, 100.0)
        day = t / 86400.0
        trend = (
            math.sin(day / 11.0) * 0.06
            + math.sin(day / 3.3) * 0.03
            + math.sin(day / 0.7) * 0.01
        )
        jitter = (_hash01(und.con_id, t) - 0.5) * 0.008
        return max(spot * (1.0 + und.vol * trend + jitter), 0.01)

    def _bar_at(
        self, inst: Instrument, und: _Underlying, t: int, step: int, *, in_reg: bool
    ) -> Bar:
        """A single deterministic OHLCV bar starting at epoch `t`."""
        o = self._level(und, t)
        c = self._level(und, t + step)  # == next bar's open -> continuous series
        wig = _hash01(und.con_id ^ 0x5F, t) * 0.004
        hi = max(o, c) * (1 + wig)
        lo = min(o, c) * (1 - wig)
        if inst.is_option:
            o, _ = self._price_at_spot(inst, o)
            c, _ = self._price_at_spot(inst, c)
            hi = max(o, c) * 1.01
            lo = min(o, c) * 0.99
        base_vol = 1500 if in_reg else 250
        vol = float(base_vol // 2 + int(_hash01(und.con_id, t + 1) * base_vol * 2))
        return Bar(
            time=t,
            open=round(o, 4), high=round(hi, 4),
            low=round(lo, 4), close=round(c, 4), volume=vol,
        )

    @staticmethod
    def _in_session(local: datetime, session: tuple[str, str], extended: bool = False) -> bool:
        (sh, sm), (eh, em) = (
            tuple(int(x) for x in session[0].split(":")),
            tuple(int(x) for x in session[1].split(":")),
        )
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        cur = local.hour * 60 + local.minute
        if end_min <= start_min:  # overnight session (e.g. futures)
            return cur >= start_min or cur < end_min
        return start_min <= cur < end_min

    # --- streaming ---

    async def subscribe(self, con_ids: Iterable[int]) -> None:
        self._subscribed.update(con_ids)

    async def unsubscribe(self, con_ids: Iterable[int]) -> None:
        self._subscribed.difference_update(con_ids)

    async def subscribe_bars(self, con_id: int, bar_size: str = "1 min") -> None:
        self._bar_subscribed.add(con_id)
        self._bar_size[con_id] = bar_size
        self._live_bar.pop(con_id, None)

    async def unsubscribe_bars(self, con_id: int) -> None:
        self._bar_subscribed.discard(con_id)
        self._bar_size.pop(con_id, None)
        self._live_bar.pop(con_id, None)

    async def _run_ticks(self) -> None:
        """Random-walk spots and push updates to subscribers ~every second."""
        try:
            while True:
                await asyncio.sleep(1.0)
                self._walk_spots()
                await self._emit_updates()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("mock tick loop crashed")

    def _walk_spots(self) -> None:
        for u in _UNDERLYINGS:
            vol_per_sec = u.vol / math.sqrt(252 * 6.5 * 3600)
            shock = self._rng.gauss(0, 1) * vol_per_sec * self._spot[u.symbol]
            self._spot[u.symbol] = max(self._spot[u.symbol] + shock, 0.01)

    async def _emit_updates(self) -> None:
        now = self._now()
        for con_id in list(self._subscribed):
            inst = self._instruments.get(con_id)
            if not inst:
                continue
            pos = self._positions.get(con_id)
            if pos:
                self._revalue(pos)
                quote = pos.quote
                greeks = pos.greeks
                if quote:
                    await self._emit_quote(quote)
                if greeks:
                    await self._emit_greeks(greeks)
                await self._emit_pnl(con_id, pos.daily_pnl, pos.unrealized_pnl, pos.market_value)
            else:
                quote = await self.get_quote(con_id)
                if quote:
                    await self._emit_quote(quote)
                greeks = await self.get_greeks(con_id)
                if greeks:
                    await self._emit_greeks(greeks)

        # live bars for chart pages
        for con_id in list(self._bar_subscribed):
            inst = self._instruments.get(con_id)
            if not inst:
                continue
            price, _ = self._price_for(inst)
            secs = _bar_seconds(self._bar_size.get(con_id, "1 min"))
            bucket = int(now // secs * secs)
            prev = self._live_bar.get(con_id)
            if prev is None or prev.time != bucket:
                bar = Bar(time=bucket, open=price, high=price, low=price,
                          close=price, volume=float(self._rng.randint(50, 500)))
                self._live_bar[con_id] = bar
                await self._emit_bar(con_id, bar, False)
            else:
                prev.close = round(price, 4)
                prev.high = max(prev.high, price)
                prev.low = min(prev.low, price)
                prev.volume += self._rng.randint(1, 50)
                await self._emit_bar(con_id, prev, True)

        # account-level totals (drives the live home-page tape)
        acct = await self.get_account_summary()
        await self._emit_account(
            acct.day_pnl, acct.unrealized_pnl, acct.net_liquidation
        )

    # --- utils ---

    @staticmethod
    def _now() -> float:
        return datetime.now(timezone.utc).timestamp()
