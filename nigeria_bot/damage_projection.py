"""Slash command /damage-projection — alliance war-readiness overview.

Reads data written by the hourly full_fetcher sweep into
``database/external.db``. The command itself makes no WarEra API calls.

Two views:
    /damage-projection                — every alliance, most players first
    /damage-projection alliantie:X    — that alliance broken down per country

For each scope, five numbers: total players, players "in war" (their skill
points lean combat rather than economy — the same classification
``/paraatheid`` on the main bot already uses, via ``citizen_levels.skill_mode``,
so a player counted "paraat" there is counted as a war player here), total
health and hunger held by those war players, and how many of them are
currently buffed (on a cocain pill), in the ~15.5h debuff that follows a
buff ending, or neither.

Data sources, all filled in by the same hourly sweep and none of them fetched
specially for this command:
    alliance_countries      — alliance.getManyPaginated, once per sweep
    citizen_levels           — the existing hourly citizen sweep (all countries)
    citizen_combat_state     — health/hunger, captured as a side effect of that
                                same sweep (see services/citizen_cache.py)
    citizen_pill_tracking    — buff/debuff timing, likewise already collected

Health and hunger totals are scoped to *war* players only: an eco player's
health doesn't contribute to what an alliance can fight with, which is the
point of a damage projection.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("nigeria_bot.damage_projection")

EXTERNAL_DB_PATH = os.getenv("RW_EXTERNAL_DB_PATH", "database/external.db")

_AUTOCOMPLETE_LIMIT = 25
_DEBUFF_DURATION = 15.5 * 3600  # kept in sync with services/db/pill_tracking.py


def _fmt_int(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def _fmt_hm(secs: float) -> str:
    """Seconds as 'Xu Ym' (matches /paraatheid's convention)."""
    total_m = max(0, int(secs)) // 60
    h, m = divmod(total_m, 60)
    return f"{h}u{m:02d}m"


def _connect_ro():
    return aiosqlite.connect(f"file:{EXTERNAL_DB_PATH}?mode=ro", uri=True)


def _empty_stats() -> dict:
    return {
        "total_players": 0, "war_players": 0, "war_health": 0.0, "war_hunger": 0.0,
        "buff_count": 0, "buff_secs_sum": 0.0,
        "debuff_count": 0, "debuff_secs_sum": 0.0,
        "neither_count": 0,
    }


def _add_stats(dst: dict, src: dict) -> None:
    for k in dst:
        dst[k] += src.get(k, 0)


def _render_row(label: str, label_w: int, stats: dict) -> str:
    """Two-line block: totals, then the buff/debuff/neither breakdown."""
    buff_avg = (
        f" (gem {_fmt_hm(stats['buff_secs_sum'] / stats['buff_count'])})"
        if stats["buff_count"] else ""
    )
    debuff_avg = (
        f" (gem {_fmt_hm(stats['debuff_secs_sum'] / stats['debuff_count'])})"
        if stats["debuff_count"] else ""
    )
    line1 = (
        f"{label[:label_w]:<{label_w}}  {_fmt_int(stats['total_players']):>5} spelers  "
        f"{_fmt_int(stats['war_players']):>5} oorlog  "
        f"HP {_fmt_int(stats['war_health']):>7}  "
        f"Honger {_fmt_int(stats['war_hunger']):>5}"
    )
    line2 = (
        f"{'':<{label_w}}  💊 Buff: {stats['buff_count']}{buff_avg}  "
        f"🤢 Debuff: {stats['debuff_count']}{debuff_avg}  "
        f"⬜ Geen: {stats['neither_count']}"
    )
    return line1 + "\n" + line2


class DamageProjectionCog(commands.Cog, name="damage_projection"):
    """Cog for the /damage-projection command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── DB reads ─────────────────────────────────────────────────────────────

    async def _tables_available(self, conn) -> bool:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('alliance_countries', 'citizen_combat_state')"
        ) as cur:
            return len(await cur.fetchall()) == 2

    async def _get_alliances(self, conn) -> list[tuple[str, str, list[str]]]:
        async with conn.execute(
            "SELECT alliance_id, alliance_name, country_id FROM alliance_countries"
        ) as cur:
            rows = await cur.fetchall()
        grouped: dict[str, tuple[str, list[str]]] = {}
        for aid, name, cid in rows:
            aid = str(aid)
            if aid not in grouped:
                grouped[aid] = (str(name), [])
            grouped[aid][1].append(str(cid))
        return [(aid, name, cids) for aid, (name, cids) in grouped.items()]

    async def _get_stats_by_country(
        self, conn, country_ids: list[str]
    ) -> dict[str, dict]:
        if not country_ids:
            return {}
        placeholders = ",".join("?" * len(country_ids))
        now = int(time.time())
        stats: dict[str, dict] = {cid: _empty_stats() for cid in country_ids}
        async with conn.execute(
            "SELECT cl.country_id, cl.skill_mode, cs.health_cur, cs.hunger_cur, "
            "       pt.buff_expires_at "
            "FROM citizen_levels cl "
            "LEFT JOIN citizen_combat_state cs ON cs.user_id = cl.user_id "
            "LEFT JOIN citizen_pill_tracking pt ON pt.user_id = cl.user_id "
            f"WHERE cl.country_id IN ({placeholders})",
            country_ids,
        ) as cur:
            async for country_id, skill_mode, health_cur, hunger_cur, expires_at in cur:
                bucket = stats.get(str(country_id))
                if bucket is None:
                    continue
                bucket["total_players"] += 1
                if skill_mode != "war":
                    continue
                bucket["war_players"] += 1
                bucket["war_health"] += float(health_cur or 0.0)
                bucket["war_hunger"] += float(hunger_cur or 0.0)
                if expires_at is None:
                    bucket["neither_count"] += 1
                elif expires_at > now:
                    bucket["buff_count"] += 1
                    bucket["buff_secs_sum"] += float(expires_at - now)
                elif expires_at + _DEBUFF_DURATION > now:
                    bucket["debuff_count"] += 1
                    bucket["debuff_secs_sum"] += float(expires_at + _DEBUFF_DURATION - now)
                else:
                    bucket["neither_count"] += 1
        return stats

    async def _country_names(self, conn) -> dict[str, str]:
        async with conn.execute(
            "SELECT country_id, name FROM country_snapshots "
            "WHERE name IS NOT NULL AND name != ''"
        ) as cur:
            return {str(r[0]): str(r[1]) for r in await cur.fetchall()}

    async def _latest_citizen_sweep(self, conn) -> str | None:
        async with conn.execute(
            "SELECT last_finished_at FROM data_freshness "
            "WHERE dataset = 'all_countries.citizens'"
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] else None

    # ── Autocomplete ─────────────────────────────────────────────────────────

    async def _alliance_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            async with _connect_ro() as conn:
                if not await self._tables_available(conn):
                    return []
                async with conn.execute(
                    "SELECT DISTINCT alliance_id, alliance_name FROM alliance_countries"
                ) as cur:
                    rows = await cur.fetchall()
        except Exception:
            logger.debug("damage-projection: alliance autocomplete failed", exc_info=True)
            return []
        needle = (current or "").strip().lower()
        return [
            app_commands.Choice(name=str(name), value=str(aid))
            for aid, name in sorted(rows, key=lambda r: r[1])
            if needle in str(name).lower()
        ][:_AUTOCOMPLETE_LIMIT]

    # ── Command ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="damage-projection",
        description="Oorlogscapaciteit per alliantie: spelers, HP, honger, buff/debuff.",
    )
    @app_commands.describe(
        alliantie="Alliantie om per land uit te splitsen. Laat leeg voor alle allianties."
    )
    @app_commands.autocomplete(alliantie=_alliance_choices)
    async def damage_projection(
        self, interaction: discord.Interaction, alliantie: str | None = None
    ) -> None:
        await interaction.response.defer()
        try:
            embed = await self._build(alliantie)
        except Exception:
            logger.exception("damage-projection: failed to build response")
            embed = discord.Embed(
                title="⚔️ Damage projection",
                description=(
                    "⚠️ Kon de database niet uitlezen. Probeer het later opnieuw."
                ),
                colour=discord.Colour.red(),
            )
        await interaction.followup.send(embed=embed)

    async def _build(self, alliance_query: str | None) -> discord.Embed:
        if not os.path.isfile(EXTERNAL_DB_PATH):
            return self._no_data_embed()

        async with _connect_ro() as conn:
            if not await self._tables_available(conn):
                return self._no_data_embed()

            alliances = await self._get_alliances(conn)
            if not alliances:
                return discord.Embed(
                    title="⚔️ Damage projection",
                    description="Geen allianties gevonden.",
                    colour=discord.Colour.orange(),
                )

            all_country_ids = sorted({cid for _, _, cids in alliances for cid in cids})
            stats_by_country = await self._get_stats_by_country(conn, all_country_ids)
            names = await self._country_names(conn)
            swept_at = await self._latest_citizen_sweep(conn)

            if alliance_query:
                match = next(
                    (a for a in alliances if a[0] == alliance_query), None
                ) or next(
                    (a for a in alliances if a[1].lower() == alliance_query.strip().lower()),
                    None,
                )
                if match is None:
                    return discord.Embed(
                        title="⚔️ Damage projection",
                        description=(
                            f"❌ `{alliance_query}` is geen bekende alliantie. "
                            f"Kies er een uit de suggestielijst."
                        ),
                        colour=discord.Colour.red(),
                    )
                embed = self._render_alliance(match, stats_by_country, names)
            else:
                embed = self._render_overview(alliances, stats_by_country, names)

        if swept_at:
            try:
                embed.set_footer(
                    text=f"Laatste spelersscan: {datetime.fromisoformat(swept_at):%d-%m-%Y %H:%M} UTC"
                )
            except (ValueError, TypeError):
                pass
        return embed

    def _no_data_embed(self) -> discord.Embed:
        return discord.Embed(
            title="⚔️ Damage projection",
            description=(
                "⏳ Er is nog geen data. De uurlijkse scan moet eerst één keer "
                "gedraaid hebben — probeer het over een uur opnieuw."
            ),
            colour=discord.Colour.orange(),
        )

    def _render_overview(
        self,
        alliances: list[tuple[str, str, list[str]]],
        stats_by_country: dict[str, dict],
        names: dict[str, str],
    ) -> discord.Embed:
        rows: list[tuple[str, dict]] = []
        for _aid, name, cids in alliances:
            totals = _empty_stats()
            for cid in cids:
                _add_stats(totals, stats_by_country.get(cid, _empty_stats()))
            rows.append((name, totals))
        rows.sort(key=lambda r: -r[1]["total_players"])

        label_w = min(26, max((len(n) for n, _ in rows), default=10))
        blocks = [_render_row(name, label_w, s) for name, s in rows]
        description = "```\n" + "\n".join(blocks) + "\n```"

        return discord.Embed(
            title="⚔️ Damage projection — alle allianties",
            description=description,
            colour=discord.Colour.dark_red(),
        )

    def _render_alliance(
        self,
        alliance: tuple[str, str, list[str]],
        stats_by_country: dict[str, dict],
        names: dict[str, str],
    ) -> discord.Embed:
        _aid, alliance_name, cids = alliance
        rows: list[tuple[str, dict]] = []
        totals = _empty_stats()
        for cid in cids:
            s = stats_by_country.get(cid, _empty_stats())
            rows.append((names.get(cid, cid), s))
            _add_stats(totals, s)
        rows.sort(key=lambda r: -r[1]["total_players"])

        label_w = min(26, max((len(n) for n, _ in rows), default=10))
        blocks = [_render_row(name, label_w, s) for name, s in rows]
        blocks.append("─" * (label_w + 45))
        blocks.append(_render_row("Totaal", label_w, totals))
        description = "```\n" + "\n".join(blocks) + "\n```"

        return discord.Embed(
            title=f"⚔️ Damage projection — {alliance_name}",
            description=description,
            colour=discord.Colour.dark_red(),
        )


async def setup(bot: commands.Bot) -> DamageProjectionCog:
    cog = DamageProjectionCog(bot)
    await bot.add_cog(cog)
    return cog
