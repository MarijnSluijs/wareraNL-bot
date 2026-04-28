"""
/oliegebruik command — show hourly oil usage by active NL bunkers.

Oil maintenance cost formula (per bunker per hour):
  Level 1: max(1,  round(0.04 * development)) - 1
  Level 2: max(2,  round(0.08 * development)) - 2
  Level 3: max(5,  round(0.16 * development)) - 5
  Level 4: max(10, round(0.32 * development)) - 10
  Level 5: max(25, round(0.64 * development)) - 25
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

# (scale, minimum_cost, oil_deduction)
_BUNKER_LEVELS: dict[int, tuple[float, int, int]] = {
    1: (0.04, 1,  1),
    2: (0.08, 2,  2),
    3: (0.16, 5,  5),
    4: (0.32, 10, 10),
    5: (0.64, 25, 25),
}


def _unwrap(resp) -> dict | list | None:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _oil_cost_per_hour(level: int, development: float) -> float:
    """Return the hourly oil cost for a single active bunker of the given level."""
    if level not in _BUNKER_LEVELS:
        return 0.0
    scale, minimum, deduction = _BUNKER_LEVELS[level]
    raw = max(minimum, round(scale * development))
    return max(0.0, raw - deduction)


class OliegebruikCog(CommandCogBase, name="oliegebruik"):
    """Oil usage command for NL active bunkers."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="oliegebruik",
        description="Toon het huidige olieverbruik van actieve Nederlandse bunkers.",
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

        development: float = float(country_data.get("development") or 0)
        if development <= 0:
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

        # 3. Fetch bunker status for each NL region
        import aiohttp as _aiohttp

        async def _fetch_bunker(region_id: str, region_name: str) -> dict | None:
            try:
                resp = await asyncio.wait_for(
                    self._client.post(
                        "/upgrade.getUpgradeByTypeAndEntity",
                        json={"upgradeType": "bunker", "regionId": region_id},
                    ),
                    timeout=30.0,
                )
                return _unwrap(resp)
            except _aiohttp.ClientResponseError as exc:
                if exc.status == 404:
                    return {}
                logger.warning("oliegebruik: bunker fetch failed for %s: %s", region_name, exc)
                return None
            except Exception as exc:
                logger.warning("oliegebruik: bunker fetch failed for %s: %s", region_name, exc)
                return None

        results = await asyncio.gather(
            *[_fetch_bunker(rid, rname) for rid, rname in nl_region_ids]
        )

        # 4. Calculate oil usage for active bunkers
        active_bunkers: list[tuple[str, int, float]] = []  # (name, level, hourly_cost)

        for (rid, rname), bunker_data in zip(nl_region_ids, results):
            if not isinstance(bunker_data, dict) or not bunker_data:
                continue
            status = (bunker_data.get("status") or "").lower()
            level = bunker_data.get("level") or 0
            if status == "active" and level > 0:
                cost = _oil_cost_per_hour(level, development)
                active_bunkers.append((rname, level, cost))

        active_bunkers.sort(key=lambda t: (-t[2], t[0]))

        total_per_hour = sum(c for _, _, c in active_bunkers)

        # 5. Build embed
        embed = discord.Embed(
            title="🛢️ Olieverbruik actieve bunkers",
            colour=discord.Colour.from_str(
                self.config.get("colors", {}).get("primary", "0xffb612")
            ),
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name=" Actieve bunkers",
            value=f"`{len(active_bunkers)}`",
            inline=True,
        )
        embed.add_field(
            name="⏱️ Verbruik per uur",
            value=f"`{total_per_hour:.1f}` olie",
            inline=True,
        )

        if active_bunkers:
            lines = [
                f"**{name}** (niveau {lvl}) — `{cost:.1f}` olie/uur"
                for name, lvl, cost in active_bunkers
            ]
            # Split into chunks to avoid field value limit
            chunks: list[list[str]] = [[]]
            for line in lines:
                current_len = sum(len(l) + 1 for l in chunks[-1])
                if current_len + len(line) + 1 > 1000 and chunks[-1]:
                    chunks.append([])
                chunks[-1].append(line)
            for i, chunk in enumerate(chunks):
                name_label = "📋 Actieve bunkers" if i == 0 else "\u200b"
                embed.add_field(name=name_label, value="\n".join(chunk), inline=False)
        else:
            embed.add_field(
                name="📋 Actieve bunkers",
                value="_Geen actieve bunkers gevonden._",
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
