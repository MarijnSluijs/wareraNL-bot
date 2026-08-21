"""Proxy/puppet-country status DB methods.

Written periodically by services/full_fetcher.py:fetch_country_proxy_status();
read by the extension's whitelisted-only /api/ext/countries/proxy endpoint
(see rijksoverheid_web/app/routers/extension_countries.py).
"""

from __future__ import annotations

from typing import Iterable

import aiosqlite


class CountryProxyMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """country_proxy_status table operations."""

    async def save_country_proxy_status(
        self, rows: Iterable[tuple[str, str, float]], updated_at: str
    ) -> int:
        """Replace the whole proxy-status snapshot with *rows* = (country_id, origin_id, rate).

        Whole-table replace, same reasoning as region_upgrade_status: a
        country that stops being a proxy this sweep must disappear from the
        output entirely, not linger with a stale row.
        """
        payload = [
            (country_id, origin_id, float(rate), updated_at)
            for country_id, origin_id, rate in rows
            if country_id and origin_id
        ]
        await self._conn.execute("DELETE FROM country_proxy_status")
        if payload:
            await self._conn.executemany(
                "INSERT INTO country_proxy_status (country_id, origin_id, rate, updated_at) "
                "VALUES (?, ?, ?, ?)",
                payload,
            )
        await self._conn.commit()
        return len(payload)

    async def get_country_proxy_status(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        async with self._conn.execute(
            "SELECT country_id, origin_id, rate FROM country_proxy_status"
        ) as cur:
            async for row in cur:
                out[row[0]] = {"origin": row[1], "rate": row[2]}
        return out
