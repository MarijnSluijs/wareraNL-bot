"""
This module provides functionality to generate Discord embeds based on JSON data. 
"""

import json

import discord
from discord.ext import commands


class GenerateEmbeds(commands.Cog, name="generate_embeds"):
    """A base cog that provides methods to load embed data from a JSON file and create Discord embeds accordingly."""
    def __init__(self, bot) -> None:
        self.bot = bot
        self.json_data = None

    def load_json(self, json_path) -> None:
        """Load the embed info from JSON file"""
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                self.json_data = json.load(f)
                self.bot.logger.info(f"JSON data loaded successfully from {json_path}")
        except Exception as e:
            self.bot.logger.error(f"Failed to load JSON data from {json_path}: {e}")
            self.json_data = {"embeds": []}

    def get_color(self, color_name: str) -> int:
        """Convert color name to hex value"""
        color_map = {
            "primary": self.bot.config.get("colors", {}).get("primary", "0xffb612"),
            "success": self.bot.config.get("colors", {}).get("success", "0x57F287"),
            "error": self.bot.config.get("colors", {}).get("error", "0xE02B2B"),
            "warning": self.bot.config.get("colors", {}).get("warning", "0xF59E42"),
        }
        return int(color_map.get(color_name, "0xffb612"), 16)

    def create_embed_from_data(self, embed_data: dict) -> discord.Embed:
        """Create a Discord embed from JSON data"""
        # Get color
        color = self.get_color(embed_data.get("color", "primary"))

        # Create embed
        embed = discord.Embed(
            title=embed_data.get("title", ""),
            description=embed_data.get("description", ""),
            color=color,
        )

        # Add optional fields
        if "thumbnail" in embed_data:
            embed.set_thumbnail(url=embed_data["thumbnail"])

        if "image" in embed_data:
            embed.set_image(url=embed_data["image"])

        if "footer" in embed_data:
            footer_data = embed_data["footer"]
            if isinstance(footer_data, dict):
                embed.set_footer(
                    text=footer_data.get("text", ""),
                    icon_url=footer_data.get("icon_url"),
                )
            else:
                embed.set_footer(text=footer_data)

        if "author" in embed_data:
            author_data = embed_data["author"]
            embed.set_author(
                name=author_data.get("name", ""), icon_url=author_data.get("icon_url")
            )

        if "fields" in embed_data:
            for field in embed_data["fields"]:
                embed.add_field(
                    name=field.get("name", ""),
                    value=field.get("value", ""),
                    inline=field.get("inline", False),
                )

        return embed


async def setup(bot) -> None:
    """Add the GenerateEmbeds cog to the bot."""
    await bot.add_cog(GenerateEmbeds(bot))
