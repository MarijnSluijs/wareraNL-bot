"""Minimal SQLite persistence for the Nigeria verification bot."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiosqlite

DB_PATH = "database/nigeria.db"


async def open_db(path: str = DB_PATH) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS identity_links (
            discord_user_id TEXT PRIMARY KEY,
            warera_user_id  TEXT NOT NULL,
            warera_username TEXT,
            saved_at        TEXT NOT NULL
        )
    """)
    # Migration: add warera_username column to existing tables
    try:
        await conn.execute(
            "ALTER TABLE identity_links ADD COLUMN warera_username TEXT"
        )
    except Exception:
        pass  # column already exists

    # Ticket deletions are scheduled with asyncio.sleep() in-process; this
    # table lets a restart mid-wait resume (or immediately run) any deletion
    # that was still pending, instead of silently losing it forever.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_ticket_deletions (
            channel_id TEXT PRIMARY KEY,
            delete_at  TEXT NOT NULL
        )
    """)
    await conn.commit()
    return conn


async def add_pending_ticket_deletion(
    conn: aiosqlite.Connection, channel_id: str, delete_at: str
) -> None:
    """Record a ticket channel that should be deleted at *delete_at* (ISO timestamp)."""
    await conn.execute(
        "INSERT OR REPLACE INTO pending_ticket_deletions (channel_id, delete_at) VALUES (?, ?)",
        (channel_id, delete_at),
    )
    await conn.commit()


async def remove_pending_ticket_deletion(conn: aiosqlite.Connection, channel_id: str) -> None:
    """Remove a pending deletion record (called once the channel is actually gone)."""
    await conn.execute(
        "DELETE FROM pending_ticket_deletions WHERE channel_id = ?", (channel_id,)
    )
    await conn.commit()


async def get_pending_ticket_deletions(
    conn: aiosqlite.Connection,
) -> list[tuple[str, str]]:
    """Return all (channel_id, delete_at) pairs still pending."""
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        "SELECT channel_id, delete_at FROM pending_ticket_deletions"
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1]))
    return rows


async def save_link(
    conn: aiosqlite.Connection,
    discord_id: str,
    warera_id: str,
    username: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT OR REPLACE INTO identity_links
            (discord_user_id, warera_user_id, warera_username, saved_at)
        VALUES (?, ?, ?, ?)
        """,
        (discord_id, warera_id, username, now),
    )
    await conn.commit()


async def get_link(
    conn: aiosqlite.Connection, discord_id: str
) -> Optional[tuple[str, Optional[str]]]:
    """Return (warera_user_id, warera_username) for the given Discord user, or None."""
    async with conn.execute(
        "SELECT warera_user_id, warera_username FROM identity_links WHERE discord_user_id = ?",
        (discord_id,),
    ) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1])


async def get_all_links(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """Return all (discord_user_id, warera_user_id) pairs."""
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        "SELECT discord_user_id, warera_user_id FROM identity_links"
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1]))
    return rows


async def get_all_links_full(
    conn: aiosqlite.Connection,
) -> list[tuple[str, str, Optional[str], str]]:
    """Return all (discord_user_id, warera_user_id, warera_username, saved_at) rows."""
    rows: list[tuple[str, str, Optional[str], str]] = []
    async with conn.execute(
        "SELECT discord_user_id, warera_user_id, warera_username, saved_at"
        " FROM identity_links ORDER BY saved_at DESC"
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1], row[2], row[3]))
    return rows
