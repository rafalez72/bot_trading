"""Tests for src/copybot/scoring.py — recent_weighted_score.

Función pura: no abre DB ni necesita fixture isolated_db. Cubrimos:
  - Caso base sin data reciente (peso 0 → score histórico intacto).
  - Penalty por loss grande reciente.
  - Boost por win reciente, con cap 1.5x.
  - Ramp de weight según cantidad de trades.
"""
from __future__ import annotations

import pytest

from src.copybot.scoring import recent_weighted_score


def test_no_recent_data_returns_historical():
    """Sin trades recientes → weight=0 → score histórico tal cual."""
    out = recent_weighted_score(historical_score=0.7, recent_pnl_7d=0.0, recent_trades_7d=0)
    assert out == pytest.approx(0.7)


def test_zero_historical_stays_zero():
    """historical=0 con cualquier perf reciente sigue 0 (multiplicación)."""
    assert recent_weighted_score(0.0, 50.0, 20) == 0.0
    assert recent_weighted_score(0.0, -50.0, 20) == 0.0


def test_negative_historical_clamps_to_zero():
    """Defensa: historical negativo se trata como 0."""
    out = recent_weighted_score(historical_score=-0.5, recent_pnl_7d=10.0, recent_trades_7d=5)
    assert out == 0.0


def test_big_recent_loss_penalizes_score():
    """Pérdida reciente grande con muestra grande → score baja."""
    base = 0.8
    # 10+ trades → peso pleno; pnl -$25 → factor = 1 + (-25/50) = 0.5
    # final = 0.8 * 0 + 0.8 * 0.5 * 1 = 0.4
    out = recent_weighted_score(base, recent_pnl_7d=-25.0, recent_trades_7d=12)
    assert out == pytest.approx(0.4, abs=1e-6)


def test_catastrophic_recent_loss_clamps_to_zero():
    """Pérdida tan grande que el factor sería negativo → final clamp a 0."""
    # pnl -$100 → factor = 1 + (-100/50) = -1.0; con peso 1 final = -0.8 → clamp 0
    out = recent_weighted_score(0.8, recent_pnl_7d=-100.0, recent_trades_7d=15)
    assert out == 0.0


def test_big_win_boosts_score_with_cap():
    """Win grande con muestra grande → boost, capeado en 1.5x."""
    base = 0.6
    # pnl $30 → factor = 1 + min(30/50, 0.5) = 1 + 0.5 = 1.5 (saturado)
    # weight 1 → final = 0.6 * 1.5 = 0.9
    out = recent_weighted_score(base, recent_pnl_7d=30.0, recent_trades_7d=20)
    assert out == pytest.approx(0.9, abs=1e-6)

    # pnl $1000 también satura en 1.5x
    out_huge = recent_weighted_score(base, recent_pnl_7d=1000.0, recent_trades_7d=20)
    assert out_huge == pytest.approx(0.9, abs=1e-6)


def test_weight_ramps_with_recent_trade_count():
    """Con la misma performance, más trades → más peso del factor reciente."""
    base = 1.0
    pnl = -25.0  # factor recent = 0.5

    # 0 trades → peso 0 → score histórico
    s0 = recent_weighted_score(base, pnl, 0)
    # 5 trades → peso 0.5 → 1.0 * 0.5 + 1.0 * 0.5 * 0.5 = 0.5 + 0.25 = 0.75
    s5 = recent_weighted_score(base, pnl, 5)
    # 10 trades → peso 1.0 → 1.0 * 0 + 1.0 * 0.5 * 1 = 0.5
    s10 = recent_weighted_score(base, pnl, 10)
    # 50 trades → peso saturado 1.0 → mismo que s10
    s50 = recent_weighted_score(base, pnl, 50)

    assert s0 == pytest.approx(1.0)
    assert s5 == pytest.approx(0.75, abs=1e-6)
    assert s10 == pytest.approx(0.5, abs=1e-6)
    assert s50 == pytest.approx(s10, abs=1e-6)
    # Monotonicidad: con loss reciente, más weight = score más bajo
    assert s0 > s5 > s10
