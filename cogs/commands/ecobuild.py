"""
/ecobuild <speler?> — Optimal eco skill distribution for maximum daily production.

Shows:
- Current eco skill levels (Entrepreneurship, Energy, Production, Companies)
- Current daily production from manual work + automated engines (AE)
- Optimal SP distribution for max production (assuming full skill reset)

Formulas
--------
- Daily work actions (entrepreneurship): floor(0.24 × ent_value)
- Manual production per day:             daily_works × production_value
- AE production per day (per company):   engine_level × 24 (max level 7 = 168 pts/day)
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, citizen_autocomplete

logger = logging.getLogger("discord_bot")

# ---------------------------------------------------------------------------
# Skill data (from gameConfig.getGameConfig — hardcoded for performance)
# ---------------------------------------------------------------------------

# Value per skill level 0–10
_SKILL_VALUES: dict[str, list[int]] = {
    "entrepreneurship": [30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80],
    "energy":           [30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130],
    "production":       [10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40],
    "companies":        [2,  3,  4,  5,  6,  7,  8,  9,  10, 11, 12],
}

# Cumulative SP cost to reach level N (levels 0–10)
_CUMUL_COST: list[int] = [0, 1, 3, 6, 10, 15, 21, 28, 36, 45, 55]

_COST_PER_ACTION: int = 10        # energy/entrepreneurship consumed per work action
_REGEN_DIVISOR: int = 10          # bar regenerates value/10 per hour (10 %/hr)
_ENGINE_DAILY_PER_LEVEL: int = 24  # automated engine: 24 pts per level per day
_ENGINE_MAX_LEVEL: int = 7         # max upgradeable level


# ---------------------------------------------------------------------------
# Pure calculation helpers
# ---------------------------------------------------------------------------


def _daily_works(skill_value: int) -> int:
    """Integer floor of daily work actions (for display only)."""
    return math.floor(skill_value / _REGEN_DIVISOR * 24 / _COST_PER_ACTION)


def _daily_works_float(skill_value: int) -> float:
    """Non-floored daily work actions for accurate PP/day calculation.

    Formula: (value / 10) × 24 / 10  =  value × 0.24
    """
    return skill_value / _REGEN_DIVISOR * 24 / _COST_PER_ACTION


def _engine_cycle_daily(engine_level: int) -> int:
    """Automated engine production per day, capped at max level."""
    return _ENGINE_DAILY_PER_LEVEL * min(max(0, engine_level), _ENGINE_MAX_LEVEL)


def _daily_prod(ent_val: int, energy_val: int, prod_val: int, engine_daily_total: int) -> float:
    """Total daily production points: manual work (ent + energy bars) + all engines."""
    return (_daily_works_float(ent_val) + _daily_works_float(energy_val)) * prod_val + engine_daily_total


def _eco_sp_budget(profile: dict) -> int:
    """Total SP the player has ever earned (= full redistribution budget).

    Assumes the player resets ALL skills (including combat) and redistributes
    everything into eco skills.  Uses leveling.totalSkillPoints when available,
    falling back to spentSkillPoints + availableSkillPoints.
    """
    leveling = profile.get("leveling") or {}
    total = leveling.get("totalSkillPoints")
    if total is None:
        spent = leveling.get("spentSkillPoints", 0) or 0
        avail = leveling.get("availableSkillPoints", 0) or 0
        total = spent + avail
    return int(total)


def _skill_table(
    rows: list[tuple[str, str, str]],
    total_pp: float,
    sp_info: str = "",
) -> str:
    """Render a fixed-width table inside a Discord code block.

    rows: list of (skill_name, level_str, pp_str).
    sp_info: optional SP line appended below the total row.
    """
    W1, W2, W3 = 22, 3, 6
    sep = "\u2500" * (W1 + 2 + W2 + 2 + W3)
    header = f"{'Skill':<{W1}}  {'Lvl':>{W2}}  {'PP/dag':>{W3}}"
    lines = [header, sep]
    for name, lvl, pp in rows:
        lines.append(f"{name:<{W1}}  {lvl:>{W2}}  {pp:>{W3}}")
    lines.append(sep)
    lines.append(f"{'Totaal':<{W1}}  {'':>{W2}}  {f'{total_pp:.0f}':>{W3}}")
    if sp_info:
        lines.append(sp_info)
    return "```\n" + "\n".join(lines) + "\n```"


def _extract_skill(profile: dict, skill_name: str) -> tuple[int, int]:
    """Return (level, effective_value) for *skill_name* from a getUserLite response."""
    entry = (profile.get("skills") or {}).get(skill_name)
    if isinstance(entry, dict):
        lvl = max(0, entry.get("level", 0) or 0)
        val = entry.get("value") or _SKILL_VALUES[skill_name][min(lvl, 10)]
        return min(lvl, 10), int(val)
    return 0, _SKILL_VALUES[skill_name][0]


def _optimize_production_v2(
    eco_budget: int,
    company_ae_sorted: list[int],
) -> tuple[int, int, int, int, int, float] | None:
    """Find the best (ent_lvl, energy_lvl, prod_lvl, comp_lvl, n_companies, daily).

    Both entrepreneurship and energy bars contribute to manual work actions, so
    the optimizer allocates SP across all four eco skill axes plus companies.

    company_ae_sorted: per-company AE daily production, sorted descending
                       (best companies first — we always keep the top-N).
    """
    n_owned = len(company_ae_sorted)
    best: tuple[int, int, int, int, int, float] | None = None

    for n_active in range(n_owned + 1):
        min_comp_lvl = next(
            (lvl for lvl in range(11) if _SKILL_VALUES["companies"][lvl] >= max(n_active, 2)),
            10,
        )
        comp_sp = _CUMUL_COST[min_comp_lvl]
        if comp_sp > eco_budget:
            continue

        ae_total = sum(company_ae_sorted[:n_active])
        remaining = eco_budget - comp_sp

        for e_lvl in range(11):
            e_cost = _CUMUL_COST[e_lvl]
            if e_cost > remaining:
                break
            for en_lvl in range(11):
                en_cost = _CUMUL_COST[en_lvl]
                if e_cost + en_cost > remaining:
                    break
                for p_lvl in range(11):
                    if e_cost + en_cost + _CUMUL_COST[p_lvl] <= remaining:
                        daily = _daily_prod(
                            _SKILL_VALUES["entrepreneurship"][e_lvl],
                            _SKILL_VALUES["energy"][en_lvl],
                            _SKILL_VALUES["production"][p_lvl],
                            ae_total,
                        )
                        if best is None:
                            best = (e_lvl, en_lvl, p_lvl, min_comp_lvl, n_active, daily)
                        elif daily > best[5]:
                            best = (e_lvl, en_lvl, p_lvl, min_comp_lvl, n_active, daily)

    return best





def _unwrap(resp: object) -> object:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _unwrap_region_list(resp: object) -> list[dict]:
    """Unwrap an API response that should contain a list of region dicts."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        result = resp.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        for key in ("data", "items", "regions", "result"):
            v = resp.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class EcoBuildCog(CommandCogBase, name="ecobuild"):
    """Eco skill build optimizer (/ecobuild)."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def _search_user(self, username: str) -> list[str]:
        """Return up to 5 candidate user IDs matching *username*."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return ids[:5]
        except Exception as exc:
            logger.warning("ecobuild: search failed for %r: %s", username, exc)
            return []

    async def _get_user_profile(self, user_id: str) -> Optional[dict]:
        try:
            raw = await self._client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            data = _unwrap(raw)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("ecobuild: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(self, query: str) -> tuple[Optional[str], Optional[dict]]:
        """Resolve *query* → (user_id, profile) using API search + fuzzy DB fallback."""
        s_low = query.lower().strip()
        user_ids = await self._search_user(query)

        if not user_ids:
            db = self._db
            if db is not None:
                nl_country_id = self.config.get("nl_country_id") or self.config.get("country_id")
                try:
                    match = await db.fuzzy_citizen_by_name(query, country_id=nl_country_id)
                    if match:
                        uid, _ = match
                        profile = await self._get_user_profile(uid)
                        if profile is not None:
                            return uid, profile
                except Exception:
                    pass
            return None, None

        candidates: list[tuple[str, dict]] = []
        for uid in user_ids:
            profile = await self._get_user_profile(uid)
            if profile is not None:
                candidates.append((uid, profile))

        # Exact match
        for uid, profile in candidates:
            if (profile.get("username") or "").lower().strip() == s_low:
                return uid, profile

        # Best ratio match
        best_uid, best_profile, best_ratio = None, None, -1.0
        for uid, profile in candidates:
            ratio = difflib.SequenceMatcher(
                None, s_low, (profile.get("username") or "").lower().strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio, best_uid, best_profile = ratio, uid, profile
        return best_uid, best_profile

    async def _get_company_ids(self, user_id: str) -> list[str]:
        """Return all company IDs owned by *user_id* (paginated)."""
        ids: list[str] = []
        cursor: Optional[str] = None
        while True:
            payload: dict = {"userId": user_id, "perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.get(
                    "/company.getCompanies",
                    params={"input": json.dumps(payload)},
                )
                data = _unwrap(raw)
            except Exception as exc:
                logger.warning("ecobuild: getCompanies failed: %s", exc)
                break
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            ids.extend(str(i) for i in items if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(0)
        return ids

    async def _get_ae_bonuses(self, companies: list[dict]) -> dict[str, float]:
        """Return {company _id → total production bonus %} via region + country APIs.

        Strategy (most reliable, avoids the dynamic recommended-region list):
        1. Call region.getById for each unique region → deposit bonus + countryId.
        2. Call country.getCountryById for each unique country →
           rankings.countryProductionBonus.value  (= SR + ethics combined, stable).
        3. total bonus = depositBonus + countryProductionBonus.

        The recommended-region list is intentionally NOT used as primary source because
        it only returns the top-N regions and changes dynamically as territory control
        shifts — a company's region can appear and disappear between two API calls.
        """
        if not self._client:
            return {}

        # Build region_id → [company_ids] and region_id → item_code mappings
        region_cids: dict[str, list[str]] = {}
        region_item: dict[str, str] = {}
        for c in companies:
            cid = str(c.get("_id") or c.get("id") or "")
            item = c.get("itemCode") or ""
            region = c.get("region") or c.get("regionId") or c.get("region_id") or ""
            if isinstance(region, dict):
                region = str(region.get("_id") or region.get("id") or "")
            region = str(region)
            if cid and region:
                region_cids.setdefault(region, []).append(cid)
                region_item[region] = item

        bonus_map: dict[str, float] = {}
        # region_id → (deposit_pct, country_id)
        region_info: dict[str, tuple[float, str]] = {}

        async def _fetch_region_info(region_id: str, item_code: str) -> None:
            try:
                raw = await self._client.get(  # type: ignore[union-attr]
                    "/region.getById",
                    params={"input": json.dumps({"regionId": region_id})},
                )
                data = _unwrap(raw)
                if not isinstance(data, dict):
                    return
                deposit = data.get("deposit") or {}
                deposit_type = deposit.get("type", "")
                deposit_pct = float(deposit.get("bonusPercent") or 0)
                # Only count deposit if it matches the company's item type
                if deposit_type and item_code and deposit_type != item_code:
                    deposit_pct = 0.0
                raw_country = data.get("country")
                country_id = ""
                if isinstance(raw_country, dict):
                    country_id = str(raw_country.get("_id") or raw_country.get("id") or "")
                elif isinstance(raw_country, str):
                    country_id = raw_country
                region_info[region_id] = (deposit_pct, country_id)
            except Exception:
                logger.warning("ecobuild: region.getById failed for %s", region_id)

        await asyncio.gather(*[
            _fetch_region_info(rid, region_item[rid])
            for rid in region_cids
        ])

        # Fetch unique countries
        country_prod_bonus: dict[str, float] = {}
        unique_country_ids = {cid for _, cid in region_info.values() if cid}

        async def _fetch_country(country_id: str) -> None:
            try:
                raw = await self._client.get(  # type: ignore[union-attr]
                    "/country.getCountryById",
                    params={"input": json.dumps({"countryId": country_id})},
                )
                data = _unwrap(raw)
                if not isinstance(data, dict):
                    return
                rb = (data.get("rankings") or {}).get("countryProductionBonus")
                if isinstance(rb, dict):
                    val = float(rb.get("value") or 0)
                    country_prod_bonus[country_id] = val
                elif isinstance(rb, (int, float)):
                    country_prod_bonus[country_id] = float(rb)
            except Exception:
                logger.warning("ecobuild: country.getCountryById failed for %s", country_id)

        await asyncio.gather(*[_fetch_country(cid) for cid in unique_country_ids])

        # Combine: deposit + country production bonus per company
        for region_id, cids in region_cids.items():
            deposit_pct, country_id = region_info.get(region_id, (0.0, ""))
            country_pct = country_prod_bonus.get(country_id, 0.0)
            total = deposit_pct + country_pct
            for cid in cids:
                bonus_map[cid] = total

        return bonus_map

    async def _get_company_details(self, company_ids: list[str]) -> list[Optional[dict]]:
        """Return full company objects for *company_ids* via tRPC batch."""
        if not company_ids:
            return []
        inputs = [{"companyId": cid} for cid in company_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("company.getById", inputs),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("ecobuild: batch_get failed: %s", exc)
            return [None] * len(company_ids)
        out: list[Optional[dict]] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            out.append(data if isinstance(data, dict) else None)
        return out

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="ecobuild",
        description="Show optimal eco skill distribution for max daily production.",
    )
    @app_commands.describe(
        speler="WarEra username (leave empty for yourself)",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def ecobuild(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not self._client:
            await interaction.followup.send(
                "❌ API client niet geïnitialiseerd.", ephemeral=True
            )
            return

        query = speler or interaction.user.display_name
        user_id, profile = await self._resolve_user(query)

        if user_id is None or profile is None:
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(query)}** niet gevonden.",
                ephemeral=True,
            )
            return

        username: str = profile.get("username") or query
        avatar_url: str = profile.get("avatarUrl") or ""
        leveling: dict = profile.get("leveling") or {}

        # ── Extract current eco skills ────────────────────────────────────
        ent_lvl, ent_val = _extract_skill(profile, "entrepreneurship")
        energy_lvl, energy_val = _extract_skill(profile, "energy")
        prod_lvl, prod_val = _extract_skill(profile, "production")
        comp_lvl, _ = _extract_skill(profile, "companies")

        player_level: int = leveling.get("level", 0) or 0

        # ── Fetch owned companies ─────────────────────────────────────────
        company_ids = await self._get_company_ids(user_id)
        companies: list[dict] = []
        if company_ids:
            details = await self._get_company_details(company_ids)
            companies = [c for c in details if c is not None]

        # ── Calculate engine daily totals ─────────────────────────────────
        ae_bonuses = await self._get_ae_bonuses(companies)

        engine_daily_total = 0
        engine_info: list[tuple[str, str, int, int, float]] = []  # (name, item, eng_lvl, daily, bonus_pct)
        for c in companies:
            cid = str(c.get("_id") or c.get("id") or "")
            upgrades = c.get("activeUpgradeLevels") or {}
            eng_lvl = int(upgrades.get("automatedEngine") or 0)
            prod_bonus_pct = ae_bonuses.get(cid, 0.0)
            ae_base = _engine_cycle_daily(eng_lvl)
            ae_daily = round(ae_base * (1 + prod_bonus_pct / 100))
            engine_daily_total += ae_daily
            engine_info.append((
                c.get("name") or "?",
                c.get("itemCode") or "?",
                eng_lvl,
                ae_daily,
                prod_bonus_pct,
            ))

        # ── Cap active companies to current companies skill level ──────────
        # The companies skill value determines how many companies a player can run.
        # Companies above the cap are disabled by the game (lowest AE output first).
        comp_val = _SKILL_VALUES["companies"][comp_lvl]
        n_active = min(comp_val, len(companies))
        engine_info_by_daily = sorted(engine_info, key=lambda x: -x[3])
        engine_daily_total = sum(x[3] for x in engine_info_by_daily[:n_active])

        # ── Current daily production ──────────────────────────────────────
        current_ent_works_f = _daily_works_float(ent_val)
        current_energy_works_f = _daily_works_float(energy_val)
        current_manual = (current_ent_works_f + current_energy_works_f) * prod_val
        current_total = current_manual + engine_daily_total

        # ── Eco SP budget ─────────────────────────────────────────────────
        eco_budget = _eco_sp_budget(profile)

        # ── Run optimizer ─────────────────────────────────────────────────
        # Pass an extended list that includes virtual "average" companies so the
        # optimizer can also recommend buying more companies than currently owned.
        _MAX_COMPANIES = max(_SKILL_VALUES["companies"])  # = 12 at level 10
        company_ae_sorted = sorted([daily for _, _, _, daily, _ in engine_info], reverse=True)
        n_owned = len(companies)
        avg_ae = round(sum(company_ae_sorted) / n_owned) if n_owned else 0
        company_ae_extended = company_ae_sorted + [avg_ae] * (_MAX_COMPANIES - n_owned)
        best_build = _optimize_production_v2(eco_budget, company_ae_extended)

        # ── Build embed ───────────────────────────────────────────────────
        color = self._embed_colour()
        embed = discord.Embed(
            title=f"Eco Build — {discord.utils.escape_markdown(username)}",
            description=f"Level {player_level}",
            color=color,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        # ── Huidige eco skills ───────────────────────────────────────────
        cur_ent_pp = _daily_works_float(ent_val) * prod_val
        cur_en_pp = _daily_works_float(energy_val) * prod_val
        cur_sp_used = (
            _CUMUL_COST[ent_lvl] + _CUMUL_COST[energy_lvl]
            + _CUMUL_COST[prod_lvl] + _CUMUL_COST[comp_lvl]
        )
        cur_bedr_label = (
            f"Bedrijven ({n_active}/{len(companies)})"
            if n_active < len(companies)
            else f"Bedrijven ({n_active})"
        )
        cur_rows: list[tuple[str, str, str]] = [
            (f"Entrepreneurship ({ent_val})", str(ent_lvl), f"{cur_ent_pp:.0f}"),
            (f"Energy ({energy_val})",        str(energy_lvl), f"{cur_en_pp:.0f}"),
            (f"Production ({prod_val})",      str(prod_lvl), "\u2014"),
            (cur_bedr_label,                 str(comp_lvl), f"{engine_daily_total:.0f}"),
        ]
        employee_note = (
            "_Let op: SP besteed aan medewerkers wordt niet meegeteld. "
            "Medewerkers worden niet meegenomen in de berekening._"
        )
        embed.add_field(
            name="\U0001f4ca Huidige eco skills",
            value=_skill_table(cur_rows, current_total, f"{cur_sp_used}/{eco_budget} SP")
            + "\n" + employee_note,
            inline=False,
        )

        # ── Optimal build ──────────────────────────────────────────────
        if best_build is not None:
            b_ent, b_en, b_prod, b_comp_lvl, b_n_companies, b_daily = best_build
            b_ent_v = _SKILL_VALUES["entrepreneurship"][b_ent]
            b_en_v = _SKILL_VALUES["energy"][b_en]
            b_prod_v = _SKILL_VALUES["production"][b_prod]
            b_ent_pp = _daily_works_float(b_ent_v) * b_prod_v
            b_en_pp = _daily_works_float(b_en_v) * b_prod_v
            # Use the extended list so virtual (new) companies contribute to b_ae.
            b_ae = sum(company_ae_extended[:b_n_companies])
            sp_used = (
                _CUMUL_COST[b_ent] + _CUMUL_COST[b_en]
                + _CUMUL_COST[b_prod] + _CUMUL_COST[b_comp_lvl]
            )
            improvement = b_daily - current_total
            improvement_str = (
                f"+{improvement:.0f} PP/day vs huidig" if improvement > 0 else "al optimaal!"
            )
            n_new = b_n_companies - n_owned
            if n_new > 0:
                b_bedr_label = f"Bedrijven ({n_owned}+{n_new} nieuw)"
            elif b_n_companies < n_owned:
                b_bedr_label = f"Bedrijven ({b_n_companies}/{n_owned})"
            else:
                b_bedr_label = f"Bedrijven ({b_n_companies})"
            opt_rows: list[tuple[str, str, str]] = [
                (f"Entrepreneurship ({b_ent_v})", str(b_ent), f"{b_ent_pp:.0f}"),
                (f"Energy ({b_en_v})",            str(b_en), f"{b_en_pp:.0f}"),
                (f"Production ({b_prod_v})",      str(b_prod), "\u2014"),
                (b_bedr_label,                   str(b_comp_lvl), f"{b_ae:.0f}"),
            ]
            new_company_note = (
                f"\n_Advies: koop {n_new} nieuw bedrijf. "
                f"Schatting gebaseerd op gemiddelde AE van huidige bedrijven (~{avg_ae} PP/dag/bedrijf)._"
                if n_new == 1
                else (
                    f"\n_Advies: koop {n_new} nieuwe bedrijven. "
                    f"Schatting gebaseerd op gemiddelde AE van huidige bedrijven (~{avg_ae} PP/dag/bedrijf)._"
                    if n_new > 1
                    else ""
                )
            )
            embed.add_field(
                name=f"\u2699\ufe0f Optimal build  [{improvement_str}]",
                value=_skill_table(opt_rows, b_daily, f"{sp_used}/{eco_budget} SP")
                + new_company_note,
                inline=False,
            )

        embed.set_footer(
            text=(
                "SP = skill points  |  PP/dag = productie punten per dag"
            )
        )

        await interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    """Register the EcoBuildCog."""
    await bot.add_cog(EcoBuildCog(bot))
