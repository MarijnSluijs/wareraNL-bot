import aiosqlite
import logging
from typing import Optional

logger = logging.getLogger("services.db")


class Database:
    """Simple async wrapper around a SQLite file for storing poll state and job metadata.

    This is intentionally small: add migrations/ORM or switch to Postgres/asyncpg later
    if you need more scale.
    """

    def __init__(self, path: str = "database/external.db") -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def setup(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS poll_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT,
                progress INTEGER,
                result_path TEXT
            )
            """
        )
        # store latest snapshot per country
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS country_snapshots (
                country_id TEXT PRIMARY KEY,
                code TEXT,
                name TEXT,
                specialized_item TEXT,
                production_bonus REAL,
                raw_json TEXT,
                updated_at TEXT
            )
            """
        )

        # store current top per specialization item (permanent bonus, no deposit)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS specialization_top (
                item TEXT PRIMARY KEY,
                country_id TEXT,
                country_name TEXT,
                production_bonus REAL,
                strategic_bonus REAL,
                ethic_bonus REAL,
                ethic_deposit_bonus REAL,
                updated_at TEXT
            )
            """
        )
        # migrations: add breakdown columns if missing
        for col in ("strategic_bonus REAL", "ethic_bonus REAL", "ethic_deposit_bonus REAL"):
            try:
                await self._conn.execute(f"ALTER TABLE specialization_top ADD COLUMN {col}")
                await self._conn.commit()
            except Exception:
                pass  # column already exists

        # store current deposit top per specialization item
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deposit_top (
                item TEXT PRIMARY KEY,
                region_id TEXT,
                region_name TEXT,
                country_id TEXT,
                country_name TEXT,
                bonus INTEGER,
                deposit_bonus REAL,
                ethic_deposit_bonus REAL,
                permanent_bonus REAL,
                deposit_end_at TEXT,
                updated_at TEXT
            )
            """
        )
        # migrations: add breakdown columns if missing
        for col in ("region_name TEXT", "deposit_bonus REAL", "ethic_deposit_bonus REAL"):
            try:
                await self._conn.execute(f"ALTER TABLE deposit_top ADD COLUMN {col}")
                await self._conn.commit()
            except Exception:
                pass  # column already exists        # citizen level cache (populated by the daily background task)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS citizen_levels (
                user_id TEXT PRIMARY KEY,
                country_id TEXT NOT NULL,
                level INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_citizen_levels_country ON citizen_levels(country_id)"
        )
        # migration: add skill_mode column if missing
        try:
            await self._conn.execute("ALTER TABLE citizen_levels ADD COLUMN skill_mode TEXT")
            await self._conn.commit()
        except Exception:
            pass  # column already exists
        # migration: add last_skills_reset_at column if missing
        try:
            await self._conn.execute("ALTER TABLE citizen_levels ADD COLUMN last_skills_reset_at TEXT")
            await self._conn.commit()
        except Exception:
            pass  # column already exists
        # migration: add citizen_name column if missing
        try:
            await self._conn.execute("ALTER TABLE citizen_levels ADD COLUMN citizen_name TEXT")
            await self._conn.commit()
        except Exception:
            pass  # column already exists
        # migration: add last_login_at column if missing
        try:
            await self._conn.execute("ALTER TABLE citizen_levels ADD COLUMN last_login_at TEXT")
            await self._conn.commit()
        except Exception:
            pass  # column already exists
        # migration: add mu_id column if missing
        try:
            await self._conn.execute("ALTER TABLE citizen_levels ADD COLUMN mu_id TEXT")
            await self._conn.commit()
        except Exception:
            pass  # column already exists
        # migration: add mu_name column if missing
        try:
            await self._conn.execute("ALTER TABLE citizen_levels ADD COLUMN mu_name TEXT")
            await self._conn.commit()
        except Exception:
            pass  # column already exists
        # track which articles have already been posted to Discord
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_articles (
                article_id TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL
            )
            """
        )
        # track which game events have already been posted to Discord
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_events (
                event_id TEXT PRIMARY KEY,
                seen_at TEXT NOT NULL
            )
            """
        )
        # store war/battle events for historical reference
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS war_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                battle_id TEXT,
                war_id TEXT,
                attacker_country_id TEXT,
                defender_country_id TEXT,
                region_id TEXT,
                region_name TEXT,
                attacker_name TEXT,
                defender_name TEXT,
                created_at TEXT,
                raw_json TEXT
            )
            """
        )
        # citizen luck score cache (populated by daily_luck_refresh task)
        await self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS citizen_luck (
                user_id TEXT PRIMARY KEY,
                country_id TEXT NOT NULL,
                citizen_name TEXT,
                luck_score REAL NOT NULL,
                opens_count INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_citizen_luck_country ON citizen_luck(country_id)"
        )
        await self._conn.commit()
        logger.info("Database initialized at %s", self.path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def get_poll_state(self, key: str) -> Optional[str]:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        async with self._conn.execute("SELECT value FROM poll_state WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_poll_state(self, key: str, value: str) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "INSERT INTO poll_state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._conn.commit()

    async def create_job(self, job_id: str) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute("INSERT OR REPLACE INTO jobs(id, status, progress) VALUES(?, ?, ?)", (job_id, "pending", 0))
        await self._conn.commit()

    async def update_job_progress(self, job_id: str, progress: int, status: Optional[str] = None) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        if status:
            await self._conn.execute("UPDATE jobs SET progress = ?, status = ? WHERE id = ?", (progress, status, job_id))
        else:
            await self._conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (progress, job_id))
        await self._conn.commit()

    async def save_country_snapshot(self, country_id: str, code: str | None, name: str | None, specialized_item: str | None, production_bonus: float | None, raw_json: str, updated_at: str) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "INSERT OR REPLACE INTO country_snapshots(country_id, code, name, specialized_item, production_bonus, raw_json, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (country_id, code, name, specialized_item, production_bonus, raw_json, updated_at),
        )
        await self._conn.commit()

    async def get_top_specialization(self, item: str) -> Optional[dict]:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        async with self._conn.execute("SELECT country_id, country_name, production_bonus, strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at FROM specialization_top WHERE item = ?", (item,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {"country_id": row[0], "country_name": row[1], "production_bonus": row[2], "strategic_bonus": row[3], "ethic_bonus": row[4], "ethic_deposit_bonus": row[5], "updated_at": row[6]}

    async def get_all_tops(self) -> list:
        """Return all specialization tops as a list of dicts."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        rows = []
        async with self._conn.execute("SELECT item, country_id, country_name, production_bonus, strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at FROM specialization_top") as cur:
            async for row in cur:
                rows.append({"item": row[0], "country_id": row[1], "country_name": row[2], "production_bonus": row[3], "strategic_bonus": row[4], "ethic_bonus": row[5], "ethic_deposit_bonus": row[6], "updated_at": row[7]})
        return rows

    async def delete_top_specialization(self, item: str) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute("DELETE FROM specialization_top WHERE item = ?", (item,))
        await self._conn.commit()

    async def set_top_specialization(self, item: str, country_id: str, country_name: str, production_bonus: float, updated_at: str, strategic_bonus: float | None = None, ethic_bonus: float | None = None, ethic_deposit_bonus: float | None = None) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "INSERT OR REPLACE INTO specialization_top(item, country_id, country_name, production_bonus, strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (item, country_id, country_name, production_bonus, strategic_bonus, ethic_bonus, ethic_deposit_bonus, updated_at),
        )
        await self._conn.commit()

    async def get_deposit_top(self, item: str) -> dict | None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        async with self._conn.execute(
            "SELECT region_id, region_name, country_id, country_name, bonus, deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at FROM deposit_top WHERE item = ?",
            (item,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "region_id": row[0], "region_name": row[1], "country_id": row[2],
                "country_name": row[3], "bonus": row[4], "deposit_bonus": row[5],
                "ethic_deposit_bonus": row[6], "permanent_bonus": row[7],
                "deposit_end_at": row[8], "updated_at": row[9],
            }

    async def get_all_deposit_tops(self) -> list[dict]:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        rows = []
        async with self._conn.execute(
            "SELECT item, region_id, region_name, country_id, country_name, bonus, deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at FROM deposit_top"
        ) as cur:
            async for row in cur:
                rows.append({
                    "item": row[0], "region_id": row[1], "region_name": row[2],
                    "country_id": row[3], "country_name": row[4], "bonus": row[5],
                    "deposit_bonus": row[6], "ethic_deposit_bonus": row[7],
                    "permanent_bonus": row[8], "deposit_end_at": row[9], "updated_at": row[10],
                })
        return rows

    async def set_deposit_top(
        self, item: str, region_id: str, region_name: str, country_id: str, country_name: str,
        bonus: int, deposit_bonus: float, ethic_deposit_bonus: float,
        permanent_bonus: float, deposit_end_at: str, updated_at: str,
    ) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "INSERT OR REPLACE INTO deposit_top(item, region_id, region_name, country_id, country_name, bonus, deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item, region_id, region_name, country_id, country_name, bonus, deposit_bonus, ethic_deposit_bonus, permanent_bonus, deposit_end_at, updated_at),
        )
        await self._conn.commit()

    async def upsert_citizen_level(self, user_id: str, country_id: str, level: int, updated_at: str, skill_mode: str | None = None, last_skills_reset_at: str | None = None, citizen_name: str | None = None, last_login_at: str | None = None, mu_id: str | None = None, mu_name: str | None = None) -> None:
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "INSERT OR REPLACE INTO citizen_levels(user_id, country_id, level, skill_mode, last_skills_reset_at, citizen_name, last_login_at, mu_id, mu_name, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, country_id, level, skill_mode, last_skills_reset_at, citizen_name, last_login_at, mu_id, mu_name, updated_at),
        )

    async def update_citizen_mu(self, user_id: str, mu_id: str | None, mu_name: str | None) -> None:
        """Update only the mu_id and mu_name fields for an existing citizen row."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "UPDATE citizen_levels SET mu_id = ?, mu_name = ? WHERE user_id = ?",
            (mu_id, mu_name, user_id),
        )

    async def clear_citizen_mus_for_country(self, country_id: str) -> None:
        """Reset mu_id and mu_name to NULL for all citizens of a country (before re-assigning)."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "UPDATE citizen_levels SET mu_id = NULL, mu_name = NULL WHERE country_id = ?",
            (country_id,),
        )
        await self._conn.commit()

    async def flush_citizen_levels(self) -> None:
        """Commit any pending citizen level upserts."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.commit()

    async def delete_citizens_for_country(self, country_id: str) -> None:
        """Remove stale citizen rows for a country before a fresh refresh."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute("DELETE FROM citizen_levels WHERE country_id = ?", (country_id,))
        await self._conn.commit()

    async def get_level_distribution(
        self, country_id: str | None
    ) -> tuple[dict[int, int], dict[int, int], str | None]:
        """Return (level_counts, active_counts, last_updated_at).

        active_counts counts only citizens whose last_login_at is within the
        last 24 hours.  If last_login_at data is unavailable the dict is empty.
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timedelta, timezone
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        counts: dict[int, int] = {}
        active: dict[int, int] = {}
        last_updated: str | None = None
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

    async def get_skill_mode_distribution(self, country_id: str | None) -> tuple[int, int, int, str | None]:
        """Return (eco_count, war_count, unknown_count, last_updated) for a country or all countries."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        eco = war = unknown = 0
        last_updated: str | None = None
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
        self, country_id: str | None
    ) -> tuple[dict[int, dict[str, int]], str | None]:
        """Return eco/war/unknown counts grouped by 5-level bucket and last_updated.

        Returns a dict keyed by bucket_start (1, 6, 11, …) where each value is
        {"eco": n, "war": n, "unknown": n}, plus the most-recent updated_at string.
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        if country_id:
            sql = "SELECT level, skill_mode, updated_at FROM citizen_levels WHERE country_id = ?"
            params: tuple = (country_id,)
        else:
            sql = "SELECT level, skill_mode, updated_at FROM citizen_levels"
            params = ()
        buckets: dict[int, dict[str, int]] = {}
        last_updated: str | None = None
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
        self, country_id: str | None
    ) -> dict[str, dict]:
        """Return eco/war/unknown counts + per-player rows grouped by MU name.

        Returns a dict keyed by mu_name (None key → players without an MU).
        Each entry: {"eco": n, "war": n, "unknown": n,
                     "players": [{"citizen_name", "level", "skill_mode"}, …]}
        Players within each MU are sorted by level DESC.
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        if country_id:
            sql = (
                "SELECT mu_name, citizen_name, level, skill_mode "
                "FROM citizen_levels WHERE country_id = ? "
                "ORDER BY mu_name, level DESC"
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
        self, country_id: str | None
    ) -> dict[str, dict]:
        """Return skill-reset cooldown stats + per-player rows grouped by MU name.

        Returns a dict keyed by mu_name ("" → players without an MU).
        Each entry: {"count": n_with_reset_data, "sum_days": float,
                     "available": n_can_reset, "no_data": n,
                     "players": [{"citizen_name", "level", "days_ago", "can_reset"}, …]}
        Players within each MU are sorted by level DESC.
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if country_id:
            sql = (
                "SELECT mu_name, citizen_name, level, last_skills_reset_at "
                "FROM citizen_levels WHERE country_id = ? "
                "ORDER BY mu_name, level DESC"
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
                days_ago: float | None = None
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
        self, country_id: str | None
    ) -> tuple[dict[int, dict], str | None]:
        """Return skill-reset cooldown stats grouped by 5-level bucket.

        For each bucket returns:
          {"count": total_with_reset_data, "avg_days_ago": float,
           "available": citizens_who_can_reset_now, "no_data": citizens_without_reset_ts}
        plus the most-recent updated_at string.
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        # Only count eco-mode (or unknown) players — war-mode players can already fight
        # and don't need to reset, so their cooldown is irrelevant for readiness purposes.
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
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        buckets: dict[int, dict] = {}
        last_updated: str | None = None
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
                        b["available"] += 1  # parse failed → assume can reset
                else:
                    b["no_data"] += 1
                    b["available"] += 1  # never reset → can reset
                if last_updated is None or (upd and upd > last_updated):
                    last_updated = upd
        # convert sum_days -> avg_days_ago
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
        """Return citizens for a country sorted by level DESC with cooldown data.

        Each dict: user_id, citizen_name, level, last_skills_reset_at, days_ago, can_reset
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
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
                days_ago: float | None = None
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
        """Search for a citizen by name (partial, case-insensitive) or exact user_id.

        Returns up to 10 matches ordered by level DESC.
        Each dict: user_id, citizen_name, level, country_id, last_skills_reset_at, days_ago, can_reset
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
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
                days_ago: float | None = None
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
        """Search for a citizen by name (partial, case-insensitive) or exact user_id.

        Returns up to 10 matches ordered by level DESC.
        Each dict: user_id, citizen_name, level, country_id, skill_mode,
                   last_skills_reset_at, days_ago, can_reset
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
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
                days_ago: float | None = None
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
        self, mu_query: str, country_id: str | None = None
    ) -> tuple[str | None, list[dict]]:
        """Return (matched_mu_name, players) for the best-matching MU name.

        mu_query is matched case-insensitively; exact match is preferred over partial.
        Each player dict: citizen_name, level, skill_mode, days_ago, can_reset
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        # Find all matching MU names
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
        # Prefer exact match over partial
        exact = next((m for m in mu_names if m.lower() == mu_query.lower()), None)
        mu_name = exact or mu_names[0]
        # Fetch all players in that MU
        if country_id:
            sql = (
                "SELECT citizen_name, level, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE mu_name = ? AND country_id = ? "
                "ORDER BY level DESC"
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
                days_ago: float | None = None
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

    async def get_distinct_mu_names(self, country_id: str | None = None) -> list[str]:
        """Return all distinct non-null MU names, optionally filtered by country."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
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


    async def get_all_mu_readiness(self, country_id: str | None = None) -> dict[str, dict]:
        """Return readiness stats for every distinct MU, keyed by mu_name.

        Each value dict:
          war          – players in war mode
          total        – all players in that MU
          can_reset    – eco players who can reset right now
          waiting_days – list of days_ago values for eco players still in cooldown
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if country_id:
            sql = (
                "SELECT mu_name, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE country_id = ? AND mu_name IS NOT NULL"
            )
            params: tuple = (country_id,)
        else:
            sql = (
                "SELECT mu_name, skill_mode, last_skills_reset_at "
                "FROM citizen_levels WHERE mu_name IS NOT NULL"
            )
            params = ()
        mus: dict[str, dict] = {}
        async with self._conn.execute(sql, params) as cur:
            async for row in cur:
                mu_name, mode, reset_at = row
                if mu_name not in mus:
                    mus[mu_name] = {"war": 0, "total": 0, "can_reset": 0, "waiting_days": []}
                m = mus[mu_name]
                m["total"] += 1
                if mode == "war":
                    m["war"] += 1
                else:
                    # eco / unknown — compute cooldown
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

    async def has_seen_article(self, article_id: str) -> bool:
        """Return True if this article has already been posted to Discord."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        async with self._conn.execute(
            "SELECT 1 FROM seen_articles WHERE article_id = ?", (article_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_article_seen(self, article_id: str) -> None:
        """Record that this article has been posted so we don't post it again."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR IGNORE INTO seen_articles(article_id, seen_at) VALUES(?, ?)",
            (article_id, now),
        )
        await self._conn.commit()

    async def has_seen_event(self, event_id: str) -> bool:
        """Return True if this event has already been posted to Discord."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        async with self._conn.execute(
            "SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_event_seen(self, event_id: str) -> None:
        """Record that this event has been posted so we don't post it again."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        from datetime import datetime, timezone
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
        """Store a war/battle event for historical reference."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
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

    # ------------------------------------------------------------------ #
    # Citizen luck ranking                                                 #
    # ------------------------------------------------------------------ #

    async def upsert_luck_score(
        self,
        user_id: str,
        country_id: str,
        citizen_name: str | None,
        luck_score: float,
        opens_count: int,
        updated_at: str,
    ) -> None:
        """Insert or replace a citizen's luck score (batch — call flush_luck_scores after)."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO citizen_luck
                (user_id, country_id, citizen_name, luck_score, opens_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, country_id, citizen_name, luck_score, opens_count, updated_at),
        )

    async def flush_luck_scores(self) -> None:
        """Commit any pending luck score upserts."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.commit()

    async def delete_luck_scores_for_country(self, country_id: str) -> None:
        """Remove all luck scores for a country before a fresh rebuild."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        await self._conn.execute(
            "DELETE FROM citizen_luck WHERE country_id = ?", (country_id,)
        )
        await self._conn.commit()

    async def get_luck_ranking(
        self, country_id: str
    ) -> list[dict]:
        """Return all luck entries for a country sorted by luck_score DESC.

        Each dict: user_id, citizen_name, luck_score, opens_count, updated_at.
        """
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        rows: list[dict] = []
        async with self._conn.execute(
            """
            SELECT user_id, citizen_name, luck_score, opens_count, updated_at
            FROM citizen_luck
            WHERE country_id = ?
            ORDER BY luck_score DESC
            """,
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append({
                    "user_id": row[0],
                    "citizen_name": row[1] or row[0],
                    "luck_score": row[2],
                    "opens_count": row[3],
                    "updated_at": row[4],
                })
        return rows

    async def get_citizens_for_luck_refresh(
        self, country_id: str
    ) -> list[tuple[str, str | None]]:
        """Return (user_id, citizen_name) for all cached citizens of a country."""
        if not self._conn:
            raise RuntimeError("Database not initialized; call setup() first")
        rows: list[tuple[str, str | None]] = []
        async with self._conn.execute(
            "SELECT user_id, citizen_name FROM citizen_levels WHERE country_id = ?",
            (country_id,),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return rows


__all__ = ["Database"]
