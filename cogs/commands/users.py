"""
User database commands.

Commands and listeners:
  /ingameid - get in-game ID mapping for a Discord user
  /discordid - get Discord user mapping(s) for an in-game ID or profile URL
  /usercount - count mapped users (optionally filtered by nationality)
  /userdbhealth - overview of DB health and conflict indicators
  /userrecent - list recently approved mappings
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")


class Users(CommandCogBase, name="users"):
	"""Admin commands for Discord ↔ in-game identity mappings."""

	def __init__(self, bot) -> None:
		self.bot = bot
		self._fallback_db = None

	async def _get_db(self):
		"""Return shared external DB, or lazily create one as fallback."""
		shared = self._db
		if shared is not None:
			return shared
		if self._fallback_db is None:
			from services.db import Database

			db_path = self.config.get("external_db_path", "database/external.db")
			self._fallback_db = Database(db_path)
			await self._fallback_db.setup()
		return self._fallback_db

	async def cog_app_command_error(
		self,
		interaction: discord.Interaction,
		error: app_commands.AppCommandError,
	) -> None:
		"""Handle app command errors for this cog."""
		if isinstance(error, app_commands.MissingPermissions):
			await interaction.response.send_message(
				"Je hebt geen toestemming om dit commando te gebruiken.",
				ephemeral=True,
			)
			return
		logger.exception("users command error: %s", error)
		if interaction.response.is_done():
			await interaction.followup.send(
				"Er ging iets mis bij het uitvoeren van dit commando.",
				ephemeral=True,
			)
		else:
			await interaction.response.send_message(
				"Er ging iets mis bij het uitvoeren van dit commando.",
				ephemeral=True,
			)

	@staticmethod
	def _normalize_ingame_id(raw_value: str) -> str:
		"""Accept in-game ID or WarEra profile URL and return plain ID."""
		raw = str(raw_value).strip()
		if not raw:
			raise ValueError("In-game ID cannot be empty.")

		match = re.match(
			r"^https?://app\.warera\.io/user/([^/?#]+)(?:[/?#].*)?$",
			raw,
			flags=re.IGNORECASE,
		)
		if match:
			normalized = match.group(1).strip()
		else:
			normalized = raw
			if "://" in raw:
				raise ValueError(
					"Invalid WarEra profile URL. Use https://app.warera.io/user/{id} or provide the raw in-game ID."
				)

		if not normalized:
			raise ValueError("Could not extract an in-game ID from the input.")
		if len(normalized) > 64:
			raise ValueError("In-game ID is too long (max 64 characters).")
		return normalized

	@app_commands.command(
		name="ingameid",
		description="Toon de in-game ID die is gekoppeld aan een Discord gebruiker",
	)
	@app_commands.describe(user="Discord gebruiker")
	@app_commands.checks.has_permissions(manage_guild=True)
	async def ingame_id(self, interaction: discord.Interaction, user: discord.Member):
		db = await self._get_db()
		record = await db.get_identity_link_by_discord(
			discord_user_id=str(user.id), guild_id=str(interaction.guild_id)
		)
		if not record:
			await interaction.response.send_message(
				"Geen mapping gevonden voor deze gebruiker.", ephemeral=True
			)
			return

		embed = discord.Embed(
			title="🔎 Mapping via Discord",
			color=discord.Color.blurple(),
		)
		embed.add_field(name="Discord", value=f"{user.mention} (`{user.id}`)", inline=False)
		embed.add_field(name="In-game ID", value=f"`{record['in_game_user_id']}`", inline=True)
		embed.add_field(name="Nationaliteit", value=record["nationality"], inline=True)
		embed.add_field(name="Type", value=record["request_type"], inline=True)
		embed.add_field(name="Goedgekeurd op", value=record["approved_at"], inline=False)
		embed.add_field(
			name="Goedgekeurd door",
			value=f"<@{record['approved_by_discord_id']}> (`{record['approved_by_discord_id']}`)",
			inline=False,
		)
		await interaction.response.send_message(embed=embed, ephemeral=True)

	@app_commands.command(
		name="discordid",
		description="Toon Discord mapping(s) voor een in-game ID of profiel-URL",
	)
	@app_commands.describe(
		in_game_id="In-game ID of profiel-URL (https://app.warera.io/user/{id})"
	)
	@app_commands.checks.has_permissions(manage_guild=True)
	async def discord_id(self, interaction: discord.Interaction, in_game_id: str):
		try:
			normalized = self._normalize_ingame_id(in_game_id)
		except ValueError as e:
			await interaction.response.send_message(str(e), ephemeral=True)
			return

		db = await self._get_db()
		links = await db.get_identity_links_by_ingame(
			in_game_user_id=normalized,
			guild_id=str(interaction.guild_id),
		)
		if not links:
			await interaction.response.send_message(
				f"Geen Discord mapping gevonden voor in-game ID `{normalized}`.",
				ephemeral=True,
			)
			return

		embed = discord.Embed(
			title="🔎 Mapping via in-game ID",
			description=f"In-game ID: `{normalized}`",
			color=discord.Color.blurple(),
		)
		for link in links[:10]:
			embed.add_field(
				name=f"Discord: <@{link['discord_user_id']}>",
				value=(
					f"ID: `{link['discord_user_id']}`\n"
					f"Nationaliteit: {link['nationality']}\n"
					f"Type: {link['request_type']}\n"
					f"Updated: {link['updated_at']}"
				),
				inline=False,
			)
		if len(links) > 10:
			embed.set_footer(text=f"Toont 10 van {len(links)} resultaten")
		await interaction.response.send_message(embed=embed, ephemeral=True)

	@app_commands.command(
		name="usercount",
		description="Toon aantal gebruikers in identity database (optioneel per nationaliteit)",
	)
	@app_commands.describe(
		nationality="Optioneel, bijv. nederlander, belgian, foreigner of een embassy-land"
	)
	@app_commands.checks.has_permissions(manage_guild=True)
	async def user_count(
		self, interaction: discord.Interaction, nationality: str | None = None
	):
		db = await self._get_db()
		total = await db.count_identity_links(guild_id=str(interaction.guild_id))
		filtered = None
		if nationality:
			filtered = await db.count_identity_links(
				guild_id=str(interaction.guild_id),
				nationality=nationality.strip(),
			)

		embed = discord.Embed(title="📊 User DB aantallen", color=discord.Color.green())
		embed.add_field(name="Totaal", value=str(total), inline=True)
		if filtered is not None:
			embed.add_field(
				name=f"Filter: {nationality.strip().lower()}",
				value=str(filtered),
				inline=True,
			)
		await interaction.response.send_message(embed=embed, ephemeral=True)

	@app_commands.command(
		name="userdbhealth",
		description="Toon databasegezondheid voor identity mappings",
	)
	@app_commands.checks.has_permissions(manage_guild=True)
	async def user_db_health(self, interaction: discord.Interaction):
		db = await self._get_db()
		guild_id = str(interaction.guild_id)
		total = await db.count_identity_links(guild_id=guild_id)
		conflicts = await db.count_identity_ingame_conflicts(guild_id=guild_id)
		by_nat = await db.identity_counts_by_nationality(guild_id=guild_id)

		embed = discord.Embed(
			title="🩺 User DB Health",
			color=discord.Color.orange() if conflicts else discord.Color.green(),
		)
		embed.add_field(name="Mappings", value=str(total), inline=True)
		embed.add_field(
			name="In-game conflicts",
			value=str(conflicts),
			inline=True,
		)
		if by_nat:
			lines = [f"- {name}: {count}" for name, count in by_nat[:12]]
			embed.add_field(
				name="Per nationaliteit",
				value="\n".join(lines),
				inline=False,
			)
		await interaction.response.send_message(embed=embed, ephemeral=True)

	@app_commands.command(
		name="userrecent",
		description="Toon recente identity mappings",
	)
	@app_commands.describe(limit="Aantal recente records (1-20, standaard 10)")
	@app_commands.checks.has_permissions(manage_guild=True)
	async def user_recent(self, interaction: discord.Interaction, limit: int = 10):
		db = await self._get_db()
		rows = await db.get_recent_identity_links(
			guild_id=str(interaction.guild_id),
			limit=max(1, min(limit, 20)),
		)
		if not rows:
			await interaction.response.send_message(
				"Nog geen identity mappings gevonden.", ephemeral=True
			)
			return

		lines = []
		for row in rows:
			lines.append(
				f"<@{row['discord_user_id']}> → `{row['in_game_user_id']}` "
				f"({row['nationality']}, {row['request_type']})"
			)
		embed = discord.Embed(
			title="🕒 Recente user mappings",
			description="\n".join(lines),
			color=discord.Color.blurple(),
		)
		await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
	await bot.add_cog(Users(bot))

