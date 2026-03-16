"""Slash command /mudmg — Dutch MU damage overview or per-member breakdown.

Usage
-----
/mudmg              — overview of all Dutch MUs grouped by category (Elite, Eco, Casual)
/mudmg mu:Name      — per-member weekly damage table for the given MU
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase
from cogs.tasks.mus import mus_path
from services.damage_calc import fmt_damage

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# ── Category display config ──────────────────────────────────────────────────

_CATEGORY_ORDER = ["Elite", "Eco", "Standaard"]
_CATEGORY_LABEL: dict[str, str] = {
    "Elite": "🎖️ Elite MU's",
    "Eco": "🏭 Eco MU's",
    "Standaard": "🛡️ Casual MU's",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_mus(testing: bool = False) -> list[dict]:
    """Load MU entries from templates/mus.json."""
    path = mus_path(testing)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("embeds", [])


def _mu_rankings(mu_data: dict) -> tuple[float, float]:
    """Extract (weekly_damage, total_damage) from a mu.getById payload."""
    if not isinstance(mu_data, dict):
        return 0.0, 0.0

    # The payload may be wrapped under a nested "mu" key
    obj = mu_data.get("mu") if isinstance(mu_data.get("mu"), dict) else mu_data

    rankings = obj.get("rankings") if isinstance(obj, dict) else None
    if not isinstance(rankings, dict):
        return 0.0, 0.0

    weekly_obj = rankings.get("muWeeklyDamages") or {}
    total_obj = rankings.get("muDamages") or {}

    weekly = float(weekly_obj.get("value") or 0)
    total = float(total_obj.get("value") or 0)
    return weekly, total


def _mu_members(mu_data: dict) -> list[str]:
    """Extract the list of member user-IDs from a mu.getById payload."""
    if not isinstance(mu_data, dict):
        return []
    obj = mu_data.get("mu") if isinstance(mu_data.get("mu"), dict) else mu_data
    if not isinstance(obj, dict):
        return []
    members = obj.get("members") or []
    return [str(m) for m in members if m]


def _overview_table(
    rows: list[tuple[str, float, float]], subtotals: tuple[float, float]
) -> str:
    """Render category rows as a fixed-width code-block table.

    rows: [(name, weekly_damage, total_damage), ...]
    subtotals: (weekly_subtotal, total_subtotal)
    """
    if not rows:
        return "*Geen data*"

    name_w = min(max(len(r[0]) for r in rows), 26)
    name_w = max(name_w, len("Subtotaal"))
    W = 10
    T = 10

    header = f"{'Naam':<{name_w}}  {'Wekelijks':>{W}}  {'Totaal':>{T}}"
    sep = "\u2500" * len(header)
    lines = [header, sep]

    for name, weekly, total in rows:
        display = name[:name_w] if len(name) > name_w else name
        w_str = fmt_damage(weekly) if weekly else "\u2014"
        t_str = fmt_damage(total) if total else "\u2014"
        lines.append(f"{display:<{name_w}}  {w_str:>{W}}  {t_str:>{T}}")

    lines.append(sep)
    sw, st = subtotals
    lines.append(
        f"{'Subtotaal':<{name_w}}  {fmt_damage(sw) if sw else chr(0x2014):>{W}}  "
        f"{fmt_damage(st) if st else chr(0x2014):>{T}}"
    )

    return "```\n" + "\n".join(lines) + "\n```"


def _extract_total_damage_map(resp: object) -> dict[str, float]:
    """Parse a ranking.getRanking[userDamages] response into {user_id: total_damage}."""
    result: dict[str, float] = {}
    if not isinstance(resp, dict):
        return result
    # unwrap tRPC envelopes
    data: object = resp
    for key in ("result", "data"):
        if isinstance(data, dict) and key in data:
            inner = data[key]  # type: ignore[index]
            if isinstance(inner, dict):
                data = inner
    # find items list
    items: list = []
    if isinstance(data, dict):
        for key in ("items", "ranking", "rankings", "data", "results"):
            v = data.get(key)  # type: ignore[union-attr]
            if isinstance(v, list):
                items = v
                break
    for item in items:
        if not isinstance(item, dict):
            continue
        uid = item.get("user")
        val = item.get("value")
        if uid and isinstance(val, (int, float)):
            result[str(uid)] = float(val)
    return result


# ── Autocomplete ─────────────────────────────────────────────────────────────


async def mu_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest Dutch MU names for the 'mu' parameter."""
    try:
        entries = _load_mus(getattr(interaction.client, "testing", False))
    except Exception:
        return []
    q = current.strip().lower()
    return [
        app_commands.Choice(name=e["name"], value=e["name"])
        for e in entries
        if q in e["name"].lower()
    ][:25]


# ── Cog ──────────────────────────────────────────────────────────────────────


class MudmgCog(CommandCogBase, name="mudmg"):
    """Cog for the /mudmg command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    def _mus(self) -> list[dict]:
        return _load_mus(getattr(self.bot, "testing", False))

    # ── Command ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(
        name="mudmg",
        description="Toon schadeoverzicht van Nederlandse Militaire Eenheden.",
    )
    @app_commands.describe(
        mu="Optioneel: naam van een specifieke MU voor een ledendetailoverzicht.",
    )
    @app_commands.autocomplete(mu=mu_autocomplete)
    async def mudmg(
        self,
        ctx: Context,
        mu: Optional[str] = None,
    ) -> None:
        """Show MU damage overview or per-member breakdown."""
        if not self._client:
            await ctx.send("API-client niet geïnitialiseerd.")
            return

        if hasattr(ctx, "defer"):
            await ctx.defer()

        entries = self._mus()
        if not entries:
            await ctx.send("Geen MU-configuratie gevonden.")
            return

        if mu:
            await self._send_mu_detail(ctx, mu, entries)
        else:
            await self._send_mu_overview(ctx, entries)

    # ── Overview (no MU arg) ──────────────────────────────────────────────────

    async def _send_mu_overview(self, ctx: Context, entries: list[dict]) -> None:
        """Fetch all MU stats and send an overview embed grouped by category."""
        inputs = [{"muId": e["id"]} for e in entries]
        try:
            results = await self._client.batch_get("/mu.getById", inputs)
        except Exception as exc:
            logger.warning("mudmg overview: batch_get failed: %s", exc)
            await ctx.send("Kon MU-data niet ophalen van de API.")
            return

        # Map entry id → live payload
        id_to_data: dict[str, dict] = {}
        for entry, payload in zip(entries, results):
            if isinstance(payload, dict):
                id_to_data[entry["id"]] = payload

        # Group entries by category
        grouped: dict[str, list[tuple[dict, dict]]] = {c: [] for c in _CATEGORY_ORDER}
        for entry in entries:
            cat = entry.get("type", "Standaard")
            if cat not in grouped:
                cat = "Standaard"
            grouped[cat].append((entry, id_to_data.get(entry["id"], {})))

        embed = discord.Embed(
            title="⚔️ Nederlandse Militaire Eenheden — Schadeoverzicht",
            colour=self._embed_colour(),
        )

        grand_weekly = 0.0
        grand_total = 0.0

        for cat in _CATEGORY_ORDER:
            group = grouped[cat]
            if not group:
                continue

            # Sort by weekly damage descending
            group.sort(key=lambda t: _mu_rankings(t[1])[0], reverse=True)

            cat_weekly = 0.0
            cat_total = 0.0
            tbl_rows: list[tuple[str, float, float]] = []

            for entry, mu_data in group:
                w, t = _mu_rankings(mu_data)
                cat_weekly += w
                cat_total += t
                tbl_rows.append((entry["name"], w, t))

            grand_weekly += cat_weekly
            grand_total += cat_total

            embed.add_field(
                name=_CATEGORY_LABEL.get(cat, cat),
                value=_overview_table(tbl_rows, (cat_weekly, cat_total)),
                inline=False,
            )

        gw_str = fmt_damage(grand_weekly) if grand_weekly else "—"
        gt_str = fmt_damage(grand_total) if grand_total else "—"
        embed.set_footer(text=f"Totaal NL — week: {gw_str} | totaal: {gt_str}")

        await ctx.send(embed=embed)

    # ── Detail (MU arg) ───────────────────────────────────────────────────────

    async def _send_mu_detail(
        self, ctx: Context, mu_name: str, entries: list[dict]
    ) -> None:
        """Fetch members of one MU and show per-member weekly + total damage."""
        # Fuzzy match: exact name first, then substring
        q = mu_name.strip().lower()
        entry = next(
            (e for e in entries if e["name"].lower() == q),
            next((e for e in entries if q in e["name"].lower()), None),
        )
        if entry is None:
            await ctx.send(
                f"Geen MU gevonden met de naam **{mu_name}**.\n"
                "Gebruik het autocomplete-menu voor een juiste naam."
            )
            return

        # Fetch MU data and global total-damage ranking concurrently
        async def _fetch_mu() -> object:
            try:
                res = await self._client.batch_get(
                    "/mu.getById", [{"muId": entry["id"]}]
                )
                return res[0] if res else None
            except Exception as exc:
                logger.warning(
                    "mudmg detail: batch_get failed for %s: %s", entry["id"], exc
                )
                return None

        async def _fetch_total_ranking() -> object:
            try:
                return await self._client.post(
                    "/ranking.getRanking",
                    json={"rankingType": "userDamages"},
                )
            except Exception as exc:
                logger.warning("mudmg detail: userDamages fetch failed: %s", exc)
                return None

        mu_data, total_resp = await asyncio.gather(_fetch_mu(), _fetch_total_ranking())

        if not isinstance(mu_data, dict):
            await ctx.send("Kon MU-data niet ophalen van de API.")
            return

        weekly_total, dmg_total = _mu_rankings(mu_data)
        members = _mu_members(mu_data)

        # Build total damage lookup: uid → total damage
        total_map = _extract_total_damage_map(total_resp)

        # Look up per-member weekly damage from the DB
        if self._db and members:
            try:
                weekly_map = await self._db.get_weekly_damages_for_users(members)
            except Exception as exc:
                logger.warning("mudmg detail: DB lookup failed: %s", exc)
                weekly_map = {}
        else:
            weekly_map = {}

        # Build rows: (citizen_name, weekly_dmg, total_dmg), sorted by weekly desc
        rows: list[tuple[str, float, float]] = []
        for uid in members:
            name: Optional[str] = None
            weekly = 0.0
            if uid in weekly_map:
                name, weekly = weekly_map[uid]
            total_dmg = total_map.get(uid, 0.0)
            if name or total_dmg:
                rows.append((name or uid, weekly, total_dmg))
        rows.sort(key=lambda r: r[1], reverse=True)

        # Format as a fixed-width code-block table
        # Keep total width ≤ 43 chars so Discord embed lines don't wrap:
        #   3 (#) + 2 + 16 (Naam) + 2 + 9 (Wekelijks) + 2 + 9 (Totaal) = 43
        if rows:
            name_w = min(max(len(r[0]) for r in rows), 16)
            name_w = max(name_w, 4)
            W = 9
            T = 9
            header = (
                f"{'#':>3}  {'Naam':<{name_w}}  {'Wekelijks':>{W}}  {'Totaal':>{T}}"
            )
            sep = "\u2500" * len(header)
            tbl_lines = [header, sep]
            for i, (name, weekly, total) in enumerate(rows, 1):
                display = name[:name_w] if len(name) > name_w else name
                w_str = fmt_damage(weekly) if weekly else "\u2014"
                t_str = fmt_damage(total) if total else "\u2014"
                tbl_lines.append(
                    f"{i:>3}  {display:<{name_w}}  {w_str:>{W}}  {t_str:>{T}}"
                )
            table = "```\n" + "\n".join(tbl_lines) + "\n```"
        else:
            table = (
                "*Geen schadedata beschikbaar voor leden van deze MU.\n"
                "Enkel NL-burgers die via /peil zijn opgehaald zijn zichtbaar.*"
            )

        embed = discord.Embed(
            title=f"⚔️ {entry['name']} — Ledenschade",
            description=table,
            colour=self._embed_colour(),
        )

        if entry.get("thumbnail"):
            embed.set_thumbnail(url=entry["thumbnail"])

        w_str = fmt_damage(weekly_total) if weekly_total else "—"
        t_str = fmt_damage(dmg_total) if dmg_total else "—"
        members_shown = len(rows)
        members_total = len(members)

        footer_parts = [f"MU totaal — week: {w_str} | totaal: {t_str}"]
        if members_total:
            footer_parts.append(f"{members_shown}/{members_total} leden met schadedata")
        footer_parts.append(
            "Schade per lid sinds lidmaatschap is niet beschikbaar via de API"
        )
        embed.set_footer(text=" · ".join(footer_parts))

        await ctx.send(embed=embed)


async def setup(bot) -> None:
    """Add the MudmgCog to the bot."""
    await bot.add_cog(MudmgCog(bot))
