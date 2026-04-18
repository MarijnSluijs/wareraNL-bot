"""Event dukaten: slash commands to track dukaten rewards for Discord event winners.

Commands (all require Manage Server permission):
  /dukaten toevoegen speler:@user hoeveelheid:int  — add dukaten; shows new total
  /dukaten verwijderen speler:@user hoeveelheid:int — remove dukaten; shows new total
  /dukaten lijst                                    — list all players with dukaten > 0
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("discord_bot")

GEM_EMOJI = "💎"

# Role ID that may run gem commands on the production server.
_COMMUNITY_ROLE_ID = 1492814531502805032


def _can_manage_dukaten():
    """Custom check: requires the community role on production; always passes in testing."""
    async def predicate(interaction: discord.Interaction) -> bool:
        bot = interaction.client
        if getattr(bot, "testing", False):
            return True
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if any(r.id == _COMMUNITY_ROLE_ID for r in member.roles):
            return True
        raise app_commands.CheckFailure(
            "Je hebt de Community-rol nodig om dit commando te gebruiken."
        )
    return app_commands.check(predicate)


def _fmt(n: int) -> str:
    """Format an integer with dots as thousands separators (Dutch convention)."""
    return f"{n:,}".replace(",", ".")


async def _member_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete that searches all cached guild members by display name or username."""
    if not interaction.guild:
        return []
    q = current.strip().lower()
    results: list[app_commands.Choice[str]] = []
    for member in interaction.guild.members:
        if member.bot:
            continue
        if q in member.display_name.lower() or q in member.name.lower():
            label = f"{member.display_name} ({member.name})"
            results.append(app_commands.Choice(name=label[:100], value=str(member.id)))
        if len(results) >= 25:
            break
    return results


class DukatenCog(commands.Cog, name="Dukaten"):
    """Slash commands for managing Discord event dukaten rewards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def _db(self):
        return getattr(self.bot, "_ext_db", None)

    # ── Command group ────────────────────────────────────────────────────────

    dukaten = app_commands.Group(
        name="dukaten",
        description="Beheer dukaten-beloningen voor Discord event-winnaars",
    )

    # ── /dukaten toevoegen ───────────────────────────────────────────────────

    @dukaten.command(name="toevoegen", description="Voeg dukaten toe aan een speler")
    @app_commands.describe(
        speler="De Discord-gebruiker die dukaten krijgt (type een naam om te zoeken)",
        hoeveelheid="Aantal toe te voegen dukaten (minimaal 1)",
    )
    @app_commands.autocomplete(speler=_member_autocomplete)
    @_can_manage_dukaten()
    async def dukaten_add(
        self,
        interaction: discord.Interaction,
        speler: str,
        hoeveelheid: app_commands.Range[int, 1],
    ) -> None:
        db = self._db
        if db is None:
            await interaction.response.send_message(
                "❌ Database is momenteel niet beschikbaar.", ephemeral=True
            )
            return

        member = interaction.guild and interaction.guild.get_member(int(speler))
        if member is None:
            await interaction.response.send_message(
                "❌ Speler niet gevonden. Kies een naam uit de lijst.", ephemeral=True
            )
            return

        new_total = await db.add_gems(
            discord_user_id=str(member.id),
            discord_username=str(member),
            guild_id=str(interaction.guild_id),
            amount=hoeveelheid,
        )

        embed = discord.Embed(
            title=f"{GEM_EMOJI} Dukaten toegevoegd",
            colour=discord.Colour.blue(),
        )
        embed.add_field(name="Speler", value=member.mention, inline=True)
        embed.add_field(name="Toegevoegd", value=f"+{_fmt(hoeveelheid)} {GEM_EMOJI}", inline=True)
        embed.add_field(name="Nieuw totaal", value=f"**{_fmt(new_total)}** {GEM_EMOJI}", inline=True)

        await interaction.response.send_message(embed=embed)
        logger.info(
            "dukaten: %s added %d dukaten to %s (new total: %d)",
            interaction.user,
            hoeveelheid,
            member,
            new_total,
        )

    # ── /dukaten verwijderen ─────────────────────────────────────────────────

    @dukaten.command(name="verwijderen", description="Verwijder dukaten van een speler")
    @app_commands.describe(
        speler="De Discord-gebruiker van wie dukaten worden verwijderd (type een naam om te zoeken)",
        hoeveelheid="Aantal te verwijderen dukaten (minimaal 1)",
    )
    @app_commands.autocomplete(speler=_member_autocomplete)
    @_can_manage_dukaten()
    async def dukaten_remove(
        self,
        interaction: discord.Interaction,
        speler: str,
        hoeveelheid: app_commands.Range[int, 1],
    ) -> None:
        db = self._db
        if db is None:
            await interaction.response.send_message(
                "❌ Database is momenteel niet beschikbaar.", ephemeral=True
            )
            return

        member = interaction.guild and interaction.guild.get_member(int(speler))
        if member is None:
            await interaction.response.send_message(
                "❌ Speler niet gevonden. Kies een naam uit de lijst.", ephemeral=True
            )
            return

        new_total = await db.remove_gems(
            discord_user_id=str(member.id),
            discord_username=str(member),
            guild_id=str(interaction.guild_id),
            amount=hoeveelheid,
        )

        embed = discord.Embed(
            title=f"{GEM_EMOJI} Dukaten verwijderd",
            colour=discord.Colour.orange(),
        )
        embed.add_field(name="Speler", value=member.mention, inline=True)
        embed.add_field(name="Verwijderd", value=f"-{_fmt(hoeveelheid)} {GEM_EMOJI}", inline=True)
        embed.add_field(name="Nieuw totaal", value=f"**{_fmt(new_total)}** {GEM_EMOJI}", inline=True)

        await interaction.response.send_message(embed=embed)
        logger.info(
            "dukaten: %s removed %d dukaten from %s (new total: %d)",
            interaction.user,
            hoeveelheid,
            member,
            new_total,
        )

    # ── /dukaten lijst ───────────────────────────────────────────────────────

    @dukaten.command(name="lijst", description="Toon alle spelers met openstaande dukaten")
    async def dukaten_list(self, interaction: discord.Interaction) -> None:
        db = self._db
        if db is None:
            await interaction.response.send_message(
                "❌ Database is momenteel niet beschikbaar.", ephemeral=True
            )
            return

        rows = await db.get_all_gem_balances()

        if not rows:
            await interaction.response.send_message(
                f"Er zijn momenteel geen spelers met openstaande {GEM_EMOJI} dukaten.",
                ephemeral=True,
            )
            return

        lines = []
        for i, row in enumerate(rows, start=1):
            lines.append(
                f"**{i}.** <@{row['discord_user_id']}> — **{_fmt(row['gems'])}** {GEM_EMOJI}"
            )

        embed = discord.Embed(
            title=f"{GEM_EMOJI} Event dukaten overzicht",
            description="\n".join(lines),
            colour=discord.Colour.gold(),
        )
        embed.set_footer(text=f"{len(rows)} speler(s) met openstaande dukaten")

        await interaction.response.send_message(embed=embed)

    # ── Error handler ────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            msg = str(error) if str(error) else "Je hebt geen rechten om dit commando te gebruiken."
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)
        else:
            logger.exception("Unhandled error in dukaten command: %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Er is een onverwachte fout opgetreden.", ephemeral=True
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DukatenCog(bot))
