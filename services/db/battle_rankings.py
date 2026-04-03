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
    ) -> None:
        """Upsert a single MU-side-battle damage entry."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO battle_mu_hits
                (battle_id, mu_id, side, mu_name, damage, battle_created_at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (battle_id, mu_id, side, mu_name, damage, battle_created_at, recorded_at),
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
