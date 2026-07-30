"""
This module defines the GelukCog, which provides the /geluk command to analyze a player's case-opening luck in the WarEraNL bot.
- /geluk speler:naam — check het geluk van een speler bij het openen van cases (op basis van username of user ID)
- /caserang speler:naam - toon ranking op bases van aantal geopende cases
"""

from __future__ import annotations

import difflib
import json
import logging
import math as _luck_math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import citizen_autocomplete, strip_division_prefix
from services.api_client import APIClient
from services.case_luck import fetch_case_transactions, merge_counts
from services.key_loader import load_api_keys

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# ---------------------------------------------------------------------------
# Luck score calculation (shared with /geluk and used by /gelukranking)
# ---------------------------------------------------------------------------

_LUCK_WEIGHTS_G: dict[str, float] = {
    r: -_luck_math.log2(p)
    for r, p in {
        "mythic": 0.0001,
        "legendary": 0.0004,
        "epic": 0.0085,
        "rare": 0.071,
        "uncommon": 0.30,
        "common": 0.62,
    }.items()
}
_LUCK_WEIGHT_TOTAL_G: float = sum(_LUCK_WEIGHTS_G.values())


def calc_luck_pct(counts: dict, total: int) -> float:
    """Weighted luck % score: 0 = average, positive = luckier than average.

    Uses Poisson z-score normalisation: (actual - expected) / sqrt(expected).
    This keeps scores in a sensible range regardless of sample size or rarity.
    """
    if total == 0:
        return 0.0
    score = 0.0
    for rarity, expected_rate in EXPECTED_RATES.items():
        expected_n = total * expected_rate
        if expected_n <= 0:
            continue
        deviation = (counts.get(rarity, 0) - expected_n) / _luck_math.sqrt(expected_n)
        score += _LUCK_WEIGHTS_G[rarity] * deviation
    return score / _LUCK_WEIGHT_TOTAL_G * 100.0


def _luck_indicator_overall(luck_pct: float) -> str:
    """Emoji indicator for an overall luck percentage.

    Calibrated for raw Poisson z-score scale (~±300% range).
    Being above average for rare loots pushes the score well above +50%.
    """
    if luck_pct >= 50:
        return "🍀🍀"
    if luck_pct >= 15:
        return "🍀"
    if luck_pct >= -15:
        return "➖"
    if luck_pct >= -50:
        return "💀"
    return "💀💀"


# ---------------------------------------------------------------------------
# Expected drop rates per rarity (from the game's stated probabilities)
# ---------------------------------------------------------------------------
RARITY_ORDER = ["mythic", "legendary", "epic", "rare", "uncommon", "common"]

EXPECTED_RATES: dict[str, float] = {
    "mythic": 0.0001,  # 0.01 %
    "legendary": 0.0004,  # 0.04 %
    "epic": 0.0085,  # 0.85 %
    "rare": 0.071,  # 7.1  %
    "uncommon": 0.30,  # 30   %
    "common": 0.62,  # 62   %
}

ELITE_EXPECTED_RATES: dict[str, float] = {
    "mythic": 0.005,   # 0.5  %
    "legendary": 0.025,  # 2.5  %
    "epic": 0.15,    # 15   %
    "rare": 0.32,    # 32   %
    "uncommon": 0.50,  # 50   %
    "common": 0.0,   # 0    %
}

_ELITE_LUCK_WEIGHTS: dict[str, float] = {
    r: -_luck_math.log2(p) if p > 0 else 0.0
    for r, p in ELITE_EXPECTED_RATES.items()
}
_ELITE_LUCK_WEIGHT_TOTAL: float = sum(v for v in _ELITE_LUCK_WEIGHTS.values() if v > 0)


def calc_elite_luck_pct(counts: dict, total: int) -> float:
    """Poisson z-score luck % for elite case (case2) openings."""
    if total == 0:
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
    if _ELITE_LUCK_WEIGHT_TOTAL <= 0:
        return 0.0
    return score / _ELITE_LUCK_WEIGHT_TOTAL * 100.0

# Display labels (in Dutch / in-game naming)
RARITY_LABELS: dict[str, str] = {
    "mythic": "Mythic",
    "legendary": "Legendary",
    "epic": "Epic",
    "rare": "Rare",
    "uncommon": "Uncommon",
    "common": "Common",
}

# ANSI colour codes for each rarity (Discord ansi code block)
_ANSI_RARITY: dict[str, str] = {
    "mythic": "\033[31m",  # red
    "legendary": "\033[33m",  # yellow
    "epic": "\033[35m",  # purple (magenta)
    "rare": "\033[34m",  # blue
    "uncommon": "\033[32m",  # green
    "common": "\033[90m",  # grey
}
_ANSI_RST = "\033[0m"

RARITY_COLORS: dict[str, str] = {
    "mythic": "🔴",
    "legendary": "🟠",
    "epic": "🟣",
    "rare": "🔵",
    "uncommon": "🟢",
    "common": "⚪",
}


def _unwrap(resp: dict) -> dict:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _luck_indicator(actual_n: int, expected_n: float) -> str:
    """Return a luck emoji based on deviation from expected count.

    Scale: 💀💀  💀  👎  ➖  👍  🍀  🍀🍀

    For expected_n in [0.5, 1.0): you were statistically ~40–60% likely to
    get at least one drop; getting zero is slightly unlucky (👎).
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


def _build_luck_table(
    total: int,
    counts: dict[str, int],
    rates: dict[str, float] | None = None,
) -> str:
    """Build a compact fixed-width ANSI table comparing actual vs expected drops."""
    effective_rates = rates if rates is not None else EXPECTED_RATES
    header = f"{'Rarity':<14} {'Exp':>6} {'Got':>5}  {'Your%':>6}  Luck"
    sep = "─" * len(header)
    rows = [header, sep]
    for rarity in RARITY_ORDER:
        expected_rate = effective_rates.get(rarity, 0.0)
        expected_n = total * expected_rate
        actual_n = counts.get(rarity, 0)
        actual_rate = actual_n / total if total > 0 else 0.0
        luck = _luck_indicator(actual_n, expected_n)
        label = RARITY_LABELS[rarity]
        color = _ANSI_RARITY[rarity]
        rows.append(
            f"{color}{label:<14}{_ANSI_RST} {expected_n:>6.1f} {actual_n:>5d}  {actual_rate * 100:>5.2f}%  {luck}"
        )
    rows.append(sep)
    rows.append(f"{'Total':<14} {total:>6d} {sum(counts.values()):>5d}")
    return "\n".join(rows)


class Geluk(commands.Cog, name="geluk"):
    """Player case-opening luck analyser."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot
        self.config: dict = getattr(bot, "config", {}) or {}
        self._client: Optional[APIClient] = None
        self._item_rarity_cache: dict[str, str] = {}  # itemCode → rarity
        self._db: Optional[object] = None  # lazy Database connection for /gelukranking

    def _build_cached_luck_embed(
        self,
        entry: dict,
        type: Optional[Literal["normaal", "elite", "gecombineerd"]],
        note: Optional[str] = None,
    ) -> discord.Embed:
        """Build an embed from a citizen_luck DB row.

        *note* is a context-specific description line prepended above the
        "updated at" line — e.g. an offline warning, or a live-refresh note.
        Defaults to nothing (plain cached result: just the freshness line).
        """
        cached_name = entry.get("citizen_name") or "Onbekend"
        updated_at_raw = entry.get("updated_at") or ""
        try:
            dt = datetime.fromisoformat(updated_at_raw)
            updated_at = dt.strftime("%d-%m-%Y %H:%M")
        except Exception:
            updated_at = (updated_at_raw or "")[:16].replace("T", " ") or "onbekend"

        opens = int(entry.get("opens_count") or 0)
        luck_score = float(entry.get("luck_score") or 0.0)
        elite_opens = int(entry.get("elite_opens_count") or 0)

        rarity_raw = entry.get("rarity_json")
        normal_counts: dict[str, int] = json.loads(rarity_raw) if rarity_raw else {}
        elite_raw = entry.get("elite_rarity_json")
        elite_counts: dict[str, int] = json.loads(elite_raw) if elite_raw else {}

        description = f"-# Gegevens bijgewerkt: {updated_at} UTC"
        if note:
            description = f"{note}\n{description}"

        embed = discord.Embed(
            title=f"🎰 Case-geluk van {cached_name}",
            description=description,
            color=discord.Color.gold(),
        )

        sign = "+" if luck_score >= 0 else ""
        ind = _luck_indicator_overall(luck_score)
        embed.add_field(
            name="Cases geopend",
            value=f"**{opens:,}**",
            inline=True,
        )
        embed.add_field(
            name="Luck score",
            value=f"**{sign}{luck_score:.1f}%** {ind}",
            inline=True,
        )

        if opens > 0 and normal_counts and type != "elite":
            table = _build_luck_table(opens, normal_counts)
            embed.add_field(
                name="🎲 Case geluk",
                value=f"_{opens:,} case openings_\n```ansi\n{table}\n```",
                inline=False,
            )
        if elite_opens > 0 and elite_counts and type != "normaal":
            elite_table = _build_luck_table(elite_opens, elite_counts, ELITE_EXPECTED_RATES)
            embed.add_field(
                name="💎 Elite Case geluk",
                value=f"_{elite_opens:,} elite case openings_\n```ansi\n{elite_table}\n```",
                inline=False,
            )

        embed.set_footer(
            text="Kansen: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1%"
            " • uncommon 30% • common 62%  |  Elite: mythic 0.5% • legendary 2.5%"
            " • epic 15% • rare 32% • uncommon 50%"
        )
        return embed

    async def _send_cached_luck_result(
        self,
        interaction: discord.Interaction,
        db,
        entry: dict,
        type: Optional[Literal["normaal", "elite", "gecombineerd"]],
        note: Optional[str] = None,
    ) -> None:
        """Build and send a /geluk embed from a citizen_luck-shaped row.

        Shared by the cache-hit fast path (the common case — daily_luck_refresh
        keeps every NL citizen's row current), the offline fallback path, and
        the live-refresh path (a synthetic row merging cached + newly-fetched
        counts). Never re-pages a player's full openCase transaction history.
        """
        embed = self._build_cached_luck_embed(entry, type, note=note)
        nl_country_id = self.config.get("nl_country_id")
        if nl_country_id and entry.get("country_id") == nl_country_id:
            try:
                ranking = await db.get_luck_ranking(nl_country_id)
                if ranking:
                    try:
                        _stored = await db.get_poll_state("luck_ranking_total")
                        rank_total = int(_stored) if _stored else len(ranking)
                    except Exception:
                        rank_total = len(ranking)
                    rank_total = min(rank_total, len(ranking))
                    _MIN_NORMAL = 20
                    _MIN_ELITE = 10
                    e_uid = entry.get("user_id")
                    e_name = entry.get("citizen_name") or ""
                    updated_at_r = (ranking[0].get("updated_at") or "")[:16].replace("T", " ")

                    if type == "normaal":
                        ns = sorted(ranking, key=lambda e: e["luck_score"], reverse=True)
                        tgt = Geluk._find_in_ranking(ns, e_uid, e_name)
                        if tgt is not None:
                            rpct = ns[tgt]["luck_score"]; rsign = "+" if rpct >= 0 else ""
                            rt = f"🏆 Gelukranking NL (normale cases) — rang **#{tgt+1}/{rank_total}** — **{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                        else:
                            rt = f"🏆 Gelukranking NL (normale cases) — _{rank_total} spelers, niet in ranking (min. {_MIN_NORMAL} cases)_"
                        embed.add_field(name=rt, value=Geluk._build_ranking_block(ns, tgt, lambda e: e["luck_score"]), inline=False)

                    elif type == "elite":
                        eo = [e for e in ranking if e.get("elite_luck_score") is not None]
                        es = sorted(eo, key=lambda e: e["elite_luck_score"], reverse=True)
                        tgt = Geluk._find_in_ranking(es, e_uid, e_name)
                        n_e = len(es)
                        if tgt is not None:
                            rpct = es[tgt]["elite_luck_score"]; rsign = "+" if rpct >= 0 else ""
                            rt = f"🏆 Gelukranking NL (elite cases) — rang **#{tgt+1}/{n_e}** — **{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                        else:
                            rt = f"🏆 Gelukranking NL (elite cases) — _{n_e} spelers, niet in ranking (min. {_MIN_ELITE} elite cases)_"
                        embed.add_field(name=rt, value=Geluk._build_ranking_block(es, tgt, lambda e: e["elite_luck_score"]) if es else "_Geen data beschikbaar._", inline=False)

                    else:
                        def _cs(e: dict) -> float:
                            ls = e.get("luck_score"); es_ = e.get("elite_luck_score")
                            if ls is not None and es_ is not None: return (ls + es_) / 2.0
                            return ls if ls is not None else (es_ if es_ is not None else 0.0)
                        ns_c = sorted(ranking, key=lambda e: e["luck_score"], reverse=True)
                        nt_c = Geluk._find_in_ranking(ns_c, e_uid, e_name)
                        if nt_c is not None:
                            embed.add_field(name="🎲 Rang NL (normale cases)", value=f"**#{nt_c+1}/{rank_total}** _(min. {_MIN_NORMAL} cases)_", inline=True)
                        eo_c = [e for e in ranking if e.get("elite_luck_score") is not None]
                        es_c = sorted(eo_c, key=lambda e: e["elite_luck_score"], reverse=True)
                        et_c = Geluk._find_in_ranking(es_c, e_uid, e_name)
                        if et_c is not None:
                            embed.add_field(name="💎 Rang NL (elite cases)", value=f"**#{et_c+1}/{len(es_c)}** _(min. {_MIN_ELITE} elite cases)_", inline=True)
                        comb = sorted(ranking, key=_cs, reverse=True)
                        ct = Geluk._find_in_ranking(comb, e_uid, e_name)
                        lb = Geluk._build_ranking_block(comb, ct, _cs)
                        if ct is not None:
                            rpct = _cs(comb[ct]); rsign = "+" if rpct >= 0 else ""
                            rt = f"🏆 Gelukranking NL (gecombineerd) — rang **#{ct+1}/{rank_total}** — **{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                        else:
                            rt = f"🏆 Gelukranking NL (gecombineerd) — _{rank_total} spelers, niet in ranking (min. {_MIN_NORMAL} cases)_"
                        embed.add_field(name=rt, value=lb, inline=False)

                    if updated_at_r:
                        ft = embed.footer.text or ""
                        if "Ranking" not in ft:
                            embed.set_footer(text=ft + f"  •  Ranking wordt 1x per dag bijgewerkt (laatst: {updated_at_r} UTC)")
            except Exception:
                logger.exception("Geluk: failed to add ranking to cached embed")
        await interaction.followup.send(embed=embed)

    def _api_offline_embed(self, note: str = "") -> discord.Embed:
        """Return a standardised 'API offline' embed."""
        desc = (
            "⚠️ De WarEra API is momenteel niet beschikbaar.\n"
            "Probeer het later opnieuw."
        )
        if note:
            desc += f"\n\n{note}"
        return discord.Embed(
            title="🔌 API Offline",
            description=desc,
            colour=discord.Colour.orange(),
        )

    @staticmethod
    def _find_in_ranking(
        sorted_list: list, uid: Optional[str], name: str
    ) -> Optional[int]:
        """Return the 0-based position of the player in *sorted_list*, or None."""
        name_low = (name or "").lower().strip()
        for pos, e in enumerate(sorted_list):
            item = e if isinstance(e, dict) else e[1]
            if item.get("user_id") == uid or (item.get("citizen_name") or "").lower().strip() == name_low:
                return pos
        return None

    @staticmethod
    def _build_ranking_block(
        sorted_entries: list, target_pos: Optional[int], score_fn
    ) -> str:
        """Build the fixed-width leaderboard text block."""
        n = len(sorted_entries)
        top5 = list(range(min(5, n)))
        bot5 = list(range(max(0, n - 5), n))
        ctx = list(range(max(0, target_pos - 2), min(n, target_pos + 3))) if target_pos is not None else []
        ordered = sorted(set(top5 + bot5 + ctx))
        lines: list[str] = [
            f"{'rang':<5} {'naam':<12} {'score':>8}   {'geluk':<6} {'cases':>6}",
            "\u2500" * 43,
        ]
        prev = -1
        for pos in ordered:
            if prev != -1 and pos > prev + 1:
                lines.append("    \u2022 \u2022 \u2022")
            e = sorted_entries[pos]
            pct = score_fn(e)
            nm = (e.get("citizen_name") or "?")[:12]
            op = e.get("opens_count", 0)
            sign = "+" if pct >= 0 else ""
            ind = _luck_indicator_overall(pct)
            marker = " \u25c4" if pos == target_pos else ""
            lines.append(f"#{pos+1:<4} {nm:<12} {sign}{pct:>6.1f}%  {ind}  {op:>6,}{marker}")
            prev = pos
        return "```\n" + "\n".join(lines) + "\n```"

    async def _get_client(self) -> APIClient:
        if getattr(self.bot, "_force_api_offline", False):
            raise RuntimeError("API offline (test mode)")
        if self._client is None:
            base_url = self.config.get("api_base_url", "https://api2.warera.io/trpc")
            api_keys = load_api_keys()
            self._client = APIClient(base_url=base_url, api_keys=api_keys, source="discord-bot")
            await self._client.start()
        return self._client

    async def _get_item_rarities(self) -> dict[str, str]:
        """Load item code → rarity mapping from gameConfig (cached)."""
        if self._item_rarity_cache:
            return self._item_rarity_cache
        try:
            client = await self._get_client()
            raw = await client.get("/gameConfig.getGameConfig", params={"input": "{}"})
            data = _unwrap(raw)
            items: dict = data.get("items", {}) if isinstance(data, dict) else {}
            for code, item in items.items():
                rarity = item.get("rarity")
                if rarity:
                    self._item_rarity_cache[code] = rarity
            logger.info(
                "Geluk: loaded %d item rarities from gameConfig",
                len(self._item_rarity_cache),
            )
        except Exception as exc:
            logger.warning("Geluk: could not load item rarities: %s", exc)
        return self._item_rarity_cache

    async def _search_user(self, username: str) -> list[str] | None:
        """Search for a player by username and return up to 5 candidate user IDs.

        Returns None if the API is unreachable (connection/timeout error).
        Returns an empty list [] if the API responded but found nothing.
        """
        try:
            client = await self._get_client()
            raw = await client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            user_ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return user_ids[:5]
        except Exception as exc:
            logger.warning("Geluk: search failed for %r: %s", username, exc)
            return None  # None = API unreachable; [] = API up but no results

    async def _get_user_profile(self, user_id: str) -> Optional[dict]:
        """Return getUserLite data for a user."""
        client = await self._get_client()
        try:
            raw = await client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            return _unwrap(raw) if isinstance(raw, dict) else None
        except Exception as exc:
            logger.warning("Geluk: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _get_db(self):
        """Return the shared Database instance, closing any standalone fallback when services become ready.

        Same-process SQLite connections competing for the WAL write lock return
        SQLITE_LOCKED (not SQLITE_BUSY), bypassing busy_timeout entirely.  This
        method ensures only ONE connection to external.db exists in this process.
        """
        shared = getattr(self.bot, "_ext_db", None)
        if shared is not None:
            if self._db is not None and self._db is not shared:
                # Close the standalone created before services were ready
                try:
                    await self._db.close()
                except Exception:
                    pass
                self._db = None
            return shared
        if self._db is None:
            from services.db import Database
            db_path = self.config.get("external_db_path", "database/external.db")
            self._db = Database(db_path)
            await self._db.setup()
        return self._db

    async def _resolve_user_from_query(
        self, query: str
    ) -> tuple[Optional[str], Optional[dict], bool]:
        """Resolve user by query: exact username first, closest search candidate as fallback.

        Resolution order:
        1. Exact case-insensitive match in citizen_levels DB (autocomplete source).
        2. API search → exact username match.
        3. API search → best fuzzy ratio match.
        4. DB fuzzy fallback (only when API returns no candidates).

        Returns (user_id, profile, api_offline) where api_offline=True means the API
        was unreachable (as opposed to the player genuinely not being found).
        """
        s_low = query.lower().strip()

        # 1. DB exact match — avoids the API returning a similarly-named player.
        db = await self._get_db()
        try:
            db_exact = await db.get_citizen_by_name_exact(query)
            if db_exact:
                uid, _ = db_exact
                p = await self._get_user_profile(uid)
                if p is not None:
                    return uid, p, False
        except Exception:
            pass

        # 2 & 3. API search
        user_ids = await self._search_user(query)

        if user_ids is None:
            # API is unreachable — signal caller to show offline embed
            return None, None, True

        if not user_ids:
            # 4. API responded but found nothing — try fuzzy match against local citizen_levels cache
            nl_country_id = self.config.get("nl_country_id")
            match = await db.fuzzy_citizen_by_name(query, country_id=nl_country_id)
            if match:
                uid, _ = match
                p = await self._get_user_profile(uid)
                if p is not None:
                    return uid, p, False
            return None, None, False

        candidates: list[tuple[str, dict]] = []
        for uid in user_ids:
            p = await self._get_user_profile(uid)
            if p is not None:
                candidates.append((uid, p))

        for uid, p in candidates:
            if (p.get("username") or "").lower().strip() == s_low:
                return uid, p, False

        best_uid: Optional[str] = None
        best_profile: Optional[dict] = None
        best_ratio = -1.0
        for uid, p in candidates:
            ratio = difflib.SequenceMatcher(
                None, s_low, (p.get("username") or "").lower().strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_uid = uid
                best_profile = p

        return best_uid, best_profile, False

    async def _fetch_all_case_transactions(
        self,
        user_id: str,
        item_rarities: dict[str, str],
        max_cases: Optional[int] = None,
    ) -> Optional[tuple[dict[str, int], dict[str, int]]]:
        """
        Page through openCase transactions for a user.

        If *max_cases* is given, stops after that many normal (non-elite)
        openings have been collected (i.e. the X most recent normal cases).

        Returns a tuple (normal_counts, elite_counts), or None if the endpoint is
        inaccessible (auth error).
        """
        client = await self._get_client()
        normal_counts: dict[str, int] = {r: 0 for r in RARITY_ORDER}
        elite_counts: dict[str, int] = {r: 0 for r in RARITY_ORDER}
        cursor: Optional[str] = None
        page = 0
        total_fetched = 0
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
            except Exception as exc:
                err = str(exc)
                if "401" in err or "Unauthorized" in err:
                    logger.info(
                        "Geluk: transaction endpoint requires session auth — "
                        "cannot retrieve case history for %s",
                        user_id,
                    )
                    return None
                logger.warning("Geluk: transaction fetch error page %d: %s", page, exc)
                break

            data = _unwrap(raw) if isinstance(raw, dict) else {}
            items = []
            if isinstance(data, dict):
                items = (
                    data.get("items")
                    or data.get("transactions")
                    or data.get("results")
                    or []
                )
                cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                items = data
                cursor = None

            for tx in items:
                if not isinstance(tx, dict):
                    continue
                opened_case = tx.get("itemCode", "")
                is_elite = item_rarities.get(opened_case) == "mythic"
                # "itemCode" is the *case* that was opened; the *received* drop is in item.code
                received_item = tx.get("item") or {}
                item_code = (
                    received_item.get("code")
                    if isinstance(received_item, dict)
                    else received_item
                ) or ""
                rarity = item_rarities.get(item_code, "common")
                if is_elite:
                    elite_counts[rarity] = elite_counts.get(rarity, 0) + 1
                else:
                    normal_counts[rarity] = normal_counts.get(rarity, 0) + 1
                    if max_cases is not None and sum(normal_counts.values()) >= max_cases:
                        limit_reached = True
                        break

            total_fetched += len(items)
            page += 1

            if not cursor or not items or limit_reached:
                break

            # await asyncio.sleep(0.3)

        logger.info(
            "Geluk: fetched %d case transactions for %s across %d pages",
            total_fetched,
            user_id,
            page,
        )
        return normal_counts, elite_counts

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="geluk",
        description="Controleer het geluk van een speler bij het openen van cases",
    )
    @app_commands.describe(
        speler="De gebruikersnaam van de speler om te controleren",
        gebruiker_id="Optioneel: WarEra user ID van de speler",
        aantal_cases="Optioneel: analyseer alleen de X meest recente case openings",
        type="Toon alleen normale cases, elite cases, of gecombineerd (standaard: gecombineerd)",
        live="Haal nieuwe cases op sinds de laatste update voor realtime geluk (standaard: gecachete data van vandaag)",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def geluk(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        gebruiker_id: Optional[str] = None,
        aantal_cases: Optional[int] = None,
        type: Optional[Literal["normaal", "elite", "gecombineerd"]] = None,
        live: bool = False,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not speler and not gebruiker_id:
            speler = strip_division_prefix(interaction.user.display_name)

        # 1. Find player — by gebruiker_id if provided, otherwise by username.
        profile: Optional[dict] = None
        resolved_user_id: Optional[str] = None
        api_offline = False
        if getattr(self.bot, "_force_api_offline", False):
            # Skip all API calls immediately when test mode forces offline.
            api_offline = True
        elif gebruiker_id:
            p = await self._get_user_profile(gebruiker_id)
            if p is not None:
                profile = p
                resolved_user_id = gebruiker_id
            elif speler:
                resolved_user_id, profile, api_offline = await self._resolve_user_from_query(speler)
        elif speler:
            resolved_user_id, profile, api_offline = await self._resolve_user_from_query(speler)

        lookup_label = gebruiker_id or speler or "?"
        if resolved_user_id is None or profile is None:
            if api_offline:
                db = await self._get_db()
                entry: Optional[dict] = None
                if db:
                    try:
                        entry = await db.get_luck_entry_by_name(lookup_label)
                    except Exception as exc:
                        logger.warning("Geluk: DB name lookup failed: %s", exc)
                if entry is not None:
                    await self._send_cached_luck_result(
                        interaction, db, entry, type,
                        note="⚠️ De API is offline — gecachete data wordt weergegeven.",
                    )
                else:
                    await interaction.followup.send(
                        embed=self._api_offline_embed(), ephemeral=True
                    )
            else:
                await interaction.followup.send(
                    f"❌ Speler **{discord.utils.escape_markdown(lookup_label)}** niet gevonden.",
                    ephemeral=True,
                )
            return

        username: str = profile.get("username") or speler or gebruiker_id or "?"
        avatar_url: str = profile.get("avatarUrl") or ""
        rankings: dict = profile.get("rankings") or {}
        cases_ranking: dict = rankings.get("userCasesOpened") or {}
        total_cases_opened: int = int(cases_ranking.get("value") or 0)
        cases_rank: Optional[int] = cases_ranking.get("rank")

        # Fast path: serve from the citizen_luck cache instead of live-paging
        # the player's full openCase transaction history. daily_luck_refresh
        # (cogs/tasks/luck.py) keeps every NL citizen's row current once a
        # day, and covers the overwhelming majority of /geluk calls — a live
        # per-player fetch can be hundreds of paginated API calls for an
        # active player, which is why this command used to be slow.
        # aantal_cases ("most recent N") can't be served by the cache (it
        # only stores full-history aggregate counts), so that still goes live.
        if aantal_cases is None:
            db_cache = await self._get_db()
            cached_entry: Optional[dict] = None
            if db_cache:
                try:
                    cached_entry = await db_cache.get_luck_entry_by_user_id(resolved_user_id)
                except Exception as exc:
                    logger.warning("Geluk: cache lookup failed: %s", exc)

            if live:
                # Realtime mode: fetch only NEW transactions since the cache's
                # last-seen cutoff (or a full history fetch if this player has
                # never been cached), merge onto the cached counts, and
                # persist the merged result — so the daily sweep and future
                # cache-hit calls both benefit from this fresher cutoff too.
                cutoff_id = cached_entry.get("last_seen_transaction_id") if cached_entry else None
                prior_normal = (
                    json.loads(cached_entry["rarity_json"])
                    if cached_entry and cached_entry.get("rarity_json") else {}
                )
                prior_elite = (
                    json.loads(cached_entry["elite_rarity_json"])
                    if cached_entry and cached_entry.get("elite_rarity_json") else {}
                )
                item_rarities = await self._get_item_rarities()
                client = await self._get_client()
                delta_normal, delta_elite, newest_id, fetched = await fetch_case_transactions(
                    client, resolved_user_id, item_rarities, cutoff_id=cutoff_id
                )
                if cutoff_id:
                    # True incremental fetch — delta is only what's new since
                    # cutoff_id, so add it onto the existing cached counts.
                    merged_normal = merge_counts(prior_normal, delta_normal)
                    merged_elite = merge_counts(prior_elite, delta_elite)
                else:
                    # No cutoff (never cached, OR a cached row whose
                    # last_seen_transaction_id is still NULL — e.g. every row
                    # that predates this column). fetch_case_transactions had
                    # nothing to stop early at, so it already paged the
                    # player's ENTIRE history — delta_normal/delta_elite ARE
                    # the full, authoritative totals. Merging them onto
                    # prior_normal/prior_elite here would double-count
                    # everything the cached row already had (this is exactly
                    # how a player's case count got reported at ~2x reality).
                    merged_normal = delta_normal
                    merged_elite = delta_elite
                new_cutoff = newest_id or cutoff_id
                total_opens = sum(merged_normal.values())
                elite_total = sum(merged_elite.values())
                luck_val = calc_luck_pct(merged_normal, total_opens)
                elite_luck_val = calc_elite_luck_pct(merged_elite, elite_total) if elite_total >= 5 else None
                now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                _nl_cid = self.config.get("nl_country_id", "")
                _player_country = (profile.get("country") or "") if profile else ""
                if db_cache and _nl_cid and _player_country == _nl_cid:
                    try:
                        await db_cache.upsert_luck_score(
                            resolved_user_id, _nl_cid, username, luck_val, total_opens,
                            json.dumps(merged_normal), now_iso,
                            elite_luck_score=elite_luck_val,
                            elite_opens_count=elite_total if elite_total >= 5 else None,
                            elite_rarity_json=json.dumps(merged_elite) if elite_total >= 5 else None,
                            last_seen_transaction_id=new_cutoff,
                        )
                        await db_cache.flush_luck_scores()
                    except Exception:
                        logger.exception("Geluk: failed to persist live-refreshed luck score")

                synthetic_entry = {
                    "user_id": resolved_user_id,
                    "citizen_name": username,
                    "country_id": _player_country,
                    "luck_score": luck_val,
                    "opens_count": total_opens,
                    "rarity_json": json.dumps(merged_normal),
                    "updated_at": now_iso,
                    "elite_luck_score": elite_luck_val,
                    "elite_opens_count": elite_total if elite_total >= 5 else None,
                    "elite_rarity_json": json.dumps(merged_elite) if elite_total >= 5 else None,
                }
                live_note = (
                    f"🔴 Live — {fetched} nieuwe case(s) opgehaald sinds laatste update"
                    if cutoff_id else
                    "🔴 Live — volledige case-geschiedenis opgehaald"
                )
                await self._send_cached_luck_result(
                    interaction, db_cache, synthetic_entry, type, note=live_note
                )
                return

            if cached_entry is not None:
                await self._send_cached_luck_result(interaction, db_cache, cached_entry, type)
                return

        # 3. Load item rarities from game config
        item_rarities = await self._get_item_rarities()

        # 4. Try to fetch actual transaction history
        result = await self._fetch_all_case_transactions(
            resolved_user_id, item_rarities, max_cases=aantal_cases
        )
        can_show_actual = result is not None
        normal_counts: dict[str, int] = {}
        elite_counts: dict[str, int] = {}
        if result is not None:
            normal_counts, elite_counts = result

        # 5. Build embed
        embed = discord.Embed(
            title=f"🎰 Case-geluk van {username}",
            color=discord.Color.gold(),
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        # Cases opened summary line
        rank_str = f" (rank #{cases_rank})" if cases_rank else ""
        embed.add_field(
            name="Cases geopend",
            value=f"**{total_cases_opened:,}**{rank_str}",
            inline=False,
        )

        if not can_show_actual:
            # Transaction API not accessible — show expected distribution only
            embed.description = (
                "⚠️ De transactie-API vereist inloggegevens van de speler zelf — "
                "individuele drops zijn niet beschikbaar via de publieke API.\n\n"
                "Hieronder staat de **verwachte** verdeling op basis van het totaal aantal "
                "geopende cases."
            )
            if total_cases_opened > 0:
                lines = ["```ansi"]
                lines.append(f"{'Rarity':<14} {'Expected':>8}  {'Chance%':>7}")
                lines.append("─" * 35)
                for rarity in RARITY_ORDER:
                    rate = EXPECTED_RATES[rarity]
                    expected_n = total_cases_opened * rate
                    label = RARITY_LABELS[rarity]
                    color = _ANSI_RARITY[rarity]
                    lines.append(
                        f"{color}{label:<14}{_ANSI_RST} {expected_n:>8.1f}  {rate * 100:>6.2f}%"
                    )
                lines.append("─" * 35)
                lines.append(f"{'Total':<14} {total_cases_opened:>8,}")
                lines.append("```")
                embed.add_field(
                    name="Verwachte verdeling",
                    value="\n".join(lines),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Geen cases gevonden",
                    value="Deze speler heeft nog geen cases geopend.",
                    inline=False,
                )
        else:
            # We have actual data
            total_counted = sum(normal_counts.values())
            elite_total = sum(elite_counts.values())

            if total_counted == 0 and elite_total == 0:
                embed.description = "Deze speler heeft nog geen cases geopend (of er waren geen geregistreerde drops)."
            else:
                # Normal cases table
                if total_counted > 0 and type != "elite":
                    analysed_note = (
                        f"_{total_counted:,} meest recente case openings_"
                        if aantal_cases is not None
                        else f"_{total_counted:,} case openings gevonden_"
                    )
                    table = _build_luck_table(total_counted, normal_counts)
                    embed.add_field(
                        name="🎲 Case geluk",
                        value=f"{analysed_note}\n```ansi\n{table}\n```",
                        inline=False,
                    )

                # Elite cases table
                if elite_total > 0 and type != "normaal":
                    elite_table = _build_luck_table(elite_total, elite_counts, ELITE_EXPECTED_RATES)
                    embed.add_field(
                        name="💎 Elite Case geluk",
                        value=f"_{elite_total:,} elite case openings_\n```ansi\n{elite_table}\n```",
                        inline=False,
                    )

        footer_base = "Kansen: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1% • uncommon 30% • common 62%  |  Elite: mythic 0.5% • legendary 2.5% • epic 15% • rare 32% • uncommon 50%"

        # Auto-upsert this player's luck score into the ranking DB if they're
        # an NL citizen with enough opens. This ensures /geluk always populates
        # the ranking even if daily_luck_refresh hasn't run yet.
        _nl_cid = self.config.get("nl_country_id", "")
        _player_country = (profile.get("country") or "") if profile else ""
        if can_show_actual and normal_counts and _nl_cid and _player_country == _nl_cid and aantal_cases is None:
            _tc = sum(normal_counts.values())
            if _tc >= 20:
                try:
                    from datetime import datetime as _dt
                    from datetime import timezone as _tz

                    _luck = calc_luck_pct(normal_counts, _tc)
                    _rarity_json = __import__("json").dumps(normal_counts)
                    _now = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    _db = await self._get_db()
                    _elite_tc = sum(elite_counts.values())
                    _elite_luck = calc_elite_luck_pct(elite_counts, _elite_tc) if _elite_tc >= 5 else None
                    _elite_rarity_json = __import__("json").dumps(elite_counts) if _elite_tc >= 5 else None
                    await _db.upsert_luck_score(
                        resolved_user_id,
                        _nl_cid,
                        username,
                        _luck,
                        _tc,
                        _rarity_json,
                        _now,
                        elite_luck_score=_elite_luck,
                        elite_opens_count=_elite_tc if _elite_tc >= 10 else None,
                        elite_rarity_json=_elite_rarity_json,
                    )
                    await _db.flush_luck_scores()
                    logger.info(
                        "Geluk: auto-upserted luck score for %s (%+.1f%%)",
                        username,
                        _luck,
                    )
                except Exception:
                    logger.exception(
                        "Geluk: failed to auto-upsert luck score for %s",
                        resolved_user_id,
                    )

        _MIN_NORMAL = 20
        _MIN_ELITE = 10

        # -- Gelukranking section (only for NL citizens) --
        try:
            nl_country_id = self.config.get("nl_country_id")
            if nl_country_id and _player_country == nl_country_id:
                db = await self._get_db()
                ranking = await db.get_luck_ranking(nl_country_id)
                if ranking:
                    try:
                        _stored = await db.get_poll_state("luck_ranking_total")
                        rank_total = int(_stored) if _stored else len(ranking)
                    except Exception:
                        rank_total = len(ranking)
                    rank_total = min(rank_total, len(ranking))

                    updated_at = (ranking[0].get("updated_at") or "")[:16].replace("T", " ")
                    _all_cases_note = "  _(ranking: alle cases)_" if aantal_cases is not None else ""

                    # ── Helpers (delegate to shared static methods) ──
                    def _find_player(sorted_list: list) -> int | None:
                        return Geluk._find_in_ranking(sorted_list, resolved_user_id, username)

                    def _build_lb(sorted_entries: list[dict], target_pos: int | None, score_fn) -> str:
                        return Geluk._build_ranking_block(sorted_entries, target_pos, score_fn)

                    if type == "normaal":
                        # Normal-only view: sort by luck_score
                        normal_sorted = sorted(ranking, key=lambda e: e["luck_score"], reverse=True)
                        normal_target = _find_player(normal_sorted)
                        if normal_target is not None:
                            rp = normal_target + 1
                            rpct = normal_sorted[normal_target]["luck_score"]
                            rsign = "+" if rpct >= 0 else ""
                            rank_title = (
                                f"🏆 Gelukranking NL (normale cases) — "
                                f"rang **#{rp}/{rank_total}** — "
                                f"**{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                                f"{_all_cases_note}"
                            )
                        else:
                            rank_title = f"🏆 Gelukranking NL (normale cases) — _{rank_total} spelers, niet in ranking (min. {_MIN_NORMAL} cases)_{_all_cases_note}"
                        lb = _build_lb(normal_sorted, normal_target, lambda e: e["luck_score"])
                        embed.add_field(name=rank_title, value=lb, inline=False)

                    elif type == "elite":
                        # Elite-only view: sort by elite_luck_score, exclude those without it
                        elite_only = [e for e in ranking if e.get("elite_luck_score") is not None]
                        elite_sorted = sorted(elite_only, key=lambda e: e["elite_luck_score"], reverse=True)  # type: ignore[arg-type]
                        elite_target = _find_player(elite_sorted)
                        n_elite = len(elite_sorted)
                        if elite_target is not None:
                            rp = elite_target + 1
                            rpct = elite_sorted[elite_target]["elite_luck_score"]  # type: ignore[index]
                            rsign = "+" if rpct >= 0 else ""
                            rank_title = (
                                f"🏆 Gelukranking NL (elite cases) — "
                                f"rang **#{rp}/{n_elite}** — "
                                f"**{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                                f"{_all_cases_note}"
                            )
                        else:
                            rank_title = f"🏆 Gelukranking NL (elite cases) — _{n_elite} spelers, niet in ranking (min. {_MIN_ELITE} elite cases)_{_all_cases_note}"
                        if elite_sorted:
                            lb = _build_lb(elite_sorted, elite_target, lambda e: e["elite_luck_score"])  # type: ignore[arg-type]
                            embed.add_field(name=rank_title, value=lb, inline=False)
                        else:
                            embed.add_field(name=rank_title, value="_Geen data beschikbaar._", inline=False)

                    else:
                        # Combined view (default)
                        # ── Normal rank ──
                        normal_sorted_c = sorted(ranking, key=lambda e: e["luck_score"], reverse=True)
                        normal_target_c = _find_player(normal_sorted_c)
                        if normal_target_c is not None:
                            embed.add_field(
                                name="🎲 Rang NL (normale cases)",
                                value=f"**#{normal_target_c + 1}/{rank_total}** _(min. {_MIN_NORMAL} cases)_",
                                inline=True,
                            )

                        # ── Elite rank ──
                        elite_only_c = [e for e in ranking if e.get("elite_luck_score") is not None]
                        elite_sorted_c = sorted(elite_only_c, key=lambda e: e["elite_luck_score"], reverse=True)  # type: ignore[arg-type]
                        elite_target_c = _find_player(elite_sorted_c)
                        n_elite_c = len(elite_sorted_c)
                        if elite_target_c is not None:
                            embed.add_field(
                                name="💎 Rang NL (elite cases)",
                                value=f"**#{elite_target_c + 1}/{n_elite_c}** _(min. {_MIN_ELITE} elite cases)_",
                                inline=True,
                            )

                        # ── Combined leaderboard ──
                        def _combined_score(e: dict) -> float:
                            ls = e.get("luck_score")
                            es = e.get("elite_luck_score")
                            if ls is not None and es is not None:
                                return (ls + es) / 2.0
                            return ls if ls is not None else (es if es is not None else 0.0)

                        combined_sorted = sorted(ranking, key=_combined_score, reverse=True)
                        combined_target = _find_player(combined_sorted)

                        lb = _build_lb(combined_sorted, combined_target, _combined_score)

                        if combined_target is not None:
                            rp = combined_target + 1
                            rpct = _combined_score(combined_sorted[combined_target])
                            rsign = "+" if rpct >= 0 else ""
                            rank_title = (
                                f"🏆 Gelukranking NL (gecombineerd) — "
                                f"rang **#{rp}/{rank_total}** — "
                                f"**{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                                f"{_all_cases_note}"
                            )
                        else:
                            rank_title = f"🏆 Gelukranking NL (gecombineerd) — _{rank_total} spelers, niet in ranking (min. {_MIN_NORMAL} cases)_{_all_cases_note}"

                        embed.add_field(name=rank_title, value=lb, inline=False)

                    if updated_at:
                        footer_base += f"  •  Ranking wordt 1x per dag bijgewerkt (laatst: {updated_at} UTC)"
        except Exception:
            logger.exception("Geluk: failed to load ranking for /geluk")

        embed.set_footer(text=footer_base)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="caserang",
        description="Toon de wereldwijde en NL top op cases; optioneel met rang van een speler",
    )
    @app_commands.describe(
        speler="De gebruikersnaam van de speler (optioneel) — werkt voor elke speler in het spel",
        gebruiker_id="Optioneel: WarEra user ID van de speler",
        top_n="Hoeveel spelers in de top tonen (standaard: 10)",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def caserang(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        gebruiker_id: Optional[str] = None,
        top_n: Optional[int] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        top = max(1, min(top_n or 10, 100))
        nl_country_id = self.config.get("nl_country_id", "")

        db = await self._get_db()
        if not db:
            await interaction.followup.send("❌ Database niet beschikbaar.", ephemeral=True)
            return

        all_entries = await db.get_global_luck_ranking(limit=None)
        if not all_entries:
            await interaction.followup.send(
                "⚠️ Geen gecachete case-data gevonden.", ephemeral=True
            )
            return

        def _to_row(r: dict) -> dict:
            return {
                "user_id": r.get("user_id") or "",
                "username": (r.get("citizen_name") or r.get("user_id") or "?").strip(),
                "country_id": r.get("country_id") or "",
                "cases": int(r.get("opens_count") or 0),
            }

        global_rows = [_to_row(r) for r in all_entries]

        # Resolve the requested player against citizen_levels — the full
        # citizen roster (same source the speler: autocomplete suggests
        # from) — NOT by fuzzy-matching against global_rows, which only
        # holds the ~1500 players with 20+ case opens who've been through a
        # sweep. Fuzzy-matching against that narrower pool actively produces
        # wrong results whenever the real target isn't in it: "best
        # available" still returns *something*, e.g. "PrinceRealMarijn" vs
        # "Biermann" scores 0.50 and "DeKapitein" vs "de_Kaiser" scores 0.63
        # — neither is remotely the right player, but with no correct
        # candidate in the pool to compare against, the wrong one still won.
        # Resolving against the full roster first means an exact name match
        # succeeds directly for any real player, and fuzzy matching (via
        # fuzzy_citizen_by_name's own cutoff) only kicks in for genuine typos.
        target_uid: Optional[str] = None
        target_username: Optional[str] = None
        if gebruiker_id:
            details = await db.get_citizen_details_by_ids([gebruiker_id])
            d = details.get(gebruiker_id)
            if d:
                target_uid = gebruiker_id
                target_username = d.get("citizen_name") or gebruiker_id
        elif speler:
            resolved = await db.get_citizen_by_name_exact(speler)
            if resolved is None:
                resolved = await db.fuzzy_citizen_by_name(speler)
            if resolved is not None:
                target_uid, target_username = resolved

        if (speler or gebruiker_id) and target_uid is None:
            lookup_label = gebruiker_id or speler or "?"
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(lookup_label)}** niet gevonden.",
                ephemeral=True,
            )
            return

        target_in_ranking = target_uid is not None and any(
            r["user_id"] == target_uid for r in global_rows
        )

        # Not in the cached ranking yet — fetch their case history live so
        # the command works for every real player, not just already-cached
        # ones, and cache the result opportunistically so the next lookup
        # for this player is instant.
        if target_uid is not None and not target_in_ranking:
            uid = target_uid
            name = target_username or uid
            details = await db.get_citizen_details_by_ids([uid])
            player_country = details.get(uid, {}).get("country_id", "")

            item_rarities = await self._get_item_rarities()
            client = await self._get_client()
            normal_counts, elite_counts, newest_id, _fetched = await fetch_case_transactions(
                client, uid, item_rarities
            )
            total_opens = sum(normal_counts.values())
            elite_total = sum(elite_counts.values())

            if player_country:
                try:
                    luck_val = calc_luck_pct(normal_counts, total_opens)
                    elite_luck_val = calc_elite_luck_pct(elite_counts, elite_total) if elite_total >= 5 else None
                    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    await db.upsert_global_luck_score(
                        uid, player_country, name, luck_val, total_opens,
                        json.dumps(normal_counts), now_iso,
                        elite_luck_score=elite_luck_val,
                        elite_opens_count=elite_total if elite_total >= 5 else None,
                        elite_rarity_json=json.dumps(elite_counts) if elite_total >= 5 else None,
                        last_seen_transaction_id=newest_id,
                    )
                    await db.flush_global_luck_scores()
                except Exception:
                    logger.exception("caserang: failed to persist live-fetched case count for %s", uid)

            # Insert into the in-memory ranking so their position is reflected
            # immediately, without needing another DB round-trip.
            global_rows.append({
                "user_id": uid, "username": name,
                "country_id": player_country, "cases": total_opens,
            })

        global_rows.sort(key=lambda r: (-r["cases"], r["username"].lower()))
        for idx, row in enumerate(global_rows, start=1):
            row["rank"] = idx

        nl_rows = [r for r in global_rows if r["country_id"] == nl_country_id]
        for idx, row in enumerate(nl_rows, start=1):
            row["nl_rank"] = idx

        target_global = next((r for r in global_rows if r["user_id"] == target_uid), None) if target_uid else None
        target_nl = next((r for r in nl_rows if r["user_id"] == target_uid), None) if target_uid else None
        is_dutch = target_global is not None and target_global["country_id"] == nl_country_id

        def _fmt_row(r: dict, rank_key: str) -> str:
            name = (r["username"] or "?")[:16]
            return f"#{r[rank_key]:<4} {name:<16} {r['cases']:>8,}"

        def _build_section(rows: list[dict], rank_key: str, target: Optional[dict], title: str) -> list[tuple[str, str]]:
            header = f"{'rang':<5} {'naam':<16} {'cases':>8}"
            sep = "─" * 34
            top_rows = rows[:top]
            extra: list = []
            if target and target[rank_key] > top:
                extra = [None, target]
            all_data_rows = top_rows + extra
            CHUNK = 25
            chunks = [all_data_rows[i : i + CHUNK] for i in range(0, len(all_data_rows), CHUNK)]
            fields: list[tuple[str, str]] = []
            for chunk_idx, chunk in enumerate(chunks):
                lines: list[str] = [header, sep] if chunk_idx == 0 else []
                for r in chunk:
                    lines.append("    • • •" if r is None else _fmt_row(r, rank_key))
                block = "```\n" + "\n".join(lines) + "\n```"
                label = title if chunk_idx == 0 else f"{title} (vervolg)"
                fields.append((label, block))
            return fields

        if target_global:
            description = f"Speler: **{discord.utils.escape_markdown(target_global['username'])}**"
        else:
            description = None

        embed = discord.Embed(
            title="🎟️ Case-rang",
            description=description,
            color=discord.Color.gold(),
        )
        for label, block in _build_section(
            global_rows, "rank", target_global, f"🌍 Wereldwijd top {top}"
        ):
            embed.add_field(name=label, value=block, inline=False)
        for label, block in _build_section(
            nl_rows, "nl_rank", target_nl if is_dutch else None, f"🇳🇱 NL top {top}"
        ):
            embed.add_field(name=label, value=block, inline=False)

        embed.set_footer(
            text=(
                f"Wordt periodiek bijgewerkt • {len(global_rows)} spelers wereldwijd, "
                f"{len(nl_rows)} NL spelers"
            )
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: DiscordBot) -> None:
    """Add the Geluk cog to the bot."""
    await bot.add_cog(Geluk(bot))
