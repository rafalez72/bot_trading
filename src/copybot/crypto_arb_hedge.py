"""Crypto arb hedge — orchestrator delta-neutral Polymarket + Binance perp.

Estrategia (Nivel B+, 2026-05-10):

- ``crypto_arb`` puro abre BUY UP/DOWN en Polymarket cuando detecta lag entre
  spot Binance y mid Polymarket. El PnL depende de la dirección final del
  bucket (UP/DOWN gana o pierde 100%).
- Este orchestrator agrega cobertura: cuando abrimos BUY UP en Polymarket,
  abrimos SHORT del símbolo en Binance perp con el mismo notional. Si el spot
  baja antes del close (perdemos en Polymarket) ganamos en la perp; si sube
  (ganamos en Polymarket) perdemos en la perp. El neto captura SOLO el lag
  del mid Polymarket vs spot — no la dirección.
- Esto permite bajar el threshold de edge (5pp vs ~10pp del crypto_arb puro)
  porque el hedge anula el riesgo direccional. El edge ahora es prácticamente
  costo de fees + funding rate + slippage.

Atomicidad:

- Abrir poly primero (más probable que falle por orderbook thin / liquidez).
- Si poly OK → abrir perp SHORT con qty calculada del notional.
- Si perp falla → rollback poly INMEDIATO (force_close al mid actual).
- Persist row en ``hedge_trades`` con el resultado de las dos piernas.

Riesgos / defensas:

- ``HEDGE_MAX_FUNDING_RATE``: si el funding del perp es muy positivo,
  pagamos funding mientras estamos short → abort antes de abrir.
- ``get_margin_info().available < required_margin`` → abort sin tocar poly.
- Rollback paga slippage del poly close (~$0.10-0.30 por trade en paper).
  Es el costo de la atomicidad.

Activación: env var ``HEDGE_ENABLED=true`` (gated). Validar SIEMPRE en paper
antes de live — la pierna perp en paper es simulada (no toca Binance real).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from src.binance.perp_client import BinancePerpClient, BinancePerpError
from src.config import (
    HEDGE_BET_USDC,
    HEDGE_CHECK_INTERVAL_S,
    HEDGE_ENABLED,
    HEDGE_LEVERAGE,
    HEDGE_MAX_FUNDING_RATE,
    HEDGE_MIN_EDGE,
)
from src.config import DB_PATH
from src.copybot.crypto_arb_signals import edge_vs_mid, get_min_edge
from src.copybot.tradebook import (
    MODE as TRADEBOOK_MODE,
    TABLE as TRADEBOOK_TABLE,
    open_position,
)

# Outbox para rows de hedge_trades que no se pudieron persistir (DB locked
# o exception transitoria). Drena en el próximo startup vía
# :func:`drain_hedge_outbox`. Mismo pattern que executor.LIVE_OUTBOX_PATH.
HEDGE_OUTBOX_PATH = DB_PATH.parent / "hedge_trades_outbox.jsonl"

log = logging.getLogger(__name__)

# Símbolos hedgeables — los que tienen mercado spot Y perp en Binance.
# Mismo set que crypto_arb (HYPE no aplica: no hay spot+perp listado).
HEDGE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT")


# --- Tipos para inyección (testabilidad) ---

# Callable que abre la pata Polymarket. Compatible con
# ``tradebook.open_position`` por keyword args. Devuelve (pid, reject_reason).
PolyOpener = Callable[..., tuple[Optional[int], Optional[str]]]

# Callable async que abre/cierra perp. En tests se mockea — en prod usa
# BinancePerpClient. Recibe dict con la order request, devuelve dict
# con resultado: {"ok": bool, "order_id": str|None, "avg_price": float|None,
# "qty": float|None, "error": str|None, "raw": dict|None}.
PerpExecutor = Callable[[dict], Awaitable[dict]]


# --- Config ---

@dataclass
class HedgeConfig:
    enabled: bool = False
    min_edge: float = 0.05
    bet_usdc: float = 10.0
    leverage: int = 2
    max_funding_rate: float = 0.0005
    check_interval_s: float = 15.0
    symbols: tuple[str, ...] = HEDGE_SYMBOLS

    @classmethod
    def from_env(cls) -> "HedgeConfig":
        return cls(
            enabled=HEDGE_ENABLED,
            min_edge=HEDGE_MIN_EDGE,
            bet_usdc=HEDGE_BET_USDC,
            leverage=HEDGE_LEVERAGE,
            max_funding_rate=HEDGE_MAX_FUNDING_RATE,
            check_interval_s=HEDGE_CHECK_INTERVAL_S,
        )


# --- Métricas ---

@dataclass
class _HedgeMetrics:
    cycles: int = 0
    setups_evaluated: int = 0
    skipped_low_edge: int = 0
    skipped_funding_rate: int = 0
    skipped_margin: int = 0
    poly_failed: int = 0
    perp_failed: int = 0
    perp_rollbacks: int = 0
    opens_ok: int = 0
    closes_ok: int = 0
    started_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        return {
            "uptime_s": round(time.time() - self.started_at, 1),
            "cycles": self.cycles,
            "setups_evaluated": self.setups_evaluated,
            "skipped": {
                "low_edge": self.skipped_low_edge,
                "funding_rate": self.skipped_funding_rate,
                "margin": self.skipped_margin,
            },
            "poly_failed": self.poly_failed,
            "perp_failed": self.perp_failed,
            "perp_rollbacks": self.perp_rollbacks,
            "opens_ok": self.opens_ok,
            "closes_ok": self.closes_ok,
        }


# --- Schema (on-demand) ---

# DDL idempotente. INTEGER PRIMARY KEY AUTOINCREMENT en SQLite, BIGSERIAL
# en Postgres (vía traducción del wrapper). Mantenemos el schema simple — el
# auditing detallado vive en `raw` (JSON).
_TABLE_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS hedge_trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_slug        TEXT NOT NULL,
    symbol             TEXT,
    side               TEXT,
    poly_trade_id      INTEGER,
    poly_trade_table   TEXT,
    perp_order_id      TEXT,
    perp_qty           REAL,
    perp_entry_price   REAL,
    spot_at_entry      REAL,
    edge_at_open       REAL,
    status             TEXT DEFAULT 'open',
    pnl_poly_usdc      REAL,
    pnl_perp_usdc      REAL,
    pnl_total_usdc     REAL,
    fees_total_usdc    REAL,
    opened_at          INTEGER,
    closed_at          INTEGER,
    raw                TEXT
)
"""

_TABLE_DDL_PG = """
CREATE TABLE IF NOT EXISTS hedge_trades (
    id                 BIGSERIAL PRIMARY KEY,
    bucket_slug        TEXT NOT NULL,
    symbol             TEXT,
    side               TEXT,
    poly_trade_id      BIGINT,
    poly_trade_table   TEXT,
    perp_order_id      TEXT,
    perp_qty           DOUBLE PRECISION,
    perp_entry_price   DOUBLE PRECISION,
    spot_at_entry      DOUBLE PRECISION,
    edge_at_open       DOUBLE PRECISION,
    status             TEXT DEFAULT 'open',
    pnl_poly_usdc      DOUBLE PRECISION,
    pnl_perp_usdc      DOUBLE PRECISION,
    pnl_total_usdc     DOUBLE PRECISION,
    fees_total_usdc    DOUBLE PRECISION,
    opened_at          BIGINT,
    closed_at          BIGINT,
    raw                JSONB
)
"""

_TABLE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_hedge_status ON hedge_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_hedge_bucket ON hedge_trades(bucket_slug)",
    "CREATE INDEX IF NOT EXISTS idx_hedge_opened_at ON hedge_trades(opened_at DESC)",
]

# Migración idempotente: ``poly_trade_table`` se agregó después del primer
# release del schema. ALTER TABLE ... ADD COLUMN IF NOT EXISTS es soportado
# por SQLite ≥3.35 y Postgres ≥9.6 — ambos casos cubiertos.
_MIGRATIONS = [
    "ALTER TABLE hedge_trades ADD COLUMN poly_trade_table TEXT",
]


def init_table() -> None:
    """Crea la tabla ``hedge_trades`` + índices si no existen.

    Idempotente. Compatible SQLite + Postgres (el wrapper traduce
    INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL). También aplica
    migraciones aditivas (ALTER TABLE ADD COLUMN) ignorando errores de
    "columna ya existe" para mantener la idempotencia.
    """
    try:
        from src.db.schema import db, BACKEND
        ddl = _TABLE_DDL_PG if BACKEND == "postgres" else _TABLE_DDL_SQLITE
        with db() as conn:
            conn.execute(ddl)
            for ix in _TABLE_INDEXES:
                conn.execute(ix)
            # Migraciones: best-effort. Si la columna ya existe, el motor
            # tira error que ignoramos (idempotencia). Cualquier otro error
            # también se traga porque rompe el arranque del loop sino, y
            # el resto del schema ya está OK.
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    msg = str(e).lower()
                    if "duplicate column" in msg or "already exists" in msg:
                        continue
                    log.debug(
                        "crypto_arb_hedge.init_table migration skipped: %s (%s)",
                        stmt, e,
                    )
    except Exception:
        log.exception("crypto_arb_hedge.init_table failed")


# --- Default executors (live) ---

async def _default_perp_executor(req: dict) -> dict:
    """Executor real contra Binance perp. Mantenido separado para mockeo.

    Espera ``req`` con: symbol, side ('BUY'|'SELL'), quantity, position_side,
    action ('open'|'close'). Devuelve resultado normalizado.
    """
    action = req.get("action", "open")
    try:
        async with BinancePerpClient() as c:
            if action == "open":
                resp = await c.place_market_order(
                    symbol=req["symbol"],
                    side=req["side"],
                    quantity=float(req["quantity"]),
                    position_side=req.get("position_side", "SHORT"),
                    client_order_id=req.get("client_order_id"),
                )
            else:
                resp = await c.close_position(
                    symbol=req["symbol"],
                    position_side=req.get("position_side", "SHORT"),
                    quantity=req.get("quantity"),
                )
        # Normalizar fields de la response Binance
        avg = float(resp.get("avgPrice") or 0.0)
        qty = float(resp.get("executedQty") or 0.0)
        return {
            "ok": (resp.get("status") or "").upper() in ("FILLED", "PARTIALLY_FILLED")
                  or qty > 0,
            "order_id": str(resp.get("orderId") or ""),
            "avg_price": avg,
            "qty": qty,
            "error": None,
            "raw": resp,
        }
    except BinancePerpError as e:
        return {
            "ok": False, "order_id": None, "avg_price": None, "qty": None,
            "error": f"perp_error code={e.code} msg={e}",
            "raw": getattr(e, "payload", None),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False, "order_id": None, "avg_price": None, "qty": None,
            "error": f"exception: {e}", "raw": None,
        }


# --- Orchestrator ---

class CryptoArbHedge:
    """Orquesta la apertura atómica Polymarket BUY + Binance perp SHORT.

    No corre el loop de detección de signals — eso vive en
    :func:`crypto_arb_hedge_loop` (que reusa la lógica de detección de
    ``crypto_arb`` y delega la apertura aquí). Esta clase es testeable como
    unidad: se le pasa una decisión ya armada y un par de ``order_executors``
    mockeables.

    Dependencias inyectables (testing):

    - ``poly_opener``: callable que ejecuta la pata Polymarket. Default:
      ``tradebook.open_position`` (paper o live según TRADEBOOK_MODE).
    - ``perp_executor``: callable async que ejecuta la pata Binance.
      Default: :func:`_default_perp_executor` (BinancePerpClient real).
    - ``poly_rollback``: callable sync que cierra force-close la pata
      Polymarket cuando la perp falla. Default: ``tradebook.force_close``.
    """

    def __init__(
        self,
        config: HedgeConfig | None = None,
        *,
        poly_opener: PolyOpener | None = None,
        perp_executor: PerpExecutor | None = None,
        poly_rollback: Callable[[int, float, str], None] | None = None,
    ) -> None:
        self.config = config or HedgeConfig.from_env()
        self._poly_opener = poly_opener or open_position
        self._perp_executor = perp_executor or _default_perp_executor
        self._poly_rollback = poly_rollback
        self.metrics = _HedgeMetrics()

    # ----- public API -----

    async def evaluate_and_open(
        self,
        *,
        bucket_slug: str,
        condition_id: str,
        outcome: str,           # 'Up' | 'Down'
        outcome_index: int,
        symbol: str,
        spot_now: float,
        secs_to_close: float,
        spot_move_pct: float,
        mid_up: float,
        mid_for_side: float,
        end_ts: int,
        funding_rate: float | None = None,
        margin_available: float | None = None,
    ) -> tuple[Optional[dict], Optional[str]]:
        """Evalúa setup + abre atómico si el edge supera el threshold.

        Args:
            bucket_slug: slug del market (``btc-updown-5m-1715000000``).
            condition_id: conditionId Polymarket.
            outcome: ``'Up'`` o ``'Down'``.
            outcome_index: 0 para Up, 1 para Down.
            symbol: símbolo perp Binance (``BTCUSDT``).
            spot_now: precio spot actual.
            secs_to_close: segundos hasta el cierre del bucket.
            spot_move_pct: movimiento spot acumulado en el bucket (%).
            mid_up: mid del lado UP en Polymarket.
            mid_for_side: mid del lado a comprar (mid_up si UP, 1-mid_up si DOWN).
            end_ts: epoch del cierre del bucket.
            funding_rate: rate del perp (decimal). Si > max → abort.
            margin_available: USDT disponibles. Si < required → abort.

        Returns:
            (record_dict, error_reason). record_dict = None si abort/falla.
        """
        self.metrics.setups_evaluated += 1

        # 1. Edge gate. ``edge_vs_mid`` decide side (Up/Down) y reporta edge.
        edge, side_label, p_up = edge_vs_mid(
            spot_move_pct=spot_move_pct,
            secs_left=float(secs_to_close),
            symbol=symbol,
            mid_up=mid_up,
            threshold=self.config.min_edge,
        )
        # En el hedge usamos un threshold MÁS BAJO que crypto_arb puro porque
        # el delta-hedge anula el riesgo direccional. El caller debería
        # pasar `outcome` ya consistente con `side_label`, pero validamos.
        if side_label is None or side_label != outcome:
            self.metrics.skipped_low_edge += 1
            return None, f"low_edge edge={edge:.3f} side={side_label}"
        if edge < self.config.min_edge:
            self.metrics.skipped_low_edge += 1
            return None, f"edge_below_min {edge:.3f}<{self.config.min_edge}"

        # 2. Funding rate gate. Si pagaríamos demasiado funding mientras estamos
        # SHORT, no vale la pena el hedge para un bucket de 5min.
        if funding_rate is not None and funding_rate > self.config.max_funding_rate:
            self.metrics.skipped_funding_rate += 1
            return None, (
                f"funding_too_high {funding_rate:.4f}>{self.config.max_funding_rate}"
            )

        # 3. Margin gate. Required = bet_usdc / leverage (initial margin del SHORT).
        required_margin = self.config.bet_usdc / max(self.config.leverage, 1)
        if margin_available is not None and margin_available < required_margin:
            self.metrics.skipped_margin += 1
            return None, (
                f"margin_insufficient avail={margin_available:.2f}<req={required_margin:.2f}"
            )

        # 4. Open atómico.
        decision = {
            "bucket_slug": bucket_slug,
            "condition_id": condition_id,
            "outcome": outcome,
            "outcome_index": outcome_index,
            "symbol": symbol,
            "spot_now": spot_now,
            "secs_to_close": secs_to_close,
            "spot_move_pct": spot_move_pct,
            "mid_up": mid_up,
            "mid_for_side": mid_for_side,
            "end_ts": end_ts,
            "p_up": p_up,
            "edge": edge,
        }
        return await self._open_atomic(decision)

    async def _open_atomic(
        self, decision: dict
    ) -> tuple[Optional[dict], Optional[str]]:
        """Open Polymarket BUY + Binance perp SHORT atómicamente.

        Orden de operaciones:
          1. Open poly (más probable que falle: liquidez thin, slippage gate).
          2. Open perp SHORT con qty calculada del notional.
          3. Si perp falla → rollback poly inmediato (force_close).
          4. Persist row hedge_trades con el resultado (status open o leg_failed).

        Devuelve (record_dict, error_str). record_dict tiene poly_pid,
        perp_order_id, perp_qty, etc. error_str = None si todo OK.
        """
        bucket_slug = decision["bucket_slug"]
        symbol = decision["symbol"]
        outcome = decision["outcome"]
        outcome_index = decision["outcome_index"]
        spot_now = decision["spot_now"]
        edge = decision["edge"]
        end_ts = decision["end_ts"]

        src_id = f"crypto_arb_hedge:{bucket_slug}:{outcome}"
        raw_payload = {
            "source": "crypto_arb_hedge",
            "slug": bucket_slug,
            "symbol": symbol,
            "side": outcome,
            "outcome_index": outcome_index,
            "spot_now": spot_now,
            "spot_move_pct": decision["spot_move_pct"],
            "secs_to_close": decision["secs_to_close"],
            "p_up": decision["p_up"],
            "edge": edge,
            "end_ts": end_ts,
            "mode": TRADEBOOK_MODE,
        }

        # 1. Open poly. tradebook.open_position es sync — offload al thread
        # pool para no bloquear el event loop.
        try:
            poly_pid, poly_reject = await asyncio.to_thread(
                self._poly_opener,
                source_wallet="crypto_arb_hedge",
                source_trade_id=src_id,
                condition_id=decision["condition_id"],
                outcome=outcome,
                outcome_index=outcome_index,
                price=decision["mid_for_side"],
                timestamp=int(time.time()),
                raw=raw_payload,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("crypto_arb_hedge.poly_open_exception slug=%s", bucket_slug)
            self.metrics.poly_failed += 1
            return None, f"poly_exception:{e}"

        if not poly_pid:
            self.metrics.poly_failed += 1
            log.info(
                "crypto_arb_hedge.poly_failed slug=%s reason=%s",
                bucket_slug, poly_reject,
            )
            return None, f"poly_failed:{poly_reject}"

        # 2. Open perp SHORT. qty derivada del notional Polymarket
        # (bet_usdc) — NO del leverage: el hedge cubre el delta DIRECCIONAL
        # del notional poly (~$10). El leverage solo decide cuánto margen
        # bloqueamos, no el size del short.
        perp_qty = self._compute_perp_qty(spot_now, self.config.bet_usdc)
        perp_req = {
            "action": "open",
            "symbol": symbol,
            "side": "SELL",
            "quantity": perp_qty,
            "position_side": "SHORT",
            "client_order_id": _client_order_id(bucket_slug, "open"),
        }
        try:
            perp_resp = await self._perp_executor(perp_req)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "crypto_arb_hedge.perp_exception slug=%s — rolling back poly", bucket_slug
            )
            await self._rollback_poly(poly_pid, decision["mid_for_side"])
            self.metrics.perp_failed += 1
            self.metrics.perp_rollbacks += 1
            self._persist_hedge(
                bucket_slug=bucket_slug, decision=decision,
                poly_pid=poly_pid, perp_resp={
                    "ok": False, "error": f"exception:{e}", "order_id": None,
                    "avg_price": None, "qty": None, "raw": None,
                },
                perp_qty=perp_qty, status="leg_failed",
            )
            return None, f"perp_exception:{e}"

        if not perp_resp.get("ok"):
            log.warning(
                "crypto_arb_hedge.perp_failed slug=%s err=%s — rollback poly pid=%d",
                bucket_slug, perp_resp.get("error"), poly_pid,
            )
            await self._rollback_poly(poly_pid, decision["mid_for_side"])
            self.metrics.perp_failed += 1
            self.metrics.perp_rollbacks += 1
            self._persist_hedge(
                bucket_slug=bucket_slug, decision=decision,
                poly_pid=poly_pid, perp_resp=perp_resp,
                perp_qty=perp_qty, status="leg_failed",
            )
            return None, f"perp_failed:{perp_resp.get('error')}"

        # 3. Persist hedge_trades row (status open).
        record = self._persist_hedge(
            bucket_slug=bucket_slug, decision=decision,
            poly_pid=poly_pid, perp_resp=perp_resp,
            perp_qty=perp_qty, status="open",
        )
        self.metrics.opens_ok += 1
        log.info(
            "crypto_arb_hedge.open_ok slug=%s symbol=%s side=%s "
            "poly_pid=%d perp_oid=%s qty=%.6f spot=%.2f edge=%.3f",
            bucket_slug, symbol, outcome, poly_pid,
            perp_resp.get("order_id"), perp_qty, spot_now, edge,
        )
        return record, None

    async def _rollback_poly(self, poly_pid: int, exit_price: float) -> None:
        """Cierra la pata Polymarket vía force_close para anular exposure.

        Si ``poly_rollback`` no fue inyectado, importamos lazily desde
        ``tradebook`` (default paper o live). Cualquier excepción se
        loggea pero no se propaga — el caller ya está en el path de error.
        """
        try:
            if self._poly_rollback is not None:
                await asyncio.to_thread(
                    self._poly_rollback, poly_pid, exit_price, "hedge_rollback"
                )
            else:
                from src.copybot.tradebook import force_close
                await asyncio.to_thread(
                    force_close, poly_pid, exit_price, reason="hedge_rollback"
                )
        except Exception:
            log.exception(
                "crypto_arb_hedge.rollback_failed pid=%d — exposure NETA queda abierta!",
                poly_pid,
            )

    def _compute_perp_qty(self, spot_now: float, notional_usdc: float) -> float:
        """qty = notional / spot. Trunca a 6 decimales (suficiente para BTC=8 dec stepSize).

        En live, el caller debería redondear al stepSize del símbolo (ver
        Binance exchangeInfo). Acá truncamos a 6 decimales para no saturar
        precisión innecesaria en logs/tests.
        """
        if spot_now <= 0:
            return 0.0
        return round(notional_usdc / spot_now, 6)

    def _persist_hedge(
        self, *,
        bucket_slug: str,
        decision: dict,
        poly_pid: int,
        perp_resp: dict,
        perp_qty: float,
        status: str,
    ) -> dict:
        """Inserta row en hedge_trades y devuelve dict equivalente al row.

        ``status``:
          - ``open``    → ambas piernas abiertas OK
          - ``leg_failed`` → perp falló, poly fue rollbackeado
          - ``closed``  → seteado por el settler post bucket-end
        """
        now = int(time.time())
        raw = {
            **decision,
            "perp_resp": {
                "ok": perp_resp.get("ok"),
                "order_id": perp_resp.get("order_id"),
                "avg_price": perp_resp.get("avg_price"),
                "qty": perp_resp.get("qty"),
                "error": perp_resp.get("error"),
            },
            "tradebook_mode": TRADEBOOK_MODE,
        }
        record = {
            "bucket_slug": bucket_slug,
            "symbol": decision["symbol"],
            "side": decision["outcome"],
            "poly_trade_id": poly_pid,
            "poly_trade_table": TRADEBOOK_TABLE,
            "perp_order_id": perp_resp.get("order_id"),
            "perp_qty": perp_qty,
            "perp_entry_price": perp_resp.get("avg_price"),
            "spot_at_entry": decision["spot_now"],
            "edge_at_open": decision["edge"],
            "status": status,
            "opened_at": now,
            "raw": raw,
        }
        try:
            from src.db.schema import tx
            with tx() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO hedge_trades
                        (bucket_slug, symbol, side, poly_trade_id, poly_trade_table,
                         perp_order_id, perp_qty, perp_entry_price,
                         spot_at_entry, edge_at_open, status,
                         opened_at, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bucket_slug,
                        decision["symbol"],
                        decision["outcome"],
                        poly_pid,
                        TRADEBOOK_TABLE,
                        perp_resp.get("order_id"),
                        perp_qty,
                        perp_resp.get("avg_price"),
                        decision["spot_now"],
                        decision["edge"],
                        status,
                        now,
                        json.dumps(raw, separators=(",", ":"), default=str),
                    ),
                )
                # SQLite expone lastrowid; PG wrapper también.
                record["id"] = getattr(cur, "lastrowid", None)
        except Exception as e:
            # Fallback outbox — si la pierna perp ya está abierta on-chain,
            # NO podemos perder este row (la position queda "huérfana" sin
            # tracking). Escribimos a disco para que el próximo startup la
            # recupere/concilie.
            log.exception(
                "crypto_arb_hedge.persist_failed slug=%s — escribiendo a outbox",
                bucket_slug,
            )
            record["id"] = None
            try:
                _write_hedge_outbox(record, error=str(e)[:300])
            except Exception:
                log.exception(
                    "crypto_arb_hedge.outbox_write_failed slug=%s — "
                    "exposure SIN tracking!", bucket_slug,
                )
        return record

    async def close_perp_for_hedge(
        self,
        hedge_id: int,
        symbol: str,
        perp_qty: float,
        spot_now: float,
        poly_pnl_usdc: float,
        perp_entry_price: float,
    ) -> tuple[bool, dict]:
        """Cierra el SHORT del perp y settle hedge_trades.

        Llamado por el settler después que el bucket cerró y la pata
        Polymarket fue settled (paper.settle_resolved o equivalente live).

        PnL:
          - perp SHORT: (entry - exit) * qty (positivo si spot bajó).
          - poly: viene del paper_trade ya settled (positivo o negativo).
          - fees_estimate: fees de las dos piernas (~0.04% de notional cada
            lado en taker market orders).

        Devuelve (ok, perp_resp).
        """
        perp_req = {
            "action": "close",
            "symbol": symbol,
            "side": "BUY",   # close SHORT
            "quantity": perp_qty,
            "position_side": "SHORT",
            "client_order_id": _client_order_id(f"close-{hedge_id}", "close"),
        }
        try:
            perp_resp = await self._perp_executor(perp_req)
        except Exception as e:  # noqa: BLE001
            log.exception("crypto_arb_hedge.close_perp_exception hedge_id=%d", hedge_id)
            return False, {"ok": False, "error": f"exception:{e}"}

        if not perp_resp.get("ok"):
            log.error(
                "crypto_arb_hedge.close_perp_failed hedge_id=%d err=%s",
                hedge_id, perp_resp.get("error"),
            )
            return False, perp_resp

        # PnL perp. Si avg_price del close es 0 (paper sin fill price),
        # fallback a spot_now.
        exit_price = float(perp_resp.get("avg_price") or spot_now)
        perp_pnl = (perp_entry_price - exit_price) * perp_qty
        # Fees estimadas: 0.04% taker x 2 (open + close) sobre notional.
        notional = perp_entry_price * perp_qty
        fees = 2 * notional * 0.0004
        total_pnl = poly_pnl_usdc + perp_pnl - fees

        try:
            from src.db.schema import tx
            with tx() as conn:
                conn.execute(
                    """
                    UPDATE hedge_trades
                    SET status='closed', closed_at=?,
                        pnl_poly_usdc=?, pnl_perp_usdc=?,
                        pnl_total_usdc=?, fees_total_usdc=?
                    WHERE id=?
                    """,
                    (
                        int(time.time()),
                        poly_pnl_usdc, perp_pnl, total_pnl, fees,
                        hedge_id,
                    ),
                )
        except Exception:
            log.exception("crypto_arb_hedge.close_perp persist failed hedge_id=%d", hedge_id)

        # Notif Telegram (fix bug #4): close_perp_for_hedge era silente —
        # solo metrics, ningún notif al user. Ahora gain/loss con acumulado
        # strategy-specific (sum pnl_total de hedge_trades closed).
        try:
            _notify_hedge_closed(
                hedge_id=hedge_id, symbol=symbol, pnl_total=total_pnl,
            )
        except Exception:
            log.exception(
                "crypto_arb_hedge.close_perp notif failed hedge_id=%d", hedge_id,
            )

        self.metrics.closes_ok += 1
        return True, perp_resp


# --- Outbox (persistencia anti-locked DB) ---

def _write_hedge_outbox(record: dict, *, error: str | None = None) -> None:
    """Escribe un record al outbox JSONL (best-effort, atómico por línea).

    Llamado cuando el INSERT a ``hedge_trades`` falla. El record incluye
    suficiente info para que ``drain_hedge_outbox()`` lo reinserte al
    startup. Notifica vía Telegram porque significa que hay una posición
    perp abierta on-chain sin tracking en DB → urgente.
    """
    payload = {
        "queued_at": int(time.time()),
        "last_error": error,
        "record": record,
    }
    HEDGE_OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HEDGE_OUTBOX_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    try:
        from src.copybot.notifier import send
        send(
            f"⚠️ *HEDGE OUTBOX*: hedge_trades INSERT falló. "
            f"slug=`{record.get('bucket_slug','?')[:30]}` "
            f"perp_oid=`{(record.get('perp_order_id') or 'none')[:14]}` "
            f"poly_pid=`{record.get('poly_trade_id','?')}` "
            f"— exposure perp posible sin tracking, revisar!"
        )
    except Exception:
        pass


def drain_hedge_outbox() -> int:
    """Drena ``HEDGE_OUTBOX_PATH`` al startup. Idempotente por (slug, perp_order_id).

    Reinserta cada record en ``hedge_trades`` si todavía no existe. Las
    líneas que vuelven a fallar quedan en el outbox para el próximo intento.

    Devuelve cantidad de rows drenados (insertados o ya presentes).
    """
    if not HEDGE_OUTBOX_PATH.exists():
        return 0
    try:
        with open(HEDGE_OUTBOX_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        log.warning("drain_hedge_outbox: no se pudo leer %s: %s", HEDGE_OUTBOX_PATH, e)
        return 0

    drained = 0
    failed: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            rec = entry.get("record") if isinstance(entry, dict) else None
            if not rec:
                continue
            try:
                from src.db.schema import db, tx
                # Dedupe: si ya existe row con mismo perp_order_id, skip.
                perp_oid = rec.get("perp_order_id")
                if perp_oid:
                    with db() as conn:
                        dup = conn.execute(
                            "SELECT id FROM hedge_trades WHERE perp_order_id=?",
                            (perp_oid,),
                        ).fetchone()
                        if dup:
                            drained += 1
                            continue
                with tx() as conn:
                    conn.execute(
                        """
                        INSERT INTO hedge_trades
                            (bucket_slug, symbol, side, poly_trade_id, poly_trade_table,
                             perp_order_id, perp_qty, perp_entry_price,
                             spot_at_entry, edge_at_open, status, opened_at, raw)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec.get("bucket_slug"),
                            rec.get("symbol"),
                            rec.get("side"),
                            rec.get("poly_trade_id"),
                            rec.get("poly_trade_table"),
                            rec.get("perp_order_id"),
                            rec.get("perp_qty"),
                            rec.get("perp_entry_price"),
                            rec.get("spot_at_entry"),
                            rec.get("edge_at_open"),
                            rec.get("status") or "open",
                            rec.get("opened_at") or int(time.time()),
                            json.dumps(rec.get("raw") or {}, separators=(",", ":"), default=str),
                        ),
                    )
                drained += 1
            except Exception as e:
                log.warning(
                    "drain_hedge_outbox: INSERT aún falla (slug=%s): %s",
                    rec.get("bucket_slug"), e,
                )
                failed.append(line)
        except json.JSONDecodeError:
            continue

    try:
        if failed:
            tmp = HEDGE_OUTBOX_PATH.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(failed) + "\n")
            tmp.replace(HEDGE_OUTBOX_PATH)
        else:
            HEDGE_OUTBOX_PATH.unlink()
    except Exception as e:
        log.warning("drain_hedge_outbox: cleanup outbox failed: %s", e)

    if drained:
        log.warning(
            "drain_hedge_outbox: %d hedge_trades drenados al startup (de %d)",
            drained, len(lines),
        )
    return drained


# --- Recovery de orphans ---

async def recover_orphan_perps(
    perp_client_factory: Callable[[], Any] | None = None,
    *,
    notifier: Callable[[str], None] | None = None,
) -> int:
    """Recovery de hedge_trades con status='leg_failed' y perp_order_id seteado.

    Caso de uso:
      - Runner abrió poly OK + perp OK, pero crasheo antes de marcar status='open'.
      - O bien: persist falló y la pierna perp quedó abierta on-chain.

    Procedimiento:
      1. Query rows ``status='leg_failed' AND perp_order_id IS NOT NULL``.
      2. Para cada → ``perp_client.get_position(symbol)``: si qty != 0 → close.
      3. Update ``status='recovered'`` (ya no es leg_failed, lo cerramos).
      4. Notif Telegram urgente.

    Args:
        perp_client_factory: callable que devuelve un context-manager
            ``BinancePerpClient``-like. Default: ``lambda: BinancePerpClient()``.
            Inyectable para tests (mock client).
        notifier: callable sync para notificar (default: ``notifier.send``).

    Devuelve cantidad de orphans recuperados (cerrados o sin position abierta).
    """
    try:
        from src.db.schema import db, tx
    except Exception:
        log.exception("recover_orphan_perps: no se pudo importar schema")
        return 0

    try:
        with db() as conn:
            rows = conn.execute(
                """
                SELECT id, symbol, perp_order_id, perp_qty, bucket_slug
                FROM hedge_trades
                WHERE status='leg_failed' AND perp_order_id IS NOT NULL
                """,
            ).fetchall()
    except Exception:
        log.exception("recover_orphan_perps: query failed")
        return 0

    if not rows:
        return 0

    log.warning(
        "recover_orphan_perps: %d orphans candidatos a recuperar", len(rows),
    )

    if perp_client_factory is None:
        def perp_client_factory():  # noqa: E306
            return BinancePerpClient()

    if notifier is None:
        try:
            from src.copybot.notifier import send as _send
            notifier = _send
        except Exception:
            notifier = lambda _t: None  # noqa: E731

    recovered = 0
    for r in rows:
        hedge_id = r["id"] if hasattr(r, "__getitem__") else r[0]
        symbol = r["symbol"] if hasattr(r, "__getitem__") else r[1]
        perp_oid = r["perp_order_id"] if hasattr(r, "__getitem__") else r[2]
        perp_qty_db = r["perp_qty"] if hasattr(r, "__getitem__") else r[3]
        slug = r["bucket_slug"] if hasattr(r, "__getitem__") else r[4]

        try:
            async with perp_client_factory() as client:
                pos = await client.get_position(symbol, position_side="SHORT")
                qty = abs(float(pos.get("qty") or 0.0))
                if qty > 1e-9:
                    log.warning(
                        "recover_orphan_perps: closing orphan hedge_id=%s symbol=%s qty=%.6f",
                        hedge_id, symbol, qty,
                    )
                    await client.close_position(
                        symbol=symbol, position_side="SHORT", quantity=qty,
                    )
                else:
                    log.info(
                        "recover_orphan_perps: hedge_id=%s symbol=%s sin position "
                        "abierta on-chain — solo update status", hedge_id, symbol,
                    )
        except Exception:
            log.exception(
                "recover_orphan_perps: fail closing hedge_id=%s — dejo status='leg_failed'",
                hedge_id,
            )
            continue

        try:
            with tx() as conn:
                conn.execute(
                    "UPDATE hedge_trades SET status='recovered', closed_at=? WHERE id=?",
                    (int(time.time()), hedge_id),
                )
            recovered += 1
            try:
                notifier(
                    f"🚨 *HEDGE RECOVERY*: orphan perp cerrado. "
                    f"slug=`{(slug or '?')[:30]}` symbol=`{symbol}` "
                    f"qty=`{perp_qty_db}` hedge_id=`{hedge_id}`"
                )
            except Exception:
                pass
        except Exception:
            log.exception(
                "recover_orphan_perps: update status failed hedge_id=%s",
                hedge_id,
            )

    if recovered:
        log.warning(
            "recover_orphan_perps: %d orphans recuperados (de %d)",
            recovered, len(rows),
        )
    return recovered


# --- Notif coverage (fix bug #4): hedge close helper ---

def _accumulated_hedge_pnl() -> float:
    """Suma pnl_total_usdc de hedge_trades closed."""
    try:
        from src.db.schema import db
        with db() as conn:
            cur = conn.execute(
                "SELECT COALESCE(SUM(pnl_total_usdc), 0) AS s FROM hedge_trades "
                "WHERE status='closed' AND pnl_total_usdc IS NOT NULL"
            )
            row = cur.fetchone()
            if row is None:
                return 0.0
            return float(row["s"] or 0.0)
    except Exception:
        log.exception("crypto_arb_hedge._accumulated_hedge_pnl failed")
        return 0.0


def _notify_hedge_closed(
    *, hedge_id: int, symbol: str, pnl_total: float,
) -> None:
    """Dispara notif Telegram al cerrar un hedge_trade (fix bug #4)."""
    if abs(pnl_total) < 1e-9:
        return
    from src.copybot.notifier import gain as notif_gain, loss as notif_loss
    accumulated = _accumulated_hedge_pnl()
    pt = {
        "raw": {
            "slug": (symbol or "").lower(),
            "title": f"hedge {symbol} #{hedge_id}",
        },
    }
    if pnl_total > 0:
        notif_gain(pnl_total, accumulated, pt=pt, bucket_label="hedge")
    else:
        notif_loss(abs(pnl_total), accumulated, pt=pt, bucket_label="hedge")


# --- Helpers ---

def _client_order_id(slug: str, action: str) -> str:
    """clientOrderId determinístico — hasta 36 chars (Binance limit).

    Formato: ``cah-{action}-{slug_short}-{ts_ms_short}``. ``cah`` = crypto
    arb hedge prefix. Determinístico por (slug, action, ts) para idempotencia
    en reintentos client-side.
    """
    short = (slug or "").replace("-", "")[:18]
    ts = int(time.time() * 1000) % 10_000_000
    cid = f"cah-{action[:4]}-{short}-{ts}"
    return cid[:36]


# --- Detector wiring (bug #7) ---

# Pre-close window: solo evaluamos buckets que cierran en próximos 180s.
# El edge del lag aparece sobre todo en los últimos 1-3 min del bucket 5min.
_PRE_CLOSE_WINDOW_S = 180.0
_MIN_BUCKET_AGE_S = 60.0
_SPOT_HISTORY_CAP = 360  # ~6 min × 1 msg/s


def _hedge_match_to_symbol(slug_prefix: str) -> str | None:
    """Mapa slug-prefix → symbol Binance. Reusa el dict de crypto_arb (readonly)."""
    try:
        from src.copybot.crypto_arb import SLUG_PREFIX_TO_SYMBOL
        return SLUG_PREFIX_TO_SYMBOL.get(slug_prefix)
    except Exception:
        return None


def _hedge_evaluate_setup(
    market: dict,
    config: HedgeConfig,
    spot_history: dict[str, list[tuple[int, float]]],
    binance_ws: Any,
) -> dict | None:
    """Evalúa un market y devuelve setup dict listo para ``evaluate_and_open``.

    Usa ``edge_vs_mid`` con el threshold del hedge (más bajo que crypto_arb
    puro porque el delta-hedge anula el riesgo direccional). Devuelve None
    si skip (fuera de ventana, sin spot history, edge insuficiente, etc.).
    """
    from src.copybot.crypto_arb import _market_midpoint  # readonly import

    now = int(time.time())
    end_ts = market.get("_end_ts")
    if not end_ts:
        return None
    secs_to_close = end_ts - now
    if secs_to_close <= 0 or secs_to_close > _PRE_CLOSE_WINDOW_S:
        return None

    symbol = _hedge_match_to_symbol(market.get("_slug_prefix", ""))
    if not symbol or symbol not in config.symbols:
        return None

    last = binance_ws.get_price(symbol)
    if last is None:
        return None
    cur_price, _ = last

    bucket_start_ts = end_ts - 300
    if (now - bucket_start_ts) < _MIN_BUCKET_AGE_S:
        return None

    history = spot_history.get(symbol) or []
    start_price = None
    for ts_ms, p in history:
        if abs((ts_ms // 1000) - bucket_start_ts) < 10:
            start_price = p
            break
    if start_price is None:
        return None

    move_pct = (cur_price / start_price - 1.0) * 100.0
    mid_up = _market_midpoint(market.get("outcomePrices"), 0)
    if mid_up is None:
        return None

    edge, side_label, _p_up = edge_vs_mid(
        spot_move_pct=move_pct,
        secs_left=float(secs_to_close),
        symbol=symbol,
        mid_up=mid_up,
        threshold=config.min_edge,
    )
    if side_label is None or edge < config.min_edge:
        return None

    outcome_index = 0 if side_label == "Up" else 1
    mid_for_side = mid_up if outcome_index == 0 else (1.0 - mid_up)

    return {
        "bucket_slug": market.get("slug") or "",
        "condition_id": market.get("conditionId") or "",
        "outcome": side_label,
        "outcome_index": outcome_index,
        "symbol": symbol,
        "spot_now": cur_price,
        "secs_to_close": float(secs_to_close),
        "spot_move_pct": move_pct,
        "mid_up": mid_up,
        "mid_for_side": mid_for_side,
        "end_ts": end_ts,
    }


# --- Loop entrypoint (live wiring) ---

async def crypto_arb_hedge_loop() -> None:
    """Loop principal del orchestrator delta-hedged.

    Diseño:
    - Mantenemos un ``BinanceTickerWS`` con history de spot prices para los
      símbolos hedgeables (mismo set que crypto_arb).
    - Cada ``check_interval_s``: listamos markets crypto-updown vía
      ``PolymarketClient`` y para cada uno en ventana de pre-close evaluamos
      edge contra el mid Polymarket; si edge ≥ HEDGE_MIN_EDGE → dispatch a
      ``CryptoArbHedge.evaluate_and_open`` (que abre Polymarket + Binance
      perp SHORT atómicamente).

    Recovery al startup:
    - ``drain_hedge_outbox()``: rows que no se persistieron antes del crash.
    - ``recover_orphan_perps()``: cierra cualquier SHORT que quedó on-chain
      sin tracking en DB.
    """
    config = HedgeConfig.from_env()
    if not config.enabled:
        log.info("crypto_arb_hedge: disabled (HEDGE_ENABLED!=true)")
        return

    log.warning(
        "crypto_arb_hedge: arrancando — bet=$%s leverage=%dx min_edge=%.3f "
        "max_funding=%.5f symbols=%s mode=%s",
        config.bet_usdc, config.leverage, config.min_edge,
        config.max_funding_rate, ",".join(config.symbols), TRADEBOOK_MODE,
    )

    # Crear tabla on-demand + migraciones. No queremos modificar src/db/schema.py.
    init_table()

    # Recovery: primero drenamos outbox (rows pendientes), después buscamos
    # orphan perps (positions abiertas sin tracking en DB). Ambos no-op si
    # no hay nada que recuperar.
    try:
        n_out = drain_hedge_outbox()
        if n_out:
            log.warning("crypto_arb_hedge: drenados %d rows del outbox", n_out)
    except Exception:
        log.exception("crypto_arb_hedge: drain_hedge_outbox failed (continuando)")

    if os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET"):
        try:
            n_rec = await recover_orphan_perps()
            if n_rec:
                log.warning(
                    "crypto_arb_hedge: recuperados %d orphan perps", n_rec,
                )
        except Exception:
            log.exception("crypto_arb_hedge: recover_orphan_perps failed (continuando)")

    # Setup hedge mode + leverage por símbolo (idempotente). Si la cuenta no
    # tiene credenciales (paper), saltamos esta fase silenciosamente.
    try:
        from src.binance.perp_account import (
            ensure_hedge_mode_enabled,
            set_leverage,
        )
        if os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET"):
            await ensure_hedge_mode_enabled()
            for sym in config.symbols:
                try:
                    await set_leverage(sym, config.leverage)
                except Exception:
                    log.warning("crypto_arb_hedge: set_leverage failed for %s", sym)
    except Exception:
        log.exception("crypto_arb_hedge: pre-setup failed (continuando)")

    orch = CryptoArbHedge(config=config)

    # Imports readonly del detector — reusamos infra de crypto_arb sin tocar
    # ese módulo (otros agents lo están modificando en paralelo).
    from src.binance.websocket import BinanceTickerWS
    from src.polymarket.client import PolymarketClient
    from src.copybot.crypto_arb import _list_active_updown_markets

    # History de spot por símbolo. Cap para evitar leak.
    spot_history: dict[str, list[tuple[int, float]]] = {s: [] for s in config.symbols}

    async def _on_tick(symbol: str, price: float, ts_ms: int) -> None:
        h = spot_history.setdefault(symbol, [])
        h.append((ts_ms, price))
        if len(h) > _SPOT_HISTORY_CAP:
            del h[: len(h) - _SPOT_HISTORY_CAP]

    binance_ws = BinanceTickerWS(symbols=config.symbols, on_tick=_on_tick)
    binance_task = asyncio.create_task(binance_ws.run(), name="hedge-binance-ws")

    # Warmup: necesitamos al menos _MIN_BUCKET_AGE_S de muestras para poder
    # leer el spot al inicio del bucket de 5min.
    log.info(
        "crypto_arb_hedge: warmup %ss para acumular spot history…",
        _MIN_BUCKET_AGE_S,
    )
    await asyncio.sleep(_MIN_BUCKET_AGE_S + 5)

    try:
        async with PolymarketClient() as client:
            while True:
                orch.metrics.cycles += 1
                try:
                    markets = await asyncio.wait_for(
                        _list_active_updown_markets(client), timeout=30,
                    )
                except asyncio.TimeoutError:
                    log.warning("crypto_arb_hedge: list markets timeout — sigo")
                    markets = []
                except Exception:
                    log.exception("crypto_arb_hedge: list markets error")
                    markets = []

                for m in markets:
                    setup = _hedge_evaluate_setup(m, config, spot_history, binance_ws)
                    if not setup:
                        continue
                    try:
                        record, err = await orch.evaluate_and_open(**setup)
                        if record:
                            log.info(
                                "crypto_arb_hedge.cycle_open slug=%s",
                                setup["bucket_slug"],
                            )
                        elif err:
                            log.debug(
                                "crypto_arb_hedge.cycle_skip slug=%s err=%s",
                                setup["bucket_slug"], err,
                            )
                    except Exception:
                        log.exception(
                            "crypto_arb_hedge.evaluate_and_open exception slug=%s",
                            setup.get("bucket_slug"),
                        )

                await asyncio.sleep(config.check_interval_s)
    finally:
        binance_ws.stop()
        binance_task.cancel()
        try:
            await binance_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("crypto_arb_hedge: loop terminado")


__all__ = [
    "CryptoArbHedge",
    "HedgeConfig",
    "init_table",
    "crypto_arb_hedge_loop",
    "recover_orphan_perps",
    "drain_hedge_outbox",
    "HEDGE_OUTBOX_PATH",
]
