"""Slash command: /overzicht — toont alle actieve abonnementen van de gebruiker."""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import CommandCogBase
from services.subscription_registry import SubscriptionEntry, all_entries, get_entry

# ── Embed builder ──────────────────────────────────────────────────────────────

async def _build(
    db,
    user_id: str,
    colour: discord.Colour,
) -> tuple[discord.Embed, discord.ui.View]:
    entries = all_entries()
    active: list[tuple[SubscriptionEntry, dict]] = []

    for entry in entries:
        row = await entry.get_fn(db, user_id)
        if row is not None:
            active.append((entry, row))

    embed = discord.Embed(
        title="📋 Jouw abonnementen",
        colour=colour,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="WareraNL Bot")

    if not active:
        embed.description = "Je hebt geen actieve abonnementen."
        return embed, discord.ui.View()

    for entry, row in active:
        embed.add_field(
            name=f"{entry.emoji} {entry.label}",
            value=entry.detail_fn(row),
            inline=False,
        )

    view = _OverzichtView()
    for entry, _ in active:
        view.add_item(_UnsubscribeButton(entry.name, entry.emoji, entry.label))

    return embed, view


# ── View & buttons ─────────────────────────────────────────────────────────────

class _OverzichtView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)


class _UnsubscribeButton(discord.ui.Button):
    def __init__(self, sub_name: str, emoji: str, label: str) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Uitschrijven: {label}",
            emoji=emoji,
        )
        self.sub_name = sub_name

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "_ext_db", None)
        if not db:
            await interaction.response.send_message(
                "Database niet beschikbaar.", ephemeral=True
            )
            return

        entry = get_entry(self.sub_name)
        if entry:
            await entry.remove_fn(db, str(interaction.user.id))

        config = getattr(interaction.client, "config", {})
        raw = (config.get("colors") or {}).get("primary", "0xffb612")
        try:
            colour = discord.Colour(int(str(raw), 16))
        except Exception:
            colour = discord.Colour.gold()

        embed, view = await _build(db, str(interaction.user.id), colour)
        await interaction.response.edit_message(embed=embed, view=view)


# ── Cog ───────────────────────────────────────────────────────────────────────

class OverzichtCog(CommandCogBase, name="overzicht"):
    """Toont een overzicht van de actieve abonnementen van de gebruiker."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="overzicht",
        description="Bekijk jouw actieve abonnementen en schrijf je uit.",
    )
    async def overzicht(self, interaction: discord.Interaction) -> None:
        if not self._db:
            await interaction.response.send_message(
                "Database niet beschikbaar.", ephemeral=True
            )
            return

        embed, view = await _build(self._db, str(interaction.user.id), self._embed_colour())
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OverzichtCog(bot))
