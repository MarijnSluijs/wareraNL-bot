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
from typing import TYPE_CHECKING, Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import citizen_autocomplete, strip_division_prefix
from services.api_client import APIClient
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
        self, entry: dict, type: Optional[Literal["normaal", "elite", "gecombineerd"]]
    ) -> discord.Embed:
        """Build an embed from a citizen_luck DB row when the API is offline."""
        from datetime import datetime

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

        embed = discord.Embed(
            title=f"🎰 Case-geluk van {cached_name}",
            description=(
                "⚠️ De API is offline — gecachete data wordt weergegeven."
                f"\n-# Gegevens bijgewerkt: {updated_at} UTC"
            ),
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
            self._client = APIClient(base_url=base_url, api_keys=api_keys)
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
        """Return the shared Database instance (from poller), or create one lazily."""
        if self._db is None:
            # Prefer the already-open connection held by ProductionChecker to avoid
            # two separate SQLite connections that would conflict on writes.
            shared = getattr(self.bot, "_ext_db", None)
            if shared is not None:
                self._db = shared
            else:
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
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def geluk(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        gebruiker_id: Optional[str] = None,
        aantal_cases: Optional[int] = None,
        type: Optional[Literal["normaal", "elite", "gecombineerd"]] = None,
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
                    embed_off = self._build_cached_luck_embed(entry, type)
                    # Add ranking if this is an NL citizen
                    nl_country_id_off = self.config.get("nl_country_id")
                    if nl_country_id_off and entry.get("country_id") == nl_country_id_off:
                        try:
                            ranking_off = await db.get_luck_ranking(nl_country_id_off)
                            if ranking_off:
                                try:
                                    _stored_off = await db.get_poll_state("luck_ranking_total")
                                    rank_total_off = int(_stored_off) if _stored_off else len(ranking_off)
                                except Exception:
                                    rank_total_off = len(ranking_off)
                                rank_total_off = min(rank_total_off, len(ranking_off))
                                _MIN_NORMAL_OFF = 20
                                _MIN_ELITE_OFF = 10
                                _e_uid = entry.get("user_id")
                                _e_name = entry.get("citizen_name") or ""
                                updated_at_r = (ranking_off[0].get("updated_at") or "")[:16].replace("T", " ")

                                if type == "normaal":
                                    ns = sorted(ranking_off, key=lambda e: e["luck_score"], reverse=True)
                                    tgt = Geluk._find_in_ranking(ns, _e_uid, _e_name)
                                    if tgt is not None:
                                        rpct = ns[tgt]["luck_score"]; rsign = "+" if rpct >= 0 else ""
                                        rt = f"\U0001f3c6 Gelukranking NL (normale cases) \u2014 rang **#{tgt+1}/{rank_total_off}** \u2014 **{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                                    else:
                                        rt = f"\U0001f3c6 Gelukranking NL (normale cases) \u2014 _{rank_total_off} spelers, niet in ranking (min. {_MIN_NORMAL_OFF} cases)_"
                                    embed_off.add_field(name=rt, value=Geluk._build_ranking_block(ns, tgt, lambda e: e["luck_score"]), inline=False)

                                elif type == "elite":
                                    eo = [e for e in ranking_off if e.get("elite_luck_score") is not None]
                                    es = sorted(eo, key=lambda e: e["elite_luck_score"], reverse=True)
                                    tgt = Geluk._find_in_ranking(es, _e_uid, _e_name)
                                    n_e = len(es)
                                    if tgt is not None:
                                        rpct = es[tgt]["elite_luck_score"]; rsign = "+" if rpct >= 0 else ""
                                        rt = f"\U0001f3c6 Gelukranking NL (elite cases) \u2014 rang **#{tgt+1}/{n_e}** \u2014 **{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                                    else:
                                        rt = f"\U0001f3c6 Gelukranking NL (elite cases) \u2014 _{n_e} spelers, niet in ranking (min. {_MIN_ELITE_OFF} elite cases)_"
                                    embed_off.add_field(name=rt, value=Geluk._build_ranking_block(es, tgt, lambda e: e["elite_luck_score"]) if es else "_Geen data beschikbaar._", inline=False)

                                else:
                                    def _cs_off(e: dict) -> float:
                                        ls = e.get("luck_score"); es_ = e.get("elite_luck_score")
                                        if ls is not None and es_ is not None: return (ls + es_) / 2.0
                                        return ls if ls is not None else (es_ if es_ is not None else 0.0)
                                    ns_c = sorted(ranking_off, key=lambda e: e["luck_score"], reverse=True)
                                    nt_c = Geluk._find_in_ranking(ns_c, _e_uid, _e_name)
                                    if nt_c is not None:
                                        embed_off.add_field(name="\U0001f3b2 Rang NL (normale cases)", value=f"**#{nt_c+1}/{rank_total_off}** _(min. {_MIN_NORMAL_OFF} cases)_", inline=True)
                                    eo_c = [e for e in ranking_off if e.get("elite_luck_score") is not None]
                                    es_c = sorted(eo_c, key=lambda e: e["elite_luck_score"], reverse=True)
                                    et_c = Geluk._find_in_ranking(es_c, _e_uid, _e_name)
                                    if et_c is not None:
                                        embed_off.add_field(name="\U0001f48e Rang NL (elite cases)", value=f"**#{et_c+1}/{len(es_c)}** _(min. {_MIN_ELITE_OFF} elite cases)_", inline=True)
                                    comb = sorted(ranking_off, key=_cs_off, reverse=True)
                                    ct = Geluk._find_in_ranking(comb, _e_uid, _e_name)
                                    lb_off = Geluk._build_ranking_block(comb, ct, _cs_off)
                                    if ct is not None:
                                        rpct = _cs_off(comb[ct]); rsign = "+" if rpct >= 0 else ""
                                        rt = f"\U0001f3c6 Gelukranking NL (gecombineerd) \u2014 rang **#{ct+1}/{rank_total_off}** \u2014 **{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                                    else:
                                        rt = f"\U0001f3c6 Gelukranking NL (gecombineerd) \u2014 _{rank_total_off} spelers, niet in ranking (min. {_MIN_NORMAL_OFF} cases)_"
                                    embed_off.add_field(name=rt, value=lb_off, inline=False)

                                if updated_at_r:
                                    _ft = embed_off.footer.text or ""
                                    if "Ranking bijgewerkt" not in _ft:
                                        embed_off.set_footer(text=_ft + f"  \u2022  Ranking bijgewerkt: {updated_at_r} UTC")
                        except Exception:
                            logger.exception("Geluk: failed to add ranking to offline embed")
                    await interaction.followup.send(embed=embed_off)
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
                        footer_base += f"  •  Ranking bijgewerkt: {updated_at} UTC"
        except Exception:
            logger.exception("Geluk: failed to load ranking for /geluk")

        embed.set_footer(text=footer_base)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="caserang",
        description="Toon de NL top op cases; optioneel met rang van een speler",
    )
    @app_commands.describe(
        speler="De gebruikersnaam van de speler (optioneel)",
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

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            await interaction.followup.send(
                "❌ `nl_country_id` is niet geconfigureerd.", ephemeral=True
            )
            return

        db = await self._get_db()
        ranking = await db.get_luck_ranking(nl_country_id)
        if not ranking:
            await interaction.followup.send(
                "⚠️ Geen gecachete case-data gevonden. Voer eerst `!pollgeluk` uit.",
                ephemeral=True,
            )
            return

        rows: list[dict] = [
            {
                "user_id": r.get("user_id") or "",
                "username": (r.get("citizen_name") or r.get("user_id") or "?").strip(),
                "cases": int(r.get("opens_count") or 0),
            }
            for r in ranking
        ]
        rows.sort(key=lambda r: (-r["cases"], r["username"].lower()))
        for idx, row in enumerate(rows, start=1):
            row["rank"] = idx

        # Resolve player if requested
        target_row: Optional[dict] = None
        if gebruiker_id:
            target_row = next((r for r in rows if r["user_id"] == gebruiker_id), None)
        if target_row is None and speler:
            s_low = speler.lower().strip()
            target_row = next(
                (r for r in rows if r["username"].lower().strip() == s_low), None
            )
            if target_row is None:
                best_ratio = -1.0
                for r in rows:
                    ratio = difflib.SequenceMatcher(
                        None, s_low, r["username"].lower().strip()
                    ).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        target_row = r

        if (speler or gebruiker_id) and target_row is None:
            lookup_label = gebruiker_id or speler or "?"
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(lookup_label)}** niet gevonden in de cache.",
                ephemeral=True,
            )
            return

        def _fmt_row(r: dict) -> str:
            name = (r["username"] or "?")[:16]
            return f"#{r['rank']:<4} {name:<16} {r['cases']:>8,}"

        top_rows = rows[:top]
        header = f"{'rang':<5} {'naam':<16} {'cases':>8}"
        sep = "─" * 34
        # Add player below top if they fall outside it
        extra: list[dict] = []
        if target_row and target_row["rank"] > top:
            extra = [None, target_row]  # None → ellipsis row

        # Split into chunks of 25 so each field stays under Discord's 1024-char limit
        CHUNK = 25
        all_data_rows = top_rows + extra  # type: ignore[operator]
        chunks: list[list] = [
            all_data_rows[i : i + CHUNK] for i in range(0, len(all_data_rows), CHUNK)
        ]

        if target_row:
            resolved_name = target_row["username"]
            description = f"Speler: **{discord.utils.escape_markdown(resolved_name)}**"
            field_title = f"Top {top} + gevraagde speler"
        else:
            description = None
            field_title = f"Top {top}"

        embed = discord.Embed(
            title="🎟️ NL case-rang",
            description=description,
            color=discord.Color.gold(),
        )
        for chunk_idx, chunk in enumerate(chunks):
            lines: list[str] = []
            if chunk_idx == 0:
                lines = [header, sep]
            for r in chunk:
                if r is None:
                    lines.append("    • • •")
                else:
                    lines.append(_fmt_row(r))
            block = "```\n" + "\n".join(lines) + "\n```"
            name_label = field_title if chunk_idx == 0 else f"Top {top} (vervolg)"
            embed.add_field(name=name_label, value=block, inline=False)

        embed.set_footer(text=f"Cache-bron: citizen_luck • NL spelers: {len(rows)}")
        await interaction.followup.send(embed=embed)


async def setup(bot: DiscordBot) -> None:
    """Add the Geluk cog to the bot."""
    await bot.add_cog(Geluk(bot))
