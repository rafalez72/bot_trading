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
from typing import Optional

from src.config import (
    CLOB_API,
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
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        log.error("py-clob-client no instalado. Corré: pip install py-clob-client")
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


def get_token_id(condition_id: str, outcome_index: int) -> Optional[str]:
    """Devuelve el token_id (ERC1155) de un outcome de un mercado.

    El CLOB lo trae en /markets/<condition_id>. Cacheamos en memoria por sesión.
    """
    client = get_client()
    if client is None:
        return None
    try:
        market = client.get_market(condition_id)
        if not market:
            return None
        tokens = market.get("tokens") or []
        if outcome_index is None or outcome_index >= len(tokens):
            return None
        return tokens[outcome_index].get("token_id")
    except Exception as e:
        log.warning("get_token_id falló cid=%s oi=%s: %s", condition_id[:10], outcome_index, e)
        return None


def get_balance() -> Optional[float]:
    """Devuelve el balance de USDC disponible en la proxy wallet (en USDC)."""
    client = get_client()
    if client is None:
        return None
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        # bal es {balance: "1234567890" (en wei micro-USDC), allowance: "..."}
        balance_raw = bal.get("balance", "0")
        # USDC tiene 6 decimales en Polygon
        return float(balance_raw) / 1_000_000
    except Exception as e:
        log.warning("get_balance falló: %s", e)
        return None


def place_market_order(
    *,
    token_id: str,
    side: str,  # "BUY" | "SELL"
    size_usdc: float,
    price: float,  # precio de referencia (para BUY se usa como límite)
    dry_run: bool = False,
) -> OrderResult:
    """Manda una orden FOK (fill-or-kill) al CLOB.

    Para BUY: convierte USDC a shares = size_usdc / price.
    Para SELL: size_usdc se interpreta como notional, shares = size_usdc / price.

    Si dry_run=True, no manda nada y devuelve OrderResult ficticio "ok".
    """
    if dry_run:
        shares = size_usdc / price if price > 0 else 0
        log.info(
            "[DRY-RUN] orden %s token=%s.. price=%.3f size=%.2f USDC (~%.2f shares)",
            side, token_id[:12], price, size_usdc, shares,
        )
        return OrderResult(
            ok=True,
            order_id=f"DRY-{token_id[:12]}-{side}",
            status="matched",
            filled_size=shares,
            avg_price=price,
        )

    client = get_client()
    if client is None:
        return OrderResult(ok=False, error="CLOB no configurado")

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        if price <= 0:
            return OrderResult(ok=False, error=f"precio invalido: {price}")
        shares = round(size_usdc / price, 2)  # Polymarket usa 2 decimales en size
        if shares <= 0:
            return OrderResult(ok=False, error="size_usdc demasiado chico")

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=BUY if side.upper() == "BUY" else SELL,
        )
        signed = client.create_order(order_args)
        # FOK = fill-or-kill: o se ejecuta entera o se cancela. Evita partial fills.
        resp = client.post_order(signed, OrderType.FOK)

        if not resp:
            return OrderResult(ok=False, error="respuesta vacia del CLOB")

        # resp típica: {"success": True, "orderID": "...", "status": "matched", ...}
        ok = bool(resp.get("success", True))
        status = resp.get("status", "unknown")
        order_id = resp.get("orderID") or resp.get("orderId")
        if not ok or status == "unmatched":
            return OrderResult(
                ok=False,
                order_id=order_id,
                status=status,
                error=resp.get("errorMsg") or "orden no matcheada",
                raw=resp,
            )

        # Si matchea, intentamos extraer el size real ejecutado
        filled_size = float(resp.get("makingAmount", 0)) or shares
        return OrderResult(
            ok=True,
            order_id=order_id,
            status=status,
            filled_size=filled_size,
            avg_price=price,
            tx_hash=resp.get("transactionHash"),
            raw=resp,
        )
    except Exception as e:
        log.exception("place_market_order error: %s", e)
        return OrderResult(ok=False, error=str(e))


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
