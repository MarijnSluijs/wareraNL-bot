"""Slash command /hits — waar heeft een speler al geraakt in actieve gevechten?

Fetches every currently active battle (battle.getBattles, isActive: true,
single page of up to 100 — same assumption cogs/commands/gevechten.py
already makes about that being enough to cover all live battles), then
calls battleLootSummary.getByBattleAndUser(battleId, userId) once per
battle for the target player to see whether they've dealt any damage there
yet.

Confirmed live (reported directly by testing the endpoint): when a player
hasn't hit in a given battle, the endpoint doesn't return clean "no data"
JSON — it returns something resp.json() can't parse. services.api_client's
APIClient.get() already treats that as non-fatal (falls back to resp.text()
instead of raising) for a 2xx response, and a non-2xx response raises,
which is caught here too. Either way there's no richer signal available at
this endpoint to distinguish "confirmed no hit" from "malformed/empty
response" — both are treated as "not hit yet".
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import (
    CommandCogBase,
    citizen_autocomplete,
    strip_division_prefix,
)

logger = logging.getLogger("discord_bot")

_BATTLE_URL = "https://app.warera.io/battle/{battle_id}"
_REQUEST_DELAY = 0.15
_DESCRIPTION_CHAR_LIMIT = 3900  # embed description hard limit is 4096; leave headroom


def _unwrap(resp: object) -> object:
    if not isinstance(resp, dict):
        return resp
    d = resp.get("result", resp)
    if isinstance(d, dict):
        return d.get("data", d)
    return d


def _battle_label(battle: dict, country_names: dict[str, str]) -> str:
    def_id = str((battle.get("defender") or {}).get("country") or "")
    att_id = str((battle.get("attacker") or {}).get("country") or "")
    def_name = country_names.get(def_id, def_id or "?")
    att_name = country_names.get(att_id, att_id or "?")
    return f"{def_name} vs {att_name}"


_MAX_LINES_PER_SECTION = 50  # keeps the combined description under Discord's 4096-char limit


def _format_section(title: str, lines: list[str]) -> str:
    shown = lines[:_MAX_LINES_PER_SECTION]
    body = "\n".join(shown) if shown else "—"
    if len(lines) > len(shown):
        body += f"\n… en {len(lines) - len(shown)} meer"
    return f"**{title} ({len(lines)})**\n{body}"


class HitsCog(CommandCogBase, name="hits"):
    """Slash command /hits — per-speler overzicht van actieve gevechten wel/niet geraakt."""

    def __init__(self, bot) -> None:
        self.bot = bot

    async def _resolve_target(
        self, ctx: Context, speler: Optional[str]
    ) -> tuple[str, str] | None:
        """Return (user_id, citizen_name) for *speler*, or the caller when None.

        Same two-tier caller resolution as /paraatheid's _resolve_caller_mu:
        Discord display name (minus war-guild division prefix) first, then
        identity_links scoped to this guild as a fallback.
        """
        if speler:
            try:
                citizen = await self._db.get_citizen_by_name_exact(speler)
            except Exception:
                citizen = None
            if citizen:
                return citizen[0], citizen[1]
            try:
                matches = await self._db.find_citizen_readiness(speler)
            except Exception:
                matches = []
            if matches:
                m = matches[0]
                return m["user_id"], m["citizen_name"]
            return None

        name = strip_division_prefix(ctx.author.display_name).strip()
        if name:
            try:
                citizen = await self._db.get_citizen_by_name_exact(name)
            except Exception:
                citizen = None
            if citizen:
                return citizen[0], citizen[1]

        if ctx.guild:
            try:
                link = await self._db.get_identity_link_by_discord(
                    str(ctx.author.id), str(ctx.guild.id)
                )
            except Exception:
                link = None
            user_id = (link or {}).get("in_game_user_id")
            if user_id:
                cit_name = await self._db.get_citizen_name_by_id(user_id)
                return user_id, cit_name or user_id
        return None

    @commands.hybrid_command(
        name="hits",
        description="Toon in welke actieve gevechten een speler al geraakt heeft, en waar nog niet.",
    )
    @app_commands.describe(speler="Zoek een speler op naam (standaard: jezelf).")
    @app_commands.autocomplete(speler=citizen_autocomplete)
    async def hits(self, ctx: Context, speler: Optional[str] = None) -> None:
        if not self._db or not self._client:
            await ctx.send("Database of API niet beschikbaar.")
            return

        target = await self._resolve_target(ctx, speler)
        if target is None:
            await ctx.send(
                "Speler niet gevonden. Geef een naam op met `speler:`, of zorg dat je geverifieerd bent."
            )
            return
        user_id, citizen_name = target

        if hasattr(ctx, "defer"):
            await ctx.defer()

        try:
            raw = await self._client.get(
                "/battle.getBattles",
                params={"input": json.dumps({"isActive": True, "limit": 100})},
            )
        except Exception as exc:
            logger.warning("hits: getBattles failed: %s", exc)
            await ctx.send("Kon actieve gevechten niet ophalen.")
            return

        data = _unwrap(raw)
        if isinstance(data, dict):
            battles: list[dict] = [b for b in data.get("items", []) if isinstance(b, dict)]
        elif isinstance(data, list):
            battles = [b for b in data if isinstance(b, dict)]
        else:
            battles = []

        if not battles:
            await ctx.send("Geen actieve gevechten gevonden.")
            return

        try:
            country_names = await self._db.get_country_name_map()
        except Exception:
            country_names = {}

        hit_lines: list[str] = []
        no_hit_lines: list[str] = []

        for i, battle in enumerate(battles):
            battle_id = str(battle.get("_id", ""))
            if not battle_id:
                continue
            if i > 0:
                await asyncio.sleep(_REQUEST_DELAY)

            summary: object = None
            try:
                raw_summary = await self._client.get(
                    "/battleLootSummary.getByBattleAndUser",
                    params={"input": json.dumps({"battleId": battle_id, "userId": user_id})},
                )
                summary = _unwrap(raw_summary)
            except Exception:
                summary = None

            url = _BATTLE_URL.format(battle_id=battle_id)
            label = _battle_label(battle, country_names)

            if isinstance(summary, dict) and summary:
                # Confirmed live: the field is "totalDmg" (not "totalDamage",
                # despite that being the name used in cogs/tasks/daily_dmg.py's
                # docstring) — kept as a fallback in case the API is inconsistent.
                dmg = summary.get("totalDmg") or summary.get("totalDamage")
                if dmg:
                    hit_lines.append(f"[{label}]({url}) — {int(dmg):,}".replace(",", "."))
                else:
                    hit_lines.append(f"[{label}]({url})")
            else:
                no_hit_lines.append(f"[{label}]({url})")

        description = (
            _format_section("✅ Geraakt", hit_lines)
            + "\n\n"
            + _format_section("❌ Nog niet geraakt", no_hit_lines)
        )
        embed = discord.Embed(
            title=f"⚔️ Hits — {citizen_name}",
            description=description[:_DESCRIPTION_CHAR_LIMIT],
            colour=self._embed_colour(),
        )

        await ctx.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(HitsCog(bot))
