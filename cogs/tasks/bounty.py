"""Background task: poll active battles for bounties every minute.

Fetches all active battles via battle.getBattles and posts an embed to the
configured channel whenever a battle has a bounty (or the bounty changes
significantly).  Each battle is only posted once until the bounty changes.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"

# Thresholds for which Discord roles exist (must match role names exactly, e.g. "0.2bounty")
_BOUNTY_THRESHOLDS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


def _extract_bounty(side: dict) -> tuple[float, float] | None:
    """Return (rate_per_1000_dmg, total_pool) from a battle-side dict, or None.

    Tries a range of field names to be resilient to API changes.
    """
    # Nested bounty object under various keys
    for b_key in (
        "bounty",
        "activeBounty",
        "currentBounty",
        "battleBounty",
        "countryBounty",
    ):
        b = side.get(b_key)
        if isinstance(b, dict):
            rate = float(
                b.get("rate")
                or b.get("perThousand")
                or b.get("ratePerThousand")
                or b.get("damageRate")
                or b.get("rewardPerThousand")
                or 0
            )
            total = float(
                b.get("total")
                or b.get("totalPool")
                or b.get("pool")
                or b.get("totalAmount")
                or b.get("amount")
                or b.get("value")
                or 0
            )
            if rate > 0 or total > 0:
                return rate, total
    # Flat fields at root of side object — actual API field names
    rate = float(
        side.get("moneyPer1kDamages")
        or side.get("bountyRate")
        or side.get("bountyPerThousand")
        or 0
    )
    total = float(
        side.get("moneyPool")
        or side.get("bountyTotal")
        or side.get("bountyPool")
        or side.get("bountyAmount")
        or 0
    )
    if rate > 0 or total > 0:
        return rate, total
    return None


class BountyTasks(TaskCogBase, name="bounty_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot
        # known_key -> (rate, total, msg_id, pending_until_ts)
        # pending_until_ts: UTC unix timestamp when the bounty becomes payable, or None if already active
        self._known: dict[str, tuple[float, float, int | None, float | None]] = {}
        # country_id -> country_name cache
        self._country_names: dict[str, str] = {}
        # Set of country IDs that should never be targeted by bounty alerts
        # (NL itself + all current allies).  Refreshed each poll cycle.
        self._protected_ids: set[str] = set()
        # Set of country IDs that are enemies of NL (declared war / at war).
        self._enemy_ids: set[str] = set()

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
        await self._preload_known_from_channel()

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _preload_known_from_channel(self) -> None:
        """Scan recent channel messages and pre-populate _known so existing bounty
        posts are not re-sent after a bot restart."""
        channels = self.config.get("channels", {})
        channel_id = channels.get("bounties")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return

        _battle_url_re = re.compile(r"https?://(?:app\.)?warera\.io/battle/([A-Za-z0-9_-]+)")
        _rate_re = re.compile(r"\*\*Beloning:\*\*\s*([\d.]+)")
        _total_re = re.compile(r"\*\*Totale pool:\*\*\s*([\d,. ]+)")
        _ts_re = re.compile(r"<t:(\d+):[^>]+>")
        _title_re = re.compile(r"(?:Bounty|Aankomende bounty):\s*(.+?)\s+vs\s+(.+)", re.IGNORECASE)
        _funder_re = re.compile(r"\*\*Aangeboden door:\*\*\s*(.+)")

        try:
            async for msg in channel.history(limit=50):
                if msg.author != self.bot.user or not msg.embeds:
                    continue
                embed = msg.embeds[0]
                url = embed.url or ""
                m = _battle_url_re.search(url)
                if not m:
                    continue
                battle_id = m.group(1)

                desc = embed.description or ""
                rate_m = _rate_re.search(desc)
                total_m = _total_re.search(desc)
                rate = float(rate_m.group(1)) if rate_m else 0.0
                total = float((total_m.group(1) or "0").replace(",", "").replace(" ", "")) if total_m else 0.0

                is_pending = embed.title is not None and embed.title.startswith("⏳")
                pending_until_ts: float | None = None
                if is_pending:
                    ts_m = _ts_re.search(desc)
                    if ts_m:
                        pending_until_ts = float(ts_m.group(1))

                # Determine which side this message belongs to by matching the funder
                # country against the attacker/defender names from the title.
                # This prevents erroneous deletions when both side keys point to the same msg.id.
                detected_side: str | None = None
                if embed.title:
                    title_m = _title_re.search(embed.title)
                    funder_m = _funder_re.search(desc)
                    if title_m and funder_m:
                        att_name = title_m.group(1).strip()
                        def_name = title_m.group(2).strip()
                        funder = funder_m.group(1).strip()
                        if funder == att_name:
                            detected_side = "atk"
                        elif funder == def_name:
                            detected_side = "dfn"

                for side_key in ("atk", "dfn"):
                    known_key = f"{battle_id}:{side_key}"
                    if known_key in self._known:
                        continue
                    # Use the real msg.id only for the side we identified; store None for
                    # the other side so an erroneous "no bounty" match never deletes this
                    # message.  A None msg_id still suppresses re-posting via the rate check.
                    stored_msg_id = msg.id if (detected_side is None or detected_side == side_key) else None
                    self._known[known_key] = (rate, total, stored_msg_id, pending_until_ts)

        except Exception:
            logger.exception("bounty_poll: failed to preload known bounties from channel")

    async def _fetch_effective_at(self, battle_id: str, side_key: str) -> float | None:
        """Return UTC unix timestamp when the bounty becomes payable, or None."""
        try:
            resp = await self._client.get(
                "/battle.getLiveBattleData",
                params={"input": json.dumps({"battleId": battle_id})},
            )
            inner = resp.get("result", resp) if isinstance(resp, dict) else {}
            data = inner.get("data", inner) if isinstance(inner, dict) else {}
            battle_live = data.get("battle", {}) if isinstance(data, dict) else {}
            prefix = "attacker" if side_key == "atk" else "defender"
            eff_str = battle_live.get(f"{prefix}BountyEffectiveAt")
            if eff_str:
                dt = datetime.fromisoformat(eff_str.replace("Z", "+00:00"))
                return dt.timestamp()
        except Exception as exc:
            logger.debug("bounty_poll: could not fetch effective timestamp for %s [%s]: %s", battle_id, side_key, exc)
        return None

    async def _run_bounty_poll(self) -> None:
        channels = self.config.get("channels", {})
        channel_id = channels.get("bounties")
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

        # Unwrap tRPC-style response
        data: object = resp
        if isinstance(resp, dict):
            inner = resp.get("result", resp)
            data = inner.get("data", inner) if isinstance(inner, dict) else resp

        battles: list[dict] = []
        if isinstance(data, dict):
            battles = data.get("items") or data.get("battles") or []
        elif isinstance(data, list):
            battles = [b for b in data if isinstance(b, dict)]

        if not isinstance(battles, list):
            return

        # Refresh country name cache
        try:
            c_resp = await self._client.get("/country.getAllCountries")
            c_inner = c_resp.get("result", c_resp) if isinstance(c_resp, dict) else {}
            c_data = (
                c_inner.get("data", c_inner) if isinstance(c_inner, dict) else c_resp
            )
            if isinstance(c_data, list):
                self._country_names = {
                    str(c["_id"]): c.get("name") or c.get("shortName") or str(c["_id"])
                    for c in c_data
                    if isinstance(c, dict) and c.get("_id")
                }
        except Exception:
            pass  # country names cache not refreshed; fall back to IDs

        # Refresh protected-country set (NL + allies)
        nl_country_id: str = self.config.get("nl_country_id", "")
        protected: set[str] = {nl_country_id} if nl_country_id else set()
        if nl_country_id:
            try:
                nl_resp = await self._client.post(
                    "/country.getCountryById",
                    json={"countryId": nl_country_id},
                )
                nl_inner = (
                    nl_resp.get("result", nl_resp) if isinstance(nl_resp, dict) else {}
                )
                nl_data = (
                    nl_inner.get("data", nl_inner)
                    if isinstance(nl_inner, dict)
                    else nl_resp
                )
                if isinstance(nl_data, dict):
                    allies = nl_data.get("allies") or []
                    protected.update(str(a) for a in allies if a)
                    # Also collect declared enemies (field names vary by API version)
                    raw_enemies = (
                        nl_data.get("enemies")
                        or nl_data.get("permanentEnemies")
                        or nl_data.get("atWarWith")
                        or []
                    )
                    self._enemy_ids = {str(e) for e in raw_enemies if e}
            except Exception:
                pass  # keep previous protected set on error
        self._protected_ids = protected

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            logger.warning("bounty_poll: channel %s not found in cache", channel_id)
            return

        # Remove stale entries for battles that are no longer active, deleting their messages
        active_ids = {str(b.get("_id") or "") for b in battles}
        stale = {k: v for k, v in self._known.items() if k.split(":")[0] not in active_ids}
        for stale_val in stale.values():
            if stale_val[2]:
                try:
                    await channel.get_partial_message(stale_val[2]).delete()
                except Exception:
                    pass
        self._known = {k: v for k, v in self._known.items() if k.split(":")[0] in active_ids}

        def _cname(side: dict) -> str:
            cid = str(side.get("country") or "")
            return (
                side.get("countryName")
                or side.get("name")
                or self._country_names.get(cid)
                or (cid[:8] if cid else "?")
            )

        for battle in battles:
            battle_id = str(battle.get("_id") or "")
            if not battle_id:
                continue

            attacker = battle.get("attacker") or {}
            defender = battle.get("defender") or {}

            att_name = _cname(attacker)
            def_name = _cname(defender)
            region_raw = (
                defender.get("region") or attacker.get("region") or battle.get("region")
            )
            region_name: str | None = (
                (region_raw.get("name") if isinstance(region_raw, dict) else None)
                or defender.get("regionName")
                or defender.get("region_name")
                or None
            )
            battle_url = _BATTLE_URL.format(battle_id=battle_id)

            # Country IDs for both sides
            att_country_id = str(attacker.get("country") or "")
            def_country_id = str(defender.get("country") or "")

            # Evaluate each side independently so both bounties are reported
            for side_key, side, side_country_id in (
                ("atk", attacker, att_country_id),
                ("dfn", defender, def_country_id),
            ):
                # Determine the opponent's country ID for this side
                opponent_country_id = def_country_id if side_key == "atk" else att_country_id

                # Skip if the bounty funder is fighting against NL or an ally — paying
                # people to battle our side.
                if opponent_country_id and opponent_country_id in self._protected_ids:
                    continue

                # Skip if the funder is a known enemy of NL — don't help them recruit.
                if side_country_id and side_country_id in self._enemy_ids:
                    continue

                known_key = f"{battle_id}:{side_key}"

                b = _extract_bounty(side)

                # No bounty, rate below threshold, or pool exhausted (paid out) — delete any previously posted message.
                if b is None or b[0] < 0.1 or b[1] <= 0:
                    prev = self._known.pop(known_key, None)
                    if prev and prev[2]:
                        try:
                            await channel.get_partial_message(prev[2]).delete()
                        except Exception:
                            pass
                    continue

                rate, total = b

                now_ts = datetime.now(timezone.utc).timestamp()
                prev = self._known.get(known_key)

                # Determine pending state and decide whether to (re-)post
                pending_until_ts: float | None = None
                skip = False

                if prev is None:
                    # New bounty: fetch live data for effective timestamp
                    eff = await self._fetch_effective_at(battle_id, side_key)
                    if eff is not None and eff > now_ts:
                        pending_until_ts = eff
                else:
                    prev_rate, _prev_total, _prev_msg, prev_pending_until = prev

                    if prev_pending_until is not None:
                        if now_ts < prev_pending_until:
                            # Still pending
                            if rate - prev_rate < 0.1:
                                skip = True  # same rate, still pending → nothing new
                            pending_until_ts = prev_pending_until
                        else:
                            # Was pending, now active — delete pending message and re-post with pings
                            if _prev_msg:
                                try:
                                    await channel.get_partial_message(_prev_msg).delete()
                                except Exception:
                                    pass
                            pending_until_ts = None  # now active
                    else:
                        # Was already active
                        if rate - prev_rate < 0.1:
                            skip = True

                if skip:
                    continue

                is_pending = pending_until_ts is not None and pending_until_ts > now_ts

                funder_name = _cname(side)
                lines: list[str] = []
                if region_name:
                    lines.append(f"**Regio:** {region_name}")
                lines.append(f"**Aangeboden door:** {funder_name}")
                if is_pending:
                    lines.append(f"**Actief over:** <t:{int(pending_until_ts)}:R>")
                if rate > 0:
                    lines.append(f"**Beloning:** {rate:g} CC per 1k schade")
                if total > 0:
                    lines.append(f"**Totale pool:** {total:,.2f} CC")

                if is_pending:
                    embed_title = f"⏳ Aankomende bounty: {att_name} vs {def_name}"
                    embed_colour = discord.Colour.orange()
                else:
                    embed_title = f"💰 Bounty: {att_name} vs {def_name}"
                    embed_colour = discord.Colour.gold()

                embed = discord.Embed(
                    title=embed_title,
                    description="\n".join(lines) if lines else None,
                    url=battle_url,
                    colour=embed_colour,
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text="WarEra — bounty alert")

                # Ping roles for both pending and active bounties
                content = None
                guild = getattr(channel, "guild", None)
                ping_parts: list[str] = []
                if guild:
                    for t in _BOUNTY_THRESHOLDS:
                        if t <= rate:
                            r = discord.utils.get(guild.roles, name=f"{t:g}bounty")
                            if r:
                                ping_parts.append(r.mention)
                content = " ".join(ping_parts) if ping_parts else None

                try:
                    msg = await channel.send(content=content, embed=embed)
                    self._known[known_key] = (rate, total, msg.id, pending_until_ts)
                except Exception as exc:
                    logger.warning(
                        "bounty_poll: failed to send embed for battle %s [%s]: %s",
                        battle_id,
                        side_key,
                        exc,
                    )
                    self._known[known_key] = (rate, total, None, pending_until_ts)


async def setup(bot) -> None:
    """Add the BountyTasks cog to the bot."""
    await bot.add_cog(BountyTasks(bot))
