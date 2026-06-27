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
    updated_at TEXT,
    avatar_url TEXT
);

-- war_mu_roles: Discord role IDs created for Dutch-owned MUs in the war guild
--   Only used by the war_sync cog; safe to exist on the production bot (empty).
CREATE TABLE IF NOT EXISTS war_mu_roles (
    mu_id           TEXT NOT NULL,
    role_type       TEXT NOT NULL,  -- 'owner', 'commander', 'member'
    discord_role_id TEXT NOT NULL,
    guild_id        TEXT NOT NULL,
    mu_name         TEXT,
    PRIMARY KEY (mu_id, role_type, guild_id)
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
    updated_at           TEXT NOT NULL,
    avatar_url           TEXT
);
CREATE INDEX IF NOT EXISTS idx_citizen_levels_country ON citizen_levels(country_id);
CREATE INDEX IF NOT EXISTS idx_citizen_levels_mu_name ON citizen_levels(mu_name) WHERE mu_name IS NOT NULL;

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

-- pending_ticket_deletions: tracks approved ticket channels that should be
--   deleted after a delay.  Rows are inserted when /approve is called and
--   removed once the channel has been deleted.  On bot restart only channels
--   recorded here are rescheduled for deletion — open (unapproved) tickets are
--   left untouched.
CREATE TABLE IF NOT EXISTS pending_ticket_deletions (
    channel_id  TEXT PRIMARY KEY,
    guild_id    TEXT NOT NULL,
    approved_at TEXT NOT NULL,  -- ISO-8601 UTC timestamp of /approve call
    delete_at   TEXT NOT NULL   -- ISO-8601 UTC timestamp when deletion is due
);

-- ticket_log: historical record of every verification ticket opened, used
--   for /ticketstats reporting. Logging started when this table was
--   introduced — tickets created before that are not represented.
CREATE TABLE IF NOT EXISTS ticket_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    request_type    TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_log_guild_created ON ticket_log(guild_id, created_at);

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
CREATE INDEX IF NOT EXISTS idx_battle_hits_user         ON battle_hits(user_id);
CREATE INDEX IF NOT EXISTS idx_battle_hits_created      ON battle_hits(battle_created_at);
CREATE INDEX IF NOT EXISTS idx_battle_hits_battle       ON battle_hits(battle_id);
-- Covering index for list_available_weeks: allows week-per-user lookup without full row scan
CREATE INDEX IF NOT EXISTS idx_battle_hits_user_created ON battle_hits(user_id, battle_created_at);

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

-- ── Pill tracking ─────────────────────────────────────────────────────────────

-- citizen_pill_tracking: tracks the last known pill buff expiry per citizen.
--   Updated hourly during the NL citizen refresh by scanning getUserLite for all
--   NL players.  buff_expires_at is the Unix timestamp when the active/last buff
--   ended (or will end).  When buff_expires_at + 57600 (16h) > now, the player
--   is in debuff.  NULL means no pill has ever been observed.
CREATE TABLE IF NOT EXISTS citizen_pill_tracking (
    user_id         TEXT PRIMARY KEY,
    country_id      TEXT NOT NULL,
    buff_expires_at INTEGER,             -- Unix timestamp (seconds); NULL = never seen
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pill_tracking_country ON citizen_pill_tracking(country_id);

-- ── Level-5 notifications ─────────────────────────────────────────────────────

-- lvl5_tracker: hourly threshold tracker — records the last-seen level for every
--   NL citizen so we can detect the <5 → ≥5 crossing.  notified=1 means we
--   already posted this citizen in the admin channel; notified stays 1 forever.
CREATE TABLE IF NOT EXISTS lvl5_tracker (
    user_id          TEXT PRIMARY KEY,
    last_seen_level  INTEGER NOT NULL,
    notified         INTEGER NOT NULL DEFAULT 0,  -- 0 = not yet posted, 1 = already posted
    updated_at       TEXT NOT NULL
);

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

-- ── Company move advisor ─────────────────────────────────────────────────────

-- company_move_advice_watchers: Discord users who want to be notified when one of
--   their companies could profitably move to a region with a higher production bonus.
CREATE TABLE IF NOT EXISTS company_move_advice_watchers (
    discord_user_id  TEXT PRIMARY KEY,
    discord_username TEXT NOT NULL,
    game_username    TEXT NOT NULL,
    game_user_id     TEXT,               -- resolved in-game ID (cached)
    guild_id         TEXT NOT NULL,
    added_at         TEXT NOT NULL
);

-- company_move_advice_alerts: tracks which (user, company, source_region → target_region)
--   combinations have already been advised, so we don't spam.  Cleared when the company
--   moves to a new region or when the subscription is removed.
CREATE TABLE IF NOT EXISTS company_move_advice_alerts (
    discord_user_id    TEXT NOT NULL,
    company_id         TEXT NOT NULL,
    source_region_id   TEXT NOT NULL,   -- company's region when advice was given
    target_region_id   TEXT NOT NULL,   -- advised target region
    alerted_at         TEXT NOT NULL,
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

-- ── Pill buff reminders ───────────────────────────────────────────────────────

-- pill_reminders: one row per Discord user subscribed to pill buff reminders.
--   in_game_user_id links to their WarEra account.
--   expires_at is a Unix timestamp (seconds UTC) set by the hourly API scan;
--   NULL means no active pill buff detected yet.
--   reminded is set to 1 once the 10-minute DM has been sent.
CREATE TABLE IF NOT EXISTS pill_reminders (
    discord_user_id TEXT PRIMARY KEY,
    in_game_user_id TEXT NOT NULL DEFAULT '',
    expires_at      INTEGER,
    reminded        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pill_reminders_expires ON pill_reminders(expires_at);

-- ── Citizen Wealth ────────────────────────────────────────────────────────────

-- citizen_wealth: wealth per NL citizen (active + inactive company wealth)
--   wealth_active:             personal wallet + wealth of active companies (from userWealth ranking)
--   wealth_inactive_companies: sum of balance of disabled/inactive companies
--   wealth_total:              wealth_active + wealth_inactive_companies
CREATE TABLE IF NOT EXISTS citizen_wealth (
    user_id                   TEXT PRIMARY KEY,
    country_id                TEXT NOT NULL,
    citizen_name              TEXT,
    wealth_active             REAL NOT NULL DEFAULT 0,
    wealth_inactive_companies REAL NOT NULL DEFAULT 0,
    wealth_total              REAL NOT NULL DEFAULT 0,
    updated_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_citizen_wealth_country ON citizen_wealth(country_id);
CREATE INDEX IF NOT EXISTS idx_citizen_wealth_total   ON citizen_wealth(wealth_total DESC);

-- citizen_wealth_history: daily snapshots of each NL citizen's total wealth.
--   One row per (user_id, snapshot_date). Used to compute wealth increase
--   over a configurable number of days in the /wealth command.
CREATE TABLE IF NOT EXISTS citizen_wealth_history (
    user_id       TEXT NOT NULL,
    country_id    TEXT NOT NULL,
    citizen_name  TEXT,
    wealth_total  REAL NOT NULL DEFAULT 0,
    snapshot_date TEXT NOT NULL,  -- ISO date: YYYY-MM-DD (UTC)
    PRIMARY KEY (user_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_wealth_history_country_date
    ON citizen_wealth_history(country_id, snapshot_date);

-- ── Event gems ────────────────────────────────────────────────────────────────

-- event_gems: gem balance per Discord user, awarded via Discord events.
--   Gems are gifted in-game once a threshold is reached; this table tracks
--   the pending balance.
CREATE TABLE IF NOT EXISTS event_gems (
    discord_user_id  TEXT PRIMARY KEY,
    discord_username TEXT NOT NULL,
    guild_id         TEXT NOT NULL,
    gems             INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL
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

-- ── Item market trades ────────────────────────────────────────────────────────

-- item_trades: itemMarket transactions polled from /transaction.getPaginatedTransactions.
--   Used by /marktprijs to rank similar historical trades and estimate a price.
--   Skill columns are NULL when the source payload didn't include that stat.
CREATE TABLE IF NOT EXISTS item_trades (
    tx_id             TEXT PRIMARY KEY,            -- mongo _id from transaction
    created_at        TEXT NOT NULL,               -- ISO timestamp of sale
    offer_created_at  TEXT,                        -- when listing was posted
    item_code         TEXT NOT NULL,
    item_type         TEXT,                        -- e.g. "equipment"
    quantity          INTEGER NOT NULL DEFAULT 1,
    price             REAL NOT NULL,               -- total money paid (coins)
    state             INTEGER,                     -- current durability
    max_state         INTEGER,                     -- max durability
    attack            INTEGER,
    critical_chance   INTEGER,
    critical_damages  INTEGER,
    armor             INTEGER,
    precision_        INTEGER,                     -- 'precision' is SQL reserved
    dodge             INTEGER,
    buyer_id          TEXT,
    seller_id         TEXT,
    raw_json          TEXT NOT NULL,               -- full payload for audit
    ingested_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_item_trades_code_ts ON item_trades(item_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_trades_ts      ON item_trades(created_at DESC);

-- ── Daily damage accumulator ─────────────────────────────────────────────────

-- daily_dmg_hits: per-player damage per battle, populated hourly by daily_dmg_task.
--   Uses battleLootSummary.getByBattleAndUser for NL citizens. Scope is limited to
--   NL citizens; country/MU aggregation is derived by joining citizen_levels.
CREATE TABLE IF NOT EXISTS daily_dmg_hits (
    round_id     TEXT NOT NULL,
    battle_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    total_damage REAL NOT NULL DEFAULT 0,
    hits         INTEGER,
    cases        INTEGER,
    round_date   TEXT NOT NULL,   -- 'YYYY-MM-DD' derived from round ObjectId timestamp (UTC)
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (round_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_dmg_hits_date ON daily_dmg_hits(round_date);
CREATE INDEX IF NOT EXISTS idx_daily_dmg_hits_user ON daily_dmg_hits(user_id);

-- daily_dmg_processed: deduplication log for the hourly daily_dmg_task.
CREATE TABLE IF NOT EXISTS daily_dmg_processed (
    battle_id    TEXT PRIMARY KEY,
    battle_date  TEXT,
    processed_at TEXT NOT NULL
);

-- ── War guild status ─────────────────────────────────────────────────────────

-- war_status_choices: per-player war-readiness choice set via buttons in the war guild
CREATE TABLE IF NOT EXISTS war_status_choices (
    discord_user_id TEXT PRIMARY KEY,
    choice          TEXT NOT NULL,    -- 'ready' | 'eco'
    updated_at      TEXT NOT NULL
);

-- citizen_mu_membership: authoritative user→MU mapping from the war-guild MU scan.
-- Populated/replaced each scan cycle; used as fallback when citizen_levels.mu_name is null
-- (e.g. for MU owners who don't follow daily orders and thus have no mu_name in the API).
CREATE TABLE IF NOT EXISTS citizen_mu_membership (
    in_game_user_id TEXT PRIMARY KEY,
    mu_id           TEXT NOT NULL,
    mu_name         TEXT NOT NULL,
    role_type       TEXT NOT NULL,    -- 'owner' | 'commander' | 'member'
    updated_at      TEXT NOT NULL
);

-- ── Web data freshness ────────────────────────────────────────────────────────
-- Tracks when each named dataset was last refreshed by a task or manual trigger.
-- Used by the rijksoverheid-web website to display "Last updated" and to power
-- the manual refresh queue.
CREATE TABLE IF NOT EXISTS data_freshness (
    dataset           TEXT PRIMARY KEY,
    last_started_at   TEXT,
    last_finished_at  TEXT,
    last_status       TEXT,        -- 'ok' | 'error' | 'running'
    last_error        TEXT,
    source            TEXT,        -- 'task' | 'manual_web' | 'manual_discord'
    duration_ms       INTEGER
);

-- ── MU channel subscriptions ─────────────────────────────────────────────────

-- mu_weekly_report_subs: channels subscribed to automatic weekly MU damage reports
--   posted every Monday at 01:00 NL time (just before the 02:00 weekly-damage reset).
--   last_posted_at guards against double-posts on bot restart within the same hour.
CREATE TABLE IF NOT EXISTS mu_weekly_report_subs (
    channel_id     TEXT NOT NULL,
    guild_id       TEXT NOT NULL,
    mu_name        TEXT NOT NULL,
    mu_id          TEXT NOT NULL,
    last_posted_at TEXT,
    added_at       TEXT NOT NULL,
    PRIMARY KEY (channel_id, mu_name)
);

-- mu_auction_win_subs: channels that receive a ping when a specific MU wins an auction.
--   seen_ids is a JSON list of already-notified auction IDs (trimmed to last 500).
--   cutoff_at is the createdAt of the newest contract known at subscribe time;
--   only contracts with createdAt strictly after this value are posted.
CREATE TABLE IF NOT EXISTS mu_auction_win_subs (
    channel_id TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    mu_name    TEXT NOT NULL,
    mu_id      TEXT NOT NULL,
    seen_ids   TEXT NOT NULL DEFAULT '[]',
    initialized INTEGER NOT NULL DEFAULT 0,
    cutoff_at  TEXT,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (channel_id, mu_name)
);

-- ── Eco donations cache ───────────────────────────────────────────────────────
-- Hourly snapshot of NL donation transactions, populated by eco_donations_poller.
-- The /eco_donaties command reads from this table instead of calling the API live.
CREATE TABLE IF NOT EXISTS eco_donations (
    txn_id       TEXT PRIMARY KEY,   -- SHA1 of (user_id|created_at|amount)
    user_id      TEXT NOT NULL,
    citizen_name TEXT,               -- resolved from citizen_levels at insert time
    mu_name      TEXT,               -- resolved from citizen_levels at insert time
    amount       REAL NOT NULL,
    created_at   TEXT NOT NULL       -- ISO8601 UTC, e.g. 2026-05-01T12:34:56.000Z
);
CREATE INDEX IF NOT EXISTS idx_eco_donations_created_at ON eco_donations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eco_donations_mu_name    ON eco_donations(mu_name);

-- ── Online history log ────────────────────────────────────────────────────────
-- Every 12 hours, snapshot whether each citizen was seen online (lastConnectionAt
-- within the past 12 hours). Used to draw an online-activity chart on player pages.
CREATE TABLE IF NOT EXISTS citizen_online_log (
    user_id    TEXT NOT NULL,
    checked_at TEXT NOT NULL,  -- ISO8601 UTC timestamp of the snapshot
    was_online INTEGER NOT NULL DEFAULT 0,  -- 1 = online in last 12h, 0 = offline
    PRIMARY KEY (user_id, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_citizen_online_log_user ON citizen_online_log(user_id, checked_at DESC);

-- ── Country paraatheid (war-readiness) daily log ──────────────────────────────
-- Once per day, snapshot war/eco counts for every known country so we can draw
-- a "paraatheid over time" graph on the country detail page.
CREATE TABLE IF NOT EXISTS country_paraatheid_log (
    country_id    TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,  -- YYYY-MM-DD
    total_count   INTEGER NOT NULL DEFAULT 0,
    war_count     INTEGER NOT NULL DEFAULT 0,
    eco_count     INTEGER NOT NULL DEFAULT 0,
    war_pct       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (country_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_country_para_log ON country_paraatheid_log(country_id, snapshot_date DESC);

-- ── Citizen level history log ─────────────────────────────────────────────────
-- One row per level-up event (only inserted when the level changes from the
-- previous snapshot), so the table stays small and charts show step-wise growth.
CREATE TABLE IF NOT EXISTS citizen_level_log (
    user_id       TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,  -- YYYY-MM-DD of the day the new level was first seen
    level         INTEGER NOT NULL,
    PRIMARY KEY (user_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_citizen_level_log ON citizen_level_log(user_id, snapshot_date DESC);

-- ── Citizen name history ──────────────────────────────────────────────────────
-- One row per unique name a citizen has ever had (captured from today onwards).
-- inserted the first time we see the name, so first_seen_date is approximate.
CREATE TABLE IF NOT EXISTS citizen_name_history (
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    first_seen_date TEXT NOT NULL,  -- YYYY-MM-DD
    PRIMARY KEY (user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_citizen_name_history_user ON citizen_name_history(user_id);
CREATE INDEX IF NOT EXISTS idx_citizen_name_history_name ON citizen_name_history(name COLLATE NOCASE);
