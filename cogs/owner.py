"""
Owner and administrator commands.

Prefix commands (owner-only unless noted):
  !sync / !unsync (scope)       — sync or remove slash commands globally or for a guild
  !uptime                       — show how long the bot has been online
  !load / !unload / !reload (cog) — hot-reload individual cog modules
  !clearluck                    — clear the luck-score cache
  !congres_analyse              — generate a congressional analysis report
  !shutdown                     — gracefully shut down the bot
  !say (message)                — make the bot send a message
  /purge (amount)               — delete messages in bulk (requires manage_messages)
"""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context


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
        sanitized = (
            message
            .replace("@everyone", "@​everyone")
            .replace("@here", "@​here")
        )
        await context.send(sanitized, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

    @commands.hybrid_command(
        name="purge",
        description="Delete a number of messages.",
    )
    @commands.has_guild_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    @app_commands.describe(amount="The amount of messages that should be deleted (max 200).")
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

    @commands.command(
        name="congres-analyse",
        description="Analyseer de congresleden en hun stemgedrag.",
    )
    @commands.is_owner()
    async def congres_analyse(self, context: Context) -> None:
        """count messages from each congress member in congress channel over last 30 days"""
        from collections import Counter
        from datetime import datetime

        channel_ids = self.bot.config.get("channels", {})
        congres_channel_id = channel_ids.get("congres")
        if not congres_channel_id:
            await context.send("❌ `congres` channel niet geconfigureerd.")
            return

        start_time = datetime(2026, 2, 7)  # Get messages from 7 february to today
        message_count = Counter()
        async for message in self.bot.get_channel(congres_channel_id).history(
            limit=None, after=start_time
        ):
            if message.author.bot:
                continue
            message_count[message.author.id] += 1

        # Send the results
        results = "\n".join(
            [f"<@{user_id}>: {count}" for user_id, count in message_count.most_common()]
        )
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
        async for thread in self.bot.get_channel(debate_channel_id).archived_threads(
            limit=None
        ):
            async for message in thread.history(limit=None, after=start_time):
                if message.author.bot:
                    continue
                message_count[message.author.id] += 1

        # Send the results
        results = "\n".join(
            [f"<@{user_id}>: {count}" for user_id, count in message_count.most_common()]
        )
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
        async for message in self.bot.get_channel(stembureau_channel_id).history(
            limit=None, after=start_time
        ):
            # count reactions as votes
            for reaction in message.reactions:
                async for user in reaction.users():
                    if user.bot:
                        continue
                    vote_count[user.id] += 1
        # Send the results
        results = "\n".join(
            [f"<@{user_id}>: {count}" for user_id, count in vote_count.most_common()]
        )
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
    """Add the Owner cog to the bot."""
    await bot.add_cog(Owner(bot))
