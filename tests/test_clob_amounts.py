"""Tests for src/polymarket/clob_client.py — _quantize_amounts.

Polymarket CLOB rechaza con HTTP 400 si los amounts del payload exceden los
límites de precisión decimal:

    invalid amounts, the market buy orders maker amount supports a max
    accuracy of 2 decimals, taker amount a max of 4 decimals

Estos tests cubren los casos típicos del bot (tick=0.01) y los edge cases
(precios con 3 decimales, sizes muy chicos, productos que requieren recortar
shares para encajar).
"""
from __future__ import annotations

from src.polymarket.clob_client import _quantize_amounts


def _max_decimals(value: float, max_dec: int) -> bool:
    """True si `value` tiene <= max_dec decimales (con tolerancia float)."""
    scaled = round(value * (10 ** max_dec))
    return abs(value * (10 ** max_dec) - scaled) < 1e-6


def test_quantize_amounts_typical_size_7_price_0_347():
    """Caso real del primer deploy: size=7.0 USDC, price=0.347 (tick=0.001).

    raw_shares = 7.0 / 0.347 = 20.1729...
    Quantize a 4 decimales: 20.1729
    maker = 20.1729 * 0.347 = 6.99998... → no encaja en 2 decimales.
    El helper recorta shares hasta que maker quede exacto a 2 dec.
    """
    shares, price = _quantize_amounts(7.0 / 0.347, 0.347)

    # shares debe tener max 4 decimales
    assert _max_decimals(shares, 4), f"shares={shares} excede 4 decimales"
    # producto (maker) debe tener max 2 decimales
    maker = shares * price
    assert _max_decimals(maker, 2), f"maker={maker} excede 2 decimales"
    # No exceder el size pedido (7.0 USDC)
    assert maker <= 7.0 + 1e-9, f"maker={maker} excede size_usdc=7.0"
    # Razonable: gastamos al menos 95% del size pedido
    assert maker >= 7.0 * 0.95


def test_quantize_amounts_tick_0_01_integer_shares():
    """tick=0.01 (price 2 decimales): shares enteros → maker siempre 2 dec.

    size=10.0, price=0.50 → raw_shares=20.0, maker=10.00 (2 dec ✓).
    """
    shares, price = _quantize_amounts(10.0 / 0.50, 0.50)
    maker = shares * price
    assert _max_decimals(shares, 4)
    assert _max_decimals(maker, 2)
    assert shares == 20.0  # caso exacto, no recorte


def test_quantize_amounts_extreme_low_price():
    """Precio muy bajo (0.01) — caso de longshot. shares grande pero maker chico."""
    shares, price = _quantize_amounts(7.0 / 0.01, 0.01)
    maker = shares * price
    assert _max_decimals(shares, 4)
    assert _max_decimals(maker, 2)
    assert maker <= 7.0 + 1e-9


def test_quantize_amounts_size_too_small_returns_zero():
    """Size tan chico que post-quantize no llega a 1 share → devuelve 0.

    size=0.001 (raro pero defensivo) — el caller debe rechazar el trade.
    """
    shares, _ = _quantize_amounts(0.0001, 0.50)
    assert shares == 0.0


def test_quantize_amounts_zero_inputs_safe():
    """Inputs <=0 → devuelve (0.0, price) sin crash."""
    shares, _ = _quantize_amounts(0.0, 0.50)
    assert shares == 0.0
    shares, _ = _quantize_amounts(-1.0, 0.50)
    assert shares == 0.0
    shares, _ = _quantize_amounts(10.0, 0.0)
    assert shares == 0.0


def test_quantize_amounts_three_decimal_price():
    """tick=0.001: price con 3 decimales (ej. updown-5m markets).

    size=5.0, price=0.123 → raw_shares=40.65..., maker=5.0 con
    decimales sucios. El helper debe recortar shares para maker exacto a 2 dec.
    """
    shares, price = _quantize_amounts(5.0 / 0.123, 0.123)
    maker = shares * price
    assert _max_decimals(shares, 4), f"shares={shares}"
    assert _max_decimals(maker, 2), f"maker={maker}"
    assert maker <= 5.0 + 1e-9
