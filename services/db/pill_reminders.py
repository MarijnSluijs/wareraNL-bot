"""DB methods for the pill buff reminder feature."""

from __future__ import annotations

import time

import aiosqlite


class PillRemindersMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ── Subscriptions ────────────────────────────────────────────────────────

    async def subscribe_pill_reminder(
        self, discord_user_id: str, in_game_user_id: str
    ) -> None:
        """Subscribe a user. Inserts or replaces; resets reminded and expires_at."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO pill_reminders
                (discord_user_id, in_game_user_id, expires_at, reminded)
            VALUES (?, ?, NULL, 0)
            """,
            (discord_user_id, in_game_user_id),
        )
        await self._conn.commit()

    async def remove_pill_reminder(self, discord_user_id: str) -> bool:
        """Unsubscribe a user. Returns True if a row was deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM pill_reminders WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def is_pill_subscriber(self, discord_user_id: str) -> bool:
        """Return True if the user is currently subscribed."""
        async with self._conn.execute(
            "SELECT 1 FROM pill_reminders WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_pill_subscriber(self, discord_user_id: str) -> dict | None:
        """Return the subscriber row, or None."""
        async with self._conn.execute(
            "SELECT discord_user_id, in_game_user_id, expires_at, reminded "
            "FROM pill_reminders WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "discord_user_id": row[0],
                "in_game_user_id": row[1],
                "expires_at": row[2],
                "reminded": row[3],
            }

    async def get_all_pill_subscribers(self) -> list[dict]:
        """Return all subscriber rows (used by the hourly scan)."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT discord_user_id, in_game_user_id, expires_at, reminded "
            "FROM pill_reminders"
        ) as cur:
            async for row in cur:
                rows.append({
                    "discord_user_id": row[0],
                    "in_game_user_id": row[1],
                    "expires_at": row[2],
                    "reminded": row[3],
                })
        return rows

    # ── Expiry tracking (written by the hourly scan) ──────────────────────────

    async def update_pill_expires_at(
        self,
        discord_user_id: str,
        expires_at: int | None,
        *,
        reset_reminded: bool = False,
    ) -> None:
        """Set (or clear) expires_at for a subscriber.

        Pass *reset_reminded=True* when a new pill buff is detected so the
        30-second DM task knows to fire again.
        """
        if reset_reminded:
            await self._conn.execute(
                "UPDATE pill_reminders SET expires_at = ?, reminded = 0 "
                "WHERE discord_user_id = ?",
                (expires_at, discord_user_id),
            )
        else:
            await self._conn.execute(
                "UPDATE pill_reminders SET expires_at = ? WHERE discord_user_id = ?",
                (expires_at, discord_user_id),
            )
        await self._conn.commit()

    # ── DM task helpers ───────────────────────────────────────────────────────

    async def get_due_pill_reminders(self) -> list[dict]:
        """Return subscribers whose pill buff expires within 10 minutes."""
        now = int(time.time())
        cutoff = now + 600  # 10 minutes from now
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT discord_user_id, expires_at FROM pill_reminders
            WHERE reminded = 0
              AND expires_at IS NOT NULL
              AND expires_at > ?
              AND expires_at <= ?
            """,
            (now, cutoff),
        ) as cur:
            async for row in cur:
                rows.append({"discord_user_id": row[0], "expires_at": row[1]})
        return rows

    async def mark_pill_reminded(self, discord_user_id: str) -> None:
        """Mark the DM as sent."""
        await self._conn.execute(
            "UPDATE pill_reminders SET reminded = 1 WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        await self._conn.commit()

