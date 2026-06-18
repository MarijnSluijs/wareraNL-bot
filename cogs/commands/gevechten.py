"""Slash command /gevechten — estimated time remaining for active battles.

Shows battles grouped by:
  1. NL is directly a belligerent
  2. NL has placed a country order on one side
  3. Allied battles
  4. (Optional) A specific battle supplied as ID or URL

Terrain rules
-------------
Each 2-minute tick, the side with the most damage gains terrain points.
Points per tick are determined by the total terrain already given out:
  <100   → +1 | 100-199 → +2 | 200-299 → +3 |
  300-399 → +4 | 400-499 → +5 | ≥500   → +6
A side wins a round at 300 terrain points.  A battle is won after
*roundsToWin* rounds (normally 2).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

_BATTLE_URL  = "https://app.warera.io/battle/{battle_id}"
_BATTLE_ID_RE = re.compile(r"[0-9a-f]{24}", re.IGNORECASE)
_TERRAIN_TO_WIN = 300
_TICK_SECONDS   = 120  # 2 minutes


# ── Terrain / time helpers ───────────────────────────────────────────────────

def _terrain_per_tick(total_terrain: int) -> int:
    """Points awarded to the winning side per tick based on total terrain this round."""
    if total_terrain < 100: return 1
    if total_terrain < 200: return 2
    if total_terrain < 300: return 3
    if total_terrain < 400: return 4
    if total_terrain < 500: return 5
    return 6


def _simulate_ticks_to_win(winner_pts: int, loser_pts: int) -> int:
    """Count ticks until *winner_pts* reaches _TERRAIN_TO_WIN (loser never gains)."""
    total = winner_pts + loser_pts
    ticks = 0
    while winner_pts < _TERRAIN_TO_WIN:
        pts = _terrain_per_tick(total)
        winner_pts += pts
        total += pts
        ticks += 1
    return ticks


def _estimate_battle(
    att_pts: int,
    def_pts: int,
    att_won: int,
    def_won: int,
    rounds_to_win: int,
) -> tuple[int, int]:
    """Return (current_round_ticks, total_battle_ticks).

    Assumes the currently-leading side wins every future tick and round.
    """
    if att_pts >= def_pts:
        winning_pts, losing_pts, winning_won = att_pts, def_pts, att_won
    else:
        winning_pts, losing_pts, winning_won = def_pts, att_pts, def_won

    rounds_still = max(rounds_to_win - winning_won, 1)
    current_ticks = _simulate_ticks_to_win(winning_pts, losing_pts)
    fresh_ticks   = _simulate_ticks_to_win(0, 0)         # a brand-new round
    total_ticks   = current_ticks + (rounds_still - 1) * fresh_ticks
    return current_ticks, total_ticks


def _fmt_seconds(total_seconds: float) -> str:
    minutes = round(total_seconds / 60)
    if minutes < 60:
        return f"~{minutes} min"
    h, m = divmod(minutes, 60)
    return f"~{h}u {m}min" if m else f"~{h}u"


def _ticks_to_seconds(ticks: int, next_tick_at: Optional[str]) -> float:
    """Convert tick count to wall-clock seconds, accounting for next-tick offset."""
    if ticks <= 0:
        return 0.0
    secs_until_next = _TICK_SECONDS  # default: assume halfway through current tick
    if next_tick_at:
        try:
            nta = datetime.fromisoformat(next_tick_at.replace("Z", "+00:00"))
            secs_until_next = max(0.0, (nta - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass
    return secs_until_next + (ticks - 1) * _TICK_SECONDS


# ── tRPC unwrap helper ───────────────────────────────────────────────────────

def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    inner = resp.get("result", resp)
    if isinstance(inner, dict):
        return inner.get("data", inner)
    return resp


# ── Battle field formatter ───────────────────────────────────────────────────

def _battle_field(
    b: dict,
    country_names: dict[str, str],
) -> tuple[str, str]:
    """Return (field_name, field_value) for an embed field describing one battle."""
    bid     = str(b.get("_id", ""))
    att_top = b.get("attacker") or {}
    dfn_top = b.get("defender") or {}
    att_cid = str(att_top.get("country") or "")
    def_cid = str(dfn_top.get("country") or "")

    def cname(cid: str) -> str:
        return country_names.get(cid, cid[:8] if cid else "?")

    att_name = cname(att_cid)
    def_name = cname(def_cid)

    cr      = b.get("currentRound") or {}
    cr_att  = cr.get("attacker") or {}
    cr_dfn  = cr.get("defender") or {}
    live    = cr.get("live") or {}

    att_pts      = int(cr_att.get("points") or 0)
    def_pts      = int(cr_dfn.get("points") or 0)
    att_won      = int(att_top.get("wonRoundsCount") or 0)
    def_won      = int(dfn_top.get("wonRoundsCount") or 0)
    rounds_to_win = int(b.get("roundsToWin") or 2)
    next_tick_at  = live.get("nextTickAt")
    round_num     = int(cr.get("number") or (att_won + def_won + 1))

    round_ticks, battle_ticks = _estimate_battle(
        att_pts, def_pts, att_won, def_won, rounds_to_win
    )

    round_secs  = _ticks_to_seconds(round_ticks, next_tick_at)
    battle_secs = _ticks_to_seconds(battle_ticks, next_tick_at)

    if att_pts >= def_pts:
        score_line = f"**{att_name}** {att_pts}–{def_pts} {def_name}"
    else:
        score_line = f"{att_name} {att_pts}–{def_pts} **{def_name}**"

    round_indicator = f"Ronde {round_num}"
    if att_won or def_won:
        round_indicator += f"  *(rondes: att {att_won} – def {def_won})*"

    field_name = f"{att_name} vs {def_name}"
    field_value = (
        f"{round_indicator}\n"
        f"{score_line}\n"
        f"Ronde klaar: **{_fmt_seconds(round_secs)}** "
        f"| Gevecht klaar: **{_fmt_seconds(battle_secs)}**\n"
        f"[Bekijk gevecht]({_BATTLE_URL.format(battle_id=bid)})"
    )
    return field_name, field_value


# ── Sort helper ─────────────────────────────────────────────────────────────

def _battle_sort_key(b: dict) -> float:
    """Return estimated total battle duration in seconds (for sorting shortest-first)."""
    att_top = b.get("attacker") or {}
    dfn_top = b.get("defender") or {}
    cr      = b.get("currentRound") or {}
    cr_att  = cr.get("attacker") or {}
    cr_dfn  = cr.get("defender") or {}
    live    = cr.get("live") or {}
    att_pts       = int(cr_att.get("points") or 0)
    def_pts       = int(cr_dfn.get("points") or 0)
    att_won       = int(att_top.get("wonRoundsCount") or 0)
    def_won       = int(dfn_top.get("wonRoundsCount") or 0)
    rounds_to_win = int(b.get("roundsToWin") or 2)
    _, battle_ticks = _estimate_battle(att_pts, def_pts, att_won, def_won, rounds_to_win)
    return _ticks_to_seconds(battle_ticks, live.get("nextTickAt"))


# ── Cog ─────────────────────────────────────────────────────────────────────

class GevechtenCog(CommandCogBase, name="gevechten"):
    """Slash command /gevechten — battle duration estimator."""

    def __init__(self, bot) -> None:
        self.bot = bot
    @commands.command(name="gevechten")
    async def gevechten(
        self,
        ctx: Context,
        gevecht: Optional[str] = None,
    ) -> None:
        if not self._client or self._client.is_available is False:
            await ctx.send(embed=self._api_offline_embed())
            return

        nl_id: str = str(self.config.get("nl_country_id") or "")

        # ── 1. Fetch all active battles ──────────────────────────────
        try:
            raw = await self._client.get(
                "/battle.getBattles",
                params={"input": json.dumps({"isActive": True, "limit": 100})},
            )
        except Exception as exc:
            logger.warning("gevechten: getBattles failed: %s", exc)
            await ctx.send("Kon gevechten niet ophalen.")
            return

        data = _unwrap(raw)
        active_battles: list[dict] = []
        if isinstance(data, dict):
            active_battles = [b for b in data.get("items", []) if isinstance(b, dict)]
        elif isinstance(data, list):
            active_battles = [b for b in data if isinstance(b, dict)]

        # ── 2. Fetch NL country for ally list ────────────────────────
        ally_ids: set[str] = set()
        if nl_id:
            try:
                c_raw  = await self._client.post(
                    "/country.getCountryById",
                    json={"countryId": nl_id},
                )
                c_data = _unwrap(c_raw)
                if isinstance(c_data, dict):
                    ally_ids = {str(a) for a in (c_data.get("allies") or []) if a}
            except Exception:
                pass

        # ── 3. Country name cache ─────────────────────────────────────
        country_names: dict[str, str] = {}
        try:
            cn_raw  = await self._client.get("/country.getAllCountries")
            cn_data = _unwrap(cn_raw)
            source  = cn_data if isinstance(cn_data, list) else (
                list(cn_data.values()) if isinstance(cn_data, dict) else []
            )
            for c in source:
                if isinstance(c, dict):
                    cid   = str(c.get("_id") or "")
                    cname = c.get("name") or c.get("shortName") or cid
                    if cid:
                        country_names[cid] = cname
        except Exception:
            pass

        # ── 4. Categorise and sort active battles ─────────────────────────────
        nl_battles:    list[dict] = []
        order_battles: list[dict] = []
        ally_battles:  list[dict] = []

        for b in active_battles:
            att     = b.get("attacker") or {}
            dfn     = b.get("defender") or {}
            att_cid = str(att.get("country") or "")
            def_cid = str(dfn.get("country") or "")
            att_ord = [str(x) for x in (att.get("countryOrders") or [])]
            def_ord = [str(x) for x in (dfn.get("countryOrders") or [])]

            if nl_id and (att_cid == nl_id or def_cid == nl_id):
                nl_battles.append(b)
            elif nl_id and (nl_id in att_ord or nl_id in def_ord):
                order_battles.append(b)
            elif att_cid in ally_ids or def_cid in ally_ids:
                ally_battles.append(b)

        # Sort each group by total battle duration remaining (shortest first)
        nl_battles.sort(key=_battle_sort_key)
        order_battles.sort(key=_battle_sort_key)
        ally_battles.sort(key=_battle_sort_key)

        # ── 5. Optional specific battle ──────────────────────────────
        specific_battle: dict | None = None
        if gevecht:
            m = _BATTLE_ID_RE.search(gevecht)
            target_id = m.group(0) if m else gevecht.strip()
            # Check fetched active battles first
            for b in active_battles:
                if str(b.get("_id", "")) == target_id:
                    specific_battle = b
                    break
            if specific_battle is None:
                # Fall back to live data endpoint
                try:
                    lv_raw  = await self._client.get(
                        "/battle.getLiveBattleData",
                        params={"input": json.dumps({"battleId": target_id})},
                    )
                    lv_data = _unwrap(lv_raw)
                    if isinstance(lv_data, dict):
                        specific_battle = lv_data.get("battle") or lv_data
                except Exception as exc:
                    logger.debug("gevechten: getLiveBattleData(%s): %s", target_id, exc)

        # ── 6. Build embeds ──────────────────────────────────────────
        color  = self._embed_colour()
        embeds: list[discord.Embed] = []

        def _add_section(title: str, battles: list[dict]) -> None:
            if not battles:
                return
            embed = discord.Embed(title=title, colour=color)
            for b in battles:
                fname, fvalue = _battle_field(b, country_names)
                embed.add_field(name=fname, value=fvalue, inline=False)
                if len(embed.fields) >= 25:
                    embeds.append(embed)
                    embed = discord.Embed(title=f"{title} (vervolg)", colour=color)
            if embed.fields:
                embeds.append(embed)

        _add_section("Nederlandse Gevechten", nl_battles)
        _add_section("Gevechten met NL-order", order_battles)
        _add_section("Bondgenotengevechten", ally_battles)
        if specific_battle:
            sname, svalue = _battle_field(specific_battle, country_names)
            s_embed = discord.Embed(title="Gevraagd Gevecht", colour=color)
            s_embed.add_field(name=sname, value=svalue, inline=False)
            embeds.append(s_embed)

        if not embeds:
            await ctx.send("Geen actieve gevechten gevonden voor Nederland of zijn bondgenoten.")
            return

        embeds[-1].set_footer(text="Schatting op basis van huidige terreinstand")
        embeds[-1].timestamp = datetime.now(timezone.utc)

        for i in range(0, len(embeds), 10):
            await ctx.send(embeds=embeds[i : i + 10])


async def setup(bot) -> None:
    await bot.add_cog(GevechtenCog(bot))
