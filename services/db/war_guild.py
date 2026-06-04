"""DB methods for war-guild MU role tracking (war_mu_roles table)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class WarGuildMixin:
    """war_mu_roles table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def get_war_mu_role(
        self, mu_id: str, role_type: str, guild_id: str
    ) -> Optional[int]:
        """Return the Discord role ID for a (mu_id, role_type, guild_id) triple."""
        async with self._conn.execute(
            "SELECT discord_role_id FROM war_mu_roles "
            "WHERE mu_id = ? AND role_type = ? AND guild_id = ?",
            (mu_id, role_type, guild_id),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def upsert_war_mu_role(
        self,
        mu_id: str,
        role_type: str,
        guild_id: str,
        discord_role_id: int,
        mu_name: str,
    ) -> None:
        """Insert or update a war-guild MU role entry."""
        await self._conn.execute(
            "INSERT INTO war_mu_roles (mu_id, role_type, discord_role_id, guild_id, mu_name) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mu_id, role_type, guild_id) DO UPDATE SET "
            "discord_role_id = excluded.discord_role_id, mu_name = excluded.mu_name",
            (mu_id, role_type, guild_id, discord_role_id, mu_name),
        )
        await self._conn.commit()

    async def get_all_war_mu_roles(self, guild_id: str) -> list[dict]:
        """Return all war_mu_roles rows for a guild."""
        results: list[dict] = []
        async with self._conn.execute(
            "SELECT mu_id, role_type, discord_role_id, mu_name FROM war_mu_roles WHERE guild_id = ?",
            (guild_id,),
        ) as cur:
            async for row in cur:
                results.append(
                    {
                        "mu_id": row[0],
                        "role_type": row[1],
                        "discord_role_id": int(row[2]),
                        "mu_name": row[3],
                    }
                )
        return results

    async def delete_war_mu_roles(self, mu_id: str, guild_id: str) -> None:
        """Remove all role entries for a given MU in a guild."""
        await self._conn.execute(
            "DELETE FROM war_mu_roles WHERE mu_id = ? AND guild_id = ?",
            (mu_id, guild_id),
        )
        await self._conn.commit()

    # ── citizen_mu_membership ─────────────────────────────────────────────────

    async def get_mu_memberships_for_citizen(
        self, in_game_user_id: str
    ) -> list[tuple[str, str, str]]:
        """Return [(mu_id, mu_name, role_type)] for a given in-game user."""
        async with self._conn.execute(
            "SELECT mu_id, mu_name, role_type FROM citizen_mu_membership "
            "WHERE in_game_user_id = ?",
            (in_game_user_id,),
        ) as cursor:
            return [(row[0], row[1], row[2]) async for row in cursor]

    async def get_all_mu_memberships(self) -> list[tuple[str, str, str, str]]:
        """Return [(in_game_user_id, mu_id, mu_name, role_type)] for all rows."""
        async with self._conn.execute(
            "SELECT in_game_user_id, mu_id, mu_name, role_type FROM citizen_mu_membership"
        ) as cursor:
            return [(row[0], row[1], row[2], row[3]) async for row in cursor]

    async def replace_citizen_mu_memberships(
        self,
        user_mu_roles: dict,   # {in_game_user_id: {mu_id: role_type}}
        mu_name_map: dict,     # {mu_id: mu_name}
    ) -> None:
        """Atomically replace all citizen_mu_membership rows from MU scan data."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("DELETE FROM citizen_mu_membership")
        for uid, mu_map in user_mu_roles.items():
            for mu_id, role_type in mu_map.items():
                mu_name = mu_name_map.get(mu_id, "")
                if not mu_name:
                    continue
                await self._conn.execute(
                    "INSERT OR REPLACE INTO citizen_mu_membership "
                    "(in_game_user_id, mu_id, mu_name, role_type, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (uid, mu_id, mu_name, role_type, now),
                )
        await self._conn.commit()
