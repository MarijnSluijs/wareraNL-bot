"""Background task: watch for countries that set the Netherlands as their sworn enemy.

Every 15 minutes, fetches country.getAllCountries and checks which countries have
``enemy == NL_COUNTRY_ID``.  When a country newly sets the Netherlands as enemy,
the Bunkerslaaf role is pinged in the configured channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Production channel where the Bunkerslaaf ping is sent (same as bunkers.py)
_PROD_CHANNEL_ID = 1489316733528576080
_PROD_ROLE_ID    = 1494815320316055573

# Testing
_TEST_CHANNEL_ID = 1474452856584011929
_TEST_ROLE_ID    = 1494815008909955257

_NL_COUNTRY_ID = "6813b6d446e731854c7ac7a0"

# DB key used to persist the known set of enemy countries
_DB_KEY = "enemy_watcher_known_enemies"

# Poll interval in minutes
_POLL_INTERVAL = 15


def _extract_country_list(resp) -> list[dict]:
    """Extract the list of countries from an API response."""
    if isinstance(resp, dict):
        data = resp.get("result", {}).get("data", resp)
        if isinstance(data, list):
            return data
    if isinstance(resp, list):
        return resp
    return []


class EnemyWatcherTasks(TaskCogBase, name="enemy_watcher_tasks"):
    """Watches for new countries that set Netherlands as their sworn enemy."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.enemy_watch_loop.start()

    def cog_unload(self) -> None:
        self.enemy_watch_loop.cancel()

    @tasks.loop(minutes=_POLL_INTERVAL)
    async def enemy_watch_loop(self) -> None:
        try:
            await self._check_enemies()
        except Exception:
            logger.exception("enemy_watcher: unexpected error in loop")

    @enemy_watch_loop.before_loop
    async def before_enemy_watch_loop(self) -> None:
        await self._wait_for_services()

    async def _check_enemies(self) -> None:
        """Fetch country data, compare enemy lists, and ping if new enemies found."""
        if not self._client:
            return

        nl_country_id: str = self.bot.config.get("nl_country_id", _NL_COUNTRY_ID)

        try:
            resp = await asyncio.wait_for(
                self._client.get("/country.getAllCountries"),
                timeout=20.0,
            )
        except Exception as exc:
            logger.warning("enemy_watcher: failed to fetch countries: %s", exc)
            return

        country_list = _extract_country_list(resp)
        if not country_list:
            logger.debug("enemy_watcher: empty country list")
            return

        # Build current set of country IDs that have NL as sworn enemy
        current_enemies: dict[str, str] = {}  # country_id → country_name
        for country in country_list:
            if not isinstance(country, dict):
                continue
            if country.get("enemy") == nl_country_id:
                cid = country.get("_id") or country.get("id") or ""
                cname = country.get("name") or cid
                if cid:
                    current_enemies[cid] = cname

        # Load previously known enemies from DB
        # `first_run` is True when the DB has never stored a value yet.
        # On first run we only initialise the state without pinging, so that
        # we don't flood the channel with enemies that were already set before
        # the bot started.
        first_run = True
        previously_known: set[str] = set()
        if self._db:
            try:
                stored = await self._db.get_poll_state(_DB_KEY)
                if stored is not None:
                    first_run = False
                    previously_known = set(json.loads(stored))
            except Exception:
                logger.exception("enemy_watcher: failed to load known enemies from DB")

        if first_run:
            logger.info(
                "enemy_watcher: first run — initialising state with %d enemies, no ping sent",
                len(current_enemies),
            )

        # Determine new enemies (countries that weren't in the previous set)
        new_enemies = {
            cid: name
            for cid, name in current_enemies.items()
            if cid not in previously_known
        }

        if new_enemies and not first_run:
            logger.info(
                "enemy_watcher: %d new sworn enemy/enemies detected: %s",
                len(new_enemies),
                list(new_enemies.values()),
            )
            try:
                await self._notify_new_enemies(new_enemies)
            except Exception:
                logger.exception("enemy_watcher: failed to send notification")

        # Persist the current enemy set to DB
        if self._db:
            try:
                await self._db.set_poll_state(
                    _DB_KEY, json.dumps(list(current_enemies.keys()))
                )
            except Exception:
                logger.exception("enemy_watcher: failed to save known enemies to DB")

    async def _notify_new_enemies(self, new_enemies: dict[str, str]) -> None:
        """Ping the Bunkerslaaf role for each new sworn enemy."""
        testing: bool = getattr(self.bot, "testing", False)
        channel_id = _TEST_CHANNEL_ID if testing else _PROD_CHANNEL_ID
        role_id    = _TEST_ROLE_ID    if testing else _PROD_ROLE_ID

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning(
                "enemy_watcher: channel %d not found in cache", channel_id
            )
            return

        guild = channel.guild
        role = guild.get_role(role_id)
        role_mention = role.mention if role else f"<@&{role_id}>"

        for country_id, country_name in new_enemies.items():
            country_url = f"https://app.warera.io/country/{country_id}"
            embed = discord.Embed(
                title="⚔️ Nieuwe gezworen vijand van Nederland!",
                description=(
                    f"**[{country_name}]({country_url})** heeft de Nederlanden"
                    " als **gezworen vijand** aangewezen."
                ),
                colour=discord.Colour.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await channel.send(
                content=role_mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            logger.info(
                "enemy_watcher: notified about new enemy %s (%s)",
                country_name, country_id,
            )


async def setup(bot) -> None:
    await bot.add_cog(EnemyWatcherTasks(bot))
