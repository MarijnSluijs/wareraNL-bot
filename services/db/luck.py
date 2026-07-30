"""Luck score DB methods (citizen_luck table)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class LuckMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """citizen_luck table operations."""

    async def upsert_luck_score(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        luck_score: float,
        opens_count: int,
        rarity_json: Optional[str],
        updated_at: str,
        *,
        elite_luck_score: Optional[float] = None,
        elite_opens_count: Optional[int] = None,
        elite_rarity_json: Optional[str] = None,
        last_seen_transaction_id: Optional[str] = None,
    ) -> None:
        """Insert or replace a luck score (call flush_luck_scores to commit batch)."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO citizen_luck
                (user_id, country_id, citizen_name, luck_score, opens_count, rarity_json,
                 updated_at, elite_luck_score, elite_opens_count, elite_rarity_json,
                 last_seen_transaction_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                country_id,
                citizen_name,
                luck_score,
                opens_count,
                rarity_json,
                updated_at,
                elite_luck_score,
                elite_opens_count,
                elite_rarity_json,
                last_seen_transaction_id,
            ),
        )

    async def flush_luck_scores(self) -> None:
        """Commit pending luck score upserts."""
        await self._conn.commit()

    async def delete_luck_scores_for_country(self, country_id: str) -> None:
        """Delete all luck score records for a specific country."""
        await self._conn.execute(
            "DELETE FROM citizen_luck WHERE country_id = ?", (country_id,)
        )
        await self._conn.commit()

    async def get_luck_ranking(self, country_id: str) -> list[dict]:
        """All luck entries for a country, sorted by luck_score DESC."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT user_id, citizen_name, luck_score, opens_count, updated_at,
                   elite_luck_score, elite_opens_count, elite_rarity_json
            FROM citizen_luck
            WHERE country_id = ?
            ORDER BY luck_score DESC
            """,
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "citizen_name": row[1] or row[0],
                        "luck_score": row[2],
                        "opens_count": row[3],
                        "updated_at": row[4],
                        "elite_luck_score": row[5],
                        "elite_opens_count": row[6],
                        "elite_rarity_json": row[7],
                    }
                )
        return rows

    async def get_luck_entry_by_user_id(self, user_id: str) -> Optional[dict]:
        """Exact-match lookup of a cached luck row by WarEra user_id.

        Preferred over get_luck_entry_by_name whenever the caller already has
        the resolved user_id — no fuzzy matching, always the right player.
        """
        async with self._conn.execute(
            """
            SELECT user_id, citizen_name, country_id, luck_score, opens_count,
                   rarity_json, updated_at, elite_luck_score, elite_opens_count,
                   elite_rarity_json, last_seen_transaction_id
            FROM citizen_luck
            WHERE user_id = ?
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0],
            "citizen_name": row[1],
            "country_id": row[2],
            "luck_score": row[3],
            "opens_count": row[4],
            "rarity_json": row[5],
            "updated_at": row[6],
            "elite_luck_score": row[7],
            "elite_opens_count": row[8],
            "elite_rarity_json": row[9],
            "last_seen_transaction_id": row[10],
        }

    async def get_luck_entry_by_name(
        self, name: str, cutoff: float = 0.55
    ) -> Optional[dict]:
        """Fuzzy-match a player by citizen_name in citizen_luck.

        Returns the full luck row dict (with rarity_json, luck_score, etc.) or None.
        """
        import difflib  # local import to avoid circular

        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT user_id, citizen_name, country_id, luck_score, opens_count,
                   rarity_json, updated_at, elite_luck_score, elite_opens_count,
                   elite_rarity_json, last_seen_transaction_id
            FROM citizen_luck
            WHERE citizen_name IS NOT NULL
            """
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "citizen_name": row[1],
                        "country_id": row[2],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                        "elite_luck_score": row[7],
                        "elite_opens_count": row[8],
                        "elite_rarity_json": row[9],
                        "last_seen_transaction_id": row[10],
                    }
                )
        if not rows:
            return None
        q_low = name.lower().strip()
        best: Optional[dict] = None
        best_ratio = cutoff
        for entry in rows:
            n = (entry["citizen_name"] or "").lower()
            ratio = difflib.SequenceMatcher(None, q_low, n).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = entry
        return best

    async def get_luck_entries_for_country(self, country_id: str) -> dict[str, dict]:
        """Bulk-load existing citizen_luck rows for a country, keyed by user_id.

        Used by the daily sweep to preload prior counts + last_seen_transaction_id
        for every citizen in one query, instead of one query per citizen.
        """
        result: dict[str, dict] = {}
        async with self._conn.execute(
            """
            SELECT user_id, rarity_json, elite_rarity_json, last_seen_transaction_id
            FROM citizen_luck
            WHERE country_id = ?
            """,
            (country_id,),
        ) as cur:
            async for row in cur:
                result[row[0]] = {
                    "rarity_json": row[1],
                    "elite_rarity_json": row[2],
                    "last_seen_transaction_id": row[3],
                }
        return result

    async def delete_luck_scores_not_in(
        self, country_id: str, keep_user_ids: list[str]
    ) -> None:
        """Remove citizen_luck rows for *country_id* whose user_id isn't in keep_user_ids.

        Cleans up citizens who left the tracked country since the last sweep.
        Safe to call with an empty keep list (deletes nothing, to avoid
        wiping the whole country on a degenerate empty citizen list).

        Uses a temp table rather than chunking a NOT IN list directly: unlike
        IN, NOT IN can't be split into independent chunked queries (each
        chunk would wrongly delete rows that only appear in a different
        chunk), so every id must be visible to one single query.
        """
        if not keep_user_ids:
            return
        await self._conn.execute("DROP TABLE IF EXISTS _luck_keep")
        await self._conn.execute("CREATE TEMP TABLE _luck_keep (user_id TEXT PRIMARY KEY)")
        await self._conn.executemany(
            "INSERT OR IGNORE INTO _luck_keep (user_id) VALUES (?)",
            [(uid,) for uid in keep_user_ids],
        )
        await self._conn.execute(
            "DELETE FROM citizen_luck WHERE country_id = ? "
            "AND user_id NOT IN (SELECT user_id FROM _luck_keep)",
            (country_id,),
        )
        await self._conn.execute("DROP TABLE _luck_keep")
        await self._conn.commit()

    async def get_citizens_for_luck_refresh(
        self, country_id: str
    ) -> list[tuple[str, Optional[str]]]:
        """(user_id, citizen_name) for all cached citizens of a country."""
        rows: list[tuple[str, Optional[str]]] = []
        async with self._conn.execute(
            "SELECT user_id, citizen_name FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows

    async def get_all_citizens_for_global_luck(
        self,
    ) -> list[tuple[str, str, Optional[str]]]:
        """(user_id, country_id, citizen_name) for all cached citizens across all countries."""
        rows: list[tuple[str, str, Optional[str]]] = []
        async with self._conn.execute(
            "SELECT user_id, country_id, citizen_name FROM citizen_levels ORDER BY country_id"
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2]))
        return rows

    # ── Global luck (all countries) ───────────────────────────────────────────

    async def upsert_global_luck_score(
        self,
        user_id: str,
        country_id: str,
        citizen_name: Optional[str],
        luck_score: float,
        opens_count: int,
        rarity_json: Optional[str],
        updated_at: str,
        *,
        elite_luck_score: Optional[float] = None,
        elite_opens_count: Optional[int] = None,
        elite_rarity_json: Optional[str] = None,
        last_seen_transaction_id: Optional[str] = None,
    ) -> None:
        """Insert or replace a global luck entry (call flush_global_luck_scores to commit)."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO global_citizen_luck
                (user_id, country_id, citizen_name, luck_score, opens_count, rarity_json,
                 updated_at, elite_luck_score, elite_opens_count, elite_rarity_json,
                 last_seen_transaction_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                country_id,
                citizen_name,
                luck_score,
                opens_count,
                rarity_json,
                updated_at,
                elite_luck_score,
                elite_opens_count,
                elite_rarity_json,
                last_seen_transaction_id,
            ),
        )

    async def flush_global_luck_scores(self) -> None:
        """Commit pending global luck upserts."""
        await self._conn.commit()

    async def clear_global_luck(self) -> None:
        """Delete all global luck entries.

        No longer called by the sweep itself (which is now incremental and
        must preserve prior rows to merge deltas onto) — kept for manual
        recovery/debugging use only.
        """
        await self._conn.execute("DELETE FROM global_citizen_luck")
        await self._conn.commit()

    async def get_all_global_luck_entries(self) -> dict[str, dict]:
        """Bulk-load existing global_citizen_luck rows, keyed by user_id.

        Used by the global sweep to preload prior counts + last_seen_transaction_id
        for every citizen in one query, instead of one query per citizen.
        """
        result: dict[str, dict] = {}
        async with self._conn.execute(
            "SELECT user_id, rarity_json, elite_rarity_json, last_seen_transaction_id "
            "FROM global_citizen_luck"
        ) as cur:
            async for row in cur:
                result[row[0]] = {
                    "rarity_json": row[1],
                    "elite_rarity_json": row[2],
                    "last_seen_transaction_id": row[3],
                }
        return result

    async def delete_global_luck_scores_not_in(self, keep_user_ids: list[str]) -> None:
        """Remove global_citizen_luck rows whose user_id isn't in keep_user_ids.

        See delete_luck_scores_not_in for why this uses a temp table instead
        of chunking (NOT IN can't be split into independent chunked queries).
        """
        if not keep_user_ids:
            return
        await self._conn.execute("DROP TABLE IF EXISTS _global_luck_keep")
        await self._conn.execute("CREATE TEMP TABLE _global_luck_keep (user_id TEXT PRIMARY KEY)")
        await self._conn.executemany(
            "INSERT OR IGNORE INTO _global_luck_keep (user_id) VALUES (?)",
            [(uid,) for uid in keep_user_ids],
        )
        await self._conn.execute(
            "DELETE FROM global_citizen_luck "
            "WHERE user_id NOT IN (SELECT user_id FROM _global_luck_keep)"
        )
        await self._conn.execute("DROP TABLE _global_luck_keep")
        await self._conn.commit()

    async def get_global_luck_ranking(
        self,
        limit: Optional[int] = None,
        order: str = "DESC",
    ) -> list[dict]:
        """Global luck entries sorted by luck_score DESC (or ASC for bottom)."""
        order = "ASC" if order.upper() == "ASC" else "DESC"
        sql = f"""
            SELECT user_id, country_id, citizen_name, luck_score, opens_count, rarity_json,
                   updated_at, elite_luck_score, elite_opens_count, elite_rarity_json
            FROM global_citizen_luck
            ORDER BY luck_score {order}
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows: list[dict] = []
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "country_id": row[1],
                        "citizen_name": row[2] or row[0],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                        "elite_luck_score": row[7],
                        "elite_opens_count": row[8],
                        "elite_rarity_json": row[9],
                    }
                )
        return rows

    async def get_global_luck_rank(self, user_id: str) -> tuple[Optional[int], int]:
        """Return (1-based rank, total) for a player in global_citizen_luck by luck_score."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM global_citizen_luck"
        ) as cur:
            total: int = (await cur.fetchone() or (0,))[0]
        async with self._conn.execute(
            """
            SELECT COUNT(*) + 1 FROM global_citizen_luck
            WHERE luck_score > (SELECT luck_score FROM global_citizen_luck WHERE user_id = ?)
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            rank: Optional[int] = row[0] if row else None
        return rank, total

    async def get_global_luck_rank_elite(self, user_id: str) -> tuple[Optional[int], int]:
        """Return (1-based rank, total) for a player in global_citizen_luck by elite_luck_score.

        Returns (None, total) when the player has no elite score recorded.
        """
        async with self._conn.execute(
            "SELECT COUNT(*) FROM global_citizen_luck WHERE elite_luck_score IS NOT NULL"
        ) as cur:
            total: int = (await cur.fetchone() or (0,))[0]
        # Check whether this user has an elite score at all
        async with self._conn.execute(
            "SELECT elite_luck_score FROM global_citizen_luck WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row is None or row[0] is None:
                return None, total
        async with self._conn.execute(
            """
            SELECT COUNT(*) + 1 FROM global_citizen_luck
            WHERE elite_luck_score > (
                SELECT elite_luck_score FROM global_citizen_luck WHERE user_id = ?
            ) AND elite_luck_score IS NOT NULL
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            rank: Optional[int] = row[0] if row else None
        return rank, total

    async def get_global_luck_rank_combined(self, user_id: str) -> tuple[Optional[int], int]:
        """Return (1-based rank, total) for a player by combined luck score.

        Combined = (luck_score + elite_luck_score) / 2 when both are available,
        otherwise falls back to whichever score is present.
        """
        async with self._conn.execute(
            "SELECT COUNT(*) FROM global_citizen_luck"
        ) as cur:
            total: int = (await cur.fetchone() or (0,))[0]
        async with self._conn.execute(
            """
            SELECT COUNT(*) + 1 FROM global_citizen_luck
            WHERE (luck_score + COALESCE(elite_luck_score, luck_score)) / 2.0
                  > (SELECT (luck_score + COALESCE(elite_luck_score, luck_score)) / 2.0
                     FROM global_citizen_luck WHERE user_id = ?)
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            rank: Optional[int] = row[0] if row else None
        return rank, total

    async def get_global_luck_ranking_combined(
        self,
        limit: Optional[int] = None,
        order: str = "DESC",
    ) -> list[dict]:
        """Global luck sorted by combined score (average of normal + elite when both available)."""
        order = "ASC" if order.upper() == "ASC" else "DESC"
        sql = f"""
            SELECT user_id, country_id, citizen_name, luck_score, opens_count, rarity_json,
                   updated_at, elite_luck_score, elite_opens_count, elite_rarity_json,
                   (luck_score + COALESCE(elite_luck_score, luck_score)) / 2.0 AS combined_score
            FROM global_citizen_luck
            ORDER BY combined_score {order}
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows: list[dict] = []
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "country_id": row[1],
                        "citizen_name": row[2] or row[0],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                        "elite_luck_score": row[7],
                        "elite_opens_count": row[8],
                        "elite_rarity_json": row[9],
                        "combined_score": row[10],
                    }
                )
        return rows

    async def get_global_luck_ranking_elite(
        self,
        limit: Optional[int] = None,
        order: str = "DESC",
    ) -> list[dict]:
        """Global luck entries sorted by elite_luck_score (players without elite score excluded)."""
        order = "ASC" if order.upper() == "ASC" else "DESC"
        sql = f"""
            SELECT user_id, country_id, citizen_name, luck_score, opens_count, rarity_json,
                   updated_at, elite_luck_score, elite_opens_count, elite_rarity_json
            FROM global_citizen_luck
            WHERE elite_luck_score IS NOT NULL
            ORDER BY elite_luck_score {order}
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows: list[dict] = []
        async with self._conn.execute(sql) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "country_id": row[1],
                        "citizen_name": row[2] or row[0],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                        "elite_luck_score": row[7],
                        "elite_opens_count": row[8],
                        "elite_rarity_json": row[9],
                    }
                )
        return rows

    async def search_global_luck_by_name(self, name: str) -> list[dict]:
        """Case-insensitive name search in global_citizen_luck.

        Includes the elite_* and last_seen_transaction_id columns — a prior
        version of this query omitted them, which silently meant /globalluck's
        cached-fallback path never had elite luck data to show.
        """
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT user_id, country_id, citizen_name, luck_score, opens_count, rarity_json,
                   updated_at, elite_luck_score, elite_opens_count, elite_rarity_json,
                   last_seen_transaction_id
            FROM global_citizen_luck
            WHERE lower(citizen_name) LIKE lower(?)
            ORDER BY luck_score DESC
            """,
            (f"%{name}%",),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "country_id": row[1],
                        "citizen_name": row[2] or row[0],
                        "luck_score": row[3],
                        "opens_count": row[4],
                        "rarity_json": row[5],
                        "updated_at": row[6],
                        "elite_luck_score": row[7],
                        "elite_opens_count": row[8],
                        "elite_rarity_json": row[9],
                        "last_seen_transaction_id": row[10],
                    }
                )
        return rows
