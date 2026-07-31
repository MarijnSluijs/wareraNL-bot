"""DB methods for division_mu_overrides — runtime edits to DIVISION_MUS."""

from __future__ import annotations

import aiosqlite


class DivisionOverridesMixin:
    """division_mu_overrides table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def upsert_division_mu_override(self, mu_name: str, division: int) -> None:
        """Persist a runtime add/move/remove edit. division=0 means removed."""
        await self._conn.execute(
            "INSERT INTO division_mu_overrides (mu_name, division) VALUES (?, ?) "
            "ON CONFLICT(mu_name) DO UPDATE SET division = excluded.division",
            (mu_name, division),
        )
        await self._conn.commit()

    async def get_all_division_mu_overrides(self) -> list[tuple[str, int]]:
        """Return [(mu_name, division)] for every stored override."""
        async with self._conn.execute(
            "SELECT mu_name, division FROM division_mu_overrides"
        ) as cur:
            return [(row[0], row[1]) async for row in cur]
