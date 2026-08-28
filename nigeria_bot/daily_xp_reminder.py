"""Nigeria-bot feature: DM a reminder to anyone who hasn't claimed today's
daily mission reward yet.

Runs once a day at 22:00 Dutch time (``TARGET_HOUR_NL``). For every Discord
user linked via ``nigeria_bot/db.py``'s ``identity_links`` table — nigeria_bot's
own Discord↔WarEra mapping, kept separately from the main bot's
``services/db/identities.py`` table (they run as independent processes with
independent databases; see ``database/nigeria.db``) — this fetches
``user.getUserById`` and checks ``missions.claimedAt.daily`` against today's
UTC date. Accounts where ``isActive`` is ``False`` are skipped entirely —
they've left the game, not just skipped today's mission, so a reminder
would be noise rather than useful.

Confirmed live: ``claimedAt.daily`` is not a stale/yesterday timestamp when
unclaimed — the key is absent from the response entirely. So its presence
always means "claimed today," and its absence always means "not claimed
today," full stop — there's no way to tell from this endpoint whether
someone has ever claimed before, only whether they have today. The reminder
embed doesn't claim otherwise (see _build_embed).

Live: every linked, active WarEra citizen who hasn't claimed today's daily
mission gets a real DM at 22:00 NL time. A staff-only ``/dailyxp-test``
slash command runs the same check on demand (e.g. to verify it's working
without waiting for the schedule) but never sends real DMs — it's a dry
run that only reports counts, precisely so staff can trigger it freely
without risking an accidental mass-DM to the whole server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from nigeria_bot.cog import _staff_check
from nigeria_bot.db import get_all_links

logger = logging.getLogger("nigeria_bot.daily_xp_reminder")

WARERA_API_BASE = "https://api2.warera.io/trpc"
WARERA_API_KEY = os.environ.get("WARERA_API_KEY", "")

_TZ_NL = ZoneInfo("Europe/Amsterdam")
TARGET_HOUR_NL = 22

# Delay between consecutive API calls, same reasoning as nigeria_bot/cog.py's
# nick_sync: with an API key ~400 req/min, without ~85 req/min (anon limit 100/min).
_REQUEST_DELAY = 0.15 if WARERA_API_KEY else 0.7


def _unwrap(resp: object) -> dict | list | None:
    """Strip a single tRPC result/data envelope."""
    if isinstance(resp, dict):
        inner = resp.get("result", resp)
        if isinstance(inner, dict):
            return inner.get("data", inner)
        return inner
    return resp  # type: ignore[return-value]


async def _get(sess: aiohttp.ClientSession, procedure: str, payload: dict | None = None):
    """GET one tRPC procedure with an optional ``input`` payload."""
    url = f"{WARERA_API_BASE}/{procedure}"
    params = {"input": json.dumps(payload)} if payload is not None else None
    headers = {"x-api-key": WARERA_API_KEY} if WARERA_API_KEY else {}
    async with sess.get(
        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        return _unwrap(await resp.json())


class DailyXpReminderCog(commands.Cog, name="daily_xp_reminder"):
    """Daily 22:00-NL check for unclaimed daily missions."""

    def __init__(self, bot: commands.Bot, db: aiosqlite.Connection) -> None:
        self.bot = bot
        self._db = db
        # NL calendar date the loop last actually ran the check on — guards
        # the 1-minute-granularity loop from re-firing repeatedly inside the
        # target hour. In-memory only: a restart during the target hour can
        # cause one extra run that same day, an acceptable risk while this
        # is still test-mode-only.
        self._last_run_date: date | None = None
        self.daily_check.start()

    def cog_unload(self) -> None:
        self.daily_check.cancel()

    @tasks.loop(minutes=1)
    async def daily_check(self) -> None:
        nl_now = datetime.now(_TZ_NL)
        if nl_now.hour != TARGET_HOUR_NL:
            return
        today = nl_now.date()
        if self._last_run_date == today:
            return
        self._last_run_date = today
        try:
            await self._run_check()
        except Exception:
            logger.exception("daily_xp_reminder: check failed")

    @daily_check.before_loop
    async def _before_daily_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_check(self, *, dry_run: bool = False) -> dict[str, int]:
        """Check every linked citizen's daily-claim status.

        When dry_run is False (the real 22:00 schedule), every active
        citizen who hasn't claimed today's mission gets a real DM. When
        dry_run is True (the /dailyxp-test command), the exact same checks
        run but no DM is ever sent — dm_sent counts how many *would* have
        gone out.
        """
        links = await get_all_links(self._db)
        if not links:
            logger.info("daily_xp_reminder: no linked users")
            return {"linked": 0, "checked": 0, "inactive_skipped": 0, "not_claimed": 0, "dm_sent": 0, "dm_failed": 0}

        today_utc = datetime.now(timezone.utc).date()
        checked = 0
        inactive_skipped = 0
        not_claimed = 0
        dm_sent = 0
        dm_failed = 0

        async with aiohttp.ClientSession() as sess:
            for i, (discord_id, warera_id) in enumerate(links):
                if i > 0:
                    await asyncio.sleep(_REQUEST_DELAY)
                try:
                    user = await _get(sess, "user.getUserById", {"userId": warera_id})
                except Exception as exc:
                    logger.warning(
                        "daily_xp_reminder: fetch failed for %s: %s", warera_id, exc
                    )
                    continue
                if not isinstance(user, dict):
                    continue
                checked += 1

                # Inactive WarEra accounts never get the reminder — they've
                # left the game, not just skipped today's mission.
                if user.get("isActive") is False:
                    inactive_skipped += 1
                    continue

                claimed_at = (
                    (user.get("missions") or {}).get("claimedAt") or {}
                ).get("daily")
                claimed_dt: datetime | None = None
                if claimed_at:
                    try:
                        claimed_dt = datetime.fromisoformat(
                            str(claimed_at).replace("Z", "+00:00")
                        )
                    except ValueError:
                        claimed_dt = None

                already_claimed_today = (
                    claimed_dt is not None
                    and claimed_dt.astimezone(timezone.utc).date() == today_utc
                )
                if already_claimed_today:
                    continue
                not_claimed += 1

                if dry_run:
                    dm_sent += 1
                    continue

                try:
                    target_user = await self.bot.fetch_user(int(discord_id))
                    await target_user.send(embed=self._build_embed())
                    dm_sent += 1
                    logger.info(
                        "daily_xp_reminder: DM sent to %s (warera_id=%s)",
                        discord_id, warera_id,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "daily_xp_reminder: cannot DM %s — DMs disabled", discord_id
                    )
                    dm_failed += 1
                except discord.HTTPException as exc:
                    logger.error(
                        "daily_xp_reminder: DM failed for %s: %s", discord_id, exc
                    )
                    dm_failed += 1

        logger.info(
            "daily_xp_reminder: checked=%d inactive_skipped=%d not_claimed=%d "
            "dm_sent=%d dm_failed=%d (dry_run=%s)",
            checked, inactive_skipped, not_claimed, dm_sent, dm_failed, dry_run,
        )
        return {
            "linked": len(links),
            "checked": checked,
            "inactive_skipped": inactive_skipped,
            "not_claimed": not_claimed,
            "dm_sent": dm_sent,
            "dm_failed": dm_failed,
        }

    # ── manual test trigger ──────────────────────────────────────────────

    @app_commands.command(
        name="dailyxp-test",
        description="[Staff] Dry-run the unclaimed-daily-XP check — reports counts, sends no DMs.",
    )
    @_staff_check()
    async def dailyxp_test(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            stats = await self._run_check(dry_run=True)
        except Exception as exc:
            logger.exception("daily_xp_reminder: manual test run failed")
            await interaction.followup.send(f"❌ Check failed: {exc}")
            return
        await interaction.followup.send(
            "✅ Dry run done — no DMs were sent:\n"
            "Linked users: **{linked}** · Checked: **{checked}** · "
            "Inactive skipped: **{inactive_skipped}** · Not claimed: **{not_claimed}** · "
            "Would DM: **{dm_sent}**".format(**stats)
        )

    def _build_embed(self) -> discord.Embed:
        # No "last claimed" field: the API drops claimedAt.daily entirely
        # once it's unclaimed, regardless of claim history, so there's no
        # honest timestamp to show here — see the module docstring.
        return discord.Embed(
            title="🎁 Dagelijkse missie nog niet geclaimd!",
            description=(
                "Je hebt de dagelijkse missiebeloning vandaag nog niet opgehaald.\n\n"
                "Log in en claim 'm voor je de XP misloopt!"
            ),
            colour=discord.Colour.orange(),
        )


async def setup(bot: commands.Bot, db: aiosqlite.Connection) -> DailyXpReminderCog:
    cog = DailyXpReminderCog(bot, db)
    await bot.add_cog(cog)
    return cog
