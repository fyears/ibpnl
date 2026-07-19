"""IBProvider — the real data source, backed by ib_async → IB Gateway / TWS.

Design rules:
  * Everything IB can state authoritatively is taken from IB, not computed:
      - option Greeks   -> ticker.modelGreeks (tickOptionComputation)
      - per-position PnL -> reqPnLSingle (dailyPnL / unrealizedPnL / value)
      - account PnL      -> reqPnL + account summary tags
      - live chart bars  -> reqHistoricalData(keepUpToDate=True) + barUpdateEvent
  * No ib_async object leaks out of this module; we translate to app.models.
  * Market-data lines are only opened for instruments something is watching
    (the WS hub refcounts and calls subscribe/unsubscribe).

Market-data types (reqMarketDataType):
    1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
  With 3/4, IB serves real-time for subscribed instruments and delayed for the
  rest, so `auto` maps to 4 (most permissive, frozen fallback when closed).
  Per-instrument achieved type arrives on ticker.marketDataType; permission
  errors (354 / 10167 / 10168 / 10089) mark the instrument NONE.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import sys
from collections.abc import Iterable
from datetime import datetime, timezone

from ib_async import ContFuture, Contract, IB
from ib_async.objects import PortfolioItem

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
from app.services.grouping import group_positions
from app.services.trading_hours import (
    classify_session,
    expand_windows,
    parse_hours,
    regular_windows,
)

log = logging.getLogger(__name__)

# Approximate FX->USD used ONLY for cross-currency sort order of groups.
# Real money numbers come from IB (account summary is already in base currency).
_FX_SORT = {"USD": 1.0, "HKD": 0.128, "KRW": 0.00072, "JPY": 0.0064,
            "EUR": 1.08, "GBP": 1.27, "CNH": 0.14, "SGD": 0.74, "AUD": 0.66}

_MDT_MAP = {
    1: MarketDataType.REALTIME,
    2: MarketDataType.FROZEN,
    3: MarketDataType.DELAYED,
    4: MarketDataType.DELAYED_FROZEN,
}

# error codes meaning "no market data permission / not available"
_NO_DATA_ERRORS = {354, 10089, 10167, 10168, 10197}

# Generic tick list opened alongside streaming market data. Adds option volume
# (100), option open interest (101), historical vol (104), option implied vol
# (106), misc stats (165) and RTVolume (233, which fills ticker.vwap).
_GENERIC_TICKS = "100,101,104,106,165,233,295"


# IB TWS API's 'unset' sentinel for double fields (Java Double.MAX_VALUE).
# Values at/above this magnitude mean "no value", not a real number.
_IB_UNSET_DOUBLE = sys.float_info.max


def _f(value: float | None) -> float | None:
    """Normalize an IB numeric field to a real value or None for JSON.

    IB marks an 'unset' field two different ways: NaN, or the sentinel
    ``Double.MAX_VALUE`` (== ``sys.float_info.max`` ≈ 1.7977e308). The latter
    leaks through as a literal 1.79e308 if not filtered — most visibly on
    reqPnLSingle.dailyPnL for positions with no prior close, which then blows
    up the group/account daily-PnL totals. Treat any non-finite value (NaN,
    ±inf) or the huge sentinel as unset.
    """
    if value is None:
        return None
    try:
        if not math.isfinite(value) or abs(value) >= _IB_UNSET_DOUBLE:
            return None
    except TypeError:
        return None
    return float(value)


def _fp(value: float | None) -> float | None:
    """Like `_f`, but also treats IB's -1 'no quote' sentinel as None.

    IB reports bid/ask (and sometimes last/close) as -1 when there is no live
    quote — common for illiquid options and outside trading hours. Showing a
    literal -1.00 price is misleading, so collapse it to None ('—')."""
    v = _f(value)
    if v is None or v < 0:
        return None
    return v


def _sec_type(ib_sec_type: str) -> SecType:
    if ib_sec_type == "CONTFUT":  # continuous future -> treat as a future
        return SecType.FUT
    try:
        return SecType(ib_sec_type)
    except ValueError:
        return SecType.OTHER


def _asset_class(contract: Contract) -> AssetClass:
    st = contract.secType
    cur = contract.currency
    if cur == "USD":
        return {
            "STK": AssetClass.US_STOCK,
            "FUT": AssetClass.US_FUTURE,
            "CONTFUT": AssetClass.US_FUTURE,
            "OPT": AssetClass.US_INDEX_OPTION
            if contract.symbol in ("SPX", "NDX", "RUT", "VIX", "XSP", "DJX")
            else AssetClass.US_OPTION,
            "FOP": AssetClass.US_FUTURE_OPTION,
            "IND": AssetClass.US_INDEX,
        }.get(st, AssetClass.OTHER)
    if cur == "HKD":
        return {
            "STK": AssetClass.HK_STOCK,
            "OPT": AssetClass.HK_OPTION,
        }.get(st, AssetClass.OTHER)
    if cur == "KRW":
        return {
            "STK": AssetClass.KR_STOCK,
            "OPT": AssetClass.KR_OPTION,
        }.get(st, AssetClass.OTHER)
    return AssetClass.OTHER


def _make_routable(contract: Contract) -> Contract:
    """Return a contract with a market-data routing `exchange` set.

    IB portfolio/position contracts arrive with `exchange=''` (only
    `primaryExchange` populated). Passing those to `reqMktData` /
    `reqHistoricalData` is rejected with *Error 321: please enter exchange*,
    which silently kills live quotes, option Greeks, and chart history. We
    fill a routing exchange without mutating the original object: SMART for
    US SMART-eligible stocks/options, the listing exchange otherwise (HK/JP/
    EU/KR stocks, futures, FOPs).
    """
    if contract.exchange:
        return contract
    c = copy.copy(contract)
    if c.currency == "USD" and c.secType in ("STK", "OPT"):
        c.exchange = "SMART"
    else:
        c.exchange = c.primaryExchange or "SMART"
    return c


class IBProvider(MarketDataProvider):
    def __init__(self) -> None:
        self._quote_cbs = []
        self._greeks_cbs = []
        self._pnl_cbs = []
        self._bar_cbs = []
        self._account_cbs = []

        self.ib = IB()
        self._account: str = settings.ib_account
        self._connected_port: int | None = None
        self._detail: str = "not connected"
        # caches
        self._instruments: dict[int, Instrument] = {}
        self._contracts: dict[int, Contract] = {}
        self._tz_cache: dict[int, str] = {}
        # con_id -> (tradingHours, liquidHours) strings from contract details
        self._hours_cache: dict[int, tuple[str, str]] = {}
        self._no_data: set[int] = set()
        self._md_refs: set[int] = set()  # con_ids with an open reqMktData line
        self._live_bars: dict[int, object] = {}  # con_id -> BarDataList
        self._live_bar_size: dict[int, str] = {}  # con_id -> requested bar size
        self._pnl_singles: dict[int, object] = {}  # con_id -> PnLSingle
        self._account_pnl = None  # PnL object from reqPnL
        self._last_net_liq: float | None = None  # cached for live account pushes
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        # wire events once (before connecting, so a drop during startup is caught)
        self.ib.pendingTickersEvent += self._on_pending_tickers
        self.ib.pnlSingleEvent += self._on_pnl_single
        self.ib.pnlEvent += self._on_account_pnl
        self.ib.barUpdateEvent += self._on_bar_update
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnected
        # Don't hard-fail if IB Gateway/TWS isn't up yet: come online in a
        # disconnected state and keep retrying in the background, so the UI can
        # tell the user to start/log in to Gateway or TWS instead of the whole
        # server refusing to boot.
        try:
            await self._connect()
        except ConnectionError:
            self._detail = (
                "IB Gateway/TWS not reachable on "
                f"{settings.ib_host}:{settings.ib_ports}. "
                "Start it, log in, and enable API access (retrying…)."
            )
            log.warning(self._detail)
            loop = asyncio.get_event_loop()
            self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _connect(self) -> None:
        last_err: Exception | None = None
        for port in settings.ib_port_list:
            try:
                log.info("Connecting to IB at %s:%s ...", settings.ib_host, port)
                await self.ib.connectAsync(
                    settings.ib_host,
                    port,
                    clientId=settings.ib_client_id,
                    readonly=settings.ib_readonly,
                    account=settings.ib_account or "",
                    timeout=6,
                )
                self._connected_port = port
                break
            except Exception as e:  # try next port
                last_err = e
                log.warning("Port %s failed: %s", port, e)
        if not self.ib.isConnected():
            self._detail = f"Could not reach IB Gateway/TWS on ports {settings.ib_ports}: {last_err}"
            log.error(self._detail)
            raise ConnectionError(self._detail)

        accounts = self.ib.managedAccounts()
        if not self._account:
            self._account = accounts[0] if accounts else ""
        self._detail = f"Connected on port {self._connected_port} ({self._account})"
        log.info(self._detail)

        # requested market-data type
        mdt = {"auto": 4, "realtime": 1, "delayed": 3, "frozen": 2}.get(
            settings.market_data_type.lower(), 4
        )
        self.ib.reqMarketDataType(mdt)

        # account-level PnL stream + per-position PnL streams
        if self._account:
            try:
                self._account_pnl = self.ib.reqPnL(self._account)
            except Exception:
                log.exception("reqPnL failed")
        await self._refresh_portfolio_subscriptions()

    async def stop(self) -> None:
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self.ib.isConnected():
            self.ib.disconnect()

    def status(self) -> ConnectionStatus:
        return ConnectionStatus(
            provider="ib",
            connected=self.ib.isConnected(),
            detail=self._detail,
            account=self._account,
            server_time=datetime.now(timezone.utc).timestamp(),
        )

    def _on_disconnected(self) -> None:
        if self._closing:
            return
        self._detail = "Disconnected from IB; reconnecting..."
        log.warning(self._detail)
        loop = asyncio.get_event_loop()
        self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = 2.0
        while not self._closing and not self.ib.isConnected():
            try:
                await self._connect()
                # re-open market data lines that were active
                for con_id in list(self._md_refs):
                    self._md_refs.discard(con_id)
                await self.subscribe(list(self._md_refs))
                return
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 1.7, 60)

    # ------------------------------------------------------------ contract utils

    async def _qualify(self, con_id: int) -> Contract | None:
        if con_id in self._contracts:
            return self._contracts[con_id]
        # Try SMART first, then bare (no exchange) — indices/futures often reject
        # SMART. Note: qualifyContractsAsync does NOT raise on an unknown
        # contract; it logs Error 200 and returns a list with a None element, so
        # we must check the element, not just catch exceptions.
        for contract in (Contract(conId=con_id, exchange="SMART"), Contract(conId=con_id)):
            try:
                qualified = await self.ib.qualifyContractsAsync(contract)
            except Exception:
                continue
            if qualified and qualified[0] is not None:
                self._contracts[con_id] = qualified[0]
                return qualified[0]
        log.warning("qualify failed for conId=%s", con_id)
        return None

    async def _exchange_tz(self, contract: Contract) -> str:
        con_id = contract.conId
        if con_id in self._tz_cache:
            return self._tz_cache[con_id]
        tz = "UTC"
        long_name = ""
        try:
            details = await self.ib.reqContractDetailsAsync(contract)
            if details:
                tz = details[0].timeZoneId or "UTC"
                long_name = details[0].longName or ""
                self._hours_cache[con_id] = (
                    details[0].tradingHours or "",
                    details[0].liquidHours or "",
                )
        except Exception:
            log.warning("reqContractDetails failed for %s", contract.localSymbol)
        self._tz_cache[con_id] = tz
        inst = self._instruments.get(con_id)
        if inst and long_name and not inst.long_name:
            inst.long_name = long_name
        return tz

    def _to_instrument(self, contract: Contract) -> Instrument:
        con_id = contract.conId
        cached = self._instruments.get(con_id)
        if cached:
            return cached
        right: OptionRight | None = None
        if contract.right in ("C", "CALL"):
            right = OptionRight.CALL
        elif contract.right in ("P", "PUT"):
            right = OptionRight.PUT
        try:
            multiplier = float(contract.multiplier) if contract.multiplier else 1.0
        except ValueError:
            multiplier = 1.0
        inst = Instrument(
            con_id=con_id,
            symbol=contract.symbol,
            sec_type=_sec_type(contract.secType),
            exchange=contract.primaryExchange or contract.exchange or "",
            currency=contract.currency or "USD",
            # IB sets contract.symbol to the underlying symbol for OPT/FOP,
            # which is exactly our grouping key.
            underlying=contract.symbol,
            asset_class=_asset_class(contract),
            right=right,
            strike=contract.strike or None,
            expiry=contract.lastTradeDateOrContractMonth or None,
            multiplier=multiplier,
            local_symbol=contract.localSymbol or contract.symbol,
        )
        self._instruments[con_id] = inst
        return inst

    # ------------------------------------------------------------- subscriptions

    async def _refresh_portfolio_subscriptions(self) -> None:
        """Ensure each held position has a PnLSingle stream and cached contract."""
        for item in self.ib.portfolio(self._account):
            contract = item.contract
            self._contracts.setdefault(contract.conId, _make_routable(contract))
            self._to_instrument(contract)
            if contract.conId not in self._pnl_singles and self._account:
                try:
                    self._pnl_singles[contract.conId] = self.ib.reqPnLSingle(
                        self._account, "", contract.conId
                    )
                except Exception:
                    log.exception("reqPnLSingle failed for %s", contract.localSymbol)

    async def subscribe(self, con_ids: Iterable[int]) -> None:
        for con_id in con_ids:
            if con_id in self._md_refs:
                continue
            contract = await self._qualify(con_id)
            if contract is None:
                self._no_data.add(con_id)
                continue
            # Options: model greeks arrive automatically via tickOptionComputation.
            # Generic ticks add volume / open interest / VWAP.
            self.ib.reqMktData(contract, _GENERIC_TICKS, False, False)
            self._md_refs.add(con_id)
            # Populate trading-hours cache (for the market-session marker) in the
            # background so the first quotes can be tagged pre/post/regular.
            if con_id not in self._hours_cache:
                asyncio.ensure_future(self._ensure_hours(contract))

    async def _ensure_hours(self, contract: Contract) -> None:
        try:
            await self._exchange_tz(contract)
        except Exception:
            pass

    async def unsubscribe(self, con_ids: Iterable[int]) -> None:
        for con_id in con_ids:
            if con_id not in self._md_refs:
                continue
            contract = self._contracts.get(con_id)
            if contract is not None:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass
            self._md_refs.discard(con_id)

    async def subscribe_bars(self, con_id: int, bar_size: str = "1 min") -> None:
        if con_id in self._live_bars:
            # already streaming; if the requested granularity changed, restart
            if self._live_bar_size.get(con_id) == bar_size:
                return
            await self.unsubscribe_bars(con_id)
        contract = await self._qualify(con_id)
        if contract is None:
            return
        what = "MIDPOINT" if contract.secType == "CASH" else "TRADES"
        # Duration only needs to cover a couple of bars for keepUpToDate; IB
        # requires it be commensurate with the bar size.
        duration = _keepalive_duration(bar_size)
        try:
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what,
                useRTH=False,
                formatDate=2,
                keepUpToDate=True,
            )
            self._live_bars[con_id] = bars
            self._live_bar_size[con_id] = bar_size
        except Exception:
            log.exception("live bars failed for conId=%s", con_id)

    async def unsubscribe_bars(self, con_id: int) -> None:
        bars = self._live_bars.pop(con_id, None)
        self._live_bar_size.pop(con_id, None)
        if bars is not None:
            try:
                self.ib.cancelHistoricalData(bars)
            except Exception:
                pass

    # ------------------------------------------------------------------- events

    def _on_pending_tickers(self, tickers) -> None:
        for ticker in tickers:
            contract = ticker.contract
            if contract is None:
                continue
            quote = self._ticker_to_quote(ticker)
            asyncio.ensure_future(self._emit_quote(quote))
            mg = ticker.modelGreeks
            if mg is not None:
                greeks = Greeks(
                    con_id=contract.conId,
                    delta=_f(mg.delta),
                    gamma=_f(mg.gamma),
                    vega=_f(mg.vega),
                    theta=_f(mg.theta),
                    iv=_f(mg.impliedVol),
                    und_price=_f(mg.undPrice),
                    option_price=_f(mg.optPrice),
                    timestamp=datetime.now(timezone.utc).timestamp(),
                )
                asyncio.ensure_future(self._emit_greeks(greeks))

    def _ticker_to_quote(self, ticker) -> Quote:
        contract = ticker.contract
        con_id = contract.conId
        if con_id in self._no_data:
            mdt = MarketDataType.NONE
        else:
            mdt = _MDT_MAP.get(ticker.marketDataType, MarketDataType.NONE)
        ts = None
        if ticker.time is not None:
            ts = ticker.time.timestamp()
        # open interest: options carry call/put OI; futures carry futuresOpenInterest
        oi = None
        if contract.secType in ("OPT", "FOP"):
            oi = _f(ticker.callOpenInterest) if contract.right in ("C", "CALL") else _f(
                ticker.putOpenInterest
            )
            if oi is None:
                oi = _f(ticker.openInterest)
        elif contract.secType == "FUT":
            oi = _f(ticker.futuresOpenInterest)
        session = self._session_of(con_id)
        return Quote(
            con_id=con_id,
            last=_fp(ticker.last) or _fp(ticker.close),
            bid=_fp(ticker.bid),
            ask=_fp(ticker.ask),
            bid_size=_f(ticker.bidSize),
            ask_size=_f(ticker.askSize),
            close=_fp(ticker.close),
            open=_fp(ticker.open),
            high=_fp(ticker.high),
            low=_fp(ticker.low),
            volume=_f(ticker.volume),
            vwap=_fp(ticker.vwap),
            open_interest=oi,
            market_data_type=mdt,
            market_session=session,
            timestamp=ts,
        )

    def _session_of(self, con_id: int) -> MarketSession:
        """Classify the exchange's current session from cached trading hours."""
        hours = self._hours_cache.get(con_id)
        tz = self._tz_cache.get(con_id)
        if not hours or not tz:
            return MarketSession.UNKNOWN
        trading, liquid = hours
        # RTH via regular_windows (drops IB's overnight electronic block that
        # can otherwise mark an overnight instant as regular); extended keeps the
        # full session, which legitimately spans midnight.
        regular = regular_windows(liquid, tz)
        extended = parse_hours(trading, tz)
        now = datetime.now(timezone.utc).timestamp()
        return classify_session(now, regular, extended)

    def _on_pnl_single(self, pnl_single) -> None:
        asyncio.ensure_future(
            self._emit_pnl(
                pnl_single.conId,
                _f(pnl_single.dailyPnL),
                _f(pnl_single.unrealizedPnL),
                _f(pnl_single.value),
            )
        )

    def _on_account_pnl(self, pnl) -> None:
        """Account-level PnL tick (reqPnL) -> broadcast live account totals."""
        day = _f(pnl.dailyPnL)
        unreal = _f(pnl.unrealizedPnL)
        # Net liquidation isn't on the PnL object; nudge the last cached value by
        # the change in unrealized so the tape ticks between account polls.
        asyncio.ensure_future(self._emit_account(day, unreal, self._last_net_liq))

    def _on_bar_update(self, bars, has_new_bar: bool) -> None:
        # find which con_id this BarDataList belongs to
        con_id = None
        for cid, blist in self._live_bars.items():
            if blist is bars:
                con_id = cid
                break
        if con_id is None or not bars:
            return
        b = bars[-1]
        bar = _ib_bar_to_bar(b)
        if bar is not None:
            asyncio.ensure_future(self._emit_bar(con_id, bar, not has_new_bar))

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        if errorCode in _NO_DATA_ERRORS and contract is not None:
            self._no_data.add(contract.conId)
            log.info("No market data for %s (%s): %s",
                     contract.localSymbol, errorCode, errorString)

    # ---------------------------------------------------------------- snapshots

    async def get_account_summary(self) -> AccountSummary:
        tags = {}
        try:
            summary = await self.ib.accountSummaryAsync(self._account)
            for row in summary:
                tags[row.tag] = row
        except Exception:
            log.exception("accountSummary failed")

        def val(tag: str) -> float | None:
            row = tags.get(tag)
            if row is None:
                return None
            try:
                return float(row.value)
            except ValueError:
                return None

        base = "USD"
        row = tags.get("NetLiquidation")
        if row is not None and row.currency:
            base = row.currency

        net_liq = val("NetLiquidation")
        self._last_net_liq = net_liq

        day_pnl = unreal = real = None
        if self._account_pnl is not None:
            day_pnl = _f(self._account_pnl.dailyPnL)
            unreal = _f(self._account_pnl.unrealizedPnL)
            real = _f(self._account_pnl.realizedPnL)
        if unreal is None:
            unreal = val("UnrealizedPnL")

        return AccountSummary(
            account=self._account,
            base_currency=base,
            net_liquidation=net_liq,
            total_cash=val("TotalCashValue"),
            buying_power=val("BuyingPower"),
            gross_position_value=val("GrossPositionValue"),
            maintenance_margin=val("MaintMarginReq"),
            available_funds=val("AvailableFunds"),
            excess_liquidity=val("ExcessLiquidity"),
            day_pnl=day_pnl,
            unrealized_pnl=unreal,
            realized_pnl=real,
            market_data=self._capability(),
            updated_at=datetime.now(timezone.utc).timestamp(),
        )

    def _capability(self) -> MarketDataCapability:
        req = settings.market_data_type.lower()
        note = {
            "auto": "Live where subscribed; delayed/frozen elsewhere",
            "realtime": "Real-time requested (errors where unsubscribed)",
            "delayed": "Delayed data requested",
            "frozen": "Frozen data requested",
        }.get(req, "")
        default = {
            "auto": MarketDataType.REALTIME,
            "realtime": MarketDataType.REALTIME,
            "delayed": MarketDataType.DELAYED,
            "frozen": MarketDataType.FROZEN,
        }.get(req, MarketDataType.REALTIME)
        return MarketDataCapability(default_type=default, note=note)

    async def get_positions(self) -> list[Position]:
        await self._refresh_portfolio_subscriptions()
        positions: list[Position] = []
        for item in self.ib.portfolio(self._account):
            positions.append(self._portfolio_item_to_position(item))
        return positions

    def _portfolio_item_to_position(self, item: PortfolioItem) -> Position:
        contract = item.contract
        con_id = contract.conId
        inst = self._to_instrument(contract)

        # live ticker if we have one open
        ticker = self.ib.ticker(self._contracts.get(con_id, contract))
        quote = self._ticker_to_quote(ticker) if ticker is not None else None
        greeks = None
        if ticker is not None and ticker.modelGreeks is not None:
            mg = ticker.modelGreeks
            greeks = Greeks(
                con_id=con_id,
                delta=_f(mg.delta), gamma=_f(mg.gamma), vega=_f(mg.vega),
                theta=_f(mg.theta), iv=_f(mg.impliedVol),
                und_price=_f(mg.undPrice), option_price=_f(mg.optPrice),
            )
        if quote is None or quote.last is None:
            # fall back to IB's own portfolio mark price
            mdt = MarketDataType.NONE if con_id in self._no_data else (
                quote.market_data_type if quote else MarketDataType.FROZEN
            )
            quote = Quote(
                con_id=con_id,
                last=_f(item.marketPrice),
                close=_f(item.marketPrice),
                market_data_type=mdt,
                timestamp=datetime.now(timezone.utc).timestamp(),
            )

        pnl_single = self._pnl_singles.get(con_id)
        daily = _f(pnl_single.dailyPnL) if pnl_single is not None else None

        # IB averageCost includes the multiplier for derivatives; normalize to a
        # per-unit price comparable to the quote / chart axis.
        mult = inst.multiplier or 1.0
        avg_price = item.averageCost / mult if item.averageCost else None

        return Position(
            instrument=inst,
            quantity=item.position,
            avg_cost=item.averageCost,
            avg_price=avg_price,
            quote=quote,
            greeks=greeks,
            market_value=_f(item.marketValue),
            unrealized_pnl=_f(item.unrealizedPNL),
            daily_pnl=daily,
            realized_pnl=_f(item.realizedPNL),
        )

    async def get_position_groups(self) -> list[PositionGroup]:
        return group_positions(await self.get_positions(), fx_to_base=_FX_SORT)

    async def get_instrument(self, con_id: int) -> Instrument | None:
        if con_id in self._instruments:
            return self._instruments[con_id]
        contract = await self._qualify(con_id)
        if contract is None:
            return None
        inst = self._to_instrument(contract)
        await self._exchange_tz(contract)  # also fills long_name
        return inst

    async def get_quote(self, con_id: int) -> Quote | None:
        contract = self._contracts.get(con_id) or await self._qualify(con_id)
        if contract is None:
            return None
        ticker = self.ib.ticker(contract)
        if ticker is not None:
            return self._ticker_to_quote(ticker)
        return None

    async def get_greeks(self, con_id: int) -> Greeks | None:
        contract = self._contracts.get(con_id) or await self._qualify(con_id)
        if contract is None or contract.secType not in ("OPT", "FOP"):
            return None
        ticker = self.ib.ticker(contract)
        if ticker is None or ticker.modelGreeks is None:
            return None
        mg = ticker.modelGreeks
        return Greeks(
            con_id=con_id,
            delta=_f(mg.delta), gamma=_f(mg.gamma), vega=_f(mg.vega),
            theta=_f(mg.theta), iv=_f(mg.impliedVol),
            und_price=_f(mg.undPrice), option_price=_f(mg.optPrice),
            timestamp=datetime.now(timezone.utc).timestamp(),
        )

    async def search(self, query: str) -> list[SearchResult]:
        try:
            descriptions = await self.ib.reqMatchingSymbolsAsync(query)
        except Exception:
            log.exception("reqMatchingSymbols failed for %r", query)
            return []
        if not descriptions:
            return []
        results: list[SearchResult] = []
        seen: set[int] = set()
        # Underlyings (IND/STK) that advertise futures — MES, ES, etc. come back
        # as the index only, so we add a continuous future the user can chart.
        contfut: list[tuple[str, str]] = []  # (symbol, exchange)
        for d in descriptions:
            c = d.contract
            if c is None or not c.conId or c.conId in seen:
                continue
            deriv = getattr(d, "derivativeSecTypes", None) or []
            if (
                "FUT" in deriv
                and c.secType in ("IND", "STK")
                and not any(s == c.symbol for s, _ in contfut)
            ):
                contfut.append((c.symbol, c.exchange or c.primaryExchange or ""))
            # Chartable underlyings only (stocks / ETFs / indices / futures).
            if c.secType not in ("STK", "IND", "FUT", "CASH"):
                continue
            seen.add(c.conId)
            results.append(
                SearchResult(
                    con_id=c.conId,
                    symbol=c.symbol,
                    sec_type=_sec_type(c.secType),
                    exchange=c.primaryExchange or c.exchange or "",
                    currency=c.currency or "USD",
                    description=c.description or "",
                    asset_class=_asset_class(c),
                )
            )
        contfuts = await self._continuous_futures(contfut, seen)
        # Surface continuous futures first — they're usually what "MES"/"ES" means.
        return (contfuts + results)[:12]

    async def _continuous_futures(
        self, symbols: list[tuple[str, str]], seen: set[int]
    ) -> list[SearchResult]:
        """Resolve continuous-future SearchResults for the given underlyings.

        `reqMatchingSymbols` returns the index/underlying but never the future,
        so for each symbol that advertises futures we qualify a `ContFuture`
        (front-month continuous contract) and add it as a chartable result.
        """
        out: list[SearchResult] = []
        for sym, exchange in symbols[:4]:  # cap the extra round-trips
            cf = ContFuture(sym, exchange=exchange) if exchange else ContFuture(sym)
            try:
                qualified = await self.ib.qualifyContractsAsync(cf)
            except Exception:
                continue
            qc = qualified[0] if qualified else None
            if qc is None or not qc.conId or qc.conId in seen:
                continue
            seen.add(qc.conId)
            self._contracts[qc.conId] = qc  # so the detail page resolves it
            out.append(
                SearchResult(
                    con_id=qc.conId,
                    symbol=qc.symbol,
                    sec_type=SecType.FUT,
                    exchange=qc.exchange or "",
                    currency=qc.currency or "USD",
                    description=f"{qc.symbol} continuous future",
                    asset_class=_asset_class(qc),
                )
            )
        return out

    async def get_history(
        self, con_id: int, *, duration: str = "1 W", bar_size: str = "1 min",
        rth_only: bool = False, end: int | None = None,
    ) -> BarSet:
        contract = await self._qualify(con_id)
        if contract is None:
            return BarSet(con_id=con_id, symbol="?", bars=[])
        inst = self._to_instrument(contract)
        tz = await self._exchange_tz(contract)
        what = "MIDPOINT" if contract.secType == "CASH" else "TRADES"
        # end=None -> "" (up to now); otherwise a tz-aware UTC datetime for IB.
        end_dt: str | datetime = ""
        if end:
            end_dt = datetime.fromtimestamp(end, tz=timezone.utc)
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=rth_only,
            formatDate=2,
            keepUpToDate=False,
        )
        out: list[Bar] = []
        for b in bars:
            bar = _ib_bar_to_bar(b)
            if bar is not None:
                out.append(bar)
        # Regular-hours windows spanning the returned bars, for ext-hours shading.
        sessions: list[SessionWindow] = []
        if out and not rth_only:
            hours = self._hours_cache.get(con_id)
            if hours and hours[1]:
                sessions = expand_windows(
                    hours[1], tz, out[0].time, out[-1].time
                )
        return BarSet(
            con_id=con_id,
            symbol=inst.display_name(),
            bar_size=bar_size,
            exchange_tz=tz,
            rth_only=rth_only,
            bars=out,
            sessions=sessions,
        )


def _keepalive_duration(bar_size: str) -> str:
    """A short IB duration string sufficient to seed a keepUpToDate stream.

    IB requires the duration be commensurate with the bar size; too small a
    window for large bars is rejected.
    """
    size = bar_size.lower()
    if "sec" in size:
        return "600 S"
    if "min" in size:
        return "1 D"
    if "hour" in size:
        return "1 W"
    if "day" in size or "week" in size:
        return "1 M"
    return "1 D"


def _ib_bar_to_bar(b) -> Bar | None:
    """Convert an ib_async BarData to our Bar (epoch seconds UTC)."""
    date = b.date
    if isinstance(date, datetime):
        ts = int(date.timestamp())
    else:  # daily bars come as date
        ts = int(datetime(date.year, date.month, date.day, tzinfo=timezone.utc).timestamp())
    if b.close is None or (isinstance(b.close, float) and math.isnan(b.close)):
        return None
    volume = b.volume
    if volume is None or (isinstance(volume, float) and math.isnan(volume)) or volume < 0:
        volume = 0.0
    return Bar(
        time=ts,
        open=float(b.open), high=float(b.high),
        low=float(b.low), close=float(b.close),
        volume=float(volume),
    )
