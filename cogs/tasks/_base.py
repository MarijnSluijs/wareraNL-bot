"""Base class shared by all background-task cogs.

Safe access to shared services (initialized by coordinator.py) via bot attributes.
"""

from __future__ import annotations

import asyncio

from discord.ext import commands


class TaskCogBase(commands.Cog):
    """Mixin that provides safe access to shared services.

    Services are initialized by ``cogs/tasks/coordinator.py`` and stored on
    the bot instance.  All task cogs inherit from this class.
    """

    @property
    def _db(self):
        return getattr(self.bot, "_ext_db", None)

    @property
    def _client(self):
        return getattr(self.bot, "_ext_client", None)

    @property
    def _citizen_cache(self):
        return getattr(self.bot, "_ext_citizen_cache", None)

    @property
    def _heavy_api_lock(self) -> asyncio.Lock:
        return getattr(self.bot, "_ext_heavy_api_lock", None)

    @property
    def config(self) -> dict:
        return getattr(self.bot, "config", {})

    async def _wait_for_services(self) -> None:
        """Block until the coordinator has finished initializing all services.

        Task ``before_loop`` handlers should call this instead of (or after)
        ``await self.bot.wait_until_ready()``.
        """
        await self.bot.wait_until_ready()
        # Wait until the coordinator has set up DB / API client
        while not hasattr(self.bot, "_ext_services_ready"):
            await asyncio.sleep(0.5)
        await self.bot._ext_services_ready.wait()
