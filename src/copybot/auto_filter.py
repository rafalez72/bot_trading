"""Auto-tuneo de los thresholds del selector basado en performance rolling.

Política simple y conservadora:
- Mira los últimos N closes de la tabla ACTIVA (paper en paper mode,
  live en live mode — incluye dry-run para que la validación previa
  a plata real refleje el mismo comportamiento que tendría real).
- Si win_rate < 0.40 → endurece filtros (sube MIN_WIN_RATE, MIN_VOLUME, MIN_TRADES)
- Si win_rate > 0.65 Y hay menos de 5 traders activos → afloja un poco
  (pero nunca por debajo del piso inicial)
- Mínimo MIN_SAMPLE_FOR_TUNE trades antes de mover thresholds (evita
  reaccionar a ruido estadístico de pocos trades).
- No corre más de una vez cada N horas.

Persistencia: filter_thresholds (key, value).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.copybot.tradebook import TABLE as TRADES_TABLE
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
MIN_SAMPLE_FOR_TUNE = 30  # mínimo de cierres antes de mover thresholds


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


def reset_to_defaults() -> dict[str, tuple[float, float]]:
    """Vuelve los thresholds a los valores DEFAULTS y resetea el cooldown.

    Útil cuando el auto-tune endureció con una muestra insuficiente y
    querés volver al baseline manualmente. Devuelve un dict
    `{key: (before, after)}` para auditar el cambio.
    """
    changes: dict[str, tuple[float, float]] = {}
    for key, default in DEFAULTS.items():
        before = _get_threshold(key)
        if abs(before - default) > 1e-9:
            changes[key] = (before, default)
            _set_threshold(key, default)
    # Resetear cooldown para que el próximo tune corra fresco
    with tx() as conn:
        conn.execute(
            "DELETE FROM bot_state WHERE key='auto_filter_last_run'"
        )
        conn.execute(
            """
            INSERT INTO learning_events
                (wallet, event_type, before_value, after_value, delta, trigger, metric_snapshot)
            VALUES ('(system)', 'auto_tune', NULL, NULL, NULL, ?, ?)
            """,
            ("Manual reset a defaults", str(changes)),
        )
    return changes


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
            f"""
            SELECT status, pnl_usdc FROM {TRADES_TABLE}
            WHERE status IN ('closed_win','closed_loss','settled_win','settled_loss')
            ORDER BY exit_at DESC LIMIT ?
            """,
            (WINDOW_TRADES,),
        ).fetchall()

    if len(rows) < MIN_SAMPLE_FOR_TUNE:
        _set_last_run()
        return {
            "changes": {},
            "reason": f"muestra insuficiente ({len(rows)} < {MIN_SAMPLE_FOR_TUNE})",
            "n": len(rows),
            "table": TRADES_TABLE,
        }

    wins = sum(1 for r in rows if r["status"].endswith("_win"))
    n = len(rows)
    wr = wins / n
    pnl = sum(r["pnl_usdc"] or 0 for r in rows)

    changes: dict[str, tuple[float, float]] = {}

    if wr < 0.40:
        # Endurecer.
        # 2026-05-08: techo MIN_WIN_RATE bajado de 0.70 → 0.65. Win-rate >0.65
        # es contraintuitivo en Polymarket (los mejores wallets están en
        # 55-65%); subirlo hasta 0.70 cerraba demasiado el grifo en mala
        # racha. Mantenemos el endurecimiento pero con un techo defensivo
        # más realista.
        new_wr = min(0.65, _get_threshold("MIN_WIN_RATE") + 0.05)
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
