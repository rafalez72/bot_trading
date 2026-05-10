"""Tests for Bug 10 (2026-05-10): phantom_cleanup distingue phantom puro de
trade real con tracking roto y graba pnl realista vía MTM.

Background: el path original de `cleanup_phantom_positions` siempre grababa
`pnl_usdc=0` para cualquier trade no presente en /positions. Eso ocultaba
pérdidas reales del kill switch y del PnL acumulado de Telegram:
  - Si un trade tenía `entry_tx_hash` (BUY ejecutó on-chain) pero ya no
    estaba en /positions (vendido o redeemed externamente), grabar pnl=0
    camuflaba una pérdida real.
  - Si NO había tx_hash, sí era phantom puro: el USDC seguía en la wallet
    y pnl=0 era correcto.

Fix: clasificar cada candidato en 3 buckets:
  1. Pure phantom (no tx, no on-chain) → pnl=0, status=closed_external,
     exit_reason='phantom_cleanup'. Igual que antes + notif `live_phantom`.
  2. Recovered con MTM (tx_hash + no on-chain) → compute MTM,
     status=closed_external, exit_reason='phantom_recovered_external',
     pnl_usdc=(mid - entry_price) * shares. Notif "PHANTOM RECOVERED".
  3. On-chain present (token sí en /positions) → no-op, risk loop maneja.

Adicional: si CLOB caído y no podemos computar MTM, el recovered queda
`open` (no se cierra con pnl=0) — failsafe: preferimos seguir trackeando
a ocultar la pérdida.
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
    entry_price: float = 0.5,
    entry_size_usdc: float = 5.0,
    entry_shares: float = 10.0,
    entry_at: int | None = None,
    entry_tx_hash: str | None = None,
    dry_run: int = 0,
) -> int:
    """Helper que inserta un live_trade open con entry_at viejo (>2h)."""
    if entry_at is None:
        entry_at = int(time.time()) - 8000
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO live_trades
                (source_wallet, condition_id, token_id, asset, outcome,
                 outcome_index, side, entry_price, entry_size_usdc,
                 entry_shares, entry_at, entry_tx_hash, status, dry_run)
            VALUES (?, ?, ?, ?, 'YES', 0, 'BUY', ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                source_wallet, condition_id, token_id, token_id,
                entry_price, entry_size_usdc, entry_shares, entry_at,
                entry_tx_hash, dry_run,
            ),
        )
        return cur.lastrowid


def _set_funder(monkeypatch, addr: str = "0xfunder1234") -> None:
    import src.config as config_mod
    monkeypatch.setattr(config_mod, "POLYMARKET_FUNDER_ADDRESS", addr)


class _FakePositionsResponse:
    """Mock de httpx Response simulando /positions del Data API."""
    def __init__(self, json_payload, status_code=200):
        self._payload = json_payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


def _read_trade(trade_id: int) -> dict:
    with db() as conn:
        row = conn.execute(
            "SELECT status, pnl_usdc, exit_reason, exit_price FROM live_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    assert row is not None, f"live_trade #{trade_id} no encontrado"
    return {
        "status": row["status"],
        "pnl_usdc": row["pnl_usdc"],
        "exit_reason": row["exit_reason"],
        "exit_price": row["exit_price"],
    }


# ---------------------------------------------------------------------------
# Case 1: phantom puro (no tx_hash, no on-chain)
# ---------------------------------------------------------------------------
def test_pure_phantom_no_tx_grava_pnl_zero(isolated_db, monkeypatch):
    """Trade sin tx_hash y sin /positions → phantom puro.

    BUY nunca llegó al CLOB (sin tx) → USDC sigue en wallet → pnl=0 correcto.
    Status: closed_external. exit_reason: 'phantom_cleanup'.
    """
    _set_funder(monkeypatch)
    tid = _insert_open_live_trade(
        condition_id="0xpure",
        token_id="tok_pure",
        entry_tx_hash=None,  # CRÍTICO: no ejecutó
        entry_price=0.5,
        entry_shares=10.0,
        entry_size_usdc=5.0,
    )
    fake_resp = _FakePositionsResponse(json_payload=[])  # nada on-chain

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(notifier, "live_phantom", return_value=True):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    assert n == 1
    state = _read_trade(tid)
    assert state["status"] == "closed_external"
    assert state["pnl_usdc"] == 0
    assert state["exit_reason"] == "phantom_cleanup"


# ---------------------------------------------------------------------------
# Case 2: phantom recovered (tx_hash + no on-chain) → MTM como pnl
# ---------------------------------------------------------------------------
def test_phantom_with_tx_grava_mtm_pnl(isolated_db, monkeypatch):
    """tx_hash NOT NULL + no en /positions → BUY ejecutó pero posición cerró
    externamente. Compute MTM con mid actual.

    Setup: entry=$0.50, mid_actual=$0.35, shares=10 → MTM = (0.35-0.50)*10 = -$1.50
    Status: closed_external. exit_reason: 'phantom_recovered_external'.
    """
    _set_funder(monkeypatch)
    tid = _insert_open_live_trade(
        condition_id="0xrecov",
        token_id="tok_recov",
        entry_tx_hash="0xdeadbeef",  # CRÍTICO: ejecutó on-chain
        entry_price=0.5,
        entry_shares=10.0,
        entry_size_usdc=5.0,
    )
    fake_resp = _FakePositionsResponse(json_payload=[])  # no on-chain

    # Mock mid_actual = 0.35 → MTM = (0.35 - 0.5) * 10 = -1.5
    def fake_mark_price(token_id):
        return 0.35

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(executor, "_get_mark_price", side_effect=fake_mark_price), \
         patch.object(notifier, "send", return_value=True):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    assert n == 1
    state = _read_trade(tid)
    assert state["status"] == "closed_external"
    assert state["exit_reason"] == "phantom_recovered_external"
    assert state["pnl_usdc"] == pytest.approx(-1.5, abs=1e-6), (
        f"MTM esperado -$1.50, got {state['pnl_usdc']}. "
        "Si falla, el path recovered está grabando pnl=0 y ocultando pérdida."
    )
    assert state["exit_price"] == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# Case 3: tx_hash NOT NULL → SIEMPRE recovered (incluso si profit MTM)
# ---------------------------------------------------------------------------
def test_phantom_with_tx_hash_recovers_even_with_profit_mtm(isolated_db, monkeypatch):
    """Si tx_hash existe, SIEMPRE recoverea (no fallback a pnl=0), incluso
    si MTM es positivo (caso raro: posición cerró externamente con ganancia).
    """
    _set_funder(monkeypatch)
    tid = _insert_open_live_trade(
        condition_id="0xprofit",
        token_id="tok_profit",
        entry_tx_hash="0xfeedface",
        entry_price=0.4,
        entry_shares=20.0,
        entry_size_usdc=8.0,
    )
    fake_resp = _FakePositionsResponse(json_payload=[])

    # Mid > entry → MTM positivo: (0.65 - 0.4) * 20 = +5.0
    def fake_mark_price(token_id):
        return 0.65

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(executor, "_get_mark_price", side_effect=fake_mark_price), \
         patch.object(notifier, "send", return_value=True):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    assert n == 1
    state = _read_trade(tid)
    assert state["exit_reason"] == "phantom_recovered_external"
    assert state["pnl_usdc"] == pytest.approx(5.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Case 4: CLOB caído → no podemos calcular MTM → trade keep open (failsafe)
# ---------------------------------------------------------------------------
def test_clob_down_keeps_recovered_open_no_cleanup(isolated_db, monkeypatch):
    """Si tenemos tx_hash pero el CLOB está caído (mark_price=None), NO
    grabamos pnl=0 ocultando la pérdida — dejamos el trade open para que
    el próximo cycle reintente.

    Failsafe crítico: preferimos seguir trackeando una posición potencialmente
    perdida a marcarla closed con pnl=0 falso.
    """
    _set_funder(monkeypatch)
    tid = _insert_open_live_trade(
        condition_id="0xcdown",
        token_id="tok_cdown",
        entry_tx_hash="0xclobdown",
        entry_price=0.55,
        entry_shares=15.0,
        entry_size_usdc=8.25,
    )
    fake_resp = _FakePositionsResponse(json_payload=[])

    def fake_mark_price_none(token_id):
        return None  # CLOB caído

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(executor, "_get_mark_price", side_effect=fake_mark_price_none), \
         patch.object(notifier, "send", return_value=True):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    # Solo se contabilizan los que efectivamente cerraron (closable). El
    # recovered_with_tx que no pudo MTM se cuenta como "kept open" → no suma.
    assert n == 0
    state = _read_trade(tid)
    # Trade sigue open: ni closed_external ni MTM. Próximo cleanup reintentará.
    assert state["status"] == "open", (
        f"Esperaba status='open' cuando CLOB cae, got '{state['status']}'. "
        "Si falla, estamos grabando phantom_cleanup falso con CLOB caído."
    )
    assert state["pnl_usdc"] is None
    assert state["exit_reason"] is None


# ---------------------------------------------------------------------------
# Case 5: batch mixto — pure + recovered + on-chain en mismo cycle
# ---------------------------------------------------------------------------
def test_batch_mixto_pure_recovered_y_onchain(isolated_db, monkeypatch):
    """Multiple phantoms en batch: cada categoría se procesa correctamente.

    - 2 pure phantoms (no tx, no on-chain) → pnl=0 closed_external
    - 2 recovered (tx, no on-chain) → MTM pnl, closed_external
    - 1 on-chain (token en /positions) → NO se toca, sigue open
    """
    _set_funder(monkeypatch)

    pure_ids = [
        _insert_open_live_trade(
            condition_id=f"0xpure_{i}",
            token_id=f"tok_pure_{i}",
            entry_tx_hash=None,
            entry_price=0.5,
            entry_shares=10.0,
            entry_size_usdc=5.0,
        )
        for i in range(2)
    ]
    recov_ids = [
        _insert_open_live_trade(
            condition_id=f"0xrec_{i}",
            token_id=f"tok_rec_{i}",
            entry_tx_hash=f"0xtx_{i}",
            entry_price=0.5,
            entry_shares=10.0,
            entry_size_usdc=5.0,
        )
        for i in range(2)
    ]
    # On-chain: aparece en /positions → no debe ser tocado
    onchain_id = _insert_open_live_trade(
        condition_id="0xlive",
        token_id="tok_live",
        entry_tx_hash="0xtxlive",
        entry_price=0.5,
        entry_shares=10.0,
        entry_size_usdc=5.0,
    )

    fake_resp = _FakePositionsResponse(
        json_payload=[{"asset": "tok_live", "size": "10"}]
    )

    def fake_mark_price(token_id):
        return 0.4  # MTM = (0.4 - 0.5) * 10 = -1.0

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(executor, "_get_mark_price", side_effect=fake_mark_price), \
         patch.object(notifier, "live_phantom", return_value=True), \
         patch.object(notifier, "send", return_value=True):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    # 2 pure + 2 recovered = 4 procesados; on-chain no cuenta
    assert n == 4

    # Pure: pnl=0, closed_external, exit_reason=phantom_cleanup
    for pid in pure_ids:
        s = _read_trade(pid)
        assert s["status"] == "closed_external"
        assert s["pnl_usdc"] == 0
        assert s["exit_reason"] == "phantom_cleanup"

    # Recovered: pnl MTM, closed_external, exit_reason=phantom_recovered_external
    for rid in recov_ids:
        s = _read_trade(rid)
        assert s["status"] == "closed_external"
        assert s["pnl_usdc"] == pytest.approx(-1.0, abs=1e-6)
        assert s["exit_reason"] == "phantom_recovered_external"

    # On-chain: intacto, sigue open
    s = _read_trade(onchain_id)
    assert s["status"] == "open"
    assert s["pnl_usdc"] is None
    assert s["exit_reason"] is None


# ---------------------------------------------------------------------------
# Case 6: notif Telegram — recovered envía "PHANTOM RECOVERED" con MTM
# ---------------------------------------------------------------------------
def test_recovered_envia_notif_phantom_recovered(isolated_db, monkeypatch):
    """Cuando hay recover con MTM, debe enviarse notif distinta de live_phantom:
    texto "PHANTOM RECOVERED" + valor MTM exacto.
    """
    _set_funder(monkeypatch)
    _insert_open_live_trade(
        condition_id="0xnotif",
        token_id="tok_notif",
        entry_tx_hash="0xtxnotif",
        entry_price=0.6,
        entry_shares=10.0,
        entry_size_usdc=6.0,
    )
    fake_resp = _FakePositionsResponse(json_payload=[])

    def fake_mark_price(token_id):
        return 0.45  # MTM = (0.45 - 0.6) * 10 = -1.50

    sent_texts: list[str] = []

    def fake_send(txt, *a, **kw):
        sent_texts.append(txt)
        return True

    with patch("httpx.get", return_value=fake_resp), \
         patch.object(executor, "_get_mark_price", side_effect=fake_mark_price), \
         patch.object(notifier, "send", side_effect=fake_send), \
         patch.object(notifier, "live_phantom", return_value=True):
        n = executor.cleanup_phantom_positions(min_age_seconds=7200)

    assert n == 1
    # Al menos una notif con "PHANTOM RECOVERED" + MTM
    recovered_texts = [t for t in sent_texts if "PHANTOM RECOVERED" in t]
    assert len(recovered_texts) >= 1, (
        f"Esperaba notif 'PHANTOM RECOVERED', got texts: {sent_texts}"
    )
    # El MTM exacto debe aparecer en el mensaje
    txt = recovered_texts[0]
    assert "-1.50" in txt or "-$1.50" in txt, (
        f"MTM exacto -1.50 debería aparecer en notif: {txt}"
    )
