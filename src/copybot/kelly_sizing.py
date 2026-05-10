"""Kelly fraccional sizing.

Background (research Dic-2025/Ene-2026):
  - Full Kelly produce drawdowns de 50-80% en bots crypto/prediction-market reales
  - Profesionales usan 10-25% Kelly (typical 15%)
  - Edge mínimo: si bet_size < 0.5% bankroll → mejor no operar (ruido > señal)

Fórmula Kelly clásica:
    f* = (bp - q) / b
    donde b = avg_win / avg_loss
          p = win_rate
          q = 1 - p

Devuelve fracción del bankroll a apostar. Multiplicamos por kelly_fraction_pct
(default 0.15 = 15% Kelly) para ajustar al riesgo conservador.

Integración con bandit.recompute_sizings():
  - OFF por default (env KELLY_SIZING_ENABLED != 'true')
  - Si ON: por cada wallet, leer (wr, avg_win, avg_loss) últimos 30d y override sizing
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Defaults conservadores. KELLY_FRACTION_PCT = 0.15 (15% Kelly).
DEFAULT_KELLY_FRACTION_PCT = 0.15
# Edge mínimo: si la apuesta calculada queda por debajo de este % del bankroll,
# devolvemos 0 (ruido > señal según research).
MIN_EDGE_PCT_OF_BANKROLL = 0.005  # 0.5%


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Kelly criterion full: f = (bp - q) / b.

    Args:
        win_rate: probabilidad de ganar [0..1]
        avg_win:  ganancia promedio por trade ganador (USDC, > 0)
        avg_loss: pérdida promedio por trade perdedor (USDC, > 0, magnitud)

    Returns:
        Fracción Kelly [0..1]. Capada en 0 si no hay edge positivo.
    """
    if avg_loss <= 0 or win_rate <= 0:
        return 0.0
    if win_rate >= 1.0:
        # Edge perfecto teórico → cap a 1.0 (nunca apostar > 100% bankroll)
        return 1.0
    b = avg_win / avg_loss
    if b <= 0:
        return 0.0
    p = win_rate
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


def fractional_bet_size(
    *,
    bankroll_usdc: float,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    kelly_fraction_pct: float = DEFAULT_KELLY_FRACTION_PCT,
) -> float:
    """Tamaño de apuesta en USDC usando Kelly fraccional.

    Args:
        bankroll_usdc: capital disponible
        win_rate: prob histórica de ganar [0..1]
        avg_win: avg PnL de trades ganadores (USDC, > 0)
        avg_loss: avg pérdida (magnitud positiva, USDC)
        kelly_fraction_pct: % de Kelly a usar. Default 0.15 (15% Kelly conservador).

    Returns:
        Bet size en USDC. 0 si no hay edge o edge < 0.5% bankroll.
    """
    if bankroll_usdc <= 0:
        return 0.0
    full_kelly = kelly_fraction(win_rate, avg_win, avg_loss)
    bet_pct = full_kelly * kelly_fraction_pct
    bet = bankroll_usdc * bet_pct
    # Sanity check: edge muy chico → ruido. No operar.
    if bet < bankroll_usdc * MIN_EDGE_PCT_OF_BANKROLL:
        return 0.0
    return bet
