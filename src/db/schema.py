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
    # timeout=30 → SQLite espera hasta 30s a que se libere el lock antes de
    # tirar "database is locked". Con 3 runners paralelos (PM/HL/DX) escribiendo,
    # evita que se pierdan INSERTs (eso causó trades fantasma 2026-05-05 —
    # BUYs ejecutados on-chain sin row en live_trades).
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30.0)  # autocommit
    conn.row_factory = sqlite3.Row
    # WAL: writers no bloquean readers (mucha mejor concurrencia que rollback journal)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _retry_locked(fn, max_retries: int = 5, base_delay: float = 0.1):
    """Retry helper para 'database is locked'."""
    import time as _t
    last_err = None
    for attempt in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower():
                raise
            last_err = e
            _t.sleep(base_delay * (2 ** attempt))  # 0.1, 0.2, 0.4, 0.8, 1.6
    raise last_err


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
    # Trailing stop: peak_price es el máximo observado durante la vida de la
    # posición. Lo usamos para activar trailing-stop cuando el trade ya está
    # +30% en ganancia, y cerrar si después cae 25% del peak.
    "ALTER TABLE live_trades ADD COLUMN peak_price REAL",
    "ALTER TABLE paper_trades ADD COLUMN peak_price REAL",
    # ── Hyperliquid (perps copy-bot dry-run, paralelo al PM bot)
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
    # ── Shadow tracking: registra trades de wallets dropped (post-drop) para
    # analizar a posteriori si dropearlos costó plata. NO se copian, solo se observan.
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
    # ── dYdX v4 (3er bot — perps en Cosmos chain, dry-run paralelo)
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
    # ── Production parity additions: simulamos costos reales para que dry-run
    # prediga net PnL real (gas + funding rate + fill realista por orderbook).
    "ALTER TABLE dx_trades ADD COLUMN funding_paid REAL DEFAULT 0",
    "ALTER TABLE dx_trades ADD COLUMN gas_paid REAL DEFAULT 0",
    "ALTER TABLE hl_trades ADD COLUMN gas_paid REAL DEFAULT 0",
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
    """Transacción explícita con retry on database is locked."""
    conn = _connect()
    try:
        # Retry el BEGIN si está locked (timeout=30 normalmente lo cubre, pero
        # con 3 runners paralelos a veces da locked igual)
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
