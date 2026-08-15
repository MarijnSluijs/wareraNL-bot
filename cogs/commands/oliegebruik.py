"""
/oliegebruik command — show hourly oil usage by active NL bunkers and bases.

Two region-scoped upgrade types share this maintenance formula: ``bunker``
(defenseBonus) and ``base`` — a military base, attackBonus — confirmed via
``gameConfig.getGameConfig().upgradesConfig`` to have byte-for-byte identical
``maintenanceCostCountryDevScale`` / ``minimumMaintenanceCost`` tables per
level as bunkers. A third candidate, ``headquarters``, turned out to be scoped
to a military unit (``mu``), not a region — passing it a ``regionId`` returns
nothing — so it's out of scope for a per-region oil report.

Oil maintenance cost formula (per structure per hour). The in-game tooltip
states it plainly per level, e.g. for level 1: "4% of country development,
minus 1 oil" — confirmed against Nigeria's real numbers (currentDevelopment
94.66 → round(0.04 × 94.66) = 4 → minus 1 = 3, matching what the game itself
showed). ``gameConfig.getGameConfig().upgradesConfig.{bunker,base}.levels``
only exposes one numeric field per level besides the scale
(``minimumMaintenanceCost``), and it turns out that field does double duty —
it is *both* the floor applied before the subtraction *and* the flat amount
subtracted afterward:
    raw  = max(round(maintenanceCostCountryDevScale * development),
                minimumMaintenanceCost)
    cost = max(0, raw - minimumMaintenanceCost)
  Level 1: max(0, max(round(0.04 * development), 1)  - 1)
  Level 2: max(0, max(round(0.08 * development), 2)  - 2)
  Level 3: max(0, max(round(0.16 * development), 5)  - 5)
  Level 4: max(0, max(round(0.32 * development), 10) - 10)
  Level 5: max(0, max(round(0.64 * development), 25) - 25)
A country too small for ``round(scale * development)`` to reach the minimum
therefore pays *zero* oil for that structure — the "minimum" is a floor on the
pre-discount amount, not a floor on what's actually charged.

An earlier version of this file removed the subtraction, reasoning that
``gameConfig`` had no separate field to justify it — that reasoning was wrong
(the field is reused, not absent) and the fix was a regression, caught only
because the reported total didn't match the in-game figure. Restored here
with the in-game text as the source of truth, not the config shape.

``development`` is the country's ``currentDevelopment`` from
``country.getCountryById``. That field used to be returned as a flat
``development`` key; the API now splits it into ``currentDevelopment``,
``averageDevelopment`` and ``coreDevelopment`` instead, which is what made this
command silently show "Ontwikkeling kon niet worden bepaald" — the old key
simply doesn't exist anymore, so it always read as 0. ``currentDevelopment``
matches the Nigeria example above exactly, so unlike the subtraction question,
this part is verified, not a guess.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

# (scale, minimum_cost) — mirrors upgradesConfig.{bunker,base}.levels[n]
# .maintenanceCostCountryDevScale / .minimumMaintenanceCost. Both upgrade
# types share this exact table (verified live against gameConfig).
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


def _unwrap(resp) -> dict | list | None:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


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


class OliegebruikCog(CommandCogBase, name="oliegebruik"):
    """Oil usage command for NL active bunkers and military bases."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="oliegebruik",
        description="Toon het huidige olieverbruik van actieve Nederlandse bunkers en militaire bases.",
    )
    async def oliegebruik(self, ctx: Context) -> None:
        if not self._client or self._client.is_available is False:
            await self._send_api_offline(ctx)
            return

        await ctx.defer()

        nl_country_id: str = self.config.get("nl_country_id", "")
        if not nl_country_id:
            await ctx.send("❌ `nl_country_id` niet geconfigureerd.", ephemeral=True)
            return

        # 1. Fetch NL country data (development)
        try:
            country_resp = await asyncio.wait_for(
                self._client.post(
                    "/country.getCountryById",
                    json={"countryId": nl_country_id},
                ),
                timeout=30.0,
            )
        except Exception as exc:
            logger.error("oliegebruik: failed to fetch country: %s", exc)
            await ctx.send("❌ Fout bij ophalen van landsdata.", ephemeral=True)
            return

        country_data = _unwrap(country_resp)
        if not isinstance(country_data, dict):
            await ctx.send("❌ Onverwacht antwoord van country API.", ephemeral=True)
            return

        development: float = float(country_data.get("currentDevelopment") or 0)
        if development <= 0:
            logger.error(
                "oliegebruik: currentDevelopment missing/zero in country payload "
                "(keys=%s)",
                sorted(country_data.keys()),
            )
            await ctx.send("❌ Ontwikkeling van Nederland kon niet worden bepaald.", ephemeral=True)
            return

        # 2. Fetch all regions and find NL-owned ones
        try:
            regions_resp = await asyncio.wait_for(
                self._client.get("/region.getRegionsObject"),
                timeout=60.0,
            )
        except Exception as exc:
            logger.error("oliegebruik: failed to fetch regions: %s", exc)
            await ctx.send("❌ Fout bij ophalen van regio's.", ephemeral=True)
            return

        regions_data = _unwrap(regions_resp)
        if not isinstance(regions_data, dict):
            await ctx.send("❌ Onverwacht antwoord van regio API.", ephemeral=True)
            return

        nl_region_ids: list[tuple[str, str]] = []  # (region_id, region_name)
        for rid, robj in regions_data.items():
            if isinstance(robj, dict) and robj.get("country") == nl_country_id:
                nl_region_ids.append((rid, robj.get("name") or rid))

        if not nl_region_ids:
            await ctx.send("Geen regio's gevonden die door Nederland worden bezit.")
            return

        # 3. Fetch bunker + base status for each NL region
        import aiohttp as _aiohttp

        async def _fetch_upgrade(
            upgrade_type: str, region_id: str, region_name: str
        ) -> dict | None:
            try:
                resp = await asyncio.wait_for(
                    self._client.post(
                        "/upgrade.getUpgradeByTypeAndEntity",
                        json={"upgradeType": upgrade_type, "regionId": region_id},
                    ),
                    timeout=30.0,
                )
                return _unwrap(resp)
            except _aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    return {}
                logger.warning(
                    "oliegebruik: %s fetch failed for %s: %s", upgrade_type, region_name, exc
                )
                return None
            except Exception as exc:
                logger.warning(
                    "oliegebruik: %s fetch failed for %s: %s", upgrade_type, region_name, exc
                )
                return None

        fetch_jobs = [
            (upgrade_type, rid, rname)
            for upgrade_type in _OIL_UPGRADE_TYPES
            for rid, rname in nl_region_ids
        ]
        results = await asyncio.gather(
            *[_fetch_upgrade(ut, rid, rname) for ut, rid, rname in fetch_jobs]
        )

        # 4. Calculate oil usage for active bunkers + bases
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

        # 5. Build embed
        embed = discord.Embed(
            title="🛢️ Olieverbruik actieve bunkers & bases",
            colour=discord.Colour.from_str(
                self.config.get("colors", {}).get("primary", "0xffb612")
            ),
            timestamp=datetime.now(timezone.utc),
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
            # Split into chunks to avoid field value limit
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

        # 6. Projection table
        if total_per_hour > 0:
            h12 = total_per_hour * 12
            h24 = total_per_hour * 24
            d7  = total_per_hour * 24 * 7
            embed.add_field(
                name="📊 Benodigde olie",
                value=(
                    f"**12 uur** — `{h12:.1f}` olie\n"
                    f"**24 uur** — `{h24:.1f}` olie\n"
                    f"**7 dagen** — `{d7:.1f}` olie"
                ),
                inline=False,
            )

        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(OliegebruikCog(bot))
