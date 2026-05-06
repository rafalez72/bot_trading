"""Runner del paper-bot.

Loop principal:
- Cada COPY_POLL_SECONDS, para cada wallet `active` en copy_subscriptions:
  - Pide /trades?user=<wallet>&limit=100 (los más recientes)
  - Filtra trades con timestamp > cursor
  - Procesa en orden cronológico ascendente:
      side=BUY  → paper.open_position
      side=SELL → paper.close_position
  - Avanza el cursor al timestamp del último procesado.
- Cada N ciclos: settle_resolved() para liquidar mercados resueltos.

Cursor por wallet en `index_state` clave `paper_cursor:<wallet>`.
Primer arranque: cursor = "ahora" (no procesa retroactivos).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from rich.console import Console

from src.config import COPY_POLL_SECONDS, STOPLOSS_SWEEP_SECONDS
from src.copybot.tradebook import (
    MODE as TRADEBOOK_MODE,
    close_position,
    open_position,
    settle_resolved,
)
from src.copybot.auto_filter import maybe_tune
from src.copybot.clusters import update_cluster_perf
from src.copybot.discovery import run_cycle as discovery_cycle
from src.copybot.risk import check_kill_switch, sweep_stops
from src.db.schema import db, init_db, tx
from src.indexer.trades import _trade_id
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)
console = Console()

SETTLE_EVERY_N_CYCLES = max(1, 600 // max(COPY_POLL_SECONDS, 1))  # cada ~10 min


def _git_short_sha() -> str | None:
    """Devuelve el hash corto del commit actual si estamos en un repo git."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode().strip() or None
    except Exception:
        return None


def _get_cursor(wallet: str) -> int | None:
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM index_state WHERE key=?",
            (f"paper_cursor:{wallet}",),
        ).fetchone()
    return int(r["value"]) if r else None


def _set_cursor(wallet: str, ts: int) -> None:
    with tx() as conn:
        conn.execute(
            """
            INSERT INTO index_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=datetime('now')
            """,
            (f"paper_cursor:{wallet}", str(ts)),
        )


def _active_wallets() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT wallet FROM copy_subscriptions WHERE status='active'"
        ).fetchall()
    return [r["wallet"] for r in rows]


async def _process_wallet(client: PolymarketClient, wallet: str) -> tuple[int, int]:
    """Devuelve (trades_examinados, paper_trades_creados/cerrados)."""
    cursor = _get_cursor(wallet)
    if cursor is None:
        # Primer arranque: setear cursor a "ahora" para no procesar histórico
        cursor = int(time.time())
        _set_cursor(wallet, cursor)
        return 0, 0

    try:
        trades = await client.trades(user=wallet, limit=100, offset=0)
    except Exception as e:
        log.warning("polling %s falló: %s", wallet[:10], e)
        return 0, 0

    # Filtrar y ordenar ascendente
    new_trades = [
        t for t in trades
        if int(t.get("timestamp") or 0) > cursor
    ]
    new_trades.sort(key=lambda t: int(t.get("timestamp") or 0))

    actions = 0
    last_ts = cursor
    for t in new_trades:
        ts = int(t.get("timestamp") or 0)
        cid = t.get("conditionId")
        side = (t.get("side") or "").upper()
        oi = t.get("outcomeIndex")
        try:
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not cid or side not in ("BUY", "SELL"):
            last_ts = max(last_ts, ts)
            continue

        tid = _trade_id(t)
        if not tid:
            last_ts = max(last_ts, ts)
            continue

        try:
            if side == "BUY":
                pid, reason = open_position(
                    source_wallet=wallet,
                    source_trade_id=tid,
                    condition_id=cid,
                    outcome=t.get("outcome"),
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                    raw=t,
                )
                if pid:
                    actions += 1
                    log.info(
                        "OPEN  %s  cid=%s..  oi=%s  px=%.3f  → paper #%d",
                        wallet[:10], cid[:10], oi, price, pid,
                    )
                elif reason and reason not in ("duplicate",):
                    log.debug(
                        "skip OPEN  %s  cid=%s..  → %s",
                        wallet[:10], cid[:10], reason,
                    )
            else:  # SELL
                pid = close_position(
                    source_wallet=wallet,
                    condition_id=cid,
                    outcome_index=oi,
                    price=price,
                    timestamp=ts,
                )
                if pid:
                    actions += 1
                    log.info(
                        "CLOSE %s  cid=%s..  oi=%s  px=%.3f  → paper #%d",
                        wallet[:10], cid[:10], oi, price, pid,
                    )
        except Exception as e:
            log.exception("error procesando trade %s: %s", tid, e)

        last_ts = max(last_ts, ts)

    if last_ts > cursor:
        _set_cursor(wallet, last_ts)
    return len(new_trades), actions


def _maybe_send_daily_summary() -> None:
    """Si pasaron >=24h del último envío, manda resumen y graba ts."""
    import time as _t
    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='daily_summary_last'"
        ).fetchone()
    last = int(r["value"]) if r else 0
    now = int(_t.time())
    if now - last < 86400:
        return
    try:
        from src.copybot.learning import summary as ls
        from src.copybot.notifier import daily_summary
        s = ls()
        daily_summary(
            pnl_today=s["today"]["pnl_usdc"],
            wins=s["today"]["wins"],
            losses=s["today"]["losses"],
            open_positions=s["paper"]["open"],
            capital_used=s["capital"]["in_open_positions_usdc"],
            capital_total=s["capital"]["total_usdc"],
            active_traders=s["copying"]["active"],
            dropped_traders=s["copying"]["dropped"],
        )
        with tx() as conn:
            conn.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES ('daily_summary_last', ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now')
                """,
                (str(now),),
            )
    except Exception as e:
        log.warning("daily_summary failed: %s", e)


async def _maybe_trigger_on_demand_discovery(state: dict) -> None:
    """Si hay flag de discovery_pending y no hay otra discovery corriendo,
    dispara una en background (asyncio.create_task — no bloquea el loop).
    """
    task = state.get("discovery_task")
    if task and not task.done():
        return  # ya corre una

    with db() as conn:
        r = conn.execute(
            "SELECT value FROM bot_state WHERE key='discovery_pending'"
        ).fetchone()
    if not r or r["value"] != "true":
        return

    with tx() as conn:
        conn.execute(
            "UPDATE bot_state SET value='false', updated_at=datetime('now') "
            "WHERE key='discovery_pending'"
        )

    async def _run() -> None:
        try:
            res = await discovery_cycle(force=True)
            if res and not res.get("skipped"):
                console.print(
                    f"[bold cyan]on-demand discovery:[/bold cyan] "
                    f"+{res.get('discovered',0)} wallets · "
                    f"backfill {res.get('backfilled',0)} · "
                    f"compute {res.get('computed',0)}"
                )
        except Exception as e:
            log.exception("on-demand discovery failed: %s", e)

    state["discovery_task"] = asyncio.create_task(_run())


async def run_loop(*, once: bool = False) -> None:
    init_db()
    log.info("runner arranca en modo tradebook=%s", TRADEBOOK_MODE)
    if TRADEBOOK_MODE.startswith("live"):
        console.print(
            f"[bold red]⚠️  TRADEBOOK MODE: {TRADEBOOK_MODE.upper()} ⚠️[/bold red]\n"
            "[red]Las órdenes se mandan al CLOB de Polymarket. "
            "Esto consume USDC reales si dry_run=false.[/red]"
        )

    # Drenar outbox de live_trades antes de operar — si el proceso anterior
    # tuvo INSERTs que fallaron por DB locked, los recuperamos ahora.
    try:
        from src.copybot.executor import drain_live_outbox
        n_drained = drain_live_outbox()
        if n_drained:
            console.print(
                f"[yellow]outbox:[/yellow] {n_drained} live_trades drenados al startup"
            )
    except Exception as e:
        log.exception("drain_live_outbox falló al startup: %s", e)

    # Watchdog en thread sistema (independiente del asyncio loop). Detecta
    # cuelgues silenciosos del cycle principal y fuerza restart via docker
    # `restart: unless-stopped` cuando supera 10min sin heartbeat.
    # Caso real 2026-05-06: 7h de zombie post-cuelgue del WS.
    try:
        from src.copybot.health import start_watchdog
        start_watchdog()
    except Exception as e:
        log.exception("watchdog falló al arrancar: %s", e)

    # Instalar el handler de errores → Telegram (rate-limited)
    try:
        from src.copybot.notifier import install_error_handler, startup
        install_error_handler()
        # Notif de arranque (incluye commit si está disponible vía env)
        import os
        commit = os.getenv("GIT_COMMIT_SHORT") or _git_short_sha()
        startup(mode=TRADEBOOK_MODE, commit=commit)
    except Exception as e:
        log.warning("no se pudo instalar telegram error handler: %s", e)

    # Listener de comandos de Telegram (long polling, comandos /status, /killswitch).
    # Corre en background — no bloquea el loop principal.
    telegram_task: asyncio.Task | None = None
    if not once:
        try:
            from src.copybot import telegram_listener

            async def _telegram_supervisor():
                """Mantiene el listener vivo: si crashea con exception, restart con backoff.

                Antes (2026-05-05): si _drain_initial o el primer client.get
                tiraba exception, la task moría silenciosamente y el listener
                no respondía a comandos. asyncio.create_task no propaga
                excepciones de tareas terminadas a menos que las awaits.
                """
                while True:
                    try:
                        log.info("telegram supervisor: arrancando listener.run()")
                        await telegram_listener.run()
                        log.warning("telegram listener.run() terminó normal — restart en 5s")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.exception("telegram listener crashed: %s — restart en 10s", e)
                    await asyncio.sleep(10)

            telegram_task = asyncio.create_task(_telegram_supervisor())
        except Exception as e:
            log.warning("no se pudo arrancar telegram listener: %s", e)

    # Hyperliquid paralelo (dry-run). Si HL_MODE=true, arrancamos el runner HL
    # como task en paralelo. NO toca el loop principal del PM bot.
    hl_task: asyncio.Task | None = None
    if os.getenv("HL_MODE", "false").lower() == "true" and not once:
        try:
            from src.copybot.hl_runner import hl_run_loop
            hl_task = asyncio.create_task(hl_run_loop())
            log.info("HL runner: arrancado en paralelo (dry-run)")
        except Exception as e:
            log.warning("no se pudo arrancar HL runner: %s", e)

    # dYdX v4 paralelo (dry-run). Activado por DX_MODE=true.
    dx_task: asyncio.Task | None = None
    if os.getenv("DX_MODE", "false").lower() == "true" and not once:
        try:
            from src.copybot.dx_runner import dx_run_loop
            dx_task = asyncio.create_task(dx_run_loop())
            log.info("DX runner: arrancado en paralelo (dry-run)")
        except Exception as e:
            log.warning("no se pudo arrancar DX runner: %s", e)

    # WS bridge a Polymarket RTDS para reducir latencia de detección de trades
    # (de ~5-7s polling a <1s push). Complementa el polling — no lo reemplaza.
    # Idempotencia vía source_trade_id "ws:<txh>" en tradebook.open_position.
    ws_task: asyncio.Task | None = None
    if os.getenv("WEBSOCKET_TRADES_ENABLED", "false").lower() == "true" and not once:
        try:
            from src.copybot.ws_bridge import ws_run_loop
            ws_task = asyncio.create_task(ws_run_loop())
            log.info("WS bridge: arrancado en paralelo (latencia reducida)")
        except Exception as e:
            log.warning("no se pudo arrancar WS bridge: %s", e)

    cycle = 0
    last_sweep = 0.0
    sweep_period_cycles = max(1, STOPLOSS_SWEEP_SECONDS // max(COPY_POLL_SECONDS, 1))
    discovery_state: dict = {"discovery_task": None}
    async with PolymarketClient() as client:
        while True:
            cycle += 1
            wallets = _active_wallets()
            if not wallets:
                console.print(
                    "[yellow]No hay wallets activos. Corré `copybot select` primero.[/yellow]"
                )
                if once:
                    return
                await asyncio.sleep(COPY_POLL_SECONDS)
                continue

            t0 = time.time()
            # Polling paralelo: todos los wallets a la vez (httpx maneja concurrencia,
            # SQLite con WAL + asyncio single-thread tolera escrituras intercaladas).
            results = await asyncio.gather(
                *(_process_wallet(client, w) for w in wallets),
                return_exceptions=True,
            )
            total_examined = 0
            total_actions = 0
            for w, r in zip(wallets, results):
                if isinstance(r, Exception):
                    log.warning("wallet %s falló en gather: %s", w[:10], r)
                    continue
                ex, ac = r
                total_examined += ex
                total_actions += ac

            # Stop-loss sweep
            if cycle % sweep_period_cycles == 0:
                try:
                    sw = await sweep_stops()
                    if sw["stop_loss"] or sw["take_profit"]:
                        console.print(
                            f"[red]risk:[/red] checked={sw['checked']} "
                            f"SL={sw['stop_loss']} TP={sw['take_profit']}"
                        )
                except Exception as e:
                    log.exception("sweep_stops error: %s", e)

            # Kill switch
            try:
                if check_kill_switch():
                    console.print("[bold red]⛔ KILL SWITCH ACTIVO[/bold red] — no se abrirán nuevas posiciones")
            except Exception as e:
                log.exception("check_kill_switch error: %s", e)

            # On-demand discovery (chequea cada ciclo, dispara solo si hay flag)
            await _maybe_trigger_on_demand_discovery(discovery_state)

            # Cluster perf refresh (cada ~5 min) — barato, solo lee
            if cycle % max(1, 300 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    update_cluster_perf()
                except Exception as e:
                    log.exception("update_cluster_perf error: %s", e)

            # Reconciliador on-chain (cada ~5 min) — detecta trades fantasma
            # (BUYs ejecutados sin row en live_trades) y los trackea.
            # CRÍTICO: previene pérdidas como las del 2026-05-05.
            if cycle % max(1, 300 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.reconciler import reconcile_once
                    rec = reconcile_once()
                    if rec.get("inserted", 0) > 0:
                        console.print(
                            f"[yellow]reconciler:[/yellow] {rec['inserted']} trades "
                            f"fantasma encontrados on-chain y trackeados"
                        )
                except Exception as e:
                    log.exception("reconciler error: %s", e)

            # Phantom cleanup (cada ~30 min, solo en LIVE). Para markets negRisk
            # settle_resolved no dispara — se acumulan rows open ocupando cap.
            # Esta función consulta /positions del proxy y cierra como
            # closed_external los que ya no existen on-chain. Libera cap.
            if TRADEBOOK_MODE.startswith("live") and cycle % max(1, 1800 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.executor import cleanup_phantom_positions
                    n_phantom = cleanup_phantom_positions()
                    if n_phantom:
                        console.print(
                            f"[yellow]phantom cleanup:[/yellow] {n_phantom} "
                            f"live_trades cerrados (ya no existen on-chain)"
                        )
                except Exception as e:
                    log.exception("cleanup_phantom_positions error: %s", e)

            # Auto-drop wallets con reject_clog (cada ~10 min)
            if cycle % max(1, 600 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.learning import auto_drop_by_rejects
                    n = auto_drop_by_rejects()
                    if n:
                        console.print(
                            f"[red]auto-drop:[/red] {n} wallets droppeados por reject_clog"
                        )
                except Exception as e:
                    log.exception("auto_drop_by_rejects error: %s", e)

            # Auto-drop por inactividad on-chain (cada ~6h, threshold 48h)
            if cycle % max(1, 21600 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.learning import auto_drop_by_inactivity
                    n = auto_drop_by_inactivity()
                    if n:
                        console.print(
                            f"[red]auto-drop:[/red] {n} wallets droppeados por inactividad >48h"
                        )
                except Exception as e:
                    log.exception("auto_drop_by_inactivity error: %s", e)

            # Shadow tracker: pollea wallets dropped y registra su actividad
            # post-drop para análisis a posteriori (cada ~1h)
            if cycle % max(1, 3600 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.shadow_tracker import shadow_poll_dropped
                    await shadow_poll_dropped()
                except Exception as e:
                    log.exception("shadow_tracker error: %s", e)

            # Force discovery on idle: si 0 trades en últimas 6h, rascamos
            # discovery on-demand para refrescar wallets candidatos
            if cycle % max(1, 21600 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    with db() as _conn:
                        idle = _conn.execute(
                            f"SELECT COUNT(*) c FROM {TRADEBOOK_MODE.startswith('live') and 'live_trades' or 'paper_trades'} "
                            f"WHERE entry_at >= ?",
                            (int(time.time()) - 6 * 3600,),
                        ).fetchone()["c"]
                    if idle == 0:
                        log.info("0 trades en 6h — fuerzo discovery_pending")
                        with tx() as _conn:
                            _conn.execute(
                                "INSERT INTO bot_state (key, value, updated_at) "
                                "VALUES ('discovery_pending', 'true', datetime('now')) "
                                "ON CONFLICT(key) DO UPDATE SET value='true', updated_at=datetime('now')"
                            )
                except Exception as e:
                    log.exception("idle discovery trigger error: %s", e)

            # Recompute bandit sizings periódicamente (cada ~10 min).
            # Antes solo se llamaba on_close — wallets dormidos nunca veían el
            # inactivity decay aplicado. Ahora se rebalancea aunque no haya cierres.
            if cycle % max(1, 600 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.bandit import recompute_sizings
                    recompute_sizings()
                except Exception as e:
                    log.exception("recompute_sizings error: %s", e)

            # Health check de servicios upstream (CLOB/proxy + Data API).
            # Cada ~3 min. Tras 3 fallas consecutivas (~9 min) → alerta Telegram.
            if cycle % max(1, 180 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    from src.copybot.health_monitor import check_outages
                    from src.config import CLOB_API
                    await check_outages(CLOB_API)
                except Exception as e:
                    log.exception("health_monitor error: %s", e)

            # Auto-discovery (chequea internamente si pasaron 12h)
            if cycle % max(1, 3600 // max(COPY_POLL_SECONDS, 1)) == 0:
                try:
                    res = await discovery_cycle()
                    if res and not res.get("skipped"):
                        console.print(
                            f"[bold cyan]discovery:[/bold cyan] "
                            f"+{res.get('discovered',0)} wallets · "
                            f"backfill {res.get('backfilled',0)} · "
                            f"compute {res.get('computed',0)}"
                        )
                except Exception as e:
                    log.exception("discovery error: %s", e)

            # Daily summary (cada ciclo barato — el helper internamente verifica 24h)
            try:
                _maybe_send_daily_summary()
            except Exception as e:
                log.warning("daily_summary trigger failed: %s", e)

            # Settle de mercados resueltos
            if cycle % SETTLE_EVERY_N_CYCLES == 0:
                n_settled = settle_resolved()
                if n_settled:
                    console.print(f"[cyan]settle:[/cyan] {n_settled} paper_trades liquidados")
                # Auto-tune cada vez que evaluamos settlement (cada ~10 min)
                try:
                    res = maybe_tune()
                    if res and res.get("changes"):
                        console.print(
                            f"[magenta]auto-tune:[/magenta] win_rate "
                            f"{res['win_rate']*100:.0f}% sobre {res['n']} trades · "
                            f"{len(res['changes'])} thresholds ajustados"
                        )
                except Exception as e:
                    log.exception("auto-tune error: %s", e)

            dt = time.time() - t0
            if total_actions > 0 or cycle % 30 == 0:
                console.print(
                    f"[dim]cycle {cycle}[/dim]  "
                    f"wallets={len(wallets)} examined={total_examined} "
                    f"actions={total_actions} ({dt:.1f}s)"
                )

            # Heartbeat — el watchdog en thread sistema chequea esto cada 60s.
            # Si pasan >10min sin update, mata el proceso → docker restart.
            try:
                from src.copybot.health import record_heartbeat
                record_heartbeat()
            except Exception as e:
                log.warning("record_heartbeat: %s", e)

            if once:
                return
            sleep_for = max(1.0, COPY_POLL_SECONDS - dt)
            await asyncio.sleep(sleep_for)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )
    # Silenciar errores transitorios del SDK que van a Telegram como ERROR:
    #  - FAK no_match: la orden no encontró contraparte en el momento exacto (normal)
    #  - timeouts del proxy Vercel (transient HTTP)
    #  - 404 No orderbook (markets que cerraron)
    # Subimos el level del logger del SDK para que no escupan ERROR a stderr.
    logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(logging.CRITICAL)
    logging.getLogger("py_clob_client.http_helpers.helpers").setLevel(logging.CRITICAL)
    try:
        asyncio.run(run_loop())
    except KeyboardInterrupt:
        console.print("[yellow]Stopped[/yellow]")
