"""Heartbeat + watchdog para el runner.

El runner async tiene un patrón conocido de cuelgues silenciosos:
- Tasks atoradas (asyncio deadlock) que no propagan exceptions
- Container "Up" pero sin actividad en logs
- Caso real 2026-05-06: 7 horas zombie post-rollback de WS, sin alertas

Solución de 2 capas:
1. **Heartbeat**: el runner escribe `bot_state.watchdog:last_cycle_ts` al
   final de cada cycle del loop principal.
2. **Watchdog en thread sistema** (NO asyncio task): chequea cada 60s.
   Si stale >5min → alerta Telegram. Si >10min → `os._exit(1)` forzado
   → docker `restart: unless-stopped` recrea el proceso fresh.

Por qué el watchdog corre en thread separado (threading.Thread) y no
asyncio task: si el async loop está bloqueado, ningún async task corre.
Un thread sistema sigue corriendo independiente del loop.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from src.config import DB_PATH

log = logging.getLogger(__name__)

HEARTBEAT_KEY = "watchdog:last_cycle_ts"  # legacy, mantenido por compat
ALERT_THRESHOLD_S = 300  # 5 min: alerta Telegram
KILL_THRESHOLD_S = 600   # 10 min: force exit → docker restart

# Heartbeat en archivo (no DB) — antes el record_heartbeat usaba tx() que
# bloqueaba si la DB estaba locked por HL/DX/server-1. Cuando la DB locked
# era exactamente el bug que el watchdog tenía que detectar, el heartbeat
# fallaba silenciosamente y el watchdog disparaba KILL acumulando false
# positives. Filesystem write es atómico (rename) y no compite con SQLite
# locks. Caso real 2026-05-06 12:34-12:44: 2 KILLs consecutivos con UN
# solo GET fetch entre cada restart.
HEARTBEAT_FILE = str(DB_PATH.parent / "heartbeat.ts")


def record_heartbeat() -> None:
    """Llamar al final/inicio de cada cycle. Atomic FS write — NO toca DB."""
    ts = int(time.time())
    try:
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(ts))
        os.replace(tmp, HEARTBEAT_FILE)  # atomic on POSIX
    except Exception as e:
        log.warning("heartbeat: no se pudo escribir: %s", e)


def get_last_heartbeat() -> int | None:
    """Devuelve unix ts del último heartbeat, o None si no existe."""
    try:
        with open(HEARTBEAT_FILE) as f:
            return int(f.read().strip())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("heartbeat read: %s", e)
        return None


def _watchdog_loop() -> None:
    """Thread sistema (daemon). Independiente del asyncio loop del runner.

    Polea cada 60s. Estados:
    - stale <300s: OK
    - 300-600s: ALERTA Telegram (debounced — 1 sola por episodio)
    - >600s: CRITICAL + os._exit(1). Docker restart policy lo recrea.

    Cuando recovers, manda notif "OK".
    """
    log.info(
        "watchdog: arrancado (alert=%ds, kill=%ds)",
        ALERT_THRESHOLD_S, KILL_THRESHOLD_S,
    )
    alerted = False
    # Espera 60s después del startup antes del primer check, para que
    # el runner alcance a hacer su primer cycle.
    time.sleep(60)
    while True:
        try:
            time.sleep(60)
            last = get_last_heartbeat()
            now_ts = int(time.time())
            if last is None:
                # No hay heartbeat aún — runner recién arrancó o nunca corrió
                stale = None
            else:
                stale = now_ts - last

            if stale is None:
                # Primer minuto sin heartbeat — esperar
                continue

            if stale > KILL_THRESHOLD_S:
                log.critical(
                    "WATCHDOG KILL: cycle stale %ds > %ds — forzando exit",
                    stale, KILL_THRESHOLD_S,
                )
                try:
                    from src.copybot.notifier import send
                    send(
                        f"💀 *WATCHDOG KILL*: runner stale {stale//60}min — "
                        f"forzando exit. Docker restart automático."
                    )
                except Exception:
                    pass
                # Brutal: termina PID 1 → docker restart policy recrea.
                os._exit(1)

            elif stale > ALERT_THRESHOLD_S:
                if not alerted:
                    log.error(
                        "WATCHDOG ALERT: cycle stale %ds — alertando",
                        stale,
                    )
                    try:
                        from src.copybot.notifier import send
                        send(
                            f"🚨 *WATCHDOG ALERT*: runner stale "
                            f"{stale//60}min. Si supera 10min auto-kill + "
                            f"docker restart."
                        )
                    except Exception as e:
                        log.warning("watchdog notif fail: %s", e)
                    alerted = True
            else:
                # stale OK
                if alerted:
                    log.info("WATCHDOG: recovered (stale=%ds)", stale)
                    try:
                        from src.copybot.notifier import send
                        send(
                            f"✅ *WATCHDOG*: runner recuperado "
                            f"(stale={stale}s)."
                        )
                    except Exception:
                        pass
                    alerted = False
        except Exception as e:
            # Que el watchdog NUNCA muera por exception
            log.exception("watchdog inner: %s", e)
            time.sleep(30)


def start_watchdog() -> None:
    """Spawn del thread daemon. Idempotente.

    IMPORTANTE: escribe un heartbeat fresco ANTES de spawn del thread.
    Si no, después de un docker restart el watchdog puede leer el heartbeat
    viejo del container muerto y disparar un kill inmediatamente, causando
    un loop infinito de restarts. Caso real 2026-05-06 11:00 UTC.
    """
    global _watchdog_started
    if _watchdog_started:
        log.warning("watchdog already started — noop")
        return
    # Heartbeat inicial fresco para evitar false-positive de un kill anterior
    record_heartbeat()
    log.info("watchdog: heartbeat inicial escrito (anti-falsa-stale post-restart)")
    t = threading.Thread(
        target=_watchdog_loop, name="bot-watchdog", daemon=True
    )
    t.start()
    _watchdog_started = True
    log.info("watchdog: thread spawned")
