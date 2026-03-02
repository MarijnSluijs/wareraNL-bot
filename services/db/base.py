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
        ]
        for table, column_def in migrations:
            try:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column_def}"
                )
                await self._conn.commit()
            except Exception:
                pass  # column already exists — ignore

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
