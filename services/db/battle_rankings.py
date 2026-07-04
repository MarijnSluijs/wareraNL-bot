"""Battle rankings DB methods (battle_hits + processed_battles tables)."""

from __future__ import annotations

from typing import Optional

import aiosqlite


class BattleRankingsMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ------------------------------------------------------------------ #
    # Write helpers
    # ------------------------------------------------------------------ #

    async def insert_battle_hit(
        self,
        battle_id: str,
        user_id: str,
        side: str,
        damage: float,
        rank: Optional[int],
        battle_created_at: str,
        recorded_at: str,
    ) -> None:
        """Upsert a single player-side-battle damage entry."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO battle_hits
                (battle_id, user_id, side, damage, rank, battle_created_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (battle_id, user_id, side, damage, rank, battle_created_at, recorded_at),
        )

    async def commit_battle_hits(self) -> None:
        """Commit pending battle hit inserts."""
        await self._conn.commit()

    async def mark_battle_processed(
        self,
        battle_id: str,
        battle_created_at: str,
        attacker_damage: float,
        defender_damage: float,
        processed_at: str,
        attacker_country_id: Optional[str] = None,
        defender_country_id: Optional[str] = None,
    ) -> None:
        """Record that a battle has been fully ingested."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_battles
                (battle_id, battle_created_at, attacker_damage, defender_damage,
                 processed_at, attacker_country_id, defender_country_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (battle_id, battle_created_at, attacker_damage, defender_damage,
             processed_at, attacker_country_id, defender_country_id),
        )
        await self._conn.commit()

    async def update_battle_countries(
        self, battle_id: str, attacker_country_id: str, defender_country_id: str
    ) -> None:
        """Backfill country IDs for an already-processed battle."""
        await self._conn.execute(
            """
            UPDATE processed_battles
               SET attacker_country_id = ?, defender_country_id = ?
             WHERE battle_id = ?
            """,
            (attacker_country_id, defender_country_id, battle_id),
        )
        await self._conn.commit()

    async def get_processed_battles_missing_countries(
        self, limit: int = 500
    ) -> list[str]:
        """Return battle_ids that have no country info yet (for backfill)."""
        rows: list[str] = []
        async with self._conn.execute(
            """
            SELECT battle_id FROM processed_battles
             WHERE attacker_country_id IS NULL
             LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def update_processed_battle_countries(
        self,
        battle_id: str,
        attacker_country_id: Optional[str],
        defender_country_id: Optional[str],
    ) -> None:
        """Update attacker/defender country IDs for an already-processed battle."""
        await self._conn.execute(
            """
            UPDATE processed_battles
               SET attacker_country_id = COALESCE(?, attacker_country_id),
                   defender_country_id = COALESCE(?, defender_country_id)
             WHERE battle_id = ?
            """,
            (attacker_country_id, defender_country_id, battle_id),
        )

    async def get_processed_battles_missing_mu_hits(
        self, limit: int = 5000
    ) -> list[tuple[str, str | None]]:
        """Return (battle_id, battle_created_at) for battles not yet in battle_mu_hits."""
        rows: list[tuple[str, str | None]] = []
        async with self._conn.execute(
            """
            SELECT pb.battle_id, pb.battle_created_at
              FROM processed_battles pb
             WHERE NOT EXISTS (
                   SELECT 1 FROM battle_mu_hits bmh
                    WHERE bmh.battle_id = pb.battle_id
             )
             LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows

    async def get_processed_battles_missing_country_hits(
        self, limit: int = 2000
    ) -> list[tuple[str, str | None]]:
        """Return (battle_id, battle_created_at) for battles not yet in battle_country_hits."""
        rows: list[tuple[str, str | None]] = []
        async with self._conn.execute(
            """
            SELECT pb.battle_id, pb.battle_created_at
              FROM processed_battles pb
             WHERE NOT EXISTS (
                   SELECT 1 FROM battle_country_hits bch
                    WHERE bch.battle_id = pb.battle_id
             )
             LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows

    # ------------------------------------------------------------------ #
    # Existence checks
    # ------------------------------------------------------------------ #

    async def derive_country_hits_from_player_hits(self) -> int:
        """Derive battle_country_hits from battle_hits JOIN citizen_levels for all
        battles not yet covered by nationality-based country hits.

        This lets us backfill historical data without needing the API's
        battleRanking.getRanking (which only retains data for ~3 days).
        Uses each player's *current* country from citizen_levels as an approximation.

        Returns the number of new rows inserted.
        """
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO battle_country_hits
                (battle_id, country_id, side, damage, battle_created_at, recorded_at)
            SELECT
                bh.battle_id,
                cl.country_id,
                bh.side,
                SUM(bh.damage)         AS damage,
                bh.battle_created_at,
                MAX(bh.recorded_at)    AS recorded_at
            FROM battle_hits bh
            JOIN citizen_levels cl ON cl.user_id = bh.user_id
            WHERE NOT EXISTS (
                SELECT 1 FROM battle_country_hits bch
                 WHERE bch.battle_id = bh.battle_id
            )
            GROUP BY bh.battle_id, cl.country_id, bh.side, bh.battle_created_at
            """
        )
        async with self._conn.execute("SELECT changes()") as cur:
            row = await cur.fetchone()
            inserted = (row[0] if row else 0) or 0
        await self._conn.commit()
        return inserted

    async def is_battle_processed(self, battle_id: str) -> bool:
        """Return True if this battle_id is in processed_battles."""
        async with self._conn.execute(
            "SELECT 1 FROM processed_battles WHERE battle_id = ? LIMIT 1",
            (battle_id,),
        ) as cur:
            return await cur.fetchone() is not None

    async def filter_unprocessed(self, battle_ids: list[str]) -> list[str]:
        """Return the subset of battle_ids NOT yet in processed_battles."""
        if not battle_ids:
            return []
        placeholders = ",".join("?" * len(battle_ids))
        async with self._conn.execute(
            f"SELECT battle_id FROM processed_battles WHERE battle_id IN ({placeholders})",
            battle_ids,
        ) as cur:
            done = {row[0] async for row in cur}
        return [bid for bid in battle_ids if bid not in done]

    # ------------------------------------------------------------------ #
    # Leaderboard queries
    # ------------------------------------------------------------------ #

    async def get_top_players_by_damage(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top players ranked by SUM(damage), optionally filtered to recent N days
        and/or to players whose nationality is *country_id*.

        Returns list of dicts with keys: user_id, total_damage, battle_count.
        """
        rows: list[dict] = []
        if country_id:
            date_cond = (
                f"AND bh.battle_created_at >= datetime('now', '-{days} days')"
                if days
                else ""
            )
            async with self._conn.execute(
                f"""
                SELECT bh.user_id,
                       SUM(bh.damage)               AS total_damage,
                       COUNT(DISTINCT bh.battle_id) AS battle_count
                FROM battle_hits bh
                JOIN citizen_levels cl ON cl.user_id = bh.user_id
                WHERE cl.country_id = ? {date_cond}
                GROUP BY bh.user_id
                ORDER BY total_damage DESC
                LIMIT ?
                """,
                (country_id, limit),
            ) as cur:
                async for row in cur:
                    rows.append(
                        {
                            "user_id": row[0],
                            "total_damage": row[1] or 0.0,
                            "battle_count": row[2] or 0,
                        }
                    )
        else:
            since_clause = (
                f"WHERE battle_created_at >= datetime('now', '-{days} days')"
                if days
                else ""
            )
            async with self._conn.execute(
                f"""
                SELECT user_id,
                       SUM(damage)   AS total_damage,
                       COUNT(DISTINCT battle_id) AS battle_count
                FROM battle_hits
                {since_clause}
                GROUP BY user_id
                ORDER BY total_damage DESC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                async for row in cur:
                    rows.append(
                        {
                            "user_id": row[0],
                            "total_damage": row[1] or 0.0,
                            "battle_count": row[2] or 0,
                        }
                    )
        return rows

    async def get_top_battles(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top battles ranked by attacker_damage + defender_damage combined.

        When *country_id* is given, only battles where that country was attacker
        or defender are returned.

        Returns list of dicts: battle_id, total_damage, attacker_damage,
        defender_damage, battle_created_at.
        """
        conds: list[str] = []
        params: list = []
        if days:
            conds.append(f"battle_created_at >= datetime('now', '-{days} days')")
        if country_id:
            conds.append("(attacker_country_id = ? OR defender_country_id = ?)")
            params.extend([country_id, country_id])
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT battle_id,
                   attacker_damage + defender_damage AS total_damage,
                   attacker_damage,
                   defender_damage,
                   battle_created_at,
                   attacker_country_id,
                   defender_country_id
            FROM processed_battles
            {where}
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "battle_id": row[0],
                        "total_damage": row[1] or 0.0,
                        "attacker_damage": row[2] or 0.0,
                        "defender_damage": row[3] or 0.0,
                        "battle_created_at": row[4] or "",
                        "attacker_country_id": row[5],
                        "defender_country_id": row[6],
                    }
                )
        return rows

    async def get_top_single_battle_damage(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top individual player performances in a single battle.

        When *country_id* is given, only players from that country are returned
        (joined via citizen_levels).

        Returns list of dicts: user_id, damage, battle_id, battle_created_at,
        attacker_country_id, defender_country_id.
        """
        conds: list[str] = []
        params: list = []
        join_clause = ""
        if country_id:
            join_clause = "JOIN citizen_levels cl ON cl.user_id = bh.user_id"
            conds.append("cl.country_id = ?")
            params.append(country_id)
        if days:
            conds.append(f"bh.battle_created_at >= datetime('now', '-{days} days')")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT bh.user_id,
                   bh.damage,
                   bh.battle_id,
                   bh.battle_created_at,
                   pb.attacker_country_id,
                   pb.defender_country_id
            FROM battle_hits bh
            LEFT JOIN processed_battles pb ON pb.battle_id = bh.battle_id
            {join_clause}
            {where}
            ORDER BY bh.damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "damage": row[1] or 0.0,
                        "battle_id": row[2],
                        "battle_created_at": row[3] or "",
                        "attacker_country_id": row[4],
                        "defender_country_id": row[5],
                    }
                )
        return rows

    async def get_top_countries_by_damage(
        self, days: Optional[int], limit: int
    ) -> list[dict]:
        """Top countries ranked by player-nationality damage.

        Uses battle_country_hits (populated via type='country' API rankings) which
        reflects the same methodology as the game's own country leaderboard —
        damage is credited to each player's home country regardless of which side
        they fought on.  Falls back to the old attacker/defender-side aggregation
        if battle_country_hits is still empty (i.e. before the first post-migration
        sweep runs).

        Returns list of dicts: country_id, total_damage, battle_count.
        """
        since_clause = (
            f"WHERE battle_created_at >= datetime('now', '-{days} days')"
            if days
            else ""
        )
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT country_id,
                   SUM(damage)               AS total_damage,
                   COUNT(DISTINCT battle_id) AS battle_count
            FROM battle_country_hits
            {since_clause}
            GROUP BY country_id
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "country_id": row[0],
                        "total_damage": row[1] or 0.0,
                        "battle_count": row[2] or 0,
                    }
                )
        if rows:
            return rows

        # Fallback: aggregate from processed_battles attacker/defender columns
        since_clause_fb = (
            f"AND battle_created_at >= datetime('now', '-{days} days')"
            if days
            else ""
        )
        async with self._conn.execute(
            f"""
            SELECT country_id,
                   SUM(damage)          AS total_damage,
                   COUNT(*)             AS battle_count
            FROM (
                SELECT attacker_country_id AS country_id,
                       attacker_damage     AS damage,
                       battle_created_at
                FROM processed_battles
                WHERE attacker_country_id IS NOT NULL AND attacker_country_id != ''
                {since_clause_fb}
                UNION ALL
                SELECT defender_country_id AS country_id,
                       defender_damage     AS damage,
                       battle_created_at
                FROM processed_battles
                WHERE defender_country_id IS NOT NULL AND defender_country_id != ''
                {since_clause_fb}
            )
            GROUP BY country_id
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "country_id": row[0],
                        "total_damage": row[1] or 0.0,
                        "battle_count": row[2] or 0,
                    }
                )
        return rows

    async def get_country_hits_date_range(self) -> tuple[str | None, str | None]:
        """Return (earliest_battle_created_at, latest_battle_created_at) in battle_country_hits."""
        async with self._conn.execute(
            "SELECT MIN(battle_created_at), MAX(battle_created_at) FROM battle_country_hits"
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0], row[1]
            return None, None

    async def get_top_mus_by_damage(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top MUs by damage, covering all countries.

        When *country_id* is given, filters to MUs whose members are from that
        country (via citizen_levels JOIN — only tracked citizens).

        Primary source (global): battle_mu_hits (populated from battleRanking.
        getRanking type='mu').  Fallback: citizen_levels JOIN on battle_hits.

        Returns list of dicts: mu_id, mu_name, total_damage, battle_count.
        """
        if country_id:
            return await self._get_top_mus_via_country_filter(days, limit, country_id)
        rows = await self._get_top_mus_from_mu_hits(days, limit)
        if rows:
            return rows
        return await self._get_top_mus_from_citizen_levels(days, limit, None)

    async def _get_top_mus_via_country_filter(
        self, days: Optional[int], limit: int, country_id: str
    ) -> list[dict]:
        """Top MUs by total damage whose home country is *country_id*.

        Filters via known_mus.country_id (populated by the mu refresh task).
        Falls back to a processed_battles side-match when country_id is not yet
        stored in known_mus.
        """
        extra = (
            f"AND bmh.battle_created_at >= datetime('now', '-{days} days')"
            if days
            else ""
        )
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT bmh.mu_id,
                   COALESCE(MAX(bmh.mu_name), MAX(km.mu_name), bmh.mu_id) AS mu_name,
                   SUM(bmh.damage)                AS total_damage,
                   COUNT(DISTINCT bmh.battle_id)  AS battle_count
            FROM battle_mu_hits bmh
            JOIN known_mus km ON km.mu_id = bmh.mu_id
            WHERE km.country_id = ?
            {extra}
            GROUP BY bmh.mu_id
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            (country_id, limit),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "total_damage": row[2] or 0.0,
                        "battle_count": row[3] or 0,
                    }
                )
        if rows:
            return rows
        # Fallback: MUs fighting on country_id's side in processed_battles
        async with self._conn.execute(
            f"""
            SELECT bmh.mu_id,
                   COALESCE(MAX(bmh.mu_name), MAX(km2.mu_name), bmh.mu_id) AS mu_name,
                   SUM(bmh.damage)                AS total_damage,
                   COUNT(DISTINCT bmh.battle_id)  AS battle_count
            FROM battle_mu_hits bmh
            JOIN processed_battles pb ON pb.battle_id = bmh.battle_id
            LEFT JOIN known_mus km2 ON km2.mu_id = bmh.mu_id
            WHERE (   (bmh.side = 'attacker' AND pb.attacker_country_id = ?)
                   OR (bmh.side = 'defender' AND pb.defender_country_id = ?) )
            {extra}
            GROUP BY bmh.mu_id
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            (country_id, country_id, limit),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "total_damage": row[2] or 0.0,
                        "battle_count": row[3] or 0,
                    }
                )
        return rows

    async def _get_top_mus_from_citizen_levels(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Derive MU totals from battle_hits JOIN citizen_levels.

        When *country_id* is given, only players from that country are included.
        """
        conds = ["cl.mu_id IS NOT NULL", "cl.mu_id != ''"]
        params: list = []
        if country_id:
            conds.append("cl.country_id = ?")
            params.append(country_id)
        if days:
            conds.append(f"bh.battle_created_at >= datetime('now', '-{days} days')")
        where = "WHERE " + " AND ".join(conds)
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT cl.mu_id,
                   MAX(cl.mu_name)               AS mu_name,
                   SUM(bh.damage)                AS total_damage,
                   COUNT(DISTINCT bh.battle_id)  AS battle_count
            FROM battle_hits bh
            JOIN citizen_levels cl ON cl.user_id = bh.user_id
            {where}
            GROUP BY cl.mu_id
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "total_damage": row[2] or 0.0,
                        "battle_count": row[3] or 0,
                    }
                )
        return rows

    async def _get_top_mus_from_mu_hits(
        self, days: Optional[int], limit: int
    ) -> list[dict]:
        """Query battle_mu_hits table directly (includes all MUs, not just tracked players)."""
        extra = (
            f"AND bmh.battle_created_at >= datetime('now', '-{days} days')"
            if days
            else ""
        )
        rows = []
        async with self._conn.execute(
            f"""
            SELECT bmh.mu_id,
                   COALESCE(MAX(bmh.mu_name), MAX(km.mu_name), bmh.mu_id) AS mu_name,
                   SUM(bmh.damage)              AS total_damage,
                   COUNT(DISTINCT bmh.battle_id) AS battle_count
            FROM battle_mu_hits bmh
            LEFT JOIN known_mus km ON km.mu_id = bmh.mu_id
            WHERE 1=1 {extra}
            GROUP BY bmh.mu_id
            ORDER BY total_damage DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "total_damage": row[2] or 0.0,
                        "battle_count": row[3] or 0,
                    }
                )
        return rows

    async def get_best_mu_per_battle(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Best single-battle damage record per MU.

        When *country_id* is given, filters to MUs that have members from that
        country (via citizen_levels subquery), using battle_mu_hits for full
        game coverage.

        Returns list of dicts: mu_id, mu_name, damage, battle_id, battle_created_at.
        """
        rows: list[dict] = []
        if country_id:
            extra = (
                f"AND bmh.battle_created_at >= datetime('now', '-{days} days')"
                if days
                else ""
            )
            async with self._conn.execute(
                f"""
                WITH ranked AS (
                    SELECT bmh.mu_id,
                           COALESCE(bmh.mu_name, km.mu_name, bmh.mu_id) AS mu_name,
                           bmh.damage, bmh.battle_id, bmh.battle_created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY bmh.mu_id ORDER BY bmh.damage DESC
                           ) AS rn
                    FROM battle_mu_hits bmh
                    JOIN known_mus km ON km.mu_id = bmh.mu_id
                    WHERE km.country_id = ?
                    {extra}
                )
                SELECT mu_id, mu_name, damage, battle_id, battle_created_at
                FROM ranked WHERE rn = 1
                ORDER BY damage DESC
                LIMIT ?
                """,
                (country_id, limit),
            ) as cur:
                async for row in cur:
                    rows.append(
                        {
                            "mu_id": row[0],
                            "mu_name": row[1] or row[0],
                            "damage": row[2] or 0.0,
                            "battle_id": row[3],
                            "battle_created_at": row[4] or "",
                        }
                    )
            if not rows:
                # Fallback: MUs fighting on country_id's side
                async with self._conn.execute(
                    f"""
                    WITH ranked AS (
                        SELECT bmh.mu_id,
                               COALESCE(bmh.mu_name, km2.mu_name, bmh.mu_id) AS mu_name,
                               bmh.damage, bmh.battle_id, bmh.battle_created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY bmh.mu_id ORDER BY bmh.damage DESC
                               ) AS rn
                        FROM battle_mu_hits bmh
                        JOIN processed_battles pb ON pb.battle_id = bmh.battle_id
                        LEFT JOIN known_mus km2 ON km2.mu_id = bmh.mu_id
                        WHERE (   (bmh.side = 'attacker' AND pb.attacker_country_id = ?)
                               OR (bmh.side = 'defender' AND pb.defender_country_id = ?) )
                        {extra}
                    )
                    SELECT mu_id, mu_name, damage, battle_id, battle_created_at
                    FROM ranked WHERE rn = 1
                    ORDER BY damage DESC
                    LIMIT ?
                    """,
                    (country_id, country_id, limit),
                ) as cur:
                    async for row in cur:
                        rows.append(
                            {
                                "mu_id": row[0],
                                "mu_name": row[1] or row[0],
                                "damage": row[2] or 0.0,
                                "battle_id": row[3],
                                "battle_created_at": row[4] or "",
                            }
                        )
        else:
            extra = (
                f"AND bmh.battle_created_at >= datetime('now', '-{days} days')"
                if days
                else ""
            )
            async with self._conn.execute(
                f"""
                WITH ranked AS (
                    SELECT bmh.mu_id,
                           COALESCE(bmh.mu_name, km.mu_name, bmh.mu_id) AS mu_name,
                           bmh.damage, bmh.battle_id, bmh.battle_created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY bmh.mu_id ORDER BY bmh.damage DESC
                           ) AS rn
                    FROM battle_mu_hits bmh
                    LEFT JOIN known_mus km ON km.mu_id = bmh.mu_id
                    WHERE 1=1 {extra}
                )
                SELECT mu_id, mu_name, damage, battle_id, battle_created_at
                FROM ranked WHERE rn = 1
                ORDER BY damage DESC
                LIMIT ?
                """,
                (limit,),
            ) as cur:
                async for row in cur:
                    rows.append(
                        {
                            "mu_id": row[0],
                            "mu_name": row[1] or row[0],
                            "damage": row[2] or 0.0,
                            "battle_id": row[3],
                            "battle_created_at": row[4] or "",
                        }
                    )
        return rows

    async def get_best_country_per_battle(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Best single-battle damage record per country (from battle_country_hits).

        When *country_id* is given, returns only records for that specific country
        (best battles for that country, sorted by damage).

        Returns list of dicts: country_id, damage, battle_id, battle_created_at.
        """
        conds: list[str] = []
        params: list = []
        if days:
            conds.append(f"battle_created_at >= datetime('now', '-{days} days')")
        if country_id:
            conds.append("country_id = ?")
            params.append(country_id)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            WITH ranked AS (
                SELECT country_id, damage, battle_id, battle_created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY country_id ORDER BY damage DESC
                       ) AS rn
                FROM battle_country_hits
                {where}
            )
            SELECT country_id, damage, battle_id, battle_created_at
            FROM ranked WHERE rn = 1
            ORDER BY damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "country_id": row[0],
                        "damage": row[1] or 0.0,
                        "battle_id": row[2],
                        "battle_created_at": row[3] or "",
                    }
                )
        return rows

    # ── Weekly best records ──────────────────────────────────────────────────

    async def get_best_player_week(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top player-week combos by total damage in any single calendar week.

        Returns list of dicts: user_id, damage, week_start, iso_week.
        """
        params: list = []
        conds: list[str] = []
        join_clause = ""
        if country_id:
            join_clause = "JOIN citizen_levels cl ON cl.user_id = bh.user_id"
            conds.append("cl.country_id = ?")
            params.append(country_id)
        if days:
            conds.append(f"bh.battle_created_at >= datetime('now', '-{days} days')")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT bh.user_id,
                   SUM(bh.damage)                           AS weekly_damage,
                   MIN(bh.battle_created_at)                AS week_start,
                   strftime('%Y-%W', bh.battle_created_at)  AS iso_week
            FROM battle_hits bh
            {join_clause}
            {where}
            GROUP BY bh.user_id, iso_week
            ORDER BY weekly_damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "user_id": row[0],
                        "damage": row[1] or 0.0,
                        "week_start": row[2] or "",
                        "iso_week": row[3],
                    }
                )
        return rows

    async def get_best_mu_week(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top MU-week combos by total damage in any single calendar week.

        Returns list of dicts: mu_id, mu_name, damage, week_start, iso_week.
        """
        if country_id:
            return await self._get_best_mu_week_via_country_filter(days, limit, country_id)
        extra = (
            f"AND bmh.battle_created_at >= datetime('now', '-{days} days')"
            if days
            else ""
        )
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT bmh.mu_id,
                   COALESCE(MAX(bmh.mu_name), MAX(km.mu_name), bmh.mu_id) AS mu_name,
                   SUM(bmh.damage)                              AS weekly_damage,
                   MIN(bmh.battle_created_at)                   AS week_start,
                   strftime('%Y-%W', bmh.battle_created_at)     AS iso_week
            FROM battle_mu_hits bmh
            LEFT JOIN known_mus km ON km.mu_id = bmh.mu_id
            WHERE 1=1 {extra}
            GROUP BY bmh.mu_id, iso_week
            ORDER BY weekly_damage DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "damage": row[2] or 0.0,
                        "week_start": row[3] or "",
                        "iso_week": row[4],
                    }
                )
        return rows

    async def _get_best_mu_week_via_country_filter(
        self, days: Optional[int], limit: int, country_id: str
    ) -> list[dict]:
        """Weekly MU totals from battle_mu_hits, filtered to MUs whose home country is *country_id*."""
        extra = (
            f"AND bmh.battle_created_at >= datetime('now', '-{days} days')"
            if days
            else ""
        )
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT bmh.mu_id,
                   COALESCE(MAX(bmh.mu_name), MAX(km.mu_name), bmh.mu_id) AS mu_name,
                   SUM(bmh.damage)                              AS weekly_damage,
                   MIN(bmh.battle_created_at)                   AS week_start,
                   strftime('%Y-%W', bmh.battle_created_at)     AS iso_week
            FROM battle_mu_hits bmh
            JOIN known_mus km ON km.mu_id = bmh.mu_id
            WHERE km.country_id = ?
            {extra}
            GROUP BY bmh.mu_id, iso_week
            ORDER BY weekly_damage DESC
            LIMIT ?
            """,
            (country_id, limit),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "damage": row[2] or 0.0,
                        "week_start": row[3] or "",
                        "iso_week": row[4],
                    }
                )
        if rows:
            return rows
        # Fallback: MUs fighting on country_id's side
        async with self._conn.execute(
            f"""
            SELECT bmh.mu_id,
                   COALESCE(MAX(bmh.mu_name), MAX(km2.mu_name), bmh.mu_id) AS mu_name,
                   SUM(bmh.damage)                              AS weekly_damage,
                   MIN(bmh.battle_created_at)                   AS week_start,
                   strftime('%Y-%W', bmh.battle_created_at)     AS iso_week
            FROM battle_mu_hits bmh
            JOIN processed_battles pb ON pb.battle_id = bmh.battle_id
            LEFT JOIN known_mus km2 ON km2.mu_id = bmh.mu_id
            WHERE (   (bmh.side = 'attacker' AND pb.attacker_country_id = ?)
                   OR (bmh.side = 'defender' AND pb.defender_country_id = ?) )
            {extra}
            GROUP BY bmh.mu_id, iso_week
            ORDER BY weekly_damage DESC
            LIMIT ?
            """,
            (country_id, country_id, limit),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "damage": row[2] or 0.0,
                        "week_start": row[3] or "",
                        "iso_week": row[4],
                    }
                )
        return rows

    async def _get_best_mu_week_from_citizen_levels(
        self, days: Optional[int], limit: int, country_id: str
    ) -> list[dict]:
        """Weekly MU totals derived from battle_hits JOIN citizen_levels (filtered by country)."""
        conds = [
            "cl.mu_id IS NOT NULL",
            "cl.mu_id != ''",
            "cl.country_id = ?",
        ]
        params: list = [country_id]
        if days:
            conds.append(f"bh.battle_created_at >= datetime('now', '-{days} days')")
        where = "WHERE " + " AND ".join(conds)
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT cl.mu_id,
                   MAX(cl.mu_name)                             AS mu_name,
                   SUM(bh.damage)                              AS weekly_damage,
                   MIN(bh.battle_created_at)                   AS week_start,
                   strftime('%Y-%W', bh.battle_created_at)     AS iso_week
            FROM battle_hits bh
            JOIN citizen_levels cl ON cl.user_id = bh.user_id
            {where}
            GROUP BY cl.mu_id, iso_week
            ORDER BY weekly_damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "mu_id": row[0],
                        "mu_name": row[1] or row[0],
                        "damage": row[2] or 0.0,
                        "week_start": row[3] or "",
                        "iso_week": row[4],
                    }
                )
        return rows

    async def get_best_country_week(
        self, days: Optional[int], limit: int, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top country-week combos by total damage in any single calendar week.

        Returns list of dicts: country_id, damage, week_start, iso_week.
        """
        params: list = []
        conds: list[str] = []
        if country_id:
            conds.append("country_id = ?")
            params.append(country_id)
        if days:
            conds.append(f"battle_created_at >= datetime('now', '-{days} days')")
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT country_id,
                   SUM(damage)                              AS weekly_damage,
                   MIN(battle_created_at)                   AS week_start,
                   strftime('%Y-%W', battle_created_at)     AS iso_week
            FROM battle_country_hits
            {where}
            GROUP BY country_id, iso_week
            ORDER BY weekly_damage DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            async for row in cur:
                rows.append(
                    {
                        "country_id": row[0],
                        "damage": row[1] or 0.0,
                        "week_start": row[2] or "",
                        "iso_week": row[3],
                    }
                )
        return rows

    async def insert_battle_mu_hit(
        self,
        battle_id: str,
        mu_id: str,
        side: str,
        mu_name: Optional[str],
        damage: float,
        battle_created_at: str,
        recorded_at: str,
        attacker_country_id: Optional[str] = None,
        defender_country_id: Optional[str] = None,
    ) -> None:
        """Upsert a single MU-side-battle damage entry."""
        await self._conn.execute(
            """
            INSERT INTO battle_mu_hits
                (battle_id, mu_id, side, mu_name, damage, battle_created_at, recorded_at,
                 attacker_country_id, defender_country_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(battle_id, mu_id, side) DO UPDATE SET
                damage              = excluded.damage,
                recorded_at         = excluded.recorded_at,
                attacker_country_id = COALESCE(excluded.attacker_country_id, attacker_country_id),
                defender_country_id = COALESCE(excluded.defender_country_id, defender_country_id)
            """,
            (battle_id, mu_id, side, mu_name, damage, battle_created_at, recorded_at,
             attacker_country_id, defender_country_id),
        )

    async def commit_battle_mu_hits(self) -> None:
        """Commit pending MU hit inserts."""
        await self._conn.commit()

    async def insert_battle_country_hit(
        self,
        battle_id: str,
        country_id: str,
        side: str,
        damage: float,
        battle_created_at: str,
        recorded_at: str,
    ) -> None:
        """Upsert a single country-side-battle damage entry."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO battle_country_hits
                (battle_id, country_id, side, damage, battle_created_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (battle_id, country_id, side, damage, battle_created_at, recorded_at),
        )

    async def commit_battle_country_hits(self) -> None:
        """Commit pending country hit inserts."""
        await self._conn.commit()

    async def get_battle_hits_count(self) -> int:
        """Return total number of stored battle_hits rows (for diagnostics)."""
        async with self._conn.execute("SELECT COUNT(*) FROM battle_hits") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_latest_processed_at(self) -> Optional[str]:
        """Return the most recent processed_at timestamp from processed_battles, or None."""
        async with self._conn.execute(
            "SELECT MAX(processed_at) FROM processed_battles"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] else None

    async def get_mu_damage_in_range(
        self, mu_ids: list[str], start_iso: str, end_iso: str
    ) -> dict[str, float]:
        """Return {mu_id: total_damage} for the given MU IDs and date range.

        Uses battle_mu_hits which stores per-MU damage per battle.
        """
        if not mu_ids:
            return {}
        placeholders = ",".join("?" for _ in mu_ids)
        result: dict[str, float] = {}
        async with self._conn.execute(
            f"""
            SELECT mu_id, SUM(damage) AS total_damage
            FROM battle_mu_hits
            WHERE mu_id IN ({placeholders})
              AND battle_created_at >= ?
              AND battle_created_at < ?
            GROUP BY mu_id
            """,
            (*mu_ids, start_iso, end_iso),
        ) as cur:
            async for row in cur:
                result[row[0]] = float(row[1] or 0)
        return result

    # ------------------------------------------------------------------ #
    # mu_battle_member_damage helpers
    # ------------------------------------------------------------------ #

    async def upsert_mu_battle_members(
        self,
        battle_id: str,
        mu_id: str,
        members: list[tuple[str, float]],  # [(user_id, damage), ...]
        battle_created_at: str,
        recorded_at: str,
    ) -> None:
        """Bulk-upsert per-member damage for a single battle+MU."""
        for user_id, damage in members:
            await self._conn.execute(
                """
                INSERT OR REPLACE INTO mu_battle_member_damage
                    (battle_id, mu_id, user_id, damage, battle_created_at, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (battle_id, mu_id, user_id, damage, battle_created_at, recorded_at),
            )
        await self._conn.commit()

    async def get_mu_battle_members(
        self, battle_id: str, mu_id: str
    ) -> list[dict]:
        """Return per-member damage for a battle+MU, sorted by damage desc."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT mbmd.user_id,
                   COALESCE(cl.citizen_name, mbmd.user_id) AS citizen_name,
                   mbmd.damage,
                   mbmd.recorded_at
            FROM mu_battle_member_damage mbmd
            LEFT JOIN citizen_levels cl ON cl.user_id = mbmd.user_id
            WHERE mbmd.battle_id = ? AND mbmd.mu_id = ?
            ORDER BY mbmd.damage DESC
            """,
            (battle_id, mu_id),
        ) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1],
                    "damage": float(row[2] or 0),
                    "recorded_at": row[3],
                })
        return rows

    async def get_member_recorded_at(
        self, battle_id: str, mu_id: str
    ) -> Optional[str]:
        """Return the recorded_at timestamp of the most recent member entry for a battle+MU."""
        async with self._conn.execute(
            """
            SELECT MAX(recorded_at) FROM mu_battle_member_damage
            WHERE battle_id = ? AND mu_id = ?
            """,
            (battle_id, mu_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def get_mu_battle_damage(
        self, battle_id: str, mu_id: str, side: str
    ) -> Optional[float]:
        """Return stored MU damage for a specific battle+mu+side, or None."""
        async with self._conn.execute(
            "SELECT damage FROM battle_mu_hits WHERE battle_id=? AND mu_id=? AND side=?",
            (battle_id, mu_id, side),
        ) as cur:
            row = await cur.fetchone()
            return float(row[0]) if row else None

    async def search_citizen_ids_by_name(
        self, name_query: str, mu_id: str
    ) -> list[str]:
        """Return user_ids from mu_battle_member_damage for *mu_id* whose
        citizen_name LIKE %name_query%."""
        rows: list[str] = []
        async with self._conn.execute(
            """
            SELECT DISTINCT mbmd.user_id
            FROM mu_battle_member_damage mbmd
            JOIN citizen_levels cl ON cl.user_id = mbmd.user_id
            WHERE mbmd.mu_id = ? AND cl.citizen_name LIKE ?
            """,
            (mu_id, f"%{name_query}%"),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def get_mu_battles_paged(
        self,
        mu_id: str,
        limit: int = 50,
        offset: int = 0,
        att_country_ids: Optional[list[str]] = None,
        def_country_ids: Optional[list[str]] = None,
        player_user_ids: Optional[list[str]] = None,
        exclude_battle_ids: Optional[list[str]] = None,
        min_damage: Optional[float] = None,
    ) -> tuple[list[dict], int]:
        """Return paginated battles for a MU from battle_mu_hits + processed_battles.

        JOIN params must come before WHERE params in SQLite's positional binding,
        so we build join_params and where_params separately and concatenate them.

        Returns (rows, total_count).
        """
        join_extra = ""
        join_params: list = []
        conds: list[str] = ["bmh.mu_id = ?"]
        where_params: list = [mu_id]

        # JOIN for player filter (must appear before WHERE in SQL → params first)
        if player_user_ids:
            ph = ",".join("?" * len(player_user_ids))
            join_extra = f"""
            JOIN mu_battle_member_damage mbmd2
              ON mbmd2.battle_id = bmh.battle_id AND mbmd2.mu_id = bmh.mu_id
             AND mbmd2.user_id IN ({ph})
            """
            join_params.extend(player_user_ids)

        if att_country_ids or def_country_ids:
            sub_conds = []
            if att_country_ids:
                ph = ",".join("?" * len(att_country_ids))
                sub_conds.append(f"pb.attacker_country_id IN ({ph})")
                where_params.extend(att_country_ids)
            if def_country_ids:
                ph = ",".join("?" * len(def_country_ids))
                sub_conds.append(f"pb.defender_country_id IN ({ph})")
                where_params.extend(def_country_ids)
            conds.append(f"({' OR '.join(sub_conds)})")

        if exclude_battle_ids:
            ph = ",".join("?" * len(exclude_battle_ids))
            conds.append(f"bmh.battle_id NOT IN ({ph})")
            where_params.extend(exclude_battle_ids)

        if min_damage is not None and min_damage > 0:
            conds.append("bmh.damage >= ?")
            where_params.append(min_damage)

        conds.append(
            "EXISTS (SELECT 1 FROM mu_battle_member_damage"
            " WHERE battle_id = bmh.battle_id AND mu_id = bmh.mu_id)"
        )

        where = "WHERE " + " AND ".join(conds)
        all_params = join_params + where_params

        # pb JOIN is only needed for country-filter WHERE conditions
        pb_join = (
            "LEFT JOIN processed_battles pb ON pb.battle_id = bmh.battle_id"
            if (att_country_ids or def_country_ids)
            else ""
        )

        total = 0
        async with self._conn.execute(
            f"""
            SELECT COUNT(DISTINCT bmh.battle_id || '|' || bmh.side)
            FROM battle_mu_hits bmh
            {pb_join}
            {join_extra}
            {where}
            """,
            all_params,
        ) as cur:
            row = await cur.fetchone()
            total = row[0] if row else 0

        rows: list[dict] = []
        async with self._conn.execute(
            f"""
            SELECT bmh.battle_id, bmh.side, bmh.damage, bmh.battle_created_at,
                   bmh.attacker_country_id, bmh.defender_country_id
            FROM battle_mu_hits bmh
            {pb_join}
            {join_extra}
            {where}
            ORDER BY bmh.battle_created_at DESC, bmh.battle_id DESC
            LIMIT ? OFFSET ?
            """,
            all_params + [limit, offset],
        ) as cur:
            async for row in cur:
                rows.append({
                    "battle_id": row[0],
                    "side": row[1],
                    "mu_damage": float(row[2] or 0),
                    "battle_created_at": row[3] or "",
                    "att_country_id": row[4] or "",
                    "def_country_id": row[5] or "",
                })
        return rows, total

    async def get_tracked_mu_members_in_battles(
        self, battle_ids: list[str]
    ) -> list[tuple[str, str, str, str]]:
        """Return (battle_id, mu_id, side, user_id) for MUs that:
          1. participated in *battle_ids* (in battle_mu_hits),
          2. have at least one member in citizen_levels, AND
          3. have been visited before (have any row in mu_battle_member_damage).

        Condition 3 avoids re-fetching data for thousands of historic MUs that
        nobody has visited. First-visit data is seeded by the fire-and-forget path.
        """
        if not battle_ids:
            return []
        ph = ",".join("?" * len(battle_ids))
        rows: list[tuple[str, str, str, str]] = []
        async with self._conn.execute(
            f"""
            SELECT DISTINCT bmh.battle_id, bmh.mu_id, bmh.side, cl.user_id
            FROM battle_mu_hits bmh
            JOIN citizen_levels cl ON cl.mu_id = bmh.mu_id
            WHERE bmh.battle_id IN ({ph})
              AND EXISTS (
                  SELECT 1 FROM mu_battle_member_damage
                  WHERE mu_id = bmh.mu_id
              )
            """,
            battle_ids,
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2], row[3]))
        return rows

    async def get_battles_without_members(
        self,
        mu_id: str,
        limit: int = 100,
        min_created_at: Optional[str] = None,
    ) -> list[tuple[str, str, str]]:
        """Return (battle_id, side, battle_created_at) for battles of *mu_id*
        that have no entry in mu_battle_member_damage yet, newest first.

        min_created_at: if given, only battles on or after this ISO timestamp.
        """
        rows: list[tuple[str, str, str]] = []
        if min_created_at:
            sql = """
                SELECT bmh.battle_id, bmh.side, bmh.battle_created_at
                FROM battle_mu_hits bmh
                WHERE bmh.mu_id = ?
                  AND bmh.battle_created_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM mu_battle_member_damage
                      WHERE battle_id = bmh.battle_id AND mu_id = bmh.mu_id
                  )
                ORDER BY bmh.battle_created_at DESC
                LIMIT ?
            """
            params: tuple = (mu_id, min_created_at, limit)
        else:
            sql = """
                SELECT bmh.battle_id, bmh.side, bmh.battle_created_at
                FROM battle_mu_hits bmh
                WHERE bmh.mu_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM mu_battle_member_damage
                      WHERE battle_id = bmh.battle_id AND mu_id = bmh.mu_id
                  )
                ORDER BY bmh.battle_created_at DESC
                LIMIT ?
            """
            params = (mu_id, limit)
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((row[0], row[1], row[2] or ""))
        return rows

    async def get_mu_member_battle_count(self, mu_id: str) -> int:
        """Return how many distinct battles already have member data for *mu_id*."""
        async with self._conn.execute(
            "SELECT COUNT(DISTINCT battle_id) FROM mu_battle_member_damage WHERE mu_id = ?",
            (mu_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def get_visited_mu_ids(self) -> list[str]:
        """Return distinct mu_ids that have any entry in mu_battle_member_damage."""
        rows: list[str] = []
        async with self._conn.execute(
            "SELECT DISTINCT mu_id FROM mu_battle_member_damage"
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def get_all_tracked_mu_ids(self) -> list[str]:
        """Return all mu_ids that have battles AND current members in citizen_levels.

        Visited MUs (those with any member data already) are returned first so
        the backfill continues filling them before starting on unvisited MUs.
        """
        rows: list[str] = []
        async with self._conn.execute(
            """
            SELECT DISTINCT bmh.mu_id
            FROM battle_mu_hits bmh
            WHERE EXISTS (
                SELECT 1 FROM citizen_levels cl WHERE cl.mu_id = bmh.mu_id
            )
            ORDER BY
                EXISTS (
                    SELECT 1 FROM mu_battle_member_damage mbd WHERE mbd.mu_id = bmh.mu_id
                ) DESC,
                bmh.mu_id
            """
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def get_mu_current_members(self, mu_id: str) -> list[str]:
        """Return user_ids from citizen_levels whose current mu_id matches."""
        rows: list[str] = []
        async with self._conn.execute(
            "SELECT user_id FROM citizen_levels WHERE mu_id = ?",
            (mu_id,),
        ) as cur:
            async for row in cur:
                rows.append(row[0])
        return rows

    async def get_player_damage_in_range(
        self, user_ids: list[str], start_iso: str, end_iso: str
    ) -> dict[str, tuple[str, float]]:
        """Return {user_id: (citizen_name, total_damage)} for the given players and date range.

        Uses battle_hits joined with citizen_levels for names.
        """
        if not user_ids:
            return {}
        placeholders = ",".join("?" for _ in user_ids)
        result: dict[str, tuple[str, float]] = {}
        async with self._conn.execute(
            f"""
            SELECT bh.user_id,
                   COALESCE(MAX(cl.citizen_name), bh.user_id) AS citizen_name,
                   SUM(bh.damage) AS total_damage
            FROM battle_hits bh
            LEFT JOIN citizen_levels cl ON cl.user_id = bh.user_id
            WHERE bh.user_id IN ({placeholders})
              AND bh.battle_created_at >= ?
              AND bh.battle_created_at < ?
            GROUP BY bh.user_id
            """,
            (*user_ids, start_iso, end_iso),
        ) as cur:
            async for row in cur:
                result[str(row[0])] = (str(row[1]), float(row[2] or 0))
        return result
