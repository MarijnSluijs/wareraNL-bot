"""Background task: daily luck score refresh for NL citizens."""

from __future__ import annotations

import asyncio
import logging
import math as _luck_math
import time
from datetime import datetime, timezone

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# ── Luck-scoring constants ────────────────────────────────────────────────────

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


def _calc_luck_pct(counts: dict, total: int) -> float:
    """Weighted luck % score.  0 = average, positive = luckier than average.

    Uses Poisson z-score normalisation: (actual - expected) / sqrt(expected).
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


def _seconds_until_hour(target_hour: int) -> float:
    """Seconds to sleep until the next target_hour:00:00 UTC."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


class LuckTasks(TaskCogBase, name="luck_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.daily_luck_refresh.start()

    def cog_unload(self) -> None:
        self.daily_luck_refresh.cancel()

    # ------------------------------------------------------------------ #
    # Daily luck score sweep                                               #
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=24)
    async def daily_luck_refresh(self):
        """Calculate and cache luck scores for all NL citizens once per day."""
        if not self._client or not self._db:
            return

        # Never run on the first tick immediately after startup.
        if self.daily_luck_refresh.current_loop == 0:
            logger.info("daily_luck_refresh: skipping first startup tick")
            return

        now_utc = datetime.now(timezone.utc)
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            return

        # 23-hour cooldown guard
        try:
            last_run_str = await self._db.get_poll_state("luck_refresh_last_run")
            if last_run_str:
                elapsed_h = (
                    now_utc - datetime.fromisoformat(last_run_str)
                ).total_seconds() / 3600
                if elapsed_h < 23:
                    logger.info(
                        "daily_luck_refresh: skipping — last run %.1fh ago (< 23h)",
                        elapsed_h,
                    )
                    return
        except Exception:
            logger.exception("daily_luck_refresh: failed to read last-run state")

        logger.info("daily_luck_refresh: starting NL luck sweep")
        _t0_luck = time.monotonic()
        async with self._heavy_api_lock:
            await self._daily_luck_refresh_sweep(now_utc, nl_country_id, _t0_luck)

    @daily_luck_refresh.before_loop
    async def before_daily_luck_refresh(self):
        await self._wait_for_services()
        # Align to next 09:00 UTC (10:00 NL winter / 11:00 NL summer)
        await asyncio.sleep(_seconds_until_hour(9))

    async def run_luck_refresh(
        self, now_utc: datetime | None = None, progress_cb=None
    ) -> None:
        """Public entry-point for /peil geluk and debug commands."""
        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            return
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        _t0 = time.monotonic()
        async with self._heavy_api_lock:
            await self._daily_luck_refresh_sweep(
                now_utc, nl_country_id, _t0, progress_cb=progress_cb
            )

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _fetch_luck_data(
        self, user_id: str, item_rarities: dict
    ) -> tuple[dict[str, int], int]:
        """Page all openCase transactions for a user. Returns (rarity_counts, total)."""
        counts: dict[str, int] = {r: 0 for r in _LUCK_EXPECTED}
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
                    params={"input": __import__("json").dumps(payload)},
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
            # await asyncio.sleep(0.2)
        return counts, sum(counts.values())

    async def _daily_luck_refresh_sweep(
        self,
        now_utc: datetime,
        nl_country_id: str,
        _t0_luck: float,
        progress_cb=None,
    ) -> None:
        """Heavy part of the luck sweep; must be called with _heavy_api_lock held."""
        try:
            await self._db.set_poll_state("luck_refresh_last_run", now_utc.isoformat())
        except Exception:
            logger.exception("daily_luck_refresh: failed to save last-run state")

        # Load item code → rarity map
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
            logger.exception("daily_luck_refresh: failed to load item rarities")
            return

        citizens = await self._db.get_citizens_for_luck_refresh(nl_country_id)
        total = len(citizens)
        logger.info("daily_luck_refresh: processing %d NL citizens", total)

        if progress_cb:
            try:
                await progress_cb(0, total, 0)
            except Exception:
                logger.debug("daily_luck_refresh: progress_cb failed at start")

        await self._db.delete_luck_scores_for_country(nl_country_id)

        MIN_OPENS = 20
        recorded = 0
        for i, (user_id, citizen_name) in enumerate(citizens):
            try:
                counts, total_opens = await self._fetch_luck_data(
                    user_id, item_rarities
                )
                if total_opens < MIN_OPENS:
                    continue
                luck_pct = _calc_luck_pct(counts, total_opens)
                updated_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                await self._db.upsert_luck_score(
                    user_id,
                    nl_country_id,
                    citizen_name,
                    luck_pct,
                    total_opens,
                    updated_at,
                )
                recorded += 1
            except Exception:
                logger.exception("daily_luck_refresh: error for user %s", user_id)
            if (i + 1) % 10 == 0:
                await self._db.flush_luck_scores()
                await asyncio.sleep(1.0)
            if progress_cb and ((i + 1) % 5 == 0 or (i + 1) == total):
                try:
                    await progress_cb(i + 1, total, recorded)
                except Exception:
                    pass

        await self._db.flush_luck_scores()
        try:
            await self._db.set_poll_state("luck_ranking_total", str(recorded))
        except Exception:
            logger.exception("daily_luck_refresh: failed to save luck_ranking_total")
        logger.info(
            "daily_luck_refresh: complete — %d/%d citizens scored", recorded, total
        )

        if self.bot.testing:
            channels = self.config.get("channels", {})
            ch_id = channels.get("testing-area") or channels.get("bot_mededelingen")
            if ch_id:
                for guild in self.bot.guilds:
                    ch = guild.get_channel(ch_id)
                    if ch:
                        try:
                            _elapsed = time.monotonic() - _t0_luck
                            _m, _s = divmod(int(_elapsed), 60)
                            _dur = f"{_m}m {_s}s" if _m else f"{_elapsed:.1f}s"
                            await ch.send(
                                f"✅ Gelukscores verversing klaar ({_dur}) — {recorded}/{total} NL burgers gescoord"
                            )
                        except Exception:
                            pass
                        break


async def setup(bot) -> None:
    """Add the LuckTasks cog to the bot."""
    await bot.add_cog(LuckTasks(bot))
