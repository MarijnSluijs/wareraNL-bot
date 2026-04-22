"""Slash command: on-demand resistance overview for NL-occupied foreign regions."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")


class ResistanceCog(TaskCogBase, name="resistance"):
    def __init__(self, bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    # Slash command                                                        #
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="bezetting",
        description="Toon het actuele verzetsoverzicht van door NL bezette regio's.",
    )
    async def bezetting(self, interaction: discord.Interaction) -> None:
        """Fetch and display resistance status for NL-occupied regions on demand."""
        await interaction.response.defer(thinking=True)
        embed = await self.build_resistance_embed()
        if embed is None:
            await interaction.followup.send(
                "Geen door NL bezette buitenlandse regio's gevonden, of de API is niet beschikbaar.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def build_resistance_embed(self) -> discord.Embed | None:
        """Fetch regions, compute resistance deltas, upsert DB, return embed or None."""
        if not self._client or not self._db:
            return None

        nl_country_id = self.config.get("nl_country_id")
        if not nl_country_id:
            return None

        try:
            resp = await self._client.get(
                "/region.getRegionsObject",
                params={"input": "{}"},
            )
        except Exception as exc:
            logger.warning("bezetting: failed to fetch regions: %s", exc)
            return None

        data: dict | list = {}
        if isinstance(resp, dict):
            inner = resp.get("result", {})
            data = inner.get("data", inner) if isinstance(inner, dict) else resp
        regions: list[dict] = []
        if isinstance(data, dict):
            regions = [v for v in data.values() if isinstance(v, dict)]
        elif isinstance(data, list):
            regions = [r for r in data if isinstance(r, dict)]

        country_names: dict[str, str] = {}
        try:
            c_resp = await self._client.get("/country.getAllCountries")
            c_inner = c_resp.get("result", c_resp) if isinstance(c_resp, dict) else {}
            c_data = (
                c_inner.get("data", c_inner) if isinstance(c_inner, dict) else c_resp
            )
            if isinstance(c_data, list):
                for c in c_data:
                    if isinstance(c, dict):
                        cid = c.get("_id") or c.get("id")
                        cname = c.get("name") or c.get("shortName")
                        if cid and cname:
                            country_names[str(cid)] = str(cname)
        except Exception:
            logger.debug("bezetting: could not build country name cache")

        occupied: list[dict] = []
        for r in regions:
            orig_id = r.get("initialCountry")
            curr_id = r.get("country")
            if curr_id == nl_country_id and orig_id and orig_id != nl_country_id:
                occupied.append(r)

        if not occupied:
            return None

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        current: list[tuple[str, str, str, float, float]] = []
        for r in occupied:
            rid = r.get("_id") or r.get("id") or ""
            rname = r.get("name") or r.get("regionName") or rid
            orig_id = r.get("initialCountry") or ""
            orig_name = country_names.get(orig_id) or orig_id or "?"
            res = float(r.get("resistance") or 0)
            maxr = float(r.get("resistanceMax") or 100.0)
            current.append((rid, rname, orig_name, res, maxr))

        def _region_field(res: float, maxr: float, delta: float | None = None) -> str:
            pct = (res / maxr * 100) if maxr else 0
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            delta_str = ""
            if delta is not None and abs(delta) > 0.01:
                arrow = "📈" if delta > 0 else "📉"
                delta_str = f" ({arrow} {delta:+.0f})"
            return f"Verzet: `{bar}` {res:.0f}/{maxr:.0f} ({pct:.0f}%){delta_str}"

        embed = discord.Embed(
            title="⚔️ Door NL bezette regio's — verzetsoverzicht",
            description="Regio's die NL beheert maar oorspronkelijk aan een ander land toebehoren.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        # Group regions by original-owner country, sorted by resistance % descending
        # within each group.  Groups themselves sorted alphabetically by country name.
        groups: dict[str, list[tuple]] = {}  # orig_name → [(rid, rname, orig, res, maxr)]
        for row in current:
            rid, rname, orig_name, res, maxr = row
            groups.setdefault(orig_name, []).append(row)

        for country_name_key in sorted(groups):
            rows_in_group = sorted(
                groups[country_name_key],
                key=lambda r: (r[3] / r[4]) if r[4] else 0,
                reverse=True,
            )
            # Header field for this country group
            embed.add_field(
                name=f"🗺️ {country_name_key}",
                value=f"*{len(rows_in_group)} regio('s)*",
                inline=False,
            )
            for rid, rname, orig, res, maxr in rows_in_group:
                stored = await self._db.get_resistance_state(rid)
                old_val: float | None = stored["resistance_value"] if stored else None
                delta = (res - old_val) if old_val is not None else None
                embed.add_field(
                    name=rname,
                    value=_region_field(res, maxr, delta),
                    inline=False,
                )
                await self._db.upsert_resistance_state(rid, rname, orig, res, maxr, now_str)

        embed.set_footer(text="WarEra — verzetspeiling")
        return embed


async def setup(bot) -> None:
    """Add the ResistanceCog to the bot."""
    await bot.add_cog(ResistanceCog(bot))
