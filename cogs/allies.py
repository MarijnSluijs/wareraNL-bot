"""Slash commands for managing the bot's Discord-only allies list.

These allies are stored in the ``discord_allies`` table and merged into the
protected-country set each time the bounty poller runs, suppressing bounty
alerts for battles involving those countries.

Commands
--------
/bondgenoot-add country    — pick a country by name; autocomplete from country_snapshots
/bondgenoot-remove country — remove; autocomplete from stored allies
/bondgenoten               — list all current Discord allies
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import has_privileged_role

logger = logging.getLogger("discord_bot")

_MARIJN_DISCORD_ID = 565626197048819731

# Country name (as stored in country_snapshots) → ISO 3166-1 alpha-2 code
_NAME_TO_ISO: dict[str, str] = {
    "Afghanistan": "AF", "Albania": "AL", "Algeria": "DZ", "Angola": "AO",
    "Argentina": "AR", "Armenia": "AM", "Australia": "AU", "Austria": "AT",
    "Azerbaijan": "AZ", "Bahamas": "BS", "Bangladesh": "BD", "Belarus": "BY",
    "Belgium": "BE", "Belize": "BZ", "Benin": "BJ", "Bhutan": "BT",
    "Bolivia": "BO", "Bosnia": "BA", "Brazil": "BR", "Bulgaria": "BG",
    "Cambodia": "KH", "Cameroon": "CM", "Canada": "CA", "Chile": "CL",
    "China": "CN", "Colombia": "CO", "Croatia": "HR", "Cuba": "CU",
    "Cyprus": "CY", "Czech Republic": "CZ", "Denmark": "DK",
    "Dominican Republic": "DO", "Ecuador": "EC", "Egypt": "EG",
    "El Salvador": "SV", "Estonia": "EE", "Ethiopia": "ET", "Finland": "FI",
    "France": "FR", "Georgia": "GE", "Germany": "DE", "Ghana": "GH",
    "Greece": "GR", "Guatemala": "GT", "Honduras": "HN", "Hungary": "HU",
    "Iceland": "IS", "India": "IN", "Indonesia": "ID", "Iran": "IR",
    "Iraq": "IQ", "Ireland": "IE", "Israel": "IL", "Italy": "IT",
    "Jamaica": "JM", "Japan": "JP", "Jordan": "JO", "Kazakhstan": "KZ",
    "Kenya": "KE", "Latvia": "LV", "Lebanon": "LB", "Libya": "LY",
    "Lithuania": "LT", "Luxembourg": "LU", "Malaysia": "MY", "Mexico": "MX",
    "Moldova": "MD", "Mongolia": "MN", "Montenegro": "ME", "Morocco": "MA",
    "Mozambique": "MZ", "Myanmar": "MM", "Netherlands": "NL", "New Zealand": "NZ",
    "Nicaragua": "NI", "Nigeria": "NG", "North Korea": "KP",
    "North Macedonia": "MK", "Norway": "NO", "Pakistan": "PK", "Panama": "PA",
    "Paraguay": "PY", "Peru": "PE", "Philippines": "PH", "Poland": "PL",
    "Portugal": "PT", "Romania": "RO", "Russia": "RU", "Saudi Arabia": "SA",
    "Senegal": "SN", "Serbia": "RS", "Sierra Leone": "SL", "Slovakia": "SK",
    "Slovenia": "SI", "Somalia": "SO", "South Africa": "ZA", "South Korea": "KR",
    "Spain": "ES", "Sri Lanka": "LK", "Sudan": "SD", "Sweden": "SE",
    "Switzerland": "CH", "Syria": "SY", "Taiwan": "TW", "Thailand": "TH",
    "Tunisia": "TN", "Turkey": "TR", "Turkiye": "TR", "Ukraine": "UA",
    "United Arab Emirates": "AE", "United Kingdom": "GB",
    "United States": "US", "Uruguay": "UY", "Uzbekistan": "UZ",
    "Venezuela": "VE", "Vietnam": "VN", "Yemen": "YE", "Zimbabwe": "ZW",
}


def _flag(country_name: str) -> str:
    """Return the flag emoji for a country name, or empty string if unknown."""
    iso = _NAME_TO_ISO.get(country_name)
    if not iso or len(iso) != 2:
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


class AlliesCog(commands.Cog, name="allies"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def _db(self):
        return getattr(self.bot, "_ext_db", None)

    def _is_privileged(self, interaction: discord.Interaction) -> bool:
        if getattr(self.bot, "testing", False):
            return True
        if interaction.user.id == _MARIJN_DISCORD_ID:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        privileged_keys = {"officier", "government", "commandant"}
        role_ids_cfg = self.bot.config.get("roles", {})
        privileged_ids = {
            int(role_ids_cfg[k])
            for k in privileged_keys
            if k in role_ids_cfg and str(role_ids_cfg[k]).isdigit()
        }
        return any(r.id in privileged_ids for r in interaction.user.roles)

    # ── autocomplete: country from country_snapshots ────────────────────────

    async def _country_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Suggest countries by name from country_snapshots; value = country_id."""
        db = self._db
        if not db:
            return []
        try:
            name_map = await db.get_country_name_map()  # {country_id: name}
        except Exception:
            return []
        q = current.strip().lower()
        choices = [
            app_commands.Choice(name=name, value=cid)
            for cid, name in sorted(name_map.items(), key=lambda x: x[1])
            if q in name.lower()
        ]
        return choices[:25]

    # ── autocomplete: country_id from existing allies ────────────────────────

    async def _ally_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        db = self._db
        if not db:
            return []
        try:
            allies = await db.get_discord_allies_full()
        except Exception:
            return []
        q = current.strip().lower()
        choices = []
        for a in allies:
            cid = a["country_id"]
            label = a["country_name"] or cid
            if q in label.lower() or q in cid.lower():
                choices.append(app_commands.Choice(name=label, value=cid))
        return choices[:25]

    # ── /bondgenoot-add ──────────────────────────────────────────────────────

    @app_commands.command(
        name="bondgenoot-add",
        description="Voeg een land toe aan de Discord-bondgenotenlijst (bounty filter).",
    )
    @app_commands.describe(country="Naam van het land (kies uit de lijst)")
    @app_commands.autocomplete(country=_country_autocomplete)
    @has_privileged_role()
    async def cmd_bondgenoot_add(
        self,
        interaction: discord.Interaction,
        country: str,
    ) -> None:
        if not self._is_privileged(interaction):
            await interaction.response.send_message(
                "❌ Je hebt geen toestemming om bondgenoten te beheren.", ephemeral=True
            )
            return

        db = self._db
        if not db:
            await interaction.response.send_message(
                "❌ Database niet beschikbaar.", ephemeral=True
            )
            return

        # `country` is either a country_id (when picked from autocomplete) or
        # freeform text the user typed.  Resolve to a valid (country_id, name) pair.
        raw = country.strip()
        country_id: str | None = None
        country_name: str | None = None
        try:
            name_map = await db.get_country_name_map()  # {country_id: name}
            if raw in name_map:
                # Exact country_id match (autocomplete selection)
                country_id = raw
                country_name = name_map[raw]
            else:
                # Try case-insensitive name match (freeform typed text)
                raw_lower = raw.lower()
                for cid, name in name_map.items():
                    if name.lower() == raw_lower:
                        country_id = cid
                        country_name = name
                        break
        except Exception:
            pass

        if not country_id:
            await interaction.response.send_message(
                f"❌ `{raw}` is geen bekend land. Kies een land uit de autocomplete-lijst.",
                ephemeral=True,
            )
            return

        try:
            await db.add_discord_ally(
                country_id=country_id,
                added_by=str(interaction.user.id),
                country_name=country_name,
            )
        except Exception:
            logger.exception("bondgenoot-add: DB error")
            await interaction.response.send_message(
                "❌ Fout bij opslaan in de database.", ephemeral=True
            )
            return

        display = f"**{country_name}** (`{country_id}`)" if country_name else f"`{country_id}`"
        await interaction.response.send_message(
            f"✅ {display} toegevoegd aan de Discord-bondgenotenlijst.\n"
            "De bounty poller gebruikt deze lijst bij de volgende poll.",
            ephemeral=True,
        )
        logger.info(
            "bondgenoot-add: %s added country_id=%s name=%s",
            interaction.user,
            country_id,
            country_name,
        )

    # ── /bondgenoot-remove ───────────────────────────────────────────────────

    @app_commands.command(
        name="bondgenoot-remove",
        description="Verwijder een land van de Discord-bondgenotenlijst.",
    )
    @app_commands.describe(country="Land om te verwijderen (kies uit de lijst)")
    @app_commands.autocomplete(country=_ally_id_autocomplete)
    @has_privileged_role()
    async def cmd_bondgenoot_remove(
        self,
        interaction: discord.Interaction,
        country: str,
    ) -> None:
        if not self._is_privileged(interaction):
            await interaction.response.send_message(
                "❌ Je hebt geen toestemming om bondgenoten te beheren.", ephemeral=True
            )
            return

        db = self._db
        if not db:
            await interaction.response.send_message(
                "❌ Database niet beschikbaar.", ephemeral=True
            )
            return

        country_id = country.strip()
        try:
            removed = await db.remove_discord_ally(country_id)
        except Exception:
            logger.exception("bondgenoot-remove: DB error")
            await interaction.response.send_message(
                "❌ Fout bij verwijderen uit de database.", ephemeral=True
            )
            return

        if removed:
            await interaction.response.send_message(
                f"✅ `{country_id}` verwijderd van de Discord-bondgenotenlijst.",
                ephemeral=True,
            )
            logger.info(
                "bondgenoot-remove: %s removed country_id=%s", interaction.user, country_id
            )
        else:
            await interaction.response.send_message(
                f"⚠️ `{country_id}` stond niet op de Discord-bondgenotenlijst.",
                ephemeral=True,
            )

    # ── /bondgenoten ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="bondgenoten",
        description="Toon de huidige Discord-bondgenotenlijst (bounty filter).",
    )
    async def cmd_bondgenoten(self, interaction: discord.Interaction) -> None:
        db = self._db
        if not db:
            await interaction.response.send_message(
                "❌ Database niet beschikbaar.", ephemeral=True
            )
            return

        try:
            allies = await db.get_discord_allies_full()
        except Exception:
            logger.exception("bondgenoten: DB error")
            await interaction.response.send_message(
                "❌ Fout bij ophalen van de lijst.", ephemeral=True
            )
            return

        if not allies:
            await interaction.response.send_message(
                "De bondgenotenlijst is leeg.", ephemeral=True
            )
            return

        lines = []
        for a in allies:
            name = a["country_name"] or "—"
            flag = _flag(name)
            prefix = f"{flag} " if flag else ""
            lines.append(f"• {prefix}**{name}**")

        embed = discord.Embed(
            title="🤝 Bondgenoten",
            description="\n".join(lines),
            colour=discord.Colour.blue(),
        )
        embed.set_footer(text=f"{len(allies)} land(en) — bounties gericht tégen deze landen worden onderdrukt")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AlliesCog(bot))
