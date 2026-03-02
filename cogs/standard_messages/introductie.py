"""
This module defines the Introductie cog, which provides commands to post a series of embeds as an introduction in a Discord channel. 
"""

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.standard_messages.generate import GenerateEmbeds


class Introductie(GenerateEmbeds, name="introductie"):
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.load_json("templates/introductie.json")

    @commands.hybrid_command(
        name="introductie",
        description="Post de introductie in het huidige kanaal.",
    )
    @commands.has_permissions(manage_messages=True)
    async def introductie(self, context: Context) -> None:
        """
        Post de introductie als een reeks embeds.

        :param context: The hybrid command context.
        """
        if not self.json_data or not self.json_data.get("embeds"):
            embed = discord.Embed(
                description="Guide data niet gevonden. Gebruik `/reloadguide` om opnieuw te laden.",
                color=self.get_color("error"),
            )
            await context.send(embed=embed, ephemeral=True)
            return

        # Send confirmation
        await context.send("📚 Bezig met posten van de introductie...", ephemeral=True)

        # Send all embeds
        for embed_data in self.json_data["embeds"]:
            try:
                embed = self.create_embed_from_data(embed_data)
                await context.channel.send(embed=embed)
            except Exception as e:
                self.bot.logger.error(f"Error sending embed: {e}")

        self.bot.logger.info(
            f"Introductie posted by {context.author} in {context.channel.name}"
        )

    @commands.hybrid_command(
        name="reloadintroductie",
        description="Herlaad de introductie JSON file.",
    )
    @commands.is_owner()
    async def reloadintroductie(self, context: Context) -> None:
        """
        Reload the introductie from the JSON file.
        :param context: The hybrid command context.
        """
        try:
            self.load_json("templates/introductie.json")
            embed = discord.Embed(
                description=f"✅ Beginner guide succesvol herladen! ({len(self.json_data.get('embeds', []))} embeds)",
                color=self.get_color("success"),
            )
            await context.send(embed=embed)
            self.bot.logger.info(f"Beginner guide reloaded by {context.author}")
        except Exception as e:
            embed = discord.Embed(
                description=f"❌ Fout bij herladen: {e}", color=self.get_color("error")
            )
            await context.send(embed=embed)


async def setup(bot) -> None:
    """Add the Introductie cog to the bot."""
    await bot.add_cog(Introductie(bot))
