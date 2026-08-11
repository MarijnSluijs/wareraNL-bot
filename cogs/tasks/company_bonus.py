"""Background task: hourly scan for companies with 0% production bonus, and
for companies that could profitably move to a region with a higher bonus.

For each subscribed Discord user, fetches their in-game companies and:
  1. (Bonus check) Calls ``company.getProductionBonus`` per company.  When the
     total bonus is 0% the user is pinged via DM once per (company, region).
  2. (Move advice) Calls ``company.getRecommendedRegionIdsByItemCode`` per
     company.  When a better region is found and the moving cost
     (``gameConfig.company.moveCost`` concrete) can be earned back in time, the
     user receives a one-time DM with advice.

Move-advice rules:
  • Deposit region  : advise if payback_days ≤ deposit_days_left − 1
  • SR-only region  : advise if payback_days ≤ 7
  • Permanent bonus : advise if payback_days < 7

Payback time uses the *difference* in bonus (new − current), not the absolute
bonus of the target region, to account for any existing bonus the company earns.

Economics
---------
Both sides of the payback ratio are expressed in credits (CC), priced per the
company's *own* item — never via a generic "average production point" proxy::

    items/day   = (ae_level × 24) / productionPoints[item]
    margin/item = price[item] × (1 − tax) − Σ productionNeeds × price[material]
    extra/day   = items/day × (bonus_gain / 100) × margin/item
    move_cost   = gameConfig.company.moveCost × price["concrete"]

The production bonus scales raw-material consumption along with output, so the
gain is a percentage of the *net* margin, not of gross revenue.

Known limitations:
  • Only the automated engine is counted; employee output is ignored, which
    makes the payback estimate conservative (advice fires less often).
  • ``taxPercent`` is only available for candidate regions, so the tax delta on
    the company's *existing* output is not modelled — only tax on the gain.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import NamedTuple

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# How often to run the scan (aligned to whole hours elsewhere; here we just
# run every hour and let the before_loop alignment handle clock-alignment).
_SCAN_INTERVAL_HOURS = 1
# Minimum minutes between scans — prevents re-notification on rapid restarts.
_MIN_SCAN_INTERVAL_MINUTES = 50


def _unwrap(resp: object) -> dict | list | None:
    """Strip a single tRPC result/data envelope."""
    if not isinstance(resp, dict):
        return resp  # type: ignore[return-value]
    inner = resp.get("result", resp)
    if isinstance(inner, dict):
        data = inner.get("data", inner)
        return data  # type: ignore[return-value]
    return inner  # type: ignore[return-value]


def _unwrap_prices(resp: object) -> dict[str, float]:
    """Extract a {item_code: price} dict from an itemTrading.getPrices response."""
    def _from_dict(d: dict) -> dict[str, float]:
        out: dict[str, float] = {}
        for k, v in d.items():
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
        return out

    if isinstance(resp, dict):
        data = (resp.get("result") or {}).get("data") if isinstance(resp.get("result"), dict) else None
        if isinstance(data, dict):
            result = _from_dict(data)
            if result:
                return result
        candidate = _from_dict(resp)
        if candidate:
            return candidate
        for v in resp.values():
            if isinstance(v, list):
                out: dict[str, float] = {}
                for entry in v:
                    if isinstance(entry, dict):
                        code = entry.get("itemCode") or entry.get("item") or entry.get("code")
                        price = entry.get("price") or entry.get("value")
                        if code and price is not None:
                            try:
                                out[code] = float(price)
                            except (TypeError, ValueError):
                                pass
                if out:
                    return out
    if isinstance(resp, list):
        out = {}
        for entry in resp:
            if isinstance(entry, dict):
                code = entry.get("itemCode") or entry.get("item") or entry.get("code")
                price = entry.get("price") or entry.get("value")
                if code and price is not None:
                    try:
                        out[code] = float(price)
                    except (TypeError, ValueError):
                        pass
        return out
    return {}


def _unwrap_region_list(api_response: object) -> list[dict]:
    """Extract the list of region entries from a getRecommendedRegionIdsByItemCode response."""
    if isinstance(api_response, list):
        return [r for r in api_response if isinstance(r, dict)]
    if isinstance(api_response, dict):
        result = api_response.get("result")
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        for key in ("data", "items", "regions"):
            v = api_response.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


class MoveEconomics(NamedTuple):
    """Everything needed to price a company move, fetched once per scan."""

    prices: dict[str, float]                  # {item_code: market price in CC}
    prod_points: dict[str, float]             # {item_code: PP needed per unit}
    prod_needs: dict[str, dict[str, float]]   # {item_code: {material: qty per unit}}
    move_cost: float                          # CC to move one company
    move_cost_units: float                    # concrete units to move one company

    def margin_per_item(self, item_code: str, tax_percent: float = 0.0) -> float:
        """CC earned per unit produced, after raw materials and region tax."""
        sell = self.prices.get(item_code, 0.0)
        if sell <= 0:
            return 0.0
        mat_cost = sum(
            qty * self.prices.get(mat, 0.0)
            for mat, qty in self.prod_needs.get(item_code, {}).items()
        )
        return sell * (1.0 - tax_percent / 100.0) - mat_cost

    def items_per_day(self, item_code: str, ae_level: int) -> float:
        """Base units produced per day by the automated engine (before bonus)."""
        pp_per_item = self.prod_points.get(item_code) or 1.0
        if pp_per_item <= 0:
            return 0.0
        return (ae_level * 24.0) / pp_per_item


class CompanyBonusTasks(TaskCogBase, name="company_bonus_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.company_bonus_scan.start()

    def cog_unload(self) -> None:
        self.company_bonus_scan.cancel()

    @tasks.loop(hours=_SCAN_INTERVAL_HOURS)
    async def company_bonus_scan(self) -> None:
        if not self._client or not self._db:
            return
        try:
            await self._run_scan()
        except Exception:
            logger.exception("company_bonus_scan: unexpected error")

    @company_bonus_scan.before_loop
    async def before_company_bonus_scan(self) -> None:
        await self._wait_for_services()

    # ── Internals ────────────────────────────────────────────────────────────

    async def _resolve_game_user_id(self, game_username: str) -> str | None:
        """Resolve game username → user_id via the search API."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": game_username})},
            )
            data = _unwrap(raw)
            if isinstance(data, dict):
                ids = data.get("userIds") or []
                if ids:
                    return str(ids[0])
        except Exception as exc:
            logger.debug("company_bonus_scan: search failed for %r: %s", game_username, exc)
        return None

    async def _fetch_company_ids_for_user(self, game_user_id: str) -> list[str]:
        """Return all company IDs owned by *game_user_id*."""
        ids: list[str] = []
        cursor: str | None = None
        for _page in range(20):  # safety cap: 20 × 100 = 2000 companies max
            payload: dict = {"userId": game_user_id, "perPage": 100}
            if cursor:
                payload["cursor"] = cursor
            try:
                raw = await self._client.post("/company.getCompanies", json=payload)
                data = _unwrap(raw)
            except Exception as exc:
                logger.debug("company_bonus_scan: getCompanies failed for %s: %s", game_user_id, exc)
                break
            if not isinstance(data, dict):
                break
            page_ids = data.get("items") or []
            ids.extend(str(i) for i in page_ids if i)
            cursor = data.get("nextCursor") or data.get("cursor")
            if not cursor or not page_ids:
                break
        return ids

    async def _fetch_company(self, company_id: str) -> dict | None:
        """Fetch full company details for *company_id*."""
        try:
            raw = await self._client.get(
                "/company.getById",
                params={"input": json.dumps({"companyId": company_id})},
            )
            data = _unwrap(raw)
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("company_bonus_scan: getById failed for %s: %s", company_id, exc)
            return None

    async def _fetch_production_bonus_total(self, company_id: str) -> float | None:
        """Return the total production bonus % for *company_id*, or None on error."""
        try:
            raw = await self._client.get(
                "/company.getProductionBonus",
                params={"input": json.dumps({"companyId": company_id})},
            )
            data = _unwrap(raw)
            if isinstance(data, dict):
                return float(data.get("total") or 0)
        except Exception as exc:
            logger.debug("company_bonus_scan: getProductionBonus failed for %s: %s", company_id, exc)
        return None

    async def _run_scan(self, *, force: bool = False) -> None:
        """Main scan loop — checks all bonus watchers and move-advice watchers.

        Args:
            force: Skip the interval guard and deduplication (for manual/owner runs).
        """
        # Skip if we ran recently (e.g. bot restarted within the same hour)
        if not force:
            last_run_str = await self._db.get_poll_state("company_bonus_last_run")
            if last_run_str:
                try:
                    last_run = datetime.fromisoformat(last_run_str)
                    elapsed_minutes = (
                        datetime.now(timezone.utc) - last_run
                    ).total_seconds() / 60
                    if elapsed_minutes < _MIN_SCAN_INTERVAL_MINUTES:
                        logger.info(
                            "company_bonus_scan: skipping — last run %.1f min ago (< %d min)",
                            elapsed_minutes,
                            _MIN_SCAN_INTERVAL_MINUTES,
                        )
                        return
                except (ValueError, TypeError):
                    pass

        watchers = await self._db.get_all_company_bonus_watchers()
        move_advice_watchers = await self._db.get_all_company_move_advice_watchers()

        if not watchers and not move_advice_watchers:
            return

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        # Record scan start time so restart-cooldown check works for this run
        await self._db.set_poll_state("company_bonus_last_run", now_iso)

        for watcher in watchers:
            try:
                await self._check_watcher(watcher, now_iso, force=force)
            except Exception:
                logger.exception(
                    "company_bonus_scan: error checking watcher %s",
                    watcher.get("discord_username"),
                )
            # Small yield between users to avoid hammering the API
            await asyncio.sleep(1)

        if move_advice_watchers:
            econ = await self._fetch_move_economics()
            if econ is None:
                logger.warning(
                    "company_move_advice: could not fetch market prices / game config, "
                    "skipping move-advice scan"
                )
            else:
                for watcher in move_advice_watchers:
                    try:
                        await self._check_move_advice_watcher(
                            watcher, now_iso, econ, now, force=force
                        )
                    except Exception:
                        logger.exception(
                            "company_move_advice: error checking watcher %s",
                            watcher.get("discord_username"),
                        )
                    await asyncio.sleep(1)

    async def _check_watcher(self, watcher: dict, now_iso: str, *, force: bool = False) -> None:
        discord_user_id: str = watcher["discord_user_id"]
        game_username: str = watcher["game_username"]
        guild_id: str = watcher["guild_id"]
        game_user_id: str | None = watcher.get("game_user_id")

        # Resolve in-game user ID if not yet cached
        if not game_user_id:
            game_user_id = await self._resolve_game_user_id(game_username)
            if not game_user_id:
                logger.warning(
                    "company_bonus_scan: could not resolve game user ID for %r, skipping",
                    game_username,
                )
                return
            await self._db.update_company_bonus_watcher_game_id(discord_user_id, game_user_id)

        company_ids = await self._fetch_company_ids_for_user(game_user_id)
        if not company_ids:
            return

        # Collect currently seen company IDs to prune stale alerts later
        seen_company_ids: set[str] = set()

        for company_id in company_ids:
            company = await self._fetch_company(company_id)
            if not company:
                continue

            # Skip companies that are disabled (disabledAt field present)
            if company.get("disabledAt"):
                continue

            region_id: str = str(company.get("region") or "")
            item_code: str = str(company.get("itemCode") or "")

            if not region_id or not item_code:
                continue

            seen_company_ids.add(company_id)

            bonus_total = await self._fetch_production_bonus_total(company_id)
            if bonus_total is None:
                # API error — skip this company rather than false-alarming
                continue
            has_bonus = bonus_total > 0

            existing_alert = await self._db.get_company_bonus_alert(discord_user_id, company_id)

            if has_bonus:
                # Bonus is back (or was never 0). Remove any stale alert so
                # a future drop can trigger a new notification.
                if existing_alert is not None:
                    await self._db.delete_company_bonus_alert(discord_user_id, company_id)
                continue  # Never notify for companies that have a bonus

            # ── Bonus is 0% in current region ────────────────────────────────
            if not force and existing_alert is not None:
                if existing_alert["region_id"] == region_id:
                    # Already notified for this (company, region) — skip
                    continue
                else:
                    # Company moved to a new region that also has 0% bonus
                    # → delete old alert so we can re-notify
                    await self._db.delete_company_bonus_alert(discord_user_id, company_id)

            # Send the notification
            company_name: str = str(company.get("name") or company_id)
            await self._notify_user(
                discord_user_id=discord_user_id,
                guild_id=guild_id,
                company_name=company_name,
                company_id=company_id,
                item_code=item_code,
                region_id=region_id,
            )
            await self._db.set_company_bonus_alert(
                discord_user_id, company_id, region_id, now_iso
            )
            await asyncio.sleep(0.5)  # small pause between DMs

    # ── Move-advice scan helpers ──────────────────────────────────────────────

    async def _fetch_move_economics(self) -> MoveEconomics | None:
        """Fetch market prices and the game config needed to price a move.

        Returns None when either call fails or the data is unusable, in which
        case the move-advice scan is skipped rather than run on bad numbers.
        """
        try:
            raw_prices = await self._client.get("/itemTrading.getPrices")
        except Exception as exc:
            logger.debug("company_move_advice: getPrices failed: %s", exc)
            return None

        prices = _unwrap_prices(raw_prices)
        concrete_price = float(prices.get("concrete") or 0)
        if concrete_price <= 0:
            logger.debug("company_move_advice: no concrete price available")
            return None

        try:
            raw_cfg = await self._client.get(
                "/gameConfig.getGameConfig", params={"input": "{}"}
            )
            cfg = _unwrap(raw_cfg)
        except Exception as exc:
            logger.debug("company_move_advice: getGameConfig failed: %s", exc)
            return None
        if not isinstance(cfg, dict):
            return None

        # Move cost comes from the live config rather than a hardcoded constant,
        # so a game-side balance change is picked up automatically.
        try:
            move_cost_units = float((cfg.get("company") or {}).get("moveCost") or 0)
        except (TypeError, ValueError):
            move_cost_units = 0.0
        if move_cost_units <= 0:
            logger.warning("company_move_advice: config has no company.moveCost")
            return None

        items_cfg = cfg.get("items") or {}
        if not isinstance(items_cfg, dict) or not items_cfg:
            return None

        prod_points: dict[str, float] = {}
        prod_needs: dict[str, dict[str, float]] = {}
        for code, info in items_cfg.items():
            if not isinstance(info, dict):
                continue
            try:
                pp = float(info.get("productionPoints") or 1)
            except (TypeError, ValueError):
                pp = 1.0
            prod_points[code] = pp if pp > 0 else 1.0
            needs = info.get("productionNeeds") or {}
            if isinstance(needs, dict) and needs:
                converted: dict[str, float] = {}
                for mat, qty in needs.items():
                    try:
                        converted[mat] = float(qty)
                    except (TypeError, ValueError):
                        pass
                if converted:
                    prod_needs[code] = converted

        return MoveEconomics(
            prices=prices,
            prod_points=prod_points,
            prod_needs=prod_needs,
            move_cost=move_cost_units * concrete_price,
            move_cost_units=move_cost_units,
        )

    async def _fetch_region_name(self, region_id: str) -> str:
        """Return the display name for a region, falling back to the region ID."""
        try:
            raw = await self._client.get(
                "/region.getById",
                params={"input": json.dumps({"regionId": region_id})},
            )
            data = _unwrap(raw)
            if isinstance(data, dict) and data.get("name"):
                return str(data["name"])
        except Exception as exc:
            logger.debug("company_move_advice: region.getById failed for %s: %s", region_id, exc)
        return region_id

    async def _find_best_move_candidate(
        self,
        company: dict,
        current_bonus_pct: float,
        econ: MoveEconomics,
        now: datetime,
    ) -> dict | None:
        """Find the best qualifying region to move this company to, or None.

        Selection rules (applied per candidate region):
          • Deposit region  : payback_days ≤ deposit_days_left − 1 (at least 1 day margin)
          • SR-only region  : payback_days ≤ 7
          • Permanent bonus : payback_days < 7

        Payback is calculated using the *gain* (new_bonus − current_bonus), not
        the absolute bonus, so companies with an existing bonus are treated fairly.
        Both the gain and the move cost are valued in CC using the company's own
        item (see the module docstring).  Among all qualifying candidates, the one
        with the highest bonus gain wins.
        """
        item_code = str(company.get("itemCode") or "")
        current_region_id = str(company.get("region") or "")
        ae_level = max(1, int((company.get("activeUpgradeLevels") or {}).get("automatedEngine") or 0))

        if not item_code:
            return None

        base_items_per_day = econ.items_per_day(item_code, ae_level)
        if base_items_per_day <= 0:
            logger.debug(
                "company_move_advice: no production data for item %r, skipping", item_code
            )
            return None

        try:
            raw = await self._client.get(
                "/company.getRecommendedRegionIdsByItemCode",
                params={"input": json.dumps({"itemCode": item_code, "count": 100})},
            )
            region_list = _unwrap_region_list(raw)
        except Exception as exc:
            logger.debug(
                "company_move_advice: getRecommendedRegionIdsByItemCode failed for %s: %s",
                item_code, exc,
            )
            return None

        best_candidate: dict | None = None
        best_bonus_gain = 0.0

        for region in region_list:
            rid = region.get("regionId") or region.get("region_id") or ""
            if rid == current_region_id:
                continue  # Skip current region

            deposit_bonus = float(region.get("depositBonus") or 0)
            sr_bonus = float(region.get("strategicBonus") or 0)
            ethics_bonus = float(region.get("ethicSpecializationBonus") or 0)
            ethics_dep_bonus = float(region.get("ethicDepositBonus") or 0)
            total_bonus = deposit_bonus + sr_bonus + ethics_bonus + ethics_dep_bonus

            bonus_gain = total_bonus - current_bonus_pct
            if bonus_gain <= 0:
                continue

            # Value the gain in CC using this company's own item. The bonus
            # scales raw-material consumption too, so the extra output earns the
            # *net* margin per unit, not gross revenue.
            tax_percent = float(region.get("taxPercent") or 0)
            margin_per_item = econ.margin_per_item(item_code, tax_percent)
            if margin_per_item <= 0:
                continue  # Producing here loses money — never advise moving in

            extra_per_day = base_items_per_day * (bonus_gain / 100) * margin_per_item
            if extra_per_day <= 0:
                continue
            payback_days = econ.move_cost / extra_per_day
            payback_hours = payback_days * 24

            has_deposit = deposit_bonus > 0
            has_sr = sr_bonus > 0
            deposit_end_at = region.get("depositEndAt") or region.get("deposit_end_at") or ""
            deposit_days_left: float | None = None

            qualifies = False
            qualifier_type: str | None = None

            if has_deposit:
                # Only advise if we know when the deposit expires
                if deposit_end_at:
                    try:
                        end_dt = datetime.fromisoformat(deposit_end_at.replace("Z", "+00:00"))
                        deposit_days_left = (end_dt - now).total_seconds() / 86400
                        # Need at least 1 day of margin after earning back the cost
                        if deposit_days_left > 1 and payback_days <= deposit_days_left - 1:
                            qualifies = True
                            qualifier_type = "deposit"
                    except Exception:
                        pass
            elif has_sr:
                # SR lasts ~30 days; cap advice at 7 days payback
                if payback_days <= 7:
                    qualifies = True
                    qualifier_type = "sr"
            else:
                # Permanent bonus — no time constraint, but cap at 7 days
                if payback_days < 7:
                    qualifies = True
                    qualifier_type = "permanent"

            if qualifies and bonus_gain > best_bonus_gain:
                best_bonus_gain = bonus_gain
                best_candidate = {
                    "region_id": rid,
                    "total_bonus": total_bonus,
                    "bonus_gain": bonus_gain,
                    "payback_days": payback_days,
                    "payback_hours": payback_hours,
                    "qualifier_type": qualifier_type,
                    "deposit_days_left": deposit_days_left,
                    "deposit_end_at": deposit_end_at,
                    "has_deposit": has_deposit,
                    "has_sr": has_sr,
                    "deposit_bonus": deposit_bonus,
                    "sr_bonus": sr_bonus,
                    "ethics_bonus": ethics_bonus,
                    "ethics_dep_bonus": ethics_dep_bonus,
                    "ae_level": ae_level,
                    "tax_percent": tax_percent,
                    "margin_per_item": margin_per_item,
                    "extra_per_day": extra_per_day,
                }

        return best_candidate

    async def _check_move_advice_watcher(
        self,
        watcher: dict,
        now_iso: str,
        econ: MoveEconomics,
        now: datetime,
        *,
        force: bool = False,
    ) -> None:
        discord_user_id: str = watcher["discord_user_id"]
        game_username: str = watcher["game_username"]
        guild_id: str = watcher["guild_id"]
        game_user_id: str | None = watcher.get("game_user_id")

        if not game_user_id:
            game_user_id = await self._resolve_game_user_id(game_username)
            if not game_user_id:
                logger.warning(
                    "company_move_advice: could not resolve game user ID for %r, skipping",
                    game_username,
                )
                return
            await self._db.update_company_move_advice_watcher_game_id(discord_user_id, game_user_id)

        company_ids = await self._fetch_company_ids_for_user(game_user_id)
        if not company_ids:
            return

        for company_id in company_ids:
            company = await self._fetch_company(company_id)
            if not company:
                continue
            if company.get("disabledAt"):
                continue

            region_id: str = str(company.get("region") or "")
            item_code: str = str(company.get("itemCode") or "")
            if not region_id or not item_code:
                continue

            current_bonus_pct = await self._fetch_production_bonus_total(company_id)
            if current_bonus_pct is None:
                continue

            # Deduplication: if already alerted from this region, skip
            existing_alert = await self._db.get_company_move_advice_alert(
                discord_user_id, company_id
            )
            if not force and existing_alert is not None and existing_alert["source_region_id"] == region_id:
                continue

            candidate = await self._find_best_move_candidate(
                company, current_bonus_pct, econ, now
            )

            if candidate is None:
                # No qualifying move — clear any stale alert
                if existing_alert is not None:
                    await self._db.delete_company_move_advice_alert(discord_user_id, company_id)
                continue

            company_name: str = str(company.get("name") or company_id)
            region_name = await self._fetch_region_name(candidate["region_id"])

            await self._notify_move_advice(
                discord_user_id=discord_user_id,
                guild_id=guild_id,
                company_name=company_name,
                company_id=company_id,
                item_code=item_code,
                current_bonus_pct=current_bonus_pct,
                candidate=candidate,
                region_name=region_name,
                econ=econ,
            )
            await self._db.set_company_move_advice_alert(
                discord_user_id, company_id, region_id, candidate["region_id"], now_iso
            )
            await asyncio.sleep(0.5)

    async def _notify_move_advice(
        self,
        discord_user_id: str,
        guild_id: str,
        company_name: str,
        company_id: str,
        item_code: str,
        current_bonus_pct: float,
        candidate: dict,
        region_name: str,
        econ: MoveEconomics,
    ) -> None:
        """Send a DM with move advice for one company."""
        try:
            user = await self.bot.fetch_user(int(discord_user_id))
        except Exception as exc:
            logger.warning(
                "company_move_advice: could not fetch Discord user %s: %s",
                discord_user_id, exc,
            )
            return

        ae_level = candidate["ae_level"]
        target_bonus = candidate["total_bonus"]
        bonus_gain = candidate["bonus_gain"]
        payback_days = candidate["payback_days"]
        qualifier_type = candidate["qualifier_type"]
        deposit_days_left = candidate["deposit_days_left"]
        deposit_bonus = candidate["deposit_bonus"]
        sr_bonus = candidate["sr_bonus"]
        ethics_bonus = candidate["ethics_bonus"]
        ethics_dep_bonus = candidate["ethics_dep_bonus"]
        tax_percent = candidate["tax_percent"]
        extra_per_day = candidate["extra_per_day"]
        margin_per_item = candidate["margin_per_item"]

        # Bonus breakdown
        breakdown_parts: list[str] = []
        if sr_bonus > 0:
            breakdown_parts.append(f"**{sr_bonus:.1f}%** strategische resources")
        if ethics_bonus > 0:
            breakdown_parts.append(f"**{ethics_bonus:.1f}%** ethiek")
        if deposit_bonus > 0:
            dep_str = f"**{deposit_bonus:.1f}%** deposit"
            if deposit_days_left is not None:
                dep_str += f" (nog ~{deposit_days_left:.1f} dag(en))"
            breakdown_parts.append(dep_str)
        if ethics_dep_bonus > 0:
            breakdown_parts.append(f"**{ethics_dep_bonus:.1f}%** ethiek deposit")
        breakdown_str = " + ".join(breakdown_parts) if breakdown_parts else f"{target_bonus:.1f}%"

        # Rationale — explain why this advice passes the threshold
        if qualifier_type == "deposit":
            rationale = (
                f"De deposit loopt nog **~{deposit_days_left:.1f} dag(en)**. "
                f"Met een terugverdientijd van **{payback_days:.1f} dag(en)** verdien je de "
                f"verhuiskosten ruim terug vóór de deposit verloopt "
                f"(vereiste marge: ≥ 1 dag)."
            )
        elif qualifier_type == "sr":
            rationale = (
                f"Strategische resources duren maximaal **30 dagen**. "
                f"De terugverdientijd van **{payback_days:.1f} dag(en)** valt binnen "
                f"de maximale terugverdientermijn van **7 dagen** voor SR-bonussen."
            )
        else:
            rationale = (
                f"Dit is een **permanente bonus** (geen deposit, geen strategische resources). "
                f"De terugverdientijd van **{payback_days:.1f} dag(en)** valt binnen "
                f"de maximale terugverdientermijn van **7 dagen**."
            )

        company_url = f"https://app.warera.io/company/{company_id}"

        embed = discord.Embed(
            title="📍 Verhuisadvies voor je bedrijf",
            url=company_url,
            description=(
                f"Je bedrijf **[{company_name}]({company_url})** (`{item_code}`) staat in een regio met "
                f"**{current_bonus_pct:.1f}% productiebonus**.\n\n"
                f"**💡 Advies: Verhuizen naar {region_name}**\n"
                f"Nieuwe bonus: **{target_bonus:.1f}%** *(+{bonus_gain:.1f}% winst t.o.v. huidige regio)*\n"
                f"Opbrengst: **+{extra_per_day:.2f} CC/dag** "
                f"(automated engine {ae_level}, marge {margin_per_item:.3f} CC/stuk "
                f"na materiaal en {tax_percent:.1f}% belasting)\n"
                f"Verhuiskosten: **{econ.move_cost_units:.0f} beton** "
                f"(~{econ.move_cost:.2f} CC)\n"
                f"Terugverdientijd: ~{payback_days:.1f} dag(en)"
            ),
            colour=discord.Colour.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Bonusopbouw", value=breakdown_str, inline=False)
        embed.add_field(name="Waarom dit advies?", value=rationale, inline=False)
        embed.set_footer(text="WareraNL Bot")

        try:
            await user.send(embed=embed)
            logger.info(
                "company_move_advice: notified %s — %s → %s",
                discord_user_id, company_name, region_name,
            )
        except discord.Forbidden:
            logger.warning(
                "company_move_advice: DM blocked for user %s, skipping",
                discord_user_id,
            )
        except Exception as exc:
            logger.warning(
                "company_move_advice: failed to DM user %s: %s", discord_user_id, exc
            )

    async def _notify_user(
        self,
        discord_user_id: str,
        guild_id: str,
        company_name: str,
        company_id: str,
        item_code: str,
        region_id: str,
    ) -> None:
        """Send a DM to the Discord user about a 0% bonus company."""
        try:
            user = await self.bot.fetch_user(int(discord_user_id))
        except Exception as exc:
            logger.warning(
                "company_bonus_scan: could not fetch Discord user %s: %s",
                discord_user_id,
                exc,
            )
            return

        company_url = f"https://app.warera.io/company/{company_id}"
        embed = discord.Embed(
            title="⚠️ Bedrijf zonder productiebonus",
            url=company_url,
            description=(
                f"Je bedrijf **[{company_name}]({company_url})** (`{item_code}`) heeft momenteel "
                f"**0% productiebonus** in de regio waar het zich bevindt.\n\n"
                "Overweeg je bedrijf te verplaatsen naar een regio met een bonus."
            ),
            colour=self._embed_colour("warning"),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="WareraNL Bot")

        try:
            await user.send(embed=embed)
            logger.info(
                "company_bonus_scan: notified %s about 0%% bonus on company %s (%s)",
                discord_user_id,
                company_name,
                company_id,
            )
        except discord.Forbidden:
            logger.warning(
                "company_bonus_scan: DM blocked for user %s, skipping",
                discord_user_id,
            )
        except Exception as exc:
            logger.warning(
                "company_bonus_scan: failed to DM user %s: %s",
                discord_user_id,
                exc,
            )

    async def _notify_via_guild(
        self, discord_user_id: str, guild_id: str, embed: discord.Embed
    ) -> None:
        """Fallback: ping the user in the bot's configured notification channel."""
        channels = self.config.get("channels", {})
        # Use bot_commands as fallback; fall back to testing-area on test servers
        channel_id = channels.get("bot_commands") or channels.get("testing-area")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(f"<@{discord_user_id}>", embed=embed)
        except Exception as exc:
            logger.warning(
                "company_bonus_scan: guild fallback notification failed for %s: %s",
                discord_user_id,
                exc,
            )


async def setup(bot) -> None:
    await bot.add_cog(CompanyBonusTasks(bot))
