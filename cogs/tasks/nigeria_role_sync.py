"""Background task: sync Nigerian role to NL players fighting in Nigeria.

Every 6 hours, iterates all members of the production guild who have the
Netherlands role and ensures they also have the Nigerian role.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from cogs.tasks._base import TaskCogBase

logger = logging.getLogger("discord_bot")

# Role given to Dutch players who are temporarily fighting in Nigeria.
_NIGERIAN_ROLE_ID = 1530164551163842611


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

    async def _run_sync(self) -> tuple[int, int]:
        """Give the Nigerian role to all NL members who don't have it yet.

        Returns (added, already_had) counts.
        """
        guild_id = int(self.config.get("guild_id") or 0)
        nl_role_id = int((self.config.get("roles") or {}).get("nederlander") or 0)
        if not guild_id or not nl_role_id:
            logger.warning("nigeria_role_sync: guild_id or roles.nederlander not configured")
            return 0, 0

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            logger.warning("nigeria_role_sync: production guild %d not in cache", guild_id)
            return 0, 0

        nl_role = guild.get_role(nl_role_id)
        if nl_role is None:
            logger.warning("nigeria_role_sync: Netherlands role %d not found", nl_role_id)
            return 0, 0

        nigerian_role = guild.get_role(_NIGERIAN_ROLE_ID)
        if nigerian_role is None:
            logger.warning("nigeria_role_sync: Nigerian role %d not found", _NIGERIAN_ROLE_ID)
            return 0, 0

        added = 0
        already_had = 0
        for member in nl_role.members:
            if member.bot:
                continue
            if nigerian_role in member.roles:
                already_had += 1
                continue
            try:
                await member.add_roles(
                    nigerian_role,
                    reason="nigeria_role_sync: NL speler vecht tijdelijk in Nigeria",
                )
                added += 1
                logger.info("nigeria_role_sync: gave Nigerian role to %s (%d)", member, member.id)
            except discord.Forbidden:
                logger.warning("nigeria_role_sync: no permission to add role to %s", member)

        logger.info(
            "nigeria_role_sync: done — %d added, %d already had the role",
            added, already_had,
        )
        return added, already_had

    @commands.command(name="sync_nigeria_roles", hidden=True)
    @commands.is_owner()
    async def sync_nigeria_roles(self, ctx: commands.Context) -> None:
        """Immediately run the Nigeria role sync for all NL members."""
        added, already_had = await self._run_sync()
        await ctx.send(
            f"✅ Nigeria role sync klaar — **{added}** rollen toegevoegd, "
            f"**{already_had}** hadden hem al.",
            ephemeral=True,
        )


async def setup(bot) -> None:
    await bot.add_cog(NigeriaRoleSyncCog(bot))
