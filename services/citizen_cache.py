"""Handles fetching and caching citizen level data from the WarEra API."""

import json
import logging
from datetime import datetime
from typing import Any

from services.api_client import APIClient
from services.db import Database

logger = logging.getLogger("discord_bot")


class CitizenCache:
    """Fetches citizen level data from the API and persists it to the DB cache."""

    def __init__(self, client: APIClient, db: Database) -> None:
        self._client = client
        self._db = db

    # ------------------------------------------------------------------ #
    # Public interface                                                     #
    # ------------------------------------------------------------------ #

    async def refresh_country(self, country_id: str, country_name: str, *, progress_msg=None) -> int:
        """Fetch every citizen's level for a country and write to DB cache.

        Uses tRPC HTTP batching (30 users per request) with automatic fallback
        to individual calls if the server doesn't support batching.

        Returns the number of citizens whose level was successfully recorded.
        """
        user_ids = await self._fetch_user_ids(country_id)
        if not user_ids:
            return 0

        await self._db.delete_citizens_for_country(country_id)

        updated_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        batch_size = 100
        inputs = [{"userId": uid} for uid in user_ids]
        total_batches = (len(user_ids) + batch_size - 1) // batch_size

        results = await self._client.batch_get(
            "/user.getUserLite",
            inputs,
            batch_size=batch_size,
            chunk_sleep=0.5,
        )

        recorded = 0
        for i, (uid, obj) in enumerate(zip(user_ids, results)):
            lvl = self._extract_level(obj)
            mode = self._extract_skill_mode(obj)
            reset_at = self._extract_last_skills_reset_at(obj)
            last_login = self._extract_last_login_at(obj)
            name = self._extract_name(obj)
            mu_id, mu_name = self._extract_mu_info(obj)
            if lvl is not None:
                await self._db.upsert_citizen_level(
                    uid, country_id, lvl, updated_at,
                    skill_mode=mode, last_skills_reset_at=reset_at,
                    citizen_name=name, last_login_at=last_login,
                    mu_id=mu_id, mu_name=mu_name,
                )
                recorded += 1

            if (i + 1) % (batch_size * 2) == 0:
                await self._db.flush_citizen_levels()
                if progress_msg:
                    batch_done = (i + 1) // batch_size
                    try:
                        await progress_msg.edit(
                            content=(
                                f"Refreshing **{country_name}**: "
                                f"batch {batch_done}/{total_batches} "
                                f"({i + 1}/{len(user_ids)} citizens)…"
                            )
                        )
                    except Exception:
                        pass

        await self._db.flush_citizen_levels()
        return recorded

    async def refresh_mu_memberships(self, country_id: str, mus_json_path: str) -> int:
        """Fetch MU member lists from the API and write mu_id/mu_name to citizen_levels.

        Reads MU IDs from *mus_json_path* (the templates/mus.json file), paginates
        /mu.getById for each MU to get the member user IDs, then bulk-updates the DB.

        Only citizens already present in citizen_levels for *country_id* are updated —
        foreign players in an NL MU are silently skipped.

        Returns the number of citizen rows updated.
        """
        # Load MU list from mus.json
        try:
            with open(mus_json_path, encoding="utf-8") as f:
                mus_data = json.load(f)
        except Exception as exc:
            logger.warning("refresh_mu_memberships: failed to load %s: %s", mus_json_path, exc)
            return 0

        embeds = mus_data.get("embeds", [])
        if not embeds:
            return 0

        # Extract mu_id from the URL in each embed's description
        # e.g. "[**Elite MU**](https://app.warera.io/mu/695c10139cddbde0503e0d36)"
        import re
        mu_entries: list[tuple[str, str]] = []  # (mu_id, mu_name)
        for embed in embeds:
            title = embed.get("title", "?")
            description = embed.get("description", "")
            m = re.search(r'/mu/([a-f0-9]+)', description)
            if m:
                mu_entries.append((m.group(1), title))

        if not mu_entries:
            logger.warning("refresh_mu_memberships: no MU IDs found in %s", mus_json_path)
            return 0

        # Reset all MU assignments for this country first
        await self._db.clear_citizen_mus_for_country(country_id)

        updated = 0
        for mu_id, mu_name in mu_entries:
            member_ids = await self._fetch_mu_member_ids(mu_id)
            for uid in member_ids:
                await self._db.update_citizen_mu(uid, mu_id, mu_name)
                updated += 1
            await self._db.flush_citizen_levels()
            logger.debug("refresh_mu_memberships: %s → %d members", mu_name, len(member_ids))

        return updated

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    async def _fetch_mu_member_ids(self, mu_id: str) -> list[str]:
        """Paginate /mu.getById and return all member user IDs for a given MU."""
        user_ids: list[str] = []
        # mu.getById returns the full MU including its members list
        try:
            resp = await self._client.get(
                "/mu.getById",
                params={"input": json.dumps({"muId": mu_id})},
            )
        except Exception as exc:
            logger.warning("_fetch_mu_member_ids(%s): request failed: %s", mu_id, exc)
            return user_ids

        # Navigate the response to find the members list
        data = resp
        if isinstance(resp, dict):
            for key in ("result", "data"):
                v = resp.get(key)
                if isinstance(v, dict):
                    data = v.get("data", v)
                    break

        members: list = []
        if isinstance(data, dict):
            for key in ("members", "citizenIds", "userIds", "users"):
                v = data.get(key)
                if isinstance(v, list):
                    members = v
                    break

        for entry in members:
            if isinstance(entry, str):
                user_ids.append(entry)
            elif isinstance(entry, dict):
                uid = (
                    entry.get("userId")
                    or entry.get("_id")
                    or entry.get("id")
                    or entry.get("citizenId")
                )
                if uid:
                    user_ids.append(str(uid))

        return user_ids

    async def _fetch_user_ids(self, country_id: str) -> list[str]:
        """Paginate /user.getUsersByCountry and return all user IDs."""
        user_ids: list[str] = []
        cursor = None
        while True:
            params: dict = {"countryId": country_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor

            resp = await self._client.get(
                "/user.getUsersByCountry",
                params={"input": json.dumps(params)},
            )

            data_obj = resp
            if isinstance(resp, dict):
                for key in ("result", "data"):
                    v = resp.get(key)
                    if isinstance(v, dict):
                        data_obj = v.get("data", v)
                        break

            users: list = []
            next_cursor = None
            if isinstance(data_obj, list):
                users = data_obj
            elif isinstance(data_obj, dict):
                for key in ("items", "users", "data", "result"):
                    v = data_obj.get(key)
                    if isinstance(v, list):
                        users = v
                        break
                next_cursor = (
                    data_obj.get("nextCursor")
                    or data_obj.get("cursor")
                    or data_obj.get("next")
                )

            for user in users:
                if isinstance(user, dict):
                    uid = user.get("_id") or user.get("id") or user.get("userId")
                    if uid:
                        user_ids.append(str(uid))

            if not next_cursor or not users:
                break
            cursor = next_cursor
        return user_ids

    @staticmethod
    def _extract_level(obj: Any) -> int | None:
        """Pull the level integer out of a getUserLite result dict."""
        if not isinstance(obj, dict):
            return None
        try:
            return int(obj["leveling"]["level"])
        except (KeyError, TypeError, ValueError):
            pass
        if obj.get("level") is not None:
            try:
                return int(obj["level"])
            except (ValueError, TypeError):
                pass
        try:
            return int(obj["rankings"]["userLevel"]["value"])
        except (KeyError, TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _extract_skill_mode(obj: Any) -> str | None:
        """Classify a player as 'eco' or 'war' based on where they spent skill points.

        Points spent in skill at level L = L*(L+1)//2.
        Eco skills : entrepreneurship, energy, production, companies, management.
        War skills : attack, health, hunger, criticalChance, criticalDamages,
                     armor, precision, dodge, lootChance.
        Ties go to eco.
        Returns None when no skill data is available.
        """
        if not isinstance(obj, dict):
            return None
        skills = obj.get("skills")
        if not isinstance(skills, dict):
            return None
        eco_names = {"entrepreneurship", "energy", "production", "companies", "management"}
        war_names = {"attack", "health", "hunger", "criticalChance", "criticalDamages",
                     "armor", "precision", "dodge", "lootChance"}
        eco_pts = 0
        war_pts = 0
        for name, sdata in skills.items():
            if not isinstance(sdata, dict):
                continue
            lv = int(sdata.get("level") or 0)
            pts = lv * (lv + 1) // 2
            if name in eco_names:
                eco_pts += pts
            elif name in war_names:
                war_pts += pts
        return "eco" if eco_pts >= war_pts else "war"

    @staticmethod
    def _extract_name(obj: Any) -> str | None:
        """Pull the player name from a getUserLite result."""
        if not isinstance(obj, dict):
            return None
        for key in ("name", "username", "displayName", "nick"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        for sub in ("profile", "user"):
            sub_obj = obj.get(sub)
            if isinstance(sub_obj, dict):
                for key in ("name", "username", "displayName"):
                    val = sub_obj.get(key)
                    if isinstance(val, str) and val:
                        return val
        return None

    @staticmethod
    def _extract_last_skills_reset_at(obj: Any) -> str | None:
        """Pull the lastSkillsResetAt ISO timestamp from a getUserLite result.

        The value lives inside the nested ``dates`` object and is absent when
        the player has never reset their skills.
        """
        if not isinstance(obj, dict):
            return None
        dates = obj.get("dates")
        if not isinstance(dates, dict):
            return None
        val = dates.get("lastSkillsResetAt")
        if isinstance(val, str) and val:
            return val
        return None

    @staticmethod
    def _extract_mu_info(obj: Any) -> tuple[str | None, str | None]:
        """Extract (mu_id, mu_name) from a getUserLite result dict.

        Tries common nested and flat field names so we're resilient to API
        changes. Returns (None, None) when no MU info is present.
        """
        if not isinstance(obj, dict):
            return None, None
        # Nested MU object under various keys
        for mu_key in ("mu", "militaryUnit", "regiment", "unit", "militaryUnits"):
            mu_obj = obj.get(mu_key)
            if isinstance(mu_obj, dict):
                mu_id = (
                    mu_obj.get("_id") or mu_obj.get("id") or mu_obj.get("muId")
                )
                mu_name = (
                    mu_obj.get("name") or mu_obj.get("title") or mu_obj.get("muName")
                )
                if mu_id:
                    return str(mu_id), str(mu_name) if mu_name else None
        # Flat fields at root level
        mu_id_flat = (
            obj.get("muId") or obj.get("militaryUnitId") or obj.get("regimentId")
        )
        mu_name_flat = (
            obj.get("muName") or obj.get("militaryUnitName") or obj.get("regimentName")
        )
        if mu_id_flat:
            return str(mu_id_flat), str(mu_name_flat) if mu_name_flat else None
        return None, None

    @staticmethod
    def _extract_last_login_at(obj: Any) -> str | None:
        """Pull the last-login ISO timestamp from a getUserLite result.

        Tries several candidate field names in the ``dates`` sub-object and at
        the root level, since the exact key varies across API versions.
        """
        if not isinstance(obj, dict):
            return None
        # Try nested ``dates`` object first (same location as lastSkillsResetAt)
        dates = obj.get("dates")
        if isinstance(dates, dict):
            for key in ("lastLoginAt", "lastSeenAt", "lastOnlineAt", "lastActiveAt", "lastLogin"):
                val = dates.get(key)
                if isinstance(val, str) and val:
                    return val
        # Fall back to root-level keys
        for key in ("lastLoginAt", "lastSeenAt", "lastOnlineAt", "lastActiveAt", "lastLogin"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        return None
