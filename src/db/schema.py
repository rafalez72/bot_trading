"""Schema + helpers de conexión — dual-backend SQLite/Postgres.

Selección de backend vía env var ``DB_BACKEND``:
- ``sqlite`` (default): driver ``sqlite3`` contra ``DB_PATH``.
- ``postgres``: driver ``psycopg`` contra ``POSTGRES_DSN`` (o construido a
  partir de POSTGRES_USER/PASSWORD/HOST/PORT/DB).

El codepath del bot usa ``conn.execute(sql, params)`` con `?` placeholders
y SQLite-specific functions (``strftime``, ``datetime('now')``, etc.). Para
no reescribir las ~100 queries del codebase, el wrapper ``_PgConn`` traduce
en runtime — costo ~µs por query (regex sub).

Tablas:
- markets         → metadata de cada mercado (condition_id como PK)
- traders         → wallets descubiertos durante la indexación
- trades          → cada trade individual (idempotente por id)
- trader_metrics  → métricas agregadas por wallet (refresh periódico)
- index_state     → cursor de indexación por wallet (para resume)
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.config import DB_PATH

log = logging.getLogger(__name__)

BACKEND = os.getenv("DB_BACKEND", "sqlite").strip().lower()
if BACKEND not in ("sqlite", "postgres"):
    raise RuntimeError(f"DB_BACKEND inválido: {BACKEND!r} (esperado: sqlite | postgres)")

# Lock process-wide para serializar TODAS las escrituras del proceso. Antes,
# 3 runners (PM/HL/DX) + sweeps + listener + reconciler + funding hacían
# BEGIN IMMEDIATE en paralelo y busy_timeout (5s) podía no alcanzar →
# "database is locked" → SELLs en runner._process_wallet caían en except y
# las posiciones nunca cerraban (incidente 2026-05-06: sólo BUYs, 0 SELLs).
# Con este Lock, las txns intra-proceso esperan en Python (~ms) en vez de
# rebotar contra SQLite. busy_timeout queda como red de seguridad para el
# choque runner↔server (procesos distintos del docker compose).
# RLock = re-entrante por si algún path llama tx() anidado.
#
# Para Postgres NO se usa: PG tiene row-level locks, dos tx que tocan filas
# distintas no se bloquean. Eso es la razón principal de migrar.
_TX_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Postgres wrapper: traduce queries SQLite-specific al volar
# --------------------------------------------------------------------------- #

# Las traducciones se aplican vía regex SOBRE la SQL string. Asumen que ningún
# patrón de los buscados aparece dentro de un string-literal del SQL (cierto
# en este codebase). Los placeholders `?` → `%s` también via regex — psycopg3
# usa `%s` por default.
_PG_RX_STRFTIME_NOW = re.compile(r"strftime\s*\(\s*'%s'\s*,\s*['\"]now['\"]\s*\)")
_PG_RX_STRFTIME_NOW_DELTA = re.compile(
    r"strftime\s*\(\s*'%s'\s*,\s*['\"]now['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)"
)
_PG_RX_STRFTIME_HOUR_UNIX = re.compile(
    r"strftime\s*\(\s*'%H'\s*,\s*([^,)]+)\s*,\s*['\"]unixepoch['\"]\s*\)"
)
_PG_RX_STRFTIME_COL = re.compile(
    r"strftime\s*\(\s*'%s'\s*,\s*([^)]+?)\s*\)"
)
_PG_RX_DATETIME_NOW = re.compile(r"datetime\s*\(\s*['\"]now['\"]\s*\)")
_PG_RX_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
_PG_RX_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE)
_PG_RX_QUESTIONMARK = re.compile(r"\?")


def _translate_sql_to_pg(sql: str) -> str:
    """Traduce SQL SQLite → PG. Se aplica a cada query antes de execute."""
    # strftime('%s','now','-1 day') → EXTRACT(EPOCH FROM NOW() + INTERVAL '-1 day')::BIGINT
    sql = _PG_RX_STRFTIME_NOW_DELTA.sub(
        lambda m: f"EXTRACT(EPOCH FROM NOW() + INTERVAL '{m.group(1)}')::BIGINT",
        sql,
    )
    # strftime('%H', col, 'unixepoch') → EXTRACT(HOUR FROM to_timestamp(col))
    sql = _PG_RX_STRFTIME_HOUR_UNIX.sub(
        lambda m: f"EXTRACT(HOUR FROM to_timestamp({m.group(1).strip()}))",
        sql,
    )
    # strftime('%s','now') → EXTRACT(EPOCH FROM NOW())::BIGINT
    sql = _PG_RX_STRFTIME_NOW.sub("EXTRACT(EPOCH FROM NOW())::BIGINT", sql)
    # strftime('%s', some_col) → EXTRACT(EPOCH FROM some_col::TIMESTAMP)::BIGINT
    sql = _PG_RX_STRFTIME_COL.sub(
        lambda m: f"EXTRACT(EPOCH FROM ({m.group(1).strip()})::TIMESTAMP)::BIGINT",
        sql,
    )
    # datetime('now') → string ISO en UTC
    sql = _PG_RX_DATETIME_NOW.sub(
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')", sql
    )
    # INSERT OR IGNORE → INSERT (con ON CONFLICT DO NOTHING agregado abajo)
    # Pero el code ya usa "ON CONFLICT(...) DO UPDATE SET ..." con explicit ON CONFLICT;
    # el INSERT OR IGNORE solo aparece en shadow_trades (1 query). Para esa hay que
    # agregar "ON CONFLICT (trade_id) DO NOTHING" — el "OR IGNORE" lo reemplazamos
    # por "" y el caller (shadow.py) tiene que agregarlo. En el codebase actual el
    # único INSERT OR IGNORE es a shadow_trades(trade_id UNIQUE) — agregamos un fix
    # postfix.
    if _PG_RX_INSERT_OR_IGNORE.search(sql):
        sql = _PG_RX_INSERT_OR_IGNORE.sub("INSERT", sql)
        if "ON CONFLICT" not in sql.upper():
            # Asume target unique columns (trade_id en shadow_trades, source_fill_id
            # en hl/dx_trades). PG necesita el conflict target explícito; sin él
            # daría error. Si aparece otra tabla con OR IGNORE, romperá ruidosamente
            # y agregamos manualmente.
            sql = sql.rstrip(" ;") + " ON CONFLICT DO NOTHING"
    if _PG_RX_INSERT_OR_REPLACE.search(sql):
        # No usamos esto en el codebase actual — falla ruidosa si aparece.
        raise RuntimeError(
            "INSERT OR REPLACE no soportado en backend postgres; usar ON CONFLICT explícito"
        )
    # ? → %s (psycopg3 default placeholder)
    sql = _PG_RX_QUESTIONMARK.sub("%s", sql)
    return sql


class _PgRow(dict):
    """Mimic de sqlite3.Row: acceso por key + posicional + iteración."""

    def __init__(self, columns: list[str], values: tuple) -> None:
        super().__init__(zip(columns, values))
        self._cols = columns
        self._vals = values

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            return self._vals[key]
        return super().__getitem__(key)

    def keys(self):  # type: ignore[override]
        return list(self._cols)


_PG_RX_INSERT_TARGET = re.compile(
    r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE
)
_PG_RX_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)
# Tablas con `id BIGSERIAL PRIMARY KEY` — sólo a éstas les inyectamos
# RETURNING id para emular sqlite3 .lastrowid. Las demás tienen PK distinto
# (wallet, key, condition_id, etc.) y RETURNING id rompería con
# UndefinedColumn → aborta la txn.
_PG_TABLES_WITH_ID = frozenset({
    "paper_trades",
    "learning_events",
    "live_trades",
    "live_rejects",
    "hl_trades",
    "hl_rejects",
    "shadow_trades",
    "dx_trades",
    "dx_rejects",
})


class _PgCursor:
    """Cursor que traduce SQL al pasar por execute*.

    Incluye un truco para emular ``sqlite3.Cursor.lastrowid``: si el SQL es
    un ``INSERT INTO`` sin ``RETURNING`` explícito, le agregamos ``RETURNING
    id`` y guardamos el primer id devuelto en ``self._lastrowid``. Sin esto,
    los callers que hacen ``cur.execute('INSERT ...'); cur.lastrowid``
    obtendrían siempre None en backend postgres (paper.py:333,
    hl_executor.py:229, dx_executor.py:212, reconciler.py:213).
    """

    def __init__(self, real_cursor) -> None:
        self._cur = real_cursor
        self._lastrowid: int | None = None

    def _wrap_rows(self, rows):
        if not rows or self._cur.description is None:
            return rows
        cols = [d[0] for d in self._cur.description]
        return [_PgRow(cols, r) for r in rows]

    def _wrap_one(self, row):
        if row is None or self._cur.description is None:
            return row
        cols = [d[0] for d in self._cur.description]
        return _PgRow(cols, row)

    def _maybe_inject_returning(self, sql: str) -> tuple[str, bool]:
        """Si es INSERT a tabla con BIGSERIAL `id`, sin RETURNING, lo agregamos."""
        m = _PG_RX_INSERT_TARGET.match(sql)
        if not m:
            return sql, False
        table = m.group(1).lower()
        if table not in _PG_TABLES_WITH_ID:
            return sql, False
        if _PG_RX_RETURNING.search(sql):
            return sql, False
        return sql.rstrip("; \n\t") + " RETURNING id", True

    def execute(self, sql: str, params=None):
        sql = _translate_sql_to_pg(sql)
        sql, captured_returning = self._maybe_inject_returning(sql)
        if params is None:
            self._cur.execute(sql)
        else:
            self._cur.execute(sql, params)
        if captured_returning and self._cur.description is not None:
            try:
                row = self._cur.fetchone()
                self._lastrowid = int(row[0]) if row else None
            except Exception:
                self._lastrowid = None
        return self

    def executemany(self, sql: str, params_list):
        sql = _translate_sql_to_pg(sql)
        # No aplicamos RETURNING en executemany — sería ambiguo qué id tomar.
        self._cur.executemany(sql, params_list)
        return self

    def executescript(self, sql_script: str):
        # PG no tiene executescript; psycopg ejecuta multi-statement directo.
        # No aplicamos traducción aquí porque executescript se llama solo desde
        # init_db con el SCHEMA SQLite (que NO se ejecuta cuando BACKEND=postgres).
        self._cur.execute(sql_script)
        return self

    def fetchone(self):
        return self._wrap_one(self._cur.fetchone())

    def fetchall(self):
        return self._wrap_rows(self._cur.fetchall())

    def __iter__(self):
        # Iterar el cursor real y wrapear por fila
        if self._cur.description is None:
            return iter([])
        cols = [d[0] for d in self._cur.description]
        return (_PgRow(cols, r) for r in self._cur)

    @property
    def lastrowid(self):
        return self._lastrowid

    def close(self):
        self._cur.close()


class _PgConn:
    """Wrapper sobre psycopg.Connection con API sqlite3.Connection."""

    def __init__(self, real_conn) -> None:
        self._conn = real_conn

    # sqlite3-style: connection.execute() devuelve un cursor con resultados.
    def execute(self, sql: str, params=None):
        cur = _PgCursor(self._conn.cursor())
        return cur.execute(sql, params)

    def executemany(self, sql: str, params_list):
        cur = _PgCursor(self._conn.cursor())
        return cur.executemany(sql, params_list)

    def executescript(self, sql_script: str):
        cur = _PgCursor(self._conn.cursor())
        cur.executescript(sql_script)
        return cur

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    # Para uso con `with _connect() as conn:` en SQLite, sqlite3.Connection es
    # context manager que commitea/rollback automáticamente. Replicamos.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
        else:
            try:
                self._conn.rollback()
            except Exception:
                pass
        self.close()
        return False


def _build_pg_dsn() -> str:
    dsn = os.getenv("POSTGRES_DSN")
    if dsn:
        return dsn
    user = os.getenv("POSTGRES_USER", "copybot")
    pw = os.getenv("POSTGRES_PASSWORD")
    if not pw:
        raise RuntimeError(
            "DB_BACKEND=postgres pero POSTGRES_PASSWORD no está seteado en .env"
        )
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "copybot")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


# --------------------------------------------------------------------------- #
# SQLite schema (sigue siendo source-of-truth cuando BACKEND=sqlite).
# Para Postgres usamos scripts/postgres/schema.sql en init_db.
# --------------------------------------------------------------------------- #

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS markets (
    condition_id      TEXT PRIMARY KEY,
    question          TEXT,
    slug              TEXT,
    category          TEXT,
    end_date          TEXT,
    active            INTEGER,
    closed            INTEGER,
    volume            REAL,
    liquidity         REAL,
    outcomes          TEXT,
    outcome_prices    TEXT,
    last_seen_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, closed);
CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);

CREATE TABLE IF NOT EXISTS traders (
    wallet            TEXT PRIMARY KEY,
    first_seen_at     TEXT DEFAULT (datetime('now')),
    last_indexed_at   TEXT,
    total_trades      INTEGER DEFAULT 0,
    flagged           INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    id                TEXT PRIMARY KEY,
    wallet            TEXT NOT NULL,
    condition_id      TEXT NOT NULL,
    side              TEXT NOT NULL,
    outcome           TEXT,
    outcome_index     INTEGER,
    price             REAL NOT NULL,
    size              REAL NOT NULL,
    usdc_value        REAL,
    timestamp         INTEGER NOT NULL,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(condition_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp DESC);

CREATE TABLE IF NOT EXISTS trader_metrics (
    wallet                  TEXT PRIMARY KEY,
    total_trades            INTEGER,
    total_volume_usdc       REAL,
    realized_pnl_usdc       REAL,
    unrealized_pnl_usdc     REAL,
    roi_pct                 REAL,
    win_rate                REAL,
    avg_position_size       REAL,
    max_drawdown_pct        REAL,
    sharpe_proxy            REAL,
    active_days             INTEGER,
    first_trade_ts          INTEGER,
    last_trade_ts           INTEGER,
    score                   REAL,
    computed_at             TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (wallet) REFERENCES traders(wallet)
);
CREATE INDEX IF NOT EXISTS idx_metrics_score ON trader_metrics(score DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_roi ON trader_metrics(roi_pct DESC);

CREATE TABLE IF NOT EXISTS index_state (
    key               TEXT PRIMARY KEY,
    value             TEXT,
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS copy_subscriptions (
    wallet            TEXT PRIMARY KEY,
    started_at        TEXT DEFAULT (datetime('now')),
    stopped_at        TEXT,
    status            TEXT DEFAULT 'active',
    reason            TEXT,
    score_at_start    REAL,
    sizing_mult       REAL DEFAULT 1.0,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_copy_status ON copy_subscriptions(status);

CREATE TABLE IF NOT EXISTS paper_trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_wallet     TEXT NOT NULL,
    source_trade_id   TEXT,
    condition_id     TEXT NOT NULL,
    outcome           TEXT,
    outcome_index     INTEGER,
    side              TEXT NOT NULL,
    entry_price       REAL,
    entry_size_usdc   REAL,
    entry_at          INTEGER,
    exit_price        REAL,
    exit_at           INTEGER,
    pnl_usdc          REAL,
    status            TEXT DEFAULT 'open',
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_source ON paper_trades(source_wallet);
CREATE INDEX IF NOT EXISTS idx_paper_entry ON paper_trades(entry_at DESC);

CREATE TABLE IF NOT EXISTS learning_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet            TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    before_value      REAL,
    after_value       REAL,
    delta             REAL,
    trigger           TEXT,
    metric_snapshot   TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learn_wallet ON learning_events(wallet, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learn_time ON learning_events(created_at DESC);
"""


def _connect_sqlite() -> sqlite3.Connection:
    # busy_timeout=15s = red de seguridad para choque INTER-PROCESO
    # (runner ↔ server FastAPI escriben al mismo .db). Para choque
    # intra-proceso usamos _TX_LOCK (Python lock) que es ~ms.
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _connect_postgres() -> _PgConn:
    import psycopg

    dsn = _build_pg_dsn()
    real = psycopg.connect(dsn, autocommit=True)
    return _PgConn(real)


def _connect():
    return _connect_sqlite() if BACKEND == "sqlite" else _connect_postgres()


def _retry_locked(fn, max_retries: int = 2, base_delay: float = 0.5):
    """Retry helper para 'database is locked' (sólo SQLite)."""
    import time as _t
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_err = e
            _t.sleep(base_delay)
    raise last_err


_MIGRATIONS = [
    # idempotentes para SQLite: si la columna ya existe, ignoramos el error.
    # Para Postgres, el schema.sql ya tiene todos los campos inlineados, así
    # que estas migrations NO se ejecutan en backend=postgres.
    "ALTER TABLE markets ADD COLUMN outcome_prices TEXT",
    "ALTER TABLE paper_trades ADD COLUMN asset TEXT",
    "ALTER TABLE paper_trades ADD COLUMN exit_reason TEXT",
    """CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS category_perf (
        category        TEXT PRIMARY KEY,
        n_trades        INTEGER DEFAULT 0,
        wins            INTEGER DEFAULT 0,
        losses          INTEGER DEFAULT 0,
        pnl_usdc        REAL DEFAULT 0,
        invested_usdc   REAL DEFAULT 0,
        status          TEXT DEFAULT 'allowed',
        blocked_at      TEXT,
        blocked_reason  TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS filter_thresholds (
        key             TEXT PRIMARY KEY,
        value           REAL,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS bandit_state (
        wallet              TEXT PRIMARY KEY,
        n_pulls             INTEGER DEFAULT 0,
        sum_reward          REAL DEFAULT 0,
        ucb_score           REAL DEFAULT 0,
        updated_at          TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS wallet_clusters (
        wallet              TEXT PRIMARY KEY,
        cluster_id          INTEGER NOT NULL,
        features            TEXT,
        updated_at          TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wcluster_id ON wallet_clusters(cluster_id)",
    """CREATE TABLE IF NOT EXISTS cluster_perf (
        cluster_id          INTEGER PRIMARY KEY,
        n_wallets           INTEGER DEFAULT 0,
        n_trades            INTEGER DEFAULT 0,
        wins                INTEGER DEFAULT 0,
        losses              INTEGER DEFAULT 0,
        pnl_usdc            REAL DEFAULT 0,
        avg_win_rate        REAL,
        status              TEXT DEFAULT 'allowed',
        updated_at          TEXT DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS live_trades (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_wallet     TEXT NOT NULL,
        source_trade_id   TEXT,
        condition_id      TEXT NOT NULL,
        token_id          TEXT,
        outcome           TEXT,
        outcome_index     INTEGER,
        side              TEXT NOT NULL,
        entry_price       REAL,
        entry_size_usdc   REAL,
        entry_shares      REAL,
        entry_at          INTEGER,
        entry_order_id    TEXT,
        entry_tx_hash     TEXT,
        exit_price        REAL,
        exit_at           INTEGER,
        exit_order_id     TEXT,
        exit_tx_hash      TEXT,
        exit_shares       REAL,
        fees_usdc         REAL DEFAULT 0,
        pnl_usdc          REAL,
        status            TEXT DEFAULT 'open',
        exit_reason       TEXT,
        asset             TEXT,
        raw               TEXT,
        dry_run           INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_live_status ON live_trades(status)",
    "CREATE INDEX IF NOT EXISTS idx_live_source ON live_trades(source_wallet)",
    "CREATE INDEX IF NOT EXISTS idx_live_entry ON live_trades(entry_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_live_token ON live_trades(token_id)",
    """CREATE TABLE IF NOT EXISTS live_rejects (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        at              INTEGER NOT NULL,
        source_wallet   TEXT NOT NULL,
        condition_id    TEXT,
        outcome_index   INTEGER,
        side            TEXT,
        price           REAL,
        reason          TEXT NOT NULL,
        detail          TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_live_rejects_at ON live_rejects(at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_live_rejects_reason ON live_rejects(reason, at DESC)",
    "ALTER TABLE live_trades ADD COLUMN peak_price REAL",
    "ALTER TABLE paper_trades ADD COLUMN peak_price REAL",
    """CREATE TABLE IF NOT EXISTS hl_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_wallet   TEXT NOT NULL,
        source_fill_id  TEXT UNIQUE,
        coin            TEXT NOT NULL,
        is_buy          INTEGER NOT NULL,
        leverage        REAL DEFAULT 1.0,
        entry_at        INTEGER NOT NULL,
        exit_at         INTEGER,
        entry_price     REAL NOT NULL,
        exit_price      REAL,
        peak_price      REAL,
        entry_size_usdc REAL NOT NULL,
        exit_size_usdc  REAL,
        pnl_usdc        REAL,
        funding_paid    REAL DEFAULT 0,
        liquidation_price REAL,
        status          TEXT NOT NULL,
        exit_reason     TEXT,
        dry_run         INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_hl_trades_wallet ON hl_trades(source_wallet, entry_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hl_trades_status ON hl_trades(status)",
    """CREATE TABLE IF NOT EXISTS hl_subscriptions (
        wallet      TEXT PRIMARY KEY,
        status      TEXT NOT NULL DEFAULT 'active',
        sizing_mult REAL DEFAULT 1.0,
        started_at  TEXT DEFAULT (datetime('now')),
        stopped_at  TEXT,
        notes       TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS hl_rejects (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        at            INTEGER NOT NULL,
        source_wallet TEXT,
        coin          TEXT,
        is_buy        INTEGER,
        price         REAL,
        reason        TEXT NOT NULL,
        detail        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_hl_rejects_at ON hl_rejects(at DESC)",
    """CREATE TABLE IF NOT EXISTS shadow_trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet        TEXT NOT NULL,
        drop_reason   TEXT,
        trade_id      TEXT UNIQUE,
        timestamp     INTEGER NOT NULL,
        condition_id  TEXT,
        slug          TEXT,
        side          TEXT,
        outcome_index INTEGER,
        price         REAL,
        size_usdc     REAL,
        observed_at   INTEGER DEFAULT (strftime('%s','now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_shadow_wallet ON shadow_trades(wallet, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_shadow_observed ON shadow_trades(observed_at DESC)",
    """CREATE TABLE IF NOT EXISTS dx_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_wallet   TEXT NOT NULL,
        source_fill_id  TEXT UNIQUE,
        ticker          TEXT NOT NULL,
        is_buy          INTEGER NOT NULL,
        leverage        REAL DEFAULT 1.0,
        entry_at        INTEGER NOT NULL,
        exit_at         INTEGER,
        entry_price     REAL NOT NULL,
        exit_price      REAL,
        peak_price      REAL,
        entry_size_usdc REAL NOT NULL,
        exit_size_usdc  REAL,
        pnl_usdc        REAL,
        liquidation_price REAL,
        status          TEXT NOT NULL,
        exit_reason     TEXT,
        dry_run         INTEGER DEFAULT 1,
        created_at      TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dx_trades_wallet ON dx_trades(source_wallet, entry_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dx_trades_status ON dx_trades(status)",
    """CREATE TABLE IF NOT EXISTS dx_subscriptions (
        wallet      TEXT PRIMARY KEY,
        status      TEXT NOT NULL DEFAULT 'active',
        sizing_mult REAL DEFAULT 1.0,
        started_at  TEXT DEFAULT (datetime('now')),
        stopped_at  TEXT,
        notes       TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS dx_rejects (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        at            INTEGER NOT NULL,
        source_wallet TEXT,
        ticker        TEXT,
        is_buy        INTEGER,
        price         REAL,
        reason        TEXT NOT NULL,
        detail        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dx_rejects_at ON dx_rejects(at DESC)",
    "ALTER TABLE dx_trades ADD COLUMN funding_paid REAL DEFAULT 0",
    "ALTER TABLE dx_trades ADD COLUMN gas_paid REAL DEFAULT 0",
    "ALTER TABLE hl_trades ADD COLUMN gas_paid REAL DEFAULT 0",
]


_PG_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "postgres" / "schema.sql"


def init_db() -> None:
    if BACKEND == "sqlite":
        with _connect_sqlite() as conn:
            conn.executescript(SCHEMA)
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise
    else:
        # Postgres: aplicar el schema.sql idempotente. Si las tablas ya
        # existen (CREATE TABLE IF NOT EXISTS) es no-op.
        if not _PG_SCHEMA_PATH.exists():
            log.warning(
                "PG schema file no encontrado en %s — asumiendo que el container "
                "postgres ya lo aplicó vía /docker-entrypoint-initdb.d",
                _PG_SCHEMA_PATH,
            )
            return
        ddl = _PG_SCHEMA_PATH.read_text()
        import psycopg
        conn = psycopg.connect(_build_pg_dsn(), autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
        finally:
            conn.close()


@contextmanager
def db() -> Iterator[Any]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx() -> Iterator[Any]:
    """Transacción explícita.

    SQLite: serializada por process-wide ``_TX_LOCK`` + BEGIN IMMEDIATE +
    busy_timeout=15s + retry_locked. Razón: lock global a nivel archivo.

    Postgres: row-level locks nativos. Una `BEGIN` simple, sin process-lock
    (PG maneja concurrencia entre procesos via MVCC). El _TX_LOCK no se
    toma — múltiples writers concurrentes a filas distintas no se bloquean.
    """
    if BACKEND == "sqlite":
        with _TX_LOCK:
            conn = _connect_sqlite()
            try:
                _retry_locked(lambda: conn.execute("BEGIN IMMEDIATE"))
                yield conn
                _retry_locked(lambda: conn.execute("COMMIT"))
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                conn.close()
    else:
        # Postgres
        import psycopg
        real = psycopg.connect(_build_pg_dsn(), autocommit=False)
        conn = _PgConn(real)
        try:
            yield conn
            real.commit()
        except Exception:
            try:
                real.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
