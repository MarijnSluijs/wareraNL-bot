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
        updated_at: str,
    ) -> None:
        """Insert or replace a luck score (call flush_luck_scores to commit batch)."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO citizen_luck
                (user_id, country_id, citizen_name, luck_score, opens_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, country_id, citizen_name, luck_score, opens_count, updated_at),
        )

    async def flush_luck_scores(self) -> None:
        """Commit any pending luck score upserts."""
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
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1] or row[0],
                    "luck_score": row[2],
                    "opens_count": row[3],
                    "updated_at": row[4],
                })
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
