"""
/speler_inactiviteit — List all inactive Dutch citizens (no login in the last N hours).

Similar to /mu_inactiviteit but covers every NL citizen, not just MU members.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

_DEFAULT_HOURS = 72
_MAX_DISPLAY = 50  # maximum rows shown in the embed table


def _fmt_duration(hours: float) -> str:
    d = int(hours // 24)
    h = int(hours % 24)
    if d:
        return f"{d}d {h}u"
    return f"{h}u"


def _last_connection(obj: object) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    dates = obj.get("dates")
    if isinstance(dates, dict):
        return dates.get("lastConnectionAt")
    return obj.get("lastConnectionAt") or obj.get("lastLoginAt")


def _username(obj: object) -> str:
    if not isinstance(obj, dict):
        return "?"
    return obj.get("username") or obj.get("name") or "?"


class SpelerInactiviteitCog(CommandCogBase, name="speler_inactiviteit"):
    """Per-citizen inactivity check for all Dutch citizens."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="speler_inactiviteit",
        description="Laat inactieve Nederlandse burgers zien (geen login in de afgelopen uren).",
    )
    @app_commands.describe(
        minimum_uren=f"Minimum uren inactief om te worden weergegeven (standaard: {_DEFAULT_HOURS})",
    )
    async def speler_inactiviteit(
        self,
        interaction: discord.Interaction,
        minimum_uren: int = _DEFAULT_HOURS,
    ) -> None:
        """Show all NL citizens who haven't logged in within *minimum_uren* hours."""
        await interaction.response.defer(thinking=True)

        db = self._db
        if not db:
            await interaction.followup.send(
                "❌ Database niet beschikbaar.", ephemeral=True
            )
            return

        client = self._client
        if not client or client.is_available is False:
            await self._send_api_offline(interaction)
            return

        nl_country_id: str = self.config.get("nl_country_id", "")
        if not nl_country_id:
            await interaction.followup.send(
                "❌ NL country ID niet geconfigureerd.", ephemeral=True
            )
            return

        # Load all NL citizens from the DB cache
        citizens: list[
            tuple[str, Optional[str]]
        ] = await db.get_citizens_for_luck_refresh(nl_country_id)
        if not citizens:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen Nederlandse burgers gevonden in de database.",
                    color=discord.Color.orange(),
                )
            )
            return

        total_citizens = len(citizens)
        all_user_ids = [uid for uid, _ in citizens]
        name_map: dict[str, str] = {uid: (name or uid[:8]) for uid, name in citizens}

        # Batch-fetch last login time for all citizens
        inputs = [{"userId": uid} for uid in all_user_ids]
        results = await client.batch_get(
            "/user.getUserLite",
            inputs,
            batch_size=30,
            chunk_sleep=0.1,
        )

        now = datetime.now(timezone.utc)
        inactive: list[tuple[float, str, str]] = []  # (hours_ago, uid, display_name)

        for uid, obj in zip(all_user_ids, results):
            display_name = name_map.get(uid, uid[:8])
            if isinstance(obj, dict):
                # Use API username if available (more up-to-date than DB cache)
                api_name = _username(obj)
                if api_name != "?":
                    display_name = api_name

            last_conn = _last_connection(obj)
            if last_conn is None:
                inactive.append((float("inf"), uid, display_name))
                continue
            try:
                ts = datetime.fromisoformat(last_conn.replace("Z", "+00:00"))
                hours_ago = (now - ts).total_seconds() / 3600
            except (ValueError, TypeError):
                inactive.append((float("inf"), uid, display_name))
                continue

            if hours_ago >= minimum_uren:
                inactive.append((hours_ago, uid, display_name))

        color = int((self.config.get("colors") or {}).get("primary", "0x154273"), 16)

        if not inactive:
            embed = discord.Embed(
                title="✅ Geen inactieve burgers",
                description=(
                    f"Alle {total_citizens:,} Nederlandse burgers zijn ingelogd "
                    f"in de afgelopen **{minimum_uren}** uur."
                ),
                color=discord.Color.green(),
                timestamp=now,
            )
            await interaction.followup.send(embed=embed)
            return

        # Sort: longest inactive first (unknown → end)
        inactive.sort(
            key=lambda x: (x[0] != float("inf"), -x[0] if x[0] != float("inf") else 0)
        )

        total_inactive = len(inactive)
        display_rows = inactive[:_MAX_DISPLAY]

        # Build monospace table
        col_name = max((len(r[2]) for r in display_rows), default=6)
        col_name = max(col_name, len("Speler"))
        header = f"{'Speler':<{col_name}}  Inactief"
        separator = "─" * (col_name + 12)
        lines = [header, separator]
        for hours, _uid, name in display_rows:
            dur = "onbekend" if hours == float("inf") else _fmt_duration(hours)
            lines.append(f"{name:<{col_name}}  {dur}")
        table = "\n".join(lines)

        truncation_note = ""
        if total_inactive > _MAX_DISPLAY:
            truncation_note = f"\n_… en {total_inactive - _MAX_DISPLAY} meer (zet minimum_uren hoger om te filteren)_"

        embed = discord.Embed(
            title="💤 Inactieve Nederlandse burgers",
            description=(
                f"**{total_inactive:,} van {total_citizens:,} burgers** hebben meer dan "
                f"**{minimum_uren}** uur niet ingelogd.\n\n"
                f"```\n{table}\n```"
                f"{truncation_note}"
            ),
            color=color,
            timestamp=now,
        )
        embed.set_footer(
            text=f"{total_citizens:,} burgers gecontroleerd • top {min(total_inactive, _MAX_DISPLAY)} getoond"
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: DiscordBot) -> None:
    """Register SpelerInactiviteitCog with the bot."""
    await bot.add_cog(SpelerInactiviteitCog(bot))
