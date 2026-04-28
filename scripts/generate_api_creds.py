"""Genera las credenciales L2 de Polymarket (API key + secret + passphrase).

CRÍTICO: este script pide tu PRIVATE KEY y la usa para firmar el mensaje
que genera las API creds. La private key NUNCA se guarda ni se transmite —
solo se usa una vez en memoria para llamar al CLOB.

Uso:
    python scripts/generate_api_creds.py

Requisitos:
    pip install py-clob-client

Pasos:
    1. Tener wallet en Polygon con USDC depositado en Polymarket
       (creada en polymarket.com con email/Magic — esa cuenta tiene una
        proxy wallet derivada con sig_type=2)
    2. Conseguir tu private key (en MetaMask: Settings → Security → Show
       Private Key. CUIDADO: cualquiera con esta clave puede mover tus fondos)
    3. Correr este script
    4. Copiar las 3 credenciales que imprime al .env del bot
    5. Borrar / olvidar la private key (las creds L2 alcanzan para tradear)
"""
from __future__ import annotations

import getpass
import sys


def main() -> None:
    print("\n=== Polymarket API Creds Generator ===\n")
    print("CRÍTICO: tu private key se usa solo para firmar la generación inicial.")
    print("Después podés rotarla — el bot solo necesita las creds L2 (api_key/secret/passphrase).\n")

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("ERROR: py-clob-client no instalado.")
        print("Corré: pip install py-clob-client")
        sys.exit(1)

    pk = getpass.getpass("Pegá tu private key (no se imprime): ").strip()
    if not pk:
        print("Cancelado: sin private key.")
        sys.exit(1)
    if not pk.startswith("0x"):
        pk = "0x" + pk

    funder = input(
        "Pegá la dirección de tu PROXY wallet de Polymarket\n"
        "(la que ves en polymarket.com → 'Deposit' → la dirección 0x...).\n"
        "Si no sabés, dejá vacío y usamos la EOA derivada de la private key.\n> "
    ).strip()

    sig_type_raw = input(
        "Sig type (default 2 = POLY_GNOSIS_SAFE; 0 = EOA standalone): "
    ).strip()
    sig_type = int(sig_type_raw) if sig_type_raw else 2

    host = "https://clob.polymarket.com"

    print("\nConectando al CLOB...")
    try:
        kwargs = dict(host=host, key=pk, chain_id=137, signature_type=sig_type)
        if funder:
            kwargs["funder"] = funder
        client = ClobClient(**kwargs)

        print("Generando o derivando API creds (firma offchain)...")
        creds = client.create_or_derive_api_creds()
    except Exception as e:
        print(f"\nERROR: {e}")
        print("\nPosibles causas:")
        print(" - Private key inválida")
        print(" - Sig type incorrecto (probá 0 si tu wallet es EOA)")
        print(" - Funder address no coincide con la derivada de tu private key")
        sys.exit(1)

    print("\n=== ✓ Credenciales generadas — pegalas al .env ===\n")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
    if funder:
        print(f"POLYMARKET_FUNDER_ADDRESS={funder}")
    print(f"POLYMARKET_SIG_TYPE={sig_type}")
    print()
    print("La private key NO la pongas en .env. El bot tradea solo con las creds L2.")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
