"""Slash command /fabrieken — company counts per item and country.

Reads the hourly company census written by :mod:`services.full_fetcher` into
``database/external.db`` (tables ``company_census`` / ``company_census_runs``).
The command makes no WarEra API calls at all: the sweep already visits every
company in the game once an hour and stores counts for *every* country × item
pair, so answering any of the four views below is one indexed SELECT.

Views, chosen by which arguments are supplied:

===============  ==========================================================
Arguments        Shows
===============  ==========================================================
item + land      Companies and workers for that one combination
item only        Every country producing that item, ranked
land only        Every item produced in that country, ranked
neither          Top 10 countries and top 10 items, by company count
eigenaren=True   Largest owners, scoped by whichever filters were given
===============  ==========================================================

Two modifiers apply on top:
  * ``alles=True`` prints the full owner list across several messages instead
    of the top 25.
  * ``met_werknemers=True`` counts only companies that have at least one
    worker, and drops owners who have none.  This needs the ``staffed_count``
    column, because "how many companies have a worker" cannot be derived from
    a total company count plus a total worker count.

Owner names are resolved from ``citizen_levels`` (also filled by the fetcher),
so the owner view stays API-free too.  An owner the citizen sweep has not
recorded is rendered as a profile link rather than dropped.

A company counts towards a country when the region it sits in is currently
controlled by that country.  That mapping is re-evaluated by every sweep, so a
region changing hands is reflected within the hour.

The 7-day delta is shown only once history reaches back that far; until then the
command says so rather than showing a meaningless "+108".
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("nigeria_bot.fabrieken")

# Written by the data-fetcher container; both containers mount ./database, so
# the Nigeria bot opens the same file read-only.
EXTERNAL_DB_PATH = os.getenv("RW_EXTERNAL_DB_PATH", "database/external.db")

_DELTA_DAYS = 7
_AUTOCOMPLETE_LIMIT = 25  # Discord's hard cap on choices
_MAX_LIST_ROWS = 25       # keeps an embed description well under 4096 chars
_TOP_N = 10               # rows per table in the no-argument overview

# Wage income-tax display, from the company_tax_revenue table.
_TAX_DAYS = 7          # per-day rows shown in the tax field
_TAX_MONTH_DAYS = 30   # window for the "last month" total
_TAX_STARTED_KEY = "wage_tax_started_at"

# `alles:True` pagination. An unfiltered owner query spans >16k players, which
# would be hundreds of messages, so the listing is capped and says so.
_MAX_ALL_ROWS = 1000
_MAX_ALL_PAGES = 10
_EMBED_DESC_LIMIT = 4096
_PAGE_BUDGET = _EMBED_DESC_LIMIT - 250  # headroom for header and truncation note

# Dutch display names for the item codes that companies actually produce.
# Anything missing falls back to the raw code, so a new game item degrades
# gracefully instead of breaking the command.
ITEM_LABELS_NL: dict[str, str] = {
    "ammo": "Munitie",
    "bread": "Brood",
    "coca": "Coca",
    "cocain": "Cocaïne",
    "concrete": "Beton",
    "cookedFish": "Gebakken vis",
    "fish": "Vis",
    "grain": "Graan",
    "heavyAmmo": "Zware munitie",
    "iron": "IJzer",
    "lead": "Lood",
    "lightAmmo": "Lichte munitie",
    "limestone": "Kalksteen",
    "livestock": "Vee",
    "oil": "Olie",
    "paper": "Papier",
    "petroleum": "Petroleum",
    "steak": "Biefstuk",
    "steel": "Staal",
    "wood": "Hout",
}


def _item_label(code: str) -> str:
    return ITEM_LABELS_NL.get(code, code)


def _fmt_int(n: int) -> str:
    """Thousands-separated with a narrow no-break space (matches other cogs)."""
    return f"{n:,}".replace(",", " ")


def _fmt_money(v: float) -> str:
    """Money with two decimals and the same narrow-space grouping as counts."""
    return f"{v:,.2f}".replace(",", "\u202f")


def _fmt_delta(now: int, then: int) -> str:
    """Render a signed change, e.g. ``+12``, ``-3``, ``0`` (unchanged)."""
    diff = now - then
    if diff > 0:
        return f"+{_fmt_int(diff)}"
    if diff < 0:
        return f"-{_fmt_int(abs(diff))}"
    return "0"


def _connect_ro():
    """Open the census DB read-only. Use as an async context manager."""
    return aiosqlite.connect(f"file:{EXTERNAL_DB_PATH}?mode=ro", uri=True)


def _table(headers: tuple[str, ...], widths: tuple[int, ...], rows: list[tuple]) -> str:
    """Render a fixed-width code-block table.

    First column is left-aligned (a label), the rest right-aligned (numbers).
    """
    def _line(cells: tuple, pad: str) -> str:
        out = []
        for i, (cell, w) in enumerate(zip(cells, widths)):
            text = str(cell)
            if len(text) > w:
                text = text[: w - 1] + "…"
            out.append(f"{text:{'<' if i == 0 else '>'}{w}}")
        return pad.join(out)

    header = _line(headers, "  ")
    sep = "─" * len(header)
    body = "\n".join(_line(r, "  ") for r in rows)
    return f"```\n{header}\n{sep}\n{body}\n```"


def _paginate(entries: list[str], first_page_reserve: int = 0) -> list[list[str]]:
    """Split *entries* into pages that each fit an embed description.

    Packs by measured length rather than a fixed row count, because a line's
    width varies a lot with the owner's name and the profile URL.
    """
    pages: list[list[str]] = []
    current: list[str] = []
    used = first_page_reserve
    for entry in entries:
        cost = len(entry) + 1  # + newline
        if current and used + cost > _PAGE_BUDGET:
            pages.append(current)
            current = []
            used = 0
        current.append(entry)
        used += cost
    if current:
        pages.append(current)
    return pages


class FabriekenCog(commands.Cog, name="fabrieken"):
    """Cog for the /fabrieken command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── low-level census reads ───────────────────────────────────────────────

    async def _census_available(self, conn) -> bool:
        """True when the data-fetcher has created its census tables."""
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('company_census', 'company_census_runs')"
        ) as cur:
            return len(await cur.fetchall()) == 2

    async def _latest_run(self, conn) -> tuple[str, int, int] | None:
        """``(captured_at, listed, checked)`` of the newest completed sweep."""
        async with conn.execute(
            "SELECT captured_at, listed_companies, checked_companies "
            "FROM company_census_runs ORDER BY captured_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return (str(row[0]), int(row[1] or 0), int(row[2] or 0)) if row else None

    async def _baseline_run(self, conn) -> str | None:
        """``captured_at`` of the newest sweep at or before the 7-day cutoff.

        Picking the newest run *at or before* the cutoff means a gap in coverage
        yields a slightly older baseline rather than no comparison at all.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_DELTA_DAYS)).isoformat()
        async with conn.execute(
            "SELECT captured_at FROM company_census_runs "
            "WHERE captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
            (cutoff,),
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def _pair(
        self, conn, at: str, country_id: str, item_code: str, staffed: bool = False
    ) -> tuple[int, int]:
        """``(companies, workers)`` for one country × item.

        With *staffed* the company figure counts only companies that have at
        least one worker.  An absent row means genuinely zero.
        """
        col = "staffed_count" if staffed else "company_count"
        async with conn.execute(
            f"SELECT {col}, worker_count FROM company_census "
            "WHERE captured_at = ? AND country_id = ? AND item_code = ?",
            (at, country_id, item_code),
        ) as cur:
            row = await cur.fetchone()
        return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

    async def _by_country(
        self, conn, at: str, item_code: str, staffed: bool = False
    ) -> list[tuple[str, int, int]]:
        """``(country_id, companies, workers)`` for one item, ranked."""
        col = "staffed_count" if staffed else "company_count"
        async with conn.execute(
            f"SELECT country_id, {col}, worker_count FROM company_census "
            f"WHERE captured_at = ? AND item_code = ? AND {col} > 0 "
            f"ORDER BY {col} DESC",
            (at, item_code),
        ) as cur:
            return [(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in await cur.fetchall()]

    async def _by_item(
        self, conn, at: str, country_id: str, staffed: bool = False
    ) -> list[tuple[str, int, int]]:
        """``(item_code, companies, workers)`` for one country, ranked."""
        col = "staffed_count" if staffed else "company_count"
        async with conn.execute(
            f"SELECT item_code, {col}, worker_count FROM company_census "
            f"WHERE captured_at = ? AND country_id = ? AND {col} > 0 "
            f"ORDER BY {col} DESC",
            (at, country_id),
        ) as cur:
            return [(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in await cur.fetchall()]

    async def _totals(
        self, conn, at: str, group: str, limit: int, staffed: bool = False
    ) -> list[tuple[str, int, int]]:
        """Top *limit* rows grouped by ``country_id`` or ``item_code``."""
        if group not in ("country_id", "item_code"):  # guard against SQL injection
            raise ValueError(group)
        col = "staffed_count" if staffed else "company_count"
        async with conn.execute(
            f"SELECT {group}, SUM({col}), SUM(worker_count) FROM company_census "
            "WHERE captured_at = ? GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
            (at, limit),
        ) as cur:
            return [(str(r[0]), int(r[1] or 0), int(r[2] or 0)) for r in await cur.fetchall()]

    async def _owner_rows(
        self, conn, country_id: str | None, item_code: str | None, limit: int,
        staffed: bool = False,
    ) -> list[tuple[str, int, int]]:
        """Top owners as ``(owner_id, companies, workers)``.

        Either filter may be None, in which case totals are summed across that
        dimension — an owner with factories in three countries is one row.
        ``company_owners`` holds only the newest sweep, so no timestamp filter
        is needed.

        With *staffed* the company figure counts only that owner's companies
        which have at least one worker, and owners with none are dropped.
        """
        col = "staffed_count" if staffed else "company_count"
        where, params = [], []
        if country_id:
            where.append("country_id = ?")
            params.append(country_id)
        if item_code:
            where.append("item_code = ?")
            params.append(item_code)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        having = "HAVING SUM(worker_count) > 0 " if staffed else ""
        params.append(limit)
        async with conn.execute(
            f"SELECT owner_id, SUM({col}), SUM(worker_count) "
            f"FROM company_owners {clause} "
            f"GROUP BY owner_id {having}ORDER BY 2 DESC, 3 DESC LIMIT ?",
            params,
        ) as cur:
            return [
                (str(r[0]), int(r[1] or 0), int(r[2] or 0))
                for r in await cur.fetchall()
            ]

    async def _owner_totals(
        self, conn, country_id: str | None, item_code: str | None,
        staffed: bool = False,
    ) -> tuple[int, int, int]:
        """``(distinct_owners, total_companies, total_workers)`` for the filter."""
        col = "staffed_count" if staffed else "company_count"
        where, params = [], []
        if country_id:
            where.append("country_id = ?")
            params.append(country_id)
        if item_code:
            where.append("item_code = ?")
            params.append(item_code)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        if staffed:
            # Count owners, not rows: an owner is "staffed" if any of their
            # companies in scope has a worker.
            where.append("worker_count > 0")
            clause = f"WHERE {' AND '.join(where)}"
        async with conn.execute(
            f"SELECT COUNT(DISTINCT owner_id), SUM({col}), SUM(worker_count) "
            f"FROM company_owners {clause}",
            params,
        ) as cur:
            row = await cur.fetchone()
        return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)) if row else (0, 0, 0)

    async def _owner_names(self, conn, owner_ids: list[str]) -> dict[str, str]:
        """``{user_id: name}`` from ``citizen_levels`` — no API call.

        Coverage is near-complete for the high-ranking owners these lists show
        (they are active players the citizen sweep records); occasional
        long-tail owners are missing and get rendered as a profile link instead.
        """
        if not owner_ids:
            return {}
        try:
            marks = ",".join("?" * len(owner_ids))
            async with conn.execute(
                f"SELECT user_id, citizen_name FROM citizen_levels "
                f"WHERE user_id IN ({marks}) "
                "AND citizen_name IS NOT NULL AND citizen_name != ''",
                owner_ids,
            ) as cur:
                return {str(r[0]): str(r[1]) for r in await cur.fetchall()}
        except Exception:
            logger.debug("fabrieken: owner name lookup failed", exc_info=True)
            return {}

    # ── tax revenue ──────────────────────────────────────────────────────────

    async def _tax_available(self, conn) -> bool:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='company_tax_revenue'"
        ) as cur:
            return await cur.fetchone() is not None

    async def _tax_by_day(
        self, conn, country_id: str | None, item_code: str | None, since_day: str
    ) -> dict[str, float]:
        """``{YYYY-MM-DD: tax}`` from *since_day* onward, for the given scope."""
        where = ["day >= ?"]
        params: list = [since_day]
        if country_id:
            where.append("country_id = ?")
            params.append(country_id)
        if item_code:
            where.append("item_code = ?")
            params.append(item_code)
        async with conn.execute(
            "SELECT day, SUM(tax_total) FROM company_tax_revenue "
            f"WHERE {' AND '.join(where)} GROUP BY day ORDER BY day",
            params,
        ) as cur:
            return {str(r[0]): float(r[1] or 0) for r in await cur.fetchall()}

    async def _tax_total(
        self, conn, country_id: str | None, item_code: str | None, since_day: str
    ) -> tuple[float, int]:
        """``(tax, distinct_days)`` from *since_day* onward, for the given scope."""
        where = ["day >= ?"]
        params: list = [since_day]
        if country_id:
            where.append("country_id = ?")
            params.append(country_id)
        if item_code:
            where.append("item_code = ?")
            params.append(item_code)
        async with conn.execute(
            "SELECT SUM(tax_total), COUNT(DISTINCT day) FROM company_tax_revenue "
            f"WHERE {' AND '.join(where)}",
            params,
        ) as cur:
            row = await cur.fetchone()
        return (float(row[0] or 0), int(row[1] or 0)) if row else (0.0, 0)

    async def _tax_by_owner_country(
        self, conn, country_id: str | None, item_code: str | None,
        since_day: str | None = None,
    ) -> list[tuple[str | None, float, float, int]]:
        """``(owner_country_id, tax, wage, companies)`` for the current scope.

        Grouped by the *owner's* nationality rather than by day, summed over
        all retained tax history (see ``TAX_RETENTION_DAYS`` in
        ``services/db/company_tax.py``) unless *since_day* (``YYYY-MM-DD``)
        narrows that to a more recent start. The attribution chain is
        ``company_tax_revenue.company_id`` → ``company_owner_map`` →
        ``owner_id`` → ``citizen_levels.country_id``; a ``None`` key means the
        chain broke somewhere (company predates ``company_owner_map``, or the
        owner has no citizenship on record) rather than being dropped silently.
        """
        where = []
        params: list = []
        if country_id:
            where.append("ctr.country_id = ?")
            params.append(country_id)
        if item_code:
            where.append("ctr.item_code = ?")
            params.append(item_code)
        if since_day:
            where.append("ctr.day >= ?")
            params.append(since_day)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        async with conn.execute(
            f"SELECT cl.country_id, SUM(ctr.tax_total), SUM(ctr.wage_total), "
            "COUNT(DISTINCT ctr.company_id) "
            "FROM company_tax_revenue ctr "
            "LEFT JOIN company_owner_map com ON com.company_id = ctr.company_id "
            "LEFT JOIN citizen_levels cl ON cl.user_id = com.owner_id "
            f"{clause} "
            "GROUP BY cl.country_id ORDER BY 2 DESC",
            params,
        ) as cur:
            return [
                (str(r[0]) if r[0] is not None else None, float(r[1] or 0),
                 float(r[2] or 0), int(r[3] or 0))
                for r in await cur.fetchall()
            ]

    async def _tax_breakdown_embeds(
        self, conn, country_id: str | None, item_code: str | None,
        names: dict[str, str], scope_title: str, since_day: str | None = None,
    ) -> list[discord.Embed]:
        """Full embeds breaking tax revenue for this scope down by the
        country of the companies' *owners*, instead of by day.

        *since_day* (``YYYY-MM-DD``) narrows the summed period to that date
        onward; ``None`` sums everything still retained (see
        ``TAX_RETENTION_DAYS``, currently 90 days).

        Unlike ``_tax_block`` this is not attached as a field on an existing
        embed: the number of contributing countries isn't bounded the way a
        7-row daily table is, so it easily blows past Discord's 1024-char
        field limit. Rendered as its own paginated embed(s) — i.e. its own
        message(s) — using the 4096-char *description* budget instead, the
        same way ``_view_owners`` paginates a long owner list.
        """
        title = f"💰 Belasting per land van eigenaar — {scope_title}"
        if not await self._tax_available(conn):
            return []
        if not await self._owner_map_available(conn):
            return [discord.Embed(
                title=title,
                description=(
                    "_Nog niet beschikbaar — deze uitsplitsing vereist een nieuwere "
                    "versie van de bedrijvenscan. Probeer het over een uur opnieuw._"
                ),
                colour=discord.Colour.orange(),
            )]

        rows = await self._tax_by_owner_country(conn, country_id, item_code, since_day)
        total_tax = sum(r[1] for r in rows)
        if not rows or total_tax <= 0:
            return [discord.Embed(
                title=title,
                description="_Nog geen belasting geïnd in deze scope._",
                colour=discord.Colour.orange(),
            )]

        entries = [
            f"`{(names.get(cid, cid) if cid else '❓ Onbekend'):<20}` "
            f"{_fmt_money(tax):>12} CC · {_fmt_int(companies)} bedrijven · "
            f"{100.0 * tax / total_tax:.0f}%"
            for cid, tax, _wage, companies in rows
        ]

        header = f"**Totaal:** {_fmt_money(total_tax)} CC over **{len(rows)}** landen"
        header += f", sinds {since_day}." if since_day else "."
        started_at = await self._tax_started_at(conn)
        if started_at:
            try:
                since = datetime.fromisoformat(started_at)
                if since_day and since_day < since.strftime("%Y-%m-%d"):
                    header += (
                        f"\n*Let op: belasting wordt pas bijgehouden sinds "
                        f"{since:%d-%m-%Y %H:%M} UTC — data van vóór die datum "
                        f"bestaat niet.*"
                    )
                else:
                    header += f"\n*Belasting bijgehouden sinds {since:%d-%m-%Y %H:%M} UTC.*"
            except (ValueError, TypeError):
                pass

        pages = _paginate(entries, first_page_reserve=len(header) + 2)
        embeds: list[discord.Embed] = []
        for idx, page in enumerate(pages):
            body = "\n".join(page)
            if idx == 0:
                body = header + "\n\n" + body
            embeds.append(discord.Embed(
                title=title if idx == 0 else f"{title} ({idx + 1}/{len(pages)})",
                description=body,
                colour=discord.Colour.gold(),
            ))
        return embeds

    async def _owner_map_available(self, conn) -> bool:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='company_owner_map'"
        ) as cur:
            return await cur.fetchone() is not None

    async def _tax_started_at(self, conn) -> str | None:
        async with conn.execute(
            "SELECT value FROM poll_state WHERE key = ?", (_TAX_STARTED_KEY,)
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] else None

    async def _tax_block(
        self, conn, country_id: str | None, item_code: str | None
    ) -> tuple[str, str] | None:
        """Return ``(field_name, field_value)`` for the tax embed field.

        None when the fetcher has not created the tax tables yet.
        """
        if not await self._tax_available(conn):
            return None

        today = datetime.now(timezone.utc).date()
        week_start = (today - timedelta(days=_TAX_DAYS - 1)).isoformat()
        month_start = (today - timedelta(days=_TAX_MONTH_DAYS - 1)).isoformat()

        per_day = await self._tax_by_day(conn, country_id, item_code, week_start)
        month_tax, month_days = await self._tax_total(
            conn, country_id, item_code, month_start
        )

        started_at = await self._tax_started_at(conn)

        # Always show all 7 days, including the ones with nothing in them, so a
        # quiet day is visibly zero rather than silently absent.
        rows = []
        for offset in range(_TAX_DAYS - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            rows.append((day[5:], _fmt_money(per_day.get(day, 0.0))))
        table = _table(("Dag", "Belasting"), (7, 14), rows)

        lines = [table]
        week_total = sum(per_day.values())
        lines.append(f"**7 dagen totaal:** {_fmt_money(week_total)} CC")
        # Only claim a monthly figure once there is more than a week of it,
        # otherwise it just restates the 7-day total.
        if month_days > _TAX_DAYS:
            lines.append(
                f"**{_TAX_MONTH_DAYS} dagen totaal:** {_fmt_money(month_tax)} CC "
                f"*(over {month_days} dagen met data)*"
            )
        if started_at:
            try:
                since = datetime.fromisoformat(started_at)
                lines.append(f"*Belasting bijgehouden sinds {since:%d-%m-%Y %H:%M} UTC.*")
            except (ValueError, TypeError):
                pass
        else:
            lines.append("*Belastingregistratie is nog niet gestart.*")

        return ("💰 Belastinginkomsten", "\n".join(lines))

    async def _country_names(self, conn) -> dict[str, str]:
        """``{country_id: name}`` from the country sweep's snapshot table."""
        try:
            async with conn.execute(
                "SELECT country_id, name FROM country_snapshots "
                "WHERE name IS NOT NULL AND name != ''"
            ) as cur:
                return {str(r[0]): str(r[1]) for r in await cur.fetchall()}
        except Exception:
            logger.debug("fabrieken: country name lookup failed", exc_info=True)
            return {}

    # ── argument resolution ──────────────────────────────────────────────────

    async def _known_item_codes(self, conn) -> set[str]:
        codes = set(ITEM_LABELS_NL)
        try:
            if await self._census_available(conn):
                async with conn.execute(
                    "SELECT DISTINCT item_code FROM company_census"
                ) as cur:
                    codes.update(str(r[0]) for r in await cur.fetchall() if r[0])
        except Exception:
            logger.debug("fabrieken: item code lookup failed", exc_info=True)
        return codes

    async def _resolve_item(self, conn, raw: str) -> str | None:
        """Map user input to an item code.

        Picking a suggestion sends the code (``paper``), but typing the label
        and pressing enter sends the raw text (``papier``) — Discord does not
        force a selection.  Both must resolve, or the command silently reports
        zero for a perfectly valid item.
        """
        needle = raw.strip().lower()
        if not needle:
            return None
        codes = await self._known_item_codes(conn)
        for code in codes:
            if code.lower() == needle:
                return code
        for code, label in ITEM_LABELS_NL.items():
            if label.lower() == needle:
                return code
        return None

    async def _resolve_country(self, conn, raw: str) -> tuple[str, str] | None:
        """Map user input to ``(country_id, name)`` — by ID, name, or code."""
        needle = raw.strip().lower()
        if not needle:
            return None
        try:
            async with conn.execute(
                "SELECT country_id, name, code FROM country_snapshots"
            ) as cur:
                rows = await cur.fetchall()
        except Exception:
            logger.debug("fabrieken: country resolve failed", exc_info=True)
            return None
        for cid, name, code in rows:
            if str(cid).lower() == needle:
                return str(cid), str(name or cid)
        for cid, name, code in rows:
            if str(name or "").lower() == needle or str(code or "").lower() == needle:
                return str(cid), str(name or cid)
        return None

    # ── Autocomplete ─────────────────────────────────────────────────────────
    # Both callbacks deliberately avoid depending on the census tables: they
    # must work before the data-fetcher has ever run, otherwise the command
    # looks broken (no suggestions at all) rather than merely having no data.

    async def _item_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest producible items, filtered by *current*; value = item code."""
        del interaction  # part of the discord.py callback signature
        try:
            async with _connect_ro() as conn:
                codes = await self._known_item_codes(conn)
        except Exception:
            codes = set(ITEM_LABELS_NL)
        needle = (current or "").strip().lower()
        return [
            app_commands.Choice(name=_item_label(c), value=c)
            for c in sorted(codes, key=_item_label)
            if needle in c.lower() or needle in _item_label(c).lower()
        ][:_AUTOCOMPLETE_LIMIT]

    async def _country_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest countries by name, filtered by *current*; value = country_id.

        Sourced from ``country_snapshots`` rather than the census, so every
        country is offerable — including ones holding no companies at all.
        """
        del interaction
        try:
            async with _connect_ro() as conn:
                names = await self._country_names(conn)
        except Exception:
            logger.debug("fabrieken: country autocomplete failed", exc_info=True)
            return []
        needle = (current or "").strip().lower()
        return [
            app_commands.Choice(name=name, value=cid)
            for cid, name in sorted(names.items(), key=lambda kv: kv[1])
            if needle in name.lower()
        ][:_AUTOCOMPLETE_LIMIT]

    # ── Command ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="fabrieken",
        description="Toon hoeveel fabrieken er per product en per land staan.",
    )
    @app_commands.describe(
        item="Product. Laat leeg voor een overzicht per land.",
        land="Land dat de regio's beheerst. Laat leeg voor een overzicht per land.",
        eigenaren="Toon de grootste eigenaren in plaats van de aantallen.",
        alles="Toon álle eigenaren i.p.v. de top 25 (kan meerdere berichten opleveren).",
        met_werknemers="Tel alleen bedrijven die minstens één werknemer hebben.",
        belasting_per_land=(
            "Splits de belastinginkomsten uit naar het land van de bedrijfseigenaren "
            "i.p.v. per dag."
        ),
        sinds=(
            "Alleen gebruikt bij belasting_per_land: tel alleen belasting vanaf deze "
            "datum (JJJJ-MM-DD). Laat leeg voor de volledige bewaarde geschiedenis "
            "(~90 dagen)."
        ),
    )
    @app_commands.autocomplete(item=_item_choices, land=_country_choices)
    async def fabrieken(
        self,
        interaction: discord.Interaction,
        item: str | None = None,
        land: str | None = None,
        eigenaren: bool = False,
        alles: bool = False,
        met_werknemers: bool = False,
        belasting_per_land: bool = False,
        sinds: str | None = None,
    ) -> None:
        await interaction.response.defer()
        since_day: str | None = None
        if sinds:
            try:
                since_day = datetime.strptime(sinds.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="🏭 Fabrieken",
                        description=f"❌ `sinds` moet het formaat JJJJ-MM-DD hebben, niet `{sinds}`.",
                        colour=discord.Colour.red(),
                    )
                )
                return
            if not belasting_per_land:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="🏭 Fabrieken",
                        description="❌ `sinds` werkt alleen samen met `belasting_per_land:True`.",
                        colour=discord.Colour.red(),
                    )
                )
                return
        try:
            embeds = await self._build(
                item, land, eigenaren, alles, met_werknemers, belasting_per_land, since_day
            )
        except Exception:
            logger.exception("fabrieken: failed to build response")
            embeds = [
                discord.Embed(
                    title="🏭 Fabrieken",
                    description=(
                        "⚠️ Kon de bedrijvendatabase niet uitlezen. "
                        "Probeer het later opnieuw."
                    ),
                    colour=discord.Colour.red(),
                )
            ]
        # One embed per message: a full owner listing easily exceeds the
        # 6000-char combined limit that applies to embeds sharing a message.
        for embed in embeds:
            await interaction.followup.send(embed=embed)

    def _no_scan_embed(self, title: str) -> discord.Embed:
        return discord.Embed(
            title=title,
            description=(
                "⏳ Er is nog geen scan uitgevoerd. De bedrijvenscan draait elk "
                "uur — probeer het over een uur opnieuw."
            ),
            colour=discord.Colour.orange(),
        )

    def _unknown_embed(self, what: str, raw: str) -> discord.Embed:
        return discord.Embed(
            title="🏭 Fabrieken",
            description=(
                f"❌ `{raw}` is geen bekend {what}. "
                f"Kies er een uit de suggestielijst."
            ),
            colour=discord.Colour.red(),
        )

    async def _build(
        self,
        item: str | None,
        land: str | None,
        eigenaren: bool = False,
        alles: bool = False,
        staffed: bool = False,
        belasting_per_land: bool = False,
        since_day: str | None = None,
    ) -> list[discord.Embed]:
        """Dispatch to a view and render it. Only the owner view spans pages."""
        if not os.path.isfile(EXTERNAL_DB_PATH):
            logger.warning("fabrieken: %s does not exist", EXTERNAL_DB_PATH)
            return [self._no_scan_embed("🏭 Fabrieken")]

        async with _connect_ro() as conn:
            if not await self._census_available(conn):
                logger.warning("fabrieken: census tables not present yet")
                return [self._no_scan_embed("🏭 Fabrieken")]

            # Resolve arguments before touching the census, so a typo is
            # reported as a typo rather than as "0 bedrijven".
            item_code: str | None = None
            if item:
                item_code = await self._resolve_item(conn, item)
                if item_code is None:
                    return [self._unknown_embed("product", item)]
            country: tuple[str, str] | None = None
            if land:
                country = await self._resolve_country(conn, land)
                if country is None:
                    return [self._unknown_embed("land", land)]

            run = await self._latest_run(conn)
            if run is None:
                return [self._no_scan_embed("🏭 Fabrieken")]
            at, listed, checked = run
            base_at = await self._baseline_run(conn)
            names = await self._country_names(conn)

            if eigenaren:
                embeds = await self._view_owners(
                    conn, country, item_code, alles, staffed
                )
            elif item_code and country:
                embeds = [await self._view_pair(
                    conn, at, base_at, country, item_code, staffed
                )]
            elif item_code:
                embeds = [await self._view_by_country(
                    conn, at, base_at, item_code, names, staffed
                )]
            elif country:
                embeds = [await self._view_by_item(conn, at, base_at, country, staffed)]
            else:
                embeds = [await self._view_overview(conn, at, names, staffed)]

            # Tax revenue is always shown, scoped to whatever filters were
            # given. The per-day table lands on the last embed as a field (a
            # paginated owner listing then doesn't repeat it on every page);
            # the per-owner-country breakdown is unbounded in length, so it
            # gets its own message(s) appended after the others instead.
            if belasting_per_land:
                if item_code and country:
                    scope_title = f"{_item_label(item_code)} in {country[1]}"
                elif item_code:
                    scope_title = f"{_item_label(item_code)} wereldwijd"
                elif country:
                    scope_title = country[1]
                else:
                    scope_title = "wereldwijd"
                try:
                    tax_embeds = await self._tax_breakdown_embeds(
                        conn,
                        country[0] if country else None,
                        item_code,
                        names,
                        scope_title,
                        since_day,
                    )
                except Exception:
                    logger.exception("fabrieken: tax breakdown failed")
                    tax_embeds = []
                embeds.extend(tax_embeds)
            else:
                try:
                    tax_field = await self._tax_block(
                        conn,
                        country[0] if country else None,
                        item_code,
                    )
                except Exception:
                    logger.exception("fabrieken: tax block failed")
                    tax_field = None
                if tax_field is not None:
                    embeds[-1].add_field(
                        name=tax_field[0], value=tax_field[1], inline=False
                    )

        footer = f"{_fmt_int(checked)} bedrijven gecontroleerd"
        if listed and listed != checked:
            footer += f" van {_fmt_int(listed)} gevonden"
        try:
            footer += f" · laatste scan {datetime.fromisoformat(at):%d-%m-%Y %H:%M} UTC"
        except (ValueError, TypeError):
            pass
        embeds[-1].set_footer(text=footer)
        return embeds

    # ── views ────────────────────────────────────────────────────────────────

    async def _view_pair(
        self, conn, at: str, base_at: str | None,
        country: tuple[str, str], item_code: str, staffed: bool = False,
    ) -> discord.Embed:
        country_id, country_name = country
        companies, workers = await self._pair(conn, at, country_id, item_code, staffed)
        lines = [
            f"**Bedrijven{' met werknemers' if staffed else ''}:** {_fmt_int(companies)}",
            f"**Werknemers:** {_fmt_int(workers)}",
        ]
        if base_at is None:
            lines.append(self._no_baseline_note())
        else:
            old_c, old_w = await self._pair(
                conn, base_at, country_id, item_code, staffed
            )
            lines.append(
                f"\n**Verschil t.o.v. {_DELTA_DAYS} dagen geleden**\n"
                f"Bedrijven: **{_fmt_delta(companies, old_c)}**  ·  "
                f"Werknemers: **{_fmt_delta(workers, old_w)}**"
            )
        return discord.Embed(
            title=f"🏭 {_item_label(item_code)}fabrieken in {country_name}",
            description="\n".join(lines),
            colour=discord.Colour.green(),
        )

    async def _view_by_country(
        self, conn, at: str, base_at: str | None,
        item_code: str, names: dict[str, str], staffed: bool = False,
    ) -> discord.Embed:
        rows = await self._by_country(conn, at, item_code, staffed)
        label = _item_label(item_code)
        suffix = " met werknemers" if staffed else ""
        if not rows:
            return discord.Embed(
                title=f"🏭 {label}fabrieken per land",
                description=f"Geen enkel land heeft {label.lower()}fabrieken{suffix}.",
                colour=discord.Colour.orange(),
            )

        base: dict[str, int] = {}
        if base_at is not None:
            base = {
                c: comp
                for c, comp, _ in await self._by_country(
                    conn, base_at, item_code, staffed
                )
            }

        shown = rows[:_MAX_LIST_ROWS]
        if base_at is None:
            table = _table(
                ("Land", "Bedrijven", "Werknemers"), (20, 9, 10),
                [(names.get(c, c), _fmt_int(comp), _fmt_int(w)) for c, comp, w in shown],
            )
        else:
            table = _table(
                ("Land", "Bedrijven", "Werknemers", f"Δ{_DELTA_DAYS}d"), (18, 9, 10, 7),
                [
                    (names.get(c, c), _fmt_int(comp), _fmt_int(w),
                     _fmt_delta(comp, base.get(c, 0)))
                    for c, comp, w in shown
                ],
            )

        total_c = sum(c for _, c, _ in rows)
        total_w = sum(w for _, _, w in rows)
        head = (
            f"**{_fmt_int(total_c)}** {label.lower()}fabrieken{suffix} in "
            f"**{len(rows)}** landen, samen **{_fmt_int(total_w)}** werknemers."
        )
        if len(rows) > len(shown):
            head += f"\n*Top {len(shown)} getoond.*"
        if base_at is None:
            head += self._no_baseline_note()
        return discord.Embed(
            title=f"🏭 {label}fabrieken per land",
            description=head + "\n" + table,
            colour=discord.Colour.green(),
        )

    async def _view_by_item(
        self, conn, at: str, base_at: str | None, country: tuple[str, str],
        staffed: bool = False,
    ) -> discord.Embed:
        country_id, country_name = country
        rows = await self._by_item(conn, at, country_id, staffed)
        suffix = " met werknemers" if staffed else ""
        if not rows:
            return discord.Embed(
                title=f"🏭 Fabrieken in {country_name}",
                description=f"{country_name} heeft geen bedrijven{suffix}.",
                colour=discord.Colour.orange(),
            )

        base: dict[str, int] = {}
        if base_at is not None:
            base = {
                i: comp
                for i, comp, _ in await self._by_item(
                    conn, base_at, country_id, staffed
                )
            }

        shown = rows[:_MAX_LIST_ROWS]
        if base_at is None:
            table = _table(
                ("Product", "Bedrijven", "Werknemers"), (20, 9, 10),
                [(_item_label(i), _fmt_int(c), _fmt_int(w)) for i, c, w in shown],
            )
        else:
            table = _table(
                ("Product", "Bedrijven", "Werknemers", f"Δ{_DELTA_DAYS}d"), (18, 9, 10, 7),
                [
                    (_item_label(i), _fmt_int(c), _fmt_int(w), _fmt_delta(c, base.get(i, 0)))
                    for i, c, w in shown
                ],
            )

        total_c = sum(c for _, c, _ in rows)
        total_w = sum(w for _, _, w in rows)
        head = (
            f"**{_fmt_int(total_c)}** bedrijven{suffix} in **{len(rows)}** "
            f"productsoorten, samen **{_fmt_int(total_w)}** werknemers."
        )
        if base_at is None:
            head += self._no_baseline_note()
        return discord.Embed(
            title=f"🏭 Fabrieken in {country_name}",
            description=head + "\n" + table,
            colour=discord.Colour.green(),
        )

    async def _view_overview(
        self, conn, at: str, names: dict[str, str], staffed: bool = False
    ) -> discord.Embed:
        top_countries = await self._totals(conn, at, "country_id", _TOP_N, staffed)
        top_items = await self._totals(conn, at, "item_code", _TOP_N, staffed)
        if not top_countries:
            return self._no_scan_embed("🏭 Fabrieken wereldwijd")

        note = (
            "Alleen bedrijven met minstens één werknemer.\n"
            if staffed else ""
        )
        embed = discord.Embed(
            title="🏭 Fabrieken wereldwijd",
            description=(
                note + "Gebruik `/fabrieken item:` of `land:` voor een uitsplitsing."
            ),
            colour=discord.Colour.green(),
        )
        embed.add_field(
            name=f"Top {len(top_countries)} landen",
            value=_table(
                ("Land", "Bedrijven", "Werknemers"), (20, 9, 10),
                [(names.get(c, c), _fmt_int(comp), _fmt_int(w))
                 for c, comp, w in top_countries],
            ),
            inline=False,
        )
        embed.add_field(
            name=f"Top {len(top_items)} producten",
            value=_table(
                ("Product", "Bedrijven", "Werknemers"), (20, 9, 10),
                [(_item_label(i), _fmt_int(comp), _fmt_int(w))
                 for i, comp, w in top_items],
            ),
            inline=False,
        )
        return embed

    async def _view_owners(
        self,
        conn,
        country: tuple[str, str] | None,
        item_code: str | None,
        alles: bool = False,
        staffed: bool = False,
    ) -> list[discord.Embed]:
        """Largest owners, scoped by whichever of country/item was supplied.

        With *alles* the full list is paginated across several embeds, capped at
        ``_MAX_ALL_PAGES`` messages — an unfiltered query covers >16k owners,
        which is hundreds of messages and helps nobody.
        """
        country_id = country[0] if country else None
        country_name = country[1] if country else None

        # Rendered as a markdown list rather than a code-block table because
        # links do not render inside code blocks, and every owner links to
        # their in-game companies page.
        suffix = " met werknemers" if staffed else ""
        if item_code and country_name:
            scope = f"{_item_label(item_code).lower()}fabrieken{suffix} in {country_name}"
            title = f"👑 Grootste eigenaren — {_item_label(item_code)} in {country_name}"
        elif item_code:
            scope = f"{_item_label(item_code).lower()}fabrieken{suffix} wereldwijd"
            title = f"👑 Grootste eigenaren — {_item_label(item_code)} wereldwijd"
        elif country_name:
            scope = f"bedrijven{suffix} in {country_name}"
            title = f"👑 Grootste eigenaren in {country_name}"
        else:
            scope = f"bedrijven{suffix} wereldwijd"
            title = "👑 Grootste eigenaren wereldwijd"

        limit = _MAX_ALL_ROWS if alles else _MAX_LIST_ROWS
        try:
            rows = await self._owner_rows(
                conn, country_id, item_code, limit, staffed
            )
        except Exception:
            logger.exception("fabrieken: owner query failed")
            return [discord.Embed(
                title=title,
                description=(
                    "⏳ De eigenarenlijst is nog niet beschikbaar. Die wordt bij "
                    "de eerstvolgende scan gevuld — probeer het over een uur opnieuw."
                ),
                colour=discord.Colour.orange(),
            )]
        if not rows:
            return [discord.Embed(
                title=title,
                description=f"Geen eigenaren gevonden voor {scope}.",
                colour=discord.Colour.orange(),
            )]

        owners, total_c, total_w = await self._owner_totals(
            conn, country_id, item_code, staffed
        )
        names = await self._owner_names(conn, [o for o, _, _ in rows])

        header = (
            f"**{_fmt_int(owners)}** spelers bezitten samen **{_fmt_int(total_c)}** "
            f"{scope} met **{_fmt_int(total_w)}** werknemers."
        )

        entries: list[str] = []
        for rank, (owner_id, comps, works) in enumerate(rows, 1):
            name = names.get(owner_id)
            label = (
                discord.utils.escape_markdown(name) if name else f"`{owner_id[:8]}…`"
            )
            who = f"**[{label}](https://app.warera.io/user/{owner_id}/companies)**"
            bedrijf = "bedrijf" if comps == 1 else "bedrijven"
            werk = "werknemer" if works == 1 else "werknemers"
            entries.append(
                f"`{rank:>3}.` {who} — {_fmt_int(comps)} {bedrijf}, "
                f"{_fmt_int(works)} {werk}"
            )

        pages = _paginate(entries, first_page_reserve=len(header) + 2)
        truncated = owners > len(rows)
        if len(pages) > _MAX_ALL_PAGES:
            pages = pages[:_MAX_ALL_PAGES]
            truncated = True

        shown = sum(len(p) for p in pages)
        embeds: list[discord.Embed] = []
        for idx, page in enumerate(pages):
            body = "\n".join(page)
            if idx == 0:
                body = header + "\n\n" + body
            if idx == len(pages) - 1 and truncated:
                body += (
                    f"\n\n*{_fmt_int(shown)} van {_fmt_int(owners)} eigenaren getoond"
                    + ("" if alles else " — gebruik `alles:True` voor de volledige lijst")
                    + ". Verfijn met `item:` of `land:` voor een kortere lijst.*"
                )
            embeds.append(discord.Embed(
                title=title if idx == 0 else f"{title} ({idx + 1}/{len(pages)})",
                description=body,
                colour=discord.Colour.gold(),
            ))
        return embeds

    def _no_baseline_note(self) -> str:
        return (
            f"\n*Verschil t.o.v. {_DELTA_DAYS} dagen geleden is nog niet "
            f"beschikbaar — daarvoor moet de scan {_DELTA_DAYS} dagen "
            f"gedraaid hebben.*"
        )


async def setup(bot: commands.Bot) -> FabriekenCog:
    cog = FabriekenCog(bot)
    await bot.add_cog(cog)
    return cog
