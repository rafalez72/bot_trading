"""Tests for phantom_cleanup Telegram notifications.

Background (2026-05-10): the user lost $76 in their first LIVE_MODE session
because 29/50 trades fell into `phantom_cleanup` (status='closed_external')
and the cleanup path NEVER notified Telegram. The user had no idea the bot
was failing silently.

Fix: `cleanup_phantom_positions` now invokes `notifier.live_phantom`, either
per-trade (small bursts) or as a summary (>5 phantoms).
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.copybot import executor
from src.copybot import notifier
from src.db.schema import db


def _insert_open_live_trade(
    *,
    source_wallet: str = "0xphantom",
    condition_id: str = "0xcid_phantom",
    token_id: str = "tok_phantom_1",
    entry_size_usdc: float = 5.0,
    entry_at: int | None = None,
    dry_run: int = 0,
) -> int:
    """Helper que inserta un live_trade open con entry_at viejo (>2h)."""
    if entry_at is None:
        # cleanup_phantom_positions usa min_age 7200s; restamos 8000 para que
        # quede del lado "viejo" del cutoff.
        entry_at = int(time.time()) - 8000
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO live_trades
                (source_wallet, condition_id, token_id, asset, outcome,
                 outcome_index, side, entry_price, entry_size_usdc,
                 entry_shares, entry_at, status, dry_run)
            VALUES (?, ?, ?, ?, 'YES', 0, 'BUY', 0.5, ?, 10.0, ?, 'open', ?)
            """,
            (
                source_wallet, condition_id, token_id, token_id,
                entry_size_usdc, entry_at, dry_run,
            ),
        )
        return cur.lastrowid


def _set_funder(monkeypatch, addr: str = "0xfunder1234") -> None:
    """cleanup_phantom_positions sale temprano si POLYMARKET_FUNDER_ADDRESS
    está vacío. El módulo importa en runtime (`from src.config import ...`),
    así que parchamos el attr de config."""
    import src.config as config_mod
    monkeypatch.setattr(config_mod, "POLYMARKET_FUNDER_ADDRESS", addr)


class _FakePositionsResponse:
    """Mock de httpx Response que simula la respuesta del Data API."""
    def __init__(self, json_payload, status_code=200):
        self._payload = json_payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def test_phantom_cleanup_sends_per_trade_notif_for_small_bursts(
    isolated_db, monkeypatch
):
    """Si <=5 phantoms se detectan, cada uno debe disparar live_phantom().

    Antes del fix: 0 notifs se enviaban — los trades se silenciaban a DB
    pero el user nunca se enteraba de las pérdidas.
    """
    _set_funder(monkeypatch)
    # 3 phantoms: trades viejos cuyo token_id NO aparecerá en /positions
    ids = []
    for i in range(3):
        ids.append(_insert_open_live_trade(
            condition_id=f"0xcid_{i}",
            token_id=f"tok_ghost_{i}",
            entry_size_usdc=5.0 + i,
        ))

    # /positions devuelve lista vacía → todos los trades de la DB son fantasmas
    fake_resp = _FakePositionsResponse(json_payload=[])

    sent_calls: list[dict] = []

    def fake_live_phantom(**kw):
        sent_calls.append(kw)
        return True

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(notifier, "live_phantom", side_effect=fake_live_phantom):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    assert n == 3
    # Una notif por phantom (3 trades → 3 calls)
    assert len(sent_calls) == 3, (
        f"Esperaba 3 notifs (una por phantom), got {len(sent_calls)}. "
        "Si falla, el path de cleanup volvió a ser silente."
    )
    # Verificar que cada call lleva trade_id y size_usdc no-None
    for c in sent_calls:
        assert c.get("trade_id") in ids
        assert c.get("size_usdc", 0) > 0


def test_phantom_cleanup_sends_summary_for_large_bursts(isolated_db, monkeypatch):
    """Si >5 phantoms se detectan, debe mandarse un solo resumen agregado
    (n_phantoms=N, size_usdc=total) en vez de spam individual.
    """
    _set_funder(monkeypatch)
    for i in range(8):
        _insert_open_live_trade(
            condition_id=f"0xcid_burst_{i}",
            token_id=f"tok_burst_{i}",
            entry_size_usdc=2.0,
        )

    fake_resp = _FakePositionsResponse(json_payload=[])
    sent_calls: list[dict] = []

    def fake_live_phantom(**kw):
        sent_calls.append(kw)
        return True

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(notifier, "live_phantom", side_effect=fake_live_phantom):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    # Defensa de cleanup: si /positions=[] y rows>50 → skip por seguridad.
    # Acá tenemos 8 < 50, debería procesar normalmente.
    assert n == 8
    # Una sola call con n_phantoms (no per-trade)
    assert len(sent_calls) == 1, (
        f"Esperaba 1 call summary, got {len(sent_calls)}. "
        "Bursts grandes deberían agregar al resumen, no spamear."
    )
    payload = sent_calls[0]
    assert payload.get("n_phantoms") == 8
    assert payload.get("size_usdc") == pytest.approx(8 * 2.0)


def test_live_phantom_function_respects_enabled_set():
    """Si live_phantom no está en ENABLED_NOTIFICATIONS, debe ser no-op."""
    # Forzar que NO esté habilitado, luego restaurar
    original = notifier.ENABLED_NOTIFICATIONS.copy()
    try:
        notifier.ENABLED_NOTIFICATIONS.discard("live_phantom")
        # Sin importar el contenido, debe devolver False y no llamar send()
        with patch.object(notifier, "send") as mock_send:
            result = notifier.live_phantom(
                trade_id=1, size_usdc=5.0, market_slug="test"
            )
        assert result is False
        assert mock_send.call_count == 0
    finally:
        notifier.ENABLED_NOTIFICATIONS.clear()
        notifier.ENABLED_NOTIFICATIONS.update(original)


def test_live_phantom_function_sends_when_enabled():
    """Cuando está enabled, debe llamar send() con texto distintivo de fantasma."""
    original = notifier.ENABLED_NOTIFICATIONS.copy()
    try:
        notifier.ENABLED_NOTIFICATIONS.add("live_phantom")
        with patch.object(notifier, "send", return_value=True) as mock_send:
            ok = notifier.live_phantom(
                trade_id=42,
                size_usdc=12.50,
                market_slug="btc-updown-5m-test",
                source_wallet="0xabcdef0123456789",
            )
        assert ok is True
        assert mock_send.call_count == 1
        # El texto debe mencionar FANTASMA y el monto exacto
        sent_txt = mock_send.call_args[0][0]
        assert "FANTASMA" in sent_txt
        assert "12.50" in sent_txt
        assert "Trade #42" in sent_txt
    finally:
        notifier.ENABLED_NOTIFICATIONS.clear()
        notifier.ENABLED_NOTIFICATIONS.update(original)
