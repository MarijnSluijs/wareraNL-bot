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

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from utils.checks import PRIVILEGED_ROLE_IDS, has_privileged_role, is_owner_or_admin


async def _owner_or_privileged(ctx: Context) -> bool:
    if await ctx.bot.is_owner(ctx.author):
        return True
    return isinstance(ctx.author, discord.Member) and bool(
        {r.id for r in ctx.author.roles} & PRIVILEGED_ROLE_IDS
    )


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

    @app_commands.command(
        name="reembed",
        description="Kopieer embeds uit een bericht naar dit kanaal. Gebruik dit commando IN het doelkanaal.",
    )
    @app_commands.describe(
        message_id="ID van het bericht met de embeds (rechtsklik → Kopieer bericht-ID).",
        source_channel="Het kanaal waar het originele bericht staat.",
    )
    @has_privileged_role()
    async def reembed(
        self,
        interaction: discord.Interaction,
        message_id: str,
        source_channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.followup.send("❌ Ongeldig bericht-ID.", ephemeral=True)
            return

        try:
            msg = await source_channel.fetch_message(msg_id)
        except discord.NotFound:
            await interaction.followup.send(
                f"❌ Bericht `{msg_id}` niet gevonden in {source_channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Geen toegang tot {source_channel.mention}.",
                ephemeral=True,
            )
            return

        if not msg.embeds:
            await interaction.followup.send("❌ Dat bericht bevat geen embeds.", ephemeral=True)
            return

        dest = interaction.channel
        for embed in msg.embeds:
            await dest.send(embed=embed)  # type: ignore[union-attr]

        await interaction.followup.send(
            f"✅ {len(msg.embeds)} embed(s) gekopieerd.", ephemeral=True
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
        datum="Startdatum in formaat DD-MM-JJJJ (bijv. 07-02-2026). Laat leeg voor 7 februari 2026.",
        met_reacties="Reacties tellen (standaard: ja). Zet op nee voor een snellere analyse zonder reacties.",
    )
    @is_owner_or_admin()
    async def congres_analyse(
        self, interaction: discord.Interaction, datum: str = "07-02-2026", met_reacties: bool = True
    ) -> None:
        """Count messages/votes from each congress member in the congress channels since a given date."""
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

        from collections import Counter, defaultdict
        from statistics import mean, median

        # ── Single status message in the channel, edited at every step ─────
        assert interaction.channel is not None
        reacties_label = "berichten + reacties" if met_reacties else "berichten"
        status_msg = await interaction.followup.send(
            embed=_status_embed(f"⏳ **Stap 1/3** — Congres kanaal wordt geanalyseerd ({reacties_label})..."),
            wait=True,
        )

        try:
            # ── Per-user tracking across all congress channels ────────────────
            user_congres_msgs: Counter[int] = Counter()
            user_debat_msgs: Counter[int] = Counter()
            user_congres_reactions: Counter[int] = Counter()
            user_debat_reactions: Counter[int] = Counter()
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
                if _is_congress_member(message.author):
                    uid = message.author.id
                    day = message.created_at.strftime("%Y-%m-%d")
                    user_congres_msgs[uid] += 1
                    user_days[uid].add(day)
                    user_msgs_per_day[uid][day] += 1
                # Count emoji reactions placed by congress members on any message
                if met_reacties:
                    for reaction in message.reactions:
                        async for user in reaction.users():
                            if _is_congress_member(user):
                                user_congres_reactions[user.id] += 1

            # ── Step 2: debat forum ───────────────────────────────────────────
            debate_channel_id = channel_ids.get("debat")
            if not debate_channel_id:
                await status_msg.edit(embed=_status_embed("❌ `debat` channel niet geconfigureerd."))
                return

            await status_msg.edit(
                embed=_status_embed(f"⏳ **Stap 2/3** — Debat kanaal (actieve + gesloten threads, {reacties_label}) wordt geanalyseerd...")
            )

            debat_channel = self.bot.get_channel(debate_channel_id)
            all_threads = list(debat_channel.threads)
            async for thread in debat_channel.archived_threads(limit=None):
                all_threads.append(thread)

            # Process threads concurrently (max 5 at a time) to avoid sequential slowness
            sem = asyncio.Semaphore(5)

            async def _process_thread(thread: discord.Thread) -> None:
                async with sem:
                    async for message in thread.history(limit=None, after=start_time):
                        if _is_congress_member(message.author):
                            uid = message.author.id
                            day = message.created_at.strftime("%Y-%m-%d")
                            user_debat_msgs[uid] += 1
                            user_days[uid].add(day)
                            user_debates[uid].add(thread.id)
                            user_msgs_per_day[uid][day] += 1
                            user_msgs_per_debate[uid][thread.id] += 1
                        if met_reacties:
                            for reaction in message.reactions:
                                async for user in reaction.users():
                                    if _is_congress_member(user):
                                        user_debat_reactions[user.id] += 1

            await asyncio.gather(*[_process_thread(t) for t in all_threads])

            # ── Build combined activity embed(s) per member ───────────────────
            all_users = set(user_congres_msgs.keys()) | set(user_debat_msgs.keys()) | set(user_congres_reactions.keys()) | set(user_debat_reactions.keys())
            sorted_users = sorted(
                all_users,
                key=lambda u: user_congres_msgs[u] + user_debat_msgs[u],
                reverse=True,
            )

            # Batch-resolve Discord user IDs → in-game names
            _db = getattr(self.bot, "_ext_db", None)
            ingame_names: dict[str, str] = {}
            if _db and all_users:
                try:
                    ingame_names = await _db.get_citizen_names_by_discord_ids(
                        list(all_users)
                    )
                except Exception:
                    pass

            activity_lines: list[str] = []
            for uid in sorted_users:
                total = user_congres_msgs[uid] + user_debat_msgs[uid]
                total_reactions = user_congres_reactions[uid] + user_debat_reactions[uid]
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
                reactions_str = f"  |  👍 {total_reactions} reacties" if total_reactions > 0 else ""
                ingame = ingame_names.get(str(uid))
                mention = f"<@{uid}>" + (f" ({ingame})" if ingame else "")
                activity_lines.append(
                    f"{mention} — **{total}** berichten"
                    f" ({user_congres_msgs[uid]}🏛️ + {user_debat_msgs[uid]}🗣️){reactions_str}\n"
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

            # Emoji legend
            await interaction.channel.send(embed=discord.Embed(  # type: ignore[union-attr]
                description=(
                    "**Legenda** — "
                    "🏛️ congres-kanaal berichten  •  "
                    "🗣️ debat-forum berichten  •  "
                    "� emoji-reacties geplaatst  •  "
                    "�📅 actieve dagen  •  "
                    "🗣️ debatten bijgewoond  •  "
                    "📊 gem./dag (mediaan)"
                ),
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

            # Batch-resolve stembureau voters → in-game names
            vote_ingame: dict[str, str] = {}
            if _db and vote_count:
                try:
                    vote_ingame = await _db.get_citizen_names_by_discord_ids(
                        list(vote_count.keys())
                    )
                except Exception:
                    pass

            results = "\n".join(
                [
                    f"<@{user_id}>"
                    + (f" ({vote_ingame.get(str(user_id), '')})" if vote_ingame.get(str(user_id)) else "")
                    + f": {count}"
                    for user_id, count in vote_count.most_common()
                ]
            ) or "*Geen stemmen gevonden.*"
            await interaction.channel.send(embed=discord.Embed(  # type: ignore[union-attr]
                title="Stembureau Analyse",
                description=f"Votes in de stembureau channel sinds {date_label}:\n{results}",
                color=self.color,
            ))

            await status_msg.edit(embed=_status_embed("✅ Analyse voltooid!"))

        except discord.HTTPException as exc:
            await status_msg.edit(
                embed=_status_embed(
                    f"❌ Discord API tijdelijk niet beschikbaar (HTTP {exc.status}). "
                    "Probeer het later opnieuw."
                )
            )
        except Exception as exc:
            await status_msg.edit(
                embed=_status_embed(
                    f"❌ Onverwachte fout: `{type(exc).__name__}: {exc}`"
                )
            )

    @app_commands.command(
        name="wakkerdam-analyse",
        description="Analyseer het aantal woorden per speler in het wakkerdam-kanaal.",
    )
    @app_commands.describe(
        start="Startdatum en -tijd (formaat: DD-MM-JJJJ HH:MM), bijv. 01-05-2026 09:00",
        eind="Einddatum en -tijd (formaat: DD-MM-JJJJ HH:MM), bijv. 04-05-2026 23:59",
    )
    @has_privileged_role()
    async def wakkerdam_analyse(
        self,
        interaction: discord.Interaction,
        start: str,
        eind: str,
    ) -> None:
        """Count words per player in the wakkerdam channel between start and end datetime."""
        _WAKKERDAM_CHANNEL_ID = 1499377427460263946

        def _parse_dt(s: str) -> datetime | None:
            for fmt in (
                "%d-%m-%Y %H:%M",
                "%d-%m-%Y %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M",
            ):
                try:
                    return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            return None

        start_dt = _parse_dt(start)
        if start_dt is None:
            await interaction.response.send_message(
                f"❌ Ongeldig startdatumformaat `{start}`. Gebruik DD-MM-JJJJ HH:MM, bijv. `01-05-2026 09:00`.",
                ephemeral=True,
            )
            return

        end_dt = _parse_dt(eind)
        if end_dt is None:
            await interaction.response.send_message(
                f"❌ Ongeldig einddatumformaat `{eind}`. Gebruik DD-MM-JJJJ HH:MM, bijv. `04-05-2026 23:59`.",
                ephemeral=True,
            )
            return

        if end_dt <= start_dt:
            await interaction.response.send_message(
                "❌ Einddatum moet na de startdatum liggen.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        channel = self.bot.get_channel(_WAKKERDAM_CHANNEL_ID)
        if channel is None:
            await interaction.followup.send(
                f"❌ Kanaal `{_WAKKERDAM_CHANNEL_ID}` niet gevonden.", ephemeral=True
            )
            return

        def _status_embed(description: str) -> discord.Embed:
            return discord.Embed(description=description, color=self.color)

        assert interaction.channel is not None
        status_msg = await interaction.followup.send(
            embed=_status_embed("⏳ Wakkerdam-kanaal wordt geanalyseerd..."),
            wait=True,
        )

        try:
            from collections import Counter

            user_words: Counter[int] = Counter()
            user_names: dict[int, str] = {}

            async for message in channel.history(limit=None, after=start_dt, before=end_dt):
                if message.author.bot:
                    continue
                word_count = len(message.content.split())
                if word_count == 0:
                    continue
                uid = message.author.id
                user_words[uid] += word_count
                if uid not in user_names:
                    user_names[uid] = message.author.display_name

            # Batch-resolve Discord IDs → in-game names
            _db = getattr(self.bot, "_ext_db", None)
            ingame_names: dict[str, str] = {}
            if _db and user_words:
                try:
                    ingame_names = await _db.get_citizen_names_by_discord_ids(
                        list(user_words.keys())
                    )
                except Exception:
                    pass

            start_label = start_dt.strftime("%-d %b %Y %H:%M")
            end_label = end_dt.strftime("%-d %b %Y %H:%M")

            if not user_words:
                await interaction.channel.send(embed=discord.Embed(  # type: ignore[union-attr]
                    title="📊 Wakkerdam Analyse",
                    description=f"*Geen berichten gevonden tussen {start_label} en {end_label}.*",
                    color=self.color,
                ))
                await status_msg.edit(embed=_status_embed("✅ Analyse voltooid (geen berichten)."))
                return

            lines: list[str] = []
            for uid, words in user_words.most_common():
                ingame = ingame_names.get(str(uid))
                display = user_names.get(uid, str(uid))
                mention = f"<@{uid}>" + (f" ({ingame})" if ingame else f" ({display})")
                lines.append(f"{mention} — **{words}** woorden")

            _MAX = 3800
            embed_chunks: list[str] = []
            current_chunk = ""
            for line in lines:
                segment = ("\n" if current_chunk else "") + line
                if len(current_chunk) + len(segment) > _MAX:
                    embed_chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += segment
            if current_chunk:
                embed_chunks.append(current_chunk)

            for i, chunk in enumerate(embed_chunks):
                title = f"📊 Wakkerdam Analyse — {start_label} t/m {end_label}"
                if len(embed_chunks) > 1:
                    title += f" ({i + 1}/{len(embed_chunks)})"
                await interaction.channel.send(embed=discord.Embed(  # type: ignore[union-attr]
                    title=title,
                    description=chunk,
                    color=self.color,
                ))

            await status_msg.edit(embed=_status_embed("✅ Analyse voltooid!"))

        except discord.HTTPException as exc:
            await status_msg.edit(
                embed=_status_embed(
                    f"❌ Discord API tijdelijk niet beschikbaar (HTTP {exc.status}). "
                    "Probeer het later opnieuw."
                )
            )
        except Exception as exc:
            await status_msg.edit(
                embed=_status_embed(f"❌ Onverwachte fout: `{type(exc).__name__}: {exc}`")
            )

    @commands.command(name="rollen_check", hidden=True)
    @commands.check(_owner_or_privileged)
    async def rollen_check(self, context: Context) -> None:
        """Compare in-game government/congress with Discord role holders and report discrepancies."""
        db = getattr(self.bot, "_ext_db", None)
        client = getattr(self.bot, "_ext_client", None)
        guild = context.guild

        if not db or not client or not guild:
            await context.send("❌ DB, API-client of guild niet beschikbaar.")
            return

        status = await context.send("⏳ Ophalen van overheidsdata…")

        nl_country_id: str = self.bot.config.get("nl_country_id", "6813b6d446e731854c7ac7a0")
        roles_cfg: dict = self.bot.config.get("roles", {})

        # ── Role IDs ──────────────────────────────────────────────────────────
        ROLE_PRESIDENT         = int(roles_cfg.get("president", 0))
        ROLE_VICE_PRESIDENT    = int(roles_cfg.get("vice_president", 0))
        ROLE_GOVERNMENT        = int(roles_cfg.get("government", 0))
        ROLE_CONGRESLID        = int(roles_cfg.get("congreslid", 0))

        # ── Step 1: fetch in-game government ─────────────────────────────────
        try:
            results = await client.batch_get(
                "government.getByCountryId",
                [{"countryId": nl_country_id}],
            )
            gov = results[0] if results else None
        except Exception as exc:
            await status.edit(content=f"❌ API-fout: {exc}")
            return

        if not isinstance(gov, dict):
            await status.edit(content="❌ API gaf geen geldige data terug.")
            return

        ingame_president    = gov.get("president") or ""
        ingame_vp           = gov.get("vicePresident") or ""
        ingame_min_def      = gov.get("minOfDefense") or ""
        ingame_min_eco      = gov.get("minOfEconomy") or ""
        ingame_min_fa       = gov.get("minOfForeignAffairs") or ""
        ingame_congress_ids: list[str] = gov.get("congressMembers") or []

        all_ingame_ids = list({
            id_ for id_ in [
                ingame_president, ingame_vp,
                ingame_min_def, ingame_min_eco, ingame_min_fa,
                *ingame_congress_ids,
            ] if id_
        })

        # ── Step 2: resolve in-game IDs → Discord IDs + citizen names ────────
        guild_id_str = str(guild.id)
        ingame_to_discord: dict[str, str] = await db.get_discord_ids_by_ingame_user_ids(
            guild_id_str, all_ingame_ids
        )

        # citizen names: join identity_links → citizen_levels
        discord_ids_for_names = list(ingame_to_discord.values())
        discord_to_citizen: dict[str, str] = {}
        if discord_ids_for_names:
            discord_to_citizen = await db.get_citizen_names_by_discord_ids(discord_ids_for_names)

        def _resolve(ingame_id: str) -> str:
            """Return 'CitizenName (<@discord_id>)' or 'CitizenName (geen Discord-link)' or ingame_id."""
            if not ingame_id:
                return "—"
            discord_id = ingame_to_discord.get(ingame_id)
            citizen_name = discord_to_citizen.get(discord_id, "") if discord_id else ""
            label = citizen_name or ingame_id
            if discord_id:
                return f"{label} (<@{discord_id}>)"
            return f"{label} *(geen Discord-link)*"

        # ── Step 3: collect Discord members with relevant roles ───────────────
        def _members_with_role(role_id: int) -> list[discord.Member]:
            role = guild.get_role(role_id)
            return role.members if role else []

        discord_presidents   = _members_with_role(ROLE_PRESIDENT)
        discord_vps          = _members_with_role(ROLE_VICE_PRESIDENT)
        discord_governments  = _members_with_role(ROLE_GOVERNMENT)
        discord_congresleden = _members_with_role(ROLE_CONGRESLID)

        # Map discord_id → in-game user id for role holders
        async def _discord_to_ingame(member: discord.Member) -> str | None:
            link = await db.get_identity_link_by_discord(str(member.id), guild_id_str)
            return link["in_game_user_id"] if link else None

        # ── Step 4: build the report ──────────────────────────────────────────
        lines: list[str] = []

        def _check_single(
            title: str, ingame_id: str, discord_members: list[discord.Member]
        ) -> list[str]:
            out = [f"**{title}**"]
            out.append(f"  In-game: {_resolve(ingame_id)}")
            expected_discord = ingame_to_discord.get(ingame_id)
            holder_ids = {str(m.id) for m in discord_members}
            if not discord_members:
                out.append("  Discord-rol: *niemand*")
            else:
                names = ", ".join(
                    f"{discord_to_citizen.get(str(m.id), m.display_name)} (<@{m.id}>)"
                    for m in discord_members
                )
                out.append(f"  Discord-rol: {names}")
            if expected_discord and expected_discord not in holder_ids:
                out.append("  ⚠️ In-game speler heeft de rol **niet**")
            extra = holder_ids - ({expected_discord} if expected_discord else set())
            if extra:
                extra_mentions = ", ".join(f"<@{i}>" for i in extra)
                out.append(f"  ⚠️ Extra rolhouder(s): {extra_mentions}")
            if expected_discord and expected_discord in holder_ids and not extra:
                out.append("  ✅ Klopt")
            return out

        lines += _check_single("President", ingame_president, discord_presidents)
        lines.append("")
        lines += _check_single("Vice-president", ingame_vp, discord_vps)
        lines.append("")

        # Individual minister sections (no separate Discord role — show government-rol status)
        gov_holder_ids = {str(m.id) for m in discord_governments}

        def _minister_section(title: str, ingame_id: str) -> list[str]:
            out = [f"**{title}**"]
            out.append(f"  In-game: {_resolve(ingame_id)}")
            if ingame_id:
                discord_id = ingame_to_discord.get(ingame_id)
                if discord_id:
                    if discord_id in gov_holder_ids:
                        out.append("  Government-rol: ✅")
                    else:
                        out.append("  Government-rol: ⚠️ Mist rol")
                else:
                    out.append("  Government-rol: *(geen Discord-link)*")
            return out

        lines += _minister_section("Minister van Defensie", ingame_min_def)
        lines.append("")
        lines += _minister_section("Minister van Economie", ingame_min_eco)
        lines.append("")
        lines += _minister_section("Minister van Buitenlandse Zaken", ingame_min_fa)
        lines.append("")

        # Government role: president + VP + all ministers should have it
        ingame_gov_ids = {
            id_ for id_ in [ingame_president, ingame_vp, ingame_min_def, ingame_min_eco, ingame_min_fa]
            if id_
        }
        expected_gov_discord = {ingame_to_discord[i] for i in ingame_gov_ids if i in ingame_to_discord}
        lines.append("**Rol 'Government' (president + VP + ministers)**")
        missing_gov = expected_gov_discord - gov_holder_ids
        extra_gov   = gov_holder_ids - expected_gov_discord
        if missing_gov:
            lines.append("  ⚠️ Mist rol: " + ", ".join(f"<@{i}>" for i in missing_gov))
        if extra_gov:
            lines.append("  ⚠️ Extra: " + ", ".join(f"<@{i}>" for i in extra_gov))
        if not missing_gov and not extra_gov:
            lines.append("  ✅ Klopt")
        lines.append("")

        # Congress comparison
        ingame_congress_set = set(ingame_congress_ids)
        expected_congress_discord = {
            ingame_to_discord[i] for i in ingame_congress_set if i in ingame_to_discord
        }
        congress_holder_ids = {str(m.id) for m in discord_congresleden}
        missing_congress = expected_congress_discord - congress_holder_ids
        extra_congress   = congress_holder_ids - expected_congress_discord

        lines.append(f"**Congresleden** — {len(ingame_congress_ids)} in-game, "
                     f"{len(discord_congresleden)} op Discord")

        if missing_congress:
            lines.append("  ⚠️ Missen Discord-rol:")
            for discord_id in missing_congress:
                citizen = discord_to_citizen.get(discord_id, discord_id)
                lines.append(f"    • {citizen} (<@{discord_id}>)")
        if extra_congress:
            lines.append("  ⚠️ Hebben Discord-rol maar zijn geen congresleden:")
            for discord_id in extra_congress:
                citizen = discord_to_citizen.get(discord_id, discord_id)
                lines.append(f"    • {citizen} (<@{discord_id}>)")

        unlinked_congress = [
            i for i in ingame_congress_ids if i not in ingame_to_discord
        ]
        if unlinked_congress:
            lines.append(f"  ℹ️ {len(unlinked_congress)} congresleden zonder Discord-link (niet traceerbaar)")

        if not missing_congress and not extra_congress:
            lines.append("  ✅ Congresrollen kloppen")

        # ── Send in chunks ────────────────────────────────────────────────────
        _MAX = 3800
        chunks: list[str] = []
        current = ""
        for line in lines:
            segment = ("\n" if current else "") + line
            if len(current) + len(segment) > _MAX:
                chunks.append(current)
                current = line
            else:
                current += segment
        if current:
            chunks.append(current)

        await status.edit(content="✅ Rollen-check klaar:")
        for i, chunk in enumerate(chunks):
            title = "🔍 Rollen-check NL"
            if len(chunks) > 1:
                title += f" ({i + 1}/{len(chunks)})"
            await context.send(embed=discord.Embed(
                title=title,
                description=chunk,
                color=self.color,
            ))

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
        await interaction.response.defer()

        db = getattr(self.bot, "_ext_db", None)
        if not db:
            await interaction.followup.send("❌ Database niet beschikbaar.")
            return

        nl_country_id: str = self.bot.config.get("nl_country_id", "6813b6d446e731854c7ac7a0")

        rows = await db.get_citizens_without_discord_link(nl_country_id, min_level)

        if not rows:
            await interaction.followup.send(
                f"✅ Alle Nederlandse spelers (level ≥ {min_level}) zijn gelinkt aan Discord.",
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

        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    @app_commands.command(
        name="nl-niet-in-mu",
        description="Toont actieve Nederlandse spelers (level ≥20, actief ≤72u) zonder militaire eenheid.",
    )
    @app_commands.describe(min_level="Minimaal level (standaard 20)")
    @has_privileged_role()
    async def nl_niet_in_mu(
        self, interaction: discord.Interaction, min_level: int = 20
    ) -> None:
        """List active NL citizens with level >= min_level, active in last 72h, without an MU (via API)."""
        await interaction.response.defer()

        db = getattr(self.bot, "_ext_db", None)
        client = getattr(self.bot, "_ext_client", None)
        if not db:
            await interaction.followup.send("❌ Database niet beschikbaar.")
            return
        if not client:
            await interaction.followup.send("❌ API client niet beschikbaar.")
            return

        nl_country_id: str = self.bot.config.get("nl_country_id", "6813b6d446e731854c7ac7a0")

        rows = await db.get_active_nl_citizens(
            nl_country_id, min_level=min_level, max_hours_inactive=72
        )

        if not rows:
            await interaction.followup.send(
                f"✅ Geen actieve Nederlandse spelers (level ≥{min_level}, actief ≤72u) gevonden.",
            )
            return

        await interaction.followup.send(
            f"⏳ {len(rows)} spelers ophalen via API, even geduld..."
        )

        sem = asyncio.Semaphore(10)

        async def _check_mu(uid: str, name: str, lvl: int) -> tuple[str, str, int] | None:
            async with sem:
                try:
                    resp = await client.get(
                        "/user.getUserById",
                        params={"input": json.dumps({"userId": uid})},
                    )
                except Exception:
                    return None
            # Unwrap tRPC envelope
            data = resp
            if isinstance(resp, dict):
                for key in ("result", "data"):
                    v = resp.get(key)
                    if isinstance(v, dict):
                        data = v.get("data", v)
                        break
            if not isinstance(data, dict):
                return None
            # No "mu" field (or null/empty) means not in a MU
            mu_val = data.get("mu")
            if not mu_val:
                return (uid, name, lvl)
            return None

        results = await asyncio.gather(*[_check_mu(uid, name, lvl) for uid, name, lvl in rows])
        no_mu = [r for r in results if r is not None]

        if not no_mu:
            await interaction.channel.send(  # type: ignore[union-attr]
                f"✅ Alle actieve Nederlandse spelers (level ≥{min_level}, actief ≤72u) zitten in een MU."
            )
            return

        lines = [
            f"• [{name}](https://app.warera.io/user/{uid}) — level {lvl}"
            for uid, name, lvl in no_mu
        ]
        header = (
            f"**Nederlandse spelers level ≥{min_level} zonder MU "
            f"(actief in laatste 72u, {len(no_mu)} van {len(rows)} totaal):**\n"
        )
        chunks: list[str] = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)

        for chunk in chunks:
            await interaction.channel.send(chunk)  # type: ignore[union-attr]

    @commands.command(
        name="logs",
        description="Stuur de laatste N regels van het logbestand (standaard 30).",
    )
    @commands.is_owner()
    async def logs(self, context: Context, lines: int = 30) -> None:
        """Send the last N lines of logs/discord.log as a Discord message."""
        lines = max(1, min(lines, 200))
        log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "discord.log")
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
        except FileNotFoundError:
            await context.send(f"❌ Logbestand niet gevonden op `{log_path}`.")
            return

        tail = "".join(all_lines[-lines:])
        # Split into ≤1990-char chunks to stay within Discord's 2000-char limit
        chunk_size = 1990
        for i in range(0, len(tail), chunk_size):
            await context.send(f"```\n{tail[i:i + chunk_size]}\n```")

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
    """Add the Owner cog to the bot.

    Owner commands are registered guild-only (not globally) to avoid hitting
    Discord's 100 global slash-command limit.
    """
    guild_id = int(bot.config.get("guild_id") or 0)
    guilds = [discord.Object(id=guild_id)] if guild_id else []
    await bot.add_cog(Owner(bot), guilds=guilds or None)
