"""MU-related slash commands for the NL Discord bot.

Commands
--------
/muplek          – Table of all Dutch MUs with member counts, limits and free spots.
/mu_inactiviteit – Lists inactive MU members (no login in the last 72 hours).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from services.api_client import APIClient

logger = logging.getLogger("cogs.mu")

# Dormitories level → maximum member capacity
DORM_CAPACITY: dict[int, int] = {
    1: 5,
    2: 10,
    3: 15,
    4: 20,
    5: 25,
}
INACTIVITY_HOURS = 72


def _unwrap(resp: object) -> object:
    """Unwrap a tRPC result envelope."""
    if not isinstance(resp, dict):
        return resp
    for key in ("result", "data"):
        v = resp.get(key)
        if isinstance(v, dict):
            inner = v.get("data", v)
            return inner
    return resp


def _last_connection(obj: object) -> Optional[str]:
    """Extract lastConnectionAt from a getUserLite response."""
    if not isinstance(obj, dict):
        return None
    dates = obj.get("dates")
    if isinstance(dates, dict):
        return dates.get("lastConnectionAt")
    # flat fallback
    return obj.get("lastConnectionAt") or obj.get("lastLoginAt")


def _username(obj: object) -> str:
    if not isinstance(obj, dict):
        return "?"
    return obj.get("username") or obj.get("name") or "?"


def _fmt_duration(hours: float) -> str:
    d = int(hours // 24)
    h = int(hours % 24)
    if d:
        return f"{d}d {h}u"
    return f"{h}u"


class MU(commands.Cog, name="mu"):
    """MU-related commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: dict = getattr(bot, "config", {}) or {}
        self._client: Optional[APIClient] = None

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _get_client(self) -> APIClient:
        if self._client is None:
            base_url = self.config.get("api_base_url", "https://api2.warera.io/trpc")
            api_keys: list[str] = []
            try:
                with open("_api_keys.json") as f:
                    api_keys = json.load(f).get("keys", [])
            except FileNotFoundError:
                pass
            self._client = APIClient(base_url=base_url, api_keys=api_keys)
            await self._client.start()
        return self._client

    async def _get_nl_user_ids(self) -> set[str]:
        """Return the set of all Dutch citizen user IDs from the external DB."""
        nl_country_id = self.config.get("nl_country_id", "")
        db_path = self.config.get("external_db_path", "database/external.db")
        try:
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute(
                    "SELECT user_id FROM citizen_levels WHERE country_id = ?",
                    (nl_country_id,),
                )
                rows = await cur.fetchall()
            return {r[0] for r in rows}
        except Exception as exc:
            logger.warning("_get_nl_user_ids: DB error: %s", exc)
            return set()

    async def _get_all_dutch_mus(self) -> list[dict]:
        """Paginate /mu.getManyPaginated and return only MUs owned by a Dutch citizen."""
        nl_users = await self._get_nl_user_ids()
        if not nl_users:
            logger.warning("_get_all_dutch_mus: no NL citizens found in DB")
            return []

        client = await self._get_client()
        mus: list[dict] = []
        cursor: Optional[str] = None
        while True:
            payload: dict = {"limit": 100}
            if cursor:
                payload["cursor"] = cursor
            try:
                resp = await client.get(
                    "/mu.getManyPaginated",
                    params={"input": json.dumps(payload)},
                )
            except Exception as exc:
                logger.error("_get_all_dutch_mus: API error: %s", exc)
                break
            data = _unwrap(resp)
            items: list[dict] = data.get("items", []) if isinstance(data, dict) else []
            for mu in items:
                if mu.get("user") in nl_users:
                    mus.append(mu)
            cursor = data.get("nextCursor") if isinstance(data, dict) else None
            if not cursor or not items:
                break

        return sorted(mus, key=lambda m: m.get("name", "").lower())

    # ------------------------------------------------------------------ #
    # /muplek
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="muplek",
        description="Laat zien hoeveel plekken er vrij zijn in de Nederlandse MU's.",
    )
    async def muplek(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        mus = await self._get_all_dutch_mus()
        if not mus:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen Nederlandse MU's gevonden (of de DB is leeg).",
                    color=discord.Color.red(),
                )
            )
            return

        rows: list[tuple[str, int, int, int]] = []
        for mu in mus:
            name = mu.get("name", "?")
            members = len(mu.get("members", []))
            dorm_lvl = mu.get("activeUpgradeLevels", {}).get("dormitories", 1)
            capacity = DORM_CAPACITY.get(dorm_lvl, dorm_lvl * 5)
            free = max(0, capacity - members)
            rows.append((name, members, capacity, free))

        total_free = sum(r[3] for r in rows)
        total_members = sum(r[1] for r in rows)
        total_capacity = sum(r[2] for r in rows)

        # Sort: most free spots first, then alphabetically
        rows.sort(key=lambda r: (-r[3], r[0].lower()))

        # Build monospace table
        MAX_MU_NAME = 20
        col1 = min(max(len(r[0]) for r in rows), MAX_MU_NAME)
        col1 = max(col1, len("MU"))
        header = f"{'MU':<{col1}}  Leden  Max  Vrij"
        separator = "-" * len(header)
        lines = [header, separator]
        for name, members, capacity, free in rows:
            free_str = f"+{free}" if free > 0 else " 0"
            lines.append(f"{name[:col1]:<{col1}}  {members:>5}  {capacity:>3}  {free_str:>4}")
        lines.append(separator)
        lines.append(
            f"{'TOTAAL':<{col1}}  {total_members:>5}  {total_capacity:>3}  +{total_free:>3}"
        )
        table = "\n".join(lines)

        color = int(self.config.get("colors", {}).get("primary", "0x154273"), 16)
        embed = discord.Embed(
            title="🪖 Nederlandse MU's – Beschikbare plekken",
            description=f"**Totaal vrij: {total_free} plek{'ken' if total_free != 1 else ''}**\n\n```\n{table}\n```",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{len(mus)} MU's • Capaciteit gebaseerd op kazernesniveau")
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # /mu_inactiviteit
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="mu_inactiviteit",
        description=f"Laat inactieve leden zien in Nederlandse MU's (geen login in {INACTIVITY_HOURS}u).",
    )
    async def mu_inactiviteit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        mus = await self._get_all_dutch_mus()
        if not mus:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen Nederlandse MU's gevonden (of de DB is leeg).",
                    color=discord.Color.red(),
                )
            )
            return

        # Build member→MU name map
        member_to_mu: dict[str, str] = {}
        for mu in mus:
            mu_name = mu.get("name", "?")
            for uid in mu.get("members", []):
                member_to_mu[uid] = mu_name

        all_member_ids = list(member_to_mu.keys())
        if not all_member_ids:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen leden gevonden in Nederlandse MU's.",
                    color=discord.Color.orange(),
                )
            )
            return

        client = await self._get_client()
        inputs = [{"userId": uid} for uid in all_member_ids]
        results = await client.batch_get(
            "/user.getUserLite",
            inputs,
            batch_size=30,
            chunk_sleep=0.5,
        )

        now = datetime.now(timezone.utc)
        inactive: list[tuple[float, str, str, str]] = []  # (hours_ago, uid, name, mu_name)

        for uid, obj in zip(all_member_ids, results):
            last_conn = _last_connection(obj)
            if last_conn is None:
                # No login info → treat as very inactive (unknown)
                inactive.append((float("inf"), uid, _username(obj), member_to_mu[uid]))
                continue
            try:
                ts = datetime.fromisoformat(last_conn.replace("Z", "+00:00"))
                hours_ago = (now - ts).total_seconds() / 3600
            except (ValueError, TypeError):
                inactive.append((float("inf"), uid, _username(obj), member_to_mu[uid]))
                continue
            if hours_ago >= INACTIVITY_HOURS:
                inactive.append((hours_ago, uid, _username(obj), member_to_mu[uid]))

        color = int(self.config.get("colors", {}).get("primary", "0x154273"), 16)

        if not inactive:
            embed = discord.Embed(
                title="✅ Geen inactieve leden",
                description=(
                    f"Alle leden van Nederlandse MU's zijn ingelogd in de afgelopen "
                    f"{INACTIVITY_HOURS} uur."
                ),
                color=discord.Color.green(),
                timestamp=now,
            )
            await interaction.followup.send(embed=embed)
            return

        # Sort: longest inactive first (inf last)
        inactive.sort(key=lambda x: (x[0] != float("inf"), -x[0] if x[0] != float("inf") else 0))

        # Build table
        col_name = max(len(r[2]) for r in inactive)
        col_name = max(col_name, len("Speler"))
        col_mu = max(len(r[3]) for r in inactive)
        col_mu = max(col_mu, len("MU"))
        header = f"{'Speler':<{col_name}}  {'MU':<{col_mu}}  Inactief"
        separator = "-" * (col_name + col_mu + 14)
        lines = [header, separator]
        for hours, uid, name, mu_name in inactive:
            dur = "onbekend" if hours == float("inf") else _fmt_duration(hours)
            lines.append(f"{name:<{col_name}}  {mu_name:<{col_mu}}  {dur}")
        table = "\n".join(lines)

        embed = discord.Embed(
            title=f"💤 Inactieve leden – Nederlandse MU's",
            description=(
                f"**{len(inactive)} leden** hebben meer dan **{INACTIVITY_HOURS} uur** niet ingelogd.\n\n"
                f"```\n{table}\n```"
            ),
            color=color,
            timestamp=now,
        )
        embed.set_footer(text=f"{len(all_member_ids)} leden gecontroleerd in {len(mus)} MU's")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MU(bot))
