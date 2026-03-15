# pylint: disable=arguments-differ
"""
This module defines the Battles cog, which provides commands to set battle priorities with links in a Discord server.
"""

from typing import Optional
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

class BattlePrioritiesStep1Modal(discord.ui.Modal, title="Battle Priorities (1/2)"):
    """First modal step collecting priorities 1 and 2."""

    prio1 = discord.ui.TextInput(
        label="Priority 1: Name", required=False, placeholder="Gevecht om Uppland (NL - SE)"
    )
    link1 = discord.ui.TextInput(
        label="Priority 1: Link", required=False, placeholder="https://..."
    )

    prio2 = discord.ui.TextInput(
        label="Priority 2: Name", required=False, placeholder="Gevecht om Rhine (BE - DE)"
    )
    link2 = discord.ui.TextInput(
        label="Priority 2: Link", required=False, placeholder="https://..."
    )

    def __init__(
        self,
        bot,
        battles_cog,
        prio1: Optional[str] = None,
        link1: Optional[str] = None,
        prio2: Optional[str] = None,
        link2: Optional[str] = None,
        prio3: Optional[str] = None,
        link3: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.battles_cog = battles_cog
        self.prio3_default = prio3
        self.link3_default = link3

        self.bot.logger.info("Initializing BattlePrioritiesStep1Modal")
        if prio1:
            self.prio1.default = prio1
        if link1:
            self.link1.default = link1
        if prio2:
            self.prio2.default = prio2
        if link2:
            self.link2.default = link2

    async def on_submit(self, interaction: discord.Interaction):
        self.bot.logger.info(
            "Battle priorities step 1 submitted: %s, %s, %s, %s",
            self.prio1.value,
            self.link1.value,
            self.prio2.value,
            self.link2.value,
        )

        guild_id = interaction.guild.id if interaction.guild else None
        user_id = interaction.user.id
        pending_key = self.battles_cog.build_pending_key(guild_id, user_id)
        self.battles_cog.pending_priorities[pending_key] = {
            "partial_data": {
                "prio1": self.prio1.value,
                "link1": self.link1.value,
                "prio2": self.prio2.value,
                "link2": self.link2.value,
            },
            "prio3": self.prio3_default,
            "link3": self.link3_default,
        }

        await interaction.response.send_message(
            "Step 1 saved. Click Continue to fill Priority 3, or Skip to submit now.",
            ephemeral=True,
            view=BattlePrioritiesStep2LauncherView(self.bot, self.battles_cog, guild_id, user_id),
        )


class BattlePrioritiesStep2LauncherView(discord.ui.View):
    """Ephemeral view that launches the second modal step."""

    def __init__(self, bot, battles_cog, guild_id: Optional[int], user_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.battles_cog = battles_cog
        self.guild_id = guild_id
        self.user_id = user_id

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This button is only for the user who started this flow.", ephemeral=True
            )
            return

        pending_key = self.battles_cog.build_pending_key(self.guild_id, self.user_id)
        pending = self.battles_cog.pending_priorities.pop(pending_key, None)
        if not pending:
            await interaction.response.send_message(
                "This session expired. Run /priorities again.", ephemeral=True
            )
            return

        step2_modal = BattlePrioritiesStep2Modal(
            self.bot,
            self.battles_cog,
            partial_data=pending["partial_data"],
            prio3=pending.get("prio3"),
            link3=pending.get("link3"),
        )
        await interaction.response.send_modal(step2_modal)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This button is only for the user who started this flow.", ephemeral=True
            )
            return

        pending_key = self.battles_cog.build_pending_key(self.guild_id, self.user_id)
        pending = self.battles_cog.pending_priorities.pop(pending_key, None)
        if not pending:
            await interaction.response.send_message(
                "This session expired. Run /priorities again.", ephemeral=True
            )
            return

        payload = {
            **pending["partial_data"],
            "prio3": "",
            "link3": "",
        }
        await self.battles_cog.submit_priorities(interaction, payload)


class BattlePrioritiesStep2Modal(discord.ui.Modal, title="Battle Priorities (2/2)"):
    """Second modal step collecting priority 3 and finalizing output."""

    prio3 = discord.ui.TextInput(
        label="Priority 3: Name", required=False, placeholder="Gevecht om Luxemburg (LU - FR)"
    )
    link3 = discord.ui.TextInput(
        label="Priority 3: Link", required=False, placeholder="https://..."
    )

    def __init__(
        self,
        bot,
        battles_cog,
        partial_data: dict,
        prio3: Optional[str] = None,
        link3: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.battles_cog = battles_cog
        self.partial_data = partial_data

        self.bot.logger.info("Initializing BattlePrioritiesStep2Modal")
        if prio3:
            self.prio3.default = prio3
        if link3:
            self.link3.default = link3

    async def on_submit(self, interaction: discord.Interaction):
        payload = {
            **self.partial_data,
            "prio3": self.prio3.value,
            "link3": self.link3.value,
        }

        self.bot.logger.info(
            "Battle priorities step 2 submitted: %s, %s", self.prio3.value, self.link3.value
        )
        await self.battles_cog.submit_priorities(interaction, payload)


class Battles(commands.Cog, name="battles"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.last_priorities = {}
        self.pending_priorities = {}

    @staticmethod
    def build_pending_key(guild_id: Optional[int], user_id: int) -> str:
        return f"{guild_id or 'dm'}:{user_id}"

    async def submit_priorities(self, interaction: discord.Interaction, payload: dict) -> None:
        description = ""
        if payload.get("prio1") and payload.get("link1"):
            description += f"1️⃣: **[{payload['prio1']}]({payload['link1']})**\n\n"
        if payload.get("prio2") and payload.get("link2"):
            description += f"2️⃣: **[{payload['prio2']}]({payload['link2']})**\n\n"
        if payload.get("prio3") and payload.get("link3"):
            description += f"3️⃣: **[{payload['prio3']}]({payload['link3']})**\n\n"

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
        await interaction.channel.send(embed=embed)

        guild_id = interaction.guild.id if interaction.guild else None
        if guild_id is not None:
            self.last_priorities[guild_id] = payload

        await interaction.response.send_message(
            "Your battle priorities have been submitted.", ephemeral=True
        )

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

        modal = BattlePrioritiesStep1Modal(
            self.bot,
            self,
            prio1=last.get("prio1"),
            link1=last.get("link1"),
            prio2=last.get("prio2"),
            link2=last.get("link2"),
            prio3=last.get("prio3"),
            link3=last.get("link3"),
        )
        self.bot.logger.info(
            "Opening battle priorities step 1 modal with pre-filled values: %s", last
        )
        await interaction.response.send_modal(modal)


async def setup(bot) -> None:
    """Add the Battles cog to the bot."""
    await bot.add_cog(Battles(bot))
