"""Background task: watch for changes in a country's ruling-party ethics.

Every 5 minutes, fetches ``country.getAllCountries`` (which includes the
``rulingParty`` field) and then batch-fetches ``party.getById`` for all
monitored countries.  When a country's ethics change, a notification embed
is posted in the configured channel.

Production:  monitors Sweden (``se``) and Germany (``de``) by default.
             Channel: 1489316733528576080  |  Role: 1509161736077312020

Testing:     monitors ALL countries.
             Channel: 1474452856584011929  |  Role: 1509162648359534672

Hidden prefix commands (owner / privileged):
  !ethics_add <country_code>     — add a country to the watch list
  !ethics_remove <country_code>  — remove a country from the watch list
  !ethics_list                   — show the current watch list
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context

from cogs.tasks._base import TaskCogBase
from utils.checks import PRIVILEGED_ROLE_IDS

logger = logging.getLogger("discord_bot")

# ---------------------------------------------------------------------------
# Channel / role IDs
# ---------------------------------------------------------------------------
_PROD_CHANNEL_ID = 1489316733528576080
_PROD_ROLE_ID    = 1509161736077312020

_TEST_CHANNEL_ID = 1474452856584011929
_TEST_ROLE_ID    = 1509162648359534672

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DEFAULT_MONITORED: list[str] = ["se", "de"]
_POLL_INTERVAL_MINUTES = 5

_DB_KEY_STATE     = "ethics_watcher_state"
_DB_KEY_MONITORED = "ethics_watcher_monitored"

# Human-readable axis labels (Dutch)
_ETHICS_LABELS: dict[str, str] = {
    "militarism":    "Militarisme",
    "isolationism":  "Isolationisme",
    "imperialism":   "Imperialisme",
    "industrialism": "Industrialisme",
}

# Descriptive label per axis per value level (game-accurate names)
_ETHICS_VALUE_LABELS: dict[str, dict[int, str]] = {
    "militarism": {
        2:  "Fanatic Expansionist",
        1:  "Expansionist",
        0:  "Neutraal",
        -1: "Pacifist",
        -2: "Fanatic Pacifist",
    },
    "isolationism": {
        2:  "Fanatic Diplomatic",
        1:  "Diplomatic",
        0:  "Neutraal",
        -1: "Isolationist",
        -2: "Fanatic Isolationist",
    },
    "imperialism": {
        2:  "Fanatic Imperialist",
        1:  "Imperialist",
        0:  "Neutraal",
        -1: "Republican",
        -2: "Fanatic Republican",
    },
    "industrialism": {
        2:  "Fanatic Industrialist",
        1:  "Industrialist",
        0:  "Neutraal",
        -1: "Agrarian",
        -2: "Fanatic Agrarian",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _owner_or_privileged(ctx: Context) -> bool:
    if await ctx.bot.is_owner(ctx.author):
        return True
    return isinstance(ctx.author, discord.Member) and bool(
        {r.id for r in ctx.author.roles} & PRIVILEGED_ROLE_IDS
    )


def _extract_country_list(resp: object) -> list[dict]:
    """Extract the list of country dicts from an API response."""
    if isinstance(resp, list):
        return [r for r in resp if isinstance(r, dict)]
    if isinstance(resp, dict):
        inner = resp.get("result", {})
        if isinstance(inner, dict):
            data = inner.get("data", resp)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        for key in ("data", "countries"):
            v = resp.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _value_label(axis: str, v: int) -> str:
    """Return a descriptive label like 'Fanatiek Pacifistisch' for an axis value."""
    by_axis = _ETHICS_VALUE_LABELS.get(axis)
    if by_axis:
        return by_axis.get(v, str(v))
    return f"+{v}" if v > 0 else str(v)


def _ethics_diff(
    old: dict[str, int], new: dict[str, int]
) -> list[tuple[str, str, int, int]]:
    """Return (axis, axis_label, old_val, new_val) for each changed axis."""
    axes = sorted(set(list(old.keys()) + list(new.keys())))
    return [
        (axis, _ETHICS_LABELS.get(axis, axis), old.get(axis, 0), new.get(axis, 0))
        for axis in axes
        if old.get(axis, 0) != new.get(axis, 0)
    ]


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class EthicsWatcherTasks(TaskCogBase, name="ethics_watcher_tasks"):
    """Watches for changes in countries' ruling-party ethics."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.ethics_watch_loop.start()

    def cog_unload(self) -> None:
        self.ethics_watch_loop.cancel()

    @tasks.loop(minutes=_POLL_INTERVAL_MINUTES)
    async def ethics_watch_loop(self) -> None:
        try:
            await self._check_ethics()
        except Exception:
            logger.exception("ethics_watcher: unexpected error in loop")

    @ethics_watch_loop.before_loop
    async def before_ethics_watch_loop(self) -> None:
        await self._wait_for_services()

    # ------------------------------------------------------------------ #
    # Persistence helpers                                                  #
    # ------------------------------------------------------------------ #

    async def _get_monitored_codes(self) -> set[str]:
        """Load monitored country codes from DB; falls back to the default list."""
        if not self._db:
            return set(_DEFAULT_MONITORED)
        try:
            stored = await self._db.get_poll_state(_DB_KEY_MONITORED)
            if stored is not None:
                codes = json.loads(stored)
                return {str(c).lower() for c in codes if c}
        except Exception:
            logger.exception("ethics_watcher: failed to load monitored codes from DB")
        return set(_DEFAULT_MONITORED)

    async def _save_monitored_codes(self, codes: set[str]) -> None:
        if not self._db:
            return
        try:
            await self._db.set_poll_state(
                _DB_KEY_MONITORED, json.dumps(sorted(codes))
            )
        except Exception:
            logger.exception("ethics_watcher: failed to save monitored codes")

    # ------------------------------------------------------------------ #
    # Core polling logic                                                   #
    # ------------------------------------------------------------------ #

    async def _check_ethics(self) -> None:
        if not self._client or not self._db:
            return

        testing: bool = getattr(self.bot, "testing", False)

        # 1. Fetch all countries (includes rulingParty field)
        try:
            resp = await asyncio.wait_for(
                self._client.get("/country.getAllCountries"),
                timeout=20.0,
            )
        except Exception as exc:
            logger.warning("ethics_watcher: failed to fetch countries: %s", exc)
            return

        country_list = _extract_country_list(resp)
        if not country_list:
            logger.debug("ethics_watcher: empty country list")
            return

        # 2. Determine which countries to monitor
        if testing:
            monitored_codes = {
                (c.get("code") or "").lower()
                for c in country_list
                if c.get("code")
            }
        else:
            monitored_codes = await self._get_monitored_codes()

        # 3. Filter to monitored countries that have a ruling party
        target_countries: list[dict] = [
            c for c in country_list
            if (c.get("code") or "").lower() in monitored_codes
            and c.get("rulingParty")
        ]

        if not target_countries:
            logger.debug("ethics_watcher: no monitored countries with a ruling party")
            return

        # 4. Batch-fetch party data for all targeted countries
        party_ids = [c["rulingParty"] for c in target_countries]
        try:
            party_results = await asyncio.wait_for(
                self._client.batch_get(
                    "party.getById",
                    [{"partyId": pid} for pid in party_ids],
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("ethics_watcher: batch party fetch failed: %s", exc)
            return

        # 5. Load stored state
        stored_state: dict[str, dict] = {}
        first_run = True
        try:
            raw_state = await self._db.get_poll_state(_DB_KEY_STATE)
            if raw_state is not None:
                first_run = False
                stored_state = json.loads(raw_state)
        except Exception:
            logger.exception("ethics_watcher: failed to load stored state")

        if first_run:
            logger.info(
                "ethics_watcher: first run — initialising state for %d countries, "
                "no notifications sent",
                len(target_countries),
            )

        # 6. Compare ethics and queue notifications
        new_state: dict[str, dict] = dict(stored_state)
        notifications: list[tuple[dict, dict, dict]] = []
        # Each entry: (country_dict, new_entry, old_entry)

        for country, party_resp in zip(target_countries, party_results):
            country_id: str = country.get("_id") or country.get("id") or ""
            country_name: str = country.get("name") or country_id
            if not country_id:
                continue

            party_data = party_resp if isinstance(party_resp, dict) else {}
            raw_ethics = party_data.get("ethics")
            new_ethics: dict[str, int] = (
                {k: int(v) for k, v in raw_ethics.items()}
                if isinstance(raw_ethics, dict)
                else {}
            )

            new_entry: dict = {
                "ethics":       new_ethics,
                "party_id":     country.get("rulingParty") or "",
                "party_name":   party_data.get("name") or "",
                "country_name": country_name,
            }

            old_entry = stored_state.get(country_id)
            new_state[country_id] = new_entry

            if old_entry is None:
                # Country not yet tracked — initialise silently
                continue

            old_ethics: dict[str, int] = old_entry.get("ethics") or {}
            if old_ethics != new_ethics and not first_run:
                notifications.append((country, new_entry, old_entry))

        # 7. Send notifications
        for country, new_entry, old_entry in notifications:
            try:
                await self._notify_ethics_change(country, new_entry, old_entry)
            except Exception:
                logger.exception(
                    "ethics_watcher: failed to send notification for %s",
                    country.get("name"),
                )

        # 8. Persist updated state
        try:
            await self._db.set_poll_state(_DB_KEY_STATE, json.dumps(new_state))
        except Exception:
            logger.exception("ethics_watcher: failed to save state")

    async def _notify_ethics_change(
        self,
        country: dict,
        new_entry: dict,
        old_entry: dict,
    ) -> None:
        testing: bool = getattr(self.bot, "testing", False)
        channel_id = _TEST_CHANNEL_ID if testing else _PROD_CHANNEL_ID
        role_id    = _TEST_ROLE_ID    if testing else _PROD_ROLE_ID

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            logger.warning("ethics_watcher: channel %d not found in cache", channel_id)
            return

        country_id   = country.get("_id") or country.get("id") or ""
        country_name = new_entry["country_name"]
        country_url  = f"https://app.warera.io/country/{country_id}"

        old_ethics: dict[str, int] = old_entry.get("ethics") or {}
        new_ethics: dict[str, int] = new_entry["ethics"]
        old_party_name = old_entry.get("party_name") or "?"
        new_party_name = new_entry.get("party_name") or "?"

        changes = _ethics_diff(old_ethics, new_ethics)

        embed = discord.Embed(
            title=f"🗳️ Ethiek gewijzigd: {country_name}",
            url=country_url,
            colour=discord.Colour.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        # Party line: highlight if the ruling party itself also changed
        if old_entry.get("party_id") != new_entry.get("party_id"):
            embed.add_field(
                name="Regerende partij",
                value=f"~~{old_party_name}~~ → **{new_party_name}**",
                inline=False,
            )
        else:
            embed.add_field(
                name="Regerende partij",
                value=f"**{new_party_name}**",
                inline=False,
            )

        # Changed axes
        if changes:
            lines = [
                f"**{label}**: {_value_label(axis, old_v)} → **{_value_label(axis, new_v)}**"
                for axis, label, old_v, new_v in changes
            ]
            embed.add_field(
                name="Gewijzigde ethiek",
                value="\n".join(lines),
                inline=False,
            )

        # Current ethics snapshot — only show non-neutral axes
        active_axes = [
            ax
            for ax in sorted(set(list(old_ethics.keys()) + list(new_ethics.keys())))
            if new_ethics.get(ax, 0) != 0
        ]
        if active_axes:
            current_lines = [
                f"{_ETHICS_LABELS.get(ax, ax)}: **{_value_label(ax, new_ethics.get(ax, 0))}**"
                for ax in active_axes
            ]
            embed.add_field(
                name="Huidige ethiek",
                value="\n".join(current_lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Huidige ethiek",
                value="**Unethical**",
                inline=False,
            )

        try:
            await channel.send(content=f"<@&{role_id}>", embed=embed)
            logger.info(
                "ethics_watcher: posted ethics-change notification for %s",
                country_name,
            )
        except discord.HTTPException:
            logger.exception(
                "ethics_watcher: failed to send notification for %s", country_name
            )

    # ------------------------------------------------------------------ #
    # Hidden prefix commands                                               #
    # ------------------------------------------------------------------ #

    async def _resolve_country(
        self, query: str
    ) -> tuple[str, str] | None:
        """Resolve a user-supplied string (code, _id, or name) to (code, name).

        Returns None if the country could not be found or the API call failed.
        """
        country_list = await self._fetch_country_list()
        return self._find_in_country_list(query.strip().lower(), country_list)

    async def _fetch_country_list(self) -> list[dict]:
        """Fetch all countries from the API, or return [] on failure."""
        if not self._client:
            return []
        try:
            resp = await asyncio.wait_for(
                self._client.get("/country.getAllCountries"),
                timeout=20.0,
            )
            return _extract_country_list(resp)
        except Exception:
            return []

    @staticmethod
    def _find_in_country_list(
        query: str, country_list: list[dict]
    ) -> tuple[str, str] | None:
        """Match a lowercased query (code, _id, or name) against a country list."""
        q = query.lower()
        for c in country_list:
            if (
                (c.get("code") or "").lower() == q
                or (c.get("_id") or "").lower() == q
                or (c.get("name") or "").lower() == q
            ):
                code = (c.get("code") or "").lower()
                name = c.get("name") or code or q
                return code, name
        return None

    @staticmethod
    def _resolve_stored_entry(
        stored: str, country_list: list[dict]
    ) -> tuple[str, str]:
        """Resolve a stored value (code or legacy _id) to (canonical_code, name).

        Falls back to (stored, stored) when the country cannot be identified.
        """
        result = EthicsWatcherTasks._find_in_country_list(stored, country_list)
        return result if result is not None else (stored, stored)

    @commands.command(name="ethics_add", hidden=True)
    @commands.check(_owner_or_privileged)
    async def cmd_ethics_add(self, ctx: Context, *, query: str) -> None:
        """Add a country to the ethics watch list (accepts code, ID, or name)."""
        if not self._db:
            await ctx.send("❌ Database niet beschikbaar.")
            return
        resolved = await self._resolve_country(query)
        if resolved is None:
            await ctx.send(f"❌ Land `{query}` niet gevonden.")
            return
        code, name = resolved
        codes = await self._get_monitored_codes()
        if code in codes:
            await ctx.send(f"ℹ️ **{name}** (`{code}`) wordt al bewaakt.")
            return
        codes.add(code)
        await self._save_monitored_codes(codes)
        await ctx.send(f"✅ **{name}** (`{code}`) toegevoegd aan de bewakingslijst.")
        logger.info("ethics_watcher: %s added '%s' (%s) to monitored list", ctx.author, name, code)

    @commands.command(name="ethics_remove", hidden=True)
    @commands.check(_owner_or_privileged)
    async def cmd_ethics_remove(self, ctx: Context, *, query: str) -> None:
        """Remove a country from the ethics watch list (accepts code, ID, or name)."""
        if not self._db:
            await ctx.send("❌ Database niet beschikbaar.")
            return
        country_list = await self._fetch_country_list()
        resolved = self._find_in_country_list(query.strip().lower(), country_list)
        if resolved is None:
            await ctx.send(f"❌ Land `{query}` niet gevonden.")
            return
        code, name = resolved
        codes = await self._get_monitored_codes()
        # The stored value may be the canonical code OR a legacy _id string —
        # find whichever stored entry resolves to the same canonical code.
        to_remove = next(
            (s for s in codes if self._resolve_stored_entry(s, country_list)[0] == code),
            None,
        )
        if to_remove is None:
            await ctx.send(f"ℹ️ **{name}** (`{code}`) staat niet op de bewakingslijst.")
            return
        codes.discard(to_remove)
        await self._save_monitored_codes(codes)
        await ctx.send(f"✅ **{name}** (`{code}`) verwijderd van de bewakingslijst.")
        logger.info("ethics_watcher: %s removed '%s' (%s) from monitored list", ctx.author, name, code)

    @commands.command(name="ethics_list", hidden=True)
    @commands.check(_owner_or_privileged)
    async def cmd_ethics_list(self, ctx: Context) -> None:
        """Show the current ethics watch list."""
        testing: bool = getattr(self.bot, "testing", False)
        if testing:
            await ctx.send("ℹ️ Test-modus: alle landen worden bewaakt.")
            return
        codes = await self._get_monitored_codes()
        if not codes:
            await ctx.send("De bewakingslijst is leeg.")
            return
        country_list = await self._fetch_country_list()
        lines = []
        for stored in sorted(codes):
            resolved_code, resolved_name = self._resolve_stored_entry(stored, country_list)
            lines.append(f"• **{resolved_name}** (`{resolved_code}`)")
        await ctx.send("**Bewakingslijst:**\n" + "\n".join(lines))

    @commands.command(name="ethics_preview", hidden=True)
    @commands.check(_owner_or_privileged)
    async def cmd_ethics_preview(self, ctx: Context, *, query: str) -> None:
        """Force-send a sample ethics-change embed for the given country (code, ID, or name)."""
        if not self._client:
            await ctx.send("❌ API client niet beschikbaar.")
            return

        country_list = await self._fetch_country_list()
        if not country_list:
            await ctx.send("❌ Kan landen niet ophalen.")
            return

        resolved = self._find_in_country_list(query.strip().lower(), country_list)
        if resolved is None:
            await ctx.send(f"❌ Land `{query}` niet gevonden.")
            return
        _code, _name = resolved
        country = next(
            c for c in country_list if (c.get("code") or "").lower() == _code
        )
        if country is None:
            await ctx.send(f"❌ Land `{query}` niet gevonden.")
            return
        if not country.get("rulingParty"):
            await ctx.send(f"❌ **{country.get('name', query)}** heeft geen regerende partij.")
            return

        # Fetch party data
        try:
            results = await asyncio.wait_for(
                self._client.batch_get(
                    "party.getById",
                    [{"partyId": country["rulingParty"]}],
                ),
                timeout=20.0,
            )
        except Exception as exc:
            await ctx.send(f"❌ Kan partijdata niet ophalen: {exc}")
            return

        party_data = results[0] if results and isinstance(results[0], dict) else {}
        raw_ethics = party_data.get("ethics")
        new_ethics: dict[str, int] = (
            {k: int(v) for k, v in raw_ethics.items()}
            if isinstance(raw_ethics, dict)
            else {}
        )

        new_entry: dict = {
            "ethics":       new_ethics,
            "party_id":     country.get("rulingParty") or "",
            "party_name":   party_data.get("name") or "?",
            "country_name": country.get("name") or query,
        }
        # Use all-zero old state so every axis shows as "changed"
        old_entry: dict = {
            "ethics":     {k: 0 for k in new_ethics},
            "party_id":   country.get("rulingParty") or "",
            "party_name": party_data.get("name") or "?",
        }

        await ctx.message.add_reaction("✅")
        await self._notify_ethics_change(country, new_entry, old_entry)


async def setup(bot) -> None:
    await bot.add_cog(EthicsWatcherTasks(bot))
