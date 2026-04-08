"""Slash command /transacties — transaction breakdown per type for a WarEra player.

Usage
-----
/transacties speler:Naam  — show per-type count and total coin value
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase, citizen_autocomplete
from services.api_client import APIClient

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAGE_SIZE = 100
_MAX_PAGES = 500  # up to 50 000 transactions

_TYPE_LABELS: dict[str, str] = {
    "applicationFee": "App. fee",
    "trading":        "Trading",
    "itemMarket":     "Item market",
    "wage":           "Wage",
    "donation":       "Donation",
    "articleTip":     "Article tip",
    "openCase":       "Open case",
    "craftItem":      "Craft item",
    "dismantleItem":  "Dismantle item",
    "battleLoot":     "Battle loot",
}
_TYPE_ORDER = list(_TYPE_LABELS.keys())

_RARITY_ORDER = ["mythic", "legendary", "epic", "rare", "uncommon", "common"]
_RARITY_LABELS: dict[str, str] = {
    "mythic":    "mythic",
    "legendary": "legendary",
    "epic":      "epic",
    "rare":      "rare",
    "uncommon":  "uncommon",
    "common":    "common",
}
# For openCase: map the CASE container's rarity to a human label
_CASE_LABELS: dict[str, str] = {
    "mythic":    "Elite case",
    "epic":      "Elite case",
    "legendary": "Case",
    "rare":      "Case",
    "uncommon":  "Case",
    "common":    "Case",
}
_ITEM_BREAKDOWN_TYPES = {"trading", "itemMarket", "openCase", "craftItem", "dismantleItem", "battleLoot"}
# Sub-rows sorted by rarity order (others sorted by count desc)
_BD_BY_RARITY = {"craftItem", "dismantleItem"}
# trading shows individual item names instead of rarity
_BD_BY_ITEM_NAME = {"trading"}
# Sub-rows sorted by count desc, capped at this many rows
_BD_MAX_ROWS = 15

# ANSI colour per category (reset at end of each row)
_ANSI_INCOME = "\033[32m"   # green  —  wage, donations received, tips received
_ANSI_SPEND  = "\033[31m"   # red    —  fees, market, cases, crafting
_ANSI_TRADE  = "\033[33m"   # yellow —  trading, item market
_ANSI_RST    = "\033[0m"

_TYPE_COLOR: dict[str, str] = {
    "applicationFee": _ANSI_SPEND,
    "trading":        _ANSI_TRADE,
    "itemMarket":     _ANSI_TRADE,
    "wage":           _ANSI_INCOME,
    "donation":       _ANSI_INCOME,
    "articleTip":     _ANSI_INCOME,
    "openCase":       _ANSI_SPEND,
    "craftItem":      _ANSI_SPEND,
    "dismantleItem":  _ANSI_SPEND,
    "battleLoot":     _ANSI_INCOME,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap(resp: dict) -> dict:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _fmt_cc(amount: float) -> str:
    """Format a CC coin amount with magnitude suffix."""
    if amount == 0:
        return "—"
    sign = "-" if amount < 0 else ""
    v = abs(amount)
    if v >= 1_000_000:
        return f"{sign}{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}{v / 1_000:.1f}k"
    return f"{sign}{v:.2f}"


def _prettify_item_code(code: str) -> str:
    """Convert a camelCase item code to a readable display name (e.g. cookedFish → Cooked Fish)."""
    import re
    words = re.sub(r'([A-Z])', r' \1', code).split()
    return " ".join(w.capitalize() for w in words)


def _sort_breakdown(
    tx_type: str, breakdown: "dict[str, tuple[int, float]]"
) -> "list[tuple[str, int, float]]":
    """Return sorted list of (sub_key, count, total) for a breakdown dict."""
    entries = [(k, v[0], v[1]) for k, v in breakdown.items()]
    if tx_type in _BD_BY_RARITY:
        order = {r: i for i, r in enumerate(_RARITY_ORDER)}
        entries.sort(key=lambda x: order.get(x[0], 999))
    else:
        entries.sort(key=lambda x: -x[1])
    return entries


def _build_table(
    counts: dict[str, int],
    totals: dict[str, float],
    item_breakdown: "dict[str, dict[str, tuple[int, float]]]",
    truncated: bool,
    total_tx: int,
) -> str:
    """Build a fixed-width ANSI table of transaction types → count + coin total."""
    col_type  = 15
    col_count =  7
    col_total = 12

    header = f"{'Type':<{col_type}} {'Aantal':>{col_count}} {'Totaal CC':>{col_total}}"
    sep = "─" * len(header)
    rows = [header, sep]

    grand_count = 0
    grand_total = 0.0

    def _add_row(tx_type: str, count: int, total: float) -> None:
        label = _TYPE_LABELS.get(tx_type, tx_type)
        color = _TYPE_COLOR.get(tx_type, "")
        rows.append(
            f"{color}{label:<{col_type}}{_ANSI_RST}"
            f" {count:>{col_count},d}"
            f" {_fmt_cc(total):>{col_total}}"
        )
        breakdown = item_breakdown.get(tx_type, {})
        if breakdown:
            sorted_rows = _sort_breakdown(tx_type, breakdown)
            cap = _BD_MAX_ROWS if tx_type not in _BD_BY_RARITY else len(sorted_rows)
            for sub_key, r_count, r_total in sorted_rows[:cap]:
                r_label = f"  {sub_key}"
                rows.append(
                    f"{r_label:<{col_type}}"
                    f" {r_count:>{col_count},d}"
                    f" {_fmt_cc(r_total):>{col_total}}"
                )
            if len(sorted_rows) > cap:
                rows.append(f"  ... +{len(sorted_rows) - cap} meer")

    # Known types first, in defined order
    for tx_type in _TYPE_ORDER:
        count = counts.get(tx_type, 0)
        if count == 0:
            continue
        total = totals.get(tx_type, 0.0)
        _add_row(tx_type, count, total)
        grand_count += count
        grand_total += total

    # Any unknown types the API may return
    for tx_type in sorted(counts):
        if tx_type in _TYPE_LABELS:
            continue
        count = counts[tx_type]
        total = totals[tx_type]
        _add_row(tx_type, count, total)
        grand_count += count
        grand_total += total

    rows.append(sep)
    rows.append(
        f"{'Totaal':<{col_type}}"
        f" {grand_count:>{col_count},d}"
        f" {_fmt_cc(grand_total):>{col_total}}"
    )

    if truncated:
        rows.append(f"\n⚠️ Eerste {total_tx} transacties getoond (meer beschikbaar)")

    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class TransactiesCog(CommandCogBase, name="transacties"):
    """Cog for the /transacties command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot
        self._api_client: Optional[APIClient] = None
        self._item_rarity_cache: dict[str, str] = {}  # itemCode → rarity
        self._item_name_cache: dict[str, str] = {}    # itemCode → display name
        self._item_type_cache: dict[str, str] = {}    # itemCode → type (e.g. "case", "weapon")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _get_client(self) -> APIClient:
        if self._api_client is None:
            base_url = self.config.get("api_base_url", "https://api2.warera.io/trpc")
            api_keys: list[str] = []
            try:
                with open("_api_keys.json") as f:
                    api_keys = json.load(f).get("keys", [])
            except FileNotFoundError:
                pass
            self._api_client = APIClient(base_url=base_url, api_keys=api_keys)
            await self._api_client.start()
        return self._api_client

    async def _get_item_rarities(self) -> "tuple[dict[str, str], dict[str, str], dict[str, str]]":
        """Load item code → rarity, name, and type from gameConfig (cached).

        Returns ``(rarity_cache, name_cache, type_cache)``.
        """
        if self._item_rarity_cache:
            return self._item_rarity_cache, self._item_name_cache, self._item_type_cache
        try:
            client = await self._get_client()
            raw = await client.get("/gameConfig.getGameConfig", params={"input": "{}"})
            data = _unwrap(raw)
            items: dict = data.get("items", {}) if isinstance(data, dict) else {}
            for code, item in items.items():
                rarity = item.get("rarity")
                if rarity:
                    self._item_rarity_cache[code] = rarity
                item_type = item.get("type")
                if item_type:
                    self._item_type_cache[code] = item_type
                name = item.get("name") or item.get("displayName")
                if name:
                    self._item_name_cache[code] = str(name)
        except Exception as exc:
            logger.warning("Transacties: could not load item rarities: %s", exc)
        return self._item_rarity_cache, self._item_name_cache, self._item_type_cache

    async def _search_user(self, query: str) -> list[str]:
        client = await self._get_client()
        try:
            raw = await client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": query})},
            )
            data = _unwrap(raw)
            return data.get("userIds", [])[:5] if isinstance(data, dict) else []
        except Exception as exc:
            logger.warning("Transacties: search failed for %r: %s", query, exc)
            return []

    async def _get_user_profile(self, user_id: str) -> Optional[dict]:
        client = await self._get_client()
        try:
            raw = await client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            return _unwrap(raw) if isinstance(raw, dict) else None
        except Exception as exc:
            logger.warning("Transacties: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(self, query: str) -> tuple[Optional[str], Optional[str]]:
        """Return (user_id, username) for *query*, or (None, None) if not found."""
        q_low = query.lower().strip()
        user_ids = await self._search_user(query)

        candidates: list[tuple[str, str]] = []
        for uid in user_ids:
            p = await self._get_user_profile(uid)
            if p is None:
                continue
            name: str = p.get("username") or uid
            if name.lower().strip() == q_low:
                return uid, name
            candidates.append((uid, name))

        if candidates:
            return candidates[0]

        # Fall back to local DB fuzzy match when the API finds nothing
        if self._db is not None:
            nl_country_id = self.config.get("nl_country_id")
            try:
                match = await self._db.fuzzy_citizen_by_name(
                    query, country_id=nl_country_id
                )
                if match:
                    uid, _ = match
                    p = await self._get_user_profile(uid)
                    if p:
                        return uid, p.get("username") or uid
            except Exception as exc:
                logger.debug("Transacties: DB fallback failed: %s", exc)

        return None, None

    async def _fetch_transactions(
        self, user_id: str
    ) -> "tuple[dict[str, int], dict[str, float], dict[str, dict[str, tuple[int, float]]], bool]":
        """Page through all transactions for *user_id* and aggregate them.

        Returns ``(counts, coin_totals, item_breakdown, truncated)`` where
        *item_breakdown* maps tx_type → rarity → (count, total) and
        *truncated* is True when the fetch was capped at ``_MAX_PAGES * _PAGE_SIZE``.
        """
        client = await self._get_client()
        item_rarities, _item_names, item_types = await self._get_item_rarities()
        counts: dict[str, int] = {}
        totals: dict[str, float] = {}
        # {tx_type: {sub_key: (count, total)}}
        item_breakdown: dict[str, dict[str, tuple[int, float]]] = {}
        cursor: Optional[str] = None
        truncated = False

        for page in range(_MAX_PAGES):
            payload: dict = {"userId": user_id, "limit": _PAGE_SIZE}
            if cursor:
                payload["cursor"] = cursor

            try:
                raw = await client.post(
                    "/transaction.getPaginatedTransactions",
                    json=payload,
                )
            except Exception as exc:
                logger.warning(
                    "Transacties: fetch error page %d for %s: %s", page, user_id, exc
                )
                break

            data = _unwrap(raw) if isinstance(raw, dict) else {}
            if isinstance(data, dict):
                items: list = (
                    data.get("items")
                    or data.get("transactions")
                    or data.get("results")
                    or []
                )
                cursor = data.get("nextCursor") or data.get("cursor")
            elif isinstance(data, list):
                items = data
                cursor = None
            else:
                break

            for tx in items:
                if not isinstance(tx, dict):
                    continue
                tx_type: str = (
                    tx.get("type")
                    or tx.get("transactionType")
                    or "unknown"
                )
                # The coin amount is stored in the "money" field
                raw_amount = tx.get("money")
                amount = float(raw_amount) if raw_amount is not None else 0.0
                counts[tx_type] = counts.get(tx_type, 0) + 1
                totals[tx_type] = totals.get(tx_type, 0.0) + amount

                # Item sub-key breakdown
                if tx_type in _ITEM_BREAKDOWN_TYPES:
                    item_code: str = tx.get("itemCode") or ""
                    if tx_type in _BD_BY_RARITY:
                        # For craft/dismantle: read rarity from the item sub-object
                        # first (most accurate), fall back to gameConfig lookup.
                        item_obj = tx.get("item") or {}
                        if isinstance(item_obj, dict):
                            sub_key = (
                                item_obj.get("rarity")
                                or item_rarities.get(item_obj.get("code") or item_code, None)
                            )
                        else:
                            sub_key = None
                        if not sub_key:
                            sub_key = item_rarities.get(item_code, "?") if item_code else "?"
                    elif tx_type == "openCase":
                        # itemCode is the case container; map its rarity to a label
                        case_rarity = item_rarities.get(item_code, "legendary") if item_code else "legendary"
                        sub_key = _CASE_LABELS.get(case_rarity, "Case")
                    elif tx_type in _BD_BY_ITEM_NAME:
                        # trading: if the item is a case, show "Case"/"Elite case" label;
                        # otherwise show the item name (iron, steel, concrete, ammo, …)
                        if item_types.get(item_code) == "case":
                            case_rarity = item_rarities.get(item_code, "legendary")
                            sub_key = _CASE_LABELS.get(case_rarity, "Case")
                        else:
                            sub_key = _prettify_item_code(item_code) if item_code else "?"
                    else:
                        # itemMarket, battleLoot: show rarity
                        sub_key = item_rarities.get(item_code, "?") if item_code else "?"
                    if sub_key:
                        by_type = item_breakdown.setdefault(tx_type, {})
                        cur = by_type.get(sub_key, (0, 0.0))
                        by_type[sub_key] = (cur[0] + 1, cur[1] + amount)

            if not cursor or not items:
                break
        else:
            # Loop exhausted all pages without breaking — more data exists
            truncated = True

        return counts, totals, item_breakdown, truncated

    # ------------------------------------------------------------------ #
    # Command
    # ------------------------------------------------------------------ #

    @commands.hybrid_command(
        name="transacties",
        description="Bekijk het transactieoverzicht van een speler per type.",
    )
    @app_commands.describe(
        speler="De spelersnaam of in-game ID om op te zoeken.",
    )
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def transacties(
        self,
        ctx: Context,
        speler: str,
    ) -> None:
        """Show a per-type transaction breakdown (count + total CC) for a player."""
        if hasattr(ctx, "defer"):
            await ctx.defer()

        user_id, username = await self._resolve_user(speler)
        if not user_id:
            await ctx.send(
                f"Geen speler gevonden voor **{speler}**.\n"
                "-# Als de API momenteel offline is, probeer het later opnieuw."
            )
            return

        counts, totals, item_breakdown, truncated = await self._fetch_transactions(user_id)
        total_tx = sum(counts.values())

        if total_tx == 0:
            await ctx.send(
                f"Geen transacties gevonden voor **{username}**.\n"
                "Mogelijk zijn de transacties privé of is de speler onbekend."
            )
            return

        table = _build_table(counts, totals, item_breakdown, truncated, total_tx)
        embed = discord.Embed(
            title=f"💰 Transacties — {username}",
            description=f"```ansi\n{table}\n```",
            colour=self._embed_colour(),
        )
        try:
            await ctx.send(embed=embed)
        except discord.NotFound:
            logger.warning(
                "Transacties: interaction expired before response could be sent for %s",
                username,
            )


async def setup(bot: DiscordBot) -> None:
    await bot.add_cog(TransactiesCog(bot))
