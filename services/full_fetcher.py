"""Standalone hourly all-countries data fetcher.

Runs as a dedicated process (separate from the Discord bot and the website)
that wakes up every hour, performs a complete sweep of API data for every
country, and writes the results into the shared SQLite database.

Why a separate container?
    * Keeps the Discord bot's task loop lean (and its crashes out of the
      data pipeline).
    * Lets us tune the sweep cadence independently.
    * Single, predictable owner of the API rate budget for full sweeps.

What is fetched, in order, every hour:
    1. ``country.getAllCountries`` → master country list (writes
       ``country_snapshots`` with the latest production_bonus / specialized_item).
    2. For each country: every citizen's ``user.getUserLite`` →
       ``citizen_levels`` (via :class:`services.citizen_cache.CitizenCache`).

Each sweep step records progress through
:meth:`services.db.Database.mark_started` / :meth:`mark_finished` under
descriptive dataset keys (``"all_countries.snapshots"``,
``"all_countries.citizens"``) so the website's /admin and /paraatheid pages
can show freshness.

Configuration via environment:
    FULL_FETCH_INTERVAL_MINUTES (default 60)
    FULL_FETCH_RUN_AT_STARTUP   (default 1; set to 0 to wait for the first
                                 interval before doing any work)
    FULL_FETCH_COUNTRY_LIMIT    (default 0 = unlimited; useful for smoke tests)
    RW_DB_PATH, RW_API_BASE_URL, RW_API_KEYS_PATH — same as the website.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from services.api_client import APIClient
from services.citizen_cache import CitizenCache
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list
from services.db import Database
from services.game_time import game_day, game_week_start

logger = logging.getLogger("full_fetcher")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _seconds_until_next_aligned_run(minute_offset: int, min_gap_s: int = 300) -> float:
    """Return seconds to sleep until the next HH:{minute_offset:02d}:00 UTC.

    ``min_gap_s`` ensures we don't re-trigger immediately after a run that
    finished just before the target minute (default 5-minute guard).
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    candidate = now.replace(minute=minute_offset, second=0, microsecond=0)
    if candidate <= now + timedelta(seconds=min_gap_s):
        candidate += timedelta(hours=1)
    return (candidate - now).total_seconds()


def _load_api_keys(path: str) -> list[str]:
    """Same shape as the website's loader, but inlined to keep this script
    runnable without importing the website package (which depends on
    fastapi etc.).

    Falls back to the shared _api_keys.json when a dedicated per-service key
    file (see docker-compose.data-fetcher.yml) hasn't been set up on this
    machine yet — e.g. a standalone deployment that only runs this fetcher.
    """
    if not os.path.isfile(path):
        fallback = "_api_keys.json"
        if path != fallback and os.path.isfile(fallback):
            logger.warning("API keys file %s not found — falling back to %s", path, fallback)
            path = fallback
        else:
            logger.warning("API keys file not found at %s — running keyless", path)
            return []
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:  # noqa: BLE001 — we want to log and continue
        logger.exception("Failed to load API keys from %s", path)
        return []
    if isinstance(data, list):
        return [str(k) for k in data if k]
    if isinstance(data, dict):
        keys = data.get("keys") or list(data.values())
        return [str(k) for k in keys if k]
    return []


# ── one-shot sweep steps ──────────────────────────────────────────────────────


async def fetch_country_snapshots(client: APIClient, db: Database) -> tuple[int, list[dict]]:
    """Fetch the master country list and upsert into ``country_snapshots``.

    Returns ``(count_written, country_list)`` where ``country_list`` is the
    canonical list used by the next steps.
    """
    dataset = "all_countries.snapshots"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        resp = await client.get("/country.getAllCountries")
        countries = extract_country_list(resp)
        if not countries:
            raise RuntimeError("getAllCountries returned no countries")

        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        written = 0
        for c in countries:
            cid = cid_of(c)
            if not cid:
                continue
            await db.save_country_snapshot(
                country_id=cid,
                code=c.get("code"),
                name=c.get("name"),
                specialized_item=c.get("specializedItem") or c.get("specialized_item"),
                production_bonus=_to_float(c.get("productionBonus") or c.get("production_bonus")),
                raw_json=json.dumps(c, ensure_ascii=False),
                updated_at=updated_at,
            )
            written += 1
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info("country_snapshots: wrote %d rows (%.1fs)", written, duration_ms / 1000)
        return written, countries
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms)
        logger.exception("country_snapshots failed")
        return 0, []


async def fetch_all_citizen_levels(
    cache: CitizenCache,
    db: Database,
    countries: list[dict],
    *,
    limit: int = 0,
) -> int:
    """Refresh ``citizen_levels`` for every country in ``countries``.

    ``limit`` > 0 caps the number of countries (smoke-test convenience).
    Returns the total number of citizens recorded.
    """
    dataset = "all_countries.citizens"
    await db.mark_started(dataset, source="full_fetcher")
    # Stamp the same key the discord bot checks so it skips its own sweep while
    # this one is (or was recently) running.
    await db.set_poll_state(
        "citizen_refresh_last_run",
        datetime.now(timezone.utc).isoformat(),
    )
    started = time.monotonic()
    total = 0
    if limit > 0:
        countries = countries[:limit]
    failed: list[str] = []
    for c in countries:
        cid = cid_of(c)
        name = str(c.get("name") or cid or "?")
        if not cid:
            continue
        try:
            t0 = time.monotonic()
            n = await cache.refresh_country(cid, name)
            total += n
            logger.info(
                "citizens %-20s wrote=%-5d (%.1fs)",
                name, n, time.monotonic() - t0,
            )
        except Exception:  # noqa: BLE001 — keep sweep alive
            logger.exception("citizen refresh failed for %s (%s)", name, cid)
            failed.append(name)

    duration_ms = int((time.monotonic() - started) * 1000)
    if failed and len(failed) == len(countries):
        await db.mark_finished(
            dataset, status="error",
            error=f"all {len(failed)} countries failed",
            duration_ms=duration_ms,
        )
    else:
        await db.mark_finished(
            dataset, status="ok",
            error=(f"{len(failed)} country failures: {','.join(failed[:5])}"
                   if failed else None),
            duration_ms=duration_ms,
        )
    logger.info(
        "citizen sweep done: total=%d, failures=%d (%.1fs)",
        total, len(failed), duration_ms / 1000,
    )
    return total


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── extra tiers ───────────────────────────────────────────────────────────────


def _unwrap_trpc(resp):
    """Unwrap a trpc {"result":{"data":...}} envelope (one or two levels)."""
    data = resp
    if isinstance(data, dict):
        for key in ("result", "data"):
            v = data.get(key)
            if isinstance(v, dict):
                data = v.get("data", v)
                break
    return data


async def fetch_recent_trades(client: APIClient, db: Database) -> int:
    """Pull the 100 most recent itemMarket transactions and upsert."""
    dataset = "all_countries.trades"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        raw = await client.post(
            "/transaction.getPaginatedTransactions",
            json={"limit": 100, "transactionType": "itemMarket"},
        )
        data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
        items: list = []
        if isinstance(data, dict):
            items = (
                data.get("items")
                or data.get("transactions")
                or data.get("results")
                or []
            )
        elif isinstance(data, list):
            items = data

        if not items:
            await db.mark_finished(
                dataset, status="ok",
                duration_ms=int((time.monotonic() - started) * 1000),
                error="no trades returned",
            )
            return 0

        inserted, seen = await db.upsert_trades(items)
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info("trades: %d new / %d seen (%.1fs)",
                    inserted, seen, duration_ms / 1000)
        return inserted
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("trades sweep failed")
        return 0


def _ranking_entries(resp) -> list[dict]:
    """Return the flat entry list from a ranking.getRanking response."""
    data = _unwrap_trpc(resp)
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        for key in ("items", "ranking", "rankings", "data", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
    return []


def _entry_user_id(entry: dict) -> str | None:
    """Extract the citizen's account id from a ranking entry.

    ``user`` references the citizen; ``_id`` is the ranking record's own id,
    so ``user`` must win when both are present.
    """
    user = entry.get("user")
    if isinstance(user, str) and user:
        return user
    if isinstance(user, dict):
        for key in ("_id", "id", "userId"):
            v = user.get(key)
            if v:
                return str(v)
    for key in ("userId", "citizenId", "id", "_id"):
        v = entry.get(key)
        if v:
            return str(v)
    return None


def _entry_username(entry: dict) -> str | None:
    for key in ("username", "name", "citizenName"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            return v
    user = entry.get("user")
    if isinstance(user, dict):
        for key in ("username", "name"):
            v = user.get(key)
            if isinstance(v, str) and v:
                return v
    return None


def _entry_damage(entry: dict) -> float:
    for key in ("value", "damage", "totalDamage", "weeklyDamage",
                "weeklyBattleDamage", "amount"):
        v = entry.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


async def fetch_weekly_damages(client: APIClient, db: Database) -> int:
    """Snapshot the global weekly-damage ranking for every player.

    ``ranking.getRanking(weeklyUserDamages)`` returns every player in the game
    in one call, so this is cheap regardless of how many countries we track.
    Each entry is tagged with the player's country/MU from ``citizen_levels``
    and handed to :meth:`Database.apply_weekly_damage_snapshot`, which writes
    the weekly history row and derives the game day's damage from the delta.
    """
    dataset = "all_countries.weekly_damage"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        resp = await client.post(
            "/ranking.getRanking", json={"rankingType": "weeklyUserDamages"}
        )
        entries = _ranking_entries(resp)
        if not entries:
            raise RuntimeError("weeklyUserDamages returned no entries")

        # country/MU per player, so history stays correct after someone moves
        meta: dict[str, tuple] = {}
        async with db._conn.execute(
            "SELECT user_id, citizen_name, country_id, mu_id, mu_name FROM citizen_levels"
        ) as cur:
            async for row in cur:
                meta[str(row[0])] = (row[1], row[2], row[3], row[4])

        payload: list[dict] = []
        seen: set[str] = set()
        for e in entries:
            uid = _entry_user_id(e)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            name, country_id, mu_id, mu_name = meta.get(uid, (None, None, None, None))
            payload.append({
                "user_id": uid,
                "citizen_name": _entry_username(e) or name,
                "country_id": country_id,
                "mu_id": mu_id,
                "mu_name": mu_name,
                "weekly_damage": _entry_damage(e),
            })

        now = datetime.now(timezone.utc)
        # Let the Discord bot's own weekly-damage task know this sweep owns
        # the data now, so it doesn't duplicate the work (or the API call).
        await db.set_poll_state("weekly_damage_fetcher_last_run", now.isoformat())
        counts = await db.apply_weekly_damage_snapshot(
            payload,
            game_date=game_day(now),
            week_start=game_week_start(now),
            updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "weekly_damage: %d players (day=%s week=%s, %d prior days closed) (%.1fs)",
            counts["weekly"], game_day(now), game_week_start(now),
            counts["closed"], duration_ms / 1000,
        )
        return counts["weekly"]
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("weekly_damage sweep failed")
        return 0


async def fetch_all_mu_names(client: APIClient, db: Database) -> int:
    """Paginate mu.getManyPaginated globally and upsert into known_mus."""
    dataset = "all_countries.mu_registry"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    total = 0
    cursor: str | None = None
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        while True:
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = await client.get(
                    "/mu.getManyPaginated",
                    params={"input": json.dumps(params)},
                )
            except Exception:  # noqa: BLE001
                logger.exception("mu_registry: API call failed")
                break

            data_obj = _unwrap_trpc(resp)
            items: list = []
            next_cursor: str | None = None
            if isinstance(data_obj, list):
                items = data_obj
            elif isinstance(data_obj, dict):
                for key in ("items", "mus", "data"):
                    v = data_obj.get(key)
                    if isinstance(v, list):
                        items = v
                        break
                next_cursor = data_obj.get("nextCursor") or data_obj.get("cursor")

            for item in items:
                if not isinstance(item, dict):
                    continue
                mu_id = str(item.get("_id") or item.get("id") or "").strip()
                mu_name = str(item.get("name") or item.get("title") or "").strip()
                if not (mu_id and mu_name):
                    continue
                country_id: str | None = None
                country_obj = item.get("country") or item.get("nation")
                if isinstance(country_obj, dict):
                    country_id = (
                        str(country_obj.get("_id") or country_obj.get("id") or "").strip()
                        or None
                    )
                if not country_id:
                    raw_cid = (
                        item.get("countryId")
                        or item.get("country_id")
                        or item.get("nationId")
                    )
                    if raw_cid:
                        country_id = str(raw_cid).strip() or None
                await db.upsert_known_mu(mu_id, mu_name, now_iso, country_id)
                total += 1

            if items:
                await db.flush_known_mus()
            if not next_cursor or not items:
                break
            cursor = next_cursor

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info("mu_registry: %d MUs upserted (%.1fs)",
                    total, duration_ms / 1000)
        return total
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("mu_registry sweep failed")
        return total


async def fetch_mu_memberships(cache: CitizenCache, db: Database) -> tuple[int, int]:
    """Sweep every known MU's membership list (slow — gated by env flag).

    Clears all MU assignments first so citizens who left every known MU end up
    with NULL rather than retaining stale data.
    """
    dataset = "all_countries.mu_memberships"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        # Clear first so citizens not in any known MU don't retain stale data.
        await db.clear_all_citizen_mus()
        mus_tagged, citizens_updated = await cache.sweep_all_mu_memberships()
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "mu_memberships: tagged=%d citizens_updated=%d (%.1fs)",
            mus_tagged, citizens_updated, duration_ms / 1000,
        )
        return mus_tagged, citizens_updated
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("mu_memberships sweep failed")
        return 0, 0


# ── checkpoint helper ─────────────────────────────────────────────────────────


async def _try_checkpoint(db: Database) -> None:
    """Attempt a TRUNCATE WAL checkpoint after a sweep.

    The no-write window immediately after run_once() is the best opportunity
    to reset the WAL write pointer and truncate the file.  If readers are
    still active the checkpoint silently does what it can; non-fatal either way.
    """
    try:
        await db.checkpoint("TRUNCATE")
        logger.info("WAL TRUNCATE checkpoint completed after sweep")
    except Exception:
        logger.warning("WAL TRUNCATE checkpoint failed (non-fatal)", exc_info=True)


# ── main loop ─────────────────────────────────────────────────────────────────


async def run_once(client: APIClient, db: Database, *, country_limit: int = 0) -> None:
    """Execute a single end-to-end sweep.

    Tiers (each gated by an env flag, default-on for cheap ones):
      - country_snapshots   (always)
      - citizens per country (always)
      - weekly_damage       FULL_FETCH_ENABLE_WEEKLY_DAMAGE (default 1)
      - trades              FULL_FETCH_ENABLE_TRADES (default 1)
      - mu_registry         FULL_FETCH_ENABLE_MU_REGISTRY (default 1)
      - mu_memberships      FULL_FETCH_ENABLE_MU_MEMBERSHIPS (default 0 — slow)
    """
    _written, countries = await fetch_country_snapshots(client, db)
    if not countries:
        return
    cache = CitizenCache(client, db)
    await fetch_all_citizen_levels(cache, db, countries, limit=country_limit)

    if _env_int("FULL_FETCH_ENABLE_WEEKLY_DAMAGE", 1) == 1:
        await fetch_weekly_damages(client, db)
    if _env_int("FULL_FETCH_ENABLE_TRADES", 1) == 1:
        await fetch_recent_trades(client, db)
    if _env_int("FULL_FETCH_ENABLE_MU_REGISTRY", 1) == 1:
        await fetch_all_mu_names(client, db)
    if _env_int("FULL_FETCH_ENABLE_MU_MEMBERSHIPS", 0) == 1:
        await fetch_mu_memberships(cache, db)


async def main() -> None:
    db_path = os.getenv("RW_DB_PATH", "database/external.db")
    api_base = os.getenv("RW_API_BASE_URL", "https://api2.warera.io/trpc")
    keys_path = os.getenv("RW_API_KEYS_PATH", "_api_keys.json")
    minute_offset = _env_int("FULL_FETCH_MINUTE_OFFSET", 5)
    run_at_startup = _env_int("FULL_FETCH_RUN_AT_STARTUP", 1) == 1
    country_limit = _env_int("FULL_FETCH_COUNTRY_LIMIT", 0)

    logger.info(
        "full-fetcher start: db=%s base=%s schedule=HH:%02d country_limit=%s",
        db_path, api_base, minute_offset, country_limit or "all",
    )

    db = Database(db_path)
    await db.setup()
    api_keys = _load_api_keys(keys_path)
    client = APIClient(api_base, api_keys=api_keys, source="data-fetcher")
    await client.start()

    stop_event = asyncio.Event()

    def _on_signal(signum, _frame=None):
        logger.info("Received signal %s — stopping", signum)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:  # pragma: no cover (windows)
            signal.signal(sig, _on_signal)

    try:
        if run_at_startup:
            try:
                await run_once(client, db, country_limit=country_limit)
            except Exception:  # noqa: BLE001
                logger.exception("startup sweep failed")
            await _try_checkpoint(db)

        while not stop_event.is_set():
            sleep_s = _seconds_until_next_aligned_run(minute_offset)
            logger.info("full-fetcher: next sweep in %.0fs (at HH:%02d UTC)", sleep_s, minute_offset)
            try:
                # Sleep until the next aligned minute, bail out early on shutdown.
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass
            else:
                break  # stop_event was set
            try:
                await run_once(client, db, country_limit=country_limit)
            except Exception:  # noqa: BLE001
                logger.exception("scheduled sweep failed")
            await _try_checkpoint(db)
    finally:
        logger.info("full-fetcher shutting down")
        await client.close()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
