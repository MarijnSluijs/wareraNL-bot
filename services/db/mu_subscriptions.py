"""MuSubscriptionsMixin — weekly damage reports and auction-win pings per channel."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone


class MuSubscriptionsMixin:
    """Mixin for mu_weekly_report_subs and mu_auction_win_subs tables."""

    # ── Weekly damage report subscriptions ────────────────────────────────────

    async def add_weekly_report_sub(
        self, channel_id: str, guild_id: str, mu_name: str, mu_id: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO mu_weekly_report_subs
                (channel_id, guild_id, mu_name, mu_id, added_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, mu_name) DO UPDATE SET
                mu_id    = excluded.mu_id,
                guild_id = excluded.guild_id
            """,
            (channel_id, guild_id, mu_name, mu_id, now),
        )
        await self._conn.commit()

    async def remove_weekly_report_sub(self, channel_id: str, mu_name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM mu_weekly_report_subs WHERE channel_id = ? AND mu_name = ?",
            (channel_id, mu_name),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def has_weekly_report_sub(self, channel_id: str, mu_name: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM mu_weekly_report_subs WHERE channel_id = ? AND mu_name = ?",
            (channel_id, mu_name),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_all_weekly_report_subs(self) -> list[dict]:
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT channel_id, guild_id, mu_name, mu_id, last_posted_at"
            " FROM mu_weekly_report_subs"
        ) as cur:
            async for row in cur:
                rows.append({
                    "channel_id":    row[0],
                    "guild_id":      row[1],
                    "mu_name":       row[2],
                    "mu_id":         row[3],
                    "last_posted_at": row[4],
                })
        return rows

    async def update_weekly_report_posted(
        self, channel_id: str, mu_name: str, posted_at: str
    ) -> None:
        await self._conn.execute(
            "UPDATE mu_weekly_report_subs SET last_posted_at = ?"
            " WHERE channel_id = ? AND mu_name = ?",
            (posted_at, channel_id, mu_name),
        )
        await self._conn.commit()

    async def try_claim_weekly_post(
        self, channel_id: str, mu_name: str
    ) -> bool:
        """Atomically mark this subscription as posted for the current week.

        Returns True if the claim succeeded (caller should post).
        Returns False if already posted within the last 6 days (caller should skip).
        The write happens BEFORE the caller sends the Discord message so that a
        failed send never causes a duplicate on the next loop iteration.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=6)).isoformat()
        cur = await self._conn.execute(
            "UPDATE mu_weekly_report_subs SET last_posted_at = ?"
            " WHERE channel_id = ? AND mu_name = ?"
            " AND (last_posted_at IS NULL OR last_posted_at < ?)",
            (now.isoformat(), channel_id, mu_name, cutoff),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    # ── Auction-win subscriptions ──────────────────────────────────────────────

    async def add_auction_win_sub(
        self,
        channel_id: str,
        guild_id: str,
        mu_name: str,
        mu_id: str,
        cutoff_at: str | None = None,
        ping_enabled: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            """
            INSERT INTO mu_auction_win_subs
                (channel_id, guild_id, mu_name, mu_id, seen_ids, initialized, cutoff_at, added_at, ping_enabled)
            VALUES (?, ?, ?, ?, '[]', 1, ?, ?, ?)
            ON CONFLICT(channel_id, mu_name) DO UPDATE SET
                mu_id        = excluded.mu_id,
                guild_id     = excluded.guild_id,
                seen_ids     = '[]',
                initialized  = 1,
                cutoff_at    = excluded.cutoff_at,
                ping_enabled = excluded.ping_enabled
            """,
            (channel_id, guild_id, mu_name, mu_id, cutoff_at, now, int(ping_enabled)),
        )
        await self._conn.commit()

    async def update_auction_cutoff(
        self, channel_id: str, mu_name: str, cutoff_at: str
    ) -> None:
        await self._conn.execute(
            "UPDATE mu_auction_win_subs SET cutoff_at = ?"
            " WHERE channel_id = ? AND mu_name = ?",
            (cutoff_at, channel_id, mu_name),
        )
        await self._conn.commit()

    async def remove_auction_win_sub(self, channel_id: str, mu_name: str) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM mu_auction_win_subs WHERE channel_id = ? AND mu_name = ?",
            (channel_id, mu_name),
        )
        await self._conn.commit()
        return (cur.rowcount or 0) > 0

    async def has_auction_win_sub(self, channel_id: str, mu_name: str) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM mu_auction_win_subs WHERE channel_id = ? AND mu_name = ?",
            (channel_id, mu_name),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_all_auction_win_subs(self) -> list[dict]:
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT channel_id, guild_id, mu_name, mu_id, seen_ids, cutoff_at, ping_enabled"
            " FROM mu_auction_win_subs"
        ) as cur:
            async for row in cur:
                try:
                    seen: list[str] = json.loads(row[4] or "[]")
                except (ValueError, TypeError):
                    seen = []
                rows.append({
                    "channel_id":   row[0],
                    "guild_id":     row[1],
                    "mu_name":      row[2],
                    "mu_id":        row[3],
                    "seen_ids":     seen,
                    "cutoff_at":    row[5] or "",
                    "ping_enabled": bool(row[6]),
                })
        return rows

    async def update_auction_seen_ids(
        self, channel_id: str, mu_name: str, seen_ids: list[str]
    ) -> None:
        await self._conn.execute(
            "UPDATE mu_auction_win_subs SET seen_ids = ?, initialized = 1"
            " WHERE channel_id = ? AND mu_name = ?",
            (json.dumps(seen_ids[-500:]), channel_id, mu_name),
        )
        await self._conn.commit()
