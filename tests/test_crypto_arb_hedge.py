"""Tests para src/copybot/crypto_arb_hedge.py.

Cubre los flujos críticos del orchestrator atómico:

1. Open atómico OK ambas piernas → row hedge_trades status='open',
   poly_trade_id + perp_order_id seteados.
2. Poly OK, perp FALLA → rollback poly invocado, row 'leg_failed'.
3. Poly FALLA (rechazo/None) → perp NO se llama.
4. Settlement: PnL total = poly_pnl + perp_pnl - fees, row → 'closed'.
5. Min edge filter: edge < HEDGE_MIN_EDGE → no opens, no perp llamada.

Mockea ``poly_opener``, ``perp_executor`` y ``poly_rollback``. Usa fixture
``isolated_db`` (ver ``tests/conftest.py``) para que los inserts a
``hedge_trades`` queden aislados — antes de cada test llamamos
``init_table()`` para crear la tabla on-demand.
"""
from __future__ import annotations

import pytest

from src.copybot.crypto_arb_hedge import (
    CryptoArbHedge,
    HedgeConfig,
    init_table,
)


# ---------- Helpers ----------

def _ensure_schema(isolated_db):  # noqa: ARG001 — fixture forces wiring
    """Crea la tabla hedge_trades en la DB aislada."""
    init_table()


def _make_decision(**overrides):
    """Decision dict default — un setup BTC UP con edge 0.20."""
    base = {
        "bucket_slug": "btc-updown-5m-1700000000",
        "condition_id": "0xCONDITION_BTC_UP",
        "outcome": "Up",
        "outcome_index": 0,
        "symbol": "BTCUSDT",
        # spot_move +1% sobre 100 → fuerza p_up >> mid_up bajo modelo normal
        "spot_now": 101.0,
        "secs_to_close": 60.0,
        "spot_move_pct": 1.0,
        # mid_up bajo (0.40) → edge_vs_mid devuelve UP con edge alto.
        "mid_up": 0.40,
        "mid_for_side": 0.40,
        "end_ts": 1_700_000_000,
        "funding_rate": 0.0001,
        "margin_available": 100.0,
    }
    base.update(overrides)
    return base


def _make_poly_opener(pid: int | None, reject: str | None = None):
    """Factory que devuelve un opener que retorna (pid, reject) fijo + tracking."""
    calls: list[dict] = []

    def _opener(**kwargs):
        calls.append(kwargs)
        return pid, reject

    _opener.calls = calls  # type: ignore[attr-defined]
    return _opener


def _make_perp_executor(
    *,
    ok: bool = True,
    avg_price: float = 100.0,
    qty: float = 0.001,
    raise_exc: Exception | None = None,
    error: str | None = None,
):
    """Factory que devuelve un perp_executor async configurable."""
    calls: list[dict] = []

    async def _exec(req):
        calls.append(req)
        if raise_exc is not None:
            raise raise_exc
        return {
            "ok": ok,
            "order_id": "PERP-MOCK-1" if ok else None,
            "avg_price": avg_price if ok else None,
            "qty": qty if ok else None,
            "error": error,
            "raw": {"mock": True},
        }

    _exec.calls = calls  # type: ignore[attr-defined]
    return _exec


def _make_rollback_tracker():
    """poly_rollback mock que graba las llamadas (sync, igual que force_close)."""
    calls: list[tuple] = []

    def _rb(pid: int, exit_price: float, reason: str) -> None:
        calls.append((pid, exit_price, reason))

    _rb.calls = calls  # type: ignore[attr-defined]
    return _rb


# ---------- Test 1: open atómico OK ambas piernas ----------

@pytest.mark.asyncio
async def test_open_atomic_ok_both_legs(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0, leverage=2)

    poly = _make_poly_opener(pid=42, reject=None)
    perp = _make_perp_executor(ok=True, avg_price=101.05, qty=0.099)
    rb = _make_rollback_tracker()

    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp, poly_rollback=rb,
    )
    decision = _make_decision()
    record, err = await orch.evaluate_and_open(**decision)

    # Resultado del orchestrator
    assert err is None, f"Esperaba éxito, got err={err}"
    assert record is not None
    assert record["poly_trade_id"] == 42
    assert record["perp_order_id"] == "PERP-MOCK-1"
    assert record["perp_qty"] == pytest.approx(10.0 / 101.0, abs=1e-4)
    assert record["status"] == "open"
    assert record["spot_at_entry"] == pytest.approx(101.0, abs=1e-9)

    # Ambas piernas se llamaron exactamente una vez, sin rollback.
    assert len(poly.calls) == 1
    assert len(perp.calls) == 1
    assert len(rb.calls) == 0
    assert orch.metrics.opens_ok == 1
    assert orch.metrics.poly_failed == 0
    assert orch.metrics.perp_failed == 0

    # La perp request se armó con SHORT del símbolo correcto.
    pr = perp.calls[0]
    assert pr["action"] == "open"
    assert pr["symbol"] == "BTCUSDT"
    assert pr["side"] == "SELL"
    assert pr["position_side"] == "SHORT"

    # El poly_opener recibió el outcome / condition_id / size correcto.
    pcall = poly.calls[0]
    assert pcall["source_wallet"] == "crypto_arb_hedge"
    assert pcall["condition_id"] == "0xCONDITION_BTC_UP"
    assert pcall["outcome"] == "Up"
    assert pcall["outcome_index"] == 0
    assert pcall["price"] == pytest.approx(0.40, abs=1e-9)

    # Row persistida en DB con status='open'.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT bucket_slug, symbol, side, poly_trade_id, perp_order_id, "
            "perp_qty, status, spot_at_entry FROM hedge_trades"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["bucket_slug"] == "btc-updown-5m-1700000000"
    assert r["symbol"] == "BTCUSDT"
    assert r["side"] == "Up"
    assert r["poly_trade_id"] == 42
    assert r["perp_order_id"] == "PERP-MOCK-1"
    assert r["status"] == "open"


# ---------- Test 2: poly OK, perp falla → rollback poly + leg_failed ----------

@pytest.mark.asyncio
async def test_poly_ok_perp_fails_triggers_rollback(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0, leverage=2)

    poly = _make_poly_opener(pid=99, reject=None)
    perp = _make_perp_executor(ok=False, error="margin_insufficient")
    rb = _make_rollback_tracker()

    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp, poly_rollback=rb,
    )
    record, err = await orch.evaluate_and_open(**_make_decision())

    assert err is not None and "perp_failed" in err
    assert record is None

    # Rollback se invocó con (pid, mid_for_side, reason).
    assert len(rb.calls) == 1
    rb_pid, rb_price, rb_reason = rb.calls[0]
    assert rb_pid == 99
    assert rb_price == pytest.approx(0.40, abs=1e-9)
    assert rb_reason == "hedge_rollback"

    assert orch.metrics.perp_failed == 1
    assert orch.metrics.perp_rollbacks == 1
    assert orch.metrics.opens_ok == 0

    # Persisted con status='leg_failed' para auditoría.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT status, poly_trade_id, perp_order_id FROM hedge_trades"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "leg_failed"
    assert rows[0]["poly_trade_id"] == 99
    assert rows[0]["perp_order_id"] is None


# ---------- Test 2b: perp lanza excepción → mismo rollback ----------

@pytest.mark.asyncio
async def test_perp_exception_triggers_rollback(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0)

    poly = _make_poly_opener(pid=7)
    perp = _make_perp_executor(raise_exc=RuntimeError("network down"))
    rb = _make_rollback_tracker()

    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp, poly_rollback=rb,
    )
    record, err = await orch.evaluate_and_open(**_make_decision())

    assert record is None
    assert err is not None and "perp_exception" in err
    assert len(rb.calls) == 1 and rb.calls[0][0] == 7
    assert orch.metrics.perp_rollbacks == 1


# ---------- Test 3: poly falla → perp NO se llama ----------

@pytest.mark.asyncio
async def test_poly_fails_perp_not_called(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0)

    poly = _make_poly_opener(pid=None, reject="duplicate")
    perp = _make_perp_executor(ok=True)  # no debería llamarse
    rb = _make_rollback_tracker()

    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp, poly_rollback=rb,
    )
    record, err = await orch.evaluate_and_open(**_make_decision())

    assert record is None
    assert err is not None and "poly_failed" in err
    # CRÍTICO: perp NO debe haberse llamado.
    assert len(perp.calls) == 0
    # Tampoco rollback (no hay nada que rollback).
    assert len(rb.calls) == 0
    assert orch.metrics.poly_failed == 1
    assert orch.metrics.perp_failed == 0

    # Tampoco persistimos row para una falla pre-poly: la lógica de _open_atomic
    # solo llama _persist_hedge cuando ya hay poly_pid.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute("SELECT id FROM hedge_trades").fetchall()
    assert len(rows) == 0


# ---------- Test 4: settlement PnL = poly_pnl + perp_pnl - fees ----------

@pytest.mark.asyncio
async def test_settlement_pnl_calculation(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0)

    # Open con perp avg=100.0, qty=0.1 → notional=10 USDC.
    poly = _make_poly_opener(pid=11)
    perp_open = _make_perp_executor(ok=True, avg_price=100.0, qty=0.1)
    rb = _make_rollback_tracker()
    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp_open, poly_rollback=rb,
    )

    decision = _make_decision(spot_now=100.0)
    record, err = await orch.evaluate_and_open(**decision)
    assert err is None
    hedge_id = record["id"]
    assert hedge_id is not None

    # Bucket cierra: spot bajó a 99 (short ganó +0.1 USDC), poly perdió -2 USDC.
    # Cambiamos el executor del orch para devolver el close fill price.
    perp_close = _make_perp_executor(ok=True, avg_price=99.0, qty=0.1)
    orch._perp_executor = perp_close  # noqa: SLF001 — test injection

    ok, resp = await orch.close_perp_for_hedge(
        hedge_id=hedge_id,
        symbol="BTCUSDT",
        perp_qty=0.1,
        spot_now=99.0,
        poly_pnl_usdc=-2.0,
        perp_entry_price=100.0,
    )
    assert ok is True
    assert resp.get("ok") is True

    # PnL esperado:
    #   perp_pnl = (100 - 99) * 0.1 = +0.1
    #   fees = 2 * (100*0.1) * 0.0004 = 0.008
    #   total = -2 + 0.1 - 0.008 = -1.908
    from src.db.schema import db
    with db() as conn:
        row = conn.execute(
            "SELECT status, pnl_poly_usdc, pnl_perp_usdc, pnl_total_usdc, "
            "fees_total_usdc, closed_at FROM hedge_trades WHERE id=?",
            (hedge_id,),
        ).fetchone()
    assert row["status"] == "closed"
    assert row["pnl_poly_usdc"] == pytest.approx(-2.0, abs=1e-9)
    assert row["pnl_perp_usdc"] == pytest.approx(0.1, abs=1e-9)
    assert row["fees_total_usdc"] == pytest.approx(0.008, abs=1e-6)
    assert row["pnl_total_usdc"] == pytest.approx(-1.908, abs=1e-6)
    assert row["closed_at"] is not None
    assert orch.metrics.closes_ok == 1


# ---------- Test 5: min edge filter → no opens, no perp call ----------

@pytest.mark.asyncio
async def test_min_edge_filter_blocks_open(isolated_db):
    _ensure_schema(isolated_db)
    # threshold ABSURDO (99pp) — ninguna decision realista alcanza.
    cfg = HedgeConfig(enabled=True, min_edge=0.99, bet_usdc=10.0)

    poly = _make_poly_opener(pid=1)
    perp = _make_perp_executor(ok=True)
    rb = _make_rollback_tracker()

    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp, poly_rollback=rb,
    )
    # Movimiento mínimo (0.05%) sobre 60s → edge ~ small, mid_up=0.50.
    decision = _make_decision(spot_move_pct=0.05, mid_up=0.50, mid_for_side=0.50)
    record, err = await orch.evaluate_and_open(**decision)

    assert record is None
    assert err is not None and ("low_edge" in err or "edge_below_min" in err)
    # CRÍTICO: ninguna pierna se llama si el edge no pasa.
    assert len(poly.calls) == 0
    assert len(perp.calls) == 0
    assert len(rb.calls) == 0
    assert orch.metrics.skipped_low_edge == 1
    assert orch.metrics.opens_ok == 0


# ---------- Test 6: funding rate gate ----------

@pytest.mark.asyncio
async def test_funding_rate_gate_blocks_open(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(
        enabled=True, min_edge=0.05, bet_usdc=10.0, max_funding_rate=0.0005,
    )
    poly = _make_poly_opener(pid=1)
    perp = _make_perp_executor(ok=True)
    orch = CryptoArbHedge(config=cfg, poly_opener=poly, perp_executor=perp)

    decision = _make_decision(funding_rate=0.001)  # > 0.0005 cap
    record, err = await orch.evaluate_and_open(**decision)

    assert record is None
    assert err is not None and "funding_too_high" in err
    assert len(poly.calls) == 0
    assert len(perp.calls) == 0
    assert orch.metrics.skipped_funding_rate == 1


# ---------- Test 7: margin gate ----------

@pytest.mark.asyncio
async def test_margin_gate_blocks_open(isolated_db):
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(
        enabled=True, min_edge=0.05, bet_usdc=10.0, leverage=2,
    )
    poly = _make_poly_opener(pid=1)
    perp = _make_perp_executor(ok=True)
    orch = CryptoArbHedge(config=cfg, poly_opener=poly, perp_executor=perp)

    # required_margin = 10/2 = 5. Pasamos margin_available=2 → abort.
    decision = _make_decision(margin_available=2.0)
    record, err = await orch.evaluate_and_open(**decision)

    assert record is None
    assert err is not None and "margin_insufficient" in err
    assert len(poly.calls) == 0
    assert len(perp.calls) == 0
    assert orch.metrics.skipped_margin == 1
