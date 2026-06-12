"""General role posting command for the role selection channel."""

import json

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands.bedrijvenbonuscheck import (
    BedrijvenBonusCheckView,
    _load_state,
    _save_state,
)
from cogs.commands.pillreminder import PillReminderView
from cogs.commands.pillreminder import _load_state as _pill_load_state
from cogs.commands.pillreminder import _save_state as _pill_save_state
from utils.checks import has_privileged_role

from .roles import RoleToggleView, general_roles_path, load_roles_template


class GeneralRoles(commands.Cog, name="general_role_selection"):
    def __init__(self, bot) -> None:
        self.bot = bot

        try:
            template = load_roles_template(
                general_roles_path(getattr(bot, "testing", False))
            )
            if template.get("embeds"):
                for embed_data in template["embeds"]:
                    if embed_data.get("buttons"):
                        self.bot.add_view(
                            RoleToggleView(
                                embed_data["buttons"],
                                exclusive=bool(embed_data.get("exclusive", False)),
                            )
                        )
            elif template.get("buttons"):
                self.bot.add_view(RoleToggleView(template["buttons"], exclusive=False))
        except Exception:
            pass

        # Re-register persistent views for special buttons
        self.bot.add_view(BedrijvenBonusCheckView())
        self.bot.add_view(PillReminderView())

    @app_commands.command(
        name="generalroles", description="Post de rol-knoppen in het rollen-kanaal."
    )
    @has_privileged_role()
    async def generalroles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        testing = getattr(self.bot, "testing", False)
        path = general_roles_path(testing)
        template = load_roles_template(path)
        embeds = template.get("embeds", [])

        if not embeds:
            await interaction.followup.send(
                "❌ Geen embeds geconfigureerd.", ephemeral=True
            )
            return

        roles_ch_id = self.bot.config.get("channels", {}).get("roles")
        target_channel = (
            interaction.guild.get_channel(roles_ch_id) if roles_ch_id else None
        ) or interaction.channel

        try:
            await target_channel.purge(
                limit=50,
                check=lambda m: m.author == self.bot.user,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
        template_dirty = False

        for embed_data in embeds:
            buttons = embed_data.get("buttons", [])
            if not buttons:
                continue

            for btn in buttons:
                role_id = int(btn.get("role_id", 0))
                role = interaction.guild.get_role(role_id) if role_id else None
                if role is None:
                    role = discord.utils.get(interaction.guild.roles, name=btn["label"])
                    if role is None:
                        try:
                            role = await interaction.guild.create_role(
                                name=btn["label"],
                                mentionable=True,
                                reason="Automatisch aangemaakt door /generalroles",
                            )
                        except Exception as e:
                            self.bot.logger.error(
                                "Failed to create role %s: %s", btn["label"], e
                            )
                            continue
                    btn["role_id"] = role.id
                    template_dirty = True

            exclusive = bool(embed_data.get("exclusive", False))
            embed_color_raw = embed_data.get("color")
            if embed_color_raw:
                embed_color = int(embed_color_raw, 16) if isinstance(embed_color_raw, str) else int(embed_color_raw)
            else:
                embed_color = color
            embed = discord.Embed(
                title=embed_data.get("title", "Kies je rollen"),
                description=embed_data.get(
                    "description", "Klik op een knop om rollen te toggelen."
                ),
                color=embed_color,
            )
            await target_channel.send(
                embed=embed, view=RoleToggleView(buttons, exclusive=exclusive)
            )

        if template_dirty:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(template, f, indent=2, ensure_ascii=False)
            except Exception as e:
                self.bot.logger.error("Failed to save general roles template: %s", e)

        # ── Wakkerdam Toeschouwer channel permissions ─────────────────────────
        if not testing:
            _wakkerdam_role = discord.utils.get(
                interaction.guild.roles, name="Wakkerdam Toeschouwer"
            )
            if _wakkerdam_role:
                _wakkerdam_channels = {
                    1499384534976954408: discord.PermissionOverwrite(
                        read_messages=True, send_messages=False
                    ),
                    1499377427460263946: discord.PermissionOverwrite(
                        read_messages=True, send_messages=False
                    ),
                    1499377525808173168: discord.PermissionOverwrite(
                        read_messages=True, send_messages=True
                    ),
                }
                for ch_id, overwrite in _wakkerdam_channels.items():
                    ch = interaction.guild.get_channel(ch_id)
                    if ch is not None:
                        try:
                            await ch.set_permissions(_wakkerdam_role, overwrite=overwrite)
                        except Exception as e:
                            self.bot.logger.warning(
                                "Failed to set permissions for channel %d: %s", ch_id, e
                            )

        # Post the Company bonus check (bedrijven bonus check) button
        bw_embed = discord.Embed(
            title="🏭 Bedrijven notificaties",
            description=(
                "**📊 0% bonus check**\n"
                "Ontvang een DM zodra een van je bedrijven **0% productiebonus** heeft.\n\n"
                "**📍 Verhuisadvies**\n"
                "Ontvang een DM als je een bedrijf winstgevend naar een regio met hogere bonus kunt verplaatsen.\n\n"
                "Klik op de gewenste knop om je aan te melden. Klik opnieuw om je af te melden."
            ),
            colour=discord.Colour.blue(),
        )
        bw_msg = await target_channel.send(embed=bw_embed, view=BedrijvenBonusCheckView())
        state = _load_state(self.bot.testing)
        state["button_message_id"] = bw_msg.id
        _save_state(state, self.bot.testing)

        # Post the Pill buff reminder button
        pill_embed = discord.Embed(
            title="💊 Pill buff herinnering",
            description=(
                "Wil je een DM ontvangen als je **pill buff** bijna verloopt?\n\n"
                "Klik op de knop hieronder om je aan te melden. "
                "De bot stuurt je een DM "
                "op het moment dat er nog precies **10 minuten** over zijn.\n\n"
                "Klik nogmaals op de knop om je af te melden."
            ),
            colour=discord.Colour.green(),
        )
        pill_msg = await target_channel.send(embed=pill_embed, view=PillReminderView())
        pill_state = _pill_load_state(self.bot.testing)
        pill_state["button_message_id"] = pill_msg.id
        _pill_save_state(pill_state, self.bot.testing)

async def setup(bot) -> None:
    """Add the GeneralRoles cog to the bot."""
    await bot.add_cog(GeneralRoles(bot))
