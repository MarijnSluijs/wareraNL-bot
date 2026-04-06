"""
This module defines the PeilCog, which provides the /peil command to trigger on-demand refreshes of various cached game data subsystems in the WarEraNL bot.
- /peil [burgers/productie/events/weerstand/alles] [land]
"""

from __future__ import annotations

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as cid_of
from services.country_utils import find_country
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
    @app_commands.choices(
        onderdeel=[
            app_commands.Choice(name="burgers", value="burgers"),
            app_commands.Choice(name="mus", value="mus"),
            app_commands.Choice(name="productie", value="productie"),
            app_commands.Choice(name="events", value="events"),
            app_commands.Choice(name="weerstand", value="weerstand"),
            app_commands.Choice(name="weekschade", value="weekschade"),
            app_commands.Choice(name="geluk", value="geluk"),
            app_commands.Choice(name="globalluck", value="globalluck"),
            app_commands.Choice(name="slagveld", value="slagveld"),
            app_commands.Choice(name="artikelen", value="artikelen"),
            app_commands.Choice(name="backfill", value="backfill"),
            app_commands.Choice(name="alles", value="alles"),
        ]
    )
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
        if onderdeel in ("weekschade", "alles"):
            await self._peil_weekschade(ctx)
        if onderdeel in ("geluk", "alles"):
            await self._peil_geluk(ctx)
        if onderdeel in ("globalluck", "alles"):
            await self._peil_globalluck(ctx)
        if onderdeel in ("slagveld", "alles"):
            await self._peil_slagveld(ctx)
        if onderdeel == "artikelen":
            await self._peil_artikelen(ctx, land)
        if onderdeel == "backfill":
            await self._peil_backfill(ctx)

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
        status_msg = await ctx.send(
            f"Burgersniveau-verversing gestart voor {label}…", ephemeral=True
        )

        t_start = time.monotonic()
        total_recorded = 0
        failed: list[str] = []
        for i, c in enumerate(countries, 1):
            cid = cid_of(c)
            name = c.get("name", cid)
            if n > 1:
                await status_msg.edit(
                    content=f"Refreshing citizen levels… ({i}/{n}) **{name}**"
                )
            try:
                recorded = await citizen_cache.refresh_country(
                    cid,
                    name,
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
        status_msg = await ctx.send("🔄 MU-namen vernieuwen…", ephemeral=True)
        try:
            mu_tasks = self.bot.get_cog("mu_tasks")
            if mu_tasks:
                await mu_tasks.refresh_mu_info()
                await mu_tasks.refresh_all_mu_names()

            total_mus = (await self._db.get_all_known_mu_ids()) if self._db else []
            total_count = len(total_mus)

            await status_msg.edit(
                content=f"🔄 MU sweep gestart — {total_count} MUs laden…"
            )

            last_edit_idx = 0

            async def _progress(done: int, total: int, mu_name: str) -> None:
                nonlocal last_edit_idx
                # Only edit every 25 MUs to avoid Discord rate limits
                if done - last_edit_idx >= 25 or done == total:
                    last_edit_idx = done
                    pct = int(done / total * 100) if total else 0
                    try:
                        await status_msg.edit(
                            content=(
                                f"🔄 MU sweep bezig… {done}/{total} ({pct}%)"
                                f"\nLaatste: **{mu_name}**"
                            )
                        )
                    except Exception:
                        pass

            t_start = time.monotonic()
            mus_tagged, citizens_updated = await citizen_cache.sweep_all_mu_memberships(
                progress_callback=_progress,
            )
            elapsed = time.monotonic() - t_start
            elapsed_str = (
                f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                if elapsed >= 60
                else f"{elapsed:.1f}s"
            )
            await status_msg.edit(
                content=(
                    f"✅ MU sweep klaar — {mus_tagged}/{total_count} MUs getagt met land, "
                    f"{citizens_updated} burgers bijgewerkt. ⏱ {elapsed_str}"
                )
            )
            logger.info(
                "peil mus: sweep done — %d MUs tagged, %d citizens updated in %s",
                mus_tagged, citizens_updated, elapsed_str,
            )
        except Exception as exc:
            logger.exception("peil mus: sweep failed")
            await status_msg.edit(content=f"❌ MU sweep mislukt: {exc}")

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
                await status_msg.edit(
                    content=f"✅ Productiepoll klaar — {len(changes)} wijziging(en):\n{summary}"
                )
            else:
                await status_msg.edit(
                    content="✅ Productiepoll klaar — geen wijzigingen."
                )
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
        resistance_cog = self.bot.get_cog("resistance")
        if not resistance_cog:
            await ctx.send("❌ Resistance cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Verzetspeiling gestart…", ephemeral=True)
        try:
            embed = await resistance_cog.build_resistance_embed()
            if embed is None:
                await status_msg.edit(content="ℹ️ Geen bezette buitenlandse regio's gevonden.")
            else:
                await status_msg.edit(content="✅ Verzetspeiling voltooid.")
        except Exception as exc:
            logger.exception("peil weerstand: error")
            await status_msg.edit(content=f"❌ Verzetspeiling mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Weekschade subsystem                                                 #
    # ------------------------------------------------------------------ #

    async def _peil_weekschade(self, ctx: Context) -> None:
        damage_cog = self.bot.get_cog("damage_tasks")
        if not damage_cog:
            await ctx.send("❌ Damage task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Wekelijkse schade ophalen…", ephemeral=True)
        try:
            updated, zeroed = await damage_cog.run_damage_refresh_once()
            await status_msg.edit(
                content=f"✅ Weekschade verversing klaar — {updated} met schade, {zeroed} op nul."
            )
        except Exception as exc:
            logger.exception("peil weekschade: error")
            await status_msg.edit(content=f"❌ Weekschade verversing mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # NL Luck subsystem                                                    #
    # ------------------------------------------------------------------ #

    async def _peil_geluk(self, ctx: Context) -> None:
        luck_cog = self.bot.get_cog("luck_tasks")
        if not luck_cog:
            await ctx.send("❌ Luck task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 NL geluksranking verversen…", ephemeral=True)
        try:
            await luck_cog.run_luck_refresh()
            total_str = await self._db.get_poll_state("luck_ranking_total")
            total = int(total_str or 0)
            try:
                await status_msg.edit(
                    content=f"✅ NL geluksranking klaar — {total:,} spelers gescoord."
                )
            except discord.HTTPException:
                logger.warning(
                    "peil geluk: interaction token expired, result saved to DB"
                )
        except Exception as exc:
            logger.exception("peil geluk: error")
            try:
                await status_msg.edit(content=f"❌ NL geluksranking mislukt: {exc}")
            except discord.HTTPException:
                logger.warning(
                    "peil geluk: interaction token expired while reporting error"
                )

    # ------------------------------------------------------------------ #
    # Slagveld subsystem                                                   #
    # ------------------------------------------------------------------ #

    async def _peil_slagveld(self, ctx: Context) -> None:
        task_cog = self.bot.get_cog("battle_rankings_task")
        if not task_cog:
            await ctx.send("❌ Battle rankings task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send("🔄 Slagveld sweep gestart…", ephemeral=True)
        try:
            new_battles, new_hits = await task_cog.run_sweep_once()
            await status_msg.edit(
                content=f"✅ Slagveld sweep klaar — {new_battles} nieuwe gevechten, {new_hits} nieuwe treffers."
            )
        except Exception as exc:
            logger.exception("peil slagveld: error")
            await status_msg.edit(content=f"❌ Slagveld sweep mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Artikel tips subsystem                                               #
    # ------------------------------------------------------------------ #

    async def _peil_artikelen(self, ctx: Context, land: str | None) -> None:
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        if not citizen_cache:
            await ctx.send("❌ Citizen cache niet beschikbaar.", ephemeral=True)
            return

        # Resolve optional country filter
        country_id_filter: str | None = None
        label = "alle landen"
        if land:
            country_list = await self._fetch_country_list(ctx)
            if country_list is None:
                return
            target = find_country(land, country_list)
            if target is None:
                await ctx.send(f"Land `{land}` niet gevonden.", ephemeral=True)
                return
            country_id_filter = cid_of(target)
            label = f"**{target.get('name', land)}**"

        status_msg = await ctx.send(
            f"🔄 Artikel tip scan gestart voor {label}… (dit kan lang duren)",
            ephemeral=True,
        )

        last_edit_idx = 0

        async def _progress(done: int, total: int, citizen_name: str) -> None:
            nonlocal last_edit_idx
            if done - last_edit_idx >= 50 or done == total:
                last_edit_idx = done
                pct = int(done / total * 100) if total else 0
                try:
                    await status_msg.edit(
                        content=(
                            f"🔄 Artikel tip scan… {done}/{total} ({pct}%)"
                            f"\nLaatste: **{citizen_name}**"
                        )
                    )
                except Exception:
                    pass

        try:
            t_start = time.monotonic()
            citizens_scanned, tips_stored = await citizen_cache.sweep_article_tips(
                country_id=country_id_filter,
                progress_callback=_progress,
            )
            elapsed = time.monotonic() - t_start
            elapsed_str = (
                f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                if elapsed >= 60
                else f"{elapsed:.1f}s"
            )
            result_msg = (
                f"✅ Artikel tip scan klaar — {citizens_scanned} burgers gescand, "
                f"{tips_stored} nieuwe tip-records opgeslagen. ⏱ {elapsed_str}"
            )
            logger.info(
                "peil artikelen: %d citizens scanned, %d tips stored in %s",
                citizens_scanned, tips_stored, elapsed_str,
            )
            try:
                await status_msg.edit(content=result_msg)
            except discord.HTTPException:
                logger.warning("peil artikelen: interaction token expired, result saved to DB")
        except Exception as exc:
            logger.exception("peil artikelen: error")
            try:
                await status_msg.edit(content=f"❌ Artikel tip scan mislukt: {exc}")
            except discord.HTTPException:
                logger.warning("peil artikelen: interaction token expired while reporting error")

    # ------------------------------------------------------------------ #
    # Backfill subsystem                                                   #
    # ------------------------------------------------------------------ #

    async def _peil_backfill(self, ctx: Context) -> None:
        task_cog = self.bot.get_cog("battle_rankings_task")
        if not task_cog:
            await ctx.send("❌ Battle rankings task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send(
            "🔄 Backfill gestart (3 stappen: land-IDs, landtreffers, MU-treffers)…",
            ephemeral=True,
        )
        try:
            # Step 1: backfill missing attacker/defender country IDs
            await status_msg.edit(content="🔄 Stap 1/3: Battle land-IDs bijvullen…")
            updated = await task_cog.run_country_id_backfill()
            # Step 2: backfill country-level damage hits
            await status_msg.edit(
                content=f"✅ Stap 1/3 klaar: {updated} gevechten bijgewerkt.\n🔄 Stap 2/3: Landtreffers bijvullen…"
            )
            battles_done, hits_added = await task_cog.run_country_backfill()
            # Step 3: backfill MU hits
            await status_msg.edit(
                content=(
                    f"✅ Stap 2/3 klaar: {battles_done} gevechten, {hits_added} landtreffers.\n"
                    "🔄 Stap 3/3: MU-treffers bijvullen… (dit kan lang duren)"
                )
            )
            mu_battles, mu_hits = await task_cog.run_mu_hits_backfill()
            await status_msg.edit(
                content=(
                    f"✅ Backfill klaar:\n"
                    f"• Land-IDs: {updated} gevechten bijgewerkt\n"
                    f"• Landtreffers: {battles_done} gevechten, {hits_added} treffers\n"
                    f"• MU-treffers: {mu_battles} gevechten, {mu_hits} treffers"
                )
            )
        except Exception as exc:
            logger.exception("peil backfill: error")
            await status_msg.edit(content=f"❌ Backfill mislukt: {exc}")

    # ------------------------------------------------------------------ #
    # Global luck subsystem                                                #
    # ------------------------------------------------------------------ #

    async def _peil_globalluck(self, ctx: Context) -> None:
        cog = self.bot.get_cog("global_luck_tasks")
        if not cog:
            await ctx.send("❌ Global luck task cog niet geladen.", ephemeral=True)
            return
        status_msg = await ctx.send(
            "🔄 Globale geluksweep gestart… (dit kan lang duren)", ephemeral=True
        )
        try:
            await cog.run_global_luck_refresh()
            total_str = await self._db.get_poll_state("global_luck_ranking_total")
            total = int(total_str or 0)
            try:
                await status_msg.edit(
                    content=f"✅ Globale gelukranking klaar — {total:,} spelers gescoord."
                )
            except discord.HTTPException:
                logger.warning(
                    "peil globalluck: interaction token expired, result saved to DB"
                )
        except Exception as exc:
            logger.exception("peil globalluck: error")
            try:
                await status_msg.edit(content=f"❌ Globale geluksweep mislukt: {exc}")
            except discord.HTTPException:
                logger.warning(
                    "peil globalluck: interaction token expired while reporting error"
                )


async def setup(bot) -> None:
    """Add the PeilCog to the bot."""
    await bot.add_cog(PeilCog(bot))
