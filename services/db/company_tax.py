"""Database mixin for wage income-tax tracking.

Tables: ``worker_company_map``, ``country_tax_rates``, ``company_tax_revenue``.

A wage transaction from ``transaction.getPaginatedTransactions`` names only the
worker (``sellerId``) and the employer (``buyerId``) — never the company or the
item.  Tax is owed to the country the *company* sits in, so the attribution
chain is::

    wage.sellerId → worker_company_map → (company, country, item)
    tax = wage.money × country_tax_rates.income_tax / 100

Written by :mod:`services.full_fetcher`, read by the Nigeria bot's
``/fabrieken`` command.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger("services.db.company_tax")

# Worker→company rows are kept this long after a worker was last seen employed,
# so wages paid shortly before they quit still resolve to the right company.
WORKER_MAP_RETENTION_DAYS = 7

# Daily tax rows are kept this long. The command shows 7 days in detail plus a
# 30-day total, so 90 days leaves room for longer look-backs later.
TAX_RETENTION_DAYS = 90

# poll_state keys
TAX_WATERMARK_KEY = "wage_tax_last_tx_id"
TAX_STARTED_KEY = "wage_tax_started_at"


class CompanyTaxMixin:
    """CRUD + query helpers for wage income-tax tracking."""

    # ── country tax rates ────────────────────────────────────────────────────

    async def save_country_tax_rates(
        self, rows: Iterable[tuple[str, float]], updated_at: str
    ) -> int:
        """Upsert ``(country_id, income_tax)`` pairs. Returns rows written."""
        payload = [
            (str(cid), float(rate), updated_at)
            for cid, rate in rows
            if cid is not None
        ]
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT INTO country_tax_rates (country_id, income_tax, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(country_id) DO UPDATE SET "
            "  income_tax = excluded.income_tax, updated_at = excluded.updated_at",
            payload,
        )
        await self._conn.commit()
        return len(payload)

    async def get_country_tax_rates(self) -> dict[str, float]:
        """Return ``{country_id: income_tax_percent}``."""
        async with self._conn.execute(
            "SELECT country_id, income_tax FROM country_tax_rates"
        ) as cur:
            return {str(r[0]): float(r[1] or 0) for r in await cur.fetchall()}

    # ── worker → company map ─────────────────────────────────────────────────

    async def save_worker_company_map(
        self, rows: Iterable[tuple[str, str, str, str]], updated_at: str
    ) -> int:
        """Upsert ``(worker_id, company_id, country_id, item_code)`` rows."""
        payload = [
            (str(w), str(c), str(co), str(i), updated_at)
            for w, c, co, i in rows
            if w and c
        ]
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT INTO worker_company_map "
            "(worker_id, company_id, country_id, item_code, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET "
            "  company_id = excluded.company_id, country_id = excluded.country_id, "
            "  item_code = excluded.item_code, updated_at = excluded.updated_at",
            payload,
        )
        await self._conn.commit()
        return len(payload)

    async def get_worker_company_map(self) -> dict[str, tuple[str, str, str]]:
        """Return ``{worker_id: (company_id, country_id, item_code)}``."""
        async with self._conn.execute(
            "SELECT worker_id, company_id, country_id, item_code FROM worker_company_map"
        ) as cur:
            return {
                str(r[0]): (str(r[1]), str(r[2]), str(r[3]))
                for r in await cur.fetchall()
            }

    async def prune_worker_company_map(self, cutoff_iso: str) -> int:
        """Drop worker rows not refreshed since *cutoff_iso*."""
        cursor = await self._conn.execute(
            "DELETE FROM worker_company_map WHERE updated_at < ?", (cutoff_iso,)
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    # ── tax revenue ──────────────────────────────────────────────────────────

    async def add_tax_revenue(
        self, rows: Iterable[tuple[str, str, str, str, float, float, int]]
    ) -> int:
        """Add ``(day, country, item, company, tax, wage, tx_count)`` buckets.

        Values are *added* to any existing row, because a day is filled in over
        many hourly sweeps.  Only transactions newer than the stored watermark
        are ever passed here, so adding never double-counts.
        """
        payload = [
            (str(day), str(country), str(item), str(company),
             float(tax), float(wage), int(count))
            for day, country, item, company, tax, wage, count in rows
        ]
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT INTO company_tax_revenue "
            "(day, country_id, item_code, company_id, tax_total, wage_total, tx_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, country_id, item_code, company_id) DO UPDATE SET "
            "  tax_total = tax_total + excluded.tax_total, "
            "  wage_total = wage_total + excluded.wage_total, "
            "  tx_count  = tx_count  + excluded.tx_count",
            payload,
        )
        await self._conn.commit()
        return len(payload)

    async def prune_tax_revenue(self, cutoff_day: str) -> int:
        """Delete daily tax rows older than *cutoff_day* (YYYY-MM-DD)."""
        cursor = await self._conn.execute(
            "DELETE FROM company_tax_revenue WHERE day < ?", (cutoff_day,)
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    # ── watermark / start marker ─────────────────────────────────────────────

    async def get_tax_watermark(self) -> Optional[str]:
        """Newest wage transaction ID already counted, or None on a fresh install."""
        return await self.get_poll_state(TAX_WATERMARK_KEY)

    async def set_tax_watermark(self, tx_id: str) -> None:
        await self.set_poll_state(TAX_WATERMARK_KEY, tx_id)

    async def get_tax_started_at(self) -> Optional[str]:
        """ISO timestamp of when tax tracking began (shown by /fabrieken)."""
        return await self.get_poll_state(TAX_STARTED_KEY)

    async def set_tax_started_at(self, iso: str) -> None:
        await self.set_poll_state(TAX_STARTED_KEY, iso)
