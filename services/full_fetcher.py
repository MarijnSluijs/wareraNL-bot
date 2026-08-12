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
    3. A census of every company in the game → ``company_census``, bucketed by
       the country controlling its region and by the item it produces.
    4. New wage transactions → ``company_tax_revenue``, the income tax paid to
       each country per company and item.
    5. Every alliance and its member countries → ``alliance_countries``, for
       the Nigeria bot's /damage-projection command. Health/hunger for that
       same command is captured as a side effect of step 2's citizen sweep
       (see ``citizen_combat_state`` in services/citizen_cache.py).
    6. Company owners missing from step 2 → backfilled by ID. Step 2 only
       learns citizen IDs via ``user.getUsersByCountry``, which silently
       omits inactive players (``isActive: false``) — see
       ``fetch_missing_owner_citizenships`` for how this was found and why a
       direct-by-ID fetch is the only way to close the gap.

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
from datetime import datetime, timedelta, timezone

from services.api_client import APIClient
from services.citizen_cache import CitizenCache
from services.country_utils import country_id as cid_of
from services.country_utils import extract_country_list
from services.db import Database
from services.db.company_census import CENSUS_RETENTION_DAYS
from services.db.company_tax import (
    TAX_RETENTION_DAYS,
    WORKER_MAP_RETENTION_DAYS,
)
from services.game_time import game_day, game_week_start

logger = logging.getLogger("full_fetcher")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# Company census tuning. 100 is the API's max page size and the tRPC batch
# limit; at ~73k companies that is ~730 pagination calls plus ~730 batched
# detail calls, which completes in roughly two minutes.
_COMPANY_CENSUS_BATCH = 100
_COMPANY_CENSUS_MAX_PAGES = 2000  # safety cap: 2000 × 100 = 200k companies

# Wage-tax tuning. ~8,700 wage transactions an hour is ~87 pages, so 600 pages
# absorbs roughly seven hours of fetcher downtime before a gap is reported.
_WAGE_TAX_MAX_PAGES = 600


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


async def fetch_alliance_countries(client: APIClient, db: Database) -> int:
    """Fetch every alliance and its member countries → ``alliance_countries``.

    ``alliance.getManyPaginated`` already embeds ``memberCountries`` on each
    alliance, so ``alliance.getById`` is never needed. The game currently has
    ~12 alliances (~133 alliance-country pairs total), so this is one cheap
    request per sweep — verified live that ``page`` is not honoured by the API
    (page 2 returns the same items as page 1), so a single call with a
    generous ``limit`` is relied on rather than looping pages; if the item
    count ever comes back equal to the limit, that's logged as a sign the
    result may be truncated.
    """
    dataset = "all_countries.alliances"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        limit = 100  # API hard-caps this at 100 (verified: 200 → 400 Bad Request)
        raw = await client.post(
            "/alliance.getManyPaginated", json={"page": 1, "limit": limit}
        )
        data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise RuntimeError("getManyPaginated returned no items")
        if len(items) >= limit:
            logger.warning(
                "alliance_countries: got %d items == limit %d — "
                "the alliance list may be truncated", len(items), limit,
            )

        now = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[str, str, str]] = []
        for alliance in items:
            if not isinstance(alliance, dict):
                continue
            aid = str(alliance.get("_id") or "")
            name = str(alliance.get("name") or aid)
            if not aid:
                continue
            for member in alliance.get("memberCountries") or []:
                if not isinstance(member, dict):
                    continue
                cid = member.get("country")
                if cid:
                    rows.append((aid, name, str(cid)))

        written = await db.save_alliance_countries(rows, now)
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "alliance_countries: %d alliances, %d alliance-country pairs (%.1fs)",
            len(items), written, duration_ms / 1000,
        )
        return written
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("alliance_countries sweep failed")
        return 0


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


async def fetch_company_census(client: APIClient, db: Database) -> int:
    """Census every company in the game, bucketed by controlling country + item.

    Two phases:
      1. Paginate ``company.getCompanies`` (no filter) for every company ID.
      2. Batch ``company.getById`` to read each company's region, itemCode and
         workerCount, mapping region → country via ``region.getRegionsObject``.

    A company belongs to the country that *currently controls* its region, so
    the mapping is re-read every sweep rather than cached.

    The same responses also carry the owner ID, so a per-owner breakdown is
    written to ``company_owners`` for free — no extra requests.

    Companies listed in phase 1 can be sold or destroyed before phase 2 reads
    them, so ``checked`` is normally a little lower than ``listed``; both are
    recorded.  Returns the number of census rows written.
    """
    dataset = "all_countries.company_census"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        # ── region → controlling country ─────────────────────────────────────
        raw_regions = await client.get("/region.getRegionsObject")
        regions = _unwrap_trpc(raw_regions) if isinstance(raw_regions, dict) else raw_regions
        if not isinstance(regions, dict) or not regions:
            raise RuntimeError("getRegionsObject returned no regions")
        region_country: dict[str, str] = {}
        for rid, robj in regions.items():
            if not isinstance(robj, dict):
                continue
            country = robj.get("country")
            if isinstance(country, dict):
                country = country.get("_id") or country.get("id")
            if country:
                region_country[str(rid)] = str(country)

        # ── phase 1: every company ID ────────────────────────────────────────
        company_ids: list[str] = []
        cursor: str | None = None
        for _page in range(_COMPANY_CENSUS_MAX_PAGES):
            payload: dict = {"perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            raw = await client.get(
                "/company.getCompanies", params={"input": json.dumps(payload)}
            )
            data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict):
                break
            page_ids = data.get("items") or []
            if not isinstance(page_ids, list):
                break
            company_ids.extend(str(i) for i in page_ids if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not page_ids:
                break
        if not company_ids:
            raise RuntimeError("getCompanies returned no companies")
        logger.info("company_census: listed %d companies", len(company_ids))

        # ── phase 2: details, in batches ─────────────────────────────────────
        # (country_id, item_code) → [companies, workers, staffed]
        # "staffed" counts companies with at least one worker, which cannot be
        # derived from the other two afterwards.
        counts: dict[tuple[str, str], list[int]] = {}
        # (country_id, item_code, owner_id) → [companies, workers, staffed].
        # Built from the same responses, so the owner breakdown and the staffed
        # counts both cost no extra API calls.
        owner_counts: dict[tuple[str, str, str], list[int]] = {}
        # (company_id, country_id, item_code) for every company with >=1 worker,
        # used below to build the worker→company map for tax attribution.
        staffed_companies: list[tuple[str, str, str]] = []
        checked = 0
        for start in range(0, len(company_ids), _COMPANY_CENSUS_BATCH):
            chunk = company_ids[start : start + _COMPANY_CENSUS_BATCH]
            results = await client.batch_get(
                "/company.getById",
                [{"companyId": cid} for cid in chunk],
                batch_size=_COMPANY_CENSUS_BATCH,
            )
            for raw_company in results:
                company = (
                    _unwrap_trpc(raw_company)
                    if isinstance(raw_company, dict)
                    else raw_company
                )
                if not isinstance(company, dict):
                    continue  # deleted between phases, or a failed lookup
                checked += 1
                country_id = region_country.get(str(company.get("region") or ""))
                item_code = str(company.get("itemCode") or "")
                if not country_id or not item_code:
                    continue
                try:
                    workers = int(company.get("workerCount") or 0)
                except (TypeError, ValueError):
                    workers = 0

                staffed = 1 if workers > 0 else 0

                bucket = counts.setdefault((country_id, item_code), [0, 0, 0])
                bucket[0] += 1
                bucket[1] += workers
                bucket[2] += staffed

                owner_id = str(company.get("user") or "")
                if owner_id:
                    obucket = owner_counts.setdefault(
                        (country_id, item_code, owner_id), [0, 0, 0]
                    )
                    obucket[0] += 1
                    obucket[1] += workers
                    obucket[2] += staffed

                # Companies with staff need a worker roster so wage
                # transactions can be attributed back to this company.
                if staffed:
                    staffed_companies.append(
                        (str(company.get("_id") or ""), country_id, item_code)
                    )

        captured_at = datetime.now(timezone.utc).isoformat()
        duration_ms = int((time.monotonic() - started) * 1000)
        written = await db.save_company_census(
            captured_at,
            [(c, i, v[0], v[1], v[2]) for (c, i), v in counts.items()],
            listed_companies=len(company_ids),
            checked_companies=checked,
            duration_ms=duration_ms,
        )

        # Worker rosters for staffed companies — the worker→company map that
        # wage transactions are attributed through. Batched 100 per request, so
        # ~10k staffed companies costs ~100 requests.
        worker_rows: list[tuple[str, str, str, str]] = []
        for start in range(0, len(staffed_companies), _COMPANY_CENSUS_BATCH):
            chunk = staffed_companies[start : start + _COMPANY_CENSUS_BATCH]
            results = await client.batch_get(
                "/worker.getWorkers",
                [{"companyId": cid} for cid, _, _ in chunk],
                batch_size=_COMPANY_CENSUS_BATCH,
            )
            for (cid, country_id, item_code), raw_workers in zip(chunk, results):
                data = (
                    _unwrap_trpc(raw_workers)
                    if isinstance(raw_workers, dict)
                    else raw_workers
                )
                if not isinstance(data, dict):
                    continue
                for w in data.get("workers") or []:
                    if not isinstance(w, dict):
                        continue
                    worker_id = str(w.get("user") or "")
                    if worker_id:
                        worker_rows.append((worker_id, cid, country_id, item_code))

        mapped = await db.save_worker_company_map(worker_rows, captured_at)
        worker_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=WORKER_MAP_RETENTION_DAYS)
        ).isoformat()
        await db.prune_worker_company_map(worker_cutoff)

        owner_rows = await db.save_company_owners(
            captured_at,
            [(c, i, o, v[0], v[1], v[2]) for (c, i, o), v in owner_counts.items()],
        )

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=CENSUS_RETENTION_DAYS)
        ).isoformat()
        pruned = await db.prune_company_census(cutoff)

        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "company_census: listed=%d checked=%d rows=%d owners=%d workers=%d "
            "pruned=%d (%.1fs)",
            len(company_ids), checked, written, owner_rows, mapped, pruned,
            duration_ms / 1000,
        )
        return written
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("company_census sweep failed")
        return 0


async def fetch_missing_owner_citizenships(
    cache: CitizenCache, db: Database
) -> int:
    """Backfill ``citizen_levels`` for company owners the citizen sweep missed.

    ``user.getUsersByCountry`` — the endpoint the regular citizen sweep
    (:func:`fetch_all_citizen_levels`) uses to enumerate each country's
    citizens — was confirmed live to silently omit inactive players
    (``isActive: false``). Their profile is still fully live and fetchable by
    ID; a country-by-country sweep just never learns those IDs exist. Since
    ``company_owners`` (just written by :func:`fetch_company_census`) already
    has the *exact* set of owner IDs that matter, any of them missing from
    ``citizen_levels`` is fetched directly by ID here — no discovery needed,
    no dependence on the broken listing endpoint.

    This is what makes ``/productie`` on the Nigeria bot able to attribute a
    company to its owner's nationality even when the owner is inactive.
    """
    dataset = "all_countries.owner_citizenships"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        async with db._conn.execute(
            "SELECT DISTINCT co.owner_id FROM company_owners co "
            "LEFT JOIN citizen_levels cl ON cl.user_id = co.owner_id "
            "WHERE cl.user_id IS NULL"
        ) as cur:
            missing = [str(r[0]) for r in await cur.fetchall()]

        recorded = await cache.refresh_specific_users(missing)

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "owner_citizenships: %d owners missing, %d recorded (%.1fs)",
            len(missing), recorded, duration_ms / 1000,
        )
        return recorded
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("owner_citizenships backfill failed")
        return 0


async def fetch_wage_taxes(
    client: APIClient, db: Database, countries: list[dict]
) -> int:
    """Collect income tax paid on wages since the last sweep.

    Wage transactions carry only the worker (``sellerId``) and employer
    (``buyerId``), never a company or item, so each one is attributed through
    ``worker_company_map`` (built by :func:`fetch_company_census`) to get the
    company, its country and its item.  Tax is then
    ``money × country income_tax %``.

    Strictly incremental: pagination walks newest-first and stops at the stored
    watermark, so nothing is ever counted twice.  On the very first run no
    history is collected at all — the watermark is simply planted at the newest
    transaction and ``wage_tax_started_at`` recorded, because backfilling would
    mean walking the entire transaction log.

    Returns the number of wage transactions counted.
    """
    dataset = "all_countries.wage_taxes"
    await db.mark_started(dataset, source="full_fetcher")
    started = time.monotonic()
    try:
        # Tax rates ride along on the country list already fetched this sweep,
        # so keeping them current costs no extra request.
        now = datetime.now(timezone.utc)
        rate_rows: list[tuple[str, float]] = []
        for c in countries:
            cid = cid_of(c)
            taxes = c.get("taxes") if isinstance(c.get("taxes"), dict) else {}
            if cid:
                rate_rows.append((cid, _to_float(taxes.get("income")) or 0.0))
        await db.save_country_tax_rates(rate_rows, now.isoformat())
        rates = await db.get_country_tax_rates()

        watermark = await db.get_tax_watermark()
        first_run = watermark is None

        # Walk newest-first until we reach the last transaction we counted.
        new_items: list[dict] = []
        cursor: str | None = None
        newest_id: str | None = None
        hit_watermark = False
        for _page in range(_WAGE_TAX_MAX_PAGES):
            payload: dict = {"limit": 100, "transactionType": "wage"}
            if cursor:
                payload["cursor"] = cursor
            raw = await client.post(
                "/transaction.getPaginatedTransactions", json=payload
            )
            data = _unwrap_trpc(raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            if not items:
                break
            if newest_id is None:
                newest_id = str(items[0].get("_id") or "")
            if first_run:
                break  # only needed the newest ID to plant the watermark
            for tx in items:
                if str(tx.get("_id") or "") == watermark:
                    hit_watermark = True
                    break
                new_items.append(tx)
            if hit_watermark:
                break
            cursor = data.get("nextCursor")
            if not cursor:
                break

        if first_run:
            if newest_id:
                await db.set_tax_watermark(newest_id)
            await db.set_tax_started_at(now.isoformat())
            duration_ms = int((time.monotonic() - started) * 1000)
            await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
            logger.info(
                "wage_taxes: first run — tracking starts now, no backfill "
                "(watermark=%s)", newest_id,
            )
            return 0

        if not hit_watermark and new_items:
            logger.warning(
                "wage_taxes: walked %d pages without reaching the watermark — "
                "some transactions were missed (fetcher down too long?)",
                _WAGE_TAX_MAX_PAGES,
            )

        worker_map = await db.get_worker_company_map()

        # (day, country, item, company) → [tax, wage, count]
        buckets: dict[tuple[str, str, str, str], list] = {}
        unattributed = 0
        for tx in new_items:
            worker_id = str(tx.get("sellerId") or "")
            entry = worker_map.get(worker_id)
            if entry is None:
                unattributed += 1
                continue
            company_id, country_id, item_code = entry
            money = _to_float(tx.get("money")) or 0.0
            if money <= 0:
                continue
            created = str(tx.get("createdAt") or "")
            day = created[:10]
            if len(day) != 10:
                continue
            tax = money * rates.get(country_id, 0.0) / 100.0
            key = (day, country_id, item_code, company_id)
            bucket = buckets.setdefault(key, [0.0, 0.0, 0])
            bucket[0] += tax
            bucket[1] += money
            bucket[2] += 1

        rows_written = await db.add_tax_revenue(
            [(d, c, i, comp, v[0], v[1], v[2]) for (d, c, i, comp), v in buckets.items()]
        )
        # Watermark only after the aggregates are committed: a crash in between
        # would re-count an hour, whereas the reverse order would silently drop
        # one, and a visible double is easier to notice than a silent hole.
        if newest_id:
            await db.set_tax_watermark(newest_id)

        cutoff_day = (now - timedelta(days=TAX_RETENTION_DAYS)).strftime("%Y-%m-%d")
        await db.prune_tax_revenue(cutoff_day)

        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(dataset, status="ok", duration_ms=duration_ms)
        logger.info(
            "wage_taxes: %d new wage tx, %d attributed to %d buckets, "
            "%d unattributed (%.1f%%) (%.1fs)",
            len(new_items), len(new_items) - unattributed, rows_written,
            unattributed,
            100.0 * unattributed / len(new_items) if new_items else 0.0,
            duration_ms / 1000,
        )
        return len(new_items)
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        await db.mark_finished(
            dataset, status="error", error=str(exc)[:500], duration_ms=duration_ms
        )
        logger.exception("wage_taxes sweep failed")
        return 0


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
      - company_census      FULL_FETCH_ENABLE_COMPANY_CENSUS (default 1)
      - wage_taxes          FULL_FETCH_ENABLE_WAGE_TAXES (default 1; needs census)
      - owner_citizenships  FULL_FETCH_ENABLE_OWNER_CITIZENSHIPS (default 1; needs census)
      - alliances           FULL_FETCH_ENABLE_ALLIANCES (default 1)
    """
    _written, countries = await fetch_country_snapshots(client, db)
    if not countries:
        return
    if _env_int("FULL_FETCH_ENABLE_ALLIANCES", 1) == 1:
        await fetch_alliance_countries(client, db)
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
    if _env_int("FULL_FETCH_ENABLE_COMPANY_CENSUS", 1) == 1:
        await fetch_company_census(client, db)
        # Both must run after the census: one depends on the worker→company
        # map it refreshes, the other on the owner IDs in company_owners.
        if _env_int("FULL_FETCH_ENABLE_WAGE_TAXES", 1) == 1:
            await fetch_wage_taxes(client, db, countries)
        if _env_int("FULL_FETCH_ENABLE_OWNER_CITIZENSHIPS", 1) == 1:
            await fetch_missing_owner_citizenships(cache, db)


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
