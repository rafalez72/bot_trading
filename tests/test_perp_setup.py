"""Tests para ``src.binance.perp_account.startup_checks`` y helpers asociados.

Estrategia de mocking
=====================
Inyectamos un ``httpx.AsyncClient`` con :class:`httpx.MockTransport` directo
en :class:`BinancePerpClient` para no tocar la red. El handler inspecciona
``request.url.path`` y devuelve respuestas pre-grabadas según el endpoint:

- ``GET  /fapi/v1/positionSide/dual``
- ``POST /fapi/v1/positionSide/dual``
- ``POST /fapi/v1/leverage``
- ``GET  /fapi/v2/account``

Las assertions se hacen sobre:

- el resultado booleano de ``startup_checks``
- una lista compartida ``calls`` (path + method + query params) que el
  handler va llenando, para verificar qué endpoints fueron golpeados.

Casos cubiertos
---------------
1. Happy path: keys + hedge=False → POST hedge → set_leverage(2x) cada
   símbolo + margin OK → True.
2. API keys faltantes → return False sin tocar httpx.
3. Hedge mode ya habilitado (GET=True) → skip POST.
4. Balance USDT < min_usdt_balance → log warning pero return True.
5. set_leverage de un símbolo falla → no aborta, sigue con los otros.
6. Hedge mode falla con error duro (-2014 invalid key) → return False.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.binance.perp_client import BinancePerpClient
from src.binance import perp_account


# ----- helpers de mocking -----

def _resp(payload: Any, status: int = 200) -> httpx.Response:
    """Construye httpx.Response con body JSON."""
    return httpx.Response(
        status_code=status,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
    )


def _build_handler(routes: dict[tuple[str, str], Any], calls: list[dict]):
    """Devuelve un handler para httpx.MockTransport.

    ``routes`` mapea ``(method, path)`` → callable(request) -> Response,
    o directamente Response. ``calls`` se llena con cada request entrante
    (para assertions de "qué endpoints se llamaron").
    """
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        key = (request.method.upper(), path)
        calls.append({
            "method": request.method.upper(),
            "path": path,
            "query": dict(request.url.params),
        })
        target = routes.get(key)
        if target is None:
            # Por default, devolvemos 404 para endpoints no esperados — así
            # los tests fallan ruidosamente si tocan algo no anticipado.
            return _resp({"code": -1100, "msg": f"unmocked {key}"}, status=400)
        if callable(target):
            return target(request)
        return target  # type: ignore[return-value]

    return _handler


def _make_client(routes: dict[tuple[str, str], Any], calls: list[dict]) -> BinancePerpClient:
    """Construye BinancePerpClient con httpx.MockTransport inyectado."""
    transport = httpx.MockTransport(_build_handler(routes, calls))
    http = httpx.AsyncClient(transport=transport)
    return BinancePerpClient(
        api_key="test_key",
        api_secret="test_secret",
        client=http,
    )


# ----- tests -----

@pytest.mark.asyncio
async def test_startup_checks_happy_path(monkeypatch):
    """Keys OK + hedge mode disabled → POST hedge + set_leverage por símbolo
    + margin info → return True. Verificamos endpoints golpeados."""
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")

    calls: list[dict] = []
    routes = {
        ("GET", "/fapi/v1/positionSide/dual"): _resp({"dualSidePosition": False}),
        ("POST", "/fapi/v1/positionSide/dual"): _resp({"code": 200, "msg": "success"}),
        ("POST", "/fapi/v1/leverage"): _resp(
            {"symbol": "BTCUSDT", "leverage": 2, "maxNotionalValue": "1000"}
        ),
        ("GET", "/fapi/v2/account"): _resp({
            "totalWalletBalance": "500.00",
            "availableBalance": "450.00",
            "totalUnrealizedProfit": "1.25",
            "totalMarginBalance": "501.25",
        }),
    }
    client = _make_client(routes, calls)

    async with client:
        ok = await perp_account.startup_checks(
            client=client,
            symbols=["BTCUSDT", "ETHUSDT"],
            leverage=2,
        )

    assert ok is True
    paths_methods = [(c["method"], c["path"]) for c in calls]
    assert ("GET", "/fapi/v1/positionSide/dual") in paths_methods
    assert ("POST", "/fapi/v1/positionSide/dual") in paths_methods
    # set_leverage llamado por cada símbolo.
    leverage_calls = [c for c in calls if c["path"] == "/fapi/v1/leverage"]
    assert len(leverage_calls) == 2
    syms = sorted(c["query"].get("symbol") for c in leverage_calls)
    assert syms == ["BTCUSDT", "ETHUSDT"]
    for c in leverage_calls:
        assert c["query"].get("leverage") == "2"
    assert ("GET", "/fapi/v2/account") in paths_methods


@pytest.mark.asyncio
async def test_startup_checks_missing_api_keys(monkeypatch):
    """Sin BINANCE_API_KEY/SECRET en env → False sin tocar httpx."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)

    calls: list[dict] = []
    # Si por error toca httpx, el handler vacío devuelve 400 y rompería —
    # exactamente lo que queremos detectar.
    client = _make_client({}, calls)

    async with client:
        ok = await perp_account.startup_checks(
            client=client, symbols=["BTCUSDT"], leverage=2,
        )

    assert ok is False
    assert calls == []  # ningún request salió.


@pytest.mark.asyncio
async def test_startup_checks_hedge_already_enabled(monkeypatch):
    """GET dice dualSidePosition=True → skip POST hedge, sigue con leverage."""
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")

    calls: list[dict] = []
    routes = {
        ("GET", "/fapi/v1/positionSide/dual"): _resp({"dualSidePosition": True}),
        # NO incluimos POST hedge — si lo llama, recibe 400 y test rompe.
        ("POST", "/fapi/v1/leverage"): _resp(
            {"symbol": "BTCUSDT", "leverage": 5, "maxNotionalValue": "1000"}
        ),
        ("GET", "/fapi/v2/account"): _resp({
            "totalWalletBalance": "200.00",
            "availableBalance": "180.00",
            "totalUnrealizedProfit": "0.00",
        }),
    }
    client = _make_client(routes, calls)

    async with client:
        ok = await perp_account.startup_checks(
            client=client, symbols=["BTCUSDT"], leverage=5,
        )

    assert ok is True
    methods_paths = [(c["method"], c["path"]) for c in calls]
    # NO debe haber POST a positionSide/dual.
    assert ("POST", "/fapi/v1/positionSide/dual") not in methods_paths
    assert ("GET", "/fapi/v1/positionSide/dual") in methods_paths


@pytest.mark.asyncio
async def test_startup_checks_low_balance_returns_true_with_warning(
    monkeypatch, caplog
):
    """available < min_usdt_balance → log.warning pero return True."""
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")

    calls: list[dict] = []
    routes = {
        ("GET", "/fapi/v1/positionSide/dual"): _resp({"dualSidePosition": True}),
        ("POST", "/fapi/v1/leverage"): _resp(
            {"symbol": "BTCUSDT", "leverage": 2}
        ),
        ("GET", "/fapi/v2/account"): _resp({
            "totalWalletBalance": "10.00",
            "availableBalance": "10.00",
            "totalUnrealizedProfit": "0.00",
        }),
    }
    client = _make_client(routes, calls)

    import logging
    with caplog.at_level(logging.WARNING, logger="src.binance.perp_account"):
        async with client:
            ok = await perp_account.startup_checks(
                client=client, symbols=["BTCUSDT"], leverage=2,
                min_usdt_balance=50.0,
            )

    assert ok is True
    # Buscamos que haya algún warning sobre "available USDT".
    warning_msgs = [
        r.getMessage() for r in caplog.records
        if r.levelno >= logging.WARNING and "available USDT" in r.getMessage()
    ]
    assert warning_msgs, f"no warning capturado en {caplog.records!r}"


@pytest.mark.asyncio
async def test_startup_checks_set_leverage_partial_failure(monkeypatch):
    """Si un símbolo falla en set_leverage, los demás se siguen seteando y
    startup_checks devuelve True (es best-effort)."""
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")

    calls: list[dict] = []

    def _leverage_handler(request: httpx.Request) -> httpx.Response:
        # BADUSDT → error -1121 (invalid symbol). BTCUSDT/ETHUSDT → OK.
        sym = request.url.params.get("symbol", "")
        if sym == "BADUSDT":
            return _resp(
                {"code": -1121, "msg": "Invalid symbol."}, status=400,
            )
        return _resp({"symbol": sym, "leverage": 2})

    routes = {
        ("GET", "/fapi/v1/positionSide/dual"): _resp({"dualSidePosition": True}),
        ("POST", "/fapi/v1/leverage"): _leverage_handler,
        ("GET", "/fapi/v2/account"): _resp({
            "totalWalletBalance": "300.00",
            "availableBalance": "280.00",
            "totalUnrealizedProfit": "0.00",
        }),
    }
    client = _make_client(routes, calls)

    async with client:
        ok = await perp_account.startup_checks(
            client=client,
            symbols=["BTCUSDT", "BADUSDT", "ETHUSDT"],
            leverage=2,
        )

    assert ok is True
    leverage_calls = [c for c in calls if c["path"] == "/fapi/v1/leverage"]
    syms = sorted(c["query"].get("symbol") for c in leverage_calls)
    # Los 3 fueron intentados (no aborta al primer error).
    assert syms == ["BADUSDT", "BTCUSDT", "ETHUSDT"]


@pytest.mark.asyncio
async def test_startup_checks_hedge_mode_hard_failure(monkeypatch):
    """Hedge mode POST falla con error no-recuperable → return False.

    Simulamos -2014 (invalid api key) — el GET también falla con eso, el
    POST también, y al no ser ``-4059`` el código se propaga como error
    duro y startup_checks atrapa BinancePerpError → False."""
    monkeypatch.setenv("BINANCE_API_KEY", "test_key")
    monkeypatch.setenv("BINANCE_API_SECRET", "test_secret")

    calls: list[dict] = []
    invalid_key_resp = _resp(
        {"code": -2014, "msg": "API-key format invalid."}, status=400,
    )
    routes = {
        ("GET", "/fapi/v1/positionSide/dual"): invalid_key_resp,
        ("POST", "/fapi/v1/positionSide/dual"): invalid_key_resp,
    }
    client = _make_client(routes, calls)

    async with client:
        ok = await perp_account.startup_checks(
            client=client, symbols=["BTCUSDT"], leverage=2,
        )

    assert ok is False
    # No debería intentar set_leverage si hedge_mode falló duro.
    leverage_calls = [c for c in calls if c["path"] == "/fapi/v1/leverage"]
    assert leverage_calls == []


# ----- tests directos de los helpers individuales -----

@pytest.mark.asyncio
async def test_get_margin_info_parses_account_endpoint():
    """get_margin_info devuelve total/available/used/unrealized_pnl
    parseados desde GET /fapi/v2/account."""
    calls: list[dict] = []
    routes = {
        ("GET", "/fapi/v2/account"): _resp({
            "totalWalletBalance": "1000.50",
            "availableBalance": "750.25",
            "totalUnrealizedProfit": "-12.30",
            "totalMarginBalance": "988.20",
        }),
    }
    client = _make_client(routes, calls)

    async with client:
        margin = await perp_account.get_margin_info(client=client)

    assert margin["total"] == pytest.approx(1000.50)
    assert margin["available"] == pytest.approx(750.25)
    assert margin["used"] == pytest.approx(1000.50 - 750.25)
    assert margin["unrealized_pnl"] == pytest.approx(-12.30)


@pytest.mark.asyncio
async def test_ensure_hedge_mode_enabled_uses_get_first():
    """Si GET dice True, no se llama POST."""
    calls: list[dict] = []
    routes = {
        ("GET", "/fapi/v1/positionSide/dual"): _resp({"dualSidePosition": True}),
    }
    client = _make_client(routes, calls)

    async with client:
        ok = await perp_account.ensure_hedge_mode_enabled(client=client)

    assert ok is True
    paths = [(c["method"], c["path"]) for c in calls]
    assert ("GET", "/fapi/v1/positionSide/dual") in paths
    assert ("POST", "/fapi/v1/positionSide/dual") not in paths
