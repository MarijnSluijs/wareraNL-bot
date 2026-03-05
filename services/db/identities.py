"""Discord ↔ in-game identity mapping DB methods (identity_links table)."""
from __future__ import annotations

from typing import Optional

import aiosqlite


class IdentityLinksMixin:
    """identity_links table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_identity_link(
        self,
        discord_user_id: str,
        guild_id: str,
        in_game_user_id: str,
        nationality: str,
        request_type: str,
        approved_by_discord_id: str,
        approved_at: str,
        embassy_country: Optional[str] = None,
    ) -> None:
        """Insert or update a Discord/in-game identity mapping."""
        await self._conn.execute(
            "INSERT INTO identity_links ("
            "discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
            "embassy_country, approved_by_discord_id, approved_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(discord_user_id) DO UPDATE SET "
            "guild_id=excluded.guild_id, "
            "in_game_user_id=excluded.in_game_user_id, "
            "nationality=excluded.nationality, "
            "request_type=excluded.request_type, "
            "embassy_country=excluded.embassy_country, "
            "approved_by_discord_id=excluded.approved_by_discord_id, "
            "approved_at=excluded.approved_at, "
            "updated_at=excluded.updated_at",
            (
                discord_user_id,
                guild_id,
                in_game_user_id,
                nationality,
                request_type,
                embassy_country,
                approved_by_discord_id,
                approved_at,
                approved_at,
            ),
        )
        await self._conn.commit()

    async def get_identity_link_by_discord(
        self, discord_user_id: str, guild_id: Optional[str] = None
    ) -> Optional[dict]:
        """Return one identity mapping for a Discord user (optionally scoped to guild)."""
        if guild_id:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE discord_user_id = ? AND guild_id = ?"
            )
            params = (discord_user_id, guild_id)
        else:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE discord_user_id = ?"
            )
            params = (discord_user_id,)

        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "discord_user_id": row[0],
            "guild_id": row[1],
            "in_game_user_id": row[2],
            "nationality": row[3],
            "request_type": row[4],
            "embassy_country": row[5],
            "approved_by_discord_id": row[6],
            "approved_at": row[7],
            "updated_at": row[8],
        }

    async def get_identity_links_by_ingame(
        self, in_game_user_id: str, guild_id: Optional[str] = None
    ) -> list[dict]:
        """Return all Discord mappings for an in-game ID (optionally scoped to guild)."""
        if guild_id:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE in_game_user_id = ? AND guild_id = ? "
                "ORDER BY updated_at DESC"
            )
            params = (in_game_user_id, guild_id)
        else:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE in_game_user_id = ? "
                "ORDER BY updated_at DESC"
            )
            params = (in_game_user_id,)

        results: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results

    async def count_identity_links(
        self, guild_id: Optional[str] = None, nationality: Optional[str] = None
    ) -> int:
        """Count identity mappings with optional guild/nationality filters."""
        clauses: list[str] = []
        params: list[str] = []
        if guild_id:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if nationality:
            clauses.append("LOWER(nationality) = LOWER(?)")
            params.append(nationality)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT COUNT(*) FROM identity_links{where}"
        async with self._conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def identity_counts_by_nationality(
        self, guild_id: Optional[str] = None
    ) -> list[tuple[str, int]]:
        """Return counts grouped by nationality."""
        if guild_id:
            sql = (
                "SELECT nationality, COUNT(*) FROM identity_links "
                "WHERE guild_id = ? GROUP BY nationality ORDER BY COUNT(*) DESC"
            )
            params = (guild_id,)
        else:
            sql = (
                "SELECT nationality, COUNT(*) FROM identity_links "
                "GROUP BY nationality ORDER BY COUNT(*) DESC"
            )
            params = ()

        rows: list[tuple[str, int]] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((str(row[0] or "unknown"), int(row[1] or 0)))
        return rows

    async def count_identity_ingame_conflicts(
        self, guild_id: Optional[str] = None
    ) -> int:
        """Count in-game IDs linked to more than one Discord user."""
        if guild_id:
            sql = (
                "SELECT COUNT(*) FROM ("
                "SELECT in_game_user_id FROM identity_links "
                "WHERE guild_id = ? GROUP BY in_game_user_id HAVING COUNT(*) > 1"
                ")"
            )
            params = (guild_id,)
        else:
            sql = (
                "SELECT COUNT(*) FROM ("
                "SELECT in_game_user_id FROM identity_links "
                "GROUP BY in_game_user_id HAVING COUNT(*) > 1"
                ")"
            )
            params = ()

        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0] if row else 0)

    async def get_recent_identity_links(
        self, guild_id: Optional[str] = None, limit: int = 10
    ) -> list[dict]:
        """Return recently updated identity mappings."""
        lim = max(1, min(int(limit), 50))
        if guild_id:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links WHERE guild_id = ? ORDER BY updated_at DESC LIMIT ?"
            )
            params = (guild_id, lim)
        else:
            sql = (
                "SELECT discord_user_id, guild_id, in_game_user_id, nationality, request_type, "
                "embassy_country, approved_by_discord_id, approved_at, updated_at "
                "FROM identity_links ORDER BY updated_at DESC LIMIT ?"
            )
            params = (lim,)

        results: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                results.append(
                    {
                        "discord_user_id": row[0],
                        "guild_id": row[1],
                        "in_game_user_id": row[2],
                        "nationality": row[3],
                        "request_type": row[4],
                        "embassy_country": row[5],
                        "approved_by_discord_id": row[6],
                        "approved_at": row[7],
                        "updated_at": row[8],
                    }
                )
        return results
