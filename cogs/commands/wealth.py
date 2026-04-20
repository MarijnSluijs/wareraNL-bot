"""Wealth command cog — /wealth."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

_GOLD_EMOJI = "💰"
_MEDAL_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _fmt_gold(amount: float) -> str:
    """Format a currency amount in game style: e.g. 11.32k C."""
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M C"
    if amount >= 1_000:
        return f"{amount / 1_000:.2f}k C"
    return f"{amount:.2f} C"


class WealthCog(CommandCogBase, name="wealth"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="wealth",
        description="Show the wealth ranking of Dutch players.",
    )
    @app_commands.describe(
        top="Number of players to show (default 10, max 50).",
        speler="Search for a specific player by name.",
    )
    async def wealth(
        self,
        interaction: discord.Interaction,
        top: int = 10,
        speler: Optional[str] = None,
    ) -> None:
        if not self._db:
            await interaction.response.send_message(
                "Database niet beschikbaar.", ephemeral=True
            )
            return

        await interaction.response.defer()

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            await interaction.followup.send(
                "NL country ID niet geconfigureerd.", ephemeral=True
            )
            return

        updated_at_str = await self._db.get_poll_state("wealth_ranking_last_run")
        top = max(1, min(top, 50))
        ranking_rows = await self._db.get_wealth_ranking(nl_country_id, top)
        if not ranking_rows:
            await interaction.followup.send(
                "No wealth data available yet. "
                "Use `/peil wealth` to fetch the data.",
                ephemeral=True,
            )
            return

        if speler:
            search_rows = await self._db.search_citizen_wealth(speler, nl_country_id, limit=5)
            if not search_rows:
                await interaction.followup.send(
                    f"Geen spelers gevonden voor **{speler}**.", ephemeral=True
                )
                return
            # Fetch rank for each matched player
            ranked_results: list[tuple[dict, Optional[int]]] = []
            for row in search_rows:
                rank = await self._db.get_citizen_wealth_rank(row["user_id"], nl_country_id)
                ranked_results.append((row, rank))
            embed = self._build_ranking_embed(ranking_rows, updated_at_str, search_results=ranked_results)
        else:
            embed = self._build_ranking_embed(ranking_rows, updated_at_str)

        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #

    def _build_ranking_embed(
        self,
        rows: list[dict],
        updated_at_str: Optional[str],
        search_results: Optional[list[tuple[dict, Optional[int]]]] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{_GOLD_EMOJI} Wealth Ranking Netherlands",
            color=discord.Color.gold(),
        )
        lines: list[str] = []
        for rank, row in enumerate(rows, 1):
            medal = _MEDAL_EMOJIS.get(rank, f"**{rank}.**")
            name = row["citizen_name"] or row["user_id"]
            total = row["wealth_total"]
            inactive = row["wealth_inactive_companies"]
            inactive_str = (
                f" *({_fmt_gold(inactive)} inactive)*" if inactive > 0 else ""
            )
            lines.append(f"{medal} **{name}** — {_fmt_gold(total)}{inactive_str}")
        embed.description = "\n".join(lines)

        if search_results:
            player_lines: list[str] = []
            for row, rank in search_results:
                name = row["citizen_name"] or row["user_id"]
                total = row["wealth_total"]
                inactive = row["wealth_inactive_companies"]
                inactive_str = f" *({_fmt_gold(inactive)} inactive)*" if inactive > 0 else ""
                rank_label = _MEDAL_EMOJIS.get(rank, f"**{rank}.**") if rank else "**?.**"
                player_lines.append(f"{rank_label} **{name}** — {_fmt_gold(total)}{inactive_str}")
            embed.description += "\n\n" + "\n".join(player_lines)

        self._set_footer(embed, updated_at_str)
        return embed

    @staticmethod
    def _set_footer(embed: discord.Embed, updated_at_str: Optional[str]) -> None:
        if not updated_at_str:
            return
        try:
            dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
            embed.set_footer(text=f"Bijgewerkt op {dt.strftime('%d-%m-%Y %H:%M')} UTC · Inclusief inactieve bedrijven")
        except Exception:
            embed.set_footer(text=f"Bijgewerkt: {updated_at_str} · Inclusief inactieve bedrijven")


async def setup(bot) -> None:
    await bot.add_cog(WealthCog(bot))
