"""Slash command /productie — item breakdown by who OWNS the companies.

Answers "what does country/alliance Y's *players* produce". Scope is one of:

    (none)       every company in the game
    land:X       companies owned by citizens of country X
    alliantie:X  companies owned by citizens of any member country of alliance X

This is deliberately keyed on the owner's citizenship (``citizen_levels
.country_id``, via ``company_owners.owner_id``), not on which country
currently controls the region a company physically sits in. Those are very
different numbers: a Dutch player's steel mill is usually built wherever the
production bonus is best that week — rarely in Dutch-controlled territory —
so a region-control count for "land:Netherlands" undercounts by roughly 20x
versus what Dutch players actually own. ``/fabrieken``'s ``land:`` argument
answers the region-control question on purpose (it exists for verhuisadvies:
"how many factories are sitting in NL territory right now"); this command
answers a different question and needs a different join.

Data sources, all filled in by the same hourly sweep and fetched specially for
neither command:
    company_owners       — per-owner company/worker counts (see /fabrieken)
    citizen_levels        — the owner's citizenship (the existing citizen sweep)
    alliance_countries    — alliance membership (see /damage-projection)

Caveat (mostly closed): a company's owner can be absent from ``citizen_levels``
because that table is built from ``user.getUsersByCountry``, which — confirmed
live against the API — silently omits inactive players (``isActive: false``)
from its results, even though such a player's profile and companies are still
fully live and fetchable by ID. ``services/full_fetcher.py``'s
``fetch_missing_owner_citizenships`` now closes this specific gap: after each
census sweep, any ``company_owners.owner_id`` still missing from
``citizen_levels`` is fetched directly by ID (bypassing the broken listing
endpoint entirely) and backfilled. A brand-new owner can still be transiently
missing for a single sweep before that backfill runs; the command reports the
current game-wide unattributed share (see ``_global_unattributed_pct``)
whenever it's non-negligible, rather than silently under-counting.

No 7-day trend column here (unlike /fabrieken): ``company_owners`` keeps only
the latest sweep — see its docstring for why — so there is no history to diff
against for an ownership-based breakdown.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from nigeria_bot.fabrieken import (
    EXTERNAL_DB_PATH,
    _connect_ro,
    _fmt_int,
    _item_label,
    _table,
)

logger = logging.getLogger("nigeria_bot.productie")

_AUTOCOMPLETE_LIMIT = 25


class ProductieCog(commands.Cog, name="productie"):
    """Cog for the /productie command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── low-level reads ──────────────────────────────────────────────────────

    async def _owners_available(self, conn) -> bool:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('company_owners', 'company_census_runs')"
        ) as cur:
            return len(await cur.fetchall()) == 2

    async def _latest_run(self, conn) -> tuple[str, int, int] | None:
        """``(captured_at, listed, checked)`` — only used for the freshness footer."""
        async with conn.execute(
            "SELECT captured_at, listed_companies, checked_companies "
            "FROM company_census_runs ORDER BY captured_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return (str(row[0]), int(row[1] or 0), int(row[2] or 0)) if row else None

    async def _item_breakdown_by_owner_country(
        self, conn, country_ids: list[str] | None, staffed: bool = False
    ) -> list[tuple[str, int, int]]:
        """``(item_code, companies, workers)``, grouped by item, by owner citizenship.

        *country_ids* of ``None`` means no nationality filter at all — every
        company in the game, including ones whose owner's nationality isn't
        known. A non-None list requires the owner to be a citizen of one of
        those countries, via an inner join, so unattributed owners drop out.
        """
        col = "staffed_count" if staffed else "company_count"
        if country_ids is None:
            async with conn.execute(
                f"SELECT item_code, SUM({col}), SUM(worker_count) FROM company_owners "
                f"GROUP BY item_code HAVING SUM({col}) > 0 ORDER BY 2 DESC"
            ) as cur:
                return [
                    (str(r[0]), int(r[1] or 0), int(r[2] or 0))
                    for r in await cur.fetchall()
                ]
        placeholders = ",".join("?" * len(country_ids))
        async with conn.execute(
            f"SELECT co.item_code, SUM(co.{col}), SUM(co.worker_count) "
            "FROM company_owners co "
            "JOIN citizen_levels cl ON cl.user_id = co.owner_id "
            f"WHERE cl.country_id IN ({placeholders}) "
            f"GROUP BY co.item_code HAVING SUM(co.{col}) > 0 ORDER BY 2 DESC",
            country_ids,
        ) as cur:
            return [
                (str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in await cur.fetchall()
            ]

    async def _global_unattributed_pct(self, conn) -> float:
        """Share of ALL companies whose owner has no citizen_levels row.

        There is no way to size this *per scope*: an owner missing a
        citizenship match is, by definition, missing precisely the piece of
        data that would say which country's or alliance's number they should
        have counted toward. A region-control proxy was tried and discarded —
        it measures the wrong thing, the same mismatch this command exists to
        fix (see module docstring). So this is reported once, game-wide, as
        context for every scoped view rather than a fabricated per-scope
        estimate.
        """
        async with conn.execute(
            "SELECT "
            "  SUM(CASE WHEN cl.user_id IS NOT NULL THEN co.company_count ELSE 0 END), "
            "  SUM(CASE WHEN cl.user_id IS NULL THEN co.company_count ELSE 0 END) "
            "FROM company_owners co "
            "LEFT JOIN citizen_levels cl ON cl.user_id = co.owner_id"
        ) as cur:
            row = await cur.fetchone()
        attributed, unattributed = (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)
        total = attributed + unattributed
        return 100.0 * unattributed / total if total else 0.0

    async def _country_names(self, conn) -> dict[str, str]:
        async with conn.execute(
            "SELECT country_id, name FROM country_snapshots "
            "WHERE name IS NOT NULL AND name != ''"
        ) as cur:
            return {str(r[0]): str(r[1]) for r in await cur.fetchall()}

    async def _resolve_country(self, conn, raw: str) -> tuple[str, str] | None:
        needle = raw.strip().lower()
        if not needle:
            return None
        async with conn.execute(
            "SELECT country_id, name, code FROM country_snapshots"
        ) as cur:
            rows = await cur.fetchall()
        for cid, name, code in rows:
            if str(cid).lower() == needle:
                return str(cid), str(name or cid)
        for cid, name, code in rows:
            if str(name or "").lower() == needle or str(code or "").lower() == needle:
                return str(cid), str(name or cid)
        return None

    async def _resolve_alliance(
        self, conn, raw: str
    ) -> tuple[str, str, list[str]] | None:
        """Map user input to ``(alliance_id, alliance_name, [country_id, ...])``."""
        needle = raw.strip().lower()
        if not needle:
            return None
        async with conn.execute(
            "SELECT alliance_id, alliance_name, country_id FROM alliance_countries"
        ) as cur:
            rows = await cur.fetchall()
        grouped: dict[str, tuple[str, list[str]]] = {}
        for aid, name, cid in rows:
            aid = str(aid)
            grouped.setdefault(aid, (str(name), []))[1].append(str(cid))
        for aid, (name, cids) in grouped.items():
            if aid.lower() == needle or name.lower() == needle:
                return aid, name, cids
        return None

    # ── Autocomplete ─────────────────────────────────────────────────────────

    async def _country_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            async with _connect_ro() as conn:
                names = await self._country_names(conn)
        except Exception:
            logger.debug("productie: country autocomplete failed", exc_info=True)
            return []
        needle = (current or "").strip().lower()
        return [
            app_commands.Choice(name=name, value=cid)
            for cid, name in sorted(names.items(), key=lambda kv: kv[1])
            if needle in name.lower()
        ][:_AUTOCOMPLETE_LIMIT]

    async def _alliance_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            async with _connect_ro() as conn:
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='alliance_countries'"
                ) as cur:
                    if await cur.fetchone() is None:
                        return []
                async with conn.execute(
                    "SELECT DISTINCT alliance_id, alliance_name FROM alliance_countries"
                ) as cur:
                    rows = await cur.fetchall()
        except Exception:
            logger.debug("productie: alliance autocomplete failed", exc_info=True)
            return []
        needle = (current or "").strip().lower()
        return [
            app_commands.Choice(name=str(name), value=str(aid))
            for aid, name in sorted(rows, key=lambda r: r[1])
            if needle in str(name).lower()
        ][:_AUTOCOMPLETE_LIMIT]

    # ── Command ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="productie",
        description="Toon welke producten spelers van een land, alliantie of wereldwijd maken.",
    )
    @app_commands.describe(
        land="Land — telt alle bedrijven van spelers met deze nationaliteit. Laat leeg voor wereldwijd.",
        alliantie="Alliantie — telt alle lidlanden samen. Laat leeg voor wereldwijd (of gebruik land).",
        met_werknemers="Tel alleen bedrijven die minstens één werknemer hebben.",
    )
    @app_commands.autocomplete(land=_country_choices, alliantie=_alliance_choices)
    async def productie(
        self,
        interaction: discord.Interaction,
        land: str | None = None,
        alliantie: str | None = None,
        met_werknemers: bool = False,
    ) -> None:
        await interaction.response.defer()
        try:
            embed = await self._build(land, alliantie, met_werknemers)
        except Exception:
            logger.exception("productie: failed to build response")
            embed = discord.Embed(
                title="📦 Productie",
                description=(
                    "⚠️ Kon de bedrijvendatabase niet uitlezen. Probeer het later opnieuw."
                ),
                colour=discord.Colour.red(),
            )
        await interaction.followup.send(embed=embed)

    def _no_scan_embed(self) -> discord.Embed:
        return discord.Embed(
            title="📦 Productie",
            description=(
                "⏳ Er is nog geen scan uitgevoerd. De bedrijvenscan draait elk "
                "uur — probeer het over een uur opnieuw."
            ),
            colour=discord.Colour.orange(),
        )

    def _unknown_embed(self, what: str, raw: str) -> discord.Embed:
        return discord.Embed(
            title="📦 Productie",
            description=f"❌ `{raw}` is geen bekend {what}. Kies er een uit de suggestielijst.",
            colour=discord.Colour.red(),
        )

    async def _build(
        self, land: str | None, alliantie: str | None, staffed: bool
    ) -> discord.Embed:
        if land and alliantie:
            return discord.Embed(
                title="📦 Productie",
                description="❌ Geef **land** of **alliantie**, niet allebei.",
                colour=discord.Colour.red(),
            )

        if not os.path.isfile(EXTERNAL_DB_PATH):
            return self._no_scan_embed()

        async with _connect_ro() as conn:
            if not await self._owners_available(conn):
                return self._no_scan_embed()

            country_ids: list[str] | None = None
            title = "📦 Productie — wereldwijd"
            scope_note = ""

            if land:
                country = await self._resolve_country(conn, land)
                if country is None:
                    return self._unknown_embed("land", land)
                country_ids = [country[0]]
                title = f"📦 Productie — spelers uit {country[1]}"
            elif alliantie:
                alliance = await self._resolve_alliance(conn, alliantie)
                if alliance is None:
                    return self._unknown_embed("alliantie", alliantie)
                _aid, alliance_name, cids = alliance
                if not cids:
                    return discord.Embed(
                        title=f"📦 Productie — {alliance_name}",
                        description="Deze alliantie heeft geen lidlanden.",
                        colour=discord.Colour.orange(),
                    )
                country_ids = cids
                title = f"📦 Productie — spelers uit {alliance_name}"
                scope_note = f"*{len(cids)} lidlanden samengeteld.*\n"

            run = await self._latest_run(conn)
            if run is None:
                return self._no_scan_embed()
            at, listed, checked = run

            rows = await self._item_breakdown_by_owner_country(conn, country_ids, staffed)
            suffix = " met werknemers" if staffed else ""
            if not rows:
                return discord.Embed(
                    title=title,
                    description=f"Geen bedrijven{suffix} gevonden voor deze scope.",
                    colour=discord.Colour.orange(),
                )

            table = _table(
                ("Product", "Bedrijven", "Werknemers"), (20, 9, 10),
                [(_item_label(i), _fmt_int(c), _fmt_int(w)) for i, c, w in rows],
            )

            total_c = sum(c for _, c, _ in rows)
            total_w = sum(w for _, _, w in rows)
            head = (
                scope_note
                + f"**{_fmt_int(total_c)}** bedrijven{suffix} in **{len(rows)}** "
                f"productsoorten, samen **{_fmt_int(total_w)}** werknemers."
            )

            # Coverage caveat, scoped views only — see module docstring for why
            # this is a flat game-wide figure rather than a per-scope estimate.
            if country_ids:
                pct = await self._global_unattributed_pct(conn)
                if pct >= 1.0:
                    head += (
                        f"\n*Let op: dit telt alleen bedrijven waarvan de eigenaar "
                        f"herkend is als speler met deze nationaliteit. Wereldwijd kon "
                        f"~{pct:.0f}% van alle bedrijven niet aan een nationaliteit "
                        f"gekoppeld worden — een deel daarvan hoort mogelijk hierbij "
                        f"en ontbreekt dus.*"
                    )

            embed = discord.Embed(
                title=title,
                description=head + "\n" + table,
                colour=discord.Colour.green(),
            )

        footer = f"{_fmt_int(checked)} bedrijven gecontroleerd"
        if listed and listed != checked:
            footer += f" van {_fmt_int(listed)} gevonden"
        try:
            footer += f" · laatste scan {datetime.fromisoformat(at):%d-%m-%Y %H:%M} UTC"
        except (ValueError, TypeError):
            pass
        embed.set_footer(text=footer)
        return embed


async def setup(bot: commands.Bot) -> ProductieCog:
    cog = ProductieCog(bot)
    await bot.add_cog(cog)
    return cog
