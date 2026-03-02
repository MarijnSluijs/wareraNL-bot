"""
This module defines the NiveauverdelingCog, which provides the /niveauverdeling command to show cached level distribution of citizens for a country or all countries in the WarEraNL bot.
- /niveauverdeling [land] [alle_niveaus:Ja/Nee]
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as cid_of
from services.country_utils import find_country

logger = logging.getLogger("discord_bot")


class NiveauverdelingCog(CommandCogBase, name="niveauverdeling"):
    """Cog for the /niveauverdeling command, showing cached level distribution of citizens for a country or all countries."""
    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="niveauverdeling",
        description="Toon de niveauverdeling van burgers voor een land (of alle).",
    )
    @app_commands.describe(
        land="Kies een land, of leeg laten voor alle landen.",
        alle_niveaus="Toon individuele niveaus in plaats van groepen van 5",
    )
    @app_commands.autocomplete(land=country_autocomplete)
    async def leveldist(
        self, ctx: Context, land: str | None = None, alle_niveaus: bool = False
    ):
        """Show the cached level distribution for a country, or all countries if no argument given."""
        if land and land.lower().endswith(" alle"):
            land = land[:-5].strip() or None
            alle_niveaus = True

        if not self._db:
            await ctx.send("Diensten niet geïnitialiseerd.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        country_name = "Alle landen"
        cid: str | None = None

        if land:
            country_list = await self._fetch_country_list(ctx)
            if country_list is None:
                return
            target = find_country(land, country_list)
            if target is None:
                await ctx.send(f"Land `{land}` niet gevonden.")
                return
            cid = cid_of(target)
            country_name = target.get("name", land)

        try:
            (
                level_counts,
                active_counts,
                last_updated,
            ) = await self._db.get_level_distribution(cid)
        except Exception as exc:
            await ctx.send(f"Databasefout: {exc}")
            return

        if not level_counts:
            await ctx.send(
                f"Nog geen gecachte niveaudata voor **{country_name}**.\n"
                f"Run `/peil burgers{' ' + land if land else ''}` om de cache op te bouwen."
            )
            return

        total = sum(level_counts.values())
        has_active = bool(active_counts)
        colour = self._embed_colour()

        _grn = "\033[32m"
        _rst = "\033[0m"
        bar_w = 20

        def _make_bar(total_cnt: int, bar_max: int, active_cnt: int = 0) -> str:
            total_filled = max(1, round(total_cnt / bar_max * bar_w))
            if has_active and active_cnt > 0:
                active_filled = min(
                    max(1, round(active_cnt / bar_max * bar_w)), total_filled
                )
                inactive_filled = total_filled - active_filled
                return (
                    _grn
                    + "█" * active_filled
                    + _rst
                    + "█" * inactive_filled
                    + "░" * (bar_w - total_filled)
                )
            return "█" * total_filled + "░" * (bar_w - total_filled)

        if alle_niveaus:
            max_level = max(level_counts)
            bar_max = max(level_counts.values())
            if has_active:
                header = f"{'Lvl':>4}  {'Totaal (actief)':>15}  Bar"
                sep = "─" * 46
                data_rows = [
                    f"{lvl:>4}  {level_counts[lvl]:>5} ({active_counts.get(lvl, 0):>4})  "
                    f"{_make_bar(level_counts[lvl], bar_max, active_counts.get(lvl, 0))}"
                    for lvl in range(1, max_level + 1)
                    if lvl in level_counts
                ]
            else:
                header = f"{'Lvl':>4}  {'Count':>6}  Bar"
                sep = "─" * 32
                data_rows = [
                    f"{lvl:>4}  {level_counts[lvl]:>6}  {_make_bar(level_counts[lvl], bar_max)}"
                    for lvl in range(1, max_level + 1)
                    if lvl in level_counts
                ]
        else:
            max_level = max(level_counts)
            buckets: dict[int, int] = {}
            active_buckets: dict[int, int] = {}
            for lvl, cnt in level_counts.items():
                bucket = ((lvl - 1) // 5) * 5 + 1
                buckets[bucket] = buckets.get(bucket, 0) + cnt
            for lvl, cnt in active_counts.items():
                bucket = ((lvl - 1) // 5) * 5 + 1
                active_buckets[bucket] = active_buckets.get(bucket, 0) + cnt
            bar_max = max(buckets.values())
            if has_active:
                header = f"{'Levels':<9}  {'Totaal (actief)':>15}  Bar"
                sep = "─" * 46
                data_rows = [
                    f"{b:>3}–{min(b + 4, max_level):<3}  "
                    f"{buckets[b]:>5} ({active_buckets.get(b, 0):>4})  "
                    f"{_make_bar(buckets[b], bar_max, active_buckets.get(b, 0))}"
                    for b in sorted(buckets)
                ]
            else:
                header = f"{'Levels':<9}  {'Count':>6}  Bar"
                sep = "─" * 34
                data_rows = [
                    f"{b:>3}–{min(b + 4, max_level):<3}  {buckets[b]:>6}  {_make_bar(buckets[b], bar_max)}"
                    for b in sorted(buckets)
                ]

        embed_limit = 3900
        label = "Alle niveaus" if alle_niveaus else "5-niveau groepen"
        footer_parts = [f"{total} burgers  •  {label}"]
        if has_active:
            total_active = sum(active_counts.values())
            footer_parts.append(f"{total_active} actief (< 24h)")
            footer_parts.append("█ groen = actief")
        if last_updated:
            footer_parts.append(
                f"Bijgewerkt: {last_updated[:16].replace('T', ' ')} UTC"
            )
        footer_text = "  •  ".join(footer_parts)

        block_lang = "ansi" if has_active else ""

        chunks: list[list[str]] = []
        current: list[str] = []
        for row in data_rows:
            candidate = "\n".join(current + [row])
            if (
                len(f"```{block_lang}\n{header}\n{sep}\n{candidate}\n```") > embed_limit
                and current
            ):
                chunks.append(current)
                current = [row]
            else:
                current.append(row)
        if current:
            chunks.append(current)

        for page_idx, chunk in enumerate(chunks):
            block = f"```{block_lang}\n{header}\n{sep}\n" + "\n".join(chunk) + "\n```"
            embed = discord.Embed(
                title=f"Niveauverdeling — {country_name}",
                description=block,
                colour=colour,
            )
            embed.set_footer(
                text=(
                    footer_text
                    if page_idx == 0
                    else f"{total} burgers  •  {label} (vervolg)"
                )
            )
            await ctx.send(embed=embed)


async def setup(bot) -> None:
    """Add the NiveauverdelingCog to the bot."""
    await bot.add_cog(NiveauverdelingCog(bot))
