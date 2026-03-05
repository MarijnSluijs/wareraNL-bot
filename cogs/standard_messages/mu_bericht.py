"""Manage and post the Military Units list and MU role buttons."""

from __future__ import annotations

import json
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.role_selection.roles import RoleToggleView, load_roles_template, mu_roles_path
from cogs.standard_messages.generate import GenerateEmbeds
from utils.checks import has_privileged_role


def mus_path(testing: bool = False) -> str:
    """Return the correct mus JSON path for the current mode."""
    return "templates/mus.testing.json" if testing else "templates/mus.json"


def _normalize_mu_type(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if raw in {"elite", "elite mu"}:
        return "Elite"
    if raw in {"eco", "eco mu"}:
        return "Eco"
    if raw in {"standaard", "standard", "standaard mu", "standard mu"}:
        return "Standaard"
    return "Standaard"


def _extract_mu_type_from_description(description: str) -> str:
    match = re.search(r"\*\*(Elite|Eco|Standaard) MU\*\*", description or "")
    return _normalize_mu_type(match.group(1) if match else None)


def _extract_mu_id_from_description(description: str) -> str | None:
    match = re.search(r"/mu/([A-Za-z0-9]+)", description or "")
    return match.group(1) if match else None


class MUs(GenerateEmbeds, name="mus"):
    """Cog for managing and posting the Military Units list in a Discord channel."""

    _MU_TYPE_ORDER: dict[str, int] = {"Elite": 0, "Eco": 1, "Standaard": 2}
    _MU_TYPE_COLORS: dict[str, discord.Color] = {
        "Elite": discord.Color.orange(),
        "Eco": discord.Color.from_rgb(46, 204, 113),
        "Standaard": discord.Color.from_rgb(52, 152, 219),
    }

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.load_json(mus_path(getattr(bot, "testing", False)))

    def _normalize_mu_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize old and new mus.json entries to {id, type, role_id}."""
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            mu_id = str(entry.get("id") or "").strip() or _extract_mu_id_from_description(
                str(entry.get("description", ""))
            )
            if not mu_id or mu_id in seen_ids:
                continue

            mu_type = _normalize_mu_type(
                entry.get("type") or _extract_mu_type_from_description(str(entry.get("description", "")))
            )

            role_id_raw = entry.get("role_id", 0)
            try:
                role_id = int(role_id_raw) if role_id_raw else 0
            except (TypeError, ValueError):
                role_id = 0

            normalized_item: dict[str, Any] = {"id": mu_id, "type": mu_type, "role_id": role_id}

            if entry.get("name"):
                normalized_item["name"] = str(entry.get("name"))
            elif entry.get("title"):
                normalized_item["name"] = str(entry.get("title"))

            if entry.get("thumbnail"):
                normalized_item["thumbnail"] = str(entry.get("thumbnail"))

            normalized.append(normalized_item)
            seen_ids.add(mu_id)

        return normalized

    def _save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.json_data, f, indent=4, ensure_ascii=False)

    async def _mu_channel(self, fallback: discord.TextChannel) -> discord.TextChannel:
        """Return the configured military_unit channel, or fallback if not found."""
        ch_id = self.bot.config.get("channels", {}).get("military_unit")
        if ch_id:
            ch = self.bot.get_channel(ch_id)
            if ch:
                return ch
        return fallback

    @commands.hybrid_command(name="mulijst", description="Post de MU lijst in het MU-kanaal.")
    @has_privileged_role()
    async def mulijst(self, context: Context) -> None:
        if not self.json_data or not self.json_data.get("embeds"):
            embed = discord.Embed(
                description="MU data niet gevonden. Gebruik `/reloadmus` om opnieuw te laden.",
                color=self.get_color("error"),
            )
            await context.send(embed=embed, ephemeral=True)
            return

        await context.send("📚 Bezig met posten van de MU lijst...", ephemeral=True)
        channel = await self._mu_channel(context.channel)
        await self._repost_mu_list(channel)
        self.bot.logger.info("MU lijst posted by %s in %s", context.author, channel.name)

    @commands.hybrid_command(name="reloadmus", description="Herlaad de MU JSON file.")
    @commands.is_owner()
    async def reloadmus(self, context: Context) -> None:
        try:
            self.load_json(mus_path(getattr(self.bot, "testing", False)))
            embed = discord.Embed(
                description=(
                    f"✅ MU succesvol herladen! ({len(self.json_data.get('embeds', []))} entries)"
                ),
                color=self.get_color("success"),
            )
            await context.send(embed=embed)
            self.bot.logger.info("MU reloaded by %s", context.author)
        except Exception as e:
            embed = discord.Embed(
                description=f"❌ Fout bij herladen: {e}", color=self.get_color("error")
            )
            await context.send(embed=embed)

    async def _repost_mu_list(self, channel: discord.TextChannel) -> None:
        """Delete previous MU posts, refresh MU info, and post embeds + dynamic buttons."""
        path = mus_path(getattr(self.bot, "testing", False))

        mu_tasks = self.bot.get_cog("mu_tasks")
        if mu_tasks:
            try:
                await mu_tasks.refresh_mu_info()
            except Exception as exc:
                self.bot.logger.warning("_repost_mu_list: MU refresh failed: %s", exc)

        self.load_json(path)

        entries = self._normalize_mu_entries((self.json_data or {}).get("embeds", []))
        if not entries:
            self.bot.logger.warning("_repost_mu_list: no valid MU entries found")
            return

        try:
            await channel.purge(limit=100, check=lambda m: m.author == self.bot.user)
        except (discord.Forbidden, discord.HTTPException):
            pass

        old_ids: list[int] = (self.json_data or {}).get("posted_message_ids", [])
        for msg_id in old_ids:
            try:
                old_msg = await channel.fetch_message(msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

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
        explanation_msg = await channel.send(embed=explanation)
        new_ids.append(explanation_msg.id)

        entries_sorted = sorted(
            entries,
            key=lambda e: (
                self._MU_TYPE_ORDER.get(e["type"], 9999),
                str(e.get("name") or f"MU {e['id'][:8]}").lower(),
            ),
        )

        for entry in entries_sorted:
            mu_id = entry["id"]
            mu_type = entry["type"]
            mu_name = str(entry.get("name") or f"MU {mu_id[:8]}")
            thumbnail = str(entry.get("thumbnail") or "")

            embed = discord.Embed(
                title=mu_name,
                description=f"[**{mu_type} MU**](https://app.warera.io/mu/{mu_id})",
                color=self._MU_TYPE_COLORS.get(mu_type, discord.Color.greyple()),
            )
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            try:
                msg = await channel.send(embed=embed)
                new_ids.append(msg.id)
            except Exception as exc:
                self.bot.logger.error("Error sending embed for MU %s: %s", mu_id, exc)

        try:
            roles_path = mu_roles_path(getattr(self.bot, "testing", False))
            roles_data = load_roles_template(roles_path)
            all_buttons = roles_data.get("buttons", [])

            pinned_labels = {"Overige MU", "Wachtlijst"}
            pinned_role_defs = [
                {"label": "Overige MU", "style": "secondary"},
                {"label": "Wachtlijst", "style": "secondary"},
            ]

            secondary_role_id = next(
                (b.get("secondary_role_id") for b in all_buttons if b.get("secondary_role_id")),
                None,
            )

            # Ensure pinned roles exist and have button entries
            for pdef in pinned_role_defs:
                existing_btn = next((b for b in all_buttons if b.get("label") == pdef["label"]), None)
                role = None
                if existing_btn and existing_btn.get("role_id"):
                    role = channel.guild.get_role(int(existing_btn["role_id"]))

                if role is None:
                    role = discord.utils.get(channel.guild.roles, name=pdef["label"])
                if role is None:
                    try:
                        role = await channel.guild.create_role(
                            name=pdef["label"],
                            color=discord.Color.orange(),
                            mentionable=True,
                            reason="Automatisch aangemaakt door bot (vaste MU-knop)",
                        )
                    except Exception as exc:
                        self.bot.logger.error(
                            "Failed to create pinned role %s: %s", pdef["label"], exc
                        )
                        continue

                if existing_btn:
                    existing_btn["role_id"] = role.id
                else:
                    item = {
                        "label": pdef["label"],
                        "role_id": role.id,
                        "style": pdef["style"],
                        "row": 0,
                    }
                    if secondary_role_id:
                        item["secondary_role_id"] = secondary_role_id
                    all_buttons.append(item)

            mu_buttons: list[dict[str, Any]] = []
            for idx, entry in enumerate(entries_sorted):
                mu_id = entry["id"]
                mu_name = str(entry.get("name") or f"MU {mu_id[:8]}")
                role_id = int(entry.get("role_id") or 0)

                role = channel.guild.get_role(role_id) if role_id else None
                if role is None:
                    role = discord.utils.get(channel.guild.roles, name=mu_name)
                if role is None:
                    try:
                        role = await channel.guild.create_role(
                            name=mu_name,
                            color=discord.Color.orange(),
                            mentionable=False,
                            reason="Automatisch aangemaakt door MU synchronisatie",
                        )
                    except Exception as exc:
                        self.bot.logger.error(
                            "Failed to create role for MU %s (%s): %s",
                            mu_name,
                            mu_id,
                            exc,
                        )
                        continue

                if role.name != mu_name:
                    try:
                        await role.edit(name=mu_name, reason="MU naam gesynchroniseerd via API")
                    except Exception as exc:
                        self.bot.logger.warning(
                            "Failed to rename MU role %s to %s: %s", role.name, mu_name, exc
                        )

                entry["role_id"] = role.id

                button = {
                    "label": mu_name,
                    "role_id": role.id,
                    "style": "primary",
                    "row": idx // 5,
                }
                if secondary_role_id:
                    button["secondary_role_id"] = secondary_role_id
                mu_buttons.append(button)

            pinned_buttons = [b for b in all_buttons if b.get("label") in pinned_labels]
            pinned_row = (len(mu_buttons) + 4) // 5
            for b in pinned_buttons:
                b["row"] = pinned_row

            buttons = mu_buttons + pinned_buttons

            roles_data["buttons"] = buttons
            with open(roles_path, "w", encoding="utf-8") as f:
                json.dump(roles_data, f, indent=2, ensure_ascii=False)

            color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
            roles_embed = discord.Embed(
                title=roles_data.get("title", "MU Lidmaatschap"),
                description=roles_data.get("description", ""),
                color=color,
            )
            btn_msg = await channel.send(
                embed=roles_embed,
                view=RoleToggleView(buttons, exclusive=True),
            )
            roles_data["button_message_id"] = btn_msg.id
            with open(roles_path, "w", encoding="utf-8") as f:
                json.dump(roles_data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            self.bot.logger.error("Error sending role buttons: %s", exc)

        self.json_data = self.json_data or {}
        self.json_data["embeds"] = entries_sorted
        self.json_data["posted_message_ids"] = new_ids
        try:
            self._save_json(path)
        except Exception as exc:
            self.bot.logger.error("Failed to save MU JSON: %s", exc)

    @app_commands.command(
        name="repostmu",
        description="Herplaats de MU-lijst en synchroniseer MU namen/thumbnails via API.",
    )
    async def repostmu(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = await self._mu_channel(interaction.channel)
        try:
            await self._repost_mu_list(channel)
            await interaction.followup.send(
                f"✅ MU-lijst herplaatst in {channel.mention}.", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Fout bij herplaatsen: {e}", ephemeral=True)

    async def _mu_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        entries = self._normalize_mu_entries((self.json_data or {}).get("embeds", []))
        current_lower = current.lower()
        return [
            app_commands.Choice(
                name=f"{entry.get('name') or entry['id']}  [{entry['type']}]",
                value=entry["id"],
            )
            for entry in entries
            if current_lower in entry["id"].lower()
            or current_lower in (entry.get("name") or "").lower()
        ][:25]

    @app_commands.command(
        name="wijzigmu",
        description="Wijzig MU type en/of gekoppelde Discord-rol en herplaats de MU-lijst.",
    )
    @app_commands.describe(
        mu_id="De MU ID om te wijzigen",
        mu_type="Het nieuwe type van de MU",
        rol="Nieuwe gekoppelde Discord-rol",
    )
    @app_commands.autocomplete(mu_id=_mu_id_autocomplete)
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
        mu_id: str,
        mu_type: str | None = None,
        rol: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not self.json_data:
            self.load_json(mus_path(getattr(self.bot, "testing", False)))

        if not any([mu_type, rol]):
            await interaction.followup.send(
                "❌ Geef minimaal één veld op om te wijzigen (mu_type of rol).",
                ephemeral=True,
            )
            return

        entries = self._normalize_mu_entries((self.json_data or {}).get("embeds", []))
        target = next((e for e in entries if e["id"] == mu_id), None)
        if target is None:
            await interaction.followup.send(
                f"❌ Geen MU gevonden met ID **{mu_id}**.", ephemeral=True
            )
            return

        changes: list[str] = []
        if mu_type:
            normalized = _normalize_mu_type(mu_type)
            target["type"] = normalized
            changes.append(f"type → **{normalized}**")
        if rol:
            target["role_id"] = rol.id
            changes.append(f"rol → {rol.mention}")

        self.json_data = self.json_data or {}
        self.json_data["embeds"] = entries

        try:
            self._save_json(mus_path(getattr(self.bot, "testing", False)))
        except Exception as e:
            await interaction.followup.send(f"❌ Opslaan mislukt: {e}", ephemeral=True)
            return

        channel = await self._mu_channel(interaction.channel)
        try:
            await self._repost_mu_list(channel)
        except Exception as e:
            await interaction.followup.send(
                f"✅ MU **{mu_id}** bijgewerkt ({', '.join(changes)}), maar herposten mislukt: {e}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ MU **{mu_id}** bijgewerkt: {', '.join(changes)}. MU-lijst herplaatst in {channel.mention}.",
            ephemeral=True,
        )


async def setup(bot) -> None:
    """Add the MUs cog to the bot."""
    await bot.add_cog(MUs(bot))
