"""
General bot commands — /help, /botinfo, /serverinfo, /ping, /invite,
/eight_ball (question), and /feedback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot import DiscordBot


class Giveaways(commands.Cog, name="giveaways"):
    """Cog for giveaway-related commands and interactions."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot
        self.config = getattr(self.bot, "config", {}) or {}

    @app_commands.command(name="reward", description="Geef gems aan een gebruiker.")
    @app_commands.describe(
        id="De WarEra ID van de gebruiker aan wie je de gems wilt geven.",
        amount="Het aantal gems dat je wilt geven.",
    )
    async def reward(
        self, interaction: discord.Interaction, id: str, amount: int
    ) -> None:
        """Geef gems aan een gebruiker."""
        if not any(
            role.id == self.config.get("roles", {}).get("community_manager")
            for role in interaction.user.roles
        ):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
            return

        try:
            await self.bot.db.store_reward(user_id=id, reward_amount=amount)
            await interaction.response.send_message(
                f"✅ Successfully rewarded {amount} gems to user ID {id}.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Failed to reward gems: {e}", ephemeral=True
            )


async def setup(bot) -> None:
    """Add the General cog to the bot."""
    await bot.add_cog(Giveaways(bot))
