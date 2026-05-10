"""Tests para src.copybot.kelly_sizing.

Cubre:
  1. Kelly básico (caso típico positive edge)
  2. Edge case wr=0 (no win rate → 0)
  3. Edge case avg_loss=0 (división protegida → 0)
  4. No-op si edge < 0.5% bankroll (ruido > señal)
"""
from __future__ import annotations

import pytest

from src.copybot.kelly_sizing import (
    DEFAULT_KELLY_FRACTION_PCT,
    MIN_EDGE_PCT_OF_BANKROLL,
    fractional_bet_size,
    kelly_fraction,
)


def test_kelly_basic_positive_edge():
    """wr=0.6, avg_win=2, avg_loss=1 → b=2, p=0.6, q=0.4 → f=(1.2-0.4)/2=0.4."""
    f = kelly_fraction(win_rate=0.6, avg_win=2.0, avg_loss=1.0)
    assert f == pytest.approx(0.4, abs=1e-6)
    # Con 15% Kelly y bankroll 1000 → 0.4 * 0.15 * 1000 = 60 USDC
    bet = fractional_bet_size(
        bankroll_usdc=1000.0,
        win_rate=0.6,
        avg_win=2.0,
        avg_loss=1.0,
        kelly_fraction_pct=DEFAULT_KELLY_FRACTION_PCT,
    )
    assert bet == pytest.approx(60.0, abs=1e-6)


def test_kelly_zero_winrate_returns_zero():
    """wr=0 → no edge → 0 (early return guard)."""
    assert kelly_fraction(win_rate=0.0, avg_win=5.0, avg_loss=1.0) == 0.0
    bet = fractional_bet_size(
        bankroll_usdc=1000.0,
        win_rate=0.0,
        avg_win=5.0,
        avg_loss=1.0,
    )
    assert bet == 0.0


def test_kelly_zero_avg_loss_protected():
    """avg_loss=0 → no se puede calcular b → 0 (no division by zero)."""
    assert kelly_fraction(win_rate=0.7, avg_win=2.0, avg_loss=0.0) == 0.0
    # avg_loss negativo también protegido
    assert kelly_fraction(win_rate=0.7, avg_win=2.0, avg_loss=-1.0) == 0.0


def test_kelly_no_op_below_min_edge():
    """Si bet < 0.5% bankroll → devuelve 0 (ruido > señal).

    wr=0.51, avg_win=1, avg_loss=1 → b=1, f=(0.51-0.49)/1=0.02
    Con 15% Kelly: 0.02 * 0.15 = 0.003 = 0.3% bankroll → debajo del 0.5% threshold → 0.
    """
    bet = fractional_bet_size(
        bankroll_usdc=1000.0,
        win_rate=0.51,
        avg_win=1.0,
        avg_loss=1.0,
        kelly_fraction_pct=DEFAULT_KELLY_FRACTION_PCT,
    )
    assert bet == 0.0
    # Sanity: el threshold realmente es 0.5% bankroll
    assert MIN_EDGE_PCT_OF_BANKROLL == 0.005


def test_kelly_negative_edge_returns_zero():
    """wr*b < q → edge negativo → no apostar."""
    # wr=0.4, b=1 → f = (0.4 - 0.6)/1 = -0.2 → cap a 0
    assert kelly_fraction(win_rate=0.4, avg_win=1.0, avg_loss=1.0) == 0.0
