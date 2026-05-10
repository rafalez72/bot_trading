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
    _hedge_evaluate_setup,
    init_table,
    recover_orphan_perps,
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


# ---------- Test 8 (bug #7): detector encuentra edge y llama _open_atomic ----------

class _FakeWS:
    """Mock minimal de BinanceTickerWS — solo expone get_price."""

    def __init__(self, prices: dict[str, tuple[float, int]]):
        self._prices = prices

    def get_price(self, symbol: str):
        return self._prices.get(symbol)


@pytest.mark.asyncio
async def test_hedge_detector_finds_edge(isolated_db, monkeypatch):
    """Feed mock Binance spot + market list → verifica que evaluate_and_open
    se llama con decision correcta (detector wired).
    """
    _ensure_schema(isolated_db)
    import time as _t
    now = int(_t.time())
    # Bucket cierra en 120s (dentro de pre_close_window=180s).
    end_ts = now + 120
    bucket_start = end_ts - 300

    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0, leverage=2)

    market = {
        "_end_ts": end_ts,
        "_slug_prefix": "btc-updown-5m-",
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xCID_BTC",
        # mid_up=0.40 — spot subió +1% → edge >> 0.05
        "outcomePrices": '["0.40", "0.60"]',
    }

    # Spot ahora = 101, hace 5min (bucket_start) era 100 → move = +1%.
    # History tiene el sample bucket_start con timestamp en ms.
    ws = _FakeWS({"BTCUSDT": (101.0, now * 1000)})
    spot_history = {"BTCUSDT": [(bucket_start * 1000, 100.0)]}

    setup = _hedge_evaluate_setup(market, cfg, spot_history, ws)
    assert setup is not None, "detector debe encontrar setup con edge"
    assert setup["symbol"] == "BTCUSDT"
    assert setup["outcome"] == "Up"   # +1% move → buy UP
    assert setup["outcome_index"] == 0
    assert setup["spot_now"] == pytest.approx(101.0)
    assert setup["spot_move_pct"] == pytest.approx(1.0, abs=1e-6)
    assert setup["mid_up"] == pytest.approx(0.40)
    assert setup["mid_for_side"] == pytest.approx(0.40)
    assert setup["bucket_slug"] == f"btc-updown-5m-{end_ts}"

    # E2E: pasamos el setup a evaluate_and_open con mocks y verificamos que
    # las dos piernas se llaman atómicamente.
    poly = _make_poly_opener(pid=77)
    perp = _make_perp_executor(ok=True, avg_price=101.0, qty=0.099)
    rb = _make_rollback_tracker()
    orch = CryptoArbHedge(
        config=cfg, poly_opener=poly, perp_executor=perp, poly_rollback=rb,
    )
    record, err = await orch.evaluate_and_open(**setup)
    assert err is None, f"Esperaba éxito, got err={err}"
    assert record is not None and record["poly_trade_id"] == 77
    assert len(perp.calls) == 1


@pytest.mark.asyncio
async def test_detector_skips_below_min_edge(isolated_db):
    """Spot apenas se movió → edge < min_edge → detector devuelve None."""
    _ensure_schema(isolated_db)
    import time as _t
    now = int(_t.time())
    end_ts = now + 120
    bucket_start = end_ts - 300

    cfg = HedgeConfig(enabled=True, min_edge=0.20, bet_usdc=10.0)
    market = {
        "_end_ts": end_ts,
        "_slug_prefix": "btc-updown-5m-",
        "slug": f"btc-updown-5m-{end_ts}",
        "conditionId": "0xCID_BTC",
        "outcomePrices": '["0.50", "0.50"]',  # mid neutral
    }
    # Movimiento mínimo (+0.02%) → p_up ~ 0.5 → edge ~ 0
    ws = _FakeWS({"BTCUSDT": (100.02, now * 1000)})
    spot_history = {"BTCUSDT": [(bucket_start * 1000, 100.0)]}

    setup = _hedge_evaluate_setup(market, cfg, spot_history, ws)
    assert setup is None


# ---------- Test 9 (bug #8): recovery cierra orphan perps ----------

class _FakePerpClient:
    """Mock BinancePerpClient como async context manager."""

    def __init__(self, qty: float):
        self.qty = qty
        self.close_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_position(self, symbol: str, *, position_side: str = "SHORT"):
        return {
            "qty": self.qty, "entry_price": 100.0, "unrealized_pnl": 0.0,
            "position_side": position_side, "raw": None,
        }

    async def close_position(self, *, symbol: str, position_side: str, quantity: float):
        self.close_calls.append({
            "symbol": symbol, "position_side": position_side, "quantity": quantity,
        })
        return {"status": "FILLED", "executedQty": str(quantity)}


@pytest.mark.asyncio
async def test_recover_orphan_perps(isolated_db):
    """DB con leg_failed + perp open mock → verificar close llamado + status updated."""
    _ensure_schema(isolated_db)
    # Sembramos un row leg_failed con perp_order_id seteado.
    from src.db.schema import tx, db
    import time as _t
    now = int(_t.time())
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO hedge_trades
                (bucket_slug, symbol, side, poly_trade_id, poly_trade_table,
                 perp_order_id, perp_qty, perp_entry_price, spot_at_entry,
                 edge_at_open, status, opened_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "btc-updown-5m-1700000000", "BTCUSDT", "Up", 555, "paper_trades",
                "PERP-ORPHAN-1", 0.1, 100.0, 100.0, 0.10,
                "leg_failed", now - 30, "{}",
            ),
        )

    fake_client = _FakePerpClient(qty=-0.1)  # short position abierta
    notif_calls: list[str] = []

    def _notif(msg: str):
        notif_calls.append(msg)

    n = await recover_orphan_perps(
        perp_client_factory=lambda: fake_client,
        notifier=_notif,
    )
    assert n == 1
    # Se llamó close_position con qty=0.1.
    assert len(fake_client.close_calls) == 1
    assert fake_client.close_calls[0]["symbol"] == "BTCUSDT"
    assert fake_client.close_calls[0]["position_side"] == "SHORT"
    assert fake_client.close_calls[0]["quantity"] == pytest.approx(0.1)
    # Status updated.
    with db() as conn:
        row = conn.execute(
            "SELECT status, closed_at FROM hedge_trades WHERE perp_order_id=?",
            ("PERP-ORPHAN-1",),
        ).fetchone()
    assert row["status"] == "recovered"
    assert row["closed_at"] is not None
    # Notif Telegram disparada.
    assert len(notif_calls) == 1
    assert "HEDGE RECOVERY" in notif_calls[0]


@pytest.mark.asyncio
async def test_recover_orphan_perps_no_position_on_chain(isolated_db):
    """Si get_position devuelve qty=0 (ya cerrada manual), no llama close,
    pero igual marca status='recovered' para no reintentarlo cada startup.
    """
    _ensure_schema(isolated_db)
    from src.db.schema import tx, db
    import time as _t
    now = int(_t.time())
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO hedge_trades
                (bucket_slug, symbol, side, poly_trade_id, poly_trade_table,
                 perp_order_id, perp_qty, perp_entry_price, spot_at_entry,
                 edge_at_open, status, opened_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "eth-updown-5m-1700000001", "ETHUSDT", "Up", 666, "paper_trades",
                "PERP-ORPHAN-2", 0.5, 3000.0, 3000.0, 0.08,
                "leg_failed", now - 60, "{}",
            ),
        )

    fake_client = _FakePerpClient(qty=0.0)  # ya no hay position en Binance
    n = await recover_orphan_perps(
        perp_client_factory=lambda: fake_client,
        notifier=lambda _: None,
    )
    assert n == 1
    # close_position NO se llama (no hay position abierta).
    assert len(fake_client.close_calls) == 0
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM hedge_trades WHERE perp_order_id=?",
            ("PERP-ORPHAN-2",),
        ).fetchone()
    assert row["status"] == "recovered"


# ---------- Test 10 (bug #9): persist setea poly_trade_table ----------

@pytest.mark.asyncio
async def test_persist_includes_poly_trade_table(isolated_db):
    """Verificar que el discriminador poly_trade_table se setea en hedge_trades
    según el modo (paper_trades vs live_trades) actual de tradebook.
    """
    _ensure_schema(isolated_db)
    cfg = HedgeConfig(enabled=True, min_edge=0.05, bet_usdc=10.0, leverage=2)

    poly = _make_poly_opener(pid=123)
    perp = _make_perp_executor(ok=True, avg_price=100.0, qty=0.1)
    orch = CryptoArbHedge(config=cfg, poly_opener=poly, perp_executor=perp)

    record, err = await orch.evaluate_and_open(**_make_decision(spot_now=100.0))
    assert err is None
    # El record en memoria también lo tiene.
    assert record.get("poly_trade_table") in ("paper_trades", "live_trades")

    # Y el row persistido en DB.
    from src.db.schema import db
    from src.copybot.tradebook import TABLE as TB_TABLE
    with db() as conn:
        row = conn.execute(
            "SELECT poly_trade_id, poly_trade_table FROM hedge_trades WHERE id=?",
            (record["id"],),
        ).fetchone()
    assert row["poly_trade_id"] == 123
    assert row["poly_trade_table"] == TB_TABLE  # consistente con runtime mode
