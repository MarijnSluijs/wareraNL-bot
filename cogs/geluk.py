"""Geluk cog — /geluk command to analyse a player's case-opening luck."""

import json
import logging
import asyncio
import difflib
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.api_client import APIClient

logger = logging.getLogger("discord_bot")

import math as _luck_math

# ---------------------------------------------------------------------------
# Luck score calculation (shared with /geluk and used by /gelukranking)
# ---------------------------------------------------------------------------

_LUCK_WEIGHTS_G: dict[str, float] = {
    r: -_luck_math.log2(p)
    for r, p in {
        "mythic": 0.0001, "legendary": 0.0004, "epic": 0.0085,
        "rare": 0.071,    "uncommon": 0.30,    "common": 0.62,
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
    if luck_pct >= 50:   return "🍀🍀"
    if luck_pct >= 15:   return "🍀"
    if luck_pct >= -15:  return "➖"
    if luck_pct >= -50:  return "💀"
    return "💀💀"


# ---------------------------------------------------------------------------
# Expected drop rates per rarity (from the game's stated probabilities)
# ---------------------------------------------------------------------------
RARITY_ORDER = ["mythic", "legendary", "epic", "rare", "uncommon", "common"]

EXPECTED_RATES: dict[str, float] = {
    "mythic":    0.0001,   # 0.01 %
    "legendary": 0.0004,   # 0.04 %
    "epic":      0.0085,   # 0.85 %
    "rare":      0.071,    # 7.1  %
    "uncommon":  0.30,     # 30   %
    "common":    0.62,     # 62   %
}

# Display labels (in Dutch / in-game naming)
RARITY_LABELS: dict[str, str] = {
    "mythic":    "Mythic",
    "legendary": "Legendary",
    "epic":      "Epic",
    "rare":      "Rare",
    "uncommon":  "Uncommon",
    "common":    "Common",
}

# ANSI colour codes for each rarity (Discord ansi code block)
_ANSI_RARITY: dict[str, str] = {
    "mythic":    "\033[31m",   # red
    "legendary": "\033[33m",   # yellow
    "epic":      "\033[35m",   # purple (magenta)
    "rare":      "\033[34m",   # blue
    "uncommon":  "\033[32m",   # green
    "common":    "\033[90m",   # grey
}
_ANSI_RST = "\033[0m"

RARITY_COLORS: dict[str, str] = {
    "mythic":    "🔴",
    "legendary": "🟠",
    "epic":      "🟣",
    "rare":      "🔵",
    "uncommon":  "🟢",
    "common":    "⚪",
}


def _unwrap(resp: dict) -> dict:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _luck_indicator(actual_n: int, expected_n: float) -> str:
    """Return a luck emoji based on deviation from expected count.

    When expected_n < 1 (rarity so low you weren't statistically due one),
    getting zero is neutral — only flag positively if you got one anyway.
    """
    if expected_n <= 0:
        return ""
    if expected_n < 1.0:
        return "🍀🍀" if actual_n >= 1 else "➖"
    ratio = actual_n / expected_n
    if ratio >= 1.5:
        return "🍀🍀"
    if ratio >= 1.2:
        return "🍀"
    if ratio >= 0.8:
        return "➖"
    if ratio >= 0.5:
        return "💀"
    return "💀💀"


def _build_luck_table(
    total: int,
    counts: dict[str, int],
) -> str:
    """Build a compact fixed-width ANSI table comparing actual vs expected drops."""
    header = f"{'Rarity':<14} {'Exp':>6} {'Got':>5}  {'Your%':>6}  Luck"
    sep = "─" * len(header)
    rows = [header, sep]
    for rarity in RARITY_ORDER:
        expected_rate = EXPECTED_RATES[rarity]
        expected_n = total * expected_rate
        actual_n = counts.get(rarity, 0)
        actual_rate = actual_n / total if total > 0 else 0.0
        luck = _luck_indicator(actual_n, expected_n)
        label = RARITY_LABELS[rarity]
        color = _ANSI_RARITY[rarity]
        rows.append(
            f"{color}{label:<14}{_ANSI_RST} {expected_n:>6.1f} {actual_n:>5d}  {actual_rate*100:>5.2f}%  {luck}"
        )
    rows.append(sep)
    rows.append(f"{'Total':<14} {total:>6d} {sum(counts.values()):>5d}")
    return "\n".join(rows)


class Geluk(commands.Cog, name="geluk"):
    """Player case-opening luck analyser."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: dict = getattr(bot, "config", {}) or {}
        self._client: Optional[APIClient] = None
        self._item_rarity_cache: dict[str, str] = {}  # itemCode → rarity
        self._db: Optional[object] = None  # lazy Database connection for /gelukranking

    async def _get_client(self) -> APIClient:
        if self._client is None:
            base_url = self.config.get("api_base_url", "https://api2.warera.io/trpc")
            api_keys = None
            try:
                with open("_api_keys.json") as f:
                    api_keys = json.load(f).get("keys", [])
            except FileNotFoundError:
                pass
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
            logger.info("Geluk: loaded %d item rarities from gameConfig", len(self._item_rarity_cache))
        except Exception as exc:
            logger.warning("Geluk: could not load item rarities: %s", exc)
        return self._item_rarity_cache

    async def _search_user(self, username: str) -> list[str]:
        """Search for a player by username and return up to 5 candidate user IDs."""
        client = await self._get_client()
        try:
            raw = await client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            user_ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return user_ids[:5]
        except Exception as exc:
            logger.warning("Geluk: search failed for %r: %s", username, exc)
            return []

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

    async def _resolve_user_from_query(self, query: str) -> tuple[Optional[str], Optional[dict]]:
        """Resolve user by query: exact username first, closest search candidate as fallback."""
        s_low = query.lower().strip()
        user_ids = await self._search_user(query)
        if not user_ids:
            return None, None

        candidates: list[tuple[str, dict]] = []
        for uid in user_ids:
            p = await self._get_user_profile(uid)
            if p is not None:
                candidates.append((uid, p))

        for uid, p in candidates:
            if (p.get("username") or "").lower().strip() == s_low:
                return uid, p

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

        return best_uid, best_profile

    async def _fetch_all_case_transactions(
        self,
        user_id: str,
        item_rarities: dict[str, str],
    ) -> Optional[dict[str, int]]:
        """
        Page through all openCase transactions for a user.

        Returns a dict of {rarity: count}, or None if the endpoint is
        inaccessible (auth error).
        """
        client = await self._get_client()
        counts: dict[str, int] = {r: 0 for r in RARITY_ORDER}
        cursor: Optional[str] = None
        page = 0
        total_fetched = 0

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
                        "cannot retrieve case history for %s", user_id
                    )
                    return None
                logger.warning("Geluk: transaction fetch error page %d: %s", page, exc)
                break

            data = _unwrap(raw) if isinstance(raw, dict) else {}
            items = []
            if isinstance(data, dict):
                items = data.get("items") or data.get("transactions") or data.get("results") or []
                cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                items = data
                cursor = None

            for tx in items:
                if not isinstance(tx, dict):
                    continue
                # Skip elite cases (case2, mythic rarity) — the profile ranking
                # only counts regular case1 openings, so we do the same.
                opened_case = tx.get("itemCode", "")
                if item_rarities.get(opened_case) == "mythic":
                    continue
                # "itemCode" is the *case* that was opened; the *received* drop is in item.code
                received_item = tx.get("item") or {}
                item_code = (
                    received_item.get("code") if isinstance(received_item, dict) else received_item
                ) or ""
                rarity = item_rarities.get(item_code, "common")
                counts[rarity] = counts.get(rarity, 0) + 1

            total_fetched += len(items)
            page += 1

            if not cursor or not items:
                break

            await asyncio.sleep(0.3)

        logger.info(
            "Geluk: fetched %d case transactions for %s across %d pages",
            total_fetched, user_id, page,
        )
        return counts

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="geluk",
        description="Controleer het geluk van een speler bij het openen van cases",
    )
    @app_commands.describe(
        speler="De gebruikersnaam van de speler om te controleren",
        user_id="Optioneel: WarEra user ID van de speler",
    )
    async def geluk(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not speler and not user_id:
            await interaction.followup.send(
                "❌ Geef een **speler** of **user_id** op.",
                ephemeral=True,
            )
            return

        # 1. Find player — by user_id if provided, otherwise by username.
        profile: Optional[dict] = None
        resolved_user_id: Optional[str] = None
        if user_id:
            p = await self._get_user_profile(user_id)
            if p is not None:
                profile = p
                resolved_user_id = user_id
            elif speler:
                resolved_user_id, profile = await self._resolve_user_from_query(speler)
        elif speler:
            resolved_user_id, profile = await self._resolve_user_from_query(speler)

        lookup_label = user_id or speler or "?"
        if resolved_user_id is None or profile is None:
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(lookup_label)}** niet gevonden.",
                ephemeral=True,
            )
            return

        username: str = profile.get("username") or speler or user_id or "?"
        avatar_url: str = profile.get("avatarUrl") or ""
        rankings: dict = profile.get("rankings") or {}
        cases_ranking: dict = rankings.get("userCasesOpened") or {}
        total_cases_opened: int = int(cases_ranking.get("value") or 0)
        cases_rank: Optional[int] = cases_ranking.get("rank")

        # 3. Load item rarities from game config
        item_rarities = await self._get_item_rarities()

        # 4. Try to fetch actual transaction history
        counts = await self._fetch_all_case_transactions(resolved_user_id, item_rarities)
        can_show_actual = counts is not None

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
                    lines.append(f"{color}{label:<14}{_ANSI_RST} {expected_n:>8.1f}  {rate*100:>6.2f}%")
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
            total_counted = sum(counts.values())

            if total_counted == 0:
                embed.description = "Deze speler heeft nog geen cases geopend (of er waren geen geregistreerde drops)."
            else:
                analysed_note = f"_{total_counted:,} case openings gevonden_"
                table = _build_luck_table(total_counted, counts)
                embed.add_field(name="Geluksanalyse", value=f"{analysed_note}\n```ansi\n{table}\n```", inline=False)

        footer_base = "Odds: mythic 0.01% • legendary 0.04% • epic 0.85% • rare 7.1% • uncommon 30% • common 62%"

        # Auto-upsert this player's luck score into the ranking DB if they're
        # an NL citizen with enough opens. This ensures /geluk always populates
        # the ranking even if daily_luck_refresh hasn't run yet.
        _nl_cid = self.config.get("nl_country_id", "")
        _player_country = (profile.get("country") or "") if profile else ""
        if can_show_actual and counts and _nl_cid and _player_country == _nl_cid:
            _tc = sum(counts.values())
            if _tc >= 20:
                try:
                    from datetime import timezone as _tz, datetime as _dt
                    _luck = calc_luck_pct(counts, _tc)
                    _now = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    _db = await self._get_db()
                    await _db.upsert_luck_score(
                        resolved_user_id, _nl_cid, username, _luck, _tc, _now
                    )
                    await _db.flush_luck_scores()
                    logger.info("Geluk: auto-upserted luck score for %s (%+.1f%%)", username, _luck)
                except Exception:
                    logger.exception("Geluk: failed to auto-upsert luck score for %s", resolved_user_id)

        # -- Gelukranking section --
        try:
            nl_country_id = self.config.get("nl_country_id")
            if nl_country_id:
                db = await self._get_db()
                ranking = await db.get_luck_ranking(nl_country_id)
                if ranking:
                    # Use the total from the last completed sweep so the denominator
                    # stays consistent even while a new sweep is in progress.
                    try:
                        _stored = await db.get_poll_state("luck_ranking_total")
                        rank_total = int(_stored) if _stored else len(ranking)
                    except Exception:
                        rank_total = len(ranking)
                    rank_target_idx: int | None = None
                    for idx, entry in enumerate(ranking):
                        if entry["user_id"] == resolved_user_id:
                            rank_target_idx = idx
                            break
                    # Name fallback (in case user_id differs between search and DB)
                    if rank_target_idx is None:
                        for idx, entry in enumerate(ranking):
                            if (entry["citizen_name"] or "").lower() == username.lower():
                                rank_target_idx = idx
                                break

                    def _rank_row(idx: int, highlight: bool = False) -> str:
                        e = ranking[idx]
                        rn = idx + 1
                        nm = (e["citizen_name"] or "?")[:12]
                        pct = e["luck_score"]
                        op = e.get("opens_count", 0)
                        sign = "+" if pct >= 0 else ""
                        ind = _luck_indicator_overall(pct)
                        marker = " ◄" if highlight else ""
                        return f"#{rn:<4} {nm:<12} {sign}{pct:>6.1f}%  {ind}  {op:>4}{marker}"

                    top5 = list(range(min(5, rank_total)))
                    bot5 = list(range(max(0, rank_total - 5), rank_total))
                    ctx_range = (
                        list(range(max(0, rank_target_idx - 2), min(rank_total, rank_target_idx + 3)))
                        if rank_target_idx is not None else []
                    )
                    ordered = sorted(set(top5 + bot5 + ctx_range))

                    rank_lines: list[str] = [
                        f"{'rang':<5} {'naam':<12} {'score':>8}   {'geluk':<6} cases",
                        "─" * 40,
                    ]
                    prev = -1
                    for idx in ordered:
                        if prev != -1 and idx > prev + 1:
                            rank_lines.append("    • • •")
                        rank_lines.append(_rank_row(idx, highlight=(idx == rank_target_idx)))
                        prev = idx

                    rank_block = "```\n" + "\n".join(rank_lines) + "\n```"

                    updated_at = (ranking[0].get("updated_at") or "")[:16].replace("T", " ")
                    if rank_target_idx is not None:
                        rp = rank_target_idx + 1
                        rpct = ranking[rank_target_idx]["luck_score"]
                        rsign = "+" if rpct >= 0 else ""
                        rank_title = (
                            f"🏆 Gelukranking NL — "
                            f"rang **#{rp}/{rank_total}** — "
                            f"**{rsign}{rpct:.1f}%** {_luck_indicator_overall(rpct)}"
                        )
                    else:
                        rank_title = f"🏆 Gelukranking NL — _{rank_total} spelers, niet in ranking (min. 20 cases)_"

                    embed.add_field(name=rank_title, value=rank_block, inline=False)
                    if updated_at:
                        footer_base += f"  •  Ranking bijgewerkt: {updated_at} UTC"
        except Exception:
            logger.exception("Geluk: failed to load ranking for /geluk")

        embed.set_footer(text=footer_base)
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="caserang",
        description="Toon de NL top 5 op cases + de rang van een speler",
    )
    @app_commands.describe(
        speler="De gebruikersnaam van de speler",
        user_id="Optioneel: WarEra user ID van de speler",
    )
    async def caserang(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not speler and not user_id:
            await interaction.followup.send(
                "❌ Geef een **speler** of **user_id** op.",
                ephemeral=True,
            )
            return

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            await interaction.followup.send("❌ `nl_country_id` is niet geconfigureerd.", ephemeral=True)
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

        # Same matching behavior as /geluk: exact first, closest fallback
        target_row: Optional[dict] = None
        if user_id:
            target_row = next((r for r in rows if r["user_id"] == user_id), None)
        if target_row is None and speler:
            s_low = speler.lower().strip()
            target_row = next((r for r in rows if r["username"].lower().strip() == s_low), None)
            if target_row is None:
                best_ratio = -1.0
                for r in rows:
                    ratio = difflib.SequenceMatcher(None, s_low, r["username"].lower().strip()).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        target_row = r

        if target_row is None:
            lookup_label = user_id or speler or "?"
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(lookup_label)}** niet gevonden in de cache.",
                ephemeral=True,
            )
            return

        def _fmt_row(r: dict) -> str:
            name = (r["username"] or "?")[:16]
            return f"#{r['rank']:<4} {name:<16} {r['cases']:>8,}"

        top5 = rows[:5]
        lines = [f"{'rang':<5} {'naam':<16} {'cases':>8}", "─" * 34]
        for r in top5:
            lines.append(_fmt_row(r))

        if target_row and target_row["rank"] > 5:
            lines.append("    • • •")
            lines.append(_fmt_row(target_row))

        block = "```\n" + "\n".join(lines) + "\n```"

        resolved_name = target_row["username"]
        embed = discord.Embed(
            title="🎟️ NL case-rang",
            description=f"Speler: **{discord.utils.escape_markdown(resolved_name)}**",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Top 5 + gevraagde speler", value=block, inline=False)
        embed.set_footer(text=f"Cache-bron: citizen_luck • NL spelers: {len(rows)}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Geluk(bot))
