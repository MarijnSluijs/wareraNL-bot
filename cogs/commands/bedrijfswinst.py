"""
This module defines the BedrijfswinstCog, which provides the /bedrijfswinst command to show
whether a user is making a profit on their employees across all their companies.

For each company the command shows, per employee:
    revenue  = sell_price × (company_production_bonus_pct + employee_fidelity_pct) / 100
    profit   = revenue − employee_wage

and a per-company and grand total summary.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap(resp: object) -> object:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _unwrap_list(resp: object) -> list[dict]:
    """Unwrap an API response that should contain a list of dicts."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        result = resp.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        for key in ("data", "items", "regions", "result"):
            v = resp.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _fmt_cc(value: float) -> str:
    """Format a currency value with 3 decimal places and a CC suffix."""
    return f"{value:,.3f} CC"


def _production_multiplier(raw: object) -> float:
    """Normalise a production-bonus value to a plain multiplier (e.g. 105 → 1.05, 1.05 → 1.05).

    The API can return either a percentage integer (105 = 5 % above base) or
    a plain multiplier (1.05).  Values > 10 are treated as percentages.
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return v / 100.0 if v > 10 else v


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class BedrijfswinstCog(CommandCogBase, name="bedrijfswinst"):
    """Employee profit analyser per company."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Internal API helpers
    # ------------------------------------------------------------------

    async def _search_user(self, username: str) -> list[str]:
        """Return up to 5 candidate user IDs for *username*."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return ids[:5]
        except Exception as exc:
            logger.warning("bedrijfswinst: user search failed for %r: %s", username, exc)
            return []

    async def _get_user_profile(self, user_id: str) -> Optional[dict]:
        try:
            raw = await self._client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            data = _unwrap(raw) if isinstance(raw, dict) else None
            return data
        except Exception as exc:
            logger.warning("bedrijfswinst: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(
        self, query: str
    ) -> tuple[Optional[str], Optional[dict]]:
        """Resolve *query* → (user_id, profile).

        Strategy: exact username match first, then closest candidate by ratio.
        Falls back to a local DB fuzzy search if the API finds nothing.
        """
        s_low = query.lower().strip()
        user_ids = await self._search_user(query)

        # API returned nothing — try fuzzy match in local citizen cache
        if not user_ids:
            db = self._db
            if db is not None:
                nl_country_id = (self.config.get("nl_country_id") or
                                 self.config.get("country_id"))
                try:
                    match = await db.fuzzy_citizen_by_name(query, country_id=nl_country_id)
                    if match:
                        uid, _ = match
                        profile = await self._get_user_profile(uid)
                        if profile is not None:
                            return uid, profile
                except Exception:
                    pass
            return None, None

        candidates: list[tuple[str, dict]] = []
        for uid in user_ids:
            profile = await self._get_user_profile(uid)
            if profile is not None:
                candidates.append((uid, profile))

        # Exact match wins
        for uid, profile in candidates:
            if (profile.get("username") or "").lower().strip() == s_low:
                return uid, profile

        # Closest ratio
        best_uid, best_profile, best_ratio = None, None, -1.0
        for uid, profile in candidates:
            ratio = difflib.SequenceMatcher(
                None, s_low, (profile.get("username") or "").lower().strip()
            ).ratio()
            if ratio > best_ratio:
                best_ratio, best_uid, best_profile = ratio, uid, profile
        return best_uid, best_profile

    async def _get_company_ids(self, user_id: str) -> list[str]:
        """Return all company IDs owned by *user_id* (getCompanies returns IDs only)."""
        ids: list[str] = []
        cursor: Optional[str] = None
        while True:
            payload: dict = {"userId": user_id, "perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.get(
                    "/company.getCompanies",
                    params={"input": json.dumps(payload)},
                )
                data = _unwrap(raw)
            except Exception as exc:
                logger.warning("bedrijfswinst: getCompanies failed: %s", exc)
                break

            if not isinstance(data, dict):
                break

            items = data.get("items") or []
            if not isinstance(items, list):
                break
            ids.extend(str(i) for i in items if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(0)
        return ids

    async def _get_company_details(self, company_ids: list[str]) -> list[Optional[dict]]:
        """Fetch full company objects for *company_ids* using tRPC batching.

        Returns a list of the same length; entries are ``None`` for failed lookups.
        """
        inputs = [{"companyId": cid} for cid in company_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("company.getById", inputs),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning("bedrijfswinst: batch_get timed out — returning all None")
            return [None] * len(company_ids)
        except Exception as exc:
            logger.warning("bedrijfswinst: batch_get failed: %s", exc)
            return [None] * len(company_ids)

        out: list[Optional[dict]] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            out.append(data if isinstance(data, dict) else None)
        return out

    async def _get_item_prices(self) -> dict[str, float]:
        """Return a mapping of item_code → best sell price from the market."""
        try:
            raw = await asyncio.wait_for(
                self._client.get(
                    "/itemTrading.getPrices",
                    params={"input": "{}"},
                ),
                timeout=15.0,
            )
            data = _unwrap(raw)
        except TimeoutError:
            logger.warning("bedrijfswinst: getPrices timed out after 15s — returning empty prices")
            return {}
        except Exception as exc:
            logger.warning("bedrijfswinst: getPrices failed: %s", exc)
            return {}

        prices: dict[str, float] = {}
        if isinstance(data, dict):
            for code, info in data.items():
                if isinstance(info, dict):
                    # take the best sell (lowest ask) price
                    price = info.get("sell") or info.get("price") or info.get("value") or 0
                else:
                    price = info
                try:
                    prices[code] = float(price)
                except (TypeError, ValueError):
                    pass
        elif isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict):
                    code = entry.get("code") or entry.get("itemCode") or ""
                    price = (
                        entry.get("sell")
                        or entry.get("price")
                        or entry.get("value")
                        or 0
                    )
                    if code:
                        try:
                            prices[code] = float(price)
                        except (TypeError, ValueError):
                            pass
        return prices

    async def _get_production_points(
        self,
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Return (prod_points, prod_needs) from the game config.

        prod_points: {item_code: productionPoints}  — production-action points
            needed to make one unit (e.g. steel=10, iron=1).
        prod_needs:  {item_code: {input_item: qty}} — raw materials consumed per
            unit produced (e.g. steel={"iron": 10.0}).

        Both dicts fall back to safe defaults on failure.
        """
        try:
            raw = await asyncio.wait_for(
                self._client.get("/gameConfig.getGameConfig", params={"input": "{}"}),
                timeout=10.0,
            )
            data = _unwrap(raw)
            items_cfg = (data or {}).get("items", {}) if isinstance(data, dict) else {}
            prod_points: dict[str, float] = {}
            prod_needs: dict[str, dict[str, float]] = {}
            for code, info in items_cfg.items():
                if not isinstance(info, dict):
                    continue
                try:
                    pp = float(info.get("productionPoints") or 1)
                    prod_points[code] = pp if pp > 0 else 1.0
                except (TypeError, ValueError):
                    prod_points[code] = 1.0
                raw_needs = info.get("productionNeeds") or {}
                if isinstance(raw_needs, dict) and raw_needs:
                    prod_needs[code] = {mat: float(qty) for mat, qty in raw_needs.items()}
            return prod_points, prod_needs
        except Exception as exc:
            logger.warning("bedrijfswinst: getGameConfig failed: %s — using defaults", exc)
            return {}, {}

    async def _get_country_bonus_map(self) -> dict[str, dict[str, float]]:
        """Return {country_id: {"sr_pct": float, "ethics_pct": float}} from getAllCountries.

        getAllCountries is the only endpoint that exposes rankings.countryProductionBonus
        (= SR + ethics combined), so ethics = total − SR can be derived here.
        """
        try:
            raw = await asyncio.wait_for(
                self._client.get("/country.getAllCountries"),
                timeout=12.0,
            )
            # Response may be a plain list or wrapped
            countries: list = []
            if isinstance(raw, list):
                countries = raw
            elif isinstance(raw, dict):
                for key in ("result", "data", "countries"):
                    v = raw.get(key)
                    if isinstance(v, list):
                        countries = v
                        break
                if not countries:
                    nested = (raw.get("result") or {})
                    if isinstance(nested, dict):
                        v = nested.get("data")
                        if isinstance(v, list):
                            countries = v
            result: dict[str, dict[str, float]] = {}
            for c in countries:
                if not isinstance(c, dict):
                    continue
                cid = c.get("_id") or c.get("id")
                if not cid:
                    continue
                sr_bonuses = (c.get("strategicResources") or {}).get("bonuses") or {}
                sr_pct = float(sr_bonuses.get("productionPercent") or 0)
                rb = (c.get("rankings") or {}).get("countryProductionBonus")
                total_pct = float(rb.get("value") or 0) if isinstance(rb, dict) else 0.0
                ethics_pct = max(0.0, total_pct - sr_pct)
                result[cid] = {"sr_pct": sr_pct, "ethics_pct": ethics_pct}
            return result
        except Exception as exc:
            logger.warning("bedrijfswinst: getAllCountries failed: %s", exc)
            return {}

    async def _get_spec_tops(self) -> dict[str, dict]:
        """Return {item: row} from the specialization_top DB table.

        Each row contains country_id, country_name, strategic_bonus, ethic_bonus etc.
        Used as a reliable fallback for ethics when the company's region is not in the
        dynamic recommended top-5 (e.g. Turkey steel pushed out by deposit-boosted regions).
        """
        if not self._db:
            return {}
        try:
            rows = await self._db.get_all_tops()
            return {row["item"]: row for row in rows if row.get("item")}
        except Exception as exc:
            logger.warning("bedrijfswinst: get_all_tops failed: %s", exc)
            return {}

    async def _get_item_country_ethics(self) -> dict[tuple[str, str], float]:
        """Return {(item, country_id): ethic_bonus} from the country_item_ethic DB table.

        The production poller records ethics for every country observed in each item's
        recommended-region list (not just the leader), giving us coverage for non-leader
        countries that would otherwise be invisible to /bedrijfswinst.
        """
        if not self._db:
            return {}
        try:
            rows = await self._db.get_all_country_item_ethics()
            return {
                (row["item"], row["country_id"]): float(row["ethic_bonus"] or 0)
                for row in rows
                if row.get("item") and row.get("country_id")
            }
        except Exception as exc:
            logger.warning("bedrijfswinst: get_all_country_item_ethics failed: %s", exc)
            return {}

    async def _get_country_spec_map(self) -> dict[str, str]:
        """Return {country_id: specialized_item} from country_snapshots.

        Used to identify countries that specialize in a given item but whose regions
        never appear in the recommended top-5 (so ethics can't be read directly from
        the recommended list or the country_item_ethic table).
        """
        if not self._db:
            return {}
        try:
            return await self._db.get_country_spec_map()
        except Exception as exc:
            logger.warning("bedrijfswinst: get_country_spec_map failed: %s", exc)
            return {}

    async def _get_workers_for_company(self, company_id: str) -> list[dict]:
        """Fetch current workers for a company via worker.getWorkers."""
        if not company_id:
            return []
        try:
            raw = await asyncio.wait_for(
                self._client.get(
                    "/worker.getWorkers",
                    params={"input": json.dumps({"companyId": company_id})},
                ),
                timeout=10.0,
            )
            data = _unwrap(raw)
            if isinstance(data, dict):
                workers = data.get("workers") or []
                return workers if isinstance(workers, list) else []
            return []
        except Exception as exc:
            logger.warning("bedrijfswinst: worker.getWorkers failed for %s: %s", company_id, exc)
            return []

    @staticmethod
    def _extract_eco_skill(data: dict, skill_name: str) -> float:
        """Extract the level/value of a named eco skill from a getUserLite response.

        The ``skills`` field can be a dict-of-dicts, a flat dict, or a list-of-dicts;
        mirrors the logic in CitizenCache._extract_skill_mode.
        Returns 0.0 when the skill is not found.
        """
        skills = data.get("skills")
        if skills is None:
            return 0.0
        def _pick(entry: dict) -> float:
            # prefer "level" but don't skip it when it is 0 (falsy)
            v = entry.get("level")
            if v is None:
                v = entry.get("value")
            return float(v) if v is not None else 0.0

        if isinstance(skills, dict):
            entry = skills.get(skill_name)
            if isinstance(entry, dict):
                return _pick(entry)
            if isinstance(entry, (int, float)):
                return float(entry)
        elif isinstance(skills, list):
            for entry in skills:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or entry.get("skill") or entry.get("type")
                if name == skill_name:
                    return _pick(entry)
        return 0.0

    async def _get_production_bonus(self, company: dict, item_code: str = "") -> dict:
        """Fetch the combined production bonus for a company from its region + country.

        Deposit bonus  : region.getById → deposit.bonusPercent (when type == item_code).
        SR + ethics    : first tries getRecommendedRegionIdsByItemCode (exact regionId match);
                         if the company's region is not in that short list, falls back to
                         country.getCountryById where:
                           SR     = strategicResources.bonuses.productionPercent
                           ethics = rankings.countryProductionBonus.value − SR  (≥ 0)

        Returns {"region_pct", "country_pct", "ethics_pct", "total_mult"}.
        """
        region_id = (
            company.get("region")
            or company.get("regionId")
            or company.get("region_id")
        )
        region_pct = 0.0
        country_pct = 0.0
        ethics_pct = 0.0
        country_id: str | None = None

        # ── Step 1: deposit bonus + country_id from region ──────────────────
        if region_id and isinstance(region_id, str):
            try:
                raw = await asyncio.wait_for(
                    self._client.get(
                        "/region.getById",
                        params={"input": json.dumps({"regionId": region_id})},
                    ),
                    timeout=8.0,
                )
                data = _unwrap(raw)
                if isinstance(data, dict):
                    deposit = data.get("deposit") or {}
                    deposit_type = deposit.get("type", "")
                    deposit_pct = float(deposit.get("bonusPercent") or 0)
                    if item_code and deposit_type == item_code and deposit_pct:
                        region_pct = deposit_pct
                    raw_country = data.get("country")
                    if isinstance(raw_country, dict):
                        country_id = raw_country.get("_id") or raw_country.get("id")
                    elif isinstance(raw_country, str):
                        country_id = raw_country
            except Exception as exc:
                logger.warning("bedrijfswinst: region lookup failed for %s: %s", region_id, exc)

        # ── Step 2: SR + ethics via recommended-region list (exact match) ───
        found_in_recommended = False
        recommended_entries: list[dict] = []
        if item_code and region_id and isinstance(region_id, str):
            try:
                raw = await asyncio.wait_for(
                    self._client.get(
                        "/company.getRecommendedRegionIdsByItemCode",
                        params={"input": json.dumps({"itemCode": item_code, "count": 100})},
                    ),
                    timeout=8.0,
                )
                region_list = _unwrap_list(raw)
                for entry in region_list:
                    if entry.get("regionId") == region_id:
                        country_pct = float(entry.get("strategicBonus") or 0)
                        ethics_pct = float(entry.get("ethicSpecializationBonus") or 0)
                        found_in_recommended = True
                        break
                if not found_in_recommended:
                    # Try with the specific regionId — some API versions force-include it in the list.
                    try:
                        raw2 = await asyncio.wait_for(
                            self._client.get(
                                "/company.getRecommendedRegionIdsByItemCode",
                                params={"input": json.dumps({"itemCode": item_code, "regionId": region_id})},
                            ),
                            timeout=8.0,
                        )
                        region_list2 = _unwrap_list(raw2)
                        for entry in region_list2:
                            if entry.get("regionId") == region_id:
                                country_pct = float(entry.get("strategicBonus") or 0)
                                ethics_pct = float(entry.get("ethicSpecializationBonus") or 0)
                                found_in_recommended = True
                                break
                        # Merge extra entries into recommended_entries for SR-matching
                        seen = {e.get("regionId") for e in region_list}
                        region_list.extend(e for e in region_list2 if e.get("regionId") not in seen)
                    except Exception:
                        pass

                    # Third variant: countryId param — may return all regions of this country
                    if not found_in_recommended and country_id:
                        try:
                            raw3 = await asyncio.wait_for(
                                self._client.get(
                                    "/company.getRecommendedRegionIdsByItemCode",
                                    params={"input": json.dumps({"itemCode": item_code, "countryId": country_id})},
                                ),
                                timeout=8.0,
                            )
                            region_list3 = _unwrap_list(raw3)
                            for entry in region_list3:
                                if entry.get("regionId") == region_id:
                                    country_pct = float(entry.get("strategicBonus") or 0)
                                    ethics_pct = float(entry.get("ethicSpecializationBonus") or 0)
                                    found_in_recommended = True
                                    break
                            seen2 = {e.get("regionId") for e in region_list}
                            region_list.extend(e for e in region_list3 if e.get("regionId") not in seen2)
                        except Exception:
                            pass

                # Always save the full list so post-processing can match by SR value
                recommended_entries = region_list
            except Exception as exc:
                logger.warning("bedrijfswinst: recommended list failed for %s/%s: %s", item_code, region_id, exc)

        # ── Step 3: fallback — country.getCountryById for SR + ethics ───────
        if not found_in_recommended and country_id and isinstance(country_id, str):
            try:
                raw = await asyncio.wait_for(
                    self._client.get(
                        "/country.getCountryById",
                        params={"input": json.dumps({"countryId": country_id})},
                    ),
                    timeout=8.0,
                )
                data = _unwrap(raw)
                if isinstance(data, dict):
                    sr_bonuses = (data.get("strategicResources") or {}).get("bonuses") or {}
                    country_pct = float(sr_bonuses.get("productionPercent") or 0)
                    rb = (data.get("rankings") or {}).get("countryProductionBonus")
                    total_country = float(rb.get("value") or 0) if isinstance(rb, dict) else 0.0
                    ethics_pct = max(0.0, total_country - country_pct)
            except Exception as exc:
                logger.warning("bedrijfswinst: country fallback failed for %s: %s", country_id, exc)

        total_pct = region_pct + country_pct + ethics_pct
        return {
            "region_pct": region_pct,
            "country_pct": country_pct,
            "ethics_pct": ethics_pct,
            "total_mult": 1.0 + total_pct / 100.0,
            "country_id": country_id,
            "_recommended_entries": recommended_entries,
        }

    async def _get_worker_profiles(
        self, user_ids: list[str]
    ) -> dict[str, dict]:
        """Return a {user_id: {username, energy, production}} mapping via batch getUserLite."""
        if not user_ids:
            return {}
        inputs = [{"userId": uid} for uid in user_ids]
        try:
            results = await asyncio.wait_for(
                self._client.batch_get("user.getUserLite", inputs),
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning("bedrijfswinst: batch getUserLite failed: %s", exc)
            return {}
        out: dict[str, dict] = {}
        for uid, raw in zip(user_ids, results):
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                out[uid] = {
                    "username": data.get("username") or uid,
                    "energy": self._extract_eco_skill(data, "energy"),
                    "production": self._extract_eco_skill(data, "production"),
                }
        return out

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bedrijfswinst",
        description="Toon de winstgevendheid van werknemers per bedrijf van een speler.",
    )
    @app_commands.describe(
        speler="WarEra-gebruikersnaam van de speler (leeg = jijzelf)",
    )
    async def bedrijfswinst(
        self,
        interaction: discord.Interaction,
        speler: Optional[str] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if not self._client:
            await interaction.followup.send(
                "❌ API client niet geïnitialiseerd.", ephemeral=True
            )
            return

        # Resolve player ------------------------------------------------
        query = speler or interaction.user.display_name
        user_id, profile = await self._resolve_user(query)

        if user_id is None or profile is None:
            await interaction.followup.send(
                f"❌ Speler **{discord.utils.escape_markdown(query)}** niet gevonden.",
                ephemeral=True,
            )
            return

        username: str = profile.get("username") or query
        avatar_url: str = profile.get("avatarUrl") or ""

        company_ids = await self._get_company_ids(user_id)

        if not company_ids:
            await interaction.followup.send(
                f"**{discord.utils.escape_markdown(username)}** heeft geen bedrijven.",
                ephemeral=True,
            )
            return

        company_details = await self._get_company_details(company_ids)
        companies = [c for c in company_details if c is not None]

        if not companies:
            await interaction.followup.send(
                f"**{discord.utils.escape_markdown(username)}** heeft geen bedrijven (gegevens niet beschikbaar).",
                ephemeral=True,
            )
            return

        workers_lists, prod_bonuses_list, (item_prices, (prod_points, prod_needs), country_bonus_map, spec_top_map, item_country_ethics, country_spec_map) = await asyncio.gather(
            asyncio.gather(*[
                self._get_workers_for_company(c.get("_id", ""))
                for c in companies
            ]),
            asyncio.gather(*[
                self._get_production_bonus(c, c.get("itemCode") or "")
                for c in companies
            ]),
            asyncio.gather(
                self._get_item_prices(),
                self._get_production_points(),
                self._get_country_bonus_map(),
                self._get_spec_tops(),
                self._get_item_country_ethics(),
                self._get_country_spec_map(),
            ),
        )
        workers_by_id: dict[str, list[dict]] = {
            c.get("_id", ""): w for c, w in zip(companies, workers_lists)
        }
        prod_bonus_by_id: dict[str, dict] = {
            c.get("_id", ""): b for c, b in zip(companies, prod_bonuses_list)
        }
        # Build a {item_code → {sr_pct → ethics_pct}} map from all recommended-list entries.
        # ethicSpecializationBonus is country-level, so any region in the same country has the
        # same value.  When the company's exact region isn't in the top-5, we can still find
        # its ethics by matching strategicBonus (= country SR, unique per country) to an entry
        # from the same country that happens to appear in the list.
        sr_ethics_by_item: dict[str, dict[float, float]] = {}
        for c, bonus in zip(companies, prod_bonuses_list):
            item = c.get("itemCode") or ""
            if not item:
                continue
            for entry in bonus.get("_recommended_entries", []):
                sr = float(entry.get("strategicBonus") or 0)
                eth = float(entry.get("ethicSpecializationBonus") or 0)
                if sr > 0 and eth > 0:
                    sr_ethics_by_item.setdefault(item, {})[sr] = eth

        # Derive per-item ethics value from whichever countries ARE in the DB.
        # e.g. lead → 30 because Nigeria appears for lead with ethics=30.
        # All countries that pass the same ethics law for an item get the same %.
        item_ethics_map: dict[str, float] = {}
        for (item_k, _), eth in item_country_ethics.items():
            if eth > item_ethics_map.get(item_k, 0.0):
                item_ethics_map[item_k] = eth

        # Post-apply SR and ethics for companies whose region wasn't in the recommended top-5.
        for c, bonus in zip(companies, prod_bonuses_list):
            if bonus.get("ethics_pct", 0.0) != 0.0:
                continue  # already resolved via exact region match
            c_id = bonus.get("country_id")
            item = c.get("itemCode") or ""
            country_sr = country_bonus_map.get(c_id, {}).get("sr_pct", bonus.get("country_pct", 0.0))
            # Try SR-value match in the recommended entries for this item
            ethics = sr_ethics_by_item.get(item, {}).get(country_sr, 0.0)
            if ethics > 0:
                bonus["ethics_pct"] = ethics
                bonus["country_pct"] = country_sr
                bonus["total_mult"] = 1.0 + (
                    bonus["region_pct"] + country_sr + ethics
                ) / 100.0
            elif item and spec_top_map.get(item, {}).get("country_id") == c_id:
                # DB fallback: specialization_top stores the permanent leader per item,
                # so if this company's country is that leader, we have reliable ethics.
                top_row = spec_top_map[item]
                db_sr = float(top_row.get("strategic_bonus") or 0)
                db_ethics = float(top_row.get("ethic_bonus") or 0)
                bonus["ethics_pct"] = db_ethics
                bonus["country_pct"] = db_sr
                bonus["total_mult"] = 1.0 + (
                    bonus["region_pct"] + db_sr + db_ethics
                ) / 100.0
            elif item and c_id and (item, c_id) in item_country_ethics:
                # Broader DB fallback: country_item_ethic stores ethics for ALL countries
                # observed in recommended lists (not just the leader), so non-leaders with
                # ethics are covered too.
                db_ethics = item_country_ethics[(item, c_id)]
                country_sr = country_bonus_map.get(c_id, {}).get("sr_pct", bonus.get("country_pct", 0.0))
                bonus["ethics_pct"] = db_ethics
                bonus["country_pct"] = country_sr
                bonus["total_mult"] = 1.0 + (
                    bonus["region_pct"] + country_sr + db_ethics
                ) / 100.0
            elif item and c_id and country_spec_map.get(c_id) == item and item_ethics_map.get(item, 0.0) > 0:
                # Country is specialized in this item but never appeared in the top-5
                # recommended list (too low SR rank).  All countries that pass the same
                # ethics law for an item get the same bonus %, so we infer it from the
                # value we observed for other countries (e.g. Nigeria → lead 30%).
                spec_ethics = item_ethics_map[item]
                country_sr = country_bonus_map.get(c_id, {}).get("sr_pct", bonus.get("country_pct", 0.0))
                bonus["ethics_pct"] = spec_ethics
                bonus["country_pct"] = country_sr
                bonus["total_mult"] = 1.0 + (
                    bonus["region_pct"] + country_sr + spec_ethics
                ) / 100.0
            elif c_id and c_id in country_bonus_map:
                # SR known from getAllCountries but ethics unavailable for this item
                cmap = country_bonus_map[c_id]
                bonus["country_pct"] = cmap["sr_pct"]
                bonus["total_mult"] = 1.0 + (
                    bonus["region_pct"] + cmap["sr_pct"]
                ) / 100.0

        all_worker_ids = list({
            emp.get("user")
            for workers in workers_by_id.values()
            for emp in workers
            if emp.get("user")
        })
        worker_profiles = await self._get_worker_profiles(all_worker_ids)

        # Build embeds
        try:
            colour = self._embed_colour()
        except Exception:
            colour = discord.Colour.gold()

        total_revenue_all = 0.0
        total_mat_cost_all = 0.0
        total_wage_all = 0.0
        total_profit_day_all = 0.0
        embeds: list[discord.Embed] = []

        try:
            # {company_name: (item_code, breakeven_f0, breakeven_f10)}
            breakeven_overview: list[tuple[str, str, float, float]] = []

            def _fmt_t(v: float) -> str:
                """Format a CC value for table display — no 'CC' suffix."""
                return f"{v:,.3f}"

            for company in companies:
                company_id_co = company.get("_id", "")
                company_name: str = company.get("name") or "Onbekend bedrijf"
                item_code: str = company.get("itemCode") or ""
                sell_price: float = item_prices.get(item_code, 0.0)
                # pts of work required to produce one item; raw materials consumed per item
                pp_pts: float = prod_points.get(item_code) or 1.0
                item_mat_needs: dict[str, float] = prod_needs.get(item_code, {})
                # Production bonuses from region + country (ethics / strategic resources)
                _pb = prod_bonus_by_id.get(company_id_co, {"region_pct": 0.0, "country_pct": 0.0, "ethics_pct": 0.0, "total_mult": 1.0})
                region_pct: float = _pb["region_pct"]
                country_pct: float = _pb["country_pct"]
                ethics_pct: float = _pb.get("ethics_pct", 0.0)
                prod_bonus_mult: float = _pb["total_mult"]
                workers: list[dict] = workers_by_id.get(company_id_co, [])

                if not workers:
                    # Still compute breakeven for the overview embed
                    _bk_items_pre = 1.0 / pp_pts
                    _bk_mat_pre   = sum(
                        float(qty) * item_prices.get(mat, 0.0)
                        for mat, qty in item_mat_needs.items()
                    )
                    _bk_margin_pre = sell_price - _bk_mat_pre
                    breakeven_overview.append((
                        company_name, item_code,
                        _bk_items_pre * (prod_bonus_mult + 0.00) * _bk_margin_pre,
                        _bk_items_pre * (prod_bonus_mult + 0.10) * _bk_margin_pre,
                    ))
                    continue

                embed = discord.Embed(
                    title=f"Bedrijf: {discord.utils.escape_markdown(company_name)}",
                    colour=colour,
                )
                bonus_footer_parts: list[str] = []
                if region_pct:
                    bonus_footer_parts.append(f"+{region_pct:.2f}% deposit")
                if country_pct:
                    bonus_footer_parts.append(f"+{country_pct:.2f}% SR")
                if ethics_pct:
                    bonus_footer_parts.append(f"+{ethics_pct:.2f}% ethiek")
                total_bonus_pct = region_pct + country_pct + ethics_pct
                bonus_footer = f"  |  Productiebonus: +{total_bonus_pct:.2f}%" if total_bonus_pct else ""
                pp_footer = f"  |  {pp_pts:.0f} PP/item"
                if item_mat_needs:
                    _mat_parts = []
                    _mat_total = 0.0
                    for mat, qty in item_mat_needs.items():
                        _unit = item_prices.get(mat, 0.0)
                        _cost = float(qty) * _unit
                        _mat_total += _cost
                        _mat_parts.append(f"{float(qty):.0f}× {mat} ({_fmt_cc(_unit)})")
                    mat_footer = (
                        "  |  Grondstoffen/item: "
                        + ", ".join(_mat_parts)
                        + f"  =  {_fmt_cc(_mat_total)}"
                    )
                else:
                    mat_footer = ""
                embed.set_footer(
                    text=f"Item: {item_code}  |  Marktprijs: {_fmt_cc(sell_price)}{bonus_footer}{pp_footer}{mat_footer}"
                )

                total_revenue_co = 0.0
                total_mat_cost_co = 0.0
                total_wage_co = 0.0
                total_profit_day_co = 0.0
                # (indicator, name, fidelity_int, wage_rate, profit_per_pp, profit_per_day)
                rows: list[tuple] = []

                for emp in workers:
                    emp_uid = emp.get("user") or ""
                    profile = worker_profiles.get(emp_uid) or {}
                    emp_name: str = profile.get("username") or emp_uid or "?"
                    fidelity = float(emp.get("fidelity") or 0)
                    # fidelity % and production % are additive, not multiplicative
                    combined_mult = prod_bonus_mult + fidelity * 0.01

                    # Employee skills
                    # The API may return the upgrade level (0-10) OR the computed value
                    # (PP total / energy pool) directly.  Values > 10 are the computed result.
                    raw_prod = float(profile.get("production") or emp.get("production") or 0)
                    emp_pp = raw_prod if raw_prod > 10 else 10.0 + raw_prod * 3.0

                    # energy: base pool 30 + 10/upgrade; max level 10 → pool 130
                    # If API returns pool directly (e.g. 30 for 0 upgrades), raw > 10 is false
                    # for base case — so we special-case: multiples of 10 in range [30,130]
                    # are almost certainly the pool value.
                    raw_energy = float(profile.get("energy") or emp.get("energy") or 0)
                    if raw_energy > 10:
                        emp_energy_pool = raw_energy  # already the pool
                    else:
                        emp_energy_pool = 30.0 + raw_energy * 10.0  # convert level to pool
                    actions_per_day = emp_energy_pool * 0.24

                    # Total PP the employee delivers per day
                    total_pp_day = emp_pp * actions_per_day

                    # items produced per PP — base rate times combined bonus (additive pcts)
                    items_per_pp = 1.0 / pp_pts
                    items_produced_per_pp = items_per_pp * combined_mult

                    # Gross revenue per PP (before material costs)
                    gross_rev_per_pp = items_produced_per_pp * sell_price

                    # Raw material cost per PP scales with production (more items = more mats)
                    mat_cost_per_pp = items_produced_per_pp * sum(
                        float(qty) * item_prices.get(mat, 0.0)
                        for mat, qty in item_mat_needs.items()
                    )

                    # Net revenue per PP (after material costs)
                    revenue_per_pp = gross_rev_per_pp - mat_cost_per_pp

                    revenue = revenue_per_pp * total_pp_day

                    # Salary per day: wage (CC/PP) × employee PP/dag
                    wage_rate = float(emp.get("wage") or 0.0)
                    wage_cost = wage_rate * total_pp_day

                    profit_per_pp = revenue_per_pp - wage_rate
                    profit_per_day = profit_per_pp * total_pp_day
                    total_revenue_co += revenue
                    total_mat_cost_co += mat_cost_per_pp * total_pp_day
                    total_wage_co += wage_cost
                    total_profit_day_co += profit_per_day

                    indicator = "+" if profit_per_day >= 0 else "-"
                    rows.append((indicator, emp_name, int(fidelity), wage_rate, profit_per_pp, profit_per_day))

                total_revenue_all += total_revenue_co
                total_mat_cost_all += total_mat_cost_co
                total_wage_all += total_wage_co
                total_profit_day_all += total_profit_day_co

                # Breakeven wage: max CC/PP at which a hire is still profitable.
                # Computed for fidelity 0 and 10.  Merged into the employee table.
                _bk_items_per_pp = 1.0 / pp_pts
                _bk_mat_per_item = sum(
                    float(qty) * item_prices.get(mat, 0.0)
                    for mat, qty in item_mat_needs.items()
                )
                # margin per item (sell - materials); production bonus scales both equally
                _bk_margin_per_item = sell_price - _bk_mat_per_item
                breakeven_f0  = _bk_items_per_pp * (prod_bonus_mult + 0.00) * _bk_margin_per_item
                breakeven_f10 = _bk_items_per_pp * (prod_bonus_mult + 0.10) * _bk_margin_per_item

                breakeven_overview.append((
                    company_name, item_code,
                    breakeven_f0, breakeven_f10,
                ))

                # Build a combined aligned table: employees + breakeven section
                _NAME_MAX  = 14
                _ANSI_GRN  = "\u001b[0;32m"
                _ANSI_RED  = "\u001b[0;31m"
                _ANSI_RST  = "\u001b[0m"

                def _trunc(s: str) -> str:
                    return s if len(s) <= _NAME_MAX else s[:_NAME_MAX - 1] + "…"

                def _ansi_val(s: str, val: float) -> str:
                    c = _ANSI_GRN if val >= 0 else _ANSI_RED
                    return f"{c}{s}{_ANSI_RST}"

                # Each row: (ind, name, fid, wage_str, pp_val, pp_str, day_val, day_str)
                emp_rows_fmt = [
                    (r[0], _trunc(r[1]), r[2], _fmt_t(r[3]), r[4], _fmt_t(r[4]), r[5], _fmt_t(r[5]))
                    for r in rows
                ]
                name_w = max((len(r[1]) for r in emp_rows_fmt), default=9)
                wage_w = max((len(r[3]) for r in emp_rows_fmt), default=5)
                pp_w   = max((len(r[5]) for r in emp_rows_fmt), default=1) if emp_rows_fmt else 1
                day_w  = max((len(r[7]) for r in emp_rows_fmt), default=1) if emp_rows_fmt else 1
                # header — units shown here (CC implied), not in cells
                _W_wage = max(wage_w, 7)   # "loon/PP" = 7
                _W_pp   = max(pp_w,   8)   # "winst/PP" = 8
                _W_day  = max(day_w,  9)   # "winst/dag" = 9
                hdr = (
                    f"  {'Naam':<{name_w}}  fid"
                    f" {'loon/PP':>{_W_wage}}"
                    f" {'winst/PP':>{_W_pp}}"
                    f" {'winst/dag':>{_W_day}}"
                )
                sep_w = len(hdr)
                sep   = "─" * sep_w

                def _trow(ind, name, fid, wage_s, pp_val, pp_s, day_val, day_s):
                    pp_padded  = f"{pp_s:>{_W_pp}}"
                    day_padded = f"{day_s:>{_W_day}}"
                    if pp_val is not None:
                        pp_padded  = _ansi_val(pp_padded, pp_val)
                    if day_val is not None:
                        day_padded = _ansi_val(day_padded, day_val)
                    return (
                        f"{ind} {name:<{name_w}} {fid:>2}/10"
                        f" {wage_s:>{_W_wage}}"
                        f" {pp_padded}"
                        f" {day_padded}"
                    )

                n_emp       = len(emp_rows_fmt)
                table_lines = [hdr, sep]
                if emp_rows_fmt:
                    table_lines += [_trow(*r) for r in emp_rows_fmt]
                else:
                    table_lines.append(f"  {'Geen werknemers':<{sep_w - 2}}")
                # Chunk into fields of ≤15 lines
                chunk_size = 15
                chunks = [table_lines[i:i + chunk_size] for i in range(0, len(table_lines), chunk_size)]
                n_chunks = len(chunks)
                base_name = f"Werknemers ({n_emp})"
                for chunk_idx, chunk in enumerate(chunks):
                    field_name = base_name if n_chunks == 1 else f"{base_name} (deel {chunk_idx + 1})"
                    embed.add_field(
                        name=field_name,
                        value="```ansi\n" + "\n".join(chunk) + "\n```",
                        inline=False,
                    )

                embeds.append(embed)

            if not embeds and not breakeven_overview:
                await interaction.followup.send(
                    f"**{discord.utils.escape_markdown(username)}** heeft geen bedrijven.",
                    ephemeral=True,
                )
                return

            # Grand-total summary — only shown when there are companies with workers
            all_embeds: list[discord.Embed] = []
            if embeds:
                companies_with_workers = sum(1 for c in companies if workers_by_id.get(c.get("_id", "")))
                _S_GRN = "\u001b[0;32m"
                _S_RED = "\u001b[0;31m"
                _S_RST = "\u001b[0m"
                rev_s    = _fmt_cc(total_revenue_all + total_mat_cost_all)
                mat_s    = _fmt_cc(total_mat_cost_all)
                wage_s   = _fmt_cc(total_wage_all)
                profit_s = _fmt_cc(total_profit_day_all)
                _lbl_w   = len("Grondstofkosten/dag:")  # longest label = 20
                _val_w   = max(len(rev_s), len(mat_s), len(wage_s), len(profit_s))
                _sep     = "─" * (_lbl_w + 1 + _val_w)
                _p_color = _S_GRN if total_profit_day_all >= 0 else _S_RED
                _profit_pfx = "+" if total_profit_day_all >= 0 else "-"
                _profit_row = f"{_profit_pfx} {'Netto winst/dag:':<{_lbl_w - 2}} {profit_s:>{_val_w}}"
                summary_table = "\n".join([
                    f"{'Totale omzet/dag:':<{_lbl_w}} {rev_s:>{_val_w}}",
                    f"{'Grondstofkosten/dag:':<{_lbl_w}} {mat_s:>{_val_w}}",
                    f"{'Loonkosten/dag:':<{_lbl_w}} {wage_s:>{_val_w}}",
                    _sep,
                    f"{_p_color}{_profit_row}{_S_RST}",
                ])
                summary = discord.Embed(
                    title=f"{discord.utils.escape_markdown(username)} — inkomsten werknemers bij bedrijven",
                    description=(
                        f"*Inkomsten van werknemers, automated engine niet meegeteld.*\n\n"
                        f"```ansi\n{summary_table}\n```"
                    ),
                    colour=colour,
                )
                if avatar_url:
                    summary.set_thumbnail(url=avatar_url)
                summary.set_footer(
                    text=f"{companies_with_workers}/{len(companies)} bedrijven hebben werknemers"
                )
                all_embeds = [summary] + embeds
            else:
                no_emp_embed = discord.Embed(
                    title=f"{discord.utils.escape_markdown(username)} — inkomsten werknemers bij bedrijven",
                    description="*Deze speler heeft geen werknemers in dienst.*",
                    colour=colour,
                )
                if avatar_url:
                    no_emp_embed.set_thumbnail(url=avatar_url)
                no_emp_embed.set_footer(text=f"0/{len(companies)} bedrijven hebben werknemers")
                all_embeds = [no_emp_embed]

            # Breakeven overview: one embed listing every company
            if breakeven_overview:
                _B_NAME_MAX = 20
                def _btrunc(s: str) -> str:
                    return s if len(s) <= _B_NAME_MAX else s[:_B_NAME_MAX - 1] + "\u2026"

                bk_name_w = max(len(_btrunc(r[0])) for r in breakeven_overview)
                bk_item_w = max(len(r[1]) for r in breakeven_overview)
                bk_f0_w   = max(len(_fmt_t(r[2])) for r in breakeven_overview)
                bk_f10_w  = max(len(_fmt_t(r[3])) for r in breakeven_overview)
                bk_hdr = (
                    f"  {'Bedrijf':<{bk_name_w}}  {'item':<{bk_item_w}}"
                    f"  {'fid 0/10':>{max(bk_f0_w, 7)}}"
                    f"  {'fid 10/10':>{max(bk_f10_w, 8)}}"
                )
                bk_sep = "\u2500" * len(bk_hdr)
                bk_lines = [bk_hdr, bk_sep]
                for co_name, co_item, bf0, bf10 in breakeven_overview:
                    bk_lines.append(
                        f"  {_btrunc(co_name):<{bk_name_w}}  {co_item:<{bk_item_w}}"
                        f"  {_fmt_t(bf0):>{max(bk_f0_w, 7)}}"
                        f"  {_fmt_t(bf10):>{max(bk_f10_w, 8)}}"
                    )
                bk_embed = discord.Embed(
                    title="Breakeven loon per bedrijf (CC/PP)",
                    description="```\n" + "\n".join(bk_lines) + "\n```",
                    colour=colour,
                )
                all_embeds.append(bk_embed)

            for i in range(0, len(all_embeds), 10):
                await interaction.followup.send(embeds=all_embeds[i : i + 10])

        except Exception as exc:
            logger.exception("bedrijfswinst: unhandled error in embed/send: %s", exc)
            await interaction.followup.send(
                f"❌ Er is een fout opgetreden: `{exc}`",
                ephemeral=True,
            )


async def setup(bot) -> None:
    """Add the bedrijfswinst command cog to the bot."""
    await bot.add_cog(BedrijfswinstCog(bot))
