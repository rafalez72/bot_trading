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
# Live notifs (live_open, live_close) están activas por default en LIVE_MODE
# para que el usuario sepa SIEMPRE qué pasa con su plata real.
# log_error: errores ERROR+ del logger se mandan a Telegram con rate-limit.
# startup: aviso cuando el runner arranca (post-restart).
ENABLED_NOTIFICATIONS = {
    "gain", "loss", "kill_switch",
    "live_open", "live_close", "live_error",
    "log_error", "startup",
}


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


# ---------- Live trading (Fase 5) ----------

def live_open(*, source_wallet: str, market_slug: str | None,
              size_usdc: float, price: float, order_id: str | None,
              tx_hash: str | None = None, dry_run: bool = False) -> None:
    """Notif al abrir una posición real."""
    if "live_open" not in ENABLED_NOTIFICATIONS:
        return
    prefix = "🧪 *DRY-RUN* " if dry_run else "💰 *LIVE OPEN*"
    txt = (
        f"{prefix}\n"
        f"Trader: `{source_wallet[:12]}...`\n"
        f"Mercado: {market_slug or '(sin slug)'}\n"
        f"Size: ${size_usdc:.2f}  @  {price:.3f}\n"
    )
    if order_id:
        txt += f"Orden: `{order_id[:20]}`\n"
    if tx_hash:
        txt += f"[Ver tx](https://polygonscan.com/tx/{tx_hash})\n"
    send(txt)


def live_close(*, source_wallet: str, market_slug: str | None,
               pnl_usdc: float, accumulated: float, exit_reason: str,
               tx_hash: str | None = None, dry_run: bool = False) -> None:
    """Notif al cerrar una posición real."""
    if "live_close" not in ENABLED_NOTIFICATIONS:
        return
    prefix = "🧪 *DRY-RUN* " if dry_run else ("💵" if pnl_usdc > 0 else "🩸")
    sign = "+" if pnl_usdc > 0 else ""
    txt = (
        f"{prefix} *LIVE CLOSE*\n"
        f"Trader: `{source_wallet[:12]}...`\n"
        f"Mercado: {market_slug or '(sin slug)'}\n"
        f"PnL: *{sign}${pnl_usdc:.2f}*  (motivo: {exit_reason})\n"
        f"Acumulado live: ${accumulated:+.2f}"
    )
    if tx_hash:
        txt += f"\n[Ver tx](https://polygonscan.com/tx/{tx_hash})"
    send(txt)


def live_error(*, stage: str, error: str) -> None:
    """Notif cuando algo falla en live (CLOB caído, balance bajo, etc.)."""
    if "live_error" not in ENABLED_NOTIFICATIONS:
        return
    send(
        f"⚠️ *LIVE ERROR*\n"
        f"Stage: `{stage}`\n"
        f"Error: {error[:300]}"
    )


# ---------- Startup + error log forwarder ----------

def startup(*, mode: str, commit: str | None = None) -> None:
    """Notif cuando el runner arranca (post-restart o boot)."""
    if "startup" not in ENABLED_NOTIFICATIONS:
        return
    txt = f"🤖 *Bot iniciado* — modo `{mode}`"
    if commit:
        txt += f"\nCommit: `{commit}`"
    send(txt, silent=True)


# Rate limiter en memoria: (key) -> last_sent_unix.
# Evita spam si se repite el mismo error N veces seguidas.
_THROTTLE_WINDOW_SEC = 300  # 5 min
_last_sent: dict[str, float] = {}


def _throttle_key(record_msg: str, logger_name: str) -> str:
    """Key estable: primeros 80 chars del msg + nombre del logger."""
    return f"{logger_name}:{record_msg[:80]}"


class TelegramErrorHandler(logging.Handler):
    """Logging handler que manda ERROR+ a Telegram con rate-limit.

    Se instala una vez en el runner (no en imports de módulos), para evitar
    duplicados. Rate-limita por (logger_name, mensaje_truncado): mismo error
    repetido en <5 min no genera más notifs.
    """

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        if "log_error" not in ENABLED_NOTIFICATIONS:
            return
        # Filtros: ignorar warnings frecuentes que no son críticos
        msg = record.getMessage()
        if any(skip in msg.lower() for skip in (
            "polling", "name or service not known", "timeout",
        )):
            return

        import time
        key = _throttle_key(msg, record.name)
        now = time.time()
        last = _last_sent.get(key, 0)
        if now - last < _THROTTLE_WINDOW_SEC:
            return
        _last_sent[key] = now

        # Texto: nivel + logger + mensaje + traceback si hay
        level = record.levelname
        body = (
            f"🚨 *Error en bot*\n"
            f"Nivel: `{level}`\n"
            f"Origen: `{record.name}`\n"
            f"```\n{msg[:600]}\n```"
        )
        if record.exc_info:
            import traceback
            tb = "".join(traceback.format_exception(*record.exc_info))
            body += f"\n```\n{tb[-400:]}\n```"
        try:
            send(body)
        except Exception:
            pass  # nunca dejamos que un fallo de Telegram rompa el logger


def install_error_handler(level: int = logging.ERROR) -> TelegramErrorHandler:
    """Instala el handler en el root logger. Idempotente.

    Llamar una vez al inicio del runner. Devuelve el handler instalado
    (útil si después querés cambiar el nivel o desinstalarlo).
    """
    root = logging.getLogger()
    # Si ya hay uno instalado, lo reusamos
    for h in root.handlers:
        if isinstance(h, TelegramErrorHandler):
            return h
    handler = TelegramErrorHandler(level=level)
    root.addHandler(handler)
    return handler


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
