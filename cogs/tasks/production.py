"""Background task: hourly production poll (top producers per item code)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list

logger = logging.getLogger("discord_bot")


def _seconds_until_aligned(interval_minutes: int) -> float:
    """Seconds to sleep until the next clock-aligned interval boundary (UTC)."""
    now = datetime.now(timezone.utc)
    total_min = now.hour * 60 + now.minute
    next_slot = ((total_min // interval_minutes) + 1) * interval_minutes
    delta_min = next_slot - total_min
    delay = delta_min * 60 - now.second - now.microsecond / 1_000_000
    return max(1.0, delay)


class ProductionTasks(TaskCogBase, name="production_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._poll_lock: asyncio.Lock = asyncio.Lock()

    def cog_load(self) -> None:
        self.hourly_production_check.start()

    def cog_unload(self) -> None:
        self.hourly_production_check.cancel()

    # ------------------------------------------------------------------ #
    # Hourly production poll                                               #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=15)
    async def hourly_production_check(self):
        """Scheduled wrapper that ensures only one poll runs at a time."""
        logger.info("[production poll] starting")
        t0 = time.monotonic()
        async with self._poll_lock:
            changes = await self._run_poll_once()
        elapsed = time.monotonic() - t0
        if changes:
            logger.info(
                "[production poll] done in %.1fs — %d change(s): %s",
                elapsed,
                len(changes),
                ", ".join(f"{item}: {old} → {new}" for item, old, new in changes),
            )
        else:
            logger.info("[production poll] done in %.1fs — no changes", elapsed)

    @hourly_production_check.before_loop
    async def before_hourly_production_check(self):
        await self._wait_for_services()
        # Align to next :00 / :15 / :30 / :45 boundary.
        await asyncio.sleep(_seconds_until_aligned(15))

    async def run_poll_once(self) -> list[tuple[str, str, str]]:
        """Public wrapper so /peil can trigger a poll under the production lock."""
        async with self._poll_lock:
            return await self._run_poll_once()

    async def _run_poll_once(self) -> list[tuple[str, str, str]]:
        """Perform a single production poll using getRecommendedRegionIdsByItemCode.

        Tracks two tops per item:
          - Permanent leader: highest (strategicBonus + ethicSpecializationBonus + ethicDepositBonus)
          - Deposit top: highest total bonus where a deposit is active

        Returns a list of change tuples: (label, old_desc, new_desc).
        """
        logger.info("Starting production poll...")
        try:
            channels = self.config.get("channels", {})
            if self.bot.testing:
                market_channel_id = channels.get("testing-area") or channels.get(
                    "bot_mededelingen"
                )
            else:
                market_channel_id = channels.get("bot_mededelingen")
            if not market_channel_id:
                logger.warning("Market channel ID not configured")
                return []
            if not self._client or not self.config.get("api_base_url"):
                logger.warning("API client or api_base_url not configured")
                return []

            try:
                all_countries = await self._client.get("/country.getAllCountries")
            except Exception:
                logger.exception("Failed to fetch country list")
                return []

            country_list = extract_country_list(all_countries)
            if not country_list:
                return []

            now = datetime.utcnow().isoformat() + "Z"
            cid_to_country: dict[str, dict] = {cid_of(c): c for c in country_list}

            items_to_poll: set[str] = set()
            for country in country_list:
                item = (
                    country.get("specializedItem")
                    or country.get("specialized_item")
                    or country.get("specialization")
                )
                if not item:
                    continue
                items_to_poll.add(item)
                if self._db:
                    pb = self._get_permanent_bonus(country)
                    try:
                        await self._db.save_country_snapshot(
                            cid_of(country),
                            country.get("code"),
                            country.get("name"),
                            item,
                            pb,
                            json.dumps(country, default=str),
                            now,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to save snapshot for country %s", cid_of(country)
                        )

            region_to_cid: dict[str, str] = {}
            region_to_name: dict[str, str] = {}
            try:
                regions_resp = await self._client.get("/region.getRegionsObject")
                regions_data = (
                    regions_resp.get("result", {}).get("data", {})
                    if isinstance(regions_resp, dict)
                    else {}
                )
                if isinstance(regions_data, dict):
                    for rid, robj in regions_data.items():
                        cid = robj.get("country") if isinstance(robj, dict) else None
                        if cid:
                            region_to_cid[rid] = cid
                    region_to_name = {
                        rid: robj.get("name", rid)
                        for rid, robj in regions_data.items()
                        if isinstance(robj, dict)
                    }
            except Exception:
                logger.exception(
                    "Failed to fetch region map; deposit names will be unavailable"
                )

            changes: list[tuple[str, str, str]] = []

            # Fetch all item-region data in one batched HTTP request instead of
            # one sequential call per item.
            items_sorted = sorted(items_to_poll)
            item_inputs = [{"itemCode": item} for item in items_sorted]
            item_responses = await self._client.batch_get(
                "/company.getRecommendedRegionIdsByItemCode",
                item_inputs,
                batch_size=max(len(item_inputs), 1),
            )

            for item, resp in zip(items_sorted, item_responses):
                if resp is None:
                    logger.warning("No response for item %s", item)
                    continue

                region_list = self._unwrap_region_list(resp)
                if not region_list:
                    continue

                # ---- Long-term leader ----
                top_perm = max(
                    region_list,
                    key=lambda r: (
                        (r.get("strategicBonus") or 0)
                        + (r.get("ethicSpecializationBonus") or 0)
                        + (r.get("ethicDepositBonus") or 0)
                    ),
                )
                perm_strategic = top_perm.get("strategicBonus") or 0
                perm_ethic = top_perm.get("ethicSpecializationBonus") or 0
                perm_ethic_dep = top_perm.get("ethicDepositBonus") or 0
                perm_bonus = perm_strategic + perm_ethic + perm_ethic_dep
                perm_rid = top_perm.get("regionId") or top_perm.get("region_id") or ""
                perm_cid = region_to_cid.get(perm_rid)
                perm_name = (
                    cid_to_country[perm_cid]["name"]
                    if perm_cid in cid_to_country
                    else "Unknown"
                )

                if perm_bonus > 0:
                    change = await self._handle_permanent_leader(
                        item,
                        perm_cid or "unknown",
                        perm_name,
                        perm_bonus,
                        perm_strategic,
                        perm_ethic,
                        perm_ethic_dep,
                        now,
                        market_channel_id,
                    )
                    if change:
                        changes.append(change)
                else:
                    if self._db:
                        try:
                            await self._db.delete_top_specialization(item)
                        except Exception:
                            logger.exception(
                                "Failed to clear stale permanent leader for %s", item
                            )

                # ---- Short-term top (deposit) ----
                deposit_regions = [
                    r for r in region_list if (r.get("depositBonus") or 0) > 0
                ]
                if deposit_regions:

                    def _end_ts(r: dict) -> float:
                        raw = r.get("depositEndAt") or r.get("deposit_end_at") or ""
                        try:
                            return datetime.fromisoformat(
                                raw.replace("Z", "+00:00")
                            ).timestamp()
                        except Exception:
                            return 0.0

                    top_dep = max(
                        deposit_regions, key=lambda r: (r.get("bonus") or 0, _end_ts(r))
                    )
                    dep_total = top_dep.get("bonus") or 0
                    dep_deposit_raw = top_dep.get("depositBonus") or 0
                    dep_ethic_dep_raw = top_dep.get("ethicDepositBonus") or 0
                    dep_perm = (top_dep.get("strategicBonus") or 0) + (
                        top_dep.get("ethicSpecializationBonus") or 0
                    )
                    dep_rid = top_dep.get("regionId") or top_dep.get("region_id") or ""
                    dep_region_name = region_to_name.get(dep_rid, dep_rid)
                    dep_cid = region_to_cid.get(dep_rid)
                    dep_name = (
                        cid_to_country[dep_cid]["name"]
                        if dep_cid in cid_to_country
                        else "Unknown"
                    )
                    dep_end_at = (
                        top_dep.get("depositEndAt")
                        or top_dep.get("deposit_end_at")
                        or ""
                    )

                    change = await self._handle_deposit_top(
                        item,
                        dep_rid,
                        dep_region_name,
                        dep_cid or "unknown",
                        dep_name,
                        dep_total,
                        dep_deposit_raw,
                        dep_ethic_dep_raw,
                        dep_perm,
                        dep_end_at,
                        now,
                        market_channel_id,
                    )
                    if change:
                        changes.append(change)

        except Exception as e:
            logger.error("Error in production poll: %s", e)
            return []

        return changes

    @staticmethod
    def _get_permanent_bonus(country: dict) -> float | None:
        """Country's permanent production bonus (strategic + party ethics, no deposit)."""
        try:
            rb = country.get("rankings", {}).get("countryProductionBonus")
            if isinstance(rb, dict) and "value" in rb:
                return float(rb["value"])
        except Exception:
            pass
        return None

    @staticmethod
    def _unwrap_region_list(api_response) -> list[dict]:
        if isinstance(api_response, list):
            return [r for r in api_response if isinstance(r, dict)]
        if isinstance(api_response, dict):
            result = api_response.get("result")
            if isinstance(result, dict):
                data = result.get("data")
                if isinstance(data, list):
                    return [r for r in data if isinstance(r, dict)]
            for key in ("data", "items", "regions"):
                v = api_response.get(key)
                if isinstance(v, list):
                    return [r for r in v if isinstance(r, dict)]
        return []

    async def _handle_permanent_leader(
        self,
        item: str,
        country_id: str,
        country_name: str,
        bonus: float,
        strategic_bonus: float,
        ethic_bonus: float,
        ethic_deposit_bonus: float,
        now: str,
        channel_id: int,
    ) -> tuple | None:
        try:
            prev = await self._db.get_top_specialization(item) if self._db else None
        except Exception:
            prev = None

        prev_bonus = float(prev.get("production_bonus") or 0) if prev else 0.0
        changed = prev is None or (bonus > prev_bonus + 0.01)
        changed = changed and (
            country_name != prev.get("country_name") if prev else True
        )

        if changed and prev is not None:
            old_desc = f"{prev.get('country_name')} ({prev.get('production_bonus')}%)"
            for guild in self.bot.guilds:
                channel = guild.get_channel(channel_id)
                if channel:
                    try:
                        await channel.send(
                            f"🏭 **{item}** nieuwe langetermijnleider: **{country_name}** ({bonus}%) — was {old_desc}"
                        )
                    except Exception:
                        logger.exception(
                            "Failed sending permanent leader update for %s", item
                        )

        if self._db:
            try:
                await self._db.set_top_specialization(
                    item,
                    country_id,
                    country_name,
                    float(bonus),
                    now,
                    strategic_bonus=strategic_bonus,
                    ethic_bonus=ethic_bonus,
                    ethic_deposit_bonus=ethic_deposit_bonus,
                )
            except Exception:
                logger.exception("Failed to persist permanent leader for %s", item)

        if changed and prev is not None:
            old_desc = f"{prev.get('country_name')} ({prev.get('production_bonus')}%)"
            return (item, old_desc, f"{country_name} ({bonus}%)")
        return None

    async def _handle_deposit_top(
        self,
        item: str,
        region_id: str,
        region_name: str,
        country_id: str,
        country_name: str,
        bonus: int,
        deposit_bonus: float,
        ethic_deposit_bonus: float,
        permanent_bonus: float,
        deposit_end_at: str,
        now: str,
        channel_id: int,
    ) -> tuple | None:
        try:
            prev = await self._db.get_deposit_top(item) if self._db else None
        except Exception:
            prev = None

        is_new = prev is None
        prev_bonus = float(prev.get("bonus") or 0) if prev else 0.0
        prev_region = (prev.get("region_id") or "") if prev else ""
        changed = is_new or (bonus != prev_bonus) or (region_id != prev_region)

        if changed and not is_new:
            # Notification is currently commented out upstream; keeping the structure
            # so it can be re-enabled later without digging through git history.
            pass

        if self._db:
            try:
                await self._db.set_deposit_top(
                    item,
                    region_id,
                    region_name,
                    country_id,
                    country_name,
                    bonus,
                    deposit_bonus,
                    ethic_deposit_bonus,
                    permanent_bonus,
                    deposit_end_at,
                    now,
                )
            except Exception:
                logger.exception("Failed to persist deposit top for %s", item)

        if changed and not is_new:
            old_region = prev.get("region_name") or prev.get("region_id") or "?"
            old_bonus = prev.get("bonus") or 0
            return (
                f"{item} [deposit]",
                f"{old_region} ({old_bonus}%)",
                f"{region_name} ({bonus}%)",
            )
        return None

    @staticmethod
    def _format_duration(iso_str: str) -> str | None:
        try:
            end = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            delta = end - datetime.now(timezone.utc)
            if delta.total_seconds() <= 0:
                return "verlopen"
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes = remainder // 60
            if hours >= 24:
                days, hrs = divmod(hours, 24)
                return f"{days}d {hrs}h" if hrs else f"{days}d"
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        except Exception:
            return None

    @staticmethod
    def _pct(v) -> str:
        try:
            return f"{float(v):.2f}%"
        except (TypeError, ValueError):
            return "0%"

    @staticmethod
    def _long_bd(t: dict) -> str:
        parts: list[str] = []
        if t.get("strategic_bonus"):
            parts.append(f"{t['strategic_bonus']}% strat")
        if t.get("ethic_bonus"):
            parts.append(f"{t['ethic_bonus']}% eth")
        if t.get("ethic_deposit_bonus"):
            parts.append(f"{t['ethic_deposit_bonus']}% eth.dep")
        return " + ".join(parts)

    @staticmethod
    def _short_bd(d: dict) -> str:
        parts: list[str] = []
        if d.get("permanent_bonus"):
            parts.append(f"{d['permanent_bonus']}% perm")
        if d.get("deposit_bonus"):
            parts.append(f"{d['deposit_bonus']}% dep")
        if d.get("ethic_deposit_bonus"):
            parts.append(f"{d['ethic_deposit_bonus']}% eth.dep")
        return " + ".join(parts)


async def setup(bot) -> None:
    """Add the ProductionTasks cog to the bot."""
    await bot.add_cog(ProductionTasks(bot))
