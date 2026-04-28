"""
Owner and administrator commands.

Prefix commands (owner-only unless noted):
  !sync / !unsync (scope)       — sync or remove slash commands globally or for a guild
  !uptime                       — show how long the bot has been online
  !load / !unload / !reload (cog) — hot-reload individual cog modules
  !clearluck                    — clear the luck-score cache
  !congres_analyse              — generate a congressional analysis report (now: /congres-analyse)
  !shutdown                     — gracefully shut down the bot
  !restart                      — restart the bot process in-place
  !say (message)                — make the bot send a message
  /purge (amount)               — delete messages in bulk (requires manage_messages)
"""

import os
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from utils.checks import has_privileged_role


class Owner(commands.Cog, name="owner"):
    """Cog for owner-only commands like syncing slash commands, checking uptime, loading/unloading cogs, and other administrative tasks."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.color = int(
            self.bot.config.get("colors", {}).get("primary", "0x154273"), 16
        )

    @commands.command(
        name="sync",
        description="Synchroniseert de slash-commands.",
    )
    @app_commands.describe(
        scope="Het bereik van de sync. Kan `global` of `guild` zijn."
    )
    @commands.is_owner()
    async def sync(self, context: Context, scope: str) -> None:
        """
        Synchronizes the slash commands.

        :param context: The command context.
        :param scope: The scope of the sync. Can be `global` or `guild`.
        """

        if scope == "global":
            await context.bot.tree.sync()
            context.bot._last_sync_at = datetime.now(timezone.utc)
            context.bot._last_sync_scope = "global (handmatig)"
            embed = discord.Embed(
                description="Slash-commands zijn globaal gesynchroniseerd.",
                color=self.color,
            )
            await context.send(embed=embed)
            return
        elif scope == "guild":
            context.bot.tree.copy_global_to(guild=context.guild)
            await context.bot.tree.sync(guild=context.guild)
            context.bot._last_sync_at = datetime.now(timezone.utc)
            context.bot._last_sync_scope = f"guild:{context.guild.id} (handmatig)"
            embed = discord.Embed(
                description="Slash-commands zijn gesynchroniseerd in deze server.",
                color=self.color,
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description="De scope moet `global` of `guild` zijn.", color=self.color
        )
        await context.send(embed=embed)

    @app_commands.command(
        name="lastsync",
        description="Toon wanneer de slash-commands voor het laatst gesynchroniseerd zijn.",
    )
    async def lastsync(self, interaction: discord.Interaction) -> None:
        last_at: datetime | None = getattr(self.bot, "_last_sync_at", None)
        last_scope: str = getattr(self.bot, "_last_sync_scope", "onbekend")

        if last_at is None:
            embed = discord.Embed(
                description="De slash-commands zijn nog niet gesynchroniseerd in deze sessie.",
                color=self.color,
            )
        else:
            ts = int(last_at.timestamp())
            embed = discord.Embed(
                title="🔄 Laatste slash-command sync",
                color=self.color,
            )
            embed.add_field(name="Tijdstip", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
            embed.add_field(name="Scope", value=last_scope, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(
        name="unsync",
        description="Desynchroniseert de slash-commando's.",
    )
    @app_commands.describe(
        scope="Het bereik. Kan `global`, `current_guild` of `guild` zijn."
    )
    @commands.is_owner()
    async def unsync(self, context: Context, scope: str) -> None:
        """
        Unsynchonizes the slash commands.

        :param context: The command context.
        :param scope: The scope of the sync. Can be `global`, `current_guild` or `guild`.
        """

        if scope == "global":
            context.bot.tree.clear_commands(guild=None)
            await context.bot.tree.sync()
            embed = discord.Embed(
                description="Slash-commands zijn globaal gedesynchroniseerd.",
                color=self.color,
            )
            await context.send(embed=embed)
            return
        elif scope == "guild":
            context.bot.tree.clear_commands(guild=context.guild)
            await context.bot.tree.sync(guild=context.guild)
            embed = discord.Embed(
                description="Slash-commands zijn gedesynchroniseerd in deze server.",
                color=self.color,
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description="De scope moet `global` of `guild` zijn.", color=self.color
        )
        await context.send(embed=embed)

    @commands.command(
        name="uptime", description="Controleer hoe lang de bot al online is."
    )
    @commands.is_owner()
    async def uptime(self, context: Context) -> None:
        """
        Check the bot's uptime.

        :param context: The command context.
        """
        start_time = self.bot.start_time
        uptime_seconds = int((discord.utils.utcnow() - start_time).total_seconds())
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_string = f"{hours}h {minutes}m {seconds}s"
        embed = discord.Embed(
            title="Bot online-tijd",
            description=f"De bot is {uptime_string} online.",
            color=self.color,
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="load",
        description="Laad een module.",
    )
    @app_commands.describe(cog="De naam van de module om te laden")
    @commands.is_owner()
    async def load(self, context: Context, cog: str) -> None:
        """
        The bot will load the given cog.

        :param context: The hybrid command context.
        :param cog: The name of the cog to load.
        """
        try:
            await self.bot.load_extension(f"cogs.{cog}")
        except Exception:
            embed = discord.Embed(
                description=f"Kon de `{cog}` module niet laden.", color=self.color
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description=f"De `{cog}` module is succesvol geladen.", color=self.color
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="unload",
        description="Verwijder een module.",
    )
    @app_commands.describe(cog="De naam van de module om te verwijderen")
    @commands.is_owner()
    async def unload(self, context: Context, cog: str) -> None:
        """
        The bot will unload the given cog.

        :param context: The hybrid command context.
        :param cog: The name of the cog to unload.
        """
        try:
            await self.bot.unload_extension(f"cogs.{cog}")
        except Exception:
            embed = discord.Embed(
                description=f"Kon de `{cog}` module niet verwijderen.", color=self.color
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description=f"De `{cog}` module is succesvol verwijderd.", color=self.color
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="reload",
        description="Herlaad een module.",
    )
    @app_commands.describe(cog="De naam van de module om te herladen")
    @commands.is_owner()
    async def reload(self, context: Context, cog: str) -> None:
        """
        The bot will reload the given cog.

        :param context: The hybrid command context.
        :param cog: The name of the cog to reload.
        """
        try:
            await self.bot.reload_extension(f"cogs.{cog}")
        except Exception:
            embed = discord.Embed(
                description=f"Kon de `{cog}` module niet herladen.", color=self.color
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description=f"De `{cog}` module is succesvol herladen.", color=self.color
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="restart",
        description="Herstart het bot-proces volledig.",
    )
    @commands.is_owner()
    async def restart(self, context: Context) -> None:
        """Restart the bot by re-executing the current process."""
        embed = discord.Embed(
            description="De bot wordt herstart. Even geduld... :arrows_counterclockwise:",
            color=self.color,
        )
        await context.send(embed=embed)
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    @commands.hybrid_command(
        name="shutdown",
        description="Zet de bot uit.",
    )
    @commands.is_owner()
    async def shutdown(self, context: Context) -> None:
        """Gracefully shut down the bot."""
        embed = discord.Embed(
            description="De bot wordt afgesloten. Tot ziens! :wave:", color=self.color
        )
        await context.send(embed=embed)
        await self.bot.close()

    @commands.hybrid_command(
        name="say",
        description="De bot herhaalt wat je invoert.",
    )
    @app_commands.describe(message="Het bericht dat de bot moet herhalen")
    @commands.is_owner()
    async def say(self, context: Context, *, message: str) -> None:
        """
        The bot will say anything you want.

        :param context: The hybrid command context.
        :param message: The message that should be repeated by the bot.
        """
        # Prevent @everyone and @here pings even if the owner accidentally includes them
        sanitized = message.replace("@everyone", "@​everyone").replace("@here", "@​here")
        await context.send(
            sanitized,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False),
        )

    @commands.hybrid_command(
        name="purge",
        description="Delete a number of messages.",
    )
    @commands.has_guild_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    @app_commands.describe(
        amount="The amount of messages that should be deleted (max 200)."
    )
    async def purge(self, context: Context, amount: int) -> None:
        """
        Delete a number of messages.

        :param context: The hybrid command context.
        :param amount: The number of messages that should be deleted.
        """
        amount = max(1, min(amount, 200))  # clamp to [1, 200]
        await context.send(
            "Deleting messages..."
        )  # Bit of a hacky way to make sure the bot responds to the interaction and doens't get a "Unknown Interaction" response
        purged_messages = await context.channel.purge(limit=amount + 1)
        embed = discord.Embed(
            description=f"**{context.author}** cleared **{len(purged_messages) - 1}** messages!",
            color=0xBEBEFE,
        )
        await context.channel.send(embed=embed)

    @app_commands.command(
        name="congres-analyse",
        description="Analyseer de congresleden en hun stemgedrag vanaf een gegeven datum.",
    )
    @app_commands.describe(
        datum="Startdatum in formaat DD-MM-JJJJ (bijv. 07-02-2026). Laat leeg voor 7 februari 2026."
    )
    async def congres_analyse(
        self, interaction: discord.Interaction, datum: str = "07-02-2026"
    ) -> None:
        """Count messages/votes from each congress member in the congress channels since a given date."""
        from collections import Counter, defaultdict
        from statistics import mean, median

        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ Alleen de bot-eigenaar kan dit gebruiken.", ephemeral=True
            )
            return

        # ── Parse date ────────────────────────────────────────────────────
        start_time: datetime | None = None
        for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                start_time = datetime.strptime(datum.strip(), fmt)
                break
            except ValueError:
                continue
        if start_time is None:
            await interaction.response.send_message(
                f"❌ Ongeldig datumformaat `{datum}`. Gebruik DD-MM-JJJJ, bijv. `07-02-2026`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        def _status_embed(description: str) -> discord.Embed:
            return discord.Embed(description=description, color=self.color)

        channel_ids = self.bot.config.get("channels", {})
        congres_channel_id = channel_ids.get("congres")
        if not congres_channel_id:
            await interaction.followup.send("❌ `congres` channel niet geconfigureerd.", ephemeral=True)
            return

        assert interaction.guild is not None
        congress_role = interaction.guild.get_role(1451181300009537547)
        date_label = start_time.strftime("%-d %B %Y")

        # ── Single status message in the channel, edited at every step ─────
        assert interaction.channel is not None
        status_msg = await interaction.followup.send(
            embed=_status_embed("⏳ **Stap 1/3** — Congres kanaal wordt geanalyseerd..."),
            wait=True,
        )

        # ── Per-user tracking across all congress channels ────────────────
        user_congres_msgs: Counter[int] = Counter()
        user_debat_msgs: Counter[int] = Counter()
        # days on which the member sent ≥1 message in any congress channel
        user_days: defaultdict[int, set[str]] = defaultdict(set)
        # debate thread IDs in which the member sent ≥1 message
        user_debates: defaultdict[int, set[int]] = defaultdict(set)
        # messages per calendar day (across both channels)
        user_msgs_per_day: defaultdict[int, Counter] = defaultdict(Counter)
        # messages per debate thread
        user_msgs_per_debate: defaultdict[int, Counter] = defaultdict(Counter)

        def _is_congress_member(author) -> bool:
            return (
                isinstance(author, discord.Member)
                and not author.bot
                and congress_role in author.roles
            )

        # ── Step 1: congres channel ───────────────────────────────────────
        async for message in self.bot.get_channel(congres_channel_id).history(
            limit=None, after=start_time
        ):
            if not _is_congress_member(message.author):
                continue
            uid = message.author.id
            day = message.created_at.strftime("%Y-%m-%d")
            user_congres_msgs[uid] += 1
            user_days[uid].add(day)
            user_msgs_per_day[uid][day] += 1

        # ── Step 2: debat forum ───────────────────────────────────────────
        debate_channel_id = channel_ids.get("debat")
        if not debate_channel_id:
            await status_msg.edit(embed=_status_embed("❌ `debat` channel niet geconfigureerd."))
            return

        await status_msg.edit(
            embed=_status_embed("⏳ **Stap 2/3** — Debat kanaal (actieve + gesloten threads) wordt geanalyseerd...")
        )

        debat_channel = self.bot.get_channel(debate_channel_id)
        all_threads = list(debat_channel.threads)
        async for thread in debat_channel.archived_threads(limit=None):
            all_threads.append(thread)

        for thread in all_threads:
            async for message in thread.history(limit=None, after=start_time):
                if not _is_congress_member(message.author):
                    continue
                uid = message.author.id
                day = message.created_at.strftime("%Y-%m-%d")
                user_debat_msgs[uid] += 1
                user_days[uid].add(day)
                user_debates[uid].add(thread.id)
                user_msgs_per_day[uid][day] += 1
                user_msgs_per_debate[uid][thread.id] += 1

        # ── Build combined activity embed(s) per member ───────────────────
        all_users = set(user_congres_msgs.keys()) | set(user_debat_msgs.keys())
        sorted_users = sorted(
            all_users,
            key=lambda u: user_congres_msgs[u] + user_debat_msgs[u],
            reverse=True,
        )

        activity_lines: list[str] = []
        for uid in sorted_users:
            total = user_congres_msgs[uid] + user_debat_msgs[uid]
            n_days = len(user_days[uid])
            n_debates = len(user_debates[uid])

            day_counts = list(user_msgs_per_day[uid].values())
            avg_day = mean(day_counts) if day_counts else 0.0
            med_day = median(day_counts) if day_counts else 0.0

            debate_counts = list(user_msgs_per_debate[uid].values())
            avg_debate = mean(debate_counts) if debate_counts else 0.0
            med_debate = median(debate_counts) if debate_counts else 0.0

            per_debate_str = (
                f" | {avg_debate:.1f}/debat (med. {med_debate:.1f})"
                if n_debates > 0
                else ""
            )
            activity_lines.append(
                f"<@{uid}> — **{total}** berichten"
                f" ({user_congres_msgs[uid]}🏛️ + {user_debat_msgs[uid]}🗣️)\n"
                f"📅 {n_days} actieve dagen  |  🗣️ {n_debates} debatten aanwezig\n"
                f"📊 {avg_day:.1f}/dag (med. {med_day:.1f}){per_debate_str}"
            )

        if not activity_lines:
            activity_lines = ["*Geen activiteit gevonden.*"]

        assert interaction.channel is not None
        # Send in chunks (≤3800 chars per embed description)
        _MAX = 3800
        embed_chunks: list[str] = []
        current_chunk = ""
        for line in activity_lines:
            segment = ("\n\n" if current_chunk else "") + line
            if len(current_chunk) + len(segment) > _MAX:
                embed_chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += segment
        if current_chunk:
            embed_chunks.append(current_chunk)

        for i, chunk in enumerate(embed_chunks):
            title = f"Congres Activiteitsanalyse — {date_label}"
            if len(embed_chunks) > 1:
                title += f" ({i + 1}/{len(embed_chunks)})"
            await interaction.channel.send(embed=discord.Embed(  # type: ignore[union-attr]
                title=title,
                description=chunk,
                color=self.color,
            ))

        # ── Step 3: stembureau reactions ──────────────────────────────────
        stembureau_channel_id = channel_ids.get("stembureau")
        if not stembureau_channel_id:
            await status_msg.edit(embed=_status_embed("❌ `stembureau` channel niet geconfigureerd."))
            return

        await status_msg.edit(
            embed=_status_embed("⏳ **Stap 3/3** — Stembureau kanaal (reacties) wordt geanalyseerd...")
        )

        vote_count: Counter[int] = Counter()
        async for message in self.bot.get_channel(stembureau_channel_id).history(
            limit=None, after=start_time
        ):
            users_counted: list[int] = []
            for reaction in message.reactions:
                async for user in reaction.users():
                    if not isinstance(user, discord.Member):
                        continue
                    if user.bot or congress_role not in user.roles:
                        continue
                    if user.id in users_counted:
                        continue
                    users_counted.append(user.id)
                    vote_count[user.id] += 1

        results = "\n".join(
            [f"<@{user_id}>: {count}" for user_id, count in vote_count.most_common()]
        ) or "*Geen stemmen gevonden.*"
        await interaction.channel.send(embed=discord.Embed(  # type: ignore[union-attr]
            title="Stembureau Analyse",
            description=f"Votes in de stembureau channel sinds {date_label}:\n{results}",
            color=self.color,
        ))

        await status_msg.edit(embed=_status_embed("✅ Analyse voltooid!"))

    # @commands.hybrid_command(
    #     name="embed",
    #     description="The bot will say anything you want, but within embeds.",
    # )
    # @app_commands.describe(message="The message that should be repeated by the bot")
    # @commands.is_owner()
    # async def embed(self, context: Context, *, message: str) -> None:
    #     """
    #     The bot will say anything you want, but using embeds.

    #     :param context: The hybrid command context.
    #     :param message: The message that should be repeated by the bot.
    #     """
    #     embed = discord.Embed(description=message, color=0xBEBEFE)
    #     await context.send(embed=embed)


    @app_commands.command(
        name="nl-niet-op-discord",
        description="Toont Nederlandse in-game spelers (level ≥15) die niet gelinkt zijn aan Discord.",
    )
    @app_commands.describe(min_level="Minimaal level (standaard 15)")
    @has_privileged_role()
    async def nl_niet_op_discord(
        self, interaction: discord.Interaction, min_level: int = 15
    ) -> None:
        """List NL in-game citizens (level ≥ min_level) with no Discord identity link."""
        await interaction.response.defer(ephemeral=True)

        db = getattr(self.bot, "_ext_db", None)
        if not db:
            await interaction.followup.send("❌ Database niet beschikbaar.", ephemeral=True)
            return

        nl_country_id: str = self.bot.config.get("nl_country_id", "6813b6d446e731854c7ac7a0")

        rows = await db.get_citizens_without_discord_link(nl_country_id, min_level)

        if not rows:
            await interaction.followup.send(
                f"✅ Alle Nederlandse spelers (level ≥ {min_level}) zijn gelinkt aan Discord.",
                ephemeral=True,
            )
            return

        # Build paginated output (≤1900 chars per message)
        lines = [
            f"• [{name}](https://app.warera.io/user/{uid}) — level {lvl}"
            for uid, name, lvl in rows
        ]
        header = f"**Nederlandse spelers level ≥{min_level} zonder Discord-koppeling ({len(rows)} totaal):**\n"
        chunks: list[str] = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)

        await interaction.followup.send(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @commands.command(
        name="apioffline",
        description="Simuleer de API als offline of herstel de verbinding (test mode).",
    )
    @commands.is_owner()
    async def apioffline(self, context: Context, state: str) -> None:
        """Toggle API offline simulation for testing fallback behaviour.

        Usage: !apioffline on   — null the shared client so all commands see the API as offline
               !apioffline off  — restore the saved client
        """
        state = state.strip().lower()
        if state == "on":
            if getattr(self.bot, "_force_api_offline", False):
                await context.send("⚠️ API offline-modus is al actief.")
                return
            # Save the real client and null it out so CommandCogBase sees None
            self.bot._saved_ext_client = getattr(self.bot, "_ext_client", None)
            self.bot._ext_client = None
            self.bot._force_api_offline = True
            await context.send(
                "🔌 **API offline-modus ingeschakeld.** Alle API-afhankelijke commando's "
                "zullen nu de offline-melding tonen. Gebruik `!apioffline off` om te herstellen."
            )
        elif state == "off":
            if not getattr(self.bot, "_force_api_offline", False):
                await context.send("✅ API offline-modus is niet actief.")
                return
            # Restore the saved client
            saved = getattr(self.bot, "_saved_ext_client", None)
            self.bot._ext_client = saved
            self.bot._force_api_offline = False
            self.bot._saved_ext_client = None
            await context.send("✅ **API offline-modus uitgeschakeld.** Verbinding hersteld.")
        else:
            await context.send("❌ Gebruik: `!apioffline on` of `!apioffline off`")


async def setup(bot) -> None:
    """Add the Owner cog to the bot."""
    await bot.add_cog(Owner(bot))
