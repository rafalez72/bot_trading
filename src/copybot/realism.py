"""Modela las fricciones reales que NO existen en paper trading puro:
slippage, fees de Polymarket, gas en Polygon, partial fills.

Diseñado para que `entry_price` y `exit_price` almacenados ya reflejen
lo que pasaría con plata REAL — sin tener que cambiar la lógica de PnL.

Toggle vía .env: `REALISTIC_MODE=true|false`.
"""
from __future__ import annotations

import os
import random


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return os.getenv("REALISTIC_MODE", "true").lower() in ("1", "true", "yes", "on")


# Calibrado heurísticamente. Estos valores son ESTIMADOS — la realidad puede
# variar ±50% según condiciones de mercado. Se pueden tunear vía .env si
# observamos diferencias sistemáticas con la ejecución real.
ENTRY_SLIP_BASE = lambda: _envf("REALISM_ENTRY_SLIP_PCT", 0.015)
EXIT_SLIP_BASE = lambda: _envf("REALISM_EXIT_SLIP_PCT", 0.015)
STOP_LOSS_SLIP_MULT = lambda: _envf("REALISM_SL_SLIP_MULT", 2.0)
LONG_SHOT_BONUS = lambda: _envf("REALISM_LONGSHOT_SLIP_PCT", 0.04)
LOW_LIQ_BONUS = lambda: _envf("REALISM_LOWLIQ_SLIP_PCT", 0.03)
LOW_LIQ_THRESHOLD = lambda: _envf("REALISM_LOWLIQ_THRESHOLD_USDC", 10000)
FEE_PCT = lambda: _envf("REALISM_FEE_PCT", 0.02)
GAS_PER_TX = lambda: _envf("REALISM_GAS_USDC", 0.02)

LONG_SHOT_LOW = 0.15
LONG_SHOT_HIGH = 0.85


def _entry_slippage_pct(price: float, liquidity: float | None) -> float:
    s = ENTRY_SLIP_BASE()
    if price < LONG_SHOT_LOW or price > LONG_SHOT_HIGH:
        s += LONG_SHOT_BONUS()
    if liquidity is None or liquidity < LOW_LIQ_THRESHOLD():
        s += LOW_LIQ_BONUS()
    # Asimétrico: la varianza tiende a empeorar nuestro precio
    s += random.uniform(-0.003, 0.008)
    return max(0.0, s)


def _exit_slippage_pct(price: float, liquidity: float | None, is_stop_loss: bool) -> float:
    s = EXIT_SLIP_BASE()
    if liquidity is None or liquidity < LOW_LIQ_THRESHOLD():
        s += LOW_LIQ_BONUS()
    if is_stop_loss:
        s *= STOP_LOSS_SLIP_MULT()
    s += random.uniform(-0.003, 0.008)
    return max(0.0, s)


def realistic_entry_price(source_price: float, liquidity: float | None) -> float:
    """Precio al que el bot REALMENTE compraría — peor que el del source."""
    if not _enabled() or source_price <= 0:
        return source_price
    slip = _entry_slippage_pct(source_price, liquidity)
    return min(0.999, source_price * (1.0 + slip))


def realistic_exit_price(
    market_price: float,
    liquidity: float | None,
    *,
    is_stop_loss: bool = False,
) -> float:
    """Precio al que el bot REALMENTE vendería — peor que el observado."""
    if not _enabled() or market_price <= 0:
        return market_price
    slip = _exit_slippage_pct(market_price, liquidity, is_stop_loss)
    return max(0.001, market_price * (1.0 - slip))


def post_close_costs(gross_pnl: float) -> tuple[float, float, float]:
    """Devuelve (fee, gas_total, net_pnl).

    fee:    2% sobre PnL si > 0 (Polymarket cobra al lado ganador)
    gas:    $0.02 × 2 (entry + exit) en Polygon
    net:    gross_pnl - fee - gas
    """
    if not _enabled():
        return (0.0, 0.0, gross_pnl)
    fee = max(0.0, gross_pnl) * FEE_PCT()
    gas = GAS_PER_TX() * 2  # BUY + SELL
    return (fee, gas, gross_pnl - fee - gas)


def is_enabled() -> bool:
    return _enabled()
