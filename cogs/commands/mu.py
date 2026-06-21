"""MU-related slash commands for the NL Discord bot.

Commands
--------
/muplek          – Table of all Dutch MUs with member counts, limits and free spots.
/mu_inactiviteit – Lists inactive MU members (no login in the last 72 hours).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from cogs.tasks.war_guild_divisions import DIVISION_MUS
from services.api_client import APIClient

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# Dormitories level → maximum member capacity
DORM_CAPACITY: dict[int, int] = {
    1: 5,
    2: 10,
    3: 15,
    4: 20,
    5: 25,
}
INACTIVITY_HOURS = 72


def _unwrap(resp: object) -> object:
    """Unwrap a tRPC result envelope."""
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            inner = v.get("data", v)
            return inner
    return resp


def _last_connection(obj: object) -> Optional[str]:
    """Extract lastConnectionAt from a getUserLite response."""
    if not isinstance(obj, dict):
        return None
    dates = obj.get("dates")
    if isinstance(dates, dict):
        return dates.get("lastConnectionAt")
    # flat fallback
    return obj.get("lastConnectionAt") or obj.get("lastLoginAt")


def _username(obj: object) -> str:
    if not isinstance(obj, dict):
        return "?"
    return obj.get("username") or obj.get("name") or "?"


def _fmt_duration(hours: float) -> str:
    d = int(hours // 24)
    h = int(hours % 24)
    if d:
        return f"{d}d {h}u"
    return f"{h}u"


class MU(commands.Cog, name="mu"):
    """MU-related commands."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot
        self.config: dict = getattr(bot, "config", {}) or {}
        self._client: Optional[APIClient] = None
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _get_client(self) -> APIClient:
        if self._client is None:
            base_url = self.config.get("api_base_url", "https://api2.warera.io/trpc")
            api_keys: list[str] = []
            try:
                with open("_api_keys.json") as f:
                    api_keys = json.load(f).get("keys", [])
            except FileNotFoundError:
                pass
            self._client = APIClient(base_url=base_url, api_keys=api_keys)
            await self._client.start()
        return self._client

    def _mus_path(self) -> str:
        testing = getattr(self.bot, "testing", False)
        return "templates/mus.testing.json" if testing else "templates/mus.json"

    def _extract_mu_ids_from_template(self) -> list[str]:
        """Read mus.json and return the MU IDs listed in it."""
        try:
            with open(self._mus_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning(
                "_extract_mu_ids_from_template: failed to read mus.json: %s", exc
            )
            return []
        ids: list[str] = []
        seen: set[str] = set()
        for entry in data.get("embeds", []):
            if not isinstance(entry, dict):
                continue
            mu_id = str(entry.get("id") or "").strip()
            if not mu_id:
                desc = str(entry.get("description", ""))
                m = re.search(r"/mu/([A-Za-z0-9]+)", desc)
                if m:
                    mu_id = m.group(1)
            if mu_id and mu_id not in seen:
                ids.append(mu_id)
                seen.add(mu_id)
        return ids

    async def _get_all_dutch_mus(self) -> list[dict]:
        """Fetch MUs listed in mus.json via batch mu.getById calls."""
        mu_ids = self._extract_mu_ids_from_template()
        if not mu_ids:
            logger.warning("_get_all_dutch_mus: no MU IDs found in mus.json")
            return []

        client = await self._get_client()
        inputs = [{"muId": mid} for mid in mu_ids]
        try:
            results = await client.batch_get("/mu.getById", inputs)
        except Exception as exc:
            logger.error("_get_all_dutch_mus: batch_get failed: %s", exc)
            return []

        mus: list[dict] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                mus.append(data)

        return sorted(mus, key=lambda m: m.get("name", "").lower())

    async def _get_division_mu_data(self) -> list[tuple[int, str, dict]]:
        """Return [(division_num, mu_name, mu_data)] for all MUs in DIVISION_MUS.

        IDs are resolved from the known_mus DB table, falling back to mus.json.
        """
        # Build name→id map from DB (known_mus)
        db_id_map: dict[str, str] = {}
        shared_db = getattr(self.bot, "_ext_db", None)
        if shared_db:
            try:
                for mu_id, mu_name, _ in await shared_db.get_all_known_mu_ids():
                    db_id_map[mu_name.lower()] = mu_id
            except Exception as exc:
                logger.warning("_get_division_mu_data: DB lookup failed: %s", exc)

        # Fallback: name→id from mus.json
        json_id_map: dict[str, str] = {}
        try:
            with open(self._mus_path(), "r", encoding="utf-8") as f:
                for e in json.load(f).get("embeds", []):
                    if e.get("id") and e.get("name"):
                        json_id_map[e["name"].lower()] = str(e["id"])
        except Exception:
            pass

        # Collect ordered (div, name, id) tuples
        ordered: list[tuple[int, str, str]] = []
        for div, names in DIVISION_MUS.items():
            for name in names:
                key = name.lower()
                mu_id = db_id_map.get(key) or json_id_map.get(key)
                if mu_id:
                    ordered.append((div, name, mu_id))
                else:
                    logger.warning("_get_division_mu_data: no ID for %r", name)

        if not ordered:
            return []

        client = await self._get_client()
        inputs = [{"muId": mu_id} for _, _, mu_id in ordered]
        try:
            results = await client.batch_get("/mu.getById", inputs)
        except Exception as exc:
            logger.error("_get_division_mu_data: batch_get failed: %s", exc)
            return []

        out: list[tuple[int, str, dict]] = []
        for (div, name, _mu_id), raw in zip(ordered, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                out.append((div, name, data))
        return out

    # ------------------------------------------------------------------ #
    # /muplek
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="muplek",
        description="Laat zien hoeveel plekken er vrij zijn in de Nederlandse MU's.",
    )
    async def muplek(self, interaction: discord.Interaction) -> None:
        """Show free spots in Dutch division MUs, grouped by division."""
        await interaction.response.defer()

        div_mu_data = await self._get_division_mu_data()
        if not div_mu_data:
            await self._send_api_offline(
                interaction,
                "Kon geen Nederlandse MU's ophalen. De API is mogelijk tijdelijk niet beschikbaar.",
            )
            return

        # Row: (div, name, members, capacity, free)
        rows: list[tuple[int, str, int, int, int]] = []
        for div, name, mu in div_mu_data:
            members = len(mu.get("members", []))
            dorm_lvl = (mu.get("activeUpgradeLevels") or {}).get("dormitories", 1)
            capacity = DORM_CAPACITY.get(dorm_lvl, dorm_lvl * 5)
            free = max(0, capacity - members)
            rows.append((div, name, members, capacity, free))

        total_free = sum(r[4] for r in rows)
        total_members = sum(r[2] for r in rows)
        total_capacity = sum(r[3] for r in rows)

        # Sort: by division, then most free spots first, then name
        rows.sort(key=lambda r: (r[0], -r[4], r[1].lower()))

        col1 = min(max((len(r[1]) for r in rows), default=4), 22)
        col1 = max(col1, len("MU"))
        _row_suffix = "  Leden  Max  Vrij"
        separator = "─" * (col1 + len(_row_suffix))

        DIV_LABELS = {
            1: "🟡 Divisie 1",
            2: "🔵 Divisie 2",
            3: "🟢 Divisie 3",
            4: "🔴 Divisie 4",
            5: "🟣 Divisie 5",
        }

        lines: list[str] = []
        prev_div: int | None = None
        for div, name, members, capacity, free in rows:
            if div != prev_div:
                if lines:
                    lines.append("")
                label = DIV_LABELS.get(div, f"Divisie {div}")
                lines.append(f"{label + ' MU':<{col1}}{_row_suffix}")
                lines.append(separator)
                prev_div = div
            free_str = f"+{free}" if free > 0 else " 0"
            lines.append(
                f"{name[:col1]:<{col1}}  {members:>5}  {capacity:>3}  {free_str:>4}"
            )

        lines.append("")
        lines.append(separator)
        lines.append(
            f"{'TOTAAL':<{col1}}  {total_members:>5}  {total_capacity:>3}  +{total_free:>3}"
        )
        table = "\n".join(lines)

        color = int(self.config.get("colors", {}).get("primary", "0x154273"), 16)
        embed = discord.Embed(
            title="🪖 Nederlandse MU's – Beschikbare plekken",
            description=f"**Totaal vrij: {total_free} plek{'ken' if total_free != 1 else ''}**\n\n```\n{table}\n```",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(
            text=f"{len(rows)} MU's • Capaciteit gebaseerd op kazernesniveau"
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # /mu_inactiviteit
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="mu_inactiviteit",
        description=f"Laat inactieve leden zien in Nederlandse MU's (geen login in {INACTIVITY_HOURS}u).",
    )
    async def mu_inactiviteit(self, interaction: discord.Interaction) -> None:
        """Show inactive members in Dutch MUs (no login in the last 72 hours)."""
        await interaction.response.defer()

        mus = await self._get_all_dutch_mus()
        if not mus:
            await self._send_api_offline(
                interaction,
                "Kon geen Nederlandse MU's ophalen. De API is mogelijk tijdelijk niet beschikbaar.",
            )
            return

        # Build member→MU name map
        member_to_mu: dict[str, str] = {}
        for mu in mus:
            mu_name = mu.get("name", "?")
            for uid in mu.get("members", []):
                member_to_mu[uid] = mu_name

        all_member_ids = list(member_to_mu.keys())
        if not all_member_ids:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen leden gevonden in Nederlandse MU's.",
                    color=discord.Color.orange(),
                )
            )
            return

        client = await self._get_client()
        inputs = [{"userId": uid} for uid in all_member_ids]
        results = await client.batch_get(
            "/user.getUserLite",
            inputs,
            batch_size=30,
            chunk_sleep=0.0,
        )

        now = datetime.now(timezone.utc)
        inactive: list[
            tuple[float, str, str, str]
        ] = []  # (hours_ago, uid, name, mu_name)

        for uid, obj in zip(all_member_ids, results):
            last_conn = _last_connection(obj)
            if last_conn is None:
                # No login info → treat as very inactive (unknown)
                inactive.append((float("inf"), uid, _username(obj), member_to_mu[uid]))
                continue
            try:
                ts = datetime.fromisoformat(last_conn.replace("Z", "+00:00"))
                hours_ago = (now - ts).total_seconds() / 3600
            except (ValueError, TypeError):
                inactive.append((float("inf"), uid, _username(obj), member_to_mu[uid]))
                continue
            if hours_ago >= INACTIVITY_HOURS:
                inactive.append((hours_ago, uid, _username(obj), member_to_mu[uid]))

        color = int(self.config.get("colors", {}).get("primary", "0x154273"), 16)

        if not inactive:
            embed = discord.Embed(
                title="✅ Geen inactieve leden",
                description=(
                    f"Alle leden van Nederlandse MU's zijn ingelogd in de afgelopen "
                    f"{INACTIVITY_HOURS} uur."
                ),
                color=discord.Color.green(),
                timestamp=now,
            )
            await interaction.followup.send(embed=embed)
            return

        # Sort: longest inactive first (inf last)
        inactive.sort(
            key=lambda x: (x[0] != float("inf"), -x[0] if x[0] != float("inf") else 0)
        )

        # Build table
        col_name = max(len(r[2]) for r in inactive)
        col_name = max(col_name, len("Speler"))
        col_mu = max(len(r[3]) for r in inactive)
        col_mu = max(col_mu, len("MU"))
        header = f"{'Speler':<{col_name}}  {'MU':<{col_mu}}  Inactief"
        separator = "-" * (col_name + col_mu + 14)
        lines = [header, separator]
        for hours, uid, name, mu_name in inactive:
            dur = "onbekend" if hours == float("inf") else _fmt_duration(hours)
            lines.append(f"{name:<{col_name}}  {mu_name:<{col_mu}}  {dur}")
        table = "\n".join(lines)

        embed = discord.Embed(
            title="💤 Inactieve leden – Nederlandse MU's",
            description=(
                f"**{len(inactive)} leden** hebben meer dan **{INACTIVITY_HOURS} uur** niet ingelogd.\n\n"
                f"```\n{table}\n```"
            ),
            color=color,
            timestamp=now,
        )
        embed.set_footer(
            text=f"{len(all_member_ids)} leden gecontroleerd in {len(mus)} MU's"
        )
        await interaction.followup.send(embed=embed)

    async def _get_db(self):
        """Return the shared Database instance (from poller), or create one lazily."""
        if self._db is None:
            # Prefer the already-open connection held by ProductionChecker to avoid
            # two separate SQLite connections that would conflict on writes.
            shared = getattr(self.bot, "_ext_db", None)
            if shared is not None:
                self._db = shared
            else:
                from services.db import Database

                db_path = self.config.get("external_db_path", "database/external.db")
                self._db = Database(db_path)
                await self._db.setup()
        return self._db

    async def _eco_mu_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete MU names from the known_mus DB (all NL MUs)."""
        nl_country_id = self.config.get("nl_country_id", "")
        # "Geen MU" is a special sentinel — empty string value means mu_name IS NULL
        geen_mu = app_commands.Choice(name="Geen MU", value="")
        choices: list[app_commands.Choice[str]] = (
            [geen_mu] if not current or "geen" in current.lower() else []
        )
        try:
            db = await self._get_db()
            mus = await db.get_all_known_mu_ids(nl_country_id or None)
            for _mu_id, mu_name, _cid in mus:
                if not mu_name:
                    continue
                if current.lower() in mu_name.lower():
                    # value = mu_name so the command receives the name directly
                    choices.append(app_commands.Choice(name=mu_name, value=mu_name))
        except Exception as exc:
            logger.warning("_eco_mu_autocomplete: DB error: %s", exc)
        return choices[:25]

    @app_commands.command(
        name="eco_donaties",
        description="Laat eco-donaties per MU en donateur zien.",
    )
    @app_commands.describe(
        hours="Aantal uur terug om te controleren (standaard: 24, wordt genegeerd als datum is ingevuld)",
        datum="Specifieke startdatum (DD-MM-YYYY), haalt donaties op vanaf deze datum",
        mu="Optioneel: specificeer een MU naam om alleen die MU te controleren",
    )
    @app_commands.autocomplete(mu=_eco_mu_autocomplete)
    async def eco_donations(
        self,
        interaction: discord.Interaction,
        hours: int = 24,
        datum: Optional[str] = None,
        mu: Optional[str] = None,
    ) -> None:
        """Show eco donations in the last specified hours or since a specific date."""
        await interaction.response.defer()

        nl_country_id = self.config.get("nl_country_id", "")
        if not nl_country_id:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="NL country ID niet geconfigureerd.",
                    color=discord.Color.red(),
                )
            )
            return

        # ── Parse time window ────────────────────────────────────────────────
        now = datetime.now(timezone.utc)
        if datum is not None:
            parsed_date = None
            for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
                try:
                    parsed_date = datetime.strptime(datum, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if parsed_date is None:
                await interaction.followup.send(
                    embed=discord.Embed(
                        description="Ongeldig datumformaat. Gebruik DD-MM-YYYY (bijv. 01-05-2026).",
                        color=discord.Color.red(),
                    )
                )
                return
            if parsed_date > now:
                await interaction.followup.send(
                    embed=discord.Embed(
                        description="De startdatum kan niet in de toekomst liggen.",
                        color=discord.Color.red(),
                    )
                )
                return
            cutoff_time = parsed_date
            period_label = f"Vanaf {parsed_date.strftime('%d-%m-%Y')}"
        else:
            cutoff_time = now - timedelta(hours=hours)
            period_label = f"Laatste {hours} uur"

        cutoff_iso = cutoff_time.isoformat()

        # ── Query DB ─────────────────────────────────────────────────────────
        db = await self._get_db()

        latest_at = await db.get_latest_eco_donation_at()
        if latest_at is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=(
                        "Eco-donatie data wordt voor het eerst geladen. "
                        "Probeer over enkele minuten opnieuw."
                    ),
                    color=discord.Color.orange(),
                )
            )
            return

        if mu is not None:
            # Specific MU selected — mu value is the MU name, or "" for Geen MU
            # Pass mu directly; the DB mixin treats "" as mu_name IS NULL
            player_rows = await db.get_eco_donation_player_totals(
                cutoff_iso, mu_name=mu
            )
        else:
            mu_rows = await db.get_eco_donation_mu_totals(cutoff_iso)
            player_rows = await db.get_eco_donation_player_totals(cutoff_iso)

        # ── Build embed ───────────────────────────────────────────────────────
        def _format_leaderboard(leaderboard_rows: list[tuple[str, float]]) -> str:
            medals = ["🥇", "🥈", "🥉"]
            lines = ["──────────────────────────────"]
            for i, (name, amount) in enumerate(leaderboard_rows):
                prefix = medals[i] if i < 3 else f"`{i + 1}.`"
                lines.append(f"{prefix} **{name}** — €{amount:,.0f}")
            return "\n".join(lines) if len(lines) > 1 else "_Geen donaties_"

        color = int(self.config.get("colors", {}).get("primary", "0x154273"), 16)

        if mu is not None:
            if not player_rows:
                await interaction.followup.send(
                    embed=discord.Embed(
                        description=f"Geen donaties gevonden voor **{'Geen MU' if mu == '' else mu}** ({period_label.lower()}).",
                        color=discord.Color.orange(),
                        timestamp=now,
                    )
                )
                return

            rows_sorted = [(name, total) for _, name, total in player_rows][:25]
            total_donations = sum(r[1] for r in rows_sorted)

            col_name = max((len(r[0]) for r in rows_sorted), default=4)
            col_name = max(min(col_name, 24), len("User"))
            header = f"{'User':<{col_name}}  Donaties"
            separator = "-" * (col_name + 20)
            lines = [header, separator]
            for name, amount in rows_sorted:
                lines.append(f"{name:<{col_name}}  €{amount:,.0f}")
            lines.append(separator)
            lines.append(f"{'TOTAAL':<{col_name}}  €{total_donations:,.0f}")
            table = "\n".join(lines)

            mu_display = "Geen MU" if mu == "" else mu
            embed = discord.Embed(
                title=f"💰 Eco-donaties {mu_display} – {period_label}",
                description=f"**Totaal: €{total_donations:,.0f}**\n\n```\n{table}\n```",
                color=color,
                timestamp=now,
            )
            embed.set_footer(text=f"Top {len(rows_sorted)} donateurs")

        else:
            if not player_rows and not mu_rows:
                await interaction.followup.send(
                    embed=discord.Embed(
                        description=f"Geen donaties gevonden ({period_label.lower()}).",
                        color=discord.Color.orange(),
                        timestamp=now,
                    )
                )
                return

            total_donations = sum(r[2] for r in player_rows)

            mu_row_data: list[tuple[str, float]] = [(n, t) for n, t in mu_rows[:10]]
            player_row_data: list[tuple[str, float]] = [
                (display_name, total) for _, display_name, total in player_rows[:10]
            ]

            embed = discord.Embed(
                title=f"💰 Eco-donaties – {period_label}",
                description=f"**Totaal: €{total_donations:,.0f}**",
                color=color,
                timestamp=now,
            )
            embed.add_field(
                name="🏆 Top 10 MU's",
                value=_format_leaderboard(mu_row_data),
                inline=False,
            )
            embed.add_field(name="\u200b", value="\u200b", inline=False)
            embed.add_field(
                name="👤 Top 10 Donateurs",
                value=_format_leaderboard(player_row_data),
                inline=False,
            )
            n_mus = len(mu_rows)
            n_donors = len(player_rows)
            embed.set_footer(text=f"{n_mus} MU's • {n_donors} donateurs")

        await interaction.followup.send(embed=embed)


async def setup(bot: DiscordBot) -> None:
    """Add the MU cog to the bot."""
    await bot.add_cog(MU(bot))
