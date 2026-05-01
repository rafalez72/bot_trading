"""Monitor de salud de servicios upstream — Polymarket + Vercel proxy.

Detecta caídas sostenidas y alerta vía Telegram. Evita falsos positivos
acumulando N fallas consecutivas antes de disparar.

Servicios chequeados:
  1. CLOB / proxy (lo que use el bot, sea direct o vía Vercel)
  2. Polymarket Data API (`/trades`) — público, NO proxy

Ambos checkeados con HEAD/GET simple. Aceptamos cualquier 2xx-4xx como
"servicio responde" (auth-required 401 es señal de vida, no de caída).
Solo 5xx, timeouts y connection errors cuentan como falla.

Threshold default: 3 fallas consecutivas → alerta. Con check cada ~3min,
da una latencia de ~9min entre la caída real y la alerta. Razonable
para evitar ruido pero capturar problemas reales rápido.

Recovery: cuando vuelve OK, alerta de recuperación. Reset state.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

# Estado por servicio: failures consecutivas + flag de "ya alerté"
_state: dict[str, dict] = {
    "clob_proxy": {"failures": 0, "alerted": False, "last_check": 0},
    "data_api":   {"failures": 0, "alerted": False, "last_check": 0},
}

ALERT_AFTER_FAILURES = 3   # 3 chequeos consecutivos fallados → alerta
CHECK_TIMEOUT = 10.0


async def _ping(url: str) -> tuple[bool, str | int]:
    """Hace GET al URL. Retorna (ok, info).

    OK = HTTP < 500 (servicio responde aunque sea 4xx por auth).
    NO OK = 5xx, timeout, conn error.
    """
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            r = await client.get(url)
            if r.status_code < 500:
                return True, r.status_code
            return False, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        return False, "timeout"
    except httpx.RequestError as e:
        return False, f"conn_error: {str(e)[:80]}"
    except Exception as e:
        return False, f"error: {str(e)[:80]}"


async def _check_one(service_key: str, label: str, target: str) -> None:
    """Checkea un servicio, gestiona state y alertas."""
    ok, info = await _ping(target)
    s = _state[service_key]
    s["last_check"] = int(time.time())

    if ok:
        # Servicio OK
        if s["alerted"]:
            # Recovery: estaba caído, ahora vuelve
            try:
                from src.copybot.notifier import recovery_alert
                recovery_alert(label, target)
            except Exception as e:
                log.warning("recovery_alert failed: %s", e)
            log.info("health: %s recovered (was alerted, now %s)", label, info)
        s["failures"] = 0
        s["alerted"] = False
    else:
        # Falla
        s["failures"] += 1
        log.warning("health: %s FAIL #%d (%s)", label, s["failures"], info)
        if s["failures"] >= ALERT_AFTER_FAILURES and not s["alerted"]:
            try:
                from src.copybot.notifier import outage_alert
                outage_alert(label, target, info)
            except Exception as e:
                log.warning("outage_alert failed: %s", e)
            s["alerted"] = True


async def check_outages(clob_api: str) -> None:
    """Checkea CLOB (vía proxy si aplica) + Data API. Llamar periódicamente."""
    # CLOB / proxy: hit el root para ver si responde
    clob_target = clob_api.rstrip("/")
    await _check_one("clob_proxy", "CLOB/proxy", clob_target + "/")

    # Data API: endpoint público de trades
    await _check_one("data_api", "Polymarket Data API",
                     "https://data-api.polymarket.com/trades?limit=1")


def status() -> dict:
    """Snapshot del estado de salud (para debugging)."""
    return {k: dict(v) for k, v in _state.items()}
