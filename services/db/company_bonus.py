"""DB methods for the bedrijven bonus check (company bonus monitor) feature."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class CompanyBonusMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ── Watchers ─────────────────────────────────────────────────────────────

    async def add_company_bonus_watcher(
        self,
        discord_user_id: str,
        discord_username: str,
        game_username: str,
        guild_id: str,
        added_at: str,
        game_user_id: Optional[str] = None,
    ) -> None:
        """Insert or replace a watcher subscription."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO company_bonus_watchers
                (discord_user_id, discord_username, game_username, game_user_id, guild_id, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (discord_user_id, discord_username, game_username, game_user_id, guild_id, added_at),
        )
        await self._conn.commit()

    async def remove_company_bonus_watcher(self, discord_user_id: str) -> bool:
        """Remove a watcher. Returns True if a row was deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM company_bonus_watchers WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_all_company_bonus_watchers(self) -> list[dict]:
        """Return all active watchers."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT discord_user_id, discord_username, game_username, game_user_id, guild_id, added_at "
            "FROM company_bonus_watchers ORDER BY added_at"
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "discord_user_id": row[0],
                        "discord_username": row[1],
                        "game_username": row[2],
                        "game_user_id": row[3],
                        "guild_id": row[4],
                        "added_at": row[5],
                    }
                )
        return rows

    async def update_company_bonus_watcher_game_id(
        self, discord_user_id: str, game_user_id: str
    ) -> None:
        """Cache the resolved in-game user ID for a watcher."""
        await self._conn.execute(
            "UPDATE company_bonus_watchers SET game_user_id = ? WHERE discord_user_id = ?",
            (game_user_id, discord_user_id),
        )
        await self._conn.commit()

    async def is_company_bonus_watcher(self, discord_user_id: str) -> bool:
        """Return True if the Discord user is already subscribed."""
        async with self._conn.execute(
            "SELECT 1 FROM company_bonus_watchers WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            return await cur.fetchone() is not None

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def get_company_bonus_alert(
        self, discord_user_id: str, company_id: str
    ) -> Optional[dict]:
        """Return the stored alert row for (user, company), or None."""
        async with self._conn.execute(
            "SELECT region_id, alerted_at FROM company_bonus_alerts "
            "WHERE discord_user_id = ? AND company_id = ?",
            (discord_user_id, company_id),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return {"region_id": row[0], "alerted_at": row[1]}

    async def set_company_bonus_alert(
        self,
        discord_user_id: str,
        company_id: str,
        region_id: str,
        alerted_at: str,
    ) -> None:
        """Record that we have pinged this user about this company."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO company_bonus_alerts
                (discord_user_id, company_id, region_id, alerted_at)
            VALUES (?, ?, ?, ?)
            """,
            (discord_user_id, company_id, region_id, alerted_at),
        )
        await self._conn.commit()

    async def delete_company_bonus_alert(
        self, discord_user_id: str, company_id: str
    ) -> None:
        """Remove the alert record so the user can be re-notified later."""
        await self._conn.execute(
            "DELETE FROM company_bonus_alerts WHERE discord_user_id = ? AND company_id = ?",
            (discord_user_id, company_id),
        )
        await self._conn.commit()

    async def delete_all_alerts_for_user(self, discord_user_id: str) -> None:
        """Remove all alert records when a user unsubscribes."""
        await self._conn.execute(
            "DELETE FROM company_bonus_alerts WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        await self._conn.commit()
