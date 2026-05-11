"""
/profiel — Show hidden stats about a WarEra player (via user.getUserLite).

Displays: account creation date, health/hunger/energy/entrepreneurship bars,
last skills reset (and days until next reset), cases opened, gems purchased,
and total bounty collected.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase, citizen_autocomplete
from utils.checks import has_privileged_role

if TYPE_CHECKING:
    from bot import DiscordBot

logger = logging.getLogger("discord_bot")

# Skill reset cooldown in days (16h = 0.667d, but WarEra uses a fixed 7-day window)
# The API doesn't expose the reset cooldown directly; we derive it from lastSkillsResetAt.
_RESET_COOLDOWN_DAYS = 7


def _unwrap(resp: object) -> object:
    if isinstance(resp, dict):
        return resp.get("result", {}).get("data", resp)
    return resp


def _bar(current: float, total: float) -> str:
    """Return 'current/total' nicely formatted."""
    cur = round(current, 1)
    tot = int(total)
    return f"{cur}/{tot}"


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "onbekend"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M:%S")
    except (ValueError, TypeError):
        return iso


def _days_since(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


class ProfielCog(CommandCogBase, name="profiel"):
    """Player profile lookup command."""

    def __init__(self, bot: DiscordBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _search_user(self, username: str) -> list[str]:
        """Return candidate user IDs for *username* via the search API."""
        try:
            raw = await self._client.get(
                "/search.searchAnything",
                params={"input": json.dumps({"searchText": username})},
            )
            data = _unwrap(raw)
            ids: list = data.get("userIds", []) if isinstance(data, dict) else []
            return ids[:5]
        except Exception as exc:
            logger.warning("profiel: search failed for %r: %s", username, exc)
            return []

    async def _get_user_lite(self, user_id: str) -> Optional[dict]:
        """Fetch getUserLite for a single user ID and unwrap the result."""
        try:
            raw = await self._client.get(
                "/user.getUserLite",
                params={"input": json.dumps({"userId": user_id})},
            )
            return _unwrap(raw) if isinstance(raw, dict) else None
        except Exception as exc:
            logger.warning("profiel: getUserLite failed for %s: %s", user_id, exc)
            return None

    async def _resolve_user(self, query: str) -> tuple[Optional[str], Optional[dict]]:
        """Resolve *query* (username) → (user_id, profile dict).

        Tries exact match first, then closest candidate by name similarity.
        Falls back to fuzzy DB search if the API returns nothing.
        """
        q_low = query.strip().lower()
        user_ids = await self._search_user(query)

        if not user_ids:
            db = self._db
            if db is not None:
                nl_country_id = self.config.get("nl_country_id")
                try:
                    match = await db.fuzzy_citizen_by_name(query, country_id=nl_country_id)
                    if match:
                        uid, _ = match
                        profile = await self._get_user_lite(uid)
                        if profile is not None:
                            return uid, profile
                except Exception:
                    pass
            return None, None

        candidates: list[tuple[str, dict]] = []
        for uid in user_ids:
            profile = await self._get_user_lite(uid)
            if profile is not None:
                candidates.append((uid, profile))

        # Exact username match wins
        for uid, profile in candidates:
            if (profile.get("username") or "").lower().strip() == q_low:
                return uid, profile

        # Best fuzzy match
        best_uid, best_profile, best_ratio = None, None, -1.0
        for uid, profile in candidates:
            name = (profile.get("username") or "").lower().strip()
            ratio = difflib.SequenceMatcher(None, q_low, name).ratio()
            if ratio > best_ratio:
                best_uid, best_profile, best_ratio = uid, profile, ratio

        if best_uid is not None:
            return best_uid, best_profile

        return None, None

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    @app_commands.command(
        name="profiel",
        description="Toon verborgen stats van een WarEra speler.",
    )
    @app_commands.describe(speler="Naam van de WarEra speler")
    @app_commands.autocomplete(speler=citizen_autocomplete)
    @has_privileged_role()
    async def profiel(
        self,
        interaction: discord.Interaction,
        speler: str,
    ) -> None:
        """Fetch and display hidden player stats from getUserLite."""
        await interaction.response.defer(thinking=True)

        client = self._client
        if not client or client.is_available is False:
            await self._send_api_offline(interaction)
            return

        user_id, profile = await self._resolve_user(speler)

        if not profile:
            await interaction.followup.send(
                f"❌ Speler **{speler}** niet gevonden."
            )
            return

        username = profile.get("username") or speler
        profile_url = f"https://app.warera.io/user/{user_id}"
        avatar_url = profile.get("avatarUrl")

        # ── Dates ──────────────────────────────────────────────────────
        created_at: Optional[str] = profile.get("createdAt")
        dates: dict = profile.get("dates") or {}
        last_reset: Optional[str] = dates.get("lastSkillsResetAt")

        days_since_reset = _days_since(last_reset)
        if days_since_reset is not None:
            days_left = max(0.0, _RESET_COOLDOWN_DAYS - days_since_reset)
            reset_str = (
                f"{_fmt_date(last_reset)}"
                + (f" ({days_left:.1f}d resterend)" if days_left > 0 else " (kan nu resetten)")
            )
        else:
            reset_str = "onbekend"

        # ── Skills ────────────────────────────────────────────────────
        skills: dict = profile.get("skills") or {}

        def _skill_bar(key: str) -> str:
            s = skills.get(key)
            if not isinstance(s, dict):
                return "?"
            cur = s.get("currentBarValue")
            tot = s.get("total")
            if cur is None or tot is None:
                return "?"
            return _bar(float(cur), float(tot))

        health_str = _skill_bar("health")
        hunger_str = _skill_bar("hunger")
        energy_str = _skill_bar("energy")
        entrepreneurship_str = _skill_bar("entrepreneurship")

        # ── Rankings (cases, gems, bounty) ────────────────────────────
        rankings: dict = profile.get("rankings") or {}

        def _ranking_val(key: str) -> str:
            r = rankings.get(key)
            if isinstance(r, dict):
                v = r.get("value")
                if v is not None:
                    return f"{v:,.0f}" if isinstance(v, (int, float)) else str(v)
            return "onbekend"

        cases_str = _ranking_val("userCasesOpened")
        gems_str = _ranking_val("userGemsPurchased")
        bounty_val = (rankings.get("userBounty") or {}).get("value")
        bounty_str = f"{bounty_val:,.2f} CC" if isinstance(bounty_val, (int, float)) else "onbekend"

        # ── MU ────────────────────────────────────────────────────────
        mu_str = "Geen"
        mu_id_resolved: str | None = None
        mu_name_resolved: str | None = None

        # First try the DB (has cached mu_id + mu_name from periodic sync)
        if self._db is not None and user_id:
            try:
                async with self._db._conn.execute(
                    "SELECT mu_id, mu_name FROM citizen_levels WHERE user_id = ?",
                    (user_id,),
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0]:
                        mu_id_resolved = str(row[0])
                        mu_name_resolved = str(row[1]) if row[1] else None
            except Exception:
                pass

        # Fall back to the raw profile field (may be a string ID or a dict)
        if not mu_id_resolved:
            mu_raw = profile.get("mu")
            if isinstance(mu_raw, str) and mu_raw:
                mu_id_resolved = mu_raw
            elif isinstance(mu_raw, dict):
                mu_id_resolved = mu_raw.get("_id") or mu_raw.get("id") or mu_raw.get("muId")
                mu_name_resolved = mu_raw.get("name") or mu_raw.get("title")
                if mu_id_resolved:
                    mu_id_resolved = str(mu_id_resolved)

        if mu_id_resolved:
            label = mu_name_resolved or mu_id_resolved
            mu_str = f"[{label}](https://app.warera.io/military-unit/{mu_id_resolved})"

        # ── Embed ─────────────────────────────────────────────────────
        color = self._embed_colour()
        embed = discord.Embed(
            title=f"🧾 Profiel — {username}",
            url=profile_url,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(
            name="📅 Account aangemaakt",
            value=_fmt_date(created_at),
            inline=True,
        )
        embed.add_field(
            name="🔁 Skills gereset",
            value=reset_str,
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=False)  # spacer

        embed.add_field(name="❤️ Health", value=health_str, inline=True)
        embed.add_field(name="🍞 Hunger", value=hunger_str, inline=True)
        embed.add_field(name="⚡ Energy", value=energy_str, inline=True)
        embed.add_field(name="💼 Entrepreneurship", value=entrepreneurship_str, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)  # spacer

        embed.add_field(name="📦 Cases geopend", value=cases_str, inline=True)
        embed.add_field(name="💎 Gems gekocht", value=gems_str, inline=True)
        embed.add_field(name="🎯 Bounty ontvangen", value=bounty_str, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)  # spacer

        embed.add_field(name="⚔️ Military Unit", value=mu_str, inline=False)

        embed.set_footer(text=f"ID: {user_id}")

        await interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(ProfielCog(bot))
