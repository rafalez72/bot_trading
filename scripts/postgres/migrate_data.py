"""Migra data SQLite → Postgres tabla por tabla.

Uso:
    python scripts/postgres/migrate_data.py \\
        --sqlite data/copybot.db \\
        --pg-dsn "postgresql://copybot:secret@localhost:5432/copybot"

Diseño:
- Lee row-by-row de SQLite y bulk-inserta en Postgres con executemany.
- Para tablas con BIGSERIAL PRIMARY KEY, después del insert hace
  setval del sequence al MAX(id) para que los próximos inserts no
  colisionen con ids existentes.
- Skipea tablas que ya tienen data (run idempotente — útil si la
  primera corrida murió a mitad).
- NO toca SQLite (read-only). Si algo sale mal, SQLite sigue intacto
  y el bot sigue corriendo contra él.

Pre-requisito: el schema PG ya creado vía
    psql $PG_DSN -f scripts/postgres/schema.sql

Tiempo estimado: ~1-3 min para 100k filas.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from typing import Any, Iterable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# Tablas con BIGSERIAL/SERIAL — necesitan setval post-import para que el
# sequence avance al máximo id existente y los próximos inserts no
# colisionen.
SERIAL_TABLES = {
    "paper_trades": "id",
    "learning_events": "id",
    "live_trades": "id",
    "live_rejects": "id",
    "hl_trades": "id",
    "hl_rejects": "id",
    "shadow_trades": "id",
    "dx_trades": "id",
    "dx_rejects": "id",
}

# Orden de migración: respeta FKs.
TABLE_ORDER = [
    "markets",
    "traders",
    "trades",
    "trader_metrics",
    "index_state",
    "copy_subscriptions",
    "paper_trades",
    "learning_events",
    "bot_state",
    "category_perf",
    "filter_thresholds",
    "bandit_state",
    "wallet_clusters",
    "cluster_perf",
    "live_trades",
    "live_rejects",
    "hl_trades",
    "hl_subscriptions",
    "hl_rejects",
    "shadow_trades",
    "dx_trades",
    "dx_subscriptions",
    "dx_rejects",
]

CHUNK_SIZE = 500


def _columns(sqlite_conn: sqlite3.Connection, table: str) -> list[str]:
    rows = sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r["name"] for r in rows]


def _row_count_pg(pg_cur, table: str) -> int:
    pg_cur.execute(f"SELECT COUNT(*) FROM {table}")
    return pg_cur.fetchone()[0]


def _pg_columns(pg_cur, table: str) -> list[str]:
    pg_cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return [r[0] for r in pg_cur.fetchall()]


def _chunked(it: Iterable, n: int) -> Iterable[list]:
    buf: list = []
    for x in it:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    sqlite_cols = _columns(sqlite_conn, table)
    if not sqlite_cols:
        log.warning("table %s no existe en SQLite — skip", table)
        return {"skipped": "missing"}

    with pg_conn.cursor() as pg_cur:
        pg_cols = _pg_columns(pg_cur, table)
        if not pg_cols:
            log.warning("table %s no existe en PG — skip", table)
            return {"skipped": "pg_missing"}
        # Solo migramos columnas que existen en AMBOS lados
        cols = [c for c in sqlite_cols if c in pg_cols]
        if len(cols) < len(sqlite_cols):
            missing = set(sqlite_cols) - set(cols)
            log.info("table %s: %d cols en SQLite no existen en PG (%s) — ignoradas",
                     table, len(missing), sorted(missing))

        if not overwrite:
            n_existing = _row_count_pg(pg_cur, table)
            if n_existing > 0:
                log.info("table %s ya tiene %d filas en PG — skip (use --overwrite)",
                         table, n_existing)
                return {"skipped": "existing", "rows": n_existing}
        else:
            pg_cur.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")

        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

        cur = sqlite_conn.execute(f"SELECT {col_list} FROM {table}")
        total = 0
        for chunk in _chunked(cur, CHUNK_SIZE):
            rows = [tuple(r[c] for c in cols) for r in chunk]
            pg_cur.executemany(sql, rows)
            total += len(rows)
        pg_conn.commit()

        # setval del sequence si aplica
        if table in SERIAL_TABLES:
            pk = SERIAL_TABLES[table]
            seq_name = f"{table}_{pk}_seq"
            pg_cur.execute(
                f"SELECT setval('{seq_name}', GREATEST(COALESCE(MAX({pk}), 0), 1)) FROM {table}"
            )
            pg_conn.commit()

        log.info("table %s migrada: %d filas", table, total)
        return {"migrated": total}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sqlite", default="data/copybot.db")
    p.add_argument("--pg-dsn", required=True,
                   help='ej: "postgresql://copybot:pwd@localhost:5432/copybot"')
    p.add_argument("--overwrite", action="store_true",
                   help="TRUNCATE las tablas PG antes de migrar (DESTRUCTIVO)")
    p.add_argument("--tables", nargs="*", default=None,
                   help="Sólo migra estas tablas. Default: todas en TABLE_ORDER.")
    args = p.parse_args()

    try:
        import psycopg
    except ImportError:
        log.error("psycopg no instalado. pip install 'psycopg[binary]>=3.2'")
        return 1

    sqlite_conn = sqlite3.connect(args.sqlite)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg.connect(args.pg_dsn)
    log.info("conectado a postgres")

    tables = args.tables or TABLE_ORDER
    summary: dict[str, dict] = {}
    for table in tables:
        try:
            summary[table] = migrate_table(
                sqlite_conn, pg_conn, table, overwrite=args.overwrite
            )
        except Exception as e:
            log.exception("table %s falló: %s", table, e)
            summary[table] = {"error": str(e)}

    log.info("resumen: %s", json.dumps(summary, indent=2))
    pg_conn.close()
    sqlite_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
