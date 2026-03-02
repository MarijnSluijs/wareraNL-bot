"""
This module defines the PeilCog, which provides the /peil command to trigger on-demand refreshes of various cached game data subsystems in the WarEraNL bot.
- /peil [burgers/productie/events/weerstand/alles] [land]
"""

from __future__ import annotations

import logging
import time

from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as cid_of, find_country
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")


class PeilCog(CommandCogBase, name="peil"):
    """Cog for the /peil command, allowing privileged users to trigger on-demand refreshes of various cached game data subsystems."""
    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # /peil                                                                #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="peil",
        description="Ververs cache of peil game-data: burgers, mus, productie, events of weerstand.",
    )
    @app_commands.describe(
        onderdeel="Wat wil je peilen?",
        land="Land (alleen voor 'burgers'). Leeg = alle landen.",
    )
    @app_commands.choices(onderdeel=[
        app_commands.Choice(name="burgers",    value="burgers"),
        app_commands.Choice(name="mus",        value="mus"),
        app_commands.Choice(name="productie",  value="productie"),
        app_commands.Choice(name="events",     value="events"),
        app_commands.Choice(name="weerstand",  value="weerstand"),
        app_commands.Choice(name="alles",      value="alles"),
    ])
    @app_commands.autocomplete(land=country_autocomplete)
    @has_privileged_role()
    async def peil(
        self,
        ctx: Context,
        onderdeel: str,
        land: str | None = None,
    ):
        """Trigger an on-demand data refresh for the chosen subsystem.

        • burgers   — ververs citizen-level cache (NL of opgegeven land, of alle)
        • mus       — ververs MU-lidmaatschappen voor NL
        • productie — voer een productiepoll uit
        • events    — voer een event-poll uit (herpost meest recente per categorie)
        • weerstand — voer een verzetspeiling uit
        • alles     — voer alle peilingen uit
        """
        if not self._client or not self._db:
            await ctx.send("Diensten niet geïnitialiseerd.", ephemeral=True)
            return

        if hasattr(ctx, "defer"):
            await ctx.defer(ephemeral=True)

        if onderdeel in ("burgers", "alles"):
            await self._peil_burgers(ctx, land)
        if onderdeel in ("mus", "alles"):
            await self._peil_mus(ctx)
        if onderdeel in ("productie", "alles"):
            await self._peil_productie(ctx)
        if onderdeel in ("events", "alles"):
            await self._peil_events(ctx)
        if onderdeel in ("weerstand", "alles"):
            await self._peil_weerstand(ctx)

    # ------------------------------------------------------------------ #
    # Burgers subsystem                                                    #
    # ------------------------------------------------------------------ #

    async def _peil_burgers(self, ctx: Context, land: str | None) -> None:
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        if not citizen_cache:
            await ctx.send("❌ Citizen cache niet beschikbaar.", ephemeral=True)
            return

        country_list = await self._fetch_country_list(ctx)
        if country_list is None:
            return

        if land:
            target = find_country(land, country_list)
            if target is None:
                await ctx.send(f"Land `{land}` niet gevonden.", ephemeral=True)
                return
            countries = [target]
        else:
            countries = country_list

        n = len(countries)
        label = f"**{countries[0].get('name', land)}**" if n == 1 else f"**{n}** landen"
        status_msg = await ctx.send(f"Burgersniveau-verversing gestart voor {label}…", ephemeral=True)

        t_start = time.monotonic()
        total_recorded = 0
        failed: list[str] = []
        for i, c in enumerate(countries, 1):
            cid = cid_of(c)
            name = c.get("name", cid)
            if n > 1:
                await status_msg.edit(content=f"Refreshing citizen levels… ({i}/{n}) **{name}**")
            try:
                recorded = await citizen_cache.refresh_country(
                    cid, name,
                    progress_msg=status_msg if n == 1 else None,
                )
                total_recorded += recorded
                logger.info("peil burgers: %s — %d levels cached", name, recorded)
            except Exception:
                logger.exception("peil burgers: error for %s", name)
                failed.append(name)

        elapsed = time.monotonic() - t_start
        elapsed_str = (
            f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            if elapsed >= 60
            else f"{elapsed:.1f}s"
        )
        if n == 1:
            summary = (
                f"Citizen level cache verversing klaar voor **{countries[0].get('name', land)}** "
                f"— {total_recorded} levels opgeslagen. ⏱ {elapsed_str}"
            )
        else:
            summary = (
                f"Citizen level cache verversing klaar voor **{n}** landen "
                f"— {total_recorded} levels opgeslagen. ⏱ {elapsed_str}"
            )
        if failed:
            summary += f"\nMislukt: {', '.join(failed)}"
        await status_msg.edit(content=summary)

    # ------------------------------------------------------------------ #
    # MUs subsystem                                                        #
    # ------------------------------------------------------------------ #

    async def _peil_mus(self, ctx: Context) -> None:
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        if not citizen_cache:
            await ctx.send("❌ Citizen cache niet beschikbaar.", ephemeral=True)
            return
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            await ctx.send("❌ `nl_country_id` is niet geconfigureerd.", ephemeral=True)
            return
        testing = getattr(self.bot, "testing", False)
        mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
        status_msg = await ctx.send("🔄 MU-lidmaatschappen verversen…", ephemeral=True)
        try:
            mu_count = await citizen_cache.refresh_mu_memberships(nl_country_id, mus_json)
            await status_msg.edit(content=f"✅ MU-lidmaatschappen verversing klaar — {mu_count} toewijzingen opgeslagen.")
            logger.info("peil mus: %d assignments refreshed", mu_count)
        except Exception as exc:
            logger.exception("peil mus: MU membership refresh failed")
            await status_msg.edit(content=f"❌ MU-verversing mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Productie subsystem                                                  #
    # ------------------------------------------------------------------ #

    async def _peil_productie(self, ctx: Context) -> None:
        prod_cog = self.bot.get_cog("production_tasks")
        if not prod_cog:
            await ctx.send("❌ Production task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Productiepoll gestart…", ephemeral=True)
        try:
            changes = await prod_cog.run_poll_once()
            if changes:
                summary = "\n".join(
                    f"• **{item}**: {old} → {new}" for item, old, new in changes
                )
                await status_msg.edit(content=f"✅ Productiepoll klaar — {len(changes)} wijziging(en):\n{summary}")
            else:
                await status_msg.edit(content="✅ Productiepoll klaar — geen wijzigingen.")
        except Exception as exc:
            logger.exception("peil productie: error")
            await status_msg.edit(content=f"❌ Productiepoll mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Events subsystem                                                     #
    # ------------------------------------------------------------------ #

    async def _peil_events(self, ctx: Context) -> None:
        event_cog = self.bot.get_cog("event_tasks")
        if not event_cog:
            await ctx.send("❌ Event task cog niet geladen.", ephemeral=True)
            return
        # Clear init keys so the catch-up block fires and re-posts latest per category.
        try:
            await self._db._conn.execute(
                "DELETE FROM poll_state WHERE key LIKE 'event_cat_init_%'"
            )
            await self._db._conn.commit()
        except Exception as exc:
            await ctx.send(f"❌ Kon init-sleutels niet wissen: {exc}", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Event-peiling gestart…", ephemeral=True)
        try:
            await event_cog.run_event_poll()
            await status_msg.edit(content="✅ Event-peiling voltooid.")
        except Exception as exc:
            logger.exception("peil events: error")
            await status_msg.edit(content=f"❌ Event-peiling mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Weerstand subsystem                                                  #
    # ------------------------------------------------------------------ #

    async def _peil_weerstand(self, ctx: Context) -> None:
        resistance_cog = self.bot.get_cog("resistance_tasks")
        if not resistance_cog:
            await ctx.send("❌ Resistance task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Verzetspeiling gestart…", ephemeral=True)
        try:
            await resistance_cog.run_resistance_poll()
            await status_msg.edit(content="✅ Verzetspeiling voltooid.")
        except Exception as exc:
            logger.exception("peil weerstand: error")
            await status_msg.edit(content=f"❌ Verzetspeiling mislukt: {exc}")


async def setup(bot) -> None:
    """Add the PeilCog to the bot."""
    await bot.add_cog(PeilCog(bot))
