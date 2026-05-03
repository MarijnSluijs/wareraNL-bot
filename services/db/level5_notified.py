"""lvl5_tracker table DB methods."""

from __future__ import annotations

from typing import TypedDict

import aiosqlite


class Lvl5TrackerRow(TypedDict):
    user_id: str
    last_seen_level: int
    notified: int


class Level5NotifiedMixin:
    """lvl5_tracker table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def get_lvl5_tracker(self) -> dict[str, Lvl5TrackerRow]:
        """Return all rows from lvl5_tracker keyed by user_id."""
        rows: dict[str, Lvl5TrackerRow] = {}
        async with self._conn.execute(
            "SELECT user_id, last_seen_level, notified FROM lvl5_tracker"
        ) as cur:
            async for row in cur:
                rows[str(row[0])] = Lvl5TrackerRow(
                    user_id=str(row[0]),
                    last_seen_level=int(row[1]),
                    notified=int(row[2]),
                )
        return rows

    async def upsert_lvl5_tracker(
        self,
        user_id: str,
        last_seen_level: int,
        notified: int,
        updated_at: str,
    ) -> None:
        """Insert or update a single lvl5_tracker row."""
        await self._conn.execute(
            """
            INSERT INTO lvl5_tracker(user_id, last_seen_level, notified, updated_at)
                VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen_level = excluded.last_seen_level,
                notified        = excluded.notified,
                updated_at      = excluded.updated_at
            """,
            (user_id, last_seen_level, notified, updated_at),
        )

    async def bulk_upsert_lvl5_tracker(
        self,
        rows: list[tuple[str, int, int, str]],
    ) -> None:
        """Bulk-upsert rows as (user_id, last_seen_level, notified, updated_at)."""
        if not rows:
            return
        await self._conn.executemany(
            """
            INSERT INTO lvl5_tracker(user_id, last_seen_level, notified, updated_at)
                VALUES(?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_seen_level = excluded.last_seen_level,
                notified        = excluded.notified,
                updated_at      = excluded.updated_at
            """,
            rows,
        )
        await self._conn.commit()
