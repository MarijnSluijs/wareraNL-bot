"""Production & citizen tracking cog for the WarEra Discord bot."""

import json
import logging
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context
import asyncio

from services.api_client import APIClient
from services.db import Database
from services.citizen_cache import CitizenCache
from services.country_utils import extract_country_list, find_country, country_id as cid_of, ALL_COUNTRY_NAMES
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

# ── Luck-scoring helpers (used by daily_luck_refresh) ────────────────────────
import math as _luck_math

_LUCK_EXPECTED: dict[str, float] = {
    "mythic": 0.0001, "legendary": 0.0004, "epic": 0.0085,
    "rare": 0.071,    "uncommon": 0.30,    "common": 0.62,
}
_LUCK_WEIGHTS: dict[str, float] = {
    r: -_luck_math.log2(p) for r, p in _LUCK_EXPECTED.items()
}
_LUCK_WEIGHT_TOTAL: float = sum(_LUCK_WEIGHTS.values())

# ── Event-poll helpers (used by event_poll) ───────────────────────────────────
_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"
_WAR_URL    = "https://app.warera.io/war/{war_id}"

_EVENT_POLL_TYPES = ["battleOpened", "warDeclared", "peaceMade", "peace_agreement"]

_EVENT_LABELS: dict[str, str] = {
    "battleOpened":    "⚔️ Slag geopend",
    "warDeclared":     "🚨 Oorlog verklaard",
    "peaceMade":       "🕊️ Vrede gesloten",
    "peace_agreement": "🕊️ Vredesakkoord",
}

_EVENT_TYPE_ALIASES: dict[str, str] = {
    "battleopened": "battleOpened",
    "wardeclared": "warDeclared",
    "peacemade": "peaceMade",
    "peace_agreement": "peace_agreement",
    "peaceagreement": "peace_agreement",
}


def _calc_luck_pct(counts: dict, total: int) -> float:
    """Weighted luck % score. 0 = average, positive = luckier than average.

    Uses Poisson z-score normalisation: (actual - expected) / sqrt(expected).
    This keeps scores in a sensible range regardless of sample size or rarity.
    """
    if total == 0:
        return 0.0
    score = 0.0
    for rarity, expected_rate in _LUCK_EXPECTED.items():
        expected_n = total * expected_rate
        if expected_n <= 0:
            continue
        deviation = (counts.get(rarity, 0) - expected_n) / _luck_math.sqrt(expected_n)
        score += _LUCK_WEIGHTS[rarity] * deviation
    return score / _LUCK_WEIGHT_TOTAL * 100.0


class ProductionChecker(commands.Cog, name="production_checker"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.config = getattr(self.bot, "config", {}) or {}
        self._client: APIClient | None = None
        self._db: Database | None = None
        self._citizen_cache: CitizenCache | None = None
        self._poll_lock: asyncio.Lock = asyncio.Lock()
        # Shared lock: only one heavy sweep (luck refresh / manual peil_burgers) may
        # run at a time.  Concurrent sweeps would saturate the API rate limit.
        self._heavy_api_lock: asyncio.Lock = asyncio.Lock()

    def cog_load(self) -> None:
        asyncio.create_task(self._ensure_services_and_start())

    def cog_unload(self) -> None:
        self.hourly_production_check.cancel()
        self.daily_citizen_refresh.cancel()
        self.daily_luck_refresh.cancel()
        self.event_poll.cancel()
        if self._client:
            asyncio.create_task(self._client.close())
        if self._db:
            asyncio.create_task(self._db.close())

    async def _ensure_services_and_start(self) -> None:
        base_url = self.config.get("api_base_url", "https://api.example.local")
        db_path = self.config.get("external_db_path", "database/external.db")
        api_keys = None
        try:
            with open("_api_keys.json", "r") as kf:
                api_keys = json.load(kf).get("keys", [])
        except Exception:
            self.bot.logger.debug("No _api_keys.json found or failed to parse")

        self._client = APIClient(base_url=base_url, api_keys=api_keys)
        await self._client.start()
        self._db = Database(db_path)
        await self._db.setup()
        # Expose the shared DB on the bot so other cogs (e.g. geluk.py) can reuse
        # the same connection instead of opening a second one (which causes DB-locked errors).
        self.bot._ext_db = self._db
        self._citizen_cache = CitizenCache(self._client, self._db)

        self.hourly_production_check.start()
        self.daily_citizen_refresh.start()
        self.daily_luck_refresh.start()
        self.event_poll.start()

    # ------------------------------------------------------------------ #
    # Hourly production poll                                               #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=15)
    async def hourly_production_check(self):
        """Scheduled wrapper that ensures only one poll runs at a time."""
        import time
        self.bot.logger.info("[production poll] starting")
        t0 = time.monotonic()
        async with self._poll_lock:
            changes = await self._run_poll_once()
        elapsed = time.monotonic() - t0
        if changes:
            self.bot.logger.info(
                "[production poll] done in %.1fs — %d change(s): %s",
                elapsed,
                len(changes),
                ", ".join(f"{item}: {old} → {new}" for item, old, new in changes),
            )
        else:
            self.bot.logger.info("[production poll] done in %.1fs — no changes", elapsed)
        if self.bot.testing:
            channels = self.config.get("channels", {})
            cid = channels.get("testing-area") or channels.get("production")
            if cid:
                for guild in self.bot.guilds:
                    ch = guild.get_channel(cid)
                    if ch:
                        try:
                            m, s = divmod(int(elapsed), 60)
                            dur = f"{m}m {s}s" if m else f"{elapsed:.1f}s"
                            if changes:
                                await ch.send(f"✅ Productiepeiling klaar ({dur}) — {len(changes)} wijziging(en)")
                            else:
                                await ch.send(f"✅ Productiepeiling klaar ({dur}) — geen wijzigingen")
                        except Exception:
                            pass
                        break

    @hourly_production_check.before_loop
    async def before_hourly_production_check(self):
        await self.bot.wait_until_ready()
        # Skip the immediate first fire — wait one full interval before polling
        interval = (
            self.hourly_production_check.hours * 3600
            + self.hourly_production_check.minutes * 60
            + self.hourly_production_check.seconds
        )
        await asyncio.sleep(interval)

    async def _run_poll_once(self) -> list[tuple[str, str, str]]:
        """Perform a single production poll using getRecommendedRegionIdsByItemCode.

        Tracks two tops per item:
          - Permanent leader: highest (strategicBonus + ethicSpecializationBonus)
          - Deposit top: highest total bonus where a deposit is active

        Returns a list of change tuples: (label, old_desc, new_desc).
        """
        self.bot.logger.info("Starting production poll...")
        try:
            channels = self.config.get("channels", {})
            if self.bot.testing:
                market_channel_id = channels.get("testing-area") or channels.get("production")
            else:
                market_channel_id = channels.get("production")
            if not market_channel_id:
                self.bot.logger.warning("Market channel ID not configured")
                return []
            if not self._client or not self.config.get("api_base_url"):
                self.bot.logger.warning("API client or api_base_url not configured")
                return []

            try:
                all_countries = await self._client.get("/country.getAllCountries")
            except Exception:
                self.bot.logger.exception("Failed to fetch country list")
                return []

            country_list = extract_country_list(all_countries)
            if not country_list:
                return []

            now = datetime.utcnow().isoformat() + "Z"

            # cid → country object — used to look up name from a country ID
            cid_to_country: dict[str, dict] = {cid_of(c): c for c in country_list}

            # items_to_poll: set of item codes that have at least one specialized country
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
                            cid_of(country), country.get("code"), country.get("name"),
                            item, pb, json.dumps(country, default=str), now,
                        )
                    except Exception:
                        self.bot.logger.exception("Failed to save snapshot for country %s", cid_of(country))

            # Build regionId → countryId map from region.getRegionsObject.
            # Each region object contains a "country" field = current owner's countryId.
            region_to_cid: dict[str, str] = {}
            try:
                regions_resp = await self._client.get("/region.getRegionsObject")
                regions_data = (
                    regions_resp.get("result", {}).get("data", {})
                    if isinstance(regions_resp, dict) else {}
                )
                if isinstance(regions_data, dict):
                    for rid, robj in regions_data.items():
                        cid = robj.get("country") if isinstance(robj, dict) else None
                        if cid:
                            region_to_cid[rid] = cid
                region_to_name: dict[str, str] = {
                    rid: robj.get("name", rid)
                    for rid, robj in (regions_data.items() if isinstance(regions_data, dict) else [])
                    if isinstance(robj, dict)
                }
            except Exception:
                self.bot.logger.exception("Failed to fetch region map; deposit names will be unavailable")
                region_to_name = {}

            changes: list[tuple[str, str, str]] = []
            for item in items_to_poll:
                try:
                    resp = await self._client.get(
                        "/company.getRecommendedRegionIdsByItemCode",
                        params={"input": json.dumps({"itemCode": item})},
                    )
                except Exception:
                    self.bot.logger.exception("Failed to fetch recommended regions for %s", item)
                    continue

                region_list = self._unwrap_region_list(resp)
                if not region_list:
                    continue

                # ---- Long-term leader: max(strategic + ethicSpec + ethicDeposit) ----
                # ethicDepositBonus is semi-permanent (party ethics), only raw depositBonus is temporary
                top_perm = max(
                    region_list,
                    key=lambda r: (r.get("strategicBonus") or 0) + (r.get("ethicSpecializationBonus") or 0) + (r.get("ethicDepositBonus") or 0),
                )
                perm_strategic = top_perm.get("strategicBonus") or 0
                perm_ethic = top_perm.get("ethicSpecializationBonus") or 0
                perm_ethic_dep = top_perm.get("ethicDepositBonus") or 0
                perm_bonus = perm_strategic + perm_ethic + perm_ethic_dep
                perm_rid = top_perm.get("regionId") or top_perm.get("region_id") or ""
                perm_cid = region_to_cid.get(perm_rid)
                perm_name = cid_to_country[perm_cid]["name"] if perm_cid in cid_to_country else "Unknown"

                if perm_bonus > 0:
                    change = await self._handle_permanent_leader(
                        item, perm_cid or "unknown", perm_name, perm_bonus,
                        perm_strategic, perm_ethic, perm_ethic_dep, now, market_channel_id
                    )
                    if change:
                        changes.append(change)
                else:
                    # Strategic bonus has dropped to 0 for all regions — clear any stale DB entry
                    if self._db:
                        try:
                            await self._db.delete_top_specialization(item)
                        except Exception:
                            self.bot.logger.exception("Failed to clear stale permanent leader for %s", item)

                # ---- Short-term top (longest remaining deposit duration) ----
                deposit_regions = [r for r in region_list if (r.get("depositBonus") or 0) > 0]
                if deposit_regions:
                    def _end_ts(r: dict) -> float:
                        raw = r.get("depositEndAt") or r.get("deposit_end_at") or ""
                        try:
                            from datetime import timezone
                            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                        except Exception:
                            return 0.0

                    # Pick region with highest total bonus; use longest deposit as tiebreaker
                    top_dep = max(deposit_regions, key=lambda r: (r.get("bonus") or 0, _end_ts(r)))
                    dep_total = top_dep.get("bonus") or 0
                    dep_deposit_raw = top_dep.get("depositBonus") or 0
                    dep_ethic_dep_raw = top_dep.get("ethicDepositBonus") or 0
                    dep_perm = (top_dep.get("strategicBonus") or 0) + (top_dep.get("ethicSpecializationBonus") or 0)
                    dep_rid = top_dep.get("regionId") or top_dep.get("region_id") or ""
                    dep_region_name = region_to_name.get(dep_rid, dep_rid)
                    dep_cid = region_to_cid.get(dep_rid)
                    dep_name = cid_to_country[dep_cid]["name"] if dep_cid in cid_to_country else "Unknown"
                    dep_end_at = top_dep.get("depositEndAt") or top_dep.get("deposit_end_at") or ""

                    change = await self._handle_deposit_top(
                        item, dep_rid, dep_region_name, dep_cid or "unknown", dep_name,
                        dep_total, dep_deposit_raw, dep_ethic_dep_raw, dep_perm, dep_end_at, now, market_channel_id,
                    )
                    if change:
                        changes.append(change)

        except Exception as e:
            self.bot.logger.error("Error in production poll: %s", e)
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
        self, item: str, country_id: str, country_name: str,
        bonus: float, strategic_bonus: float, ethic_bonus: float, ethic_deposit_bonus: float,
        now: str, channel_id: int,
    ) -> tuple | None:
        try:
            prev = await self._db.get_top_specialization(item) if self._db else None
        except Exception:
            prev = None

        prev_bonus = float(prev.get("production_bonus") or 0) if prev else 0.0
        # Only report when the best permanent bonus actually increases
        changed = prev is None or (bonus > prev_bonus + 0.01)

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
                        self.bot.logger.exception("Failed sending permanent leader update for %s", item)

        if self._db:
            try:
                await self._db.set_top_specialization(
                    item, country_id, country_name, float(bonus), now,
                    strategic_bonus=strategic_bonus, ethic_bonus=ethic_bonus,
                    ethic_deposit_bonus=ethic_deposit_bonus,
                )
            except Exception:
                self.bot.logger.exception("Failed to persist permanent leader for %s", item)

        if changed and prev is not None:
            old_desc = f"{prev.get('country_name')} ({prev.get('production_bonus')}%)"
            return (item, old_desc, f"{country_name} ({bonus}%)")
        return None

    async def _handle_deposit_top(
        self, item: str, region_id: str, region_name: str, country_id: str, country_name: str,
        bonus: int, deposit_bonus: float, ethic_deposit_bonus: float,
        permanent_bonus: float, deposit_end_at: str, now: str, channel_id: int,
    ) -> tuple | None:
        try:
            prev = await self._db.get_deposit_top(item) if self._db else None
        except Exception:
            prev = None

        def _ts(iso: str) -> float:
            try:
                return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
            except Exception:
                return 0.0

        is_new = prev is None
        prev_bonus = float(prev.get("bonus") or 0) if prev else 0.0
        prev_region = (prev.get("region_id") or "") if prev else ""
        # Report when bonus changes OR when a different region takes the lead
        changed = is_new or (bonus != prev_bonus) or (region_id != prev_region)

        if changed and not is_new:
            duration = self._format_duration(deposit_end_at)
            for guild in self.bot.guilds:
                channel = guild.get_channel(channel_id)
                if channel:
                    try:
                        # await channel.send(
                        #     f"⚡ **{item}** nieuwe kortetermijnleider: **{region_name}** — "
                        #     f"**{bonus}%** totaal"
                        #     + (f" ⏳ {duration}" if duration else "")
                        # )
                        pass
                    except Exception:
                        self.bot.logger.exception("Failed sending deposit update for %s", item)

        if self._db:
            try:
                await self._db.set_deposit_top(
                    item, region_id, region_name, country_id, country_name,
                    bonus, deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, now,
                )
            except Exception:
                self.bot.logger.exception("Failed to persist deposit top for %s", item)

        # Only emit a change tuple when the leader actually changed
        if changed and not is_new:
            old_region = prev.get("region_name") or prev.get("region_id") or "?"
            old_bonus = prev.get("bonus") or 0
            return (f"{item} [deposit]", f"{old_region} ({old_bonus}%)", f"{region_name} ({bonus}%)")
        return None

    @staticmethod
    def _format_duration(iso_str: str) -> str | None:
        from datetime import timezone
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
        if t.get("strategic_bonus"): parts.append(f"{t['strategic_bonus']}% strat")
        if t.get("ethic_bonus"): parts.append(f"{t['ethic_bonus']}% eth")
        if t.get("ethic_deposit_bonus"): parts.append(f"{t['ethic_deposit_bonus']}% eth.dep")
        return " + ".join(parts)

    @staticmethod
    def _short_bd(d: dict) -> str:
        parts: list[str] = []
        if d.get("permanent_bonus"): parts.append(f"{d['permanent_bonus']}% perm")
        if d.get("deposit_bonus"): parts.append(f"{d['deposit_bonus']}% dep")
        if d.get("ethic_deposit_bonus"): parts.append(f"{d['ethic_deposit_bonus']}% eth.dep")
        return " + ".join(parts)

    # ------------------------------------------------------------------ #
    # Daily citizen level cache                                            #
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=1)
    async def daily_citizen_refresh(self):
        """Refresh citizen level cache for every country once per hour.

        Uses poll_state to persist the last-run timestamp so restarts don't
        trigger a duplicate refresh within the same 1-hour window.
        """
        if not self._client or not self._db or not self._citizen_cache:
            return

        # Never run on the first tick immediately after startup.
        if self.daily_citizen_refresh.current_loop == 0:
            self.bot.logger.info("daily_citizen_refresh: skipping first startup tick")
            return

        from datetime import timezone
        now_utc = datetime.now(timezone.utc)

        # Check if a refresh already happened in the last 24 hours
        try:
            last_run_str = await self._db.get_poll_state("citizen_refresh_last_run")
            if last_run_str:
                last_run = datetime.fromisoformat(last_run_str)
                elapsed_h = (now_utc - last_run).total_seconds() / 3600
                if elapsed_h < 1:
                    self.bot.logger.info(
                        "daily_citizen_refresh: skipping — last run %.1fh ago (< 1h)", elapsed_h
                    )
                    return
        except Exception:
            self.bot.logger.exception("daily_citizen_refresh: failed to read last-run state")

        self.bot.logger.info("daily_citizen_refresh: starting full country sweep")
        import time as _time
        _t0_citizen = _time.monotonic()
        try:
            all_countries = await self._client.get("/country.getAllCountries")
        except Exception:
            self.bot.logger.exception("daily_citizen_refresh: failed to fetch countries")
            return

        # Persist the start time before the sweep so a crash mid-run doesn't
        # cause an immediate retry on next restart.
        try:
            await self._db.set_poll_state("citizen_refresh_last_run", now_utc.isoformat())
        except Exception:
            self.bot.logger.exception("daily_citizen_refresh: failed to save last-run state")

        country_list = extract_country_list(all_countries)
        total = len(country_list)
        for i, country in enumerate(country_list, 1):
            cid = cid_of(country)
            name = country.get("name", cid)
            self.bot.logger.info("daily_citizen_refresh: (%d/%d) %s", i, total, name)
            try:
                await self._citizen_cache.refresh_country(cid, name)
            except Exception:
                self.bot.logger.exception("daily_citizen_refresh: error refreshing %s", name)
        self.bot.logger.info("daily_citizen_refresh: complete (%d countries)", total)

        if self.bot.testing:
            channels = self.config.get("channels", {})
            cid = channels.get("testing-area") or channels.get("production")
            if cid:
                for guild in self.bot.guilds:
                    ch = guild.get_channel(cid)
                    if ch:
                        try:
                            _elapsed = _time.monotonic() - _t0_citizen
                            _m, _s = divmod(int(_elapsed), 60)
                            _dur = f"{_m}m {_s}s" if _m else f"{_elapsed:.1f}s"
                            await ch.send(
                                f"✅ Burgersniveau-verversing klaar ({_dur}) — {total} landen verwerkt"
                            )
                        except Exception:
                            pass
                        break

    @daily_citizen_refresh.before_loop
    async def before_daily_citizen_refresh(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Daily luck score cache                                               #
    # ------------------------------------------------------------------ #

    async def _fetch_luck_data(
        self, user_id: str, item_rarities: dict
    ) -> tuple[dict[str, int], int]:
        """Page all openCase transactions for a user. Returns (rarity_counts, total)."""
        counts: dict[str, int] = {
            r: 0 for r in _LUCK_EXPECTED
        }
        cursor = None
        while True:
            payload: dict = {
                "userId": user_id,
                "transactionType": "openCase",
                "limit": 100,
            }
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.get(
                    "/transaction.getPaginatedTransactions",
                    params={"input": json.dumps(payload)},
                )
            except Exception:
                break
            data = (
                raw.get("result", {}).get("data", raw)
                if isinstance(raw, dict) else {}
            )
            if isinstance(data, dict):
                items = (
                    data.get("items")
                    or data.get("transactions")
                    or []
                )
                cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                items = data
                cursor = None
            else:
                break
            for tx in items:
                if not isinstance(tx, dict):
                    continue
                # skip elite (mythic) case openings
                if item_rarities.get(tx.get("itemCode", "")) == "mythic":
                    continue
                received = tx.get("item") or {}
                item_code = (
                    received.get("code") if isinstance(received, dict) else received
                ) or ""
                rarity = item_rarities.get(item_code, "common")
                counts[rarity] = counts.get(rarity, 0) + 1
            if not cursor or not items:
                break
            await asyncio.sleep(0.2)
        return counts, sum(counts.values())

    @tasks.loop(hours=24)
    async def daily_luck_refresh(self):
        """Calculate and cache luck scores for all NL citizens once per day."""
        if not self._client or not self._db:
            return

        # Never run on the first tick immediately after startup.
        if self.daily_luck_refresh.current_loop == 0:
            self.bot.logger.info("daily_luck_refresh: skipping first startup tick")
            return

        from datetime import timezone
        now_utc = datetime.now(timezone.utc)
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            return

        # 23-hour cooldown guard
        try:
            last_run_str = await self._db.get_poll_state("luck_refresh_last_run")
            if last_run_str:
                elapsed_h = (
                    (now_utc - datetime.fromisoformat(last_run_str)).total_seconds() / 3600
                )
                if elapsed_h < 23:
                    self.bot.logger.info(
                        "daily_luck_refresh: skipping — last run %.1fh ago (< 23h)", elapsed_h
                    )
                    return
        except Exception:
            self.bot.logger.exception("daily_luck_refresh: failed to read last-run state")

        self.bot.logger.info("daily_luck_refresh: starting NL luck sweep")
        import time as _time
        _t0_luck = _time.monotonic()
        async with self._heavy_api_lock:
            await self._daily_luck_refresh_sweep(now_utc, nl_country_id, _t0_luck)

    async def _daily_luck_refresh_sweep(
        self, now_utc, nl_country_id: str, _t0_luck: float, progress_cb=None
    ) -> None:
        """The heavy part of daily_luck_refresh; must be called with _heavy_api_lock held."""
        import time as _time
        try:
            await self._db.set_poll_state("luck_refresh_last_run", now_utc.isoformat())
        except Exception:
            self.bot.logger.exception("daily_luck_refresh: failed to save last-run state")

        # Load item code → rarity map
        try:
            raw = await self._client.get(
                "/gameConfig.getGameConfig", params={"input": "{}"}
            )
            data = raw.get("result", {}).get("data", raw) if isinstance(raw, dict) else {}
            item_rarities: dict[str, str] = {
                code: item.get("rarity")
                for code, item in (data.get("items") or {}).items()
                if item.get("rarity")
            }
        except Exception:
            self.bot.logger.exception("daily_luck_refresh: failed to load item rarities")
            return

        # Get NL citizens from citizen_levels cache
        citizens = await self._db.get_citizens_for_luck_refresh(nl_country_id)
        total = len(citizens)
        self.bot.logger.info("daily_luck_refresh: processing %d NL citizens", total)

        if progress_cb:
            try:
                await progress_cb(0, total, 0)
            except Exception:
                self.bot.logger.debug("daily_luck_refresh: progress callback failed at start")

        await self._db.delete_luck_scores_for_country(nl_country_id)

        MIN_OPENS = 20
        recorded = 0
        for i, (user_id, citizen_name) in enumerate(citizens):
            try:
                counts, total_opens = await self._fetch_luck_data(user_id, item_rarities)
                if total_opens < MIN_OPENS:
                    continue  # too few opens for a meaningful score
                luck_pct = _calc_luck_pct(counts, total_opens)
                updated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                await self._db.upsert_luck_score(
                    user_id, nl_country_id, citizen_name, luck_pct, total_opens, updated_at
                )
                recorded += 1
            except Exception:
                self.bot.logger.exception(
                    "daily_luck_refresh: error for user %s", user_id
                )
            # Periodic flush + rate limit
            if (i + 1) % 10 == 0:
                await self._db.flush_luck_scores()
                await asyncio.sleep(1.0)

            if progress_cb and ((i + 1) % 5 == 0 or (i + 1) == total):
                try:
                    await progress_cb(i + 1, total, recorded)
                except Exception:
                    self.bot.logger.debug("daily_luck_refresh: progress callback failed at %d/%d", i + 1, total)

        await self._db.flush_luck_scores()
        # Store the final ranked count so /geluk always shows a consistent denominator.
        try:
            await self._db.set_poll_state("luck_ranking_total", str(recorded))
        except Exception:
            self.bot.logger.exception("daily_luck_refresh: failed to save luck_ranking_total")
        self.bot.logger.info(
            "daily_luck_refresh: complete — %d/%d citizens scored", recorded, total
        )

        if self.bot.testing:
            channels = self.config.get("channels", {})
            ch_id = channels.get("testing-area") or channels.get("production")
            if ch_id:
                for guild in self.bot.guilds:
                    ch = guild.get_channel(ch_id)
                    if ch:
                        try:
                            _elapsed = _time.monotonic() - _t0_luck
                            _m, _s = divmod(int(_elapsed), 60)
                            _dur = f"{_m}m {_s}s" if _m else f"{_elapsed:.1f}s"
                            await ch.send(
                                f"✅ Gelukscores verversing klaar ({_dur}) — {recorded}/{total} NL burgers gescoord"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        break

    @daily_luck_refresh.before_loop
    async def before_daily_luck_refresh(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Event poll (battleOpened, warDeclared, peaceMade)                   #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def event_poll(self) -> None:
        """Poll for new war/battle events and post them to the events channel."""
        if not self._client or not self._db:
            return
        try:
            self.bot.logger.info("Starting event poll...")
            await self._run_event_poll()
        except Exception:
            self.bot.logger.exception("event_poll: unexpected error")

    @event_poll.before_loop
    async def before_event_poll(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _extract_event_type(event: dict) -> str:
        """Extract and normalize event type from varying API payload shapes."""
        if not isinstance(event, dict):
            return "unknown"
        edata = event.get("data") or event.get("eventData") or {}
        raw = (
            event.get("type")
            or event.get("eventType")
            or event.get("event_type")
            or (edata.get("type") if isinstance(edata, dict) else None)
            or (edata.get("eventType") if isinstance(edata, dict) else None)
            or "unknown"
        )
        normalized = str(raw).strip()
        key = normalized.lower()
        return _EVENT_TYPE_ALIASES.get(key, normalized)

    async def _run_event_poll(self) -> None:
        from datetime import timezone
        channel_id = self.config.get("channels", {}).get("events")
        if not channel_id:
            return
        nl_country_id = self.config.get("nl_country_id")
        try:
            resp = await self._client.get(
                "/event.getEventsPaginated",
                params={"input": json.dumps({
                    "limit": 20,
                    "countryId": nl_country_id,
                    "eventTypes": _EVENT_POLL_TYPES,
                })},
            )
        except Exception as exc:
            self.bot.logger.warning("event_poll: failed to fetch events: %s", exc)
            return

        # Unwrap tRPC envelope
        data: dict = {}
        if isinstance(resp, dict):
            inner = resp.get("result", {})
            data = inner.get("data", inner) if isinstance(inner, dict) else resp
        items: list = data.get("items") or data.get("events") or []
        if not items:
            return

        # On the very first tick, mark everything seen to avoid spamming on restart.
        if self.event_poll.current_loop == 0:
            for event in items:
                eid = str(event.get("id") or event.get("_id") or "")
                if eid:
                    await self._db.mark_event_seen(eid)
            self.bot.logger.info(
                "event_poll: startup — marked %d events as seen", len(items)
            )
            return

        for event in items:
            eid = str(event.get("id") or event.get("_id") or "")
            if not eid or await self._db.has_seen_event(eid):
                continue
            event_type = self._extract_event_type(event)
            if event_type not in _EVENT_LABELS:
                self.bot.logger.warning(
                    "event_poll: skipping unsupported event type '%s' (id=%s)",
                    event_type,
                    eid,
                )
                await self._db.mark_event_seen(eid)
                continue
            await self._post_event(event, eid, channel_id)
            await self._db.mark_event_seen(eid)
            await asyncio.sleep(0.5)

    async def _post_event(self, event: dict, event_id: str, channel_id: int) -> None:
        """Build and post an embed for a single game event and store it in the DB."""
        from datetime import timezone
        event_type = self._extract_event_type(event)
        label = _EVENT_LABELS.get(event_type, f"🔔 {event_type}")

        # ── Field extraction ─────────────────────────────────────────────
        # The API may place payload fields at the root level OR in a nested
        # "data" / "eventData" / "payload" sub-object.  Probe all levels.
        sub_dicts: list[dict] = [event]
        for _key in ("data", "eventData", "payload", "battleData"):
            _v = event.get(_key)
            if isinstance(_v, dict):
                sub_dicts.append(_v)

        def _first(*keys: str) -> str | None:
            for d in sub_dicts:
                for k in keys:
                    v = d.get(k)
                    if v:
                        return str(v)
            return None

        def _first_obj(*keys: str) -> dict:
            for d in sub_dicts:
                for k in keys:
                    v = d.get(k)
                    if isinstance(v, dict):
                        return v
            return {}

        battle_id = _first("battleId", "battle_id", "battleID")
        war_id    = _first("warId", "war_id", "warID")

        attacker_obj = _first_obj("attackerCountry", "attacker", "attackerCountryData")
        defender_obj = _first_obj("defenderCountry", "defender", "defenderCountryData")
        region_obj   = _first_obj("region", "regionData")

        attacker_id: str | None = (
            _first("attackerCountryId", "attackerId", "attacker_country_id")
            or attacker_obj.get("_id") or attacker_obj.get("id")
        )
        defender_id: str | None = (
            _first("defenderCountryId", "defenderId", "defender_country_id")
            or defender_obj.get("_id") or defender_obj.get("id")
        )
        region_id: str | None = (
            _first("regionId", "region_id", "regionID")
            or region_obj.get("_id") or region_obj.get("id")
        )

        attacker_name: str | None = attacker_obj.get("name") or attacker_obj.get("shortName")
        defender_name: str | None = defender_obj.get("name") or defender_obj.get("shortName")
        region_name:   str | None = (
            region_obj.get("name")
            or _first("regionName", "region_name")
        )

        self.bot.logger.debug(
            "event_poll _post_event: type=%s battle=%s war=%s atk=%s dfn=%s rgn=%s",
            event_type, battle_id, war_id, attacker_id, defender_id, region_id,
        )

        # ── Enrich battleOpened via /battle.getById ───────────────────────
        if event_type == "battleOpened" and battle_id:
            try:
                b_resp = await self._client.get(
                    "/battle.getById",
                    params={"input": json.dumps({"battleId": battle_id})},
                )
                b_data: dict = {}
                if isinstance(b_resp, dict):
                    b_inner = b_resp.get("result") or b_resp
                    b_data = b_inner.get("data", b_inner) if isinstance(b_inner, dict) else b_resp
                self.bot.logger.debug("event_poll battle enrich keys: %s", list(b_data.keys()) if isinstance(b_data, dict) else type(b_data))
                if isinstance(b_data, dict):
                    # Try to extract country objects directly from battle response
                    b_atk = b_data.get("attackerCountry") or b_data.get("attacker") or {}
                    b_dfn = b_data.get("defenderCountry") or b_data.get("defender") or {}
                    b_rgn = b_data.get("region") or {}
                    if isinstance(b_atk, dict):
                        attacker_id   = attacker_id   or b_atk.get("_id") or b_atk.get("id")
                        attacker_name = attacker_name or b_atk.get("name") or b_atk.get("shortName")
                    if isinstance(b_dfn, dict):
                        defender_id   = defender_id   or b_dfn.get("_id") or b_dfn.get("id")
                        defender_name = defender_name or b_dfn.get("name") or b_dfn.get("shortName")
                    if isinstance(b_rgn, dict) and not region_name:
                        region_name = b_rgn.get("name")
                    if isinstance(b_rgn, dict) and not region_id:
                        region_id = b_rgn.get("_id") or b_rgn.get("id")
                    # Flat field fallbacks
                    attacker_id   = attacker_id   or b_data.get("attackerCountryId") or b_data.get("attackerId")
                    defender_id   = defender_id   or b_data.get("defenderCountryId") or b_data.get("defenderId")
                    region_id     = region_id     or b_data.get("regionId")
                    region_name   = region_name   or b_data.get("regionName")
            except Exception:
                self.bot.logger.debug("event_poll: could not enrich battle %s", battle_id)

        # ── Resolve country names via API (only if name is still unknown) ─
        for c_id, slot in [(attacker_id, "attacker"), (defender_id, "defender")]:
            if not c_id:
                continue
            if slot == "attacker" and attacker_name:
                continue
            if slot == "defender" and defender_name:
                continue
            try:
                c_resp = await self._client.get(
                    "/country.getCountryById",
                    params={"input": json.dumps({"countryId": c_id})},
                )
                c_data: dict = {}
                if isinstance(c_resp, dict):
                    c_inner = c_resp.get("result") or c_resp
                    c_data = c_inner.get("data", c_inner) if isinstance(c_inner, dict) else c_resp
                name = (c_data.get("name") or c_data.get("shortName")) if isinstance(c_data, dict) else None
                if name:
                    if slot == "attacker":
                        attacker_name = name
                    else:
                        defender_name = name
            except Exception:
                self.bot.logger.debug("event_poll: could not resolve country %s", c_id)

        # Timestamp
        ts_str = (
            event.get("createdAt")
            or event.get("date")
            or event.get("timestamp")
            or (edata.get("createdAt") if isinstance(edata, dict) else None)
        )
        timestamp: datetime | None = None
        if ts_str:
            try:
                timestamp = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except Exception:
                pass

        # Store in DB
        try:
            await self._db.store_war_event(
                event_id=event_id,
                event_type=event_type,
                battle_id=battle_id,
                war_id=war_id,
                attacker_country_id=attacker_id,
                defender_country_id=defender_id,
                region_id=region_id,
                region_name=region_name,
                attacker_name=attacker_name,
                defender_name=defender_name,
                created_at=ts_str,
                raw_json=json.dumps(event, ensure_ascii=False),
            )
        except Exception:
            self.bot.logger.exception("event_poll: failed to store event %s", event_id)

        # Build embed
        atk = attacker_name or attacker_id or "?"
        dfn = defender_name or defender_id or "?"
        rgn = region_name   or region_id   or "?"

        if event_type == "battleOpened":
            color = discord.Color.red()
            description = f"**{atk}** valt **{dfn}** aan in regio **{rgn}**"
            url = _BATTLE_URL.format(battle_id=battle_id) if battle_id else None
        elif event_type == "warDeclared":
            color = discord.Color.dark_red()
            description = f"**{atk}** heeft oorlog verklaard aan **{dfn}**"
            url = _WAR_URL.format(war_id=war_id) if war_id else None
        elif event_type in ("peaceMade", "peace_agreement"):
            color = discord.Color.green()
            description = f"**{atk}** en **{dfn}** hebben vrede gesloten"
            url = _WAR_URL.format(war_id=war_id) if war_id else None
        else:
            color = discord.Color.blurple()
            description = "Nieuw event ontvangen."
            url = None

        embed = discord.Embed(
            title=label,
            description=description,
            color=color,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        embed.set_footer(text="WarEra Events")

        view = discord.ui.View()
        if url:
            view.add_item(discord.ui.Button(
                label="Bekijk in game", url=url, style=discord.ButtonStyle.link,
            ))

        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed, view=view if url else None)
                    self.bot.logger.info(
                        "event_poll: posted %s (id=%s) to guild %s",
                        event_type, event_id, guild.name,
                    )
                except Exception:
                    self.bot.logger.exception(
                        "event_poll: failed to post to guild %s", guild.name
                    )

    # ------------------------------------------------------------------ #
    # Commands — production                                                #
    # ------------------------------------------------------------------ #

    @commands.command(name="peil_nu")
    @commands.is_owner()
    async def poll_now(self, ctx: Context):
        """Trigger a single production poll immediately."""
        if not self._client:
            await ctx.send("API-client is niet geïnitialiseerd.")
            return
        if self._poll_lock.locked():
            await ctx.send("Er loopt al een productiepeiling.")
            return
        channel = ctx.channel
        await channel.send("Productiepeiling gestart...")

        async def _run_and_report():
            async with self._poll_lock:
                changes = await self._run_poll_once()
            if not changes:
                try:
                    await channel.send("Productiepeiling voltooid: geen wijzigingen gedetecteerd.")
                except Exception:
                    self.bot.logger.exception("Failed to send poll completion message")
                if self.bot.testing:
                    channels = self.config.get("channels", {})
                    cid = channels.get("testing-area") or channels.get("production")
                    if cid:
                        for guild in self.bot.guilds:
                            ch = guild.get_channel(cid)
                            if ch and ch != channel:
                                try:
                                    await ch.send("✅ Productiepeiling klaar — geen wijzigingen")
                                except Exception:
                                    pass
                return
            try:
                lines = []
                for item, prev, new in changes:
                    is_deposit = item.endswith(" [deposit]")
                    base = item[:-10] if is_deposit else item
                    # new/prev are like "Turkey (62.75%)" or "Bahamas (73%)"
                    if is_deposit:
                        lines.append(
                            f"⚡ Depot **{base}** nieuwe kortetermijnleider: **{new}** ← was {prev}"
                        )
                    else:
                        lines.append(
                            f"🏭 Specialisatie **{base}** nieuwe langetermijnleider: **{new}** ← was {prev}"
                        )
                await channel.send(f"Productiepeiling voltooid — {len(changes)} wijziging(en):\n" + "\n".join(lines))
            except Exception:
                self.bot.logger.exception("Failed to send poll report")

        asyncio.create_task(_run_and_report())

    @commands.command(name="nep_leider")
    @commands.is_owner()
    async def fake_leader(self, ctx: Context):
        """Set all stored production bonuses to 0 so the next !poll_now reports changes.

        Useful for testing: run !fake_leader, then !poll_now.  Every item whose
        actual bonus is > 0 will appear as a leadership change.
        """
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return
        try:
            await self._db._conn.execute("UPDATE specialization_top SET production_bonus = 0")
            await self._db._conn.execute("UPDATE deposit_top SET bonus = 0")
            await self._db._conn.commit()
            rows = await self._db._conn.execute("SELECT COUNT(*) FROM specialization_top")
            count = (await rows.fetchone())[0]
            if count == 0:
                await ctx.send(
                    "Tabellen zijn leeg — run eerst `!peil_nu` om ze te vullen, dan `!nep_leider`, dan `!peil_nu` opnieuw."
                )
            else:
                await ctx.send(
                    f"Alle opgeslagen bonussen op nul gezet ({count} items). Run `!peil_nu` — elk item met een echte bonus wordt als nieuwe leider getoond."
                )
        except Exception:
            self.bot.logger.exception("fake_leader: failed to update DB")
            await ctx.send("DB-update mislukt; zie logs.")

    # ------------------------------------------------------------------ #
    # Helper — primary colour for embeds                                   #
    # ------------------------------------------------------------------ #

    def _embed_colour(self) -> discord.Colour:
        raw = (self.config.get("colors") or {}).get("primary", "0xffb612")
        try:
            return discord.Colour(int(str(raw), 16))
        except Exception:
            return discord.Colour.gold()

    # ------------------------------------------------------------------ #
    # /bonus                                                               #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(name="bonus", description="Toon productieleiders voor elk item.")
    async def bonus(self, ctx: Context):
        """Display the current production leaders for each specialization."""
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return
        if hasattr(ctx, 'defer'):
            await ctx.defer()
        try:
            tops = await self._db.get_all_tops()
        except Exception:
            self.bot.logger.exception("Failed to fetch production leaders")
            await ctx.send("Ophalen van productieleiders mislukt; zie logs.")
            return

        deposit_tops: list[dict] = []
        try:
            deposit_tops = await self._db.get_all_deposit_tops()
        except Exception:
            pass

        if not tops and not deposit_tops:
            await ctx.send("Geen productieleiders opgeslagen.")
            return

        dep_by_item = {d.get("item"): d for d in deposit_tops}
        top_by_item = {t.get("item"): t for t in tops}
        all_items = sorted(set(top_by_item) | set(dep_by_item))

        long_rows = [(item, top_by_item[item]) for item in all_items if item in top_by_item]
        short_rows = [(item, dep_by_item[item]) for item in all_items if item in dep_by_item]

        best_l_idx = (
            max(range(len(long_rows)), key=lambda i: float(long_rows[i][1].get("production_bonus") or 0))
            if long_rows else None
        )
        best_s_idx = (
            max(range(len(short_rows)), key=lambda i: float(short_rows[i][1].get("bonus") or 0))
            if short_rows else None
        )

        colour = self._embed_colour()

        # ── Long-term embed ──────────────────────────────────────────────
        if long_rows:
            wi = max(max(len(item) for item, _ in long_rows), 4)
            wc = max(max(len(t.get("country_name") or "") for _, t in long_rows), 7)
            wb = max(max(len(self._pct(t.get("production_bonus"))) for _, t in long_rows), 5)
            bds_l = [self._long_bd(t) for _, t in long_rows]
            wbd = max(max(len(bd) for bd in bds_l), 9)
            hdr_l = f"  {'Item':<{wi}}  {'Land':<{wc}}  {'Bonus':>{wb}}  {'Specificatie':<{wbd}}"
            sep_l = "  " + "-" * (len(hdr_l) - 2)
            rows_l = [
                f"{'>' if i == best_l_idx else ' '} {item:<{wi}}  {(t.get('country_name') or 'Onbekend'):<{wc}}  {self._pct(t.get('production_bonus')):>{wb}}  {bd:<{wbd}}"
                for i, ((item, t), bd) in enumerate(zip(long_rows, bds_l))
            ]
            table_l = "\n".join([hdr_l, sep_l] + rows_l)
        else:
            table_l = "(geen)"

        if short_rows:
            wi2 = max(max(len(item) for item, _ in short_rows), 4)
            wr = max(max(len(d.get("region_name") or d.get("region_id") or "") for _, d in short_rows), 6)
            wb2 = max(max(len(self._pct(d.get("bonus"))) for _, d in short_rows), 5)
            bds_s = [self._short_bd(d) for _, d in short_rows]
            durs = [self._format_duration(d.get("deposit_end_at") or "") or "" for _, d in short_rows]
            wbd2 = max(max(len(bd) for bd in bds_s), 9)
            wdur = max(max(len(dur) for dur in durs), 7)
            hdr_s = f"  {'Item':<{wi2}}  {'Regio':<{wr}}  {'Bonus':>{wb2}}  {'Specificatie':<{wbd2}}  {'Verloopt':<{wdur}}"
            sep_s = "  " + "-" * (len(hdr_s) - 2)
            rows_s = [
                f"{'>' if i == best_s_idx else ' '} {item:<{wi2}}  {(d.get('region_name') or d.get('region_id') or '?'):<{wr}}  {self._pct(d.get('bonus')):>{wb2}}  {bd:<{wbd2}}  {dur:<{wdur}}"
                for i, ((item, d), bd, dur) in enumerate(zip(short_rows, bds_s, durs))
            ]
            table_s = "\n".join([hdr_s, sep_s] + rows_s)
        else:
            table_s = "(geen)"

        MSG_LIMIT = 1900  # plain message limit with safe margin

        async def _send_table(title: str, table_text: str) -> None:
            """Send table as plain code-block message(s) — full channel width."""
            lines = table_text.splitlines()
            header_lines = lines[:2]
            data_lines = lines[2:]
            chunks: list[list[str]] = []
            chunk: list[str] = []
            for line in data_lines:
                body = "\n".join(header_lines + chunk + [line])
                if len(f"**{title}**\n```\n{body}\n```") > MSG_LIMIT and chunk:
                    chunks.append(chunk)
                    chunk = [line]
                else:
                    chunk.append(line)
            if chunk:
                chunks.append(chunk)
            for idx, ch in enumerate(chunks):
                chunk_title = title if idx == 0 else f"{title} (vervolg)"
                block = f"**{chunk_title}**\n```\n" + "\n".join(header_lines + ch) + "\n```"
                await ctx.send(block)

        await _send_table("📈 Langetermijnleiders", table_l)
        await _send_table("⚡ Kortetermijnleiders", table_s)

        # Best-of summary as a compact embed
        best_embed = discord.Embed(colour=colour)
        if best_l_idx is not None:
            bl_item, bl = long_rows[best_l_idx]
            best_embed.add_field(
                name="🏆 Hoogste langetermijn",
                value=f"**{bl_item}** — {bl.get('country_name')} **{bl.get('production_bonus')}%**",
                inline=False,
            )
        if best_s_idx is not None:
            bs_item, bs = short_rows[best_s_idx]
            rl = bs.get("region_name") or bs.get("region_id") or "?"
            dur = self._format_duration(bs.get("deposit_end_at") or "")
            best_embed.add_field(
                name="⚡ Hoogste kortetermijn",
                value=(
                    f"**{bs_item}** — {rl} **{bs.get('bonus')}%**"
                    + (f"  ⏳ {dur}" if dur else "")
                ),
                inline=False,
            )
        if best_embed.fields:
            await ctx.send(embed=best_embed)

    # ------------------------------------------------------------------ #
    # /topbonus                                                            #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(name="topbonus", description="Toon de beste langetermijn- en kortetermijnbonus.")
    async def topbonus(self, ctx: Context):
        """Show the single best long-term and best short-term production bonus."""
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return
        if hasattr(ctx, 'defer'):
            await ctx.defer()
        tops: list[dict] = []
        deposit_tops: list[dict] = []
        try:
            tops = await self._db.get_all_tops()
            deposit_tops = await self._db.get_all_deposit_tops()
        except Exception:
            self.bot.logger.exception("Failed to fetch production data")
            await ctx.send("Ophalen van productiedata mislukt; zie logs.")
            return

        if not tops and not deposit_tops:
            await ctx.send("Nog geen productiedata opgeslagen.")
            return

        colour = self._embed_colour()
        embed = discord.Embed(title="Hoogste Productiebonussen", colour=colour)

        if tops:
            bl = max(tops, key=lambda t: float(t.get("production_bonus") or 0))
            bd = self._long_bd(bl)
            embed.add_field(
                name="🏆 Hoogste langetermijn",
                value=(
                    f"**{bl.get('item')}** — {bl.get('country_name')} **{bl.get('production_bonus')}%**"
                    + (f"\n*{bd}*" if bd else "")
                ),
                inline=False,
            )
        if deposit_tops:
            bs = max(deposit_tops, key=lambda d: float(d.get("bonus") or 0))
            rl = bs.get("region_name") or bs.get("region_id") or "?"
            dur = self._format_duration(bs.get("deposit_end_at") or "")
            bd = self._short_bd(bs)
            embed.add_field(
                name="⚡ Hoogste kortetermijn",
                value=(
                    f"**{bs.get('item')}** — {rl} **{bs.get('bonus')}%**"
                    + (f"  ⏳ {dur}" if dur else "")
                    + (f"\n*{bd}*" if bd else "")
                ),
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command(name="reset_productie")
    @commands.is_owner()
    async def clear_production(self, ctx: Context):
        """Wipe all rows from specialization_top and deposit_top.

        The next poll will repopulate them from scratch.
        """
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return
        try:
            await self._db._conn.execute("DELETE FROM specialization_top")
            await self._db._conn.execute("DELETE FROM deposit_top")
            await self._db._conn.commit()
            await ctx.send("✅ Tabellen `specialization_top` en `deposit_top` gewist. Run `!peil_nu` om ze opnieuw te vullen.")
        except Exception:
            self.bot.logger.exception("Failed to clear production tables")
            await ctx.send("Wissen van tabellen mislukt; zie logs.")

    @commands.hybrid_command(name="verhuiskosten", description="Toon het break-evenpunt om verhuiskosten van een bedrijf terug te verdienen.")
    @app_commands.describe(
        bonuses='Optioneel: huidige bonus, of "huidig nieuw" (bijv. "30" of "30 55"). Leeg laten voor volledige tabel.',
    )
    async def verhuiskosten(self, ctx: Context, bonuses: str = ""):
        """Break-even table: hours of Automated Engine production to recover the 5-concrete move cost.

        Only the bonus *gain* counts — your engine's base output runs regardless of location.
        Rows = new production bonus (5 %–80 %), columns = automated engine level (1–7).
        Rows at or below your current bonus are shown as ∞ (moving gives no gain there).
        Colour: green ≤ 72 h, yellow 73–120 h, red > 120 h / ∞.
        Usage: ``/verhuiskosten``  ``/verhuiskosten 30``  ``/verhuiskosten 30 55``
        """
        # Parse the combined bonuses argument
        parts = bonuses.split()
        bonus: int = 0
        new_bonus: int | None = None
        try:
            if len(parts) >= 1:
                bonus = int(parts[0])
            if len(parts) >= 2:
                new_bonus = int(parts[1])
        except ValueError:
            await ctx.send("Ongeldige invoer. Gebruik `/verhuiskosten`, `/verhuiskosten 30`, of `/verhuiskosten 30 55`.")
            return
        if not self._client:
            await ctx.send("API-client is niet geïnitialiseerd.")
            return
        if hasattr(ctx, "defer"):
            await ctx.defer()

        try:
            prices_resp = await self._client.get("/itemTrading.getPrices")
        except Exception as exc:
            await ctx.send(f"Ophalen van marktprijzen mislukt: {exc}")
            return

        prices = self._unwrap_prices(prices_resp)
        if not prices:
            await ctx.send("Kon marktprijzen niet verwerken vanuit API-antwoord.")
            return

        concrete_price = float(prices.get("concrete") or prices.get("Concrete") or 0)
        if concrete_price <= 0:
            await ctx.send("Betonprijs niet gevonden of nul in marktdata.")
            return
        move_cost = 5.0 * concrete_price

        pp_items = ["grain", "lead", "iron", "limestone"]
        pp_prices = [float(prices[k]) for k in pp_items if prices.get(k) and float(prices[k]) > 0]
        if not pp_prices:
            await ctx.send("Kon niet genoeg artikelprijzen ophalen voor PP-waardeberekening.")
            return
        avg_pp_value = sum(pp_prices) / len(pp_prices)

        colour = self._embed_colour()

        def _fmt_h(h: float) -> str:
            """Format a duration in hours as e.g. '7h' or '3d5h'."""
            total_h = round(h)
            if total_h < 24:
                return f"{total_h}h"
            d, rem = divmod(total_h, 24)
            return f"{d}d{rem}h"

        # ── ANSI colour codes (Discord ansi code block) ──────────────────
        G = "\u001b[32m"   # green  — ≤ 3 d
        Y = "\u001b[33m"   # yellow — 3–5 d
        R = "\u001b[31m"   # red    — > 5 d / ∞
        RESET = "\u001b[0m"

        def _col(h: float) -> str:
            return G if h <= 72 else (Y if h <= 120 else R)

        levels = list(range(1, 8))  # engine level 1 … 7

        # ── Single break-even result (new_bonus provided) ─────────────────
        if new_bonus is not None:
            bonus_gain = new_bonus - bonus
            assumption = (
                f"Verhuizing van **{bonus}%** → **{new_bonus}%** (winst: **+{bonus_gain}%**)"
            )
            if bonus_gain <= 0:
                embed = discord.Embed(
                    title="Break-evenpunt — bedrijfsverhuizing",
                    description=(
                        f"{assumption}\n\n"
                        f"De nieuwe bonus is niet hoger dan je huidige bonus — verhuizing levert geen winst op."
                    ),
                    colour=colour,
                )
            else:
                level_lines = []
                for lv in levels:
                    extra_per_hour = lv * (bonus_gain / 100) * avg_pp_value
                    h = move_cost / extra_per_hour
                    level_lines.append(f"Niveau {lv}: **{_fmt_h(h)}**")
                embed = discord.Embed(
                    title="Break-evenpunt — bedrijfsverhuizing",
                    description=(
                        f"Automated Engine productietijd om de verhuiskosten terug te verdienen.\n"
                        f"{assumption}\n\n"
                        + "\n".join(level_lines)
                        + f"\n\n**Verhuiskosten:** 5 × {concrete_price:.2f} = **{move_cost:.2f} coins**\n"
                        f"**Gemiddelde PP-waarde:** {avg_pp_value:.4f} coins/pp"
                    ),
                    colour=colour,
                )
            await ctx.send(embed=embed)
            return

        # ── Full table ────────────────────────────────────────────────────
        bonuses = list(range(5, 85, 5))   # 5 % … 80 % in steps of 5
        CELL = 6  # visual chars per cell (e.g. "  45h" or "3d5h")

        # "Automated Engine Level" centred over the level columns
        level_cols_width = 6 * len(levels)
        eng_label = "Automated Engine Level"
        pad_left = max(0, (level_cols_width - len(eng_label)) // 2)
        eng_header = " " * 7 + " " * pad_left + eng_label

        hdr = f"{'Bonus':>5} │" + "".join(f" {'Lv'+str(lv):<{CELL}}" for lv in levels)
        sep = "──────┼" + "─" * (6 * len(levels))

        rows = []
        for b in bonuses:
            bonus_gain = b - bonus
            cells = []
            for lv in levels:
                if bonus_gain <= 0:
                    cells.append(f"{R}{'∞':>{CELL}}{RESET}")
                else:
                    extra_per_hour = lv * (bonus_gain / 100) * avg_pp_value
                    h = move_cost / extra_per_hour
                    cells.append(f"{_col(h)}{_fmt_h(h):>{CELL}}{RESET}")
            rows.append(f" {b:>3}% │" + "".join(f" {c}" for c in cells))

        table = (
            "```ansi\n"
            + eng_header + "\n"
            + hdr + "\n"
            + sep + "\n"
            + "\n".join(rows)
            + "\n```"
        )

        if bonus > 0:
            assumption = (
                f"Je huidige productiebonus is **{bonus}%**.\n"
                f"Voeg een tweede getal toe voor een specifiek doel, bijv. `/verhuiskosten {bonus} 55`."
            )
        else:
            assumption = (
                "Je bedrijf heeft momenteel **geen productiebonus**.\n"
                "Je kunt je huidige bonus als eerste getal opgeven (bijv. `/verhuiskosten 30`), "
                "en optioneel een doelbonus als tweede getal (bijv. `/verhuiskosten 30 55`)."
            )
        embed = discord.Embed(
            title="Break-evenpunt — bedrijfsverhuizing",
            description=(
                f"Automated Engine productietijd om de verhuiskosten terug te verdienen.\n"
                f"{assumption}\n\n"
                f"**Verhuiskosten:** 5 × {concrete_price:.2f} = **{move_cost:.2f} coins**\n"
                f"**Gemiddelde PP-waarde:** {avg_pp_value:.4f} coins/pp"
            ),
            colour=colour,
        )

        if len(table) <= 1990:
            await ctx.send(table)
        else:
            await ctx.send(table[:1990] + "\n```")
        await ctx.send(embed=embed)

    @staticmethod
    def _unwrap_prices(resp) -> dict[str, float]:
        """Extract a {itemCode: price} dict from various API response shapes."""
        def _from_dict(d: dict) -> dict[str, float]:
            out: dict[str, float] = {}
            for k, v in d.items():
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    pass
            return out

        if isinstance(resp, dict):
            # Try result.data first
            data = (resp.get("result") or {}).get("data") if isinstance(resp.get("result"), dict) else None
            if isinstance(data, dict):
                result = _from_dict(data)
                if result:
                    return result
            # Try root-level keys that look like item codes
            candidate = _from_dict(resp)
            if candidate:
                return candidate
            # Try a list under any key
            for v in resp.values():
                if isinstance(v, list):
                    out: dict[str, float] = {}
                    for entry in v:
                        if isinstance(entry, dict):
                            code = entry.get("itemCode") or entry.get("item") or entry.get("code")
                            price = entry.get("price") or entry.get("value")
                            if code and price is not None:
                                try:
                                    out[code] = float(price)
                                except (TypeError, ValueError):
                                    pass
                    if out:
                        return out
        if isinstance(resp, list):
            out = {}
            for entry in resp:
                if isinstance(entry, dict):
                    code = entry.get("itemCode") or entry.get("item") or entry.get("code")
                    price = entry.get("price") or entry.get("value")
                    if code and price is not None:
                        try:
                            out[code] = float(price)
                        except (TypeError, ValueError):
                            pass
            return out
        return {}

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Country helpers (defined before commands that reference them)        #
    # ------------------------------------------------------------------ #

    async def _country_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for country parameters: filters ALL_COUNTRY_NAMES."""
        q = current.strip().lower()
        return [
            app_commands.Choice(name=name, value=name)
            for name in ALL_COUNTRY_NAMES
            if q in name.lower()
        ][:25]

    # ------------------------------------------------------------------ #
    # Commands — citizen levels                                            #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(name="niveauverdeling", description="Toon de niveauverdeling van burgers voor een land (of alle).")
    @app_commands.describe(
        country="Kies een land, of leeg laten voor alle landen.",
        all_levels="Toon individuele niveaus in plaats van groepen van 5",
    )
    @app_commands.autocomplete(country=_country_autocomplete)
    async def leveldist(self, ctx: Context, country: str | None = None, all_levels: bool = False):
        """Show the cached level distribution for a country, or all countries if no argument given.

        Accepts a country code or name.
        Usage: ``/niveauverdeling NL``  ``/niveauverdeling Netherlands all_levels:True``  ``/niveauverdeling`` (all)
        Prefix shorthand: ``!niveauverdeling NL all``  (trailing 'all' enables all_levels)
        """
        # Prefix mode may pass all_levels inside the country string; strip it here.
        if country and country.lower().endswith(" all"):
            country = country[:-4].strip() or None
            all_levels = True

        if not self._db:
            await ctx.send("Diensten niet geïnitialiseerd.")
            return

        if hasattr(ctx, 'defer'):
            await ctx.defer()

        country_name = "Alle landen"
        cid: str | None = None

        if country:
            country_list = await self._fetch_country_list(ctx)
            if country_list is None:
                return
            target = find_country(country, country_list)
            if target is None:
                await ctx.send(f"Land `{country}` niet gevonden.")
                return
            cid = cid_of(target)
            country_name = target.get("name", country)

        try:
            level_counts, active_counts, last_updated = await self._db.get_level_distribution(cid)
        except Exception as exc:
            await ctx.send(f"Databasefout: {exc}")
            return

        if not level_counts:
            await ctx.send(
                f"Nog geen gecachte niveaudata voor **{country_name}**.\n"
                f"Run `/peil_burgers{' ' + country if country else ''}` om de cache op te bouwen."
            )
            return

        total = sum(level_counts.values())
        has_active = bool(active_counts)
        colour = self._embed_colour()

        # ── ANSI colour for active portion of bars ──────────────────────
        _GRN = "\033[32m"   # green
        _RST = "\033[0m"    # reset
        BAR_W = 20

        def _make_bar(total_cnt: int, bar_max: int, active_cnt: int = 0) -> str:
            total_filled = max(1, round(total_cnt / bar_max * BAR_W))
            if has_active and active_cnt > 0:
                active_filled = min(max(1, round(active_cnt / bar_max * BAR_W)), total_filled)
                inactive_filled = total_filled - active_filled
                return (
                    _GRN + "█" * active_filled + _RST
                    + "█" * inactive_filled
                    + "░" * (BAR_W - total_filled)
                )
            return "█" * total_filled + "░" * (BAR_W - total_filled)

        if all_levels:
            # Individual level rows
            max_level = max(level_counts)
            bar_max = max(level_counts.values())
            if has_active:
                header = f"{'Lvl':>4}  {'Totaal (actief)':>15}  Bar"
                sep = "─" * 46
                data_rows = [
                    f"{lvl:>4}  {level_counts[lvl]:>5} ({active_counts.get(lvl, 0):>4})  "
                    f"{_make_bar(level_counts[lvl], bar_max, active_counts.get(lvl, 0))}"
                    for lvl in range(1, max_level + 1) if lvl in level_counts
                ]
            else:
                header = f"{'Lvl':>4}  {'Count':>6}  Bar"
                sep = "─" * 32
                data_rows = [
                    f"{lvl:>4}  {level_counts[lvl]:>6}  {_make_bar(level_counts[lvl], bar_max)}"
                    for lvl in range(1, max_level + 1) if lvl in level_counts
                ]
        else:
            # Bucket rows of 5 levels
            max_level = max(level_counts)
            buckets: dict[int, int] = {}
            active_buckets: dict[int, int] = {}
            for lvl, cnt in level_counts.items():
                bucket = ((lvl - 1) // 5) * 5 + 1
                buckets[bucket] = buckets.get(bucket, 0) + cnt
            for lvl, cnt in active_counts.items():
                bucket = ((lvl - 1) // 5) * 5 + 1
                active_buckets[bucket] = active_buckets.get(bucket, 0) + cnt
            bar_max = max(buckets.values())
            if has_active:
                header = f"{'Levels':<9}  {'Totaal (actief)':>15}  Bar"
                sep = "─" * 46
                data_rows = [
                    f"{b:>3}–{min(b+4, max_level):<3}  "
                    f"{buckets[b]:>5} ({active_buckets.get(b, 0):>4})  "
                    f"{_make_bar(buckets[b], bar_max, active_buckets.get(b, 0))}"
                    for b in sorted(buckets)
                ]
            else:
                header = f"{'Levels':<9}  {'Count':>6}  Bar"
                sep = "─" * 34
                data_rows = [
                    f"{b:>3}–{min(b+4, max_level):<3}  {buckets[b]:>6}  {_make_bar(buckets[b], bar_max)}"
                    for b in sorted(buckets)
                ]

        # Send paginated embeds — chunk by character length, not row count
        EMBED_LIMIT = 3900
        label = "All levels" if all_levels else "5-level buckets"
        footer_parts = [f"{total} burgers  •  {label}"]
        if has_active:
            total_active = sum(active_counts.values())
            footer_parts.append(f"{total_active} actief (< 24h)")
            footer_parts.append("█ groen = actief")
        if last_updated:
            footer_parts.append(f"Bijgewerkt: {last_updated[:16].replace('T', ' ')} UTC")
        footer_text = "  •  ".join(footer_parts)

        # Use ``ansi`` block when active data is present so green bars render.
        block_lang = "ansi" if has_active else ""

        chunks: list[list[str]] = []
        current: list[str] = []
        for row in data_rows:
            candidate = "\n".join(current + [row])
            if len(f"```{block_lang}\n{header}\n{sep}\n{candidate}\n```") > EMBED_LIMIT and current:
                chunks.append(current)
                current = [row]
            else:
                current.append(row)
        if current:
            chunks.append(current)

        for page_idx, chunk in enumerate(chunks):
            block = f"```{block_lang}\n{header}\n{sep}\n" + "\n".join(chunk) + "\n```"
            embed = discord.Embed(
                title=f"Niveauverdeling — {country_name}",
                description=block,
                colour=colour,
            )
            embed.set_footer(text=(
                footer_text if page_idx == 0
                else f"{total} burgers  •  {label} (vervolg)"
            ))
            await ctx.send(embed=embed)

    # ── /paraatheid (replaces /skilldist + /skillcooldown) ─────────────

    async def _paraatheid_mu_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete MU names from the citizen_levels cache."""
        if not self._db:
            return []
        try:
            nl_country_id = self.config.get("nl_country_id")
            names = await self._db.get_distinct_mu_names(nl_country_id)
        except Exception:
            return []
        return [
            app_commands.Choice(name=n, value=n)
            for n in names
            if current.lower() in n.lower()
        ][:25]

    @commands.hybrid_command(
        name="paraatheid",
        description="Toon oorlogsparaatheid: wie is al in oorlogsmodus, en wie kan snel wisselen?",
    )
    @app_commands.describe(
        country="Land — overzicht per niveaugroep: %oorlogsmodus + cooldown voor eco-spelers.",
        speler="Zoek een speler op naam of ID.",
        mu="MU-naam — % oorlogsmodus + cooldown voor eco-spelers in de MU.",
        alle_mus="Toon paraatheid voor alle NL MUs in één tabel (geen verdere invoer nodig).",
    )
    @app_commands.autocomplete(country=_country_autocomplete, mu=_paraatheid_mu_autocomplete)
    @app_commands.choices(alle_mus=[app_commands.Choice(name="Ja", value="ja")])
    async def paraatheid(  # noqa: C901  (complex but intentionally monolithic)
        self, ctx: Context,
        country: str | None = None,
        speler: str | None = None,
        mu: str | None = None,
        alle_mus: str | None = None,
    ):
        """/paraatheid — oorlogsparaatheid in vier modi:

        /paraatheid land:NL      — tabel per niveaugroep: %oorlog + cooldown voor eco-spelers
        /paraatheid speler:naam  — of speler al in oorlogsmodus is, of wanneer die kan resetten
        /paraatheid mu:naam      — %oorlog + reset-cooldown voor eco-spelers in de MU
        /paraatheid alle_mus:Ja  — overzicht van alle NL MUs gegroepeerd op type

        Spelers in oorlogsmodus zijn al klaar om te vechten; hun cooldown doet er niet toe.
        Cooldown-info geldt alleen voor spelers in eco-modus die naar oorlog moeten resetten.
        """
        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return

        provided = sum(x is not None for x in (country, speler, mu)) + int(alle_mus is not None)
        if provided == 0:
            await ctx.send(
                "Geef één van de volgende opties op:\n"
                "• `/paraatheid land:NL` — overzicht per niveaugroep\n"
                "• `/paraatheid speler:naam` — één speler opzoeken\n"
                "• `/paraatheid mu:naam` — MU-overzicht\n"
                "• `/paraatheid alle_mus:Ja` — alle NL MUs in één tabel"
            )
            return
        if provided > 1:
            await ctx.send("Geef precies één optie op: **land**, **speler**, **mu** of **alle_mus**.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        colour = self._embed_colour()

        # ══ Mode 1: player ═══════════════════════════════════════════════
        if speler is not None:
            try:
                results = await self._db.find_citizen_readiness(speler)
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return
            if not results:
                await ctx.send(f"Geen speler gevonden voor `{speler}`.")
                return
            # Build a country id→name lookup so we can show proper names
            country_list_p = await self._fetch_country_list(ctx)
            cid_to_name: dict[str, str] = {}
            if country_list_p:
                for _c in country_list_p:
                    _cid = cid_of(_c)
                    _name = _c.get("name") or _c.get("code") or _cid
                    if _cid:
                        cid_to_name[_cid] = _name
            lines_p: list[str] = []
            for r in results:
                mode = r["skill_mode"]
                lvl = r["level"] or "?"
                raw_cid = r["country_id"] or ""
                land = cid_to_name.get(raw_cid) or raw_cid or "?"
                if mode == "war":
                    # Already in war mode → can fight immediately, cooldown irrelevant
                    lines_p.append(
                        f"**{r['citizen_name']}** (lvl {lvl}, land {land})\n"
                        f"⚔️ Paraat — kan nu vechten"
                    )
                else:
                    mode_str = "🌾 eco" if mode == "eco" else "❓ onbekend"
                    if r["can_reset"]:
                        cd_str = "✅ kan nu resetten naar oorlogsmodus"
                    elif r["days_ago"] is not None:
                        remaining = max(0.0, 7 - r["days_ago"])
                        cd_str = f"⏳ nog {remaining:.1f}d wachten voor reset"
                    else:
                        cd_str = "✅ kan nu resetten naar oorlogsmodus"
                    lines_p.append(
                        f"**{r['citizen_name']}** (lvl {lvl}, land {land})\n"
                        f"Skill-mode: {mode_str}\n"
                        f"Cooldown: {cd_str}"
                    )
            embed = discord.Embed(
                title=f"Paraatheid — {speler}",
                description="\n\n".join(lines_p),
                colour=colour,
            )
            await ctx.send(embed=embed)
            return

        # ══ Mode 2: MU ═══════════════════════════════════════════════════
        if mu is not None:
            nl_country_id = self.config.get("nl_country_id")
            try:
                mu_name, players = await self._db.get_mu_readiness_players(mu, nl_country_id)
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return
            if mu_name is None or not players:
                await ctx.send(f"Geen MU gevonden die overeenkomt met `{mu}`.")
                return

            total_mu = len(players)
            war_count = sum(1 for p in players if p["skill_mode"] == "war")
            # Cooldown stats: only eco-players who are still waiting (not yet resettable)
            eco_players = [p for p in players if p["skill_mode"] != "war"]
            can_reset_count = sum(1 for p in eco_players if p["can_reset"])
            waiting_players = [p for p in eco_players if not p["can_reset"] and p["days_ago"] is not None]
            if waiting_players:
                avg_rem = max(0.0, 7 - sum(p["days_ago"] for p in waiting_players) / len(waiting_players))
                cd_avg_str = f"gem. {avg_rem:.1f}d cooldown resterend"
            else:
                cd_avg_str = None
            war_pct = war_count / total_mu * 100 if total_mu else 0.0

            desc_lines = [
                f"⚔️ Paraat: **{war_count}** / {total_mu} ({war_pct:.0f}%)",
            ]
            if can_reset_count:
                desc_lines.append(f"✅ Kan resetten: **{can_reset_count}**")
            if cd_avg_str:
                desc_lines.append(f"⏱️ {cd_avg_str}")

            summary_embed = discord.Embed(
                title=f"Paraatheid — {mu_name}",
                description="\n".join(desc_lines),
                colour=colour,
            )
            await ctx.send(embed=summary_embed)

            # Per-player list — monospace code block so cooldown column aligns
            EMBED_LIMIT_MU = 3900
            # Fixed column widths (emoji counts as ~1 char in code block math)
            NAME_W = 20
            LVL_W  = 2

            async def _flush_mu(lines: list[str]) -> None:
                block = "```\n" + "\n".join(lines) + "\n```"
                embed = discord.Embed(description=block, colour=colour)
                await ctx.send(embed=embed)

            header = f"  {'naam':<{NAME_W}}  {'lv':>{LVL_W}}  cooldown"
            sep    = "─" * len(header)
            pending: list[str] = [header, sep]
            for p in players:
                mode = p["skill_mode"]
                mode_icon = "🌾" if mode == "eco" else ("⚔️" if mode == "war" else "❓")
                name = str(p["citizen_name"] or "?")[:NAME_W].ljust(NAME_W)
                lvl  = str(p["level"] or "?").rjust(LVL_W)
                if mode == "war":
                    cd = "paraat"
                elif p["can_reset"]:
                    cd = "kan nu resetten"
                elif p["days_ago"] is not None:
                    cd = f"⏳ {max(0.0, 7 - p['days_ago']):.1f}d"
                else:
                    cd = "kan nu resetten"
                line = f"{mode_icon} {name}  {lvl}  {cd}"
                candidate = "\n".join(pending + [line])
                if len(candidate) > EMBED_LIMIT_MU and len(pending) > 2:
                    await _flush_mu(pending)
                    pending = [header, sep, line]
                else:
                    pending.append(line)
            if len(pending) > 2:  # more than just header + sep
                await _flush_mu(pending)
            return

        # ══ Mode 3: alle MUs ══════════════════════════════════════════════
        if alle_mus:
            nl_country_id = self.config.get("nl_country_id")
            testing = getattr(self.bot, "testing", False)
            mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
            # Load MU catalogue from mus.json to get type + canonical order
            import json as _json, re as _re
            try:
                with open(mus_json, encoding="utf-8") as _f:
                    _mus_data = _json.load(_f)
            except Exception as exc:
                await ctx.send(f"Kon {mus_json} niet lezen: {exc}")
                return
            # Parse MU type from description: "[**Elite MU**](...)"
            _mu_types: dict[str, str] = {}
            for _emb in _mus_data.get("embeds", []):
                _title = _emb.get("title", "")
                _m = _re.search(r'\[\*\*(.+?)\*\*\]', _emb.get("description", ""))
                _mu_types[_title] = _m.group(1) if _m else "Standaard MU"
            if not _mu_types:
                await ctx.send("Geen MUs gevonden in het configuratiebestand.")
                return
            try:
                mu_stats = await self._db.get_all_mu_readiness(nl_country_id)
            except Exception as exc:
                await ctx.send(f"Databasefout: {exc}")
                return

            # Column widths
            NAME_W_ALL = 22
            HDR = f"{'naam':<{NAME_W_ALL}}  {'par':>5}  {'kan':>3}  {'avg':>5}"
            SEP = "─" * len(HDR)

            # Category config: (type key, emoji, field label) — order matches mu_bericht.py
            _CAT_CFG = [
                ("Elite MU",    "🟠", "🟠 Elite MU"),
                ("Eco MU",      "🟢", "🟢 Eco MU"),
                ("Standaard MU","🔵", "🔵 Standaard MU"),
            ]

            emb = discord.Embed(
                title="Paraatheid — Alle NL MUs",
                description="par = paraat / totaal  •  kan = kan nu resetten  •  avg = gem. wachttijd eco-spelers",
                colour=discord.Color.gold(),
            )
            has_data = False

            for mu_type, _emoji, field_label in _CAT_CFG:
                mu_names_of_type = [n for n, t in _mu_types.items() if t == mu_type]
                if not mu_names_of_type:
                    continue

                rows: list[str] = []
                total_par = total_total = total_kan = 0
                all_waiting: list[float] = []
                for mu_name in mu_names_of_type:
                    stats = mu_stats.get(mu_name)
                    if stats is None:
                        row = f"{mu_name[:NAME_W_ALL]:<{NAME_W_ALL}}  {'?':>5}  {'?':>3}  {'?':>5}"
                    else:
                        par_str = f"{stats['war']}/{stats['total']}"
                        kan_str = str(stats["can_reset"])
                        if stats["waiting_days"]:
                            avg_rem = max(0.0, 7 - sum(stats["waiting_days"]) / len(stats["waiting_days"]))
                            avg_str = f"{avg_rem:.1f}d"
                        else:
                            avg_str = "—"
                        row = f"{mu_name[:NAME_W_ALL]:<{NAME_W_ALL}}  {par_str:>5}  {kan_str:>3}  {avg_str:>5}"
                        total_par   += stats["war"]
                        total_total += stats["total"]
                        total_kan   += stats["can_reset"]
                        all_waiting.extend(stats["waiting_days"])
                    rows.append(row)

                # Category totals row
                if total_total:
                    tot_par_str = f"{total_par}/{total_total}"
                    tot_kan_str = str(total_kan)
                    if all_waiting:
                        tot_avg_rem = max(0.0, 7 - sum(all_waiting) / len(all_waiting))
                        tot_avg_str = f"{tot_avg_rem:.1f}d"
                    else:
                        tot_avg_str = "—"
                    rows.append("─" * len(HDR))
                    rows.append(f"{'totaal':<{NAME_W_ALL}}  {tot_par_str:>5}  {tot_kan_str:>3}  {tot_avg_str:>5}")

                block_text = "```\n" + HDR + "\n" + SEP + "\n" + "\n".join(rows) + "\n```"
                emb.add_field(name=field_label, value=block_text, inline=False)
                has_data = True

            if not has_data:
                await ctx.send("Geen gecachete MU-data gevonden. Voer eerst `/peil_burgers` uit.")
                return
            await ctx.send(embed=emb)
            return

        # ══ Mode 4: country ══════════════════════════════════════════════
        country_list = await self._fetch_country_list(ctx)
        if country_list is None:
            return
        target = find_country(country, country_list)
        if target is None:
            await ctx.send(f"Land `{country}` niet gevonden.")
            return
        cid = cid_of(target)
        country_name = target.get("name", country)

        try:
            skill_buckets, last_updated = await self._db.get_skill_mode_by_level_buckets(cid)
            cd_buckets, _ = await self._db.get_skill_reset_cooldown_by_level_buckets(cid)
        except Exception as exc:
            await ctx.send(f"Databasefout: {exc}")
            return

        if not skill_buckets:
            await ctx.send(
                f"Nog geen gecachte vaardigheidsdata voor **{country_name}**.\n"
                f"Run `/peil_burgers {country}` om de cache op te bouwen."
            )
            return

        # Combined per-bucket table: Levels | Spelers | %Oorlog | Kan reset (eco) | Gem.CD (eco)
        # Note: Kan and CD only count eco-mode players; war-mode players can already fight.
        all_bkts = sorted(set(skill_buckets) | set(cd_buckets))
        max_bucket = max(all_bkts)
        header = f"{'Levels':<9}  {'Spl':>4}  {'%Oor':>4}  {'Kan':>4}  {'CD':>6}"
        sep = "─" * (9 + 2 + 4 + 2 + 4 + 2 + 4 + 2 + 6)

        data_rows: list[str] = []
        for b in all_bkts:
            s = skill_buckets.get(b, {"eco": 0, "war": 0, "unknown": 0})
            c = cd_buckets.get(b, {"count": 0, "avg_days_ago": 0.0, "available": 0, "no_data": 0})
            total_s = s["eco"] + s["war"] + s["unknown"]
            known_s = s["eco"] + s["war"]
            war_pct_b = s["war"] / known_s * 100 if known_s else 0.0
            total_c = c["count"] + c["no_data"]
            avail_pct_b = c["available"] / total_c * 100 if total_c else 0.0
            avg_rem_b = max(0.0, 7 - c["avg_days_ago"]) if c["count"] else None
            cd_str_b = f"{avg_rem_b:.1f}d" if avg_rem_b is not None else "n.v.t."
            b_end = min(b + 4, max_bucket + 4)
            data_rows.append(
                f" {b:>3}–{b_end:<3}  {total_s:>4}  {war_pct_b:>3.0f}%  {avail_pct_b:>3.0f}%  {cd_str_b:>6}"
            )

        EMBED_LIMIT = 3900
        chunks: list[list[str]] = []
        cur_chunk: list[str] = []
        for row in data_rows:
            candidate = "\n".join(cur_chunk + [row])
            if len(f"```\n{header}\n{sep}\n{candidate}\n```") > EMBED_LIMIT and cur_chunk:
                chunks.append(cur_chunk)
                cur_chunk = [row]
            else:
                cur_chunk.append(row)
        if cur_chunk:
            chunks.append(cur_chunk)

        total_eco = sum(v["eco"] for v in skill_buckets.values())
        total_war = sum(v["war"] for v in skill_buckets.values())
        total_known = total_eco + total_war
        total_citizens = sum(v["eco"] + v["war"] + v["unknown"] for v in skill_buckets.values())
        total_avail = sum(v["available"] for v in cd_buckets.values())
        total_cd_data = sum(v["count"] for v in cd_buckets.values())
        war_pct_total = total_war / total_known * 100 if total_known else 0.0
        # avail_pct expressed as % of eco players (war players already fight; only eco needs to reset)
        avail_pct_total = total_avail / total_eco * 100 if total_eco else 0.0

        footer_parts = [f"{total_citizens} burgers"]
        if last_updated:
            footer_parts.append(f"Bijgewerkt: {last_updated[:16].replace('T', ' ')} UTC")
        footer_parts.append("Spl=spelers  %Oor=%Oorlog  Kan/CD=alleen eco-spelers")
        footer_text = "  •  ".join(footer_parts)

        page_embeds: list[discord.Embed] = []
        for page_idx, chunk in enumerate(chunks):
            block = f"```\n{header}\n{sep}\n" + "\n".join(chunk) + "\n```"
            emb = discord.Embed(
                title=f"Paraatheid — {country_name}",
                description=block,
                colour=colour,
            )
            emb.set_footer(text=footer_text if page_idx == 0 else f"{total_citizens} burgers (vervolg)")
            page_embeds.append(emb)

        last = page_embeds[-1]
        if total_known > 0:
            last.add_field(
                name="⚔️ Paraat (oorlogsmodus)",
                value=f"**{total_war}** / {total_citizens} ({war_pct_total:.1f}%)",
                inline=True,
            )
        last.add_field(
            name="✅ Eco — kan nu resetten",
            value=f"**{total_avail}** ({avail_pct_total:.0f}% van eco)",
            inline=True,
        )
        if total_cd_data > 0:
            overall_avg_d = sum(v["avg_days_ago"] * v["count"] for v in cd_buckets.values()) / total_cd_data
            last.add_field(
                name="⏱️ Gem. wachttijd eco-spelers",
                value=f"**{max(0.0, 7 - overall_avg_d):.1f}** dagen",
                inline=True,
            )

        for emb in page_embeds:
            await ctx.send(embed=emb)

    @commands.hybrid_command(name="peil_burgers", description="Ververs de cache voor burgersniveaus.")
    @app_commands.describe(country="Kies een land, of leeg laten voor alle landen.")
    @app_commands.autocomplete(country=_country_autocomplete)
    @has_privileged_role()
    async def poll_citizens(self, ctx: Context, country: str | None = None):
        """Refresh the citizen level cache for one country, or all countries if no argument given.

        Usage: ``/peil_burgers NL``  or  ``/peil_burgers`` (all)
        """
        if not self._client or not self._db or not self._citizen_cache:
            await ctx.send("Diensten niet geïnitialiseerd.")
            return

        if hasattr(ctx, 'defer'):
            await ctx.defer()
        country_list = await self._fetch_country_list(ctx)
        if country_list is None:
            return

        if country:
            target = find_country(country, country_list)
            if target is None:
                await ctx.send(f"Land `{country}` niet gevonden.")
                return
            countries = [target]
        else:
            countries = country_list

        n = len(countries)
        label = f"**{countries[0].get('name', country)}**" if n == 1 else f"**{n}** countries"

        status_msg = await ctx.send(f"Burgersniveau-verversing gestart voor {label}…")

        import time
        t_start = time.monotonic()
        total_recorded = 0
        failed: list[str] = []
        for i, c in enumerate(countries, 1):
            cid = cid_of(c)
            name = c.get("name", cid)
            if n > 1:
                await status_msg.edit(content=f"Refreshing citizen levels… ({i}/{n}) **{name}**")
            try:
                recorded = await self._citizen_cache.refresh_country(
                    cid, name,
                    progress_msg=status_msg if n == 1 else None,
                )
                total_recorded += recorded
                self.bot.logger.info("poll_citizens: %s — %d levels cached", name, recorded)
            except Exception:
                self.bot.logger.exception("poll_citizens: error for %s", name)
                failed.append(name)

        elapsed = time.monotonic() - t_start
        elapsed_str = (
            f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
            if elapsed >= 60
            else f"{elapsed:.1f}s"
        )
        if n == 1:
            summary = f"Citizen level cache refreshed for **{countries[0].get('name', country)}** — {total_recorded} levels stored. ⏱ {elapsed_str}"
        else:
            summary = f"Citizen level cache refreshed for **{n}** countries — {total_recorded} levels stored. ⏱ {elapsed_str}"
        if failed:
            summary += f"\nFailed: {', '.join(failed)}"
        await status_msg.edit(content=summary)

        # Refresh MU memberships for any NL country that was just refreshed
        nl_country_id = self.config.get("nl_country_id")
        if nl_country_id and any(cid_of(c) == nl_country_id for c in countries):
            testing = getattr(self.bot, "testing", False)
            mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
            try:
                mu_count = await self._citizen_cache.refresh_mu_memberships(nl_country_id, mus_json)
                self.bot.logger.info("poll_citizens: MU memberships refreshed — %d assignments", mu_count)
            except Exception:
                self.bot.logger.exception("poll_citizens: MU membership refresh failed")

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

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


async def setup(bot) -> None:
    await bot.add_cog(ProductionChecker(bot))

