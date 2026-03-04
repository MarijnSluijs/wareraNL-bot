"""MU-related slash commands for the NL Discord bot.

Commands
--------
/muplek          – Table of all Dutch MUs with member counts, limits and free spots.
/mu_inactiviteit – Lists inactive MU members (no login in the last 72 hours).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

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

    def _mus_path(self) -> str:
        testing = getattr(self.bot, "testing", False)
        return "templates/mus.testing.json" if testing else "templates/mus.json"

    def _extract_mu_ids_from_template(self) -> list[str]:
        """Read mus.json and return the MU IDs listed in it."""
        try:
            with open(self._mus_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("_extract_mu_ids_from_template: failed to read mus.json: %s", exc)
            return []
        ids: list[str] = []
        seen: set[str] = set()
        for entry in data.get("embeds", []):
            if not isinstance(entry, dict):
                continue
            mu_id = str(entry.get("id") or "").strip()
            if not mu_id:
                desc = str(entry.get("description", ""))
                m = re.search(r"/mu/([A-Za-z0-9]+)", desc)
                if m:
                    mu_id = m.group(1)
            if mu_id and mu_id not in seen:
                ids.append(mu_id)
                seen.add(mu_id)
        return ids

    async def _get_all_dutch_mus(self) -> list[dict]:
        """Fetch MUs listed in mus.json via batch mu.getById calls."""
        mu_ids = self._extract_mu_ids_from_template()
        if not mu_ids:
            logger.warning("_get_all_dutch_mus: no MU IDs found in mus.json")
            return []

        client = await self._get_client()
        inputs = [{"muId": mid} for mid in mu_ids]
        try:
            results = await client.batch_get("/mu.getById", inputs)
        except Exception as exc:
            logger.error("_get_all_dutch_mus: batch_get failed: %s", exc)
            return []

        mus: list[dict] = []
        for raw in results:
            data = _unwrap(raw) if isinstance(raw, dict) else raw
            if isinstance(data, dict):
                mus.append(data)

        return sorted(mus, key=lambda m: m.get("name", "").lower())

    # ------------------------------------------------------------------ #
    # /muplek
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="muplek",
        description="Laat zien hoeveel plekken er vrij zijn in de Nederlandse MU's.",
    )
    async def muplek(self, interaction: discord.Interaction) -> None:
        """Show how many free spots are available in Dutch MUs."""
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
        max_mu_name = 20
        col1 = min(max(len(r[0]) for r in rows), max_mu_name)
        col1 = max(col1, len("MU"))
        header = f"{'MU':<{col1}}  Leden  Max  Vrij"
        separator = "-" * len(header)
        lines = [header, separator]
        for name, members, capacity, free in rows:
            free_str = f"+{free}" if free > 0 else " 0"
            lines.append(
                f"{name[:col1]:<{col1}}  {members:>5}  {capacity:>3}  {free_str:>4}"
            )
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
        embed.set_footer(
            text=f"{len(mus)} MU's • Capaciteit gebaseerd op kazernesniveau"
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ #
    # /mu_inactiviteit
    # ------------------------------------------------------------------ #

    @app_commands.command(
        name="mu_inactiviteit",
        description=f"Laat inactieve leden zien in Nederlandse MU's (geen login in {INACTIVITY_HOURS}u).",
    )
    async def mu_inactiviteit(self, interaction: discord.Interaction) -> None:
        """Show inactive members in Dutch MUs (no login in the last 72 hours)."""
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
        inactive: list[
            tuple[float, str, str, str]
        ] = []  # (hours_ago, uid, name, mu_name)

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
        inactive.sort(
            key=lambda x: (x[0] != float("inf"), -x[0] if x[0] != float("inf") else 0)
        )

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
            title="💤 Inactieve leden – Nederlandse MU's",
            description=(
                f"**{len(inactive)} leden** hebben meer dan **{INACTIVITY_HOURS} uur** niet ingelogd.\n\n"
                f"```\n{table}\n```"
            ),
            color=color,
            timestamp=now,
        )
        embed.set_footer(
            text=f"{len(all_member_ids)} leden gecontroleerd in {len(mus)} MU's"
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="eco_donaties",
        description="Laat eco-donaties sinds gespecifieerd aantal uur zien.",
    )
    @app_commands.describe(
        hours="Aantal uur terug om te controleren (standaard: 24)",
    )
    async def eco_donations(self, interaction: discord.Interaction, hours: int = 24):
        """Show eco donations in the last specified hours."""
        await interaction.response.defer()

        # Ensure MU metadata is fresh before reading from JSON
        mu_tasks = self.bot.get_cog("mu_tasks")
        if mu_tasks:
            try:
                await mu_tasks.refresh_mu_info()
            except Exception as exc:
                logger.warning("eco_donations: MU refresh failed: %s", exc)

        testing = getattr(self.bot, "testing", False)
        mus_json_path = "templates/mus.testing.json" if testing else "templates/mus.json"
        eco_mus: list[dict[str, str]] = []
        try:
            with open(mus_json_path, "r", encoding="utf-8") as f:
                mus_data = json.load(f)
            for item in mus_data.get("embeds", []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("type", "")).lower() != "eco":
                    continue
                mu_id = str(item.get("id", "")).strip()
                if not mu_id:
                    continue
                name = str(item.get("name") or item.get("title") or f"MU {mu_id[:8]}")
                eco_mus.append({"title": name, "mu_id": mu_id})
        except Exception as exc:
            logger.warning("eco_donations: failed to load %s: %s", mus_json_path, exc)

        if not eco_mus:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen eco-MU's gevonden in template.",
                    color=discord.Color.red(),
                )
            )
            return

        client = await self._get_client()
        nl_country_id = self.config.get("nl_country_id", "")
        if not nl_country_id:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="NL country ID niet geconfigureerd.",
                    color=discord.Color.red(),
                )
            )
            return

        # Fetch MU details and collect members
        mu_members: dict[
            str, tuple[str, list[str]]
        ] = {}  # mu_id -> (mu_name, [user_ids])
        for eco_mu in eco_mus:
            mu_id = eco_mu["mu_id"]
            fallback_name = eco_mu["title"]
            try:
                resp = await client.get(
                    "/mu.getById",
                    params={"input": json.dumps({"muId": mu_id})},
                )
                data = _unwrap(resp)
                if isinstance(data, dict):
                    mu_name = str(data.get("name") or data.get("title") or fallback_name)
                    members = data.get("members", [])
                    mu_members[mu_id] = (mu_name, members)
            except Exception as exc:
                logger.warning("eco_donations: Failed to get MU %s: %s", mu_id, exc)

        if not mu_members:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Kon geen MU-gegevens of leden ophalen.",
                    color=discord.Color.red(),
                )
            )
            return

        # Calculate time cutoff
        now = datetime.now(timezone.utc)
        cutoff_time = now - timedelta(hours=hours)

        # Collect all unique members
        all_members = []
        for _, (_, members) in mu_members.items():
            all_members.extend(members)
        all_members = list(set(all_members))  # deduplicate

        if not all_members:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="Geen leden gevonden in eco-MU's.",
                    color=discord.Color.orange(),
                )
            )
            return

        # Build set of eco MU member IDs for fast lookup
        eco_member_set = set(all_members)
        # Build user_id -> mu_id mapping
        user_to_mu: dict[str, str] = {}
        for mu_id, (_, members) in mu_members.items():
            for user_id in members:
                user_to_mu[user_id] = mu_id

        # Fetch transactions for the country with donation type
        # Note: API treats parameters as OR, so we fetch by countryId and filter locally
        # We paginate through all results until we reach the cutoff_time
        mu_donations: dict[str, float] = {}  # mu_id -> total donations
        cursor: Optional[str] = None
        reached_cutoff = False

        while not reached_cutoff:
            try:
                payload = {
                    "countryId": nl_country_id,
                    "transactionType": "donation",
                    "limit": 100,
                }
                if cursor:
                    payload["cursor"] = cursor

                resp = await client.get(
                    "/transaction.getPaginatedTransactions",
                    params={"input": json.dumps(payload)},
                )
                data = _unwrap(resp)
                transactions = data.get("items", []) if isinstance(data, dict) else []

                # Filter and accumulate
                for txn in transactions:
                    try:
                        user_id = txn.get("buyerId")
                        if not user_id or user_id not in eco_member_set:
                            continue

                        created_at_str = txn.get("createdAt")
                        if not created_at_str:
                            continue

                        # Parse ISO format timestamp
                        created_at = datetime.fromisoformat(
                            created_at_str.replace("Z", "+00:00")
                        )

                        # If we hit a transaction older than cutoff, we can stop paging
                        # (assuming transactions are ordered newest first)
                        if created_at < cutoff_time:
                            reached_cutoff = True
                            continue

                        amount = float(txn.get("money", 0))
                        mu_id = user_to_mu.get(user_id)
                        if mu_id:
                            mu_donations[mu_id] = mu_donations.get(mu_id, 0) + amount
                    except Exception:
                        continue

                # Check for next page
                cursor = data.get("nextCursor") if isinstance(data, dict) else None
                if not cursor:
                    break

                # Small delay to avoid rate limiting
                # await asyncio.sleep(0.2)

            except Exception as exc:
                logger.warning("eco_donations: Failed to get transactions: %s", exc)
                break

        if not mu_donations:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"Geen donaties gevonden in de afgelopen {hours} uur.",
                    color=discord.Color.orange(),
                    timestamp=now,
                )
            )
            return

        # Build table data
        rows: list[tuple[str, float]] = []
        for mu_id, total in mu_donations.items():
            mu_name = mu_members[mu_id][0]
            rows.append((mu_name, total))

        # Sort by donations descending
        rows.sort(key=lambda r: -r[1])

        total_donations = sum(r[1] for r in rows)

        # Format as monospace table
        col_name = max(len(r[0]) for r in rows)
        col_name = max(col_name, len("MU"))
        header = f"{'MU':<{col_name}}  Donaties"
        separator = "-" * (col_name + 20)
        lines = [header, separator]

        for name, amount in rows:
            lines.append(f"{name:<{col_name}}  €{amount:,.0f}")

        lines.append(separator)
        lines.append(f"{'TOTAAL':<{col_name}}  €{total_donations:,.0f}")
        table = "\n".join(lines)

        color = int(self.config.get("colors", {}).get("primary", "0x154273"), 16)
        embed = discord.Embed(
            title=f"💰 Eco-donaties – Laatste {hours} uur",
            description=f"**Totaal: €{total_donations:,.0f}**\n\n```\n{table}\n```",
            color=color,
            timestamp=now,
        )
        embed.set_footer(
            text=f"{len(rows)} MU's • {len(all_members)} leden gecontroleerd"
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Add the MU cog to the bot."""
    await bot.add_cog(MU(bot))
