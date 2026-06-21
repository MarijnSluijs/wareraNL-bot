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
from typing import Any

import discord
from discord import app_commands
from discord.ext import tasks

from cogs.tasks._base import TaskCogBase
from cogs.tasks.war_guild_divisions import DIVISION_MUS
from services.country_utils import extract_country_list
from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

# ── Cooldown intervals ──────────────────────────────────────────────────────
_DAILY_H = 24
_WEEKLY_H = 168  # 7 days
_INACTIVITY_DAYS = 3  # days without login → flagged in audit (≈72 h)

# Marijn's Discord user ID (receives the weekly audit DM)
_MARIJN_DISCORD_ID = 565626197048819731

# Members with this role are guests (no in-game account) and must never appear in audits
_GUEST_ROLE_ID = 1518315102967697610


# ── Helpers ─────────────────────────────────────────────────────────────────

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


def _unwrap_trpc(resp: object) -> object:
    """Unwrap a generic tRPC response: {result: {data: ...}} → data."""
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


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

        for name, key, interval_h, afternoon_only, skip_in_testing, coro in [
            (
                "commander_role_check",
                "commander_role_check_last_run",
                _DAILY_H,
                False,
                False,
                self._check_commander_roles,
            ),
            (
                "citizenship_audit",
                "citizenship_audit_last_run",
                _DAILY_H,
                True,  # only run in the afternoon (13–16 UTC)
                True,  # do not run automatically on the test server
                self._citizenship_audit,
            ),
        ]:
            if not self._db:
                continue
            # Skip tasks that are disabled in testing mode
            if skip_in_testing and getattr(self.bot, "testing", False):
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

        # Look up MU IDs for all division MUs from the known_mus registry
        all_mu_names: list[str] = [
            name for names in DIVISION_MUS.values() for name in names
        ]
        tracked_mu_ids: list[str] = []
        for mu_name in all_mu_names:
            mu_id, _ = await self._db.get_known_mu_by_name(mu_name)
            if mu_id:
                tracked_mu_ids.append(mu_id)
        if not tracked_mu_ids:
            logger.warning(
                "commander_role_check: no MU IDs found in known_mus for division MUs"
            )
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
        testing = bool(getattr(self.bot, "testing", False))
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

        # If the API returned no data at all, assume it is down and abort so
        # that we never accidentally strip Commander roles due to an outage.
        if stats["mus_checked"] == 0:
            logger.warning(
                "commander_role_check: no MUs could be fetched from the API "
                "(API may be down) — skipping role changes to avoid mass-removal"
            )
            return stats

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

            guest_role = guild.get_role(_GUEST_ROLE_ID)
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
                if guest_role and guest_role in member.roles:
                    continue  # guests have no in-game account by design

                discord_id = str(member.id)
                in_game_id = discord_to_ingame.get(discord_id)

                if not in_game_id:
                    no_link.append(f"• {member.mention}")
                    continue

                profile = f"https://app.warera.io/user/{in_game_id}"
                d = details.get(in_game_id)
                if not d:
                    # citizen_levels has no entry — check inactivity via live API
                    display_name = in_game_id
                    api_last_conn: str | None = None
                    if self._client:
                        try:
                            raw = await self._client.get(
                                "/user.getUserLite",
                                params={"input": json.dumps({"userId": in_game_id})},
                            )
                            data = _unwrap_trpc(raw)
                            if isinstance(data, dict):
                                display_name = data.get("username") or in_game_id
                                api_last_conn = (
                                    (data.get("dates") or {}).get("lastConnectionAt")
                                )
                        except Exception:
                            pass
                    api_days = _days_since(api_last_conn)
                    if api_days is not None and api_days > _INACTIVITY_DAYS:
                        too_inactive.append(
                            f"• {member.mention} ([{display_name}]({profile}))"
                            f" — {int(api_days)} dagen inactief"
                        )
                    continue

                citizen_name = d["citizen_name"] or in_game_id
                country = d["country_id"]
                last_login = d["last_login_at"]
                days_inactive = _days_since(last_login)

                if country != nl_country_id:
                    country_label = country_names.get(country, country)
                    wrong_country.append(
                        f"• {member.mention} ([{citizen_name}]({profile})) — land **{country_label}**"
                    )

                if days_inactive is not None and days_inactive > _INACTIVITY_DAYS:
                    too_inactive.append(
                        f"• {member.mention} ([{citizen_name}]({profile})) — {int(days_inactive)} dagen inactief"
                    )
                elif self._client:
                    # DB says active (or last_login_at is NULL) — verify with live API
                    # to catch stale/wrong stored dates (e.g. data-fetcher cached a
                    # recent-looking timestamp for a genuinely inactive member).
                    try:
                        raw = await self._client.get(
                            "/user.getUserLite",
                            params={"input": json.dumps({"userId": in_game_id})},
                        )
                        data = _unwrap_trpc(raw)
                        if isinstance(data, dict):
                            api_last_conn = (data.get("dates") or {}).get("lastConnectionAt")
                            api_days = _days_since(api_last_conn)
                            if api_days is not None and api_days > _INACTIVITY_DAYS:
                                api_name = data.get("username") or citizen_name
                                too_inactive.append(
                                    f"• {member.mention} ([{api_name}]({profile}))"
                                    f" — {int(api_days)} dagen inactief"
                                )
                    except Exception:
                        pass

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
                if guest_role and guest_role in member.roles:
                    continue

                if nederlander_role not in member.roles:
                    profile = f"https://app.warera.io/user/{user_id}"
                    missing_role.append(
                        f"• {member.mention} ([{citizen_name or user_id}]({profile}))"
                    )

        # ── Load previous snapshot & compute delta ───────────────────────────
        _SNAPSHOT_KEY = "citizenship_audit_snapshot"
        prev_snapshot: dict[str, list[str]] = {}
        if self._db:
            try:
                raw_snap = await self._db.get_poll_state(_SNAPSHOT_KEY)
                if raw_snap:
                    prev_snapshot = json.loads(raw_snap)
            except Exception:
                logger.exception("citizenship_audit: failed loading previous snapshot")

        def _inactive_days_key(line: str) -> int:
            try:
                return int(line.split(" dagen inactief")[0].rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                return 0

        too_inactive.sort(key=_inactive_days_key, reverse=True)

        current_snapshot: dict[str, list[str]] = {
            "no_link": no_link,
            "wrong_country": wrong_country,
            "too_inactive": too_inactive,
            "missing_role": missing_role,
        }

        def _mention_key(line: str) -> str:
            """Extract the mention/identifier from a bullet line for comparison."""
            # lines look like "• @mention ..." or "• @mention ([name](url)) ..."
            return line.split(" ")[1] if " " in line else line

        def _delta(section_key: str) -> tuple[list[str], list[str]]:
            prev = {_mention_key(l) for l in prev_snapshot.get(section_key, [])}
            curr = {_mention_key(l) for l in current_snapshot.get(section_key, [])}
            new_entries = [l for l in current_snapshot[section_key] if _mention_key(l) not in prev]
            resolved = [l for l in prev_snapshot.get(section_key, []) if _mention_key(l) not in curr]
            return new_entries, resolved

        # Save current snapshot
        if self._db:
            try:
                await self._db.set_poll_state(_SNAPSHOT_KEY, json.dumps(current_snapshot))
            except Exception:
                logger.exception("citizenship_audit: failed saving snapshot")

        # ── Build report ─────────────────────────────────────────────────────
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        lines: list[str] = [
            f"## 🇳🇱 Burgerschap Audit — {date_str}",
            "",
        ]

        # ── Delta section ────────────────────────────────────────────────────
        if prev_snapshot:
            all_new: list[str] = []
            all_resolved: list[str] = []
            _section_labels = {
                "no_link": "Geen identity koppeling",
                "wrong_country": "Land veranderd",
                "too_inactive": "Inactief",
                "missing_role": "Mist Nederlander-rol",
            }
            for key, label in _section_labels.items():
                new_e, resolved_e = _delta(key)
                for e in new_e:
                    all_new.append(f"• **[{label}]** {e.lstrip('• ')}")
                # Don't report "resolved" for inactivity — becoming active again
                # is normal; the absence from the current list is sufficient.
                if key != "too_inactive":
                    for e in resolved_e:
                        all_resolved.append(f"• **[{label}]** {e.lstrip('• ')}")

            lines.append("### 🔄 Wijzigingen t.o.v. vorige audit")
            if not all_new and not all_resolved:
                lines.append("*Geen wijzigingen.*")
            else:
                if all_new:
                    lines.append("**Nieuw:**")
                    lines.extend(all_new)
                if all_resolved:
                    lines.append("**Opgelost:**")
                    lines.extend(all_resolved)
            lines.append("")

        lines.append("### ❌ Geen identity koppeling")
        lines.append(
            "*Nederlander-rol maar geen in-game koppeling — gebruik `/approve` of `/identitylink`.*"
        )
        lines.extend(no_link if no_link else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append("### 🌍 Geen Nederlander in-game (land veranderd)")
        lines.append(
            "*Heeft de Discord-rol maar is in-game verhuisd.*"
        )
        lines.extend(wrong_country if wrong_country else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append(f"### 💤 Inactief ({_INACTIVITY_DAYS}+ dagen)")
        lines.append(
            f"*Al {_INACTIVITY_DAYS}+ dagen niet ingelogd — overweeg een bericht of rolverwijdering.*"
        )
        lines.extend(too_inactive if too_inactive else ["*Geen problemen gevonden.*"])

        lines.append("")
        lines.append("### 🎭 In-game Nederlanders zonder Discord rol")
        lines.append(
            "*In-game Nederlander maar mist de Discord-rol — controleer hun ticket en gebruik `/approve`.*"
        )
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
                        await channel.send(chunk, suppress_embeds=True)
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
