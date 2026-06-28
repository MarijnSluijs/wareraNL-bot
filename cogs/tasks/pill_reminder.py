"""Background tasks for pill buff reminders.

Two loops:
  * ``pill_buff_scan``  — runs hourly; calls ``user.getUserLite`` for every
    subscribed citizen and updates ``expires_at`` in the DB from ``buffs.buffEndAt``.
  * ``pill_reminder_check`` — runs every 30 seconds; sends a DM when a
    subscriber's pill buff has fewer than 10 minutes remaining.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_PILL_ITEM_CODE  = "cocain"
_WARN_SECONDS_10 = 600   # 10 minutes
_WARN_SECONDS_30 = 1800  # 30 minutes


def _parse_iso(ts: str | None) -> int | None:
    """Parse an ISO 8601 timestamp to a Unix timestamp (int seconds UTC)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


class PillReminderTask(TaskCogBase, name="pill_reminder_task"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.pill_buff_scan.start()
        self.pill_reminder_check.start()

    def cog_unload(self) -> None:
        self.pill_buff_scan.cancel()
        self.pill_reminder_check.cancel()

    # ── Hourly API scan ───────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def pill_buff_scan(self) -> None:
        if not self._db or not self._client:
            return
        try:
            await self._run_scan()
        except Exception:
            logger.exception("pill_buff_scan: unexpected error")

    @pill_buff_scan.before_loop
    async def before_pill_buff_scan(self) -> None:
        await self._wait_for_services()

    async def _run_scan(self) -> None:
        import time as _time
        now = int(_time.time())

        subs_10 = await self._db.get_all_pill_subscribers()
        subs_30 = await self._db.get_all_pill_reminder_30_subscribers()

        # Build a deduplicated map: in_game_user_id → list of (table, subscriber_dict)
        ingame_map: dict[str, list[tuple[str, dict]]] = {}
        for s in subs_10:
            ingame_map.setdefault(s["in_game_user_id"], []).append(("10", s))
        for s in subs_30:
            ingame_map.setdefault(s["in_game_user_id"], []).append(("30", s))

        if not ingame_map:
            return

        unique_ids = list(ingame_map.keys())
        inputs = [{"userId": uid} for uid in unique_ids]

        try:
            results = await self._client.batch_get("user.getUserLite", inputs)
        except Exception as exc:
            logger.error("pill_buff_scan: batch API call failed: %s", exc)
            return

        for in_game_id, user_data in zip(unique_ids, results):
            if not isinstance(user_data, dict):
                continue

            buffs = user_data.get("buffs") or {}
            buff_codes: list = buffs.get("buffCodes") or []
            buff_end_at_raw: str | None = buffs.get("buffEndAt")
            new_expires_at = _parse_iso(buff_end_at_raw) if (_PILL_ITEM_CODE in buff_codes and buff_end_at_raw) else None

            for table, subscriber in ingame_map[in_game_id]:
                discord_user_id = subscriber["discord_user_id"]
                old_expires_at = subscriber.get("expires_at")
                is_new_buff = new_expires_at is not None and (old_expires_at is None or old_expires_at != new_expires_at)

                if table == "10":
                    if new_expires_at is not None:
                        await self._db.update_pill_expires_at(discord_user_id, new_expires_at, reset_reminded=is_new_buff)
                        if is_new_buff:
                            logger.info("pill_buff_scan: %s (10m) active pill, expires %s", discord_user_id, new_expires_at)
                    elif old_expires_at is not None and old_expires_at > now:
                        await self._db.update_pill_expires_at(discord_user_id, None)
                else:
                    if new_expires_at is not None:
                        await self._db.update_pill_reminder_30_expires_at(discord_user_id, new_expires_at, reset_reminded=is_new_buff)
                        if is_new_buff:
                            logger.info("pill_buff_scan: %s (30m) active pill, expires %s", discord_user_id, new_expires_at)
                    elif old_expires_at is not None and old_expires_at > now:
                        await self._db.update_pill_reminder_30_expires_at(discord_user_id, None)

    # ── 30-second DM check ────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def pill_reminder_check(self) -> None:
        if not self._db:
            return
        try:
            await self._run_check()
        except Exception:
            logger.exception("pill_reminder_check: unexpected error")

    @pill_reminder_check.before_loop
    async def before_pill_reminder_check(self) -> None:
        await self._wait_for_services()

    async def _run_check(self) -> None:
        import time as _time
        due_10 = await self._db.get_due_pill_reminders()
        due_30 = await self._db.get_due_pill_reminders_30()

        # (entry, table_label) pairs
        all_due = [(e, "10") for e in due_10] + [(e, "30") for e in due_30]

        for entry, table in all_due:
            discord_user_id: str = entry["discord_user_id"]
            expires_at: int = entry["expires_at"]

            minutes_left = max(0, (expires_at - int(_time.time())) // 60)
            expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
            expires_str = discord.utils.format_dt(expires_dt, style="R")

            try:
                user = await self.bot.fetch_user(int(discord_user_id))
            except (discord.NotFound, discord.HTTPException):
                logger.warning(
                    "pill_reminder_check: could not fetch user %s, removing subscription",
                    discord_user_id,
                )
                if table == "10":
                    await self._db.remove_pill_reminder(discord_user_id)
                else:
                    await self._db.remove_pill_reminder_30(discord_user_id)
                continue

            embed = discord.Embed(
                title="💊 Pill buff verloopt bijna!",
                description=(
                    f"Je **pill buff** verloopt **{expires_str}** "
                    f"(nog ~{minutes_left} minuten).\n\n"
                    "Zorg dat je klaar staat om op tijd te battlen!"
                ),
                colour=self._embed_colour("warning"),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text="WareraNL Bot — pill herinnering")

            try:
                await user.send(embed=embed)
                logger.info(
                    "pill_reminder_check: DM sent to %s (%s) [%sm], expires %s",
                    user, discord_user_id, table, expires_dt.isoformat(),
                )
            except discord.Forbidden:
                logger.warning(
                    "pill_reminder_check: cannot DM %s (%s) — DMs disabled",
                    user, discord_user_id,
                )
            except discord.HTTPException as exc:
                logger.error(
                    "pill_reminder_check: DM failed for %s: %s", discord_user_id, exc
                )
                continue

            if table == "10":
                await self._db.mark_pill_reminded(discord_user_id)
            else:
                await self._db.mark_pill_reminder_30_reminded(discord_user_id)


async def setup(bot) -> None:
    """Add the PillReminderTask cog to the bot."""
    await bot.add_cog(PillReminderTask(bot))

