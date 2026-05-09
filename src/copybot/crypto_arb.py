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
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.binance.websocket import BinanceTickerWS
from src.copybot.tradebook import MODE as TRADEBOOK_MODE, open_position
from src.polymarket.client import PolymarketClient

log = logging.getLogger(__name__)

# Símbolos que tradeamos. Mapping a los slug-prefixes de Polymarket.
SYMBOL_TO_SLUG_PREFIX = {
    "BTCUSDT": "btc-updown-5m-",
    "ETHUSDT": "eth-updown-5m-",
    "SOLUSDT": "sol-updown-5m-",
}
SLUG_PREFIX_TO_SYMBOL = {v: k for k, v in SYMBOL_TO_SLUG_PREFIX.items()}
SLUG_REGEX = re.compile(r"^(btc|eth|sol)-updown-5m-(\d+)$")

# --- Parámetros configurables vía env ---
DEFAULT_CHECK_INTERVAL_S = 15.0
DEFAULT_PRE_CLOSE_WINDOW_S = 60.0   # solo evaluar markets que cierran en próximos 60s
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
        )


# --- Métricas in-memory (para /api/crypto-arb-status si se agrega) ---
@dataclass
class _Metrics:
    cycles: int = 0
    markets_seen: int = 0
    markets_in_window: int = 0
    skipped_no_spot: int = 0
    skipped_low_momentum: int = 0
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
        return {
            "uptime_s": round(now - self.started_at, 1),
            "cycles": self.cycles,
            "markets_seen": self.markets_seen,
            "markets_in_window": self.markets_in_window,
            "skipped": {
                "no_spot": self.skipped_no_spot,
                "low_momentum": self.skipped_low_momentum,
                "overbought": self.skipped_overbought,
                "too_young": self.skipped_too_young,
                "open_failed": self.skipped_open_failed,
            },
            "opens": {"yes": self.opens_yes, "no": self.opens_no},
            "last_cycle_at": self.last_cycle_at,
            "last_open_at": self.last_open_at,
        }


metrics = _Metrics()


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
    # iter_markets ya viene paginado en el client. Usamos un cap de
    # páginas para no quemar la API si no hay matches (no debería).
    async for m in client.iter_markets(page_size=500, closed=False):
        seen += 1
        slug = m.get("slug") or ""
        parsed = _parse_slug(slug)
        if not parsed:
            if seen >= 2000:
                break
            continue
        end_ts = _parse_iso_to_epoch(m.get("endDate") or "")
        if end_ts is None or end_ts <= now:
            continue
        m["_end_ts"] = end_ts
        m["_slug_prefix"] = parsed[0]
        out.append(m)
        if len(out) >= 100:
            # Suficiente para 1 ciclo (típicamente 6-12 markets activos)
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
    abs_move = abs(move_pct)
    if abs_move < config.momentum_threshold_pct:
        metrics.skipped_low_momentum += 1
        return None

    # Determinar side. Polymarket suele tener outcomes [Up, Down] o
    # [Yes, No] dependiendo del market. Asumimos outcome_index 0=Up/Yes,
    # 1=Down/No (verificar contra outcomes en la primera operación real).
    if move_pct > 0:
        side_label = "Up"
        outcome_index = 0
    else:
        side_label = "Down"
        outcome_index = 1

    mid = _market_midpoint(market.get("outcomePrices"), outcome_index)
    if mid is not None and mid > config.max_mid_target:
        metrics.skipped_overbought += 1
        return None

    return {
        "action": "buy",
        "side": side_label,
        "outcome_index": outcome_index,
        "size_usdc": config.bet_size_usdc,
        "reason": (
            f"spot {symbol} {move_pct:+.2f}% en {bucket_age:.0f}s "
            f"(thr {config.momentum_threshold_pct:.2f}%) · mid={mid}"
        ),
        "spot_now": cur_price,
        "spot_start": start_price,
        "secs_to_close": secs_to_close,
    }


async def _open_arb_trade(market: dict, decision: dict) -> int | None:
    """Abre el paper_trade vía tradebook.open_position. Devuelve pid o None."""
    cid = market.get("conditionId") or ""
    slug = market.get("slug") or ""
    end_ts = market["_end_ts"]
    src_id = f"crypto_arb:{slug}:{decision['side']}"
    # Precio de entrada: usamos el midpoint (mejor proxy si no podemos
    # leer orderbook real desde acá). Si midpoint es None, fallback al
    # threshold (compramos asumiendo edge).
    mid = _market_midpoint(market.get("outcomePrices"), decision["outcome_index"])
    entry_price = mid if mid is not None else 0.50
    raw_payload = {
        "source": "crypto_arb",
        "slug": slug,
        "symbol": _match_to_symbol(market["_slug_prefix"]),
        "side": decision["side"],
        "outcome_index": decision["outcome_index"],
        "spot_now": decision["spot_now"],
        "spot_start": decision["spot_start"],
        "secs_to_close": decision["secs_to_close"],
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
    log.info(
        "crypto_arb: arrancando — interval=%ss thr=%s%% bet=$%s symbols=%s",
        config.check_interval_s, config.momentum_threshold_pct,
        config.bet_size_usdc, ",".join(config.symbols),
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

    # Espera inicial para acumular history (necesitamos al menos
    # min_bucket_age_s de muestras para calcular momentum).
    log.info("crypto_arb: warming up %ss para acumular spot history…", config.min_bucket_age_s)
    await asyncio.sleep(config.min_bucket_age_s + 5)

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
                except Exception:
                    log.exception("crypto_arb: list markets error")
                    markets = []

                # Evaluar cada market en la ventana
                for m in markets:
                    decision = _evaluate_market(m, binance_ws, config, spot_history)
                    if decision and decision["action"] == "buy":
                        await _open_arb_trade(m, decision)

                await asyncio.sleep(config.check_interval_s)
    finally:
        binance_ws.stop()
        binance_task.cancel()
        try:
            await binance_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("crypto_arb: loop terminado")
