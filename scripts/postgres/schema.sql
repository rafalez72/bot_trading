-- Polymarket Copybot — Postgres schema (mirror de SQLite schema.py)
--
-- Reglas de traducción aplicadas:
--   INTEGER PRIMARY KEY AUTOINCREMENT  → BIGSERIAL PRIMARY KEY
--   REAL                               → DOUBLE PRECISION
--   timestamps de epoch (entry_at, ts) → BIGINT
--   TEXT DEFAULT (datetime('now'))     → TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
--   PRAGMA *                           → (no aplica)
--   raw JSON                           → JSONB (storage compacto + indexable)
--                                        Wrapper en src/db/schema.py serializa
--                                        dict→string en read para compat con json.loads.
--
-- Migraciones (los ALTER TABLE de schema.py) están INLINEADAS acá. Una vez
-- que arrancás Postgres limpio, este archivo crea TODO en el estado final.

CREATE TABLE IF NOT EXISTS markets (
    condition_id      TEXT PRIMARY KEY,
    question          TEXT,
    slug              TEXT,
    category          TEXT,
    end_date          TEXT,
    active            INTEGER,
    closed            INTEGER,
    volume            DOUBLE PRECISION,
    liquidity         DOUBLE PRECISION,
    outcomes          TEXT,
    outcome_prices    TEXT,
    last_seen_at      TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, closed);
CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);

CREATE TABLE IF NOT EXISTS traders (
    wallet            TEXT PRIMARY KEY,
    first_seen_at     TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
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
    price             DOUBLE PRECISION NOT NULL,
    size              DOUBLE PRECISION NOT NULL,
    usdc_value        DOUBLE PRECISION,
    timestamp         BIGINT NOT NULL,
    raw               JSONB
);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(condition_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp DESC);

CREATE TABLE IF NOT EXISTS trader_metrics (
    wallet                  TEXT PRIMARY KEY REFERENCES traders(wallet),
    total_trades            INTEGER,
    total_volume_usdc       DOUBLE PRECISION,
    realized_pnl_usdc       DOUBLE PRECISION,
    unrealized_pnl_usdc     DOUBLE PRECISION,
    roi_pct                 DOUBLE PRECISION,
    win_rate                DOUBLE PRECISION,
    avg_position_size       DOUBLE PRECISION,
    max_drawdown_pct        DOUBLE PRECISION,
    sharpe_proxy            DOUBLE PRECISION,
    active_days             INTEGER,
    first_trade_ts          BIGINT,
    last_trade_ts           BIGINT,
    score                   DOUBLE PRECISION,
    computed_at             TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_metrics_score ON trader_metrics(score DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_roi ON trader_metrics(roi_pct DESC);

CREATE TABLE IF NOT EXISTS index_state (
    key               TEXT PRIMARY KEY,
    value             TEXT,
    updated_at        TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS copy_subscriptions (
    wallet            TEXT PRIMARY KEY,
    started_at        TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
    stopped_at        TEXT,
    status            TEXT DEFAULT 'active',
    reason            TEXT,
    score_at_start    DOUBLE PRECISION,
    sizing_mult       DOUBLE PRECISION DEFAULT 1.0,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_copy_status ON copy_subscriptions(status);

CREATE TABLE IF NOT EXISTS paper_trades (
    id                BIGSERIAL PRIMARY KEY,
    source_wallet     TEXT NOT NULL,
    source_trade_id   TEXT,
    condition_id      TEXT NOT NULL,
    outcome           TEXT,
    outcome_index     INTEGER,
    side              TEXT NOT NULL,
    entry_price       DOUBLE PRECISION,
    entry_size_usdc   DOUBLE PRECISION,
    entry_at          BIGINT,
    exit_price        DOUBLE PRECISION,
    exit_at           BIGINT,
    pnl_usdc          DOUBLE PRECISION,
    status            TEXT DEFAULT 'open',
    raw               JSONB,
    asset             TEXT,
    exit_reason       TEXT,
    peak_price        DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_source ON paper_trades(source_wallet);
CREATE INDEX IF NOT EXISTS idx_paper_entry ON paper_trades(entry_at DESC);

CREATE TABLE IF NOT EXISTS learning_events (
    id                BIGSERIAL PRIMARY KEY,
    wallet            TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    before_value      DOUBLE PRECISION,
    after_value       DOUBLE PRECISION,
    delta             DOUBLE PRECISION,
    trigger           TEXT,
    metric_snapshot   TEXT,
    created_at        TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_learn_wallet ON learning_events(wallet, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_learn_time ON learning_events(created_at DESC);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS category_perf (
    category        TEXT PRIMARY KEY,
    n_trades        INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    pnl_usdc        DOUBLE PRECISION DEFAULT 0,
    invested_usdc   DOUBLE PRECISION DEFAULT 0,
    status          TEXT DEFAULT 'allowed',
    blocked_at      TEXT,
    blocked_reason  TEXT,
    updated_at      TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS filter_thresholds (
    key             TEXT PRIMARY KEY,
    value           DOUBLE PRECISION,
    updated_at      TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS bandit_state (
    wallet              TEXT PRIMARY KEY,
    n_pulls             INTEGER DEFAULT 0,
    sum_reward          DOUBLE PRECISION DEFAULT 0,
    ucb_score           DOUBLE PRECISION DEFAULT 0,
    updated_at          TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS wallet_clusters (
    wallet              TEXT PRIMARY KEY,
    cluster_id          INTEGER NOT NULL,
    features            TEXT,
    updated_at          TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_wcluster_id ON wallet_clusters(cluster_id);

CREATE TABLE IF NOT EXISTS cluster_perf (
    cluster_id          INTEGER PRIMARY KEY,
    n_wallets           INTEGER DEFAULT 0,
    n_trades            INTEGER DEFAULT 0,
    wins                INTEGER DEFAULT 0,
    losses              INTEGER DEFAULT 0,
    pnl_usdc            DOUBLE PRECISION DEFAULT 0,
    avg_win_rate        DOUBLE PRECISION,
    status              TEXT DEFAULT 'allowed',
    updated_at          TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS live_trades (
    id                BIGSERIAL PRIMARY KEY,
    source_wallet     TEXT NOT NULL,
    source_trade_id   TEXT,
    condition_id      TEXT NOT NULL,
    token_id          TEXT,
    outcome           TEXT,
    outcome_index     INTEGER,
    side              TEXT NOT NULL,
    entry_price       DOUBLE PRECISION,
    entry_size_usdc   DOUBLE PRECISION,
    entry_shares      DOUBLE PRECISION,
    entry_at          BIGINT,
    entry_order_id    TEXT,
    entry_tx_hash     TEXT,
    exit_price        DOUBLE PRECISION,
    exit_at           BIGINT,
    exit_order_id     TEXT,
    exit_tx_hash      TEXT,
    exit_shares       DOUBLE PRECISION,
    fees_usdc         DOUBLE PRECISION DEFAULT 0,
    pnl_usdc          DOUBLE PRECISION,
    status            TEXT DEFAULT 'open',
    exit_reason       TEXT,
    asset             TEXT,
    raw               JSONB,
    dry_run           INTEGER DEFAULT 0,
    peak_price        DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_live_status ON live_trades(status);
CREATE INDEX IF NOT EXISTS idx_live_source ON live_trades(source_wallet);
CREATE INDEX IF NOT EXISTS idx_live_entry ON live_trades(entry_at DESC);
CREATE INDEX IF NOT EXISTS idx_live_token ON live_trades(token_id);

CREATE TABLE IF NOT EXISTS live_rejects (
    id              BIGSERIAL PRIMARY KEY,
    at              BIGINT NOT NULL,
    source_wallet   TEXT NOT NULL,
    condition_id    TEXT,
    outcome_index   INTEGER,
    side            TEXT,
    price           DOUBLE PRECISION,
    reason          TEXT NOT NULL,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_rejects_at ON live_rejects(at DESC);
CREATE INDEX IF NOT EXISTS idx_live_rejects_reason ON live_rejects(reason, at DESC);

CREATE TABLE IF NOT EXISTS hl_trades (
    id              BIGSERIAL PRIMARY KEY,
    source_wallet   TEXT NOT NULL,
    source_fill_id  TEXT UNIQUE,
    coin            TEXT NOT NULL,
    is_buy          INTEGER NOT NULL,
    leverage        DOUBLE PRECISION DEFAULT 1.0,
    entry_at        BIGINT NOT NULL,
    exit_at         BIGINT,
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_price      DOUBLE PRECISION,
    peak_price      DOUBLE PRECISION,
    entry_size_usdc DOUBLE PRECISION NOT NULL,
    exit_size_usdc  DOUBLE PRECISION,
    pnl_usdc        DOUBLE PRECISION,
    funding_paid    DOUBLE PRECISION DEFAULT 0,
    liquidation_price DOUBLE PRECISION,
    status          TEXT NOT NULL,
    exit_reason     TEXT,
    dry_run         INTEGER DEFAULT 1,
    gas_paid        DOUBLE PRECISION DEFAULT 0,
    created_at      TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_hl_trades_wallet ON hl_trades(source_wallet, entry_at DESC);
CREATE INDEX IF NOT EXISTS idx_hl_trades_status ON hl_trades(status);

CREATE TABLE IF NOT EXISTS hl_subscriptions (
    wallet      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'active',
    sizing_mult DOUBLE PRECISION DEFAULT 1.0,
    started_at  TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
    stopped_at  TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS hl_rejects (
    id            BIGSERIAL PRIMARY KEY,
    at            BIGINT NOT NULL,
    source_wallet TEXT,
    coin          TEXT,
    is_buy        INTEGER,
    price         DOUBLE PRECISION,
    reason        TEXT NOT NULL,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_hl_rejects_at ON hl_rejects(at DESC);

CREATE TABLE IF NOT EXISTS shadow_trades (
    id            BIGSERIAL PRIMARY KEY,
    wallet        TEXT NOT NULL,
    drop_reason   TEXT,
    trade_id      TEXT UNIQUE,
    timestamp     BIGINT NOT NULL,
    condition_id  TEXT,
    slug          TEXT,
    side          TEXT,
    outcome_index INTEGER,
    price         DOUBLE PRECISION,
    size_usdc     DOUBLE PRECISION,
    observed_at   BIGINT DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
);
CREATE INDEX IF NOT EXISTS idx_shadow_wallet ON shadow_trades(wallet, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_observed ON shadow_trades(observed_at DESC);

CREATE TABLE IF NOT EXISTS dx_trades (
    id              BIGSERIAL PRIMARY KEY,
    source_wallet   TEXT NOT NULL,
    source_fill_id  TEXT UNIQUE,
    ticker          TEXT NOT NULL,
    is_buy          INTEGER NOT NULL,
    leverage        DOUBLE PRECISION DEFAULT 1.0,
    entry_at        BIGINT NOT NULL,
    exit_at         BIGINT,
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_price      DOUBLE PRECISION,
    peak_price      DOUBLE PRECISION,
    entry_size_usdc DOUBLE PRECISION NOT NULL,
    exit_size_usdc  DOUBLE PRECISION,
    pnl_usdc        DOUBLE PRECISION,
    liquidation_price DOUBLE PRECISION,
    status          TEXT NOT NULL,
    exit_reason     TEXT,
    dry_run         INTEGER DEFAULT 1,
    funding_paid    DOUBLE PRECISION DEFAULT 0,
    gas_paid        DOUBLE PRECISION DEFAULT 0,
    created_at      TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
);
CREATE INDEX IF NOT EXISTS idx_dx_trades_wallet ON dx_trades(source_wallet, entry_at DESC);
CREATE INDEX IF NOT EXISTS idx_dx_trades_status ON dx_trades(status);

CREATE TABLE IF NOT EXISTS dx_subscriptions (
    wallet      TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'active',
    sizing_mult DOUBLE PRECISION DEFAULT 1.0,
    started_at  TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
    stopped_at  TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS dx_rejects (
    id            BIGSERIAL PRIMARY KEY,
    at            BIGINT NOT NULL,
    source_wallet TEXT,
    ticker        TEXT,
    is_buy        INTEGER,
    price         DOUBLE PRECISION,
    reason        TEXT NOT NULL,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_dx_rejects_at ON dx_rejects(at DESC);
