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
        self.template = load_roles_template(
            mu_roles_path(getattr(bot, "testing", False))
        )
        # Register persistent views for templates so buttons keep working after restarts
        if self.template.get("embeds"):
            for embed_data in self.template["embeds"]:
                if embed_data.get("buttons"):
                    self.bot.add_view(
                        RoleToggleView(embed_data["buttons"], exclusive=True)
                    )
        if self.template.get("buttons"):
            self.bot.add_view(RoleToggleView(self.template["buttons"], exclusive=True))

        # Also load and register the general roles template
        # General roles are NOT exclusive — users can hold multiple simultaneously.
        try:
            general_template = load_roles_template(
                general_roles_path(getattr(bot, "testing", False))
            )
            if general_template.get("embeds"):
                for embed_data in general_template["embeds"]:
                    if embed_data.get("buttons"):
                        self.bot.add_view(
                            RoleToggleView(embed_data["buttons"], exclusive=False)
                        )
            elif general_template.get("buttons"):
                self.bot.add_view(
                    RoleToggleView(general_template["buttons"], exclusive=False)
                )
        except Exception:
            # Fail quietly; commands will still load and can post the view manually
            pass

    @app_commands.command(name="muroles", description="Post de MU-rolknoppen.")
    @has_privileged_role()
    async def muroles(self, interaction: discord.Interaction) -> None:
        path = mu_roles_path(getattr(self.bot, "testing", False))
        self.template = load_roles_template(path)
        buttons = self.template.get("buttons", [])

        if not buttons:
            await interaction.response.send_message(
                "Geen knoppen geconfigureerd in de MU-template.", ephemeral=True
            )
            return

        # Determine target channel: config military_unit → fallback to interaction channel
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        await interaction.response.send_message(
            f"✅ MU-rolknoppen gepost in {target_channel.mention}.", ephemeral=True
        )
        color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
        await post_or_edit_buttons(target_channel, self.template, path, color)

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

        # Resolve target channel from config
        roles_ch_id = self.bot.config.get("channels", {}).get("roles")
        target_channel = (
            interaction.guild.get_channel(roles_ch_id) if roles_ch_id else None
        ) or interaction.channel

        # Purge previous bot messages in the roles channel
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

            # Ensure every role exists in Discord; create it if missing or role_id is 0
            for btn in buttons:
                role_id = int(btn.get("role_id", 0))
                role = interaction.guild.get_role(role_id) if role_id else None
                if role is None:
                    # Try to find by name first, then create
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

        # Persist any newly-created role IDs back to the JSON
        if template_dirty:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(template, f, indent=2, ensure_ascii=False)
            except Exception as e:
                self.bot.logger.error("Failed to save general roles template: %s", e)

        await interaction.followup.send(
            f"✅ Rol-knoppen gepost in {target_channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="muwachtlijst",
        description="Tel het aantal mensen op de wachtlijst voor MU's.",
    )
    @has_privileged_role()
    async def muwachtlijst(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(
                "❌ Guild not found.", ephemeral=True
            )
            return

        wachtlijst_role_id = self.bot.config.get("roles", {}).get("wachtlijst")
        if not wachtlijst_role_id:
            await interaction.response.send_message(
                "❌ Wachtlijst role not configured.", ephemeral=True
            )
            return
        wachtlijst_role = guild.get_role(wachtlijst_role_id)
        if not wachtlijst_role:
            await interaction.response.send_message(
                "❌ Wachtlijst role not found.", ephemeral=True
            )
            return

        count = len(wachtlijst_role.members)
        await interaction.response.send_message(
            f"📋 Er zijn momenteel {count} mensen op de wachtlijst voor MU's."
        )

    async def _mu_label_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        path = mu_roles_path(getattr(self.bot, "testing", False))
        data = load_roles_template(path)
        labels = [b["label"] for b in data.get("buttons", [])]
        return [
            app_commands.Choice(name=lbl, value=lbl)
            for lbl in labels
            if current.lower() in lbl.lower()
        ][:25]

    @app_commands.command(
        name="voegmu",
        description="Voeg een nieuwe MU toe aan de MU-rolselector en de MU-lijst.",
    )
    @app_commands.describe(
        label="De naam van de MU",
        mu_type="Het type van de MU",
        link="Link naar de MU pagina op warera.io",
        thumbnail="URL van het MU logo",
        rol="Bestaande Discord-rol (laat leeg om er automatisch een aan te maken)",
        row="Rijnummer van de knop (0–4); wordt automatisch bepaald als je dit weglaat",
        style="Knopstijl: primary (blauw), secondary (grijs), success (groen), danger (rood)",
    )
    @app_commands.choices(
        mu_type=[
            app_commands.Choice(name="Elite", value="Elite"),
            app_commands.Choice(name="Eco", value="Eco"),
            app_commands.Choice(name="Standaard", value="Standaard"),
        ]
    )
    @has_privileged_role()
    async def voegmu(
        self,
        interaction: discord.Interaction,
        label: str,
        mu_type: str,
        link: str,
        thumbnail: str,
        rol: discord.Role | None = None,
        row: int | None = None,
        style: str = "primary",
    ) -> None:
        """Voeg een nieuwe MU-knop toe aan de juiste mu_roles JSON en post de bijgewerkte lijst."""
        await interaction.response.defer(ephemeral=True)

        # ── Resolve or create the role ────────────────────────────────────
        if rol is None:
            try:
                rol = await interaction.guild.create_role(
                    name=label,
                    color=discord.Color.orange(),
                    mentionable=False,
                    reason=f"Aangemaakt door /voegmu van {interaction.user}",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ Ik heb geen toestemming om rollen aan te maken (vereist: Rollen beheren).",
                    ephemeral=True,
                )
                return
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Rol aanmaken mislukt: {e}", ephemeral=True
                )
                return

        # ── Update mu_roles JSON ────────────────────────────────────────────
        path = mu_roles_path(getattr(self.bot, "testing", False))
        data = load_roles_template(path)

        _PINNED_LABELS = {"Overige MU", "Wachtlijst"}

        existing_buttons = data.get("buttons", [])
        secondary_role_id = (
            existing_buttons[0].get("secondary_role_id") if existing_buttons else None
        )

        if any(int(b["role_id"]) == rol.id for b in existing_buttons):
            await interaction.followup.send(
                f"❌ De rol **{rol.name}** staat al in de MU-selector.", ephemeral=True
            )
            return

        # Split pinned (Overige MU / Wachtlijst) from normal buttons so the
        # new MU is always inserted before them.
        normal_buttons = [
            b for b in existing_buttons if b.get("label") not in _PINNED_LABELS
        ]
        pinned_buttons = [
            b for b in existing_buttons if b.get("label") in _PINNED_LABELS
        ]

        if row is None:
            row = len(normal_buttons) // 5

        new_button: dict = {
            "label": label,
            "role_id": rol.id,
            "style": style
            if style in ("primary", "secondary", "success", "danger")
            else "primary",
            "row": max(0, min(4, row)),
        }
        if secondary_role_id is not None:
            new_button["secondary_role_id"] = secondary_role_id

        data["buttons"] = normal_buttons + [new_button] + pinned_buttons

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Opslaan mu_roles mislukt: {e}", ephemeral=True
            )
            return

        self.template = data

        # ── Update mus JSON ─────────────────────────────────────────────
        testing = getattr(self.bot, "testing", False)
        mus_json_path = (
            "templates/mus.testing.json" if testing else "templates/mus.json"
        )
        try:
            with open(mus_json_path, "r", encoding="utf-8") as f:
                mus_data = json.load(f)
        except FileNotFoundError:
            mus_data = {"embeds": []}

        mus_data.setdefault("embeds", []).append(
            {
                "title": label,
                "description": f"[**{mu_type} MU**]({link})",
                "thumbnail": thumbnail,
            }
        )

        try:
            with open(mus_json_path, "w", encoding="utf-8") as f:
                json.dump(mus_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Opslaan mus.json mislukt: {e}", ephemeral=True
            )
            return

        # ── Repost full MU list via the MUs cog ───────────────────────────
        try:
            await self._repost_mus(
                self,
                interaction,
            )
        except Exception as e:
            await interaction.followup.send(
                f"✅ MU **{label}** (rol: {rol.mention}) toegevoegd, maar herposten mislukt: {e}",
                ephemeral=True,
            )
            return

        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel
        await interaction.followup.send(
            f"✅ **{label}** (rol: {rol.mention}, rij {row}) toegevoegd en MU-lijst herplaatst in {target_channel.mention}.",
            ephemeral=True,
        )

    async def _repost_mus(
        self,
        interaction: discord.Interaction,
        mus_json_path: str,
        data: dict,
        path: str,
        label: str,
        rol: discord.Role,
    ) -> None:
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        mus_cog = self.bot.cogs.get("mus")
        if mus_cog:
            mus_cog.load_json(mus_json_path)  # reload with the new entry
            await mus_cog._repost_mu_list(target_channel)

        else:
            # Fallback: just update the buttons message
            color = int(
                self.bot.config.get("colors", {}).get("primary", "0x154273"), 16
            )
            await post_or_edit_buttons(target_channel, data, path, color)

    @app_commands.command(
        name="verwijdermu", description="Verwijder een MU uit de MU-rolselector."
    )
    @app_commands.describe(
        label="De naam van de MU om te verwijderen",
        verwijder_rol="Verwijder ook de bijbehorende Discord-rol (standaard: ja)",
    )
    @app_commands.autocomplete(label=_mu_label_autocomplete)
    @has_privileged_role()
    async def verwijdermu(
        self,
        interaction: discord.Interaction,
        label: str,
        verwijder_rol: bool = True,
    ) -> None:
        """Verwijder een MU-knop uit de JSON en post de bijgewerkte lijst."""
        await interaction.response.defer(ephemeral=True)

        path = mu_roles_path(getattr(self.bot, "testing", False))
        data = load_roles_template(path)
        buttons = data.get("buttons", [])

        target = next((b for b in buttons if b["label"] == label), None)
        if target is None:
            await interaction.followup.send(
                f"❌ Geen MU gevonden met naam **{label}**.", ephemeral=True
            )
            return

        data["buttons"] = [b for b in buttons if b["label"] != label]

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            await interaction.followup.send(f"❌ Opslaan mislukt: {e}", ephemeral=True)
            return

        self.template = data

        # Optionally delete the Discord role
        deleted_role_msg = ""
        if verwijder_rol:
            role = interaction.guild.get_role(int(target["role_id"]))
            if role:
                try:
                    await role.delete(
                        reason=f"Verwijderd door /verwijdermu van {interaction.user}"
                    )
                    deleted_role_msg = f" Discord-rol **{role.name}** verwijderd."
                except discord.Forbidden:
                    deleted_role_msg = (
                        " ⚠️ Kon de Discord-rol niet verwijderen (onvoldoende rechten)."
                    )
                except Exception as e:
                    deleted_role_msg = f" ⚠️ Rol verwijderen mislukt: {e}"
            else:
                deleted_role_msg = " ⚠️ Discord-rol niet gevonden in deze server."

        # ── Also remove the embed from mus.json ──────────────────────────
        testing = getattr(self.bot, "testing", False)
        mus_json_path = (
            "templates/mus.testing.json" if testing else "templates/mus.json"
        )
        try:
            with open(mus_json_path, "r", encoding="utf-8") as f:
                mus_data = json.load(f)
            mus_data["embeds"] = [
                e for e in mus_data.get("embeds", []) if e.get("title") != label
            ]
            with open(mus_json_path, "w", encoding="utf-8") as f:
                json.dump(mus_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            deleted_role_msg += f" ⚠️ Bijwerken mus.json mislukt: {e}"
            mus_data = None

        # ── Repost full MU list via the MUs cog ───────────────────────────
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        mus_cog = self.bot.cogs.get("mus")
        if mus_cog:
            mus_cog.load_json(mus_json_path)
            try:
                await mus_cog._repost_mu_list(target_channel)
            except Exception as e:
                deleted_role_msg += f" ⚠️ Herposten mislukt: {e}"
        else:
            color = int(
                self.bot.config.get("colors", {}).get("primary", "0x154273"), 16
            )
            try:
                await post_or_edit_buttons(target_channel, data, path, color)
            except Exception as e:
                deleted_role_msg += f" ⚠️ Bewerken mislukt: {e}"

        await interaction.followup.send(
            f"✅ **{label}** verwijderd uit de MU-selector.{deleted_role_msg}",
            ephemeral=True,
        )

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
