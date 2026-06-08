"""Brick B — sesgo de selección por horizonte (trades_per_day).

Los scalpers (muchísimos trades/día) operan mercados ultra-cortos que el filtro
de ejecución (market_too_short) rechaza siempre → 0 copias. _trades_per_day es
el proxy que usa select_traders para excluirlos.
"""
from __future__ import annotations

from src.config import MAX_TRADES_PER_DAY
from src.copybot.selector import _trades_per_day


def test_scalper_high_tpd_excluded():
    # 11514 trades en 180 días ≈ 64/día → scalper, por encima del cap.
    assert _trades_per_day({"total_trades": 11514, "active_days": 180}) > MAX_TRADES_PER_DAY


def test_position_trader_low_tpd_kept():
    # 300 trades en 180 días ≈ 1.7/día → horizonte largo, pasa.
    assert _trades_per_day({"total_trades": 300, "active_days": 180}) <= MAX_TRADES_PER_DAY


def test_precomputed_tpd_used():
    assert _trades_per_day({"trades_per_day": 5.0}) == 5.0


def test_missing_days_defaults_safe():
    # Sin days/total → no explota, devuelve 0.
    assert _trades_per_day({}) == 0.0
