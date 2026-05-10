"""Tests del kill switch HARD 3-layer (src/copybot/risk.py).

Capa nueva añadida sobre `check_kill_switch()` el 2026-05-10. Son 3 caps
HARD complementarios al legacy DAILY_KILL_SWITCH_PCT (10% rolling-24h):

  1) daily_loss_cap     — SUM(pnl) desde 00:00 UTC <= -DAILY_LOSS_CAP_USDC
  2) consecutive_losses — últimos N trades cerrados son LOSS
  3) drawdown           — peak-to-trough capital < -MAX_DRAWDOWN_PCT

Defaults (paper, BOT_CAPITAL=$100):
  DAILY_LOSS_CAP_USDC=10   MAX_CONSECUTIVE_LOSSES=5   MAX_DRAWDOWN_PCT=0.20
  DAILY_KILL_SWITCH_PCT=0.10  → legacy threshold = -$10 rolling-24h.

El legacy se evalúa primero. Para aislar las nuevas layers en estos tests
mantenemos el SUM(pnl) > -$10 (no dispara legacy ni daily_loss_cap salvo
cuando el test específicamente lo busca).
"""
from __future__ import annotations

import time

from src.copybot import risk
from src.db.schema import db, tx


# ---------- helpers ----------


def _insert_closed(
    *,
    pnl: float,
    exit_at: int,
    status: str = "closed_loss",
    condition_id: str = "0xcid",
    source_wallet: str = "0xabc",
) -> int:
    """Inserta un paper_trade cerrado con pnl/exit_at/status dados.

    Devuelve el id del row insertado para poder verificar `ids` en notifs.
    """
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO paper_trades
                (source_wallet, source_trade_id, condition_id, outcome,
                 outcome_index, side, entry_price, entry_size_usdc,
                 entry_at, exit_price, exit_at, pnl_usdc, status)
            VALUES (?, NULL, ?, 'YES', 0, 'BUY', 0.5, 5.0,
                    ?, 0.4, ?, ?, ?)
            """,
            (
                source_wallet,
                condition_id,
                exit_at - 60,
                exit_at,
                pnl,
                status,
            ),
        )
        return int(cur.lastrowid)


def _set_peak(value: float) -> None:
    """Setea bot_state.peak_balance_usdc al valor dado (forzado)."""
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('peak_balance_usdc', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value, updated_at = datetime('now')
            """,
            (f"{value:.6f}",),
        )


# ---------- Layer 1: daily_loss_cap ----------


def test_daily_loss_cap_triggers_when_today_utc_pnl_exceeds_cap(
    isolated_db, now_ts, monkeypatch
):
    """SUM(pnl) desde 00:00 UTC <= -$10 → kill switch active.

    El legacy dispara antes con el mismo total (-$12 también supera el 10%
    de $100 rolling-24h). El motivo persistido va a ser el del legacy
    layer, que es el comportamiento esperado: layer "más estricto" (legacy
    = ventana más amplia) gana cuando ambas condiciones se cumplen.
    Para verificar específicamente que el layer NUEVO funciona, monkey-
    patcheamos el legacy threshold a un valor inalcanzable.
    """
    # Anular legacy: threshold inmenso → nunca dispara → fuerza fallthrough
    # a las layers nuevas.
    monkeypatch.setattr(risk, "DAILY_KILL_SWITCH_PCT", 9.99)

    # 3 trades hoy UTC, total -$12 (excede DAILY_LOSS_CAP_USDC=$10).
    _insert_closed(pnl=-4.0, exit_at=now_ts - 100)
    _insert_closed(pnl=-4.0, exit_at=now_ts - 200)
    _insert_closed(pnl=-4.0, exit_at=now_ts - 300)

    assert risk.check_kill_switch() is True
    s = risk.kill_switch_status()
    assert s["active"] is True
    assert "daily_loss_cap" in (s["reason"] or "")


# ---------- Layer 2: consecutive_losses ----------


def test_consecutive_losses_triggers_after_n_losses_in_a_row(
    isolated_db, now_ts
):
    """5 LOSS consecutivos (y total > -$10) → kill switch active.

    Cada loss es chico (-$1.5) → SUM(pnl)=-$7.50, no dispara legacy ni
    daily_loss_cap. Solo el layer consecutive debería disparar.
    """
    ids = []
    for i in range(5):
        ids.append(
            _insert_closed(
                pnl=-1.5,
                exit_at=now_ts - 100 - i * 10,  # decrecientes
                status="closed_loss",
                condition_id=f"0xcid_{i}",
            )
        )

    assert risk.check_kill_switch() is True
    s = risk.kill_switch_status()
    assert s["active"] is True
    assert "consecutive_losses" in (s["reason"] or "")


def test_consecutive_losses_does_not_trigger_with_a_win_in_streak(
    isolated_db, now_ts
):
    """4 LOSS + 1 WIN intercalado → racha rota, no dispara consecutive."""
    # Streak: WIN, LOSS, LOSS, LOSS, LOSS (más reciente primero por exit_at)
    _insert_closed(pnl=+0.5, exit_at=now_ts - 50, status="closed_win")
    for i in range(4):
        _insert_closed(
            pnl=-1.5,
            exit_at=now_ts - 100 - i * 10,
            status="closed_loss",
            condition_id=f"0xcid_{i}",
        )
    assert risk.check_kill_switch() is False
    assert risk.kill_switch_status()["active"] is False


# ---------- Layer 3: drawdown ----------


def test_drawdown_triggers_when_balance_drops_below_peak_threshold(
    isolated_db, now_ts, monkeypatch
):
    """Peak persistido > current_balance × (1 - MAX_DRAWDOWN_PCT) → fire.

    Setup: peak forzado a $200 en bot_state. Capital efectivo (paper)=$100.
    Sin trades current=$100. dd = (100-200)/200 = -50% < -20% → triggers.
    Sin pérdidas registradas, ni legacy ni daily_loss_cap ni consecutive
    pueden disparar (PnL=$0, 0 cerrados) → solo drawdown.
    """
    # Aislar drawdown: legacy desactivado (threshold imposible).
    monkeypatch.setattr(risk, "DAILY_KILL_SWITCH_PCT", 9.99)

    _set_peak(200.0)

    assert risk.check_kill_switch() is True
    s = risk.kill_switch_status()
    assert s["active"] is True
    assert "drawdown" in (s["reason"] or "")


# ---------- reset_at high-water mark ----------


def test_reset_at_clears_consecutive_streak_and_does_not_reactivate(
    isolated_db, now_ts, monkeypatch
):
    """Tras reset manual, los 5 losses pre-reset NO vuelven a contar.

    Verifica el contrato del high-water mark `kill_switch_reset_at` en el
    layer consecutive (mismo patrón que el legacy ya tenía cubierto).
    """
    # Disable race-grace para test determinístico.
    monkeypatch.setattr(risk, "RESET_GRACE_SECONDS", 0)

    # 5 losses → consecutive_losses dispara.
    for i in range(5):
        _insert_closed(
            pnl=-1.5,
            exit_at=now_ts - 200 - i * 10,
            status="closed_loss",
            condition_id=f"0xcid_{i}",
        )
    assert risk.check_kill_switch() is True

    # Reset manual.
    risk.reset_kill_switch()
    assert risk.kill_switch_status()["active"] is False

    # Esperamos 1s para que reset_at sea estrictamente > exit_at de los trades viejos.
    time.sleep(1)

    # Los 5 losses viejos no deben reactivar el kill switch.
    assert risk.check_kill_switch() is False
    assert risk.kill_switch_status()["active"] is False


# ---------- Multi-layer ordering ----------


def test_multi_layer_ordering_consecutive_wins_over_drawdown(
    isolated_db, now_ts, monkeypatch
):
    """Cuando consecutive Y drawdown disparan a la vez, gana consecutive
    (es la primera en el for-loop de check_kill_switch).

    Setup:
      - 5 losses chicos (-$1 c/u) → racha LOSS=5 → consecutive trigger.
      - Total pnl = -$5, < cap diario $10 y legacy → no disparan.
      - peak forzado=$200, current=$100-$5=$95 → dd=-52% → drawdown trigger.
    Resultado esperado: reason persistido contiene 'consecutive_losses',
    NO 'drawdown'.
    """
    monkeypatch.setattr(risk, "DAILY_KILL_SWITCH_PCT", 9.99)

    for i in range(5):
        _insert_closed(
            pnl=-1.0,
            exit_at=now_ts - 100 - i * 10,
            status="closed_loss",
            condition_id=f"0xcid_{i}",
        )
    _set_peak(200.0)

    assert risk.check_kill_switch() is True
    reason = risk.kill_switch_status()["reason"] or ""
    assert "consecutive_losses" in reason, (
        f"esperado layer consecutive primero, reason={reason!r}"
    )
    # Sanity: 'drawdown' no debe estar en el reason persistido (la primera
    # capa que dispara cortocircuita el resto).
    assert "drawdown" not in reason
