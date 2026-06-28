"""Pill buff reminder: button UI + Cog.

A persistent button is posted in the roles channel via ``/generalroles``.
Clicking it:
  - If not yet subscribed:
      * If the user has a verified identity link → auto-subscribes them.
      * Otherwise → opens a Modal asking for their in-game username.
  - If already subscribed: unsubscribes them immediately.

The bot's hourly scan checks the API for active pill buffs and the 30-second
DM task fires when fewer than 10 minutes remain.
"""

from __future__ import annotations

import json
import logging

import discord
from discord.ext import commands

logger = logging.getLogger("discord_bot")

BUTTON_CUSTOM_ID    = "pill_reminder:toggle"
BUTTON_CUSTOM_ID_30 = "pill_reminder_30:toggle"

_STATE_FILE         = "templates/pillreminder_state.json"
_STATE_FILE_TESTING = "templates/pillreminder_state.testing.json"


def _state_file(testing: bool) -> str:
    return _STATE_FILE_TESTING if testing else _STATE_FILE


def _load_state(testing: bool = False) -> dict:
    try:
        with open(_state_file(testing), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict, testing: bool = False) -> None:
    with open(_state_file(testing), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _unwrap(resp: object) -> dict | list | None:
    if not isinstance(resp, dict):
        return resp  # type: ignore[return-value]
    inner = resp.get("result", resp)
    if isinstance(inner, dict):
        return inner.get("data", inner)  # type: ignore[return-value]
    return inner  # type: ignore[return-value]


# ── Modal (fallback when identity_links has no entry) ────────────────────────

class SpelernaamModal(discord.ui.Modal, title="Aanmelden voor pill buff herinnering"):
    spelersnaam = discord.ui.TextInput(
        label="In-game gebruikersnaam",
        placeholder="Jouw WarEra gebruikersnaam",
        min_length=1,
        max_length=64,
    )

    def __init__(self, minutes: int = 10) -> None:
        super().__init__(title=f"Aanmelden voor pill buff herinnering ({minutes} min)")
        self.minutes = minutes

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        bot = interaction.client
        db = getattr(bot, "_ext_db", None)
        api_client = getattr(bot, "_ext_client", None)

        if db is None:
            await interaction.followup.send(
                "❌ Database is momenteel niet beschikbaar. Probeer het later opnieuw.",
                ephemeral=True,
            )
            return

        discord_user_id = str(interaction.user.id)
        spelersnaam = self.spelersnaam.value.strip()

        # Try to resolve username → in-game ID via search endpoint
        game_user_id: str | None = None
        if api_client:
            try:
                raw = await api_client.get(
                    "/search.searchAnything",
                    params={"input": json.dumps({"searchText": spelersnaam})},
                )
                data = _unwrap(raw)
                if isinstance(data, dict):
                    ids = data.get("userIds") or []
                    if ids:
                        game_user_id = str(ids[0])
            except Exception as exc:
                logger.debug("pill_reminder modal: search failed for %r: %s", spelersnaam, exc)

        if game_user_id is None:
            await interaction.followup.send(
                f"❌ De gebruikersnaam **{discord.utils.escape_markdown(spelersnaam)}** "
                "kon niet worden gevonden in WarEra. "
                "Controleer je spelling en probeer het opnieuw.",
                ephemeral=True,
            )
            return

        if self.minutes == 30:
            await db.subscribe_pill_reminder_30(discord_user_id, game_user_id)
        else:
            await db.subscribe_pill_reminder(discord_user_id, game_user_id)

        embed = discord.Embed(
            title="✅ Aangemeld voor pill buff herinnering",
            description=(
                f"**In-game naam:** {discord.utils.escape_markdown(spelersnaam)}\n\n"
                f"De bot stuurt je een DM "
                f"op het moment dat er nog precies **{self.minutes} minuten** over zijn.\n\n"
                "Klik opnieuw op de knop om je af te melden."
            ),
            colour=discord.Colour.green(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(
            "pill_reminder: %s subscribed via modal as %r (game_id=%s, minutes=%d)",
            interaction.user, spelersnaam, game_user_id, self.minutes,
        )


# ── Persistent button & view ─────────────────────────────────────────────────

class PillReminderButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Pill buff herinnering",
            style=discord.ButtonStyle.success,
            custom_id=BUTTON_CUSTOM_ID,
            emoji="💊",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "_ext_db", None)
        if db is None:
            await interaction.response.send_message(
                "❌ Database is momenteel niet beschikbaar. Probeer het later opnieuw.",
                ephemeral=True,
            )
            return

        discord_user_id = str(interaction.user.id)

        if await db.is_pill_subscriber(discord_user_id):
            # Already subscribed → unsubscribe
            await db.remove_pill_reminder(discord_user_id)
            await interaction.response.send_message(
                "✅ Je bent afgemeld van pill buff herinneringen.",
                ephemeral=True,
            )
            logger.info("pill_reminder: %s unsubscribed", interaction.user)
            return

        # Not subscribed → check identity_links first
        identity = await db.get_identity_link_by_discord(discord_user_id)
        if identity and identity.get("in_game_user_id"):
            game_user_id = identity["in_game_user_id"]
            await db.subscribe_pill_reminder(discord_user_id, game_user_id)
            embed = discord.Embed(
                title="✅ Aangemeld voor pill buff herinnering",
                description=(
                    "Je WarEra-account is automatisch gekoppeld via je geverifieerde identiteit.\n\n"
                    "De bot stuurt je een DM "
                    "op het moment dat er nog precies **10 minuten** over zijn.\n\n"
                    "Klik opnieuw op de knop om je af te melden."
                ),
                colour=discord.Colour.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(
                "pill_reminder: %s auto-subscribed via identity_links (game_id=%s)",
                interaction.user,
                game_user_id,
            )
        else:
            await interaction.response.send_modal(SpelernaamModal(minutes=10))


class PillReminderButton30(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Pill buff herinnering (30 min)",
            style=discord.ButtonStyle.success,
            custom_id=BUTTON_CUSTOM_ID_30,
            emoji="💊",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "_ext_db", None)
        if db is None:
            await interaction.response.send_message(
                "❌ Database is momenteel niet beschikbaar. Probeer het later opnieuw.",
                ephemeral=True,
            )
            return

        discord_user_id = str(interaction.user.id)

        if await db.is_pill_reminder_30_subscriber(discord_user_id):
            await db.remove_pill_reminder_30(discord_user_id)
            await interaction.response.send_message(
                "✅ Je bent afgemeld van de 30-minuten pill buff herinnering.",
                ephemeral=True,
            )
            logger.info("pill_reminder_30: %s unsubscribed", interaction.user)
            return

        identity = await db.get_identity_link_by_discord(discord_user_id)
        if identity and identity.get("in_game_user_id"):
            game_user_id = identity["in_game_user_id"]
            await db.subscribe_pill_reminder_30(discord_user_id, game_user_id)
            embed = discord.Embed(
                title="✅ Aangemeld voor pill buff herinnering (30 min)",
                description=(
                    "Je WarEra-account is automatisch gekoppeld via je geverifieerde identiteit.\n\n"
                    "De bot stuurt je een DM "
                    "op het moment dat er nog precies **30 minuten** over zijn.\n\n"
                    "Klik opnieuw op de knop om je af te melden."
                ),
                colour=discord.Colour.green(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(
                "pill_reminder_30: %s auto-subscribed via identity_links (game_id=%s)",
                interaction.user,
                game_user_id,
            )
        else:
            await interaction.response.send_modal(SpelernaamModal(minutes=30))


class PillReminderView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(PillReminderButton())
        self.add_item(PillReminderButton30())


# ── Cog ──────────────────────────────────────────────────────────────────────

class PillReminderCog(commands.Cog, name="pill_reminder"):
    def __init__(self, bot) -> None:
        self.bot = bot
        bot.add_view(PillReminderView())

    async def cog_load(self) -> None:
        pass


async def setup(bot) -> None:
    """Add the PillReminderCog to the bot."""
    from services.subscription_registry import register

    register(
        name="pill",
        emoji="💊",
        label="Pill Reminder",
        get_fn=lambda db, uid: db.get_pill_subscriber(uid),
        remove_fn=lambda db, uid: db.remove_pill_reminder(uid),
        detail_fn=lambda row: f"In-game gebruiker: **{row['in_game_user_id']}**",
    )
    await bot.add_cog(PillReminderCog(bot))

