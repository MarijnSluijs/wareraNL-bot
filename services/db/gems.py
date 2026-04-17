"""DB methods for the Discord event gems feature."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GemsMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def add_gems(
        self,
        discord_user_id: str,
        discord_username: str,
        guild_id: str,
        amount: int,
    ) -> int:
        """Add *amount* gems to a user. Creates the row if it does not exist.

        Returns the new gem total.
        """
        await self._conn.execute(
            """
            INSERT INTO event_gems (discord_user_id, discord_username, guild_id, gems, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                gems             = gems + excluded.gems,
                discord_username = excluded.discord_username,
                updated_at       = excluded.updated_at
            """,
            (discord_user_id, discord_username, guild_id, amount, _now()),
        )
        await self._conn.commit()
        return await self._get_gems(discord_user_id)

    async def remove_gems(
        self,
        discord_user_id: str,
        discord_username: str,
        guild_id: str,
        amount: int,
    ) -> int:
        """Subtract *amount* gems from a user (floor at 0). Creates the row if needed.

        Returns the new gem total.
        """
        await self._conn.execute(
            """
            INSERT INTO event_gems (discord_user_id, discord_username, guild_id, gems, updated_at)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                gems             = MAX(0, gems - ?),
                discord_username = excluded.discord_username,
                updated_at       = excluded.updated_at
            """,
            (discord_user_id, discord_username, guild_id, _now(), amount),
        )
        await self._conn.commit()
        return await self._get_gems(discord_user_id)

    async def _get_gems(self, discord_user_id: str) -> int:
        """Return the current gem balance for a user (0 if no row)."""
        async with self._conn.execute(
            "SELECT gems FROM event_gems WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_all_gem_balances(self) -> list[dict]:
        """Return all rows with gems > 0, sorted descending by gems."""
        async with self._conn.execute(
            """
            SELECT discord_user_id, discord_username, gems
            FROM event_gems
            WHERE gems > 0
            ORDER BY gems DESC
            """,
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"discord_user_id": r[0], "discord_username": r[1], "gems": r[2]}
                for r in rows
            ]
