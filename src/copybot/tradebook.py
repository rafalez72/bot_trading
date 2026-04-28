"""Dispatcher que elige paper o live execution según LIVE_MODE.

Permite que runner.py y risk.py importen funciones genéricas sin saber si
estamos en paper o en live. La decisión se toma una sola vez al import-time.

Uso:
    from src.copybot.tradebook import (
        open_position, close_position, force_close, settle_resolved
    )

Para alternar modo:
    LIVE_MODE=false   → paper (default)
    LIVE_MODE=true    → ejecuta órdenes reales
    LIVE_DRY_RUN=true + LIVE_MODE=true → ejecutor cargado pero sin mandar órdenes
"""
from __future__ import annotations

import logging

from src.config import LIVE_DRY_RUN, LIVE_MODE

log = logging.getLogger(__name__)

if LIVE_MODE:
    if LIVE_DRY_RUN:
        log.warning("=== TRADEBOOK: LIVE MODE + DRY RUN — órdenes simuladas ===")
    else:
        log.warning("=== TRADEBOOK: LIVE MODE — ÓRDENES REALES con USDC ===")
    from src.copybot.executor import (
        close_position,
        force_close,
        open_position,
        settle_resolved,
    )
    MODE = "live_dry" if LIVE_DRY_RUN else "live"
    TABLE = "live_trades"
else:
    log.info("tradebook: PAPER mode")
    from src.copybot.paper import (
        close_position,
        force_close,
        open_position,
        settle_resolved,
    )
    MODE = "paper"
    TABLE = "paper_trades"


__all__ = [
    "open_position", "close_position", "force_close", "settle_resolved",
    "MODE", "TABLE",
]
