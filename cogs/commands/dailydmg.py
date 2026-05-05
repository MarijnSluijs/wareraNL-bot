"""Slash command /dailydmg — daily battle damage for NL players, countries and MUs.

Data comes from ``daily_dmg_hits`` which is populated hourly by the
``cogs/tasks/daily_dmg.py`` task.  That task scans finished battles from the
last 48 hours and stores per-NL-player damage via
``battleLootSummary.getByBattleAndUser``.

Input
-----
/dailydmg type:speler  naam:PlayerName [datum:DD-MM-JJJJ]
/dailydmg type:land    naam:Netherlands [datum:DD-MM-JJJJ]
/dailydmg type:mu      naam:MU Name     [datum:DD-MM-JJJJ]

If *datum* is omitted, today (UTC) is used.
If *naam* is omitted, a top-10 leaderboard is shown for the chosen type.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.country_utils import find_country, country_id as get_country_id
from services.damage_calc import fmt_damage

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

_DEFAULT_TOP = 10
_MAX_TOP = 50

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _medal(rank: int) -> str:
    return _MEDALS.get(rank, f"`{rank:>2}.`")


def _no_data_msg(date_str: str, earliest: str | None) -> str:
    """Return a user-friendly 'no data' message, noting earliest available date."""
    if earliest and date_str < earliest:
        label = datetime.strptime(earliest, "%Y-%m-%d").strftime("%-d %B %Y")
        return (
            f"Geen historische data beschikbaar voor deze datum.\n"
            f"De vroegste beschikbare datum is **{label}**.\n"
            "Gebruik `/peil dagschade-backfill` om oudere data op te halen."
        )
    return "Nog geen data voor deze datum — de bot verzamelt elk uur nieuwe gevechtsdata."


def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    inner = resp.get("result", {})
    if isinstance(inner, dict):
        return inner.get("data", inner)
    return resp


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

    # "land"
    return await country_autocomplete(interaction, current)


# ── Cog ──────────────────────────────────────────────────────────────────────

class DailydmgCog(CommandCogBase, name="dailydmg"):
    """Cog for the /dailydmg command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Country name → ID resolution
    # ------------------------------------------------------------------ #

    async def _fetch_country_list(self) -> list[dict]:
        """Return raw list of country dicts from API or DB."""
        client = self._client
        if client:
            try:
                raw = await client.get("/country.getAllCountries")
                data = _unwrap(raw)
                if isinstance(data, list):
                    return [c for c in data if isinstance(c, dict)]
                if isinstance(data, dict):
                    for key in ("items", "countries", "data", "results"):
                        v = data.get(key)
                        if isinstance(v, list):
                            return [c for c in v if isinstance(c, dict)]
            except Exception:
                pass
        if self._db:
            try:
                name_map = await self._db.get_country_name_map()
                return [{"_id": cid, "name": cname} for cid, cname in name_map.items()]
            except Exception:
                pass
        return []

    async def _fetch_country_map(self) -> dict[str, str]:
        """Return {country_id: country_name}."""
        return {
            get_country_id(c): (c.get("name") or get_country_id(c))
            for c in await self._fetch_country_list()
            if get_country_id(c)
        }

    async def _resolve_country_id(self, country_name: str) -> Optional[str]:
        """Map a country name (Dutch or English) to its in-game country_id."""
        country_list = await self._fetch_country_list()
        hit = find_country(country_name, country_list)
        if hit:
            return get_country_id(hit)
        return None

    # ------------------------------------------------------------------ #
    # Command
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="dailydmg",
        description="Toon de dagelijkse gevechtsschade per speler, land of MU.",
    )
    @app_commands.describe(
        soort="Wat wil je bekijken? (speler / land / mu)",
        naam="Naam van de speler, het land of de MU (leeg = top 10).",
        datum="Datum in DD-MM-JJJJ formaat (leeg = vandaag).",
        top_n="Aantal te tonen rijen voor een leaderboard (standaard 10, max 50).",
    )
    @app_commands.choices(soort=[
        app_commands.Choice(name="speler", value="speler"),
        app_commands.Choice(name="land",   value="land"),
        app_commands.Choice(name="mu",     value="mu"),
    ])
    @app_commands.autocomplete(naam=_naam_autocomplete)
    async def dailydmg(
        self,
        interaction: discord.Interaction,
        soort: str,
        naam: Optional[str] = None,
        datum: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> None:
        """Daily battle damage leaderboard for NL citizens by player / country / MU."""
        if not self._db:
            await interaction.response.send_message(
                "Database nog niet gereed.", ephemeral=True
            )
            return

        # ── Parse date ────────────────────────────────────────────────
        if datum:
            date_str: Optional[str] = None
            for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    date_str = datetime.strptime(datum.strip(), fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass
            if date_str is None:
                await interaction.response.send_message(
                    f"❌ Ongeldig datumformaat `{datum}`. Gebruik DD-MM-JJJJ.",
                    ephemeral=True,
                )
                return
        else:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        await interaction.response.defer(thinking=True)

        limit = max(1, min(top_n or _DEFAULT_TOP, _MAX_TOP))
        date_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%-d %B %Y")

        # ── Dispatch ──────────────────────────────────────────────────
        if soort == "speler":
            await self._handle_speler(interaction, naam, date_str, date_label, limit)
        elif soort == "land":
            await self._handle_land(interaction, naam, date_str, date_label, limit)
        else:  # "mu"
            await self._handle_mu(interaction, naam, date_str, date_label, limit)

    # ------------------------------------------------------------------ #
    # Handlers per type
    # ------------------------------------------------------------------ #

    async def _handle_speler(
        self,
        interaction: discord.Interaction,
        naam: Optional[str],
        date_str: str,
        date_label: str,
        limit: int,
    ) -> None:
        db = self._db
        earliest = await db.get_daily_dmg_earliest_date()

        if naam:
            # ── Single player ──────────────────────────────────────────────────
            row = await db.get_player_daily_dmg(date_str, naam)
            if row is None:
                await interaction.followup.send(
                    f"Geen dagelijkse schadedata gevonden voor **{naam}** op {date_label}."
                    f"\n{_no_data_msg(date_str, earliest)}"
                )
                return

            top_rows = await db.get_top_players_daily_dmg(date_str, 5)
            nl_rank = None
            for i, r in enumerate(top_rows, 1):
                if r["user_id"] == row["user_id"]:
                    nl_rank = i
                    break
            if nl_rank is None:
                # Player not in top 5 — find their actual rank (no cap)
                all_rows = await db.get_top_players_daily_dmg(date_str, 10_000)
                for i, r in enumerate(all_rows, 1):
                    if r["user_id"] == row["user_id"]:
                        nl_rank = i
                        break

            lines = await self._fmt_player_top5(top_rows, highlight_id=row["user_id"])
            embed = discord.Embed(
                title=f"⚔️ Dagelijkse schade — {date_label} — Top 5",
                description="\n".join(lines) if lines else "*Nog geen data*",
                colour=self._embed_colour(),
            )
            if row["user_id"] not in [r["user_id"] for r in top_rows[:5]]:
                rank_str = f"#{nl_rank}" if nl_rank else "?"
                embed.add_field(
                    name=f"📍 {row['citizen_name']}",
                    value=f"Rang **{rank_str}** — {fmt_damage(row['total_damage'])} ({row['battle_count']} gevecht{'en' if row['battle_count'] != 1 else ''})",
                    inline=False,
                )
            embed.set_footer(text=f"Data voor {date_label} · elk uur bijgewerkt")
            await interaction.followup.send(embed=embed)

        else:
            # ── Leaderboard ────────────────────────────────────────────
            rows = await db.get_top_players_daily_dmg(date_str, limit)
            if not rows:
                await interaction.followup.send(_no_data_msg(date_str, earliest))
                return

            lines = await self._fmt_player_top5(rows)
            embed = discord.Embed(
                title=f"⚔️ Dagelijkse schade spelers — {date_label} — Top {len(rows)}",
                description="\n".join(lines),
                colour=self._embed_colour(),
            )
            embed.set_footer(text=f"Data voor {date_label} · elk uur bijgewerkt")
            await interaction.followup.send(embed=embed)

    async def _handle_land(
        self,
        interaction: discord.Interaction,
        naam: Optional[str],
        date_str: str,
        date_label: str,
        limit: int,
    ) -> None:
        db = self._db
        country_map = await self._fetch_country_map()
        earliest = await db.get_daily_dmg_earliest_date()

        if naam:
            # ── Single country ─────────────────────────────────────────
            country_id = await self._resolve_country_id(naam)
            if country_id is None:
                await interaction.followup.send(
                    f"❌ Land **{naam}** niet gevonden. Gebruik de Engelse naam, bijv. `Netherlands`.",
                    ephemeral=True,
                )
                return

            country_display = country_map.get(country_id, naam)
            row = await db.get_country_daily_dmg(date_str, country_id)
            if row is None or row["total_damage"] == 0:
                await interaction.followup.send(
                    f"Geen dagelijkse schadedata voor **{country_display}** op {date_label}."
                    f"\n{_no_data_msg(date_str, earliest)}"
                )
                return

            embed = discord.Embed(
                title=f"🌍 Dagelijkse schade — {country_display} — {date_label}",
                colour=self._embed_colour(),
            )
            embed.add_field(
                name="Totale schade",
                value=fmt_damage(row["total_damage"]),
                inline=True,
            )
            embed.add_field(
                name="Actieve spelers",
                value=str(row["player_count"]),
                inline=True,
            )
            embed.add_field(
                name="Gevechten",
                value=str(row["battle_count"]),
                inline=True,
            )
            top_rows = await db.get_top_players_daily_dmg(date_str, 10, country_id=country_id)
            if top_rows:
                player_lines = await self._fmt_player_top5(top_rows)
                embed.add_field(
                    name=f"Top {len(top_rows)} spelers",
                    value="\n".join(player_lines),
                    inline=False,
                )
            embed.set_footer(text=f"Data voor {date_label} · elk uur bijgewerkt")
            await interaction.followup.send(embed=embed)

        else:
            # ── Leaderboard ────────────────────────────────────────────
            rows = await db.get_top_countries_daily_dmg(date_str, limit)
            if not rows:
                await interaction.followup.send(_no_data_msg(date_str, earliest))
                return

            lines: list[str] = []
            for rank, r in enumerate(rows, 1):
                cname = country_map.get(r["country_id"], r["country_id"])
                prefix = _medal(rank)
                lines.append(
                    f"{prefix} **{cname}** — {fmt_damage(r['total_damage'])}"
                    f" ({r['player_count']} spelers, {r['battle_count']} gevechten)"
                )

            embed = discord.Embed(
                title=f"🌍 Dagelijkse schade landen — {date_label} — Top {len(rows)}",
                description="\n".join(lines),
                colour=self._embed_colour(),
            )
            embed.set_footer(text=f"Data voor {date_label} · elk uur bijgewerkt")
            await interaction.followup.send(embed=embed)

    async def _handle_mu(
        self,
        interaction: discord.Interaction,
        naam: Optional[str],
        date_str: str,
        date_label: str,
        limit: int,
    ) -> None:
        db = self._db
        earliest = await db.get_daily_dmg_earliest_date()

        if naam:
            # ── Single MU ──────────────────────────────────────────────────────
            row = await db.get_mu_daily_dmg(date_str, naam)
            if row is None or row["total_damage"] == 0:
                await interaction.followup.send(
                    f"Geen dagelijkse schadedata gevonden voor MU **{naam}** op {date_label}."
                    f"\n{_no_data_msg(date_str, earliest)}"
                )
                return

            mu_display = row["mu_name"] or naam
            embed = discord.Embed(
                title=f"🏴 Dagelijkse schade — {mu_display} — {date_label}",
                colour=self._embed_colour(),
            )
            embed.add_field(
                name="Totale schade",
                value=fmt_damage(row["total_damage"]),
                inline=True,
            )
            embed.add_field(
                name="Actieve spelers",
                value=str(row["player_count"]),
                inline=True,
            )
            embed.add_field(
                name="Gevechten",
                value=str(row["battle_count"]),
                inline=True,
            )
            top_rows = await db.get_top_players_daily_dmg(date_str, 10, mu_id=row["mu_id"])
            if top_rows:
                player_lines = await self._fmt_player_top5(top_rows)
                embed.add_field(
                    name=f"Top {len(top_rows)} spelers",
                    value="\n".join(player_lines),
                    inline=False,
                )
            embed.set_footer(text=f"Data voor {date_label} · elk uur bijgewerkt")
            await interaction.followup.send(embed=embed)

        else:
            # ── Leaderboard ────────────────────────────────────────────
            rows = await db.get_top_mus_daily_dmg(date_str, limit)
            if not rows:
                await interaction.followup.send(_no_data_msg(date_str, earliest))
                return

            lines: list[str] = []
            for rank, r in enumerate(rows, 1):
                mu_display = r["mu_name"] or r["mu_id"]
                prefix = _medal(rank)
                lines.append(
                    f"{prefix} **{mu_display}** — {fmt_damage(r['total_damage'])}"
                    f" ({r['player_count']} spelers)"
                )

            embed = discord.Embed(
                title=f"🏴 Dagelijkse schade MUs — {date_label} — Top {len(rows)}",
                description="\n".join(lines),
                colour=self._embed_colour(),
            )
            embed.set_footer(text=f"Data voor {date_label} · elk uur bijgewerkt")
            await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _fmt_player_top5(
        self,
        rows: list[dict],
        highlight_id: Optional[str] = None,
    ) -> list[str]:
        """Format a player leaderboard list, resolving names from DB."""
        if not rows:
            return []
        user_ids = [r["user_id"] for r in rows]
        # Resolve names from citizen_levels (best effort)
        names: dict[str, str] = {}
        if self._db:
            try:
                for uid in user_ids:
                    name = await self._db.get_citizen_name_by_id(uid)
                    if name:
                        names[uid] = name
            except Exception:
                pass

        lines: list[str] = []
        for rank, r in enumerate(rows, 1):
            uid = r["user_id"]
            name = names.get(uid, uid)
            display = f"**__{name}__**" if uid == highlight_id else f"**{name}**"
            prefix = _medal(rank)
            battles = r.get("battle_count", 0)
            battle_str = f" ({battles} gevecht{'en' if battles != 1 else ''})"
            lines.append(f"{prefix} {display} — {fmt_damage(r['total_damage'])}{battle_str}")
        return lines


async def setup(bot) -> None:
    await bot.add_cog(DailydmgCog(bot))
