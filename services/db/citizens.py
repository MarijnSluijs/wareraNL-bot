"""Citizen-level DB methods (citizen_levels table)."""
from __future__ import annotations

import difflib
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite


class CitizensMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """citizen_levels table operations."""

    async def upsert_citizen_level(
        self,
        user_id: str,
        country_id: str,
        level: int,
        updated_at: str,
        skill_mode: Optional[str] = None,
        last_skills_reset_at: Optional[str] = None,
        citizen_name: Optional[str] = None,
        last_login_at: Optional[str] = None,
        mu_id: Optional[str] = None,
        mu_name: Optional[str] = None,
    ) -> None:
        """Insert or update a citizen's level and related info."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO citizen_levels"
            "(user_id, country_id, level, skill_mode, last_skills_reset_at, "
            "citizen_name, last_login_at, mu_id, mu_name, updated_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, country_id, level, skill_mode, last_skills_reset_at,
             citizen_name, last_login_at, mu_id, mu_name, updated_at),
        )

    async def update_citizen_mu(
        self, user_id: str, mu_id: Optional[str], mu_name: Optional[str]
    ) -> None:
        """Update a citizen's military unit information."""
        await self._conn.execute(
            "UPDATE citizen_levels SET mu_id = ?, mu_name = ? WHERE user_id = ?",
            (mu_id, mu_name, user_id),
        )

    async def clear_citizen_mus_for_country(self, country_id: str) -> None:
        """Clear military unit information for all citizens in a specific country."""
        await self._conn.execute(
            "UPDATE citizen_levels SET mu_id = NULL, mu_name = NULL WHERE country_id = ?",
            (country_id,),
        )
        await self._conn.commit()

    async def flush_citizen_levels(self) -> None:
        """Commit any pending changes to the citizen_levels table."""
        await self._conn.commit()

    async def delete_citizens_for_country(self, country_id: str) -> None:
        """Delete all citizen level records for a specific country."""
        await self._conn.execute(
            "DELETE FROM citizen_levels WHERE country_id = ?", (country_id,)
        )
        await self._conn.commit()

    async def get_level_distribution(
        self, country_id: Optional[str]
    ) -> tuple[dict[int, int], dict[int, int], Optional[str]]:
        """Return (level_counts, active_counts, last_updated_at).

        active_counts: citizens whose last_login_at is within 24 hours.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        counts: dict[int, int] = {}
        active: dict[int, int] = {}
        last_updated: Optional[str] = None

        if country_id:
            sql = "SELECT level, updated_at, last_login_at FROM citizen_levels WHERE country_id = ?"
            params: tuple = (country_id,)
        else:
            sql = "SELECT level, updated_at, last_login_at FROM citizen_levels"
            params = ()
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                lvl, updated_at, last_login_at = row
                if lvl is not None:
                    lvl = int(lvl)
                    counts[lvl] = counts.get(lvl, 0) + 1
                    if last_login_at and last_login_at[:19] >= cutoff:
                        active[lvl] = active.get(lvl, 0) + 1
                if last_updated is None or updated_at > last_updated:
                    last_updated = updated_at
        return counts, active, last_updated

    async def get_skill_mode_distribution(
        self, country_id: Optional[str]
    ) -> tuple[int, int, int, Optional[str]]:
        """Return (eco_count, war_count, unknown_count, last_updated)."""
        eco = war = unknown = 0
        last_updated: Optional[str] = None
        if country_id:
            sql = "SELECT skill_mode, updated_at FROM citizen_levels WHERE country_id = ?"
            params: tuple = (country_id,)
        else:
            sql = "SELECT skill_mode, updated_at FROM citizen_levels"
            params = ()
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mode, upd = row
                if mode == "eco":
                    eco += 1
                elif mode == "war":
                    war += 1
                else:
                    unknown += 1
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        return eco, war, unknown, last_updated

    async def get_skill_mode_by_level_buckets(
        self, country_id: Optional[str]
    ) -> tuple[dict[int, dict[str, int]], Optional[str]]:
        """Eco/war/unknown counts per 5-level bucket."""
        if country_id:
            sql = "SELECT level, skill_mode, updated_at FROM citizen_levels WHERE country_id = ?"
            params: tuple = (country_id,)
        else:
            sql = "SELECT level, skill_mode, updated_at FROM citizen_levels"
            params = ()
        buckets: dict[int, dict[str, int]] = {}
        last_updated: Optional[str] = None
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                level, mode, upd = row
                bucket = ((int(level or 1) - 1) // 5) * 5 + 1
                if bucket not in buckets:
                    buckets[bucket] = {"eco": 0, "war": 0, "unknown": 0}
                if mode == "eco":
                    buckets[bucket]["eco"] += 1
                elif mode == "war":
                    buckets[bucket]["war"] += 1
                else:
                    buckets[bucket]["unknown"] += 1
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        return buckets, last_updated

    async def get_skill_mode_by_mu(
        self, country_id: Optional[str]
    ) -> dict[str, dict]:
        """Eco/war/unknown counts + player list grouped by MU name."""
        if country_id:
            sql = (
                "SELECT mu_name, citizen_name, level, skill_mode "
                "FROM citizen_levels WHERE country_id = ? ORDER BY mu_name, level DESC"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT mu_name, citizen_name, level, skill_mode "
                "FROM citizen_levels ORDER BY mu_name, level DESC"
            )
            params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu, name, level, mode = row
                key = mu or ""
                if key not in mus:
                    mus[key] = {"eco": 0, "war": 0, "unknown": 0, "players": []}
                if mode == "eco":
                    mus[key]["eco"] += 1
                elif mode == "war":
                    mus[key]["war"] += 1
                else:
                    mus[key]["unknown"] += 1
                mus[key]["players"].append({
                    "citizen_name": name or "?",
                    "level": level,
                    "skill_mode": mode,
                })
        return mus

    async def get_citizen_cooldowns_by_mu(
        self, country_id: Optional[str]
    ) -> dict[str, dict]:
        """Skill-reset cooldown stats + player list grouped by MU name."""
        now = datetime.now(timezone.utc)
        if country_id:
            sql = (
                "SELECT mu_name, citizen_name, level, last_skills_reset_at "
                "FROM citizen_levels WHERE country_id = ? ORDER BY mu_name, level DESC"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT mu_name, citizen_name, level, last_skills_reset_at "
                "FROM citizen_levels ORDER BY mu_name, level DESC"
            )
            params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu, name, level, reset_at = row
                key = mu or ""
                if key not in mus:
                    mus[key] = {"count": 0, "sum_days": 0.0, "available": 0, "no_data": 0, "players": []}
                b = mus[key]
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                        b["count"] += 1
                        b["sum_days"] += days_ago
                        if can_reset:
                            b["available"] += 1
                    except Exception:
                        b["no_data"] += 1
                        b["available"] += 1
                else:
                    b["no_data"] += 1
                    b["available"] += 1
                b["players"].append({
                    "citizen_name": name or "?",
                    "level": level,
                    "days_ago": days_ago,
                    "can_reset": can_reset,
                })
        return mus

    async def get_skill_reset_cooldown_by_level_buckets(
        self, country_id: Optional[str]
    ) -> tuple[dict[int, dict], Optional[str]]:
        """Skill-reset cooldown per 5-level bucket (eco/unknown only)."""
        if country_id:
            sql = (
                "SELECT level, last_skills_reset_at, updated_at FROM citizen_levels "
                "WHERE country_id = ? AND (skill_mode IS NULL OR skill_mode != 'war')"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT level, last_skills_reset_at, updated_at FROM citizen_levels "
                "WHERE skill_mode IS NULL OR skill_mode != 'war'"
            )
            params = ()
        now = datetime.now(timezone.utc)
        buckets: dict[int, dict] = {}
        last_updated: Optional[str] = None
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                level, reset_at, upd = row
                bucket = ((int(level or 1) - 1) // 5) * 5 + 1
                if bucket not in buckets:
                    buckets[bucket] = {"count": 0, "sum_days": 0.0, "available": 0, "no_data": 0}
                b = buckets[bucket]
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        b["count"] += 1
                        b["sum_days"] += days_ago
                        if days_ago >= 7:
                            b["available"] += 1
                    except Exception:
                        b["no_data"] += 1
                        b["available"] += 1
                else:
                    b["no_data"] += 1
                    b["available"] += 1
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        result: dict[int, dict] = {}
        for bkt, b in buckets.items():
            result[bkt] = {
                "count": b["count"],
                "avg_days_ago": b["sum_days"] / b["count"] if b["count"] else 0.0,
                "available": b["available"],
                "no_data": b["no_data"],
            }
        return result, last_updated

    async def get_citizens_cooldown_list(
        self, country_id: str, limit: int = 50
    ) -> list[dict]:
        """Citizens for a country sorted by level DESC with cooldown data."""
        now = datetime.now(timezone.utc)
        sql = """
            SELECT user_id, citizen_name, level, last_skills_reset_at
            FROM citizen_levels
            WHERE country_id = ?
            ORDER BY level DESC, user_id
            LIMIT ?
        """
        rows: list[dict] = []
        async with self._conn.execute(sql, (country_id, limit)) as cur:
            async for row in cur:
                uid, name, level, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                rows.append({
                    "user_id": uid,
                    "citizen_name": name or uid,
                    "level": level,
                    "last_skills_reset_at": reset_at,
                    "days_ago": days_ago,
                    "can_reset": can_reset,
                })
        return rows

    async def find_citizen_cooldown(self, query: str) -> list[dict]:
        """Search citizens by name (partial) or exact user_id — cooldown data."""
        now = datetime.now(timezone.utc)
        sql = """
            SELECT user_id, citizen_name, level, country_id, last_skills_reset_at
            FROM citizen_levels
            WHERE user_id = ? OR lower(citizen_name) LIKE lower(?)
            ORDER BY level DESC
            LIMIT 10
        """
        rows: list[dict] = []
        async with self._conn.execute(sql, (query, f"%{query}%")) as cur:
            async for row in cur:
                uid, name, level, country_id, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                rows.append({
                    "user_id": uid,
                    "citizen_name": name or uid,
                    "level": level,
                    "country_id": country_id,
                    "last_skills_reset_at": reset_at,
                    "days_ago": days_ago,
                    "can_reset": can_reset,
                })
        return rows

    async def find_citizen_readiness(self, query: str) -> list[dict]:
        """Search citizens by name/id — skill mode + cooldown data."""
        now = datetime.now(timezone.utc)
        sql = """
            SELECT user_id, citizen_name, level, country_id, skill_mode, last_skills_reset_at
            FROM citizen_levels
            WHERE user_id = ? OR lower(citizen_name) LIKE lower(?)
            ORDER BY level DESC
            LIMIT 10
        """
        rows: list[dict] = []
        async with self._conn.execute(sql, (query, f"%{query}%")) as cur:
            async for row in cur:
                uid, name, level, country_id, mode, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                rows.append({
                    "user_id": uid,
                    "citizen_name": name or uid,
                    "level": level,
                    "country_id": country_id,
                    "skill_mode": mode,
                    "last_skills_reset_at": reset_at,
                    "days_ago": days_ago,
                    "can_reset": can_reset,
                })
        return rows

    async def get_mu_readiness_players(
        self, mu_query: str, country_id: Optional[str] = None
    ) -> tuple[Optional[str], list[dict]]:
        """Return (matched_mu_name, players) for the best-matching MU."""
        now = datetime.now(timezone.utc)
        if country_id:
            sql_mu = (
                "SELECT DISTINCT mu_name FROM citizen_levels "
                "WHERE country_id = ? AND lower(mu_name) LIKE lower(?) AND mu_name IS NOT NULL"
            )
            params_mu: tuple = (country_id, f"%{mu_query}%")
        else:
            sql_mu = (
                "SELECT DISTINCT mu_name FROM citizen_levels "
                "WHERE lower(mu_name) LIKE lower(?) AND mu_name IS NOT NULL"
            )
            params_mu = (f"%{mu_query}%",)
        mu_names: list[str] = []
        async with self._conn.execute(sql_mu, params_mu) as cur:
            async for row in cur:
                if row[0]:
                    mu_names.append(row[0])
        if not mu_names:
            return None, []
        exact = next((m for m in mu_names if m.lower() == mu_query.lower()), None)
        mu_name = exact or mu_names[0]
        if country_id:
            sql = (
                "SELECT citizen_name, level, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE mu_name = ? AND country_id = ? ORDER BY level DESC"
            )
            params2: tuple = (mu_name, country_id)
        else:
            sql = (
                "SELECT citizen_name, level, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE mu_name = ? ORDER BY level DESC"
            )
            params2 = (mu_name,)
        players: list[dict] = []
        async with self._conn.execute(sql, params2) as cur:
            async for row in cur:
                name, level, mode, reset_at = row
                days_ago: Optional[float] = None
                can_reset = True
                if reset_at:
                    try:
                        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                        days_ago = (now - ts).total_seconds() / 86400
                        can_reset = days_ago >= 7
                    except Exception:
                        pass
                players.append({
                    "citizen_name": name or "?",
                    "level": level,
                    "skill_mode": mode,
                    "days_ago": days_ago,
                    "can_reset": can_reset,
                })
        return mu_name, players
    
    async def get_citizen_name_by_id(self, user_id: str) -> Optional[str]:
        """Return the citizen name for a given user_id, or None if not found."""
        sql = "SELECT citizen_name FROM citizen_levels WHERE user_id = ?"
        async with self._conn.execute(sql, (user_id,)) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return row[0]
        return None

    async def get_distinct_mu_names(
        self, country_id: Optional[str] = None
    ) -> list[str]:
        """All distinct non-null MU names, optionally filtered by country."""
        if country_id:
            sql = (
                "SELECT DISTINCT mu_name FROM citizen_levels "
                "WHERE country_id = ? AND mu_name IS NOT NULL ORDER BY mu_name"
            )
            params: tuple = (country_id,)
        else:
            sql = "SELECT DISTINCT mu_name FROM citizen_levels WHERE mu_name IS NOT NULL ORDER BY mu_name"
            params = ()
        names: list[str] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                if row[0]:
                    names.append(row[0])
        return names

    async def get_all_mu_readiness(
        self, country_id: Optional[str] = None
    ) -> dict[str, dict]:
        """Readiness stats for every distinct MU, keyed by mu_name.

        Each value: war, total, can_reset, waiting_days, war_15, war_20
        """
        now = datetime.now(timezone.utc)
        if country_id:
            sql = (
                "SELECT mu_name, skill_mode, last_skills_reset_at, level "
                "FROM citizen_levels WHERE country_id = ? AND mu_name IS NOT NULL"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT mu_name, skill_mode, last_skills_reset_at, level "
                "FROM citizen_levels WHERE mu_name IS NOT NULL"
            )
            params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu_name, mode, reset_at, level = row
                level_int = int(level) if level is not None else 0
                if mu_name not in mus:
                    mus[mu_name] = {
                        "war": 0, "total": 0, "can_reset": 0,
                        "waiting_days": [], "war_15": 0, "war_20": 0,
                    }
                m = mus[mu_name]
                m["total"] += 1
                if mode == "war":
                    m["war"] += 1
                    if level_int >= 15:
                        m["war_15"] += 1
                    if level_int >= 20:
                        m["war_20"] += 1
                else:
                    can_reset = True
                    if reset_at:
                        try:
                            ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                            days_ago = (now - ts).total_seconds() / 86400
                            can_reset = days_ago >= 7
                            if not can_reset:
                                m["waiting_days"].append(days_ago)
                        except Exception:
                            pass
                    if can_reset:
                        m["can_reset"] += 1
        return mus

    async def fuzzy_citizen_by_name(
        self,
        query: str,
        country_id: Optional[str] = None,
        cutoff: float = 0.55,
    ) -> Optional[tuple[str, str]]:
        """Return (user_id, citizen_name) for the closest fuzzy match to *query*.

        Queries citizen_levels for all known names and picks the best match using
        difflib SequenceMatcher.  Returns None when nothing exceeds *cutoff*.
        """
        if country_id:
            sql = (
                "SELECT user_id, citizen_name FROM citizen_levels "
                "WHERE country_id = ? AND citizen_name IS NOT NULL"
            )
            params: tuple = (country_id,)
        else:
            sql = "SELECT user_id, citizen_name FROM citizen_levels WHERE citizen_name IS NOT NULL"
            params = ()
        rows: list[tuple[str, str]] = []
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        if not rows:
            return None
        q_low = query.lower().strip()
        best: Optional[tuple[str, str]] = None
        best_ratio = cutoff
        for uid, name in rows:
            ratio = difflib.SequenceMatcher(None, q_low, name.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = (uid, name)
        return best
