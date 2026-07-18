"""API smoke tests over the ASGI app (mock provider)."""

import httpx
import pytest

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    # httpx ASGITransport doesn't run lifespan; do it manually
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_status(client: httpx.AsyncClient):
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "mock"
    assert body["connected"] is True


async def test_account(client: httpx.AsyncClient):
    r = await client.get("/api/account")
    assert r.status_code == 200
    assert r.json()["net_liquidation"] > 0


async def test_positions_grouped(client: httpx.AsyncClient):
    r = await client.get("/api/positions")
    assert r.status_code == 200
    groups = r.json()
    assert len(groups) >= 4
    spx = next(g for g in groups if g["symbol"] == "SPX")
    assert len(spx["positions"]) >= 3
    leg = spx["positions"][0]
    assert leg["quote"] is not None
    assert leg["greeks"] is not None


async def test_instrument_and_404(client: httpx.AsyncClient):
    r = await client.get("/api/positions")
    con_id = r.json()[0]["positions"][0]["instrument"]["con_id"]
    r2 = await client.get(f"/api/instrument/{con_id}")
    assert r2.status_code == 200
    r3 = await client.get("/api/instrument/999999")
    assert r3.status_code == 404


async def test_history(client: httpx.AsyncClient):
    r = await client.get("/api/positions")
    groups = r.json()
    aapl = next(g for g in groups if g["symbol"] == "AAPL")
    stk = next(p for p in aapl["positions"] if p["instrument"]["sec_type"] == "STK")
    con_id = stk["instrument"]["con_id"]
    r2 = await client.get(f"/api/history/{con_id}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["exchange_tz"] == "America/New_York"
    assert len(body["bars"]) > 100
