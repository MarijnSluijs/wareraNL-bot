"""Background task: daily resistance overview for NL-occupied foreign regions."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")


def _seconds_until_hour(target_hour: int) -> float:
    """Seconds to sleep until the next target_hour:00:00 UTC."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


class ResistanceTasks(TaskCogBase, name="resistance_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.resistance_poll.start()

    def cog_unload(self) -> None:
        self.resistance_poll.cancel()

    # ------------------------------------------------------------------ #
    # Daily resistance overview (09:00 UTC)                               #
    # ------------------------------------------------------------------ #

    @tasks.loop(hours=24)
    async def resistance_poll(self) -> None:
        """Daily overview: resistance in NL-controlled foreign regions."""
        if not self._client or not self._db:
            return
        try:
            await self._run_resistance_poll()
        except Exception:
            logger.exception("resistance_poll: unexpected error")

    @resistance_poll.before_loop
    async def before_resistance_poll(self) -> None:
        await self._wait_for_services()
        await asyncio.sleep(_seconds_until_hour(9))

    async def run_resistance_poll(self) -> None:
        """Public wrapper so /peil and debug commands can trigger the poll."""
        await self._run_resistance_poll(silent=True)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _run_resistance_poll(self, *, silent: bool = False) -> None:
        """Fetch all regions, find NL originals occupied by others, report resistance."""
        nl_country_id = self.config.get("nl_country_id")
        channels = self.config.get("channels", {})
        channel_id = channels.get("bot_mededelingen") or channels.get("testing-area")
        if not channel_id or not nl_country_id:
            return

        try:
            resp = await self._client.get(
                "/region.getRegionsObject",
                params={"input": "{}"},
            )
        except Exception as exc:
            logger.warning("resistance_poll: failed to fetch regions: %s", exc)
            return

        data: dict | list = {}
        if isinstance(resp, dict):
            inner = resp.get("result", {})
            data = inner.get("data", inner) if isinstance(inner, dict) else resp
        regions: list[dict] = []
        if isinstance(data, dict):
            regions = [v for v in data.values() if isinstance(v, dict)]
        elif isinstance(data, list):
            regions = [r for r in data if isinstance(r, dict)]

        country_names: dict[str, str] = {}
        try:
            c_resp = await self._client.get("/country.getAllCountries")
            c_inner = c_resp.get("result", c_resp) if isinstance(c_resp, dict) else {}
            c_data = (
                c_inner.get("data", c_inner) if isinstance(c_inner, dict) else c_resp
            )
            if isinstance(c_data, list):
                for c in c_data:
                    if isinstance(c, dict):
                        cid = c.get("_id") or c.get("id")
                        cname = c.get("name") or c.get("shortName")
                        if cid and cname:
                            country_names[str(cid)] = str(cname)
        except Exception:
            logger.debug("resistance_poll: could not build country name cache")

        occupied: list[dict] = []
        for r in regions:
            orig_id = r.get("initialCountry")
            curr_id = r.get("country")
            if curr_id == nl_country_id and orig_id and orig_id != nl_country_id:
                occupied.append(r)

        if not occupied:
            logger.info(
                "resistance_poll: NL controls no foreign regions (no resistance active)"
            )
            return

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        current: list[tuple[str, str, str, float, float]] = []
        for r in occupied:
            rid = r.get("_id") or r.get("id") or ""
            rname = r.get("name") or r.get("regionName") or rid
            orig_id = r.get("initialCountry") or ""
            orig_name = country_names.get(orig_id) or orig_id or "?"
            res = float(r.get("resistance") or 0)
            maxr = float(r.get("resistanceMax") or 100.0)
            current.append((rid, rname, orig_name, res, maxr))

        def _region_field(res: float, maxr: float, delta: float | None = None) -> str:
            pct = (res / maxr * 100) if maxr else 0
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            delta_str = ""
            if delta is not None and abs(delta) > 0.01:
                arrow = "📈" if delta > 0 else "📉"
                delta_str = f" ({arrow} {delta:+.0f})"
            return f"Verzet: `{bar}` {res:.0f}/{maxr:.0f} ({pct:.0f}%){delta_str}"

        fields: list[tuple[str, str]] = []
        for rid, rname, orig, res, maxr in current:
            stored = await self._db.get_resistance_state(rid)
            old_val: float | None = stored["resistance_value"] if stored else None
            delta = (res - old_val) if old_val is not None else None
            fields.append((f"{rname} ({orig})", _region_field(res, maxr, delta)))
            await self._db.upsert_resistance_state(rid, rname, orig, res, maxr, now_str)

        embed = discord.Embed(
            title="⚔️ Door NL bezette regio's — dagelijks verzetsoverzicht",
            description="Regio's die NL beheert maar oorspronkelijk aan een ander land toebehoren.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        for rname, field_val in fields:
            embed.add_field(name=rname, value=field_val, inline=False)
        embed.set_footer(text="WarEra — verzetspeiling")

        if silent:
            logger.info("resistance_poll: DB updated silently (%d regions), skipping post", len(fields))
            return

        for guild in self.bot.guilds:
            ch = guild.get_channel(channel_id)
            if ch:
                try:
                    await ch.send(embed=embed)
                except Exception:
                    logger.exception("resistance_poll: failed to post daily overview")


async def setup(bot) -> None:
    """Add the ResistanceTasks cog to the bot."""
    await bot.add_cog(ResistanceTasks(bot))
