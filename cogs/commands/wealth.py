"""Wealth command cog — /wealth."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, _TZ_NL

logger = logging.getLogger("discord_bot")

_GOLD_EMOJI = "💰"
_TREND_EMOJI = "📈"
_MEDAL_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _fmt_gold(amount: float) -> str:
    """Format a currency amount in game style: e.g. 11.32k C."""
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M C"
    if amount >= 1_000:
        return f"{amount / 1_000:.2f}k C"
    return f"{amount:.2f} C"


def _fmt_increase(amount: float) -> str:
    """Format a wealth increase, prefixed with + or -."""
    sign = "+" if amount >= 0 else ""
    return f"{sign}{_fmt_gold(amount)}"


def _days_since(date_str: str) -> int:
    """Return number of days between ``date_str`` (YYYY-MM-DD) and today (UTC)."""
    try:
        then = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).days
    except (ValueError, TypeError):
        return 0


class WealthCog(CommandCogBase, name="wealth"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="wealth",
        description="Toon de vermogens-ranking van Nederlandse spelers.",
    )
    @app_commands.describe(
        top="Aantal spelers om te tonen (standaard 10, max 50).",
        speler="Zoek een specifieke speler op naam.",
        dagen="Toon ook rijkste stijging over de laatste X dagen (max = beschikbare historie).",
    )
    async def wealth(
        self,
        interaction: discord.Interaction,
        top: int = 10,
        speler: Optional[str] = None,
        dagen: Optional[int] = None,
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

        top = max(1, min(top, 50))

        # Determine max available days from history
        oldest_date = await self._db.get_wealth_history_oldest_date(nl_country_id)
        max_days = _days_since(oldest_date) if oldest_date else 0

        # Clamp requested days to what's available
        if dagen is not None:
            dagen = max(1, min(dagen, max_days)) if max_days > 0 else None

        updated_at_str = await self._db.get_poll_state("wealth_ranking_last_run")
        ranking_rows = await self._db.get_wealth_ranking(nl_country_id, top)
        if not ranking_rows:
            await interaction.followup.send(
                "Nog geen wealth-data beschikbaar. Gebruik `/peil wealth` om de data op te halen.",
                ephemeral=True,
            )
            return

        increase_rows: list[dict] = []
        if dagen and dagen > 0:
            increase_rows = await self._db.get_wealth_increase_ranking(nl_country_id, dagen, top)

        if speler:
            search_rows = await self._db.search_citizen_wealth(speler, nl_country_id, limit=5)
            if not search_rows:
                await interaction.followup.send(
                    f"Geen spelers gevonden voor **{speler}**.", ephemeral=True
                )
                return
            ranked_results: list[tuple[dict, Optional[int]]] = []
            for row in search_rows:
                rank = await self._db.get_citizen_wealth_rank(row["user_id"], nl_country_id)
                ranked_results.append((row, rank))
            embed = self._build_embed(
                ranking_rows, increase_rows, updated_at_str,
                dagen=dagen, max_days=max_days, search_results=ranked_results,
            )
        else:
            embed = self._build_embed(
                ranking_rows, increase_rows, updated_at_str,
                dagen=dagen, max_days=max_days,
            )

        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #

    def _build_embed(
        self,
        rows: list[dict],
        increase_rows: list[dict],
        updated_at_str: Optional[str],
        *,
        dagen: Optional[int],
        max_days: int,
        search_results: Optional[list[tuple[dict, Optional[int]]]] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{_GOLD_EMOJI} Wealth Ranking Nederland",
            color=discord.Color.gold(),
        )

        # ── Total wealth ranking ─────────────────────────────────────────
        lines: list[str] = []
        for rank, row in enumerate(rows, 1):
            medal = _MEDAL_EMOJIS.get(rank, f"**{rank}.**")
            name = row["citizen_name"] or row["user_id"]
            lines.append(f"{medal} **{name}** — {_fmt_gold(row['wealth_total'])}")

        if search_results:
            lines.append("")
            for row, rank in search_results:
                name = row["citizen_name"] or row["user_id"]
                rank_label = _MEDAL_EMOJIS.get(rank, f"**{rank}.**") if rank else "**?.**"
                lines.append(f"{rank_label} **{name}** — {_fmt_gold(row['wealth_total'])}")

        embed.add_field(
            name=f"{_GOLD_EMOJI} Totaal vermogen (top {len(rows)})",
            value="\n".join(lines) or "*Geen data.*",
            inline=False,
        )

        # ── Wealth increase ranking ──────────────────────────────────────
        if dagen and dagen > 0:
            if increase_rows:
                inc_lines: list[str] = []
                for rank, row in enumerate(increase_rows, 1):
                    medal = _MEDAL_EMOJIS.get(rank, f"**{rank}.**")
                    name = row["citizen_name"] or row["user_id"]
                    inc_lines.append(
                        f"{medal} **{name}** — {_fmt_increase(row['increase'])}"
                        f" *(nu {_fmt_gold(row['wealth_now'])})*"
                    )
                embed.add_field(
                    name=f"{_TREND_EMOJI} Grootste stijging afgelopen {dagen} dagen",
                    value="\n".join(inc_lines),
                    inline=False,
                )
            else:
                embed.add_field(
                    name=f"{_TREND_EMOJI} Stijging afgelopen {dagen} dagen",
                    value="*Niet genoeg historische data voor dit tijdvenster.*",
                    inline=False,
                )
        elif max_days > 0:
            embed.set_footer(
                text=(
                    f"Tip: gebruik /wealth dagen:<aantal> om ook de stijging te zien "
                    f"(maximaal {max_days} dagen beschikbaar)"
                )
            )

        if updated_at_str:
            try:
                dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                dt_nl = dt.astimezone(_TZ_NL)
                footer_text = f"Bijgewerkt op {dt_nl.strftime('%d-%m-%Y %H:%M')} {dt_nl.strftime('%Z')}"
                if max_days > 0:
                    footer_text += f" · Historie beschikbaar: {max_days} dag(en)"
                if not (dagen is None or max_days == 0):
                    embed.set_footer(text=footer_text)
                elif max_days == 0:
                    embed.set_footer(text=footer_text)
            except Exception:
                pass

        return embed


async def setup(bot) -> None:
    await bot.add_cog(WealthCog(bot))
