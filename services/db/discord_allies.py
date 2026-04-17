"""DiscordAlliesMixin — manually maintained list of Discord-only allied countries."""

from __future__ import annotations

from datetime import datetime, timezone


class DiscordAlliesMixin:
    """Mixin for the ``discord_allies`` table."""

    async def get_discord_allies(self) -> list[str]:
        """Return all stored discord-ally country IDs."""
        async with self._conn.execute(
            "SELECT country_id FROM discord_allies ORDER BY added_at"
        ) as cur:
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def get_discord_allies_full(self) -> list[dict]:
        """Return all stored allies as dicts with country_id, country_name, added_by, added_at."""
        async with self._conn.execute(
            "SELECT country_id, country_name, added_by, added_at FROM discord_allies ORDER BY added_at"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"country_id": r[0], "country_name": r[1], "added_by": r[2], "added_at": r[3]}
            for r in rows
        ]

    async def add_discord_ally(
        self, country_id: str, added_by: str, country_name: str | None = None
    ) -> None:
        """Insert or update a discord-ally entry."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO discord_allies (country_id, country_name, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(country_id) DO UPDATE SET
                country_name = COALESCE(excluded.country_name, discord_allies.country_name),
                added_by     = excluded.added_by,
                added_at     = excluded.added_at
            """,
            (country_id, country_name, added_by, now),
        )
        await self._conn.commit()

    async def remove_discord_ally(self, country_id: str) -> bool:
        """Remove a discord-ally entry.  Returns True if a row was deleted."""
        cur = await self._conn.execute(
            "DELETE FROM discord_allies WHERE country_id = ?",
            (country_id,),
        )
        await self._conn.commit()
        return cur.rowcount > 0
