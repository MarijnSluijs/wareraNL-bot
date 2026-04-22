"""
/bunkers command — show the bunker status for all regions owned by the Netherlands.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")


def _unwrap(resp) -> dict | list | None:
    """Unwrap a tRPC/API response to its inner data."""
    if isinstance(resp, dict):
        data = resp.get("result", {}).get("data", resp)
        return data
    return resp


class BunkersCog(CommandCogBase, name="bunkers"):
    """Bunker status command for NL-owned regions."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="bunkers",
        description="Toon de bunkerstatus van alle regio's die Nederland bezit.",
    )
    async def bunkers(self, ctx: Context) -> None:
        """Display bunker status for all NL-owned regions."""
        if not self._client:
            await ctx.send("❌ API client niet beschikbaar.", ephemeral=True)
            return

        await ctx.defer()

        nl_country_id: str = self.config.get("nl_country_id", "")
        if not nl_country_id:
            await ctx.send("❌ `nl_country_id` niet geconfigureerd.", ephemeral=True)
            return

        # 1. Fetch all regions and filter to NL-owned ones
        try:
            regions_resp = await asyncio.wait_for(
                self._client.get("/region.getRegionsObject"),
                timeout=60.0,
            )
        except Exception as exc:
            logger.error("bunkers: failed to fetch regions: %s", exc)
            await ctx.send("❌ Fout bij ophalen van regio's.", ephemeral=True)
            return

        regions_data = _unwrap(regions_resp)
        if not isinstance(regions_data, dict):
            await ctx.send("❌ Onverwacht antwoord van regio API.", ephemeral=True)
            return

        nl_regions: list[tuple[str, str, str]] = []  # (region_id, region_name, initial_country_id)
        for rid, robj in regions_data.items():
            if not isinstance(robj, dict):
                continue
            owner = robj.get("country") or robj.get("ownedBy") or robj.get("owner")
            if owner == nl_country_id:
                name = robj.get("name") or rid
                initial_country = str(robj.get("initialCountry") or nl_country_id)
                nl_regions.append((rid, name, initial_country))

        if not nl_regions:
            await ctx.send("Geen regio's gevonden die door Nederland worden bezit.")
            return

        # 2. Build country name lookup
        country_names: dict[str, str] = {}
        try:
            c_resp = await self._client.get("/country.getAllCountries")
            c_data = _unwrap(c_resp)
            if isinstance(c_data, list):
                for c in c_data:
                    if isinstance(c, dict):
                        cid = str(c.get("_id") or c.get("id") or "")
                        cname = c.get("name") or c.get("countryName") or ""
                        if cid and cname:
                            country_names[cid] = cname
        except Exception:
            logger.debug("bunkers: could not build country name cache")

        # 3. Fetch bunker upgrade status for each region
        async def _fetch_bunker(region_id: str, region_name: str) -> dict | None:
            try:
                resp = await asyncio.wait_for(
                    self._client.post(
                        "/upgrade.getUpgradeByTypeAndEntity",
                        json={"upgradeType": "bunker", "regionId": region_id},
                    ),
                    timeout=30.0,
                )
                data = _unwrap(resp)
                logger.debug("bunkers raw [%s]: %s", region_name, data)
                return data
            except Exception as exc:
                logger.warning("bunkers: failed to fetch bunker for %s: %s", region_id, exc)
                return None

        results = await asyncio.gather(*[_fetch_bunker(rid, rname) for rid, rname, _ in nl_regions])

        # 4. Group by original country, sorted alphabetically
        groups: dict[str, list[tuple[str, str, dict | None]]] = {}
        for (rid, rname, init_cid), bunker_data in zip(nl_regions, results):
            country_label = country_names.get(init_cid) or init_cid or "?"
            groups.setdefault(country_label, []).append((rid, rname, bunker_data))

        # Within each group sort by status priority: active(0) > pending(1) > inactive(2) > not built(3)
        def _status_priority(bunker_data: dict | None) -> int:
            if bunker_data is None or not isinstance(bunker_data, dict) or not bunker_data:
                return 3
            status = (bunker_data.get("status") or "").lower()
            level = bunker_data.get("level") or 0
            if status == "active":
                return 0
            if status == "pending":
                return 1
            if level and level > 0:
                return 2
            return 3

        # 5. Build embed
        embed = discord.Embed(
            title="🏰 Bunkerstatus Nederland",
            colour=discord.Colour.from_str(
                self.config.get("colors", {}).get("primary", "0xffb612")
            ),
            timestamp=datetime.now(timezone.utc),
        )

        total = len(nl_regions)
        for country_label in sorted(groups):
            rows = sorted(groups[country_label], key=lambda t: _status_priority(t[2]))
            lines: list[str] = []
            for _, rname, bunker_data in rows:
                status_str = _format_bunker_status(bunker_data)
                lines.append(f"**{rname}** — {status_str}")

            # Split into chunks of ≤900 chars to stay within field value limit
            chunks: list[list[str]] = [[]]
            for line in lines:
                current_len = sum(len(l) + 1 for l in chunks[-1])
                if current_len + len(line) + 1 > 900 and chunks[-1]:
                    chunks.append([])
                chunks[-1].append(line)

            for i, chunk in enumerate(chunks):
                field_name = f"🗺️ {country_label}" if i == 0 else "\u200b"
                embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

        embed.set_footer(text=f"{total} regio('s)")
        await ctx.send(embed=embed)


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 / RFC-3339 timestamp string to an aware datetime, or None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _format_bunker_status(data: dict | None) -> str:
    """Return a human-readable bunker status string from the API response.

    The API uses these fields:
      status         : 'disabled' | 'pending' | 'active'
      level          : integer bunker level
      willBeActiveAt : ISO timestamp when activation completes (present when pending)

    Possible outputs:
      ⬛ niet gebouwd           — no bunker exists (data is None / empty / level 0)
      ⏳ actief over <t:TS:R>  — status pending with future willBeActiveAt
      ✅ actief (niveau N)     — status active
      🟡 inactief (niveau N)   — built but disabled / past activation window
    """
    if data is None or not isinstance(data, dict) or not data:
        return "⬛ niet gebouwd"

    level = data.get("level") or 0
    status = (data.get("status") or "").lower()  # 'disabled' / 'pending' / 'active'
    will_be_active_at_str = data.get("willBeActiveAt")

    # Being activated: status is 'pending' and willBeActiveAt is still in the future
    if status == "pending" and will_be_active_at_str:
        finish_dt = _parse_iso(will_be_active_at_str)
        if finish_dt and finish_dt > datetime.now(timezone.utc):
            ts = int(finish_dt.timestamp())
            return f"⏳ actief over <t:{ts}:R>"

    # Fully active
    if status == "active":
        lvl_str = f" (niveau {level})" if level else ""
        return f"🟢 actief{lvl_str}"

    # Built but not active (disabled, or pending with an already-passed activation time)
    if level and level > 0:
        return f"🔴 inactief (niveau {level})"

    return "⬛ niet gebouwd"


async def setup(bot) -> None:
    await bot.add_cog(BunkersCog(bot))
