"""Anti-survivorship en composite_score.

Un win_rate alto medido solo sobre posiciones REALIZADAS engaña cuando el
trader aguanta perdedores ABIERTOS (no los cierra). Esos perdedores van a
unrealized_pnl_usdc. El score debe contarlos:
  - net (realizado + no-realizado) <= MIN_NET_PNL_USDC  → score 0 (no copiar)
  - arrastre no-realizado grande                        → downrank
"""
from __future__ import annotations

from src.analytics.metrics import WalletMetrics, composite_score


def _wm(**kw) -> WalletMetrics:
    base = dict(
        wallet="0xtest", total_trades=300, total_volume_usdc=50_000.0,
        realized_pnl_usdc=1000.0, unrealized_pnl_usdc=0.0, roi_pct=50.0,
        win_rate=0.7, avg_position_size=100.0, max_drawdown_pct=10.0,
        sharpe_proxy=1.2, active_days=200, first_trade_ts=0, last_trade_ts=0,
    )
    base.update(kw)
    return WalletMetrics(**base)


def test_clean_trader_scores_positive():
    assert composite_score(_wm()) > 0


def test_survivorship_holder_rejected():
    # win_rate realizado 100%, +$1000 realizado, pero -$1500 en perdedores
    # ABIERTOS → neto -$500 → no se copia.
    m = _wm(realized_pnl_usdc=1000.0, unrealized_pnl_usdc=-1500.0, win_rate=1.0)
    assert composite_score(m) == 0.0


def test_unrealized_drag_downranks():
    clean = composite_score(_wm(unrealized_pnl_usdc=0.0))
    # neto +$500 (pasa el piso) pero con arrastre → score menor que el limpio.
    dragged = composite_score(_wm(unrealized_pnl_usdc=-500.0))
    assert 0.0 < dragged < clean
