"""Dynamic threshold overrides persistidos en bot_state.

Permite al asistente AI ajustar thresholds críticos (horizon, liquidez,
volumen) sin tocar el .env Lenovo. Los getters chequean bot_state primero;
si no hay override, retornan el default de src.config (que ya consulta env).

Patrón:
    from src.copybot.threshold_overrides import get_market_horizon_min_secs
    horizon = get_market_horizon_min_secs()  # DB → env → default

Set/clear via endpoint admin POST /api/admin/thresholds/<name>.
"""
from __future__ import annotations

import logging
from typing import Any

from src.db.schema import db, tx

log = logging.getLogger(__name__)

# Lista cerrada de overrides permitidos. Prevent injection arbitrario.
_ALLOWED = {
    "MARKET_HORIZON_MIN_SECS": float,
    "MIN_MARKET_LIQUIDITY_USDC": float,
    "MIN_MARKET_VOLUME_USDC": float,
}

_KEY_PREFIX = "threshold_override:"


def _read_db(name: str) -> str | None:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key=?",
            (f"{_KEY_PREFIX}{name}",),
        ).fetchone()
    return r["value"] if r else None


def _typed(name: str, value: str | None, fallback: Any) -> Any:
    if value is None:
        return fallback
    caster = _ALLOWED.get(name, str)
    try:
        return caster(value)
    except (TypeError, ValueError):
        log.warning("threshold_override: %s value=%r no parseable", name, value)
        return fallback


def get_market_horizon_min_secs() -> int:
    from src.config import MARKET_HORIZON_MIN_SECS
    return int(_typed("MARKET_HORIZON_MIN_SECS", _read_db("MARKET_HORIZON_MIN_SECS"), MARKET_HORIZON_MIN_SECS))


def get_min_market_liquidity_usdc() -> float:
    from src.config import MIN_MARKET_LIQUIDITY_USDC
    return float(_typed("MIN_MARKET_LIQUIDITY_USDC", _read_db("MIN_MARKET_LIQUIDITY_USDC"), MIN_MARKET_LIQUIDITY_USDC))


def get_min_market_volume_usdc() -> float:
    from src.config import MIN_MARKET_VOLUME_USDC
    return float(_typed("MIN_MARKET_VOLUME_USDC", _read_db("MIN_MARKET_VOLUME_USDC"), MIN_MARKET_VOLUME_USDC))


def set_override(name: str, value: float | None) -> dict:
    """Setea override. value=None limpia (vuelve a env/default)."""
    if name not in _ALLOWED:
        raise ValueError(f"threshold {name!r} no permitido. Allowed: {list(_ALLOWED.keys())}")
    full_key = f"{_KEY_PREFIX}{name}"
    with tx() as conn:
        if value is None:
            conn.execute("DELETE FROM bot_state WHERE key=?", (full_key,))
            return {"name": name, "value": None, "action": "cleared"}
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (full_key, str(value)),
        )
    return {"name": name, "value": value, "action": "set"}


def list_overrides() -> dict:
    """Snapshot de runtime values + sources para diagnóstico."""
    from src.config import (
        MARKET_HORIZON_MIN_SECS,
        MIN_MARKET_LIQUIDITY_USDC,
        MIN_MARKET_VOLUME_USDC,
    )
    result = {}
    for name, default in (
        ("MARKET_HORIZON_MIN_SECS", MARKET_HORIZON_MIN_SECS),
        ("MIN_MARKET_LIQUIDITY_USDC", MIN_MARKET_LIQUIDITY_USDC),
        ("MIN_MARKET_VOLUME_USDC", MIN_MARKET_VOLUME_USDC),
    ):
        db_val = _read_db(name)
        runtime = _typed(name, db_val, default)
        result[name] = {
            "runtime": runtime,
            "source": "db" if db_val is not None else "env_or_default",
            "config_value": default,
            "db_override": db_val,
        }
    return result
