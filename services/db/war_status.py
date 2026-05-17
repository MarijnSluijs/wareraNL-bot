"""DB methods for war-guild status choices (war_status_choices table)."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


class WarStatusMixin:
    """war_status_choices table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_war_status(self, discord_user_id: str, choice: str) -> None:
        """Record or update a player's war-readiness choice ('ready' | 'eco')."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO war_status_choices (discord_user_id, choice, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(discord_user_id) DO UPDATE SET "
            "choice = excluded.choice, updated_at = excluded.updated_at",
            (discord_user_id, choice, now),
        )
        await self._conn.commit()

    async def get_war_status(self, discord_user_id: str) -> str | None:
        """Return the current choice for a Discord user, or None if unset."""
        async with self._conn.execute(
            "SELECT choice FROM war_status_choices WHERE discord_user_id = ?",
            (discord_user_id,),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_war_status_by_mu(self) -> list[dict]:
        """Per-MU war-status counts, joined with identity_links + citizen_levels.

        Returns a list of dicts with keys: mu_name, choice, count.
        MU name falls back to '(geen MU)' when the player has no known MU.
        Players without a verified identity_link show up as '(onbekend)'.
        """
        sql = """
            SELECT
                COALESCE(cl.mu_name, '(geen MU)') AS mu_name,
                wsc.choice,
                COUNT(*) AS cnt
            FROM war_status_choices wsc
            LEFT JOIN identity_links il ON il.discord_user_id = wsc.discord_user_id
            LEFT JOIN citizen_levels cl ON cl.user_id = il.in_game_user_id
            GROUP BY mu_name, wsc.choice
            ORDER BY mu_name, wsc.choice
        """
        rows: list[dict] = []
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                rows.append({"mu_name": row[0], "choice": row[1], "count": row[2]})
        return rows

    async def get_all_war_statuses(self) -> list[dict]:
        """Return all war status entries ordered by most recently updated."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT discord_user_id, choice, updated_at "
            "FROM war_status_choices ORDER BY updated_at DESC"
        ) as cur:
            async for row in cur:
                rows.append(
                    {"discord_user_id": row[0], "choice": row[1], "updated_at": row[2]}
                )
        return rows
