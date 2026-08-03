"""Background task: hourly per-player daily damage accumulation.

Polls ``battle.getBattles`` (finished battles, last 48 h) once per hour.
For each new battle, identifies NL citizen participants via
``battleRanking.getRanking`` (type=user), then enriches each NL participant's
record by calling ``battleLootSummary.getByBattleAndUser`` to obtain
``totalDamage``, ``hits``, and ``cases``.  Results are stored in
``daily_dmg_hits`` keyed by ``(battle_id, user_id)``.

Why use battleLootSummary.getByBattleAndUser?
  Unlike the summary endpoint, ``battleRanking.getRanking`` only provides
  total per-player damage.  The loot summary also returns hit counts and
  case-opening data, and may reflect live in-progress damage more accurately
  for battles that finish during the hour.

  The ranking call is still used as a discovery step so we only call the
  loot summary for players who actually participated — keeping API calls
  proportional to the number of NL fighters, not to the roster size.

Data retention:
  ``daily_dmg_hits`` accumulates indefinitely (one row per player per battle).
  Old rows can be pruned via a future maintenance task; the command only
  queries by ``battle_date`` so old data has no query cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_STARTUP_DELAY_S = 60           # seconds after services-ready before first run
_FETCH_LIMIT     = 100          # battles per getBattles page (API max)
_MAX_PAGES       = 200          # hard safety cap (20 000 battles — effectively unlimited)
_LOOKBACK_HOURS  = 48           # how far back to look for new battles


def _unwrap(resp: object) -> object:
    """Strip tRPC result/data envelopes."""
    if not isinstance(resp, dict):
        return resp
    inner = resp.get("result", {})
    if isinstance(inner, dict):
        return inner.get("data", inner)
    return resp


def _objectid_date(oid: str) -> str:
    """Return 'YYYY-MM-DD' (UTC) from a MongoDB ObjectId hex string.

    The first 8 hex characters of an ObjectId encode a Unix timestamp
    (big-endian, seconds since epoch).  This gives the UTC date when
    the document (round or battle) was created, without needing a
    separate timestamp field in the API response.
    """
    ts = int(oid[:8], 16)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class DailyDmgTask(TaskCogBase, name="daily_dmg_task"):
    """Hourly task that stores per-NL-player daily damage in the local DB."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # Superseded by the fetcher's weekly-damage sweep, which derives daily
    # damage from the game's own weekly counter (services/db/damage_history.py)
    # instead of scanning every finished battle and calling
    # battleLootSummary.getByBattleAndUser per participant.  That made this the
    # single most API-expensive task in the bot for data /dailydmg no longer
    # reads.  The sweep is kept for the manual /peil backfill commands, which
    # still populate the legacy per-battle daily_dmg_hits table.
    AUTO_SWEEP_ENABLED = False

    def cog_load(self) -> None:
        if self.AUTO_SWEEP_ENABLED:
            self.hourly_daily_dmg.start()
        else:
            logger.info(
                "daily_dmg_task: hourly sweep disabled — daily damage now comes "
                "from the weekly-damage snapshots written by full_fetcher"
            )

    def cog_unload(self) -> None:
        if self.hourly_daily_dmg.is_running():
            self.hourly_daily_dmg.cancel()

    # ------------------------------------------------------------------ #
    # Loop
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=1)
    async def hourly_daily_dmg(self) -> None:
        if not self._client or not self._db:
            return
        try:
            new_battles, new_hits = await self._run_sweep()
            logger.info(
                "daily_dmg sweep: %d new battles, %d NL player-hits stored",
                new_battles,
                new_hits,
            )
        except Exception:
            logger.exception("daily_dmg sweep: unexpected error")

    @hourly_daily_dmg.before_loop
    async def _before_hourly(self) -> None:
        await self._wait_for_services()
        await asyncio.sleep(_STARTUP_DELAY_S)

    # ------------------------------------------------------------------ #
    # Public entry for manual triggers
    # ------------------------------------------------------------------ #

    async def run_sweep_once(self) -> tuple[int, int]:
        """Run the sweep immediately (e.g. from an owner command)."""
        return await self._run_sweep()

    async def run_backfill(self, days_back: int) -> tuple[int, int]:
        """Backfill daily damage for the last *days_back* days.

        Unlike the regular sweep, this re-processes battles regardless of
        whether they appear in ``daily_dmg_processed`` (force=True).
        Active battles are skipped — backfills only cover finished battles.
        """
        return await self._run_sweep(lookback_hours=days_back * 24, force=True)

    # ------------------------------------------------------------------ #
    # Core sweep
    # ------------------------------------------------------------------ #

    async def _run_sweep(
        self,
        lookback_hours: int = _LOOKBACK_HOURS,
        force: bool = False,
    ) -> tuple[int, int]:
        now_utc = datetime.now(timezone.utc)
        now_str = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        cutoff = (now_utc - timedelta(hours=lookback_hours)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

        client = self._client
        db = self._db
        nl_country_id: Optional[str] = self.config.get("nl_country_id")

        if not nl_country_id:
            logger.warning("daily_dmg: nl_country_id not configured — skipping sweep")
            return 0, 0

        # ── 1. Fetch battles (finished + active) ─────────────────────
        finished_battles: list[dict] = []
        active_battles: list[dict] = []

        for is_active, bucket in ((False, finished_battles), (True, active_battles)):
            cursor: Optional[str] = None
            for page in range(_MAX_PAGES):
                payload: dict = {"isActive": is_active, "limit": _FETCH_LIMIT}
                if cursor:
                    payload["cursor"] = cursor
                try:
                    raw = await client.get(
                        "/battle.getBattles",
                        params={"input": json.dumps(payload)},
                    )
                except Exception as exc:
                    logger.warning(
                        "daily_dmg: getBattles(isActive=%s) page %d failed: %s",
                        is_active, page, exc,
                    )
                    break

                data = _unwrap(raw)
                page_items: list[dict] = []
                next_cursor: Optional[str] = None
                if isinstance(data, dict):
                    page_items = data.get("items", [])
                    next_cursor = data.get("nextCursor") or data.get("cursor")
                elif isinstance(data, list):
                    page_items = data

                for b in page_items:
                    if isinstance(b, dict):
                        created = b.get("createdAt", "")
                        if created >= cutoff:
                            bucket.append(b)

                oldest_on_page = min(
                    (b.get("createdAt", "") for b in page_items if isinstance(b, dict)),
                    default="",
                )
                if oldest_on_page and oldest_on_page < cutoff:
                    break
                if not next_cursor or not page_items:
                    break
                cursor = next_cursor

        # ── 2. Filter finished battles to unprocessed ones ────────────
        finished_ids = [b["_id"] for b in finished_battles if b.get("_id")]
        if force:
            new_finished = [b for b in finished_battles if b.get("_id")]
        else:
            unprocessed_ids = set(await db.filter_daily_dmg_unprocessed(finished_ids))
            new_finished = [b for b in finished_battles if b.get("_id") in unprocessed_ids]

        # In force/backfill mode skip active battles — they may not be finished yet
        battles_to_check_active = [] if force else active_battles

        if not new_finished and not battles_to_check_active:
            logger.debug("daily_dmg: nothing to process (finished=%d all done, active=0)", len(finished_battles))
            return 0, 0

        logger.info(
            "daily_dmg: processing %d new finished + %d active battles (force=%s, lookback=%dh)",
            len(new_finished), len(battles_to_check_active), force, lookback_hours,
        )

        # ── 3. Pre-load NL citizen IDs for filtering ──────────────────
        nl_citizen_ids: set[str] = set(await db.get_country_user_ids(nl_country_id))

        # ── 4. Process finished battles (mark as done afterwards) ─────
        total_new_battles = 0
        total_new_hits = 0

        for battle in new_finished:
            bid: str = battle["_id"]

            hits_added = await self._process_battle(
                client=client,
                db=db,
                battle_id=bid,
                recorded_at=now_str,
                nl_citizen_ids=nl_citizen_ids,
            )

            if not force:
                await db.mark_daily_dmg_processed(bid, None, now_str)
            total_new_battles += 1
            total_new_hits += hits_added
            logger.debug("daily_dmg: finished battle %s — %d NL player-rounds stored", bid, hits_added)

        # ── 5. Process active battles (upsert, never mark processed) ──
        for battle in battles_to_check_active:
            bid = battle["_id"]
            if not bid:
                continue

            hits_updated = await self._process_battle(
                client=client,
                db=db,
                battle_id=bid,
                recorded_at=now_str,
                nl_citizen_ids=nl_citizen_ids,
            )
            # Count active battles only when they have NL participants
            if hits_updated:
                total_new_battles += 1
                total_new_hits += hits_updated
            logger.debug("daily_dmg: active battle %s — %d NL player-rounds updated", bid, hits_updated)

        return total_new_battles, total_new_hits

    async def _process_battle(
        self,
        *,
        client,
        db,
        battle_id: str,
        recorded_at: str,
        nl_citizen_ids: set[str],
    ) -> int:
        """Fetch NL participant damage for one battle and persist to DB.

        Strategy:
          1. Call battle.getLiveBattleData to discover ALL round IDs for this
             battle (roundIds = active round, roundHistory = completed rounds).
             A battle typically has 2-3 rounds; damage is stored per round.
          2. For each round, call battleRanking.getRanking (type=user, roundId)
             for attacker + defender to get per-round damage per player.
          3. Derive the date of each round from its ObjectId (first 4 bytes
             encode a Unix timestamp in UTC).
          4. Intersect with nl_citizen_ids and persist one row per
             (round_id, user_id) so damage is attributed to the correct day.

        Returns number of NL player-round rows stored.
        """
        round_ids = await self._fetch_round_ids(battle_id)
        stored = 0

        if round_ids:
            for rid in round_ids:
                round_date = _objectid_date(rid)
                round_dmg: dict[str, float] = {}
                for side in ("attacker", "defender"):
                    entries = await self._fetch_battle_ranking(
                        battle_id, side, round_id=rid
                    )
                    for entry in entries:
                        uid = entry.get("user")
                        if isinstance(uid, str) and uid:
                            round_dmg[uid] = (
                                round_dmg.get(uid, 0.0)
                                + float(entry.get("value") or 0)
                            )
                for uid, dmg in round_dmg.items():
                    if uid not in nl_citizen_ids or dmg <= 0:
                        continue
                    await db.insert_daily_dmg_hit(
                        round_id=rid,
                        battle_id=battle_id,
                        user_id=uid,
                        total_damage=dmg,
                        round_date=round_date,
                        recorded_at=recorded_at,
                    )
                    stored += 1
        else:
            # Fallback: no round IDs — query by battleId (single cumulative result).
            # Use battle_id as synthetic round key; derive date from battle ObjectId.
            round_date = _objectid_date(battle_id)
            participant_dmg: dict[str, float] = {}
            for side in ("attacker", "defender"):
                entries = await self._fetch_battle_ranking(battle_id, side)
                for entry in entries:
                    uid = entry.get("user")
                    if isinstance(uid, str) and uid:
                        participant_dmg[uid] = (
                            participant_dmg.get(uid, 0.0)
                            + float(entry.get("value") or 0)
                        )
            for uid, dmg in participant_dmg.items():
                if uid not in nl_citizen_ids or dmg <= 0:
                    continue
                await db.insert_daily_dmg_hit(
                    round_id=battle_id,
                    battle_id=battle_id,
                    user_id=uid,
                    total_damage=dmg,
                    round_date=round_date,
                    recorded_at=recorded_at,
                )
                stored += 1

        if stored:
            await db.commit_daily_dmg()
        return stored

    async def _fetch_round_ids(self, battle_id: str) -> list[str]:
        """Return all round IDs (active + historical) for a battle.

        Calls battle.getLiveBattleData which returns:
          battle.roundIds      — currently active round(s)
          battle.roundHistory  — already-completed rounds
        For a finished battle roundIds is typically empty and all rounds are
        in roundHistory.
        """
        try:
            raw = await self._client.post(
                "/battle.getLiveBattleData",
                json={"battleId": battle_id},
            )
        except Exception as exc:
            logger.debug("daily_dmg: getLiveBattleData %s failed: %s", battle_id, exc)
            return []

        data = _unwrap(raw)
        if not isinstance(data, dict):
            return []

        battle = data.get("battle", {})
        if not isinstance(battle, dict):
            return []

        round_ids: list[str] = []
        for key in ("roundIds", "roundHistory"):
            items = battle.get(key) or []
            if isinstance(items, list):
                for r in items:
                    if isinstance(r, str) and r:
                        round_ids.append(r)
                    elif isinstance(r, dict):
                        rid = r.get("_id") or r.get("id") or r.get("roundId")
                        if rid and isinstance(rid, str):
                            round_ids.append(rid)

        logger.debug(
            "daily_dmg: battle %s — found %d round IDs: %s",
            battle_id, len(round_ids), round_ids,
        )
        return round_ids

    async def _fetch_battle_ranking(
        self, battle_id: str, side: str, *, round_id: Optional[str] = None
    ) -> list[dict]:
        """Fetch battleRanking.getRanking for one battle+side (type=user).

        If *round_id* is provided, filters by roundId (single-round damage).
        Otherwise, filters by battleId (may only cover the last active round).
        """
        payload: dict = {
            "dataType": "damage",
            "type": "user",
            "side": side,
        }
        if round_id:
            payload["roundId"] = round_id
        else:
            payload["battleId"] = battle_id

        try:
            raw = await self._client.get(
                "/battleRanking.getRanking",
                params={
                    "input": json.dumps(payload)
                },
            )
        except Exception as exc:
            logger.debug("daily_dmg: getRanking %s/%s failed: %s", battle_id, side, exc)
            return []

        data = _unwrap(raw)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        if isinstance(data, dict):
            for key in ("items", "ranking", "rankings", "data", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return [e for e in v if isinstance(e, dict)]
        return []


async def setup(bot) -> None:
    await bot.add_cog(DailyDmgTask(bot))
