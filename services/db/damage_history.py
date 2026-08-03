"""Weekly/daily damage history built from the hourly weeklyUserDamages ranking.

Two tables back everything here (see database/schema.sql):

``citizen_weekly_damage_history``
    One row per player per game week — a straight snapshot of the API's
    weekly damage counter.

``citizen_daily_damage``
    One row per player per game day.  The game does not expose a daily
    counter, so a day's damage is derived as ``weekly_end - baseline``:
    the weekly counter's value now, minus its value when the day opened.
    :meth:`DamageHistoryMixin.apply_weekly_damage_snapshot` maintains both.
"""

from __future__ import annotations

from typing import Any, Optional

import aiosqlite

from services.game_time import game_week_start, week_start_of_day


class DamageHistoryMixin:
    """Weekly-history and derived daily-damage storage and queries."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    # ── Write path (used by the hourly fetcher sweep) ──────────────────────

    async def apply_weekly_damage_snapshot(
        self,
        entries: list[dict],
        *,
        game_date: str,
        week_start: str,
        updated_at: str,
    ) -> dict[str, int]:
        """Record one hourly ``weeklyUserDamages`` reading.

        *entries* is a list of dicts with keys ``user_id``, ``citizen_name``,
        ``country_id``, ``mu_id``, ``mu_name`` and ``weekly_damage``.

        Writes the current-week snapshot, the weekly history row, and the
        derived daily row for *game_date*.  When this is the first reading of
        a new game day, the previous day is also closed out using this same
        reading — the value the API publishes at the top of the hour that a
        day starts is simultaneously the previous day's final total and the
        new day's baseline, so both days stay exact.

        Returns counts: ``{"weekly": n, "daily": n, "closed": n}``.
        """
        if not entries:
            return {"weekly": 0, "daily": 0, "closed": 0}

        # Rows already written for this game day keep their original baseline;
        # re-deriving it every sweep would make the day's total drift.
        existing_today: dict[str, float] = {}
        async with self._conn.execute(
            "SELECT user_id, baseline FROM citizen_daily_damage WHERE game_date = ?",
            (game_date,),
        ) as cur:
            async for row in cur:
                existing_today[str(row[0])] = float(row[1] or 0.0)

        # Most recent earlier day per player, to detect a day rollover and to
        # know which week that reading belonged to.  Bounded to a short window
        # so this stays a small scan as history grows.
        prev_rows: dict[str, tuple[str, str, float]] = {}
        async with self._conn.execute(
            """
            SELECT user_id, game_date, week_start, baseline
              FROM (
                SELECT user_id, game_date, week_start, baseline,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id ORDER BY game_date DESC
                       ) AS rn
                  FROM citizen_daily_damage
                 WHERE game_date < ? AND game_date >= date(?, '-8 days')
              )
             WHERE rn = 1
            """,
            (game_date, game_date),
        ) as cur:
            async for row in cur:
                prev_rows[str(row[0])] = (str(row[1]), str(row[2]), float(row[3] or 0.0))

        weekly_rows: list[tuple] = []
        daily_rows: list[tuple] = []
        closeout_rows: list[tuple] = []

        for e in entries:
            uid = e.get("user_id")
            if not uid:
                continue
            dmg = float(e.get("weekly_damage") or 0.0)
            name = e.get("citizen_name")
            country_id = e.get("country_id")
            mu_id = e.get("mu_id")
            mu_name = e.get("mu_name")

            weekly_rows.append(
                (uid, week_start, name, country_id, mu_id, mu_name, dmg, updated_at)
            )

            if uid in existing_today:
                baseline = existing_today[uid]
            else:
                prev = prev_rows.get(uid)
                if prev is not None and prev[1] == week_start:
                    # Same week, new day: this reading is the day-boundary value,
                    # so it is both the previous day's close and today's baseline.
                    baseline = dmg
                    closeout_rows.append((dmg, updated_at, uid, prev[0]))
                else:
                    # No prior reading, or the weekly counter reset at this very
                    # boundary (Monday 02:00) — the day starts from zero.
                    baseline = 0.0

            daily_rows.append(
                (
                    uid, game_date, week_start, name, country_id, mu_id, mu_name,
                    baseline, dmg, max(0.0, dmg - baseline), updated_at,
                )
            )

        if closeout_rows:
            await self._conn.executemany(
                """
                UPDATE citizen_daily_damage
                   SET weekly_end = ?,
                       damage     = MAX(0.0, ? - baseline),
                       updated_at = ?
                 WHERE user_id = ? AND game_date = ?
                """,
                [(w, w, ts, uid, gd) for (w, ts, uid, gd) in closeout_rows],
            )

        await self._conn.executemany(
            """
            INSERT INTO citizen_weekly_damage_history
                (user_id, week_start, citizen_name, country_id, mu_id, mu_name,
                 weekly_damage, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, week_start) DO UPDATE SET
                citizen_name  = COALESCE(excluded.citizen_name, citizen_weekly_damage_history.citizen_name),
                country_id    = COALESCE(excluded.country_id, citizen_weekly_damage_history.country_id),
                mu_id         = excluded.mu_id,
                mu_name       = excluded.mu_name,
                weekly_damage = excluded.weekly_damage,
                updated_at    = excluded.updated_at
            """,
            weekly_rows,
        )

        await self._conn.executemany(
            """
            INSERT INTO citizen_daily_damage
                (user_id, game_date, week_start, citizen_name, country_id,
                 mu_id, mu_name, baseline, weekly_end, damage, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, game_date) DO UPDATE SET
                citizen_name = COALESCE(excluded.citizen_name, citizen_daily_damage.citizen_name),
                country_id   = COALESCE(excluded.country_id, citizen_daily_damage.country_id),
                mu_id        = excluded.mu_id,
                mu_name      = excluded.mu_name,
                weekly_end   = excluded.weekly_end,
                damage       = MAX(0.0, excluded.weekly_end - citizen_daily_damage.baseline),
                updated_at   = excluded.updated_at
            """,
            daily_rows,
        )

        # Keep the flat current-week table in sync — the /geluk, profile and
        # paraatheid code paths still read it as "damage this week".
        await self._conn.executemany(
            """
            INSERT INTO citizen_weekly_damages
                (user_id, citizen_name, country_id, weekly_damage, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                citizen_name  = COALESCE(excluded.citizen_name, citizen_weekly_damages.citizen_name),
                country_id    = excluded.country_id,
                weekly_damage = excluded.weekly_damage,
                updated_at    = excluded.updated_at
            """,
            [
                (uid, name, country_id or "", dmg, updated_at)
                for (uid, _wk, name, country_id, _mid, _mn, dmg, _ts) in weekly_rows
            ],
        )

        await self._conn.commit()
        return {
            "weekly": len(weekly_rows),
            "daily": len(daily_rows),
            "closed": len(closeout_rows),
        }

    # ── Available periods (for command dropdowns / website selectors) ──────

    async def get_recorded_weeks(self, limit: int = 26) -> list[str]:
        """Return recorded game-week starts (``YYYY-MM-DD``), newest first."""
        rows: list[str] = []
        async with self._conn.execute(
            "SELECT DISTINCT week_start FROM citizen_weekly_damage_history "
            "ORDER BY week_start DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(str(row[0]))
        return rows

    async def get_recorded_game_days(self, limit: int = 30) -> list[str]:
        """Return recorded game days (``YYYY-MM-DD``), newest first."""
        rows: list[str] = []
        async with self._conn.execute(
            "SELECT DISTINCT game_date FROM citizen_daily_damage "
            "ORDER BY game_date DESC LIMIT ?",
            (limit,),
        ) as cur:
            async for row in cur:
                rows.append(str(row[0]))
        return rows

    # ── Weekly queries ────────────────────────────────────────────────────

    async def get_weekly_damage_ranking(
        self, country_id: str, week_start: str, limit: int = 10
    ) -> tuple[list[dict], Optional[str]]:
        """Top players of *country_id* for a game week.

        Ranks every in-game citizen recorded for that country in that week —
        no Discord-account matching involved, so players who have since moved
        country still appear under the country they fought for.
        """
        rows: list[dict] = []
        last_updated: Optional[str] = None
        async with self._conn.execute(
            """
            SELECT user_id, COALESCE(citizen_name, user_id), weekly_damage,
                   mu_name, updated_at
              FROM citizen_weekly_damage_history
             WHERE country_id = ? AND week_start = ? AND weekly_damage > 0
             ORDER BY weekly_damage DESC
             LIMIT ?
            """,
            (country_id, week_start, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "user_id": str(row[0]),
                    "citizen_name": str(row[1]),
                    "damage": float(row[2] or 0.0),
                    "mu_name": row[3],
                })
                if last_updated is None:
                    last_updated = row[4]
        return rows, last_updated

    async def get_weekly_damage_player(
        self, week_start: str, query: str, *, country_id: Optional[str] = None
    ) -> Optional[dict]:
        """Look up one player's weekly damage by user id or name."""
        sql = """
            SELECT user_id, COALESCE(citizen_name, user_id), weekly_damage,
                   country_id, mu_name, updated_at
              FROM citizen_weekly_damage_history
             WHERE week_start = ?
               AND (user_id = ? OR lower(citizen_name) = lower(?)
                    OR lower(citizen_name) LIKE lower(?))
        """
        params: list[Any] = [week_start, query, query, f"%{query}%"]
        if country_id:
            sql += " AND country_id = ?"
            params.append(country_id)
        sql += " ORDER BY weekly_damage DESC LIMIT 1"
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "user_id": str(row[0]),
            "citizen_name": str(row[1]),
            "damage": float(row[2] or 0.0),
            "country_id": row[3],
            "mu_name": row[4],
            "updated_at": row[5],
        }

    async def get_weekly_damage_rank(
        self, country_id: str, week_start: str, user_id: str
    ) -> Optional[int]:
        """Return the player's 1-based rank within their country for a week."""
        async with self._conn.execute(
            """
            SELECT COUNT(*) + 1
              FROM citizen_weekly_damage_history
             WHERE country_id = ? AND week_start = ?
               AND weekly_damage > COALESCE((
                     SELECT weekly_damage FROM citizen_weekly_damage_history
                      WHERE user_id = ? AND week_start = ?
                   ), -1)
            """,
            (country_id, week_start, user_id, week_start),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def get_player_weekly_history(
        self, user_id: str, limit: int = 12
    ) -> list[dict]:
        """Return a player's weekly damage per game week, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT week_start, weekly_damage FROM citizen_weekly_damage_history "
            "WHERE user_id = ? ORDER BY week_start DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "week_start": str(row[0]),
                    "damage": float(row[1] or 0.0),
                })
        return rows

    async def get_mu_weekly_history(self, mu_id: str, limit: int = 12) -> list[dict]:
        """Return an MU's summed weekly damage per game week, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT week_start, SUM(weekly_damage), COUNT(*)
              FROM citizen_weekly_damage_history
             WHERE mu_id = ?
             GROUP BY week_start
             ORDER BY week_start DESC LIMIT ?
            """,
            (mu_id, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "week_start": str(row[0]),
                    "damage": float(row[1] or 0.0),
                    "members": int(row[2] or 0),
                })
        return rows

    async def get_country_weekly_history(
        self, country_id: str, limit: int = 12
    ) -> list[dict]:
        """Return a country's summed weekly damage per game week, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT week_start, SUM(weekly_damage), COUNT(*)
              FROM citizen_weekly_damage_history
             WHERE country_id = ?
             GROUP BY week_start
             ORDER BY week_start DESC LIMIT ?
            """,
            (country_id, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "week_start": str(row[0]),
                    "damage": float(row[1] or 0.0),
                    "members": int(row[2] or 0),
                })
        return rows

    # ── Daily queries ─────────────────────────────────────────────────────

    async def get_daily_damage_ranking(
        self, game_date: str, limit: int = 10, *, country_id: Optional[str] = None
    ) -> list[dict]:
        """Top players by damage for a game day, optionally scoped to a country."""
        sql = """
            SELECT user_id, COALESCE(citizen_name, user_id), damage, mu_name
              FROM citizen_daily_damage
             WHERE game_date = ? AND damage > 0
        """
        params: list[Any] = [game_date]
        if country_id:
            sql += " AND country_id = ?"
            params.append(country_id)
        sql += " ORDER BY damage DESC LIMIT ?"
        params.append(limit)

        rows: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append({
                    "user_id": str(row[0]),
                    "citizen_name": str(row[1]),
                    "damage": float(row[2] or 0.0),
                    "mu_name": row[3],
                })
        return rows

    async def get_daily_damage_player(
        self, game_date: str, query: str
    ) -> Optional[dict]:
        """Look up one player's damage for a game day by user id or name."""
        async with self._conn.execute(
            """
            SELECT user_id, COALESCE(citizen_name, user_id), damage,
                   country_id, mu_name, updated_at
              FROM citizen_daily_damage
             WHERE game_date = ?
               AND (user_id = ? OR lower(citizen_name) = lower(?)
                    OR lower(citizen_name) LIKE lower(?))
             ORDER BY damage DESC LIMIT 1
            """,
            (game_date, query, query, f"%{query}%"),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "user_id": str(row[0]),
            "citizen_name": str(row[1]),
            "damage": float(row[2] or 0.0),
            "country_id": row[3],
            "mu_name": row[4],
            "updated_at": row[5],
        }

    async def get_daily_damage_rank(
        self, game_date: str, user_id: str, *, country_id: Optional[str] = None
    ) -> Optional[int]:
        """Return a player's 1-based rank for a game day."""
        sql = """
            SELECT COUNT(*) + 1 FROM citizen_daily_damage
             WHERE game_date = ?
               AND damage > COALESCE((
                     SELECT damage FROM citizen_daily_damage
                      WHERE user_id = ? AND game_date = ?
                   ), -1)
        """
        params: list[Any] = [game_date, user_id, game_date]
        if country_id:
            sql += " AND country_id = ?"
            params.append(country_id)
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def get_daily_damage_by_country(
        self, game_date: str, limit: int = 10
    ) -> list[dict]:
        """Aggregate a game day's damage per country."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT cdd.country_id,
                   COALESCE(cs.name, cdd.country_id),
                   SUM(cdd.damage),
                   COUNT(*)
              FROM citizen_daily_damage cdd
              LEFT JOIN country_snapshots cs ON cs.country_id = cdd.country_id
             WHERE cdd.game_date = ? AND cdd.damage > 0 AND cdd.country_id IS NOT NULL
             GROUP BY cdd.country_id
             ORDER BY SUM(cdd.damage) DESC LIMIT ?
            """,
            (game_date, limit),
        ) as cur:
            async for row in cur:
                rows.append({
                    "country_id": str(row[0]),
                    "name": str(row[1]),
                    "damage": float(row[2] or 0.0),
                    "fighters": int(row[3] or 0),
                })
        return rows

    async def get_daily_damage_by_mu(
        self, game_date: str, limit: int = 10, *, country_id: Optional[str] = None
    ) -> list[dict]:
        """Aggregate a game day's damage per MU."""
        sql = """
            SELECT mu_id, COALESCE(mu_name, mu_id), SUM(damage), COUNT(*)
              FROM citizen_daily_damage
             WHERE game_date = ? AND damage > 0 AND mu_id IS NOT NULL
        """
        params: list[Any] = [game_date]
        if country_id:
            sql += " AND country_id = ?"
            params.append(country_id)
        sql += " GROUP BY mu_id ORDER BY SUM(damage) DESC LIMIT ?"
        params.append(limit)

        rows: list[dict] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append({
                    "mu_id": str(row[0]),
                    "name": str(row[1]),
                    "damage": float(row[2] or 0.0),
                    "fighters": int(row[3] or 0),
                })
        return rows

    async def get_country_daily_total(
        self, game_date: str, country_id: str
    ) -> Optional[dict]:
        """Total damage + fighter count for one country on a game day."""
        async with self._conn.execute(
            """
            SELECT SUM(damage), COUNT(*) FROM citizen_daily_damage
             WHERE game_date = ? AND country_id = ? AND damage > 0
            """,
            (game_date, country_id),
        ) as cur:
            row = await cur.fetchone()
        if not row or row[1] == 0:
            return None
        return {"damage": float(row[0] or 0.0), "fighters": int(row[1] or 0)}

    async def get_mu_daily_total(self, game_date: str, mu_id: str) -> Optional[dict]:
        """Total damage + fighter count for one MU on a game day."""
        async with self._conn.execute(
            """
            SELECT SUM(damage), COUNT(*) FROM citizen_daily_damage
             WHERE game_date = ? AND mu_id = ? AND damage > 0
            """,
            (game_date, mu_id),
        ) as cur:
            row = await cur.fetchone()
        if not row or row[1] == 0:
            return None
        return {"damage": float(row[0] or 0.0), "fighters": int(row[1] or 0)}

    # ── Per-entity daily series (website detail pages) ─────────────────────

    async def get_player_daily_series(
        self, user_id: str, days: int = 30
    ) -> list[dict]:
        """Return a player's daily damage, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            "SELECT game_date, damage FROM citizen_daily_damage "
            "WHERE user_id = ? ORDER BY game_date DESC LIMIT ?",
            (user_id, days),
        ) as cur:
            async for row in cur:
                rows.append({
                    "game_date": str(row[0]),
                    "damage": float(row[1] or 0.0),
                })
        return rows

    async def get_mu_daily_series(self, mu_id: str, days: int = 30) -> list[dict]:
        """Return an MU's summed daily damage, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT game_date, SUM(damage), COUNT(*)
              FROM citizen_daily_damage
             WHERE mu_id = ?
             GROUP BY game_date ORDER BY game_date DESC LIMIT ?
            """,
            (mu_id, days),
        ) as cur:
            async for row in cur:
                rows.append({
                    "game_date": str(row[0]),
                    "damage": float(row[1] or 0.0),
                    "fighters": int(row[2] or 0),
                })
        return rows

    async def get_country_daily_series(
        self, country_id: str, days: int = 30
    ) -> list[dict]:
        """Return a country's summed daily damage, newest first."""
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT game_date, SUM(damage), COUNT(*)
              FROM citizen_daily_damage
             WHERE country_id = ?
             GROUP BY game_date ORDER BY game_date DESC LIMIT ?
            """,
            (country_id, days),
        ) as cur:
            async for row in cur:
                rows.append({
                    "game_date": str(row[0]),
                    "damage": float(row[1] or 0.0),
                    "fighters": int(row[2] or 0),
                })
        return rows

    # ── Convenience ───────────────────────────────────────────────────────

    async def get_current_week_start(self) -> str:
        """Return the current game week start (helper for callers)."""
        return game_week_start()

    async def resolve_week_start(self, game_date: str) -> str:
        """Return the game week containing *game_date*."""
        return week_start_of_day(game_date)
