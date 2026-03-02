"""
Base class shared by all command cogs.

Provides safe access to shared services and common Discord utilities.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from services.country_utils import ALL_COUNTRY_NAMES, extract_country_list


async def country_autocomplete(
    interaction: discord.Interaction, current: str
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

    def _embed_colour(self) -> discord.Colour:
        raw = (self.config.get("colors") or {}).get("primary", "0xffb612")
        try:
            return discord.Colour(int(str(raw), 16))
        except Exception:
            return discord.Colour.gold()

    async def _fetch_country_list(self, ctx: Context) -> list[dict] | None:
        """Fetch and unwrap the country list; sends an error to ctx on failure."""
        try:
            resp = await self._client.get("/country.getAllCountries")
        except Exception as exc:
            await ctx.send(f"Ophalen van landen mislukt: {exc}")
            return None
        result = extract_country_list(resp)
        if not result:
            await ctx.send("Kon landenlijst niet ophalen van API.")
            return None
        return result
