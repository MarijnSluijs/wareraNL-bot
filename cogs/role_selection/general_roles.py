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

from .roles import RoleToggleView, games_roles_path, general_roles_path, load_roles_template


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

        try:
            games_tpl = load_roles_template(
                games_roles_path(getattr(bot, "testing", False))
            )
            for embed_data in games_tpl.get("embeds", []):
                if embed_data.get("buttons"):
                    self.bot.add_view(
                        RoleToggleView(embed_data["buttons"], exclusive=False)
                    )
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
                "**10 minuten**: De bot stuurt je een DM wanneer er nog **10 minuten** over zijn.\n"
                "**30 minuten**: De bot stuurt je een DM wanneer er nog **30 minuten** over zijn.\n\n"
                "Klik op een knop om je aan te melden. Klik nogmaals om je af te melden."
            ),
            colour=discord.Colour.green(),
        )
        pill_msg = await target_channel.send(embed=pill_embed, view=PillReminderView())
        pill_state = _pill_load_state(self.bot.testing)
        pill_state["button_message_id"] = pill_msg.id
        _pill_save_state(pill_state, self.bot.testing)

        await interaction.followup.send("✅ Rollen-knoppen gepost.", ephemeral=True)

    @app_commands.command(
        name="syncgamesroles",
        description="Synchroniseer game-rolknoppen met de kanalen in de games-categorie.",
    )
    @has_privileged_role()
    async def syncgamesroles(self, interaction: discord.Interaction) -> None:
        """Scan the games category, create missing roles, drop stale buttons, repost."""
        _GAMES_CHANNEL_ID = 1520543859820724254
        _GAMES_CATEGORY_ID = 1517179213554385047

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("❌ Guild niet gevonden.", ephemeral=True)
            return

        category = guild.get_channel(_GAMES_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                "❌ Games-categorie niet gevonden.", ephemeral=True
            )
            return

        channels = sorted(
            [ch for ch in category.channels if isinstance(ch, discord.TextChannel)],
            key=lambda c: c.position,
        )
        channel_names = {ch.name for ch in channels}

        testing = getattr(self.bot, "testing", False)
        path = games_roles_path(testing)
        template = load_roles_template(path)

        embeds = template.get("embeds") or []
        if not embeds:
            embeds = [{
                "title": "🎮 Game rollen",
                "color": "0x9b59b6",
                "description": (
                    "Klik op een knop om de rol voor dat spel te krijgen of te verwijderen.\n"
                    "Met deze rollen kun je anderen pingen om samen te spelen."
                ),
                "buttons": [],
            }]

        existing_buttons: list[dict] = embeds[0].get("buttons", [])
        existing_by_label: dict[str, dict] = {btn["label"]: btn for btn in existing_buttons}

        new_buttons: list[dict] = []
        created_roles: list[str] = []
        errors: list[str] = []

        for ch in channels:
            label = ch.name
            if label in existing_by_label:
                new_buttons.append(existing_by_label[label])
            else:
                role = discord.utils.get(guild.roles, name=label)
                if role is None:
                    try:
                        role = await guild.create_role(
                            name=label,
                            mentionable=True,
                            reason=f"Aangemaakt door /syncgamesroles voor #{label}",
                        )
                        created_roles.append(label)
                    except Exception as exc:
                        errors.append(f"{label}: {exc}")
                        continue
                new_buttons.append({
                    "label": label,
                    "role_id": role.id,
                    "style": "primary",
                })

        removed = [lbl for lbl in existing_by_label if lbl not in channel_names]

        for idx, btn in enumerate(new_buttons):
            btn["row"] = idx // 5

        embeds[0]["buttons"] = new_buttons
        template["embeds"] = embeds

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(template, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Opslaan games_roles.json mislukt: {exc}", ephemeral=True
            )
            return

        self.bot.add_view(RoleToggleView(new_buttons, exclusive=False))

        target_channel = self.bot.get_channel(_GAMES_CHANNEL_ID)
        if target_channel is None:
            await interaction.followup.send(
                "❌ Games-kanaal niet gevonden.", ephemeral=True
            )
            return

        try:
            await target_channel.purge(
                limit=50, check=lambda m: m.author == self.bot.user
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
        embed_data = embeds[0]
        embed_color_raw = embed_data.get("color")
        if embed_color_raw:
            embed_color = (
                int(embed_color_raw, 16)
                if isinstance(embed_color_raw, str)
                else int(embed_color_raw)
            )
        else:
            embed_color = color

        embed = discord.Embed(
            title=embed_data.get("title", "Game rollen"),
            description=embed_data.get(
                "description", "Klik op een knop om een rol te toggelen."
            ),
            color=embed_color,
        )
        await target_channel.send(
            embed=embed, view=RoleToggleView(new_buttons, exclusive=False)
        )

        lines = [f"✅ Game-rolknoppen gesynchroniseerd in <#{_GAMES_CHANNEL_ID}>."]
        lines.append(f"**{len(new_buttons)}** knoppen actief.")
        if created_roles:
            lines.append(f"**{len(created_roles)}** nieuwe rol(len) aangemaakt: {', '.join(created_roles)}.")
        if removed:
            lines.append(f"**{len(removed)}** knop(pen) verwijderd: {', '.join(removed)}.")
        if errors:
            lines.append(f"⚠️ Fouten bij aanmaken rollen: {'; '.join(errors)}.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="gamesroles",
        description="Post de game-rolknoppen in het games-kanaal.",
    )
    @has_privileged_role()
    async def gamesroles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        testing = getattr(self.bot, "testing", False)
        path = games_roles_path(testing)
        template = load_roles_template(path)
        embeds = template.get("embeds", [])

        if not embeds:
            await interaction.followup.send(
                "❌ Geen embeds geconfigureerd in games_roles.json.", ephemeral=True
            )
            return

        _GAMES_CHANNEL_ID = 1520543859820724254
        target_channel = self.bot.get_channel(_GAMES_CHANNEL_ID)
        if target_channel is None:
            await interaction.followup.send(
                "❌ Games-kanaal niet gevonden.", ephemeral=True
            )
            return

        try:
            await target_channel.purge(
                limit=50,
                check=lambda m: m.author == self.bot.user,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)

        for embed_data in embeds:
            buttons = embed_data.get("buttons", [])
            if not buttons:
                continue
            embed_color_raw = embed_data.get("color")
            if embed_color_raw:
                embed_color = int(embed_color_raw, 16) if isinstance(embed_color_raw, str) else int(embed_color_raw)
            else:
                embed_color = color
            embed = discord.Embed(
                title=embed_data.get("title", "Game rollen"),
                description=embed_data.get("description", "Klik op een knop om een rol te toggelen."),
                color=embed_color,
            )
            await target_channel.send(
                embed=embed, view=RoleToggleView(buttons, exclusive=False)
            )

        await interaction.followup.send(
            f"✅ Game-rolknoppen gepost in <#{_GAMES_CHANNEL_ID}>.", ephemeral=True
        )


async def setup(bot) -> None:
    """Add the GeneralRoles cog to the bot."""
    await bot.add_cog(GeneralRoles(bot))
