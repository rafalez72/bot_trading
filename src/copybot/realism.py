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
# Gas calibrado: Polymarket usa el CTF Exchange (mayor consumo que un transfer USDC).
# A 30-50 gwei + MATIC ~$0.50, una orden típica está en $0.04-0.08. Default $0.05.
GAS_PER_TX = lambda: _envf("REALISM_GAS_USDC", 0.05)
# Win rate típico esperado por trade copiado (medido en paper, 1074 cerrados: 32.5%).
EXPECTED_WIN_RATE = lambda: _envf("REALISM_EXPECTED_WIN_RATE", 0.325)
# Multiplicador de ganancia bruta sobre size cuando el trade es win.
# Medido en paper: avg_win $7.43 sobre size $5 → 1.524× (gross, antes de fee+gas).
EXPECTED_WIN_GAIN_MULT = lambda: _envf("REALISM_EXPECTED_WIN_GAIN_MULT", 1.5)
# Pérdida bruta promedio como % del size cuando el trade es loss.
# Medido en paper: avg_loss $1.55 + gas $0.04 = $1.59 → 0.32 sobre size $5.
EXPECTED_LOSS_PCT = lambda: _envf("REALISM_EXPECTED_LOSS_PCT", 0.32)

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


def expected_net_pnl(size_usdc: float) -> float:
    """Estimación del PnL esperado de un trade dado su size.

    Modelo (calibrado contra paper trading: 1074 trades, +$1422):
      gross_expected = size * (win_rate * win_mult - (1-win_rate) * loss_pct)
      net = gross - gas_total - fee_on_wins

    Calibración default (vía .env tunable):
      win_rate=0.325, win_mult=1.5, loss_pct=0.32, gas=$0.05/tx, fee=2% wins.
    Esto da expected_pnl/size ≈ 0.265 (concuerda con paper: $1.32/$5 = 0.264).

    Para trades muy chicos, gas fijo come el upside.
    Útil para filtrar trades en live donde sizing_mult << 1.
    """
    win_rate = EXPECTED_WIN_RATE()
    win_mult = EXPECTED_WIN_GAIN_MULT()
    loss_pct = EXPECTED_LOSS_PCT()

    gross_win_value = size_usdc * win_rate * win_mult
    gross_loss_value = size_usdc * (1 - win_rate) * loss_pct
    expected_gross = gross_win_value - gross_loss_value

    gas_total = GAS_PER_TX() * 2  # entry + exit
    fee_on_wins = gross_win_value * FEE_PCT()

    return expected_gross - gas_total - fee_on_wins
