"""
This module defines the Battles cog, which provides commands to set battle priorities with links in a Discord server.
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role


class BattlePrioritiesModal(discord.ui.Modal, title="Battle Priorities"):
    """Modal for setting battle priorities with pre-filled values."""

    prio1 = discord.ui.TextInput(
        label="Priority 1: Name", required=False, placeholder="e.g., Enemy HQ"
    )
    link1 = discord.ui.TextInput(
        label="Priority 1: Link", required=False, placeholder="https://..."
    )

    prio2 = discord.ui.TextInput(
        label="Priority 2: Name", required=False, placeholder="e.g., Border Region"
    )
    link2 = discord.ui.TextInput(
        label="Priority 2: Link", required=False, placeholder="https://..."
    )

    def __init__(
        self,
        bot,
        prio1: Optional[str] = None,
        link1: Optional[str] = None,
        prio2: Optional[str] = None,
        link2: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.bot = bot

        # Pre-fill with previous values if provided
        if prio1:
            self.prio1.default = prio1
        if link1:
            self.link1.default = link1
        if prio2:
            self.prio2.default = prio2
        if link2:
            self.link2.default = link2

    async def on_submit(self, interaction: discord.Interaction):
        description = ""
        if self.prio1.value and self.link1.value:
            description += f"1️⃣: **[{self.prio1.value}]({self.link1.value})**\n"
        if self.prio2.value and self.link2.value:
            description += f"2️⃣: **[{self.prio2.value}]({self.link2.value})**\n"

        if not description:
            await interaction.response.send_message(
                "Please provide at least one priority.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Battle Prioriteiten",
            description=description.rstrip("\n"),
            color=int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16),
        )
        channel = interaction.guild.get_channel(self.bot.config["channels"]["orders"])
        await channel.send(embed=embed)
        await interaction.response.send_message(
            "Your battle priorities have been submitted.", ephemeral=True
        )


class Battles(commands.Cog, name="battles"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.last_priorities = {}

    @app_commands.command(
        name="priorities",
        description="Set battle priorities with links.",
    )
    @has_privileged_role()
    async def set_priorities(self, interaction: discord.Interaction) -> None:
        """
        Open a modal to set battle priorities.
        """
        guild_id = interaction.guild.id
        last = self.last_priorities.get(guild_id, {})

        modal = BattlePrioritiesModal(
            self.bot,
            prio1=last.get("prio1"),
            link1=last.get("link1"),
            prio2=last.get("prio2"),
            link2=last.get("link2"),
        )
        await interaction.response.send_modal(modal)

        # Wait for submission and store the values
        await modal.wait()
        self.last_priorities[guild_id] = {
            "prio1": modal.prio1.value,
            "link1": modal.link1.value,
            "prio2": modal.prio2.value,
            "link2": modal.link2.value,
        }


async def setup(bot) -> None:
    """Add the Battles cog to the bot."""
    await bot.add_cog(Battles(bot))
