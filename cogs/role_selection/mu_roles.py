"""MU role selection commands and MU membership management."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.role_selection.roles import RoleToggleView, load_roles_template, mu_roles_path
from utils.checks import has_privileged_role

logger = logging.getLogger(__name__)


_MAX_BUTTONS_PER_MESSAGE = 25


def _chunk_buttons_for_view(buttons: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split buttons into Discord-safe pages and recompute rows per page."""
    chunks: list[list[dict[str, Any]]] = []
    for start in range(0, len(buttons), _MAX_BUTTONS_PER_MESSAGE):
        page = buttons[start : start + _MAX_BUTTONS_PER_MESSAGE]
        normalized_page: list[dict[str, Any]] = []
        for idx, btn in enumerate(page):
            item = dict(btn)
            item["row"] = idx // 5
            normalized_page.append(item)
        if normalized_page:
            chunks.append(normalized_page)
    return chunks


class MuRoles(commands.Cog, name="mu_roles"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.template = load_roles_template(
            mu_roles_path(getattr(bot, "testing", False))
        )

        if self.template.get("embeds"):
            for embed_data in self.template["embeds"]:
                for page in _chunk_buttons_for_view(embed_data.get("buttons", [])):
                    self.bot.add_view(RoleToggleView(page, exclusive=True))
        if self.template.get("buttons"):
            for page in _chunk_buttons_for_view(self.template["buttons"]):
                self.bot.add_view(RoleToggleView(page, exclusive=True))

    @commands.command(name="muroles")
    @has_privileged_role()
    async def muroles(self, ctx: Context) -> None:
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            ctx.guild.get_channel(mu_channel_id) if mu_channel_id and ctx.guild else None
        ) or ctx.channel

        mus_cog = self.bot.cogs.get("mus")
        if mus_cog:
            try:
                await mus_cog._repost_mu_list(target_channel)
                await ctx.send(f"✅ MU-lijst + knoppen opnieuw gepost in {target_channel.mention}.")
                return
            except Exception as exc:
                await ctx.send(f"❌ Herposten van MU-lijst mislukt: {exc}")
                return

        await ctx.send(f"✅ MU-rolknoppen gepost in {target_channel.mention}.")

    @commands.command(name="muwachtlijst")
    @has_privileged_role()
    async def muwachtlijst(self, ctx: Context) -> None:
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ Guild not found.")
            return

        wachtlijst_role_id = self.bot.config.get("roles", {}).get("wachtlijst")
        if not wachtlijst_role_id:
            await ctx.send("❌ Wachtlijst role not configured.")
            return

        wachtlijst_role = guild.get_role(wachtlijst_role_id)
        if not wachtlijst_role:
            await ctx.send("❌ Wachtlijst role not found.")
            return

        count = len(wachtlijst_role.members)
        await ctx.send(f"📋 Er zijn momenteel {count} mensen op de wachtlijst voor MU's.")


async def setup(bot) -> None:
    """Add the MuRoles cog to the bot."""
    await bot.add_cog(MuRoles(bot))
