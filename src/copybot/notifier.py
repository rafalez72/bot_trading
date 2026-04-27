"""Notificaciones a Telegram.

Pensado para ser **fire-and-forget**: jamás bloquea el flow del bot.
Si el envío falla, lo loguea y sigue. Si no hay credenciales, hace no-op.

Setup (una vez):
1. En Telegram, hablale a @BotFather → /newbot → seguís los pasos
2. Te da un TOKEN tipo `123456:ABC-DEF...`
3. Hablale a tu nuevo bot (mandale "/start" o cualquier mensaje)
4. Corré: `python copybot.py telegram-setup` — descubre el chat_id solo
5. Listo: copiar token+chat_id al .env y corré `python copybot.py telegram-test`

Tipos de eventos enviados:
  • Kill switch activado / desactivado
  • Trader droppeado por racha
  • Cluster bloqueado / penalizado
  • Stop-loss grande (>$3)
  • Take-profit grande (>$3)
  • Resumen diario (9am UTC)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Iterable

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
TIMEOUT = 5.0
MAX_LEN = 4000  # límite real es 4096, dejamos margen

# Tipos de notif activos. Para reactivar uno, agregalo a este set.
# Pedido del usuario: solo gain/loss + kill_switch (safety override).
ENABLED_NOTIFICATIONS = {"gain", "loss", "kill_switch"}


def _enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send(text: str, *, parse_mode: str = "Markdown", silent: bool = False) -> bool:
    """Manda un mensaje. Devuelve True si OK, False si falla o desactivado."""
    if not _enabled():
        return False
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 20] + "\n…(truncado)"
    try:
        r = httpx.post(
            f"{API_BASE}/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_notification": "true" if silent else "false",
                "disable_web_page_preview": "true",
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("telegram send %d: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("telegram send error: %s", e)
        return False


# ---------- Helpers de eventos específicos ----------

def gain(amount: float, accumulated: float) -> None:
    """Notif por cada trade cerrado con ganancia."""
    if "gain" not in ENABLED_NOTIFICATIONS:
        return
    send(
        f"📈 *Ganancia*\n"
        f"Ganó: ${amount:.2f}\n"
        f"Acumulado: ${accumulated:+.2f}"
    )


def loss(amount: float, accumulated: float) -> None:
    """Notif por cada trade cerrado con pérdida."""
    if "loss" not in ENABLED_NOTIFICATIONS:
        return
    send(
        f"📉 *Pérdida*\n"
        f"Perdió: ${amount:.2f}\n"
        f"Acumulado: ${accumulated:+.2f}"
    )


def kill_switch_activated(reason: str, pnl_24h: float) -> None:
    if "kill_switch" not in ENABLED_NOTIFICATIONS:
        return
    msg = (
        "⛔ *KILL SWITCH ACTIVADO*\n\n"
        f"PnL últimas 24h: *${pnl_24h:+.2f}*\n"
        f"Motivo: {reason}\n\n"
        "Bot pausado. Para resetear: `python copybot.py reset-killswitch`"
    )
    send(msg)


def kill_switch_deactivated() -> None:
    if "kill_switch" not in ENABLED_NOTIFICATIONS:
        return
    send("✅ Kill switch *desactivado*. Bot reanudado.", silent=True)


# Stubs desactivados (mantenidos para compat con call sites existentes)
def trader_dropped(*args, **kwargs) -> None:
    if "trader_dropped" in ENABLED_NOTIFICATIONS:
        # Implementación previa removida. Reactivar agregando al set arriba.
        pass


def cluster_blocked(*args, **kwargs) -> None:
    if "cluster_blocked" in ENABLED_NOTIFICATIONS:
        pass


def big_stop_loss(*args, **kwargs) -> None:
    if "big_stop_loss" in ENABLED_NOTIFICATIONS:
        pass


def big_take_profit(*args, **kwargs) -> None:
    if "big_take_profit" in ENABLED_NOTIFICATIONS:
        pass


def daily_summary(**kwargs) -> None:
    if "daily_summary" in ENABLED_NOTIFICATIONS:
        pass


def test_message() -> bool:
    return send(
        "✅ *Polymarket Copy Bot conectado*\n\n"
        "Vas a recibir alertas de:\n"
        "• ⛔ Kill switch\n"
        "• 🚫 Traders droppeados\n"
        "• 🚧 Clusters bloqueados\n"
        "• 📉 Stop-loss > $3\n"
        "• 📈 Take-profit > $3\n"
        "• 📊 Resumen diario\n\n"
        "Para silenciar (no borrar): `Configuración del chat → Silenciar`."
    )


# ---------- Setup helper ----------

def discover_chat_id() -> dict:
    """Lee getUpdates para encontrar el chat_id automáticamente.

    Útil en setup: el usuario manda /start al bot, después corre este helper
    y le decimos qué chat_id poner en .env.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN no está en .env"}
    try:
        r = httpx.get(f"{API_BASE}/bot{token}/getUpdates", timeout=TIMEOUT)
    except Exception as e:
        return {"ok": False, "error": f"red: {e}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    data = r.json()
    chats: list[dict] = []
    for u in data.get("result", []):
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats.append({
                "chat_id": chat["id"],
                "type": chat.get("type"),
                "title": chat.get("title") or chat.get("username") or chat.get("first_name"),
                "from_user": (msg.get("from") or {}).get("username"),
            })
    # Únicos por chat_id
    seen: set[int] = set()
    unique: list[dict] = []
    for c in chats:
        if c["chat_id"] not in seen:
            unique.append(c)
            seen.add(c["chat_id"])
    return {"ok": True, "chats": unique}
