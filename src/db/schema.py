"""Schema SQLite + helpers de conexión.

Tablas:
- markets         → metadata de cada mercado (condition_id como PK)
- traders         → wallets descubiertos durante la indexación
- trades          → cada trade individual (idempotente por id)
- trader_metrics  → métricas agregadas por wallet (refresh periódico)
- index_state     → cursor de indexación por wallet (para resume)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from src.config import DB_PATH

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
    outcomes          TEXT,        -- JSON array
    outcome_prices    TEXT,        -- JSON array; tras resolución es [1,0] o [0,1]
    last_seen_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, closed);
CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);

CREATE TABLE IF NOT EXISTS traders (
    wallet            TEXT PRIMARY KEY,    -- 0x... lowercase
    first_seen_at     TEXT DEFAULT (datetime('now')),
    last_indexed_at   TEXT,
    total_trades      INTEGER DEFAULT 0,
    flagged           INTEGER DEFAULT 0    -- 1 = candidato a copiar
);

CREATE TABLE IF NOT EXISTS trades (
    id                TEXT PRIMARY KEY,    -- tx hash + log index normalizado
    wallet            TEXT NOT NULL,
    condition_id      TEXT NOT NULL,
    side              TEXT NOT NULL,       -- BUY | SELL
    outcome           TEXT,
    outcome_index     INTEGER,
    price             REAL NOT NULL,
    size              REAL NOT NULL,       -- en shares
    usdc_value        REAL,
    timestamp         INTEGER NOT NULL,    -- unix seconds
    raw               TEXT                 -- JSON original (debug)
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
    score                   REAL,           -- score compuesto (ranking)
    computed_at             TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (wallet) REFERENCES traders(wallet)
);
CREATE INDEX IF NOT EXISTS idx_metrics_score ON trader_metrics(score DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_roi ON trader_metrics(roi_pct DESC);

CREATE TABLE IF NOT EXISTS index_state (
    key               TEXT PRIMARY KEY,    -- e.g. "trades_cursor:0xabc..."
    value             TEXT,
    updated_at        TEXT DEFAULT (datetime('now'))
);

-- Suscripciones de copy: traders que el bot está copiando ahora.
CREATE TABLE IF NOT EXISTS copy_subscriptions (
    wallet            TEXT PRIMARY KEY,
    started_at        TEXT DEFAULT (datetime('now')),
    stopped_at        TEXT,
    status            TEXT DEFAULT 'active',   -- active | paused | dropped
    reason            TEXT,                     -- razón humana de por qué se copia
    score_at_start    REAL,
    sizing_mult       REAL DEFAULT 1.0,         -- ajustado por learning (0.1..2.0)
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_copy_status ON copy_subscriptions(status);

-- Paper trades: simulación 1:1 de cada trade copiado.
CREATE TABLE IF NOT EXISTS paper_trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_wallet     TEXT NOT NULL,
    source_trade_id   TEXT,           -- referencia a trades.id
    condition_id     TEXT NOT NULL,
    outcome           TEXT,
    outcome_index     INTEGER,
    side              TEXT NOT NULL,  -- BUY | SELL
    entry_price       REAL,
    entry_size_usdc   REAL,           -- USDC apostado (despues de sizing_mult)
    entry_at          INTEGER,        -- unix ts
    exit_price        REAL,
    exit_at           INTEGER,
    pnl_usdc          REAL,
    status            TEXT DEFAULT 'open',  -- open | closed_win | closed_loss | settled_win | settled_loss
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_source ON paper_trades(source_wallet);
CREATE INDEX IF NOT EXISTS idx_paper_entry ON paper_trades(entry_at DESC);

-- Eventos de aprendizaje: cada vez que ajustamos sizing/score por performance.
CREATE TABLE IF NOT EXISTS learning_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet            TEXT NOT NULL,
    event_type        TEXT NOT NULL,   -- size_up | size_down | drop | promote | score_update
    before_value      REAL,
    after_value       REAL,
    delta             REAL,
    trigger           TEXT,            -- e.g. "5 wins consecutivos"
    metric_snapshot   TEXT,            -- JSON con métricas al momento
    created_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_learn_wallet ON learning_events(wallet, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learn_time ON learning_events(created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    return conn


_MIGRATIONS = [
    # idempotentes: si la columna ya existe, ignoramos el error
    "ALTER TABLE markets ADD COLUMN outcome_prices TEXT",
    "ALTER TABLE paper_trades ADD COLUMN asset TEXT",
    "ALTER TABLE paper_trades ADD COLUMN exit_reason TEXT",
    """CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    )""",
    # Performance acumulada por categoría de mercado
    """CREATE TABLE IF NOT EXISTS category_perf (
        category        TEXT PRIMARY KEY,
        n_trades        INTEGER DEFAULT 0,
        wins            INTEGER DEFAULT 0,
        losses          INTEGER DEFAULT 0,
        pnl_usdc        REAL DEFAULT 0,
        invested_usdc   REAL DEFAULT 0,
        status          TEXT DEFAULT 'allowed',  -- allowed | blocked
        blocked_at      TEXT,
        blocked_reason  TEXT,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""",
    # Thresholds dinámicos del selector (auto-tuned)
    """CREATE TABLE IF NOT EXISTS filter_thresholds (
        key             TEXT PRIMARY KEY,
        value           REAL,
        updated_at      TEXT DEFAULT (datetime('now'))
    )""",
    # Estado per-trader del bandit (UCB1)
    """CREATE TABLE IF NOT EXISTS bandit_state (
        wallet              TEXT PRIMARY KEY,
        n_pulls             INTEGER DEFAULT 0,   -- trades cerrados
        sum_reward          REAL DEFAULT 0,      -- sum de PnL normalizado
        ucb_score           REAL DEFAULT 0,
        updated_at          TEXT DEFAULT (datetime('now'))
    )""",
    # Clustering de wallets por features de comportamiento (Fase 6b)
    """CREATE TABLE IF NOT EXISTS wallet_clusters (
        wallet              TEXT PRIMARY KEY,
        cluster_id          INTEGER NOT NULL,
        features            TEXT,           -- JSON de features usadas
        updated_at          TEXT DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wcluster_id ON wallet_clusters(cluster_id)",
    # Performance agregada por cluster (cross-pollination)
    """CREATE TABLE IF NOT EXISTS cluster_perf (
        cluster_id          INTEGER PRIMARY KEY,
        n_wallets           INTEGER DEFAULT 0,
        n_trades            INTEGER DEFAULT 0,
        wins                INTEGER DEFAULT 0,
        losses              INTEGER DEFAULT 0,
        pnl_usdc            REAL DEFAULT 0,
        avg_win_rate        REAL,
        status              TEXT DEFAULT 'allowed',  -- allowed | penalized | blocked
        updated_at          TEXT DEFAULT (datetime('now'))
    )""",
    # Live trades: trades reales ejecutados en Polymarket CLOB (Fase 5).
    # Esquema mirror de paper_trades + campos de execution real:
    #   token_id, entry_order_id, exit_order_id, entry_shares, exit_shares,
    #   entry_tx_hash, exit_tx_hash, fees_usdc.
    """CREATE TABLE IF NOT EXISTS live_trades (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_wallet     TEXT NOT NULL,
        source_trade_id   TEXT,
        condition_id      TEXT NOT NULL,
        token_id          TEXT,           -- ERC1155 token id en Polymarket
        outcome           TEXT,
        outcome_index     INTEGER,
        side              TEXT NOT NULL,
        entry_price       REAL,
        entry_size_usdc   REAL,
        entry_shares      REAL,           -- shares compradas
        entry_at          INTEGER,
        entry_order_id    TEXT,           -- order ID del CLOB
        entry_tx_hash     TEXT,           -- tx hash en Polygon (cuando matchea)
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
    # Live rejects: cada vez que un trade es descartado por el pipeline de
    # validación live, dejamos un registro con la razón y contexto. Sirve
    # para entender POR QUÉ no se abrieron posiciones (filtros muy duros,
    # concentración, kill_switch, etc.) sin tener que reproducir el trade.
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
]


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Transacción explícita."""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
