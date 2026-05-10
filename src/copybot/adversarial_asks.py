"""Adversarial dust asks pre-close — explota retail/bots en último minuto.

ADVERSARIAL — explota retail/bots tontos comprando a market en el último
minuto. Es éticamente cuestionable pero financieramente válido en mercados
de prediction. Usar bajo responsabilidad del operador.

Estrategia (Nivel 3, 2026-05-10):
- Mercados ``{symbol}-updown-5m-{epoch}`` resuelven en 5 min.
- En los últimos 30-60s pre-close, si ``crypto_arb_signals.implied_up_probability``
  estima P(UP) >= 0.85 (o <=0.15), el lado perdedor ya está prácticamente
  decidido (el spot tiene poca varianza residual para flippear).
- Postear LIMIT ASK (SELL) del lado perdedor a precio basura (0.05).
- Retail/bots desinformados que entran a market BUY en el último minuto
  pueden pegar nuestra ask → recibimos $0.05 por share.
- Settle: el lado vale $0 → pero ya cobramos. PROFIT puro.

INVESTIGACIÓN: ¿se puede SELL sin shares previas en Polymarket?
================================================================
Polymarket CLOB requiere ERC1155 balance del token_id antes de aceptar
la orden SELL. NO se puede shortear directamente. Verificación:
    - py_clob_client_v2 acepta side=SELL en OrderArgs sin chequear balance
      cliente-side, pero el server-side valida la wallet (POLY_GNOSIS_SAFE
      con balance ERC1155 en CTF Exchange contract).
    - Sin balance → response: ``not enough balance / allowance``.

Workarounds viables:
- **Plan A (BUY-then-ASK)**: si vemos a OTRO operador postear dust ask
  en el lado perdedor, le compramos las shares barato y reposteamos
  nuestra ask un tick más alto. Funciona pero depende de oferta externa.
- **Plan B (mint complete set)**: split 1 USDC en 1 YES + 1 NO via
  ``CTFExchange.splitPosition`` (neg-risk markets) o ConditionalTokens.
  Después postear ASK del lado perdedor. Si fillea: recibimos $0.05 y
  retenemos el ganador (settle $1). Net si fillea = $0.05 ganancia
  marginal sobre el cost basis $1. Si no fillea: ganador settle $1,
  perdedor $0, net $0.
- **Plan C (existing inventory)**: si el bot ya tiene posiciones abiertas
  del lado perdedor (e.g., crypto_arb falló y quedó con shares del lado
  que va a perder), las usamos como inventario para postear la ask en
  lugar de cerrar a SL. "Salvataje" de posiciones perdedoras.

Implementación actual (esqueleto v1):
- Modo ``signal_only=true`` (default): NO postea órdenes. Solo detecta
  oportunidades, las loggea y las persiste en ``adversarial_orders``
  con ``status='detected'``. Permite analizar fill-rate teórico antes
  de comprometer capital.
- Modo ``signal_only=false``: requiere implementación adicional de
  ``_post_adversarial_ask`` que coordine con CTF Exchange para mint
  complete set + post limit. Stub queda con ``NotImplementedError``.

Activación: ``ADVERSARIAL_ENABLED=true``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from src.copybot.crypto_arb_signals import (
    get_sigma_pct_per_min,
    implied_up_probability,
)

log = logging.getLogger(__name__)


# --- Constantes ---

# Slugs crypto-updown-5m soportados (mismos que crypto_arb).
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

# Sides
SIDE_UP = "Up"
SIDE_DOWN = "Down"

# Status valores en tabla adversarial_orders
STATUS_DETECTED = "detected"      # señal observada (signal_only)
STATUS_OPEN = "open"              # ask posteada al CLOB
STATUS_FILLED = "filled"          # retail pegó nuestra ask
STATUS_CANCELLED = "cancelled"    # cancelada antes de fill
STATUS_SETTLED_WIN = "settled_win"
STATUS_SETTLED_LOSS = "settled_loss"
STATUS_EXPIRED_NOFILL = "expired_nofill"  # nadie pegó, settle $0


# --- Configuración ---

@dataclass
class AdversarialConfig:
    """Config leída de env. Defaults conservadores: signal_only y desactivado."""

    enabled: bool = False
    signal_only: bool = True            # default true: NO postea órdenes reales
    max_secs_to_close: float = 60.0     # solo último minuto
    min_secs_to_close: float = 10.0     # margen pa que la orden filtre antes de close
    min_loser_prob: float = 0.85        # P(side ganador) >= 0.85 → opuesto es loser
    ask_price: float = 0.05             # bait price del ask
    size_usdc: float = 2.0              # tamaño nominal del ask (USDC notional)
    check_interval_s: float = 5.0       # cada cuánto evaluar markets
    history_cap: int = 360              # samples max por símbolo (6 min @ 1/s)
    symbols: tuple[str, ...] = field(
        default_factory=lambda: tuple(SYMBOL_TO_SLUG_PREFIX.keys())
    )

    @classmethod
    def from_env(cls) -> "AdversarialConfig":
        return cls(
            enabled=_envbool("ADVERSARIAL_ENABLED", False),
            signal_only=_envbool("ADVERSARIAL_SIGNAL_ONLY", True),
            max_secs_to_close=_envfloat("ADVERSARIAL_MAX_SECS_TO_CLOSE", 60.0),
            min_secs_to_close=_envfloat("ADVERSARIAL_MIN_SECS_TO_CLOSE", 10.0),
            min_loser_prob=_envfloat("ADVERSARIAL_MIN_LOSER_PROB", 0.85),
            ask_price=_envfloat("ADVERSARIAL_ASK_PRICE", 0.05),
            size_usdc=_envfloat("ADVERSARIAL_SIZE_USDC", 2.0),
            check_interval_s=_envfloat("ADVERSARIAL_CHECK_INTERVAL_S", 5.0),
        )


def _envbool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "y", "on")


def _envfloat(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Schema lazy ---

def init_schema() -> None:
    """Crea la tabla ``adversarial_orders`` si no existe (idempotente).

    Usa el connector compartido ``src.db.schema.db()``. Soporta SQLite y
    Postgres — la sintaxis CREATE TABLE IF NOT EXISTS es estándar y los
    tipos usados (BIGSERIAL/PRIMARY KEY/TEXT/BIGINT/DOUBLE PRECISION)
    son compatibles con ambos backends a través del traductor SQL del
    schema module.

    Es lazy a propósito: el módulo no quiere requerir cambios en
    ``schema.py`` para no acoplarse al ciclo de vida de migraciones.
    Idempotente — re-llamar es seguro y barato (no caché global porque
    el path de DB puede cambiar runtime en tests con monkeypatch).
    """
    try:
        from src.db.schema import BACKEND, db
    except Exception as e:  # pragma: no cover — entornos sin DB
        log.debug("adversarial_asks.init_schema: db unavailable (%s)", e)
        return

    # Tipo de PK depende del backend: PG usa BIGSERIAL, SQLite usa
    # INTEGER PRIMARY KEY AUTOINCREMENT. El traductor SQL de schema.py
    # NO traduce BIGSERIAL — emitimos el DDL apropiado a cada backend.
    pk_decl = (
        "id BIGSERIAL PRIMARY KEY"
        if BACKEND == "postgres"
        else "id INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    ddl = f"""
    CREATE TABLE IF NOT EXISTS adversarial_orders (
      {pk_decl},
      symbol TEXT,
      bucket_slug TEXT NOT NULL,
      bucket_end_ts BIGINT NOT NULL,
      loser_side TEXT NOT NULL,
      p_up_at_signal DOUBLE PRECISION,
      ask_price DOUBLE PRECISION,
      size_usdc DOUBLE PRECISION,
      order_id TEXT,
      status TEXT DEFAULT 'detected',
      fill_price DOUBLE PRECISION,
      pnl_usdc DOUBLE PRECISION,
      signal_at BIGINT,
      filled_at BIGINT
    )
    """
    try:
        with db() as conn:
            conn.execute(ddl)
    except Exception as e:
        log.warning("adversarial_asks.init_schema failed: %s", e)


# --- Persistencia ---

def record_signal(
    *,
    symbol: Optional[str],
    bucket_slug: str,
    bucket_end_ts: int,
    loser_side: str,
    p_up: float,
    ask_price: float,
    size_usdc: float,
    status: str = STATUS_DETECTED,
    order_id: Optional[str] = None,
    signal_at: Optional[int] = None,
) -> Optional[int]:
    """Persiste señal/orden en ``adversarial_orders``. Devuelve id (o None)."""
    init_schema()
    try:
        from src.db.schema import tx
    except Exception:
        return None

    signal_at = signal_at if signal_at is not None else int(time.time())
    sql = """
    INSERT INTO adversarial_orders (
      symbol, bucket_slug, bucket_end_ts, loser_side, p_up_at_signal,
      ask_price, size_usdc, order_id, status, signal_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        symbol, bucket_slug, int(bucket_end_ts), loser_side,
        float(p_up), float(ask_price), float(size_usdc),
        order_id, status, signal_at,
    )
    try:
        with tx() as conn:
            cur = conn.execute(sql, params)
            try:
                return cur.lastrowid  # SQLite
            except Exception:
                return None
    except Exception as e:
        log.warning("adversarial_asks.record_signal failed: %s", e)
        return None


def _accumulated_adv_pnl() -> float:
    """Suma pnl_usdc de adversarial_orders cerradas."""
    try:
        from src.db.schema import db
        with db() as conn:
            cur = conn.execute(
                "SELECT COALESCE(SUM(pnl_usdc), 0) AS s FROM adversarial_orders "
                "WHERE pnl_usdc IS NOT NULL"
            )
            row = cur.fetchone()
            if row is None:
                return 0.0
            return float(row["s"] or 0.0)
    except Exception:
        log.exception("adversarial_asks._accumulated_adv_pnl failed")
        return 0.0


def update_settlement(
    *, order_id_db: int, status: str,
    fill_price: Optional[float] = None,
    pnl_usdc: Optional[float] = None,
    filled_at: Optional[int] = None,
    bucket_slug: Optional[str] = None,
) -> bool:
    """Actualiza una row tras fill/settle/cancel.

    Fix bug #4: cierres adversarial eran silentes — ahora si llega pnl_usdc
    y status indica close (filled/settled_*/expired_nofill), disparamos
    notif Telegram con acumulado strategy-specific.
    """
    init_schema()
    try:
        from src.db.schema import tx
    except Exception:
        return False
    try:
        with tx() as conn:
            conn.execute(
                """
                UPDATE adversarial_orders
                SET status=?, fill_price=?, pnl_usdc=?, filled_at=?
                WHERE id=?
                """,
                (status, fill_price, pnl_usdc, filled_at, int(order_id_db)),
            )
    except Exception as e:
        log.warning("adversarial_asks.update_settlement failed: %s", e)
        return False

    # Notif Telegram fix bug #4. Solo disparamos en estados que indican
    # cierre y con pnl_usdc concreto != 0.
    close_states = {
        STATUS_FILLED, STATUS_SETTLED_WIN, STATUS_SETTLED_LOSS,
        STATUS_EXPIRED_NOFILL,
    }
    if status in close_states and pnl_usdc is not None and abs(pnl_usdc) > 1e-9:
        try:
            from src.copybot.notifier import gain as notif_gain, loss as notif_loss
            accumulated = _accumulated_adv_pnl()
            pt = {
                "raw": {
                    "slug": (bucket_slug or "").lower() or "adversarial",
                    "title": f"adversarial ask {bucket_slug or ''}".strip(),
                },
            }
            if pnl_usdc > 0:
                notif_gain(pnl_usdc, accumulated, pt=pt, bucket_label="adversarial")
            else:
                notif_loss(abs(pnl_usdc), accumulated, pt=pt, bucket_label="adversarial")
        except Exception:
            log.exception(
                "adversarial_asks.update_settlement notif failed id=%s",
                order_id_db,
            )
    return True


# --- Lógica de detección de loser side ---

def detect_loser_side(
    *, p_up: float, min_loser_prob: float,
) -> Optional[str]:
    """Devuelve ``'Down'`` si UP es ganador casi seguro, ``'Up'`` si DOWN
    es ganador casi seguro, ``None`` si la prob está en zona incierta.

    Logic:
    - p_up >= min_loser_prob → UP gana, DOWN es el loser → vendemos DOWN
      (su token va a $0).
    - p_up <= 1 - min_loser_prob → DOWN gana, UP es el loser → vendemos UP.
    - sino → incertidumbre alta → skip.
    """
    if p_up >= min_loser_prob:
        return SIDE_DOWN
    if p_up <= 1.0 - min_loser_prob:
        return SIDE_UP
    return None


def parse_slug(slug: str) -> Optional[tuple[str, int]]:
    """Devuelve (slug_prefix, end_epoch) o None."""
    if not slug:
        return None
    m = SLUG_REGEX.match(slug)
    if not m:
        return None
    return f"{m.group(1)}-updown-5m-", int(m.group(2))


def _match_to_symbol(slug_prefix: str) -> Optional[str]:
    return SLUG_PREFIX_TO_SYMBOL.get(slug_prefix)


# --- Evaluador puro (testeable) ---

@dataclass
class AdversarialDecision:
    """Decisión de postear ask adversarial. ``post=False`` → skip.

    Campos:
    - ``post``: si True, postear ask en ``token_id`` del loser_side.
    - ``loser_side``: ``"Up"`` o ``"Down"`` (None si no aplica).
    - ``p_up``: probabilidad implied calculada (logging).
    - ``secs_to_close``: secs hasta el bucket end.
    - ``reason``: motivo del skip o el match.
    """
    post: bool
    loser_side: Optional[str]
    p_up: float
    secs_to_close: float
    reason: str
    bucket_slug: str = ""
    bucket_end_ts: int = 0
    symbol: Optional[str] = None


def evaluate_market(
    *,
    bucket_slug: str,
    bucket_end_ts: int,
    spot_now: Optional[float],
    spot_at_bucket_start: Optional[float],
    now_ts: int,
    config: AdversarialConfig,
) -> AdversarialDecision:
    """Evalúa un market y devuelve si debemos postear ask adversarial.

    Función pura (no I/O, no DB) — fácil de testear con datos sintéticos.

    Args:
        bucket_slug: e.g., ``"btc-updown-5m-1777505400"``.
        bucket_end_ts: epoch (s) del cierre del bucket.
        spot_now: precio Binance actual del símbolo, o None si no hay.
        spot_at_bucket_start: precio Binance al inicio del bucket
            (bucket_end_ts - 300), o None si no hay history.
        now_ts: timestamp actual en s.
        config: ``AdversarialConfig``.

    Returns:
        ``AdversarialDecision`` con ``post=True`` si toca postear.
    """
    parsed = parse_slug(bucket_slug)
    if not parsed:
        return AdversarialDecision(
            post=False, loser_side=None, p_up=0.5, secs_to_close=0.0,
            reason="invalid_slug", bucket_slug=bucket_slug,
            bucket_end_ts=bucket_end_ts,
        )
    slug_prefix, _epoch = parsed
    symbol = _match_to_symbol(slug_prefix)

    secs_to_close = float(bucket_end_ts - now_ts)

    # Ventana temporal: solo cerrar al final del bucket, con margen mínimo.
    if secs_to_close > config.max_secs_to_close:
        return AdversarialDecision(
            post=False, loser_side=None, p_up=0.5, secs_to_close=secs_to_close,
            reason="too_far_from_close", bucket_slug=bucket_slug,
            bucket_end_ts=bucket_end_ts, symbol=symbol,
        )
    if secs_to_close < config.min_secs_to_close:
        return AdversarialDecision(
            post=False, loser_side=None, p_up=0.5, secs_to_close=secs_to_close,
            reason="too_close_to_close", bucket_slug=bucket_slug,
            bucket_end_ts=bucket_end_ts, symbol=symbol,
        )

    if spot_now is None or spot_at_bucket_start is None or spot_at_bucket_start <= 0:
        return AdversarialDecision(
            post=False, loser_side=None, p_up=0.5, secs_to_close=secs_to_close,
            reason="no_spot", bucket_slug=bucket_slug,
            bucket_end_ts=bucket_end_ts, symbol=symbol,
        )

    move_pct = (spot_now / spot_at_bucket_start - 1.0) * 100.0
    sigma = get_sigma_pct_per_min(symbol or "BTCUSDT")
    p_up = implied_up_probability(
        spot_move_pct=move_pct,
        secs_left=max(secs_to_close, 0.0),
        sigma_pct_per_min=sigma,
    )

    loser = detect_loser_side(p_up=p_up, min_loser_prob=config.min_loser_prob)
    if loser is None:
        return AdversarialDecision(
            post=False, loser_side=None, p_up=p_up, secs_to_close=secs_to_close,
            reason=f"uncertain_p_up_{p_up:.3f}", bucket_slug=bucket_slug,
            bucket_end_ts=bucket_end_ts, symbol=symbol,
        )

    return AdversarialDecision(
        post=True, loser_side=loser, p_up=p_up, secs_to_close=secs_to_close,
        reason=(
            f"loser={loser} p_up={p_up:.3f} move={move_pct:+.3f}% "
            f"secs_left={secs_to_close:.0f}"
        ),
        bucket_slug=bucket_slug, bucket_end_ts=bucket_end_ts, symbol=symbol,
    )


# --- Posting (stubs / paper) ---

# Hook tipado para inyectar en tests / live. Devuelve order_id o None.
PostAskHook = Callable[
    [str, str, float, float],  # token_id, side, price, size_usdc
    Awaitable[Optional[str]],
]


async def _post_adversarial_ask_signal_only(
    token_id: str, side: str, price: float, size_usdc: float,
) -> Optional[str]:
    """No-op: solo loggea. Default cuando ``signal_only=True``."""
    log.info(
        "adversarial.signal_only token=%s side=%s price=%.4f size=%.2f USDC",
        token_id[:10] + "..." if len(token_id) > 10 else token_id,
        side, price, size_usdc,
    )
    return None


async def _live_executor(setup: dict) -> dict:
    """Live executor "Plan B" — split complete set + post limit SELL del loser.

    Setup dict::

        {
          "bucket_slug": str,           # e.g. "btc-updown-5m-1777505400"
          "loser_side": "Up" | "Down",  # lado a vender (token va a $0)
          "ask_price":  float,          # precio del SELL (e.g. 0.05)
          "size_usdc":  float,          # USDC a mintear via splitPosition
        }

    Flujo:
      1. Resolver ``conditionId`` desde ``slug`` via Gamma (closed=true porque
         el bucket está casi resolviendo — si está activo, también matchea).
      2. Resolver ``token_id`` del lado perdedor via ``token_resolver``.
      3. ``split_position(condition_id, size_usdc)`` — mint YES+NO 1:1.
      4. ``place_limit_order_gtc(token_id, side='SELL', price=ask_price, ...)``
         — orden resting hasta bucket close.

    Resultado:
      - Si retail/bot pega la ask → cobramos ``ask_price * shares`` y settle
        del lado perdedor vale $0 (ya no tenemos esas shares). Net positivo.
      - Si nadie pega → loser settle $0, winner settle $1 → recuperamos lo
        gastado (size_usdc) sin pérdida ni ganancia.

    Devuelve dict::

        {"ok": bool, "order_id": str | None, "split_tx_hash": str | None,
         "token_id": str | None, "error": str | None}
    """
    # Imports lazy: si LIVE no está activo no pagamos costo del SDK.
    try:
        from src.polymarket.clob_client import (
            place_limit_order_gtc,
            split_position,
        )
        from src.polymarket.client import GAMMA_API, PolymarketClient
    except Exception as e:
        return {"ok": False, "error": f"clob_import: {e}"}

    try:
        from src.polymarket.token_resolver import resolve_token_id
    except Exception as e:
        return {"ok": False, "error": f"token_resolver_import: {e}"}

    slug = setup.get("bucket_slug") or ""
    loser_side = setup.get("loser_side") or ""
    ask_price = float(setup.get("ask_price") or 0.0)
    size_usdc = float(setup.get("size_usdc") or 0.0)

    if not slug or loser_side not in (SIDE_UP, SIDE_DOWN):
        return {"ok": False, "error": "invalid_setup"}
    if ask_price <= 0 or size_usdc <= 0:
        return {"ok": False, "error": "invalid_amounts"}

    loser_outcome_index = 0 if loser_side == SIDE_UP else 1

    # 1+2: resolver conditionId + token_id en una sola sesión async.
    try:
        async with PolymarketClient() as c:
            try:
                d = await c._get(
                    f"{GAMMA_API}/markets",
                    params={"slug": slug, "closed": "true", "limit": 1},
                )
            except Exception as e:
                return {"ok": False, "error": f"gamma_lookup: {e}"}
            if not d:
                return {"ok": False, "error": "market_not_found"}
            m = d[0] if isinstance(d, list) else d
            if not isinstance(m, dict):
                return {"ok": False, "error": "market_not_dict"}
            condition_id = m.get("conditionId")
            token_id = await resolve_token_id(c, slug, loser_outcome_index)
    except Exception as e:
        return {"ok": False, "error": f"resolve_failed: {e}"}

    if not condition_id or not token_id:
        return {"ok": False, "error": "no_cid_or_token"}

    # 3. Split position: gasta size_usdc → mintea YES+NO 1:1.
    try:
        tx_hash = split_position(condition_id, size_usdc)
    except Exception as e:
        return {"ok": False, "error": f"split_exception: {e}"}
    if not tx_hash:
        return {"ok": False, "error": "split_failed"}

    # 4. Postear LIMIT SELL GTC del lado perdedor.
    try:
        result = place_limit_order_gtc(
            token_id=token_id,
            side="SELL",
            price=ask_price,
            size=size_usdc / ask_price,
            ttl_s=None,  # GTC — vive hasta bucket close o cancel manual
            condition_id=condition_id,
        )
    except Exception as e:
        return {
            "ok": False,
            "error": f"limit_post_exception: {e}",
            "split_tx_hash": tx_hash,
        }
    if not getattr(result, "ok", False):
        return {
            "ok": False,
            "error": f"limit_post_failed: {getattr(result, 'error', None)}",
            "split_tx_hash": tx_hash,
            "token_id": token_id,
        }

    return {
        "ok": True,
        "order_id": getattr(result, "order_id", None),
        "split_tx_hash": tx_hash,
        "token_id": token_id,
        "error": None,
    }


async def _post_adversarial_ask_live(
    token_id: str, side: str, price: float, size_usdc: float,
) -> Optional[str]:
    """Hook adapter — el flujo live REAL pasa por ``_live_executor`` con
    setup completo (incluye bucket_slug+loser_side para mint+resolve). Este
    adapter queda como fallback raise: indica al caller que use el path
    de ``_live_executor`` directo desde ``evaluate_and_post``.

    Mantenido por compat con la signature de ``PostAskHook`` (4 args). Si
    alguien instancia ``AdversarialAsks(post_ask_hook=_post_adversarial_ask_live)``
    explícito sin pasar por evaluate_and_post → falla loud.
    """
    raise NotImplementedError(
        "use AdversarialAsks.evaluate_and_post live path "
        "(LIVE_MODE=True + signal_only=False) — _live_executor handles "
        "split+limit. Direct hook signature insufficient (no condition_id/slug)."
    )


# --- Loop coordinator ---

class AdversarialAsks:
    """Orquestador del flujo adversarial.

    Mantiene history de spot prices, evalúa markets activos cerca del
    close, y postea (o loggea) asks adversarial.

    Diseñado para ser inyectable en tests:
        - ``markets_provider``: async fn () -> list[market_dict] con
          ``slug``, ``end_ts``, opcional ``clobTokenIds``.
        - ``post_ask_hook``: async fn(token_id, side, price, size_usdc) -> order_id
          (default: signal_only logger).
        - ``now_fn``: () -> int (default: time.time).

    En production, el caller plugea un ``markets_provider`` que llame
    a ``PolymarketClient.iter_markets`` y un ``post_ask_hook`` que llame
    al CLOB real.
    """

    def __init__(
        self,
        *,
        config: Optional[AdversarialConfig] = None,
        markets_provider: Optional[Callable[[], Awaitable[list[dict]]]] = None,
        post_ask_hook: Optional[PostAskHook] = None,
        now_fn: Optional[Callable[[], int]] = None,
        live_executor: Optional[Callable[[dict], Awaitable[dict]]] = None,
    ):
        self.config = config or AdversarialConfig.from_env()
        self.markets_provider = markets_provider
        self.post_ask_hook = post_ask_hook or (
            _post_adversarial_ask_signal_only
            if self.config.signal_only
            else _post_adversarial_ask_live
        )
        # Live executor inyectable: en prod es ``_live_executor`` (split +
        # limit SELL GTC); en tests pasamos un mock. Solo se invoca cuando
        # LIVE_MODE=True AND signal_only=False.
        self._live_executor_fn = live_executor or _live_executor
        self.now_fn = now_fn or (lambda: int(time.time()))

        # spot_history[symbol] = list[(ts_ms, price)]
        self.spot_history: dict[str, list[tuple[int, float]]] = {
            s: [] for s in self.config.symbols
        }

        # Dedup: no postear más de una ask por (slug, side) por bucket.
        self._posted: set[tuple[str, str]] = set()

    # ----- Spot tick ingestion -----

    async def on_binance_tick(
        self, symbol: str, price: float, ts_ms: int,
    ) -> None:
        """Acumula history. Se llama desde el callback del Binance WS."""
        h = self.spot_history.setdefault(symbol, [])
        h.append((ts_ms, price))
        if len(h) > self.config.history_cap:
            del h[: len(h) - self.config.history_cap]

    # ----- Spot lookup -----

    def spot_at(self, symbol: str, target_ts_s: int, tol_s: int = 10) -> Optional[float]:
        """Devuelve el precio spot más cercano a target_ts_s, o None."""
        h = self.spot_history.get(symbol) or []
        target_ms = target_ts_s * 1000
        # Búsqueda lineal — N≤360, no vale la pena binary search.
        best_diff = tol_s * 1000 + 1
        best_p: Optional[float] = None
        for ts_ms, p in h:
            d = abs(ts_ms - target_ms)
            if d < best_diff:
                best_diff = d
                best_p = p
        return best_p

    def spot_now(self, symbol: str) -> Optional[float]:
        """Último precio observado del símbolo."""
        h = self.spot_history.get(symbol) or []
        if not h:
            return None
        return h[-1][1]

    # ----- Eval & post -----

    async def evaluate_and_post(self, market: dict) -> Optional[AdversarialDecision]:
        """Evalúa un market y, si toca, postea la ask.

        Espera market dict con:
        - ``slug``: str
        - ``end_ts``: int (epoch)
        - ``clobTokenIds``: list[str] o JSON string (opcional para signal_only)
        """
        slug = market.get("slug") or ""
        end_ts = int(market.get("end_ts") or 0)
        if not slug or end_ts <= 0:
            return None

        parsed = parse_slug(slug)
        symbol = _match_to_symbol(parsed[0]) if parsed else None
        bucket_start_ts = end_ts - 300

        spot_start = self.spot_at(symbol or "", bucket_start_ts) if symbol else None
        spot_cur = self.spot_now(symbol or "") if symbol else None

        decision = evaluate_market(
            bucket_slug=slug,
            bucket_end_ts=end_ts,
            spot_now=spot_cur,
            spot_at_bucket_start=spot_start,
            now_ts=self.now_fn(),
            config=self.config,
        )

        if not decision.post or not decision.loser_side:
            log.debug("adversarial.skip slug=%s reason=%s", slug, decision.reason)
            return decision

        # Dedup: una ask por (slug, side).
        dedup_key = (slug, decision.loser_side)
        if dedup_key in self._posted:
            return decision

        # Resolver token_id del lado loser (best-effort, para signal_only).
        # En LIVE el ``_live_executor`` re-resuelve via token_resolver async.
        token_id = self._resolve_token_id(market, decision.loser_side)

        # Branch LIVE: si LIVE_MODE=True AND signal_only=False, usamos
        # ``_live_executor`` (split_position + limit SELL GTC). El executor
        # resuelve condition_id + token_id internamente — no requiere que
        # el market dict los traiga.
        live_path = False
        try:
            from src.config import LIVE_MODE as _LIVE_MODE
        except Exception:
            _LIVE_MODE = False
        if _LIVE_MODE and not self.config.signal_only:
            live_path = True

        order_id: Optional[str] = None
        if live_path:
            setup = {
                "bucket_slug": slug,
                "loser_side": decision.loser_side,
                "ask_price": self.config.ask_price,
                "size_usdc": self.config.size_usdc,
            }
            try:
                live_result = await self._live_executor_fn(setup)
            except Exception:
                log.exception("adversarial.live_executor crash slug=%s", slug)
                return decision
            if not live_result.get("ok"):
                log.warning(
                    "adversarial.live_executor_failed slug=%s err=%s",
                    slug, live_result.get("error"),
                )
                return decision
            order_id = live_result.get("order_id")
            token_id = live_result.get("token_id") or token_id
        else:
            if not token_id:
                log.warning(
                    "adversarial.no_token_id slug=%s side=%s",
                    slug, decision.loser_side,
                )
                return decision
            # Path no-live (signal_only o paper): hook tipado.
            try:
                order_id = await self.post_ask_hook(
                    token_id, "SELL",
                    self.config.ask_price, self.config.size_usdc,
                )
            except NotImplementedError:
                log.warning(
                    "adversarial.live_not_implemented slug=%s — set signal_only=true",
                    slug,
                )
                return decision
            except Exception:
                log.exception("adversarial.post_ask error slug=%s", slug)
                return decision

        self._posted.add(dedup_key)

        # Persistir.
        record_signal(
            symbol=symbol,
            bucket_slug=slug,
            bucket_end_ts=end_ts,
            loser_side=decision.loser_side,
            p_up=decision.p_up,
            ask_price=self.config.ask_price,
            size_usdc=self.config.size_usdc,
            status=STATUS_OPEN if order_id else STATUS_DETECTED,
            order_id=order_id,
            signal_at=self.now_fn(),
        )

        log.info(
            "adversarial.posted slug=%s side=%s p_up=%.3f price=%.4f size=$%.2f order_id=%s",
            slug, decision.loser_side, decision.p_up,
            self.config.ask_price, self.config.size_usdc,
            order_id or "(signal_only)",
        )
        return decision

    @staticmethod
    def _resolve_token_id(market: dict, side: str) -> Optional[str]:
        """Lee ``clobTokenIds[0]`` (Up) o ``[1]`` (Down) del market dict."""
        ct_raw = market.get("clobTokenIds")
        if isinstance(ct_raw, str):
            try:
                ct_raw = json.loads(ct_raw)
            except Exception:
                return None
        if not isinstance(ct_raw, list) or len(ct_raw) < 2:
            return None
        idx = 0 if side == SIDE_UP else 1
        try:
            return str(ct_raw[idx])
        except Exception:
            return None

    # ----- Cycle loop -----

    async def run_once(self) -> int:
        """Una iteración del loop: pulla markets activos y evalúa.

        Devuelve cantidad de decisiones con ``post=True``.
        """
        if not self.markets_provider:
            return 0
        try:
            markets = await self.markets_provider()
        except Exception:
            log.exception("adversarial.markets_provider error")
            return 0

        posted = 0
        for m in markets:
            d = await self.evaluate_and_post(m)
            if d and d.post:
                posted += 1
        return posted

    async def run_loop(self) -> None:
        """Loop indefinido. ``check_interval_s`` entre iteraciones."""
        if not self.config.enabled:
            log.info("adversarial: disabled (ADVERSARIAL_ENABLED!=true)")
            return
        log.warning(
            "adversarial: arrancando — signal_only=%s min_loser_prob=%.2f "
            "ask_price=%.3f size=$%.2f window=[%s..%s]s",
            self.config.signal_only, self.config.min_loser_prob,
            self.config.ask_price, self.config.size_usdc,
            self.config.min_secs_to_close, self.config.max_secs_to_close,
        )
        init_schema()
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("adversarial.run_once error")
            await asyncio.sleep(self.config.check_interval_s)


__all__ = [
    "AdversarialAsks",
    "AdversarialConfig",
    "AdversarialDecision",
    "_live_executor",
    "detect_loser_side",
    "evaluate_market",
    "init_schema",
    "parse_slug",
    "record_signal",
    "update_settlement",
    "SIDE_UP",
    "SIDE_DOWN",
    "STATUS_DETECTED",
    "STATUS_OPEN",
    "STATUS_FILLED",
    "STATUS_CANCELLED",
    "STATUS_SETTLED_WIN",
    "STATUS_SETTLED_LOSS",
    "STATUS_EXPIRED_NOFILL",
]
