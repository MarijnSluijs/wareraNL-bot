"""MU role selection commands and MU membership management."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from cogs.role_selection.roles import RoleToggleView, load_roles_template, mu_roles_path
from cogs.welcome import Welcome
from utils.checks import has_privileged_role

logger = logging.getLogger(__name__)


def mus_json_path(testing: bool = False) -> str:
    return "templates/mus.testing.json" if testing else "templates/mus.json"


_MAX_BUTTONS_PER_MESSAGE = 25


def _chunk_buttons_for_view(buttons: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split buttons into Discord-safe pages and recompute rows per page."""
    chunks: list[list[dict[str, Any]]] = []
    for start in range(0, len(buttons), _MAX_BUTTONS_PER_MESSAGE):
        page = buttons[start : start + _MAX_BUTTONS_PER_MESSAGE]
        normalized_page: list[dict[str, Any]] = []
        for idx, btn in enumerate(page):
            item = dict(btn)
            item["row"] = idx // 5
            normalized_page.append(item)
        if normalized_page:
            chunks.append(normalized_page)
    return chunks


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
        self.template = load_roles_template(
            mu_roles_path(getattr(bot, "testing", False))
        )

        if self.template.get("embeds"):
            for embed_data in self.template["embeds"]:
                for page in _chunk_buttons_for_view(embed_data.get("buttons", [])):
                    self.bot.add_view(RoleToggleView(page, exclusive=True))
        if self.template.get("buttons"):
            for page in _chunk_buttons_for_view(self.template["buttons"]):
                self.bot.add_view(RoleToggleView(page, exclusive=True))

    @commands.command(name="muroles")
    @has_privileged_role()
    async def muroles(self, ctx: Context) -> None:
        mu_channel_id = self.bot.config.get("channels", {}).get("military_unit")
        target_channel = (
            ctx.guild.get_channel(mu_channel_id) if mu_channel_id and ctx.guild else None
        ) or ctx.channel

        mus_cog = self.bot.cogs.get("mus")
        if mus_cog:
            try:
                await mus_cog._repost_mu_list(target_channel)
                await ctx.send(f"✅ MU-lijst + knoppen opnieuw gepost in {target_channel.mention}.")
                return
            except Exception as exc:
                await ctx.send(f"❌ Herposten van MU-lijst mislukt: {exc}")
                return

        await ctx.send(f"✅ MU-rolknoppen gepost in {target_channel.mention}.")

    @commands.command(name="muwachtlijst")
    @has_privileged_role()
    async def muwachtlijst(self, ctx: Context) -> None:
        guild = ctx.guild
        if not guild:
            await ctx.send("❌ Guild not found.")
            return

        wachtlijst_role_id = self.bot.config.get("roles", {}).get("wachtlijst")
        if not wachtlijst_role_id:
            await ctx.send("❌ Wachtlijst role not configured.")
            return

        wachtlijst_role = guild.get_role(wachtlijst_role_id)
        if not wachtlijst_role:
            await ctx.send("❌ Wachtlijst role not found.")
            return

        count = len(wachtlijst_role.members)
        await ctx.send(f"📋 Er zijn momenteel {count} mensen op de wachtlijst voor MU's.")

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
    
    def _normalize_mu_id(self, in_game_id: str) -> str:
        """Normalize and validate in-game ID or WarEra profile URL input."""
        raw_value = str(in_game_id).strip()
        if not raw_value:
            raise ValueError("In-game ID cannot be empty.")

        # Accept direct profile links like: https://app.warera.io/mu/{id}
        match = re.match(
            r"^https?://app\.warera\.io/mu/([^/?#]+)(?:[/?#].*)?$",
            raw_value,
            flags=re.IGNORECASE,
        )
        if match:
            normalized = match.group(1).strip()
        else:
            normalized = raw_value
            if "://" in raw_value:
                raise ValueError(
                    "Invalid WarEra profile URL. "
                    "Use `https://app.warera.io/mu/{id}` or provide the raw in-game ID."
                )

        if not normalized:
            raise ValueError("Could not extract an in-game ID from the provided input.")
        if len(normalized) > 64:
            raise ValueError("In-game ID is too long (max 64 characters).")
        return normalized

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


        mu_id = self._normalize_mu_id(mu_id)
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
                await interaction.followup.send(
                    f"❌ Rol aanmaken mislukt: {e}", ephemeral=True
                )
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
            await interaction.followup.send(
                f"❌ Opslaan mus.json mislukt: {e}", ephemeral=True
            )
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

        # Refresh MU memberships so the new MU's members land in the DB before the sync
        citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
        nl_country_id = self.bot.config.get("nl_country_id")
        testing = getattr(self.bot, "testing", False)
        mus_path_str = "templates/mus.testing.json" if testing else "templates/mus.json"
        if citizen_cache and nl_country_id:
            try:
                with open(mus_path_str, encoding="utf-8") as _f:
                    _mus_data = json.load(_f)
                _mu_entries = [
                    (str(e.get("id", "")).strip(), str(e.get("name") or e.get("title") or ""))
                    for e in _mus_data.get("embeds", [])
                    if str(e.get("id", "")).strip()
                ]
                if _mu_entries:
                    await citizen_cache.refresh_mu_memberships(nl_country_id, _mu_entries)
            except Exception as _e:
                logger.warning("voegmu: refresh_mu_memberships failed: %s", _e)

        # Trigger an immediate MU role sync so members get the new role right away
        citizen_tasks_cog = self.bot.cogs.get("citizen_tasks")
        if citizen_tasks_cog:
            try:
                sync_stats = await citizen_tasks_cog._sync_mu_roles_for_all_guilds()
                assigned = sync_stats.get("assigned", 0)
                await interaction.followup.send(
                    f"✅ MU **{mu_name}** toegevoegd (id: `{mu_id}`, rol: {rol.mention}) en MU-lijst herplaatst in {target_channel.mention}.\n"
                    f"🪖 MU-rollen gesynchroniseerd: **{assigned}** leden kregen de rol.",
                    ephemeral=True,
                )
            except Exception as e:
                logger.warning("voegmu: MU role sync failed: %s", e)
                await interaction.followup.send(
                    f"✅ MU **{mu_name}** toegevoegd (id: `{mu_id}`, rol: {rol.mention}) en MU-lijst herplaatst in {target_channel.mention}.\n"
                    f"⚠️ MU-rollen synchronisatie mislukt: {e}",
                    ephemeral=True,
                )
        else:
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
                    deleted_role_msg = (
                        " ⚠️ Kon de Discord-rol niet verwijderen (onvoldoende rechten)."
                    )
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
