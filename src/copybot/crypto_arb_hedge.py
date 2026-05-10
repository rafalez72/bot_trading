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
from src.copybot.crypto_arb_signals import edge_vs_mid, get_min_edge
from src.copybot.tradebook import MODE as TRADEBOOK_MODE, open_position

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
_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS hedge_trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_slug        TEXT NOT NULL,
    symbol             TEXT,
    side               TEXT,
    poly_trade_id      INTEGER,
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

_TABLE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_hedge_status ON hedge_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_hedge_bucket ON hedge_trades(bucket_slug)",
    "CREATE INDEX IF NOT EXISTS idx_hedge_opened_at ON hedge_trades(opened_at DESC)",
]


def init_table() -> None:
    """Crea la tabla ``hedge_trades`` + índices si no existen.

    Idempotente. Compatible SQLite + Postgres (el wrapper traduce
    INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL).
    """
    try:
        from src.db.schema import db
        with db() as conn:
            conn.execute(_TABLE_DDL)
            for ix in _TABLE_INDEXES:
                conn.execute(ix)
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
                        (bucket_slug, symbol, side, poly_trade_id,
                         perp_order_id, perp_qty, perp_entry_price,
                         spot_at_entry, edge_at_open, status,
                         opened_at, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bucket_slug,
                        decision["symbol"],
                        decision["outcome"],
                        poly_pid,
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
        except Exception:
            log.exception(
                "crypto_arb_hedge.persist_failed slug=%s — row no se grabó",
                bucket_slug,
            )
            record["id"] = None
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

        self.metrics.closes_ok += 1
        return True, perp_resp


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


# --- Loop entrypoint (live wiring) ---

async def crypto_arb_hedge_loop() -> None:
    """Loop principal del orchestrator.

    Diseño:
    - Reusa la detección de signals de ``crypto_arb`` (no la duplicamos).
    - Cada ciclo: pre-checks de margen + funding rate, luego para cada market
      activo en ventana de pre-close evalúa edge y dispara open atómico.
    - Settlement post bucket-end: cierre del perp + actualización
      hedge_trades.

    NOTA: este loop es un wireframe. La integración detallada con
    ``crypto_arb._list_active_updown_markets`` + ``_evaluate_market`` se hace
    en el deploy step. Acá dejamos el skeleton para que ``runner.py`` lo
    arranque vía ``asyncio.create_task`` cuando ``HEDGE_ENABLED=true``.
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

    # Crear tabla on-demand. No queremos modificar src/db/schema.py.
    init_table()

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

    # El detalle del loop (detección signals + dispatch a evaluate_and_open)
    # se conecta cuando HEDGE_ENABLED se ponga true en deploy. Por ahora,
    # mantenemos el orchestrator listo y el sleep esperando la integración.
    while config.enabled:
        orch.metrics.cycles += 1
        await asyncio.sleep(config.check_interval_s)


__all__ = [
    "CryptoArbHedge",
    "HedgeConfig",
    "init_table",
    "crypto_arb_hedge_loop",
]
