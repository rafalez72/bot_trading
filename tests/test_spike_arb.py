"""Tests para src/copybot/spike_arb.py.

Cubre 5 casos:
  1. Spike detection: 0.5% en 30s → trigger
  2. No-trigger si <threshold
  3. Limit price calc (mid actual del side correcto)
  4. TTL expira → cancel persistido como ``cancelled``
  5. Fill → row pasa a ``filled`` y guarda fill_price

Mockea el ``order_executor`` y ``mid_resolver`` (no toca CLOB ni red). Usa
fixture ``isolated_db`` (ver ``tests/conftest.py``) para que los inserts a
``spike_arb_trades`` queden aislados.
"""
from __future__ import annotations

import pytest

from src.copybot.spike_arb import (
    SpikeArb,
    SpikeArbConfig,
    init_table,
)


# ---------- Helpers ----------

async def _yes_executor(filled: bool, fill_price: float | None = None):
    """Factory que devuelve un order_executor que filea o no según el flag."""
    async def _exec(signal):
        return {
            "ok": True,
            "order_id": f"MOCK-{signal['symbol']}-{signal['ts_ms']}",
            "filled": filled,
            "fill_price": fill_price if filled else None,
            "error": None,
            "raw": {"mock": True},
        }
    return _exec


async def _mid_const(value: float):
    async def _resolver(symbol: str, side: str):
        return value
    return _resolver


def _ensure_schema(isolated_db):  # noqa: ARG001 — fixture forces wiring
    """Crea la tabla spike_arb_trades en la DB aislada."""
    init_table()


# ---------- Test 1: spike detection 0.5% / 30s → trigger ----------

@pytest.mark.asyncio
async def test_spike_detection_triggers_signal(isolated_db):
    _ensure_schema(isolated_db)
    cfg = SpikeArbConfig(threshold_pct=0.4, window_s=30, target_size_usdc=3.0)
    executor = await _yes_executor(filled=False)
    resolver = await _mid_const(0.50)
    arb = SpikeArb(cfg, order_executor=executor, mid_resolver=resolver)

    # Sample antiguo (t=0s) y sample actual (t=30s) +0.5%.
    base_ts = 1_700_000_000_000
    p0 = 100.0
    p1 = p0 * 1.005  # +0.5%

    res0 = await arb.on_binance_tick("BTCUSDT", p0, base_ts)
    assert res0 is None, "Primer tick no debería disparar (sin history previa)"
    res1 = await arb.on_binance_tick("BTCUSDT", p1, base_ts + 30_000)
    await arb.wait_inflight(timeout=2.0)

    assert res1 is not None, "Spike +0.5% en 30s DEBE disparar signal"
    assert res1["side"] == "UP"
    assert res1["spike_pct"] == pytest.approx(0.5, abs=0.01)
    assert res1["mid_at_signal"] == pytest.approx(0.50, abs=1e-9)
    assert res1["limit_price"] == pytest.approx(0.50, abs=1e-9)
    assert res1["size_usdc"] == 3.0
    assert arb.metrics.spikes_detected == 1


# ---------- Test 2: no-trigger si <threshold ----------

@pytest.mark.asyncio
async def test_below_threshold_no_trigger(isolated_db):
    _ensure_schema(isolated_db)
    cfg = SpikeArbConfig(threshold_pct=0.4, window_s=30)
    arb = SpikeArb(
        cfg,
        order_executor=await _yes_executor(filled=False),
        mid_resolver=await _mid_const(0.50),
    )

    base_ts = 1_700_000_000_000
    # Movimiento +0.2% < threshold 0.4% → no trigger
    await arb.on_binance_tick("BTCUSDT", 100.0, base_ts)
    res = await arb.on_binance_tick("BTCUSDT", 100.20, base_ts + 30_000)
    await arb.wait_inflight(timeout=1.0)

    assert res is None
    assert arb.metrics.spikes_detected == 0


# ---------- Test 3: limit price = mid actual del side correcto ----------

@pytest.mark.asyncio
async def test_limit_price_matches_mid_for_correct_side(isolated_db):
    _ensure_schema(isolated_db)
    cfg = SpikeArbConfig(threshold_pct=0.4, window_s=30, max_mid_target=0.55)

    # Capturamos qué signal le llega al executor.
    captured: dict = {}

    async def _capturing_exec(signal):
        captured.update(signal)
        return {
            "ok": True, "order_id": "X", "filled": False,
            "fill_price": None, "error": None, "raw": None,
        }

    # Caso UP: spike positivo, resolver devuelve mid_up=0.45 → limit=0.45.
    arb_up = SpikeArb(
        cfg,
        order_executor=_capturing_exec,
        mid_resolver=await _mid_const(0.45),
    )
    base_ts = 1_700_000_000_000
    await arb_up.on_binance_tick("ETHUSDT", 100.0, base_ts)
    await arb_up.on_binance_tick("ETHUSDT", 100.5, base_ts + 30_000)
    await arb_up.wait_inflight(timeout=2.0)

    assert captured.get("side") == "UP"
    # Limit posteado al mid del lado a comprar (UP):
    assert captured.get("limit_price") == pytest.approx(0.45, abs=1e-9)
    assert captured.get("mid_at_signal") == pytest.approx(0.45, abs=1e-9)
    # El executor lo recibe junto con el size configurado:
    assert captured.get("size_usdc") == cfg.target_size_usdc

    # Caso DOWN: spike negativo, mid_down=0.40 → limit=0.40.
    captured2: dict = {}

    async def _cap2(signal):
        captured2.update(signal)
        return {"ok": True, "order_id": "Y", "filled": False,
                "fill_price": None, "error": None, "raw": None}

    arb_dn = SpikeArb(
        cfg, order_executor=_cap2, mid_resolver=await _mid_const(0.40),
    )
    await arb_dn.on_binance_tick("ETHUSDT", 100.0, base_ts)
    await arb_dn.on_binance_tick("ETHUSDT", 99.5, base_ts + 30_000)
    await arb_dn.wait_inflight(timeout=2.0)

    assert captured2.get("side") == "DOWN"
    assert captured2.get("limit_price") == pytest.approx(0.40, abs=1e-9)


# ---------- Test 4: TTL expira → status='cancelled' en DB ----------

@pytest.mark.asyncio
async def test_ttl_expires_persists_cancelled(isolated_db):
    _ensure_schema(isolated_db)
    cfg = SpikeArbConfig(
        threshold_pct=0.4, window_s=30, target_size_usdc=3.0, limit_ttl_s=60,
    )
    # Executor simula que el TTL expiró sin fill (filled=False).
    arb = SpikeArb(
        cfg,
        order_executor=await _yes_executor(filled=False),
        mid_resolver=await _mid_const(0.50),
    )
    base_ts = 1_700_000_000_000
    await arb.on_binance_tick("SOLUSDT", 50.0, base_ts)
    await arb.on_binance_tick("SOLUSDT", 50.25, base_ts + 30_000)
    await arb.wait_inflight(timeout=2.0)

    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT symbol, side, status, fill_price, order_id "
            "FROM spike_arb_trades ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "SOLUSDT"
    assert r["side"] == "UP"
    assert r["status"] == "cancelled"
    assert r["fill_price"] is None
    assert r["order_id"] is not None
    assert arb.metrics.orders_cancelled_ttl == 1
    assert arb.metrics.orders_filled == 0


# ---------- Test 5: fill → status='filled' + fill_price ----------

@pytest.mark.asyncio
async def test_fill_persists_filled_with_price(isolated_db):
    _ensure_schema(isolated_db)
    cfg = SpikeArbConfig(threshold_pct=0.4, window_s=30, target_size_usdc=3.0)
    arb = SpikeArb(
        cfg,
        order_executor=await _yes_executor(filled=True, fill_price=0.485),
        mid_resolver=await _mid_const(0.48),
    )
    base_ts = 1_700_000_000_000
    await arb.on_binance_tick("BTCUSDT", 100.0, base_ts)
    await arb.on_binance_tick("BTCUSDT", 100.5, base_ts + 30_000)
    await arb.wait_inflight(timeout=2.0)

    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT symbol, side, status, fill_price, mid_at_signal, "
            "limit_price, size_usdc, order_id, filled_at "
            "FROM spike_arb_trades ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "BTCUSDT"
    assert r["side"] == "UP"
    assert r["status"] == "filled"
    assert r["fill_price"] == pytest.approx(0.485, abs=1e-9)
    assert r["mid_at_signal"] == pytest.approx(0.48, abs=1e-9)
    assert r["limit_price"] == pytest.approx(0.48, abs=1e-9)
    assert r["size_usdc"] == pytest.approx(3.0, abs=1e-9)
    assert r["order_id"] is not None
    assert r["filled_at"] is not None
    assert arb.metrics.orders_filled == 1


# ---------- Bonus guard: overbought → no order ----------

@pytest.mark.asyncio
async def test_overbought_skipped_no_order(isolated_db):
    _ensure_schema(isolated_db)
    cfg = SpikeArbConfig(threshold_pct=0.4, window_s=30, max_mid_target=0.55)
    # mid=0.70 > cap 0.55 → debe skip aunque el spike sea fuerte.
    arb = SpikeArb(
        cfg,
        order_executor=await _yes_executor(filled=True, fill_price=0.70),
        mid_resolver=await _mid_const(0.70),
    )
    base_ts = 1_700_000_000_000
    await arb.on_binance_tick("BTCUSDT", 100.0, base_ts)
    res = await arb.on_binance_tick("BTCUSDT", 101.0, base_ts + 30_000)
    await arb.wait_inflight(timeout=1.0)

    assert res is None
    assert arb.metrics.skipped_overbought == 1
    assert arb.metrics.orders_filled == 0
    assert arb.metrics.orders_cancelled_ttl == 0
