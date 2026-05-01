"""Regenera creds usando un Cloudflare Worker como proxy.

El Worker bypassea el bloqueo de Polymarket porque corre dentro de la red
de Cloudflare (intra-CF, no aplica el WAF de país/datacenter).

Uso:
    WORKER_URL=https://tu-worker.tu-cuenta.workers.dev \
    PK=0x... FUNDER=0x... \
    python scripts/regen_via_worker.py

La private key NUNCA sale de este script — el Worker solo recibe la
firma ya construida (POLY_SIGNATURE) y la dirección pública.
"""
from __future__ import annotations

import os
import sys
import json

import httpx

PK = os.environ.get("PK", "").strip()
FUNDER = os.environ.get("FUNDER", "").strip()
SIG = int(os.environ.get("SIG", "2"))
WORKER_URL = os.environ.get("WORKER_URL", "").strip()

if not PK.startswith("0x"):
    PK = "0x" + PK

if not WORKER_URL:
    print("ERROR: setea WORKER_URL=https://tu-worker.workers.dev")
    sys.exit(1)
if not PK or not FUNDER:
    print("ERROR: setea PK y FUNDER")
    sys.exit(1)

try:
    from py_clob_client.signer import Signer
    from py_clob_client.headers.headers import create_level_1_headers
except ImportError:
    print("ERROR: pip install py-clob-client")
    sys.exit(1)

# Build firma local (no llama a polymarket todavía)
signer = Signer(PK, chain_id=137)
headers = create_level_1_headers(signer, 0)
print(f"Address: {headers['POLY_ADDRESS']}")
print(f"Signing → POST {WORKER_URL}")

# El Worker forwardea a clob.polymarket.com/auth/api-key con esos headers
r = httpx.post(WORKER_URL, headers=headers, timeout=30.0)
print(f"\nWorker status: {r.status_code}")
print(f"Body: {r.text[:500]}")

if r.status_code != 200:
    print("\nFAIL — el worker no obtuvo creds. Revisa el código del Worker o el Worker URL.")
    sys.exit(1)

creds_raw = r.json()
api_key = creds_raw.get("apiKey") or creds_raw.get("api_key")
secret = creds_raw.get("secret") or creds_raw.get("api_secret")
passphrase = creds_raw.get("passphrase") or creds_raw.get("api_passphrase")

if not (api_key and secret and passphrase):
    print(f"\nWARN: shape inesperado: {creds_raw}")
    sys.exit(1)

print()
print("=== CREDS ===")
print(f"POLYMARKET_API_KEY={api_key}")
print(f"POLYMARKET_API_SECRET={secret}")
print(f"POLYMARKET_API_PASSPHRASE={passphrase}")
print()

# Probar balance — esto va directo al CLOB sin proxy
# (no falla por geo-block porque /balance-allowance NO está bloqueado)
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams, ApiCreds
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PK, chain_id=137, signature_type=SIG, funder=FUNDER,
        creds=ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase),
    )
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type="COLLATERAL"))
    print(f"BALANCE: {bal}")
except Exception as e:
    print(f"Balance check failed: {e}")
