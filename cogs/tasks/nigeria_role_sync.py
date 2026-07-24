"""Background task: sync Nigerian role to NL players currently in Nigeria in-game.

Every 6 hours, iterates all members of the production guild who have the
Netherlands role and adds or removes the Nigerian role based on in-game country.

Lookup strategy (in order):
  1. identity_links (all guilds) → in-game user ID → citizen_levels by user ID
  2. Fallback: citizen_levels by Discord display name (for members without a link)

Role assignment:
  - country_id == Nigeria AND logged in within 3 days → keep/add Nigerian role
  - country_id != Nigeria, inactive, or not found in citizen_levels → remove role
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_NIGERIAN_ROLE_ID   = 1530164551163842611
_NIGERIA_COUNTRY_ID = "683ddd2c24b5a2e114af15fa"
# Players inactive longer than this are not considered to be actively fighting
_INACTIVE_THRESHOLD = timedelta(days=3)


def _is_active_in_nigeria(
    entry: tuple[str, str | None] | None,
    now: datetime,
) -> bool:
    """Return True iff the citizen_levels entry shows Nigeria + recent login."""
    if entry is None:
        return False
    country_id, last_login_at = entry
    if country_id != _NIGERIA_COUNTRY_ID:
        return False
    if not last_login_at:
        return False
    try:
        login_dt = datetime.fromisoformat(last_login_at.replace("Z", "+00:00"))
        return (now - login_dt) < _INACTIVE_THRESHOLD
    except Exception:
        return False


class NigeriaRoleSyncCog(TaskCogBase, name="nigeria_role_sync"):
    def __init__(self, bot) -> None:
        self.bot = bot

    def cog_load(self) -> None:
        self.nigeria_role_sync.start()

    def cog_unload(self) -> None:
        self.nigeria_role_sync.cancel()

    @tasks.loop(hours=6)
    async def nigeria_role_sync(self) -> None:
        try:
            await self._run_sync()
        except Exception:
            logger.exception("nigeria_role_sync: unexpected error")

    @nigeria_role_sync.before_loop
    async def _before(self) -> None:
        await self._wait_for_services()

    async def _run_sync(self) -> tuple[int, int, int, int]:
        """Sync Nigerian role based on each NL member's current in-game country.

        Returns (added, removed, no_data_removed, already_correct) counts.
        """
        guild_id   = int(self.config.get("guild_id") or 0)
        nl_role_id = int((self.config.get("roles") or {}).get("nederlander") or 0)
        if not guild_id or not nl_role_id:
            logger.warning(
                "nigeria_role_sync: guild_id or roles.nederlander not configured"
            )
            return 0, 0, 0, 0

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning(
                "nigeria_role_sync: production guild %d not in cache", guild_id
            )
            return 0, 0, 0, 0

        nl_role = guild.get_role(nl_role_id)
        nigerian_role = guild.get_role(_NIGERIAN_ROLE_ID)
        if nl_role is None or nigerian_role is None:
            logger.warning(
                "nigeria_role_sync: Netherlands role or Nigerian role not found in guild"
            )
            return 0, 0, 0, 0

        nl_members = [m for m in nl_role.members if not m.bot]
        if not nl_members or not self._db:
            return 0, 0, 0, 0

        # ── Build lookup data ──────────────────────────────────────────────────

        # discord_id → in_game_id from identity_links across ALL guilds
        discord_to_ingame: dict[str, str] = await self._db.get_discord_to_ingame_map()

        # Gather in-game IDs for members we can map, and names for the rest
        mapped_ids: list[str] = []
        unmapped_nicks: list[str] = []
        for member in nl_members:
            ingame_id = discord_to_ingame.get(str(member.id))
            if ingame_id:
                mapped_ids.append(ingame_id)
            else:
                unmapped_nicks.append((member.nick or member.name).lower())

        # Batch-fetch citizen_levels data for both lookup paths
        id_country_map   = await self._db.get_citizen_countries_by_ids(mapped_ids)
        name_country_map = await self._db.get_citizen_countries_by_names(unmapped_nicks)

        # ── Apply roles ────────────────────────────────────────────────────────

        now = datetime.now(timezone.utc)
        added = removed = no_data_removed = already_correct = 0

        for member in nl_members:
            ingame_id = discord_to_ingame.get(str(member.id))
            if ingame_id:
                entry = id_country_map.get(ingame_id)
            else:
                nick = (member.nick or member.name).lower()
                entry = name_country_map.get(nick)

            should_have = _is_active_in_nigeria(entry, now)
            has_role    = nigerian_role in member.roles

            if should_have == has_role:
                already_correct += 1
                continue

            if should_have:
                try:
                    await member.add_roles(
                        nigerian_role,
                        reason="nigeria_role_sync: NL speler vecht actief in Nigeria",
                    )
                    added += 1
                    logger.info(
                        "nigeria_role_sync: added Nigerian role to %s (%d)",
                        member, member.id,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "nigeria_role_sync: no permission to add role to %s", member
                    )
            else:
                # entry is None (not in citizen_levels) or wrong country/inactive
                reason = (
                    "nigeria_role_sync: NL speler niet meer actief in Nigeria"
                    if entry is not None
                    else "nigeria_role_sync: geen in-game data gevonden, rol verwijderd"
                )
                try:
                    await member.remove_roles(nigerian_role, reason=reason)
                    if entry is None:
                        no_data_removed += 1
                    else:
                        removed += 1
                    logger.info(
                        "nigeria_role_sync: removed Nigerian role from %s (%d) — %s",
                        member, member.id,
                        "no data" if entry is None else f"country={entry[0]}",
                    )
                except discord.Forbidden:
                    logger.warning(
                        "nigeria_role_sync: no permission to remove role from %s", member
                    )

        logger.info(
            "nigeria_role_sync: done — %d added, %d removed (country/inactive), "
            "%d removed (no data), %d already correct",
            added, removed, no_data_removed, already_correct,
        )
        return added, removed, no_data_removed, already_correct

    @commands.command(name="sync_nigeria_roles", hidden=True)
    @commands.is_owner()
    async def sync_nigeria_roles(self, ctx: commands.Context) -> None:
        """Immediately run the Nigeria role sync."""
        added, removed, no_data, correct = await self._run_sync()
        await ctx.send(
            f"✅ Nigeria role sync klaar — "
            f"**{added}** toegevoegd, **{removed}** verwijderd (land/inactief), "
            f"**{no_data}** verwijderd (geen data), **{correct}** al correct.",
        )


async def setup(bot) -> None:
    await bot.add_cog(NigeriaRoleSyncCog(bot))
