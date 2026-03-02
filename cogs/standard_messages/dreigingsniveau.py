"""
This module defines the dreiging cog, which provides commands to post a series of embeds explaining the threat levels in a Discord channel.
"""

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.standard_messages.generate import GenerateEmbeds


class dreiging(GenerateEmbeds, name="dreiging"):
    """A cog that provides commands to post a series of embeds explaining the threat levels."""
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.load_json("templates/dreigingsniveau.json")

    @commands.hybrid_command(
        name="dreigingsniveau",
        description="Post de uitleg van de dreigingsniveau's.",
    )
    @commands.has_permissions(manage_messages=True)
    async def dreigingsniveau(self, context: Context) -> None:
        """
        Post de uitleg van de dreigingsniveau's als een embed.

        :param context: The hybrid command context.
        """
        if not self.json_data or not self.json_data.get("embeds"):
            embed = discord.Embed(
                description="Dreigingsniveau data niet gevonden.",
                color=self.get_color("error"),
            )
            await context.send(embed=embed, ephemeral=True)
            return

        # Send confirmation
        await context.send(
            "📚 Bezig met posten van de dreigingsniveau uitleg...", ephemeral=True
        )

        # Send all embeds
        for embed_data in self.json_data["embeds"]:
            try:
                embed = self.create_embed_from_data(embed_data)
                await context.channel.send(embed=embed)
            except Exception as e:
                self.bot.logger.error(f"Error sending embed: {e}")

        self.bot.logger.info(
            f"Dreigingsniveau uitleg posted by {context.author} in {context.channel.name}"
        )


async def setup(bot) -> None:
    """Add the dreiging cog to the bot."""
    await bot.add_cog(dreiging(bot))
