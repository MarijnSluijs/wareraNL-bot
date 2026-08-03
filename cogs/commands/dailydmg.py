"""Slash command /dailydmg — daily battle damage for players, countries and MUs.

Data comes from ``citizen_daily_damage``, which the hourly fetcher sweep
derives from the game's own weekly damage counter:

    daily damage = weekly counter now − weekly counter when the game day opened

A game day runs 02:00→02:00 UTC and the weekly counter resets Monday 02:00,
so a week rollover simply means the day starts from zero (see
``services/game_time.py``).  This replaces the old approach of scanning every
finished battle and summing per-player loot summaries — same numbers, a single
API call, and it covers every player in the game rather than just NL citizens.

Input
-----
/dailydmg soort:speler  naam:PlayerName  [dag:2026-08-02] [land:Netherlands]
/dailydmg soort:land    naam:Netherlands [dag:2026-08-02]
/dailydmg soort:mu      naam:MU Name     [dag:2026-08-02]

If *dag* is omitted, the current game day is used.
If *naam* is omitted, a top-10 leaderboard is shown for the chosen type.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import country_id as get_country_id
from services.country_utils import find_country
from services.damage_calc import fmt_damage
from services.game_time import fmt_game_day, game_day

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

_DEFAULT_TOP = 10
_MAX_TOP = 50
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _medal(rank: int) -> str:
    return _MEDALS.get(rank, f"`{rank:>2}.`")


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def _naam_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete for 'naam': dispatch to player/MU/country based on 'soort'."""
    options = {
        o["name"]: o["value"]
        for o in (interaction.data or {}).get("options", [])  # type: ignore[union-attr]
    }
    selected_type = options.get("soort", "speler")
    db = getattr(interaction.client, "_ext_db", None)

    if selected_type == "speler":
        if not db:
            return []
        try:
            matches = await db.search_citizen_names(current, limit=25)
            return [app_commands.Choice(name=n, value=n) for n, _ in matches]
        except Exception:  # noqa: BLE001
            return []

    if selected_type == "mu":
        if not db:
            return []
        try:
            names = await db.get_known_mu_names(current)
            return [app_commands.Choice(name=n, value=n) for n in names]
        except Exception:  # noqa: BLE001
            return []

    return await country_autocomplete(interaction, current)


async def _dag_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Offer the game days we actually have recorded, newest first."""
    db = getattr(interaction.client, "_ext_db", None)
    if not db:
        return []
    try:
        days = await db.get_recorded_game_days(limit=25)
    except Exception:  # noqa: BLE001
        return []
    today = game_day()
    q = current.strip().lower()
    out: list[app_commands.Choice[str]] = []
    for d in days:
        label = fmt_game_day(d)
        if d == today:
            label = f"Vandaag — {label}"
        if q and q not in label.lower() and q not in d:
            continue
        out.append(app_commands.Choice(name=label[:100], value=d))
    return out[:25]


# ── Cog ──────────────────────────────────────────────────────────────────────

class DailydmgCog(CommandCogBase, name="dailydmg"):
    """Cog for the /dailydmg command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ── country helpers ───────────────────────────────────────────────

    async def _country_map(self) -> dict[str, str]:
        if self._db:
            try:
                name_map = await self._db.get_country_name_map()
                if name_map:
                    return dict(name_map)
            except Exception:  # noqa: BLE001
                pass
        return {}

    async def _resolve_country_id(self, country_name: str) -> Optional[str]:
        name_map = await self._country_map()
        hit = find_country(
            country_name, [{"_id": cid, "name": n} for cid, n in name_map.items()]
        )
        return get_country_id(hit) if hit else None

    async def _no_data(self, dag: str) -> str:
        days = await self._db.get_recorded_game_days(limit=8)
        if days and dag not in days:
            return (
                f"Geen data voor **{fmt_game_day(dag)}**.\n"
                "Beschikbare dagen: " + ", ".join(fmt_game_day(d) for d in days[:5])
            )
        return (
            f"Nog geen data voor **{fmt_game_day(dag)}** — "
            "de schade wordt elk uur bijgewerkt."
        )

    # ── command ───────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="dailydmg",
        description="Toon de dagelijkse gevechtsschade per speler, land of MU.",
    )
    @app_commands.describe(
        soort="Wat wil je bekijken? (speler / land / mu)",
        naam="Naam van de speler, het land of de MU (leeg = top 10).",
        dag="Game-dag om te tonen (standaard vandaag). Dag loopt 02:00–02:00 UTC.",
        land="Optioneel: beperk de speler-/MU-ranglijst tot één land.",
        top_n="Aantal te tonen rijen voor een leaderboard (standaard 10, max 50).",
    )
    @app_commands.choices(soort=[
        app_commands.Choice(name="speler", value="speler"),
        app_commands.Choice(name="land",   value="land"),
        app_commands.Choice(name="mu",     value="mu"),
    ])
    @app_commands.autocomplete(
        naam=_naam_autocomplete,
        dag=_dag_autocomplete,
        land=country_autocomplete,
    )
    async def dailydmg(
        self,
        ctx: Context,
        soort: str = "speler",
        naam: Optional[str] = None,
        dag: Optional[str] = None,
        land: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> None:
        """Daily battle damage leaderboard by player / country / MU."""
        if not self._db:
            await ctx.send("Database nog niet gereed.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        date_str = (dag or "").strip() or game_day()
        date_label = fmt_game_day(date_str)
        limit = max(1, min(top_n or _DEFAULT_TOP, _MAX_TOP))

        scope_id: Optional[str] = None
        scope_name: Optional[str] = None
        if land:
            scope_id = await self._resolve_country_id(land)
            if not scope_id:
                await ctx.send(f"❌ Land **{land}** niet gevonden.")
                return
            scope_name = (await self._country_map()).get(scope_id, land)

        if soort == "speler":
            await self._handle_speler(
                ctx, naam, date_str, date_label, limit, scope_id, scope_name
            )
        elif soort == "land":
            await self._handle_land(ctx, naam, date_str, date_label, limit)
        else:
            await self._handle_mu(
                ctx, naam, date_str, date_label, limit, scope_id, scope_name
            )

    # ── speler ────────────────────────────────────────────────────────

    async def _handle_speler(
        self,
        ctx: Context,
        naam: Optional[str],
        date_str: str,
        date_label: str,
        limit: int,
        scope_id: Optional[str],
        scope_name: Optional[str],
    ) -> None:
        db = self._db
        suffix = f" ({scope_name})" if scope_name else ""

        if naam:
            row = await db.get_daily_damage_player(date_str, naam)
            if row is None:
                await ctx.send(
                    f"Geen dagelijkse schadedata voor **{naam}** op {date_label}.\n"
                    + await self._no_data(date_str)
                )
                return

            player_country = row["country_id"]
            rank = await db.get_daily_damage_rank(
                date_str, row["user_id"], country_id=player_country
            )
            top_rows = await db.get_daily_damage_ranking(
                date_str, 5, country_id=player_country
            )
            label = (await self._country_map()).get(player_country or "", "") or ""

            lines: list[str] = []
            in_top5 = False
            for i, r in enumerate(top_rows, 1):
                url = f"https://app.warera.io/user/{r['user_id']}"
                if r["user_id"] == row["user_id"]:
                    in_top5 = True
                    lines.append(
                        f"{_medal(i)} **__[{r['citizen_name']}]({url})__** — {fmt_damage(r['damage'])}"
                    )
                else:
                    lines.append(
                        f"{_medal(i)} **[{r['citizen_name']}]({url})** — {fmt_damage(r['damage'])}"
                    )

            embed = discord.Embed(
                title=f"⚔️ Dagelijkse schade {label} — {date_label} — Top 5".replace("  ", " "),
                description="\n".join(lines) if lines else "*Nog geen data*",
                colour=self._embed_colour(),
            )
            if not in_top5:
                extra = f" · {row['mu_name']}" if row.get("mu_name") else ""
                value = (
                    f"Rang **#{rank}** — {fmt_damage(row['damage'])}{extra}"
                    if rank else f"{fmt_damage(row['damage'])}{extra}"
                )
                embed.add_field(
                    name=f"📍 {row['citizen_name']}", value=value, inline=False
                )
            embed.set_footer(text=f"Game-dag {date_label} (02:00–02:00 UTC) · elk uur bijgewerkt")
            await ctx.send(embed=embed)
            return

        rows = await db.get_daily_damage_ranking(date_str, limit, country_id=scope_id)
        if not rows:
            await ctx.send(await self._no_data(date_str))
            return

        lines = []
        for rank, r in enumerate(rows, 1):
            url = f"https://app.warera.io/user/{r['user_id']}"
            lines.append(
                f"{_medal(rank)} **[{r['citizen_name']}]({url})** — {fmt_damage(r['damage'])}"
            )

        embed = discord.Embed(
            title=f"⚔️ Dagelijkse schade spelers{suffix} — {date_label} — Top {len(rows)}",
            description="\n".join(lines),
            colour=self._embed_colour(),
        )
        embed.set_footer(text=f"Game-dag {date_label} (02:00–02:00 UTC) · elk uur bijgewerkt")
        await ctx.send(embed=embed)

    # ── land ──────────────────────────────────────────────────────────

    async def _handle_land(
        self,
        ctx: Context,
        naam: Optional[str],
        date_str: str,
        date_label: str,
        limit: int,
    ) -> None:
        db = self._db

        if naam:
            country_id = await self._resolve_country_id(naam)
            if not country_id:
                await ctx.send(f"❌ Land **{naam}** niet gevonden.")
                return
            name_map = await self._country_map()
            display = name_map.get(country_id, naam)

            total = await db.get_country_daily_total(date_str, country_id)
            if not total:
                await ctx.send(
                    f"Geen dagelijkse schadedata voor **{display}** op {date_label}.\n"
                    + await self._no_data(date_str)
                )
                return

            embed = discord.Embed(
                title=f"🌍 {display} — dagelijkse schade {date_label}",
                description=(
                    f"**Totaal:** {fmt_damage(total['damage'])}\n"
                    f"**Vechters:** {total['fighters']}"
                ),
                colour=self._embed_colour(),
            )
            top_rows = await db.get_daily_damage_ranking(
                date_str, 5, country_id=country_id
            )
            if top_rows:
                embed.add_field(
                    name=f"Top {len(top_rows)} spelers",
                    value="\n".join(
                        f"{_medal(i)} **{r['citizen_name']}** — {fmt_damage(r['damage'])}"
                        for i, r in enumerate(top_rows, 1)
                    ),
                    inline=False,
                )
            embed.set_footer(text=f"Game-dag {date_label} (02:00–02:00 UTC) · elk uur bijgewerkt")
            await ctx.send(embed=embed)
            return

        rows = await db.get_daily_damage_by_country(date_str, limit)
        if not rows:
            await ctx.send(await self._no_data(date_str))
            return

        lines = [
            f"{_medal(i)} **{r['name']}** — {fmt_damage(r['damage'])}"
            f" ({r['fighters']} vechters)"
            for i, r in enumerate(rows, 1)
        ]
        embed = discord.Embed(
            title=f"🌍 Dagelijkse schade landen — {date_label} — Top {len(rows)}",
            description="\n".join(lines),
            colour=self._embed_colour(),
        )
        embed.set_footer(text=f"Game-dag {date_label} (02:00–02:00 UTC) · elk uur bijgewerkt")
        await ctx.send(embed=embed)

    # ── mu ────────────────────────────────────────────────────────────

    async def _handle_mu(
        self,
        ctx: Context,
        naam: Optional[str],
        date_str: str,
        date_label: str,
        limit: int,
        scope_id: Optional[str],
        scope_name: Optional[str],
    ) -> None:
        db = self._db
        suffix = f" ({scope_name})" if scope_name else ""

        if naam:
            mu_id, mu_name = await db.get_known_mu_by_name(naam)
            if not mu_id:
                await ctx.send(f"❌ MU **{naam}** niet gevonden.")
                return

            total = await db.get_mu_daily_total(date_str, mu_id)
            if not total:
                await ctx.send(
                    f"Geen dagelijkse schadedata voor **{mu_name or naam}** op {date_label}.\n"
                    + await self._no_data(date_str)
                )
                return

            embed = discord.Embed(
                title=f"🏴 {mu_name or naam} — dagelijkse schade {date_label}",
                description=(
                    f"**Totaal:** {fmt_damage(total['damage'])}\n"
                    f"**Vechters:** {total['fighters']}"
                ),
                colour=self._embed_colour(),
            )
            embed.set_footer(text=f"Game-dag {date_label} (02:00–02:00 UTC) · elk uur bijgewerkt")
            await ctx.send(embed=embed)
            return

        rows = await db.get_daily_damage_by_mu(date_str, limit, country_id=scope_id)
        if not rows:
            await ctx.send(await self._no_data(date_str))
            return

        lines = [
            f"{_medal(i)} **{r['name']}** — {fmt_damage(r['damage'])}"
            f" ({r['fighters']} vechters)"
            for i, r in enumerate(rows, 1)
        ]
        embed = discord.Embed(
            title=f"🏴 Dagelijkse schade MUs{suffix} — {date_label} — Top {len(rows)}",
            description="\n".join(lines),
            colour=self._embed_colour(),
        )
        embed.set_footer(text=f"Game-dag {date_label} (02:00–02:00 UTC) · elk uur bijgewerkt")
        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(DailydmgCog(bot))
