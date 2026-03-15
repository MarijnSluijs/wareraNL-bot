"""Background tasks: citizen level cache refresh (NL every hour, all countries every 6 h)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list

logger = logging.getLogger("discord_bot")

# Full all-countries sweep at most once every N hours.
_ALL_COUNTRIES_INTERVAL_H = 6


class CitizenTasks(TaskCogBase, name="citizen_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.citizen_refresh.start()

    def cog_unload(self) -> None:
        self.citizen_refresh.cancel()

    # ------------------------------------------------------------------ #
    # Merged hourly citizen refresh (NL every tick; all countries every 6 h)
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=1)
    async def citizen_refresh(self):
        if not self._client or not self._db or not self._citizen_cache:
            return

        nl_country_id = self.config.get("nl_country_id")

        # ── Always refresh NL ──────────────────────────────────────────
        if nl_country_id:
            await self._do_nl_refresh(nl_country_id)

        # ── All-countries sweep (guarded by 6-hour cooldown) ──────────
        now_utc = datetime.now(timezone.utc)
        try:
            last_run_str = await self._db.get_poll_state("citizen_refresh_last_run")
            if last_run_str:
                elapsed_h = (
                    now_utc - datetime.fromisoformat(last_run_str)
                ).total_seconds() / 3600
                if elapsed_h < _ALL_COUNTRIES_INTERVAL_H:
                    logger.info(
                        "citizen_refresh: skipping full sweep — last run %.1fh ago (< %dh)",
                        elapsed_h,
                        _ALL_COUNTRIES_INTERVAL_H,
                    )
                    return
        except Exception:
            logger.exception("citizen_refresh: failed to read last-run state")

        await self._do_all_countries_refresh(now_utc)

    @citizen_refresh.before_loop
    async def before_citizen_refresh(self):
        await self._wait_for_services()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    async def _do_nl_refresh(self, nl_country_id: str) -> None:
        """Refresh citizen level cache for NL only."""
        logger.info("citizen_refresh: refreshing NL citizens")
        try:
            all_countries = await self._client.get("/country.getAllCountries")
            country_list = extract_country_list(all_countries)
            nl_country = next(
                (c for c in country_list if cid_of(c) == nl_country_id), None
            )
            name = nl_country.get("name", "NL") if nl_country else "NL"
            async with self._heavy_api_lock:
                await self._citizen_cache.refresh_country(nl_country_id, name)
            logger.info("citizen_refresh: NL done")
        except Exception:
            logger.exception("citizen_refresh: NL refresh failed")
            return

        try:
            await self._sync_discord_nicknames_for_country(nl_country_id, name)
        except Exception:
            logger.exception("citizen_refresh: NL nickname sync failed")

    async def _do_all_countries_refresh(self, now_utc: datetime) -> None:
        """Refresh citizen level cache for all countries."""
        logger.info("citizen_refresh: starting full country sweep")

        # Persist start time first so a mid-sweep crash doesn't cause instant retry.
        try:
            await self._db.set_poll_state(
                "citizen_refresh_last_run", now_utc.isoformat()
            )
        except Exception:
            logger.exception("citizen_refresh: failed to save last-run state")

        try:
            all_countries = await self._client.get("/country.getAllCountries")
        except Exception:
            logger.exception("citizen_refresh: failed to fetch countries")
            return

        country_list = extract_country_list(all_countries)
        total = len(country_list)
        for i, country in enumerate(country_list, 1):
            cid = cid_of(country)
            name = country.get("name", cid)
            logger.info("citizen_refresh: (%d/%d) %s", i, total, name)
            try:
                await self._citizen_cache.refresh_country(cid, name)
            except Exception:
                logger.exception("citizen_refresh: error refreshing %s", name)
                continue

            try:
                await self._sync_discord_nicknames_for_country(cid, name)
            except Exception:
                logger.exception("citizen_refresh: nickname sync error for %s", name)
        logger.info("citizen_refresh: full sweep complete (%d countries)", total)

    async def _sync_discord_nicknames_for_country(
        self,
        country_id: str,
        country_name: str,
    ) -> None:
        """Sync Discord nicknames to latest citizen names for mapped users."""
        if not self._db:
            return

        try:
            citizens = await self._db.get_nl_citizen_ids(country_id)
        except Exception:
            logger.exception(
                "citizen_refresh: failed loading citizens for nickname sync (%s)",
                country_name,
            )
            return

        if not citizens:
            return

        name_by_ingame = {
            str(user_id): str(citizen_name).strip()
            for user_id, citizen_name in citizens
            if str(citizen_name).strip()
        }
        if not name_by_ingame:
            return

        updated = 0
        skipped = 0
        failed = 0

        for guild in self.bot.guilds:
            try:
                links = await self._db.get_identity_links_for_guild(str(guild.id))
            except Exception:
                logger.exception(
                    "citizen_refresh: failed loading identity links for guild %s",
                    guild.id,
                )
                continue

            for link in links:
                in_game_user_id = str(link.get("in_game_user_id") or "").strip()
                target_nick = name_by_ingame.get(in_game_user_id)
                if not target_nick:
                    continue

                target_nick = target_nick[:32]
                if not target_nick:
                    continue

                discord_user_id = str(link.get("discord_user_id") or "").strip()
                if not discord_user_id.isdigit():
                    continue

                member = guild.get_member(int(discord_user_id))
                if member is None or member.bot:
                    continue

                current_nick = (member.nick or "").strip()
                if current_nick == target_nick:
                    skipped += 1
                    continue

                if member.nick is None and member.name == target_nick:
                    skipped += 1
                    continue

                try:
                    await member.edit(
                        nick=target_nick,
                        reason=f"Sync met WarEra citizen naam ({country_name})",
                    )
                    updated += 1
                except Exception:
                    failed += 1

        logger.info(
            "citizen_refresh: nickname sync %s finished (updated=%d skipped=%d failed=%d)",
            country_name,
            updated,
            skipped,
            failed,
        )

    


async def setup(bot) -> None:
    """Add the CitizenTasks cog to the bot."""
    await bot.add_cog(CitizenTasks(bot))
