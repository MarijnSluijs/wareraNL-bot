"""Background task: global luck score refresh for all citizens in all countries."""

from __future__ import annotations

import asyncio
import json
import logging
import math as _luck_math
import time
from datetime import datetime, timedelta, timezone

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# ── Luck-scoring constants (mirrored from luck.py) ────────────────────────────

_LUCK_EXPECTED: dict[str, float] = {
    "mythic": 0.0001,
    "legendary": 0.0004,
    "epic": 0.0085,
    "rare": 0.071,
    "uncommon": 0.30,
    "common": 0.62,
}
_LUCK_WEIGHTS: dict[str, float] = {
    r: -_luck_math.log2(p) for r, p in _LUCK_EXPECTED.items()
}
_LUCK_WEIGHT_TOTAL: float = sum(_LUCK_WEIGHTS.values())

_ELITE_EXPECTED: dict[str, float] = {
    "mythic": 0.005,
    "legendary": 0.025,
    "epic": 0.15,
    "rare": 0.32,
    "uncommon": 0.50,
    "common": 0.0,
}
_ELITE_LUCK_WEIGHTS: dict[str, float] = {
    r: -_luck_math.log2(p) if p > 0 else 0.0
    for r, p in _ELITE_EXPECTED.items()
}
_ELITE_LUCK_WEIGHT_TOTAL: float = sum(v for v in _ELITE_LUCK_WEIGHTS.values() if v > 0)

MIN_OPENS = 20


def _seconds_until_hour(target_hour: int) -> float:
    """Seconds to sleep until the next target_hour:00:00 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _calc_luck_pct(counts: dict, total: int) -> float:
    """Weighted Poisson z-score luck percentage.  0 = average."""
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


def _calc_elite_luck_pct(counts: dict, total: int) -> float:
    """Poisson z-score luck % for elite case (case2) openings."""
    if total == 0 or _ELITE_LUCK_WEIGHT_TOTAL <= 0:
        return 0.0
    score = 0.0
    for rarity, expected_rate in _ELITE_EXPECTED.items():
        if expected_rate <= 0:
            continue
        expected_n = total * expected_rate
        if expected_n <= 0:
            continue
        deviation = (counts.get(rarity, 0) - expected_n) / _luck_math.sqrt(expected_n)
        score += _ELITE_LUCK_WEIGHTS[rarity] * deviation
    return score / _ELITE_LUCK_WEIGHT_TOTAL * 100.0


class GlobalLuckTasks(TaskCogBase, name="global_luck_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.global_luck_refresh.start()

    def cog_unload(self) -> None:
        self.global_luck_refresh.cancel()

    # ------------------------------------------------------------------ #
    # Periodic global luck sweep (once per day)                           #
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=24)
    async def global_luck_refresh(self) -> None:
        """Calculate and cache luck scores for all citizens in the game."""
        if not self._client or not self._db:
            return

        # Skip the first tick immediately after startup
        if self.global_luck_refresh.current_loop == 0:
            logger.info("global_luck_refresh: skipping first startup tick")
            return

        now_utc = datetime.now(timezone.utc)

        # 23-hour cooldown guard
        try:
            last_run_str = await self._db.get_poll_state("global_luck_refresh_last_run")
            if last_run_str:
                elapsed_h = (
                    now_utc - datetime.fromisoformat(last_run_str)
                ).total_seconds() / 3600
                if elapsed_h < 23:
                    logger.info(
                        "global_luck_refresh: skipping — last run %.1fh ago (< 23h)",
                        elapsed_h,
                    )
                    return
        except Exception:
            logger.exception("global_luck_refresh: failed to read last-run state")

        logger.info("global_luck_refresh: starting global sweep")
        _t0 = time.monotonic()
        async with self._heavy_api_lock:
            await self._run_global_sweep(now_utc, _t0)

    @global_luck_refresh.before_loop
    async def before_global_luck_refresh(self) -> None:
        await self._wait_for_services()
        # Align to next 03:00 UTC so the heavy sweep runs overnight
        await asyncio.sleep(_seconds_until_hour(3))

    async def run_global_luck_refresh(self) -> None:
        """Public entry point for /peil or debug commands."""
        now_utc = datetime.now(timezone.utc)
        _t0 = time.monotonic()
        async with self._heavy_api_lock:
            await self._run_global_sweep(now_utc, _t0)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _fetch_luck_data(
        self, user_id: str, item_rarities: dict
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Page all openCase transactions for a user.

        Returns (normal_counts, elite_counts) separated by case type.
        """
        rarity_keys = list(_LUCK_EXPECTED.keys())
        normal_counts: dict[str, int] = {r: 0 for r in rarity_keys}
        elite_counts: dict[str, int] = {r: 0 for r in rarity_keys}
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
                raw.get("result", {}).get("data", raw) if isinstance(raw, dict) else {}
            )
            if isinstance(data, dict):
                items = data.get("items") or data.get("transactions") or []
                cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                items = data
                cursor = None
            else:
                break
            for tx in items:
                if not isinstance(tx, dict):
                    continue
                opened_case = tx.get("itemCode", "")
                is_elite = item_rarities.get(opened_case) == "mythic"
                received = tx.get("item") or {}
                item_code = (
                    received.get("code") if isinstance(received, dict) else received
                ) or ""
                rarity = item_rarities.get(item_code, "common")
                if is_elite:
                    elite_counts[rarity] = elite_counts.get(rarity, 0) + 1
                else:
                    normal_counts[rarity] = normal_counts.get(rarity, 0) + 1
            if not cursor or not items:
                break
        return normal_counts, elite_counts

    async def _run_global_sweep(self, now_utc: datetime, _t0: float) -> None:
        """Heavy sweep: iterate all cached citizens, compute luck, store in DB."""
        try:
            await self._db.set_poll_state(
                "global_luck_refresh_last_run", now_utc.isoformat()
            )
        except Exception:
            logger.exception("global_luck_refresh: failed to save last-run state")

        # Load item rarity map
        try:
            raw = await self._client.get(
                "/gameConfig.getGameConfig", params={"input": "{}"}
            )
            data = (
                raw.get("result", {}).get("data", raw) if isinstance(raw, dict) else {}
            )
            item_rarities: dict[str, str] = {
                code: item.get("rarity")
                for code, item in (data.get("items") or {}).items()
                if item.get("rarity")
            }
        except Exception:
            logger.exception("global_luck_refresh: failed to load item rarities")
            return

        # All citizens across all countries (from citizen_levels cache)
        citizens = await self._db.get_all_citizens_for_global_luck()
        total = len(citizens)
        logger.info("global_luck_refresh: processing %d citizens globally", total)

        if total == 0:
            logger.warning(
                "global_luck_refresh: citizen_levels is empty — run /peil burgers first"
            )
            return

        # Full wipe and recompute
        await self._db.clear_global_luck()

        recorded = 0
        for i, (user_id, country_id, citizen_name) in enumerate(citizens):
            try:
                normal_counts, elite_counts = await self._fetch_luck_data(
                    user_id, item_rarities
                )
                total_opens = sum(normal_counts.values())
                if total_opens < MIN_OPENS:
                    continue
                luck_pct = _calc_luck_pct(normal_counts, total_opens)
                elite_total = sum(elite_counts.values())
                elite_luck_pct = _calc_elite_luck_pct(elite_counts, elite_total) if elite_total >= 5 else None
                updated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                await self._db.upsert_global_luck_score(
                    user_id,
                    country_id,
                    citizen_name,
                    luck_pct,
                    total_opens,
                    json.dumps(normal_counts),
                    updated_at,
                    elite_luck_score=elite_luck_pct,
                    elite_opens_count=elite_total if elite_total >= 5 else None,
                    elite_rarity_json=json.dumps(elite_counts) if elite_total >= 5 else None,
                )
                recorded += 1
            except Exception:
                logger.exception(
                    "global_luck_refresh: error processing user %s", user_id
                )

            # Batch flush + rate-limit pause every 10 users
            if (i + 1) % 10 == 0:
                await self._db.flush_global_luck_scores()
                await asyncio.sleep(1.0)

            if (i + 1) % 100 == 0:
                logger.info(
                    "global_luck_refresh: %d/%d processed, %d scored so far",
                    i + 1,
                    total,
                    recorded,
                )

        await self._db.flush_global_luck_scores()
        try:
            await self._db.set_poll_state("global_luck_ranking_total", str(recorded))
        except Exception:
            logger.exception(
                "global_luck_refresh: failed to save global_luck_ranking_total"
            )

        elapsed = time.monotonic() - _t0
        m, s = divmod(int(elapsed), 60)
        dur = f"{m}m {s}s" if m else f"{elapsed:.1f}s"
        logger.info(
            "global_luck_refresh: done — %d/%d citizens scored in %s",
            recorded,
            total,
            dur,
        )


async def setup(bot) -> None:
    """Add the GlobalLuckTasks cog to the bot."""
    await bot.add_cog(GlobalLuckTasks(bot))
