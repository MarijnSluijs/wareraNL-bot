"""Region upgrade (base/bunker) status + resistance DB methods.

Written hourly by services/full_fetcher.py:fetch_region_status(); read by the
extension's whitelisted-only /api/ext/regions/* endpoints (see
rijksoverheid_web/app/routers/extension_regions.py).
"""

from __future__ import annotations

from typing import Iterable, Optional

import aiosqlite


class RegionStatusMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """region_upgrade_status / region_resistance table operations."""

    async def save_region_upgrade_status(
        self,
        upgrade_type: str,
        rows: Iterable[tuple[str, str, int, Optional[str]]],
        updated_at: str,
    ) -> int:
        """Replace all rows for *upgrade_type* ('base' or 'bunker') with *rows*
        = (region_id, status, level, will_be_active_at).

        Regions are a small, fixed set that never appear/disappear mid-sweep,
        so — like alliance_countries — this deletes and reinserts whole rather
        than upserting: simpler, and guarantees a region the API stops
        returning doesn't leave a stale row behind forever.
        """
        payload = [
            (region_id, upgrade_type, status, int(level), will_be_active_at, updated_at)
            for region_id, status, level, will_be_active_at in rows
            if region_id and status
        ]
        await self._conn.execute(
            "DELETE FROM region_upgrade_status WHERE upgrade_type = ?", (upgrade_type,)
        )
        if payload:
            await self._conn.executemany(
                "INSERT INTO region_upgrade_status "
                "(region_id, upgrade_type, status, level, will_be_active_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
        await self._conn.commit()
        return len(payload)

    async def get_region_upgrade_status(self, upgrade_type: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        async with self._conn.execute(
            "SELECT region_id, status, level, will_be_active_at "
            "FROM region_upgrade_status WHERE upgrade_type = ?",
            (upgrade_type,),
        ) as cur:
            async for row in cur:
                out[row[0]] = {"status": row[1], "level": row[2], "willBeActiveAt": row[3]}
        return out

    async def save_region_resistance(
        self, rows: Iterable[tuple[str, float, float]], updated_at: str
    ) -> int:
        """Replace the resistance snapshot with *rows* = (region_id, resistance, resistance_max)."""
        payload = [
            (region_id, float(res), float(res_max), updated_at)
            for region_id, res, res_max in rows
            if region_id
        ]
        await self._conn.execute("DELETE FROM region_resistance")
        if payload:
            await self._conn.executemany(
                "INSERT INTO region_resistance (region_id, resistance, resistance_max, updated_at) "
                "VALUES (?, ?, ?, ?)",
                payload,
            )
        await self._conn.commit()
        return len(payload)

    async def get_region_resistance(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        async with self._conn.execute(
            "SELECT region_id, resistance, resistance_max FROM region_resistance"
        ) as cur:
            async for row in cur:
                out[row[0]] = {"resistance": row[1], "resistanceMax": row[2]}
        return out
