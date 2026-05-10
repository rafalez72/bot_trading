"""Tests para src/copybot/market_maker.py.

Cobertura:
1. Spread calc correcto (mid 0.50 spread 300bps → bid 0.485 ask 0.515).
2. Mid movement >50bps → cancela orders previas y repostea.
3. Max concurrent pairs respetado (cap=2 con 5 candidatos input).
4. Fill detection → record en mm_orders status='filled'.
5. Bucket close → settle de fills residuales con PnL correcto.

Mockeamos todos los CLOB calls (place_limit_order/cancel_order/get_fills_since)
para no tocar la red ni el SDK py-clob-client.
"""
from __future__ import annotations

from src.copybot.market_maker import (
    LimitOrderResult,
    MarketMaker,
    MarketMakerConfig,
    compute_spread_prices,
    should_recalc,
)


# --------------------------------------------------------------------------- #
# 1. Spread math
# --------------------------------------------------------------------------- #


def test_compute_spread_prices_typical():
    """mid=0.50 spread_bps=300 → half=150bps=0.015 → bid=0.485 ask=0.515."""
    bid, ask = compute_spread_prices(0.50, 300)
    assert abs(bid - 0.485) < 1e-9, f"bid={bid}"
    assert abs(ask - 0.515) < 1e-9, f"ask={ask}"
    # Sanity: bid < mid < ask
    assert bid < 0.50 < ask


def test_compute_spread_prices_clamps_extremes():
    """Si mid muy cerca de 0 o 1, el bid/ask debe clamp a [0.01, 0.99]."""
    bid, _ = compute_spread_prices(0.005, 300)
    assert bid >= 0.01
    _, ask = compute_spread_prices(0.995, 300)
    assert ask <= 0.99


# --------------------------------------------------------------------------- #
# 2. Mid movement → re-quote
# --------------------------------------------------------------------------- #


def test_mid_move_50bps_triggers_recalc(isolated_db):
    """Si el mid se mueve más de 50bps (5 décimos de %) desde la última
    cotización, _reconcile_pair debe cancelar las órdenes previas y postear
    nuevas al precio actualizado.

    Caso: last_mid=0.500, current_mid=0.504 → delta=80bps > 50bps → recalc.
    """
    placed: list[dict] = []
    cancelled: list[str] = []

    def fake_place(*, token_id, side, price, size_usdc):
        placed.append({"token_id": token_id, "side": side, "price": price})
        return LimitOrderResult(ok=True, order_id=f"FAKE-{side}-{price}")

    def fake_cancel(order_id):
        cancelled.append(order_id)
        return True

    cfg = MarketMakerConfig(
        enabled=True, spread_bps=300, bet_per_side_usdc=2.0,
        max_concurrent_pairs=5,
    )
    mm = MarketMaker(config=cfg, place_fn=fake_place, cancel_fn=fake_cancel)

    candidate = {
        "condition_id": "0xCID1", "token_id_yes": "0xTOK1", "mid": 0.500,
    }
    # Primer reconcile: no hay last_mid → primer post.
    mm._reconcile_pair(candidate)
    assert len(placed) == 2  # bid + ask
    assert cancelled == []  # nothing to cancel

    # Sanity: should_recalc helper también True para mid grande.
    assert should_recalc(last_quoted_mid=0.500, current_mid=0.504, threshold_bps=50)
    # Y False para mid pequeño (<=50bps).
    assert not should_recalc(last_quoted_mid=0.500, current_mid=0.502, threshold_bps=50)

    # Segundo reconcile: mid movió a 0.504 → 80bps → debe cancelar 2 (bid+ask)
    # y postear 2 nuevos.
    candidate["mid"] = 0.504
    mm._reconcile_pair(candidate)
    assert len(cancelled) == 2, f"esperado 2 cancels, got {cancelled}"
    assert len(placed) == 4, f"esperado 4 places (2 + 2), got {len(placed)}"

    # Tercer reconcile: mid movió poquito (0.5045) → < 50bps → NO recalc.
    candidate["mid"] = 0.5045
    mm._reconcile_pair(candidate)
    assert len(cancelled) == 2, "no debería cancelar de nuevo"
    assert len(placed) == 4, "no debería postear de nuevo"


# --------------------------------------------------------------------------- #
# 3. Max concurrent pairs cap
# --------------------------------------------------------------------------- #


def test_max_concurrent_pairs_respected(isolated_db):
    """Con cap=2 y 5 candidatos vacíos, sólo 2 deben terminar con orders."""
    placed: list[dict] = []

    def fake_place(*, token_id, side, price, size_usdc):
        placed.append({"token_id": token_id, "side": side})
        return LimitOrderResult(ok=True, order_id=f"FAKE-{token_id}-{side}")

    def fake_cancel(order_id):
        return True

    cfg = MarketMakerConfig(
        enabled=True, spread_bps=300, bet_per_side_usdc=2.0,
        max_concurrent_pairs=2,
    )

    candidates = [
        {"condition_id": f"0xCID{i}", "token_id_yes": f"0xTOK{i}", "mid": 0.50}
        for i in range(5)
    ]

    mm = MarketMaker(
        config=cfg,
        list_candidates_fn=lambda: candidates,
        place_fn=fake_place, cancel_fn=fake_cancel,
    )

    # Aplicar el cap manualmente (lo que hace _tick).
    capped = mm._apply_concurrency_cap(candidates)
    assert len(capped) == 2, f"esperado 2 candidates after cap, got {len(capped)}"

    # Reconciliar los 2 capped → cada uno postea bid+ask = 4 orders total.
    for c in capped:
        mm._reconcile_pair(c)
    assert len(placed) == 4, f"esperado 4 (2 cids x 2 sides), got {len(placed)}"

    # Verificar que sólo 2 cids distintos en self._pairs.
    assert len(mm._pairs) == 2, f"_pairs debería tener 2, tiene {len(mm._pairs)}"


# --------------------------------------------------------------------------- #
# 4. Fill detection → record paper_trade equivalente
# --------------------------------------------------------------------------- #


def test_fill_detection_records_filled(isolated_db):
    """Cuando get_fills_since devuelve un fill, _handle_fills debe marcar
    la mm_orders correspondiente como status='filled' con fill_price+filled_at.
    """
    def fake_place(*, token_id, side, price, size_usdc):
        return LimitOrderResult(ok=True, order_id=f"REAL-{side}-{int(price*1000)}")

    cfg = MarketMakerConfig(
        enabled=True, spread_bps=300, bet_per_side_usdc=2.0,
        max_concurrent_pairs=5,
    )

    # Mock que devuelve un fill SOLO la primera vez que se llama.
    fills_calls = {"n": 0}

    def fake_fills(since_ts):
        fills_calls["n"] += 1
        if fills_calls["n"] == 1:
            return [{
                "order_id": "REAL-BUY-485",
                "fill_price": 0.485,
                "filled_at": 1_700_000_000,
            }]
        return []

    mm = MarketMaker(
        config=cfg,
        place_fn=fake_place,
        cancel_fn=lambda oid: True,
        fills_fn=fake_fills,
    )

    # Postear bid+ask para tener orders abiertas en DB.
    mm._reconcile_pair({
        "condition_id": "0xFILLCID", "token_id_yes": "0xTOK", "mid": 0.50,
    })

    # Detectar fill.
    mm._handle_fills()

    # Verificar en DB.
    from src.db.schema import db
    with db() as conn:
        cur = conn.execute(
            "SELECT side, price, status, fill_price, filled_at "
            "FROM mm_orders WHERE order_id = ?",
            ("REAL-BUY-485",),
        )
        row = cur.fetchone()
        assert row is not None, "mm_orders no tiene la BUY row"
        assert row["status"] == "filled", f"status={row['status']}"
        assert abs(row["fill_price"] - 0.485) < 1e-9
        assert row["filled_at"] == 1_700_000_000

        # La SELL no se filleó → debe seguir 'open'.
        cur2 = conn.execute(
            "SELECT status FROM mm_orders "
            "WHERE condition_id = ? AND side = 'SELL'",
            ("0xFILLCID",),
        )
        sell_row = cur2.fetchone()
        assert sell_row["status"] == "open"


# --------------------------------------------------------------------------- #
# 5. Bucket close → settle PnL
# --------------------------------------------------------------------------- #


def test_bucket_close_settles_residual(isolated_db):
    """Si el bucket cierra (resuelve YES=1.0) y tenemos un BUY filled a 0.485,
    PnL = (1.0 - 0.485) * shares ≈ +0.515 * shares.

    shares = size_usdc / fill_price = 2.0 / 0.485 ≈ 4.124
    PnL ≈ 0.515 * 4.124 ≈ 2.124 USDC.
    """
    cfg = MarketMakerConfig(
        enabled=True, spread_bps=300, bet_per_side_usdc=2.0,
        max_concurrent_pairs=5,
    )

    placed_results = iter([
        LimitOrderResult(ok=True, order_id="SETTLE-BUY"),
        LimitOrderResult(ok=True, order_id="SETTLE-SELL"),
    ])

    def fake_place(*, token_id, side, price, size_usdc):
        return next(placed_results)

    mm = MarketMaker(
        config=cfg,
        place_fn=fake_place,
        cancel_fn=lambda oid: True,
        fills_fn=lambda ts: [{
            "order_id": "SETTLE-BUY",
            "fill_price": 0.485,
            "filled_at": 1_700_000_000,
        }],
    )

    cid = "0xBUCKET1"
    mm._reconcile_pair({
        "condition_id": cid, "token_id_yes": "0xTOK", "mid": 0.50,
    })
    mm._handle_fills()

    # Settle YES=1.0.
    result = mm.settle_bucket(cid, 1.0)
    assert result["n_settled"] == 1, f"n_settled={result['n_settled']}"

    expected_pnl = (1.0 - 0.485) * (2.0 / 0.485)
    assert abs(result["pnl_usdc_total"] - expected_pnl) < 1e-6, (
        f"pnl={result['pnl_usdc_total']} expected={expected_pnl}"
    )

    # Verificar que el state se limpió.
    assert cid not in mm._pairs

    # Verificar la fila en DB.
    from src.db.schema import db
    with db() as conn:
        cur = conn.execute(
            "SELECT status, pnl_usdc FROM mm_orders WHERE order_id = ?",
            ("SETTLE-BUY",),
        )
        row = cur.fetchone()
        assert row["status"] == "settled"
        assert abs(row["pnl_usdc"] - expected_pnl) < 1e-6
