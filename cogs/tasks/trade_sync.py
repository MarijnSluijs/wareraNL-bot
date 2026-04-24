"""Background task: hourly itemMarket trade sync.

Polls the 100 most-recent ``itemMarket`` transactions from Warera and
stores new ones in the ``item_trades`` table. Used by ``/marktprijs``
to estimate prices for similar items.
"""

from __future__ import annotations

import logging
import time

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")


def _unwrap(resp) -> dict | list:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


class TradeSyncTask(TaskCogBase, name="trade_sync"):
    """Hourly sync of recent itemMarket trades."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.sync_trades.start()

    def cog_unload(self) -> None:
        self.sync_trades.cancel()

    @tasks.loop(hours=1)
    async def sync_trades(self) -> None:
        client = self._client
        db = self._db
        if client is None or db is None:
            logger.warning("[trade_sync] services not ready; skipping tick")
            return

        t0 = time.monotonic()
        try:
            raw = await client.post(
                "/transaction.getPaginatedTransactions",
                json={"limit": 100, "transactionType": "itemMarket"},
            )
        except Exception as exc:
            logger.warning("[trade_sync] fetch error: %s", exc)
            return

        data = _unwrap(raw) if isinstance(raw, dict) else {}
        items: list = []
        if isinstance(data, dict):
            items = (
                data.get("items")
                or data.get("transactions")
                or data.get("results")
                or []
            )
        elif isinstance(data, list):
            items = data

        if not items:
            logger.info("[trade_sync] no trades returned")
            return

        try:
            inserted, seen = await db.upsert_trades(items)
        except Exception as exc:
            logger.exception("[trade_sync] upsert error: %s", exc)
            return

        elapsed = time.monotonic() - t0
        logger.info(
            "[trade_sync] %d new / %d seen in %.2fs", inserted, seen, elapsed
        )

    @sync_trades.before_loop
    async def _before(self) -> None:
        await self._wait_for_services()


async def setup(bot) -> None:
    await bot.add_cog(TradeSyncTask(bot))
