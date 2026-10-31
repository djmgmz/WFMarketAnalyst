-- WFMarketAnalyst SQLite schema. Every statement is idempotent so it can run on each start.

PRAGMA foreign_keys = ON;

-- Key/value store: collection versions from /v2/versions, sync timestamps.
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Catalog (reference data, refreshed when /v2/versions reports a change)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS items (
    id                TEXT PRIMARY KEY,
    slug              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    category          TEXT NOT NULL,            -- derived from tags, see catalog.categorize()
    game_ref          TEXT,
    tags              TEXT NOT NULL DEFAULT '[]', -- JSON array
    rarity            TEXT,
    max_rank          INTEGER,
    max_charges       INTEGER,
    max_amber_stars   INTEGER,
    max_cyan_stars    INTEGER,
    base_endo         INTEGER,
    endo_multiplier   REAL,
    ducats            INTEGER,
    vosfor            INTEGER,
    req_mastery_rank  INTEGER,
    trading_tax       INTEGER,
    vaulted           INTEGER,                  -- 0/1, NULL when not applicable
    tradable          INTEGER,
    bulk_tradable     INTEGER,
    subtypes          TEXT,                     -- JSON array
    set_root          INTEGER,
    set_parts         TEXT,                     -- JSON array of item ids
    quantity_in_set   INTEGER,
    description       TEXT,
    wiki_link         TEXT,
    icon              TEXT,
    thumb             TEXT,
    sub_icon          TEXT,
    list_synced_at    TEXT NOT NULL,
    detail_synced_at  TEXT                      -- NULL until /v2/item/{slug} has been fetched
);

CREATE INDEX IF NOT EXISTS idx_items_category ON items (category);
CREATE INDEX IF NOT EXISTS idx_items_name ON items (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS item_tags (
    item_id TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (item_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags (tag);

-- Riven weapons/attributes, lich and sister weapons/ephemeras/quirks, missions, npcs, locations.
CREATE TABLE IF NOT EXISTS aux_catalog (
    kind      TEXT NOT NULL,  -- e.g. riven_weapon, lich_ephemera, mission
    id        TEXT NOT NULL,
    slug      TEXT,
    name      TEXT,
    data      TEXT NOT NULL,  -- raw JSON from the API
    synced_at TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);

-- ---------------------------------------------------------------------------
-- Recorded market data (filled by the recorders, empty until you start them)
-- ---------------------------------------------------------------------------

-- Every new order seen on the WebSocket feed (or from /v2/orders/recent).
CREATE TABLE IF NOT EXISTS orders_seen (
    id               TEXT PRIMARY KEY,
    item_id          TEXT NOT NULL,
    type             TEXT NOT NULL CHECK (type IN ('buy', 'sell')),
    platinum         INTEGER NOT NULL,
    quantity         INTEGER NOT NULL,
    per_trade        INTEGER,
    rank             INTEGER,
    subtype          TEXT,
    charges          INTEGER,
    amber_stars      INTEGER,
    cyan_stars       INTEGER,
    platform         TEXT,
    crossplay        INTEGER,
    user_id          TEXT,
    user_name        TEXT,
    user_reputation  INTEGER,
    user_status      TEXT,
    created_at       TEXT NOT NULL,
    seen_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_item_time ON orders_seen (item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_time ON orders_seen (created_at);

-- Price statistics from the legacy v1 endpoint /v1/items/{slug}/statistics.
--   source:      closed = completed trades, live = order-book snapshots
--   granularity: hour (last 48h) or day (last 90 days)
--   mod_rank:    -1 when the item has no ranks
--   order_type:  '' for closed, 'buy'/'sell' for live
CREATE TABLE IF NOT EXISTS price_stats (
    item_id      TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('closed', 'live')),
    granularity  TEXT NOT NULL CHECK (granularity IN ('hour', 'day')),
    bucket       TEXT NOT NULL,  -- ISO-8601 UTC start of the period
    mod_rank     INTEGER NOT NULL DEFAULT -1,
    subtype      TEXT NOT NULL DEFAULT '',
    order_type   TEXT NOT NULL DEFAULT '',
    volume       INTEGER,
    min_price    REAL,
    max_price    REAL,
    open_price   REAL,
    closed_price REAL,
    avg_price    REAL,
    wa_price     REAL,
    median       REAL,
    moving_avg   REAL,
    donch_top    REAL,
    donch_bot    REAL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (item_id, source, granularity, bucket, mod_rank, subtype, order_type)
);

CREATE INDEX IF NOT EXISTS idx_price_stats_lookup
    ON price_stats (item_id, source, granularity, mod_rank, bucket);

-- Latest daily median trade price of every item, at its default rank (rank 0, or
-- unranked = -1); see prices.default_rank. Dropped and recreated on every start
-- so a changed definition always takes effect.
DROP VIEW IF EXISTS item_prices;
CREATE VIEW item_prices AS
SELECT i.id AS item_id,
       (SELECT ps.median FROM price_stats ps
         WHERE ps.item_id = i.id AND ps.source = 'closed' AND ps.granularity = 'day'
           AND ps.mod_rank = CASE WHEN i.max_rank IS NULL THEN -1 ELSE 0 END
         ORDER BY ps.bucket DESC LIMIT 1) AS price
FROM items i;

-- Periodic best-price snapshots from /v2/orders/item/{slug}/top for watchlist items.
CREATE TABLE IF NOT EXISTS top_snapshots (
    item_id    TEXT NOT NULL,
    taken_at   TEXT NOT NULL,
    rank       INTEGER NOT NULL DEFAULT -1,
    best_sell  INTEGER,
    best_buy   INTEGER,
    sell_count INTEGER NOT NULL,
    buy_count  INTEGER NOT NULL,
    PRIMARY KEY (item_id, taken_at, rank)
);

-- Status of the long-running CLI jobs (latest run of each), shown in the dashboard.
--   status: running | finished | stopped | failed
--   heartbeat_at: refreshed every few seconds while the process is alive, so the
--                 API can tell a crashed job ("running" but silent) from a live one.
--   stop_requested: the job polls it with each heartbeat and stops itself cleanly.
CREATE TABLE IF NOT EXISTS jobs (
    name         TEXT PRIMARY KEY,            -- backfill | sync-catalog | record
    status       TEXT NOT NULL,
    total        INTEGER,                     -- NULL for open-ended jobs (record)
    done         INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    current      TEXT,                        -- item being processed
    detail       TEXT NOT NULL DEFAULT '{}',  -- JSON with job-specific counters
    message      TEXT,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at  TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0 -- set by the dashboard's Stop button
);

-- ---------------------------------------------------------------------------
-- Forecasts (written by the forecast module's training job)
-- ---------------------------------------------------------------------------

-- Latest forecast per item, rank and horizon (days ahead). method says whether
-- the model or the naive baseline produced it (see forecast.py).
CREATE TABLE IF NOT EXISTS forecasts (
    item_id    TEXT NOT NULL,
    rank       INTEGER NOT NULL DEFAULT -1,   -- -1: the item has no ranks
    horizon    INTEGER NOT NULL,              -- days ahead: 1 or 7
    made_at    TEXT NOT NULL,
    base_date  TEXT NOT NULL,                 -- the last day with data; the forecast is for base_date + horizon
    base_price REAL NOT NULL,
    predicted  REAL NOT NULL,
    low        REAL NOT NULL,                 -- 80% range
    high       REAL NOT NULL,
    method     TEXT NOT NULL CHECK (method IN ('model', 'baseline')),
    PRIMARY KEY (item_id, rank, horizon)
);

-- One row per training run: the evaluation report as JSON.
CREATE TABLE IF NOT EXISTS forecast_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at TEXT NOT NULL,
    report  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- User data
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS watchlist (
    item_id      TEXT PRIMARY KEY REFERENCES items (id) ON DELETE CASCADE,
    target_price INTEGER,
    added_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
