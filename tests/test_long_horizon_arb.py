"""Tests para src/copybot/long_horizon_arb.py.

Cubre 6 casos:
  1. Slug parser: extrae underlying btc/eth/sol de variantes válidas.
  2. Edge calc realista: spot move + horizon → edge_pp con sign correcto.
  3. Filtro min liquidez: rechaza markets <$30k.
  4. Limit order placement: cycle dispara executor con limit=mid actual.
  5. Settlement path: market resuelto / early exit / hold.
  6. Filtro horizonte: rechaza markets con end_date < +1 día.

Mockea CLOB / binance / gamma con callables inyectables. Usa fixture
``isolated_db`` para que los inserts a ``long_horizon_trades`` sean aislados.
"""
from __future__ import annotations

import json
import time

import pytest

from src.copybot.long_horizon_arb import (
    LongHorizonArb,
    LongHorizonConfig,
    estimate_edge_pp,
    evaluate_market,
    filter_market,
    init_table,
    parse_underlying,
    settlement_action,
)


# ---------- Helpers ----------

def _mk_market(
    *, slug: str, mid_up: float = 0.45, liquidity: float = 50_000.0,
    end_offset_s: int = 30 * 86400, condition_id: str = "0xCID",
) -> dict:
    """Construye un market dict mock con los campos que usa el bot."""
    end_ts = int(time.time()) + end_offset_s
    from datetime import datetime, timezone
    end_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "slug": slug,
        "conditionId": condition_id,
        "outcomePrices": json.dumps([str(mid_up), str(1.0 - mid_up)]),
        "liquidity": liquidity,
        "volume": 100_000.0,
        "endDate": end_iso,
        "closed": False,
    }


def _ensure_schema(isolated_db):  # noqa: ARG001 — fixture forces wiring
    init_table()


# ---------- Test 1: slug parser ----------

def test_parse_underlying_extracts_short_symbol():
    # Casos válidos
    assert parse_underlying("bitcoin-200k-by-end-2026") == "btc"
    assert parse_underlying("btc-100k-by-2026") == "btc"
    assert parse_underlying("ethereum-reaches-5k-this-month") == "eth"
    assert parse_underlying("eth-monthly-up-may") == "eth"
    assert parse_underlying("sol-300-by-2026-12-31") == "sol"
    assert parse_underlying("solana-monthly-2026") == "sol"
    assert parse_underlying("xrp-2-by-2027") == "xrp"
    assert parse_underlying("doge-1-by-end-2027") == "doge"

    # Casos rechazados
    assert parse_underlying("") is None
    assert parse_underlying(None) is None
    # Updown excluido (ese es del crypto_arb 5m)
    assert parse_underlying("btc-updown-5m-1700000000") is None
    assert parse_underlying("eth-updown-15m-1700000000") is None
    # Underlying no soportado
    assert parse_underlying("ada-100-by-2026") is None
    # Slug random sin formato long-horizon
    assert parse_underlying("trump-2024-election") is None
    assert parse_underlying("nba-finals-lakers") is None


# ---------- Test 2: edge calc realista ----------

def test_estimate_edge_pp_sign_and_magnitude():
    # Caso 1: spot subió +5%, mid_up=0.5 (neutral). En 30 días horizon,
    # attenuator=~0.7. impact_pp = min(5*2, 10)*0.7 = 7pp; priced_in=0.
    edge, side = estimate_edge_pp(
        spot_move_pct=5.0, mid_up=0.5, secs_to_end=30 * 86400, underlying="btc",
    )
    assert side == "Up"
    assert edge > 0
    # No exceder cap impact 10pp.
    assert edge <= 10.0 + 0.01

    # Caso 2: spot bajó -3%, mid_up=0.5 → side=Down, edge positivo (3*2*att).
    edge2, side2 = estimate_edge_pp(
        spot_move_pct=-3.0, mid_up=0.5, secs_to_end=30 * 86400, underlying="eth",
    )
    assert side2 == "Down"
    assert edge2 > 0

    # Caso 3: priced-in alto. mid_up=0.85 (Up ya MUY priced) y spot +2% →
    # edge se reduce sustancialmente.
    edge3, _ = estimate_edge_pp(
        spot_move_pct=2.0, mid_up=0.85, secs_to_end=7 * 86400, underlying="btc",
    )
    edge_baseline, _ = estimate_edge_pp(
        spot_move_pct=2.0, mid_up=0.50, secs_to_end=7 * 86400, underlying="btc",
    )
    # priced-in baja edge respecto al baseline 0.5
    assert edge3 < edge_baseline

    # Caso 4: horizon largo (90+ días) atenúa el edge respecto a corto.
    edge_short, _ = estimate_edge_pp(
        spot_move_pct=4.0, mid_up=0.5, secs_to_end=5 * 86400, underlying="btc",
    )
    edge_long, _ = estimate_edge_pp(
        spot_move_pct=4.0, mid_up=0.5, secs_to_end=120 * 86400, underlying="btc",
    )
    assert edge_short > edge_long


# ---------- Test 3: filtro min liquidez ----------

def test_filter_market_min_liquidity():
    cfg_min_liq = 30_000.0
    cfg_min_horizon = 86400  # 1 día
    now = int(time.time())

    # Liq alta — pasa
    m_ok = _mk_market(slug="bitcoin-200k-by-2026", liquidity=50_000.0,
                      end_offset_s=30 * 86400)
    assert filter_market(
        m_ok, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon, now_ts=now,
    ) is True

    # Liq baja — rechaza
    m_low = _mk_market(slug="bitcoin-200k-by-2026", liquidity=5_000.0,
                       end_offset_s=30 * 86400)
    assert filter_market(
        m_low, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon, now_ts=now,
    ) is False

    # Slug que no parsea — rechaza
    m_bad_slug = _mk_market(slug="trump-vs-biden-2026", liquidity=100_000.0,
                            end_offset_s=30 * 86400)
    assert filter_market(
        m_bad_slug, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon,
        now_ts=now,
    ) is False

    # Updown 5m con liq alta (no debería ser permitido — universo crypto_arb)
    m_updown = _mk_market(slug="btc-updown-5m-1700000000",
                          liquidity=100_000.0, end_offset_s=30 * 86400)
    assert filter_market(
        m_updown, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon,
        now_ts=now,
    ) is False

    # Closed=True — rechaza
    m_closed = _mk_market(slug="bitcoin-200k-by-2026", liquidity=50_000.0,
                          end_offset_s=30 * 86400)
    m_closed["closed"] = True
    assert filter_market(
        m_closed, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon,
        now_ts=now,
    ) is False


# ---------- Test 4: limit order placement vía cycle ----------

@pytest.mark.asyncio
async def test_cycle_places_limit_order_at_current_mid(isolated_db):
    _ensure_schema(isolated_db)
    cfg = LongHorizonConfig(
        enabled=True, min_liq_usdc=30_000.0, min_edge_pct=2.0,
        bet_usdc=10.0, max_mid_target=0.85,
    )
    market = _mk_market(
        slug="bitcoin-200k-by-end-2026", mid_up=0.40,
        liquidity=80_000.0, end_offset_s=180 * 86400,
    )

    captured: dict = {}

    async def _capturing_executor(signal: dict) -> dict:
        captured.update(signal)
        return {
            "ok": True, "order_id": "MOCK-LH-1", "filled": True,
            "fill_price": signal["limit_price"], "error": None,
            "raw": {"mock": True},
        }

    async def _markets_provider() -> list[dict]:
        return [market]

    # Spot provider: now-1h price = 100, now = 105 → +5% move (BTC).
    def _spot(symbol: str, ts: int) -> float | None:
        if symbol != "BTCUSDT":
            return None
        # Distinguimos por delta vs now: si ts < now-100s → "1h ago", else "now".
        now = int(time.time())
        if (now - ts) >= 1800:  # más de 30min atrás
            return 100.0
        return 105.0

    arb = LongHorizonArb(
        cfg, markets_provider=_markets_provider,
        spot_provider=_spot, order_executor=_capturing_executor,
    )
    results = await arb.cycle()

    assert len(results) == 1, f"Esperaba 1 trade, hubo {len(results)}: {results}"
    assert captured.get("side") == "Up"
    # limit_price posteado al mid actual del lado a comprar (0.40 para Up).
    assert captured.get("limit_price") == pytest.approx(0.40, abs=1e-9)
    assert captured.get("entry_mid") == pytest.approx(0.40, abs=1e-9)
    assert captured.get("size_usdc") == pytest.approx(10.0)
    assert captured.get("underlying") == "btc"
    assert captured.get("ttl_s") > 0
    assert arb.metrics.orders_filled == 1

    # Verificar persist en DB.
    from src.db.schema import db
    with db() as conn:
        rows = conn.execute(
            "SELECT market_slug, underlying, side, entry_mid, fill_price, "
            "status, bet_usdc, edge_estimate_pct, end_date_market "
            "FROM long_horizon_trades ORDER BY id DESC LIMIT 1"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["market_slug"] == "bitcoin-200k-by-end-2026"
    assert r["underlying"] == "btc"
    assert r["side"] == "Up"
    assert r["status"] == "filled"
    assert r["fill_price"] == pytest.approx(0.40, abs=1e-9)
    assert r["bet_usdc"] == pytest.approx(10.0)
    assert r["edge_estimate_pct"] >= cfg.min_edge_pct


# ---------- Test 5: settlement path ----------

def test_settlement_action_decides_exits():
    # Hold: no resolved, no big gain.
    out = settlement_action(
        current_mid=0.45, entry_mid=0.40, market_closed=False,
        payout=None, early_exit_gain_pct=0.50,
    )
    assert out["action"] == "hold"
    assert out["pnl_usdc_factor"] is None

    # Early exit: gain >= 50% sobre entry.
    out2 = settlement_action(
        current_mid=0.62, entry_mid=0.40, market_closed=False,
        payout=None, early_exit_gain_pct=0.50,
    )
    assert out2["action"] == "early_exit"
    # 0.62/0.40 - 1 = 0.55 → +55% → factor ~0.55
    assert out2["pnl_usdc_factor"] == pytest.approx(0.55, abs=0.01)

    # Settle win: market closed, payout=1.
    out3 = settlement_action(
        current_mid=0.5, entry_mid=0.40, market_closed=True,
        payout=1.0, early_exit_gain_pct=0.50,
    )
    assert out3["action"] == "settle"
    # (1.0 - 0.40)/0.40 = 1.5 → +150%
    assert out3["pnl_usdc_factor"] == pytest.approx(1.5, abs=0.01)

    # Settle loss: market closed, payout=0.
    out4 = settlement_action(
        current_mid=0.05, entry_mid=0.40, market_closed=True,
        payout=0.0, early_exit_gain_pct=0.50,
    )
    assert out4["action"] == "settle"
    # (0 - 0.40)/0.40 = -1 → -100%
    assert out4["pnl_usdc_factor"] == pytest.approx(-1.0, abs=0.001)


# ---------- Test 6: filtro horizonte mínimo ----------

def test_filter_market_min_horizon():
    cfg_min_liq = 30_000.0
    cfg_min_horizon = 86400  # 1 día
    now = int(time.time())

    # End_date a 12h: rechaza (menos del mínimo 1 día).
    m_short = _mk_market(slug="bitcoin-200k-by-2026", liquidity=50_000.0,
                         end_offset_s=12 * 3600)
    assert filter_market(
        m_short, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon,
        now_ts=now,
    ) is False

    # End_date a 2 días: pasa.
    m_ok = _mk_market(slug="bitcoin-200k-by-2026", liquidity=50_000.0,
                      end_offset_s=2 * 86400)
    assert filter_market(
        m_ok, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon, now_ts=now,
    ) is True

    # End_date pasado: rechaza.
    m_past = _mk_market(slug="bitcoin-200k-by-2026", liquidity=50_000.0,
                        end_offset_s=-3600)
    assert filter_market(
        m_past, min_liq=cfg_min_liq, min_horizon_s=cfg_min_horizon, now_ts=now,
    ) is False


# ---------- Test 7: bonus — overbought / low edge guards ----------

@pytest.mark.asyncio
async def test_low_edge_skipped_no_order(isolated_db):
    _ensure_schema(isolated_db)
    cfg = LongHorizonConfig(
        enabled=True, min_liq_usdc=30_000.0, min_edge_pct=20.0,  # cap alto
        bet_usdc=10.0,
    )
    market = _mk_market(
        slug="bitcoin-200k-by-end-2026", mid_up=0.50, liquidity=80_000.0,
        end_offset_s=180 * 86400,
    )

    async def _markets_provider() -> list[dict]:
        return [market]

    def _spot(symbol: str, ts: int) -> float | None:
        if symbol != "BTCUSDT":
            return None
        now = int(time.time())
        if (now - ts) >= 1800:
            return 100.0
        return 100.5  # +0.5% move → impact ~1pp << 20pp threshold

    called: list[dict] = []

    async def _exec(signal: dict) -> dict:
        called.append(signal)
        return {"ok": True, "order_id": "X", "filled": True,
                "fill_price": 0.5, "error": None, "raw": None}

    arb = LongHorizonArb(
        cfg, markets_provider=_markets_provider,
        spot_provider=_spot, order_executor=_exec,
    )
    results = await arb.cycle()
    assert results == []
    assert called == []
    assert arb.metrics.skipped_low_edge >= 1


# ---------- Test 8: evaluate_market sin spot history ----------

def test_evaluate_market_skips_when_no_spot():
    cfg = LongHorizonConfig(min_liq_usdc=30_000.0, min_edge_pct=2.0)
    market = _mk_market(
        slug="bitcoin-200k-by-2026", mid_up=0.40, liquidity=50_000.0,
        end_offset_s=30 * 86400,
    )
    now = int(time.time())
    # Sin spot_now / spot_lookback → None.
    assert evaluate_market(
        market, config=cfg, spot_now=None, spot_lookback=100.0, now_ts=now,
    ) is None
    assert evaluate_market(
        market, config=cfg, spot_now=100.0, spot_lookback=None, now_ts=now,
    ) is None
    # spot_lookback=0 evita división por 0.
    assert evaluate_market(
        market, config=cfg, spot_now=100.0, spot_lookback=0.0, now_ts=now,
    ) is None
