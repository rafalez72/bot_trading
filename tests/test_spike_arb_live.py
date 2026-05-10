"""Tests para `_live_order_executor` en src/copybot/spike_arb.py.

Cubre el wiring real LIVE: token_resolver + clob_client.place_limit_order_gtc.

Mockea ambos módulos en sys.modules para no requerir red ni SDK CLOB.
"""
from __future__ import annotations

import sys
import types

import pytest

from src.copybot import spike_arb as sa
from src.polymarket.clob_client import OrderResult


# ----------------- Helpers de mocking -----------------

def _install_token_resolver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_id: str | None = "TK-MOCK-123",
    raises: Exception | None = None,
    capture: dict | None = None,
):
    """Instala un módulo fake src.polymarket.token_resolver con
    `resolve_token_id_by_condition_id(client, slug, outcome_index)`.

    Si `capture` es un dict, se llenan las kwargs reales recibidas.
    """
    mod = types.ModuleType("src.polymarket.token_resolver")

    async def _resolver(client, slug, outcome_index):  # noqa: ARG001
        if capture is not None:
            capture["slug"] = slug
            capture["outcome_index"] = outcome_index
            capture["client_is_polymarket"] = client is not None
        if raises is not None:
            raise raises
        return token_id

    mod.resolve_token_id_by_condition_id = _resolver  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.polymarket.token_resolver", mod)
    return mod


def _patch_place_limit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: OrderResult,
    capture: dict | None = None,
):
    """Reemplaza src.polymarket.clob_client.place_limit_order_gtc por un
    stub que devuelve `result`. Si `capture` es dict, guarda kwargs.
    """
    from src.polymarket import clob_client as cc

    def _fake(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return result

    monkeypatch.setattr(cc, "place_limit_order_gtc", _fake)


def _base_signal(**overrides) -> dict:
    base = {
        "bucket_slug": "btc-updown-5m-1778000000",
        "symbol": "BTCUSDT",
        "side": "UP",
        "spike_pct": 0.5,
        "mid_at_signal": 0.50,
        "limit_price": 0.50,
        "size_usdc": 3.0,
        "ttl_s": 60,
        "ts_ms": 1_700_000_000_000,
    }
    base.update(overrides)
    return base


# ----------------- Tests -----------------

@pytest.mark.asyncio
async def test_live_executor_calls_clob_and_resolver(monkeypatch):
    """1) Ok-path: resolver devuelve token_id, place_limit ok → ok=True con
    order_id, fill_price=limit_price (placeholder), token_id en payload.
    """
    captured_resolve: dict = {}
    captured_clob: dict = {}
    _install_token_resolver(
        monkeypatch, token_id="TK-OK", capture=captured_resolve
    )
    _patch_place_limit(
        monkeypatch,
        result=OrderResult(ok=True, order_id="ORD-1", status="live"),
        capture=captured_clob,
    )

    sig = _base_signal(side="UP", limit_price=0.42, size_usdc=2.10, ttl_s=45)
    out = await sa._live_order_executor(sig)

    assert out["ok"] is True, out
    assert out["order_id"] == "ORD-1"
    assert out["fill_price"] == pytest.approx(0.42)
    assert out["token_id"] == "TK-OK"
    # Resolver fue llamado con slug + outcome_index correcto (UP=0)
    assert captured_resolve["slug"] == sig["bucket_slug"]
    assert captured_resolve["outcome_index"] == 0
    assert captured_resolve["client_is_polymarket"] is True
    # CLOB fue llamado BUY al limit_price con shares=USDC/price
    assert captured_clob["side"] == "BUY"
    assert captured_clob["price"] == pytest.approx(0.42)
    assert captured_clob["token_id"] == "TK-OK"
    assert captured_clob["size"] == pytest.approx(2.10 / 0.42)


@pytest.mark.asyncio
async def test_live_executor_token_id_not_found(monkeypatch):
    """2) Resolver devuelve None → ok=False con error 'token_id_not_found'.
    place_limit NO debe ser llamado.
    """
    _install_token_resolver(monkeypatch, token_id=None)
    clob_called = {"n": 0}
    from src.polymarket import clob_client as cc

    def _no_call(**_kw):
        clob_called["n"] += 1
        return OrderResult(ok=True, order_id="should-not-happen")

    monkeypatch.setattr(cc, "place_limit_order_gtc", _no_call)

    out = await sa._live_order_executor(_base_signal())

    assert out["ok"] is False
    assert out["error"] == "token_id_not_found"
    assert clob_called["n"] == 0


@pytest.mark.asyncio
async def test_live_executor_clob_failure_propagates(monkeypatch):
    """3) place_limit devuelve ok=False → executor devuelve ok=False con el
    error original propagado.
    """
    _install_token_resolver(monkeypatch, token_id="TK-FAIL")
    _patch_place_limit(
        monkeypatch,
        result=OrderResult(ok=False, error="post_order: 429 rate limit"),
    )

    out = await sa._live_order_executor(_base_signal())

    assert out["ok"] is False
    assert "rate limit" in (out["error"] or "")


@pytest.mark.asyncio
async def test_live_executor_passes_ttl_and_side_down(monkeypatch):
    """4) ttl_s pasa correctamente al clob, y side='DOWN' resuelve con
    outcome_index=1 (NO).
    """
    captured_resolve: dict = {}
    captured_clob: dict = {}
    _install_token_resolver(
        monkeypatch, token_id="TK-DOWN", capture=captured_resolve
    )
    _patch_place_limit(
        monkeypatch,
        result=OrderResult(ok=True, order_id="ORD-2"),
        capture=captured_clob,
    )

    sig = _base_signal(side="DOWN", ttl_s=120, limit_price=0.30, size_usdc=6.0)
    out = await sa._live_order_executor(sig)

    assert out["ok"] is True
    # outcome_index=1 para DOWN (compramos NO)
    assert captured_resolve["outcome_index"] == 1
    # ttl_s propagado
    assert captured_clob["ttl_s"] == 120
    # shares = 6.0 / 0.30 = 20.0
    assert captured_clob["size"] == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_live_executor_invalid_side(monkeypatch):
    """5) side bogus → ok=False sin tocar resolver/clob."""
    _install_token_resolver(monkeypatch, token_id="TK-NOOP")
    from src.polymarket import clob_client as cc

    def _no_call(**_kw):
        raise AssertionError("place_limit_order_gtc no debería invocarse")

    monkeypatch.setattr(cc, "place_limit_order_gtc", _no_call)

    out = await sa._live_order_executor(_base_signal(side="SIDEWAYS"))
    assert out["ok"] is False
    assert "side" in (out["error"] or "").lower()
