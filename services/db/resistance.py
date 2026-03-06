"""Resistance state DB methods (resistance_state table)."""
from __future__ import annotations

from typing import Optional

import aiosqlite


class ResistanceMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """resistance_state table operations."""

    async def get_resistance_state(self, region_id: str) -> Optional[dict]:
        """Get the resistance state for a specific region."""
        async with self._conn.execute(
            "SELECT region_id, region_name, occupying_country, resistance_value, "
            "COALESCE(resistance_max, 100.0) as resistance_max, updated_at "
            "FROM resistance_state WHERE region_id = ?",
            (region_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "region_id": row[0],
            "region_name": row[1],
            "occupying_country": row[2],
            "resistance_value": row[3],
            "resistance_max": row[4],
            "updated_at": row[5],
        }

    async def upsert_resistance_state(
        self,
        region_id: str,
        region_name: Optional[str],
        occupying_country: Optional[str],
        resistance_value: float,
        resistance_max: float,
        updated_at: str,
    ) -> None:
        """Insert or update the resistance state for a specific region."""
        await self._conn.execute(
            """
            INSERT INTO resistance_state
                (region_id, region_name, occupying_country, resistance_value, resistance_max, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(region_id) DO UPDATE SET
                region_name       = excluded.region_name,
                occupying_country = excluded.occupying_country,
                resistance_value  = excluded.resistance_value,
                resistance_max    = excluded.resistance_max,
                updated_at        = excluded.updated_at
            """,
            (region_id, region_name, occupying_country, resistance_value, resistance_max, updated_at),
        )
        await self._conn.commit()