"""Data freshness tracking for tasks and manual refreshes.

The ``data_freshness`` table stores one row per named dataset (for example
``"citizens"``, ``"luck"``, ``"battle_drops"``).  Every time a task or a
manual refresh runs, it should call :meth:`mark_started` before doing work
and :meth:`mark_finished` (with status ``"ok"`` or ``"error"``) afterwards.

The website reads these rows to display "Last updated X minutes ago" and
to drive the refresh queue UI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("services.db.freshness")


class FreshnessMixin:
    """Mixin: read/write the ``data_freshness`` table."""

    async def mark_started(self, dataset: str, source: str = "task") -> None:
        """Mark *dataset* as currently running."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT INTO data_freshness (dataset, last_started_at, last_status, source) "
            "VALUES (?, ?, 'running', ?) "
            "ON CONFLICT(dataset) DO UPDATE SET "
            "  last_started_at = excluded.last_started_at, "
            "  last_status     = 'running', "
            "  source          = excluded.source",
            (dataset, now, source),
        )
        await self._conn.commit()

    async def mark_finished(
        self,
        dataset: str,
        status: str = "ok",
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        """Mark *dataset* as finished with the given *status* (``ok`` | ``error``)."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "UPDATE data_freshness SET "
            "  last_finished_at = ?, "
            "  last_status      = ?, "
            "  last_error       = ?, "
            "  duration_ms      = ? "
            "WHERE dataset = ?",
            (now, status, error, duration_ms, dataset),
        )
        await self._conn.commit()

    async def get_freshness(self, dataset: str) -> Optional[dict]:
        """Return a dict with freshness info for *dataset*, or ``None``."""
        async with self._conn.execute(
            "SELECT dataset, last_started_at, last_finished_at, last_status, "
            "       last_error, source, duration_ms "
            "FROM data_freshness WHERE dataset = ?",
            (dataset,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "dataset": row[0],
            "last_started_at": row[1],
            "last_finished_at": row[2],
            "last_status": row[3],
            "last_error": row[4],
            "source": row[5],
            "duration_ms": row[6],
        }

    async def get_all_freshness(self) -> list[dict]:
        """Return freshness rows for every dataset."""
        async with self._conn.execute(
            "SELECT dataset, last_started_at, last_finished_at, last_status, "
            "       last_error, source, duration_ms "
            "FROM data_freshness "
            "ORDER BY dataset"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "dataset": r[0],
                "last_started_at": r[1],
                "last_finished_at": r[2],
                "last_status": r[3],
                "last_error": r[4],
                "source": r[5],
                "duration_ms": r[6],
            }
            for r in rows
        ]
