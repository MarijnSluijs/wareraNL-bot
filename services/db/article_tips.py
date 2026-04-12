"""Article tips DB methods (article_tips table)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class ArticleTipsMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_article_tip(
        self,
        user_id: str,
        country_id: Optional[str],
        citizen_name: Optional[str],
        amount: float,
        tip_at: str,
        recorded_at: str,
    ) -> bool:
        """Insert a single tip record. Returns True if newly inserted, False if duplicate."""
        cursor = await self._conn.execute(
            """
            INSERT OR IGNORE INTO article_tips
                (user_id, country_id, citizen_name, amount, tip_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, country_id, citizen_name, amount, tip_at, recorded_at),
        )
        return cursor.rowcount == 1

    async def flush_article_tips(self) -> None:
        """Commit pending article tip inserts."""
        await self._conn.commit()

    async def delete_article_tips_for_user(self, user_id: str) -> None:
        """Remove all tip records for a user (for re-scan)."""
        await self._conn.execute(
            "DELETE FROM article_tips WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def get_top_tippers(
        self,
        days: Optional[int],
        limit: int,
        country_id: Optional[str] = None,
    ) -> list[dict]:
        """Return top tippers sorted by total CC spent, with optional country/time filter.

        Each row: {user_id, citizen_name, country_id, tip_count, tip_total}
        """
        conds: list[str] = []
        params: list = []

        if days:
            conds.append(f"tip_at >= datetime('now', '-{days} days')")
        if country_id:
            conds.append("country_id = ?")
            params.append(country_id)

        where = ("WHERE " + " AND ".join(conds)) if conds else ""

        query = f"""
            SELECT user_id,
                   MAX(citizen_name) AS citizen_name,
                   MAX(country_id)   AS country_id,
                   COUNT(*)          AS tip_count,
                   SUM(amount)       AS tip_total
            FROM article_tips
            {where}
            GROUP BY user_id
            ORDER BY tip_total DESC
            LIMIT ?
        """
        params.append(limit)

        rows: list[dict] = []
        async with self._conn.execute(query, params) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "citizen_name": row[1] or row[0],
                        "country_id": row[2] or "",
                        "tip_count": row[3],
                        "tip_total": row[4] or 0.0,
                    }
                )
        return rows

    async def get_article_tips_date_range(self) -> tuple[Optional[str], Optional[str]]:
        """Return (earliest tip_at, latest tip_at) in the table."""
        async with self._conn.execute(
            "SELECT MIN(tip_at), MAX(tip_at) FROM article_tips"
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0], row[1]
        return None, None

    async def get_article_tips_user_count(self) -> int:
        """Return the number of distinct users with stored tip records."""
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM article_tips"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_latest_tip_at_for_user(self, user_id: str) -> Optional[str]:
        """Return the ISO timestamp of the most recent tip already stored for *user_id*.

        Used by the incremental scan to skip transactions we've already stored.
        Returns None if no tips are stored for this user yet.
        """
        async with self._conn.execute(
            "SELECT MAX(tip_at) FROM article_tips WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None

    async def get_last_scanned_at(self, user_id: str) -> Optional[str]:
        """Return the ISO timestamp of the last full scan for *user_id*, or None."""
        async with self._conn.execute(
            "SELECT last_scanned_at FROM article_tip_scans WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def upsert_scan_timestamp(self, user_id: str, scanned_at: str) -> None:
        """Record that *user_id* was scanned at *scanned_at*."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO article_tip_scans (user_id, last_scanned_at)
            VALUES (?, ?)
            """,
            (user_id, scanned_at),
        )

    async def bulk_init_scan_timestamps(self, scanned_at: str) -> None:
        """Stamp all citizens from citizen_levels not yet in article_tip_scans.

        Called at the end of a sweep so that zero-tip citizens from a previous run
        (which had no per-citizen timestamp tracking) are marked as scanned and will
        be skipped on the next sweep within RESCAN_DAYS.
        """
        await self._conn.execute(
            "INSERT OR IGNORE INTO article_tip_scans (user_id, last_scanned_at) "
            "SELECT user_id, ? FROM citizen_levels",
            (scanned_at,),
        )
        await self._conn.commit()
