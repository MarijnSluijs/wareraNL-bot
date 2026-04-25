"""Database mixin + helpers for the ``item_trades`` table.

Stores ``itemMarket`` transactions polled hourly from
``/transaction.getPaginatedTransactions`` and exposes helpers for the
``/marktprijs`` command: fetching candidate rows, ranking by weighted
stat-distance to a query, and aggregating prices over time windows.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Iterable, Optional

logger = logging.getLogger("services.db.trades")


# Payload skill-key (camelCase) ↔ DB column (snake_case). Source of truth for
# both insert and query paths — keep in sync.
SKILL_PAYLOAD_TO_COLUMN: dict[str, str] = {
    "attack": "attack",
    "criticalChance": "critical_chance",
    "criticalDamages": "critical_damages",
    "armor": "armor",
    "precision": "precision_",
    "dodge": "dodge",
}

# Stats used for distance matching: the 6 skills above plus state (durability).
STAT_COLUMNS: tuple[str, ...] = (
    "attack",
    "critical_chance",
    "critical_damages",
    "armor",
    "precision_",
    "dodge",
    "state",
)


def _int_or_none(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract_trade_fields(tx: dict) -> Optional[dict]:
    """Flatten a raw itemMarket payload into column values for ``item_trades``.

    Returns ``None`` if the payload lacks the bare minimum (id, itemCode, money).
    """
    if tx.get("transactionType") != "itemMarket":
        return None

    tx_id = tx.get("_id") or tx.get("id")
    item_code = tx.get("itemCode")
    money = tx.get("money")
    if not tx_id or not item_code or money is None:
        return None

    item = tx.get("item") if isinstance(tx.get("item"), dict) else {}
    skills = item.get("skills") if isinstance(item.get("skills"), dict) else {}

    row: dict = {
        "tx_id": str(tx_id),
        "created_at": tx.get("createdAt"),
        "offer_created_at": tx.get("offerCreatedAt"),
        "item_code": str(item_code),
        "item_type": item.get("type"),
        "quantity": int(tx.get("quantity") or 1),
        "price": int(money),
        "state": _int_or_none(item.get("state")),
        "max_state": _int_or_none(item.get("maxState")),
        "buyer_id": tx.get("buyerId"),
        "seller_id": tx.get("sellerId"),
        "raw_json": json.dumps(tx, ensure_ascii=False),
    }
    for payload_key, col in SKILL_PAYLOAD_TO_COLUMN.items():
        row[col] = _int_or_none(skills.get(payload_key))
    return row


class TradesMixin:
    """CRUD + query helpers for ``item_trades``."""

    _INSERT_SQL = """
        INSERT OR IGNORE INTO item_trades (
            tx_id, created_at, offer_created_at, item_code, item_type,
            quantity, price, state, max_state,
            attack, critical_chance, critical_damages, armor, precision_, dodge,
            buyer_id, seller_id, raw_json
        ) VALUES (
            :tx_id, :created_at, :offer_created_at, :item_code, :item_type,
            :quantity, :price, :state, :max_state,
            :attack, :critical_chance, :critical_damages, :armor, :precision_, :dodge,
            :buyer_id, :seller_id, :raw_json
        )
    """

    async def upsert_trade(self, tx: dict) -> bool:
        """Insert one trade if new. Returns ``True`` if a row was inserted."""
        row = extract_trade_fields(tx)
        if row is None:
            return False
        cursor = await self._conn.execute(self._INSERT_SQL, row)
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def upsert_trades(self, txs: Iterable[dict]) -> tuple[int, int]:
        """Insert many trades. Returns ``(inserted, seen)``.

        Uses total_changes() delta because changes() after executemany only
        reflects the final statement in the batch.
        """
        rows = [r for r in (extract_trade_fields(tx) for tx in txs) if r is not None]
        if not rows:
            return 0, 0
        async with self._conn.execute("SELECT total_changes()") as cur:
            before_row = await cur.fetchone()
        before = int(before_row[0]) if before_row else 0
        await self._conn.executemany(self._INSERT_SQL, rows)
        await self._conn.commit()
        async with self._conn.execute("SELECT total_changes()") as cur:
            after_row = await cur.fetchone()
        after = int(after_row[0]) if after_row else 0
        inserted = max(0, after - before)
        return inserted, len(rows)

    async def fetch_trades_for_match(
        self, item_code: str, since_iso: Optional[str] = None
    ) -> list[dict]:
        """Return all rows for *item_code*, optionally only newer than *since_iso*."""
        sql = (
            "SELECT created_at, price, quantity, state, max_state, "
            "attack, critical_chance, critical_damages, armor, precision_, dodge "
            "FROM item_trades WHERE item_code = ?"
        )
        params: list = [item_code]
        if since_iso:
            sql += " AND created_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY created_at DESC"
        async with self._conn.execute(sql, params) as cur:
            cols = [d[0] for d in cur.description]
            rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    async def distinct_item_codes(
        self, prefix: str = "", limit: int = 25
    ) -> list[str]:
        """Unique item_codes in the table, optionally filtered by prefix/substring."""
        if prefix:
            sql = (
                "SELECT DISTINCT item_code FROM item_trades "
                "WHERE item_code LIKE ? ORDER BY item_code LIMIT ?"
            )
            params = [f"%{prefix}%", limit]
        else:
            sql = "SELECT DISTINCT item_code FROM item_trades ORDER BY item_code LIMIT ?"
            params = [limit]
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def item_trades_count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM item_trades") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0


# ────────────────────────────────────────────────────────────────────────────
# Pure functions used by /marktprijs — kept out of the mixin so they're easy
# to unit-test without a DB connection.
# ────────────────────────────────────────────────────────────────────────────


def _unit_price(row: dict) -> float:
    qty = row.get("quantity") or 1
    return float(row["price"]) / qty if qty else float(row["price"])


def compute_norms(rows: list[dict]) -> dict[str, float]:
    """Per-stat scale factor = max observed value in *rows* (fallback 1.0).

    Dividing (row.stat - query.stat) by this yields a roughly 0..1 unit so
    different stats (e.g. armor up to 200, dodge up to ~30) contribute
    comparably to the Euclidean distance.
    """
    norms: dict[str, float] = {}
    for col in STAT_COLUMNS:
        m = 0
        for r in rows:
            v = r.get(col)
            if v is not None and v > m:
                m = v
        norms[col] = float(m) if m > 0 else 1.0
    return norms


def weighted_distance(
    query: dict[str, Optional[int]], row: dict, norms: dict[str, float]
) -> Optional[float]:
    """Normalised Euclidean distance over stats the user actually supplied.

    Returns ``None`` if no supplied stat has a counterpart in *row*.
    """
    sq = 0.0
    used = 0
    for col in STAT_COLUMNS:
        qv = query.get(col)
        rv = row.get(col)
        if qv is None or rv is None:
            continue
        norm = norms.get(col, 1.0) or 1.0
        diff = (float(rv) - float(qv)) / norm
        sq += diff * diff
        used += 1
    if used == 0:
        return None
    return math.sqrt(sq / used)  # averaged so distance doesn't explode with many stats


def rank_matches(
    query: dict[str, Optional[int]], rows: list[dict], top_k: int = 20
) -> list[tuple[dict, float]]:
    """Rank *rows* by distance to *query*; keep only rows with a valid distance."""
    norms = compute_norms(rows)
    scored: list[tuple[dict, float]] = []
    for r in rows:
        d = weighted_distance(query, r, norms)
        if d is None:
            continue
        scored.append((r, d))
    scored.sort(key=lambda t: t[1])
    return scored[:top_k]


def aggregate(
    rows_with_dist: list[tuple[dict, float]],
    window_cutoff_iso: str,
) -> dict:
    """Aggregate price stats for rows whose created_at >= *window_cutoff_iso*.

    Returns ``{n, mean, weighted, min, max}`` (price per unit).
    *weighted* uses ``1 / (1 + distance)`` so closer matches dominate.
    """
    prices: list[float] = []
    wsum = 0.0
    wtot = 0.0
    for row, dist in rows_with_dist:
        ca = row.get("created_at") or ""
        if ca < window_cutoff_iso:
            continue
        up = _unit_price(row)
        prices.append(up)
        w = 1.0 / (1.0 + dist)
        wsum += up * w
        wtot += w
    if not prices:
        return {"n": 0, "mean": None, "weighted": None, "min": None, "max": None}
    return {
        "n": len(prices),
        "mean": sum(prices) / len(prices),
        "weighted": wsum / wtot if wtot > 0 else None,
        "min": min(prices),
        "max": max(prices),
    }
