"""Background task: poll active battles for bounties every minute.

Fetches all active battles via battle.getBattles and posts an embed to the
configured channel whenever a battle has a bounty (or the bounty changes
significantly).  Each battle is only posted once until the bounty changes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"


def _extract_bounty(side: dict) -> tuple[float, float] | None:
    """Return (rate_per_1000_dmg, total_pool) from a battle-side dict, or None.

    Tries a range of field names to be resilient to API changes.
    """
    # Nested bounty object under various keys
    for b_key in ("bounty", "activeBounty", "currentBounty", "battleBounty", "countryBounty"):
        b = side.get(b_key)
        if isinstance(b, dict):
            rate = float(
                b.get("rate") or b.get("perThousand") or b.get("ratePerThousand")
                or b.get("damageRate") or b.get("rewardPerThousand") or 0
            )
            total = float(
                b.get("total") or b.get("totalPool") or b.get("pool")
                or b.get("totalAmount") or b.get("amount") or b.get("value") or 0
            )
            if rate > 0 or total > 0:
                return rate, total
    # Flat fields at root of side object — actual API field names
    rate = float(
        side.get("moneyPer1kDamages") or side.get("bountyRate")
        or side.get("bountyPerThousand") or 0
    )
    total = float(
        side.get("moneyPool") or side.get("bountyTotal")
        or side.get("bountyPool") or side.get("bountyAmount") or 0
    )
    if rate > 0 or total > 0:
        return rate, total
    return None


class BountyTasks(TaskCogBase, name="bounty_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot
        # battle_id -> (rate_per_1000, total_pool) — last posted state
        self._known: dict[str, tuple[float, float]] = {}
        # country_id -> country_name cache
        self._country_names: dict[str, str] = {}

    def cog_load(self) -> None:
        self.bounty_poll.start()

    def cog_unload(self) -> None:
        self.bounty_poll.cancel()

    @tasks.loop(minutes=1)
    async def bounty_poll(self) -> None:
        if not self._client:
            return
        try:
            await self._run_bounty_poll()
        except Exception:
            logger.exception("bounty_poll: unexpected error")

    @bounty_poll.before_loop
    async def before_bounty_poll(self) -> None:
        await self._wait_for_services()

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _run_bounty_poll(self) -> None:
        channels = self.config.get("channels", {})
        # In testing mode use the testing-area channel; in production use bot_mededelingen.
        if getattr(self.bot, "testing", False):
            channel_id = channels.get("testing-area")
        else:
            channel_id = channels.get("bot_mededelingen")
        logger.info("bounty_poll: starting — testing=%s channel_id=%s",
                    getattr(self.bot, "testing", False), channel_id)
        if not channel_id:
            logger.warning("bounty_poll: no channel configured, skipping")
            return

        try:
            resp = await self._client.get(
                "/battle.getBattles",
                params={"input": json.dumps({"isActive": True, "limit": 100})},
            )
        except Exception as exc:
            logger.warning("bounty_poll: failed to fetch battles: %s", exc)
            return

        logger.debug("bounty_poll: raw resp type=%s keys=%s",
                     type(resp).__name__,
                     list(resp.keys()) if isinstance(resp, dict) else "N/A")

        # Unwrap tRPC-style response
        data: object = resp
        if isinstance(resp, dict):
            inner = resp.get("result", resp)
            data = inner.get("data", inner) if isinstance(inner, dict) else resp

        logger.debug("bounty_poll: unwrapped data type=%s keys=%s",
                     type(data).__name__,
                     list(data.keys()) if isinstance(data, dict) else "N/A")

        battles: list[dict] = []
        if isinstance(data, dict):
            battles = data.get("items") or data.get("battles") or []
        elif isinstance(data, list):
            battles = [b for b in data if isinstance(b, dict)]

        if not isinstance(battles, list):
            logger.debug("bounty_poll: battles is not a list (%s)", type(battles).__name__)
            return

        logger.debug("bounty_poll: got %d active battles", len(battles))

        # Refresh country name cache
        try:
            c_resp = await self._client.get("/country.getAllCountries")
            c_inner = c_resp.get("result", c_resp) if isinstance(c_resp, dict) else {}
            c_data = c_inner.get("data", c_inner) if isinstance(c_inner, dict) else c_resp
            if isinstance(c_data, list):
                self._country_names = {
                    str(c["_id"]): c.get("name") or c.get("shortName") or str(c["_id"])
                    for c in c_data
                    if isinstance(c, dict) and c.get("_id")
                }
        except Exception:
            logger.debug("bounty_poll: could not refresh country names")

        # Remove stale entries for battles that are no longer active
        active_ids = {str(b.get("_id") or "") for b in battles}
        self._known = {
            k: v for k, v in self._known.items()
            if k.split(":")[0] in active_ids
        }

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            logger.warning("bounty_poll: channel %s not found in cache", channel_id)
            return

        def _cname(side: dict) -> str:
            cid = str(side.get("country") or "")
            return (
                side.get("countryName") or side.get("name")
                or self._country_names.get(cid)
                or (cid[:8] if cid else "?")
            )

        for battle in battles:
            battle_id = str(battle.get("_id") or "")
            if not battle_id:
                continue

            attacker = battle.get("attacker") or {}
            defender = battle.get("defender") or {}

            logger.debug(
                "bounty_poll: battle %s — atk keys=%s dfn keys=%s",
                battle_id, list(attacker.keys()), list(defender.keys()),
            )

            att_name = _cname(attacker)
            def_name = _cname(defender)
            region_raw = defender.get("region") or attacker.get("region") or battle.get("region")
            region_name: str | None = (
                (region_raw.get("name") if isinstance(region_raw, dict) else None)
                or defender.get("regionName") or defender.get("region_name")
                or None
            )
            battle_url = _BATTLE_URL.format(battle_id=battle_id)

            # Evaluate each side independently so both bounties are reported
            for side_key, side in (("atk", attacker), ("dfn", defender)):
                b = _extract_bounty(side)
                if not b:
                    continue
                rate, total = b

                logger.debug(
                    "bounty_poll: battle %s [%s] — rate=%.4f total=%.4f",
                    battle_id, side_key, rate, total,
                )

                if total <= 0:
                    continue  # Pool depleted

                if rate < 1.0:
                    continue  # Below minimum threshold

                known_key = f"{battle_id}:{side_key}"
                prev = self._known.get(known_key)
                if prev is not None:
                    prev_rate, _ = prev
                    if rate - prev_rate < 0.1:
                        continue  # Rate not increased enough

                self._known[known_key] = (rate, total)

                funder_name = _cname(side)
                lines: list[str] = []
                if region_name:
                    lines.append(f"**Regio:** {region_name}")
                lines.append(f"**Aangeboden door:** {funder_name}")
                if rate > 0:
                    lines.append(f"**Beloning:** {rate:.3f} CC per 1.000 schade")
                if total > 0:
                    lines.append(f"**Totale pool:** {total:,.2f} CC")

                embed = discord.Embed(
                    title=f"💰 Bounty: {att_name} vs {def_name}",
                    description="\n".join(lines) if lines else None,
                    url=battle_url,
                    colour=discord.Colour.gold(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text="WarEra — bounty alert")

                try:
                    await channel.send(embed=embed)
                except Exception as exc:
                    logger.warning(
                        "bounty_poll: failed to send embed for battle %s [%s]: %s",
                        battle_id, side_key, exc,
                    )


async def setup(bot) -> None:
    """Add the BountyTasks cog to the bot."""
    await bot.add_cog(BountyTasks(bot))
