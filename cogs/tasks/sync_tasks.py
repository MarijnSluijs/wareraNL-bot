"""Automated daily/weekly sync tasks:

1. Daily commander role check — grant/revoke the commandant role based on
   mu.getById API data.
2. Weekly citizenship audit — DM marijn with Dutch-role holders who moved
   country / went inactive and any in-game Dutch citizens lacking the role.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase
from services.country_utils import extract_country_list
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

# ── Cooldown intervals ──────────────────────────────────────────────────────
_DAILY_H = 24
_WEEKLY_H = 168  # 7 days
_INACTIVITY_DAYS = 5  # days without login → flagged in audit

# Marijn's Discord user ID (receives the weekly audit DM)
_MARIJN_DISCORD_ID = 565626197048819731
# captainwyvern's Discord user ID (also receives the weekly audit DM)
_CAPTAINWYVERN_DISCORD_ID = 296971354807205888

# All recipients of the citizenship audit DM
_AUDIT_DM_RECIPIENTS: list[int] = [_MARIJN_DISCORD_ID, _CAPTAINWYVERN_DISCORD_ID]


# ── Helpers ─────────────────────────────────────────────────────────────────

def _mus_template_path(testing: bool = False) -> Path:
    return Path("templates/mus.testing.json" if testing else "templates/mus.json")


def _mu_entries(data: dict) -> list[dict[str, Any]]:
    return [e for e in data.get("embeds", []) if isinstance(e, dict)]


def _unwrap_mu_getbyid(resp: Any) -> dict | None:
    """Unwrap tRPC response from /mu.getById and return the data dict."""
    if not isinstance(resp, dict):
        return None
    for outer in (resp, resp.get("result", {})):
        if isinstance(outer, dict):
            data = outer.get("data")
            if isinstance(data, dict) and "_id" in data:
                return data
    return None


def _days_since(iso_str: str | None) -> float | None:
    """Return fractional days since an ISO-8601 UTC timestamp, or None."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


class SyncTasks(TaskCogBase, name="sync_tasks"):
    """Daily/weekly automated sync tasks + manual slash commands."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.sync_loop.start()

    def cog_unload(self) -> None:
        self.sync_loop.cancel()

    # ── Main hourly tick ────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def sync_loop(self) -> None:
        now = datetime.now(timezone.utc)

        for name, key, interval_h, afternoon_only, coro in [
            (
                "commander_role_check",
                "commander_role_check_last_run",
                _DAILY_H,
                False,
                self._check_commander_roles,
            ),
            (
                "citizenship_audit",
                "citizenship_audit_last_run",
                _DAILY_H,
                True,  # only run in the afternoon (13–16 UTC)
                self._citizenship_audit,
            ),
        ]:
            if not self._db:
                continue
            # Afternoon gate: only fire citizenship audit between 13:00 and 15:59 UTC
            if afternoon_only and not (13 <= now.hour < 16):
                continue
            try:
                last_str = await self._db.get_poll_state(key)
                if last_str:
                    elapsed_h = (
                        now - datetime.fromisoformat(last_str)
                    ).total_seconds() / 3600
                    if elapsed_h < interval_h:
                        continue
            except Exception:
                logger.exception("sync_loop: failed reading poll state %s", key)

            try:
                await coro()
            except Exception:
                logger.exception("sync_loop: %s failed", name)

            try:
                await self._db.set_poll_state(key, now.isoformat())
            except Exception:
                logger.exception("sync_loop: failed writing poll state %s", key)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self._wait_for_services()

    # ════════════════════════════════════════════════════════════════════════
    # Task 1 — Commander (commandant) role check
    # ════════════════════════════════════════════════════════════════════════

    async def _check_commander_roles(self) -> dict[str, int]:
        """Grant/revoke the commandant role based on mu.getById API data."""
        stats = {
            "mus_checked": 0,
            "commanders_found": 0,
            "added": 0,
            "removed": 0,
            "errors": 0,
        }
        if not self._db or not self._client:
            return stats

        testing = bool(getattr(self.bot, "testing", False))
        path = _mus_template_path(testing)
        if not path.exists():
            return stats

        data = json.loads(path.read_text(encoding="utf-8"))
        tracked_mu_ids: list[str] = [
            str(e.get("id") or "").strip()
            for e in _mu_entries(data)
            if str(e.get("id") or "").strip()
        ]
        if not tracked_mu_ids:
            return stats

        commandant_role_id: int | None = None
        try:
            commandant_role_id = int(
                self.bot.config.get("roles", {}).get("commandant", 0) or 0
            )
        except (TypeError, ValueError):
            commandant_role_id = None

        if not commandant_role_id:
            logger.warning("commander_role_check: commandant role ID not configured")
            return stats

        # Collect all in-game commander IDs across tracked MUs
        current_commanders: set[str] = set()  # in_game_user_ids

        for mu_id in tracked_mu_ids:
            try:
                resp = await self._client.get(
                    "/mu.getById",
                    params={"input": json.dumps({"muId": mu_id})},
                )
                mu_data = _unwrap_mu_getbyid(resp)
                if not mu_data:
                    continue
                stats["mus_checked"] += 1
                commanders: list[str] = mu_data.get("roles", {}).get("commanders", [])
                # get managers (owner) and append
                commanders.extend(mu_data.get("roles", {}).get("managers", []))
                current_commanders.update(str(c) for c in commanders if c)
            except Exception:
                logger.exception(
                    "commander_role_check: failed fetching mu %s", mu_id
                )

        # In testing mode, merge in fake commanders defined in config
        if testing:
            fake_commanders = self.bot.config.get("testing_commanders", [])
            current_commanders.update(str(c) for c in fake_commanders if c)
            if fake_commanders:
                logger.info(
                    "commander_role_check: testing mode — added %d fake commanders: %s",
                    len(fake_commanders),
                    fake_commanders,
                )

        stats["commanders_found"] = len(current_commanders)

        for guild in self.bot.guilds:
            guild_id = str(guild.id)
            commandant_role = guild.get_role(commandant_role_id)
            if commandant_role is None:
                continue

            # Build in_game → discord mapping for all linked users
            try:
                links = await self._db.get_identity_links_for_guild(guild_id)
            except Exception:
                logger.exception(
                    "commander_role_check: failed loading links for guild %s", guild.id
                )
                continue

            ingame_to_discord: dict[str, str] = {
                str(link["in_game_user_id"]): str(link["discord_user_id"])
                for link in links
                if link.get("in_game_user_id") and link.get("discord_user_id")
            }
            discord_to_ingame: dict[str, str] = {v: k for k, v in ingame_to_discord.items()}

            # Current holder set (members who already have commandant role)
            current_holders: set[discord.Member] = {
                m for m in guild.members if commandant_role in m.roles and not m.bot
            }

            # Grant role to commanders not yet holding it
            for in_game_id in current_commanders:
                discord_id = ingame_to_discord.get(in_game_id)
                if not discord_id or not discord_id.isdigit():
                    continue
                member = guild.get_member(int(discord_id))
                if member is None or member.bot:
                    continue
                if commandant_role in member.roles:
                    continue
                try:
                    await member.add_roles(
                        commandant_role,
                        reason="Commandant rol sync op basis van MU API data",
                    )
                    stats["added"] += 1
                except discord.HTTPException:
                    stats["errors"] += 1

            # Revoke role from members who are no longer commanders
            for member in current_holders:
                discord_id = str(member.id)
                in_game_id = discord_to_ingame.get(discord_id)
                if in_game_id and in_game_id in current_commanders:
                    continue  # still a commander
                try:
                    await member.remove_roles(
                        commandant_role,
                        reason="Commandant rol sync — niet meer commandant",
                    )
                    stats["removed"] += 1
                except discord.HTTPException:
                    stats["errors"] += 1

        logger.info(
            "commander_role_check: done (mus=%d commanders=%d added=%d removed=%d errors=%d)",
            stats["mus_checked"],
            stats["commanders_found"],
            stats["added"],
            stats["removed"],
            stats["errors"],
        )
        return stats

    # ════════════════════════════════════════════════════════════════════════
    # Task 2 — Weekly citizenship audit
    # ════════════════════════════════════════════════════════════════════════

    async def _citizenship_audit(self) -> None:
        """DM marijn with Dutch-role holders who moved/went inactive and
        in-game Dutch citizens without the Discord Dutch role."""
        if not self._db:
            return

        nl_country_id: str | None = self.bot.config.get("nl_country_id")
        nederlander_role_id: int | None = None
        try:
            nederlander_role_id = int(
                self.bot.config.get("roles", {}).get("nederlander", 0) or 0
            )
        except (TypeError, ValueError):
            nederlander_role_id = None

        if not nl_country_id or not nederlander_role_id:
            logger.warning("citizenship_audit: nl_country_id or nederlander role not configured")
            return

        no_link: list[str] = []         # geen identity koppeling
        not_in_db: list[str] = []        # niet gevonden in citizen DB
        wrong_country: list[str] = []    # geen Nederlander in-game
        too_inactive: list[str] = []     # inactief 5+ dagen
        missing_role: list[str] = []     # in-game Nederlanders zonder Discord rol

        # Build country id → name lookup
        country_names: dict[str, str] = {}
        if self._client:
            try:
                raw = await self._client.get("/country.getAllCountries")
                for c in extract_country_list(raw):
                    cid = str(c.get("_id") or c.get("id") or "")
                    name = str(c.get("name") or c.get("code") or cid)
                    if cid:
                        country_names[cid] = name
            except Exception:
                logger.warning("citizenship_audit: failed to fetch country list for name lookup")

        for guild in self.bot.guilds:
            nederlander_role = guild.get_role(nederlander_role_id)
            if nederlander_role is None:
                continue

            # ── Section A: Discord members with 'nederlander' role ──────────
            try:
                links = await self._db.get_identity_links_for_guild(str(guild.id))
            except Exception:
                logger.exception("citizenship_audit: failed loading links for guild %s", guild.id)
                continue

            ingame_to_discord: dict[str, str] = {
                str(link["in_game_user_id"]): str(link["discord_user_id"])
                for link in links
                if link.get("in_game_user_id") and link.get("discord_user_id")
            }
            discord_to_ingame: dict[str, str] = {v: k for k, v in ingame_to_discord.items()}

            holders = [m for m in guild.members if nederlander_role in m.roles and not m.bot]
            holder_ingame_ids = [
                discord_to_ingame[str(m.id)]
                for m in holders
                if str(m.id) in discord_to_ingame
            ]

            try:
                details = await self._db.get_citizen_details_by_ids(holder_ingame_ids)
            except Exception:
                logger.exception("citizenship_audit: failed loading citizen details")
                details = {}

            for member in holders:
                discord_id = str(member.id)
                in_game_id = discord_to_ingame.get(discord_id)

                if not in_game_id:
                    no_link.append(f"• {member.mention}")
                    continue

                d = details.get(in_game_id)
                if not d:
                    not_in_db.append(f"• {member.mention}")
                    continue

                country = d["country_id"]
                last_login = d["last_login_at"]
                days_inactive = _days_since(last_login)

                if country != nl_country_id:
                    country_label = country_names.get(country, country)
                    wrong_country.append(
                        f"• {member.mention} — land **{country_label}**"
                    )

                if days_inactive is not None and days_inactive > _INACTIVITY_DAYS:
                    too_inactive.append(
                        f"• {member.mention} — {int(days_inactive)} dagen inactief"
                    )

            # ── Section B: In-game Dutch citizens without 'nederlander' role ─
            try:
                nl_citizens = await self._db.get_citizens_in_country(nl_country_id)
            except Exception:
                logger.exception("citizenship_audit: failed loading NL citizens")
                nl_citizens = []

            for user_id, _cid, citizen_name in nl_citizens:
                discord_id = ingame_to_discord.get(user_id)
                if not discord_id or not discord_id.isdigit():
                    continue

                member = guild.get_member(int(discord_id))
                if member is None or member.bot:
                    continue

                if nederlander_role not in member.roles:
                    missing_role.append(
                        f"• {member.mention} — in-game: **{citizen_name or user_id}**"
                    )

        # ── Build report ─────────────────────────────────────────────────────
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        ping = f"<@{_CAPTAINWYVERN_DISCORD_ID}>"
        lines: list[str] = [
            f"## 🇳🇱 Burgerschap Audit — {date_str}",
            f"{ping} hier is de dagelijkse audit.",
            "",
        ]

        lines.append("### ❌ Geen identity koppeling")
        lines.extend(no_link if no_link else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append("### 🔍 Niet gevonden in citizen DB")
        lines.extend(not_in_db if not_in_db else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append("### 🌍 Geen Nederlander in-game (land veranderd)")
        lines.extend(wrong_country if wrong_country else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append(f"### 💤 Inactief ({_INACTIVITY_DAYS}+ dagen)")
        lines.extend(too_inactive if too_inactive else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append("### 🎭 In-game Nederlanders zonder Discord rol")
        lines.extend(missing_role if missing_role else ["*Geen problemen gevonden.*"])

        report = "\n".join(lines)

        # Chunk into ≤2000-char DM messages
        chunks: list[str] = []
        current = ""
        for line in report.split("\n"):
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line
            else:
                current += ("\n" if current else "") + line
        if current:
            chunks.append(current)

        # Send to the audit-log channel instead of DMs
        channel_id = self.bot.config.get("channels", {}).get("audit_log")
        if not channel_id:
            # fallback to testing-area on test servers
            channel_id = self.bot.config.get("channels", {}).get("testing-area")
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel is not None:
                try:
                    for chunk in chunks:
                        await channel.send(chunk)
                    logger.info(
                        "citizenship_audit: report sent to channel %d (%d sections)",
                        int(channel_id), len(chunks),
                    )
                except Exception:
                    logger.exception(
                        "citizenship_audit: failed to send to channel %d", int(channel_id)
                    )
            else:
                logger.warning(
                    "citizenship_audit: audit_log channel %d not found", int(channel_id)
                )
        else:
            logger.warning("citizenship_audit: no audit_log channel configured")

    # ════════════════════════════════════════════════════════════════════════
    # Slash commands — manual triggers
    # ════════════════════════════════════════════════════════════════════════

    def _is_privileged(self, interaction: discord.Interaction) -> bool:
        """Return True if the invoker is the bot owner or has a privileged role."""
        if getattr(self.bot, "testing", False):
            return True
        if interaction.user.id == _MARIJN_DISCORD_ID:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        privileged_keys = {"officier", "government", "commandant"}
        role_ids = {
            self.bot.config.get("roles", {}).get(k)
            for k in privileged_keys
        }
        return any(r.id in role_ids for r in interaction.user.roles)

    @app_commands.command(
        name="commandant-check",
        description="Sync de commandant rol op basis van MU API data.",
    )
    @has_privileged_role()
    async def cmd_commandant_check(self, interaction: discord.Interaction) -> None:
        if not self._is_privileged(interaction):
            await interaction.response.send_message(
                "Je hebt geen toegang tot dit commando.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🎖️ Commandant check gestart...", ephemeral=True
        )
        try:
            stats = await self._check_commander_roles()
        except Exception as exc:
            logger.exception("cmd_commandant_check: failed")
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Commandant check klaar — MU's gecontroleerd: **{stats['mus_checked']}**, "
            f"commandanten gevonden: **{stats['commanders_found']}**, "
            f"toegevoegd: **{stats['added']}**, verwijderd: **{stats['removed']}**, "
            f"fouten: **{stats['errors']}**.",
            ephemeral=True,
        )
        if self._db:
            await self._db.set_poll_state(
                "commander_role_check_last_run", datetime.now(timezone.utc).isoformat()
            )

    @app_commands.command(
        name="burgerschap-audit",
        description="Wekelijkse burgerschap audit — post rapport in #audit-log.",
    )
    @has_privileged_role()
    async def cmd_burgerschap_audit(self, interaction: discord.Interaction) -> None:
        if not self._is_privileged(interaction):
            await interaction.response.send_message(
                "Je hebt geen toegang tot dit commando.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "📋 Burgerschap audit gestart...", ephemeral=True
        )
        try:
            await self._citizenship_audit()
        except Exception as exc:
            logger.exception("cmd_burgerschap_audit: failed")
            await interaction.followup.send(f"❌ Fout: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ Burgerschap audit klaar — rapport gepost in #audit-log.",
            ephemeral=True,
        )
        if self._db:
            await self._db.set_poll_state(
                "citizenship_audit_last_run", datetime.now(timezone.utc).isoformat()
            )


async def setup(bot) -> None:
    """Add the SyncTasks cog to the bot."""
    await bot.add_cog(SyncTasks(bot))
