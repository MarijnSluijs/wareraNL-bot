"""Slash command /weeklydmg — top weekly battle damage for NL citizens.

Usage
-----
/weeklydmg                    — show top 10 NL players this week
/weeklydmg speler:PlayerName  — show a specific player's weekly damage
/weeklydmg top_n:25           — show top 25 players
/weeklydmg top_n:25 speler:X  — ignored if speler is given
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase
from services.damage_calc import fmt_damage

logger = logging.getLogger("discord_bot")

_DEFAULT_TOP = 10
_MAX_TOP = 50


class WeeklydmgCog(CommandCogBase, name="weeklydmg"):
    """Cog for the /weeklydmg command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="weeklydmg",
        description="Toon de top wekelijkse gevechtsschade van Nederlandse burgers.",
    )
    @app_commands.describe(
        speler="Optioneel: zoek een specifieke speler op naam of ID.",
        top_n="Optioneel: toon de top N spelers (standaard 10, max 50).",
    )
    async def weeklydmg(
        self,
        ctx: Context,
        speler: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> None:
        """Show weekly battle damage leaderboard for NL citizens."""
        if not self._db:
            await ctx.send("Diensten niet geïnitialiseerd.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            await ctx.send("nl_country_id is niet geconfigureerd.")
            return

        # ── Single player lookup ──────────────────────────────────────
        if speler:
            row = await self._db.get_weekly_damage_for_player(nl_country_id, speler)
            if row is None:
                await ctx.send(
                    f"Geen wekelijkse schadedata gevonden voor **{speler}**.\n"
                    "Controleer de naam of wacht tot de cache is bijgewerkt."
                )
                return
            uid, name, dmg, updated_at = row

            # Top 5 NL
            top_rows, last_updated = await self._db.get_top_weekly_damages(nl_country_id, 5)
            nl_rank = await self._db.get_player_nl_rank(nl_country_id, uid)

            medal = {1: "🥇", 2: "🥈", 3: "🥉"}
            top_lines: list[str] = []
            player_in_top5 = False
            for rank, (r_uid, r_name, r_dmg) in enumerate(top_rows, 1):
                prefix = medal.get(rank, f"`{rank}.`")
                if r_uid == uid:
                    top_lines.append(f"{prefix} **__{r_name}__** — {fmt_damage(r_dmg)}")
                    player_in_top5 = True
                else:
                    top_lines.append(f"{prefix} **{r_name}** — {fmt_damage(r_dmg)}")

            embed = discord.Embed(
                title="⚔️ Wekelijkse schade Nederland — Top 5",
                description="\n".join(top_lines) if top_lines else "*Nog geen data*",
                colour=self._embed_colour(),
            )

            if not player_in_top5:
                rank_str = f"#{nl_rank}" if nl_rank else "?"
                embed.add_field(
                    name=f"📍 {name}",
                    value=f"Rang **{rank_str}** — {fmt_damage(dmg)}",
                    inline=False,
                )

            ts = (updated_at or last_updated or "")[:19].replace("T", " ")
            embed.set_footer(text=f"Bijgewerkt: {ts} UTC")
            await ctx.send(embed=embed)
            return

        # ── Leaderboard ───────────────────────────────────────────────
        limit = max(1, min(top_n or _DEFAULT_TOP, _MAX_TOP))
        rows, last_updated = await self._db.get_top_weekly_damages(nl_country_id, limit)

        if not rows:
            await ctx.send(
                "Nog geen wekelijkse schadedata voor Nederland.\n"
                "De cache wordt elk uur bijgewerkt (eerste run bij opstarten)."
            )
            return

        # Build leaderboard text
        lines: list[str] = []
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}
        for rank, (uid, name, dmg) in enumerate(rows, 1):
            prefix = medal.get(rank, f"`{rank:>2}.`")
            lines.append(f"{prefix} **{name}** — {fmt_damage(dmg)}")

        embed = discord.Embed(
            title=f"⚔️ Wekelijkse schade — Top {len(rows)} Nederland",
            description="\n".join(lines),
            colour=self._embed_colour(),
        )
        if last_updated:
            embed.set_footer(
                text=f"Bijgewerkt: {last_updated[:19].replace('T', ' ')} UTC · elk uur ververst"
            )
        await ctx.send(embed=embed)


async def setup(bot) -> None:
    """Add the WeeklydmgCog to the bot."""
    await bot.add_cog(WeeklydmgCog(bot))
