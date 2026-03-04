"""Production-related DB methods (snapshots, specialization tops, deposit tops)."""
from __future__ import annotations

from typing import Optional

import aiosqlite


class ProductionMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """country_snapshots, specialization_top, deposit_top tables."""

    async def save_country_snapshot(
        self,
        country_id: str,
        code: Optional[str],
        name: Optional[str],
        specialized_item: Optional[str],
        production_bonus: Optional[float],
        raw_json: str,
        updated_at: str,
    ) -> None:
        """Insert or replace a country snapshot record."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO country_snapshots"
            "(country_id, code, name, specialized_item, production_bonus, raw_json, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (country_id, code, name, specialized_item, production_bonus, raw_json, updated_at),
        )
        await self._conn.commit()

    # ── specialization_top ───────────────────────────────────────────────────

    async def get_top_specialization(self, item: str) -> Optional[dict]:
        """Get the top specialization for a specific item."""
        async with self._conn.execute(
            "SELECT country_id, country_name, production_bonus, strategic_bonus, "
            "ethic_bonus, ethic_deposit_bonus, updated_at "
            "FROM specialization_top WHERE item = ?",
            (item,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "country_id": row[0],
                "country_name": row[1],
                "production_bonus": row[2],
                "strategic_bonus": row[3],
                "ethic_bonus": row[4],
                "ethic_deposit_bonus": row[5],
                "updated_at": row[6],
            }

    async def get_all_tops(self) -> list[dict]:
        """Get all specialization tops."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT item, country_id, country_name, production_bonus, "
            "strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at "
            "FROM specialization_top"
        ) as cur:
            async for row in cur:
                rows.append({
                    "item": row[0],
                    "country_id": row[1],
                    "country_name": row[2],
                    "production_bonus": row[3],
                    "strategic_bonus": row[4],
                    "ethic_bonus": row[5],
                    "ethic_deposit_bonus": row[6],
                    "updated_at": row[7],
                })
        return rows

    async def set_top_specialization(
        self,
        item: str,
        country_id: str,
        country_name: str,
        production_bonus: float,
        updated_at: str,
        strategic_bonus: Optional[float] = None,
        ethic_bonus: Optional[float] = None,
        ethic_deposit_bonus: Optional[float] = None,
    ) -> None:
        """Insert or replace a specialization top record."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO specialization_top"
            "(item, country_id, country_name, production_bonus, "
            "strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (item, country_id, country_name, production_bonus,
             strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at),
        )
        await self._conn.commit()

    async def delete_top_specialization(self, item: str) -> None:
        """Delete a specialization top record for a specific item."""
        await self._conn.execute(
            "DELETE FROM specialization_top WHERE item = ?", (item,)
        )
        await self._conn.commit()

    # ── country_item_ethic ───────────────────────────────────────────────────

    async def save_country_item_ethic(
        self,
        item: str,
        country_id: str,
        strategic_bonus: float,
        ethic_bonus: float,
        updated_at: str,
    ) -> None:
        """Upsert an ethics entry for a (item, country_id) pair."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO country_item_ethic"
            "(item, country_id, strategic_bonus, ethic_bonus, updated_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (item, country_id, strategic_bonus, ethic_bonus, updated_at),
        )
        await self._conn.commit()

    async def get_all_country_item_ethics(self) -> list[dict]:
        """Return all rows from country_item_ethic as a list of dicts."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT item, country_id, strategic_bonus, ethic_bonus FROM country_item_ethic"
        ) as cur:
            async for row in cur:
                rows.append({
                    "item": row[0],
                    "country_id": row[1],
                    "strategic_bonus": row[2],
                    "ethic_bonus": row[3],
                })
        return rows

    async def get_country_spec_map(self) -> dict[str, str]:
        """Return {country_id: specialized_item} for all countries with a specialization."""
        result: dict[str, str] = {}
        async with self._conn.execute(
            "SELECT country_id, specialized_item FROM country_snapshots"
            " WHERE specialized_item IS NOT NULL AND specialized_item != ''"
        ) as cur:
            async for row in cur:
                result[row[0]] = row[1]
        return result

    # ── deposit_top ──────────────────────────────────────────────────────────

    async def get_deposit_top(self, item: str) -> Optional[dict]:
        """Get the top deposit for a specific item."""
        async with self._conn.execute(
            "SELECT region_id, region_name, country_id, country_name, bonus, "
            "deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at "
            "FROM deposit_top WHERE item = ?",
            (item,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "region_id": row[0],
                "region_name": row[1],
                "country_id": row[2],
                "country_name": row[3],
                "bonus": row[4],
                "deposit_bonus": row[5],
                "ethic_deposit_bonus": row[6],
                "permanent_bonus": row[7],
                "deposit_end_at": row[8],
                "updated_at": row[9],
            }

    async def get_all_deposit_tops(self) -> list[dict]:
        """Get all deposit tops."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT item, region_id, region_name, country_id, country_name, bonus, "
            "deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at "
            "FROM deposit_top"
        ) as cur:
            async for row in cur:
                rows.append({
                    "item": row[0],
                    "region_id": row[1],
                    "region_name": row[2],
                    "country_id": row[3],
                    "country_name": row[4],
                    "bonus": row[5],
                    "deposit_bonus": row[6],
                    "ethic_deposit_bonus": row[7],
                    "permanent_bonus": row[8],
                    "deposit_end_at": row[9],
                    "updated_at": row[10],
                })
        return rows

    async def set_deposit_top(
        self,
        item: str,
        region_id: str,
        region_name: str,
        country_id: str,
        country_name: str,
        bonus: int,
        deposit_bonus: float,
        ethic_deposit_bonus: float,
        permanent_bonus: float,
        deposit_end_at: str,
        updated_at: str,
    ) -> None:
        """Insert or replace a deposit top record."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO deposit_top"
            "(item, region_id, region_name, country_id, country_name, bonus, "
            "deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item, region_id, region_name, country_id, country_name, bonus,
             deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at),
        )
        await self._conn.commit()
