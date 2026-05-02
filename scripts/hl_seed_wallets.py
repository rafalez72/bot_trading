"""Seed inicial de wallets HL para empezar a copiar.

Insertá 10 wallets en hl_subscriptions con status='active'. Idempotente:
si ya existe, skipea. --reset borra todos antes de insertar.

Uso:
    python scripts/hl_seed_wallets.py
    python scripts/hl_seed_wallets.py --reset
"""
from __future__ import annotations

import sys
from src.db.schema import db, init_db, tx

# Wallets seed iniciales. Estos son ejemplos - el bot empieza a operar con
# ellas y la fase de discovery (futura) refina con leaderboard real.
SEED = [
    ("0x010461c14e146ac35fe42271bdc1134ee31c703a", "seed:perfil-1"),
    ("0x6b5e15bdaecc8a39e84a59f0c8c08f1d6f55d854", "seed:perfil-2"),
    ("0xa15a7ab6ad0fe8a07d50dd9f9e72d31e60b8fdab", "seed:perfil-3"),
    ("0x2eecf78ddf06a9bdd5e1b94f5f9da40ad3bd1cf5", "seed:perfil-4"),
    ("0x49e54ce95a1a55e1b65c46dbfedba0a2e7cf6a23", "seed:perfil-5"),
    ("0xf3f496c9486be5924a93d67e98298733bb47057c", "seed:perfil-6"),
    ("0x1c4d3df8ff48a89faa6bb96bf5e36c5337e9091e", "seed:perfil-7"),
    ("0x95d6c75de26795da4ce8bf02a2e5da6d39b4f04a", "seed:perfil-8"),
    ("0x7e72a44a0ee8ac0fa48bb96bf5d60b76e1cb4b2c", "seed:perfil-9"),
    ("0xe8a6f00fb3bf4c93b8a39d4f5f03c8a4b51f7a16", "seed:perfil-10"),
]


def main() -> None:
    init_db()
    if "--reset" in sys.argv:
        with tx() as c:
            c.execute("DELETE FROM hl_subscriptions")
        print("hl_subscriptions reset.")
    inserted = 0
    skipped = 0
    with tx() as c:
        for wallet, note in SEED:
            existing = c.execute(
                "SELECT 1 FROM hl_subscriptions WHERE wallet=?", (wallet,)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            c.execute(
                "INSERT INTO hl_subscriptions (wallet, status, sizing_mult, notes) "
                "VALUES (?, 'active', 1.0, ?)",
                (wallet, note),
            )
            inserted += 1
    print(f"Seed: +{inserted} insertados, ={skipped} skipeados.")
    with db() as c:
        rows = c.execute(
            "SELECT wallet, status, sizing_mult FROM hl_subscriptions ORDER BY wallet"
        ).fetchall()
        print(f"Estado final: {len(rows)} wallets")
        for r in rows:
            print(f"  {r['wallet'][:14]}.. status={r['status']} mult={r['sizing_mult']}")


if __name__ == "__main__":
    main()
