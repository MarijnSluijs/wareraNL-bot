"""Shared case-opening transaction fetching for luck calculations.

Used by the daily NL sweep (cogs/tasks/luck.py), the global sweep
(cogs/tasks/global_luck.py), and the /geluk + /globalluck commands.

transaction.getPaginatedTransactions pages newest-first (confirmed by direct
API testing: page 1 starts at the most recent transaction, each following
page continues further back in time). That makes an incremental fetch cheap:
page from the start and stop as soon as a transaction's _id <= cutoff_id is
reached — everything from there on was already counted by a previous call.
Without a cutoff, every sweep/command call re-fetched a player's ENTIRE
lifetime case-opening history from scratch, every time.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

RARITY_KEYS = ["mythic", "legendary", "epic", "rare", "uncommon", "common"]

logger = logging.getLogger("discord_bot")


async def fetch_case_transactions(
    client,
    user_id: str,
    item_rarities: dict[str, str],
    *,
    cutoff_id: Optional[str] = None,
    max_cases: Optional[int] = None,
) -> tuple[dict[str, int], dict[str, int], Optional[str], int]:
    """Page openCase transactions for *user_id*, newest first.

    Stops as soon as a transaction's _id <= cutoff_id is reached. Pass
    cutoff_id=None for a full history fetch (e.g. a player's first scan).

    max_cases, if given, additionally stops once that many normal (non-elite)
    openings have been collected — for "most recent N cases" requests. Not
    meant to be combined with cutoff_id (mutually exclusive use cases).

    Returns (normal_counts, elite_counts, newest_id_seen, total_fetched).
    newest_id_seen is the _id of the first (newest) transaction encountered,
    or None if the player has no openCase transactions at all.
    """
    normal_counts: dict[str, int] = {r: 0 for r in RARITY_KEYS}
    elite_counts: dict[str, int] = {r: 0 for r in RARITY_KEYS}
    cursor: Optional[str] = None
    newest_id: Optional[str] = None
    total_fetched = 0
    limit_reached = False

    while True:
        payload: dict = {"userId": user_id, "transactionType": "openCase", "limit": 100}
        if cursor:
            payload["cursor"] = cursor
        try:
            raw = await client.get(
                "/transaction.getPaginatedTransactions",
                params={"input": json.dumps(payload)},
            )
        except Exception:
            logger.warning("fetch_case_transactions: request failed for %s", user_id)
            break

        data = raw
        if isinstance(raw, dict):
            inner = raw.get("result", raw)
            data = inner.get("data", inner) if isinstance(inner, dict) else raw
        items = []
        if isinstance(data, dict):
            items = data.get("items") or data.get("transactions") or []
            cursor = data.get("nextCursor") or data.get("cursor")
        elif isinstance(data, list):
            items = data
            cursor = None

        stop = False
        for tx in items:
            if not isinstance(tx, dict):
                continue
            tx_id = str(tx.get("_id") or "")
            if cutoff_id and tx_id and tx_id <= cutoff_id:
                stop = True
                break
            if newest_id is None and tx_id:
                newest_id = tx_id  # first item on the first page is the newest
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
            total_fetched += 1

        if not cursor or not items or stop or limit_reached:
            break

    return normal_counts, elite_counts, newest_id, total_fetched


def merge_counts(base: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    """Add delta rarity counts onto a base counts dict (for incremental merges)."""
    merged = dict(base)
    for k, v in delta.items():
        merged[k] = merged.get(k, 0) + v
    return merged
