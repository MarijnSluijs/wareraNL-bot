"""Slash command /weeklydmg — weekly battle damage ranking per country.

Data comes from ``citizen_weekly_damage_history``, which the hourly fetcher
sweep fills from ``ranking.getRanking(weeklyUserDamages)`` for **every** player
in the game.  The ranking is therefore built purely from in-game data — no
Discord-account matching is involved — so players who have since moved country
still appear correctly under the country they fought for that week.

Usage
-----
/weeklydmg                          — top 10 for the Netherlands, this week
/weeklydmg land:Poland              — top 10 for another country
/weeklydmg week:2026-07-27          — a past game week
/weeklydmg speler:PlayerName        — one player's damage + rank
/weeklydmg top_n:25                 — longer leaderboard
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import (
    CommandCogBase,
    citizen_autocomplete,
    country_autocomplete,
    fmt_nl_time,
)
from services.country_utils import country_id as get_country_id
from services.country_utils import extract_country_list, find_country
from services.damage_calc import fmt_damage
from services.game_time import fmt_week_range, game_week_start, iso_week_label

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

_DEFAULT_TOP = 10
_MAX_TOP = 50
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def _week_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Offer the game weeks we actually have recorded, newest first."""
    db = getattr(interaction.client, "_ext_db", None)
    if not db:
        return []
    try:
        weeks = await db.get_recorded_weeks(limit=25)
    except Exception:  # noqa: BLE001
        return []
    current_week = game_week_start()
    q = current.strip().lower()
    out: list[app_commands.Choice[str]] = []
    for w in weeks:
        label = f"{iso_week_label(w)} ({fmt_week_range(w)})"
        if w == current_week:
            label = f"Deze week — {label}"
        if q and q not in label.lower() and q not in w:
            continue
        out.append(app_commands.Choice(name=label[:100], value=w))
    return out[:25]


class WeeklydmgCog(CommandCogBase, name="weeklydmg"):
    """Cog for the /weeklydmg command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ── helpers ───────────────────────────────────────────────────────

    async def _country_map(self) -> dict[str, str]:
        """Return {country_id: name}, preferring the local snapshot cache."""
        if self._db:
            try:
                name_map = await self._db.get_country_name_map()
                if name_map:
                    return dict(name_map)
            except Exception:  # noqa: BLE001
                pass
        if self._client:
            try:
                resp = await self._client.get("/country.getAllCountries")
                return {
                    get_country_id(c): (c.get("name") or get_country_id(c))
                    for c in extract_country_list(resp)
                    if get_country_id(c)
                }
            except Exception:  # noqa: BLE001
                pass
        return {}

    async def _resolve_country(
        self, land: Optional[str]
    ) -> tuple[Optional[str], str]:
        """Return (country_id, display_name); defaults to the configured NL."""
        name_map = await self._country_map()
        if not land:
            nl_id = self.config.get("nl_country_id")
            return nl_id, name_map.get(nl_id or "", "Nederland")
        country_list = [{"_id": cid, "name": n} for cid, n in name_map.items()]
        hit = find_country(land, country_list)
        if hit:
            cid = get_country_id(hit)
            return cid, str(hit.get("name") or land)
        return None, land

    # ── command ───────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="weeklydmg",
        description="Toon de wekelijkse gevechtsschade-ranglijst van een land.",
    )
    @app_commands.describe(
        land="Land om de ranglijst van te tonen (standaard Nederland).",
        week="Game-week om te tonen (standaard deze week).",
        speler="Optioneel: zoek een specifieke speler op naam of ID.",
        top_n="Optioneel: toon de top N spelers (standaard 10, max 50).",
    )
    @app_commands.autocomplete(
        speler=citizen_autocomplete,
        land=country_autocomplete,
        week=_week_autocomplete,
    )
    async def weeklydmg(
        self,
        ctx: Context,
        land: Optional[str] = None,
        week: Optional[str] = None,
        speler: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> None:
        """Weekly battle damage leaderboard, ranked from in-game data."""
        if not self._db:
            await ctx.send("Diensten niet geïnitialiseerd.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        country_id, country_name = await self._resolve_country(land)
        if not country_id:
            await ctx.send(f"❌ Land **{land}** niet gevonden.")
            return

        week_start = (week or "").strip() or game_week_start()
        is_current = week_start == game_week_start()
        week_label = f"{iso_week_label(week_start)} ({fmt_week_range(week_start)})"

        if speler:
            await self._show_player(ctx, speler, week_start, week_label, country_id, country_name)
            return

        limit = max(1, min(top_n or _DEFAULT_TOP, _MAX_TOP))
        rows, last_updated = await self._db.get_weekly_damage_ranking(
            country_id, week_start, limit
        )

        if not rows:
            weeks = await self._db.get_recorded_weeks(limit=10)
            hint = ""
            if weeks and week_start not in weeks:
                hint = "\nBeschikbare weken: " + ", ".join(
                    iso_week_label(w) for w in weeks[:6]
                )
            await ctx.send(
                f"Nog geen wekelijkse schadedata voor **{country_name}** "
                f"in week {week_label}.{hint}"
            )
            return

        lines = []
        for rank, r in enumerate(rows, 1):
            prefix = _MEDALS.get(rank, f"`{rank:>2}.`")
            url = f"https://app.warera.io/user/{r['user_id']}"
            lines.append(
                f"{prefix} **[{r['citizen_name']}]({url})** — {fmt_damage(r['damage'])}"
            )

        embed = discord.Embed(
            title=f"⚔️ Wekelijkse schade {country_name} — Top {len(rows)}",
            description="\n".join(lines),
            colour=self._embed_colour(),
        )
        footer = f"Week {week_label}"
        if is_current:
            footer += " (lopend)"
        if last_updated:
            footer += f" · bijgewerkt: {fmt_nl_time(last_updated)}"
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    # ── single-player view ────────────────────────────────────────────

    async def _show_player(
        self,
        ctx: Context,
        speler: str,
        week_start: str,
        week_label: str,
        country_id: str,
        country_name: str,
    ) -> None:
        row = await self._db.get_weekly_damage_player(week_start, speler)
        if row is None:
            await ctx.send(
                f"Geen wekelijkse schadedata gevonden voor **{speler}** "
                f"in week {week_label}."
            )
            return

        # Rank within the country the player actually fought for that week,
        # which may differ from the requested country if they moved since.
        player_country = row["country_id"] or country_id
        rank = await self._db.get_weekly_damage_rank(
            player_country, week_start, row["user_id"]
        )
        top_rows, _ = await self._db.get_weekly_damage_ranking(
            player_country, week_start, 5
        )

        lines: list[str] = []
        in_top5 = False
        for i, r in enumerate(top_rows, 1):
            prefix = _MEDALS.get(i, f"`{i}.`")
            url = f"https://app.warera.io/user/{r['user_id']}"
            if r["user_id"] == row["user_id"]:
                in_top5 = True
                lines.append(
                    f"{prefix} **__[{r['citizen_name']}]({url})__** — {fmt_damage(r['damage'])}"
                )
            else:
                lines.append(
                    f"{prefix} **[{r['citizen_name']}]({url})** — {fmt_damage(r['damage'])}"
                )

        label = country_name
        if player_country != country_id:
            name_map = await self._country_map()
            label = name_map.get(player_country, player_country)

        embed = discord.Embed(
            title=f"⚔️ Wekelijkse schade {label} — Top 5",
            description="\n".join(lines) if lines else "*Nog geen data*",
            colour=self._embed_colour(),
        )
        if not in_top5:
            extra = f" · {row['mu_name']}" if row.get("mu_name") else ""
            value = (
                f"Rang **#{rank}** — {fmt_damage(row['damage'])}{extra}"
                if rank
                else f"{fmt_damage(row['damage'])}{extra}"
            )
            embed.add_field(name=f"📍 {row['citizen_name']}", value=value, inline=False)
        embed.set_footer(
            text=f"Week {week_label} · bijgewerkt: {fmt_nl_time(row.get('updated_at') or '')}"
        )
        await ctx.send(embed=embed)


async def setup(bot) -> None:
    """Add the WeeklydmgCog to the bot."""
    await bot.add_cog(WeeklydmgCog(bot))
