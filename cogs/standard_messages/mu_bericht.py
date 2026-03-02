"""
This module defines the MUs cog, which provides commands to post and manage a list of Military Units (MU's) in a Discord channel. 
"""

import json
import re

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.role_selection.roles import (RoleToggleView, load_roles_template,
                                       mu_roles_path)
from cogs.standard_messages.generate import GenerateEmbeds
from utils.checks import has_privileged_role


def mus_path(testing: bool = False) -> str:
    """Return the correct mus JSON path for the current mode."""
    return "templates/mus.testing.json" if testing else "templates/mus.json"


class MUs(GenerateEmbeds, name="mus"):
    """Cog for managing and posting the Military Units (MU's) list in a designated Discord channel."""
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.load_json(mus_path(getattr(bot, "testing", False)))

    @commands.hybrid_command(
        name="mulijst",
        description="Post de MU lijst in het MU-kanaal.",
    )
    @has_privileged_role()
    async def mulijst(self, context: Context) -> None:
        """
        Post de MU lijst als een reeks embeds.

        :param context: The hybrid command context.
        """

        if not self.json_data or not self.json_data.get("embeds"):
            embed = discord.Embed(
                description="MU data niet gevonden. Gebruik `/reloadmus` om opnieuw te laden.",
                color=self.get_color("error"),
            )
            await context.send(embed=embed, ephemeral=True)
            return

        # Send confirmation
        await context.send("📚 Bezig met posten van de MU lijst...", ephemeral=True)

        channel = await self._mu_channel(context.channel)
        await self._repost_mu_list(channel)

        self.bot.logger.info(f"MU lijst posted by {context.author} in {channel.name}")

    @commands.hybrid_command(
        name="reloadmus",
        description="Herlaad de MU JSON file.",
    )
    @commands.is_owner()
    async def reloadmus(self, context: Context) -> None:
        """
        Reload the MU from the JSON file.
        :param context: The hybrid command context.
        """
        try:
            self.load_json(mus_path(getattr(self.bot, "testing", False)))
            print(self.json_data)
            embed = discord.Embed(
                description=f"✅ MU succesvol herladen! ({len(self.json_data.get('embeds', []))} embeds)",
                color=self.get_color("success"),
            )
            await context.send(embed=embed)
            self.bot.logger.info(f"MU reloaded by {context.author}")
        except Exception as e:
            embed = discord.Embed(
                description=f"❌ Fout bij herladen: {e}", color=self.get_color("error")
            )
            await context.send(embed=embed)

    async def _mu_channel(self, fallback: discord.TextChannel) -> discord.TextChannel:
        """Return the configured military_unit channel, or *fallback* if not found."""
        ch_id = self.bot.config.get("channels", {}).get("military_unit")
        if ch_id:
            ch = self.bot.get_channel(ch_id)
            if ch:
                return ch
        return fallback

    async def _repost_mu_list(self, channel: discord.TextChannel) -> None:
        """Delete previously tracked messages, post fresh ones, save new IDs to JSON."""
        path = mus_path(getattr(self.bot, "testing", False))

        # Bulk-delete recent bot messages (≤14 days) — covers the common case
        try:
            await channel.purge(limit=100, check=lambda m: m.author == self.bot.user)
        except (discord.Forbidden, discord.HTTPException):
            pass

        # Also individually delete any tracked IDs that may be older than 14 days
        old_ids: list[int] = self.json_data.get("posted_message_ids", [])
        for msg_id in old_ids:
            try:
                old_msg = await channel.fetch_message(msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # Already gone or no permission — continue

        # Post explanation embed
        explanation = discord.Embed(
            title="MU Soorten",
            description=(
                "- **Elite MU**: Deze MU's zullen als eerste ingezet worden tijdens gevechten. "
                "Dit betekent dat ze geacht worden een voorraad aan equipment, munitie, eten, pillen en geld beschikbaar te houden. "
                "Daarnaast wordt actieve deelname aan oorlogen verwacht.\n"
                "- **Eco MU**: Leden van deze MU's zullen tijdens oorlogen in eco stand blijven om de staatskas aan te vullen. "
                "Hiervan wordt verwacht dat leden actief doneren aan de staatskas tijdens oorlogen om bounties te kunnen betalen.\n"
                "- **Standaard MU**: Van overige MU's wordt niet veel gevraagd, behalve dat ze meevechten tijdens oorlogen. "
                "In de aanloop naar oorlogen kunnen leden aanwijzingen volgen van de regering, "
                "maar er wordt niet verwacht altijd een voorraad beschikbaar te hebben."
            ),
            color=discord.Color.gold(),
        )
        new_ids: list[int] = []
        msg = await channel.send(embed=explanation)
        new_ids.append(msg.id)

        _pinned_labels = {"Overige MU", "Wachtlijst"}
        _type_order = {"Elite": 0, "Eco": 1, "Standaard": 2}

        def _mu_type(description: str) -> int:
            """Return sort key from embed description like '[**Elite MU**](...)'."""
            m = re.search(r"\*\*(Elite|Eco|Standaard) MU\*\*", description or "")
            return _type_order.get(m.group(1), 9999) if m else 9999

        # Sort embeds: Elite → Eco → Standaard
        embeds_sorted = sorted(
            self.json_data.get("embeds", []),
            key=lambda e: _mu_type(e.get("description", "")),
        )

        poller = self.bot.cogs.get("event_tasks")
        if poller:
            mu_ids = []
            # Get new thumbnail URLs for each MU type from the first embed of that type, if available
            for embed in embeds_sorted:
                mu_id = (
                    embed.get("description", "").split("/")[-1].strip(")")
                )  # Extract MU ID from description URL
                mu_ids.append(mu_id)
            thumbnails = await poller._get_mu_thumbnails(mu_ids)
            for embed in embeds_sorted:
                mu_id = (
                    embed.get("description", "").split("/")[-1].strip(")")
                )  # Extract MU ID from description URL
                if mu_id in thumbnails:
                    embed["thumbnail"] = thumbnails[mu_id]

            # Update json with new thumbnail URLs so they persist for future reloads
            try:
                self.json_data["embeds"] = embeds_sorted
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.json_data, f, indent=4, ensure_ascii=False)
            except Exception as e:
                self.bot.logger.error(f"Failed to update MU thumbnails in JSON: {e}")
        else:
            self.bot.logger.warning(
                "Poller cog not found, skipping MU thumbnail update"
            )

        # Use exact embed position as button sort key so button order = embed order
        embed_position: dict[str, int] = {
            e["title"]: i for i, e in enumerate(embeds_sorted)
        }

        # Colors per MU type
        _type_colors: dict[str, discord.Color] = {
            "Elite": discord.Color.orange(),  # #E67E22
            "Eco": discord.Color.from_rgb(46, 204, 113),  # emerald green
            "Standaard": discord.Color.from_rgb(52, 152, 219),  # steel blue
        }

        def _mu_type_str(description: str) -> str | None:
            """Return the type string (Elite/Eco/Standaard) from an embed description."""
            m = re.search(r"\*\*(Elite|Eco|Standaard) MU\*\*", description or "")
            return m.group(1) if m else None

        # One embed per MU (preserves thumbnail), coloured by type, sorted Elite → Eco → Standaard
        for embed_data in embeds_sorted:
            mu_type_str = _mu_type_str(embed_data.get("description", ""))
            color = _type_colors.get(mu_type_str, discord.Color.greyple())
            embed = discord.Embed(
                title=embed_data.get("title", ""),
                description=embed_data.get("description", ""),
                color=color,
            )
            if "thumbnail" in embed_data:
                embed.set_thumbnail(url=embed_data["thumbnail"])
            try:
                msg = await channel.send(embed=embed)
                new_ids.append(msg.id)
            except Exception as e:
                self.bot.logger.error(
                    f"Error sending embed for {embed_data.get('title')}: {e}"
                )

        # Post role-selection embed with buttons — order mirrors embeds exactly,
        # pinned buttons (Overige MU / Wachtlijst) stay last, row numbers recalculated
        try:
            roles_path = mu_roles_path(getattr(self.bot, "testing", False))
            roles_data = load_roles_template(roles_path)
            all_buttons = roles_data.get("buttons", [])

            # Ensure pinned roles exist in the JSON and in Discord
            _pinned_role_defs = [
                {"label": "Overige MU", "style": "secondary"},
                {"label": "Wachtlijst", "style": "secondary"},
            ]
            _secondary_role_id = next(
                (
                    b.get("secondary_role_id")
                    for b in all_buttons
                    if b.get("secondary_role_id")
                ),
                None,
            )
            for pdef in _pinned_role_defs:
                existing_btn = next(
                    (b for b in all_buttons if b.get("label") == pdef["label"]), None
                )
                discord_role = None
                if existing_btn:
                    # Button entry exists — check if the Discord role still exists
                    discord_role = channel.guild.get_role(int(existing_btn["role_id"]))
                if discord_role is None:
                    # Role missing from Discord — look up by name or create it
                    discord_role = discord.utils.get(
                        channel.guild.roles, name=pdef["label"]
                    )
                    if discord_role is None:
                        try:
                            discord_role = await channel.guild.create_role(
                                name=pdef["label"],
                                color=discord.Color.orange(),
                                mentionable=True,
                                reason="Automatisch aangemaakt door bot (vaste MU-knop)",
                            )
                        except Exception as e:
                            self.bot.logger.error(
                                "Failed to create pinned role %s: %s", pdef["label"], e
                            )
                            continue
                    # Update or add the button entry with the (new) role ID
                    if existing_btn:
                        existing_btn["role_id"] = discord_role.id
                    else:
                        entry = {
                            "label": pdef["label"],
                            "role_id": discord_role.id,
                            "style": pdef["style"],
                            "row": 0,
                        }
                        if _secondary_role_id:
                            entry["secondary_role_id"] = _secondary_role_id
                        all_buttons.append(entry)
            roles_data["buttons"] = all_buttons

            if all_buttons:
                normal_btns = [
                    b for b in all_buttons if b.get("label") not in _pinned_labels
                ]
                pinned_btns = [
                    b for b in all_buttons if b.get("label") in _pinned_labels
                ]

                normal_btns.sort(
                    key=lambda b: embed_position.get(b.get("label", ""), 9999)
                )

                # Recalculate row numbers after sort (5 per row).
                # Use ceiling division so pinned buttons always start on a fresh row,
                # even when the normal button count is not a multiple of 5.
                for i, b in enumerate(normal_btns):
                    b["row"] = i // 5
                pinned_row = (len(normal_btns) + 4) // 5
                for b in pinned_btns:
                    b["row"] = pinned_row

                buttons = normal_btns + pinned_btns

                # Save sorted buttons back to JSON so future reads are consistent
                roles_data["buttons"] = buttons
                with open(roles_path, "w", encoding="utf-8") as f:
                    json.dump(roles_data, f, indent=2, ensure_ascii=False)

                color = int(
                    self.bot.config.get("colors", {}).get("primary", "0x154273"), 16
                )
                roles_embed = discord.Embed(
                    title=roles_data.get("title", "MU Lidmaatschap"),
                    description=roles_data.get("description", ""),
                    color=color,
                )
                btn_msg = await channel.send(
                    embed=roles_embed, view=RoleToggleView(buttons, exclusive=True)
                )
                roles_data["button_message_id"] = btn_msg.id
                with open(roles_path, "w", encoding="utf-8") as f:
                    json.dump(roles_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.bot.logger.error(f"Error sending role buttons: {e}")

        # Persist the new message IDs
        self.json_data["posted_message_ids"] = new_ids
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.json_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            self.bot.logger.error(f"Failed to save posted_message_ids: {e}")

    @app_commands.command(
        name="repostmu",
        description="Voeg een nieuwe MU toe aan mus.json en herplaats de MU-lijst.",
    )
    async def repostmu(self, interaction: discord.Interaction) -> None:
        """Repost the MU list without changes, to refresh the channel."""
        await interaction.response.defer(ephemeral=True)
        channel = await self._mu_channel(interaction.channel)
        try:
            await self._repost_mu_list(channel)
            await interaction.followup.send(
                f"✅ MU-lijst herplaatst in {channel.mention}.", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Fout bij herplaatsen: {e}", ephemeral=True
            )

    async def _mu_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        titles = [e["title"] for e in (self.json_data or {}).get("embeds", [])]
        return [
            app_commands.Choice(name=t, value=t)
            for t in titles
            if current.lower() in t.lower()
        ][:25]

    @app_commands.command(
        name="wijzigmu",
        description="Wijzig de gegevens van een MU en herplaats de MU-lijst.",
    )
    @app_commands.describe(
        mu_naam="De naam van de MU om te wijzigen",
        titel="Nieuwe naam/titel van de MU",
        mu_type="Het nieuwe type van de MU",
        link="Nieuwe link naar de MU-pagina op warera.io",
        thumbnail="Nieuwe URL van het MU-logo",
    )
    @app_commands.autocomplete(mu_naam=_mu_name_autocomplete)
    @app_commands.choices(
        mu_type=[
            app_commands.Choice(name="Elite", value="Elite"),
            app_commands.Choice(name="Eco", value="Eco"),
            app_commands.Choice(name="Standaard", value="Standaard"),
        ]
    )
    @app_commands.default_permissions(manage_messages=True)
    @has_privileged_role()
    async def wijzigmu(
        self,
        interaction: discord.Interaction,
        mu_naam: str,
        titel: str | None = None,
        mu_type: str | None = None,
        link: str | None = None,
        thumbnail: str | None = None,
    ) -> None:
        """Wijzig één of meer velden van een MU in mus.json en herplaats de MU-lijst."""
        await interaction.response.defer(ephemeral=True)

        if not self.json_data:
            self.load_json(mus_path(getattr(self.bot, "testing", False)))

        if not any([titel, mu_type, link, thumbnail]):
            await interaction.followup.send(
                "❌ Geef minimaal één veld op om te wijzigen (titel, mu_type, link of thumbnail).",
                ephemeral=True,
            )
            return

        embeds = self.json_data.get("embeds", [])
        target = next((e for e in embeds if e["title"] == mu_naam), None)
        if target is None:
            await interaction.followup.send(
                f"❌ Geen MU gevonden met naam **{mu_naam}**.", ephemeral=True
            )
            return

        changes = []

        # ── Update title ────────────────────────────────────────────
        if titel:
            target["title"] = titel
            changes.append(f"titel → **{titel}**")

        # ── Parse current description into (type, url) so we can patch either ──
        old_desc = target.get("description", "")
        desc_match = re.match(r"\[\*\*(.*?) MU\*\*\]\((.*?)\)", old_desc)
        current_type = desc_match.group(1) if desc_match else None
        current_url = desc_match.group(2) if desc_match else None

        new_type = mu_type or current_type
        new_url = link or current_url

        if mu_type or link:
            if new_type and new_url:
                target["description"] = f"[**{new_type} MU**]({new_url})"
            elif new_type:
                target["description"] = f"**{new_type} MU**"
            if mu_type:
                changes.append(f"type → **{new_type} MU**")
            if link:
                changes.append("link bijgewerkt")

        # ── Update thumbnail ──────────────────────────────────────
        if thumbnail:
            target["thumbnail"] = thumbnail
            changes.append("thumbnail bijgewerkt")

        try:
            with open(
                mus_path(getattr(self.bot, "testing", False)), "w", encoding="utf-8"
            ) as f:
                json.dump(self.json_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            await interaction.followup.send(f"❌ Opslaan mislukt: {e}", ephemeral=True)
            return

        # Repost the full list to the configured MU channel
        channel = await self._mu_channel(interaction.channel)
        try:
            await self._repost_mu_list(channel)
        except Exception as e:
            await interaction.followup.send(
                f"✅ **{mu_naam}** bijgewerkt ({', '.join(changes)}), maar herposten mislukt: {e}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ **{mu_naam}** bijgewerkt: {', '.join(changes)}. MU-lijst herplaatst in {channel.mention}.",
            ephemeral=True,
        )


async def setup(bot) -> None:
    """Add the MUs cog to the bot."""
    await bot.add_cog(MUs(bot))
