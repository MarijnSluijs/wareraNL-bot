"""Database mixin for the player transaction cache used by /transacties."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional


class TxCacheMixin:
    """CRUD for the ``player_tx_cache`` table."""

    async def get_player_tx_cache(self, user_id: str) -> Optional[dict]:
        """Return the cached row for *user_id*, or ``None`` if absent."""
        async with self._conn.execute(
            "SELECT * FROM player_tx_cache WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    async def set_player_tx_cache(
        self,
        *,
        user_id: str,
        username: str,
        counts: dict,
        totals: dict,
        breakdown: dict,
        newest_tx_at: Optional[str],
        total_tx: int,
        truncated: bool,
    ) -> None:
        """Insert or replace the cache row for *user_id*."""
        # Breakdown contains tuple values — convert to lists for JSON serialisation.
        bd_json: dict = {}
        for tx_type, sub in breakdown.items():
            bd_json[tx_type] = {k: list(v) for k, v in sub.items()}

        await self._conn.execute(
            """
            INSERT INTO player_tx_cache
                (user_id, username, counts_json, totals_json, breakdown_json,
                 newest_tx_at, total_tx, truncated, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username       = excluded.username,
                counts_json    = excluded.counts_json,
                totals_json    = excluded.totals_json,
                breakdown_json = excluded.breakdown_json,
                newest_tx_at   = excluded.newest_tx_at,
                total_tx       = excluded.total_tx,
                truncated      = excluded.truncated,
                updated_at     = excluded.updated_at
            """,
            (
                user_id,
                username,
                json.dumps(counts),
                json.dumps(totals),
                json.dumps(bd_json),
                newest_tx_at,
                total_tx,
                1 if truncated else 0,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._conn.commit()

    async def delete_player_tx_cache(self, user_id: str) -> None:
        """Remove the cached transactions for *user_id* (e.g. force full refresh)."""
        await self._conn.execute(
            "DELETE FROM player_tx_cache WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()
