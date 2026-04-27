"""Auto-tuneo de los thresholds del selector basado en performance rolling.

Política simple y conservadora:
- Mira los últimos N closes del paper trading.
- Si win_rate < 0.40 → endurece filtros (sube MIN_WIN_RATE, MIN_VOLUME, MIN_TRADES)
- Si win_rate > 0.65 Y hay menos de 5 traders activos → afloja un poco
  (pero nunca por debajo del piso inicial)
- No corre más de una vez cada N horas.

Persistencia: filter_thresholds (key, value).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.db.schema import db, tx

log = logging.getLogger(__name__)

# Pisos absolutos: el auto-tuneo nunca va por debajo de estos
FLOORS: dict[str, float] = {
    "MIN_SCORE":         0.45,
    "MIN_PNL":           300.0,
    "MIN_WIN_RATE":      0.50,
    "MIN_TOTAL_TRADES":  100.0,
    "MIN_VOLUME":        15_000.0,
    "MAX_DRAWDOWN_PCT":  60.0,
    "MIN_SHARPE":        0.30,
}

# Defaults iniciales
DEFAULTS: dict[str, float] = {
    "MIN_SCORE":         0.55,
    "MIN_PNL":           500.0,
    "MIN_WIN_RATE":      0.55,
    "MIN_TOTAL_TRADES":  150.0,
    "MIN_VOLUME":        25_000.0,
    "MAX_DRAWDOWN_PCT":  50.0,
    "MIN_SHARPE":        0.40,
}

CHECK_EVERY_HOURS = 6
WINDOW_TRADES = 100


def _get_threshold(key: str) -> float:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM filter_thresholds WHERE key=?", (key,)
        ).fetchone()
    return float(r["value"]) if r else DEFAULTS[key]


def _set_threshold(key: str, value: float) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO filter_thresholds (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (key, value),
        )


def get_all() -> dict[str, float]:
    return {k: _get_threshold(k) for k in DEFAULTS}


def _clamp(key: str, value: float, *, floor_only: bool = False) -> float:
    floor = FLOORS[key]
    if floor_only:
        return max(value, floor)
    # MAX_DRAWDOWN va al revés (más alto = más permisivo)
    if key == "MAX_DRAWDOWN_PCT":
        return min(value, floor)  # nunca aceptar dd más permisivo que el piso
    return max(value, floor)


def _last_run_ts() -> int:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='auto_filter_last_run'"
        ).fetchone()
    return int(r["value"]) if r else 0


def _set_last_run() -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES ('auto_filter_last_run', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (str(int(time.time())),),
        )


def maybe_tune(*, force: bool = False) -> dict[str, Any] | None:
    """Si pasaron CHECK_EVERY_HOURS y hay data, recalcula thresholds.

    Devuelve un dict con { changes, win_rate, n } o None si no corrió.
    """
    if not force and (time.time() - _last_run_ts()) < CHECK_EVERY_HOURS * 3600:
        return None

    with db() as conn:
        rows = conn.execute(
            """
            SELECT status, pnl_usdc FROM paper_trades
            WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')
            ORDER BY exit_at DESC LIMIT ?
            """,
            (WINDOW_TRADES,),
        ).fetchall()

    if len(rows) < 20:
        _set_last_run()
        return {"changes": {}, "reason": "muy poca data", "n": len(rows)}

    wins = sum(1 for r in rows if r["status"].endswith("_win"))
    n = len(rows)
    wr = wins / n
    pnl = sum(r["pnl_usdc"] or 0 for r in rows)

    changes: dict[str, tuple[float, float]] = {}

    if wr < 0.40:
        # Endurecer
        new_wr = min(0.70, _get_threshold("MIN_WIN_RATE") + 0.05)
        new_vol = _get_threshold("MIN_VOLUME") * 1.2
        new_trades = _get_threshold("MIN_TOTAL_TRADES") * 1.15
        new_score = min(0.85, _get_threshold("MIN_SCORE") + 0.05)
        changes["MIN_WIN_RATE"] = (_get_threshold("MIN_WIN_RATE"), new_wr)
        changes["MIN_VOLUME"] = (_get_threshold("MIN_VOLUME"), new_vol)
        changes["MIN_TOTAL_TRADES"] = (_get_threshold("MIN_TOTAL_TRADES"), new_trades)
        changes["MIN_SCORE"] = (_get_threshold("MIN_SCORE"), new_score)
        _set_threshold("MIN_WIN_RATE", new_wr)
        _set_threshold("MIN_VOLUME", new_vol)
        _set_threshold("MIN_TOTAL_TRADES", new_trades)
        _set_threshold("MIN_SCORE", new_score)
    elif wr > 0.65:
        # Aflojar (sólo si hay pocos activos)
        with db() as conn:
            n_active = conn.execute(
                "SELECT COUNT(*) c FROM copy_subscriptions WHERE status='active'"
            ).fetchone()["c"]
        if n_active < 5:
            new_wr = _clamp("MIN_WIN_RATE", _get_threshold("MIN_WIN_RATE") - 0.03)
            new_vol = _clamp("MIN_VOLUME", _get_threshold("MIN_VOLUME") * 0.85)
            changes["MIN_WIN_RATE"] = (_get_threshold("MIN_WIN_RATE"), new_wr)
            changes["MIN_VOLUME"] = (_get_threshold("MIN_VOLUME"), new_vol)
            _set_threshold("MIN_WIN_RATE", new_wr)
            _set_threshold("MIN_VOLUME", new_vol)

    _set_last_run()

    if changes:
        with tx() as conn:
            conn.execute(
                """
                INSERT INTO learning_events
                    (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
                VALUES ('(system)', 'auto_tune', NULL, NULL, NULL, ?, ?)
                """,
                (
                    f"Auto-tune por win_rate {wr*100:.0f}% en últimos {n} trades",
                    str(changes),
                ),
            )

    return {"changes": changes, "win_rate": wr, "pnl": pnl, "n": n}
