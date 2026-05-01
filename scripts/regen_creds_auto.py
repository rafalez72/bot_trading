"""Regenera API creds L2 sin input interactivo. Para correr en Codespaces.

Lee PK y FUNDER de variables de entorno PK y FUNDER (defaults hardcoded
para ahorrar copy-paste — esta clave ya está expuesta en el chat).

Uso:
    pip install -q py-clob-client
    python scripts/regen_creds_auto.py
"""
import os, sys

PK_DEFAULT = "0x0535ec660b1dcd9f59f330ecf5d7d7c99be9de8f7314405165425f1bddcc3d48"
FUNDER_DEFAULT = "0x110345DeA0Ae8A7584D5e43244c1DCd6Ee170E2d"
SIG_DEFAULT = 2

PK = os.environ.get("PK", PK_DEFAULT).strip()
FUNDER = os.environ.get("FUNDER", FUNDER_DEFAULT).strip()
SIG = int(os.environ.get("SIG", str(SIG_DEFAULT)))
if not PK.startswith("0x"):
    PK = "0x" + PK

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import BalanceAllowanceParams
except ImportError:
    print("ERROR: py-clob-client no instalado. Corré: pip install py-clob-client")
    sys.exit(1)

print(f"funder={FUNDER}  sig_type={SIG}")
client = ClobClient(
    host="https://clob.polymarket.com",
    key=PK, chain_id=137, signature_type=SIG, funder=FUNDER,
)
print("Llamando create_or_derive_api_creds...")
creds = client.create_or_derive_api_creds()
print()
print("=== CREDS ===")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
print()
client.set_api_creds(creds)
try:
    bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type="COLLATERAL"))
    print(f"BALANCE: {bal}")
except Exception as e:
    print(f"Balance check failed: {e}")
