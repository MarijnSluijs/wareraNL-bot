"""Background task: ping the Minister role with "Bunkers" every 8–14 hours.

The interval is randomised after each send so the timing is unpredictable.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Production
_PROD_CHANNEL_ID = 1489316733528576080
_PROD_ROLE_ID    = 1458527742646816892

# Testing
_TEST_CHANNEL_ID = 1474452856584011929
_TEST_ROLE_ID    = 1494431123391119531

_MIN_HOURS = 8
_MAX_HOURS = 14


class BunkersTasks(TaskCogBase, name="bunkers_tasks"):
    """Sends a randomised-interval "Bunkers" ping to the Minister role."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._next_send_at: float | None = None  # Unix timestamp

    def cog_load(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._bunkers_loop())

    def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def _bunkers_loop(self) -> None:
        await self._wait_for_services()

        while True:
            # Choose a random delay between 8 and 14 hours
            delay_seconds = random.uniform(_MIN_HOURS * 3600, _MAX_HOURS * 3600)
            self._next_send_at = time.time() + delay_seconds
            logger.info(
                "bunkers: next ping in %.1f hours", delay_seconds / 3600
            )
            await asyncio.sleep(delay_seconds)
            self._next_send_at = None

            try:
                await self._send_bunkers()
            except Exception:
                logger.exception("bunkers: failed to send ping")

    async def _send_bunkers(self) -> None:
        testing: bool = getattr(self.bot, "testing", False)

        channel_id = _TEST_CHANNEL_ID if testing else _PROD_CHANNEL_ID
        role_id    = _TEST_ROLE_ID    if testing else _PROD_ROLE_ID

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning("bunkers: channel %d not found in cache", channel_id)
            return

        guild = channel.guild
        role = guild.get_role(role_id)
        if role is None:
            logger.warning("bunkers: role %d not found in guild %s", role_id, guild.id)
            return

        await channel.send(
            f"{role.mention} Bunkers",
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
        logger.info("bunkers: sent ping to %s in #%s", role.name, channel.name)

    @app_commands.command(
        name="volgende_bunkers",
        description="Toon wanneer het volgende Bunkers-bericht wordt gestuurd",
    )
    async def volgende_bunkers(self, interaction: discord.Interaction) -> None:
        if self._next_send_at is None:
            await interaction.response.send_message(
                "⏳ De volgende Bunkers-ping is nog niet gepland.", ephemeral=True
            )
            return
        ts = int(self._next_send_at)
        await interaction.response.send_message(
            f"🏛️ Volgende **Bunkers**-ping: <t:{ts}:F> (<t:{ts}:R>)",
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(BunkersTasks(bot))
