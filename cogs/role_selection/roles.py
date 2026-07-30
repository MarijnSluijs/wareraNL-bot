"""
This module defines the Roles cog, which provides commands to manage self-assignable roles in a Discord server.
"""

import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

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


def games_roles_path(testing: bool = False) -> str:
    """Return the correct games roles JSON path for the current mode."""
    if testing:
        return f"{TEMPLATES_PATH}/games_roles.testing.json"
    return f"{TEMPLATES_PATH}/games_roles.json"


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
    pages = data.get("embeds") or []
    if pages:
        existing_ids = data.get("button_message_ids") or []
        if not existing_ids and data.get("button_message_id"):
            existing_ids = [data.get("button_message_id")]

        sent_ids: list[int] = []
        total_pages = len(pages)

        for idx, page in enumerate(pages, start=1):
            buttons = page.get("buttons", [])
            title = data.get("title", "MU Lidmaatschap")
            description = data.get("description", "")
            if idx > 1:
                title = f"{title} ({idx}/{total_pages})"
                description = "Vervolg van de MU-knoppen."

            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
            )
            view = (
                RoleToggleView(buttons, exclusive=True) if buttons else discord.ui.View()
            )

            msg = None
            if idx - 1 < len(existing_ids):
                try:
                    msg = await channel.fetch_message(existing_ids[idx - 1])
                    await msg.edit(embed=embed, view=view)
                except (discord.NotFound, discord.HTTPException):
                    msg = None

            if msg is None:
                msg = await channel.send(embed=embed, view=view)

            sent_ids.append(msg.id)

        data["button_message_id"] = sent_ids[0] if sent_ids else None
        data["button_message_ids"] = sent_ids
    else:
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
                msg = None  # Gone - fall through to send

        if msg is None:
            msg = await channel.send(embed=embed, view=view)

        data["button_message_id"] = msg.id
        data["button_message_ids"] = [msg.id]

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

        # Entry log BEFORE the defer — if this line is missing from the logs
        # for a click the user reports, the interaction never reached this
        # callback at all (e.g. no persistent view was registered for this
        # custom_id after a restart), which is a completely different bug
        # class than anything raised below.
        logger.info(
            "role_toggle: click user=%s(%s) role_id=%s guild=%s",
            getattr(member, "display_name", member), getattr(member, "id", "?"),
            self.role_id, getattr(guild, "id", "?"),
        )

        # Ack within Discord's 3 s interaction deadline BEFORE doing any role
        # edits.  add_roles/remove_roles are HTTP calls that can block on a rate
        # limit bucket, and a busy event loop can delay them well past 3 s —
        # which surfaces to the user as "… didn't respond in time".
        await interaction.response.defer(ephemeral=True)

        if not guild:
            logger.warning("role_toggle: no guild on interaction for role_id=%s", self.role_id)
            await interaction.followup.send("❌ Guild not found.", ephemeral=True)
            return

        role = guild.get_role(self.role_id)
        secondary_role = (
            guild.get_role(self.secondary_role_id) if self.secondary_role_id else None
        )

        if not role:
            logger.warning(
                "role_toggle: role_id=%s not found in guild=%s (deleted role? stale template?)",
                self.role_id, guild.id,
            )
            await interaction.followup.send("❌ Role not found.", ephemeral=True)
            return

        try:
            # Collect role IDs for all buttons in this view (avoids stale guild cache).
            # member.roles is always fresh from the interaction payload.
            primary_role_ids: set[int] = {
                child.role_id
                for child in getattr(self.view, "children", [])
                if isinstance(child, RoleToggleButton)
            }

            # Which of the member's current roles belong to this exclusive group
            member_primary_roles = [r for r in member.roles if r.id in primary_role_ids]

            # If user clicked a primary they already have -> remove that primary only
            if role in member.roles:
                await member.remove_roles(role, reason="Self-assign role toggle")
                logger.info(
                    "role_toggle: removed role=%s(%s) from user=%s",
                    role.name, role.id, member.id,
                )
                await interaction.followup.send(
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
                logger.info(
                    "role_toggle: added role(s)=%s to user=%s", names, member.id,
                )
                await interaction.followup.send(
                    f"✅ Added role(s): {names}", ephemeral=True
                )
            else:
                await interaction.followup.send("✅ No roles to add.", ephemeral=True)

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to manage that role.", ephemeral=True
            )
        except Exception:
            logger.exception("role toggle failed for role_id=%s", self.role_id)
            await interaction.followup.send(
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
                    secondary_role_id=(
                        int(btn["secondary_role_id"])
                        if btn.get("secondary_role_id")
                        else None
                    ),
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
    @has_privileged_role()
    async def ambassadeurs(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
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
            if ambassadeur_role in user.roles:
                await user.remove_roles(
                    ambassadeur_role,
                    reason="Verwijderd door Minister van Buitenlandse Zaken",
                )
                await interaction.response.send_message(
                    f"✅ De ambassadeurrol is verwijderd van {user.mention}."
                )
            else:
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

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, (app_commands.MissingPermissions, app_commands.CheckFailure)):
            await interaction.response.send_message(
                "❌ Je hebt geen rechten om dit commando te gebruiken.", ephemeral=True
            )
        else:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Er is een onverwachte fout opgetreden.", ephemeral=True
                )


async def setup(bot) -> None:
    """Add the Roles cog to the bot."""
    await bot.add_cog(Roles(bot))
