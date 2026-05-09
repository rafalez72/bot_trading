"""Scoring helpers para tunear/blendear el ranking de wallets.

Estos helpers son **puros** — no leen DB ni envían side-effects. Pensados
para usarse desde el selector u otras heurísticas a futuro.

Feature I (2026-05-09): ``recent_weighted_score`` blendea el score
histórico con la performance de los últimos 7 días. Idea: una wallet
con score histórico alto pero PnL 7d feo merece menos peso que su
historial sugiere; al revés, una en racha caliente merece un boost
acotado. Se aplica un peso proporcional al tamaño de la muestra reciente
para que muestras chicas no doblen el ranking.
"""
from __future__ import annotations


def recent_weighted_score(
    historical_score: float,
    recent_pnl_7d: float,
    recent_trades_7d: int,
) -> float:
    """Blendea ``historical_score`` con la performance de los últimos 7 días.

    Args:
        historical_score: score histórico del wallet (>= 0 esperado).
        recent_pnl_7d: PnL realizado USDC en los últimos 7 días.
        recent_trades_7d: cantidad de trades cerrados en los últimos 7 días.

    Returns:
        Score blendeado, siempre >= 0.

    Reglas:
        - ``recent_factor``:
            * si ``recent_pnl_7d < 0``  → ``1 + recent_pnl_7d / 50`` (penaliza,
              sin floor explícito; el clamp final corta a 0).
            * si ``recent_pnl_7d >= 0`` → ``1 + min(recent_pnl_7d / 50, 0.5)``,
              boost cap 1.5x (un win de $25+ ya satura).
        - ``weight_recent`` = ``min(recent_trades_7d / 10, 1.0)`` — full
          weight con 10+ trades, lineal abajo.
        - final = ``historical * (1 - weight_recent) + historical * recent_factor * weight_recent``
        - clamp ``>= 0``.

    Casos borde:
        - ``recent_trades_7d == 0`` → ``weight_recent == 0`` → devuelve
          ``historical_score`` tal cual (no hay data reciente para mover el
          ranking). Si ``historical_score`` viene negativo lo cortamos a 0.
        - ``historical_score < 0`` → tratamos como 0 (defensa: el caller
          no debería pasar negativo, pero no rompemos).
    """
    if historical_score < 0:
        historical_score = 0.0

    if recent_pnl_7d < 0:
        recent_factor = 1.0 + recent_pnl_7d / 50.0
    else:
        recent_factor = 1.0 + min(recent_pnl_7d / 50.0, 0.5)

    weight_recent = min(max(recent_trades_7d, 0) / 10.0, 1.0)

    final = (
        historical_score * (1.0 - weight_recent)
        + historical_score * recent_factor * weight_recent
    )

    return max(final, 0.0)
