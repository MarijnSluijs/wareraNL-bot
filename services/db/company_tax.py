"""Database mixin for wage income-tax tracking.

Tables: ``worker_company_map``, ``country_tax_rates``, ``company_tax_revenue``,
``company_owner_map``.

A wage transaction from ``transaction.getPaginatedTransactions`` names only the
worker (``sellerId``) and the employer (``buyerId``) — never the company or the
item.  Tax is owed to the country the *company* sits in, so the attribution
chain is::

    wage.sellerId → worker_company_map → (company, country, item)
    tax = wage.money × country_tax_rates.income_tax / 100

``company_owner_map`` extends this chain one hop further, from a company to
its owner's *nationality*::

    company_tax_revenue.company_id → company_owner_map → owner_id → citizen_levels.country_id

which is what lets the Nigeria bot's ``/tax-breakdown`` command say "of the
tax generated in Nigeria, this much came from companies owned by Dutch
citizens" — a different question from "how much tax did Nigeria collect",
which ``company_tax_revenue.country_id`` alone already answers (and is what
``/fabrieken`` shows).

Written by :mod:`services.full_fetcher`, read by the Nigeria bot's
``/fabrieken`` and ``/tax-breakdown`` commands.
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

    # ── company → owner map (tax attribution by owner nationality) ──────────

    async def save_company_owner_map(
        self, rows: Iterable[tuple[str, str, str, str]], updated_at: str
    ) -> int:
        """Upsert ``(company_id, owner_id, country_id, item_code)`` rows.

        Built from the same ``company.getById`` responses the census phase
        already reads, for every company seen (not just staffed ones), so
        this costs no extra API calls. Read by ``/tax-breakdown`` to trace a
        ``company_tax_revenue`` row — which carries only a company id — back
        to the owner's nationality via ``citizen_levels``.
        """
        payload = [
            (str(c), str(o), str(co), str(i), updated_at)
            for c, o, co, i in rows
            if c and o
        ]
        if not payload:
            return 0
        await self._conn.executemany(
            "INSERT INTO company_owner_map "
            "(company_id, owner_id, country_id, item_code, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(company_id) DO UPDATE SET "
            "  owner_id = excluded.owner_id, country_id = excluded.country_id, "
            "  item_code = excluded.item_code, updated_at = excluded.updated_at",
            payload,
        )
        await self._conn.commit()
        return len(payload)

    async def prune_company_owner_map(self, cutoff_iso: str) -> int:
        """Drop owner-map rows not refreshed since *cutoff_iso*.

        Kept as long as the tax revenue history they attribute
        (``TAX_RETENTION_DAYS``), so a company sold or destroyed mid-window
        still resolves to whoever owned it while the tax was collected.
        """
        cursor = await self._conn.execute(
            "DELETE FROM company_owner_map WHERE updated_at < ?", (cutoff_iso,)
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

    async def get_country_tax_summary(
        self, today: str, week_start: str, month_start: str
    ) -> dict[str, dict[str, float]]:
        """Per-country tax totals for three trailing windows, in one query.

        ``today``/``week_start``/``month_start`` are ``YYYY-MM-DD`` (UTC) day
        strings computed by the caller — ``today`` is today's own bucket
        (still filling in as the day goes), ``week_start``/``month_start`` are
        the first day of a 7-/30-day trailing window (inclusive of today).
        Returns ``{country_id: {"daily": x, "weekly": y, "monthly": z}}`` for
        every country with at least one row in the monthly window; a country
        with no tax at all this month is simply absent, not zeroed.

        Used by the browser extension's country-page tax tiles — see
        rijksoverheid_web/app/routers/extension_countries.py.
        """
        async with self._conn.execute(
            "SELECT country_id, "
            "  SUM(CASE WHEN day = ? THEN tax_total ELSE 0 END), "
            "  SUM(CASE WHEN day >= ? THEN tax_total ELSE 0 END), "
            "  SUM(CASE WHEN day >= ? THEN tax_total ELSE 0 END) "
            "FROM company_tax_revenue "
            "WHERE day >= ? "
            "GROUP BY country_id",
            (today, week_start, month_start, month_start),
        ) as cur:
            rows = await cur.fetchall()
        return {
            str(cid): {"daily": float(d or 0), "weekly": float(w or 0), "monthly": float(m or 0)}
            for cid, d, w, m in rows
        }

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
