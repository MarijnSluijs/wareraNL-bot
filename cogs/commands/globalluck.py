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
from typing import TYPE_CHECKING, Optional

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


def _build_luck_table(total: int, counts: dict[str, int]) -> str:
    header = f"{'Rarity':<14} {'Exp':>6} {'Got':>5}  {'Your%':>6}  Luck"
    sep = "─" * len(header)
    rows = [header, sep]
    for rarity in RARITY_ORDER:
        rate = EXPECTED_RATES[rarity]
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
    ) -> tuple[dict[str, int], int] | None:
        """Fetch and return (rarity_counts, total_opens) live from the API.

        Mirrors the logic in global_luck.py._fetch_luck_data.
        Returns None if the client is unavailable.
        """
        client = await self._get_client()
        if not client:
            return None
        item_rarities = await self._get_item_rarities()
        counts: dict[str, int] = {r: 0 for r in RARITY_ORDER}
        cursor = None
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
                # Skip elite cases (same filter as geluk.py)
                if item_rarities.get(tx.get("itemCode", "")) == "mythic":
                    continue
                received = tx.get("item") or {}
                item_code = (
                    received.get("code") if isinstance(received, dict) else received
                ) or ""
                rarity = item_rarities.get(item_code, "common")
                counts[rarity] = counts.get(rarity, 0) + 1
            if not cursor or not items_list:
                break
        return counts, sum(counts.values())

    # ------------------------------------------------------------------ #
    # /globalluck                                                          #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="globalluck",
        description="Wereldwijde case-geluk ranking — top 5, bottom 5, of zoek een speler",
    )
    @app_commands.describe(
        speler="Optioneel: zoek op spelernaam voor persoonlijke analyse + wereldwijde rang",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def globalluck(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
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
            await self._show_player(interaction, db, speler, total_ranked, _cname)
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
            title="🌍 Wereldwijde Gelukranking",
            description=(
                f"Top 5 gelukkigste & ongelukkigste spelers wereldwijd\n"
                f"_{total_ranked:,} spelers gescoord_"
            ),
            color=discord.Color.gold(),
        )

        def _row(rank: int, entry: dict) -> str:
            name = (entry.get("citizen_name") or "?")[:18]
            score = entry["luck_score"]
            opens = entry["opens_count"]
            country = _cname(entry.get("country_id", ""))
            sign = "+" if score >= 0 else ""
            ind = _luck_indicator_overall(score)
            return f"#{rank:<4} {name:<18} {sign}{score:>7.1f}%  {ind}  {opens:>6,} ({country})"

        header = (
            f"{'rang':<5} {'naam':<18} {'score':>8}   {'geluk':<5}  {'cases':>6}  land"
        )
        sep = "─" * 72

        # Top 5
        top_lines = [header, sep]
        for i, entry in enumerate(top5, start=1):
            top_lines.append(_row(i, entry))
        embed.add_field(
            name="🍀 Top 5 — Gelukkigste spelers",
            value="```\n" + "\n".join(top_lines) + "\n```",
            inline=False,
        )

        # Bottom 5 (show their actual rank)
        bot_lines = [header, sep]
        for i, entry in enumerate(reversed(bot5), start=0):
            rank = total_ranked - i
            bot_lines.append(_row(rank, entry))
        embed.add_field(
            name="💀 Bottom 5 — Ongelukkigste spelers",
            value="```\n" + "\n".join(bot_lines) + "\n```",
            inline=False,
        )

        embed.set_footer(
            text=(
                f"Kansen: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1%  "
                f"•  Min. 20 cases vereist  •  Bijgewerkt: {updated_at} UTC  "
                f"•  Gebruik /globalluck speler:naam voor analyse"
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
    ) -> None:
        # Search by name (DB LIKE search, then fuzzy-match)
        candidates = await db.search_global_luck_by_name(speler)

        if not candidates:
            await interaction.followup.send(
                f"❌ Geen speler gevonden die overeenkomt met **{discord.utils.escape_markdown(speler)}** "
                f"in de globale ranking.\n"
                f"Zorg dat de spelling klopt of wacht tot de ranking bijgewerkt is.",
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
                f"❌ Geen resultaat voor **{discord.utils.escape_markdown(speler)}**.",
                ephemeral=True,
            )
            return

        username = target.get("citizen_name") or target.get("user_id") or "?"
        user_id = target["user_id"]
        country_id = target.get("country_id", "")
        rank_updated_at = (target.get("updated_at") or "")[:16].replace("T", " ")

        rank, _total = await db.get_global_luck_rank(user_id)
        rank_str = f"#{rank:,}" if rank is not None else "?"

        # Fetch live case data (same query as /geluk) so the analysis is always
        # up-to-date, regardless of when the last global sweep ran.
        live = await self._fetch_live_luck(user_id)

        if live is not None:
            counts, opens = live
            luck_score = _calc_luck_score(counts, opens)
            analysis_note = "🔴 Live"
        else:
            # Fall back to cached data
            counts_raw = target.get("rarity_json")
            counts = json.loads(counts_raw) if counts_raw else {}
            opens = target["opens_count"]
            luck_score = target["luck_score"]
            analysis_note = f"Cache {rank_updated_at} UTC"

        sign = "+" if luck_score >= 0 else ""
        ind = _luck_indicator_overall(luck_score)

        embed = discord.Embed(
            title=f"🌍 Wereldwijd geluk — {username}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Score",
            value=f"**{sign}{luck_score:.1f}%** {ind}",
            inline=True,
        )
        embed.add_field(
            name="Wereldwijde rang",
            value=f"**{rank_str}** / {total_ranked:,}",
            inline=True,
        )
        embed.add_field(
            name="Land",
            value=_cname(country_id),
            inline=True,
        )
        embed.add_field(
            name="Cases geopend",
            value=f"{opens:,}",
            inline=True,
        )

        if opens > 0 and counts:
            table = _build_luck_table(opens, counts)
            embed.add_field(
                name="Geluksanalyse",
                value=f"```ansi\n{table}\n```",
                inline=False,
            )

        embed.set_footer(
            text=(
                f"Kansen: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1%  "
                f"•  Min. 20 cases vereist  •  Analyse: {analysis_note}  "
                f"•  Rang bijgewerkt: {rank_updated_at} UTC"
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: DiscordBot) -> None:
    """Add the GlobalLuck cog to the bot."""
    await bot.add_cog(GlobalLuck(bot))
