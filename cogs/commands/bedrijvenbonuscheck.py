"""Bedrijven bonus check: production-bonus monitor — button UI + owner commands.

A persistent button is posted in the roles channel via ``!bedrijven_bonus_check_post``.
Clicking it:
  - If not yet subscribed: opens a Modal asking for the in-game username.
  - If already subscribed: unsubscribes immediately (ephemeral confirmation).

Owner-only prefix commands:
  !bedrijven_bonus_check_post   — post (or update) the button message in the roles channel
  !bedrijven_bonus_check_check  — run the bonus scan immediately
  !bedrijven_bonus_check_lijst  — list all active subscriptions
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from discord.ext.commands import Context

from cogs.commands._base import CommandCogBase

logger = logging.getLogger("discord_bot")

# JSON file that persists the button message ID across restarts
_STATE_FILE          = "templates/bedrijvenbonuscheck_state.json"
_STATE_FILE_TESTING  = "templates/bedrijvenbonuscheck_state.testing.json"


def _state_file(testing: bool) -> str:
    return _STATE_FILE_TESTING if testing else _STATE_FILE

BUTTON_CUSTOM_ID = "bedrijven_bonus_check:toggle"


def _unwrap(resp: object) -> dict | list | None:
    if not isinstance(resp, dict):
        return resp  # type: ignore[return-value]
    inner = resp.get("result", resp)
    if isinstance(inner, dict):
        return inner.get("data", inner)  # type: ignore[return-value]
    return inner  # type: ignore[return-value]


def _load_state(testing: bool = False) -> dict:
    try:
        with open(_state_file(testing), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict, testing: bool = False) -> None:
    with open(_state_file(testing), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── Modal ────────────────────────────────────────────────────────────────────

class SpelernaamModal(discord.ui.Modal, title="Aanmelden voor Bedrijven bonus check"):
    spelersnaam = discord.ui.TextInput(
        label="In-game gebruikersnaam",
        placeholder="Jouw WarEra gebruikersnaam",
        min_length=1,
        max_length=64,
    )

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
        discord_username = str(interaction.user)
        guild_id = str(interaction.guild_id or "")
        spelersnaam = self.spelersnaam.value.strip()

        # Try to resolve the in-game username to a user ID up front.
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
                logger.debug("bedrijven_bonus_check modal: search failed for %r: %s", spelersnaam, exc)

        now_iso = datetime.now(timezone.utc).isoformat()
        await db.add_company_bonus_watcher(
            discord_user_id=discord_user_id,
            discord_username=discord_username,
            game_username=spelersnaam,
            guild_id=guild_id,
            added_at=now_iso,
            game_user_id=game_user_id,
        )

        resolved_note = ""
        if game_user_id is None:
            resolved_note = (
                "\n⚠️ De spelernaam kon niet worden opgezocht in de API. "
                "Dit zal opnieuw worden geprobeerd bij de volgende scan."
            )

        embed = discord.Embed(
            title="✅ Aangemeld voor Bedrijven bonus check",
            description=(
                f"Je ontvangt voortaan een DM als een van je bedrijven "
                f"**0% productiebonus** heeft.\n\n"
                f"**In-game naam:** {spelersnaam}{resolved_note}"
            ),
            colour=discord.Colour.green(),
        )
        embed.set_footer(text="Klik opnieuw op de knop om je af te melden.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info("bedrijven_bonus_check: %s subscribed as %r", discord_username, spelersnaam)


# ── Persistent button & view ─────────────────────────────────────────────────

class BedrijvenBonusCheckButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Bedrijven bonus check",
            style=discord.ButtonStyle.primary,
            custom_id=BUTTON_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        db = getattr(bot, "_ext_db", None)

        if db is None:
            await interaction.response.send_message(
                "❌ Database is momenteel niet beschikbaar. Probeer het later opnieuw.",
                ephemeral=True,
            )
            return

        discord_user_id = str(interaction.user.id)

        if await db.is_company_bonus_watcher(discord_user_id):
            # Already subscribed → unsubscribe
            await db.remove_company_bonus_watcher(discord_user_id)
            await db.delete_all_alerts_for_user(discord_user_id)
            await interaction.response.send_message(
                "✅ Je bent afgemeld van Bedrijven bonus check-meldingen.",
                ephemeral=True,
            )
            logger.info("bedrijven_bonus_check: %s unsubscribed", interaction.user)
        else:
            # Not yet subscribed → show modal to collect in-game name
            await interaction.response.send_modal(SpelernaamModal())


class BedrijvenBonusCheckView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(BedrijvenBonusCheckButton())


# ── Cog ──────────────────────────────────────────────────────────────────────

class BedrijvenBonusCheckCog(CommandCogBase, name="bedrijven_bonus_check"):
    """Persistent button UI and owner commands for the production-bonus monitor."""

    def __init__(self, bot) -> None:
        self.bot = bot
        # Register the persistent view so it survives restarts
        bot.add_view(BedrijvenBonusCheckView())

    # ── Owner prefix commands ─────────────────────────────────────────────────

    @commands.command(
        name="bedrijven_bonus_check_post",
        description="Post (of update) de Bedrijven bonus check-knop in het rollen-kanaal.",
        hidden=True,
    )
    @commands.is_owner()
    async def bedrijven_bonus_check_post(self, context: Context) -> None:
        """Owner-only: post or edit the persistent bedrijven bonus check button message."""
        roles_ch_id = self.bot.config.get("channels", {}).get("roles")
        channel = (
            self.bot.get_channel(int(roles_ch_id)) if roles_ch_id else None
        ) or context.channel

        embed = discord.Embed(
            title="🏭 Bedrijven bonus check",
            description=(
                "Wil je een melding ontvangen als een van je bedrijven **0% productiebonus** heeft?\n\n"
                "Klik op de knop hieronder om je aan te melden. "
                "Je in-game gebruikersnaam wordt gevraagd via een popup.\n\n"
                "Klik nogmaals op de knop om je weer af te melden."
            ),
            colour=discord.Colour.blue(),
        )

        view = BedrijvenBonusCheckView()
        state = _load_state(self.bot.testing)
        msg_id = state.get("button_message_id")
        msg = None

        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=view)
            except (discord.NotFound, discord.HTTPException):
                msg = None

        if msg is None:
            msg = await channel.send(embed=embed, view=view)

        state["button_message_id"] = msg.id
        _save_state(state, self.bot.testing)
        await context.send(f"✅ Bedrijven bonus check-bericht geplaatst in {channel.mention}.", delete_after=10)

    @commands.command(
        name="bedrijven_bonus_check_check",
        description="Voer de bedrijven bonus check-scan direct uit (owner-only).",
        hidden=True,
    )
    @commands.is_owner()
    async def bedrijven_bonus_check_check(self, context: Context) -> None:
        """Owner-only: immediately run the company bonus scan."""
        task_cog = self.bot.cogs.get("company_bonus_tasks")
        if task_cog is None:
            await context.send("❌ Company bonus task cog is niet geladen.")
            return
        await context.send("⏳ Bedrijven bonus check-scan wordt uitgevoerd...")
        try:
            await task_cog._run_scan()
            await context.send("✅ Scan voltooid.")
        except Exception as exc:
            logger.exception("bedrijven_bonus_check_check: scan mislukt")
            await context.send(f"❌ Scan mislukt: {exc}")

    @commands.command(
        name="bedrijven_bonus_check_lijst",
        description="Toon alle gebruikers die aangemeld zijn voor Bedrijven bonus check.",
        hidden=True,
    )
    @commands.is_owner()
    async def bedrijven_bonus_check_lijst(self, context: Context) -> None:
        """Owner-only: print all active bedrijven bonus check subscriptions."""
        db = self._db
        if db is None:
            await context.send("Database niet beschikbaar.")
            return

        watchers = await db.get_all_company_bonus_watchers()
        if not watchers:
            await context.send("Geen gebruikers aangemeld voor Bedrijven bonus check.")
            return

        lines: list[str] = [f"**Bedrijven bonus check abonnees ({len(watchers)}):**\n"]
        for w in watchers:
            resolved = w["game_user_id"] or "*(nog niet opgelost)*"
            lines.append(
                f"• {w['discord_username']} — in-game: **{w['game_username']}** "
                f"(ID: `{resolved}`) — aangemeld: {w['added_at'][:10]}"
            )

        message = "\n".join(lines)
        for chunk_start in range(0, len(message), 1900):
            await context.send(message[chunk_start : chunk_start + 1900])


async def setup(bot) -> None:
    await bot.add_cog(BedrijvenBonusCheckCog(bot))
