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

import csv
import datetime
import difflib
import io
import logging
import re
import unicodedata
from pathlib import Path

import discord
from discord import app_commands

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")


class Users(CommandCogBase, name="users"):
    """Admin commands for Discord ↔ in-game identity mappings."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._fallback_db = None

    @staticmethod
    def _normalize_name(value: str) -> str:
        """Normalize a username for fuzzy matching across Discord/game formats."""
        ascii_value = (
            unicodedata.normalize("NFKD", str(value or ""))
            .encode("ascii", "ignore")
            .decode("ascii")
            .lower()
        )
        return "".join(ch for ch in ascii_value if ch.isalnum())

    def _best_citizen_match(
        self,
        member: discord.Member,
        citizens: list[tuple[str, str, str]],
    ) -> tuple[str, str, float, float, str] | None:
        """Return best citizen match for a member: (id, name, best, second, variant)."""
        variants: list[str] = []
        for raw in [member.display_name, member.nick, member.name]:
            norm = self._normalize_name(raw)
            if norm and norm not in variants:
                variants.append(norm)
        if not variants or not citizens:
            return None

        best_uid = ""
        best_name = ""
        best_variant = ""
        best_score = 0.0
        second_score = 0.0

        for uid, citizen_name, citizen_norm in citizens:
            if not citizen_norm:
                continue
            citizen_best = 0.0
            citizen_variant = ""
            for variant in variants:
                if variant == citizen_norm:
                    ratio = 1.0
                else:
                    ratio = difflib.SequenceMatcher(None, variant, citizen_norm).ratio()
                    if variant.startswith(citizen_norm) or citizen_norm.startswith(
                        variant
                    ):
                        ratio = min(1.0, ratio + 0.08)
                if ratio > citizen_best:
                    citizen_best = ratio
                    citizen_variant = variant

            if citizen_best > best_score:
                second_score = best_score
                best_score = citizen_best
                best_uid = uid
                best_name = citizen_name
                best_variant = citizen_variant
            elif citizen_best > second_score:
                second_score = citizen_best

        if not best_uid:
            return None
        return best_uid, best_name, best_score, second_score, best_variant

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
        embed.add_field(
            name="Discord", value=f"{user.mention} (`{user.id}`)", inline=False
        )
        embed.add_field(
            name="In-game ID", value=f"`{record['in_game_user_id']}`", inline=True
        )
        embed.add_field(name="Nationaliteit", value=record["nationality"], inline=True)
        embed.add_field(name="Type", value=record["request_type"], inline=True)
        embed.add_field(
            name="Goedgekeurd op", value=record["approved_at"], inline=False
        )
        embed.add_field(
            name="Goedgekeurd door",
            value=f"<@{record['approved_by_discord_id']}> (`{record['approved_by_discord_id']}`)",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="linkid",
        description="Link een Discord gebruiker aan een in-game ID (of update bestaande mapping)",
    )
    @app_commands.describe(
        user="Discord gebruiker",
        in_game_id="In-game ID of profiel-URL (https://app.warera.io/user/{id})",
        nationality="Optioneel: nationaliteit (bijv. nederlander, belgian, foreigner)",
        request_type="Optioneel: request type (standaard: manual_link)",
        embassy_country="Optioneel: embassy-land (alleen voor embassy mappings)",
        force="Sta toe dat dit in-game ID al aan een andere Discord gebruiker hangt",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def link_id(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        in_game_id: str,
        nationality: str | None = None,
        request_type: str | None = None,
        embassy_country: str | None = None,
        force: bool = False,
    ) -> None:
        """Manually create or update a Discord ↔ in-game mapping."""
        try:
            normalized = self._normalize_ingame_id(in_game_id)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        discord_id = str(user.id)
        db = await self._get_db()

        existing_for_discord = await db.get_identity_link_by_discord(
            discord_user_id=discord_id,
            guild_id=guild_id,
        )
        existing_for_ingame = await db.get_identity_links_by_ingame(
            in_game_user_id=normalized,
            guild_id=guild_id,
        )
        conflicting_discord = next(
            (
                link.get("discord_user_id")
                for link in existing_for_ingame
                if str(link.get("discord_user_id")) != discord_id
            ),
            None,
        )

        if conflicting_discord and not force:
            await interaction.response.send_message(
                (
                    "Dit in-game ID is al gekoppeld aan een andere Discord gebruiker: "
                    f"<@{conflicting_discord}> (`{conflicting_discord}`). "
                    "Gebruik `force=True` als je deze mapping bewust wilt overschrijven."
                ),
                ephemeral=True,
            )
            return

        final_nationality = (
            str(nationality).strip().lower()
            if nationality and str(nationality).strip()
            else str((existing_for_discord or {}).get("nationality") or "manual")
        )
        final_request_type = (
            str(request_type).strip().lower()
            if request_type and str(request_type).strip()
            else str((existing_for_discord or {}).get("request_type") or "manual_link")
        )
        final_embassy_country = (
            str(embassy_country).strip()
            if embassy_country and embassy_country.strip()
            else None
        )
        approved_at = datetime.datetime.now(datetime.UTC).isoformat()

        await db.upsert_identity_link(
            discord_user_id=discord_id,
            guild_id=guild_id,
            in_game_user_id=normalized,
            nationality=final_nationality,
            request_type=final_request_type,
            embassy_country=final_embassy_country,
            approved_by_discord_id=str(interaction.user.id),
            approved_at=approved_at,
        )

        embed = discord.Embed(
            title="✅ Mapping opgeslagen",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Discord", value=f"{user.mention} (`{user.id}`)", inline=False
        )
        embed.add_field(name="In-game ID", value=f"`{normalized}`", inline=True)
        embed.add_field(name="Nationaliteit", value=final_nationality, inline=True)
        embed.add_field(name="Type", value=final_request_type, inline=True)
        if final_embassy_country:
            embed.add_field(
                name="Embassy-land",
                value=final_embassy_country,
                inline=True,
            )

        if existing_for_discord and existing_for_discord.get("in_game_user_id"):
            previous = str(existing_for_discord.get("in_game_user_id"))
            if previous != normalized:
                embed.add_field(
                    name="Vorige in-game ID", value=f"`{previous}`", inline=False
                )
            else:
                embed.add_field(
                    name="Info", value="Bestaande mapping bijgewerkt.", inline=False
                )
        else:
            embed.add_field(
                name="Info", value="Nieuwe mapping aangemaakt.", inline=False
            )

        if conflicting_discord and force:
            embed.add_field(
                name="⚠️ Force override",
                value=(
                    "Dit in-game ID stond ook op een andere Discord gebruiker. "
                    "Controleer met `/discordid` of aanvullende opschoning nodig is."
                ),
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

    @app_commands.command(
        name="userverifybackfillnl",
        description="Backfill Discord↔in-game mappings voor Nederlander-rol met fuzzy matching",
    )
    @app_commands.describe(
        apply="Schrijf mappings weg (standaard: alleen preview)",
        min_score="Minimale fuzzy score (0.50-1.00), standaard 0.90",
        refresh_nl="Eerst NL citizens verversen via API",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_verify_backfill_nl(
        self,
        interaction: discord.Interaction,
        apply: bool = False,
        min_score: float = 0.90,
        refresh_nl: bool = True,
    ) -> None:
        """Backfill identity links for members with the Nederlander role."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Dit commando kan alleen in een server gebruikt worden.", ephemeral=True
            )
            return

        min_score = max(0.50, min(1.00, float(min_score)))
        nl_country_id = str(self.config.get("nl_country_id") or "").strip()
        nl_role_id = (self.config.get("roles") or {}).get("nederlander")
        if not nl_country_id:
            await interaction.followup.send(
                "`nl_country_id` ontbreekt in de configuratie.", ephemeral=True
            )
            return
        if not nl_role_id:
            await interaction.followup.send(
                "`roles.nederlander` ontbreekt in de configuratie.", ephemeral=True
            )
            return

        nl_role = guild.get_role(int(nl_role_id))
        if nl_role is None:
            await interaction.followup.send(
                "De Nederlander-rol kon niet worden gevonden in deze server.",
                ephemeral=True,
            )
            return

        refreshed = 0
        if refresh_nl:
            citizen_cache = getattr(self.bot, "_ext_citizen_cache", None)
            if not citizen_cache:
                await interaction.followup.send(
                    "Citizen cache service is niet beschikbaar.", ephemeral=True
                )
                return
            try:
                lock = getattr(self.bot, "_ext_heavy_api_lock", None)
                if lock:
                    async with lock:
                        refreshed = await citizen_cache.refresh_country(
                            nl_country_id,
                            "Netherlands",
                        )
                else:
                    refreshed = await citizen_cache.refresh_country(
                        nl_country_id,
                        "Netherlands",
                    )
            except Exception as exc:
                logger.exception("backfillnl: NL refresh failed")
                await interaction.followup.send(
                    f"NL refresh mislukt: `{exc}`", ephemeral=True
                )
                return

        db = await self._get_db()
        citizens_raw = await db.get_nl_citizen_ids(nl_country_id)
        citizens: list[tuple[str, str, str]] = [
            (uid, name, self._normalize_name(name)) for uid, name in citizens_raw
        ]
        nl_citizen_ids = {uid for uid, _name in citizens_raw}
        if not citizens:
            await interaction.followup.send(
                "Geen NL-citizens in de cache gevonden. Draai eerst een citizens refresh.",
                ephemeral=True,
            )
            return

        members = [m for m in nl_role.members if not m.bot]
        if not members:
            await interaction.followup.send(
                "Geen leden met de Nederlander-rol gevonden.", ephemeral=True
            )
            return

        approved_at = datetime.datetime.now(datetime.UTC).isoformat()
        rows: list[dict[str, str]] = []
        candidates: list[dict[str, object]] = []
        already_mapped = 0
        mapped_not_nl = 0

        for member in members:
            discord_id = str(member.id)
            existing = await db.get_identity_link_by_discord(
                discord_user_id=discord_id,
                guild_id=str(guild.id),
            )
            if existing and existing.get("in_game_user_id"):
                existing_ingame_id = str(existing.get("in_game_user_id"))
                if existing_ingame_id in nl_citizen_ids:
                    already_mapped += 1
                    status = "already_mapped"
                    note = "bestond al in identity_links"
                else:
                    mapped_not_nl += 1
                    status = "mapped_not_nl"
                    note = "bestaande mapping wijst naar niet-NL citizen"
                rows.append(
                    {
                        "discord_id": discord_id,
                        "discord_name": member.display_name,
                        "status": status,
                        "in_game_id": existing_ingame_id,
                        "in_game_name": "",
                        "score": "1.000",
                        "note": note,
                    }
                )
                continue

            match = self._best_citizen_match(member, citizens)
            if match is None:
                rows.append(
                    {
                        "discord_id": discord_id,
                        "discord_name": member.display_name,
                        "status": "no_match",
                        "in_game_id": "",
                        "in_game_name": "",
                        "score": "0.000",
                        "note": "geen bruikbare naamvariant",
                    }
                )
                continue

            in_game_id, in_game_name, best, second, variant = match
            candidates.append(
                {
                    "discord_id": discord_id,
                    "discord_name": member.display_name,
                    "in_game_id": in_game_id,
                    "in_game_name": in_game_name,
                    "score": best,
                    "second": second,
                    "variant": variant,
                }
            )

        candidates.sort(key=lambda item: float(item["score"]), reverse=True)
        chosen_ingame: set[str] = set()
        auto_linked = 0
        high_confidence = 0
        ambiguous = 0
        conflicts = 0

        for cand in candidates:
            discord_id = str(cand["discord_id"])
            in_game_id = str(cand["in_game_id"])
            score = float(cand["score"])
            second = float(cand["second"])
            margin = score - second

            status = "candidate"
            note = f"variant={cand['variant']} margin={margin:.3f}"

            if score < min_score:
                status = "ambiguous"
                note = f"score {score:.3f} < min_score {min_score:.3f}"
            elif margin < 0.08:
                status = "ambiguous"
                note = f"klein verschil met 2e match ({margin:.3f})"
            elif in_game_id in chosen_ingame:
                status = "conflict"
                note = "zelfde in-game ID al toegekend aan sterkere match"
            else:
                existing_ingame = await db.get_identity_links_by_ingame(
                    in_game_user_id=in_game_id,
                    guild_id=str(guild.id),
                )
                linked_other = next(
                    (
                        link.get("discord_user_id")
                        for link in existing_ingame
                        if str(link.get("discord_user_id")) != discord_id
                    ),
                    None,
                )
                if linked_other:
                    status = "conflict"
                    note = f"in-game ID al gekoppeld aan <@{linked_other}>"
                else:
                    high_confidence += 1
                    chosen_ingame.add(in_game_id)
                    if apply:
                        await db.upsert_identity_link(
                            discord_user_id=discord_id,
                            guild_id=str(guild.id),
                            in_game_user_id=in_game_id,
                            nationality="nederlander",
                            request_type="backfill_nederlander",
                            embassy_country=None,
                            approved_by_discord_id=str(interaction.user.id),
                            approved_at=approved_at,
                        )
                        status = "linked"
                        note = "automatisch gelinkt"
                        auto_linked += 1
                    else:
                        status = "review"
                        note = "hoog vertrouwen; klaar voor handmatige check"

            if status == "ambiguous":
                ambiguous += 1
            elif status == "conflict":
                conflicts += 1

            rows.append(
                {
                    "discord_id": discord_id,
                    "discord_name": str(cand["discord_name"]),
                    "status": status,
                    "in_game_id": in_game_id,
                    "in_game_name": str(cand["in_game_name"]),
                    "score": f"{score:.3f}",
                    "note": note,
                }
            )

        rows.sort(key=lambda r: (r["status"], r["discord_name"].lower()))

        csv_buf = io.StringIO()
        csv_buf.write(
            "discord_id,discord_name,status,in_game_id,in_game_name,score,note\n"
        )
        for row in rows:
            safe = []
            for key in (
                "discord_id",
                "discord_name",
                "status",
                "in_game_id",
                "in_game_name",
                "score",
                "note",
            ):
                value = str(row.get(key, "")).replace('"', '""')
                safe.append(f'"{value}"')
            csv_buf.write(",".join(safe) + "\n")

        embed = discord.Embed(
            title="🇳🇱 Nederlander backfill verificatie",
            color=discord.Color.green() if apply else discord.Color.orange(),
            description=(
                "Resultaat van fuzzy matching tussen Discord Nederlander-leden "
                "en NL citizens uit de cache."
            ),
        )
        embed.add_field(name="Nederlander-leden", value=str(len(members)), inline=True)
        embed.add_field(
            name="NL citizens (cache)", value=str(len(citizens)), inline=True
        )
        embed.add_field(name="Al gemapt", value=str(already_mapped), inline=True)
        embed.add_field(name="Mapped not NL", value=str(mapped_not_nl), inline=True)
        embed.add_field(name="High confidence", value=str(high_confidence), inline=True)
        embed.add_field(name="Ambiguous", value=str(ambiguous), inline=True)
        embed.add_field(name="Conflicts", value=str(conflicts), inline=True)
        if refresh_nl:
            embed.add_field(name="NL refreshed", value=str(refreshed), inline=True)
        if apply:
            embed.add_field(name="Nieuw gelinkt", value=str(auto_linked), inline=True)
            embed.set_footer(text="apply=true: links zijn opgeslagen in identity_links")
        else:
            embed.set_footer(
                text="apply=false: preview mode. Controleer CSV en voer daarna opnieuw uit met apply=true."
            )

        filename_ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        report_text = csv_buf.getvalue()
        default_report_path = Path(
            self.config.get(
                "nl_backfill_report_path",
                "output/nl_backfill_report_latest.csv",
            )
        )
        try:
            default_report_path.parent.mkdir(parents=True, exist_ok=True)
            default_report_path.write_text(report_text, encoding="utf-8")
            embed.add_field(
                name="Saved report",
                value=f"`{default_report_path}`",
                inline=False,
            )
        except Exception as exc:
            logger.warning("backfillnl: failed to save report to disk: %s", exc)

        report_file = discord.File(
            io.BytesIO(report_text.encode("utf-8")),
            filename=f"nl_backfill_report_{filename_ts}.csv",
        )
        await interaction.followup.send(embed=embed, file=report_file, ephemeral=True)

    @app_commands.command(
        name="userverifyapplynlcsv",
        description="Pas handmatig gereviewde NL backfill CSV toe op identity_links",
    )
    @app_commands.describe(
        reviewed_csv="Optioneel: CSV uit /userverifybackfillnl (anders default bestandspad)",
        dry_run="Alleen valideren/simuleren, niet wegschrijven",
        overwrite_existing="Sta overschrijven van bestaande Discord mapping toe",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def user_verify_apply_nl_csv(
        self,
        interaction: discord.Interaction,
        reviewed_csv: discord.Attachment | None = None,
        dry_run: bool = True,
        overwrite_existing: bool = False,
    ) -> None:
        """Apply reviewed NL backfill CSV rows to identity_links."""
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "Dit commando kan alleen in een server gebruikt worden.", ephemeral=True
            )
            return

        nl_country_id = str(self.config.get("nl_country_id") or "").strip()
        nl_role_id = (self.config.get("roles") or {}).get("nederlander")
        if not nl_country_id or not nl_role_id:
            await interaction.followup.send(
                "Configuratie mist `nl_country_id` of `roles.nederlander`.",
                ephemeral=True,
            )
            return

        if reviewed_csv is not None:
            try:
                raw_bytes = await reviewed_csv.read()
                text = raw_bytes.decode("utf-8-sig")
            except Exception as exc:
                await interaction.followup.send(
                    f"CSV kon niet worden gelezen als UTF-8: `{exc}`", ephemeral=True
                )
                return
            source_label = f"attachment `{reviewed_csv.filename}`"
        else:
            default_review_path = Path(
                self.config.get(
                    "nl_backfill_report_path",
                    "output/nl_backfill_report_latest.csv",
                )
            )
            try:
                text = default_review_path.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                await interaction.followup.send(
                    "Geen attachment meegegeven en default CSV bestaat niet: "
                    f"`{default_review_path}`",
                    ephemeral=True,
                )
                return
            except Exception as exc:
                await interaction.followup.send(
                    "Default CSV kon niet worden gelezen: "
                    f"`{default_review_path}` ({exc})",
                    ephemeral=True,
                )
                return
            source_label = f"default file `{default_review_path}`"

        reader = csv.DictReader(io.StringIO(text))
        required_cols = {"discord_id", "in_game_id"}
        header = set(reader.fieldnames or [])
        if not required_cols.issubset(header):
            await interaction.followup.send(
                "CSV mist verplichte kolommen: `discord_id`, `in_game_id`.",
                ephemeral=True,
            )
            return

        db = await self._get_db()
        nl_citizens = await db.get_nl_citizen_ids(nl_country_id)
        nl_citizen_ids = {uid for uid, _name in nl_citizens}

        nl_role = guild.get_role(int(nl_role_id))
        nl_member_ids = (
            {str(m.id) for m in nl_role.members if not m.bot} if nl_role else set()
        )

        approved_statuses = {"review", "approved", "apply", "linked", "manual"}
        approved_at = datetime.datetime.now(datetime.UTC).isoformat()

        scanned = 0
        to_apply = 0
        applied = 0
        skipped = 0
        conflicts = 0
        not_nl_citizen = 0
        not_nederlander_role = 0
        malformed = 0

        result_rows: list[dict[str, str]] = []

        for row in reader:
            scanned += 1
            discord_id = str((row.get("discord_id") or "")).strip()
            in_game_id = str((row.get("in_game_id") or "")).strip()
            status = str((row.get("status") or "review")).strip().lower()

            if not discord_id or not in_game_id:
                malformed += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": "malformed",
                        "action": "skipped",
                        "note": "ontbrekende discord_id of in_game_id",
                    }
                )
                continue

            if status not in approved_statuses:
                skipped += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "skipped",
                        "note": "status niet gemarkeerd voor toepassen",
                    }
                )
                continue

            if in_game_id not in nl_citizen_ids:
                not_nl_citizen += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "rejected",
                        "note": "in-game ID staat niet in huidige NL citizens cache",
                    }
                )
                continue

            if discord_id not in nl_member_ids:
                not_nederlander_role += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "rejected",
                        "note": "Discord gebruiker heeft nu geen Nederlander-rol",
                    }
                )
                continue

            existing_discord = await db.get_identity_link_by_discord(
                discord_user_id=discord_id,
                guild_id=str(guild.id),
            )
            if existing_discord and existing_discord.get("in_game_user_id"):
                current = str(existing_discord.get("in_game_user_id"))
                if current != in_game_id and not overwrite_existing:
                    conflicts += 1
                    result_rows.append(
                        {
                            "discord_id": discord_id,
                            "in_game_id": in_game_id,
                            "status": status,
                            "action": "conflict",
                            "note": f"bestaande mapping: {current} (overwrite_existing=false)",
                        }
                    )
                    continue

            existing_ingame = await db.get_identity_links_by_ingame(
                in_game_user_id=in_game_id,
                guild_id=str(guild.id),
            )
            linked_other = next(
                (
                    link.get("discord_user_id")
                    for link in existing_ingame
                    if str(link.get("discord_user_id")) != discord_id
                ),
                None,
            )
            if linked_other:
                conflicts += 1
                result_rows.append(
                    {
                        "discord_id": discord_id,
                        "in_game_id": in_game_id,
                        "status": status,
                        "action": "conflict",
                        "note": f"in-game ID al gekoppeld aan {linked_other}",
                    }
                )
                continue

            to_apply += 1
            if not dry_run:
                await db.upsert_identity_link(
                    discord_user_id=discord_id,
                    guild_id=str(guild.id),
                    in_game_user_id=in_game_id,
                    nationality="nederlander",
                    request_type="backfill_nederlander_reviewed",
                    embassy_country=None,
                    approved_by_discord_id=str(interaction.user.id),
                    approved_at=approved_at,
                )
                applied += 1
                action = "applied"
                note = "mapping opgeslagen"
            else:
                action = "would_apply"
                note = "dry-run: mapping zou opgeslagen worden"

            result_rows.append(
                {
                    "discord_id": discord_id,
                    "in_game_id": in_game_id,
                    "status": status,
                    "action": action,
                    "note": note,
                }
            )

        out_csv = io.StringIO()
        out_csv.write("discord_id,in_game_id,status,action,note\n")
        for rr in result_rows:
            vals = []
            for key in ("discord_id", "in_game_id", "status", "action", "note"):
                vals.append(f'"{str(rr.get(key, "")).replace('"', '""')}"')
            out_csv.write(",".join(vals) + "\n")

        embed = discord.Embed(
            title="🧾 NL backfill CSV apply",
            color=discord.Color.orange() if dry_run else discord.Color.green(),
            description=(
                "Resultaat van toepassen van handmatig gereviewde CSV. "
                "Alleen expliciet goedgekeurde statussen zijn verwerkt."
            ),
        )
        embed.add_field(name="Rijen gescand", value=str(scanned), inline=True)
        embed.add_field(name="Bron", value=source_label, inline=False)
        embed.add_field(name="Klaar om toe te passen", value=str(to_apply), inline=True)
        embed.add_field(name="Toegepast", value=str(applied), inline=True)
        embed.add_field(name="Overgeslagen", value=str(skipped), inline=True)
        embed.add_field(name="Conflicts", value=str(conflicts), inline=True)
        embed.add_field(name="Malformed", value=str(malformed), inline=True)
        embed.add_field(name="Niet NL citizen", value=str(not_nl_citizen), inline=True)
        embed.add_field(
            name="Geen Nederlander-rol",
            value=str(not_nederlander_role),
            inline=True,
        )
        embed.set_footer(
            text=(
                "dry_run=true: niets weggeschreven"
                if dry_run
                else "dry_run=false: mappings opgeslagen"
            )
        )

        filename_ts = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        result_file = discord.File(
            io.BytesIO(out_csv.getvalue().encode("utf-8")),
            filename=f"nl_backfill_apply_result_{filename_ts}.csv",
        )
        await interaction.followup.send(embed=embed, file=result_file, ephemeral=True)


async def setup(bot) -> None:
    await bot.add_cog(Users(bot))
