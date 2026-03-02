"""MU role selection commands and MU role posting functionality."""

import json

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

from .roles import (
    RoleToggleView,
    load_roles_template,
    mu_roles_path,
    post_or_edit_buttons,
)


class MuRoles(commands.Cog, name="mu_roles"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.template = load_roles_template(mu_roles_path(getattr(bot, "testing", False)))

        if self.template.get("embeds"):
            for embed_data in self.template["embeds"]:
                if embed_data.get("buttons"):
                    self.bot.add_view(RoleToggleView(embed_data["buttons"], exclusive=True))
        if self.template.get("buttons"):
            self.bot.add_view(RoleToggleView(self.template["buttons"], exclusive=True))

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

        path = mu_roles_path(getattr(self.bot, "testing", False))
        data = load_roles_template(path)

        pinned_labels = {"Overige MU", "Wachtlijst"}

        existing_buttons = data.get("buttons", [])
        secondary_role_id = (
            existing_buttons[0].get("secondary_role_id") if existing_buttons else None
        )

        if any(int(b["role_id"]) == rol.id for b in existing_buttons):
            await interaction.followup.send(
                f"❌ De rol **{rol.name}** staat al in de MU-selector.", ephemeral=True
            )
            return

        normal_buttons = [
            b for b in existing_buttons if b.get("label") not in pinned_labels
        ]
        pinned_buttons = [
            b for b in existing_buttons if b.get("label") in pinned_labels
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

        testing = getattr(self.bot, "testing", False)
        mus_json_path = "templates/mus.testing.json" if testing else "templates/mus.json"
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

        try:
            await self._repost_mus(interaction, mus_json_path, data, path)
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
    ) -> None:
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        mus_cog = self.bot.cogs.get("mus")
        if mus_cog:
            mus_cog.load_json(mus_json_path)
            await mus_cog._repost_mu_list(target_channel)
        else:
            color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
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

        testing = getattr(self.bot, "testing", False)
        mus_json_path = "templates/mus.testing.json" if testing else "templates/mus.json"
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
            color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
            try:
                await post_or_edit_buttons(target_channel, data, path, color)
            except Exception as e:
                deleted_role_msg += f" ⚠️ Bewerken mislukt: {e}"

        await interaction.followup.send(
            f"✅ **{label}** verwijderd uit de MU-selector.{deleted_role_msg}",
            ephemeral=True,
        )


async def setup(bot) -> None:
    """Add the MuRoles cog to the bot."""
    await bot.add_cog(MuRoles(bot))
