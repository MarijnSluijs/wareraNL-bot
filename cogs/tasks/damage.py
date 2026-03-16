"""Background task: fetch weekly battle damage for all NL citizens, store in DB.

Runs every hour.  Calls the global weeklyUserDamages ranking once, then
cross-references with the NL citizen IDs stored in citizen_levels.  Any
citizen that appears in the ranking gets their damage upserted.  Citizens with
no damage this week are set to 0 so they still appear (at the bottom) in the
command output.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Wait this long after services are ready before the first run, so the citizen
# refresh task has time to populate citizen_levels first.
_STARTUP_DELAY_S = 180  # 3 minutes


def _unwrap(resp: object) -> object:
    """Strip tRPC result/data envelopes."""
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            inner = v.get("data", v)
            return inner
    return resp


def _extract_ranking_entries(resp: object) -> list[dict]:
    """Return a flat list of ranking entries from a ranking.getRanking response."""
    data = _unwrap(resp)
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for key in ("items", "ranking", "rankings", "data", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
    return []


def _entry_user_id(entry: dict) -> Optional[str]:
    """Extract the user ID from a ranking entry.

    The 'user' field is the citizen's account ID; '_id' is the ranking
    record's own ID — we prefer 'user' so we get the citizen's ID.
    """
    # Prefer the citizen reference field over the entry's own _id
    user = entry.get("user")
    if isinstance(user, str) and user:
        return user
    if isinstance(user, dict):
        for key in ("_id", "id", "userId"):
            v = user.get(key)
            if v:
                return str(v)
    for key in ("userId", "citizenId", "id", "_id"):
        v = entry.get(key)
        if v:
            return str(v)
    return None


def _entry_username(entry: dict) -> Optional[str]:
    """Extract a human-readable username from a ranking entry."""
    for key in ("username", "name", "citizenName"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v
    user = entry.get("user")
    if isinstance(user, dict):
        for key in ("username", "name"):
            v = user.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _entry_damage(entry: dict) -> float:
    """Extract the weekly damage value from a ranking entry."""
    for key in (
        "value",
        "damage",
        "totalDamage",
        "weeklyDamage",
        "weeklyBattleDamage",
        "amount",
    ):
        v = entry.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


class DamageTasks(TaskCogBase, name="damage_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.weekly_damage_refresh.start()

    def cog_unload(self) -> None:
        self.weekly_damage_refresh.cancel()

    @tasks.loop(hours=1)
    async def weekly_damage_refresh(self) -> None:
        if not self._client or not self._db:
            return
        try:
            await self._run_damage_refresh()
        except Exception:
            logger.exception("weekly_damage_refresh: unexpected error")

    @weekly_damage_refresh.before_loop
    async def before_weekly_damage_refresh(self) -> None:
        await self._wait_for_services()
        # Give the citizen_refresh task time to populate citizen_levels.
        logger.info(
            "weekly_damage_refresh: waiting %ds before first run", _STARTUP_DELAY_S
        )
        await asyncio.sleep(_STARTUP_DELAY_S)

    async def run_damage_refresh_once(self) -> tuple[int, int]:
        """Public entry point for manual triggers (e.g. /peil weekschade).

        Returns (updated, zeroed) counts.
        """
        return await self._run_damage_refresh()

    # ------------------------------------------------------------------ #

    async def _run_damage_refresh(self) -> tuple[int, int]:
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            logger.warning("weekly_damage_refresh: nl_country_id not configured")
            return 0, 0

        logger.info("weekly_damage_refresh: starting")

        # ── 1. Fetch global weekly damage ranking ─────────────────────
        try:
            resp = await self._client.post(
                "/ranking.getRanking",
                json={"rankingType": "weeklyUserDamages"},
            )
        except Exception as exc:
            logger.warning("weekly_damage_refresh: API request failed: %s", exc)
            return 0, 0

        entries = _extract_ranking_entries(resp)
        logger.info(
            "weekly_damage_refresh: got %d global ranking entries", len(entries)
        )

        # Build lookup: user_id -> (damage, username) AND lowercase name -> damage
        ranking_map: dict[str, tuple[float, Optional[str]]] = {}
        ranking_by_name: dict[str, float] = {}
        for entry in entries:
            uid = _entry_user_id(entry)
            dmg = _entry_damage(entry)
            name = _entry_username(entry)
            if uid:
                ranking_map[uid] = (dmg, name)
            if name:
                ranking_by_name[name.lower()] = dmg

        logger.info(
            "weekly_damage_refresh: ranking_map has %d entries by ID, %d by name",
            len(ranking_map),
            len(ranking_by_name),
        )

        # ── 2. Get all NL citizens from DB ────────────────────────────
        citizens = await self._db.get_nl_citizen_ids(nl_country_id)
        if not citizens:
            logger.warning("weekly_damage_refresh: no NL citizens in DB")
            return 0, 0

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated = 0
        zeroed = 0

        for user_id, citizen_name in citizens:
            # Try ID match first, then fall back to case-insensitive name match
            if user_id in ranking_map:
                dmg, api_name = ranking_map[user_id]
                resolved_name = api_name or citizen_name
            elif citizen_name and citizen_name.lower() in ranking_by_name:
                dmg = ranking_by_name[citizen_name.lower()]
                resolved_name = citizen_name
            else:
                await self._db.upsert_weekly_damage(
                    user_id=user_id,
                    citizen_name=citizen_name,
                    country_id=nl_country_id,
                    weekly_damage=0.0,
                    updated_at=now_str,
                )
                zeroed += 1
                continue

            await self._db.upsert_weekly_damage(
                user_id=user_id,
                citizen_name=resolved_name,
                country_id=nl_country_id,
                weekly_damage=dmg,
                updated_at=now_str,
            )
            updated += 1

        await self._db.flush_weekly_damages()
        logger.info(
            "weekly_damage_refresh: done — %d with damage, %d at zero",
            updated,
            zeroed,
        )
        return updated, zeroed


async def setup(bot) -> None:
    """Add the DamageTasks cog to the bot."""
    await bot.add_cog(DamageTasks(bot))
