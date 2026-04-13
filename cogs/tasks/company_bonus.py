"""Background task: hourly scan for companies with 0% production bonus.

For each subscribed Discord user, fetches their in-game companies and calls
``company.getProductionBonus`` per company.  When the total bonus is 0% the
user is pinged via DM once per (company, region) combination.

When a company moves to a new region the old alert is cleared so a fresh
notification can be sent if the new region also has 0% bonus.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# How often to run the scan (aligned to whole hours elsewhere; here we just
# run every hour and let the before_loop alignment handle clock-alignment).
_SCAN_INTERVAL_HOURS = 1
# Minimum minutes between scans — prevents re-notification on rapid restarts.
_MIN_SCAN_INTERVAL_MINUTES = 50


def _unwrap(resp: object) -> dict | list | None:
    """Strip a single tRPC result/data envelope."""
    if not isinstance(resp, dict):
        return resp  # type: ignore[return-value]
    inner = resp.get("result", resp)
    if isinstance(inner, dict):
        data = inner.get("data", inner)
        return data  # type: ignore[return-value]
    return inner  # type: ignore[return-value]


class CompanyBonusTasks(TaskCogBase, name="company_bonus_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.company_bonus_scan.start()

    def cog_unload(self) -> None:
        self.company_bonus_scan.cancel()

    @tasks.loop(hours=_SCAN_INTERVAL_HOURS)
    async def company_bonus_scan(self) -> None:
        if not self._client or not self._db:
            return
        try:
            await self._run_scan()
        except Exception:
            logger.exception("company_bonus_scan: unexpected error")

    @company_bonus_scan.before_loop
    async def before_company_bonus_scan(self) -> None:
        await self._wait_for_services()

    # ── Internals ────────────────────────────────────────────────────────────

    async def _resolve_game_user_id(self, game_username: str) -> str | None:
        """Resolve game username → user_id via the search API."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": game_username})},
            )
            data = _unwrap(raw)
            if isinstance(data, dict):
                ids = data.get("userIds") or []
                if ids:
                    return str(ids[0])
        except Exception as exc:
            logger.debug("company_bonus_scan: search failed for %r: %s", game_username, exc)
        return None

    async def _fetch_company_ids_for_user(self, game_user_id: str) -> list[str]:
        """Return all company IDs owned by *game_user_id*."""
        ids: list[str] = []
        cursor: str | None = None
        for _page in range(20):  # safety cap: 20 × 100 = 2000 companies max
            payload: dict = {"userId": game_user_id, "perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.post("/company.getCompanies", json=payload)
                data = _unwrap(raw)
            except Exception as exc:
                logger.debug("company_bonus_scan: getCompanies failed for %s: %s", game_user_id, exc)
                break
            if not isinstance(data, dict):
                break
            page_ids = data.get("items") or []
            ids.extend(str(i) for i in page_ids if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not page_ids:
                break
        return ids

    async def _fetch_company(self, company_id: str) -> dict | None:
        """Fetch full company details for *company_id*."""
        try:
            raw = await self._client.get(
                "/company.getById",
                params={"input": json.dumps({"companyId": company_id})},
            )
            data = _unwrap(raw)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("company_bonus_scan: getById failed for %s: %s", company_id, exc)
            return None

    async def _fetch_production_bonus_total(self, company_id: str) -> float | None:
        """Return the total production bonus % for *company_id*, or None on error."""
        try:
            raw = await self._client.get(
                "/company.getProductionBonus",
                params={"input": json.dumps({"companyId": company_id})},
            )
            data = _unwrap(raw)
            if isinstance(data, dict):
                return float(data.get("total") or 0)
        except Exception as exc:
            logger.debug("company_bonus_scan: getProductionBonus failed for %s: %s", company_id, exc)
        return None

    async def _run_scan(self) -> None:
        """Main scan loop — checks all watchers."""
        # Skip if we ran recently (e.g. bot restarted within the same hour)
        last_run_str = await self._db.get_poll_state("company_bonus_last_run")
        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str)
                elapsed_minutes = (
                    datetime.now(timezone.utc) - last_run
                ).total_seconds() / 60
                if elapsed_minutes < _MIN_SCAN_INTERVAL_MINUTES:
                    logger.info(
                        "company_bonus_scan: skipping — last run %.1f min ago (< %d min)",
                        elapsed_minutes,
                        _MIN_SCAN_INTERVAL_MINUTES,
                    )
                    return
            except (ValueError, TypeError):
                pass

        watchers = await self._db.get_all_company_bonus_watchers()
        if not watchers:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        # Record scan start time so restart-cooldown check works for this run
        await self._db.set_poll_state("company_bonus_last_run", now_iso)

        for watcher in watchers:
            try:
                await self._check_watcher(watcher, now_iso)
            except Exception:
                logger.exception(
                    "company_bonus_scan: error checking watcher %s",
                    watcher.get("discord_username"),
                )
            # Small yield between users to avoid hammering the API
            await asyncio.sleep(1)

    async def _check_watcher(self, watcher: dict, now_iso: str) -> None:
        discord_user_id: str = watcher["discord_user_id"]
        game_username: str = watcher["game_username"]
        guild_id: str = watcher["guild_id"]
        game_user_id: str | None = watcher.get("game_user_id")

        # Resolve in-game user ID if not yet cached
        if not game_user_id:
            game_user_id = await self._resolve_game_user_id(game_username)
            if not game_user_id:
                logger.warning(
                    "company_bonus_scan: could not resolve game user ID for %r, skipping",
                    game_username,
                )
                return
            await self._db.update_company_bonus_watcher_game_id(discord_user_id, game_user_id)

        company_ids = await self._fetch_company_ids_for_user(game_user_id)
        if not company_ids:
            return

        # Collect currently seen company IDs to prune stale alerts later
        seen_company_ids: set[str] = set()

        for company_id in company_ids:
            company = await self._fetch_company(company_id)
            if not company:
                continue

            # Skip companies that are disabled (disabledAt field present)
            if company.get("disabledAt"):
                continue

            region_id: str = str(company.get("region") or "")
            item_code: str = str(company.get("itemCode") or "")

            if not region_id or not item_code:
                continue

            seen_company_ids.add(company_id)

            bonus_total = await self._fetch_production_bonus_total(company_id)
            if bonus_total is None:
                # API error — skip this company rather than false-alarming
                continue
            has_bonus = bonus_total > 0

            existing_alert = await self._db.get_company_bonus_alert(discord_user_id, company_id)

            if has_bonus:
                # Bonus is back (or was never 0). Remove any stale alert so
                # a future drop can trigger a new notification.
                if existing_alert is not None:
                    await self._db.delete_company_bonus_alert(discord_user_id, company_id)
                continue

            # ── Bonus is 0% in current region ────────────────────────────────
            if existing_alert is not None:
                if existing_alert["region_id"] == region_id:
                    # Already notified for this (company, region) — skip
                    continue
                else:
                    # Company moved to a new region that also has 0% bonus
                    # → delete old alert so we can re-notify
                    await self._db.delete_company_bonus_alert(discord_user_id, company_id)

            # Send the notification
            company_name: str = str(company.get("name") or company_id)
            await self._notify_user(
                discord_user_id=discord_user_id,
                guild_id=guild_id,
                company_name=company_name,
                company_id=company_id,
                item_code=item_code,
                region_id=region_id,
            )
            await self._db.set_company_bonus_alert(
                discord_user_id, company_id, region_id, now_iso
            )
            await asyncio.sleep(0.5)  # small pause between DMs

    async def _notify_user(
        self,
        discord_user_id: str,
        guild_id: str,
        company_name: str,
        company_id: str,
        item_code: str,
        region_id: str,
    ) -> None:
        """Send a DM to the Discord user about a 0% bonus company."""
        try:
            user = await self.bot.fetch_user(int(discord_user_id))
        except Exception as exc:
            logger.warning(
                "company_bonus_scan: could not fetch Discord user %s: %s",
                discord_user_id,
                exc,
            )
            return

        embed = discord.Embed(
            title="⚠️ Bedrijf zonder productiebonus",
            description=(
                f"Je bedrijf **{company_name}** (`{item_code}`) heeft momenteel "
                f"**0% productiebonus** in de regio waar het zich bevindt.\n\n"
                "Overweeg je bedrijf te verplaatsen naar een regio met een bonus."
            ),
            colour=discord.Colour.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        try:
            await user.send(embed=embed)
            logger.info(
                "company_bonus_scan: notified %s about 0%% bonus on company %s (%s)",
                discord_user_id,
                company_name,
                company_id,
            )
        except discord.Forbidden:
            # User has DMs disabled — try to find a fallback channel in the guild
            logger.warning(
                "company_bonus_scan: DM blocked for user %s, trying guild fallback",
                discord_user_id,
            )
            await self._notify_via_guild(discord_user_id, guild_id, embed)
        except Exception as exc:
            logger.warning(
                "company_bonus_scan: failed to DM user %s: %s",
                discord_user_id,
                exc,
            )

    async def _notify_via_guild(
        self, discord_user_id: str, guild_id: str, embed: discord.Embed
    ) -> None:
        """Fallback: ping the user in the bot's configured notification channel."""
        channel_id = self.config.get("channels", {}).get("bot_mededelingen")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(f"<@{discord_user_id}>", embed=embed)
        except Exception as exc:
            logger.warning(
                "company_bonus_scan: guild fallback notification failed for %s: %s",
                discord_user_id,
                exc,
            )


async def setup(bot) -> None:
    await bot.add_cog(CompanyBonusTasks(bot))
