"""Battle drops DB methods (battle_drops table)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class BattleDropsMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def get_battle_drops_count(self) -> int:
        """Return the total number of stored battle drop rows (diagnostic)."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM battle_drops"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_drops_by_user(
        self, user_id: str, limit: int = 50
    ) -> list[dict]:
        """Return recent loot drops for a user, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT id, battle_id, side, rank, damage_dealt,
                   item_code, item_skills_json, dropped_at
            FROM battle_drops
            WHERE user_id = ?
            ORDER BY dropped_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "id": row[0],
                        "battle_id": row[1],
                        "side": row[2],
                        "rank": row[3],
                        "damage_dealt": row[4],
                        "item_code": row[5],
                        "item_skills_json": row[6],
                        "dropped_at": row[7],
                    }
                )
        return rows

    async def get_top_drops_by_damage(
        self, limit: int = 10, days: Optional[int] = None
    ) -> list[dict]:
        """Return the highest-damage drops, optionally filtered to recent N days."""
        since_clause = (
            f"WHERE dropped_at >= datetime('now', '-{days} days')" if days else ""
        )
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT id, battle_id, user_id, side, rank, damage_dealt, item_code, dropped_at
            FROM battle_drops
            {since_clause}
            ORDER BY damage_dealt DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "id": row[0],
                        "battle_id": row[1],
                        "user_id": row[2],
                        "side": row[3],
                        "rank": row[4],
                        "damage_dealt": row[5] or 0.0,
                        "item_code": row[6],
                        "dropped_at": row[7],
                    }
                )
        return rows

    async def insert_battle_drop(
        self,
        item_id: str,
        battle_id: str,
        user_id: str,
        side: str,
        rank: Optional[int],
        damage_dealt: Optional[float],
        item_code: str,
        item_skills_json: Optional[str],
        dropped_at: Optional[str],
        recorded_at: str,
    ) -> bool:
        """Insert a single loot drop.  Returns True if newly inserted, False if already exists."""
        cur = await self._conn.execute(
            """
            INSERT OR IGNORE INTO battle_drops
                (id, battle_id, user_id, side, rank, damage_dealt,
                 item_code, item_skills_json, dropped_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, battle_id, user_id, side, rank, damage_dealt,
             item_code, item_skills_json, dropped_at, recorded_at),
        )
        return cur.rowcount > 0

    async def commit_battle_drops(self) -> None:
        """Commit pending battle drop inserts."""
        await self._conn.commit()
