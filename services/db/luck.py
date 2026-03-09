"""Luck score DB methods (citizen_luck table)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class LuckMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """citizen_luck table operations."""

    async def upsert_luck_score(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        luck_score: float,
        opens_count: int,
        rarity_json: Optional[str],
        updated_at: str,
    ) -> None:
        """Insert or replace a luck score (call flush_luck_scores to commit batch)."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO citizen_luck
                (user_id, country_id, citizen_name, luck_score, opens_count, rarity_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                country_id,
                citizen_name,
                luck_score,
                opens_count,
                rarity_json,
                updated_at,
            ),
        )

    async def flush_luck_scores(self) -> None:
        """Commit pending luck score upserts."""
        await self._conn.commit()

    async def delete_luck_scores_for_country(self, country_id: str) -> None:
        """Delete all luck score records for a specific country."""
        await self._conn.execute(
            "DELETE FROM citizen_luck WHERE country_id = ?", (country_id,)
        )
        await self._conn.commit()

    async def get_luck_ranking(self, country_id: str) -> list[dict]:
        """All luck entries for a country, sorted by luck_score DESC."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT user_id, citizen_name, luck_score, opens_count, updated_at
            FROM citizen_luck
            WHERE country_id = ?
            ORDER BY luck_score DESC
            """,
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "citizen_name": row[1] or row[0],
                        "luck_score": row[2],
                        "opens_count": row[3],
                        "updated_at": row[4],
                    }
                )
        return rows

    async def get_citizens_for_luck_refresh(
        self, country_id: str
    ) -> list[tuple[str, Optional[str]]]:
        """(user_id, citizen_name) for all cached citizens of a country."""
        rows: list[tuple[str, Optional[str]]] = []
        async with self._conn.execute(
            "SELECT user_id, citizen_name FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows

    async def get_all_citizens_for_global_luck(
        self,
    ) -> list[tuple[str, str, Optional[str]]]:
        """(user_id, country_id, citizen_name) for all cached citizens across all countries."""
        rows: list[tuple[str, str, Optional[str]]] = []
        async with self._conn.execute(
            "SELECT user_id, country_id, citizen_name FROM citizen_levels ORDER BY country_id"
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2]))
        return rows

    # ── Global luck (all countries) ───────────────────────────────────────────

    async def upsert_global_luck_score(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        luck_score: float,
        opens_count: int,
        rarity_json: Optional[str],
        updated_at: str,
    ) -> None:
        """Insert or replace a global luck entry (call flush_global_luck_scores to commit)."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO global_citizen_luck
                (user_id, country_id, citizen_name, luck_score, opens_count, rarity_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                country_id,
                citizen_name,
                luck_score,
                opens_count,
                rarity_json,
                updated_at,
            ),
        )

    async def flush_global_luck_scores(self) -> None:
        """Commit pending global luck upserts."""
        await self._conn.commit()

    async def clear_global_luck(self) -> None:
        """Delete all global luck entries (called at the start of a full sweep)."""
        await self._conn.execute("DELETE FROM global_citizen_luck")
        await self._conn.commit()

    async def get_global_luck_ranking(
        self,
        limit: Optional[int] = None,
        order: str = "DESC",
    ) -> list[dict]:
        """Global luck entries sorted by luck_score DESC (or ASC for bottom)."""
        order = "ASC" if order.upper() == "ASC" else "DESC"
        sql = f"""
            SELECT user_id, country_id, citizen_name, luck_score, opens_count, rarity_json, updated_at
            FROM global_citizen_luck
            ORDER BY luck_score {order}
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows: list[dict] = []
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "country_id": row[1],
                        "citizen_name": row[2] or row[0],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                    }
                )
        return rows

    async def get_global_luck_rank(self, user_id: str) -> tuple[Optional[int], int]:
        """Return (1-based rank, total) for a player in global_citizen_luck."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM global_citizen_luck"
        ) as cur:
            total: int = (await cur.fetchone() or (0,))[0]
        async with self._conn.execute(
            """
            SELECT COUNT(*) + 1 FROM global_citizen_luck
            WHERE luck_score > (SELECT luck_score FROM global_citizen_luck WHERE user_id = ?)
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            rank: Optional[int] = row[0] if row else None
        return rank, total

    async def search_global_luck_by_name(self, name: str) -> list[dict]:
        """Case-insensitive name search in global_citizen_luck."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT user_id, country_id, citizen_name, luck_score, opens_count, rarity_json, updated_at
            FROM global_citizen_luck
            WHERE lower(citizen_name) LIKE lower(?)
            ORDER BY luck_score DESC
            """,
            (f"%{name}%",),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "country_id": row[1],
                        "citizen_name": row[2] or row[0],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                    }
                )
        return rows
