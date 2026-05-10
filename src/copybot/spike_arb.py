"""Spike arbitrage — limit orders pre-spike Binance → Polymarket.

Estrategia (Nivel B, 2026-05-10):
- Detectamos un spike absoluto en spot Binance (>SPIKE_ARB_THRESHOLD_PCT en
  los últimos SPIKE_ARB_WINDOW_S segundos).
- Si Polymarket todavía cotiza el lado correcto en zona "neutral" (mid
  por debajo del cap), posteamos un LIMIT BUY al mid actual.
- TTL del limit: SPIKE_ARB_LIMIT_TTL_S segundos. Si no fillea → cancel.
- Si fillea → recordamos fill_price, esperamos resolución on-chain.

Diferencia vs ``crypto_arb.py`` existente:
- Trigger simple (umbral absoluto en %) en vez de modelo prob normal-residual.
- LIMIT orders (NO market) — no destruido por slippage en thin orderbooks.
- Defensa básica: no fillea en TTL → cancel y olvidar.

Diseño:
- ``SpikeArb`` es testeable como unidad: recibe ticks via ``on_binance_tick``
  y un ``order_executor`` callable (mockeable) que simula la fase de
  postear/cancelar limits. La clase NO importa CLOB directo.
- El ``spike_arb_loop`` ata BinanceTickerWS + ``SpikeArb`` + executor real.

Schema:
- Tabla ``spike_arb_trades`` (creada on-demand al arrancar el loop). Almacena
  signal/order/fill/settle para auditoría.

Activación: env var ``SPIKE_ARB_ENABLED=true``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


# --- Config defaults ---
DEFAULT_THRESHOLD_PCT = 0.4          # spike >= 0.4% en window
DEFAULT_WINDOW_S = 30                # ventana de medición (segundos)
DEFAULT_TARGET_USDC = 3.0            # bet por trade (USDC)
DEFAULT_LIMIT_TTL_S = 60             # tiempo de vida del limit antes de cancel
DEFAULT_MAX_MID_TARGET = 0.55        # mid del lado a comprar debe ser < esto
DEFAULT_HISTORY_CAP = 600            # ~10 min de samples (1 msg/s)
DEFAULT_REARM_S = 30                 # cool-down por símbolo+side (anti-spam)


# --- Tipo del callable que postea/cancela órdenes ---
# Recibe dict con la signal y devuelve dict con resultado del intento.
# Mantenerlo abstracto permite testear sin tocar CLOB real.
#
# Signal dict (input):
#   {
#       "symbol": "BTCUSDT",
#       "side": "UP" | "DOWN",
#       "spike_pct": float,
#       "mid_at_signal": float,
#       "limit_price": float,
#       "size_usdc": float,
#       "ttl_s": int,
#       "ts_ms": int,
#   }
#
# Result dict (output):
#   {
#       "ok": True | False,
#       "order_id": str | None,
#       "filled": bool,            # True si filleó dentro del TTL
#       "fill_price": float | None,
#       "error": str | None,
#       "raw": dict | None,
#   }
OrderExecutor = Callable[[dict], Awaitable[dict]]

# Callable opcional para resolver mid actual del market Polymarket dado
# (symbol, side). Útil para tests — paper executor lo usa para calcular el
# limit. Devuelve None si no hay market resoluble.
MidResolver = Callable[[str, str], Awaitable[Optional[float]]]


@dataclass
class SpikeArbConfig:
    enabled: bool = False
    threshold_pct: float = DEFAULT_THRESHOLD_PCT
    window_s: int = DEFAULT_WINDOW_S
    target_size_usdc: float = DEFAULT_TARGET_USDC
    limit_ttl_s: int = DEFAULT_LIMIT_TTL_S
    max_mid_target: float = DEFAULT_MAX_MID_TARGET
    history_cap: int = DEFAULT_HISTORY_CAP
    rearm_s: int = DEFAULT_REARM_S
    symbols: tuple[str, ...] = field(
        default_factory=lambda: ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    )

    @classmethod
    def from_env(cls) -> "SpikeArbConfig":
        return cls(
            enabled=os.getenv("SPIKE_ARB_ENABLED", "false").lower() == "true",
            threshold_pct=float(os.getenv(
                "SPIKE_ARB_THRESHOLD_PCT", DEFAULT_THRESHOLD_PCT)),
            window_s=int(os.getenv(
                "SPIKE_ARB_WINDOW_S", DEFAULT_WINDOW_S)),
            target_size_usdc=float(os.getenv(
                "SPIKE_ARB_TARGET_USDC", DEFAULT_TARGET_USDC)),
            limit_ttl_s=int(os.getenv(
                "SPIKE_ARB_LIMIT_TTL_S", DEFAULT_LIMIT_TTL_S)),
            max_mid_target=float(os.getenv(
                "SPIKE_ARB_MAX_MID_TARGET", DEFAULT_MAX_MID_TARGET)),
        )


@dataclass
class _Metrics:
    ticks: int = 0
    spikes_detected: int = 0
    skipped_no_mid: int = 0
    skipped_overbought: int = 0
    skipped_rearm: int = 0
    orders_posted: int = 0
    orders_filled: int = 0
    orders_cancelled_ttl: int = 0
    orders_failed: int = 0
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "ticks": self.ticks,
            "spikes_detected": self.spikes_detected,
            "skipped_no_mid": self.skipped_no_mid,
            "skipped_overbought": self.skipped_overbought,
            "skipped_rearm": self.skipped_rearm,
            "orders_posted": self.orders_posted,
            "orders_filled": self.orders_filled,
            "orders_cancelled_ttl": self.orders_cancelled_ttl,
            "orders_failed": self.orders_failed,
        }


# --- DB schema (on-demand) ---

_TABLE_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS spike_arb_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    spike_pct       REAL,
    mid_at_signal   REAL,
    limit_price     REAL,
    size_usdc       REAL,
    order_id        TEXT,
    fill_price      REAL,
    status          TEXT DEFAULT 'open',
    pnl_usdc        REAL,
    bucket_slug     TEXT,
    bucket_end_ts   INTEGER,
    signal_at       INTEGER,
    filled_at       INTEGER,
    closed_at       INTEGER
)
"""

_TABLE_DDL_PG = """
CREATE TABLE IF NOT EXISTS spike_arb_trades (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    spike_pct       DOUBLE PRECISION,
    mid_at_signal   DOUBLE PRECISION,
    limit_price     DOUBLE PRECISION,
    size_usdc       DOUBLE PRECISION,
    order_id        TEXT,
    fill_price      DOUBLE PRECISION,
    status          TEXT DEFAULT 'open',
    pnl_usdc        DOUBLE PRECISION,
    bucket_slug     TEXT,
    bucket_end_ts   BIGINT,
    signal_at       BIGINT,
    filled_at       BIGINT,
    closed_at       BIGINT
)
"""

_TABLE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_spike_arb_status ON spike_arb_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_spike_arb_symbol ON spike_arb_trades(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_spike_arb_signal_at ON spike_arb_trades(signal_at DESC)",
]


def init_table() -> None:
    """Crea la tabla ``spike_arb_trades`` si no existe.

    Idempotente. Compatible con SQLite y Postgres (a través del wrapper
    de ``src.db.schema``: ``BIGSERIAL`` se traduce, ``INTEGER PRIMARY KEY
    AUTOINCREMENT`` también — ver ``_translate_sql_to_pg``).
    """
    try:
        from src.db.schema import db, BACKEND
        ddl = _TABLE_DDL_PG if BACKEND == "postgres" else _TABLE_DDL_SQLITE
        with db() as conn:
            conn.execute(ddl)
            for ix in _TABLE_INDEXES:
                conn.execute(ix)
    except Exception:
        log.exception("spike_arb.init_table failed")


# --- Core: SpikeArb (testeable sin red) ---

class SpikeArb:
    """Detector de spikes + emisor de signals para limit orders.

    Thread-safe-ish: pensado para uso single-threaded asyncio. El estado
    interno (history, _last_signal_at) se muta solo en ``on_binance_tick``.
    """

    def __init__(
        self,
        config: SpikeArbConfig | None = None,
        *,
        order_executor: OrderExecutor | None = None,
        mid_resolver: MidResolver | None = None,
    ) -> None:
        self.config = config or SpikeArbConfig()
        self._order_executor = order_executor
        self._mid_resolver = mid_resolver
        # symbol → list[(ts_ms, price)] ordenado ascendente
        self._history: dict[str, list[tuple[int, float]]] = {}
        # cool-down (symbol, side) → ts del último signal disparado
        self._last_signal_at: dict[tuple[str, str], int] = {}
        self.metrics = _Metrics()
        # Lista de tasks de orders en flight — útil para test/cleanup.
        self._inflight: set[asyncio.Task] = set()

    # ----- public API -----

    @property
    def history(self) -> dict[str, list[tuple[int, float]]]:
        """Vista (no-copia) del history actual — útil para tests."""
        return self._history

    async def on_binance_tick(
        self, symbol: str, price: float, ts_ms: int
    ) -> Optional[dict]:
        """Procesa un tick de Binance, detecta spike y dispara signal.

        Devuelve la signal dict si se disparó (test asserts), None si no.
        El "execute order" se hace fire-and-forget vía task background.
        """
        symbol = symbol.upper()
        self.metrics.ticks += 1
        h = self._history.setdefault(symbol, [])
        h.append((ts_ms, float(price)))
        if len(h) > self.config.history_cap:
            del h[: len(h) - self.config.history_cap]

        spike = self._detect_spike(symbol, ts_ms)
        if spike is None:
            return None
        side, spike_pct, price_now = spike

        # Cool-down anti-spam: si ya disparamos este (sym, side) recientemente,
        # skip. Evita rafagear N orders por el mismo movimiento.
        now_s = ts_ms // 1000
        last = self._last_signal_at.get((symbol, side), 0)
        if now_s - last < self.config.rearm_s:
            self.metrics.skipped_rearm += 1
            return None

        self.metrics.spikes_detected += 1

        # Pedir mid al executor (vía resolver inyectable). Si no hay → skip.
        mid = None
        if self._mid_resolver is not None:
            try:
                mid = await self._mid_resolver(symbol, side)
            except Exception:
                log.exception("spike_arb.mid_resolver failed sym=%s", symbol)
                mid = None
        if mid is None:
            self.metrics.skipped_no_mid += 1
            return None

        if mid > self.config.max_mid_target:
            # Ya está priced-in: el mid del lado a comprar pasó el cap.
            self.metrics.skipped_overbought += 1
            return None

        signal = {
            "symbol": symbol,
            "side": side,
            "spike_pct": spike_pct,
            "mid_at_signal": float(mid),
            "limit_price": float(mid),  # exactamente al mid (sin agresión)
            "size_usdc": self.config.target_size_usdc,
            "ttl_s": self.config.limit_ttl_s,
            "ts_ms": ts_ms,
            "spot_now": price_now,
        }
        # Marcamos cool-down ANTES de despachar (evita carrera con próximo tick)
        self._last_signal_at[(symbol, side)] = now_s

        if self._order_executor is not None:
            task = asyncio.create_task(
                self._dispatch_order(signal),
                name=f"spike_arb-order-{symbol}-{side}-{now_s}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        return signal

    async def wait_inflight(self, timeout: float = 5.0) -> None:
        """Útil para tests: espera que las orders en flight terminen."""
        if not self._inflight:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._inflight, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "spike_arb.wait_inflight timeout n=%d", len(self._inflight)
            )

    # ----- internals -----

    def _detect_spike(
        self, symbol: str, now_ms: int
    ) -> Optional[tuple[str, float, float]]:
        """Devuelve (side, spike_pct, price_now) o None.

        side ∈ {"UP", "DOWN"}. spike_pct es signed (ej +0.5 o -0.5).
        """
        h = self._history.get(symbol) or []
        if len(h) < 2:
            return None
        cutoff_ms = now_ms - self.config.window_s * 1000
        # Buscamos el sample más antiguo dentro de la ventana.
        ref_price = None
        for ts_ms, p in h:
            if ts_ms >= cutoff_ms:
                ref_price = p
                break
        if ref_price is None or ref_price <= 0:
            return None
        price_now = h[-1][1]
        if price_now <= 0:
            return None
        delta_pct = (price_now / ref_price - 1.0) * 100.0
        threshold = self.config.threshold_pct
        if abs(delta_pct) < threshold:
            return None
        side = "UP" if delta_pct > 0 else "DOWN"
        return side, delta_pct, price_now

    async def _dispatch_order(self, signal: dict) -> None:
        """Ejecuta el order_executor y persiste en DB. Errores no propagan."""
        signal_at_s = signal["ts_ms"] // 1000
        # Insert pre-order (status='posting'); nos permite trackear si la
        # llamada al executor murió a mitad. Si CLOB rechaza, se hace UPDATE.
        trade_id = _persist_signal(signal, signal_at=signal_at_s)
        try:
            result = await self._order_executor(signal)  # type: ignore[misc]
        except Exception as e:
            log.exception("spike_arb.executor raised sym=%s", signal["symbol"])
            self.metrics.orders_failed += 1
            _update_status(trade_id, status="failed", extra={"error": str(e)[:300]})
            return
        if not result or not result.get("ok"):
            self.metrics.orders_failed += 1
            err = (result or {}).get("error") or "executor returned not-ok"
            _update_status(trade_id, status="failed", extra={"error": err[:300]})
            return
        order_id = result.get("order_id")
        if result.get("filled"):
            self.metrics.orders_filled += 1
            fill_price = result.get("fill_price") or signal["limit_price"]
            _update_status(
                trade_id, status="filled",
                fields={
                    "order_id": order_id,
                    "fill_price": float(fill_price),
                    "filled_at": int(time.time()),
                },
            )
            log.info(
                "spike_arb.fill sym=%s side=%s spike=%+.2f%% mid=%.4f fill=%.4f",
                signal["symbol"], signal["side"], signal["spike_pct"],
                signal["mid_at_signal"], fill_price,
            )
        else:
            # No filleó dentro del TTL → executor ya canceló. Persist como
            # cancelled para auditoría.
            self.metrics.orders_cancelled_ttl += 1
            _update_status(
                trade_id, status="cancelled",
                fields={"order_id": order_id, "closed_at": int(time.time())},
            )
            log.info(
                "spike_arb.ttl_cancel sym=%s side=%s mid=%.4f",
                signal["symbol"], signal["side"], signal["mid_at_signal"],
            )
        self.metrics.orders_posted += 1


# --- Persistencia helpers (módulo-level, importables por tests) ---

def _persist_signal(signal: dict, *, signal_at: int) -> Optional[int]:
    """Inserta una row en spike_arb_trades con status='open' (pre-fill).

    Devuelve el id (autoinc) o None si la inserción falló.
    """
    try:
        from src.db.schema import db, tx
    except Exception:
        log.exception("spike_arb.persist: schema import failed")
        return None
    try:
        with tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO spike_arb_trades (
                    symbol, side, spike_pct, mid_at_signal, limit_price,
                    size_usdc, status, signal_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    signal["symbol"], signal["side"],
                    float(signal["spike_pct"]),
                    float(signal["mid_at_signal"]),
                    float(signal["limit_price"]),
                    float(signal["size_usdc"]),
                    int(signal_at),
                ),
            )
            row_id = cur.lastrowid if hasattr(cur, "lastrowid") else None
            return row_id
    except Exception:
        log.exception("spike_arb.persist insert failed")
        return None


def _update_status(
    trade_id: Optional[int], *,
    status: str,
    fields: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> None:
    """UPDATE spike_arb_trades SET ... WHERE id=trade_id.

    `fields` mapea columna→valor (whitelisted abajo).
    `extra` (opcional) se concatena al campo bucket_slug como JSON debug —
    útil para guardar errors sin necesitar columna nueva.
    """
    if trade_id is None:
        return
    allowed = {
        "order_id", "fill_price", "filled_at", "closed_at",
        "pnl_usdc", "bucket_slug", "bucket_end_ts",
    }
    sets: list[str] = ["status = ?"]
    params: list[Any] = [status]
    f = dict(fields or {})
    if extra:
        # Encajamos el extra en bucket_slug (campo libre) si no está usado.
        existing = f.get("bucket_slug")
        f["bucket_slug"] = json.dumps(
            {"prev": existing, **extra}, default=str
        ) if existing else json.dumps(extra, default=str)
    for k, v in f.items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        params.append(v)
    params.append(int(trade_id))
    sql = f"UPDATE spike_arb_trades SET {', '.join(sets)} WHERE id = ?"
    try:
        from src.db.schema import tx
        with tx() as conn:
            conn.execute(sql, tuple(params))
    except Exception:
        log.exception("spike_arb.update_status failed id=%s", trade_id)


# --- Live order executor (paper / live) ---

async def _paper_order_executor(signal: dict) -> dict:
    """Executor "paper": simula post + TTL wait + (no) fill.

    En paper no hay un fill real — devolvemos ``filled=False`` y dejamos que
    se persista como ``cancelled``. La idea es que SPIKE_ARB en paper sirva
    solo para validar el detector + filtro de mid; el PnL real arranca en LIVE.
    """
    # Simular el TTL: en paper no esperamos realmente para no bloquear el loop.
    # Ya el detector no postea de nuevo el mismo símbolo gracias al cool-down.
    return {
        "ok": True,
        "order_id": f"PAPER-{signal['symbol']}-{signal['ts_ms']}",
        "filled": False,
        "fill_price": None,
        "error": None,
        "raw": {"mode": "paper"},
    }


async def _live_order_executor(signal: dict) -> dict:
    """Executor "live": postea LIMIT GTC al CLOB y polea hasta TTL.

    Importa ``py_clob_client_v2`` solo cuando se llama (lazy) — lo mismo
    que hace ``executor.py``. Si el SDK no está disponible o falla la firma,
    devuelve ok=False y se persiste como ``failed``.

    Nota: este path NO se ejercita en los tests unitarios (necesita CLOB
    real); su responsabilidad real está cubierta por mocks en
    ``tests/test_spike_arb.py`` vía un executor inyectable.
    """
    try:
        from src.polymarket.clob_client import (
            compute_limit_price as _clp,  # noqa: F401  — importable para tests
            get_client,
        )
    except Exception as e:
        return {"ok": False, "order_id": None, "filled": False,
                "fill_price": None, "error": f"clob import: {e}", "raw": None}

    client = get_client()
    if client is None:
        return {"ok": False, "order_id": None, "filled": False,
                "fill_price": None, "error": "CLOB not configured", "raw": None}

    # Resolución de token_id queda fuera del scope de este módulo: el
    # mid_resolver del live debe entregar mid junto a token_id, o la
    # integración real necesita una segunda llamada. Para no acoplar a
    # market discovery aquí, devolvemos failed con motivo claro.
    return {
        "ok": False,
        "order_id": None,
        "filled": False,
        "fill_price": None,
        "error": "live executor pending market resolution wiring",
        "raw": None,
    }


# --- Loop principal ---

async def spike_arb_loop() -> None:
    """Loop principal del bot spike_arb.

    - Crea la tabla on-demand
    - Conecta al WS Binance
    - Suscribe ``SpikeArb.on_binance_tick`` como callback de ticks
    """
    config = SpikeArbConfig.from_env()
    if not config.enabled:
        log.info("spike_arb: disabled (SPIKE_ARB_ENABLED!=true)")
        return

    init_table()

    # Importes diferidos para no pagar el costo si está disabled.
    from src.binance.websocket import BinanceTickerWS
    from src.config import LIVE_MODE

    log.info(
        "spike_arb: arrancando — threshold=%.2f%% window=%ss size=$%.2f "
        "ttl=%ss max_mid=%.2f mode=%s",
        config.threshold_pct, config.window_s, config.target_size_usdc,
        config.limit_ttl_s, config.max_mid_target,
        "live" if LIVE_MODE else "paper",
    )

    executor = _live_order_executor if LIVE_MODE else _paper_order_executor

    # Mid resolver mínimo: por defecto devuelve None (no hay market activo
    # resoluble). La integración real con gamma+CLOB se enchufa cuando se
    # quiera; para validar el detector en paper basta con esto.
    async def _no_mid(symbol: str, side: str) -> Optional[float]:
        return None

    arb = SpikeArb(
        config=config,
        order_executor=executor,
        mid_resolver=_no_mid,
    )

    binance_ws = BinanceTickerWS(
        symbols=config.symbols,
        on_tick=arb.on_binance_tick,
    )
    binance_task = asyncio.create_task(binance_ws.run(), name="binance-ws-spike")
    try:
        # Heartbeat cada 5 min para confirmar liveness.
        while True:
            await asyncio.sleep(300)
            log.info("spike_arb.heartbeat %s", arb.metrics.snapshot())
    except asyncio.CancelledError:
        raise
    finally:
        binance_ws.stop()
        binance_task.cancel()
        try:
            await binance_task
        except (asyncio.CancelledError, Exception):
            pass
        await arb.wait_inflight(timeout=2.0)
        log.info("spike_arb: loop terminado")
