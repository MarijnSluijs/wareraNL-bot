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

-- ── MU Registry ──────────────────────────────────────────────────────────────

-- known_mus: registry of all MUs in the game, populated by mu.getManyPaginated
CREATE TABLE IF NOT EXISTS known_mus (
    mu_id      TEXT PRIMARY KEY,
    mu_name    TEXT NOT NULL,
    updated_at TEXT
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

-- identity_links: mapping between Discord identities and in-game identities
--   updated on verification approvals in welcome flow
CREATE TABLE IF NOT EXISTS identity_links (
    discord_user_id        TEXT PRIMARY KEY,
    guild_id               TEXT NOT NULL,
    in_game_user_id        TEXT NOT NULL,
    nationality            TEXT NOT NULL,
    request_type           TEXT NOT NULL,
    embassy_country        TEXT,
    approved_by_discord_id TEXT NOT NULL,
    approved_at            TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_links_ingame ON identity_links(in_game_user_id);

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
    rarity_json  TEXT,
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
    resistance_max    REAL DEFAULT 100.0,
    updated_at        TEXT
);


-- ── Giveaways ─────────────────────────────────────────────────────────────────

-- giveaways: rewards inventory for citizens
CREATE TABLE IF NOT EXISTS wallets (
    user_id TEXT PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- wallet_transactions: ledger of all wallet changes (positive and negative)
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    tx_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES wallets(user_id)
);

-- ── Weekly damage ────────────────────────────────────────────────────────────

-- citizen_weekly_damages: latest weekly battle damage per NL citizen
CREATE TABLE IF NOT EXISTS citizen_weekly_damages (
    user_id       TEXT PRIMARY KEY,
    citizen_name  TEXT,
    country_id    TEXT NOT NULL,
    weekly_damage REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekly_damages_country ON citizen_weekly_damages(country_id);

-- ── Global luck ────────────────────────────────────────────────────────────────

-- global_citizen_luck: case-opening luck scores across all countries
CREATE TABLE IF NOT EXISTS global_citizen_luck (
    user_id      TEXT PRIMARY KEY,
    country_id   TEXT NOT NULL,
    citizen_name TEXT,
    luck_score   REAL NOT NULL,
    opens_count  INTEGER NOT NULL,
    rarity_json  TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_luck_score ON global_citizen_luck(luck_score);
CREATE INDEX IF NOT EXISTS idx_global_luck_country ON global_citizen_luck(country_id);

-- ── Legacy (krypton template) ─────────────────────────────────────────────────

-- ── Battle rankings accumulator ──────────────────────────────────────────────

-- battle_hits: per-player damage per battle side, written by the daily poller
CREATE TABLE IF NOT EXISTS battle_hits (
    battle_id         TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    side              TEXT NOT NULL,      -- 'attacker' | 'defender'
    damage            REAL NOT NULL DEFAULT 0,
    rank              INTEGER,
    battle_created_at TEXT NOT NULL,      -- ISO-8601 UTC from battle.createdAt
    recorded_at       TEXT NOT NULL,
    PRIMARY KEY (battle_id, user_id, side)
);
CREATE INDEX IF NOT EXISTS idx_battle_hits_user    ON battle_hits(user_id);
CREATE INDEX IF NOT EXISTS idx_battle_hits_created ON battle_hits(battle_created_at);
CREATE INDEX IF NOT EXISTS idx_battle_hits_battle  ON battle_hits(battle_id);

-- processed_battles: deduplication tracker for the daily battle poller
CREATE TABLE IF NOT EXISTS processed_battles (
    battle_id         TEXT PRIMARY KEY,
    battle_created_at TEXT,
    attacker_damage   REAL,
    defender_damage   REAL,
    processed_at      TEXT NOT NULL
);

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

-- battle_drops: loot items dropped during battles (from battleRanking.getRanking)
CREATE TABLE IF NOT EXISTS battle_drops (
    id               TEXT PRIMARY KEY,   -- lootItem._id
    battle_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    side             TEXT NOT NULL,       -- 'attacker' | 'defender'
    rank             INTEGER,
    damage_dealt     REAL,
    item_code        TEXT,
    item_skills_json TEXT,
    dropped_at       TEXT,
    recorded_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_battle_drops_user    ON battle_drops(user_id);
CREATE INDEX IF NOT EXISTS idx_battle_drops_battle  ON battle_drops(battle_id);

-- battle_mu_hits: per-MU damage per battle side, fetched via type="mu" rankings
CREATE TABLE IF NOT EXISTS battle_mu_hits (
    battle_id         TEXT NOT NULL,
    mu_id             TEXT NOT NULL,
    side              TEXT NOT NULL,       -- 'attacker' | 'defender'
    mu_name           TEXT,
    damage            REAL NOT NULL DEFAULT 0,
    battle_created_at TEXT,
    recorded_at       TEXT,
    PRIMARY KEY (battle_id, mu_id, side)
);
CREATE INDEX IF NOT EXISTS idx_battle_mu_hits_mu      ON battle_mu_hits(mu_id);
CREATE INDEX IF NOT EXISTS idx_battle_mu_hits_created ON battle_mu_hits(battle_created_at);

-- battle_country_hits: per-country damage per battle side, fetched via type="country" rankings
--   Reflects player nationality (same as the game's own country leaderboard), NOT
--   which country was the attacker/defender side.
CREATE TABLE IF NOT EXISTS battle_country_hits (
    battle_id         TEXT NOT NULL,
    country_id        TEXT NOT NULL,
    side              TEXT NOT NULL,       -- 'attacker' | 'defender'
    damage            REAL NOT NULL DEFAULT 0,
    battle_created_at TEXT,
    recorded_at       TEXT,
    PRIMARY KEY (battle_id, country_id, side)
);
CREATE INDEX IF NOT EXISTS idx_battle_country_hits_country  ON battle_country_hits(country_id);
CREATE INDEX IF NOT EXISTS idx_battle_country_hits_created  ON battle_country_hits(battle_created_at);

-- ── Article tips ──────────────────────────────────────────────────────────────

-- article_tips: individual article tip transactions (outgoing from the tipper's perspective)
--   Populated by /peil artikelen which scans known citizens' transactions.
--   amount is always positive (abs(money)); tip_at comes from transaction.createdAt.
CREATE TABLE IF NOT EXISTS article_tips (
    user_id      TEXT NOT NULL,
    country_id   TEXT,
    citizen_name TEXT,
    amount       REAL NOT NULL,
    tip_at       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, tip_at)
);
CREATE INDEX IF NOT EXISTS idx_article_tips_date    ON article_tips(tip_at);
CREATE INDEX IF NOT EXISTS idx_article_tips_country ON article_tips(country_id);

-- article_tip_scans: tracks the last time a citizen was scanned for article tips.
--   Used for incremental scanning: citizens with no tips are skipped until
--   RESCAN_DAYS have passed since their last scan.
CREATE TABLE IF NOT EXISTS article_tip_scans (
    user_id        TEXT PRIMARY KEY,
    last_scanned_at TEXT NOT NULL
);

-- ── Bedrijven bonus check (company bonus monitor) ────────────────────────────────────

-- company_bonus_watchers: Discord users who want to be notified when one of their
--   companies has a 0% production bonus in its current region.
CREATE TABLE IF NOT EXISTS company_bonus_watchers (
    discord_user_id  TEXT PRIMARY KEY,
    discord_username TEXT NOT NULL,
    game_username    TEXT NOT NULL,
    game_user_id     TEXT,               -- resolved in-game ID (cached)
    guild_id         TEXT NOT NULL,
    added_at         TEXT NOT NULL
);

-- company_bonus_alerts: tracks which (user, company, region) combinations have
--   already been pinged, so we don't spam.  When the company moves to a new
--   region the old row is deleted, allowing a fresh alert if the new region
--   also has 0% bonus.
CREATE TABLE IF NOT EXISTS company_bonus_alerts (
    discord_user_id TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    region_id       TEXT NOT NULL,
    alerted_at      TEXT NOT NULL,
    PRIMARY KEY (discord_user_id, company_id)
);

-- discord_allies: manually maintained list of countries that are allied via Discord
--   (not necessarily reflected in the game API).  Used by the bounty poller to
--   suppress alerts for battles these countries are engaged in.
CREATE TABLE IF NOT EXISTS discord_allies (
    country_id   TEXT PRIMARY KEY,
    country_name TEXT,          -- optional display label (e.g. "België")
    added_by     TEXT NOT NULL,  -- Discord user ID who added the entry
    added_at     TEXT NOT NULL   -- ISO timestamp
);

-- player_tx_cache: cached transaction aggregates per player for /transacties.
--   On each command invocation only *new* transactions (createdAt > newest_tx_at)
--   are fetched, then merged into the stored aggregates.
CREATE TABLE IF NOT EXISTS player_tx_cache (
    user_id        TEXT PRIMARY KEY,
    username       TEXT NOT NULL,
    counts_json    TEXT NOT NULL DEFAULT '{}',   -- JSON dict type→count
    totals_json    TEXT NOT NULL DEFAULT '{}',   -- JSON dict type→total_cc
    breakdown_json TEXT NOT NULL DEFAULT '{}',   -- JSON dict type→subkey→[count,total]
    newest_tx_at   TEXT,                         -- ISO timestamp of newest cached tx
    total_tx       INTEGER NOT NULL DEFAULT 0,
    truncated      INTEGER NOT NULL DEFAULT 0,   -- 1 if original fetch was capped
    updated_at     TEXT NOT NULL
);
