"""extension_sessions table DB methods (browser-extension Discord OAuth + refresh tokens)."""

from __future__ import annotations

from typing import Optional, TypedDict

import aiosqlite


class ExtensionSessionRow(TypedDict):
    id: str
    user_id: str
    username: str
    refresh_token_hash: str
    prev_token_hash: Optional[str]
    prev_token_expires_at: Optional[str]
    created_at: str
    last_used_at: str
    expires_at: str
    revoked: int
    user_agent: Optional[str]


class ExtensionAuthMixin:
    """extension_sessions table operations."""

    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def create_extension_session(
        self,
        session_id: str,
        user_id: str,
        username: str,
        refresh_token_hash: str,
        created_at: str,
        expires_at: str,
        user_agent: str | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO extension_sessions(
                id, user_id, username, refresh_token_hash,
                created_at, last_used_at, expires_at, revoked, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (session_id, user_id, username, refresh_token_hash, created_at, created_at, expires_at, user_agent),
        )
        await self._conn.commit()

    async def get_extension_session(self, session_id: str) -> Optional[ExtensionSessionRow]:
        async with self._conn.execute(
            """
            SELECT id, user_id, username, refresh_token_hash, prev_token_hash,
                   prev_token_expires_at, created_at, last_used_at, expires_at,
                   revoked, user_agent
            FROM extension_sessions WHERE id = ?
            """,
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return ExtensionSessionRow(
            id=row[0], user_id=row[1], username=row[2], refresh_token_hash=row[3],
            prev_token_hash=row[4], prev_token_expires_at=row[5], created_at=row[6],
            last_used_at=row[7], expires_at=row[8], revoked=row[9], user_agent=row[10],
        )

    async def rotate_extension_session(
        self,
        session_id: str,
        new_refresh_token_hash: str,
        old_refresh_token_hash: str,
        now: str,
        new_expires_at: str,
        prev_grace_expires_at: str,
    ) -> bool:
        """Rotate a session's refresh token, keeping the old hash for reuse detection.

        Only succeeds (returns True) if `old_refresh_token_hash` still matches the
        row's current hash — this makes rotation a compare-and-swap, so two
        concurrent refreshes with the same (shared) token can't both "win".
        """
        cur = await self._conn.execute(
            """
            UPDATE extension_sessions
            SET refresh_token_hash = ?,
                prev_token_hash = refresh_token_hash,
                prev_token_expires_at = ?,
                last_used_at = ?,
                expires_at = ?
            WHERE id = ? AND refresh_token_hash = ? AND revoked = 0
            """,
            (new_refresh_token_hash, prev_grace_expires_at, now, new_expires_at, session_id, old_refresh_token_hash),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def revoke_extension_session(self, session_id: str) -> None:
        await self._conn.execute(
            "UPDATE extension_sessions SET revoked = 1 WHERE id = ?", (session_id,)
        )
        await self._conn.commit()

    async def revoke_extension_sessions_for_user(self, user_id: str) -> None:
        """Revoke every session belonging to a user — call this the moment you
        remove someone from the extension whitelist, so access is cut immediately
        instead of waiting for their refresh token to naturally expire."""
        await self._conn.execute(
            "UPDATE extension_sessions SET revoked = 1 WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def list_extension_sessions(self) -> list[ExtensionSessionRow]:
        """All non-revoked sessions, for admin visibility."""
        rows: list[ExtensionSessionRow] = []
        async with self._conn.execute(
            """
            SELECT id, user_id, username, refresh_token_hash, prev_token_hash,
                   prev_token_expires_at, created_at, last_used_at, expires_at,
                   revoked, user_agent
            FROM extension_sessions WHERE revoked = 0 ORDER BY last_used_at DESC
            """
        ) as cur:
            async for row in cur:
                rows.append(ExtensionSessionRow(
                    id=row[0], user_id=row[1], username=row[2], refresh_token_hash=row[3],
                    prev_token_hash=row[4], prev_token_expires_at=row[5], created_at=row[6],
                    last_used_at=row[7], expires_at=row[8], revoked=row[9], user_agent=row[10],
                ))
        return rows
