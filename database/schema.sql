-- WarEra Discord Bot — SQLite schema
-- All tables are created here with IF NOT EXISTS guards.
-- Column-level migrations (ALTER TABLE) are applied at startup in services/db/base.py.

-- ── State & jobs ──────────────────────────────────────────────────────────────

-- poll_state: key/value store for background task timestamps and init flags
CREATE TABLE IF NOT EXISTS poll_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- jobs: background job tracking (progress, status)
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    status      TEXT,
    progress    INTEGER,
    result_path TEXT
);

-- ── Production ────────────────────────────────────────────────────────────────

-- country_snapshots: latest production snapshot per country (raw API JSON)
CREATE TABLE IF NOT EXISTS country_snapshots (
    country_id       TEXT PRIMARY KEY,
    code             TEXT,
    name             TEXT,
    specialized_item TEXT,
    production_bonus REAL,
    raw_json         TEXT,
    updated_at       TEXT
);

-- specialization_top: current best permanent bonus per specialization item
--   strategic_bonus + ethic_bonus + ethic_deposit_bonus = production_bonus
CREATE TABLE IF NOT EXISTS specialization_top (
    item                TEXT PRIMARY KEY,
    country_id          TEXT,
    country_name        TEXT,
    production_bonus    REAL,
    strategic_bonus     REAL,
    ethic_bonus         REAL,
    ethic_deposit_bonus REAL,
    updated_at          TEXT
);

-- country_item_ethic: ethics bonus per (item, country) pair seen in recommended lists
--   populated by the production poller from all recommended-region entries
CREATE TABLE IF NOT EXISTS country_item_ethic (
    item            TEXT NOT NULL,
    country_id      TEXT NOT NULL,
    strategic_bonus REAL,
    ethic_bonus     REAL,
    updated_at      TEXT,
    PRIMARY KEY (item, country_id)
);

-- deposit_top: current best deposit bonus region per specialization item
CREATE TABLE IF NOT EXISTS deposit_top (
    item                TEXT PRIMARY KEY,
    region_id           TEXT,
    region_name         TEXT,
    country_id          TEXT,
    country_name        TEXT,
    bonus               INTEGER,
    deposit_bonus       REAL,
    ethic_deposit_bonus REAL,
    permanent_bonus     REAL,
    deposit_end_at      TEXT,
    updated_at          TEXT
);

-- ── Citizens ──────────────────────────────────────────────────────────────────

-- citizen_levels: hourly snapshot of level, skill mode, and MU per citizen
CREATE TABLE IF NOT EXISTS citizen_levels (
    user_id              TEXT PRIMARY KEY,
    country_id           TEXT NOT NULL,
    level                INTEGER,
    skill_mode           TEXT,
    last_skills_reset_at TEXT,
    citizen_name         TEXT,
    last_login_at        TEXT,
    mu_id                TEXT,
    mu_name              TEXT,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citizen_levels_country ON citizen_levels(country_id);

-- ── Events ────────────────────────────────────────────────────────────────────

-- seen_articles: deduplication for posted articles
CREATE TABLE IF NOT EXISTS seen_articles (
    article_id TEXT PRIMARY KEY,
    seen_at    TEXT NOT NULL
);

-- seen_events: deduplication for posted game events
CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    seen_at  TEXT NOT NULL
);

-- war_events: historical archive of all posted war/battle events
CREATE TABLE IF NOT EXISTS war_events (
    event_id            TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,
    battle_id           TEXT,
    war_id              TEXT,
    attacker_country_id TEXT,
    defender_country_id TEXT,
    region_id           TEXT,
    region_name         TEXT,
    attacker_name       TEXT,
    defender_name       TEXT,
    created_at          TEXT,
    raw_json            TEXT
);

-- ── Luck ──────────────────────────────────────────────────────────────────────

-- citizen_luck: case-opening luck scores per citizen
--   luck_score: weighted z-score (0 = average, positive = luckier)
CREATE TABLE IF NOT EXISTS citizen_luck (
    user_id      TEXT PRIMARY KEY,
    country_id   TEXT NOT NULL,
    citizen_name TEXT,
    luck_score   REAL NOT NULL,
    opens_count  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citizen_luck_country ON citizen_luck(country_id);

-- ── Resistance ────────────────────────────────────────────────────────────────

-- resistance_state: current resistance bar for NL-occupied foreign regions
CREATE TABLE IF NOT EXISTS resistance_state (
    region_id         TEXT PRIMARY KEY,
    region_name       TEXT,
    occupying_country TEXT,
    resistance_value  REAL,
    updated_at        TEXT
);

-- ── Legacy (krypton template) ─────────────────────────────────────────────────

-- warns: moderation warn log used by database/__init__.py DatabaseManager
CREATE TABLE IF NOT EXISTS warns (
    id           INTEGER,
    user_id      TEXT    NOT NULL,
    server_id    TEXT    NOT NULL,
    moderator_id TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, user_id, server_id)
);
