"""Wrapper sobre py-clob-client para colocar órdenes reales en Polymarket.

Diseño:
- Singleton lazy: se inicializa la primera vez que se llama a `get_client()`
- Usa credenciales L2 (api_key + secret + passphrase) para placement
- La private key es opcional (solo necesaria para regenerar API creds)
- Todos los métodos atrapan excepciones y devuelven dict {ok, error, data}
  para que el caller no necesite try/except

NO importar este módulo a top-level desde otros archivos del bot:
- py-clob-client carga web3 + eth_account, costoso
- Importarlo solo cuando LIVE_MODE=true
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from src.config import (
    CLOB_API,
    LIVE_MAX_SLIPPAGE_PCT,
    LIVE_MIN_ORDERBOOK_DEPTH_USDC,
    LIVE_ORDER_TYPE,
    LIVE_RETRY_PRICE_BUMP_PCT,
    POLYMARKET_API_KEY,
    POLYMARKET_API_PASSPHRASE,
    POLYMARKET_API_SECRET,
    POLYMARKET_FUNDER_ADDRESS,
    POLYMARKET_PRIVATE_KEY,
    POLYMARKET_SIG_TYPE,
)

log = logging.getLogger(__name__)

CHAIN_ID_POLYGON = 137

_client = None  # singleton


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str] = None
    status: Optional[str] = None  # matched | live | unmatched | cancelled
    filled_size: Optional[float] = None  # shares ejecutadas
    avg_price: Optional[float] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = None


def _validate_creds() -> tuple[bool, str]:
    if not POLYMARKET_API_KEY:
        return False, "POLYMARKET_API_KEY no está en .env"
    if not POLYMARKET_API_SECRET:
        return False, "POLYMARKET_API_SECRET no está en .env"
    if not POLYMARKET_API_PASSPHRASE:
        return False, "POLYMARKET_API_PASSPHRASE no está en .env"
    if not POLYMARKET_FUNDER_ADDRESS:
        return False, "POLYMARKET_FUNDER_ADDRESS no está en .env"
    return True, ""


def get_client():
    """Inicializa (lazy) el py-clob-client con las credenciales del env.

    Devuelve None si las credenciales no están configuradas.
    """
    global _client
    if _client is not None:
        return _client

    ok, err = _validate_creds()
    if not ok:
        log.error("CLOB no configurable: %s", err)
        return None

    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
    except ImportError:
        log.error("py-clob-client-v2 no instalado. Corré: pip install py-clob-client-v2")
        return None

    creds = ApiCreds(
        api_key=POLYMARKET_API_KEY,
        api_secret=POLYMARKET_API_SECRET,
        api_passphrase=POLYMARKET_API_PASSPHRASE,
    )

    # signature_type 2 = POLY_GNOSIS_SAFE (proxy wallet creada por email/Magic).
    # Si tu cuenta es EOA standalone, cambia POLYMARKET_SIG_TYPE=0 en .env.
    kwargs = dict(
        host=CLOB_API,
        chain_id=CHAIN_ID_POLYGON,
        creds=creds,
        signature_type=POLYMARKET_SIG_TYPE,
        funder=POLYMARKET_FUNDER_ADDRESS,
    )
    if POLYMARKET_PRIVATE_KEY:
        kwargs["key"] = POLYMARKET_PRIVATE_KEY

    try:
        _client = ClobClient(**kwargs)
        log.info("CLOB client inicializado (funder=%s, sig_type=%d)",
                 POLYMARKET_FUNDER_ADDRESS[:10], POLYMARKET_SIG_TYPE)
        return _client
    except Exception as e:
        log.exception("error inicializando CLOB client: %s", e)
        return None


# Cache módulo: condition_id → (neg_risk: bool, tick_size: str, tokens: list)
_market_meta_cache: dict = {}


def _get_market_meta(condition_id: str) -> Optional[dict]:
    """Devuelve {neg_risk, tick_size, tokens} cacheado para un mercado.

    Necesario para firmar órdenes en CLOB: si neg_risk=True o tick≠0.01,
    el server rechaza con `order_version_mismatch` si no se pasan
    explícitamente en `PartialCreateOrderOptions`.
    """
    cached = _market_meta_cache.get(condition_id)
    if cached is not None:
        return cached
    client = get_client()
    if client is None:
        return None
    try:
        market = client.get_market(condition_id)
        if not market:
            return None
        meta = {
            "neg_risk": bool(market.get("neg_risk")),
            "tick_size": str(market.get("minimum_tick_size") or "0.01"),
            "tokens": market.get("tokens") or [],
        }
        _market_meta_cache[condition_id] = meta
        return meta
    except Exception as e:
        log.warning("_get_market_meta falló cid=%s: %s", condition_id[:10], e)
        return None


def get_token_id(condition_id: str, outcome_index: int) -> Optional[str]:
    """Devuelve el token_id (ERC1155) de un outcome de un mercado."""
    meta = _get_market_meta(condition_id)
    if not meta:
        return None
    tokens = meta["tokens"]
    if outcome_index is None or outcome_index >= len(tokens):
        return None
    return tokens[outcome_index].get("token_id")


def get_balance() -> Optional[float]:
    """Devuelve el balance de USDC disponible en la proxy wallet (en USDC)."""
    client = get_client()
    if client is None:
        return None
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        # bal es {balance: "1234567890" (en wei micro-USDC), allowance: "..."}
        balance_raw = bal.get("balance", "0")
        # USDC tiene 6 decimales en Polygon
        return float(balance_raw) / 1_000_000
    except Exception as e:
        log.warning("get_balance falló: %s", e)
        return None


def estimate_slippage(
    *, token_id: str, side: str, target_size_shares: float, target_price: float
) -> dict:
    """Pre-check del orderbook: calcula la VWAP esperada y el slippage.

    Devuelve {ok, vwap, slippage_pct, available_shares, error}.
    - ok=False si no hay liquidez suficiente o el slippage > LIVE_MAX_SLIPPAGE_PCT.

    Para BUY: walk de asks (precios bajos a altos) hasta acumular el size deseado.
    Para SELL: walk de bids (precios altos a bajos).
    """
    client = get_client()
    if client is None:
        return {"ok": False, "error": "CLOB no configurado"}

    try:
        ob = client.get_order_book(token_id)
        # v2 devuelve dict con keys "asks"/"bids" que son lists de dict {price, size}.
        # v1 devolvía objeto con .asks/.bids como lists de OrderSummary.
        # Normalizamos ambas formas.
        def _entry(o):
            if hasattr(o, "price"):
                return float(o.price), float(o.size)
            return float(o["price"]), float(o["size"])
        if isinstance(ob, dict):
            asks_raw = ob.get("asks") or []
            bids_raw = ob.get("bids") or []
        else:
            asks_raw = ob.asks or []
            bids_raw = ob.bids or []
        asks = sorted([_entry(o) for o in asks_raw])
        bids = sorted([_entry(o) for o in bids_raw], reverse=True)
    except Exception as e:
        # Silenciar el caso común "No orderbook" (mercado AMM-only o que cerró)
        # — es benigno, no merece alerta a Telegram
        msg = str(e)
        if "No orderbook exists" in msg or "404" in msg:
            return {"ok": False, "error": "no_orderbook"}
        return {"ok": False, "error": f"get_order_book: {e}"}

    book = asks if side.upper() == "BUY" else bids
    if not book:
        return {"ok": False, "error": "orderbook vacio"}

    # Min orderbook depth check (2026-05-10): suma USDC notional de los
    # primeros 5 niveles del book del side relevante. Si es < threshold,
    # market es demasiado thin para operar — slippage real será catastrófico
    # incluso aunque el VWAP teórico cumpla LIVE_MAX_SLIPPAGE_PCT.
    if LIVE_MIN_ORDERBOOK_DEPTH_USDC > 0:
        depth_usdc = sum(p * s for p, s in book[:5])
        if depth_usdc < LIVE_MIN_ORDERBOOK_DEPTH_USDC:
            return {
                "ok": False,
                "error": (
                    f"orderbook_too_thin (top5={depth_usdc:.0f} USDC < "
                    f"{LIVE_MIN_ORDERBOOK_DEPTH_USDC:.0f})"
                ),
                "depth_usdc_top5": depth_usdc,
            }

    accum_shares = 0.0
    accum_value = 0.0
    for price, size in book:
        take = min(target_size_shares - accum_shares, size)
        if take <= 0:
            break
        accum_value += take * price
        accum_shares += take
        if accum_shares >= target_size_shares - 1e-9:
            break

    if accum_shares < target_size_shares * 0.99:  # menos del 99% disponible
        return {
            "ok": False,
            "error": f"liquidez insuficiente ({accum_shares:.1f}/{target_size_shares:.1f} shares)",
            "available_shares": accum_shares,
        }

    vwap = accum_value / accum_shares
    # Slippage: para BUY positivo si pagamos MÁS que target. Para SELL positivo si recibimos MENOS.
    if side.upper() == "BUY":
        slippage = (vwap - target_price) / target_price
    else:
        slippage = (target_price - vwap) / target_price

    ok = slippage <= LIVE_MAX_SLIPPAGE_PCT
    return {
        "ok": ok,
        "vwap": vwap,
        "slippage_pct": slippage,
        "available_shares": accum_shares,
        "error": None if ok else f"slippage {slippage*100:.1f}% > {LIVE_MAX_SLIPPAGE_PCT*100:.1f}%",
    }


# Polymarket CLOB exige los siguientes límites de precisión decimal en el
# payload de la orden. Si los excedés, el server responde HTTP 400:
#   `invalid amounts, the market buy orders maker amount supports a max
#    accuracy of 2 decimals, taker amount a max of 4 decimals`
# Como maker = USDC (BUY: shares*price, SELL: shares directos en algunos paths),
# y taker = shares (BUY) o USDC (SELL), nuestra estrategia conservadora es
# garantizar que TANTO `shares` como `shares*price` queden dentro de los
# límites estrictos: shares con max 4 decimales, producto con max 2 decimales.
MAKER_MAX_DECIMALS = 2  # USDC notional → 2 decimales (centavos)
TAKER_MAX_DECIMALS = 4  # shares → 4 decimales

_DEC_2 = Decimal("0.01")
_DEC_4 = Decimal("0.0001")


def _quantize_amounts(shares: float, price: float) -> tuple[float, float]:
    """Cuantiza (shares, price) para cumplir los límites de Polymarket.

    Garantiza:
      - shares: max 4 decimales (taker_max)
      - shares * price: max 2 decimales (maker_max)

    Estrategia adaptada al tick del precio:
      * tick=0.01 (price 2 dec): shares enteros → maker = N*0.PP (2 dec ✓).
      * tick=0.001 (price 3 dec): shares múltiplos de 10 → maker = 10K*0.PPP =
        K*P.PP (2 dec ✓). En general shares*10^k debe ser entero donde k es
        el exceso de decimales del price sobre 2.
      * tick=0.0001 (4 dec): múltiplos de 100.
      * Para precios "raros" (no en tick estándar), iteramos buscando el
        mayor M' = N centavos t.q. M'/price tenga max 4 decimales y el
        producto exacto sea M'.

    NUNCA overshoot: el producto resultante <= shares*price original
    (ROUND_DOWN). Devuelve (shares_quant, price); price no se modifica.
    """
    if shares <= 0 or price <= 0:
        return (0.0, price)

    s_dec = Decimal(str(shares))
    p_dec = Decimal(str(price))

    # ¿Cuántos decimales tiene el price? (Decimal exponent: -2 = 2 decimales)
    p_exp = -p_dec.as_tuple().exponent if p_dec.as_tuple().exponent < 0 else 0

    # Caso 1: tick estándar (price con 2, 3 o 4 decimales). Quantizar shares
    # para que el producto sea entero múltiplo de 0.01.
    # shares * 10^(p_exp-2) debe ser entero, i.e. shares múltiplo de 10^(p_exp-2)
    # cuando p_exp > 2. Para p_exp <= 2, shares enteros bastan.
    if p_exp <= 4:
        if p_exp <= 2:
            step_int = 1
        else:
            step_int = 10 ** (p_exp - 2)
        # Truncar shares al múltiplo de step_int más cercano hacia abajo.
        s_int = int(s_dec)
        shares_quant = (s_int // step_int) * step_int
        if shares_quant <= 0:
            return (0.0, price)
        # Sanity-check: el producto debe encajar en 2 decimales. Si por
        # algún corner case (p ej. price con 5+ decimales) no encaja,
        # caemos al loop iterativo abajo.
        prod = Decimal(shares_quant) * p_dec
        if prod.quantize(_DEC_2, rounding=ROUND_DOWN) == prod:
            return (float(shares_quant), price)

    # Caso 2: fallback iterativo. Arranca con M = floor(shares*price*100)/100
    # y baja en pasos de 0.01 buscando un M tal que (M/price) sea exactamente
    # 4-decimal y M / price * price = M.
    maker_cents = int((s_dec * p_dec * 100).to_integral_value(rounding=ROUND_DOWN))
    for _ in range(500):
        if maker_cents <= 0:
            return (0.0, price)
        M = Decimal(maker_cents) / Decimal(100)
        s_candidate = (M / p_dec).quantize(_DEC_4, rounding=ROUND_DOWN)
        if s_candidate > 0 and (s_candidate * p_dec) == M:
            return (float(s_candidate), price)
        maker_cents -= 1

    # Fallback defensivo: si nada cuadra en 500 pasos (price extremadamente
    # raro), devolvemos 0 — el caller rechazará el trade en vez de mandar
    # algo que el server va a rebotar con 400.
    return (0.0, price)


_OUTBOX_PATH = "/app/data/orders_outbox.jsonl"


def _outbox_log(event: str, payload: dict) -> None:
    """Audit trail persistente de cada intento/respuesta de orden, en JSONL.

    Crítico para reconciliación: si la DB falla al INSERT live_trades, el
    outbox queda como única evidencia local de que el bot mandó la orden.
    """
    import json as _json, time as _t, os as _os
    try:
        _os.makedirs(_os.path.dirname(_OUTBOX_PATH), exist_ok=True)
        with open(_OUTBOX_PATH, "a") as f:
            f.write(_json.dumps({"ts": int(_t.time()), "event": event, **payload}) + "\n")
    except Exception:
        pass  # never let outbox break the order flow


def _resp_indicates_fill(resp: dict) -> bool:
    """Detecta si una response de post_order indica que la orden filleó al menos
    parcialmente. Útil para distinguir errores reales de errores donde la orden
    SÍ se ejecutó pero el SDK retornó algo raro.
    """
    if not isinstance(resp, dict):
        return False
    if resp.get("transactionsHashes") or resp.get("transactionHash"):
        return True
    making = resp.get("makingAmount") or 0
    taking = resp.get("takingAmount") or 0
    try:
        if float(making) > 0 or float(taking) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if resp.get("status") in ("matched", "filled"):
        return True
    return False


def compute_limit_price(target_price: float, side: str, max_slippage_pct: float) -> float:
    """Calcula el limit price para una orden LIMIT_FOK.

    BUY:  limit = target * (1 + max_slippage_pct) → cap superior, jamás
          pagamos más que esto.
    SELL: limit = target * (1 - max_slippage_pct) → cap inferior, jamás
          recibimos menos que esto.

    Defensa anti-MEV/adversarial en thin orderbooks: si un sniper bot
    sweepea liquidez antes de nuestro fill, el server cancela en vez de
    ejecutar a precio peor.
    """
    if target_price <= 0:
        return target_price
    pct = max(0.0, float(max_slippage_pct))
    if side.upper() == "BUY":
        return target_price * (1.0 + pct)
    return target_price * (1.0 - pct)


def _build_and_post(
    client, *, token_id, side, shares, price, condition_id=None,
    order_type_kind: str = "FAK",
):
    """Helper: arma una orden y la postea.

    `order_type_kind` controla el tipo enviado al server:
      - "FAK" (default legacy): IOC con partial fill permitido. El bot maneja
        retry si fill < 50%.
      - "FOK" (limit fill-or-kill): la orden filla 100% al limit price o se
        cancela. NO hay retry — caller decide si reintentar con bump.

    Devuelve (success, response_dict).

    NUEVO post-2026-05-05 (trades fantasma):
    - Audit log persistente en outbox antes y después de cada intento
    - Si el SDK tira excepción pero la response sugiere fill (txHash, makingAmount>0,
      status=matched), tratamos como éxito y devolvemos la response.

    Si se pasa `condition_id`, fetcha neg_risk + tick_size del mercado.
    """
    from py_clob_client_v2.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client_v2.order_builder.constants import BUY, SELL

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=shares,
        side=BUY if side.upper() == "BUY" else SELL,
    )

    options = None
    if condition_id:
        meta = _get_market_meta(condition_id)
        if meta:
            options = PartialCreateOrderOptions(
                neg_risk=meta["neg_risk"],
                tick_size=meta["tick_size"],
            )

    _outbox_log("attempt", {
        "token_id": token_id, "condition_id": condition_id,
        "side": side, "shares": shares, "price": price,
        "order_type": order_type_kind,
    })

    try:
        if options is not None:
            signed = client.create_order(order_args, options)
        else:
            signed = client.create_order(order_args)
    except Exception as e:
        _outbox_log("create_failed", {"token_id": token_id, "error": str(e)[:300]})
        return False, {"errorMsg": f"create_order: {e}"}

    try:
        if order_type_kind.upper() == "FOK":
            order_type = OrderType.FOK
        else:
            order_type = getattr(OrderType, "FAK", None) or OrderType.FOK
        resp = client.post_order(signed, order_type)
        _outbox_log("post_ok", {"token_id": token_id, "resp": resp or {}})
        return True, (resp or {})
    except Exception as e:
        # CRITICAL: aún si el SDK tiró exception, la orden puede haberse ejecutado
        # parcialmente. Intentamos extraer la response del exception (algunos
        # PolyApiException incluyen body con tx_hash o makingAmount > 0).
        err_str = str(e)
        partial_resp = {}
        try:
            # PolyApiException tiene .response_data o similar; fallback a parsing
            for attr in ("response_data", "args"):
                v = getattr(e, attr, None)
                if isinstance(v, dict):
                    partial_resp = v
                    break
                if isinstance(v, (list, tuple)) and v and isinstance(v[0], dict):
                    partial_resp = v[0]
                    break
        except Exception:
            pass

        if _resp_indicates_fill(partial_resp):
            _outbox_log("post_succeeded_despite_error", {
                "token_id": token_id, "error": err_str[:300], "resp": partial_resp,
            })
            return True, partial_resp

        _outbox_log("post_failed", {"token_id": token_id, "error": err_str[:300]})
        return False, {"errorMsg": f"post_order: {e}"}


def place_market_order(
    *,
    token_id: str,
    side: str,  # "BUY" | "SELL"
    size_usdc: float,
    price: float,  # precio de referencia
    dry_run: bool = False,
    skip_slippage_check: bool = False,
    condition_id: Optional[str] = None,  # para neg_risk/tick_size correctos
    order_type: Optional[str] = None,  # "MARKET" | "LIMIT_FOK"
) -> OrderResult:
    """Manda una orden al CLOB con pre-check de slippage.

    `order_type`:
      - None / "LIMIT_FOK" (default): limit fill-or-kill a price = mid
        ± max_slippage_pct. Si no hay liquidez completa al limit, el
        server cancela. Defensa MEV/adversarial — jamás pagamos peor
        que el cap. NO hay retry.
      - "MARKET": legacy IOC (FAK) + retry con bump. Acepta partial fill
        y permite slippage variable hasta el cap. Usar solo en books con
        depth >> bet_size.

    Default decidido por env var ``LIVE_ORDER_TYPE`` si caller pasa None.

    Flujo (MARKET):
      1. (dry_run) → loguea y devuelve fake ok basado en VWAP del book real.
      2. Pre-check del orderbook → VWAP esperada vs target. Si slippage > umbral, abort.
      3. Primera orden FAK al precio target.
      4. Si no fillea (o fillea <50%), retry 1 vez con precio bumpeado.

    Flujo (LIMIT_FOK):
      1. (dry_run) → mismo dry-run realista que MARKET.
      2. Pre-check del orderbook (depth + slippage VWAP teórico) — si falla, abort.
      3. limit_price = compute_limit_price(price, side, max_slippage_pct).
      4. Una sola orden FOK al limit. Si fillea → ok. Si no → cancelled.

    Devuelve OrderResult con filled_size = shares ejecutadas reales.
    """
    # Resolver tipo de orden default desde env si no fue forzado por caller.
    effective_order_type = (order_type or LIVE_ORDER_TYPE or "LIMIT_FOK").strip().upper()
    if effective_order_type not in ("LIMIT_FOK", "MARKET"):
        effective_order_type = "LIMIT_FOK"
    if dry_run:
        # NUEVO 2026-05-10: dry-run REALISTA. Antes devolvía midpoint
        # idealizado, lo que generó 86% win rate fake en simulación
        # mientras live operaba a 0%. Ahora consultamos el orderbook real
        # (mismo que usa el live) y simulamos un fill VWAP-based.
        # Así el dry-run se vuelve predictivo de la performance live.
        if price <= 0:
            return OrderResult(ok=False, error=f"precio invalido: {price}")
        raw_shares = size_usdc / price
        if not skip_slippage_check:
            slip = estimate_slippage(
                token_id=token_id, side=side,
                target_size_shares=raw_shares, target_price=price,
            )
            if not slip.get("ok"):
                # Mismo path de rechazo que live: si no hay liquidez o
                # slippage > umbral, dry-run también debe rechazar.
                log.info(
                    "[DRY-RUN] %s abortado por pre-check (orderbook real): %s",
                    side, slip.get("error"),
                )
                return OrderResult(
                    ok=False,
                    error=f"pre-check: {slip.get('error')}",
                    raw=slip,
                )
            vwap = float(slip.get("vwap") or price)
            sim_avg = vwap
        else:
            sim_avg = price
        # Recalc shares para el size objetivo dado el avg_price simulado
        shares = (size_usdc / sim_avg) if sim_avg > 0 else 0
        log.info(
            "[DRY-RUN] %s token=%s.. target_px=%.3f sim_avg=%.3f size=%.2f USDC "
            "(~%.2f shares)",
            side, token_id[:12], price, sim_avg, size_usdc, shares,
        )
        return OrderResult(
            ok=True,
            order_id=f"DRY-{token_id[:12]}-{side}",
            status="matched",
            filled_size=shares,
            avg_price=sim_avg,
        )

    client = get_client()
    if client is None:
        return OrderResult(ok=False, error="CLOB no configurado")

    if price <= 0:
        return OrderResult(ok=False, error=f"precio invalido: {price}")

    # Polymarket CLOB rechaza con HTTP 400 si los amounts del payload superan
    # sus límites de precisión: maker (USDC) max 2 decimales, taker (shares)
    # max 4 decimales. Como la SDK firma con (shares, price) y computa
    # maker = shares*price internamente, hay que cuantizar `shares` para que
    # el producto siempre quede a 2 decimales exactos (sin pasarse del size
    # pedido por el caller).
    tick_size = "0.01"
    if condition_id:
        meta = _get_market_meta(condition_id)
        if meta:
            tick_size = meta["tick_size"]

    raw_shares = size_usdc / price
    shares, price = _quantize_amounts(raw_shares, price)
    if shares <= 0:
        return OrderResult(ok=False, error=f"size_usdc demasiado chico (tick={tick_size}, raw_shares={raw_shares:.4f})")

    # --- Pre-check del orderbook ---
    if not skip_slippage_check:
        slip = estimate_slippage(
            token_id=token_id, side=side, target_size_shares=shares, target_price=price,
        )
        if not slip["ok"]:
            log.info("orden abortada por pre-check: %s", slip.get("error"))
            return OrderResult(ok=False, error=f"pre-check: {slip.get('error')}", raw=slip)

    # --- Path LIMIT_FOK (default desde 2026-05-10) ---
    # Una sola orden FOK al limit_price = mid ± max_slippage_pct.
    # Si el server no puede fillear el 100% al limit, cancela. NO retry.
    if effective_order_type == "LIMIT_FOK":
        limit_price = compute_limit_price(price, side, LIVE_MAX_SLIPPAGE_PCT)
        # Redondeamos al tick del market — un price fuera del tick_size hace
        # que el server rechace con "invalid price".
        tick_dec = 2 if tick_size in ("0.01",) else 3 if tick_size in ("0.001",) else 4
        limit_price = round(limit_price, tick_dec)
        # Re-cuantizamos shares con el limit_price (el producto shares*limit
        # debe tener max 2 decimales).
        fok_shares, limit_price = _quantize_amounts(size_usdc / limit_price, limit_price)
        if fok_shares <= 0:
            return OrderResult(
                ok=False,
                error=f"FOK size_usdc demasiado chico al limit (tick={tick_size}, limit={limit_price})",
            )
        log.info(
            "LIMIT_FOK %s token=%s.. mid=%.4f limit=%.4f shares=%.4f size=%.2f USDC",
            side, token_id[:12], price, limit_price, fok_shares, size_usdc,
        )
        success_fok, resp_fok = _build_and_post(
            client, token_id=token_id, side=side, shares=fok_shares,
            price=limit_price, condition_id=condition_id, order_type_kind="FOK",
        )
        if not success_fok:
            return OrderResult(
                ok=False,
                error=resp_fok.get("errorMsg", "FOK post error"),
                raw=resp_fok,
            )
        status_fok = resp_fok.get("status", "unknown")
        order_id_fok = resp_fok.get("orderID") or resp_fok.get("orderId")
        # FOK: server cancela atomicamente si no llena 100%. Detectamos
        # fill vs no-fill via _resp_indicates_fill (mira tx_hash, status,
        # making/taking amounts > 0). Si es un cancel sin fill →
        # status='cancelled_fok' y ok=False sin retry.
        making_fok = float(resp_fok.get("makingAmount", 0) or 0) or 0
        taking_fok = float(resp_fok.get("takingAmount", 0) or 0) or 0
        if not _resp_indicates_fill(resp_fok) or status_fok in ("unmatched", "cancelled"):
            log.info(
                "LIMIT_FOK no fillea (status=%s making=%s) → cancelado",
                status_fok, making_fok,
            )
            return OrderResult(
                ok=False,
                order_id=order_id_fok,
                status="cancelled_fok",
                filled_size=0.0,
                error=f"FOK no fillea al limit {limit_price:.4f}",
                raw=resp_fok,
            )
        # Fill OK. takingAmount = shares recibidas (BUY) o USDC (SELL);
        # makingAmount = lo dado. Para reportar filled_size en shares,
        # usamos taking en BUY y making en SELL.
        filled_shares = taking_fok if side.upper() == "BUY" else making_fok
        if filled_shares <= 0:
            filled_shares = fok_shares  # fallback al size pedido (FOK fillea full)
        return OrderResult(
            ok=True,
            order_id=order_id_fok,
            status=status_fok,
            filled_size=filled_shares,
            avg_price=limit_price,
            tx_hash=resp_fok.get("transactionHash"),
            raw=resp_fok,
        )

    # --- Path MARKET (legacy IOC con retry) ---
    # --- Intento 1: precio target ---
    success, resp = _build_and_post(
        client, token_id=token_id, side=side, shares=shares, price=price,
        condition_id=condition_id,
    )
    if not success:
        return OrderResult(ok=False, error=resp.get("errorMsg", "post error"), raw=resp)

    status = resp.get("status", "unknown")
    order_id = resp.get("orderID") or resp.get("orderId")
    filled = float(resp.get("makingAmount", 0) or 0) or 0
    fill_pct = filled / shares if shares > 0 else 0

    # Si fillea bien (>=50%) → ok
    if fill_pct >= 0.5 and status not in ("unmatched",):
        return OrderResult(
            ok=True, order_id=order_id, status=status,
            filled_size=filled or shares, avg_price=price,
            tx_hash=resp.get("transactionHash"), raw=resp,
        )

    # --- Intento 2: precio bumpeado ---
    # BUY: pagamos un poco más para asegurar fill. SELL: aceptamos un poco menos.
    # IMPORTANTE: redondeamos retry_price al tick del market (2 o 3 decimales)
    # — pasarle un precio fuera del tick_size hace que el server rechace la
    # orden con "invalid price" (mismo flujo de errores que decimales).
    bump = 1 + LIVE_RETRY_PRICE_BUMP_PCT if side.upper() == "BUY" else 1 - LIVE_RETRY_PRICE_BUMP_PCT
    tick_dec = 2 if tick_size in ("0.01",) else 3 if tick_size in ("0.001",) else 4
    retry_price = round(price * bump, tick_dec)
    # `filled` viene en USDC (makingAmount del response del SDK), no en shares.
    # Convertimos a shares restantes: shares_remaining = shares_total - filled/price
    # y volvemos a cuantizar para mantener los límites decimales en la retry.
    filled_shares = filled / price if price > 0 else 0
    remaining_raw = max(0.0, shares - filled_shares)
    remaining, retry_price = _quantize_amounts(remaining_raw, retry_price)
    if remaining <= 0:
        return OrderResult(
            ok=True, order_id=order_id, status=status,
            filled_size=filled, avg_price=price, raw=resp,
        )

    log.info(
        "retry %s con precio %.3f (vs %.3f, fill previo %.1f%%, remaining %.4f shares)",
        side, retry_price, price, fill_pct * 100, remaining,
    )
    success2, resp2 = _build_and_post(
        client, token_id=token_id, side=side, shares=remaining, price=retry_price,
        condition_id=condition_id,
    )
    if not success2:
        return OrderResult(
            ok=False, order_id=order_id, status="retry_failed",
            error=resp2.get("errorMsg", "post error"), raw={"first": resp, "retry": resp2},
        )

    filled2 = float(resp2.get("makingAmount", 0) or 0) or 0
    total_filled = filled + filled2
    if total_filled <= 0:
        return OrderResult(
            ok=False, order_id=order_id, status="unmatched_after_retry",
            error="ni la primera ni la retry matchearon", raw={"first": resp, "retry": resp2},
        )

    # Avg price ponderado entre los dos fills
    weighted_price = (price * filled + retry_price * filled2) / total_filled if total_filled else price
    return OrderResult(
        ok=True,
        order_id=resp2.get("orderID") or resp2.get("orderId") or order_id,
        status="matched_after_retry",
        filled_size=total_filled,
        avg_price=weighted_price,
        tx_hash=resp2.get("transactionHash") or resp.get("transactionHash"),
        raw={"first": resp, "retry": resp2},
    )


# =============================================================================
# Extensiones para market_maker (A) + spike_arb (B) + adversarial (C)
# =============================================================================
#
# Estas funciones agregan a `clob_client.py` la primitiva mínima que necesitan
# los bots N1 (mm), N2 (spike_arb) y N3 (adversarial_asks) para operar en live:
#
#   - place_limit_order_gtc:  GTC = Good-Till-Cancelled. Posteás y NO esperás
#     fill — devuelve order_id. Distinto al FOK (cancela si no fillea inmediato)
#     y al FAK (IOC partial fill).
#   - cancel_order / cancel_all_orders: control imperativo del orderbook propio.
#   - get_open_orders / get_fills_since: reconciliation post-restart o post-fill.
#   - split_position / redeem_position: ConditionalTokensFramework on-chain ops.
#     Permite gastar USDC para obtener YES+NO shares 1:1 (split) o redimir
#     shares ganadoras a USDC (redeem) tras settlement.
#
# Diseño:
#   - Mismo patrón lazy import: nunca pagar el costo del SDK si no se usa.
#   - Errors capturados → devolvemos None / False / [] para no romper callers.
#   - Audit log en outbox para split/redeem (son tx on-chain costosas).
#
# Referencias:
#   - https://docs.polymarket.com/developers/CLOB/orders/cancel-orders
#   - https://docs.polymarket.com/developers/CLOB/orders/get-orders
#   - https://docs.polymarket.com/developers/CLOB/data-api (fills/trades)
#   - eshan-bhimani/polymarket-hft-bot: patterns FOK + limit
#   - Polygon ConditionalTokens: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
#   - Polygon NegRiskAdapter:    0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296

# Polygon mainnet — direcciones canónicas Polymarket.
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CONDITIONAL_TOKENS_POLYGON = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER_POLYGON = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ABI mínimo para split/merge/redeem en ConditionalTokensFramework.
# Polymarket también expone el `NegRiskAdapter` con la MISMA firma
# (`splitPosition` / `redeemPositions`) para neg_risk markets — es un
# wrapper. El ABI sirve para ambos.
_CT_ABI_FRAGMENTS = [
    {
        "name": "splitPosition",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "partition", "type": "uint256[]"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    },
]


def place_limit_order_gtc(
    *,
    token_id: str,
    side: str,
    price: float,
    size: float,
    ttl_s: Optional[int] = None,
    condition_id: Optional[str] = None,
) -> OrderResult:
    """Postea una orden LIMIT GTC (Good-Till-Cancelled) y devuelve sin esperar fill.

    GTC = la orden vive en el book hasta que sea matcheada o cancelada
    explícitamente (vs FOK que cancela atomicamente si no fillea, o FAK que
    IOC partial fill). En Polymarket CLOB → ``OrderType.GTC`` con
    ``expiration=0`` (sin expiry) o ``expiration=epoch_seconds`` si ttl_s.

    Uso primario:
      - market_maker (N1): postear bids/asks pasivos en torno al mid.
      - spike_arb (N2): limit a mid con TTL=60s.
      - adversarial_asks (N3): ask en spike upward.

    Devuelve OrderResult con order_id si el server aceptó la orden. Caller
    debe usar ``cancel_order(order_id)`` o ``get_open_orders()`` para tracking.

    NOTA: a diferencia de ``place_market_order``, NO hay pre-check de slippage
    porque GTC no es taker — el caller controla el price, asume que sabe.
    Sí cuantizamos amounts para los límites de Polymarket (maker 2 dec,
    taker 4 dec).
    """
    if price <= 0:
        return OrderResult(ok=False, error=f"precio invalido: {price}")
    if size <= 0:
        return OrderResult(ok=False, error=f"size invalido: {size}")

    client = get_client()
    if client is None:
        return OrderResult(ok=False, error="CLOB no configurado")

    # Cuantizar shares para que el producto (USDC notional) quepa en 2 decimales.
    shares_q, price_q = _quantize_amounts(size, price)
    if shares_q <= 0:
        return OrderResult(
            ok=False,
            error=f"size {size} demasiado chico al price {price} (post-quantize=0)",
        )

    try:
        from py_clob_client_v2.clob_types import (
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
        )
        from py_clob_client_v2.order_builder.constants import BUY, SELL
    except ImportError as e:
        return OrderResult(ok=False, error=f"sdk import: {e}")

    expiration = 0
    if ttl_s and ttl_s > 0:
        import time as _t
        expiration = int(_t.time()) + int(ttl_s)

    # OrderArgs en algunas versiones del SDK no acepta `expiration` como
    # kwarg. Intentamos con, y si falla por TypeError, fallback sin (TTL=0).
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price_q,
            size=shares_q,
            side=BUY if side.upper() == "BUY" else SELL,
            expiration=expiration,
        )
    except TypeError:
        order_args = OrderArgs(
            token_id=token_id,
            price=price_q,
            size=shares_q,
            side=BUY if side.upper() == "BUY" else SELL,
        )

    options = None
    if condition_id:
        meta = _get_market_meta(condition_id)
        if meta:
            options = PartialCreateOrderOptions(
                neg_risk=meta["neg_risk"],
                tick_size=meta["tick_size"],
            )

    _outbox_log("attempt_gtc", {
        "token_id": token_id, "condition_id": condition_id,
        "side": side, "shares": shares_q, "price": price_q,
        "ttl_s": ttl_s, "expiration": expiration,
    })

    try:
        if options is not None:
            signed = client.create_order(order_args, options)
        else:
            signed = client.create_order(order_args)
    except Exception as e:
        _outbox_log("gtc_create_failed", {"token_id": token_id, "error": str(e)[:300]})
        return OrderResult(ok=False, error=f"create_order: {e}")

    try:
        resp = client.post_order(signed, OrderType.GTC) or {}
    except Exception as e:
        _outbox_log("gtc_post_failed", {"token_id": token_id, "error": str(e)[:300]})
        return OrderResult(ok=False, error=f"post_order: {e}")

    order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
    status = resp.get("status", "live")
    _outbox_log("gtc_post_ok", {"token_id": token_id, "order_id": order_id, "status": status})

    return OrderResult(
        ok=True,
        order_id=order_id,
        status=status,
        filled_size=float(resp.get("makingAmount", 0) or 0) or 0.0,
        avg_price=price_q,
        raw=resp,
    )


def cancel_order(order_id: str) -> bool:
    """Cancela UNA orden por id. Devuelve True si el server confirma cancel.

    Polymarket SDK v2: ``client.cancel(order_id)`` devuelve un dict con
    ``canceled: [<id>]`` y ``not_canceled: {<id>: reason}``. Si el id está
    en ``canceled`` → True. Si está en ``not_canceled`` (ej. ya fillada o
    inexistente) → False. Si la SDK tira excepción → False (defensivo).
    """
    if not order_id:
        return False
    client = get_client()
    if client is None:
        return False
    try:
        resp = client.cancel(order_id) or {}
    except Exception as e:
        log.warning("cancel_order falló id=%s: %s", str(order_id)[:20], e)
        _outbox_log("cancel_failed", {"order_id": order_id, "error": str(e)[:200]})
        return False
    canceled = resp.get("canceled") or []
    not_canceled = resp.get("not_canceled") or {}
    ok = order_id in canceled and order_id not in not_canceled
    _outbox_log("cancel", {"order_id": order_id, "ok": ok, "resp": resp})
    return ok


def cancel_all_orders(token_id: Optional[str] = None) -> int:
    """Cancela TODAS las orders abiertas (o filtradas por token_id).

    Útil al shutdown del bot — evita que queden bids/asks zombies en el book
    si el proceso muere por OOM o restart no-limpio.

    SDK v2:
      - ``client.cancel_all()`` cancela TODO.
      - ``client.cancel_market_orders(market=token_id)`` cancela solo de un
        token específico.

    Devuelve el número de orders canceladas exitosamente (puede ser 0 si no
    había nada). En error, devuelve 0 — el caller no se entera, pero el
    outbox queda con la traza.
    """
    client = get_client()
    if client is None:
        return 0
    try:
        if token_id:
            resp = client.cancel_market_orders(market=token_id) or {}
        else:
            resp = client.cancel_all() or {}
    except Exception as e:
        log.warning("cancel_all_orders falló (token=%s): %s", token_id or "ALL", e)
        _outbox_log("cancel_all_failed", {"token_id": token_id, "error": str(e)[:200]})
        return 0
    canceled = resp.get("canceled") or []
    n = len(canceled)
    _outbox_log("cancel_all", {"token_id": token_id, "n_canceled": n, "resp": resp})
    return n


def get_open_orders(token_id: Optional[str] = None) -> list[dict]:
    """Lista todas las orders abiertas del usuario, opcionalmente filtradas.

    SDK v2: ``client.get_orders(OpenOrderParams(asset_id=token_id))``.

    Devuelve list de dicts (raw del server). Cada item tiene típicamente:
      { "id": str, "asset_id": str, "side": "BUY"|"SELL",
        "price": str, "size": str, "size_matched": str,
        "owner": str, "status": "LIVE"|"PARTIAL"|... }

    En error o sin orders → lista vacía (caller puede hacer `if not orders:`).
    """
    client = get_client()
    if client is None:
        return []
    try:
        from py_clob_client_v2.clob_types import OpenOrderParams
        params = OpenOrderParams(asset_id=token_id) if token_id else OpenOrderParams()
        resp = client.get_orders(params)
    except ImportError:
        # Fallback: SDK más nuevo expone get_orders sin params helper.
        try:
            resp = client.get_orders()
        except Exception as e:
            log.warning("get_open_orders fallback falló: %s", e)
            return []
    except Exception as e:
        log.warning("get_open_orders falló (token=%s): %s", token_id or "ALL", e)
        return []
    if not resp:
        return []
    if isinstance(resp, dict):
        # Algunas versiones devuelven {"orders": [...]}.
        resp = resp.get("orders") or resp.get("data") or []
    if not isinstance(resp, list):
        return []
    if token_id:
        # Doble check filter por si la SDK no filtra server-side.
        resp = [
            o for o in resp
            if str(o.get("asset_id") or o.get("token_id") or "") == token_id
        ]
    return resp


def get_fills_since(timestamp_s: int) -> list[dict]:
    """Devuelve fills (trades ejecutados) del usuario desde ``timestamp_s`` (epoch).

    Polymarket data-api endpoint:
      GET ``{DATA_API}/data-api/trades?user={funder}&takerOnly=false``

    Estrategia:
      1. Si la SDK expone ``get_trades(TradeParams(...))`` lo usamos.
      2. Sino, httpx directo al data-api con el funder address.
      3. Filtramos client-side por ``timestamp >= timestamp_s``.

    Devuelve list de dicts: cada fill tiene típicamente
      { "id": str, "asset_id": str, "side": "BUY"|"SELL",
        "price": float, "size": float, "timestamp": int (epoch s),
        "transaction_hash": str, "maker_orders": [...], ... }

    Útil para market_maker reconciliation: tras restart, reconstruir
    inventory desde la última timestamp persistida.
    """
    if timestamp_s < 0:
        timestamp_s = 0
    client = get_client()
    if client is None:
        return []

    # Path 1: SDK helper.
    fills: list[dict] = []
    try:
        from py_clob_client_v2.clob_types import TradeParams
        params = TradeParams(after=timestamp_s)
        resp = client.get_trades(params)
        if isinstance(resp, list):
            fills = resp
        elif isinstance(resp, dict):
            fills = resp.get("trades") or resp.get("data") or []
    except ImportError:
        fills = []
    except Exception as e:
        log.warning("get_fills_since via SDK falló: %s", e)
        fills = []

    # Path 2: data-api directo si la SDK no devolvió nada (o tiró).
    if not fills and POLYMARKET_FUNDER_ADDRESS:
        try:
            import httpx
            from src.config import DATA_API
            url = f"{DATA_API}/trades"
            r = httpx.get(
                url,
                params={"user": POLYMARKET_FUNDER_ADDRESS, "takerOnly": "false"},
                timeout=10.0,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    fills = data
                elif isinstance(data, dict):
                    fills = data.get("trades") or data.get("data") or []
        except Exception as e:
            log.warning("get_fills_since via data-api falló: %s", e)

    # Filter client-side por timestamp (defensivo: la SDK/API podría no respetar after).
    out = []
    for f in fills:
        ts = f.get("timestamp") or f.get("ts") or f.get("match_time") or 0
        try:
            ts = int(float(ts))
        except (TypeError, ValueError):
            ts = 0
        # Algunos endpoints devuelven ms en vez de seg.
        if ts > 10**12:
            ts //= 1000
        if ts >= timestamp_s:
            out.append(f)
    return out


def _w3_client() -> Optional[tuple]:
    """Helper compartido por split/redeem: instancia web3 + account.

    Devuelve (w3, account, ct_address_default) o None si no hay private key
    o web3 no está disponible.

    El ``ct_address_default`` es el ConditionalTokens framework canónico;
    el caller puede usar NegRiskAdapter para markets con neg_risk=True.
    """
    if not POLYMARKET_PRIVATE_KEY:
        log.error("split/redeem requieren POLYMARKET_PRIVATE_KEY (no en .env)")
        return None
    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError as e:
        log.error("web3 no instalado: %s", e)
        return None

    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        account = Account.from_key(POLYMARKET_PRIVATE_KEY)
    except Exception as e:
        log.error("web3 init falló: %s", e)
        return None

    return (w3, account, CONDITIONAL_TOKENS_POLYGON)


def split_position(
    condition_id: str,
    size_usdc: float,
    *,
    neg_risk: Optional[bool] = None,
) -> Optional[str]:
    """ERC1155 ``splitPosition``: gasta USDC y obtiene YES + NO shares 1:1.

    Flujo on-chain:
      1. Approve USDC al ConditionalTokens (o NegRiskAdapter) si necesario.
      2. Call ``splitPosition(USDC, parent=0x0, conditionId, partition=[1,2], amount)``.
      3. Resultado: tu wallet recibe ``size_usdc`` shares YES + ``size_usdc`` NO.

    ¿Por qué? Útil para market_maker / adversarial: si ves un mid YES=0.55,
    NO=0.40 (suma <1.00 = arb), splitteás $X → te llevás $X YES + $X NO,
    y vendés YES@0.55 + NO@0.40 → ingresás $0.95X. Edge directo.

    Devuelve el tx_hash si la tx fue submitted, None en error. NO espera
    confirmation — caller debe poll si lo necesita.

    Nota: ``size_usdc`` se convierte a wei (USDC tiene 6 decimales en
    Polygon). conditionId es bytes32 (0x prefijo, 32 bytes).
    """
    if size_usdc <= 0:
        return None
    if not condition_id or not condition_id.startswith("0x") or len(condition_id) != 66:
        log.error("split_position: condition_id inválido (%s)", condition_id)
        return None

    setup = _w3_client()
    if setup is None:
        return None
    w3, account, _default_addr = setup

    # neg_risk → usar NegRiskAdapter; sino ConditionalTokens.
    if neg_risk is None:
        meta = _get_market_meta(condition_id)
        neg_risk = bool(meta["neg_risk"]) if meta else False
    target = NEG_RISK_ADAPTER_POLYGON if neg_risk else CONDITIONAL_TOKENS_POLYGON

    try:
        contract = w3.eth.contract(address=w3.to_checksum_address(target),
                                   abi=_CT_ABI_FRAGMENTS)
    except Exception as e:
        log.error("split_position: contract init falló: %s", e)
        return None

    amount_wei = int(round(size_usdc * 10**6))
    parent_collection = b"\x00" * 32
    partition = [1, 2]  # YES, NO

    try:
        tx = contract.functions.splitPosition(
            w3.to_checksum_address(USDC_POLYGON),
            parent_collection,
            condition_id,
            partition,
            amount_wei,
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 300_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(
            getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        )
        tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        _outbox_log("split_position", {
            "condition_id": condition_id, "size_usdc": size_usdc,
            "neg_risk": neg_risk, "tx_hash": tx_hex,
        })
        return tx_hex
    except Exception as e:
        log.exception("split_position falló cid=%s: %s", condition_id[:10], e)
        _outbox_log("split_failed", {"condition_id": condition_id, "error": str(e)[:300]})
        return None


def redeem_position(
    condition_id: str,
    outcome_index: int,
    *,
    neg_risk: Optional[bool] = None,
) -> Optional[str]:
    """ERC1155 ``redeemPositions``: redime shares ganadoras a USDC tras settlement.

    Counterpart de ``split_position``. Una vez UMA resuelve el market y
    el ConditionalTokens conoce el payout, el holder de shares ganadoras
    puede llamar ``redeemPositions(USDC, parent=0x0, conditionId, indexSets)``
    para canjear sus tokens por USDC.

    ``outcome_index``: 0 = YES, 1 = NO. Se traduce a indexSet:
      - YES → 0b01 = 1
      - NO  → 0b10 = 2

    Devuelve tx_hash o None en error. NO espera confirmation. Si el market
    NO resolvió aún, la tx revierte (web3 puede tirar exception en
    ``send_raw_transaction`` o aceptar y revertir on-chain — depende del
    nodo).
    """
    if outcome_index not in (0, 1):
        log.error("redeem_position: outcome_index inválido (%d), debe ser 0 (YES) o 1 (NO)",
                  outcome_index)
        return None
    if not condition_id or not condition_id.startswith("0x") or len(condition_id) != 66:
        log.error("redeem_position: condition_id inválido (%s)", condition_id)
        return None

    setup = _w3_client()
    if setup is None:
        return None
    w3, account, _default_addr = setup

    if neg_risk is None:
        meta = _get_market_meta(condition_id)
        neg_risk = bool(meta["neg_risk"]) if meta else False
    target = NEG_RISK_ADAPTER_POLYGON if neg_risk else CONDITIONAL_TOKENS_POLYGON

    try:
        contract = w3.eth.contract(address=w3.to_checksum_address(target),
                                   abi=_CT_ABI_FRAGMENTS)
    except Exception as e:
        log.error("redeem_position: contract init falló: %s", e)
        return None

    # YES = indexSet 1 (bit 0), NO = indexSet 2 (bit 1).
    index_set = 1 if outcome_index == 0 else 2
    parent_collection = b"\x00" * 32

    try:
        tx = contract.functions.redeemPositions(
            w3.to_checksum_address(USDC_POLYGON),
            parent_collection,
            condition_id,
            [index_set],
        ).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "gas": 250_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(
            getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
        )
        tx_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
        _outbox_log("redeem_position", {
            "condition_id": condition_id, "outcome_index": outcome_index,
            "neg_risk": neg_risk, "tx_hash": tx_hex,
        })
        return tx_hex
    except Exception as e:
        log.exception("redeem_position falló cid=%s: %s", condition_id[:10], e)
        _outbox_log("redeem_failed", {
            "condition_id": condition_id, "outcome_index": outcome_index,
            "error": str(e)[:300],
        })
        return None


def health_check() -> dict:
    """Verifica que la conexión y credenciales funcionen.

    Útil para CLI: `python copybot.py check-live`.
    """
    ok, err = _validate_creds()
    if not ok:
        return {"ok": False, "stage": "creds", "error": err}

    client = get_client()
    if client is None:
        return {"ok": False, "stage": "client_init", "error": "no se pudo crear el client"}

    try:
        balance = get_balance()
        if balance is None:
            return {"ok": False, "stage": "balance", "error": "no se pudo leer el balance"}
        return {
            "ok": True,
            "funder": POLYMARKET_FUNDER_ADDRESS,
            "balance_usdc": balance,
            "sig_type": POLYMARKET_SIG_TYPE,
            "host": CLOB_API,
        }
    except Exception as e:
        return {"ok": False, "stage": "health", "error": str(e)}
