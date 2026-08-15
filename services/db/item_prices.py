"""Database mixin for the ``item_price_history`` table.

Stores hourly snapshots of ``itemTrading.getPrices`` (the WarEra API only
exposes the current price for fungible resources, not history) so the
website can render a price chart. Populated by
``cogs/tasks/item_price_sync.py``.
"""

from __future__ import annotations

from typing import Optional


class ItemPricesMixin:
    """CRUD + query helpers for ``item_price_history``."""

    async def upsert_price_snapshot(self, prices: dict[str, float], captured_at: str) -> int:
        """Insert one row per item_code. Returns the number of rows inserted."""
        rows = [
            {"item_code": code, "price": float(price), "captured_at": captured_at}
            for code, price in prices.items()
            if isinstance(price, (int, float))
        ]
        if not rows:
            return 0
        await self._conn.executemany(
            "INSERT OR IGNORE INTO item_price_history (item_code, price, captured_at) "
            "VALUES (:item_code, :price, :captured_at)",
            rows,
        )
        await self._conn.commit()
        return len(rows)

    async def fetch_price_history(
        self, item_code: str, since_iso: Optional[str] = None
    ) -> list[dict]:
        """Return ``[{captured_at, price}, ...]`` for *item_code*, oldest first."""
        sql = "SELECT captured_at, price FROM item_price_history WHERE item_code = ?"
        params: list = [item_code]
        if since_iso:
            sql += " AND captured_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY captured_at ASC"
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [{"captured_at": r[0], "price": r[1]} for r in rows]

    async def price_at_or_before(self, item_code: str, cutoff_iso: str) -> Optional[float]:
        """Return the latest known price for *item_code* at or before *cutoff_iso*."""
        async with self._conn.execute(
            "SELECT price FROM item_price_history "
            "WHERE item_code = ? AND captured_at <= ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (item_code, cutoff_iso),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else None
