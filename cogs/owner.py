"""
Copyright © Krypton 2019-Present - https://github.com/kkrypt0nn (https://krypton.ninja)
Description:
🐍 A simple template to start to code your own and personalized Discord bot in Python

Version: 6.5.0
"""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context


class Owner(commands.Cog, name="owner"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.color = int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16)

    @commands.command(
        name="sync",
        description="Synchroniseert de slash-commands.",
    )
    @app_commands.describe(scope="Het bereik van de sync. Kan `global` of `guild` zijn.")
    @commands.is_owner()
    async def sync(self, context: Context, scope: str) -> None:
        """
        Synchronizes the slash commands.

        :param context: The command context.
        :param scope: The scope of the sync. Can be `global` or `guild`.
        """

        if scope == "global":
            await context.bot.tree.sync()
            embed = discord.Embed(
                description="Slash-commands zijn globaal gesynchroniseerd.",
                color=self.color,
            )
            await context.send(embed=embed)
            return
        elif scope == "guild":
            context.bot.tree.copy_global_to(guild=context.guild)
            await context.bot.tree.sync(guild=context.guild)
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

    @commands.command(name="uptime", description="Controleer hoe lang de bot al online is.")
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

    @commands.command(
        name="pollgeluk",
        description="Ververs de gelukscores voor alle NL burgers direct.",
    )
    @commands.is_owner()
    async def pollgeluk(self, context: Context) -> None:
        poller = self.bot.cogs.get("production_checker")
        if poller is None or not hasattr(poller, "_daily_luck_refresh_sweep"):
            embed = discord.Embed(
                description="❌ De poller cog is niet geladen.", color=self.color
            )
            await context.send(embed=embed)
            return
        if poller._heavy_api_lock.locked():
            embed = discord.Embed(
                description="⏳ Er loopt al een sweep. Wacht tot die klaar is.", color=self.color
            )
            await context.send(embed=embed)
            return
        embed = discord.Embed(
            description="🔄 Gelukscores verversing gestart (cooldown omzeild).",
            color=self.color,
        )
        status_msg = await context.send(embed=embed)

        async def _run():
            from datetime import datetime, timezone
            import time as _time
            nl_country_id = poller.config.get("nl_country_id")
            if not nl_country_id:
                await status_msg.edit(content="❌ `nl_country_id` niet geconfigureerd.", embed=None)
                return
            # Check if the citizen cache has been populated
            try:
                citizens = await poller._db.get_citizens_for_luck_refresh(nl_country_id)
                if not citizens:
                    await status_msg.edit(
                        content="⚠️ Geen burgers gevonden in de cache. Voer eerst `!peil_burgers` uit om de burgercache te vullen.",
                        embed=None,
                    )
                    return
                total_citizens = len(citizens)
            except Exception as exc:
                await status_msg.edit(content=f"❌ Kon burgercache niet lezen: {exc}", embed=None)
                return

            now_utc = datetime.now(timezone.utc)
            _t0 = _time.monotonic()
            _last_progress_update = 0.0

            def _fmt_dur(seconds: float) -> str:
                m, s = divmod(int(seconds), 60)
                return f"{m}m {s}s" if m else f"{seconds:.1f}s"

            async def _progress(processed: int, total: int, recorded: int) -> None:
                nonlocal _last_progress_update
                now = _time.monotonic()
                if processed not in (0, total) and (now - _last_progress_update) < 4.0:
                    return
                _last_progress_update = now
                await status_msg.edit(
                    content=(
                        f"🔄 Gelukssweep bezig... burgers: **{processed}/{total_citizens}**"
                        f" • gescoord: **{recorded}** • duur: **{_fmt_dur(now - _t0)}**"
                    ),
                    embed=None,
                )

            await status_msg.edit(
                content=f"⏳ Verwerken van **0/{total_citizens}** NL burgers • duur: **0.0s**",
                embed=None,
            )

            try:
                async with poller._heavy_api_lock:
                    await poller._daily_luck_refresh_sweep(
                        now_utc,
                        nl_country_id,
                        _t0,
                        progress_cb=_progress,
                    )
            except Exception as exc:
                await status_msg.edit(content=f"❌ Sweep mislukt: {exc}", embed=None)
                return
            _elapsed = _time.monotonic() - _t0
            _m, _s = divmod(int(_elapsed), 60)
            _dur = f"{_m}m {_s}s" if _m else f"{_elapsed:.1f}s"
            await status_msg.edit(
                content=(
                    f"✅ Gelukssweep voltooid • burgers: **{total_citizens}/{total_citizens}**"
                    f" • duur: **{_dur}**"
                ),
                embed=None,
            )

        import asyncio
        asyncio.create_task(_run())

    @commands.command(
        name="clearluck",
        description="Leeg de opgeslagen NL gelukscores zodat je opnieuw kunt pollen.",
    )
    @commands.is_owner()
    async def clearluck(self, context: Context) -> None:
        poller = self.bot.cogs.get("production_checker")
        if poller is None or not getattr(poller, "_db", None):
            embed = discord.Embed(
                description="❌ De poller/DB is niet beschikbaar.", color=self.color
            )
            await context.send(embed=embed)
            return

        nl_country_id = poller.config.get("nl_country_id")
        if not nl_country_id:
            await context.send("❌ `nl_country_id` niet geconfigureerd.")
            return

        try:
            await poller._db.delete_luck_scores_for_country(nl_country_id)
            await poller._db.set_poll_state("luck_ranking_total", "0")
        except Exception as exc:
            await context.send(f"❌ Wissen van gelukscores mislukt: {exc}")
            return

        embed = discord.Embed(
            description=(
                "🧹 NL gelukscores gewist.\n"
                "Gebruik nu `!pollgeluk` om de tabel opnieuw te vullen."
            ),
            color=self.color,
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="shutdown",
        description="Zet de bot uit.",
    )
    @commands.is_owner()
    async def shutdown(self, context: Context) -> None:
        embed = discord.Embed(description="De bot wordt afgesloten. Tot ziens! :wave:", color=self.color)
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
        await context.send(message)

    @commands.hybrid_command(
        name="purge",
        description="Delete a number of messages.",
    )
    @commands.has_guild_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    @app_commands.describe(amount="The amount of messages that should be deleted.")
    async def purge(self, context: Context, amount: int) -> None:
        """
        Delete a number of messages.

        :param context: The hybrid command context.
        :param amount: The number of messages that should be deleted.
        """
        await context.send(
            "Deleting messages..."
        )  # Bit of a hacky way to make sure the bot responds to the interaction and doens't get a "Unknown Interaction" response
        purged_messages = await context.channel.purge(limit=amount + 1)
        embed = discord.Embed(
            description=f"**{context.author}** cleared **{len(purged_messages)-1}** messages!",
            color=0xBEBEFE,
        )
        await context.channel.send(embed=embed)

    @commands.command(
        name="congres-analyse",
        description="Analyseer de congresleden en hun stemgedrag.",
    )
    @commands.is_owner()
    async def congres_analyse(self, context: Context) -> None:
        # count messages from each congress member in congress channel over last 30 days
        from datetime import datetime, timedelta
        from collections import Counter

        channel_ids = self.bot.config.get("channels", {})
        congres_channel_id = channel_ids.get("congres")
        if not congres_channel_id:
            await context.send("❌ `congres` channel niet geconfigureerd.")
            return

        start_time = datetime(2026, 2, 7)  # Get messages from 7 february to today
        message_count = Counter()
        async for message in self.bot.get_channel(congres_channel_id).history(limit=None, after=start_time):
            if message.author.bot:
                continue
            message_count[message.author.id] += 1

        # Send the results
        results = "\n".join([f"<@{user_id}>: {count}" for user_id, count in message_count.most_common()])
        embed = discord.Embed(
            title="Congresleden Analyse",
            description=f"Berichten in de congres channel over de laatste 30 dagen:\n{results}",
            color=self.color,
        )
        await context.send(embed=embed)

        # count messages from each congress member in debate forum over last 30 days
        debate_channel_id = channel_ids.get("debat")
        if not debate_channel_id:
            await context.send("❌ `debat` channel niet geconfigureerd.")
            return

        message_count = Counter()
        # this is a forum channel so we can't use history
        for thread in self.bot.get_channel(debate_channel_id).threads:
            async for message in thread.history(limit=None, after=start_time):
                if message.author.bot:
                    continue
                message_count[message.author.id] += 1

        # also count over closed threads
        async for thread in self.bot.get_channel(debate_channel_id).archived_threads(limit=None):
            async for message in thread.history(limit=None, after=start_time):
                if message.author.bot:
                    continue
                message_count[message.author.id] += 1

        # Send the results
        results = "\n".join([f"<@{user_id}>: {count}" for user_id, count in message_count.most_common()])
        embed = discord.Embed(
            title="Debatleden Analyse",
            description=f"Berichten in de debat channel over de laatste 30 dagen:\n{results}",
            color=self.color,
        )
        await context.send(embed=embed)

        # count votes from each congress member in stembureau channel over last 30 days
        stembureau_channel_id = channel_ids.get("stembureau")
        if not stembureau_channel_id:
            await context.send("❌ `stembureau` channel niet geconfigureerd.")
            return
        vote_count = Counter()
        async for message in self.bot.get_channel(stembureau_channel_id).history(limit=None, after=start_time):
            # count reactions as votes
            for reaction in message.reactions:
                async for user in reaction.users():
                    if user.bot:
                        continue
                    vote_count[user.id] += 1
        # Send the results
        results = "\n".join([f"<@{user_id}>: {count}" for user_id, count in vote_count.most_common()])
        embed = discord.Embed(
            title="Stembureau Analyse",
            description=f"Votes in de stembureau channel over de laatste 30 dagen:\n{results}",
            color=self.color,
        )
        await context.send(embed=embed)

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


async def setup(bot) -> None:
    await bot.add_cog(Owner(bot))
