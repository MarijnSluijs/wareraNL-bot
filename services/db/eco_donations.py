"""EcoDonationsMixin — cache for NL eco donation transactions.

The ``eco_donations`` table is populated hourly by
``cogs/tasks/eco_donations_poller.py`` and queried by the
``/eco_donaties`` slash command.
"""

from __future__ import annotations

from typing import Optional


class EcoDonationsMixin:
    """Mixin for the ``eco_donations`` table."""

    # ------------------------------------------------------------------
    # Write helpers (used by the poller)
    # ------------------------------------------------------------------

    async def upsert_eco_donation(
        self,
        txn_id: str,
        user_id: str,
        citizen_name: Optional[str],
        mu_name: Optional[str],
        amount: float,
        created_at: str,
    ) -> None:
        """Insert a donation row; silently skip if *txn_id* already exists."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO eco_donations"
            " (txn_id, user_id, citizen_name, mu_name, amount, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (txn_id, user_id, citizen_name, mu_name, amount, created_at),
        )

    async def flush_eco_donations(self) -> None:
        """Commit any pending eco_donation inserts."""
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Read helpers (used by the command)
    # ------------------------------------------------------------------

    async def get_latest_eco_donation_at(self) -> Optional[str]:
        """Return the most recent ``created_at`` value, or *None* when empty."""
        async with self._conn.execute(
            "SELECT MAX(created_at) FROM eco_donations"
        ) as cur:
            row = await cur.fetchone()
            return str(row[0]) if row and row[0] else None

    async def get_eco_donation_mu_totals(
        self,
        since_iso: str,
    ) -> list[tuple[str, float]]:
        """Return ``[(mu_name, total_amount), ...]`` ordered by total descending.

        Rows without an MU are grouped under ``'Geen MU'`` and sorted last.
        """
        rows: list[tuple[str, float]] = []
        async with self._conn.execute(
            "SELECT COALESCE(mu_name, 'Geen MU') as mu_label, SUM(amount) as total"
            " FROM eco_donations"
            " WHERE created_at >= ?"
            " GROUP BY mu_label"
            " ORDER BY CASE WHEN mu_name IS NULL THEN 1 ELSE 0 END, total DESC",
            (since_iso,),
        ) as cur:
            async for row in cur:
                rows.append((str(row[0]), float(row[1])))
        return rows

    async def get_eco_donation_player_totals(
        self,
        since_iso: str,
        mu_name: Optional[str] = None,
    ) -> list[tuple[str, str, float]]:
        """Return ``[(user_id, display_name, total_amount), ...]`` ordered by total desc.

        *display_name* is ``citizen_name`` when available, otherwise ``user_id``.
        If *mu_name* is given, only rows for that MU are returned.
        Pass an empty string ``""`` to get only players *without* an MU.
        """
        rows: list[tuple[str, str, float]] = []
        if mu_name is not None:
            if mu_name == "":  # "Geen MU" — players not in any MU
                sql = (
                    "SELECT user_id, COALESCE(citizen_name, user_id), SUM(amount) as total"
                    " FROM eco_donations"
                    " WHERE created_at >= ? AND mu_name IS NULL"
                    " GROUP BY user_id"
                    " ORDER BY total DESC"
                )
                params: tuple = (since_iso,)
            else:
                sql = (
                    "SELECT user_id, COALESCE(citizen_name, user_id), SUM(amount) as total"
                    " FROM eco_donations"
                    " WHERE created_at >= ? AND mu_name = ?"
                    " GROUP BY user_id"
                    " ORDER BY total DESC"
                )
                params = (since_iso, mu_name)
        else:
            sql = (
                "SELECT user_id, COALESCE(citizen_name, user_id), SUM(amount) as total"
                " FROM eco_donations"
                " WHERE created_at >= ?"
                " GROUP BY user_id"
                " ORDER BY total DESC"
            )
            params = (since_iso,)
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((str(row[0]), str(row[1]), float(row[2])))
        return rows

    async def count_eco_donation_donors(self, since_iso: str) -> int:
        """Return the number of distinct donors since *since_iso*."""
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM eco_donations WHERE created_at >= ?",
            (since_iso,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def count_eco_donation_mus(self, since_iso: str) -> int:
        """Return the number of distinct MUs with donations since *since_iso*."""
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT mu_name) FROM eco_donations"
            " WHERE created_at >= ? AND mu_name IS NOT NULL",
            (since_iso,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0
