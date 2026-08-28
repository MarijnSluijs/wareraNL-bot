"""Nigeria-bot feature: post a concise summary whenever someone links a WarEra battle.

Detects battle URLs (``https://app.warera.io/battle/<id>``, with or without
the scheme) anywhere in a message and replies with the two countries
fighting, the round number, this round's damage per side, and the current
ground points — defender on the left, attacker on the right, matching how
the game's own battle page frames it.

Live API only (``battle.getById`` + ``battle.getLiveBattleData`` +
``country.getCountryById`` ×2), same lightweight aiohttp +
``WARERA_API_BASE``/``WARERA_API_KEY`` pattern as the rest of nigeria_bot
(see ``nigeria_bot/oliegebruik.py``) — a battle's live state is exactly the
kind of thing an hourly-swept cache would misrepresent.

Field mapping confirmed live against battle 6a89d3b885b52ed4ae52b8b2
(India attacking Thailand, round 1): ``battle.getById``'s ``defender.country``
/ ``attacker.country`` give the two country IDs;
``battle.getLiveBattleData``'s ``round.defenderDamages``/``attackerDamages``
and ``round.defenderPoints``/``attackerPoints`` give this round's damage and
ground points; round number is ``len(battle.roundHistory) + 1`` (the
currently active round is never itself in ``roundHistory``). The country
object also carries a lowercase ISO 3166-1 alpha-2 ``code`` field (confirmed
live: India → "in", Thailand → "th"), which converts straight to a flag
emoji via Unicode regional indicators — no name→ISO lookup table needed.
"""

from __future__ import annotations

import json
import logging
import os
import re

import aiohttp
import discord
from discord.ext import commands

logger = logging.getLogger("nigeria_bot.battle_watch")

WARERA_API_BASE = "https://api2.warera.io/trpc"
WARERA_API_KEY = os.environ.get("WARERA_API_KEY", "")

_BATTLE_URL_RE = re.compile(
    r"(?:https?://)?app\.warera\.io/battle/([0-9a-f]{24})", re.IGNORECASE
)


def _unwrap(resp: object) -> dict | list | None:
    """Strip a single tRPC result/data envelope."""
    if isinstance(resp, dict):
        inner = resp.get("result", resp)
        if isinstance(inner, dict):
            return inner.get("data", inner)
        return inner
    return resp  # type: ignore[return-value]


async def _get(sess: aiohttp.ClientSession, procedure: str, payload: dict | None = None):
    """GET one tRPC procedure with an optional ``input`` payload."""
    url = f"{WARERA_API_BASE}/{procedure}"
    params = {"input": json.dumps(payload)} if payload is not None else None
    headers = {"x-api-key": WARERA_API_KEY} if WARERA_API_KEY else {}
    async with sess.get(
        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        resp.raise_for_status()
        return _unwrap(await resp.json())


def _fmt_dmg(value: object) -> str:
    v = float(value or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:.0f}"


def _flag(iso_code: object) -> str:
    """Return the flag emoji for a lowercase/uppercase ISO 3166-1 alpha-2 code."""
    code = str(iso_code or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


class BattleWatchCog(commands.Cog, name="battle_watch"):
    """Watches chat for WarEra battle links and posts a live summary."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        match = _BATTLE_URL_RE.search(message.content or "")
        if not match:
            return
        battle_id = match.group(1)

        try:
            embed = await self._build_summary(battle_id)
        except Exception:
            logger.exception("battle_watch: failed to build summary for %s", battle_id)
            return
        if embed is None:
            return

        try:
            await message.channel.send(embed=embed)
        except discord.HTTPException as exc:
            logger.error("battle_watch: failed to send summary for %s: %s", battle_id, exc)

    async def _build_summary(self, battle_id: str) -> discord.Embed | None:
        async with aiohttp.ClientSession() as sess:
            try:
                battle = await _get(sess, "battle.getById", {"battleId": battle_id})
                live = await _get(sess, "battle.getLiveBattleData", {"battleId": battle_id})
            except Exception as exc:
                logger.warning("battle_watch: API call failed for %s: %s", battle_id, exc)
                return None
            if not isinstance(battle, dict) or not isinstance(live, dict):
                return None

            defender_country_id = str((battle.get("defender") or {}).get("country") or "")
            attacker_country_id = str((battle.get("attacker") or {}).get("country") or "")

            defender_name = defender_country_id
            defender_flag = ""
            attacker_name = attacker_country_id
            attacker_flag = ""
            try:
                defender_country = await _get(
                    sess, "country.getCountryById", {"countryId": defender_country_id}
                )
                if isinstance(defender_country, dict):
                    defender_name = defender_country.get("name") or defender_name
                    defender_flag = _flag(defender_country.get("code"))
            except Exception:
                pass
            try:
                attacker_country = await _get(
                    sess, "country.getCountryById", {"countryId": attacker_country_id}
                )
                if isinstance(attacker_country, dict):
                    attacker_name = attacker_country.get("name") or attacker_name
                    attacker_flag = _flag(attacker_country.get("code"))
            except Exception:
                pass

        defender_label = f"{defender_flag} {defender_name}".strip()
        attacker_label = f"{attacker_flag} {attacker_name}".strip()

        live_battle = live.get("battle") or {}
        round_history = live_battle.get("roundHistory") or []
        round_number = len(round_history) + 1

        round_data = live.get("round")
        if not isinstance(round_data, dict):
            # No active round left — the battle has finished.
            embed = discord.Embed(
                title=f"⚔️ {defender_label} vs {attacker_label}",
                description="This battle has ended — no active round left.",
                colour=discord.Colour.dark_grey(),
                url=f"https://app.warera.io/battle/{battle_id}",
            )
            return embed

        defender_damage = round_data.get("defenderDamages")
        attacker_damage = round_data.get("attackerDamages")
        defender_points = round_data.get("defenderPoints") or 0
        attacker_points = round_data.get("attackerPoints") or 0

        embed = discord.Embed(
            title=f"⚔️ {defender_label} vs {attacker_label} — Round {round_number}",
            description=(
                f"**{defender_label}** (defender)  vs  **{attacker_label}** (attacker)\n\n"
                f"🔥 Damage this round: {_fmt_dmg(defender_damage)} — {_fmt_dmg(attacker_damage)}\n"
                f"🚩 Ground points: {defender_points} — {attacker_points}"
            ),
            colour=discord.Colour.dark_red(),
            url=f"https://app.warera.io/battle/{battle_id}",
        )
        return embed


async def setup(bot: commands.Bot) -> BattleWatchCog:
    cog = BattleWatchCog(bot)
    await bot.add_cog(cog)
    return cog
