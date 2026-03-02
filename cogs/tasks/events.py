"""Background task: game event poll (battles, wars, peace, etc.)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# ── Event-poll constants ──────────────────────────────────────────────────────

_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"
_WAR_URL = "https://app.warera.io/war/{war_id}"

_EVENT_POLL_TYPES = [
    "battleOpened",
    "warDeclared",
    "peaceMade",
    "peace_agreement",
    "regionTransfer",
    "depositDiscovered",
    "depositDepleted",
    "allianceBroken",
    "allianceFormed",
    "regionLiberated",
    "resistanceIncreased",
    "resistanceDecreased",
    "countryMoneyTransfer",
    "newPresident",
    "revolutionStarted",
    "revolutionEnded",
    "financedRevolt",
]

_EVENT_LABELS: dict[str, str] = {
    "battleOpened": "⚔️ Slag geopend",
    "warDeclared": "🚨 Oorlog verklaard",
    "peaceMade": "🕊️ Vrede gesloten",
    "peace_agreement": "🕊️ Vredesakkoord",
    "regionTransfer": "🗺️ Regio overgedragen",
    "depositDiscovered": "⚒️ Deposit ontdekt",
    "depositDepleted": "📦 Deposit uitgeput",
    "allianceBroken": "💔 Alliantie verbroken",
    "allianceFormed": "🤝 Alliantie gesloten",
    "regionLiberated": "🏳️ Regio bevrijd",
    "resistanceIncreased": "📈 Verzet toegenomen",
    "resistanceDecreased": "📉 Verzet afgenomen",
    "countryMoneyTransfer": "💰 Geldtransactie",
    "newPresident": "🏛️ Nieuwe president",
    "revolutionStarted": "🔥 Revolte gestart",
    "revolutionEnded": "✅ Revolte beëindigd",
    "financedRevolt": "💸 Revolte gefinancierd",
}

_EVENT_TYPE_ALIASES: dict[str, str] = {
    "battleopened": "battleOpened",
    "wardeclared": "warDeclared",
    "peacemade": "peaceMade",
    "peace_agreement": "peace_agreement",
    "peaceagreement": "peace_agreement",
    "regiontransfer": "regionTransfer",
    "depositdiscovered": "depositDiscovered",
    "depositdepleted": "depositDepleted",
    "alliancebroken": "allianceBroken",
    "allianceformed": "allianceFormed",
    "regionliberated": "regionLiberated",
    "resistanceincreased": "resistanceIncreased",
    "resistancedecreased": "resistanceDecreased",
    "countrymoneytransfer": "countryMoneyTransfer",
    "newpresident": "newPresident",
    "revolutionstarted": "revolutionStarted",
    "revolutionended": "revolutionEnded",
    "financedrevolt": "financedRevolt",
}

_EVENT_TYPE_TO_CATEGORY: dict[str, str] = {
    "battleOpened": "battle",
    "warDeclared": "war",
    "peaceMade": "peace",
    "peace_agreement": "peace",
    "regionTransfer": "transfer",
    "depositDiscovered": "deposit",
    "depositDepleted": "deposit",
    "allianceBroken": "alliance",
    "allianceFormed": "alliance",
    "regionLiberated": "liberated",
    "resistanceIncreased": "resistance",
    "resistanceDecreased": "resistance",
    "countryMoneyTransfer": "money",
    "newPresident": "president",
    "revolutionStarted": "revolution",
    "revolutionEnded": "revolution",
    "financedRevolt": "revolt",
}


class EventTasks(TaskCogBase, name="event_tasks"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.event_poll.start()

    def cog_unload(self) -> None:
        self.event_poll.cancel()

    # ------------------------------------------------------------------ #
    # Event poll (every minute)                                            #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=1)
    async def event_poll(self) -> None:
        """Poll for new war/battle events and post them to the events channel."""
        if not self._client or not self._db:
            return
        try:
            await self._run_event_poll()
        except Exception:
            logger.exception("event_poll: unexpected error")

    @event_poll.before_loop
    async def before_event_poll(self) -> None:
        await self._wait_for_services()
        # Align to the next full minute boundary.
        now = datetime.now(timezone.utc)
        await asyncio.sleep(max(1.0, 60 - now.second - now.microsecond / 1_000_000))

    async def run_event_poll(self) -> None:
        """Public wrapper so /peil and debug commands can trigger an event poll."""
        await self._run_event_poll()

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_event_type(event: dict) -> str:
        """Extract and normalize event type from varying API payload shapes."""
        if not isinstance(event, dict):
            return "unknown"
        edata = event.get("data") or event.get("eventData") or {}
        raw = (
            event.get("type")
            or event.get("eventType")
            or event.get("event_type")
            or (edata.get("type") if isinstance(edata, dict) else None)
            or (edata.get("eventType") if isinstance(edata, dict) else None)
            or "unknown"
        )
        normalized = str(raw).strip()
        key = normalized.lower()
        return _EVENT_TYPE_ALIASES.get(key, normalized)

    async def _run_event_poll(self) -> None:
        channel_id = self.config.get("channels", {}).get("events")
        if not channel_id:
            logger.warning("event_poll: events channel ID not configured")
            return
        nl_country_id = self.config.get("nl_country_id")

        # Build country_id → name lookup
        country_names: dict[str, str] = {}
        try:
            c_resp = await self._client.get("/country.getAllCountries")
            c_data: list = []
            if isinstance(c_resp, dict):
                c_inner = c_resp.get("result", c_resp)
                c_data = (
                    c_inner.get("data", c_inner)
                    if isinstance(c_inner, dict)
                    else c_resp
                )
            if isinstance(c_data, list):
                for c in c_data:
                    if isinstance(c, dict):
                        cid = c.get("_id") or c.get("id")
                        cname = c.get("name") or c.get("shortName")
                        if cid and cname:
                            country_names[str(cid)] = str(cname)
        except Exception:
            logger.debug("event_poll: could not build country name cache")

        try:
            resp = await self._client.get(
                "/event.getEventsPaginated",
                params={
                    "input": json.dumps(
                        {
                            "limit": 20,
                            "eventTypes": _EVENT_POLL_TYPES,
                        }
                    )
                },
            )
        except Exception as exc:
            logger.warning("event_poll: failed to fetch events: %s", exc)
            return

        data: dict = {}
        if isinstance(resp, dict):
            inner = resp.get("result", {})
            data = inner.get("data", inner) if isinstance(inner, dict) else resp
        items: list = data.get("items") or data.get("events") or []
        if not items:
            logger.warning("event_poll: no events returned from API")
            return

        # Startup / catch-up block — fire on first boot and after !reset_events
        category_latest: dict[str, tuple[dict, str]] = {}
        all_eids: list[str] = []
        for event in items:
            eid = str(event.get("id") or event.get("_id") or "")
            if not eid:
                continue
            event_type = self._extract_event_type(event)
            cat = _EVENT_TYPE_TO_CATEGORY.get(event_type)
            if cat and cat not in category_latest and event_type in _EVENT_LABELS:
                if self._event_involves_nl(event, nl_country_id):
                    category_latest[cat] = (event, eid)
            all_eids.append(eid)

        uninit_cats = {
            cat
            for cat in category_latest
            if not await self._db.get_poll_state(f"event_cat_init_{cat}")
        }

        if uninit_cats:
            posted = 0
            for cat, (event, eid) in category_latest.items():
                if cat not in uninit_cats:
                    continue
                await self._post_event(event, eid, channel_id, country_names)
                await self._db.set_poll_state(f"event_cat_init_{cat}", "1")
                posted += 1
                await asyncio.sleep(0.5)
            for eid in all_eids:
                await self._db.mark_event_seen(eid)
            logger.info(
                "event_poll: catch-up — %d events found, %d categories announced",
                len(all_eids),
                posted,
            )
            return

        for event in reversed(items):
            eid = str(event.get("id") or event.get("_id") or "")
            if not eid or await self._db.has_seen_event(eid):
                if not eid:
                    logger.warning("event_poll: skipping event with no ID: %s", event)
                continue
            if not self._event_involves_nl(event, nl_country_id):
                await self._db.mark_event_seen(eid)
                continue
            event_type = self._extract_event_type(event)
            if event_type not in _EVENT_LABELS:
                logger.warning(
                    "event_poll: skipping unsupported event type '%s' (id=%s)",
                    event_type,
                    eid,
                )
                await self._db.mark_event_seen(eid)
                continue
            await self._post_event(event, eid, channel_id, country_names)
            await self._db.mark_event_seen(eid)
            await asyncio.sleep(0.5)

    async def _get_mu_thumbnails(self, mu_ids: list[str]) -> dict[str, str]:
        """Return a dict mapping MU IDs to their avatar URLs."""
        if not mu_ids or not self._client:
            return {}
        inputs = [{"muId": mu_id} for mu_id in mu_ids]
        try:
            results = await self._client.batch_get(
                "/mu.getById",
                inputs,
                batch_size=30,
                chunk_sleep=0.5,
            )
        except Exception as exc:
            logger.warning("_get_mu_thumbnails: batch_get failed: %s", exc)
            return {}
        thumbnails = {}
        for mu_id, data in zip(mu_ids, results):
            if isinstance(data, dict):
                avatar_url = data.get("avatarUrl")
                if avatar_url:
                    thumbnails[mu_id] = avatar_url
        return thumbnails

    def _event_involves_nl(self, event: dict, nl_id: str) -> bool:
        """Return True if nl_id appears in any country field of the event."""
        if not nl_id:
            return True
        edata: dict = event.get("data") or {}
        if not isinstance(edata, dict):
            edata = {}
        country_keys = (
            "attackerCountry",
            "attackerCountryId",
            "defenderCountry",
            "defenderCountryId",
            "country",
            "countryId",
        )
        for src in (edata, event):
            for key in country_keys:
                val = src.get(key)
                if isinstance(val, str) and val == nl_id:
                    return True
                if isinstance(val, dict):
                    cid = val.get("_id") or val.get("id")
                    if cid and str(cid) == nl_id:
                        return True
            for c in src.get("countries") or []:
                if str(c) == nl_id:
                    return True
        return False

    async def _post_event(
        self,
        event: dict,
        event_id: str,
        channel_id: int,
        country_names: dict[str, str] | None = None,
    ) -> None:
        """Build and post an embed for a single game event."""
        event_type = self._extract_event_type(event)
        label = _EVENT_LABELS.get(event_type, f"🔔 {event_type}")
        cn = country_names or {}

        edata: dict = event.get("data") or {}
        if not isinstance(edata, dict):
            edata = {}

        def _s(*keys: str) -> str | None:
            for k in keys:
                for src in (edata, event):
                    v = src.get(k)
                    if v and isinstance(v, str):
                        return v
            return None

        def _num(*keys: str) -> str | None:
            for k in keys:
                for src in (edata, event):
                    v = src.get(k)
                    if v is not None and v != "":
                        return str(v)
            return None

        def _obj_name(key: str) -> str | None:
            obj = event.get(key)
            if isinstance(obj, dict):
                return obj.get("name") or obj.get("shortName")
            return None

        c_list: list[str] = [
            str(c)
            for c in (edata.get("countries") or event.get("countries") or [])
            if c
        ]

        battle_id = _s("battle", "battleId", "battleID")
        wars_raw = edata.get("wars") or []
        war_id = _s("war", "warId", "warID") or (str(wars_raw[0]) if wars_raw else None)

        atk_id = _s("attackerCountry", "attackerCountryId") or (
            c_list[0] if c_list else None
        )
        dfn_id = _s("defenderCountry", "defenderCountryId") or (
            c_list[1] if len(c_list) > 1 else None
        )
        pres_country_id = _s("country", "countryId")

        atk_name = _obj_name("attackerCountry") or cn.get(atk_id or "")
        dfn_name = _obj_name("defenderCountry") or cn.get(dfn_id or "")

        regions_raw = edata.get("regions") or []
        region_id = _s("region", "regionId", "defenderRegion", "attackerRegion") or (
            str(regions_raw[0]) if regions_raw else None
        )

        region_name: str | None = _obj_name("region")
        if not region_name and region_id and not event_id.startswith("fake_"):
            try:
                rr = await self._client.get(
                    "/region.getById",
                    params={"input": json.dumps({"regionId": region_id})},
                )
                if isinstance(rr, dict):
                    ri = rr.get("result") or rr
                    rd = ri.get("data", ri) if isinstance(ri, dict) else rr
                    if isinstance(rd, dict):
                        region_name = rd.get("name") or rd.get("regionName")
            except Exception:
                logger.debug("event_poll: region lookup failed for %s", region_id)

        atk = atk_name or atk_id or "?"
        dfn = dfn_name or dfn_id or "?"
        rgn = region_name or region_id or "?"

        logger.debug(
            "_post_event: type=%s atk=%s dfn=%s rgn=%s battle=%s war=%s",
            event_type,
            atk,
            dfn,
            rgn,
            battle_id,
            war_id,
        )

        ts_str = event.get("createdAt") or event.get("date") or event.get("timestamp")
        timestamp: datetime | None = None
        if ts_str:
            try:
                timestamp = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except Exception:
                pass

        try:
            await self._db.store_war_event(
                event_id=event_id,
                event_type=event_type,
                battle_id=battle_id,
                war_id=war_id,
                attacker_country_id=atk_id,
                defender_country_id=dfn_id,
                region_id=region_id,
                region_name=region_name,
                attacker_name=atk_name,
                defender_name=dfn_name,
                created_at=ts_str,
                raw_json=json.dumps(event, ensure_ascii=False),
            )
        except Exception:
            logger.exception("event_poll: failed to store event %s", event_id)

        url: str | None = None

        if event_type == "battleOpened":
            color = discord.Color.red()
            description = f"**{atk}** valt **{dfn}** aan in regio **{rgn}**"
            url = _BATTLE_URL.format(battle_id=battle_id) if battle_id else None

        elif event_type == "warDeclared":
            color = discord.Color.dark_red()
            description = f"**{atk}** heeft oorlog verklaard aan **{dfn}**"
            url = _WAR_URL.format(war_id=war_id) if war_id else None

        elif event_type in ("peaceMade", "peace_agreement"):
            color = discord.Color.green()
            description = f"**{atk}** en **{dfn}** hebben vrede gesloten"
            url = _WAR_URL.format(war_id=war_id) if war_id else None

        elif event_type == "regionTransfer":
            amount = _num("amount")
            amount_str = f" voor **{amount}** munt" if amount else ""
            description = (
                f"**{atk}** heeft regio **{rgn}** overgenomen van **{dfn}**{amount_str}"
            )
            color = discord.Color.orange()

        elif event_type == "depositDiscovered":
            item = _s("itemCode", "item", "itemName", "resource") or "onbekend"
            bonus = _num("bonusPercent", "bonus", "bonusValue")
            days = _num("durationDays", "days", "duration")
            b_str = f" +{bonus}%" if bonus else ""
            d_str = f" voor {days} dagen" if days else ""
            description = f"Deposit **{item}{b_str}** ontdekt in regio **{rgn}**{d_str}"
            color = discord.Color.gold()

        elif event_type == "depositDepleted":
            description = f"Het deposit in regio **{rgn}** is uitgeput"
            color = discord.Color.dark_grey()

        elif event_type == "allianceBroken":
            color = discord.Color.dark_orange()
            description = f"De alliantie tussen **{atk}** en **{dfn}** is verbroken"

        elif event_type == "allianceFormed":
            color = discord.Color.teal()
            description = f"**{atk}** en **{dfn}** hebben een alliantie gesloten"

        elif event_type == "regionLiberated":
            color = discord.Color.green()
            description = f"Regio **{rgn}** is bevrijd door **{atk}** van **{dfn}**"

        elif event_type in ("resistanceIncreased", "resistanceDecreased"):
            color = (
                discord.Color.orange()
                if event_type == "resistanceIncreased"
                else discord.Color.greyple()
            )
            arrow = "📈" if event_type == "resistanceIncreased" else "📉"
            res = _num("resistanceValue", "resistance", "currentResistance", "value")
            val = f" — verzet: **{res}**" if res else ""
            description = f"{arrow} Verzet in regio **{rgn}**{val}"

        elif event_type == "countryMoneyTransfer":
            amount = _num("money", "amount", "coins", "gold")
            amt_str = f" **{amount}** munten" if amount else ""
            description = f"**{atk}** heeft{amt_str} overgemaakt aan **{dfn}**"
            color = discord.Color.yellow()

        elif event_type == "newPresident":
            pres_name = _s("presidentName", "president", "citizenName", "name")
            country = (
                cn.get(pres_country_id or "")
                or pres_country_id
                or atk
                or dfn
                or "Nederland"
            )
            pres_str = f" **{pres_name}**" if pres_name else ""
            description = f"Nieuwe president{pres_str} heeft de macht overgenomen in **{country}**"
            color = discord.Color.blue()

        elif event_type == "revolutionStarted":
            description = f"**{atk}** is een revolte gestart in regio **{rgn}** (bezet door **{dfn}**)"
            color = discord.Color.red()

        elif event_type == "revolutionEnded":
            description = f"De revolte in regio **{rgn}** is beëindigd"
            color = discord.Color.greyple()

        elif event_type == "financedRevolt":
            description = f"**{atk}** heeft een revolte gefinancierd in regio **{rgn}**"
            color = discord.Color.dark_red()

        else:
            color = discord.Color.blurple()
            description = "Nieuw event ontvangen."

        embed = discord.Embed(
            title=label,
            description=description,
            color=color,
            timestamp=timestamp or datetime.now(timezone.utc),
        )
        embed.set_footer(text="WarEra Events")

        view = discord.ui.View()
        if url:
            view.add_item(
                discord.ui.Button(
                    label="Bekijk in game",
                    url=url,
                    style=discord.ButtonStyle.link,
                )
            )

        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed, view=view if url else None)
                    logger.info(
                        "event_poll: posted %s (id=%s) to guild %s",
                        event_type,
                        event_id,
                        guild.name,
                    )
                except Exception:
                    logger.exception(
                        "event_poll: failed to post to guild %s", guild.name
                    )


async def setup(bot) -> None:
    """Add the EventTasks cog to the bot."""
    await bot.add_cog(EventTasks(bot))
