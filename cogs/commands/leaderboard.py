"""Slash command /leaderboard — various hall-of-fame rankings.

Types
-----
speler_schade  — Top players by total battle damage (accumulated DB)
gevecht        — Top battles by combined damage (accumulated DB)
solo           — Top single-fight player performance (accumulated DB)
mu             — Top MUs by total battle damage (accumulated DB)
vermogen       — Top players by wealth (live API, no days filter)

If no type is given, a compact overview embed is shown (top 3 per type,
excluding vermogen).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, country_autocomplete
from services.damage_calc import fmt_damage

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

_DEFAULT_TOP   = 10
_MAX_TOP       = 100
_OVERVIEW_TOP  = 3
_ROWS_PER_EMBED = 50   # max table rows per embed page

_MAANDEN = ["jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"]

# ── Helpers ──────────────────────────────────────────────────────────────────

def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v.get("data", v)
    return resp


def _medal(rank: int) -> str:
    return f"{rank:>2}."


def _fmt_bonus(value: float) -> str:
    """Format a production bonus percentage: show 2 decimals only if the second is non-zero."""
    s = f"{value:.2f}"
    return (s if not s.endswith("0") else f"{value:.1f}") + "%"


def _fmt_date(date_str: str) -> str:
    """Convert '2026-03-31' → '31 mrt \'26'."""
    try:
        y, m, d = date_str[:10].split("-")
        return f"{int(d)} {_MAANDEN[int(m) - 1]} '{y[2:]}"
    except (ValueError, IndexError):
        return date_str[:10]


def _code_table(header: tuple, rows: list[tuple]) -> str:
    """Render a fixed-width monospace table inside a Discord code block."""
    all_rows: list[tuple] = [header, *rows]
    widths: list[int] = [
        max(len(str(cell)) for cell in col) for col in zip(*all_rows)
    ]

    def _row(r: tuple) -> str:
        return "  ".join(str(v).ljust(w) for v, w in zip(r, widths))

    sep = "─" * (sum(widths) + 2 * (len(widths) - 1))
    lines = [_row(header), sep] + [_row(r) for r in rows]
    return "```\n" + "\n".join(lines) + "\n```"


def _paginate_table(header: tuple, rows: list[tuple]) -> list[str]:
    """Split table rows into pages of _ROWS_PER_EMBED and return a list of code-block strings."""
    pages: list[str] = []
    for start in range(0, max(1, len(rows)), _ROWS_PER_EMBED):
        chunk = rows[start : start + _ROWS_PER_EMBED]
        pages.append(_code_table(header, chunk))
    return pages


# ── Entry field helpers for /ranking.getRanking responses ────────────────────

def _entry_user_id(entry: dict) -> str:
    user = entry.get("user") or {}
    if isinstance(user, str):
        return user
    return user.get("_id") or user.get("id") or ""


def _entry_user_name(entry: dict) -> str:
    user = entry.get("user") or {}
    if isinstance(user, dict):
        return user.get("username") or user.get("name") or ""
    return ""


def _entry_country_id(entry: dict) -> str:
    country = entry.get("country") or {}
    if isinstance(country, str):
        return country
    return country.get("_id") or country.get("id") or ""


def _entry_country_name(entry: dict) -> str:
    country = entry.get("country") or {}
    if isinstance(country, dict):
        return country.get("name") or country.get("shortName") or ""
    return ""


def _entry_mu_id(entry: dict) -> str:
    mu = entry.get("mu") or entry.get("org") or {}
    if isinstance(mu, str):
        return mu
    return mu.get("_id") or mu.get("id") or ""


def _entry_mu_name(entry: dict) -> str:
    mu = entry.get("mu") or entry.get("org") or {}
    if isinstance(mu, dict):
        return mu.get("name") or mu.get("fullName") or ""
    return ""


# ── Cog ──────────────────────────────────────────────────────────────────────

class LeaderboardCog(CommandCogBase, name="leaderboard"):
    """Cog for the /leaderboard command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Ranking API helper
    # ------------------------------------------------------------------ #

    async def _fetch_ranking(self, ranking_type: str) -> list[dict]:
        """Fetch global ranking entries from /ranking.getRanking."""
        if not self._client:
            return []
        try:
            resp = await self._client.post(
                "/ranking.getRanking",
                json={"rankingType": ranking_type},
            )
        except Exception as exc:
            logger.warning("_fetch_ranking(%s) failed: %s", ranking_type, exc)
            return []
        data = _unwrap(resp)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        if isinstance(data, dict):
            for key in ("items", "ranking", "rankings", "data", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return [e for e in v if isinstance(e, dict)]
        return []

    # ------------------------------------------------------------------ #
    # Name resolution
    # ------------------------------------------------------------------ #

    async def _resolve_name(self, user_id: str) -> str:
        """Return display name for user_id. DB first, then API, then ID."""
        if self._db:
            name = await self._db.get_citizen_name_by_id(user_id)
            if name:
                return name
        client = self._client
        if client:
            try:
                raw = await client.get(
                    "/user.getUserLite",
                    params={"input": json.dumps({"userId": user_id})},
                )
                data = _unwrap(raw)
                if isinstance(data, dict):
                    return data.get("username") or user_id
            except Exception:
                pass
        return user_id

    async def _resolve_names(self, user_ids: list[str]) -> dict[str, str]:
        """Return {user_id: name} for a list of IDs."""
        results = await asyncio.gather(*[self._resolve_name(uid) for uid in user_ids])
        return dict(zip(user_ids, results))

    async def _resolve_mu_name(self, mu_id: str) -> str:
        """Return display name for mu_id via mu.getById API."""
        client = self._client
        if not client:
            return mu_id
        try:
            raw = await client.post("/mu.getById", json={"muId": mu_id})
            data = _unwrap(raw)
            if isinstance(data, dict):
                return data.get("name") or data.get("fullName") or mu_id
        except Exception:
            pass
        return mu_id

    async def _resolve_mu_names(self, mu_ids: list[str]) -> dict[str, str]:
        """Return {mu_id: name} for a list of MU IDs in parallel."""
        results = await asyncio.gather(*[self._resolve_mu_name(mid) for mid in mu_ids])
        return dict(zip(mu_ids, results))

    async def _fetch_country_map(self) -> dict[str, str]:
        """Return {country_id: country_name}.

        Tries the live API first; falls back to the country_snapshots DB cache.
        """
        client = self._client
        if client:
            try:
                raw = await client.get("/country.getAllCountries")
                data = _unwrap(raw)
                countries: list[dict] = []
                if isinstance(data, list):
                    countries = data
                elif isinstance(data, dict):
                    for key in ("items", "countries", "data", "results"):
                        v = data.get(key)
                        if isinstance(v, list):
                            countries = v
                            break
                result = {
                    c["_id"]: c.get("name") or c["_id"]
                    for c in countries
                    if isinstance(c, dict) and c.get("_id")
                }
                if result:
                    return result
            except Exception:
                pass
        # Fallback: read country names cached in DB from production polling
        if self._db:
            try:
                return await self._db.get_country_name_map()
            except Exception:
                pass
        return {}

    # ------------------------------------------------------------------ #
    # Section builders
    # ------------------------------------------------------------------ #

    async def _section_speler_schade(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[str]:
        # Use live ranking API when no day/country filter (more complete data)
        if days is None and country_id is None and self._client:
            entries = await self._fetch_ranking("userDamages")
            if entries:
                entries = entries[:limit]
                user_ids = [_entry_user_id(e) for e in entries]
                inline_names = [_entry_user_name(e) for e in entries]
                missing = [uid for uid, nm in zip(user_ids, inline_names) if not nm and uid]
                resolved = await self._resolve_names(missing)
                table_rows = [
                    (_medal(i + 1), (inline or resolved.get(uid) or uid)[:20], fmt_damage(e.get("value", 0)))
                    for i, (e, uid, inline) in enumerate(zip(entries, user_ids, inline_names))
                ]
                pages = _paginate_table(("#", "Speler", "Schade"), table_rows)
                pages[-1] += "\n-# Live data van API"
                return pages
        # Fallback: DB
        assert self._db
        rows = await self._db.get_top_players_by_damage(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        user_ids = [r["user_id"] for r in rows]
        names = await self._resolve_names(user_ids)
        table_rows = [
            (_medal(i + 1), (names.get(r["user_id"]) or r["user_id"])[:20],
             fmt_damage(r["total_damage"]), str(r["battle_count"]))
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "Speler", "Schade", "Gevechten"), table_rows)

    async def _section_gevecht(
        self, days: Optional[int], limit: int,
        country_map: Optional[dict[str, str]] = None,
        country_id: Optional[str] = None,
    ) -> list[str]:
        assert self._db
        rows = await self._db.get_top_battles(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        if country_map is None:
            country_map = await self._fetch_country_map()
        table_rows = []
        for i, row in enumerate(rows):
            cid_att = row["attacker_country_id"] or ""
            cid_def = row["defender_country_id"] or ""
            att_c = (country_map.get(cid_att) or cid_att or "?")[:10]
            def_c = (country_map.get(cid_def) or cid_def or "?")[:10]
            table_rows.append((
                _medal(i + 1),
                att_c,
                def_c,
                fmt_damage(row["total_damage"]),
                _fmt_date(row["battle_created_at"]),
            ))
        return _paginate_table(
            ("#", "Aanvaller", "Verdediger", "Totaal", "Datum"),
            table_rows,
        )

    async def _section_solo(
        self, days: Optional[int], limit: int,
        country_map: Optional[dict[str, str]] = None,
        country_id: Optional[str] = None,
    ) -> list[str]:
        assert self._db
        rows = await self._db.get_top_single_battle_damage(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        user_ids = list({r["user_id"] for r in rows})
        names = await self._resolve_names(user_ids)
        if country_map is None:
            country_map = await self._fetch_country_map()
        table_rows = []
        for i, r in enumerate(rows):
            cid_att = r.get("attacker_country_id") or ""
            cid_def = r.get("defender_country_id") or ""
            att_c = (country_map.get(cid_att) or cid_att or "?")[:10]
            def_c = (country_map.get(cid_def) or cid_def or "?")[:10]
            table_rows.append((
                _medal(i + 1),
                (names.get(r["user_id"]) or r["user_id"])[:14],
                fmt_damage(r["damage"]),
                att_c,
                def_c,
                _fmt_date(r["battle_created_at"]),
            ))
        return _paginate_table(("#", "Speler", "Schade", "Atk", "Def", "Datum"), table_rows)

    async def _section_mu(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[str]:
        if days is None and country_id is None and self._client:
            entries = await self._fetch_ranking("muDamages")
            if entries:
                entries = entries[:limit]
                mu_ids = [_entry_mu_id(e) for e in entries]
                inline_names = [_entry_mu_name(e) for e in entries]
                missing = [mid for mid, nm in zip(mu_ids, inline_names) if not nm and mid]
                resolved = await self._resolve_mu_names(missing)
                table_rows = [
                    (_medal(i + 1),
                     (inline or resolved.get(mid) or mid)[:24],
                     fmt_damage(e.get("value", 0)))
                    for i, (e, mid, inline) in enumerate(zip(entries, mu_ids, inline_names))
                ]
                pages = _paginate_table(("#", "MU", "Totaal"), table_rows)
                pages[-1] += "\n-# Live data van API"
                return pages
        # Fallback: DB
        assert self._db
        rows = await self._db.get_top_mus_by_damage(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        # Resolve names for MUs where DB only stored the ID
        ids_needing_lookup = [
            r["mu_id"] for r in rows
            if not r.get("mu_name") or r["mu_name"] == r["mu_id"]
        ]
        resolved = await self._resolve_mu_names(ids_needing_lookup)
        table_rows = [
            (_medal(i + 1),
             (resolved.get(r["mu_id"]) or r["mu_name"] or r["mu_id"])[:24],
             fmt_damage(r["total_damage"]), str(r["battle_count"]))
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "MU", "Totaal", "Gevechten"), table_rows)

    async def _section_land(
        self, days: Optional[int], limit: int,
        country_map: Optional[dict[str, str]] = None,
    ) -> list[str]:
        if days is None and self._client:
            entries = await self._fetch_ranking("countryDamages")
            if entries:
                entries = entries[:limit]
                if country_map is None:
                    country_map = await self._fetch_country_map()
                table_rows = [
                    (
                        _medal(i + 1),
                        (_entry_country_name(e) or country_map.get(_entry_country_id(e)) or _entry_country_id(e))[:20],
                        fmt_damage(e.get("value", 0)),
                    )
                    for i, e in enumerate(entries)
                ]
                pages = _paginate_table(("#", "Land", "Totaal"), table_rows)
                pages[-1] += "\n-# Live data van API"
                return pages
        # Fallback: DB
        assert self._db
        rows = await self._db.get_top_countries_by_damage(days, limit)
        if not rows:
            return ["*Nog geen data*"]
        if country_map is None:
            country_map = await self._fetch_country_map()
        table_rows = [
            (_medal(i + 1), (country_map.get(r["country_id"]) or r["country_id"])[:20],
             fmt_damage(r["total_damage"]))
            for i, r in enumerate(rows)
        ]
        pages = _paginate_table(("#", "Land", "Totaal"), table_rows)

        # Append data-coverage note to the last page so users know why numbers
        # may differ from the in-game country leaderboard.
        earliest, _ = await self._db.get_country_hits_date_range()
        if earliest:
            try:
                date_label = _fmt_date(earliest)
            except Exception:
                date_label = earliest[:10]
            note = f"\n-# Data beschikbaar vanaf {date_label} · oudere gevechten ontbreken"
        else:
            note = "\n-# Gebaseerd op aanvaller/verdediger zijde (nationaliteitsdata nog niet beschikbaar)"
        pages[-1] = pages[-1] + note
        return pages

    async def _section_mu_record(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[str]:
        assert self._db
        rows = await self._db.get_best_mu_per_battle(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        ids_needing_lookup = [
            r["mu_id"] for r in rows
            if not r.get("mu_name") or r["mu_name"] == r["mu_id"]
        ]
        resolved = await self._resolve_mu_names(ids_needing_lookup)
        table_rows = [
            (
                _medal(i + 1),
                (resolved.get(r["mu_id"]) or r["mu_name"] or r["mu_id"])[:24],
                fmt_damage(r["damage"]),
                _fmt_date(r["battle_created_at"]),
            )
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "MU", "Schade", "Datum"), table_rows)

    async def _section_land_record(
        self, days: Optional[int], limit: int,
        country_map: Optional[dict[str, str]] = None,
        country_id: Optional[str] = None,
    ) -> list[str]:
        assert self._db
        rows = await self._db.get_best_country_per_battle(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        if country_map is None:
            country_map = await self._fetch_country_map()
        table_rows = [
            (
                _medal(i + 1),
                (country_map.get(r["country_id"]) or r["country_id"])[:20],
                fmt_damage(r["damage"]),
                _fmt_date(r["battle_created_at"]),
            )
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "Land", "Schade", "Datum"), table_rows)

    async def _section_wekelijks_speler(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[str]:
        # Use live API for current week when no filters (DB data is incomplete)
        if days is None and country_id is None and self._client:
            entries = await self._fetch_ranking("weeklyUserDamages")
            if entries:
                entries = entries[:limit]
                user_ids = [_entry_user_id(e) for e in entries]
                inline_names = [_entry_user_name(e) for e in entries]
                missing = [uid for uid, nm in zip(user_ids, inline_names) if not nm and uid]
                resolved = await self._resolve_names(missing)
                table_rows = [
                    (
                        _medal(i + 1),
                        (inline or resolved.get(uid) or uid)[:14],
                        fmt_damage(e.get("value", 0)),
                    )
                    for i, (e, uid, inline) in enumerate(zip(entries, user_ids, inline_names))
                ]
                pages = _paginate_table(("#", "Speler", "Schade"), table_rows)
                pages[-1] += "\n-# Live data · huidige week"
                return pages
        # Fallback: DB
        assert self._db
        rows = await self._db.get_best_player_week(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        user_ids = list({r["user_id"] for r in rows})
        names = await self._resolve_names(user_ids)
        table_rows = [
            (
                _medal(i + 1),
                (names.get(r["user_id"]) or r["user_id"])[:14],
                fmt_damage(r["damage"]),
                _fmt_date(r["week_start"]),
            )
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "Speler", "Schade", "Week van"), table_rows)

    async def _section_wekelijks_mu(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[str]:
        if days is None and country_id is None and self._client:
            entries = await self._fetch_ranking("muWeeklyDamages")
            if entries:
                entries = entries[:limit]
                mu_ids = [_entry_mu_id(e) for e in entries]
                inline_names = [_entry_mu_name(e) for e in entries]
                missing = [mid for mid, nm in zip(mu_ids, inline_names) if not nm and mid]
                resolved = await self._resolve_mu_names(missing)
                table_rows = [
                    (
                        _medal(i + 1),
                        (inline or resolved.get(mid) or mid)[:24],
                        fmt_damage(e.get("value", 0)),
                    )
                    for i, (e, mid, inline) in enumerate(zip(entries, mu_ids, inline_names))
                ]
                pages = _paginate_table(("#", "MU", "Schade"), table_rows)
                pages[-1] += "\n-# Live data · huidige week"
                return pages
        # Fallback: DB
        assert self._db
        rows = await self._db.get_best_mu_week(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        ids_needing_lookup = [
            r["mu_id"] for r in rows
            if not r.get("mu_name") or r["mu_name"] == r["mu_id"]
        ]
        resolved = await self._resolve_mu_names(ids_needing_lookup)
        table_rows = [
            (
                _medal(i + 1),
                (resolved.get(r["mu_id"]) or r["mu_name"] or r["mu_id"])[:24],
                fmt_damage(r["damage"]),
                _fmt_date(r["week_start"]),
            )
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "MU", "Schade", "Week van"), table_rows)

    async def _section_wekelijks_land(
        self, days: Optional[int], limit: int,
        country_map: Optional[dict[str, str]] = None,
        country_id: Optional[str] = None,
    ) -> list[str]:
        if days is None and country_id is None and self._client:
            entries = await self._fetch_ranking("weeklyCountryDamages")
            if entries:
                entries = entries[:limit]
                if country_map is None:
                    country_map = await self._fetch_country_map()
                table_rows = [
                    (
                        _medal(i + 1),
                        (_entry_country_name(e) or country_map.get(_entry_country_id(e)) or _entry_country_id(e))[:20],
                        fmt_damage(e.get("value", 0)),
                    )
                    for i, e in enumerate(entries)
                ]
                pages = _paginate_table(("#", "Land", "Schade"), table_rows)
                pages[-1] += "\n-# Live data · huidige week"
                return pages
        # Fallback: DB
        assert self._db
        rows = await self._db.get_best_country_week(days, limit, country_id)
        if not rows:
            return ["*Nog geen data*"]
        if country_map is None:
            country_map = await self._fetch_country_map()
        table_rows = [
            (
                _medal(i + 1),
                (country_map.get(r["country_id"]) or r["country_id"])[:20],
                fmt_damage(r["damage"]),
                _fmt_date(r["week_start"]),
            )
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "Land", "Schade", "Week van"), table_rows)

    async def _section_productie_bonus(self, limit: int) -> list[str]:
        if not self._db:
            return ["*Diensten niet beschikbaar*"]
        rows = await self._db.get_top_countries_by_production_bonus(limit)
        if not rows:
            return ["*Nog geen data*"]
        table_rows = [
            (
                _medal(i + 1),
                (r["name"] or r["country_id"])[:20],
                _fmt_bonus(r["production_bonus"]),
            )
            for i, r in enumerate(rows)
        ]
        return _paginate_table(("#", "Land", "Bonus"), table_rows)

    async def _section_artikel_tip(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[str]:
        assert self._db
        rows = await self._db.get_top_tippers(days, limit, country_id)
        if not rows:
            return ["*Nog geen data — voer eerst `/peil artikelen` uit.*"]
        table_rows = [
            (
                _medal(i + 1),
                (r["citizen_name"] or r["user_id"])[:22],
                f"{r['tip_total']:,.2f}",
                str(r["tip_count"]),
            )
            for i, r in enumerate(rows)
        ]
        pages = _paginate_table(("#", "Speler", "Totaal CC", "Tips"), table_rows)
        earliest, _ = await self._db.get_article_tips_date_range()
        note = ""
        if earliest:
            note = f"\n-# Data vanaf {_fmt_date(earliest[:10])}"
        if days:
            note += f" · filter: laatste {days} dagen"
        if note:
            pages[-1] = pages[-1] + note
        return pages

    async def _section_bevolking(self, limit: int) -> list[str]:
        client = self._client
        if not client:
            return ["*API niet beschikbaar*"]
        try:
            raw = await client.get("/country.getAllCountries")
            data = _unwrap(raw) if isinstance(raw, dict) else raw
        except Exception as exc:
            logger.warning("Leaderboard: bevolking API call failed: %s", exc)
            return ["*API-fout bij ophalen bevolking*"]

        if not isinstance(data, list):
            return ["*Geen data ontvangen*"]

        entries: list[tuple[str, int]] = []
        for c in data:
            if not isinstance(c, dict):
                continue
            pop_obj = (c.get("rankings") or {}).get("countryActivePopulation")
            pop = pop_obj.get("value", 0) if isinstance(pop_obj, dict) else 0
            name = c.get("name") or c.get("_id") or ""
            if name and pop:
                entries.append((name, int(pop)))

        entries.sort(key=lambda x: -x[1])
        entries = entries[:limit]

        if not entries:
            return ["*Nog geen data*"]

        table_rows = [
            (_medal(i + 1), name[:20], str(pop))
            for i, (name, pop) in enumerate(entries)
        ]
        pages = _paginate_table(("#", "Land", "Spelers"), table_rows)
        pages[-1] += "\n-# Actieve burgers (live data van game server)"
        return pages

    async def _section_regio_count(self, limit: int) -> list[str]:
        client = self._client
        if not client:
            return ["*API niet beschikbaar*"]
        try:
            raw = await client.get("/region.getRegionsObject")
            regions_data = (
                raw.get("result", {}).get("data", {})
                if isinstance(raw, dict)
                else {}
            )
            if not isinstance(regions_data, dict) or not regions_data:
                regions_data = _unwrap(raw) if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning("Leaderboard: regio API call failed: %s", exc)
            return ["*API-fout bij ophalen regio's*"]

        counts: dict[str, int] = {}
        if isinstance(regions_data, dict):
            for robj in regions_data.values():
                if isinstance(robj, dict):
                    cid = robj.get("country")
                    if cid:
                        counts[cid] = counts.get(cid, 0) + 1

        if not counts:
            return ["*Geen regio-data ontvangen*"]

        country_map = await self._fetch_country_map()
        sorted_countries = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        table_rows = [
            (
                _medal(i + 1),
                (country_map.get(cid) or cid)[:20],
                str(cnt),
            )
            for i, (cid, cnt) in enumerate(sorted_countries)
        ]
        return _paginate_table(("#", "Land", "Regio's"), table_rows)

    async def _section_vermogen(self, limit: int) -> list[str]:
        client = self._client
        if not client or client.is_available is False:
            return ["*API momenteel niet beschikbaar (offline)*"]
        try:
            resp = await client.post(
                "/ranking.getRanking",
                json={"rankingType": "userWealth"},
            )
        except Exception as exc:
            logger.warning("Leaderboard: vermogen API call failed: %s", exc)
            return ["*API-fout bij ophalen vermogen*"]

        data = _unwrap(resp)
        entries: list[dict] = []
        if isinstance(data, list):
            entries = [e for e in data if isinstance(e, dict)]
        elif isinstance(data, dict):
            for key in ("items", "ranking", "rankings", "data", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    entries = [e for e in v if isinstance(e, dict)]
                    break

        if not entries:
            return ["*Geen data ontvangen*"]

        # Collect all user IDs so we can resolve names in bulk
        user_ids: list[str] = []
        for entry in entries[:limit]:
            user = entry.get("user") or {}
            uid = user if isinstance(user, str) else user.get("_id") or user.get("id") or ""
            user_ids.append(uid)
        names = await self._resolve_names([u for u in user_ids if u])

        table_rows = []
        for i, (entry, uid) in enumerate(zip(entries[:limit], user_ids)):
            # Try embedded name first, then resolved name, then raw ID
            user = entry.get("user") or {}
            if isinstance(user, dict):
                inline_name = user.get("username") or user.get("name") or ""
            else:
                inline_name = ""
            name = inline_name or names.get(uid, uid) or uid
            wealth = entry.get("value") or entry.get("wealth") or 0
            table_rows.append((_medal(i + 1), name[:22], f"{wealth:,.0f}"))

        return _paginate_table(("#", "Speler", "Vermogen (CC)"), table_rows)

    # ------------------------------------------------------------------ #
    # Command
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="leaderboard",
        description="Toon diverse ranglijsten: schade, gevechten, MUs of vermogen.",
    )
    @app_commands.describe(
        soort="Welk type ranglijst? Laat leeg voor een overzicht.",
        days="Filter op de afgelopen N dagen (niet van toepassing op vermogen).",
        top_n="Hoeveel posities tonen? Standaard 10, max 100. Grote aantallen worden over meerdere berichten verspreid.",
        land="Optioneel: filter op land (bijv. 'Netherlands'). Leeg = wereldwijde ranglijst.",
    )
    @app_commands.choices(
        soort=[
            app_commands.Choice(name="Speler schade (totaal)", value="speler_schade"),
            app_commands.Choice(name="Gevecht (meeste schade)", value="gevecht"),
            app_commands.Choice(name="Solo record (1 gevecht)", value="solo"),
            app_commands.Choice(name="MU schade (totaal)", value="mu"),
            app_commands.Choice(name="MU record (1 gevecht)", value="mu_record"),
            app_commands.Choice(name="Land schade (totaal)", value="land"),
            app_commands.Choice(name="Land record (1 gevecht)", value="land_record"),
            app_commands.Choice(name="Wekelijks record — speler", value="wekelijks_speler"),
            app_commands.Choice(name="Wekelijks record — MU", value="wekelijks_mu"),
            app_commands.Choice(name="Wekelijks record — land", value="wekelijks_land"),
            app_commands.Choice(name="Artikel tips (meest gegeven)", value="artikel_tip"),
            app_commands.Choice(name="Productie bonus (live)", value="productie_bonus"),
            app_commands.Choice(name="Actieve bevolking (live)", value="bevolking"),
            app_commands.Choice(name="Meeste regio's (live)", value="regio_count"),
            app_commands.Choice(name="Vermogen (live)", value="vermogen"),
        ]
    )
    @app_commands.autocomplete(land=country_autocomplete)
    @app_commands.rename(soort="type")
    async def leaderboard(
        self,
        ctx: Context,
        soort: Optional[str] = None,
        days: Optional[int] = None,
        top_n: Optional[int] = None,
        land: Optional[str] = None,
    ) -> None:
        if not self._db:
            await ctx.send("Diensten niet geïnitialiseerd.")
            return

        await ctx.defer()

        limit = max(1, min(top_n or _DEFAULT_TOP, _MAX_TOP))

        # Always fetch country map first — needed for name resolution and display
        country_map = await self._fetch_country_map()

        # Resolve optional country filter
        country_id: Optional[str] = None
        country_label = ""
        if land:
            name_lower = land.lower().strip()
            country_id = next(
                (cid for cid, cname in country_map.items() if cname.lower() == name_lower),
                None,
            )
            if country_id is None:
                await ctx.send(f"Land **{land}** niet gevonden. Controleer de naam.")
                return
            country_label = country_map.get(country_id, land)

        days_suffix = (
            (f" — {country_label}" if country_label else "")
            + (f" — laatste {days} dagen" if days else "")
        )

        # ── Overview (no type) ────────────────────────────────────────
        if soort is None:
            embed = discord.Embed(
                title=f"🏆 Leaderboard Overzicht{days_suffix}",
                description=f"Top {_OVERVIEW_TOP} per categorie",
                colour=self._embed_colour(),
            )
            sections = [
                ("⚔️ Speler schade", self._section_speler_schade(days, _OVERVIEW_TOP, country_id)),
                ("💥 Gevecht (totaal)", self._section_gevecht(days, _OVERVIEW_TOP, country_map, country_id)),
                ("🎯 Solo record (1 gevecht)", self._section_solo(days, _OVERVIEW_TOP, country_map, country_id)),
                ("🛡️ MU schade", self._section_mu(days, _OVERVIEW_TOP, country_id)),
                ("🌍 Land schade", self._section_land(days, _OVERVIEW_TOP, country_map)),
                ("🎖️ MU record (1 gevecht)", self._section_mu_record(days, _OVERVIEW_TOP, country_id)),
                ("🗺️ Land record (1 gevecht)", self._section_land_record(days, _OVERVIEW_TOP, country_map, country_id)),
                ("📅 Wekelijks record — speler", self._section_wekelijks_speler(days, _OVERVIEW_TOP, country_id)),
                ("📅 Wekelijks record — MU", self._section_wekelijks_mu(days, _OVERVIEW_TOP, country_id)),
                ("📅 Wekelijks record — land", self._section_wekelijks_land(days, _OVERVIEW_TOP, country_map, country_id)),
                ("⚙️ Productie bonus", self._section_productie_bonus(_OVERVIEW_TOP)),
                ("👥 Actieve bevolking", self._section_bevolking(_OVERVIEW_TOP)),
                ("🗾 Meeste regio's", self._section_regio_count(_OVERVIEW_TOP)),
                ("💬 Artikel tips", self._section_artikel_tip(days, _OVERVIEW_TOP, country_id)),
            ]
            for field_title, coro in sections:
                pages = await coro
                embed.add_field(
                    name=field_title,
                    value=pages[0],
                    inline=False,
                )
            embed.set_footer(text="/leaderboard type:... voor meer detail")
            embed.timestamp = datetime.now(timezone.utc)
            await ctx.send(embed=embed)
            return

        # ── Single type ───────────────────────────────────────────────
        type_titles = {
            "artikel_tip":     "💬 Top tippers — meeste artikel tips gegeven",
            "speler_schade":   "⚔️ Top spelers — totale schade",            "gevecht":         "💥 Top gevechten — meeste schade",
            "solo":            "🎯 Solo records — meeste schade in 1 gevecht",
            "mu":              "🛡️ Top MUs — totale schade",
            "mu_record":       "🎖️ MU records — meeste schade in 1 gevecht",
            "land":            "🌍 Top landen — totale schade",
            "land_record":     "🗺️ Land records — meeste schade in 1 gevecht",
            "wekelijks_speler": "📅 Wekelijks record — speler",
            "wekelijks_mu":    "📅 Wekelijks record — MU",
            "wekelijks_land":  "📅 Wekelijks record — land",
            "productie_bonus": "⚙️ Productie bonus",
            "bevolking":       "👥 Actieve bevolking",
            "regio_count":     "🗾 Meeste regio's",
            "vermogen":        "💰 Top spelers — vermogen (live)",
        }
        title = type_titles.get(soort, f"Leaderboard: {soort}")

        # ── DB sections with optional day/country filter ─────────────
        if soort == "artikel_tip":
            pages = await self._section_artikel_tip(days, limit, country_id)
            footer = "Dagelijks bijgewerkt via /peil artikelen"
        # ── Live / no-days sections ───────────────────────────────────
        elif soort == "vermogen":
            pages = await self._section_vermogen(limit)
            footer = "Live data van API · vermogen in CC · geen dagenfilter beschikbaar"
        elif soort == "productie_bonus":
            pages = await self._section_productie_bonus(limit)
            footer = "API-snapshot · geen dagenfilter beschikbaar"
        elif soort == "bevolking":
            pages = await self._section_bevolking(limit)
            footer = "Citizen refresh data · geen dagenfilter beschikbaar"
        elif soort == "regio_count":
            pages = await self._section_regio_count(limit)
            footer = "Live data van API · geen dagenfilter beschikbaar"
        # ── Time-filtered sections ────────────────────────────────────
        else:
            if soort == "gevecht":
                pages = await self._section_gevecht(days, limit, country_map, country_id)
            elif soort == "land":
                pages = await self._section_land(days, limit, country_map)
            elif soort == "solo":
                pages = await self._section_solo(days, limit, country_map, country_id)
            elif soort == "mu":
                pages = await self._section_mu(days, limit, country_id)
            elif soort == "mu_record":
                pages = await self._section_mu_record(days, limit, country_id)
            elif soort == "land_record":
                pages = await self._section_land_record(days, limit, country_map, country_id)
            elif soort == "wekelijks_speler":
                pages = await self._section_wekelijks_speler(days, limit, country_id)
            elif soort == "wekelijks_mu":
                pages = await self._section_wekelijks_mu(days, limit, country_id)
            elif soort == "wekelijks_land":
                pages = await self._section_wekelijks_land(days, limit, country_map, country_id)
            else:
                pages = await self._section_speler_schade(days, limit, country_id)
            footer = "Dagelijks bijgewerkt"

        total_pages = len(pages)
        for idx, page_content in enumerate(pages):
            page_label = f" ({idx + 1}/{total_pages})" if total_pages > 1 else ""
            embed = discord.Embed(
                title=f"🏆 {title}{days_suffix}{page_label}",
                description=page_content,
                colour=self._embed_colour(),
            )
            embed.set_footer(text=footer)
            embed.timestamp = datetime.now(timezone.utc)
            await ctx.send(embed=embed)


async def setup(bot) -> None:
    """Add the LeaderboardCog to the bot."""
    await bot.add_cog(LeaderboardCog(bot))
