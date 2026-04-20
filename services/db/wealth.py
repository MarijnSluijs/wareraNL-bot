"""Citizen wealth DB mixin."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class WealthMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_citizen_wealth(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        wealth_active: float,
        wealth_inactive: float,
        updated_at: str,
    ) -> None:
        """Insert or update a citizen's wealth record."""
        wealth_total = wealth_active + wealth_inactive
        await self._conn.execute(
            "INSERT INTO citizen_wealth"
            " (user_id, country_id, citizen_name, wealth_active, wealth_inactive_companies, wealth_total, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            "   country_id                = excluded.country_id,"
            "   citizen_name              = COALESCE(excluded.citizen_name, citizen_wealth.citizen_name),"
            "   wealth_active             = excluded.wealth_active,"
            "   wealth_inactive_companies = excluded.wealth_inactive_companies,"
            "   wealth_total              = excluded.wealth_total,"
            "   updated_at                = excluded.updated_at",
            (user_id, country_id, citizen_name, wealth_active, wealth_inactive, wealth_total, updated_at),
        )

    async def flush_citizen_wealth(self) -> None:
        """Commit pending wealth writes."""
        await self._conn.commit()

    async def get_wealth_ranking(self, country_id: str, limit: int = 10) -> list[dict]:
        """Return top `limit` citizens sorted by total wealth descending."""
        sql = (
            "SELECT user_id, citizen_name, wealth_active, wealth_inactive_companies,"
            " wealth_total, updated_at"
            " FROM citizen_wealth"
            " WHERE country_id = ?"
            " ORDER BY wealth_total DESC"
            " LIMIT ?"
        )
        rows: list[dict] = []
        async with self._conn.execute(sql, (country_id, limit)) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1],
                    "wealth_active": row[2],
                    "wealth_inactive_companies": row[3],
                    "wealth_total": row[4],
                    "updated_at": row[5],
                })
        return rows

    async def search_citizen_wealth(
        self,
        name_query: str,
        country_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search citizens by name (case-insensitive substring), sorted by total wealth."""
        sql = (
            "SELECT user_id, citizen_name, wealth_active, wealth_inactive_companies,"
            " wealth_total, updated_at"
            " FROM citizen_wealth"
            " WHERE country_id = ? AND LOWER(citizen_name) LIKE LOWER(?)"
            " ORDER BY wealth_total DESC"
            " LIMIT ?"
        )
        pattern = f"%{name_query}%"
        rows: list[dict] = []
        async with self._conn.execute(sql, (country_id, pattern, limit)) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1],
                    "wealth_active": row[2],
                    "wealth_inactive_companies": row[3],
                    "wealth_total": row[4],
                    "updated_at": row[5],
                })
        return rows

    async def get_citizen_wealth_rank(self, user_id: str, country_id: str) -> Optional[int]:
        """Return the 1-based rank of a citizen by total wealth, or None if not found."""
        sql = (
            "SELECT rank FROM ("
            "  SELECT user_id, ROW_NUMBER() OVER (ORDER BY wealth_total DESC) AS rank"
            "  FROM citizen_wealth WHERE country_id = ?"
            ") WHERE user_id = ?"
        )
        async with self._conn.execute(sql, (country_id, user_id)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None
