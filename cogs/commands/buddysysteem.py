"""
/buddysysteem <speler1> <speler2> [bedrijven1] [bedrijven2]

Suggests an economic skill distribution for two players who work at each
other's companies ("buddy system").

Background
-----------
- Working at your OWN company uses the entrepreneurship + production skills.
- Working at SOMEONE ELSE's company uses the energy + production skills.
  This "energy_pp" value is what each player effectively produces for their
  buddy every day.
- /ecobuild gives each player an individually optimal split, but when the
  two players have different levels (and thus different total skill point
  budgets), their optimal energy_pp values differ — they can't "work the
  same amount" for each other.

This command picks a common target energy_pp (the midpoint between both
players' individually optimal energy_pp), then for each player finds the
energy+production combination whose energy_pp is closest to that target,
and spends the remaining skill points on entrepreneurship for their own
production.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from typing import Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, citizen_autocomplete
from cogs.commands.ecobuild import (
    _CUMUL_COST,
    _SKILL_VALUES,
    _daily_works_float,
    _optimize_eco_skills,
    _skill_table,
    _unwrap,
)

logger = logging.getLogger("discord_bot")


# ---------------------------------------------------------------------------
# Pure calculation helpers
# ---------------------------------------------------------------------------


def _companies_skill_level(n_companies: int) -> int:
    """Minimum Companies skill level needed to run *n_companies* companies."""
    if n_companies <= 0:
        return 0
    return next(
        (lvl for lvl in range(11) if _SKILL_VALUES["companies"][lvl] >= n_companies),
        10,
    )


def _energy_pp(energy_lvl: int, prod_lvl: int) -> float:
    """Daily production points delivered while working at someone else's company."""
    return _daily_works_float(_SKILL_VALUES["energy"][energy_lvl]) * _SKILL_VALUES["production"][prod_lvl]


def _ent_pp(ent_lvl: int, prod_lvl: int) -> float:
    """Daily production points from working at your own company."""
    return _daily_works_float(_SKILL_VALUES["entrepreneurship"][ent_lvl]) * _SKILL_VALUES["production"][prod_lvl]


def _build_candidates(eco_budget: int) -> list[dict]:
    """All feasible (energy, production) combinations within *eco_budget*.

    For each combination, entrepreneurship is set to the highest level the
    remaining skill points allow.
    """
    candidates: list[dict] = []
    for e_lvl in range(11):
        for p_lvl in range(11):
            cost = _CUMUL_COST[e_lvl] + _CUMUL_COST[p_lvl]
            if cost > eco_budget:
                continue
            remaining = eco_budget - cost
            ent_lvl = max(l for l in range(11) if _CUMUL_COST[l] <= remaining)
            candidates.append({
                "ent_lvl": ent_lvl,
                "energy_lvl": e_lvl,
                "prod_lvl": p_lvl,
                "energy_pp": _energy_pp(e_lvl, p_lvl),
                "ent_pp": _ent_pp(ent_lvl, p_lvl),
                "sp_used": cost + _CUMUL_COST[ent_lvl],
            })
    return candidates


def _pick_build(candidates: list[dict], target_energy_pp: float) -> dict:
    """Pick the candidate whose energy_pp is closest to *target_energy_pp*.

    Ties (e.g. multiple candidates exactly matching the target) are broken by
    maximising the player's own total daily production.
    """
    return min(
        candidates,
        key=lambda c: (abs(c["energy_pp"] - target_energy_pp), -(c["ent_pp"] + c["energy_pp"])),
    )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class BuddySysteemCog(CommandCogBase, name="buddysysteem"):
    """Buddy-system eco skill advisor (/buddysysteem)."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    async def _search_user(self, username: str) -> list[str]:
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return ids[:5]
        except Exception as exc:
            logger.warning("buddysysteem: search failed for %r: %s", username, exc)
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
            logger.warning("buddysysteem: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(self, query: str) -> tuple[Optional[str], Optional[dict]]:
        """Resolve *query* → (user_id, profile). See ecobuild._resolve_user for details."""
        s_low = query.lower().strip()

        db = self._db
        if db is not None:
            try:
                db_exact = await db.get_citizen_by_name_exact(query)
                if db_exact:
                    uid, _ = db_exact
                    profile = await self._get_user_profile(uid)
                    if profile is not None:
                        return uid, profile
            except Exception:
                pass

        user_ids = await self._search_user(query)

        if not user_ids:
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

        for uid, profile in candidates:
            if (profile.get("username") or "").lower().strip() == s_low:
                return uid, profile

        best_uid, best_profile, best_ratio = None, None, -1.0
        for uid, profile in candidates:
            ratio = difflib.SequenceMatcher(
                None, s_low, (profile.get("username") or "").lower().strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio, best_uid, best_profile = ratio, uid, profile
        return best_uid, best_profile

    async def _get_company_count(self, user_id: str) -> int:
        """Return the total number of companies owned by *user_id*."""
        count = 0
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
                logger.warning("buddysysteem: getCompanies failed: %s", exc)
                break
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            count += len(items)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(0)
        return count

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="buddysysteem",
        description="Toon de aanbevolen eco skill-verdeling voor twee spelers die bij elkaar werken.",
    )
    @app_commands.describe(
        speler1="WarEra username van speler 1",
        speler2="WarEra username van speler 2",
        bedrijven1="Aantal bedrijven van speler 1 (standaard: huidig aantal)",
        bedrijven2="Aantal bedrijven van speler 2 (standaard: huidig aantal)",
    )
    @app_commands.autocomplete(speler1=citizen_autocomplete, speler2=citizen_autocomplete)
    async def buddysysteem(
        self,
        interaction: discord.Interaction,
        speler1: str,
        speler2: str,
        bedrijven1: Optional[int] = None,
        bedrijven2: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not self._client or self._client.is_available is False:
            await self._send_api_offline(interaction)
            return

        (uid1, profile1), (uid2, profile2) = await asyncio.gather(
            self._resolve_user(speler1),
            self._resolve_user(speler2),
        )

        if uid1 is None or profile1 is None:
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(speler1)}** niet gevonden.",
                ephemeral=True,
            )
            return
        if uid2 is None or profile2 is None:
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(speler2)}** niet gevonden.",
                ephemeral=True,
            )
            return

        username1: str = profile1.get("username") or speler1
        username2: str = profile2.get("username") or speler2
        level1: int = int((profile1.get("leveling") or {}).get("level") or 1)
        level2: int = int((profile2.get("leveling") or {}).get("level") or 1)

        if bedrijven1 is None:
            bedrijven1 = await self._get_company_count(uid1)
        if bedrijven2 is None:
            bedrijven2 = await self._get_company_count(uid2)

        bedrijven1 = max(0, min(bedrijven1, 12))
        bedrijven2 = max(0, min(bedrijven2, 12))

        # ── SP budgets ────────────────────────────────────────────────
        total_sp1 = max(1, level1) * 4
        total_sp2 = max(1, level2) * 4

        comp_lvl1 = _companies_skill_level(bedrijven1)
        comp_lvl2 = _companies_skill_level(bedrijven2)

        eco_budget1 = max(0, total_sp1 - _CUMUL_COST[comp_lvl1])
        eco_budget2 = max(0, total_sp2 - _CUMUL_COST[comp_lvl2])

        # ── Individual optima (used to derive the shared target) ────────
        baseline1 = _optimize_eco_skills(eco_budget1, 0)
        baseline2 = _optimize_eco_skills(eco_budget2, 0)

        if baseline1 is None or baseline2 is None:
            await interaction.followup.send(
                "❌ Niet genoeg skillpoints beschikbaar voor een zinvolle verdeling.",
                ephemeral=True,
            )
            return

        target_energy_pp = (
            _energy_pp(baseline1[1], baseline1[2]) + _energy_pp(baseline2[1], baseline2[2])
        ) / 2

        # ── Pick balanced builds ────────────────────────────────────────
        candidates1 = _build_candidates(eco_budget1)
        candidates2 = _build_candidates(eco_budget2)

        build1 = _pick_build(candidates1, target_energy_pp)
        build2 = _pick_build(candidates2, target_energy_pp)

        # ── Build embed ───────────────────────────────────────────────
        color = self._embed_colour()
        embed = discord.Embed(
            title="🤝 Buddysysteem — economische skills",
            description=(
                f"**{discord.utils.escape_markdown(username1)}** (lvl {level1}) en "
                f"**{discord.utils.escape_markdown(username2)}** (lvl {level2})\n"
                f"Streefwaarde voor dit buddysysteem: **{target_energy_pp:.0f} PP/dag**."
            ),
            color=color,
        )

        for username, build, n_companies, comp_lvl, total_sp in (
            (username1, build1, bedrijven1, comp_lvl1, total_sp1),
            (username2, build2, bedrijven2, comp_lvl2, total_sp2),
        ):
            ent_v = _SKILL_VALUES["entrepreneurship"][build["ent_lvl"]]
            en_v = _SKILL_VALUES["energy"][build["energy_lvl"]]
            prod_v = _SKILL_VALUES["production"][build["prod_lvl"]]
            rows: list[tuple[str, str, str]] = [
                (f"Entrepreneurship ({ent_v})", str(build["ent_lvl"]), f"{build['ent_pp']:.0f}"),
                (f"Energy ({en_v})", str(build["energy_lvl"]), f"{build['energy_pp']:.0f}"),
                (f"Production ({prod_v})", str(build["prod_lvl"]), "—"),
                (f"Bedrijven ({n_companies})", str(comp_lvl), "—"),
            ]
            sp_info = f"SP gebruikt: {build['sp_used'] + _CUMUL_COST[comp_lvl]}/{total_sp}"
            embed.add_field(
                name=f"\U0001f464 {discord.utils.escape_markdown(username)}",
                value=_skill_table(rows, build["ent_pp"] + build["energy_pp"], sp_info)[:1024],
                inline=False,
            )

        gap = abs(build1["energy_pp"] - build2["energy_pp"])
        if gap >= 1:
            embed.add_field(
                name="⚠️ Verschil",
                value=(
                    f"{discord.utils.escape_markdown(username1)} werkt **{build1['energy_pp']:.0f}** PP/dag "
                    f"bij {discord.utils.escape_markdown(username2)}, terwijl "
                    f"{discord.utils.escape_markdown(username2)} **{build2['energy_pp']:.0f}** PP/dag "
                    f"bij {discord.utils.escape_markdown(username1)} werkt "
                    f"(verschil: {gap:.0f} PP/dag). Door het levelverschil is een perfecte balans niet mogelijk."
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "Energy + Production bepalen hoeveel je voor elkaar werkt  |  "
                "Entrepreneurship + Production bepalen je eigen productie  |  "
                "SP = skill points, PP/dag = productiepunten per dag"
            )
        )

        await interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    """Register the BuddySysteemCog."""
    await bot.add_cog(BuddySysteemCog(bot))
