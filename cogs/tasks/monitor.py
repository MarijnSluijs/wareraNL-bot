"""Background task: periodic war-readiness monitoring per country (every 15 minutes).

Compares the current skill-mode-by-level-bucket snapshot against the previous
one and posts a change report to the configured Discord channel when anything
in the 21+ level range differs.

Subscriptions are persisted in the ``poll_state`` table under the key
``monitor_subscriptions`` (JSON dict: country_id → {country_name, channel_id,
guild_id}).  Per-country snapshots live under ``monitor_snapshot_<country_id>``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Only report changes for citizens at or above this level bucket.
_MIN_LEVEL = 21


def _normalize(raw: dict) -> dict[int, dict[str, int]]:
    """Convert JSON-loaded bucket dict (str keys) back to int-keyed form."""
    return {int(k): v for k, v in raw.items()}


class MonitorTask(TaskCogBase, name="monitor_tasks"):
    """Periodically checks war-readiness and reports changes."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.monitor_loop.start()

    def cog_unload(self) -> None:
        self.monitor_loop.cancel()

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=15)
    async def monitor_loop(self) -> None:
        if not self._db:
            return

        try:
            subs_json = await self._db.get_poll_state("monitor_subscriptions")
        except Exception:
            logger.exception("monitor_loop: failed to read subscriptions")
            return

        if not subs_json:
            return

        try:
            subscriptions: dict[str, dict] = json.loads(subs_json)
        except Exception:
            logger.warning("monitor_loop: invalid subscriptions JSON, skipping")
            return

        for country_id, sub in list(subscriptions.items()):
            try:
                await self._check_country(country_id, sub)
            except Exception:
                logger.exception(
                    "monitor_loop: unhandled error for country %s", country_id
                )

    @monitor_loop.before_loop
    async def before_monitor_loop(self) -> None:
        await self._wait_for_services()

    # ------------------------------------------------------------------ #
    # Per-country check                                                    #
    # ------------------------------------------------------------------ #

    async def _check_country(self, country_id: str, sub: dict) -> None:
        channel_id: int = int(sub["channel_id"])
        country_name: str = sub.get("country_name", country_id)

        # ── Fetch current buckets from DB ──────────────────────────────
        try:
            raw_buckets, _ = await self._db.get_skill_mode_by_level_buckets(country_id)
        except Exception:
            logger.error(
                "monitor: failed to fetch skill buckets for %s", country_id
            )
            return

        # Keep only level 21+ buckets for monitoring.
        current: dict[int, dict[str, int]] = {
            b: v for b, v in raw_buckets.items() if b >= _MIN_LEVEL
        }

        snap_key = f"monitor_snapshot_{country_id}"

        # ── Load previous snapshot ─────────────────────────────────────
        try:
            snap_json = await self._db.get_poll_state(snap_key)
        except Exception:
            logger.error(
                "monitor: failed to read snapshot for %s", country_id
            )
            snap_json = None

        # First run or reset: save baseline and exit without reporting.
        if not snap_json:
            await self._db.set_poll_state(snap_key, json.dumps(current))
            logger.info(
                "monitor: baseline snapshot saved for %s (%d buckets)",
                country_name,
                len(current),
            )
            return

        try:
            previous = _normalize(json.loads(snap_json))
        except Exception:
            logger.warning(
                "monitor: corrupt snapshot for %s, resetting baseline", country_name
            )
            await self._db.set_poll_state(snap_key, json.dumps(current))
            return

        # ── Compute diff over level 21+ buckets ───────────────────────
        all_buckets = sorted(set(current) | set(previous))
        change_lines: list[str] = []

        for b in all_buckets:
            prev_b = previous.get(b, {"eco": 0, "war": 0, "unknown": 0})
            curr_b = current.get(b, {"eco": 0, "war": 0, "unknown": 0})

            war_diff = curr_b["war"] - prev_b["war"]
            eco_diff = curr_b["eco"] - prev_b["eco"]
            total_prev = prev_b["war"] + prev_b["eco"] + prev_b["unknown"]
            total_curr = curr_b["war"] + curr_b["eco"] + curr_b["unknown"]
            total_diff = total_curr - total_prev

            # Skip buckets with no change.
            if war_diff == 0 and eco_diff == 0 and total_diff == 0:
                continue

            b_end = b + 4
            parts: list[str] = [f"**Lvl {b}–{b_end}**:"]

            if war_diff != 0:
                sign = "+" if war_diff > 0 else ""
                parts.append(
                    f"⚔️ {prev_b['war']}→{curr_b['war']} ({sign}{war_diff})"
                )
            if eco_diff != 0:
                sign = "+" if eco_diff > 0 else ""
                parts.append(
                    f"🌾 {prev_b['eco']}→{curr_b['eco']} ({sign}{eco_diff})"
                )
            # Only mention total delta if war/eco alone don't explain it
            # (e.g. unknown-mode citizens appeared or disappeared).
            if total_diff != 0 and war_diff == 0 and eco_diff == 0:
                sign = "+" if total_diff > 0 else ""
                parts.append(f"totaal {sign}{total_diff}")

            change_lines.append("  ".join(parts))

        # ── Persist updated snapshot ───────────────────────────────────
        await self._db.set_poll_state(snap_key, json.dumps(current))

        if not change_lines:
            return  # Nothing to report.

        # ── Overall paraatheid percentage (level 21+) ──────────────────
        def _totals(buckets: dict[int, dict[str, int]]) -> tuple[int, int]:
            """Return (war_count, total_known) for level 21+ buckets."""
            war = total = 0
            for v in buckets.values():
                war += v["war"]
                total += v["war"] + v["eco"]
            return war, total

        prev_war, prev_total = _totals(previous)
        curr_war, curr_total = _totals(current)
        prev_pct = prev_war / prev_total * 100 if prev_total else 0.0
        curr_pct = curr_war / curr_total * 100 if curr_total else 0.0
        pct_diff = curr_pct - prev_pct
        pct_sign = "+" if pct_diff >= 0 else ""
        overall_str = (
            f"{prev_pct:.1f}% → **{curr_pct:.1f}%** ({pct_sign}{pct_diff:.1f}%)"
            f"   ({prev_war}→{curr_war} / {curr_total} spelers)"
        )

        # ── Post report to the subscribed channel ──────────────────────
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning(
                "monitor: channel %d not found (for %s)", channel_id, country_name
            )
            return

        now_str = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
        embed = discord.Embed(
            title=f"📊 Paraatheidsupdate — {country_name}",
            description="\n".join(change_lines),
            colour=discord.Colour.gold(),
        )
        embed.add_field(name="⚔️ Totaal paraat (lvl 21+)", value=overall_str, inline=False)
        embed.set_footer(text=f"Alleen wijzigingen op niveau 21+  •  {now_str}")

        try:
            await channel.send(embed=embed)
        except Exception:
            logger.exception(
                "monitor: failed to send update to channel %d", channel_id
            )


async def setup(bot) -> None:
    """Add the MonitorTask cog to the bot."""
    await bot.add_cog(MonitorTask(bot))
