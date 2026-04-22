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

_PILL_ITEM_CODE = "cocain"
_WARN_SECONDS   = 600   # 10 minutes


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
        subscribers = await self._db.get_all_pill_subscribers()
        if not subscribers:
            return

        inputs = [{"userId": s["in_game_user_id"]} for s in subscribers]

        try:
            results = await self._client.batch_get("user.getUserLite", inputs)
        except Exception as exc:
            logger.error("pill_buff_scan: batch API call failed: %s", exc)
            return

        import time as _time
        now = int(_time.time())

        for subscriber, user_data in zip(subscribers, results):
            discord_user_id = subscriber["discord_user_id"]

            if not isinstance(user_data, dict):
                continue

            buffs = user_data.get("buffs") or {}
            buff_codes: list = buffs.get("buffCodes") or []
            buff_end_at_raw: str | None = buffs.get("buffEndAt")

            if _PILL_ITEM_CODE in buff_codes and buff_end_at_raw:
                new_expires_at = _parse_iso(buff_end_at_raw)
                if new_expires_at is None:
                    continue

                old_expires_at = subscriber.get("expires_at")
                # Reset reminded flag if this is a new/different pill buff
                is_new_buff = (old_expires_at is None or old_expires_at != new_expires_at)

                await self._db.update_pill_expires_at(
                    discord_user_id,
                    new_expires_at,
                    reset_reminded=is_new_buff,
                )
                if is_new_buff:
                    expires_dt = datetime.fromtimestamp(new_expires_at, tz=timezone.utc)
                    logger.info(
                        "pill_buff_scan: %s has active pill, expires %s",
                        discord_user_id,
                        expires_dt.isoformat(),
                    )
            else:
                # No active pill buff — clear expires_at if it was set
                if subscriber.get("expires_at") is not None and subscriber.get("expires_at", 0) > now:
                    await self._db.update_pill_expires_at(discord_user_id, None)

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
        due = await self._db.get_due_pill_reminders()
        for entry in due:
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
                await self._db.remove_pill_reminder(discord_user_id)
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
                    "pill_reminder_check: DM sent to %s (%s), expires %s",
                    user,
                    discord_user_id,
                    expires_dt.isoformat(),
                )
            except discord.Forbidden:
                logger.warning(
                    "pill_reminder_check: cannot DM %s (%s) — DMs disabled",
                    user,
                    discord_user_id,
                )
            except discord.HTTPException as exc:
                logger.error(
                    "pill_reminder_check: DM failed for %s: %s", discord_user_id, exc
                )
                continue  # Don't mark as reminded if send failed

            await self._db.mark_pill_reminded(discord_user_id)


async def setup(bot) -> None:
    """Add the PillReminderTask cog to the bot."""
    await bot.add_cog(PillReminderTask(bot))

