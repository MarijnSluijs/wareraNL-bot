"""Jail, bribes, bail and appeals for the Nigerian Scam-Economy.

A scam loss you cannot settle in cash gets you arrested, with the unpaid
amount as your bribe.  This is what stops the Investment Fund being a bunker:
parking every Naira in the fund used to make a failed ``/scam`` completely
free, because losses are capped at what you hold in hand.  Now it just means
you have to withdraw to buy your way out.

Poverty is judged on **cash plus fund**, so somebody sitting on 20.000 in the
fund with an empty wallet is not "broke" and is expected to pay.  Players who
genuinely have almost nothing are never arrested at all — ``/scam`` has to stay
usable as a way back from zero.

Getting out:
    ``/paybribe``     settle it yourself, in cash
    ``/bail @player`` somebody else settles it for you
    ``/appeal``       plead your case publicly and let the room vote
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from nigeria_bot.scam_game import (
    APPEAL_DURATION_SECONDS,
    GAME_CHANNEL_ID,
    GAME_CHANNEL_URL,
    INDIGENT_WEALTH_THRESHOLD,
    is_indigent,
    _EMBED_GOLD,
    _EMBED_GREEN,
    _EMBED_GREY,
    _EMBED_RED,
    _iso,
    _now,
    _parse,
    _require_channel,
    adjust_balance,
    get_jail,
    get_player,
    money,
    release_player,
    total_wealth,
)

logger = logging.getLogger("nigeria_bot.scam_jail")

_DEFENCES = [
    "The money was merely resting in my account.",
    "I was acting under diplomatic immunity.",
    "The alleged victim clearly consented by answering the telephone.",
    "My lawyer advises me not to remember the incident.",
    "This was a cultural exchange programme.",
    "I reject the premise of the investigation.",
    "The transaction was entirely legitimate until the police became involved.",
    "I was not committing fraud. I was conducting experimental diplomacy.",
    "Everything was in order. The paperwork simply has not been invented yet.",
]


# ── Schema ────────────────────────────────────────────────────────────────────

async def setup_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_appeals (
            message_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            closes_at  TEXT NOT NULL,
            resolved   INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_appeal_votes (
            message_id TEXT NOT NULL,
            voter_id   TEXT NOT NULL,
            pardon     INTEGER NOT NULL,
            PRIMARY KEY (message_id, voter_id)
        )
    """)
    await conn.commit()


# ── Appeal voting buttons ─────────────────────────────────────────────────────

class AppealView(discord.ui.View):
    """Pardon / keep-locked-up vote on a public appeal.

    Persistent via static ``custom_id``s; the appeal is resolved from the
    message the button was clicked on, so several appeals can run at once.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _vote(self, interaction: discord.Interaction, pardon: bool) -> None:
        cog = interaction.client.get_cog("scam_jail")
        if cog is None:
            await interaction.response.send_message(
                "❌ The game is not available right now.", ephemeral=True
            )
            return
        await cog.record_vote(interaction, str(interaction.message.id), pardon)

    @discord.ui.button(
        label="Pardon him", emoji="👑",
        style=discord.ButtonStyle.success, custom_id="scam:appeal_pardon",
    )
    async def pardon(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._vote(interaction, True)

    @discord.ui.button(
        label="Keep him locked up", emoji="🚔",
        style=discord.ButtonStyle.danger, custom_id="scam:appeal_keep",
    )
    async def keep(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._vote(interaction, False)


# ── Cog ───────────────────────────────────────────────────────────────────────

class ScamJailCog(commands.Cog, name="scam_jail"):
    def __init__(self, bot: commands.Bot, conn: aiosqlite.Connection) -> None:
        self.bot = bot
        self.conn = conn
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        await setup_schema(self.conn)
        self.jail_tick.start()

    def cog_unload(self) -> None:
        self.jail_tick.cancel()

    # ── release + appeal-closing loop ─────────────────────────────────

    @tasks.loop(minutes=1)
    async def jail_tick(self) -> None:
        try:
            await self._release_due()
            await self._close_due_appeals()
        except Exception:
            logger.exception("scam_jail: tick failed")

    @jail_tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _release_due(self) -> None:
        """Free anybody whose sentence has run out and say so publicly."""
        due: list[str] = []
        async with self.conn.execute(
            "SELECT discord_user_id, jail_until FROM scam_players"
            " WHERE jail_until IS NOT NULL"
        ) as cur:
            async for row in cur:
                if _parse(str(row[1])) <= _now():
                    due.append(str(row[0]))
        if not due:
            return
        channel = self.bot.get_channel(GAME_CHANNEL_ID)
        for uid in due:
            await release_player(self.conn, uid)
            if channel is None:
                continue
            try:
                await channel.send(embed=discord.Embed(
                    title="🚪 RELEASED",
                    description=(
                        f"<@{uid}> has served their sentence and walks free. "
                        "The outstanding bribe has been quietly written off by "
                        "an official who would rather not discuss it."
                    ),
                    colour=_EMBED_GREY,
                ))
            except discord.HTTPException:
                pass

    async def _close_due_appeals(self) -> None:
        rows: list[tuple[str, str, str]] = []
        async with self.conn.execute(
            "SELECT message_id, channel_id, user_id FROM scam_appeals"
            " WHERE resolved = 0 AND closes_at <= ?", (_iso(_now()),),
        ) as cur:
            async for r in cur:
                rows.append((str(r[0]), str(r[1]), str(r[2])))
        for message_id, channel_id, user_id in rows:
            await self._resolve_appeal(message_id, channel_id, user_id)

    # ── /paybribe ─────────────────────────────────────────────────────

    @app_commands.command(
        name="paybribe", description="Pay your outstanding bribe and walk free."
    )
    async def paybribe(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        uid = str(interaction.user.id)
        async with self._lock:
            jail = await get_jail(self.conn, uid)
            if jail is None:
                await interaction.response.send_message(
                    "✅ You are not in custody. Carry on.", ephemeral=True
                )
                return
            player = await get_player(self.conn, uid)
            if player["balance"] < jail["bribe"]:
                short = jail["bribe"] - player["balance"]
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="❌ Not enough cash for the bribe",
                        description=(
                            f"**Bribe:** {money(jail['bribe'])}\n"
                            f"**Cash:** {money(player['balance'])}\n"
                            f"**Short by:** {money(short)}\n\n"
                            + (
                                f"You have {money(player['invested'])} in the "
                                "fund — `/invest withdraw` will free it up."
                                if player["invested"] else
                                "Try `/beg`, or wait for somebody to `/bail` you."
                            )
                        ),
                        colour=_EMBED_RED,
                    ),
                    ephemeral=True,
                )
                return
            await adjust_balance(self.conn, uid, -jail["bribe"], "jail_bribe")
            await self.conn.execute(
                "UPDATE scam_players SET bribes_paid = bribes_paid + ?"
                " WHERE discord_user_id = ?", (jail["bribe"], uid),
            )
            await release_player(self.conn, uid)
            balance = (await get_player(self.conn, uid))["balance"]

        await interaction.response.send_message(embed=discord.Embed(
            title="💸 BRIBE PAID",
            description=(
                f"{interaction.user.mention} has made a **{money(jail['bribe'])}** "
                "contribution to a police charity that does not exist.\n\n"
                "All charges have been dropped and the file has been lost.\n"
                f"Remaining balance: {money(balance)}"
            ),
            colour=_EMBED_GREEN,
        ))

    # ── /bail ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="bail", description="Pay somebody else's bribe and get them out."
    )
    @app_commands.describe(player="The prince you are freeing.")
    async def bail(
        self, interaction: discord.Interaction, player: discord.Member
    ) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        helper_id = str(interaction.user.id)
        target_id = str(player.id)
        if helper_id == target_id:
            await interaction.response.send_message(
                "❌ Bailing yourself out is just `/paybribe`.", ephemeral=True
            )
            return

        async with self._lock:
            jail = await get_jail(self.conn, target_id)
            if jail is None:
                await interaction.response.send_message(
                    f"❌ {player.display_name} is not in custody.", ephemeral=True
                )
                return
            helper = await get_player(self.conn, helper_id)
            if helper["balance"] < jail["bribe"]:
                await interaction.response.send_message(
                    f"❌ Their bribe is {money(jail['bribe'])} and you have "
                    f"{money(helper['balance'])}.",
                    ephemeral=True,
                )
                return
            await adjust_balance(self.conn, helper_id, -jail["bribe"], "bail_paid")
            await self.conn.execute(
                "UPDATE scam_players SET bails_given = bails_given + 1"
                " WHERE discord_user_id = ?", (helper_id,),
            )
            await self.conn.execute(
                "UPDATE scam_players SET bails_received = bails_received + 1"
                " WHERE discord_user_id = ?", (target_id,),
            )
            await release_player(self.conn, target_id)

        await interaction.response.send_message(embed=discord.Embed(
            title="🤝 ROYAL BAILOUT",
            description=(
                f"{interaction.user.mention} has paid {player.mention}'s "
                f"**{money(jail['bribe'])}** bribe.\n\n"
                f"{player.display_name} has been released. No questions have "
                "been asked about where the money came from."
            ),
            colour=_EMBED_GOLD,
        ))

    # ── /appeal ───────────────────────────────────────────────────────

    @app_commands.command(
        name="appeal", description="Plead your case publicly and let the room vote."
    )
    async def appeal(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        uid = str(interaction.user.id)
        async with self._lock:
            jail = await get_jail(self.conn, uid)
            if jail is None:
                await interaction.response.send_message(
                    "✅ You are not in custody. Nothing to appeal.", ephemeral=True
                )
                return
            wealth = await total_wealth(self.conn, uid)
            if not is_indigent(wealth, jail["bribe"]):
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="⚖️ Appeal denied",
                        description=(
                            f"You are worth **{money(wealth)}** and the bribe is "
                            f"only **{money(jail['bribe'])}**. The court takes a "
                            "dim view of wealthy men pleading poverty.\n\n"
                            "Withdraw from the fund and `/paybribe` like "
                            "everybody else."
                        ),
                        colour=_EMBED_RED,
                    ),
                    ephemeral=True,
                )
                return
            async with self.conn.execute(
                "SELECT message_id FROM scam_appeals"
                " WHERE user_id = ? AND resolved = 0", (uid,),
            ) as cur:
                if await cur.fetchone():
                    await interaction.response.send_message(
                        "❌ Your appeal is already before the court.",
                        ephemeral=True,
                    )
                    return

        closes = _now() + timedelta(seconds=APPEAL_DURATION_SECONDS)
        embed = discord.Embed(
            title="⚖️ ROYAL APPEAL",
            description=(
                f"{interaction.user.mention} claims they cannot afford their "
                f"**{money(jail['bribe'])}** bribe.\n\n"
                f"**Current net worth:** {money(wealth)}\n\n"
                f"**Their defence:**\n> _{random.choice(_DEFENCES)}_\n\n"
                f"Voting closes <t:{int(closes.timestamp())}:R>. "
                "If nobody votes, they walk."
            ),
            colour=_EMBED_GOLD,
        )
        await interaction.response.send_message(embed=embed, view=AppealView())
        try:
            message = await interaction.original_response()
            await self.conn.execute(
                "INSERT INTO scam_appeals"
                " (message_id, channel_id, user_id, closes_at, resolved)"
                " VALUES (?, ?, ?, ?, 0)",
                (str(message.id), str(interaction.channel_id), uid, _iso(closes)),
            )
            await self.conn.commit()
        except Exception:
            logger.exception("scam_jail: could not record the appeal")

    async def record_vote(
        self, interaction: discord.Interaction, message_id: str, pardon: bool
    ) -> None:
        async with self.conn.execute(
            "SELECT user_id, resolved FROM scam_appeals WHERE message_id = ?",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "❌ That appeal is no longer being tracked.", ephemeral=True
            )
            return
        if int(row[1]):
            await interaction.response.send_message(
                "❌ The court has already ruled.", ephemeral=True
            )
            return
        if str(row[0]) == str(interaction.user.id):
            await interaction.response.send_message(
                "❌ You cannot vote on your own appeal. Nice try.", ephemeral=True
            )
            return

        await self.conn.execute(
            "INSERT INTO scam_appeal_votes (message_id, voter_id, pardon)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(message_id, voter_id) DO UPDATE SET pardon = excluded.pardon",
            (message_id, str(interaction.user.id), 1 if pardon else 0),
        )
        await self.conn.commit()
        await interaction.response.send_message(
            "👑 Pardon recorded." if pardon else "🚔 Noted — keep him locked up.",
            ephemeral=True,
        )

    async def _resolve_appeal(
        self, message_id: str, channel_id: str, user_id: str
    ) -> None:
        async with self.conn.execute(
            "SELECT SUM(pardon), COUNT(*) FROM scam_appeal_votes WHERE message_id = ?",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
        pardons = int(row[0] or 0)
        total = int(row[1] or 0)
        keeps = total - pardons
        # No votes means nobody cared enough to keep him in — let him out.
        pardoned = total == 0 or pardons >= keeps

        await self.conn.execute(
            "UPDATE scam_appeals SET resolved = 1 WHERE message_id = ?", (message_id,)
        )
        if pardoned:
            await self.conn.execute(
                "UPDATE scam_players SET appeals_won = appeals_won + 1"
                " WHERE discord_user_id = ?", (str(user_id),),
            )
        await self.conn.commit()

        if pardoned:
            await release_player(self.conn, user_id)
            embed = discord.Embed(
                title="👑 PARDONED",
                description=(
                    f"<@{user_id}> walks free.\n\n"
                    f"Votes: **{pardons}** to pardon, **{keeps}** against."
                    + ("\n\nNobody voted at all, which the court has chosen to "
                       "interpret as overwhelming public sympathy." if total == 0
                       else "")
                ),
                colour=_EMBED_GREEN,
            )
        else:
            embed = discord.Embed(
                title="🚔 APPEAL REJECTED",
                description=(
                    f"<@{user_id}> stays where they are.\n\n"
                    f"Votes: **{pardons}** to pardon, **{keeps}** against.\n\n"
                    "They will be released when their sentence runs out — or "
                    "sooner, if somebody finds their conscience and `/bail`s them."
                ),
                colour=_EMBED_RED,
            )
        channel = self.bot.get_channel(int(channel_id))
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot, conn: aiosqlite.Connection) -> ScamJailCog:
    cog = ScamJailCog(bot, conn)
    await bot.add_cog(cog)
    return cog
