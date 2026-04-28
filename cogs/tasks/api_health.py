"""Background task: hourly API health check with bot-mededelingen alerts.

Every hour, probes the WarEra API to check availability.  When the API
transitions from available → unavailable (or is down on first check) an alert
is sent to the bot-mededelingen channel.  When the API recovers a follow-up
recovery message is sent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")


class ApiHealthTasks(TaskCogBase, name="api_health_tasks"):
    """Sends alerts to bot-mededelingen when the API goes down or recovers."""

    def __init__(self, bot) -> None:
        self.bot = bot
        # None = not yet checked, True = was up, False = was down
        self._last_known_available: bool | None = None

    def cog_load(self) -> None:
        self.api_health_loop.start()

    def cog_unload(self) -> None:
        self.api_health_loop.cancel()

    @tasks.loop(hours=1)
    async def api_health_loop(self) -> None:
        try:
            await self._check_api_health()
        except Exception:
            logger.exception("api_health: unexpected error in loop")

    @api_health_loop.before_loop
    async def before_api_health_loop(self) -> None:
        await self._wait_for_services()

    async def _check_api_health(self) -> None:
        if not self._client:
            return

        # Probe with a lightweight endpoint
        now_available: bool
        try:
            await asyncio.wait_for(
                self._client.get("/country.getAllCountries"),
                timeout=20.0,
            )
            now_available = True
        except Exception:
            now_available = False

        prev = self._last_known_available
        self._last_known_available = now_available

        if now_available == prev:
            # No state change — nothing to report
            return

        if not now_available:
            # Transition to down (including first-check-is-down)
            logger.warning("api_health: API is now UNAVAILABLE — sending alert")
            await self._send_alert(up=False)
        elif prev is False:
            # Recovered from down state
            logger.info("api_health: API has RECOVERED — sending recovery message")
            await self._send_alert(up=True)
        # If prev was None and now_available is True → normal startup, no alert

    async def _send_alert(self, *, up: bool) -> None:
        channel_id = self.config.get("channels", {}).get("bot_mededelingen")
        if not channel_id:
            logger.warning("api_health: bot_mededelingen channel not configured")
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning("api_health: channel %s not found in cache", channel_id)
            return

        if up:
            embed = discord.Embed(
                title="✅ WarEra API hersteld",
                description="De WarEra API is weer **beschikbaar**. Commando's werken normaal.",
                colour=discord.Colour.green(),
                timestamp=datetime.now(timezone.utc),
            )
        else:
            embed = discord.Embed(
                title="🔌 WarEra API offline",
                description=(
                    "De WarEra API is momenteel **niet beschikbaar**.\n"
                    "Commando's die realtime API-data nodig hebben werken tijdelijk niet.\n"
                    "Er wordt automatisch een bericht gestuurd zodra de API hersteld is."
                ),
                colour=discord.Colour.red(),
                timestamp=datetime.now(timezone.utc),
            )

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.warning("api_health: failed to send alert: %s", exc)


async def setup(bot) -> None:
    await bot.add_cog(ApiHealthTasks(bot))
