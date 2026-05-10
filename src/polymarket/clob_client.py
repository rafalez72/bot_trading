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
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from src.config import (
    CLOB_API,
    LIVE_MAX_SLIPPAGE_PCT,
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


def _build_and_post(client, *, token_id, side, shares, price, condition_id=None):
    """Helper: arma una orden FAK (IOC, permite partial fill) y la postea.

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
) -> OrderResult:
    """Manda una orden IOC al CLOB con pre-check de slippage y retry.

    Flujo:
      1. (dry_run) → loguea y devuelve fake ok
      2. Pre-check del orderbook → VWAP esperada vs target. Si slippage > umbral, abort.
      3. Primera orden FAK al precio target.
      4. Si no fillea (o fillea <50%), retry 1 vez con precio bumpeado
         (BUY: target * (1 + retry_bump), SELL: target * (1 - retry_bump)).

    Devuelve OrderResult con filled_size = shares ejecutadas reales.
    """
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
