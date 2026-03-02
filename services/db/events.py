"""Event-related DB methods (seen_articles, seen_events, war_events)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiosqlite


class EventsMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """seen_articles, seen_events, war_events tables."""

    # ── Articles ─────────────────────────────────────────────────────────────

    async def has_seen_article(self, article_id: str) -> bool:
        """Check if an article has already been marked as seen."""
        async with self._conn.execute(
            "SELECT 1 FROM seen_articles WHERE article_id = ?", (article_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_article_seen(self, article_id: str) -> None:
        """Mark an article as seen with the current timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR IGNORE INTO seen_articles(article_id, seen_at) VALUES(?, ?)",
            (article_id, now),
        )
        await self._conn.commit()

    # ── Events ────────────────────────────────────────────────────────────────

    async def has_seen_event(self, event_id: str) -> bool:
        """Check if an event has already been marked as seen."""
        async with self._conn.execute(
            "SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_event_seen(self, event_id: str) -> None:
        """Mark an event as seen with the current timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR IGNORE INTO seen_events(event_id, seen_at) VALUES(?, ?)",
            (event_id, now),
        )
        await self._conn.commit()

    async def store_war_event(
        self,
        event_id: str,
        event_type: str,
        battle_id: Optional[str],
        war_id: Optional[str],
        attacker_country_id: Optional[str],
        defender_country_id: Optional[str],
        region_id: Optional[str],
        region_name: Optional[str],
        attacker_name: Optional[str],
        defender_name: Optional[str],
        created_at: Optional[str],
        raw_json: str,
    ) -> None:
        """Insert or update a war event record."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO war_events
                (event_id, event_type, battle_id, war_id,
                 attacker_country_id, defender_country_id,
                 region_id, region_name, attacker_name, defender_name,
                 created_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id, event_type, battle_id, war_id,
                attacker_country_id, defender_country_id,
                region_id, region_name, attacker_name, defender_name,
                created_at, raw_json,
            ),
        )
        await self._conn.commit()
