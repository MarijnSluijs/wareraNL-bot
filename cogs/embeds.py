"""
Embed creator — !embed command (owner-only) to compose and post a rich embed
to a selected channel via an interactive modal.
"""

import discord
from discord.ext import commands
from discord.ext.commands import Context


class EmbedModal(discord.ui.Modal, title="Create Embed"):
    """Modal dialog for creating an embed message with multi-line support."""
    def __init__(self, bot, selected_channel: discord.TextChannel) -> None:
        super().__init__()
        self.bot = bot
        self.selected_channel = selected_channel

    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.long,  # Multi-line text box
        placeholder="Type your message here...",
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            description=str(self.message),
            color=int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16),
        )

        try:
            await self.selected_channel.send(embed=embed)
            await interaction.response.send_message(
                f"✅ Embed posted to {self.selected_channel.mention}", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I don't have permission to post in {self.selected_channel.mention}",
                ephemeral=True,
            )


class ChannelSelectView(discord.ui.View):
    """View for selecting a channel to post the embed."""
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select a channel to post the embed",
        channel_types=[discord.ChannelType.text],
    )
    async def channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ):
        channel_id = select.values[0].id
        selected_channel = interaction.guild.get_channel(channel_id)
        modal = EmbedModal(self.bot, selected_channel)
        await interaction.response.send_modal(modal)


class Embeds(commands.Cog, name="embeds"):
    """Cog for the !embed command, allowing the bot owner to create and post rich embeds via an interactive modal."""
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="embed",
        description="Create an embed message with multi-line support.",
    )
    @commands.is_owner()
    async def embed(self, context: Context) -> None:
        """
        Opens a channel selector, then a modal to create an embed.

        :param context: The hybrid command context.
        """
        view = ChannelSelectView(self.bot)
        await context.interaction.response.send_message(
            "Select a channel to post the embed:", view=view, ephemeral=True
        )


async def setup(bot) -> None:
    """Add the Embeds cog to the bot."""
    await bot.add_cog(Embeds(bot))
