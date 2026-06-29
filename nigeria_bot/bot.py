"""Nigerian WarEra Discord — verification bot.

Handles verification of Nigerian citizens, Dutch citizens, and embassy members.
Run with: python nigeria_bot/bot.py

Required environment variables:
  TOKEN_NIGERIA   Discord bot token for this application.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands

# Allow importing from the project root (services/, etc.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("nigeria_bot")


class NigeriaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        # Open persistent DB
        from nigeria_bot.db import open_db
        self.nigeria_db = await open_db()

        # Load the verification cog
        import nigeria_bot.cog as cog_module
        await self.add_cog(cog_module.VerificationCog(self, self.nigeria_db))

        # Register persistent views so buttons survive restarts
        from nigeria_bot.cog import VerificationView, TicketActionView
        self.add_view(VerificationView())
        self.add_view(TicketActionView())

        # Sync slash commands to the guild
        from nigeria_bot.cog import GUILD_ID
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Slash commands synced to guild %d", GUILD_ID)

    async def on_ready(self) -> None:
        logger.info("Nigeria bot ready — logged in as %s", self.user)


def main() -> None:
    token = os.environ.get("TOKEN_NIGERIA")
    if not token:
        logger.error("TOKEN_NIGERIA environment variable not set")
        sys.exit(1)

    bot = NigeriaBot()
    asyncio.run(bot.start(token))


if __name__ == "__main__":
    main()
