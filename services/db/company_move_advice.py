"""DB methods for the company move advisor feature."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class CompanyMoveAdviceMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ── Watchers ─────────────────────────────────────────────────────────────

    async def add_company_move_advice_watcher(
        self,
        discord_user_id: str,
        discord_username: str,
        game_username: str,
        guild_id: str,
        added_at: str,
        game_user_id: Optional[str] = None,
    ) -> None:
        """Insert or replace a move-advice subscription."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO company_move_advice_watchers
                (discord_user_id, discord_username, game_username, game_user_id, guild_id, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (discord_user_id, discord_username, game_username, game_user_id, guild_id, added_at),
        )
        await self._conn.commit()

    async def remove_company_move_advice_watcher(self, discord_user_id: str) -> bool:
        """Remove a move-advice subscription. Returns True if a row was deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM company_move_advice_watchers WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_all_company_move_advice_watchers(self) -> list[dict]:
        """Return all active move-advice subscriptions."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT discord_user_id, discord_username, game_username, game_user_id, guild_id, added_at "
            "FROM company_move_advice_watchers ORDER BY added_at"
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

    async def is_company_move_advice_watcher(self, discord_user_id: str) -> bool:
        """Return True if the Discord user is subscribed to move advice."""
        async with self._conn.execute(
            "SELECT 1 FROM company_move_advice_watchers WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            return await cur.fetchone() is not None

    async def update_company_move_advice_watcher_game_id(
        self, discord_user_id: str, game_user_id: str
    ) -> None:
        """Cache the resolved in-game user ID for a move-advice watcher."""
        await self._conn.execute(
            "UPDATE company_move_advice_watchers SET game_user_id = ? WHERE discord_user_id = ?",
            (game_user_id, discord_user_id),
        )
        await self._conn.commit()

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def get_company_move_advice_alert(
        self, discord_user_id: str, company_id: str
    ) -> Optional[dict]:
        """Return the stored alert row for (user, company), or None."""
        async with self._conn.execute(
            "SELECT source_region_id, target_region_id, alerted_at FROM company_move_advice_alerts "
            "WHERE discord_user_id = ? AND company_id = ?",
            (discord_user_id, company_id),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return {"source_region_id": row[0], "target_region_id": row[1], "alerted_at": row[2]}

    async def set_company_move_advice_alert(
        self,
        discord_user_id: str,
        company_id: str,
        source_region_id: str,
        target_region_id: str,
        alerted_at: str,
    ) -> None:
        """Record that we have sent a move-advice DM for this company."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO company_move_advice_alerts
                (discord_user_id, company_id, source_region_id, target_region_id, alerted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (discord_user_id, company_id, source_region_id, target_region_id, alerted_at),
        )
        await self._conn.commit()

    async def delete_company_move_advice_alert(
        self, discord_user_id: str, company_id: str
    ) -> None:
        """Remove the alert record so the user can be re-notified later."""
        await self._conn.execute(
            "DELETE FROM company_move_advice_alerts WHERE discord_user_id = ? AND company_id = ?",
            (discord_user_id, company_id),
        )
        await self._conn.commit()

    async def delete_all_move_advice_alerts_for_user(self, discord_user_id: str) -> None:
        """Remove all move-advice alert records when a user unsubscribes."""
        await self._conn.execute(
            "DELETE FROM company_move_advice_alerts WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        await self._conn.commit()
