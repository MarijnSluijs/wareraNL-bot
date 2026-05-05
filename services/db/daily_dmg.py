"""Daily damage DB methods (daily_dmg_hits + daily_dmg_processed tables)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class DailyDmgMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ------------------------------------------------------------------ #
    # Write helpers
    # ------------------------------------------------------------------ #

    async def insert_daily_dmg_hit(
        self,
        round_id: str,
        battle_id: str,
        user_id: str,
        total_damage: float,
        round_date: str,
        recorded_at: str,
        hits: Optional[int] = None,
        cases: Optional[int] = None,
    ) -> None:
        """Upsert a single player-round damage entry."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO daily_dmg_hits
                (round_id, battle_id, user_id, total_damage, hits, cases, round_date, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (round_id, battle_id, user_id, total_damage, hits, cases, round_date, recorded_at),
        )

    async def commit_daily_dmg(self) -> None:
        """Commit pending daily_dmg_hits inserts."""
        await self._conn.commit()

    async def mark_daily_dmg_processed(
        self,
        battle_id: str,
        battle_date: Optional[str],
        processed_at: str,
    ) -> None:
        """Record that a battle has been processed by the daily_dmg task."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO daily_dmg_processed
                (battle_id, battle_date, processed_at)
            VALUES (?, ?, ?)
            """,
            (battle_id, battle_date, processed_at),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------ #
    # Existence checks
    # ------------------------------------------------------------------ #

    async def filter_daily_dmg_unprocessed(self, battle_ids: list[str]) -> list[str]:
        """Return the subset of battle_ids NOT yet in daily_dmg_processed."""
        if not battle_ids:
            return []
        placeholders = ",".join("?" * len(battle_ids))
        async with self._conn.execute(
            f"SELECT battle_id FROM daily_dmg_processed WHERE battle_id IN ({placeholders})",
            battle_ids,
        ) as cur:
            done = {row[0] async for row in cur}
        return [bid for bid in battle_ids if bid not in done]

    # ------------------------------------------------------------------ #
    # Query helpers — NL citizen IDs
    # ------------------------------------------------------------------ #

    async def get_country_user_ids(self, country_id: str) -> list[str]:
        """Return all user_ids from citizen_levels for the given country.

        Note: a separate :meth:`CitizensMixin.get_nl_citizen_ids` returns
        ``[(user_id, citizen_name)]`` tuples; do not confuse the two.
        """
        rows: list[str] = []
        async with self._conn.execute(
            "SELECT user_id FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def filter_nl_citizen_ids(
        self, country_id: str, user_ids: list[str]
    ) -> list[str]:
        """Return the subset of user_ids that are NL citizens."""
        if not user_ids:
            return []
        placeholders = ",".join("?" * len(user_ids))
        async with self._conn.execute(
            f"""
            SELECT user_id FROM citizen_levels
             WHERE country_id = ? AND user_id IN ({placeholders})
            """,
            [country_id, *user_ids],
        ) as cur:
            return [row[0] async for row in cur]

    # ------------------------------------------------------------------ #
    # Leaderboard queries
    # ------------------------------------------------------------------ #

    async def get_top_players_daily_dmg(
        self,
        date_str: str,
        limit: int,
        country_id: Optional[str] = None,
        mu_id: Optional[str] = None,
    ) -> list[dict]:
        """Top players by SUM(total_damage) on a given date.

        If *country_id* is given, only include players from that country.
        If *mu_id* is given, only include players from that MU.
        Returns list of dicts: user_id, total_damage, battle_count.
        """
        rows: list[dict] = []
        if country_id:
            async with self._conn.execute(
                """
                SELECT ddh.user_id,
                       SUM(ddh.total_damage)               AS total_damage,
                       COUNT(DISTINCT ddh.battle_id)       AS battle_count
                  FROM daily_dmg_hits ddh
                  JOIN citizen_levels cl ON cl.user_id = ddh.user_id
                 WHERE ddh.round_date = ?
                   AND cl.country_id = ?
                 GROUP BY ddh.user_id
                 ORDER BY total_damage DESC
                 LIMIT ?
                """,
                (date_str, country_id, limit),
            ) as cur:
                async for row in cur:
                    rows.append({
                        "user_id": row[0],
                        "total_damage": row[1] or 0.0,
                        "battle_count": row[2] or 0,
                    })
        elif mu_id:
            async with self._conn.execute(
                """
                SELECT ddh.user_id,
                       SUM(ddh.total_damage)               AS total_damage,
                       COUNT(DISTINCT ddh.battle_id)       AS battle_count
                  FROM daily_dmg_hits ddh
                  JOIN citizen_levels cl ON cl.user_id = ddh.user_id
                 WHERE ddh.round_date = ?
                   AND cl.mu_id = ?
                 GROUP BY ddh.user_id
                 ORDER BY total_damage DESC
                 LIMIT ?
                """,
                (date_str, mu_id, limit),
            ) as cur:
                async for row in cur:
                    rows.append({
                        "user_id": row[0],
                        "total_damage": row[1] or 0.0,
                        "battle_count": row[2] or 0,
                    })
        else:
            async with self._conn.execute(
                """
                SELECT user_id,
                       SUM(total_damage)               AS total_damage,
                       COUNT(DISTINCT battle_id)       AS battle_count
                  FROM daily_dmg_hits
                 WHERE round_date = ?
                 GROUP BY user_id
                 ORDER BY total_damage DESC
                 LIMIT ?
                """,
                (date_str, limit),
            ) as cur:
                async for row in cur:
                    rows.append({
                        "user_id": row[0],
                        "total_damage": row[1] or 0.0,
                        "battle_count": row[2] or 0,
                    })
        return rows

    async def get_player_daily_dmg(
        self,
        date_str: str,
        citizen_name: str,
    ) -> Optional[dict]:
        """Return total daily damage for a single player by name.

        Returns dict with user_id, citizen_name, total_damage, battle_count
        or None if not found.
        """
        async with self._conn.execute(
            """
            SELECT ddh.user_id,
                   cl.citizen_name,
                   SUM(ddh.total_damage)         AS total_damage,
                   COUNT(DISTINCT ddh.battle_id) AS battle_count
              FROM daily_dmg_hits ddh
              JOIN citizen_levels cl ON cl.user_id = ddh.user_id
             WHERE ddh.round_date = ?
               AND lower(cl.citizen_name) = lower(?)
             GROUP BY ddh.user_id
            """,
            (date_str, citizen_name),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "user_id": row[0],
                "citizen_name": row[1],
                "total_damage": row[2] or 0.0,
                "battle_count": row[3] or 0,
            }

    async def get_top_countries_daily_dmg(
        self,
        date_str: str,
        limit: int,
    ) -> list[dict]:
        """Top countries by SUM of their citizens' damage on a given date.

        Country is derived from citizen_levels.country_id.
        Returns list of dicts: country_id, total_damage, player_count, battle_count.
        """
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT cl.country_id,
                   SUM(ddh.total_damage)               AS total_damage,
                   COUNT(DISTINCT ddh.user_id)         AS player_count,
                   COUNT(DISTINCT ddh.battle_id)       AS battle_count
              FROM daily_dmg_hits ddh
              JOIN citizen_levels cl ON cl.user_id = ddh.user_id
             WHERE ddh.round_date = ?
             GROUP BY cl.country_id
             ORDER BY total_damage DESC
             LIMIT ?
            """,
            (date_str, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "country_id": row[0],
                    "total_damage": row[1] or 0.0,
                    "player_count": row[2] or 0,
                    "battle_count": row[3] or 0,
                })
        return rows

    async def get_country_daily_dmg(
        self,
        date_str: str,
        country_id: str,
    ) -> Optional[dict]:
        """Total daily damage for a specific country's citizens.

        Returns dict with country_id, total_damage, player_count, battle_count or None.
        """
        async with self._conn.execute(
            """
            SELECT cl.country_id,
                   SUM(ddh.total_damage)               AS total_damage,
                   COUNT(DISTINCT ddh.user_id)         AS player_count,
                   COUNT(DISTINCT ddh.battle_id)       AS battle_count
              FROM daily_dmg_hits ddh
              JOIN citizen_levels cl ON cl.user_id = ddh.user_id
             WHERE ddh.round_date = ?
               AND cl.country_id = ?
             GROUP BY cl.country_id
            """,
            (date_str, country_id),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "country_id": row[0],
                "total_damage": row[1] or 0.0,
                "player_count": row[2] or 0,
                "battle_count": row[3] or 0,
            }

    async def get_top_mus_daily_dmg(
        self,
        date_str: str,
        limit: int,
    ) -> list[dict]:
        """Top MUs by SUM of their members' damage on a given date.

        MU is derived from citizen_levels.mu_id / mu_name.
        Returns list of dicts: mu_id, mu_name, total_damage, player_count, battle_count.
        """
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT cl.mu_id,
                   cl.mu_name,
                   SUM(ddh.total_damage)               AS total_damage,
                   COUNT(DISTINCT ddh.user_id)         AS player_count,
                   COUNT(DISTINCT ddh.battle_id)       AS battle_count
              FROM daily_dmg_hits ddh
              JOIN citizen_levels cl ON cl.user_id = ddh.user_id
             WHERE ddh.round_date = ?
               AND cl.mu_id IS NOT NULL
             GROUP BY cl.mu_id
             ORDER BY total_damage DESC
             LIMIT ?
            """,
            (date_str, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "mu_id": row[0],
                    "mu_name": row[1],
                    "total_damage": row[2] or 0.0,
                    "player_count": row[3] or 0,
                    "battle_count": row[4] or 0,
                })
        return rows

    async def get_mu_daily_dmg(
        self,
        date_str: str,
        mu_name: str,
    ) -> Optional[dict]:
        """Total daily damage for a specific MU by name.

        Returns dict or None.
        """
        async with self._conn.execute(
            """
            SELECT cl.mu_id,
                   cl.mu_name,
                   SUM(ddh.total_damage)               AS total_damage,
                   COUNT(DISTINCT ddh.user_id)         AS player_count,
                   COUNT(DISTINCT ddh.battle_id)       AS battle_count
              FROM daily_dmg_hits ddh
              JOIN citizen_levels cl ON cl.user_id = ddh.user_id
             WHERE ddh.round_date = ?
               AND lower(cl.mu_name) = lower(?)
             GROUP BY cl.mu_id
            """,
            (date_str, mu_name),
        ) as cur:
            row = await cur.fetchone()
            if row is None:
                return None
            return {
                "mu_id": row[0],
                "mu_name": row[1],
                "total_damage": row[2] or 0.0,
                "player_count": row[3] or 0,
                "battle_count": row[4] or 0,
            }

    async def get_daily_dmg_dates(self, limit: int = 7) -> list[str]:
        """Return the most recent dates for which we have daily damage data."""
        rows: list[str] = []
        async with self._conn.execute(
            """
            SELECT DISTINCT round_date
              FROM daily_dmg_hits
             ORDER BY round_date DESC
             LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def get_daily_dmg_earliest_date(self) -> Optional[str]:
        """Return the oldest round_date for which we have daily damage data."""
        async with self._conn.execute(
            "SELECT MIN(round_date) FROM daily_dmg_hits"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None
