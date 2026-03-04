"""Giveaway-related DB methods."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import aiosqlite

class GiveawaysMixin:
    _conn: aiosqlite.Connection  # provided by DatabaseBase

    async def store_reward(self, user_id: str, reward_amount: int) -> None:
        """Store a giveaway reward for a user."""
        now = datetime.now(timezone.utc).isoformat()

        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 1) Append-only ledger entry (audit trail)
            await self._conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, amount, tx_type, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (user_id, reward_amount, "giveaway_reward", now),
            )

            # 2) Upsert wallet and increment balance
            await self._conn.execute(
                """
                INSERT INTO wallets (user_id, balance, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    balance = wallets.balance + excluded.balance,
                    updated_at = excluded.updated_at
                """,
                (user_id, reward_amount, now),
            )

            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def get_balance(self, user_id: str) -> int:
        """Get the current giveaway balance for a user."""
        async with self._conn.execute(
            "SELECT balance FROM wallets WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
        
    async def get_transaction_history(
        self, user_id: str, limit: int = 10, offset: int = 0
    ) -> list[dict]:
        """Get a paginated list of giveaway transactions for a user."""
        async with self._conn.execute(
            """
            SELECT amount, tx_type, created_at
            FROM wallet_transactions
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [
                {"amount": row[0], "tx_type": row[1], "created_at": row[2]}
                for row in rows
            ]
        
    async def remove_balance(self, user_id: str, amount: int) -> None:
        """Remove a specified amount from a user's giveaway balance."""
        now = datetime.now(timezone.utc).isoformat()

        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 1) Append-only ledger entry (audit trail)
            await self._conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, amount, tx_type, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (user_id, -amount, "giveaway_deduction", now),
            )

            # 2) Decrement wallet balance
            await self._conn.execute(
                """
                UPDATE wallets
                SET balance = balance - ?, updated_at = ?
                WHERE user_id = ?
                """,
                (amount, now, user_id),
            )

            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def get_leaderboard(self, limit: int = 10) -> list[dict]:
        """Get a leaderboard of users with the highest giveaway balances."""
        async with self._conn.execute(
            """
            SELECT user_id, balance
            FROM wallets
            WHERE balance > 0
            ORDER BY balance DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"user_id": row[0], "balance": row[1]} for row in rows]