"""
Base class shared by all command cogs.

Provides safe access to shared services and common Discord utilities.
"""

from __future__ import annotations

from typing import Union

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from services.country_utils import ALL_COUNTRY_NAMES, extract_country_list


async def citizen_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete callback for player name parameters.

    Queries citizen_levels table (populated by /peil burgers) for a
    case-insensitive substring match. Falls back to empty list if DB
    is not yet available.
    """
    db = getattr(interaction.client, "_ext_db", None)
    if not db:
        return []
    try:
        matches = await db.search_citizen_names(current, limit=25)
        return [app_commands.Choice(name=name, value=name) for name, _uid in matches]
    except Exception:
        return []


async def country_autocomplete(
    _interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete callback for country name parameters.

    Use this directly in ``@app_commands.autocomplete(country=country_autocomplete)``.
    """
    q = current.strip().lower()
    return [
        app_commands.Choice(name=name, value=name)
        for name in ALL_COUNTRY_NAMES
        if q in name.lower()
    ][:25]


async def dreigingsniveau_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Module-level autocomplete callback for dreigingsniveau parameters."""
    q = current.strip().lower()
    options = ["groen", "geel", "oranje", "rood"]
    return [
        app_commands.Choice(name=option.capitalize(), value=option)
        for option in options
        if q in option
    ]


class CommandCogBase(commands.Cog):
    """Mixin providing service properties and shared utilities for command cogs."""

    @property
    def _db(self):
        return getattr(self.bot, "_ext_db", None)

    @property
    def _client(self):
        return getattr(self.bot, "_ext_client", None)

    @property
    def config(self) -> dict:
        return getattr(self.bot, "config", {})

    def _embed_colour(self, key: str = "primary") -> discord.Colour:
        raw = (self.config.get("colors") or {}).get(key, "0xffb612")
        try:
            return discord.Colour(int(str(raw), 16))
        except Exception:
            return discord.Colour.gold()

    def _api_offline_embed(self, note: str = "") -> discord.Embed:
        """Return a standardised 'API offline' embed."""
        desc = (
            "⚠️ De WarEra API is momenteel niet beschikbaar.\n"
            "Probeer het later opnieuw."
        )
        if note:
            desc += f"\n\n{note}"
        embed = discord.Embed(
            title="🔌 API Offline",
            description=desc,
            colour=self._embed_colour("warning"),
        )
        embed.set_footer(text="WareraNL Bot")
        return embed

    async def _send_api_offline(
        self,
        ctx: Union[Context, discord.Interaction],
        note: str = "",
    ) -> None:
        """Send a standardised 'API offline' message to *ctx* or *interaction*."""
        embed = self._api_offline_embed(note)
        if isinstance(ctx, discord.Interaction):
            if ctx.response.is_done():
                await ctx.followup.send(embed=embed)
            else:
                await ctx.response.send_message(embed=embed, ephemeral=True)
        else:
            await ctx.send(embed=embed)

    async def _fetch_country_list(self, ctx: Union[Context, discord.Interaction]) -> list[dict] | None:
        """Fetch and unwrap the country list; sends an API-offline embed on failure."""
        if not self._client:
            await self._send_api_offline(ctx)
            return None
        try:
            resp = await self._client.get("/country.getAllCountries")
        except Exception:
            await self._send_api_offline(ctx)
            return None
        result = extract_country_list(resp)
        if not result:
            await self._send_api_offline(ctx, "Lege landenlijst ontvangen van de API.")
            return None
        return result
