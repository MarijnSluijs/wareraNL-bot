"""
This module defines the Roles cog, which provides commands to manage self-assignable roles in a Discord server. 
"""

import json
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

TEMPLATES_PATH = "templates"


def mu_roles_path(testing: bool = False) -> str:
    """Return the correct mu_roles JSON path for the current mode."""
    if testing:
        return f"{TEMPLATES_PATH}/mu_roles.testing.json"
    return f"{TEMPLATES_PATH}/mu_roles.json"


def general_roles_path(testing: bool = False) -> str:
    """Return the correct general roles JSON path for the current mode."""
    if testing:
        return f"{TEMPLATES_PATH}/roles.testing.json"
    return f"{TEMPLATES_PATH}/roles.json"


def load_roles_template(path: str = f"{TEMPLATES_PATH}/mu_roles.json") -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "title": "Choose your roles",
        "description": "Click a button to toggle roles.",
        "buttons": [],
    }


async def post_or_edit_buttons(
    channel: discord.TextChannel,
    data: dict,
    path: str,
    color: int,
) -> None:
    """Edit the existing button message if its ID is tracked in *data*, otherwise send a new one.
    Always saves the (new) button_message_id back to *path*.
    """
    buttons = data.get("buttons", [])
    embed = discord.Embed(
        title=data.get("title", "MU Lidmaatschap"),
        description=data.get("description", ""),
        color=color,
    )
    view = RoleToggleView(buttons, exclusive=True) if buttons else discord.ui.View()

    msg_id = data.get("button_message_id")
    msg = None
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        except (discord.NotFound, discord.HTTPException):
            msg = None  # Gone — fall through to send

    if msg is None:
        msg = await channel.send(embed=embed, view=view)

    data["button_message_id"] = msg.id
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def button_style(style_name: str) -> discord.ButtonStyle:
    styles = {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }
    return styles.get(style_name, discord.ButtonStyle.secondary)


class RoleToggleButton(discord.ui.Button):
    def __init__(
        self,
        label: str,
        role_id: int,
        style: discord.ButtonStyle,
        emoji: str | None = None,
        row: int | None = None,
        secondary_role_id: int | None = None,
    ):
        super().__init__(
            label=label,
            style=style,
            emoji=emoji,
            row=row,
            custom_id=f"role_toggle:{role_id}",
        )
        self.role_id = role_id
        self.secondary_role_id = secondary_role_id

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user

        if not guild:
            await interaction.response.send_message(
                "❌ Guild not found.", ephemeral=True
            )
            return

        role = guild.get_role(self.role_id)
        secondary_role = (
            guild.get_role(self.secondary_role_id) if self.secondary_role_id else None
        )

        if not role:
            await interaction.response.send_message(
                "❌ Role not found.", ephemeral=True
            )
            return

        try:
            # Collect primary roles defined on this view
            primary_roles: list[discord.Role] = []
            for child in getattr(self.view, "children", []):
                if isinstance(child, RoleToggleButton):
                    r = guild.get_role(child.role_id)
                    if r:
                        primary_roles.append(r)

            # Which primary roles the member currently has
            member_primary_roles = [r for r in primary_roles if r in member.roles]

            # If user clicked a primary they already have -> remove that primary only
            if role in member.roles:
                await member.remove_roles(role, reason="Self-assign role toggle")
                await interaction.response.send_message(
                    f"✅ Removed role: {role.name}", ephemeral=True
                )
                return

            # We're adding a primary role
            # If exclusive, remove any other primary roles the member has
            if getattr(self.view, "exclusive", False):
                roles_to_remove = [r for r in member_primary_roles if r != role]
                if roles_to_remove:
                    await member.remove_roles(
                        *roles_to_remove, reason="Self-assign role exclusive toggle"
                    )

            # Build list of roles to add: always add the selected primary; add secondary only if user doesn't have it
            roles_to_add = [role]
            if secondary_role and secondary_role not in member.roles:
                roles_to_add.append(secondary_role)

            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Self-assign role toggle")
                names = ", ".join(r.name for r in roles_to_add)
                await interaction.response.send_message(
                    f"✅ Added role(s): {names}", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "✅ No roles to add.", ephemeral=True
                )

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage that role.", ephemeral=True
            )
        except Exception:
            await interaction.response.send_message(
                "❌ An error occurred while toggling the role.", ephemeral=True
            )


class RoleToggleView(discord.ui.View):
    def __init__(self, buttons_config: list[dict], exclusive: bool = False):
        super().__init__(timeout=None)
        self.exclusive = exclusive
        for btn in buttons_config:
            self.add_item(
                RoleToggleButton(
                    label=btn["label"],
                    role_id=int(btn["role_id"]),
                    style=button_style(btn.get("style", "secondary")),
                    emoji=btn.get("emoji"),
                    row=btn.get("row"),
                    secondary_role_id=int(btn["secondary_role_id"])
                    if btn.get("secondary_role_id")
                    else None,
                )
            )


class Roles(commands.Cog, name="roles"):
    def __init__(self, bot) -> None:
        self.bot = bot

    

    @app_commands.command(
        name="verwijderrol",
        description="Verwijder een Discord-rol van de server op naam.",
    )
    @app_commands.describe(rol="De rol om te verwijderen")
    @has_privileged_role()
    async def verwijderrol(
        self, interaction: discord.Interaction, rol: discord.Role
    ) -> None:
        """Verwijder een Discord-rol van de server."""
        try:
            naam = rol.name
            await rol.delete(
                reason=f"Verwijderd door /verwijderrol van {interaction.user}"
            )
            await interaction.response.send_message(
                f"✅ Rol **{naam}** succesvol verwijderd.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Ik heb geen toestemming om deze rol te verwijderen.", ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Verwijderen mislukt: {e}", ephemeral=True
            )

    @app_commands.command(name="ambassadeurs", description="Geef de ambassadeur rol.")
    @app_commands.describe(
        user="De gebruiker aan wie je de ambassadeur rol wilt geven."
    )
    async def ambassadeurs(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        # check if command is used by minister van buitenlandse zaken
        if not any(
            role.id == self.bot.config["roles"]["minister_foreign_affairs"]
            for role in interaction.user.roles
        ):
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Guild not found.", ephemeral=True
            )
            return

        ambassadeur_role = guild.get_role(
            self.bot.config["roles"]["ambassadeur"]
        )  # Ambassadeur role ID
        if not ambassadeur_role:
            await interaction.response.send_message(
                "❌ Ambassadeur role not found.", ephemeral=True
            )
            return

        try:
            await user.add_roles(
                ambassadeur_role,
                reason="Toegewezen door Minister van Buitenlandse Zaken",
            )
            await interaction.response.send_message(
                f"✅ {user.mention} is nu een Ambassadeur!"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage that role.", ephemeral=True
            )
        except Exception:
            await interaction.response.send_message(
                "❌ An error occurred while assigning the role.", ephemeral=True
            )


async def setup(bot) -> None:
    """Add the Roles cog to the bot."""
    await bot.add_cog(Roles(bot))
