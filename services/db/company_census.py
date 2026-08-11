"""Database mixin for ``company_census`` / ``company_census_runs``.

An hourly census of every company in the game, bucketed by the country that
currently controls the region the company sits in, and by the item it produces.
Written by :mod:`services.full_fetcher`; read by the Nigeria bot's
``/fabrieken`` command.

Storing counts for every (country, item) pair costs no extra API calls — the
sweep has to look at all ~73k companies regardless — which is what lets
``/fabrieken`` answer for any country × item combination without touching the
WarEra API.

``company_owners`` adds a per-owner breakdown from the very same
``company.getById`` responses (the owner ID was previously discarded), so it is
likewise free.  It keeps only the newest sweep — see
:meth:`CompanyCensusMixin.save_company_owners`.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger("services.db.company_census")

# How long census history is kept. Comfortably covers the 7-day delta the
# commands show, with room for longer look-backs later.
CENSUS_RETENTION_DAYS = 90


class CompanyCensusMixin:
    """CRUD + query helpers for the hourly company census."""

    async def save_company_census(
        self,
        captured_at: str,
        rows: Iterable[tuple[str, str, int, int, int]],
        *,
        listed_companies: int,
        checked_companies: int,
        duration_ms: Optional[int] = None,
    ) -> int:
        """Persist one complete sweep.

        *rows* is an iterable of ``(country_id, item_code, company_count,
        worker_count, staffed_count)``.  All rows share the same *captured_at*
        so a reader can treat one timestamp as one consistent snapshot.
        Returns the number of census rows written.
        """
        payload = [
            (captured_at, str(country_id), str(item_code), int(companies),
             int(workers), int(staffed))
            for country_id, item_code, companies, workers, staffed in rows
            if country_id and item_code
        ]
        if payload:
            await self._conn.executemany(
                "INSERT OR REPLACE INTO company_census "
                "(captured_at, country_id, item_code, company_count, worker_count, "
                " staffed_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
        await self._conn.execute(
            "INSERT OR REPLACE INTO company_census_runs "
            "(captured_at, listed_companies, checked_companies, duration_ms) "
            "VALUES (?, ?, ?, ?)",
            (captured_at, int(listed_companies), int(checked_companies), duration_ms),
        )
        await self._conn.commit()
        return len(payload)

    async def get_latest_company_census(
        self, country_id: str, item_code: str
    ) -> Optional[dict]:
        """Most recent census row for one (country, item), or None.

        Returns ``{captured_at, company_count, worker_count, listed_companies,
        checked_companies}``.  The run totals are joined in so a command can
        report how many companies the sweep looked at.
        """
        async with self._conn.execute(
            "SELECT c.captured_at, c.company_count, c.worker_count, "
            "       r.listed_companies, r.checked_companies "
            "FROM company_census c "
            "LEFT JOIN company_census_runs r ON r.captured_at = c.captured_at "
            "WHERE c.country_id = ? AND c.item_code = ? "
            "ORDER BY c.captured_at DESC LIMIT 1",
            (country_id, item_code),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "captured_at": row[0],
            "company_count": int(row[1] or 0),
            "worker_count": int(row[2] or 0),
            "listed_companies": int(row[3] or 0),
            "checked_companies": int(row[4] or 0),
        }

    async def get_company_census_at_or_before(
        self, country_id: str, item_code: str, cutoff_iso: str
    ) -> Optional[dict]:
        """Newest census row at or before *cutoff_iso*, or None if none exists.

        Used for the "vs 7 days ago" delta.  Picking the newest row *at or
        before* the cutoff (rather than the oldest row after it) means a gap in
        sweep coverage yields a slightly older comparison point rather than no
        comparison at all.
        """
        async with self._conn.execute(
            "SELECT captured_at, company_count, worker_count "
            "FROM company_census "
            "WHERE country_id = ? AND item_code = ? AND captured_at <= ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (country_id, item_code, cutoff_iso),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "captured_at": row[0],
            "company_count": int(row[1] or 0),
            "worker_count": int(row[2] or 0),
        }

    async def save_company_owners(
        self, captured_at: str, rows: Iterable[tuple[str, str, str, int, int, int]]
    ) -> int:
        """Replace the owner snapshot with *rows* for this sweep.

        *rows* is ``(country_id, item_code, owner_id, company_count,
        worker_count, staffed_count)``.  Inserts first and only then deletes
        older sweeps, so a concurrent reader sees either the previous complete
        snapshot or the new one — never a half-empty table.  Returns the number
        of rows written.
        """
        payload = [
            (captured_at, str(country_id), str(item_code), str(owner_id),
             int(companies), int(workers), int(staffed))
            for country_id, item_code, owner_id, companies, workers, staffed in rows
            if country_id and item_code and owner_id
        ]
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT OR REPLACE INTO company_owners "
            "(captured_at, country_id, item_code, owner_id, company_count, "
            " worker_count, staffed_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            payload,
        )
        await self._conn.execute(
            "DELETE FROM company_owners WHERE captured_at != ?", (captured_at,)
        )
        await self._conn.commit()
        return len(payload)

    async def prune_company_census(self, cutoff_iso: str) -> int:
        """Delete census rows and runs older than *cutoff_iso*. Returns rows deleted."""
        cursor = await self._conn.execute(
            "DELETE FROM company_census WHERE captured_at < ?", (cutoff_iso,)
        )
        deleted = cursor.rowcount or 0
        await self._conn.execute(
            "DELETE FROM company_census_runs WHERE captured_at < ?", (cutoff_iso,)
        )
        await self._conn.commit()
        return deleted
