"""MU role selection commands and MU membership management."""

from __future__ import annotations

import json
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

from .roles import RoleToggleView, load_roles_template, mu_roles_path, post_or_edit_buttons


def mus_json_path(testing: bool = False) -> str:
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


def _extract_mu_id_from_link(link: str) -> str | None:
    match = re.search(r"/mu/([A-Za-z0-9]+)", link or "")
    return match.group(1) if match else None


def _normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in entries:
        if not isinstance(item, dict):
            continue

        mu_id = str(item.get("id") or "").strip()
        if not mu_id:
            description = str(item.get("description", ""))
            mu_id = _extract_mu_id_from_link(description) or ""
        if not mu_id or mu_id in seen:
            continue

        mu_type = _normalize_mu_type(item.get("type"))
        if "type" not in item:
            description = str(item.get("description", "")).lower()
            if "elite" in description:
                mu_type = "Elite"
            elif "eco" in description:
                mu_type = "Eco"

        role_id_raw = item.get("role_id", 0)
        try:
            role_id = int(role_id_raw) if role_id_raw else 0
        except (TypeError, ValueError):
            role_id = 0

        normalized_item: dict[str, Any] = {
            "id": mu_id,
            "type": mu_type,
            "role_id": role_id,
        }

        if item.get("name"):
            normalized_item["name"] = str(item.get("name"))
        elif item.get("title"):
            normalized_item["name"] = str(item.get("title"))

        if item.get("thumbnail"):
            normalized_item["thumbnail"] = str(item.get("thumbnail"))

        normalized.append(normalized_item)
        seen.add(mu_id)

    return normalized


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
        await interaction.response.defer(ephemeral=True)

        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        mus_cog = self.bot.cogs.get("mus")
        if mus_cog:
            try:
                await mus_cog._repost_mu_list(target_channel)
                await interaction.followup.send(
                    f"✅ MU-lijst + knoppen opnieuw gepost in {target_channel.mention}.",
                    ephemeral=True,
                )
                return
            except Exception as exc:
                await interaction.followup.send(
                    f"❌ Herposten van MU-lijst mislukt: {exc}", ephemeral=True
                )
                return

        path = mu_roles_path(getattr(self.bot, "testing", False))
        self.template = load_roles_template(path)
        buttons = self.template.get("buttons", [])
        if not buttons:
            await interaction.followup.send(
                "Geen knoppen geconfigureerd in de MU-template.", ephemeral=True
            )
            return

        color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)
        await post_or_edit_buttons(target_channel, self.template, path, color)
        await interaction.followup.send(
            f"✅ MU-rolknoppen gepost in {target_channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="muwachtlijst",
        description="Tel het aantal mensen op de wachtlijst voor MU's.",
    )
    @has_privileged_role()
    async def muwachtlijst(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Guild not found.", ephemeral=True)
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

    def _load_mus_entries(self) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        path = mus_json_path(getattr(self.bot, "testing", False))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {"embeds": [], "posted_message_ids": []}

        entries = _normalize_entries(data.get("embeds", []))
        return path, data, entries

    async def _fetch_mu_name(self, mu_id: str) -> str:
        client = getattr(self.bot, "_ext_client", None)
        if not client:
            return f"MU {mu_id[:8]}"

        try:
            resp = await client.get(
                "/mu.getById",
                params={"input": json.dumps({"muId": mu_id})},
            )
        except Exception:
            return f"MU {mu_id[:8]}"

        data: Any = resp
        if isinstance(resp, dict):
            result = resp.get("result")
            if isinstance(result, dict):
                data = result.get("data", result)

        if isinstance(data, dict):
            return str(data.get("name") or data.get("title") or f"MU {mu_id[:8]}")
        return f"MU {mu_id[:8]}"

    async def _mu_id_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        _, _, entries = self._load_mus_entries()
        current_lower = current.lower()
        return [
            app_commands.Choice(
                name=f"{entry['type']} • {entry['id']}",
                value=entry["id"],
            )
            for entry in entries
            if current_lower in entry["id"].lower()
        ][:25]

    @app_commands.command(
        name="voegmu",
        description="Voeg een MU toe op basis van MU-id en type.",
    )
    @app_commands.describe(
        mu_id="WarEra MU ID (uit de MU URL)",
        mu_type="Het type van de MU",
        rol="Bestaande Discord-rol (laat leeg om er automatisch een aan te maken)",
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
        mu_id: str,
        mu_type: str,
        rol: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        mu_name = await self._fetch_mu_name(mu_id)

        if rol is None:
            try:
                rol = await interaction.guild.create_role(
                    name=mu_name,
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
                await interaction.followup.send(f"❌ Rol aanmaken mislukt: {e}", ephemeral=True)
                return

        path, data, entries = self._load_mus_entries()

        if any(e["id"] == mu_id for e in entries):
            await interaction.followup.send(
                f"❌ MU **{mu_id}** staat al in mus.json.", ephemeral=True
            )
            return

        entries.append(
            {
                "id": mu_id,
                "type": _normalize_mu_type(mu_type),
                "role_id": rol.id,
            }
        )
        data["embeds"] = entries

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            await interaction.followup.send(f"❌ Opslaan mus.json mislukt: {e}", ephemeral=True)
            return

        mus_cog = self.bot.cogs.get("mus")
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        if mus_cog:
            mus_cog.load_json(path)
            try:
                await mus_cog._repost_mu_list(target_channel)
            except Exception as e:
                await interaction.followup.send(
                    f"✅ MU **{mu_name}** toegevoegd, maar herposten mislukt: {e}",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(
            f"✅ MU **{mu_name}** toegevoegd (id: `{mu_id}`, rol: {rol.mention}) en MU-lijst herplaatst in {target_channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="verwijdermu",
        description="Verwijder een MU uit mus.json en de MU-rolselector.",
    )
    @app_commands.describe(
        mu_id="De MU ID om te verwijderen",
        verwijder_rol="Verwijder ook de gekoppelde Discord-rol (standaard: ja)",
    )
    @app_commands.autocomplete(mu_id=_mu_id_autocomplete)
    @has_privileged_role()
    async def verwijdermu(
        self,
        interaction: discord.Interaction,
        mu_id: str,
        verwijder_rol: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        path, data, entries = self._load_mus_entries()
        target = next((e for e in entries if e["id"] == mu_id), None)
        if target is None:
            await interaction.followup.send(
                f"❌ Geen MU gevonden met ID **{mu_id}**.", ephemeral=True
            )
            return

        entries = [e for e in entries if e["id"] != mu_id]
        data["embeds"] = entries

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            await interaction.followup.send(f"❌ Opslaan mislukt: {e}", ephemeral=True)
            return

        deleted_role_msg = ""
        if verwijder_rol:
            role = interaction.guild.get_role(int(target.get("role_id") or 0))
            if role:
                try:
                    await role.delete(
                        reason=f"Verwijderd door /verwijdermu van {interaction.user}"
                    )
                    deleted_role_msg = f" Discord-rol **{role.name}** verwijderd."
                except discord.Forbidden:
                    deleted_role_msg = " ⚠️ Kon de Discord-rol niet verwijderen (onvoldoende rechten)."
                except Exception as e:
                    deleted_role_msg = f" ⚠️ Rol verwijderen mislukt: {e}"
            else:
                deleted_role_msg = " ⚠️ Discord-rol niet gevonden in deze server."

        mus_cog = self.bot.cogs.get("mus")
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            interaction.guild.get_channel(mu_channel_id) if mu_channel_id else None
        ) or interaction.channel

        if mus_cog:
            mus_cog.load_json(path)
            try:
                await mus_cog._repost_mu_list(target_channel)
            except Exception as e:
                deleted_role_msg += f" ⚠️ Herposten mislukt: {e}"

        await interaction.followup.send(
            f"✅ MU **{mu_id}** verwijderd.{deleted_role_msg}",
            ephemeral=True,
        )


async def setup(bot) -> None:
    """Add the MuRoles cog to the bot."""
    await bot.add_cog(MuRoles(bot))
