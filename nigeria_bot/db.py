"""Minimal SQLite persistence for the Nigeria verification bot."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

DB_PATH = "database/nigeria.db"


async def open_db(path: str = DB_PATH) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS identity_links (
            discord_user_id TEXT PRIMARY KEY,
            warera_user_id  TEXT NOT NULL,
            saved_at        TEXT NOT NULL
        )
    """)
    await conn.commit()
    return conn


async def save_link(
    conn: aiosqlite.Connection, discord_id: str, warera_id: str
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT OR REPLACE INTO identity_links (discord_user_id, warera_user_id, saved_at)
        VALUES (?, ?, ?)
        """,
        (discord_id, warera_id, now),
    )
    await conn.commit()


async def get_all_links(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Return all (discord_user_id, warera_user_id) pairs."""
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        "SELECT discord_user_id, warera_user_id FROM identity_links"
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1]))
    return rows
