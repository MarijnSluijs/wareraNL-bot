"""Background task: fetch wealth for all NL citizens and store in DB.

Runs every 24 hours.  Calls the global ``userWealth`` ranking once to get the
active wealth for every user (personal wallet + active companies), then for
each NL citizen also paginates ``company.getCompanies`` to find
disabled/inactive companies and adds their balance to the total.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from typing import Optional

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Give the citizen_refresh task enough time to populate citizen_levels first.
_STARTUP_DELAY_S = 240  # 4 minutes


# ── Response parsing helpers ──────────────────────────────────────────────────

def _unwrap(resp: object) -> object:
    """Strip tRPC result/data envelopes."""
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v.get("data", v)
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
    """Extract the user ID from a ranking entry."""
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


def _entry_wealth(entry: dict) -> float:
    """Extract the wealth value from a ranking entry."""
    for key in ("value", "wealth", "amount", "total", "balance"):
        v = entry.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


# ── Task cog ──────────────────────────────────────────────────────────────────

class WealthTasks(TaskCogBase, name="wealth_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.wealth_refresh.start()

    def cog_unload(self) -> None:
        self.wealth_refresh.cancel()

    @tasks.loop(time=time(4, 0, tzinfo=timezone.utc))
    async def wealth_refresh(self) -> None:
        if not self._client or not self._db:
            return
        try:
            await self._run_wealth_refresh()
        except Exception:
            logger.exception("wealth_refresh: unexpected error")

    @wealth_refresh.before_loop
    async def before_wealth_refresh(self) -> None:
        await self._wait_for_services()
        logger.info("wealth_refresh: waiting %ds before first run", _STARTUP_DELAY_S)
        await asyncio.sleep(_STARTUP_DELAY_S)

    async def run_wealth_refresh_once(self) -> dict:
        """Public entry point for manual triggers (e.g. /peil wealth).

        Returns a stats dict with at least a ``'saved'`` key.
        """
        return await self._run_wealth_refresh()

    # ------------------------------------------------------------------ #

    async def _run_wealth_refresh(self) -> dict:
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            logger.warning("wealth_refresh: nl_country_id not configured")
            return {"saved": 0}

        logger.info("wealth_refresh: starting")

        # ── 1. Fetch global userWealth ranking ─────────────────────────
        try:
            resp = await self._client.post(
                "/ranking.getRanking",
                json={"rankingType": "userWealth"},
            )
        except Exception as exc:
            logger.warning("wealth_refresh: ranking API request failed: %s", exc)
            return {"saved": 0}

        entries = _extract_ranking_entries(resp)
        logger.info("wealth_refresh: got %d global wealth ranking entries", len(entries))

        # Build lookup: user_id -> (wealth_active, username)
        wealth_map: dict[str, tuple[float, Optional[str]]] = {}
        for entry in entries:
            uid = _entry_user_id(entry)
            wealth = _entry_wealth(entry)
            name = _entry_username(entry)
            if uid:
                wealth_map[uid] = (wealth, name)

        # ── 2. Get all NL citizens from DB ─────────────────────────────
        citizens = await self._db.get_nl_citizen_ids(nl_country_id)
        if not citizens:
            logger.warning("wealth_refresh: no NL citizens in DB")
            return {"saved": 0}

        logger.info("wealth_refresh: processing %d NL citizens", len(citizens))

        now_str = datetime.now(timezone.utc).isoformat()

        # Process citizens concurrently in batches of 20
        _BATCH = 20
        sem = asyncio.Semaphore(_BATCH)

        async def _process_citizen(user_id: str, citizen_name: Optional[str]) -> None:
            async with sem:
                wealth_active, api_name = wealth_map.get(user_id, (0.0, None))
                resolved_name = api_name or citizen_name
                await self._db.upsert_citizen_wealth(
                    user_id=user_id,
                    country_id=nl_country_id,
                    citizen_name=resolved_name,
                    wealth_active=wealth_active,
                    wealth_inactive=0.0,
                    updated_at=now_str,
                )

        await asyncio.gather(*[_process_citizen(uid, name) for uid, name in citizens])
        saved = len(citizens)

        await self._db.flush_citizen_wealth()
        await self._db.set_poll_state("wealth_ranking_total", str(saved))
        await self._db.set_poll_state("wealth_ranking_last_run", now_str)
        logger.info("wealth_refresh: done — %d citizens saved", saved)
        return {"saved": saved}


async def setup(bot) -> None:
    await bot.add_cog(WealthTasks(bot))
