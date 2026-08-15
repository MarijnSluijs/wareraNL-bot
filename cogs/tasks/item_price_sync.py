"""Background task: hourly item-price snapshot.

Polls ``itemTrading.getPrices`` (current market price for every fungible
resource: iron, bread, ammo, cases, ...) and stores one row per item in
``item_price_history``. The WarEra API only exposes the *current* price,
not history, so this is what powers the price chart on the website's
/markt/items pages.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")


def _unwrap(resp) -> dict:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


class ItemPriceSyncTask(TaskCogBase, name="item_price_sync"):
    """Hourly snapshot of itemTrading.getPrices."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.sync_prices.start()

    def cog_unload(self) -> None:
        self.sync_prices.cancel()

    @tasks.loop(hours=1)
    async def sync_prices(self) -> None:
        client = self._client
        db = self._db
        if client is None or db is None:
            logger.warning("[item_price_sync] services not ready; skipping tick")
            return

        t0 = time.monotonic()
        try:
            raw = await client.get("/itemTrading.getPrices")
        except Exception as exc:
            logger.warning("[item_price_sync] fetch error: %s", exc)
            return

        prices = _unwrap(raw)
        if not isinstance(prices, dict) or not prices:
            logger.info("[item_price_sync] no prices returned")
            return

        captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            n = await db.upsert_price_snapshot(prices, captured_at)
        except Exception as exc:
            logger.exception("[item_price_sync] upsert error: %s", exc)
            return

        elapsed = time.monotonic() - t0
        logger.info("[item_price_sync] stored %d item prices in %.2fs", n, elapsed)

    @sync_prices.before_loop
    async def _before(self) -> None:
        await self._wait_for_services()


async def setup(bot) -> None:
    await bot.add_cog(ItemPriceSyncTask(bot))
