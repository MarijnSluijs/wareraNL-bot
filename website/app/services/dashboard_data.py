from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

_TS_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[(?P<lvl>[^\]]+)\]\s+(?P<logger>[^:]+):\s(?P<msg>.*)$")


class PanelDataService:
    def __init__(self, *, db_path: str, log_path: str, config_path: str, audit_log_path: str) -> None:
        self.db_path = Path(db_path)
        self.log_path = Path(log_path)
        self.config_path = Path(config_path)
        self.audit_log_path = Path(audit_log_path)

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path.as_posix())
        conn.row_factory = aiosqlite.Row
        return conn

    @staticmethod
    def _safe_dt(ts: str | None) -> datetime | None:
        if not ts:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                continue
        return None

    @staticmethod
    def _filter_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        cleaned = value.strip()
        for fmt in (
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%d-%m-%Y %H:%M:%S",
        ):
            try:
                return datetime.strptime(cleaned, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def _tail_lines(self, max_lines: int = 5000, chunk_size: int = 8192) -> list[str]:
        if not self.log_path.exists():
            return []
        size = self.log_path.stat().st_size
        if size == 0:
            return []

        with self.log_path.open("rb") as handle:
            remaining = size
            data = b""
            lines = 0
            while remaining > 0 and lines <= max_lines:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                handle.seek(remaining)
                chunk = handle.read(read_size)
                data = chunk + data
                lines = data.count(b"\n")
            text = data.decode("utf-8", errors="replace")
        all_lines = [line for line in text.splitlines() if line.strip()]
        return all_lines[-max_lines:]

    def _parse_logs(self, max_lines: int = 4000) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in self._tail_lines(max_lines=max_lines):
            match = _TS_RE.match(line)
            if not match:
                continue
            groups = match.groupdict()
            out.append(
                {
                    "raw": line,
                    "timestamp": groups["ts"],
                    "level": groups["lvl"].strip().upper(),
                    "logger": groups["logger"].strip(),
                    "message": groups["msg"].strip(),
                }
            )
        return out

    async def dashboard_overview(self, days: int = 7) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(days=days)
        status = "Offline"
        kpis: dict[str, dict[str, Any]] = {
            "bot_status": {"label": "Bot status", "value": "Offline", "delta": None},
            "uptime": {"label": "Uptime", "value": "N/A", "delta": None},
            "commands": {"label": "Commands uitgevoerd", "value": 0, "delta": None},
            "active_users": {"label": "Actieve users (24u)", "value": 0, "delta": None},
            "open_cases": {"label": "Open moderatiecases", "value": 0, "delta": None},
            "task_failures": {"label": "Task failures (24u)", "value": 0, "delta": None},
        }

        conn = await self._connect()
        try:
            row = await (
                await conn.execute(
                    """
                    SELECT MAX(updated_at) AS updated_at
                    FROM (
                             SELECT updated_at FROM citizen_levels
                             UNION ALL
                             SELECT updated_at FROM citizen_wealth
                             UNION ALL
                             SELECT updated_at FROM country_snapshots
                         )
                    """
                )
            ).fetchone()
            last_update = self._safe_dt(row["updated_at"] if row else None)
            if last_update:
                age_min = (now - last_update).total_seconds() / 60
                if age_min <= 30:
                    status = "Online"
                elif age_min <= 360:
                    status = "Degraded"

            poll = await (
                await conn.execute("SELECT value FROM poll_state WHERE key = ?", ("bot_started_at",))
            ).fetchone()
            boot_ts = self._safe_dt(poll["value"] if poll else None)
            if boot_ts:
                uptime = now - boot_ts
                hours, rem = divmod(int(uptime.total_seconds()), 3600)
                minutes = rem // 60
                kpis["uptime"]["value"] = f"{hours}h {minutes}m"

            row = await (
                await conn.execute(
                    "SELECT COUNT(*) AS total FROM citizen_levels WHERE last_login_at >= ?",
                    (threshold.isoformat().replace("+00:00", "Z"),),
                )
            ).fetchone()
            kpis["active_users"]["value"] = int(row["total"] if row else 0)

            row = await (await conn.execute("SELECT COUNT(*) AS total FROM warns")).fetchone()
            kpis["open_cases"]["value"] = int(row["total"] if row else 0)
        finally:
            await conn.close()

        logs = self._parse_logs(max_lines=12000)
        last_24h = now - timedelta(hours=24)
        today = now.date()
        yesterday = today - timedelta(days=1)

        commands_today = 0
        commands_yesterday = 0
        failures_24h = 0
        incidents: list[dict[str, Any]] = []
        dependency_status: Counter[str] = Counter()

        for entry in reversed(logs):
            ts = self._safe_dt(entry["timestamp"])
            if ts is None:
                continue
            msg = entry["message"]
            lvl = entry["level"]

            if " /" in msg or "command" in msg.lower():
                if ts.date() == today:
                    commands_today += 1
                elif ts.date() == yesterday:
                    commands_yesterday += 1

            if ts >= last_24h and lvl in {"ERROR", "CRITICAL"}:
                failures_24h += 1

            lowered = msg.lower()
            if "rate limited" in lowered:
                dependency_status["rate_limited"] += 1
            elif "session closed" in lowered:
                dependency_status["closed"] += 1
            elif lvl == "ERROR":
                dependency_status["error"] += 1
            else:
                dependency_status["ok"] += 1

            if lvl in {"ERROR", "CRITICAL", "WARNING"}:
                incidents.append(
                    {
                        "timestamp": entry["timestamp"],
                        "level": lvl,
                        "message": msg,
                    }
                )

        kpis["bot_status"]["value"] = status
        kpis["commands"]["value"] = commands_today
        kpis["commands"]["delta"] = commands_today - commands_yesterday
        kpis["task_failures"]["value"] = failures_24h

        return {
            "kpis": kpis,
            "incidents": incidents[-12:][::-1],
            "dependency_status": {
                "ok": dependency_status.get("ok", 0),
                "rate_limited": dependency_status.get("rate_limited", 0),
                "error": dependency_status.get("error", 0),
                "closed": dependency_status.get("closed", 0),
            },
        }

    async def task_health(self) -> list[dict[str, Any]]:
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT key, value
                    FROM poll_state
                    WHERE key LIKE 'last_%' OR key LIKE '%_last_run%' OR key LIKE '%task%'
                    ORDER BY key
                        LIMIT 200
                    """
                )
            ).fetchall()
        finally:
            await conn.close()

        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "task": row["key"],
                    "last_run": row["value"],
                    "status": "ok",
                    "success_ratio": "n/a",
                    "next_run": "n/a",
                }
            )
        return out

    async def users(self, search: str = "", limit: int = 100) -> list[dict[str, Any]]:
        query = """
                SELECT i.discord_user_id,
                       c.user_id AS in_game_user_id,
                       i.nationality,
                       COALESCE(i.updated_at, c.updated_at) AS updated_at,
                       c.citizen_name,
                       c.country_id,
                       c.level,
                       c.skill_mode,
                       c.mu_name,
                       c.last_login_at
                FROM citizen_levels c
                         LEFT JOIN identity_links i ON i.in_game_user_id = c.user_id
                WHERE (
                    ? = ''
                    OR i.discord_user_id LIKE ?
                    OR c.citizen_name LIKE ?
                    OR c.user_id LIKE ?
                    OR c.mu_name LIKE ?
                )
                ORDER BY c.updated_at DESC
                    LIMIT ? \
                """
        like = f"%{search}%"
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(query, (search, like, like, like, like, limit))
            ).fetchall()
        finally:
            await conn.close()
        return [dict(row) for row in rows]

    async def moderation_cases(self, status_filter: str = "all", limit: int = 200) -> list[dict[str, Any]]:
        del status_filter
        conn = await self._connect()
        try:
            rows = await (
                await conn.execute(
                    """
                    SELECT id, user_id, server_id, moderator_id, reason, created_at
                    FROM warns
                    ORDER BY created_at DESC
                        LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        finally:
            await conn.close()
        return [dict(row) for row in rows]

    async def guild_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {"channels": {}, "roles": {}, "flags": {}}
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        return {
            "guild_id": data.get("guild_id", 0),
            "channels": data.get("channels", {}),
            "roles": data.get("roles", {}),
            "flags": {
                "welcome": True,
                "giveaways": True,
                "economy": True,
                "monitor": True,
            },
        }

    async def logs(
        self,
        level: str = "ALL",
        limit: int = 200,
        van: str | None = None,
        tot: str | None = None,
    ) -> list[dict[str, Any]]:
        level = level.upper()
        start_at = self._filter_dt(van)
        end_at = self._filter_dt(tot)
        max_lines = 100000 if start_at or end_at else max(1000, limit * 20)
        records = self._parse_logs(max_lines=max_lines)
        if level != "ALL":
            records = [r for r in records if r["level"] == level]
        if start_at or end_at:
            filtered = []
            for record in records:
                timestamp = self._safe_dt(record["timestamp"])
                if timestamp is None:
                    continue
                if start_at and timestamp < start_at:
                    continue
                if end_at and timestamp > end_at:
                    continue
                filtered.append(record)
            records = filtered
        return records[-limit:][::-1]

    async def audit(self, actor_id: str, action: str, details: dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor_id": actor_id,
            "action": action,
            "details": details,
        }
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")

    async def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.audit_log_path.exists():
            return []
        lines = self.audit_log_path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    async def economy_snapshot(self) -> dict[str, Any]:
        conn = await self._connect()
        try:
            wealth = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(wealth_total), 0) AS total_wealth, COUNT(*) AS rows_count FROM citizen_wealth"
                )
            ).fetchone()
            tx = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS tx_volume, COUNT(*) AS tx_count FROM wallet_transactions"
                )
            ).fetchone()
            battle = await (
                await conn.execute(
                    "SELECT COALESCE(SUM(attacker_damage + defender_damage), 0) AS damage_total FROM processed_battles"
                )
            ).fetchone()
        finally:
            await conn.close()

        return {
            "wealth": float(wealth["total_wealth"] if wealth else 0),
            "wealth_rows": int(wealth["rows_count"] if wealth else 0),
            "transaction_volume": int(tx["tx_volume"] if tx else 0),
            "transaction_count": int(tx["tx_count"] if tx else 0),
            "battle_damage_total": float(battle["damage_total"] if battle else 0),
        }
