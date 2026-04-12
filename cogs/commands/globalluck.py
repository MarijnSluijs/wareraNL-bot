"""
/globalluck — Global case-opening luck ranking across all countries.

- /globalluck               — top 5 luckiest + bottom 5 unluckiest worldwide
- /globalluck speler:naam   — full luck analysis + worldwide rank for a player
"""

from __future__ import annotations

import difflib
import json
import logging
import math as _luck_math
from typing import TYPE_CHECKING, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import citizen_autocomplete
from services.api_client import APIClient

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# ── Luck constants (mirrored from geluk.py) ───────────────────────────────────

EXPECTED_RATES: dict[str, float] = {
    "mythic": 0.0001,
    "legendary": 0.0004,
    "epic": 0.0085,
    "rare": 0.071,
    "uncommon": 0.30,
    "common": 0.62,
}
ELITE_EXPECTED_RATES: dict[str, float] = {
    "mythic": 0.005,
    "legendary": 0.025,
    "epic": 0.15,
    "rare": 0.32,
    "uncommon": 0.50,
    "common": 0.0,
}
RARITY_ORDER = ["mythic", "legendary", "epic", "rare", "uncommon", "common"]
RARITY_LABELS: dict[str, str] = {
    "mythic": "Mythic",
    "legendary": "Legendary",
    "epic": "Epic",
    "rare": "Rare",
    "uncommon": "Uncommon",
    "common": "Common",
}
_LUCK_WEIGHTS: dict[str, float] = {
    r: -_luck_math.log2(p) for r, p in EXPECTED_RATES.items()
}
_LUCK_WEIGHT_TOTAL: float = sum(_LUCK_WEIGHTS.values())
_ELITE_LUCK_WEIGHTS: dict[str, float] = {
    r: -_luck_math.log2(p) if p > 0 else 0.0
    for r, p in ELITE_EXPECTED_RATES.items()
}
_ELITE_LUCK_WEIGHT_TOTAL: float = sum(v for v in _ELITE_LUCK_WEIGHTS.values() if v > 0)


def _calc_luck_score(counts: dict[str, int], total: int) -> float:
    """Poisson z-score luck % (0 = average, positive = luckier)."""
    if total == 0:
        return 0.0
    score = 0.0
    for rarity, expected_rate in EXPECTED_RATES.items():
        expected_n = total * expected_rate
        if expected_n <= 0:
            continue
        deviation = (counts.get(rarity, 0) - expected_n) / _luck_math.sqrt(expected_n)
        score += _LUCK_WEIGHTS[rarity] * deviation
    return score / _LUCK_WEIGHT_TOTAL * 100.0


def _calc_elite_luck_score(counts: dict[str, int], total: int) -> float:
    """Poisson z-score luck % for elite case (case2) openings."""
    if total == 0 or _ELITE_LUCK_WEIGHT_TOTAL <= 0:
        return 0.0
    score = 0.0
    for rarity, expected_rate in ELITE_EXPECTED_RATES.items():
        if expected_rate <= 0:
            continue
        expected_n = total * expected_rate
        if expected_n <= 0:
            continue
        deviation = (counts.get(rarity, 0) - expected_n) / _luck_math.sqrt(expected_n)
        score += _ELITE_LUCK_WEIGHTS[rarity] * deviation
    return score / _ELITE_LUCK_WEIGHT_TOTAL * 100.0


_ANSI_RARITY: dict[str, str] = {
    "mythic": "\033[31m",
    "legendary": "\033[33m",
    "epic": "\033[35m",
    "rare": "\033[34m",
    "uncommon": "\033[32m",
    "common": "\033[90m",
}
_ANSI_RST = "\033[0m"


def _luck_indicator_overall(luck_pct: float) -> str:
    if luck_pct >= 50:
        return "🍀🍀"
    if luck_pct >= 15:
        return "🍀"
    if luck_pct >= -15:
        return "➖"
    if luck_pct >= -50:
        return "💀"
    return "💀💀"


def _luck_indicator_per_rarity(actual_n: int, expected_n: float) -> str:
    """Scale: 💀💀  💀  👎  ➖  👍  🍀  🍀🍀

    For expected_n in [0.5, 1.0): getting zero is slightly unlucky (👎).
    Below 0.5 expected the drop was unlikely anyway, so zero is neutral (➖).
    """
    if expected_n <= 0:
        return ""
    if expected_n < 1.0:
        if actual_n >= 1:
            return "🍀🍀" if expected_n < 0.5 else "🍀"
        return "👎" if expected_n >= 0.5 else "➖"
    ratio = actual_n / expected_n
    if ratio >= 1.5:
        return "🍀🍀"
    if ratio >= 1.2:
        return "🍀"
    if ratio >= 1.05:
        return "👍"
    if ratio >= 0.95:
        return "➖"
    if ratio >= 0.8:
        return "👎"
    if ratio >= 0.5:
        return "💀"
    return "💀💀"


def _build_luck_table(total: int, counts: dict[str, int], rates: dict[str, float] | None = None) -> str:
    effective_rates = rates if rates is not None else EXPECTED_RATES
    header = f"{'Rarity':<14} {'Exp':>6} {'Got':>5}  {'Your%':>6}  Luck"
    sep = "─" * len(header)
    rows = [header, sep]
    for rarity in RARITY_ORDER:
        rate = effective_rates.get(rarity, 0.0)
        expected_n = total * rate
        actual_n = counts.get(rarity, 0)
        actual_rate = actual_n / total if total > 0 else 0.0
        luck = _luck_indicator_per_rarity(actual_n, expected_n)
        label = RARITY_LABELS[rarity]
        color = _ANSI_RARITY[rarity]
        rows.append(
            f"{color}{label:<14}{_ANSI_RST} {expected_n:>6.1f} {actual_n:>5d}"
            f"  {actual_rate * 100:>5.2f}%  {luck}"
        )
    rows.append(sep)
    rows.append(f"{'Total':<14} {total:>6d} {sum(counts.values()):>5d}")
    return "\n".join(rows)


class GlobalLuck(commands.Cog, name="globalluck"):
    """Global case-opening luck ranking."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot
        self.config: dict = getattr(bot, "config", {}) or {}
        self._db = None
        self._country_name_cache: dict[str, str] = {}

    async def _get_db(self):
        if self._db is None:
            self._db = getattr(self.bot, "_ext_db", None)
        return self._db

    async def _get_client(self) -> Optional[APIClient]:
        return getattr(self.bot, "_ext_client", None)

    async def _get_country_names(self) -> dict[str, str]:
        """Build country_id → name map (cached per session)."""
        if self._country_name_cache:
            return self._country_name_cache
        client = await self._get_client()
        if not client:
            return {}
        try:
            resp = await client.get("/country.getAllCountries")
            data: list = []
            if isinstance(resp, dict):
                inner = resp.get("result", resp)
                data = inner.get("data", inner) if isinstance(inner, dict) else resp
            if isinstance(data, list):
                for c in data:
                    if isinstance(c, dict):
                        cid = c.get("_id") or c.get("id")
                        cname = c.get("name") or c.get("shortName")
                        if cid and cname:
                            self._country_name_cache[str(cid)] = str(cname)
        except Exception:
            logger.exception("globalluck: failed to fetch country names")
        return self._country_name_cache

    async def _get_item_rarities(self) -> dict[str, str]:
        """Return {item_code: rarity} from gameConfig (cached per cog instance)."""
        if hasattr(self, "_item_rarities_cache") and self._item_rarities_cache:
            return self._item_rarities_cache  # type: ignore[attr-defined]
        client = await self._get_client()
        if not client:
            return {}
        try:
            raw = await client.get("/gameConfig.getGameConfig", params={"input": "{}"})
            data = {}
            if isinstance(raw, dict):
                inner = raw.get("result", raw)
                data = inner.get("data", inner) if isinstance(inner, dict) else raw
            rarities = {
                code: item.get("rarity")
                for code, item in (data.get("items") or {}).items()
                if item.get("rarity")
            }
        except Exception:
            logger.exception("globalluck: failed to load item rarities")
            rarities = {}
        self._item_rarities_cache: dict[str, str] = rarities  # type: ignore[attr-defined]
        return rarities

    async def _fetch_live_luck(
        self,
        user_id: str,
        max_cases: Optional[int] = None,
    ) -> tuple[dict[str, int], dict[str, int]] | None:
        """Fetch and return (normal_counts, elite_counts) live from the API.

        If *max_cases* is given, stops after that many normal case
        openings have been collected (the X most recent normal cases).
        Returns None if the client is unavailable.
        """
        client = await self._get_client()
        if not client:
            return None
        item_rarities = await self._get_item_rarities()
        normal_counts: dict[str, int] = {r: 0 for r in RARITY_ORDER}
        elite_counts: dict[str, int] = {r: 0 for r in RARITY_ORDER}
        cursor = None
        limit_reached = False
        while True:
            payload: dict = {
                "userId": user_id,
                "transactionType": "openCase",
                "limit": 100,
            }
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await client.get(
                    "/transaction.getPaginatedTransactions",
                    params={"input": json.dumps(payload)},
                )
            except Exception:
                break
            if isinstance(raw, dict):
                inner = raw.get("result", raw)
                data = inner.get("data", inner) if isinstance(inner, dict) else raw
            else:
                data = {}
            if isinstance(data, dict):
                items_list = data.get("items") or data.get("transactions") or []
                cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                items_list = data
                cursor = None
            else:
                break
            for tx in items_list:
                if not isinstance(tx, dict):
                    continue
                opened_case = tx.get("itemCode", "")
                is_elite = item_rarities.get(opened_case) == "mythic"
                received = tx.get("item") or {}
                item_code = (
                    received.get("code") if isinstance(received, dict) else received
                ) or ""
                rarity = item_rarities.get(item_code, "common")
                if is_elite:
                    elite_counts[rarity] = elite_counts.get(rarity, 0) + 1
                else:
                    normal_counts[rarity] = normal_counts.get(rarity, 0) + 1
                    if max_cases is not None and sum(normal_counts.values()) >= max_cases:
                        limit_reached = True
                        break
            if not cursor or not items_list or limit_reached:
                break
        return normal_counts, elite_counts

    # ------------------------------------------------------------------ #
    # /globalluck                                                          #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="globalluck",
        description="Worldwide case-opening luck ranking — top 5, bottom 5, or look up a player",
    )
    @app_commands.describe(
        speler="Optional: search by player name for personal analysis + worldwide rank",
        aantal_cases="Optional: analyse only the X most recent case openings (player mode only)",
        type="Show only normal cases, elite cases, or combined (default: combined)",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def globalluck(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        aantal_cases: Optional[int] = None,
        type: Optional[Literal["normaal", "elite", "gecombineerd"]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        db = await self._get_db()
        if not db:
            await interaction.followup.send(
                "❌ Database niet beschikbaar.", ephemeral=True
            )
            return

        # Check if the ranking has been populated at all
        total_str = await db.get_poll_state("global_luck_ranking_total")
        total_ranked = int(total_str or 0)

        if total_ranked == 0:
            await interaction.followup.send(
                "⚠️ De globale gelukranking is nog niet gevuld. "
                "Wacht tot de globale geluk-sweep voltooid is (loopt elke 6 uur).",
                ephemeral=True,
            )
            return

        country_names = await self._get_country_names()

        def _cname(cid: str) -> str:
            return country_names.get(cid, cid[:8] + "…" if len(cid) > 8 else cid)

        if speler:
            await self._show_player(interaction, db, speler, total_ranked, _cname, max_cases=aantal_cases, weergave=type)
        else:
            await self._show_leaderboard(interaction, db, total_ranked, _cname)

    # ------------------------------------------------------------------ #
    # Leaderboard view (top 5 + bottom 5)                                 #
    # ------------------------------------------------------------------ #

    async def _show_leaderboard(
        self,
        interaction: discord.Interaction,
        db,
        total_ranked: int,
        _cname,
    ) -> None:
        top5 = await db.get_global_luck_ranking(limit=5, order="DESC")
        bot5 = await db.get_global_luck_ranking(limit=5, order="ASC")
        # bot5 comes back in ASC order (worst first) — reverse for display
        bot5 = list(reversed(bot5))

        updated_at = (
            (top5[0].get("updated_at") or "")[:16].replace("T", " ") if top5 else "?"
        )

        embed = discord.Embed(
            title="🌍 Worldwide Luck Ranking",
            description=(
                f"Top 5 luckiest & unluckiest players worldwide\n"
                f"_{total_ranked:,} players scored_"
            ),
            color=discord.Color.gold(),
        )

        def _row(rank: int, entry: dict) -> str:
            name = (entry.get("citizen_name") or "?")[:13]
            score = entry["luck_score"]
            opens = entry["opens_count"]
            country = _cname(entry.get("country_id", ""))[:6]
            sign = "+" if score >= 0 else ""
            ind = _luck_indicator_overall(score)
            return f"#{rank:<4} {name:<13} {sign}{score:>7.1f}%  {ind}  {opens:>5,} {country:<6}"

        header = (
            f"{'rank':<5} {'name':<13} {'score':>8}   {'luck':<4}  {'cases':>5} ctry"
        )
        sep = "─" * 50

        # Top 5
        top_lines = [header, sep]
        for i, entry in enumerate(top5, start=1):
            top_lines.append(_row(i, entry))
        embed.add_field(
            name="🍀 Top 5 — Luckiest players",
            value="```\n" + "\n".join(top_lines) + "\n```",
            inline=False,
        )

        # Bottom 5 (show their actual rank)
        bot_lines = [header, sep]
        for i, entry in enumerate(reversed(bot5), start=0):
            rank = total_ranked - i
            bot_lines.append(_row(rank, entry))
        embed.add_field(
            name="💀 Bottom 5 — Unluckiest players",
            value="```\n" + "\n".join(bot_lines) + "\n```",
            inline=False,
        )

        embed.set_footer(
            text=(
                f"Odds: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1%  "
                f"•  Min. 20 cases required  •  Updated: {updated_at} UTC  "
                f"•  Use /globalluck speler:name for player analysis"
            )
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Player detail view                                                   #
    # ------------------------------------------------------------------ #

    async def _show_player(
        self,
        interaction: discord.Interaction,
        db,
        speler: str,
        total_ranked: int,
        _cname,
        max_cases: Optional[int] = None,
        weergave: Optional[Literal["normaal", "elite", "gecombineerd"]] = None,
    ) -> None:
        # Search by name (DB LIKE search, then fuzzy-match)
        candidates = await db.search_global_luck_by_name(speler)

        if not candidates:
            await interaction.followup.send(
                f"❌ No player found matching **{discord.utils.escape_markdown(speler)}** "
                f"in the global ranking.\n"
                f"Check the spelling or wait until the ranking is updated.",
                ephemeral=True,
            )
            return

        # Exact match first, then best fuzzy match
        s_low = speler.lower().strip()
        target = next(
            (
                c
                for c in candidates
                if (c.get("citizen_name") or "").lower().strip() == s_low
            ),
            None,
        )
        if target is None:
            best_ratio = -1.0
            for c in candidates:
                ratio = difflib.SequenceMatcher(
                    None, s_low, (c.get("citizen_name") or "").lower().strip()
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    target = c

        if target is None:
            await interaction.followup.send(
                f"❌ No result for **{discord.utils.escape_markdown(speler)}**.",
                ephemeral=True,
            )
            return

        username = target.get("citizen_name") or target.get("user_id") or "?"
        user_id = target["user_id"]
        country_id = target.get("country_id", "")
        rank_updated_at = (target.get("updated_at") or "")[:16].replace("T", " ")

        rank, _total = await db.get_global_luck_rank(user_id)
        elite_rank, elite_rank_total = await db.get_global_luck_rank_elite(user_id)
        combined_rank, _combined_total = await db.get_global_luck_rank_combined(user_id)
        rank_str = f"#{rank:,}" if rank is not None else "?"
        elite_rank_str = f"#{elite_rank:,}" if elite_rank is not None else "?"
        combined_rank_str = f"#{combined_rank:,}" if combined_rank is not None else "?"

        # Fetch live case data (same query as /geluk) so the analysis is always
        # up-to-date, regardless of when the last global sweep ran.
        live = await self._fetch_live_luck(user_id, max_cases=max_cases)

        if live is not None:
            normal_counts, elite_counts = live
            opens = sum(normal_counts.values())
            luck_score = _calc_luck_score(normal_counts, opens)
            analysis_note = (
                f"🔴 Live ({opens:,} most recent)"
                if max_cases is not None
                else "🔴 Live"
            )
        else:
            # Fall back to cached data
            counts_raw = target.get("rarity_json")
            normal_counts = json.loads(counts_raw) if counts_raw else {}
            elite_raw = target.get("elite_rarity_json")
            elite_counts = json.loads(elite_raw) if elite_raw else {}
            opens = target["opens_count"]
            luck_score = target["luck_score"]
            analysis_note = f"Cache {rank_updated_at} UTC"

        sign = "+" if luck_score >= 0 else ""
        ind = _luck_indicator_overall(luck_score)

        # Compute live elite luck score if available (for combined display)
        elite_opens = sum(elite_counts.values()) if elite_counts else 0
        live_elite_luck: float | None = None
        if elite_opens > 0:
            live_elite_luck = _calc_elite_luck_score(elite_counts, elite_opens)

        def _combined_score_local() -> float:
            if live_elite_luck is not None:
                return (luck_score + live_elite_luck) / 2.0
            return luck_score

        embed = discord.Embed(
            title=f"🌍 Worldwide luck — {username}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Score",
            value=f"**{sign}{luck_score:.1f}%** {ind}",
            inline=True,
        )
        embed.add_field(
            name="Combined rank",
            value=(
                f"**{combined_rank_str}** / {total_ranked:,}"
                + ("\n_based on all cases_" if max_cases is not None else "")
            ),
            inline=True,
        )
        embed.add_field(
            name="Country",
            value=_cname(country_id),
            inline=True,
        )
        embed.add_field(
            name="Cases opened",
            value=f"{opens:,}" + (f" _(most recent {max_cases:,})_" if max_cases is not None and live is not None else ""),
            inline=True,
        )

        if opens > 0 and normal_counts and weergave != "elite":
            table = _build_luck_table(opens, normal_counts)
            normal_rank_note = f"\nNormal cases rank: **{rank_str}** / {total_ranked:,}" if rank is not None else ""
            embed.add_field(
                name="🎲 Case luck",
                value=f"```ansi\n{table}\n```{normal_rank_note}",
                inline=False,
            )

        if elite_opens > 0 and weergave != "normaal":
            elite_table = _build_luck_table(elite_opens, elite_counts, ELITE_EXPECTED_RATES)
            elite_rank_note = f"\nElite cases rank: **{elite_rank_str}** / {elite_rank_total:,}" if elite_rank is not None else ""
            embed.add_field(
                name="💎 Elite Case luck",
                value=f"```ansi\n{elite_table}\n```{elite_rank_note}",
                inline=False,
            )

        # ── Worldwide leaderboard ──
        _MIN_NORMAL = 20
        _MIN_ELITE = 5
        try:
            def _row_lb(rn: int, entry: dict, score_val: float, highlight: bool = False) -> str:
                name = (entry.get("citizen_name") or "?")[:13]
                c_opens = entry.get("opens_count") or 0
                country = _cname(entry.get("country_id", ""))[:6]
                csign = "+" if score_val >= 0 else ""
                cind = _luck_indicator_overall(score_val)
                marker = " ◄" if highlight else ""
                return f"#{rn:<4} {name:<13} {csign}{score_val:>7.1f}%  {cind}  {c_opens:>5,} {country:<6}{marker}"

            lb_header = f"{'rank':<5} {'name':<13} {'score':>8}   {'luck':<4}  {'cases':>5} ctry"
            lb_sep = "─" * 50

            if weergave == "normaal":
                # Normal-only worldwide leaderboard
                top5_n = await db.get_global_luck_ranking(limit=5, order="DESC")
                bot5_n = await db.get_global_luck_ranking(limit=5, order="ASC")
                bot5_n = list(reversed(bot5_n))
                if top5_n:
                    is_in_top = any(e["user_id"] == user_id for e in top5_n)
                    is_in_bot = any(e["user_id"] == user_id for e in bot5_n)
                    top_lines = [lb_header, lb_sep]
                    for i, entry in enumerate(top5_n, start=1):
                        top_lines.append(_row_lb(i, entry, entry["luck_score"], highlight=(entry["user_id"] == user_id)))
                    player_lines: list[str] = []
                    if not is_in_top and not is_in_bot and rank is not None:
                        player_entry = {"citizen_name": username, "luck_score": luck_score, "opens_count": opens, "country_id": country_id, "user_id": user_id}
                        player_lines = ["    • • •", _row_lb(rank, player_entry, luck_score, highlight=True)]
                    bot_lines = ["    • • •", lb_header, lb_sep]
                    for i, entry in enumerate(reversed(bot5_n), start=0):
                        actual_rank = total_ranked - i
                        bot_lines.append(_row_lb(actual_rank, entry, entry["luck_score"], highlight=(entry["user_id"] == user_id)))
                    lb_block = "```\n" + "\n".join(top_lines + player_lines + bot_lines) + "\n```"
                    if rank is not None:
                        rsign = "+" if luck_score >= 0 else ""
                        lb_title = f"🌍 World Luck Ranking (normal cases) — rank **{rank_str}/{total_ranked:,}** — **{rsign}{luck_score:.1f}%** {_luck_indicator_overall(luck_score)}"
                    else:
                        lb_title = f"🌍 World Luck Ranking (normal cases) — _{total_ranked:,} players, not ranked (min. {_MIN_NORMAL} cases)_"
                    embed.add_field(name=lb_title, value=lb_block, inline=False)

            elif weergave == "elite":
                # Elite-only worldwide leaderboard
                top5_e = await db.get_global_luck_ranking_elite(limit=5, order="DESC")
                bot5_e = await db.get_global_luck_ranking_elite(limit=5, order="ASC")
                bot5_e = list(reversed(bot5_e))
                elite_total_world = elite_rank_total
                if top5_e:
                    is_in_top = any(e["user_id"] == user_id for e in top5_e)
                    is_in_bot = any(e["user_id"] == user_id for e in bot5_e)
                    top_lines = [lb_header, lb_sep]
                    for i, entry in enumerate(top5_e, start=1):
                        top_lines.append(_row_lb(i, entry, entry["elite_luck_score"], highlight=(entry["user_id"] == user_id)))
                    player_lines = []
                    if not is_in_top and not is_in_bot and elite_rank is not None:
                        esc = live_elite_luck if live_elite_luck is not None else (target.get("elite_luck_score") or 0.0)
                        player_entry = {"citizen_name": username, "luck_score": luck_score, "elite_luck_score": esc, "opens_count": elite_opens, "country_id": country_id, "user_id": user_id}
                        player_lines = ["    • • •", _row_lb(elite_rank, player_entry, esc, highlight=True)]
                    bot_lines = ["    • • •", lb_header, lb_sep]
                    for i, entry in enumerate(reversed(bot5_e), start=0):
                        actual_rank = elite_total_world - i
                        bot_lines.append(_row_lb(actual_rank, entry, entry["elite_luck_score"], highlight=(entry["user_id"] == user_id)))
                    lb_block = "```\n" + "\n".join(top_lines + player_lines + bot_lines) + "\n```"
                    esc_player = live_elite_luck if live_elite_luck is not None else (target.get("elite_luck_score") or 0.0)
                    if elite_rank is not None:
                        esign = "+" if esc_player >= 0 else ""
                        lb_title = f"🌍 World Luck Ranking (elite cases) — rank **{elite_rank_str}/{elite_total_world:,}** — **{esign}{esc_player:.1f}%** {_luck_indicator_overall(esc_player)}"
                    else:
                        lb_title = f"🌍 World Luck Ranking (elite cases) — _{elite_total_world:,} players, not ranked (min. {_MIN_ELITE} elite cases)_"
                    embed.add_field(name=lb_title, value=lb_block, inline=False)
                else:
                    embed.add_field(name="🌍 World Luck Ranking (elite cases)", value="_No elite data available._", inline=False)

            else:
                # Combined worldwide leaderboard (default)
                top5_comb = await db.get_global_luck_ranking_combined(limit=5, order="DESC")
                bot5_comb = await db.get_global_luck_ranking_combined(limit=5, order="ASC")
                bot5_comb = list(reversed(bot5_comb))

                if top5_comb:
                    def _comb_score(entry: dict) -> float:
                        ls = entry.get("luck_score", 0.0) or 0.0
                        es = entry.get("elite_luck_score")
                        if es is not None:
                            return (ls + es) / 2.0
                        return ls

                    is_in_top5 = any(e["user_id"] == user_id for e in top5_comb)
                    is_in_bot5 = any(e["user_id"] == user_id for e in bot5_comb)

                    top_lines = [lb_header, lb_sep]
                    for i, entry in enumerate(top5_comb, start=1):
                        score = entry.get("combined_score") if entry.get("combined_score") is not None else _comb_score(entry)
                        top_lines.append(_row_lb(i, entry, score, highlight=(entry["user_id"] == user_id)))

                    player_lines = []
                    if not is_in_top5 and not is_in_bot5 and combined_rank is not None:
                        player_entry = {
                            "citizen_name": username,
                            "luck_score": luck_score,
                            "elite_luck_score": live_elite_luck,
                            "combined_score": _combined_score_local(),
                            "opens_count": opens,
                            "country_id": country_id,
                            "user_id": user_id,
                        }
                        player_lines = ["    • • •", _row_lb(combined_rank, player_entry, _combined_score_local(), highlight=True)]

                    bot_lines = ["    • • •", lb_header, lb_sep]
                    for i, entry in enumerate(reversed(bot5_comb), start=0):
                        score = entry.get("combined_score") if entry.get("combined_score") is not None else _comb_score(entry)
                        bot_lines.append(_row_lb(total_ranked - i, entry, score, highlight=(entry["user_id"] == user_id)))

                    lb_block = "```\n" + "\n".join(top_lines + player_lines + bot_lines) + "\n```"

                    lb_rank_info = f"rank **{combined_rank_str}/{total_ranked:,}** — " if combined_rank is not None else ""
                    cscore = _combined_score_local()
                    csign2 = "+" if cscore >= 0 else ""
                    lb_title = (
                        f"🌍 World Luck Ranking (combined: cases + elite cases) — "
                        f"{lb_rank_info}**{csign2}{cscore:.1f}%** {_luck_indicator_overall(cscore)}"
                    )
                    embed.add_field(name=lb_title, value=lb_block, inline=False)
        except Exception:
            logger.exception("globalluck: failed to build leaderboard for player view")

        embed.set_footer(
            text=(
                f"Odds: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1%  "
                f"Elite: mythic 0.5% • legendary 2.5% • epic 15% • rare 32% • uncommon 50%  •  "
                f"Min. 20 cases required  •  Analysis: {analysis_note}  "
                f"•  Rank updated: {rank_updated_at} UTC"
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: DiscordBot) -> None:
    """Add the GlobalLuck cog to the bot."""
    await bot.add_cog(GlobalLuck(bot))
