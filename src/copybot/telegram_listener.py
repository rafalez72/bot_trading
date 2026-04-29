"""Listener de comandos de Telegram (long polling).

Corre como tarea async dentro del runner. Solo responde al chat_id
configurado en TELEGRAM_CHAT_ID — mensajes de otros chats se ignoran.

Comandos soportados:
  /start          → ayuda
  /help           → lista comandos
  /status         → estado breve (modo, kill_switch, PnL 24h, wallets)
  /killswitch     → desactiva el kill_switch
  /resetkill      → alias de /killswitch

Notas:
- Usa long polling con timeout 25s, así no consume CPU en idle.
- Si el bot recibe muchos updates al arrancar (mensajes viejos),
  los descarta moviendo el offset al último.
- Errores de red se silencian con backoff incremental para no
  spamear el log.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_SEC = 25  # long-polling
HTTP_TIMEOUT = POLL_TIMEOUT_SEC + 10
BACKOFF_MAX_SEC = 60.0


def _enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def _authorized_chat_id() -> int | None:
    raw = os.getenv("TELEGRAM_CHAT_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _send_reply(text: str, *, parse_mode: str = "Markdown") -> None:
    try:
        from src.copybot.notifier import send
        send(text, parse_mode=parse_mode, silent=True)
    except Exception as e:
        log.warning("telegram reply send failed: %s", e)


def _format_status() -> str:
    """Construye el body de /status. Lazy imports para no acoplar."""
    from src.copybot.risk import kill_switch_status
    from src.copybot.tradebook import MODE as TBM, TABLE as TBT
    from src.db.schema import db

    ks = kill_switch_status()
    with db() as conn:
        n_active = conn.execute(
            "SELECT COUNT(*) c FROM copy_subscriptions WHERE status='active'"
        ).fetchone()["c"]
        last24 = int(time.time()) - 86400
        pnl_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(pnl_usdc),0) pnl, COUNT(*) n
            FROM {TBT}
            WHERE exit_at >= ?
              AND status IN ('closed_win','closed_loss','settled_win','settled_loss')
            """,
            (last24,),
        ).fetchone()
        opens = conn.execute(
            f"SELECT COUNT(*) c FROM {TBT} WHERE status='open'"
        ).fetchone()["c"]

    ks_line = (
        f"⛔ ACTIVO — {ks.get('reason') or 'sin motivo'}"
        if ks.get("active")
        else "✅ inactivo"
    )
    return (
        "*Estado del bot*\n"
        f"Modo: `{TBM}` (tabla `{TBT}`)\n"
        f"Kill switch: {ks_line}\n"
        f"Wallets activos: *{n_active}*\n"
        f"Posiciones abiertas: *{opens}*\n"
        f"PnL 24h: *${(pnl_row['pnl'] or 0):+.2f}* en {pnl_row['n']} cierres"
    )


def _help_text() -> str:
    return (
        "*Comandos disponibles*\n"
        "/status — estado breve del bot\n"
        "/killswitch — desactiva el kill switch\n"
        "/resetkill — alias de /killswitch\n"
        "/help — esta ayuda"
    )


def _handle_command(cmd: str) -> str | None:
    """Devuelve el texto a responder, o None si no se reconoce."""
    norm = cmd.strip().lower().split("@", 1)[0]  # /cmd@bot → /cmd
    if norm in ("/start", "/help"):
        return _help_text()
    if norm == "/status":
        try:
            return _format_status()
        except Exception as e:
            log.exception("status failed: %s", e)
            return f"⚠️ Error al leer estado: `{str(e)[:200]}`"
    if norm in ("/killswitch", "/resetkill"):
        try:
            from src.copybot.risk import kill_switch_status, reset_kill_switch
            ks = kill_switch_status()
            if not ks.get("active"):
                return "ℹ️ Kill switch ya estaba inactivo. Nada que hacer."
            reset_kill_switch()
            return "✅ Kill switch *desactivado*. Bot reanudado."
        except Exception as e:
            log.exception("reset kill failed: %s", e)
            return f"⚠️ No se pudo resetear: `{str(e)[:200]}`"
    return None  # comando no reconocido — no respondemos para evitar ruido


async def _process_update(u: dict[str, Any], allowed_chat: int) -> None:
    msg = u.get("message") or u.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    if chat.get("id") != allowed_chat:
        log.debug("ignored telegram msg from chat_id=%s", chat.get("id"))
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    reply = _handle_command(text)
    if reply:
        _send_reply(reply)


async def _drain_initial(client: httpx.AsyncClient, token: str) -> int:
    """Consume updates pendientes al arrancar (mensajes viejos del bot).

    Devuelve el offset desde donde escuchar nuevo tráfico.
    """
    try:
        r = await client.get(
            f"{API_BASE}/bot{token}/getUpdates",
            params={"timeout": 0, "limit": 100},
            timeout=10.0,
        )
        if r.status_code != 200:
            return 0
        result = r.json().get("result") or []
        if not result:
            return 0
        last_id = max(int(u["update_id"]) for u in result)
        # ack: pasar offset = last_id+1 para que Telegram los olvide
        await client.get(
            f"{API_BASE}/bot{token}/getUpdates",
            params={"offset": last_id + 1, "timeout": 0, "limit": 1},
            timeout=10.0,
        )
        log.info("telegram listener: drained %d pending updates", len(result))
        return last_id + 1
    except Exception as e:
        log.warning("telegram initial drain failed: %s", e)
        return 0


async def run() -> None:
    """Long-polling loop. Termina solo si el task se cancela."""
    if not _enabled():
        log.info("telegram listener disabled (no TOKEN/CHAT_ID)")
        return

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    allowed = _authorized_chat_id()
    if allowed is None:
        log.warning("TELEGRAM_CHAT_ID inválido, listener no arranca")
        return

    log.info("telegram listener: arrancando (chat_id autorizado=%s)", allowed)

    backoff = 1.0
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        offset = await _drain_initial(client, token)
        while True:
            try:
                r = await client.get(
                    f"{API_BASE}/bot{token}/getUpdates",
                    params={
                        "timeout": POLL_TIMEOUT_SEC,
                        "offset": offset,
                        "allowed_updates": '["message"]',
                    },
                )
                if r.status_code != 200:
                    log.warning("telegram getUpdates %d: %s",
                                r.status_code, r.text[:200])
                    await asyncio.sleep(backoff)
                    backoff = min(BACKOFF_MAX_SEC, backoff * 2)
                    continue
                backoff = 1.0
                updates = r.json().get("result") or []
                for u in updates:
                    try:
                        await _process_update(u, allowed)
                    except Exception as e:
                        log.exception("telegram update handler failed: %s", e)
                    offset = max(offset, int(u["update_id"]) + 1)
            except asyncio.CancelledError:
                log.info("telegram listener: cancelado")
                raise
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                log.debug("telegram poll error (network): %s", e)
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_SEC, backoff * 2)
            except Exception as e:
                log.exception("telegram listener loop error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(BACKOFF_MAX_SEC, backoff * 2)
