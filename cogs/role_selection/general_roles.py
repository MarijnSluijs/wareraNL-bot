"""General role posting command for the role selection channel."""

import json

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

from .roles import RoleToggleView, general_roles_path, load_roles_template


class GeneralRoles(commands.Cog, name="general_role_selection"):
    def __init__(self, bot) -> None:
        self.bot = bot

        try:
            template = load_roles_template(general_roles_path(getattr(bot, "testing", False)))
            if template.get("embeds"):
                for embed_data in template["embeds"]:
                    if embed_data.get("buttons"):
                        self.bot.add_view(RoleToggleView(embed_data["buttons"], exclusive=False))
            elif template.get("buttons"):
                self.bot.add_view(RoleToggleView(template["buttons"], exclusive=False))
        except Exception:
            pass

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
                limit=50, check=lambda m: m.author == self.bot.user
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

            embed = discord.Embed(
                title=embed_data.get("title", "Kies je rollen"),
                description=embed_data.get(
                    "description", "Klik op een knop om rollen te toggelen."
                ),
                color=color,
            )
            await target_channel.send(
                embed=embed, view=RoleToggleView(buttons, exclusive=False)
            )

        if template_dirty:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(template, f, indent=2, ensure_ascii=False)
            except Exception as e:
                self.bot.logger.error("Failed to save general roles template: %s", e)

        await interaction.followup.send(
            f"✅ Rol-knoppen gepost in {target_channel.mention}.", ephemeral=True
        )


async def setup(bot) -> None:
    """Add the GeneralRoles cog to the bot."""
    await bot.add_cog(GeneralRoles(bot))
