"""Seed inicial de wallets dYdX v4. Discovery on-the-fly via top wallets
del market BTC-USD (mayor volumen). Inserta en dx_subscriptions."""
import asyncio, sys
from src.db.schema import init_db, db, tx
from src.dydx.client import DydxClient

async def main() -> None:
    init_db()
    if "--reset" in sys.argv:
        with tx() as c:
            c.execute("DELETE FROM dx_subscriptions")
        print("dx_subscriptions reset.")

    print("Discovery: top wallets active en BTC-USD trades últimos 100...")
    async with DydxClient() as client:
        # Buscar wallets en trades recientes de los markets más activos
        candidates = set()
        for ticker in ["BTC-USD", "ETH-USD", "SOL-USD"]:
            try:
                trades = await client.trades(ticker, limit=100)
                for t in trades:
                    # subaccount.address es la dirección
                    sub_id = t.get("subaccountId") or {}
                    addr = sub_id.get("owner") if isinstance(sub_id, dict) else None
                    if not addr:
                        # Maker/taker addresses (alternative structure)
                        addr = t.get("makerAddress") or t.get("takerAddress")
                    if addr and addr.startswith("dydx1"):
                        candidates.add(addr.lower())
                print(f"  {ticker}: {len(trades)} trades scanned")
            except Exception as e:
                print(f"  {ticker} error: {str(e)[:80]}")

        candidates = list(candidates)[:20]
        if not candidates:
            print("  ⚠️ No se encontraron candidatos vía trades. Hardcodeando placeholders.")
            # Placeholders dydx Cosmos addresses (se reemplazan via discovery real luego)
            candidates = [f"dydx1placeholder{i:02d}" for i in range(10)]

    inserted, skipped = 0, 0
    with tx() as c:
        for addr in candidates[:10]:
            existing = c.execute("SELECT 1 FROM dx_subscriptions WHERE wallet=?", (addr,)).fetchone()
            if existing:
                skipped += 1
                continue
            c.execute(
                "INSERT INTO dx_subscriptions (wallet, status, sizing_mult, notes) "
                "VALUES (?, 'active', 1.0, 'discovered')",
                (addr,),
            )
            inserted += 1

    print(f"\n+{inserted} insertados, ={skipped} skipeados.")
    with db() as c:
        rows = c.execute("SELECT wallet, status FROM dx_subscriptions ORDER BY wallet").fetchall()
        print(f"Estado final: {len(rows)} wallets")
        for r in rows:
            print(f"  {r['wallet'][:20]}.. status={r['status']}")


if __name__ == "__main__":
    asyncio.run(main())
