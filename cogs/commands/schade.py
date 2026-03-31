"""Slash command /schade — estimated 8h damage potential for a country, MU, or player.

Usage
-----
/schade               — analyse the configured home country (nl_country_id)
/schade land:NL       — analyse a specific country (autocomplete)
/schade mu:Alpha      — analyse the members of a specific MU (by name or ID)
"""

from __future__ import annotations

import json
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
)
from services.country_utils import country_id as cid_of
from services.country_utils import find_country
from services.damage_calc import (
    ALLIANCE_BONUS,
    COUNTRY_ORDER_BONUS,
    MU_HQ_BONUS,
    MU_ORDER_BONUS,
    PILL_BONUS,
    damage_for_level,
    equipment_tier_name,
    extract_rank_bonus,
    fmt_damage,
    player_breakdown,
)

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# Maximum level for which we compute damage (sanity cap)
_MAX_LEVEL = 100


def _unwrap(resp: object) -> object:
    """Unwrap a tRPC result envelope (result → data → payload)."""
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            return v.get("data", v)
    return resp


def _extract_level(obj: object) -> Optional[int]:
    """Extract player level from a getUserLite response dict."""
    if not isinstance(obj, dict):
        return None
    # Most common paths
    leveling = obj.get("leveling")
    if isinstance(leveling, dict):
        v = leveling.get("level")
        if isinstance(v, int):
            return v
    v = obj.get("level")
    if isinstance(v, int):
        return v
    rankings = obj.get("rankings")
    if isinstance(rankings, dict):
        ul = rankings.get("userLevel")
        if isinstance(ul, dict):
            v = ul.get("value")
            if isinstance(v, int):
                return v
    return None


def _extract_name(obj: object) -> str:
    if not isinstance(obj, dict):
        return "?"
    return obj.get("username") or obj.get("name") or "?"


class SchadeCog(CommandCogBase, name="schade"):
    """Damage potential calculator."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Autocomplete for MU names                                           #
    # ------------------------------------------------------------------ #

    async def _mu_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        testing = getattr(self.bot, "testing", False)
        mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
        try:
            with open(mus_json, encoding="utf-8") as f:
                mus_data = json.load(f)
        except Exception:
            return []

        current_lower = current.lower()
        choices: list[app_commands.Choice[str]] = []
        seen: set[str] = set()
        for embed in mus_data.get("embeds", []):
            if not isinstance(embed, dict):
                continue
            mu_id = str(embed.get("id") or "").strip()
            if not mu_id or mu_id in seen:
                continue
            seen.add(mu_id)
            mu_name = str(embed.get("name") or embed.get("title") or mu_id)
            if current_lower in mu_name.lower() or current_lower in mu_id.lower():
                choices.append(app_commands.Choice(name=mu_name, value=mu_name))
            if len(choices) >= 25:
                break
        return choices

    # ------------------------------------------------------------------ #
    # Player lookup helper                                                 #
    # ------------------------------------------------------------------ #

    async def _lookup_player(
        self, query: str
    ) -> tuple[Optional[str], Optional[str], Optional[int], float, Optional[int]]:
        """Resolve a player by name or ID.

        Returns (user_id, citizen_name, level, rank_bonus, rank_level).
        user_id is None when the player could not be found.
        """
        user_id: Optional[str] = None
        citizen_name: Optional[str] = None
        level: Optional[int] = None
        rank_bonus: float = 0.0
        rank_level: Optional[int] = None

        # ── 1. DB lookup ─────────────────────────────────────────────────
        if self._db:
            try:
                sql = (
                    "SELECT user_id, citizen_name, level "
                    "FROM citizen_levels "
                    "WHERE user_id = ? OR lower(citizen_name) = lower(?) "
                    "ORDER BY level DESC LIMIT 1"
                )
                async with self._db._conn.execute(sql, (query, query)) as cur:
                    row = await cur.fetchone()
                if row is None:
                    # Partial name fallback
                    sql2 = (
                        "SELECT user_id, citizen_name, level "
                        "FROM citizen_levels "
                        "WHERE lower(citizen_name) LIKE lower(?) "
                        "ORDER BY level DESC LIMIT 1"
                    )
                    async with self._db._conn.execute(sql2, (f"%{query}%",)) as cur:
                        row = await cur.fetchone()
                if row:
                    user_id, citizen_name, level = str(row[0]), row[1], row[2]
            except Exception as exc:
                logger.warning("schade player lookup: DB error: %s", exc)

        # ── 2. API getUserLite for rank (and possibly level/name) ─────────
        if self._client:
            lookup_id = user_id or (query if query.isdigit() else None)
            if lookup_id:
                try:
                    resp = await self._client.get(
                        "/user.getUserLite",
                        params={"input": json.dumps({"userId": lookup_id})},
                    )
                    data = _unwrap(resp)
                    if data:
                        rank_bonus, rank_level = extract_rank_bonus(data)
                        api_level = _extract_level(data)
                        if api_level and 1 <= api_level <= _MAX_LEVEL:
                            level = api_level
                        api_name = _extract_name(data)
                        if api_name and api_name != "?":
                            citizen_name = api_name
                        if not user_id:
                            user_id = lookup_id
                except Exception as exc:
                    logger.warning("schade player lookup: API error: %s", exc)

        return user_id, citizen_name or query, level, rank_bonus, rank_level

    # ------------------------------------------------------------------ #
    # DB helpers                                                           #
    # ------------------------------------------------------------------ #

    async def _get_country_players(self, country_id: str) -> list[tuple[str, int]]:
        """Return [(user_id, level)] for all citizens of *country_id*.

        Skips rows without a level.
        """
        rows: list[tuple[str, int]] = []
        sql = (
            "SELECT user_id, level FROM citizen_levels "
            "WHERE country_id = ? AND level IS NOT NULL"
        )
        async with self._db._conn.execute(sql, (country_id,)) as cur:
            async for row in cur:
                uid, level = row
                if level and 1 <= int(level) <= _MAX_LEVEL:
                    rows.append((str(uid), int(level)))
        return rows

    async def _get_mu_players(
        self, mu_name_or_id: str
    ) -> tuple[str, list[tuple[str, int]]]:
        """Resolve a MU by name or ID and return (mu_display_name, [(user_id, level)]).

        First tries the DB (citizen_levels.mu_name / mu_id).
        Falls back to API /mu.getById for fresh membership list.
        Returns ("", []) when the MU cannot be found.
        """
        # ── 1. Look up MU ID and name from DB ───────────────────────────
        mu_id: Optional[str] = None
        mu_display = mu_name_or_id
        rows: list[tuple[str, int]] = []

        try:
            sql_id = (
                "SELECT DISTINCT mu_id, mu_name FROM citizen_levels "
                "WHERE lower(mu_name) = lower(?) AND mu_id IS NOT NULL LIMIT 1"
            )
            async with self._db._conn.execute(sql_id, (mu_name_or_id,)) as cur:
                row = await cur.fetchone()
            if row:
                mu_id, mu_display = (
                    str(row[0]),
                    str(row[1]) if row[1] else mu_name_or_id,
                )
        except Exception as exc:
            logger.warning("schade: DB MU lookup failed: %s", exc)

        # If input looks numeric, treat it as an ID directly
        if mu_id is None and mu_name_or_id.isdigit():
            mu_id = mu_name_or_id

        # ── 1b. Resolve from mus.json if DB had no MU data ──────────────
        if mu_id is None:
            testing = getattr(self.bot, "testing", False)
            mus_json = "templates/mus.testing.json" if testing else "templates/mus.json"
            try:
                with open(mus_json, encoding="utf-8") as f:
                    mus_data = json.load(f)
                for embed in mus_data.get("embeds", []):
                    if not isinstance(embed, dict):
                        continue
                    eid = str(embed.get("id") or "").strip()
                    ename = str(embed.get("name") or embed.get("title") or "").strip()
                    if eid and eid.lower() == mu_name_or_id.lower():
                        mu_id = eid
                        if ename:
                            mu_display = ename
                        break
                    if ename and ename.lower() == mu_name_or_id.lower():
                        if eid:
                            mu_id = eid
                        mu_display = ename
                        break
            except Exception as exc:
                logger.warning("schade: mus.json MU lookup failed: %s", exc)

        if mu_id is None:
            # No mu_id found — try to get members directly by mu_name
            try:
                sql_by_name = (
                    "SELECT user_id, level FROM citizen_levels "
                    "WHERE lower(mu_name) = lower(?) AND level IS NOT NULL"
                )
                async with self._db._conn.execute(sql_by_name, (mu_name_or_id,)) as cur:
                    async for row in cur:
                        uid, level = row
                        if level and 1 <= int(level) <= _MAX_LEVEL:
                            rows.append((str(uid), int(level)))
            except Exception as exc:
                logger.warning("schade: DB MU name-only member lookup failed: %s", exc)
            return mu_display, rows

        # ── 2. Get member IDs from DB (fast path) ───────────────────────
        try:
            sql_members = (
                "SELECT user_id, level FROM citizen_levels "
                "WHERE mu_id = ? AND level IS NOT NULL"
            )
            async with self._db._conn.execute(sql_members, (mu_id,)) as cur:
                async for row in cur:
                    uid, level = row
                    if level and 1 <= int(level) <= _MAX_LEVEL:
                        rows.append((str(uid), int(level)))
        except Exception as exc:
            logger.warning("schade: DB MU member lookup failed: %s", exc)

        if rows:
            return mu_display, rows

        # ── 3. Fallback: fetch live from API ──────────────────────────────
        if not self._client:
            return mu_display, []
        try:
            resp = await self._client.get(
                "/mu.getById",
                params={"input": json.dumps({"muId": mu_id})},
            )
            # Unwrap tRPC envelope (same approach as citizen_cache)
            data: object = resp
            if isinstance(resp, dict):
                for key in ("result", "data"):
                    v = resp.get(key)
                    if isinstance(v, dict):
                        data = v.get("data", v)
                        break

            member_uids: list[str] = []
            if isinstance(data, dict):
                live_name = data.get("name") or data.get("title")
                if isinstance(live_name, str) and live_name:
                    mu_display = live_name
                for key in ("members", "citizenIds", "userIds", "users"):
                    v = data.get(key)
                    if isinstance(v, list):
                        for entry in v:
                            if isinstance(entry, str) and entry:
                                member_uids.append(entry)
                            elif isinstance(entry, dict):
                                uid = (
                                    entry.get("userId")
                                    or entry.get("_id")
                                    or entry.get("id")
                                    or entry.get("citizenId")
                                )
                                if uid:
                                    member_uids.append(str(uid))
                        break

            if member_uids and self._db and self._db._conn:
                # Fetch cached levels for these users in one query
                placeholders = ",".join("?" * len(member_uids))
                sql_lvl = (
                    f"SELECT user_id, level FROM citizen_levels "
                    f"WHERE user_id IN ({placeholders}) AND level IS NOT NULL"
                )
                level_map: dict[str, int] = {}
                try:
                    async with self._db._conn.execute(
                        sql_lvl, tuple(member_uids)
                    ) as cur:
                        async for row in cur:
                            level_map[str(row[0])] = int(row[1])
                except Exception as exc:
                    logger.warning(
                        "schade: level lookup for MU members failed: %s", exc
                    )

                for uid in member_uids:
                    # Fall back to level 1 — _analyse_players will fetch the
                    # real level via getUserLite and override this value.
                    lvl = level_map.get(uid, 1)
                    if 1 <= lvl <= _MAX_LEVEL:
                        rows.append((uid, lvl))
            else:
                # No DB — pass members with default level 1
                for uid in member_uids:
                    rows.append((uid, 1))

        except Exception as exc:
            logger.warning("schade: API MU getById failed: %s", exc)

        return mu_display, rows

    # ------------------------------------------------------------------ #
    # Core analysis                                                        #
    # ------------------------------------------------------------------ #

    async def _analyse_players(
        self,
        players: list[tuple[str, int]],
    ) -> list[dict]:
        """Fetch military rank via getUserLite and compute damage per player.

        Returns list of dicts:
          {user_id, level, citizen_name, rank_bonus, damage, rank_found}
        sorted by damage DESC.
        """
        if not players:
            return []

        results: list[dict] = []

        if self._client:
            inputs = [{"userId": uid} for uid, _ in players]
            try:
                responses = await self._client.batch_get(
                    "/user.getUserLite",
                    inputs,
                    batch_size=100,
                    chunk_sleep=0.5,
                )
            except Exception as exc:
                logger.warning("schade: batch getUserLite failed: %s", exc)
                responses = [None] * len(players)
        else:
            responses = [None] * len(players)

        for (uid, level), resp in zip(players, responses):
            data = _unwrap(resp) if isinstance(resp, dict) else resp
            rank_bonus, rank_level = extract_rank_bonus(data) if data else (0.0, None)
            rank_found = rank_bonus > 0.0
            citizen_name = _extract_name(data) if data else uid

            # If the API returned a different level, prefer it
            api_level = _extract_level(data) if data else None
            used_level = (
                api_level if api_level and 1 <= api_level <= _MAX_LEVEL else level
            )

            base_dmg = damage_for_level(used_level)
            damage = base_dmg * (1.0 + rank_bonus)

            results.append(
                {
                    "user_id": uid,
                    "level": used_level,
                    "citizen_name": citizen_name,
                    "rank_bonus": rank_bonus,
                    "rank_level": rank_level,
                    "damage": damage,
                    "rank_found": rank_found,
                }
            )

        results.sort(key=lambda d: d["damage"], reverse=True)
        return results

    # ------------------------------------------------------------------ #
    # Single-player breakdown embed                                        #
    # ------------------------------------------------------------------ #

    def _build_player_embed(
        self,
        name: str,
        bd: dict,
        rank_found: bool,
        rank_level: Optional[int] = None,
    ) -> discord.Embed:
        """Build a detailed single-player damage breakdown embed."""
        colour = self._embed_colour()
        level = bd["player_level"]
        tier = bd["equipment_tier"]
        s = bd["skills"]

        embed = discord.Embed(
            title=f"\u2694\ufe0f Damage \u2014 {name}  (lvl {level})",
            colour=colour,
        )

        # \u2500\u2500 Summary \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        embed.add_field(
            name="Total Damage (8h)",
            value=f"**{fmt_damage(bd['total_dmg'])}**",
            inline=True,
        )
        embed.add_field(name="Hits (8h)", value=f"{bd['hits']:.0f}", inline=True)
        embed.add_field(
            name="Avg. Dmg / Hit",
            value=fmt_damage(bd["e_per_hit"] * bd["total_mult"]),
            inline=True,
        )

        # \u2500\u2500 Skills & Equipment \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Columns: stat base value from skill | equipment bonus | combined total
        skill_pre = (0.50 + 0.05 * s.precision) * 100
        skill_cc = (0.10 + 0.05 * s.crit_chance) * 100
        skill_cd = (1.00 + 0.20 * s.crit_dmg) * 100
        skill_arm = 0.04 * s.armor * 100
        skill_dod = 0.04 * s.dodge * 100

        HDR = f"{'':14} {'Skill':>7}  {'Equip':>7}  {'Total':>7}"
        SEP = "\u2500" * len(HDR)

        def rowa(label: str, sk: float, eq: float, tot: float) -> str:
            return f"{label:<14} {sk:>7.0f}  {eq:>+7.0f}  {tot:>7.0f}"

        def rowp(
            label: str, sk_p: float, eq_p: float, tot_p: float, note: str = ""
        ) -> str:
            cap = (
                "  \u2190 cap"
                if (label in ("Armor", "Dodge") and tot_p >= 79.9)
                else ""
            )
            return (
                f"{label:<14} {sk_p:>6.1f}%  {eq_p:>+6.1f}%  {tot_p:>6.1f}%{cap}{note}"
            )

        def rownoeq(label: str, val: float) -> str:
            return f"{label:<14} {val:>7.0f}        \u2014  {val:>7.0f}"

        skills_text = "\n".join(
            [
                "```",
                HDR,
                SEP,
                rowa("Attack", bd["skill_base_atk"], bd["eq_attack"], bd["attack"]),
                rowp(
                    "Precision",
                    skill_pre,
                    bd["eq_precision"] * 100,
                    bd["precision"] * 100,
                ),
                rowp(
                    "Crit chance",
                    skill_cc,
                    bd["eq_crit_chance"] * 100,
                    bd["crit_chance"] * 100,
                ),
                rowp(
                    "Crit. dmg",
                    skill_cd,
                    bd["eq_crit_dmg"] * 100,
                    bd["crit_dmg_bonus"] * 100,
                ),
                rowp("Armor", skill_arm, bd["eq_armor"] * 100, bd["armor"] * 100),
                rowp("Dodge", skill_dod, bd["eq_dodge"] * 100, bd["dodge"] * 100),
                rownoeq("Max HP", bd["max_hp"]),
                rownoeq("Max hunger", bd["max_hunger"]),
                "```",
            ]
        )
        embed.add_field(
            name=f"Skills & Equipment  ({tier}, SP {bd['sp_used']}/{bd['sp_budget']})",
            value=skills_text,
            inline=False,
        )

        # \u2500\u2500 HP & Hits \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        arm_pct = f"{bd['armor'] * 100:.0f}%"
        dod_pct = f"{bd['dodge'] * 100:.0f}%"
        hp_text = "\n".join(
            [
                "```",
                f"{'Start HP:':<32} {bd['max_hp']:>6.0f}",
                f"{'+ HP regen (10%/h \u00d7 8h):':<32} {bd['hp_regen']:>6.0f}",
                f"{'+ Food (' + str(bd['hunger_start'] + bd['hunger_regen']) + '\u00d7 fish):':<32} {bd['food_hp']:>6.0f}",
                "\u2500" * 50,
                f"{'= Total HP:':<32} {bd['total_hp']:>6.0f}",
                f"{'Armor (' + arm_pct + ') \u2192 HP/landed hit:':<32} {bd['hp_per_landed']:>6.2f}",
                f"{'Dodge (' + dod_pct + ') \u2192 HP/action:':<32} {bd['hp_per_hit']:>6.2f}",
                "\u2500" * 50,
                f"{'Total actions (8h):':<32} {bd['hits']:>6.0f}",
                f"{'  Dodged (' + dod_pct + '):':<32} {bd['n_dodges']:>6.0f}",
                f"{'  Landed hits:':<32} {bd['n_landed']:>6.0f}",
                "```",
            ]
        )
        embed.add_field(name="HP & Hits (8h)", value=hp_text, inline=False)

        # \u2500\u2500 Hit probabilities \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        crit_mult = 1.0 + bd["crit_dmg_bonus"]
        prob_text = "\n".join(
            [
                "```",
                f"{'':16} {'chance':>7}  {'count':>5}  {'mult':>6}  {'dmg/hit':>8}",
                "\u2500" * 52,
                f"{'Miss:':<16} {bd['miss_rate'] * 100:>6.1f}%  {bd['n_misses']:>5.0f}  0.500\u00d7  {bd['dmg_miss']:>8.0f}",
                f"{'Hit, no crit:':<16} {bd['hit_no_crit_rate'] * 100:>6.1f}%  {bd['n_hits']:>5.0f}  1.000\u00d7  {bd['dmg_hit']:>8.0f}",
                f"{'Critical:':<16} {bd['hit_crit_rate'] * 100:>6.1f}%  {bd['n_crits']:>5.0f}  {crit_mult:.3f}\u00d7  {bd['dmg_crit']:>8.0f}",
                "\u2500" * 52,
                f"{'Total:':<16} {'100.0%':>7}  {bd['hits']:>5.0f}",
                "```",
            ]
        )
        embed.add_field(name="Hit Probabilities", value=prob_text, inline=False)

        # \u2500\u2500 Bonuses (multiplicative) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        pill_tick = "\u2705" if bd["pill_active"] else "\u274c (lvl < 15)"
        rank_label = (
            f"Rank (lvl {rank_level})" if rank_level is not None else "Military rank"
        )
        rank_note = "" if rank_found else "  (not found)"
        ammo_label = f"{bd['ammo_name']} (+{bd['ammo_bonus'] * 100:.0f}%)"
        bonus_lines = [
            "```",
            f"{'Country order':<22} \u00d7{1 + COUNTRY_ORDER_BONUS:.2f}",
            f"{'MU order':<22} \u00d7{1 + MU_ORDER_BONUS:.2f}",
            f"{'Alliance':<22} \u00d7{1 + ALLIANCE_BONUS:.2f}",
            f"{'MU HQ':<22} \u00d7{1 + MU_HQ_BONUS:.2f}",
            f"{'Pill (+60%)':<22} \u00d7{1 + PILL_BONUS:.2f}  {pill_tick}",
            f"{ammo_label:<22} \u00d7{1 + bd['ammo_bonus']:.2f}",
            f"{rank_label:<22} \u00d7{1 + bd['rank_bonus']:.2f}{rank_note}",
            "\u2500" * 34,
            f"{'Combined':<22} \u00d7{bd['total_mult']:.2f}",
            "```",
        ]
        embed.add_field(
            name=f"Bonuses  (combined \u00d7{bd['total_mult']:.2f})",
            value="\n".join(bonus_lines),
            inline=False,
        )

        embed.set_footer(
            text=(
                f"Equipment: {tier} tier (mid-range values)  \u00b7  "
                "Skills: optimal allocation  \u00b7  Bonuses: all maximum assumed"
            )
        )
        return embed

    # ------------------------------------------------------------------ #
    # Embed builders                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tier_breakdown(results: list[dict]) -> str:
        tiers: dict[str, int] = {}
        for r in results:
            t = equipment_tier_name(r["level"])
            tiers[t] = tiers.get(t, 0) + 1
        order = ["Mythic", "Legendary", "Epic", "Rare", "Uncommon"]
        lines = []
        for t in order:
            if t in tiers:
                lines.append(f"{t}: {tiers[t]}")
        return " | ".join(lines) if lines else "—"

    @staticmethod
    def _top_table_chunks(results: list[dict], n: int = 10) -> list[str]:
        """Build ranking table; split into 1024-char chunks for embed fields."""
        lines = [
            "```",
            f"{'#':>3} {'Naam':<20} {'Lvl':>4} {'Bonus':>6} {'Schade':>10}",
            "─" * 48,
        ]
        for i, r in enumerate(results[:n], 1):
            bonus_str = f"+{r['rank_bonus'] * 100:.0f}%" if r["rank_bonus"] > 0 else "—"
            lines.append(
                f"{i:>3} {r['citizen_name'][:20]:<20} {r['level']:>4} "
                f"{bonus_str:>6} {fmt_damage(r['damage']):>10}"
            )
        lines.append("```")
        body = "\n".join(lines)
        # Split into ≤1024-char chunks
        chunks: list[str] = []
        while len(body) > 1024:
            cut = body.rfind("\n", 0, 1024)
            if cut == -1:
                cut = 1024
            chunks.append(body[:cut])
            body = body[cut:].lstrip("\n")
        if body:
            chunks.append(body)
        return chunks

    def _build_embed(
        self,
        title: str,
        results: list[dict],
        label: str,
        top_n: int = 10,
    ) -> list[discord.Embed]:
        """Build one or more embeds (splits if many fields needed)."""
        total_dmg = sum(r["damage"] for r in results)
        avg_dmg = total_dmg / len(results) if results else 0.0
        n_no_rank = sum(1 for r in results if not r["rank_found"])

        colour = self._embed_colour()
        embed = discord.Embed(title=title, colour=colour)
        embed.add_field(
            name="Totaal (8u)", value=f"**{fmt_damage(total_dmg)}**", inline=True
        )
        embed.add_field(name="Gemiddeld (8u)", value=fmt_damage(avg_dmg), inline=True)
        embed.add_field(name="Spelers", value=str(len(results)), inline=True)
        embed.add_field(
            name="Niveau-verdeling (uitrusting tier)",
            value=self._tier_breakdown(results),
            inline=False,
        )

        chunks = self._top_table_chunks(results, n=top_n)
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name=f"Top {top_n} {label}"
                + (f" (deel {i + 1})" if len(chunks) > 1 else ""),
                value=chunk,
                inline=False,
            )

        # Assumptions footnote
        pill_note = "pil (+60% voor lvl≥15)"
        rank_note = (
            f" | {n_no_rank} speler(s) zonder rang-data (0% bonus)" if n_no_rank else ""
        )
        embed.set_footer(
            text=(
                f"Aannames (max): landsorde +{COUNTRY_ORDER_BONUS * 100:.0f}%, "
                f"MU-orde +{MU_ORDER_BONUS * 100:.0f}%, alliantie +{ALLIANCE_BONUS * 100:.0f}%, "
                f"MU HQ +{MU_HQ_BONUS * 100:.0f}%, {pill_note}, optimale skills"
                f"{rank_note}"
            )
        )

        # If embed is too large, split it (Discord limit 6000 chars)
        if embed.__len__() <= 6000:
            return [embed]

        # Fallback: remove top table and return just the summary
        embed2 = discord.Embed(title=title + " (samenvatting)", colour=colour)
        embed2.add_field(
            name="Totaal (8u)", value=f"**{fmt_damage(total_dmg)}**", inline=True
        )
        embed2.add_field(name="Gemiddeld (8u)", value=fmt_damage(avg_dmg), inline=True)
        embed2.add_field(name="Spelers", value=str(len(results)), inline=True)
        embed2.add_field(
            name="Niveau-verdeling",
            value=self._tier_breakdown(results),
            inline=False,
        )
        embed2.set_footer(text=embed.footer.text)
        return [embed2]

    # ------------------------------------------------------------------ #
    # /schade                                                              #
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="schade",
        description=(
            "Bereken het schadevermogen (8u) van een speler, MU of land. "
            "Zonder opties: Nederland."
        ),
    )
    @app_commands.describe(
        speler="Spelernaam of ID — uitgebreide breakdown voor één speler.",
        land="Landnaam — analyseer alle spelers van dit land.",
        mu="MU-naam of MU-ID — analyseer de leden van deze MU.",
        top="Aantal spelers in de top-tabel (standaard: 10).",
    )
    @app_commands.autocomplete(
        speler=citizen_autocomplete, land=country_autocomplete, mu=_mu_autocomplete
    )
    async def schade(
        self,
        ctx: Context,
        speler: str | None = None,
        land: str | None = None,
        mu: str | None = None,
        top: int = 10,
    ) -> None:
        """/schade — schadevermogen over 8 uur."""
        if not self._client:
            await ctx.send("API-client niet beschikbaar.")
            return

        n_modes = sum(x is not None for x in (speler, land, mu))
        if n_modes > 1:
            await ctx.send("Geef **speler**, **land** óf **mu** op, niet meerdere.")
            return

        top_n = max(1, min(top, 25))

        if hasattr(ctx, "defer"):
            await ctx.defer()

        # ── Single-player breakdown ───────────────────────────────────────
        if speler is not None:
            user_id, name, level, rank_bonus, rank_level = await self._lookup_player(
                speler
            )
            if level is None or level <= 0:
                await ctx.send(
                    f"Speler **{speler}** niet gevonden in de database of API."
                )
                return
            bd = player_breakdown(level, rank_bonus)
            rank_found = rank_bonus > 0.0
            embed = self._build_player_embed(
                name, bd, rank_found, rank_level=rank_level
            )
            await ctx.send(embed=embed)
            return

        if not self._db:
            await ctx.send("Database niet geïnitialiseerd.")
            return

        # ── Resolve target ────────────────────────────────────────────────
        if mu is not None:
            # MU mode
            mu_display, players = await self._get_mu_players(mu)
            if not players:
                await ctx.send(
                    f"Geen spelers gevonden voor MU **{mu}**. "
                    "Controleer de naam of wacht tot de cache is bijgewerkt."
                )
                return
            title = f"⚔️ Schade — MU: {mu_display}"
            label = "spelers"

        else:
            # Country mode (default to nl_country_id if no land given)
            if land is None:
                country_id = self.config.get("nl_country_id", "")
                country_name = "Nederland"
            else:
                country_list = await self._fetch_country_list(ctx)
                if not country_list:
                    return
                found = find_country(land, country_list)
                if not found:
                    await ctx.send(f"Land **{land}** niet gevonden.")
                    return
                country_id = cid_of(found)
                country_name = found.get("name") or land

            if not country_id:
                await ctx.send("Geen land-ID gevonden.")
                return

            players = await self._get_country_players(country_id)
            if not players:
                await ctx.send(
                    f"Geen spelers gevonden voor **{country_name}**. "
                    "Wacht tot de burgercache is bijgewerkt (`/peil`)."
                )
                return
            title = f"⚔️ Schade — {country_name}"
            label = "spelers"

        # ── Analyse ───────────────────────────────────────────────────────
        results = await self._analyse_players(players)
        if not results:
            await ctx.send("Schadevermogen kon niet worden berekend.")
            return

        # ── Send embeds ───────────────────────────────────────────────────
        embeds = self._build_embed(title, results, label, top_n=top_n)
        first = True
        for emb in embeds:
            if first:
                await ctx.send(embed=emb)
                first = False
            else:
                await ctx.send(embed=emb)


async def setup(bot: DiscordBot) -> None:
    await bot.add_cog(SchadeCog(bot))
