"""
/monitor [land] — beheer periodieke paraatheidsmonitoring per land.

Subcommands (via optionele parameters):
  /monitor                   — toon alle actieve monitors
  /monitor land:NL           — start monitoring voor dit kanaal (of wijzig kanaal)
  /monitor land:NL stop:Ja   — stop monitoring voor dit land
"""

from __future__ import annotations

import json
import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as cid_of
from services.country_utils import find_country

logger = logging.getLogger("discord_bot")


class MonitorCog(CommandCogBase, name="monitor"):
    """Paraatheidsmonitoring commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # /monitor                                                             #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="monitor",
        description="Schakel periodieke paraatheidsmonitoring (niveau 21+) in of uit voor een land.",
    )
    @app_commands.describe(
        land="Land om te monitoren. Laat leeg voor een overzicht van actieve monitors.",
        stop="Zet op 'Ja' om monitoring voor dit land te stoppen.",
    )
    @app_commands.autocomplete(land=country_autocomplete)
    @app_commands.choices(stop=[app_commands.Choice(name="Ja", value="ja")])
    async def monitor(
        self,
        ctx: Context,
        land: str | None = None,
        stop: str | None = None,
    ) -> None:
        """/monitor — beheer landmonitoring voor periodieke paraatheidsrapportages.

        /monitor              — toon actieve monitors
        /monitor land:NL      — start monitoring in dit kanaal (elke 15 min, niveau 21+)
        /monitor land:NL stop:Ja — stop monitoring
        """
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        # ── Load current subscriptions ─────────────────────────────────
        try:
            subs_json = await self._db.get_poll_state("monitor_subscriptions")
            subscriptions: dict[str, dict] = json.loads(subs_json) if subs_json else {}
        except Exception:
            subscriptions = {}

        # ── Mode: list (no land given) ─────────────────────────────────
        if land is None:
            if not subscriptions:
                await ctx.send(
                    "Geen actieve monitors. Gebruik `/monitor land:NL` om er een te starten."
                )
                return
            lines = [
                f"• **{sub.get('country_name', cid)}** → <#{sub['channel_id']}>"
                for cid, sub in subscriptions.items()
            ]
            embed = discord.Embed(
                title="Actieve paraatheidsmonitors (niveau 21+)",
                description="\n".join(lines),
                colour=self._embed_colour(),
            )
            embed.set_footer(text="Rapportage elke 15 minuten bij wijzigingen.")
            await ctx.send(embed=embed)
            return

        # ── Resolve country ────────────────────────────────────────────
        country_list = await self._fetch_country_list(ctx)
        if country_list is None:
            return
        target = find_country(land, country_list)
        if target is None:
            await ctx.send(f"Land `{land}` niet gevonden.")
            return

        country_id = cid_of(target)
        country_name = target.get("name", land)

        # ── Mode: stop ─────────────────────────────────────────────────
        if stop and stop.lower() == "ja":
            if country_id not in subscriptions:
                await ctx.send(f"**{country_name}** wordt momenteel niet gemonitord.")
                return
            del subscriptions[country_id]
            await self._db.set_poll_state(
                "monitor_subscriptions", json.dumps(subscriptions)
            )
            # Clean up the snapshot so a future restart is clean.
            await self._db.set_poll_state(f"monitor_snapshot_{country_id}", "")
            await ctx.send(f"✅ Monitoring gestopt voor **{country_name}**.")
            return

        # ── Mode: start / update ───────────────────────────────────────
        is_update = country_id in subscriptions
        subscriptions[country_id] = {
            "country_name": country_name,
            "channel_id": str(ctx.channel.id),
            "guild_id": str(ctx.guild.id) if ctx.guild else None,
        }
        await self._db.set_poll_state(
            "monitor_subscriptions", json.dumps(subscriptions)
        )

        # Reset the snapshot so the very next task run saves a fresh baseline
        # instead of generating a spurious diff.
        await self._db.set_poll_state(f"monitor_snapshot_{country_id}", "")

        action = "bijgewerkt" if is_update else "geactiveerd"
        await ctx.send(
            f"✅ Monitoring {action} voor **{country_name}**.\n"
            "Elke 15 minuten worden wijzigingen in paraatheid (niveau 21+) "
            "in dit kanaal gerapporteerd."
        )


async def setup(bot) -> None:
    """Add the MonitorCog to the bot."""
    await bot.add_cog(MonitorCog(bot))
