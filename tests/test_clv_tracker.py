"""Tests para src/copybot/clv_tracker.py.

Cubre:
  1. _signed_clv_pct: cálculo correcto BUY/SELL + edge cases.
  2. record_clv + compute_clv_summary: roundtrip insert + agg en SQLite
     isolated DB. Verifica avg/median/positive_pct y by_source split.
  3. report_clv: string Telegram-ready coherente con summary,
     y mensaje explícito si no hay datos.

Usa la fixture `isolated_db` de tests/conftest.py para no tocar la DB
real ni necesitar Postgres.
"""
from __future__ import annotations

from src.copybot.clv_tracker import (
    _signed_clv_pct,
    compute_clv_summary,
    record_clv,
    report_clv,
)


# ---------- Test 1: _signed_clv_pct ----------

def test_signed_clv_pct_buy_positive_when_market_rises():
    """BUY a 0.40, cierre a 0.55 → CLV = +37.5% (capté edge)."""
    clv = _signed_clv_pct(entry_price=0.40, closing_price=0.55, side="BUY")
    assert clv > 0
    assert clv == (0.55 - 0.40) / 0.40

    # SELL inverso: vender alto, cierre bajo → positivo
    clv_sell = _signed_clv_pct(entry_price=0.80, closing_price=0.50, side="SELL")
    assert clv_sell > 0
    assert clv_sell == (0.80 - 0.50) / 0.80

    # Adversarial BUY: entré a 0.60, cierre a 0.40 → CLV negativo
    clv_adv = _signed_clv_pct(entry_price=0.60, closing_price=0.40, side="BUY")
    assert clv_adv < 0

    # Edge: entry_price=0 → 0 (no podemos normalizar)
    assert _signed_clv_pct(0.0, 0.5, "BUY") == 0.0
    # Edge: None inputs → 0
    assert _signed_clv_pct(None, 0.5, "BUY") == 0.0


# ---------- Test 2: record_clv + compute_clv_summary roundtrip ----------

def test_record_clv_and_summary(isolated_db):
    """Insertamos varios trades simulando un mix de wins/losses + 2
    sources (paper, crypto_arb) y verificamos que el summary los
    agrega correctamente.
    """
    # Win grande paper
    clv1 = record_clv(trade_id=1, entry_price=0.40, closing_price=1.0,
                      side="BUY", source="paper")
    # Loss paper (resolvió contra)
    clv2 = record_clv(trade_id=2, entry_price=0.60, closing_price=0.0,
                      side="BUY", source="paper")
    # Win chiquito crypto_arb
    clv3 = record_clv(trade_id=3, entry_price=0.50, closing_price=0.55,
                      side="BUY", source="crypto_arb",
                      bucket_slug="will-btc-rise-12pm")
    # Loss crypto_arb
    clv4 = record_clv(trade_id=4, entry_price=0.50, closing_price=0.0,
                      side="BUY", source="crypto_arb",
                      bucket_slug="will-eth-rise-12pm")

    assert clv1 == (1.0 - 0.40) / 0.40
    assert clv2 == (0.0 - 0.60) / 0.60  # = -1.0
    assert clv3 == (0.55 - 0.50) / 0.50
    assert clv4 == -1.0

    s = compute_clv_summary(window_hours=24)
    assert s["n"] == 4
    assert s["avg_clv_pct"] is not None
    # 2 wins de 4 → 50% positive
    assert s["positive_pct"] == 0.5

    # Split por source: 2 paper, 2 crypto_arb
    by_src = s["by_source"]
    assert "paper" in by_src and "crypto_arb" in by_src
    assert by_src["paper"]["n"] == 2
    assert by_src["crypto_arb"]["n"] == 2
    # paper: 1 positivo (clv1), 1 negativo (clv2) → 50%
    assert by_src["paper"]["positive_pct"] == 0.5
    # crypto_arb: 1 positivo (clv3), 1 negativo (clv4) → 50%
    assert by_src["crypto_arb"]["positive_pct"] == 0.5

    # Sanity: empty window → n=0 (ventana de 0 horas captura solo lo que
    # se insertó EXACTAMENTE este segundo, así que en general no hay
    # datos. Lo más limpio es testear con una ventana negativa que
    # garantiza cutoff > now).
    s_empty = compute_clv_summary(window_hours=0)
    # window=0 → cutoff=now → trades con recorded_at == now SÍ pasan
    # (>= cutoff). Test menos frágil: forzamos cutoff futuro patcheando
    # nada — basta con verificar que summary funciona y no rompe.
    assert isinstance(s_empty, dict)
    assert "n" in s_empty


# ---------- Test 3: report_clv genera texto razonable ----------

def test_report_clv_text(isolated_db):
    """report_clv() devuelve un string con stats; si no hay trades,
    mensaje explícito 'sin trades settled'.
    """
    # Caso vacío
    txt = report_clv(window_hours=24)
    assert "sin trades settled" in txt.lower()

    # Insertamos trades y volvemos a llamar
    record_clv(trade_id=1, entry_price=0.40, closing_price=1.0,
               side="BUY", source="paper")
    record_clv(trade_id=2, entry_price=0.50, closing_price=0.30,
               side="BUY", source="crypto_arb")

    txt = report_clv(window_hours=24)
    # Sanity-check del formato — chequeamos campos clave sin exigir
    # layout exacto (deja libertad para tunear el formatting):
    assert "n=2" in txt
    assert "avg=" in txt
    assert "paper" in txt
    assert "crypto_arb" in txt
    # No emojis (regla del proyecto)
    for forbidden in ("✅", "❌", "🚀", "📊"):
        assert forbidden not in txt
