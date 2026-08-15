"""Slash command /oliegebruik — hourly oil usage by active Nigerian bunkers and bases.

Nigeria-bot equivalent of the main bot's ``cogs/commands/oliegebruik.py``
(which reports on Netherlands). Unlike ``/fabrieken``/``/productie``/
``/damage-projection``, this makes live WarEra API calls on every invocation
rather than reading the hourly-swept ``external.db``: upgrade status
(active/pending/disabled) and level are not part of the hourly census, and a
stale oil figure would be actively misleading for a number people plan fuel
purchases around.

Two region-scoped upgrade types share the maintenance formula: ``bunker``
(defenseBonus) and ``base`` — a military base, attackBonus — confirmed via
``gameConfig.getGameConfig().upgradesConfig`` to have byte-for-byte identical
per-level scale/minimum tables. A third candidate, ``headquarters``, turned
out to be scoped to a military unit rather than a region, so it's excluded.

Formula: ``cost = max(0, max(round(scale * development), minimum) - minimum)``
— the in-game tooltip states this plainly per level ("4% of country
development, minus 1 oil" for level 1), and it was confirmed against
Nigeria's real numbers (currentDevelopment 94.66 → round(0.04 × 94.66) = 4 →
minus 1 = 3, matching the in-game figure exactly). ``minimum`` does double
duty: it floors the raw scaled cost, then is subtracted back off — so a
country too small to reach the minimum via scaling pays *zero* oil, not the
minimum. An earlier version of this file (and the main bot's) dropped that
subtraction on the theory that ``gameConfig`` had no separate field to
justify it; that reasoning was wrong (the field is reused, not absent), and
the fix was a regression, caught only because the reported total (4) didn't
match the real in-game one (3).

Formula and constants (``_OIL_UPGRADE_LEVELS``, ``_oil_cost_per_hour``) are
duplicated from the main bot's oliegebruik.py rather than imported — the two
bots are separately deployed containers with no existing shared-code path
between ``nigeria_bot/`` and ``cogs/``, and the formula is five lines. Keep
both copies in sync if the game ever rebalances these costs; the main bot's
module docstring has the fuller derivation, including why
``currentDevelopment`` was picked over ``averageDevelopment``/``coreDevelopment``.

Uses the same lightweight ``aiohttp`` + ``WARERA_API_BASE``/``WARERA_API_KEY``
pattern already established in ``nigeria_bot/cog.py``, rather than the shared
``services.api_client.APIClient`` the main bot's commands use — nigeria_bot
has never depended on that shared client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("nigeria_bot.oliegebruik")

WARERA_API_BASE = "https://api2.warera.io/trpc"
WARERA_API_KEY = os.environ.get("WARERA_API_KEY", "")

NIGERIA_COUNTRY_ID = "683ddd2c24b5a2e114af15fa"

# (scale, minimum_cost) — mirrors gameConfig.getGameConfig().upgradesConfig
# .{bunker,base}.levels[n].maintenanceCostCountryDevScale / .minimumMaintenanceCost.
# Both upgrade types share this exact table. See cogs/commands/oliegebruik.py
# for the full derivation.
_OIL_UPGRADE_LEVELS: dict[int, tuple[float, int]] = {
    1: (0.04, 1),
    2: (0.08, 2),
    3: (0.16, 5),
    4: (0.32, 10),
    5: (0.64, 25),
}

# upgradeType (API) -> (Dutch label, emoji)
_OIL_UPGRADE_TYPES: dict[str, tuple[str, str]] = {
    "bunker": ("Bunker", "🛢️"),
    "base": ("Militaire basis", "⚔️"),
}


def _oil_cost_per_hour(level: int, development: float) -> float:
    """Return the hourly oil cost for a single active structure of the given level.

    ``minimum`` is used twice: first as a floor on the raw scaled cost, then
    subtracted back off. See the module docstring for why — verified against
    the in-game tooltip and real numbers, not just the game config's shape.
    """
    if level not in _OIL_UPGRADE_LEVELS:
        return 0.0
    scale, minimum = _OIL_UPGRADE_LEVELS[level]
    raw = max(minimum, round(scale * development))
    return float(max(0, raw - minimum))


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
        url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp.raise_for_status()
        return _unwrap(await resp.json())


class OliegebruikCog(commands.Cog, name="oliegebruik"):
    """Cog for the /oliegebruik command (Nigeria-controlled regions)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="oliegebruik",
        description="Toon het huidige olieverbruik van actieve bunkers en bases in door Nigeria beheerste regio's.",
    )
    async def oliegebruik(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            embed = await self._build()
        except Exception:
            logger.exception("oliegebruik: failed to build response")
            embed = discord.Embed(
                title="🛢️ Olieverbruik",
                description="⚠️ Kon de WarEra API niet bevragen. Probeer het later opnieuw.",
                colour=discord.Colour.red(),
            )
        await interaction.followup.send(embed=embed)

    async def _build(self) -> discord.Embed:
        async with aiohttp.ClientSession() as sess:
            try:
                country_data = await _get(
                    sess, "country.getCountryById", {"countryId": NIGERIA_COUNTRY_ID}
                )
            except Exception as exc:
                logger.error("oliegebruik: failed to fetch country: %s", exc)
                return discord.Embed(
                    title="🛢️ Olieverbruik",
                    description="❌ Fout bij ophalen van landsdata.",
                    colour=discord.Colour.red(),
                )
            if not isinstance(country_data, dict):
                return discord.Embed(
                    title="🛢️ Olieverbruik",
                    description="❌ Onverwacht antwoord van country API.",
                    colour=discord.Colour.red(),
                )

            development = float(country_data.get("currentDevelopment") or 0)
            if development <= 0:
                logger.error(
                    "oliegebruik: currentDevelopment missing/zero in country payload "
                    "(keys=%s)",
                    sorted(country_data.keys()),
                )
                return discord.Embed(
                    title="🛢️ Olieverbruik",
                    description="❌ Ontwikkeling van Nigeria kon niet worden bepaald.",
                    colour=discord.Colour.red(),
                )

            try:
                regions_data = await _get(sess, "region.getRegionsObject")
            except Exception as exc:
                logger.error("oliegebruik: failed to fetch regions: %s", exc)
                return discord.Embed(
                    title="🛢️ Olieverbruik",
                    description="❌ Fout bij ophalen van regio's.",
                    colour=discord.Colour.red(),
                )
            if not isinstance(regions_data, dict):
                return discord.Embed(
                    title="🛢️ Olieverbruik",
                    description="❌ Onverwacht antwoord van regio API.",
                    colour=discord.Colour.red(),
                )

            ng_regions: list[tuple[str, str]] = [
                (rid, robj.get("name") or rid)
                for rid, robj in regions_data.items()
                if isinstance(robj, dict) and robj.get("country") == NIGERIA_COUNTRY_ID
            ]
            if not ng_regions:
                return discord.Embed(
                    title="🛢️ Olieverbruik",
                    description="Geen regio's gevonden die door Nigeria worden bezit.",
                    colour=discord.Colour.orange(),
                )

            async def _fetch_upgrade(
                upgrade_type: str, region_id: str, region_name: str
            ) -> dict | None:
                try:
                    return await _get(
                        sess,
                        "upgrade.getUpgradeByTypeAndEntity",
                        {"upgradeType": upgrade_type, "regionId": region_id},
                    )
                except aiohttp.ClientResponseError as exc:
                    if exc.status == 404:
                        return {}
                    logger.warning(
                        "oliegebruik: %s fetch failed for %s: %s",
                        upgrade_type, region_name, exc,
                    )
                    return None
                except Exception as exc:
                    logger.warning(
                        "oliegebruik: %s fetch failed for %s: %s",
                        upgrade_type, region_name, exc,
                    )
                    return None

            fetch_jobs = [
                (upgrade_type, rid, rname)
                for upgrade_type in _OIL_UPGRADE_TYPES
                for rid, rname in ng_regions
            ]
            results = await asyncio.gather(
                *[_fetch_upgrade(ut, rid, rname) for ut, rid, rname in fetch_jobs]
            )

        # (region_name, upgrade_type, level, hourly_cost)
        active: list[tuple[str, str, int, float]] = []
        for (upgrade_type, _rid, rname), data in zip(fetch_jobs, results):
            if not isinstance(data, dict) or not data:
                continue
            status = (data.get("status") or "").lower()
            level = data.get("level") or 0
            if status == "active" and level > 0:
                cost = _oil_cost_per_hour(level, development)
                active.append((rname, upgrade_type, level, cost))
        active.sort(key=lambda t: (-t[3], t[0]))

        total_per_hour = sum(c for _, _, _, c in active)
        subtotals: dict[str, float] = {
            ut: sum(c for _, u, _, c in active if u == ut) for ut in _OIL_UPGRADE_TYPES
        }

        embed = discord.Embed(
            title="🛢️ Olieverbruik actieve bunkers & bases — Nigeria",
            colour=discord.Colour.green(),
        )
        subtotal_lines = "\n".join(
            f"{emoji} {label}: `{subtotals[ut]:.1f}` olie/uur"
            for ut, (label, emoji) in _OIL_UPGRADE_TYPES.items()
        )
        embed.add_field(
            name="⏱️ Verbruik per uur",
            value=f"**`{total_per_hour:.1f}` olie totaal**\n{subtotal_lines}",
            inline=False,
        )

        if active:
            lines = [
                f"{_OIL_UPGRADE_TYPES[ut][1]} **{name}** — {_OIL_UPGRADE_TYPES[ut][0]} "
                f"(niveau {lvl}) — `{cost:.1f}` olie/uur"
                for name, ut, lvl, cost in active
            ]
            chunks: list[list[str]] = [[]]
            for line in lines:
                current_len = sum(len(ln) + 1 for ln in chunks[-1])
                if current_len + len(line) + 1 > 1000 and chunks[-1]:
                    chunks.append([])
                chunks[-1].append(line)
            for i, chunk in enumerate(chunks):
                name_label = "📋 Actieve structuren" if i == 0 else "​"
                embed.add_field(name=name_label, value="\n".join(chunk), inline=False)
        else:
            embed.add_field(
                name="📋 Actieve structuren",
                value="_Geen actieve bunkers of bases gevonden._",
                inline=False,
            )

        if total_per_hour > 0:
            h12 = total_per_hour * 12
            h24 = total_per_hour * 24
            d7 = total_per_hour * 24 * 7
            embed.add_field(
                name="📊 Benodigde olie",
                value=(
                    f"**12 uur** — `{h12:.1f}` olie\n"
                    f"**24 uur** — `{h24:.1f}` olie\n"
                    f"**7 dagen** — `{d7:.1f}` olie"
                ),
                inline=False,
            )

        embed.set_footer(text=f"{len(ng_regions)} Nigeriaanse regio's gecontroleerd")
        return embed


async def setup(bot: commands.Bot) -> OliegebruikCog:
    cog = OliegebruikCog(bot)
    await bot.add_cog(cog)
    return cog
