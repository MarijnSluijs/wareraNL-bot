"""DB helpers for citizen_pill_tracking — hourly pill buff/debuff state for all NL players."""

from __future__ import annotations

import time
from typing import Optional

_DEBUFF_DURATION = 16 * 3600  # seconds


class PillTrackingMixin:
    """Methods for the ``citizen_pill_tracking`` table."""

    async def upsert_pill_tracking(
        self,
        user_id: str,
        country_id: str,
        buff_expires_at: Optional[int],
        updated_at: str,
    ) -> None:
        """Insert or update a player's pill buff expiry.

        *buff_expires_at* is a Unix timestamp (seconds) for when the buff ends/ended.
        Pass ``None`` to keep the existing value unchanged (so we don't erase old
        expiry data when the API shows no active buff — that would break debuff detection).
        """
        if buff_expires_at is not None:
            await self._conn.execute(
                """
                INSERT INTO citizen_pill_tracking (user_id, country_id, buff_expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    country_id      = excluded.country_id,
                    buff_expires_at = excluded.buff_expires_at,
                    updated_at      = excluded.updated_at
                """,
                (user_id, country_id, buff_expires_at, updated_at),
            )
        else:
            # Only create a row if none exists; don't overwrite buff_expires_at with NULL
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO citizen_pill_tracking (user_id, country_id, buff_expires_at, updated_at)
                VALUES (?, ?, NULL, ?)
                """,
                (user_id, country_id, updated_at),
            )
        await self._conn.commit()

    async def bulk_upsert_pill_tracking(
        self,
        rows: list[tuple[str, str, Optional[int], str]],
    ) -> None:
        """Bulk upsert (user_id, country_id, buff_expires_at, updated_at) rows.

        For rows where buff_expires_at is not None, always overwrite.
        For rows where it IS None, only insert a placeholder if no row exists yet.
        """
        now_rows = [(uid, cid, exp, upd) for uid, cid, exp, upd in rows if exp is not None]
        null_rows = [(uid, cid, upd) for uid, cid, exp, upd in rows if exp is None]

        if now_rows:
            await self._conn.executemany(
                """
                INSERT INTO citizen_pill_tracking (user_id, country_id, buff_expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    country_id      = excluded.country_id,
                    buff_expires_at = excluded.buff_expires_at,
                    updated_at      = excluded.updated_at
                """,
                now_rows,
            )
        if null_rows:
            await self._conn.executemany(
                """
                INSERT OR IGNORE INTO citizen_pill_tracking (user_id, country_id, buff_expires_at, updated_at)
                VALUES (?, ?, NULL, ?)
                """,
                null_rows,
            )
        await self._conn.commit()

    async def get_pill_tracking_bulk(self, user_ids: list[str]) -> dict[str, int | None]:
        """Return {user_id: buff_expires_at} for the given user IDs."""
        if not user_ids:
            return {}
        placeholders = ",".join("?" * len(user_ids))
        result: dict[str, int | None] = {}
        async with self._conn.execute(
            f"SELECT user_id, buff_expires_at FROM citizen_pill_tracking WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        ) as cur:
            async for row_uid, expires_at in cur:
                result[row_uid] = expires_at
        return result

    async def get_pill_stats_from_tracking(
        self,
        *,
        country_id: str | None = None,
        mu_names: list[str] | None = None,
    ) -> dict:
        """Aggregate pill status counts for all tracked citizens in a group.

        Joins ``citizen_pill_tracking`` with ``citizen_levels`` when filtering by MU.
        Returns ``{buff, debuff, none, total, avg_buff_secs, avg_debuff_secs}``.
        """
        now = int(time.time())

        if mu_names:
            placeholders = ",".join("?" * len(mu_names))
            sql = f"""
                SELECT pt.buff_expires_at
                FROM citizen_pill_tracking pt
                JOIN citizen_levels cl ON cl.user_id = pt.user_id
                WHERE cl.mu_name IN ({placeholders})
            """
            params: tuple = tuple(mu_names)
        elif country_id:
            sql = """
                SELECT buff_expires_at
                FROM citizen_pill_tracking
                WHERE country_id = ?
            """
            params = (country_id,)
        else:
            return {"buff": 0, "debuff": 0, "none": 0, "total": 0,
                    "avg_buff_secs": 0.0, "avg_debuff_secs": 0.0}

        buff = debuff = none_ = 0
        buff_secs: list[float] = []
        debuff_secs: list[float] = []

        async with self._conn.execute(sql, params) as cur:
            async for (expires_at,) in cur:
                if expires_at is None:
                    none_ += 1
                elif expires_at > now:
                    buff += 1
                    buff_secs.append(float(expires_at - now))
                elif expires_at + _DEBUFF_DURATION > now:
                    debuff += 1
                    debuff_secs.append(float(expires_at + _DEBUFF_DURATION - now))
                else:
                    none_ += 1

        return {
            "buff": buff,
            "debuff": debuff,
            "none": none_,
            "total": buff + debuff + none_,
            "avg_buff_secs": sum(buff_secs) / len(buff_secs) if buff_secs else 0.0,
            "avg_debuff_secs": sum(debuff_secs) / len(debuff_secs) if debuff_secs else 0.0,
        }
