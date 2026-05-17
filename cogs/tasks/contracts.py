"""Background task: poll mercenary contract auctions every minute.

Fetches all active auctions via mercenaryContractAuction.getPaginatedAuctions
and posts an embed to the configured channel whenever a contract is posted by
NL or one of its allies.

Two subscription roles gate who is pinged:
  - "Contracten"    → all relevant contracts (NL + allies)
  - "Contracten NL" → only contracts placed directly by NL
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"

# Discord role names for subscription pings (must match exactly)
_ROLE_ALL = "Contracten"
_ROLE_NL = "NL Contracten"

# Footer prefix used to identify our own embed messages
_FOOTER_PREFIX = "WarEra — contract:"


async def _unping_and_delete(
    msg: discord.PartialMessage | discord.Message,
) -> None:
    """Clear the content (removes the @role ping) then delete the message."""
    try:
        await msg.edit(content=None)
    except Exception:
        pass
    await msg.delete()


class ContractsCog(TaskCogBase, name="contracts_tasks"):
    """Monitors mercenary contract auctions and posts notifications."""

    def __init__(self, bot) -> None:
        self.bot = bot
        # auction_id → (currentPerK, currentPayout, message_id | None)
        self._known: dict[str, tuple[float, float, int | None]] = {}
        self._protected_ids: set[str] = set()  # NL + allies
        self._enemy_ids: set[str] = set()
        self._country_names: dict[str, str] = {}
        # battle_id → (region_name | None, attacker_country_id | None, defender_country_id | None)
        self._battle_cache: dict[str, tuple[str | None, str | None, str | None]] = {}

    def cog_load(self) -> None:
        self._contracts_poll.start()

    def cog_unload(self) -> None:
        self._contracts_poll.cancel()

    @tasks.loop(minutes=1)
    async def _contracts_poll(self) -> None:
        if not self._client:
            return
        try:
            await self._run_contracts_poll()
        except Exception:
            logger.exception("contracts_poll: unexpected error")

    @_contracts_poll.before_loop
    async def _before_contracts_poll(self) -> None:
        await self._wait_for_services()
        await self._preload_known_from_channel()

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _get_battle_info(
        self, battle_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Return (region_name, attacker_country_id, defender_country_id)."""
        if battle_id in self._battle_cache:
            return self._battle_cache[battle_id]
        try:
            resp = await self._client.post(
                "/battle.getById", json={"battleId": battle_id}
            )
            inner = resp.get("result", resp) if isinstance(resp, dict) else {}
            data = inner.get("data", inner) if isinstance(inner, dict) else {}
            if not isinstance(data, dict):
                data = {}
            defender = data.get("defender") or {}
            attacker = data.get("attacker") or {}
            region_id = str(defender.get("region") or "")
            attacker_id = str(attacker.get("country") or "")
            defender_id = str(defender.get("country") or "")
            region_name: str | None = None
            if region_id:
                try:
                    r_resp = await self._client.post(
                        "/region.getById", json={"regionId": region_id}
                    )
                    r_inner = r_resp.get("result", r_resp) if isinstance(r_resp, dict) else {}
                    r_data = r_inner.get("data", r_inner) if isinstance(r_inner, dict) else {}
                    if isinstance(r_data, dict):
                        region_name = r_data.get("name") or None
                except Exception:
                    pass
            result = (region_name, attacker_id or None, defender_id or None)
            self._battle_cache[battle_id] = result
            return result
        except Exception:
            return (None, None, None)

    async def _preload_known_from_channel(self) -> None:
        """Scan recent channel messages and pre-populate _known so existing
        contract posts are not re-sent after a bot restart."""
        channel_id = self.config.get("channels", {}).get("contracts")
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return

        _footer_re = re.compile(
            re.escape(_FOOTER_PREFIX) + r"([a-f0-9]+)", re.IGNORECASE
        )
        try:
            async for msg in channel.history(limit=200):
                if msg.author != self.bot.user or not msg.embeds:
                    continue
                embed = msg.embeds[0]
                footer_text = embed.footer.text if embed.footer else ""
                m = _footer_re.search(footer_text)
                if m:
                    auction_id = m.group(1)
                    if auction_id not in self._known:
                        self._known[auction_id] = (0.0, 0.0, msg.id)
        except Exception as exc:
            logger.warning("contracts_poll: could not preload channel history: %s", exc)

    async def _run_contracts_poll(self) -> None:
        channel_id = self.config.get("channels", {}).get("contracts")
        if not channel_id:
            logger.debug("contracts_poll: no contracts channel configured; skipping")
            return

        # ── Fetch active auctions ──────────────────────────────────────
        try:
            resp = await self._client.post(
                "/mercenaryContractAuction.getPaginatedAuctions",
                json={"status": "active", "limit": 50, "page": 1},
            )
            inner = resp.get("result", resp) if isinstance(resp, dict) else {}
            data = inner.get("data", inner) if isinstance(inner, dict) else resp
            if isinstance(data, dict):
                auctions = (
                    data.get("auctions")
                    or data.get("items")
                    or data.get("docs")
                    or (data.get("data") if isinstance(data.get("data"), list) else None)
                    or []
                )
            elif isinstance(data, list):
                auctions = data
            else:
                auctions = []
        except Exception as exc:
            logger.warning("contracts_poll: failed to fetch auctions: %s", exc)
            return

        if not isinstance(auctions, list):
            return

        # ── Refresh country name cache ─────────────────────────────────
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
            pass  # use previous cache on error

        # ── Refresh protected / enemy sets ────────────────────────────
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
                    raw_enemies = (
                        nl_data.get("enemies")
                        or nl_data.get("permanentEnemies")
                        or nl_data.get("atWarWith")
                        or []
                    )
                    self._enemy_ids = {str(e) for e in raw_enemies if e}
            except Exception:
                pass

        if self._db:
            try:
                discord_ally_ids = await self._db.get_discord_allies()
                protected.update(discord_ally_ids)
            except Exception:
                logger.debug("contracts_poll: could not load discord allies from DB")

        self._protected_ids = protected

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            logger.warning("contracts_poll: channel %s not found in cache", channel_id)
            return

        # ── Remove stale entries (auctions no longer in active response) ─
        active_ids = {str(a.get("_id") or "") for a in auctions if a.get("_id")}
        stale = {k: v for k, v in self._known.items() if k not in active_ids}
        for stale_val in stale.values():
            if stale_val[2]:
                try:
                    await _unping_and_delete(channel.get_partial_message(stale_val[2]))
                except Exception:
                    pass
        self._known = {k: v for k, v in self._known.items() if k in active_ids}

        now = datetime.now(timezone.utc)

        # ── Process each auction ──────────────────────────────────────
        for auction in auctions:
            auction_id = str(auction.get("_id") or "")
            if not auction_id:
                continue

            # Remove non-active contracts
            if auction.get("status") != "active":
                prev = self._known.pop(auction_id, None)
                if prev and prev[2]:
                    try:
                        await _unping_and_delete(channel.get_partial_message(prev[2]))
                    except Exception:
                        pass
                continue

            # Remove expired contracts
            expires_at_str = auction.get("expiresAt")
            expires_ts: int | None = None
            if expires_at_str:
                try:
                    expires_dt = datetime.fromisoformat(
                        expires_at_str.replace("Z", "+00:00")
                    )
                    expires_ts = int(expires_dt.timestamp())
                    if expires_dt <= now:
                        prev = self._known.pop(auction_id, None)
                        if prev and prev[2]:
                            try:
                                await _unping_and_delete(
                                    channel.get_partial_message(prev[2])
                                )
                            except Exception:
                                pass
                        continue
                except (ValueError, TypeError):
                    pass

            placer_id = str(auction.get("country") or "")

            # Skip contracts placed by enemies of NL
            if placer_id in self._enemy_ids:
                prev = self._known.pop(auction_id, None)
                if prev and prev[2]:
                    try:
                        await _unping_and_delete(channel.get_partial_message(prev[2]))
                    except Exception:
                        pass
                continue

            # Show all contracts not placed by enemies.
            # is_nl / is_ally only affects which ping roles are used.
            is_nl = nl_country_id and placer_id == nl_country_id
            is_ally = (not is_nl) and (placer_id in self._protected_ids)

            per_k = float(
                auction.get("currentPerK") or auction.get("initialPerK") or 0
            )
            budget = float(
                auction.get("currentPayout") or auction.get("budget") or 0
            )

            # Skip re-post if nothing meaningful changed AND message still exists
            prev = self._known.get(auction_id)
            is_new = prev is None  # only ping on the very first send
            if prev is not None:
                prev_per_k, prev_budget, _prev_msg_id = prev
                if abs(per_k - prev_per_k) < 0.01 and abs(budget - prev_budget) < 1:
                    if _prev_msg_id is not None:
                        try:
                            await channel.fetch_message(_prev_msg_id)
                            continue  # prices unchanged and message still present
                        except discord.NotFound:
                            # message was deleted externally; fall through to re-post
                            prev = (prev_per_k, prev_budget, None)
                            self._known[auction_id] = prev
                        except Exception:
                            continue  # unknown error; assume message is fine
                    # _prev_msg_id is None: no current message, fall through to post

            # ── Build embed ───────────────────────────────────────────
            battle_id = str(auction.get("battle") or "")
            for_country_id = str(auction.get("forCountry") or placer_id)
            for_side = auction.get("forCountrySide", "")
            min_dmg = auction.get("minimumDamage") or 0
            prof_only = bool(auction.get("professionalsOnly"))

            def _cname(cid: str) -> str:
                return (
                    self._country_names.get(cid) or (cid[:8] if cid else "?")
                )

            placer_name = _cname(placer_id)
            for_country_name = _cname(for_country_id)

            side_nl = (
                "aanvaller"
                if for_side == "attacker"
                else ("verdediger" if for_side == "defender" else for_side or "?")
            )

            # ── Fetch battle info (region + enemy) ────────────────────
            region_name: str | None = None
            enemy_id: str | None = None
            if battle_id:
                b_region, b_atk, b_def = await self._get_battle_info(battle_id)
                region_name = b_region
                if for_side == "attacker":
                    enemy_id = b_def
                elif for_side == "defender":
                    enemy_id = b_atk

            lines: list[str] = [f"**Geplaatst door:** {placer_name}"]
            if for_country_id != placer_id:
                lines.append(f"**Voor:** {for_country_name}")
            lines.append(f"**Zijde:** {side_nl}")
            if region_name:
                lines.append(f"**Regio:** {region_name}")
            if enemy_id:
                lines.append(f"**Tegenstander:** {_cname(enemy_id)}")
            if per_k > 0:
                lines.append(f"**Vergoeding:** {per_k:g} CC per 1k schade")
            if budget > 0:
                lines.append(f"**Budget:** {budget:,.0f} CC")
            if min_dmg:
                lines.append(f"**Minimum schade:** {min_dmg:,}")
            if prof_only:
                lines.append("⚠️ **Alleen professionele MU's**")
            if expires_ts:
                lines.append(f"**Verloopt:** <t:{expires_ts}:R>")

            embed = discord.Embed(
                title=f"⚔️ Contract: {placer_name}",
                description="\n".join(lines),
                url=_BATTLE_URL.format(battle_id=battle_id) if battle_id else None,
                colour=discord.Colour(0xe67e22),
                timestamp=now,
            )
            embed.set_footer(text=f"{_FOOTER_PREFIX}{auction_id}")

            # ── Determine ping roles ──────────────────────────────────
            guild = getattr(channel, "guild", None)
            ping_parts: list[str] = []
            if guild:
                r_all = discord.utils.get(guild.roles, name=_ROLE_ALL)
                r_nl = discord.utils.get(guild.roles, name=_ROLE_NL)
                if r_all:
                    ping_parts.append(r_all.mention)
                if is_nl and r_nl:
                    ping_parts.append(r_nl.mention)
            # Only ping on the very first send — not on edits or re-posts
            content = " ".join(ping_parts) if (ping_parts and is_new) else None

            # ── Edit existing message or send new ─────────────────────
            if prev is not None and prev[2] is not None:
                try:
                    partial = channel.get_partial_message(prev[2])
                    await partial.edit(content=content, embed=embed)
                    self._known[auction_id] = (per_k, budget, prev[2])
                    logger.info(
                        "contracts_poll: edited message %s for auction %s",
                        prev[2],
                        auction_id,
                    )
                    continue
                except discord.NotFound:
                    # Message was deleted; clear msg_id and fall through to re-send
                    self._known[auction_id] = (per_k, budget, None)
                    prev = self._known[auction_id]
                except Exception as exc:
                    logger.warning(
                        "contracts_poll: failed to edit message for auction %s, "
                        "sending new: %s",
                        auction_id,
                        exc,
                    )

            try:
                msg = await channel.send(content=content, embed=embed)
                self._known[auction_id] = (per_k, budget, msg.id)
                logger.info(
                    "contracts_poll: posted auction %s (per_k=%.2f, budget=%.0f)",
                    auction_id,
                    per_k,
                    budget,
                )
            except Exception as exc:
                logger.warning(
                    "contracts_poll: failed to send embed for auction %s: %s",
                    auction_id,
                    exc,
                )
                self._known[auction_id] = (per_k, budget, None)


async def setup(bot) -> None:
    await bot.add_cog(ContractsCog(bot))
