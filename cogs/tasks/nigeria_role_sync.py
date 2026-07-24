"""Background task: sync Nigerian role to NL players currently in Nigeria in-game.

Every 6 hours, iterates all members of the production guild who have the
Netherlands role, checks their current in-game country via citizen_levels,
and adds or removes the Nigerian role accordingly.

- country_id == NIGERIA_COUNTRY_ID  → add Nigerian role
- country_id != NIGERIA_COUNTRY_ID  → remove Nigerian role (if they have it)
- name not found in citizen_levels   → skip (cannot determine country)

Discord members are matched to citizen_levels rows by their current nickname
(or display name as fallback), which the hourly citizen sync keeps up to date.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

_NIGERIAN_ROLE_ID   = 1530164551163842611
_NIGERIA_COUNTRY_ID = "683ddd2c24b5a2e114af15fa"


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

        Returns (added, removed, skipped_no_match, already_correct) counts.
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
        if nl_role is None:
            logger.warning(
                "nigeria_role_sync: Netherlands role %d not found", nl_role_id
            )
            return 0, 0, 0, 0

        nigerian_role = guild.get_role(_NIGERIAN_ROLE_ID)
        if nigerian_role is None:
            logger.warning(
                "nigeria_role_sync: Nigerian role %d not found", _NIGERIAN_ROLE_ID
            )
            return 0, 0, 0, 0

        # Collect NL members and their display names for batch DB lookup
        nl_members = [m for m in nl_role.members if not m.bot]
        if not nl_members:
            return 0, 0, 0, 0

        # Build nick → member map (use nick if set, else Discord username)
        nick_to_member: dict[str, discord.Member] = {}
        for member in nl_members:
            name = (member.nick or member.name).lower()
            nick_to_member[name] = member

        # Batch-lookup current country for each display name
        if not self._db:
            logger.warning("nigeria_role_sync: DB not available")
            return 0, 0, 0, 0

        country_map = await self._db.get_citizen_countries_by_names(
            list(nick_to_member.keys())
        )

        added = removed = skipped = already_correct = 0

        for nick_lower, member in nick_to_member.items():
            country_id = country_map.get(nick_lower)
            in_nigeria_ingame = country_id == _NIGERIA_COUNTRY_ID
            has_nigerian_role = nigerian_role in member.roles

            if country_id is None:
                # Can't determine in-game country — leave role as-is
                skipped += 1
                continue

            if in_nigeria_ingame and not has_nigerian_role:
                try:
                    await member.add_roles(
                        nigerian_role,
                        reason="nigeria_role_sync: NL speler vecht in Nigeria",
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
            elif not in_nigeria_ingame and has_nigerian_role:
                try:
                    await member.remove_roles(
                        nigerian_role,
                        reason="nigeria_role_sync: NL speler niet meer in Nigeria",
                    )
                    removed += 1
                    logger.info(
                        "nigeria_role_sync: removed Nigerian role from %s (%d)",
                        member, member.id,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "nigeria_role_sync: no permission to remove role from %s", member
                    )
            else:
                already_correct += 1

        logger.info(
            "nigeria_role_sync: done — %d added, %d removed, "
            "%d skipped (no name match), %d already correct",
            added, removed, skipped, already_correct,
        )
        return added, removed, skipped, already_correct

    @commands.command(name="sync_nigeria_roles", hidden=True)
    @commands.is_owner()
    async def sync_nigeria_roles(self, ctx: commands.Context) -> None:
        """Immediately run the Nigeria role sync."""
        added, removed, skipped, already_correct = await self._run_sync()
        await ctx.send(
            f"✅ Nigeria role sync klaar — "
            f"**{added}** toegevoegd, **{removed}** verwijderd, "
            f"**{skipped}** overgeslagen (naam niet gevonden), "
            f"**{already_correct}** al correct.",
        )


async def setup(bot) -> None:
    await bot.add_cog(NigeriaRoleSyncCog(bot))
