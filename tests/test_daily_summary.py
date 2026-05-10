"""Tests for the daily_summary Telegram notification.

Background (2026-05-10): user lost $76 in their first LIVE_MODE session
without any aggregate visibility — `daily_summary` was a no-op stub. The fix
implements a proper 24h summary with PnL, wins/losses, capital usage and
drawdown.
"""
from __future__ import annotations

from unittest.mock import patch

from src.copybot import notifier


def test_daily_summary_no_op_when_disabled():
    """Si 'daily_summary' no está en ENABLED_NOTIFICATIONS, no manda nada."""
    original = notifier.ENABLED_NOTIFICATIONS.copy()
    try:
        notifier.ENABLED_NOTIFICATIONS.discard("daily_summary")
        with patch.object(notifier, "send") as mock_send:
            ok = notifier.daily_summary(pnl_today=10.0, wins=5, losses=2)
        assert ok is False
        assert mock_send.call_count == 0
    finally:
        notifier.ENABLED_NOTIFICATIONS.clear()
        notifier.ENABLED_NOTIFICATIONS.update(original)


def test_daily_summary_includes_pnl_wins_losses_when_enabled():
    """Cuando está enabled, debe llamar send() con un texto que incluya el
    PnL, win count y loss count exactos.
    """
    original = notifier.ENABLED_NOTIFICATIONS.copy()
    try:
        notifier.ENABLED_NOTIFICATIONS.add("daily_summary")
        with patch.object(notifier, "send", return_value=True) as mock_send:
            ok = notifier.daily_summary(
                pnl_today=-3.50,
                wins=2,
                losses=8,
                open_positions=1,
                capital_used=15.0,
                capital_total=50.0,
                drawdown_pct=7.0,
                active_traders=42,
                dropped_traders=3,
            )
        assert ok is True
        assert mock_send.call_count == 1
        sent_txt = mock_send.call_args[0][0]
        # Verificar campos críticos
        assert "Resumen 24h" in sent_txt
        assert "$-3.50" in sent_txt
        # 2W / 8L
        assert "2W" in sent_txt and "8L" in sent_txt
        # Capital
        assert "15.00" in sent_txt and "50.00" in sent_txt
        # Drawdown
        assert "7.0%" in sent_txt
    finally:
        notifier.ENABLED_NOTIFICATIONS.clear()
        notifier.ENABLED_NOTIFICATIONS.update(original)


def test_daily_summary_handles_zero_trades():
    """Día sin trades: total=0, win_rate calc no debe explotar (div by zero)."""
    original = notifier.ENABLED_NOTIFICATIONS.copy()
    try:
        notifier.ENABLED_NOTIFICATIONS.add("daily_summary")
        with patch.object(notifier, "send", return_value=True) as mock_send:
            ok = notifier.daily_summary(
                pnl_today=0.0, wins=0, losses=0,
                capital_used=0.0, capital_total=50.0,
            )
        assert ok is True
        sent_txt = mock_send.call_args[0][0]
        assert "0%" in sent_txt  # win_rate cuando no hay trades
    finally:
        notifier.ENABLED_NOTIFICATIONS.clear()
        notifier.ENABLED_NOTIFICATIONS.update(original)


def test_daily_summary_drawdown_optional():
    """Si drawdown_pct es None, debe mostrarse como 'n/a' sin crash."""
    original = notifier.ENABLED_NOTIFICATIONS.copy()
    try:
        notifier.ENABLED_NOTIFICATIONS.add("daily_summary")
        with patch.object(notifier, "send", return_value=True) as mock_send:
            notifier.daily_summary(
                pnl_today=1.0, wins=1, losses=0,
                capital_total=10.0, drawdown_pct=None,
            )
        sent_txt = mock_send.call_args[0][0]
        assert "n/a" in sent_txt
    finally:
        notifier.ENABLED_NOTIFICATIONS.clear()
        notifier.ENABLED_NOTIFICATIONS.update(original)
