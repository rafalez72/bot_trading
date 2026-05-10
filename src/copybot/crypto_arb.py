"""Crypto temporal arbitrage — explota el lag del precio de Polymarket vs spot.

Estrategia (Nivel 2, 2026-05-08):
- Polymarket lista mercados ``btc-updown-5m-{epoch}``, ``eth-updown-5m-*``,
  ``sol-updown-5m-*`` que resuelven cada 5 min sobre si el precio cierra
  Up o Down respecto al inicio del bucket.
- El midpoint en Polymarket reacciona ~5-30s después del movimiento real
  en Binance (latencia + thin orderbook + dependencia de market makers).
- En la ventana de últimos 30-60s pre-close, comparamos:
  - Spot Binance ahora vs spot al inicio del bucket de 5min
  - Si spot subió >0.3% (umbral configurable) → comprar YES (Up)
  - Si spot bajó >0.3% → comprar NO (Down)
  - Solo si midpoint Polymarket cotiza el lado "correcto" debajo del
    UMBRAL_TARGET (no overbought) — sino el edge ya está priced in.

Riesgos conocidos:
- Tendencia falsa (subió 0.3%, después cae antes del close).
- Slippage en thin orderbook (mercados de 5min suelen tener <$5k liquidity).
- Resolution puede demorar minutos en setearse — la posición queda
  ``status='open'`` hasta que el polling de markets activos detecte el
  cierre.

Defensas:
- Cap por trade (default $5 USDC paper).
- Solo trades con edge claro (spot move > THRESHOLD_PCT, midpoint en zona).
- Loggea TODOS los matches/skips para análisis post-mortem.
- Fail-open: si Binance WS desconecta, no abrir nada (precios stale).

Activación: env var ``CRYPTO_ARB_ENABLED=true``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time

import httpx
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.binance.websocket import BinanceTickerWS
from src.copybot.crypto_arb_signals import edge_vs_mid, get_min_edge
from src.copybot.tradebook import MODE as TRADEBOOK_MODE, open_position
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)

# Snapshot path para que /api/crypto-arb-status (corre en server, otro
# container) pueda leer las métricas reales del runner. Mismo patrón que
# ws_metrics.py — atómico vía rename, en data/ que es shared volume.
_SNAPSHOT_PATH = Path(os.getenv("DB_PATH", "data/copybot.db")).parent / "crypto_arb_metrics.json"

# Símbolos que tradeamos. Mapping a los slug-prefixes de Polymarket.
# HYPE no está en Binance Spot (verificado vía exchangeInfo), así que
# queda excluido aunque Polymarket sí liste hype-updown-5m-*.
SYMBOL_TO_SLUG_PREFIX = {
    "BTCUSDT": "btc-updown-5m-",
    "ETHUSDT": "eth-updown-5m-",
    "SOLUSDT": "sol-updown-5m-",
    "XRPUSDT": "xrp-updown-5m-",
    "BNBUSDT": "bnb-updown-5m-",
    "DOGEUSDT": "doge-updown-5m-",
}
SLUG_PREFIX_TO_SYMBOL = {v: k for k, v in SYMBOL_TO_SLUG_PREFIX.items()}
SLUG_REGEX = re.compile(r"^(btc|eth|sol|xrp|bnb|hype|doge)-updown-5m-(\d+)$")

# --- Parámetros configurables vía env ---
DEFAULT_CHECK_INTERVAL_S = 15.0
DEFAULT_PRE_CLOSE_WINDOW_S = 300.0  # solo evaluar markets que cierran en próximos 300s
# 2026-05-10: subido 180 → 300. UpDown 5min: bot solo veía últimos 3min (ya priced-in).
# 5min completo = más oportunidades capture señal antes que mercado converja.
DEFAULT_MOMENTUM_THRESHOLD_PCT = 0.3
DEFAULT_MAX_MID_TARGET = 0.70       # midpoint del lado a comprar debe ser <0.70
DEFAULT_BET_SIZE_USDC = 5.0
DEFAULT_MIN_BUCKET_AGE_S = 60.0     # bucket abierto hace al menos 60s (necesario para medir momentum)


@dataclass
class CryptoArbConfig:
    enabled: bool = False
    check_interval_s: float = DEFAULT_CHECK_INTERVAL_S
    pre_close_window_s: float = DEFAULT_PRE_CLOSE_WINDOW_S
    momentum_threshold_pct: float = DEFAULT_MOMENTUM_THRESHOLD_PCT
    max_mid_target: float = DEFAULT_MAX_MID_TARGET
    bet_size_usdc: float = DEFAULT_BET_SIZE_USDC
    min_bucket_age_s: float = DEFAULT_MIN_BUCKET_AGE_S
    # Activación explícita en LIVE_MODE. Default false: los markets crypto-updown-5m
    # tienen orderbooks thin ($1-5k liquidez), un BUY de $5-10 = 0.5-1% del book →
    # slippage 50-90% en live → pierde plata garantizado. Validar 24h en paper antes
    # de poner CRYPTO_ARB_ALLOW_LIVE=true.
    allow_live: bool = False
    # Símbolos activos
    symbols: tuple[str, ...] = field(
        default_factory=lambda: tuple(SYMBOL_TO_SLUG_PREFIX.keys())
    )

    @classmethod
    def from_env(cls) -> "CryptoArbConfig":
        return cls(
            enabled=os.getenv("CRYPTO_ARB_ENABLED", "false").lower() == "true",
            check_interval_s=float(os.getenv("CRYPTO_ARB_CHECK_INTERVAL_S", DEFAULT_CHECK_INTERVAL_S)),
            pre_close_window_s=float(os.getenv("CRYPTO_ARB_PRE_CLOSE_WINDOW_S", DEFAULT_PRE_CLOSE_WINDOW_S)),
            momentum_threshold_pct=float(os.getenv("CRYPTO_ARB_MOMENTUM_THRESHOLD_PCT", DEFAULT_MOMENTUM_THRESHOLD_PCT)),
            max_mid_target=float(os.getenv("CRYPTO_ARB_MAX_MID_TARGET", DEFAULT_MAX_MID_TARGET)),
            bet_size_usdc=float(os.getenv("CRYPTO_ARB_BET_SIZE_USDC", DEFAULT_BET_SIZE_USDC)),
            allow_live=os.getenv("CRYPTO_ARB_ALLOW_LIVE", "false").lower() == "true",
        )


# --- Métricas in-memory (para /api/crypto-arb-status si se agrega) ---
@dataclass
class _Metrics:
    cycles: int = 0
    markets_seen: int = 0
    markets_in_window: int = 0
    skipped_no_spot: int = 0
    skipped_low_edge: int = 0          # nuevo (edge-based): reemplaza skipped_low_momentum
    skipped_slope_disagrees: int = 0   # slope de últimos 30s contradice el side
    skipped_overbought: int = 0
    skipped_too_young: int = 0
    skipped_open_failed: int = 0
    opens_yes: int = 0
    opens_no: int = 0
    last_cycle_at: float | None = None
    last_open_at: float | None = None
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        now = time.time()
        # ``low_momentum`` se mantiene como alias deprecated de ``low_edge``
        # para no romper consumidores existentes del snapshot file.
        return {
            "uptime_s": round(now - self.started_at, 1),
            "cycles": self.cycles,
            "markets_seen": self.markets_seen,
            "markets_in_window": self.markets_in_window,
            "skipped": {
                "no_spot": self.skipped_no_spot,
                "low_edge": self.skipped_low_edge,
                "low_momentum": self.skipped_low_edge,  # deprecated alias
                "slope_disagrees": self.skipped_slope_disagrees,
                "overbought": self.skipped_overbought,
                "too_young": self.skipped_too_young,
                "open_failed": self.skipped_open_failed,
            },
            "opens": {"yes": self.opens_yes, "no": self.opens_no},
            "last_cycle_at": self.last_cycle_at,
            "last_open_at": self.last_open_at,
        }

    def persist_to_file(self) -> None:
        """Atómico: escribe snapshot al volumen compartido con server."""
        try:
            snap = self.snapshot()
            snap["_persisted_at"] = time.time()
            target = _SNAPSHOT_PATH
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=".crypto_arb_", suffix=".tmp", dir=str(target.parent)
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(snap, f, default=str)
                os.replace(tmp, target)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            log.debug("crypto_arb.persist_to_file failed: %s", e)

    def start_persist_thread(self, interval_s: float = 5.0) -> None:
        if getattr(self, "_persist_thread_started", False):
            return
        self._persist_thread_started = True

        def _loop() -> None:
            while True:
                try:
                    time.sleep(interval_s)
                    self.persist_to_file()
                except Exception:
                    pass

        t = threading.Thread(target=_loop, name="crypto_arb-persist", daemon=True)
        t.start()


metrics = _Metrics()


def read_snapshot_from_file() -> dict[str, Any] | None:
    """Lee el snapshot persistido por el runner (usado por el server)."""
    try:
        if not _SNAPSHOT_PATH.exists():
            return None
        with _SNAPSHOT_PATH.open("r") as f:
            return json.load(f)
    except Exception as e:
        log.debug("crypto_arb.read_snapshot_from_file failed: %s", e)
        return None


# --- Utilidades ---

def _parse_slug(slug: str) -> tuple[str, int] | None:
    """Devuelve (symbol_pmkt_prefix, end_epoch) o None si no matchea."""
    m = SLUG_REGEX.match(slug or "")
    if not m:
        return None
    sym_short = m.group(1)
    epoch = int(m.group(2))
    return f"{sym_short}-updown-5m-", epoch


def _parse_iso_to_epoch(end_iso: str) -> int | None:
    if not end_iso:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _market_midpoint(outcome_prices_json: str | None, outcome_index: int) -> float | None:
    """Lee outcomePrices[outcome_index] (JSON array de strings)."""
    if not outcome_prices_json:
        return None
    try:
        import json as _json
        prices = _json.loads(outcome_prices_json)
        if isinstance(prices, list) and outcome_index < len(prices):
            return float(prices[outcome_index])
    except Exception:
        return None
    return None


# --- Lógica principal ---

async def _list_active_updown_markets(client: PolymarketClient) -> list[dict]:
    """Lista mercados activos con slug btc/eth/sol-updown-5m.

    Filtra por endDate futuro y close pendiente. La query a gamma API
    pagina hasta encontrar suficientes — los mercados están ordenados
    por endDate ascending para que los más cercanos a cerrar aparezcan
    primero.
    """
    now = int(time.time())
    out: list[dict] = []
    seen = 0
    # Filtro server-side por endDate: solo markets que cierran en próximos
    # 30 min. Sin esto, iter_markets devolvería miles de markets viejos
    # con `closed=false` administrativo y nunca llegaríamos a los
    # updown-5m activos. Con end_date_min=now la API devuelve <100 rows.
    from datetime import datetime, timezone
    iso_now = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iso_max = datetime.fromtimestamp(now + 1800, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async for m in client.iter_markets(
        page_size=500, closed=False, order="endDate", ascending=True,
        end_date_min=iso_now, end_date_max=iso_max,
    ):
        seen += 1
        slug = m.get("slug") or ""
        parsed = _parse_slug(slug)
        if not parsed:
            continue
        end_ts = _parse_iso_to_epoch(m.get("endDate") or "")
        if end_ts is None or end_ts <= now:
            continue
        m["_end_ts"] = end_ts
        m["_slug_prefix"] = parsed[0]
        out.append(m)
        if len(out) >= 100:
            break
    metrics.markets_seen += seen
    return out


def _match_to_symbol(slug_prefix: str) -> str | None:
    return SLUG_PREFIX_TO_SYMBOL.get(slug_prefix)


def _evaluate_market(
    market: dict,
    binance_ws: BinanceTickerWS,
    config: CryptoArbConfig,
    spot_history: dict[str, list[tuple[int, float]]],
) -> dict | None:
    """Evalúa un market y devuelve un dict con la decisión, o None si skip.

    Decision dict::

        {"action": "buy", "side": "Up"|"Down", "outcome_index": 0|1,
         "size_usdc": float, "reason": str}

    Skips quedan registrados en metrics.* para auditoría.
    """
    now = int(time.time())
    end_ts = market["_end_ts"]
    secs_to_close = end_ts - now
    if secs_to_close > config.pre_close_window_s:
        return None  # demasiado lejos del close, esperamos
    metrics.markets_in_window += 1

    symbol = _match_to_symbol(market["_slug_prefix"])
    if not symbol:
        return None
    last = binance_ws.get_price(symbol)
    if last is None:
        metrics.skipped_no_spot += 1
        return None
    cur_price, cur_ts_ms = last

    # bucket_start = end_ts - 300 (5min markets)
    bucket_start_ts = end_ts - 300
    bucket_age = now - bucket_start_ts
    if bucket_age < config.min_bucket_age_s:
        metrics.skipped_too_young += 1
        return None

    # Buscar precio al inicio del bucket en el history. Tomamos el sample
    # más cercano a bucket_start_ts (no >5s de error).
    history = spot_history.get(symbol) or []
    start_price = None
    for ts_ms, p in history:
        ts_s = ts_ms // 1000
        if abs(ts_s - bucket_start_ts) < 10:
            start_price = p
            break
    if start_price is None:
        # Si no tenemos histórico (bot recién arrancado), no opera.
        metrics.skipped_no_spot += 1
        return None

    move_pct = (cur_price / start_price - 1.0) * 100.0

    # Modelo probabilístico (reemplaza el threshold de momentum):
    # - p_up = P(close > start) bajo drift normal residual
    # - edge_vs_mid compara contra el mid del lado UP de Polymarket y
    #   decide side (Up/Down) o ninguno si el mispricing < threshold.
    mid_up = _market_midpoint(market.get("outcomePrices"), 0)
    if mid_up is None:
        # Sin mid no podemos comparar — no es low_edge propiamente, lo
        # tratamos como no_spot semánticamente (datos faltantes).
        metrics.skipped_no_spot += 1
        return None
    edge, side_label, p_up = edge_vs_mid(
        spot_move_pct=move_pct,
        secs_left=float(secs_to_close),
        symbol=symbol,
        mid_up=mid_up,
    )
    if side_label is None:
        metrics.skipped_low_edge += 1
        return None

    outcome_index = 0 if side_label == "Up" else 1

    # Slope confirmation: si el slope de los últimos 30s contradice el
    # side, skip (defensa contra rebote tardío). Tomamos el sample más
    # antiguo dentro de los últimos ~30s + el sample actual.
    cutoff_ms = (now - 30) * 1000
    p_30s_ago = None
    for ts_ms, p in history:
        if ts_ms >= cutoff_ms:
            p_30s_ago = p
            break
    if p_30s_ago is not None:
        slope_pct = (cur_price / p_30s_ago - 1.0) * 100.0
        # Tolerancia chica (~1bp) para evitar descartar por ruido.
        if (side_label == "Up" and slope_pct < -0.01) or (
            side_label == "Down" and slope_pct > 0.01
        ):
            metrics.skipped_slope_disagrees += 1
            return None

    # Overbought guard: el mid del lado a comprar.
    side_mid = mid_up if outcome_index == 0 else (1.0 - mid_up)
    if side_mid > config.max_mid_target:
        metrics.skipped_overbought += 1
        return None

    return {
        "action": "buy",
        "side": side_label,
        "outcome_index": outcome_index,
        "size_usdc": config.bet_size_usdc,
        "reason": (
            f"spot {symbol} {move_pct:+.2f}% en {bucket_age:.0f}s "
            f"· p_up={p_up:.3f} mid_up={mid_up:.3f} edge={edge:.3f} "
            f"side={side_label} secs_left={secs_to_close}"
        ),
        "spot_now": cur_price,
        "spot_start": start_price,
        "secs_to_close": secs_to_close,
        "p_up": p_up,
        "mid_up": mid_up,
        "edge": edge,
    }


async def _open_arb_trade(market: dict, decision: dict) -> int | None:
    """Abre el paper_trade vía tradebook.open_position. Devuelve pid o None.

    2026-05-10: paper realista. Antes usaba midpoint idealizado como
    entry_price → paper +$269 hoy mientras live perdió $76 (orderbooks
    thin del crypto-updown 5min). Ahora lee orderbook real vía
    `estimate_slippage` y usa VWAP → si no hay liquidez, skip (mismo
    comportamiento que tendría live). Esto hace paper PREDICTIVO del
    live, no fantasía.
    """
    cid = market.get("conditionId") or ""
    slug = market.get("slug") or ""
    end_ts = market["_end_ts"]
    src_id = f"crypto_arb:{slug}:{decision['side']}"

    mid = _market_midpoint(market.get("outcomePrices"), decision["outcome_index"])
    if mid is None:
        metrics.skipped_open_failed += 1
        log.info("crypto_arb.open_skipped slug=%s reason=no_mid", slug)
        return None

    # Pre-check orderbook real (también en paper). Si thin → abort, igual
    # que live haría. Bet size en shares aproximado para target_size.
    config = CryptoArbConfig.from_env()
    try:
        from src.polymarket.clob_client import estimate_slippage
        # gamma devuelve clobTokenIds como JSON string, no list. Parsear si es str.
        ct_raw = market.get("clobTokenIds")
        if isinstance(ct_raw, str):
            try:
                token_id = json.loads(ct_raw)
            except Exception:
                token_id = None
        elif isinstance(ct_raw, list):
            token_id = ct_raw
        else:
            token_id = None
        if isinstance(token_id, list) and len(token_id) > decision["outcome_index"]:
            token_id_str = token_id[decision["outcome_index"]]
        else:
            token_id_str = None
        if token_id_str:
            target_shares = config.bet_size_usdc / max(mid, 0.01)
            slip = estimate_slippage(
                token_id=str(token_id_str), side="BUY",
                target_size_shares=target_shares, target_price=mid,
            )
            if not slip.get("ok"):
                metrics.skipped_open_failed += 1
                log.info(
                    "crypto_arb.open_skipped slug=%s reason=orderbook_%s detail=%s",
                    slug, slip.get("error", "?")[:30], slip.get("depth_usdc_top5"),
                )
                return None
            entry_price = float(slip.get("vwap") or mid)
        else:
            entry_price = mid  # fallback si no hay token_id resoluble
    except Exception as e:
        log.debug("crypto_arb.estimate_slippage failed: %s — fallback midpoint", e)
        entry_price = mid
    raw_payload = {
        "source": "crypto_arb",
        "slug": slug,
        "symbol": _match_to_symbol(market["_slug_prefix"]),
        "side": decision["side"],
        "outcome_index": decision["outcome_index"],
        "spot_now": decision["spot_now"],
        "spot_start": decision["spot_start"],
        "secs_to_close": decision["secs_to_close"],
        "p_up": decision.get("p_up"),
        "mid_up": decision.get("mid_up"),
        "edge": decision.get("edge"),
        "reason": decision["reason"],
        "end_ts": end_ts,
    }
    try:
        # open_position es sync — offload a thread para no bloquear loop.
        pid, reason = await asyncio.to_thread(
            open_position,
            source_wallet="crypto_arb",
            source_trade_id=src_id,
            condition_id=cid,
            outcome=decision["side"],
            outcome_index=decision["outcome_index"],
            price=entry_price,
            timestamp=int(time.time()),
            raw=raw_payload,
        )
        if pid:
            if decision["outcome_index"] == 0:
                metrics.opens_yes += 1
            else:
                metrics.opens_no += 1
            metrics.last_open_at = time.time()
            log.info(
                "crypto_arb.open pid=%d slug=%s side=%s mid=%.3f reason=%s",
                pid, slug, decision["side"], entry_price, decision["reason"],
            )
            return pid
        else:
            metrics.skipped_open_failed += 1
            log.info(
                "crypto_arb.open_skipped slug=%s reason=%s",
                slug, reason,
            )
    except Exception:
        metrics.skipped_open_failed += 1
        log.exception("crypto_arb.open_failed slug=%s", slug)
    return None


# --- Settler dedicado ---

async def _settle_crypto_arb_resolved(client: PolymarketClient) -> int:
    """Settle paper_trades open con source_wallet='crypto_arb' cuyos buckets
    ya expiraron. Hace fetch directo a gamma por conditionId, lee
    outcomePrices, y aplica settle a través del flujo de paper.settle_resolved.

    Race contra resolución on-chain: post-bucket end, Polymarket tarda
    ~10-60s en setear outcomePrices definitivos. Si el market aún está
    `closed=false`, lo dejamos para el próximo ciclo.

    Devuelve cantidad de trades settled en este pass.
    """
    from src.db.schema import db, tx
    from src.copybot.paper import _resolved_payout, post_close_costs, EPSILON
    from src.copybot.learning import on_paper_trade_closed

    now = int(time.time())
    # Targets: trades open de crypto_arb cuyos end_ts pasaron hace >=20s
    # (margen para que la resolución on-chain se setee).
    with db() as conn:
        # Incluye waiting_settlement: sweep_stops puede marcar buckets cuyo
        # slug ya expiró pero todavía no aparecen en gamma como closed=1.
        # Al settler le interesan ambos estados — si gamma confirma resuelto,
        # los liquida.
        rows = conn.execute(
            """
            SELECT id, condition_id, entry_price, entry_size_usdc, outcome_index,
                   side, raw
            FROM paper_trades
            WHERE source_wallet='crypto_arb' AND status IN ('open', 'waiting_settlement')
            """,
        ).fetchall()
    candidates = []
    for r in rows:
        try:
            raw = json.loads(r["raw"]) if r["raw"] else {}
            end_ts = int(raw.get("end_ts") or 0)
        except Exception:
            end_ts = 0
        if end_ts and (now - end_ts) >= 20:
            candidates.append((r, end_ts))
    if not candidates:
        return 0

    # Agrupamos por slug (no por condition_id): gamma /markets?conditionId=X
    # IGNORA el filtro tanto con closed=true como sin él, devolviendo markets
    # arbitrarios (verificado con probes a Biden + Rhianna). Pero
    # /markets?slug=Y&closed=true SÍ respeta el filtro y devuelve el market
    # correcto. El slug está en el raw_payload de cada paper_trade.
    from src.polymarket.client import GAMMA_API
    by_slug: dict[str, list] = {}
    for r, _ in candidates:
        try:
            raw = json.loads(r["raw"]) if r["raw"] else {}
        except Exception:
            raw = {}
        slug = raw.get("slug") or ""
        if not slug:
            continue
        by_slug.setdefault(slug, []).append(r)

    to_settle: list[tuple] = []
    clv_records: list[tuple] = []  # (trade_id, entry, payout, side, slug)
    for slug, rs in by_slug.items():
        try:
            data = await client._get(
                f"{GAMMA_API}/markets",
                params={"slug": slug, "closed": "true", "limit": 1},
            )
        except Exception:
            continue
        m = data[0] if isinstance(data, list) and data else None
        if not m:
            # Probemos sin closed=true por si está aún active.
            try:
                data2 = await client._get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug, "limit": 1},
                )
                m = data2[0] if isinstance(data2, list) and data2 else None
            except Exception:
                continue
        if not m:
            continue
        # Validación: el slug del response DEBE matchear el solicitado.
        if (m.get("slug") or "").lower() != slug.lower():
            log.debug(
                "crypto_arb.settle: gamma devolvió slug distinto al pedido "
                "(%s vs %s) — saltando",
                (m.get("slug") or "")[:30], slug[:30],
            )
            continue
        outcome_prices = m.get("outcomePrices")
        # outcomePrices viene como list o JSON string en gamma
        if isinstance(outcome_prices, list):
            outcome_prices = json.dumps(outcome_prices)
        is_closed = m.get("closed") is True or (
            outcome_prices and any(
                p in (1, 1.0, "1", "1.0") for p in (json.loads(outcome_prices) if outcome_prices else [])
            )
        )
        if not is_closed:
            continue
        for r in rs:
            payout = _resolved_payout(outcome_prices, r["outcome_index"])
            if payout is None:
                continue
            entry = r["entry_price"]
            size = r["entry_size_usdc"]
            if entry > EPSILON:
                shares = size / entry
                gross = shares * (payout - entry)
            else:
                gross = 0.0
            _, _, net_pnl = post_close_costs(gross)
            status = "settled_win" if net_pnl > 0 else "settled_loss"
            to_settle.append((payout, net_pnl, status, r["id"]))
            clv_records.append((r["id"], entry, payout, r["side"], slug))

    if not to_settle:
        return 0

    with tx() as conn:
        conn.executemany(
            """
            UPDATE paper_trades
            SET exit_price=?, exit_at=strftime('%s','now'), pnl_usdc=?, status=?
            WHERE id=?
            """,
            to_settle,
        )
    # CLV tracking: edge real ortogonal al PnL (positivo si bot captó
    # movimiento de market post-entry). source='crypto_arb' lo separa
    # de paper genérico en compute_clv_summary. No bloquea si falla.
    try:
        from src.copybot.clv_tracker import record_clv
        for tid, entry, payout, side, bslug in clv_records:
            try:
                await asyncio.to_thread(
                    record_clv, tid, entry, payout,
                    side=side or "BUY", source="crypto_arb",
                    bucket_slug=bslug,
                )
            except Exception:
                log.debug("crypto_arb.settle clv record fail pid=%s", tid)
    except Exception:
        log.debug("crypto_arb.settle clv import error — settle continúa")

    # on_paper_trade_closed dispara la notif Telegram (gain/loss) + bandit
    # recompute. Llamado fuera de tx() porque cada uno abre su propia tx.
    for _, _, _, pid in to_settle:
        try:
            await asyncio.to_thread(on_paper_trade_closed, pid)
        except Exception:
            log.exception("crypto_arb.settle on_paper_trade_closed pid=%d", pid)
    return len(to_settle)


# --- Loop principal ---

async def crypto_arb_loop() -> None:
    """Loop principal del bot crypto_arb.

    Conecta al WS de Binance, mantiene un history de spot prices
    (últimos 6 min) y cada CHECK_INTERVAL_S evalúa los markets crypto
    activos cercanos a cerrar.
    """
    config = CryptoArbConfig.from_env()
    if not config.enabled:
        log.info("crypto_arb: disabled (CRYPTO_ARB_ENABLED!=true)")
        return
    # Guard contra LIVE: orderbooks de updown-5m son thin ($1-5k) y un BUY de
    # $5-10 mueve 0.5-1% del book → slippage real 50-90% → pérdida garantizada.
    # Solo lo habilitamos en LIVE si el user lo activa explícitamente con
    # CRYPTO_ARB_ALLOW_LIVE=true (después de validar 24h en paper).
    from src.config import LIVE_MODE
    if LIVE_MODE and not config.allow_live:
        log.warning(
            "crypto_arb: deshabilitado en LIVE_MODE (orderbooks thin → "
            "slippage extremo). Para activar, setear CRYPTO_ARB_ALLOW_LIVE=true "
            "después de validar 24h en paper."
        )
        return
    log.info(
        "crypto_arb: arrancando — interval=%ss min_edge=%.3f bet=$%s "
        "pre_close_window=%ss symbols=%s%s",
        config.check_interval_s, get_min_edge(),
        config.bet_size_usdc, config.pre_close_window_s,
        ",".join(config.symbols),
        "  (LIVE_MODE — opt-in)" if (LIVE_MODE and config.allow_live) else "",
    )

    # Histórico de precios spot por símbolo. Cada (ts_ms, price). Cap de
    # ~360 entries por símbolo (6 min × 1 msg/s) — más que suficiente
    # para buscar el precio al inicio del bucket de 5min.
    spot_history: dict[str, list[tuple[int, float]]] = {s: [] for s in config.symbols}
    HISTORY_CAP = 360

    async def _on_tick(symbol: str, price: float, ts_ms: int) -> None:
        h = spot_history.setdefault(symbol, [])
        h.append((ts_ms, price))
        if len(h) > HISTORY_CAP:
            del h[: len(h) - HISTORY_CAP]

    binance_ws = BinanceTickerWS(symbols=config.symbols, on_tick=_on_tick)
    binance_task = asyncio.create_task(binance_ws.run(), name="binance-ws")

    # Persist snapshot to file para que /api/crypto-arb-status (server) lo lea.
    metrics.start_persist_thread(interval_s=5.0)

    # Espera inicial para acumular history (necesitamos al menos
    # min_bucket_age_s de muestras para calcular momentum).
    log.info("crypto_arb: warming up %ss para acumular spot history…", config.min_bucket_age_s)
    await asyncio.sleep(config.min_bucket_age_s + 5)

    # Log heartbeat cada N ciclos (cycles*15s ~ 5min con N=20). Sin
    # heartbeat el bot es silent si no abre ops, y no podemos saber si
    # está vivo desde docker logs.
    HEARTBEAT_EVERY = 20

    try:
        async with PolymarketClient() as client:
            while True:
                metrics.cycles += 1
                metrics.last_cycle_at = time.time()
                try:
                    markets = await asyncio.wait_for(
                        _list_active_updown_markets(client), timeout=30,
                    )
                except asyncio.TimeoutError:
                    log.warning("crypto_arb: list markets timeout — sigo")
                    markets = []
                except (httpx.HTTPError, ConnectionError) as e:
                    # Transient net error (gamma close conn). Bot continúa
                    # siguiente ciclo. No spamear Telegram con stacktrace.
                    log.warning(
                        "crypto_arb: list markets transient_net %s — sigo",
                        type(e).__name__,
                    )
                    markets = []
                except Exception:
                    log.exception("crypto_arb: list markets error")
                    markets = []

                # Evaluar cada market en la ventana
                for m in markets:
                    decision = _evaluate_market(m, binance_ws, config, spot_history)
                    if decision and decision["action"] == "buy":
                        await _open_arb_trade(m, decision)

                # Settle paper_trades open de buckets ya expirados. Los
                # markets crypto-updown-5m resuelven on-chain ~30s después
                # del bucket end. settle_resolved() global solo procesa
                # markets con closed=1 en DB, pero el refresh de markets
                # corre cada 4h — demasiado lento para buckets de 5min.
                # Acá fetch directo via gamma por conditionId y settle.
                try:
                    n = await asyncio.wait_for(
                        _settle_crypto_arb_resolved(client), timeout=15,
                    )
                    if n:
                        log.info("crypto_arb.settled n=%d trades", n)
                except asyncio.TimeoutError:
                    log.warning("crypto_arb.settle timeout — sigo")
                except Exception:
                    log.exception("crypto_arb.settle error")

                if metrics.cycles % HEARTBEAT_EVERY == 0:
                    snap = metrics.snapshot()
                    log.info(
                        "crypto_arb.heartbeat cycles=%d markets_seen=%d in_window=%d "
                        "skipped=%s opens=%s",
                        snap["cycles"], snap["markets_seen"], snap["markets_in_window"],
                        snap["skipped"], snap["opens"],
                    )

                await asyncio.sleep(config.check_interval_s)
    finally:
        binance_ws.stop()
        binance_task.cancel()
        try:
            await binance_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("crypto_arb: loop terminado")
