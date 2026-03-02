"""poll_state and jobs DB methods."""
from __future__ import annotations

from typing import Optional

import aiosqlite


class StateMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase
    """Key/value poll state and background job tracking."""

    # _conn is provided by DatabaseBase

    async def get_poll_state(self, key: str) -> Optional[str]:
        """Get the value for a specific poll state key, or None if not set."""
        async with self._conn.execute(
            "SELECT value FROM poll_state WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def set_poll_state(self, key: str, value: str) -> None:
        """Set the value for a specific poll state key."""
        await self._conn.execute(
            "INSERT INTO poll_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self._conn.commit()

    async def create_job(self, job_id: str) -> None:
        """Create a new background job with the given ID and initial progress."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO jobs(id, status, progress) VALUES(?, ?, ?)",
            (job_id, "pending", 0),
        )
        await self._conn.commit()

    async def update_job_progress(
        self, job_id: str, progress: int, status: Optional[str] = None
    ) -> None:
        """Update the progress and optionally the status of a background job."""
        if status:
            await self._conn.execute(
                "UPDATE jobs SET progress = ?, status = ? WHERE id = ?",
                (progress, status, job_id),
            )
        else:
            await self._conn.execute(
                "UPDATE jobs SET progress = ? WHERE id = ?", (progress, job_id)
            )
        await self._conn.commit()
