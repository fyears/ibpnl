"""Regression tests for IBProvider contract resolution and search.

- `_qualify`: `ib_async.qualifyContractsAsync` does NOT raise when a contract
  can't be resolved on a given exchange — it logs Error 200 and returns a list
  whose element is None. Indices like SPX reject SMART routing this way, so
  `_qualify` must detect the None element and fall through to a bare
  (no-exchange) attempt. See the "search SPX -> not supported" bug.
- `search`: `reqMatchingSymbols` returns the index/underlying but never the
  future, so a search for "MES"/"ES" must add a continuous future.
"""

from __future__ import annotations

import pytest

from app.ib.ib_provider import IBProvider
from app.models import SecType


class _FakeContract:
    def __init__(
        self,
        conId: int,
        exchange: str = "",
        secType: str = "IND",
        symbol: str = "",
        currency: str = "USD",
        primaryExchange: str = "",
        description: str = "",
    ):
        self.conId = conId
        self.exchange = exchange
        self.secType = secType
        self.symbol = symbol
        self.currency = currency
        self.primaryExchange = primaryExchange
        self.description = description


class _FakeIB:
    """Mimics the one method _qualify calls, with per-exchange behavior."""

    def __init__(self, behavior):
        self._behavior = behavior
        self.calls: list[str] = []

    async def qualifyContractsAsync(self, contract):
        exch = contract.exchange or "<none>"
        self.calls.append(exch)
        return self._behavior(contract)


@pytest.fixture
def provider() -> IBProvider:
    p = IBProvider.__new__(IBProvider)  # skip __init__ (no real IB connection)
    p._contracts = {}
    return p


async def test_qualify_falls_back_when_smart_returns_none(provider: IBProvider):
    """SMART yields [None] (like SPX); bare exchange resolves -> use that."""

    def behavior(contract):
        if contract.exchange == "SMART":
            return [None]  # IB's "no security definition" shape
        return [_FakeContract(contract.conId, exchange="CBOE")]

    provider.ib = _FakeIB(behavior)
    result = await provider._qualify(416904)

    assert result is not None
    assert result.conId == 416904
    assert result.exchange == "CBOE"
    assert provider.ib.calls == ["SMART", "<none>"]  # tried SMART, then bare
    assert provider._contracts[416904] is result  # cached the real contract


async def test_qualify_uses_smart_when_it_resolves(provider: IBProvider):
    """Normal stock: SMART resolves immediately, no fallback call."""

    def behavior(contract):
        return [_FakeContract(contract.conId, exchange="SMART", secType="STK")]

    provider.ib = _FakeIB(behavior)
    result = await provider._qualify(265598)

    assert result is not None
    assert provider.ib.calls == ["SMART"]  # no fallback needed


async def test_qualify_returns_none_when_all_attempts_fail(provider: IBProvider):
    """Both [None] and exceptions across attempts -> None (and not cached)."""

    def behavior(contract):
        if contract.exchange == "SMART":
            return [None]
        raise RuntimeError("boom")

    provider.ib = _FakeIB(behavior)
    result = await provider._qualify(999999)

    assert result is None
    assert 999999 not in provider._contracts


class _FakeDescription:
    def __init__(self, contract, derivativeSecTypes=None):
        self.contract = contract
        self.derivativeSecTypes = derivativeSecTypes or []


class _FakeSearchIB:
    """Fakes reqMatchingSymbolsAsync + qualifyContractsAsync for search()."""

    def __init__(self, descriptions, contfut):
        self._descriptions = descriptions
        self._contfut = contfut  # symbol -> qualified CONTFUT contract (or None)
        self.qualified_symbols: list[str] = []

    async def reqMatchingSymbolsAsync(self, query):
        return self._descriptions

    async def qualifyContractsAsync(self, contract):
        # search() only qualifies ContFuture(symbol); record and answer.
        self.qualified_symbols.append(contract.symbol)
        qc = self._contfut.get(contract.symbol)
        return [qc] if qc is not None else [None]


async def test_search_adds_continuous_future_first(provider: IBProvider):
    """MES comes back as an index advertising FUT -> a continuous future is added."""
    idx = _FakeContract(
        362673777, exchange="CME", secType="IND", symbol="MES",
        description="Micro E-Mini S&P 500 Stock Price Index",
    )
    stock = _FakeContract(
        481976377, exchange="FWB2", secType="STK", symbol="MES",
        currency="EUR", description="MITSUBISHI ESTATE CO LTD",
    )
    descriptions = [
        _FakeDescription(idx, derivativeSecTypes=["FOP", "FUT", "BAG"]),
        _FakeDescription(stock, derivativeSecTypes=[]),
    ]
    contfut = _FakeContract(
        793356217, exchange="CME", secType="CONTFUT", symbol="MES",
    )
    provider.ib = _FakeSearchIB(descriptions, {"MES": contfut})

    results = await provider.search("mes")

    assert provider.ib.qualified_symbols == ["MES"]  # only the FUT-bearing one
    # continuous future is first and typed as a future
    assert results[0].con_id == 793356217
    assert results[0].sec_type == SecType.FUT
    assert results[0].asset_class == "us_future"
    assert "continuous future" in results[0].description.lower()
    # the CONTFUT contract is cached so the detail page resolves the con_id
    assert 793356217 in provider._contracts
    # the index and stock are still present
    assert {r.con_id for r in results} >= {362673777, 481976377}


async def test_search_without_futures_adds_nothing(provider: IBProvider):
    """A plain stock symbol triggers no ContFuture qualification."""
    stock = _FakeContract(
        265598, exchange="NASDAQ", secType="STK", symbol="AAPL",
        description="APPLE INC",
    )
    provider.ib = _FakeSearchIB([_FakeDescription(stock, [])], {})

    results = await provider.search("aapl")

    assert provider.ib.qualified_symbols == []  # no futures advertised
    assert [r.con_id for r in results] == [265598]
