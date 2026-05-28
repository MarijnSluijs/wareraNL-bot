"""Background task: hourly eco-donation cache sync.

Polls ``transaction.getPaginatedTransactions`` for NL donations and stores
new rows in the ``eco_donations`` table.  The ``/eco_donaties`` command reads
from that table instead of calling the API live.

First run:  fetches the last *INITIAL_LOOKBACK_DAYS* days (default 90).
Later runs: fetches only transactions newer than the most recent cached row.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_INITIAL_LOOKBACK_DAYS = 90
_POLL_INTERVAL_HOURS = 1


def _unwrap(resp) -> dict:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return {}


def _compute_txn_id(user_id: str, created_at: str, amount: float) -> str:
    """Deterministic SHA-1 fallback when the API provides no transaction ID."""
    raw = f"{user_id}|{created_at}|{amount}"
    return hashlib.sha1(raw.encode()).hexdigest()


class EcoDonationsPoller(TaskCogBase, name="eco_donations_poller"):
    """Hourly sync of NL eco-donation transactions into the local DB."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.poll_donations.start()

    def cog_unload(self) -> None:
        self.poll_donations.cancel()

    # ------------------------------------------------------------------
    # Task loop
    # ------------------------------------------------------------------

    @tasks.loop(hours=_POLL_INTERVAL_HOURS)
    async def poll_donations(self) -> None:
        db = self._db
        client = self._client
        if db is None or client is None:
            logger.warning("[eco_donations_poller] services not ready; skipping tick")
            return

        nl_country_id: str = self.config.get("nl_country_id", "")
        if not nl_country_id:
            logger.warning("[eco_donations_poller] nl_country_id not configured; skipping")
            return

        t0 = time.monotonic()
        try:
            inserted = await self._fetch_and_store(db, client, nl_country_id)
        except Exception as exc:
            logger.exception("[eco_donations_poller] unexpected error: %s", exc)
            return

        elapsed = time.monotonic() - t0
        logger.info("[eco_donations_poller] stored %d new rows in %.2fs", inserted, elapsed)

    @poll_donations.before_loop
    async def _before(self) -> None:
        await self._wait_for_services()

    async def run_once(self) -> int:
        """Fetch and store new donations immediately; returns count inserted.

        Can be called from :func:`/peil eco_donaties` for an on-demand sync.
        """
        db = self._db
        client = self._client
        nl_country_id: str = self.config.get("nl_country_id", "")
        if db is None or client is None or not nl_country_id:
            raise RuntimeError("Services not ready or nl_country_id not configured")
        return await self._fetch_and_store(db, client, nl_country_id)

    # ------------------------------------------------------------------
    # Core fetch logic
    # ------------------------------------------------------------------

    async def _fetch_and_store(self, db, client, nl_country_id: str) -> int:
        """Fetch new transactions from the API and store them; returns count inserted."""

        # Determine the cutoff: only fetch transactions newer than this.
        latest_at_str = await db.get_latest_eco_donation_at()
        if latest_at_str is None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=_INITIAL_LOOKBACK_DAYS)
            logger.info(
                "[eco_donations_poller] first run — fetching last %d days", _INITIAL_LOOKBACK_DAYS
            )
        else:
            cutoff = datetime.fromisoformat(latest_at_str.replace("Z", "+00:00"))
            logger.debug(
                "[eco_donations_poller] incremental fetch since %s", latest_at_str
            )

        # Pre-load citizen map (user_id -> (name, mu_name)) for bulk name resolution.
        citizen_map: dict[str, tuple[str | None, str | None]] = (
            await db.get_citizen_name_mu_map(nl_country_id)
        )
        logger.debug(
            "[eco_donations_poller] citizen map: %d citizens", len(citizen_map)
        )

        cursor: str | None = None
        total_inserted = 0

        while True:
            payload: dict = {
                "countryId": nl_country_id,
                "transactionType": "donation",
                "limit": 100,
            }
            if cursor:
                payload["cursor"] = cursor

            try:
                resp = await client.post(
                    "/transaction.getPaginatedTransactions",
                    json=payload,
                )
                data = _unwrap(resp) if isinstance(resp, dict) else {}
                transactions: list = (
                    data.get("items")
                    or data.get("transactions")
                    or data.get("results")
                    or []
                ) if isinstance(data, dict) else []
            except Exception as exc:
                logger.warning("[eco_donations_poller] API error: %s", exc)
                break

            if not transactions:
                break

            done = False
            batch: list[tuple] = []

            for txn in transactions:
                user_id = str(txn.get("buyerId") or "").strip()
                created_at_str = str(txn.get("createdAt") or "").strip()
                raw_amount = txn.get("money", 0)

                if not user_id or not created_at_str:
                    continue

                try:
                    amount = float(raw_amount)
                    created_at = datetime.fromisoformat(
                        created_at_str.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    continue

                if amount <= 0:
                    continue

                # Transactions arrive newest-first; once we pass the cutoff we're done.
                if created_at <= cutoff:
                    done = True
                    break

                citizen_name, mu_name = citizen_map.get(user_id, (None, None))
                txn_id = (
                    str(txn.get("_id") or txn.get("id") or "").strip()
                    or _compute_txn_id(user_id, created_at_str, amount)
                )

                batch.append(
                    (txn_id, user_id, citizen_name, mu_name, amount, created_at_str)
                )

            # Bulk upsert the batch
            for row in batch:
                await db.upsert_eco_donation(*row)

            total_inserted += len(batch)

            if done or not batch:
                break

            cursor = (
                data.get("nextCursor") or data.get("cursor")
            ) if isinstance(data, dict) else None
            if not cursor:
                break

        if total_inserted > 0:
            await db.flush_eco_donations()

        return total_inserted


async def setup(bot) -> None:
    await bot.add_cog(EcoDonationsPoller(bot))
