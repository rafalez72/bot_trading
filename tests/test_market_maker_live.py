"""Tests para el wiring LIVE_MODE de market_maker → clob_client.

Cobertura:
1. `place_limit_order` LIVE delega a clob_client.place_limit_order_gtc.
2. `cancel_order` LIVE delega a clob_client.cancel_order.
3. `get_open_orders` LIVE delega a clob_client.get_open_orders.
4. `get_fills_since` LIVE delega a clob_client.get_fills_since.
5. Paper mode mantiene comportamiento stub (no llama a clob_client).
6. `place_limit_order` LIVE propaga error del SDK como LimitOrderResult(ok=False).

Mockeamos clob_client al nivel del módulo importado para no tocar la red ni
el SDK py-clob-client real. NO hacemos live calls.

Nota sobre el patching de LIVE_MODE:
- market_maker.py hace `from src.config import LIVE_MODE` *dentro* de cada
  function (lazy), entonces monkeypatch sobre `src.config.LIVE_MODE` toma
  efecto en cada call. Si fuera bound al import-time del módulo no
  funcionaría.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.copybot import market_maker as mm_mod


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@dataclass
class _FakeOrderResult:
    """Mimic minimo de clob_client.OrderResult — solo los campos que usamos."""
    ok: bool
    order_id: Optional[str] = None
    error: Optional[str] = None


def _set_live(monkeypatch, value: bool) -> None:
    """Patch LIVE_MODE en el módulo config (donde lo lee market_maker lazy)."""
    import src.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "LIVE_MODE", value)


# --------------------------------------------------------------------------- #
# 1. place_limit_order LIVE → delega a clob_client.place_limit_order_gtc
# --------------------------------------------------------------------------- #


def test_place_limit_order_live_delegates_to_clob_gtc(monkeypatch):
    """LIVE_MODE=true debe llamar a place_limit_order_gtc con los kwargs
    correctos y devolver un LimitOrderResult mapeado del OrderResult del SDK.
    """
    _set_live(monkeypatch, True)

    captured: dict = {}

    def fake_gtc(*, token_id, side, price, size, ttl_s, condition_id):
        captured.update({
            "token_id": token_id, "side": side, "price": price,
            "size": size, "ttl_s": ttl_s, "condition_id": condition_id,
        })
        return _FakeOrderResult(ok=True, order_id="LIVE-ORDER-123")

    # Patcheamos en el módulo clob_client (lazy import dentro de la function).
    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "place_limit_order_gtc", fake_gtc)

    res = mm_mod.place_limit_order(
        token_id="0xTOKEN_A", side="BUY", price=0.485, size_usdc=2.0,
    )

    assert res.ok is True
    assert res.order_id == "LIVE-ORDER-123"
    assert res.error is None
    # Verificamos que los kwargs llegaron tal cual + ttl_s=None y condition_id=None.
    assert captured == {
        "token_id": "0xTOKEN_A",
        "side": "BUY",
        "price": 0.485,
        "size": 2.0,
        "ttl_s": None,
        "condition_id": None,
    }


def test_place_limit_order_live_propagates_sdk_error(monkeypatch):
    """Si el SDK devuelve ok=False, LimitOrderResult debe quedar ok=False con
    el error string preservado. NO levantamos exception (silent fail aquí
    es OK porque el caller decide si reintentar)."""
    _set_live(monkeypatch, True)

    def fake_gtc(*, token_id, side, price, size, ttl_s, condition_id):
        return _FakeOrderResult(ok=False, error="insufficient balance")

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "place_limit_order_gtc", fake_gtc)

    res = mm_mod.place_limit_order(
        token_id="0xTOK", side="SELL", price=0.515, size_usdc=2.0,
    )
    assert res.ok is False
    assert res.error == "insufficient balance"
    assert res.order_id is None


# --------------------------------------------------------------------------- #
# 2. cancel_order LIVE → delega
# --------------------------------------------------------------------------- #


def test_cancel_order_live_delegates(monkeypatch):
    """LIVE_MODE=true debe llamar a clob_client.cancel_order y devolver el bool."""
    _set_live(monkeypatch, True)

    received: list[str] = []

    def fake_cancel(order_id):
        received.append(order_id)
        return True

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "cancel_order", fake_cancel)

    ok = mm_mod.cancel_order("LIVE-ORDER-123")
    assert ok is True
    assert received == ["LIVE-ORDER-123"]


def test_cancel_order_live_returns_false_on_sdk_failure(monkeypatch):
    """Si el SDK devuelve False (ya fillada / no_canceled), pasamos el False."""
    _set_live(monkeypatch, True)

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "cancel_order", lambda oid: False)

    assert mm_mod.cancel_order("ALREADY-FILLED-999") is False


# --------------------------------------------------------------------------- #
# 3. get_open_orders LIVE → delega (token_id=None = todas)
# --------------------------------------------------------------------------- #


def test_get_open_orders_live_delegates(monkeypatch):
    """LIVE_MODE=true debe llamar a clob_client.get_open_orders(token_id=None)
    y devolver la lista raw del SDK sin transformación."""
    _set_live(monkeypatch, True)

    captured: dict = {}

    def fake_open(token_id=None):
        captured["token_id"] = token_id
        return [
            {"id": "ORD1", "side": "BUY", "price": "0.485", "size": "2.0"},
            {"id": "ORD2", "side": "SELL", "price": "0.515", "size": "2.0"},
        ]

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "get_open_orders", fake_open)

    out = mm_mod.get_open_orders()
    assert captured["token_id"] is None  # MM siempre lista todas
    assert len(out) == 2
    assert out[0]["id"] == "ORD1"
    assert out[1]["side"] == "SELL"


# --------------------------------------------------------------------------- #
# 4. get_fills_since LIVE → delega
# --------------------------------------------------------------------------- #


def test_get_fills_since_live_delegates(monkeypatch):
    """LIVE_MODE=true debe llamar a clob_client.get_fills_since(timestamp)
    y devolver la lista del SDK sin transformación."""
    _set_live(monkeypatch, True)

    captured: dict = {}

    def fake_fills(timestamp_s):
        captured["ts"] = timestamp_s
        return [
            {"order_id": "ORD1", "fill_price": 0.485,
             "filled_at": 1_700_000_500, "side": "BUY"},
        ]

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "get_fills_since", fake_fills)

    out = mm_mod.get_fills_since(1_700_000_000)
    assert captured["ts"] == 1_700_000_000
    assert len(out) == 1
    assert out[0]["order_id"] == "ORD1"


# --------------------------------------------------------------------------- #
# 5. Paper mode (LIVE_MODE=false) → mantiene comportamiento stub
# --------------------------------------------------------------------------- #


def test_paper_mode_place_returns_stub_id(monkeypatch):
    """Con LIVE_MODE=false, place_limit_order NO debe llamar a clob_client
    y debe devolver un fake order_id determinístico."""
    _set_live(monkeypatch, False)

    # Si el código llamara a clob_client igual, este patch tira AssertionError.
    def boom(*a, **kw):
        raise AssertionError("clob_client no debe ser llamado en paper mode")

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "place_limit_order_gtc", boom)
    monkeypatch.setattr(clob_mod, "cancel_order", boom)
    monkeypatch.setattr(clob_mod, "get_fills_since", boom)

    res = mm_mod.place_limit_order(
        token_id="0xTOKABCDEFGHIJ", side="BUY", price=0.485, size_usdc=2.0,
    )
    assert res.ok is True
    assert res.order_id is not None
    assert res.order_id.startswith("STUB-")
    # Determinístico: side y precio (en bps escalados x10000) embedded.
    assert "BUY" in res.order_id
    assert "4850" in res.order_id


def test_paper_mode_cancel_and_fills_skip_clob(monkeypatch):
    """Paper: cancel_order siempre True, get_fills_since []. Sin red."""
    _set_live(monkeypatch, False)

    def boom(*a, **kw):
        raise AssertionError("clob_client no debe ser llamado en paper mode")

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "cancel_order", boom)
    monkeypatch.setattr(clob_mod, "get_fills_since", boom)

    assert mm_mod.cancel_order("ANY-ID") is True
    assert mm_mod.get_fills_since(0) == []


def test_paper_mode_get_open_orders_reads_db(monkeypatch, isolated_db):
    """Paper: get_open_orders lee de tabla mm_orders (NO clob_client)."""
    _set_live(monkeypatch, False)

    def boom(*a, **kw):
        raise AssertionError("clob_client no debe ser llamado en paper mode")

    import src.polymarket.clob_client as clob_mod
    monkeypatch.setattr(clob_mod, "get_open_orders", boom)

    # Insertamos una row de prueba en la tabla mm_orders (la creamos primero).
    mm_mod._ensure_schema()
    from src.db.schema import tx
    with tx() as conn:
        conn.execute(
            "INSERT INTO mm_orders (condition_id, side, price, size_usdc, "
            "order_id, status) VALUES (?, ?, ?, ?, ?, 'open')",
            ("0xCID_PAPER", "BUY", 0.485, 2.0, "STUB-XYZ"),
        )

    out = mm_mod.get_open_orders()
    assert len(out) == 1
    assert out[0]["condition_id"] == "0xCID_PAPER"
    assert out[0]["order_id"] == "STUB-XYZ"
    assert out[0]["side"] == "BUY"


# --------------------------------------------------------------------------- #
# 6. Token resolver fail-soft cuando módulo no existe
# --------------------------------------------------------------------------- #


def test_resolve_market_tokens_fail_soft_on_missing_module(monkeypatch):
    """Si `src.polymarket.token_resolver` no existe (otro agent aún no lo
    crea), `_resolve_market_tokens` debe devolver None sin tirar.

    Simulamos el ImportError patcheando builtins.__import__ para fallar solo
    cuando se intenta importar token_resolver.
    """
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "src.polymarket.token_resolver":
            raise ImportError("simulated: token_resolver not yet created")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    cfg = mm_mod.MarketMakerConfig(
        enabled=True, spread_bps=300, bet_per_side_usdc=2.0,
        max_concurrent_pairs=5,
    )
    mm = mm_mod.MarketMaker(config=cfg)

    import asyncio
    out = asyncio.run(mm._resolve_market_tokens({"slug": "test-slug"}))
    assert out is None


def test_resolve_market_tokens_returns_none_without_slug(monkeypatch):
    """Sin slug en el market dict → None (no resolvemos sin clave de búsqueda)."""
    cfg = mm_mod.MarketMakerConfig(
        enabled=True, spread_bps=300, bet_per_side_usdc=2.0,
        max_concurrent_pairs=5,
    )
    mm = mm_mod.MarketMaker(config=cfg)

    import asyncio
    out = asyncio.run(mm._resolve_market_tokens({"condition_id": "0xCID"}))
    assert out is None


# --------------------------------------------------------------------------- #
# 7. _ensure_schema isolation pin (no se rompe entre tests live/paper)
# --------------------------------------------------------------------------- #


def test_ensure_schema_idempotent(isolated_db):
    """Llamar varias veces no debe explotar — DDL es CREATE TABLE IF NOT EXISTS."""
    mm_mod._ensure_schema()
    mm_mod._ensure_schema()
    mm_mod._ensure_schema()
    # Si llegamos acá sin exception, el test pasa.
