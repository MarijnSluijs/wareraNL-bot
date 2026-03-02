"""Service coordinator — initializes shared bot services.

Creates and attaches the following attributes to the bot instance so that all
cog modules can access shared resources without creating their own connections:

  - ``_ext_db``               – :class:`services.db.Database` instance
  - ``_ext_client``           – :class:`services.api_client.APIClient` instance
  - ``_ext_citizen_cache``    – :class:`services.citizen_cache.CitizenCache` instance
  - ``_ext_heavy_api_lock``   – :class:`asyncio.Lock` for serializing heavy API sweeps
  - ``_ext_prod_poll_lock``   – :class:`asyncio.Lock` for serializing production polls
  - ``_ext_services_ready``   – :class:`asyncio.Event` set once all services are up
"""

from __future__ import annotations

import asyncio
import json
import logging

from discord.ext import commands

from services.api_client import APIClient
from services.citizen_cache import CitizenCache
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list
from services.db import Database

logger = logging.getLogger("discord_bot")


class ServiceCoordinator(commands.Cog, name="service_coordinator"):
    def __init__(self, bot) -> None:
        self.bot = bot
        # Create the ready event synchronously so task cogs can find it
        # even if they load (and queue their before_loops) before this cog's
        # cog_load has run.
        if not hasattr(bot, "_ext_services_ready"):
            bot._ext_services_ready = asyncio.Event()

    def cog_load(self) -> None:
        asyncio.create_task(self._ensure_services())

    def cog_unload(self) -> None:
        event = getattr(self.bot, "_ext_services_ready", None)
        if event:
            event.clear()
        client = getattr(self.bot, "_ext_client", None)
        if client:
            asyncio.create_task(client.close())
        db = getattr(self.bot, "_ext_db", None)
        if db:
            asyncio.create_task(db.close())

    async def _ensure_services(self) -> None:
        """Initialize DB, API client, and citizen cache; then signal readiness."""
        config = self.bot.config
        base_url = config.get("api_base_url", "https://api.example.local")
        db_path = config.get("external_db_path", "database/external.db")

        api_keys = None
        try:
            with open("_api_keys.json", "r") as kf:
                api_keys = json.load(kf).get("keys", [])
        except Exception:
            logger.debug("No _api_keys.json found, continuing without API keys")

        client = APIClient(base_url=base_url, api_keys=api_keys)
        await client.start()
        db = Database(db_path)
        await db.setup()

        citizen_cache = CitizenCache(client, db)

        self.bot._ext_client = client
        self.bot._ext_db = db
        self.bot._ext_citizen_cache = citizen_cache
        self.bot._ext_heavy_api_lock = asyncio.Lock()
        self.bot._ext_prod_poll_lock = asyncio.Lock()

        # Expose for backward-compat (geluk.py and others reference _ext_db)
        self.bot._ext_db = db

        self.bot._ext_services_ready.set()
        logger.info("Services initialized — DB=%s, API=%s", db_path, base_url)

        # If NL citizen cache is empty, run a full initial fill in the background
        asyncio.create_task(self._initial_citizen_fill_if_needed())

    async def _initial_citizen_fill_if_needed(self) -> None:
        """Run a full citizen refresh on first boot if the NL cache is empty."""
        await self.bot.wait_until_ready()
        db = self.bot._ext_db
        client = self.bot._ext_client
        citizen_cache = self.bot._ext_citizen_cache
        config = self.bot.config

        nl_country_id = config.get("nl_country_id")
        if not nl_country_id:
            return
        try:
            counts, _, _ = await db.get_level_distribution(nl_country_id)
            if counts:
                return  # already populated
            logger.info("citizen_cache: DB empty on startup — running initial fill")
        except Exception:
            logger.exception("_initial_citizen_fill_if_needed: DB check failed")
            return
        try:
            all_countries = await client.get("/country.getAllCountries")
            country_list = extract_country_list(all_countries)
            lock = self.bot._ext_heavy_api_lock
            async with lock:
                for country in country_list:
                    cid = cid_of(country)
                    name = country.get("name", cid)
                    try:
                        await citizen_cache.refresh_country(cid, name)
                    except Exception:
                        logger.exception(
                            "_initial_citizen_fill_if_needed: error for %s", name
                        )
            from datetime import datetime, timezone

            await db.set_poll_state(
                "citizen_refresh_last_run",
                datetime.now(timezone.utc).isoformat(),
            )
            logger.info("citizen_cache: initial fill complete")
        except Exception:
            logger.exception("_initial_citizen_fill_if_needed: fill failed")


async def setup(bot) -> None:
    """Add the ServiceCoordinator cog to the bot."""
    await bot.add_cog(ServiceCoordinator(bot))
