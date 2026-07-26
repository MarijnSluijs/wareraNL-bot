"""Manage and post the Military Units list."""

from __future__ import annotations

import json
import re
from typing import Any

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.standard_messages.generate import GenerateEmbeds


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


def has_mu_privilige() -> commands.check:
    """commands check for mulijst using cog-configured role IDs (owner bypass)."""

    async def predicate(ctx: Context) -> bool:
        bot = ctx.bot
        if getattr(bot, "testing", False):
            return True

        if await bot.is_owner(ctx.author):
            return True

        if not isinstance(ctx.author, discord.Member):
            raise commands.CheckFailure(
                "Dit commando kan alleen in een server gebruikt worden."
            )

        role_ids = [
            bot.config.get("roles", {}).get(r) for r in ["officier", "government"]
        ]

        if role_ids and any(role.id in role_ids for role in ctx.author.roles):
            return True

        raise commands.CheckFailure(
            "Je hebt geen permissie om dit commando te gebruiken."
        )

    return commands.check(predicate)


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

    def _normalize_mu_entries(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Normalize old and new mus.json entries to {id, type, role_id}."""
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            mu_id = str(
                entry.get("id") or ""
            ).strip() or _extract_mu_id_from_description(
                str(entry.get("description", ""))
            )
            if not mu_id or mu_id in seen_ids:
                continue

            mu_type = _normalize_mu_type(
                entry.get("type")
                or _extract_mu_type_from_description(str(entry.get("description", "")))
            )

            role_id_raw = entry.get("role_id", 0)
            try:
                role_id = int(role_id_raw) if role_id_raw else 0
            except (TypeError, ValueError):
                role_id = 0

            normalized_item: dict[str, Any] = {
                "id": mu_id,
                "type": mu_type,
                "role_id": role_id,
            }

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

    @commands.command(name="mulijst")
    @has_mu_privilige()
    async def mulijst(self, context: Context) -> None:
        if not self.json_data or not self.json_data.get("embeds"):
            embed = discord.Embed(
                description="MU data niet gevonden.",
                color=self.get_color("error"),
            )
            await context.send(embed=embed, ephemeral=True)
            return

        await context.send("📚 Bezig met posten van de MU lijst...", ephemeral=True)
        channel = await self._mu_channel(context.channel)
        await self._repost_mu_list(channel)
        self.bot.logger.info(
            "MU lijst posted by %s in %s", context.author, channel.name
        )

    async def _repost_mu_list(self, channel: discord.TextChannel) -> None:
        """Delete previous MU posts, refresh MU info, and post embeds."""
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

        self.json_data = self.json_data or {}
        self.json_data["embeds"] = entries_sorted
        self.json_data["posted_message_ids"] = new_ids
        try:
            self._save_json(path)
        except Exception as exc:
            self.bot.logger.error("Failed to save MU JSON: %s", exc)

    @commands.command(name="repostmu")
    @has_mu_privilige()
    async def repostmu(self, ctx: Context) -> None:
        channel = await self._mu_channel(ctx.channel)
        try:
            await self._repost_mu_list(channel)
            await ctx.send(f"✅ MU-lijst herplaatst in {channel.mention}.")
        except Exception as e:
            await ctx.send(f"❌ Fout bij herplaatsen: {e}")


async def setup(bot) -> None:
    """Add the MUs cog to the bot."""
    await bot.add_cog(MUs(bot))
