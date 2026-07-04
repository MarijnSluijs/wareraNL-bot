"""Database connection management and schema initialization."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("services.db")


class DatabaseBase:
    """Connection management and schema initialization.

    Subclasses (via :class:`~services.db.Database`) share ``self._conn``.
    """

    def __init__(self, path: str = "database/external.db") -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def setup(self) -> None:
        """Open the SQLite connection, create all tables, and apply migrations."""
        self._conn = await aiosqlite.connect(self.path)

        # Enable WAL mode — allows concurrent readers + one writer without
        # blocking each other, which eliminates most "database is locked" errors
        # when the data-fetcher and discord-bot write simultaneously.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait up to 30 s when the DB is locked before raising an error
        # (covers write-write contention between this connection, the other
        # Database() instances opened by mu/geluk/users/welcome/articles cogs,
        # and the standalone data-fetcher process — all of which share one
        # SQLite file and can briefly hold the single writer lock during a
        # citizen_refresh sweep or schema/migration run).
        await self._conn.execute("PRAGMA busy_timeout=30000")      # 30 s
        # Performance tuning: map the whole DB into the OS page cache (avoids
        # the internal 8 MB page cache round-trip for reads).  2 GB limit is
        # enough for the current 2.3 GB db; the OS maps only what it needs.
        await self._conn.execute("PRAGMA mmap_size=2147483648")   # 2 GB
        # Keep a 64 MB in-process page cache as well (fallback / write buffer).
        await self._conn.execute("PRAGMA cache_size=-65536")       # 64 MB
        # Checkpoint the WAL every 200 pages instead of 1000 — keeps the WAL
        # small (currently 760 MB) so reads don't need to scan a huge WAL file.
        await self._conn.execute("PRAGMA wal_autocheckpoint=200")

        # Run main schema (all CREATE TABLE IF NOT EXISTS)
        schema_path = Path("database/schema.sql")
        with schema_path.open("r", encoding="utf-8") as f:
            await self._conn.executescript(f.read())
        await self._conn.commit()

        # Apply incremental column additions (safe no-ops if already present)
        await self._apply_migrations()

        logger.info("Database initialized at %s", self.path)

    async def _apply_migrations(self) -> None:
        """Add columns that were introduced after the initial schema."""
        migrations: list[tuple[str, str]] = [
            # specialization_top — bonus breakdown columns
            ("specialization_top", "strategic_bonus REAL"),
            ("specialization_top", "ethic_bonus REAL"),
            ("specialization_top", "ethic_deposit_bonus REAL"),
            # deposit_top — breakdown columns
            ("deposit_top", "region_name TEXT"),
            ("deposit_top", "deposit_bonus REAL"),
            ("deposit_top", "ethic_deposit_bonus REAL"),
            # citizen_levels — extended fields
            ("citizen_levels", "skill_mode TEXT"),
            ("citizen_levels", "last_skills_reset_at TEXT"),
            ("citizen_levels", "citizen_name TEXT"),
            ("citizen_levels", "last_login_at TEXT"),
            ("citizen_levels", "mu_id TEXT"),
            ("citizen_levels", "mu_name TEXT"),
            # processed_battles — country identifiers added post-launch
            ("processed_battles", "attacker_country_id TEXT"),
            ("processed_battles", "defender_country_id TEXT"),
            # known_mus — home country added post-launch
            ("known_mus", "country_id TEXT"),
            # citizen_luck — elite case columns added post-launch
            ("citizen_luck", "elite_luck_score REAL"),
            ("citizen_luck", "elite_opens_count INTEGER"),
            ("citizen_luck", "elite_rarity_json TEXT"),
            # global_citizen_luck — elite case columns added post-launch
            ("global_citizen_luck", "elite_luck_score REAL"),
            ("global_citizen_luck", "elite_opens_count INTEGER"),
            ("global_citizen_luck", "elite_rarity_json TEXT"),
            # company_bonus_watchers — game_user_id cached at runtime
            ("company_bonus_watchers", "game_user_id TEXT"),
            # discord_allies — display label added post-launch
            ("discord_allies", "country_name TEXT"),
            # pill_reminders — in_game_user_id added post-launch; expires_at made nullable
            ("pill_reminders", "in_game_user_id TEXT NOT NULL DEFAULT ''"),
            # mu_auction_win_subs — initialized flag added to prevent posting old auctions
            ("mu_auction_win_subs", "initialized INTEGER NOT NULL DEFAULT 0"),
            # mu_auction_win_subs — cutoff_at: createdAt of newest contract at subscribe time
            ("mu_auction_win_subs", "cutoff_at TEXT"),
            # avatar URLs — added to citizen_levels and known_mus
            ("citizen_levels", "avatar_url TEXT"),
            ("known_mus", "avatar_url TEXT"),
            # specialization_top — last-notified record (only updated on notification, never by poll)
            ("specialization_top", "last_notified_country TEXT"),
            ("specialization_top", "last_notified_bonus REAL"),
        ]
        for table, column_def in migrations:
            try:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
                await self._conn.commit()
            except Exception:
                pass  # column already exists — ignore

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
