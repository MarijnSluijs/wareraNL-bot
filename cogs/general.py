"""
General bot commands — /help, /botinfo, /serverinfo, /ping, /invite,
/eight_ball (question), and /feedback.
"""

import platform
import random

import discord
import pytz
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

# Configuration is provided by the bot at runtime via `bot.config`.


class FeedbackForm(discord.ui.Modal, title="Feedback"):
    """Modal dialog for submitting feedback to the bot owners."""
    feedback = discord.ui.TextInput(
        label="Wat vind je van deze bot?",
        style=discord.TextStyle.long,
        placeholder="Typ je antwoord hier...",
        required=True,
        max_length=256,
    )

    async def on_submit(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.answer = str(self.feedback)
        self.stop()


class General(commands.Cog, name="general"):
    """Cog for general-purpose commands like /help, /botinfo, /serverinfo, /ping, /invite, and /feedback."""
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.context_menu_user = app_commands.ContextMenu(
            name="Grab ID", callback=self.grab_id
        )
        self.bot.tree.add_command(self.context_menu_user)
        self.context_menu_message = app_commands.ContextMenu(
            name="Remove spoilers", callback=self.remove_spoilers
        )
        self.bot.tree.add_command(self.context_menu_message)
        self.color = int(
            self.bot.config.get("colors", {}).get("primary", "0x154273"), 16
        )
        self.config = getattr(self.bot, "config", {}) or {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Suppress embeds for messages that contain links to app.warera.io.
        Requires MANAGE_MESSAGES permission for the bot in the channel.
        """
        if message.author.bot:
            return
        content = message.content or ""
        # self.bot.logger.debug(f"Received message: {content} from {message.author} in {getattr(message.channel, 'id', 'DM')}")
        if "app.warera.io" not in content:
            return
        try:
            await message.edit(suppress=True)
            self.bot.logger.info(
                f"Suppressed embeds for message {message.id} in {getattr(message.channel, 'id', 'DM')}"
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            self.bot.logger.error(
                f"Failed to suppress embeds for message {message.id}: {e}"
            )

    # Message context menu command
    async def remove_spoilers(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """
        Removes the spoilers from the message. This command requires the MESSAGE_CONTENT intent to work properly.

        :param interaction: The application command interaction.
        :param message: The message that is being interacted with.
        """
        spoiler_attachment = None
        for attachment in message.attachments:
            if attachment.is_spoiler():
                spoiler_attachment = attachment
                break
        embed = discord.Embed(
            title="Bericht zonder spoilers",
            description=message.content.replace("||", ""),
            color=self.color,
        )
        if spoiler_attachment is not None:
            embed.set_image(url=attachment.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # User context menu command
    async def grab_id(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        """
        Grabs the ID of the user.

        :param interaction: The application command interaction.
        :param user: The user that is being interacted with.
        """
        embed = discord.Embed(
            description=f"Het ID van {user.mention} is `{user.id}`.",
            color=self.color,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.hybrid_command(
        name="help", description="Toon alle commands die de bot heeft geladen."
    )
    async def help(self, context: Context) -> None:
        """Show all commands that the bot has loaded."""
        if context.interaction:
            await context.interaction.response.defer(ephemeral=False)

        fields: list[tuple[str, str]] = []

        for cog_name in self.bot.cogs:
            if cog_name == "owner" and not (await self.bot.is_owner(context.author)):
                continue

            cog = self.bot.get_cog(cog_name.lower())
            if cog is None:
                continue

            cog_commands = cog.get_commands()
            cog_commands.extend(cog.get_app_commands())
            data = []
            for command in cog_commands:
                if not await self._is_visible_to_user(context, command):
                    continue
                description = (command.description or "").partition("\n")[0]
                data.append(f"{command.name} - {description}".strip(" -"))

            if data:
                fields.append((cog_name.capitalize(), "```" + "\n".join(data) + "```"))

        if not fields:
            await context.send("Geen beschikbare commands gevonden.")
            return

        # Discord limit: max 25 fields per embed
        for idx in range(0, len(fields), 25):
            embed = discord.Embed(
                title="Help" if idx == 0 else "Help (vervolg)",
                description="Lijst van beschikbare commands:",
                color=self.color,
            )
            for name, value in fields[idx : idx + 25]:
                embed.add_field(name=name, value=value, inline=False)
            await context.send(embed=embed)

    async def _is_visible_to_user(self, context: Context, command) -> bool:
        """Check if a command is visible to the user in the given context."""
        # Prefix / hybrid command objects
        if isinstance(command, commands.Command):
            try:
                return await command.can_run(context)
            except Exception:
                return False

        # Slash / context-menu app commands
        if isinstance(command, (app_commands.Command, app_commands.ContextMenu)):
            interaction = context.interaction
            if interaction is None:
                # No interaction -> cannot reliably evaluate app_command checks
                return True
            try:
                return await command._check_can_run(interaction)  # private API
            except Exception:
                return False

        return False
    

    @commands.hybrid_command(
        name="botinfo",
        description="Laat nuttige informatie over de bot zien.",
    )
    async def botinfo(self, context: Context) -> None:
        """
        Get some useful (or not) information about the bot.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            description="Used [Krypton's](https://krypton.ninja) template",
            color=self.color,
        )
        embed.set_author(name="Bot-informatie")
        embed.add_field(name="Eigenaar:", value="teunp", inline=True)
        embed.add_field(
            name="Python-versie:", value=f"{platform.python_version()}", inline=True
        )
        embed.add_field(
            name="Prefix:",
            value=f"/ (Slash-commands) of {self.bot.bot_prefix} voor normale commands",
            inline=False,
        )
        embed.set_footer(text=f"Gevraagd door {context.author}")
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="serverinfo",
        description="Laat nuttige informatie over de server zien.",
    )
    async def serverinfo(self, context: Context) -> None:
        """
        Get some useful (or not) information about the server.

        :param context: The hybrid command context.
        """
        roles = [role.name for role in context.guild.roles]
        num_roles = len(roles)
        if num_roles > 50:
            roles = roles[:50]
            roles.append(f">>>> Displaying [50/{num_roles}] Roles")
        roles = ", ".join(roles)

        embed = discord.Embed(
            title="**Servernaam:**", description=f"{context.guild}", color=self.color
        )
        if context.guild.icon is not None:
            embed.set_thumbnail(url=context.guild.icon.url)
        embed.add_field(name="Server-ID", value=context.guild.id)
        embed.add_field(name="Ledenaantal", value=context.guild.member_count)
        embed.add_field(
            name="Tekst/Spraakkanalen", value=f"{len(context.guild.channels)}"
        )
        embed.add_field(name=f"Rollen ({len(context.guild.roles)})", value=roles)
        embed.set_footer(text=f"Aangemaakt op: {context.guild.created_at}")
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="ping",
        description="Controleer of de bot online is.",
    )
    async def ping(self, context: Context) -> None:
        """
        Check if the bot is alive.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            title="🏓 Pong!",
            description=f"De botvertraging is {round(self.bot.latency * 1000)}ms.",
            color=0xBEBEFE,
        )
        await context.send(embed=embed)

    @commands.hybrid_command(
        name="invite",
        description="Krijg de uitnodigingslink van de bot.",
    )
    async def invite(self, context: Context) -> None:
        """
        Get the invite link of the bot to be able to invite it.

        :param context: The hybrid command context.
        """
        embed = discord.Embed(
            description=f"Nodig me uit door [hier]({self.bot.invite_link}) te klikken.",
            color=self.color,
        )
        try:
            await context.author.send(embed=embed)
            await context.send("Ik heb je een privébericht gestuurd!")
        except discord.Forbidden:
            await context.send(embed=embed)

    # @commands.hybrid_command(
    #     name="server",
    #     description="Get the invite link of the discord server of the bot for some support.",
    # )
    # async def server(self, context: Context) -> None:
    #     """
    #     Get the invite link of the discord server of the bot for some support.

    #     :param context: The hybrid command context.
    #     """
    #     embed = discord.Embed(
    #         description=f"Join the support server for the bot by clicking [here](https://discord.gg/mTBrXyWxAF).",
    #         color=self.color,
    #     )
    #     try:
    #         await context.author.send(embed=embed)
    #         await context.send("I sent you a private message!")
    #     except discord.Forbidden:
    #         await context.send(embed=embed)

    @commands.hybrid_command(
        name="8ball",
        description="Stel de bot een willekeurige vraag.",
    )
    @app_commands.describe(question="De vraag die je wilt stellen.")
    async def eight_ball(self, context: Context, *, question: str) -> None:
        """
        Ask any question to the bot.

        :param context: The hybrid command context.
        :param question: The question that should be asked by the user.
        """
        answers = [
            "Het is zeker.",
            "Absoluut.",
            "Je kunt erop rekenen.",
            "Zonder twijfel.",
            "Ja, zeker weten.",
            "Zoals ik het zie, ja.",
            "Hoogstwaarschijnlijk.",
            "Ziet er goed uit.",
            "Ja.",
            "Alle tekenen wijzen op ja.",
            "Antwoord vaag, probeer later opnieuw.",
            "Vraag het later nog eens.",
            "Beter om het nu niet te zeggen.",
            "Kan het nu niet voorspellen.",
            "Concentreer je en stel de vraag opnieuw.",
            "Reken er maar niet op.",
            "Mijn antwoord is nee.",
            "Mijn bronnen zeggen nee.",
            "Vooruitzichten niet zo goed.",
            "Zeer twijfelachtig.",
        ]
        embed = discord.Embed(
            title="**Mijn Antwoord:**",
            description=f"{random.choice(answers)}",
            color=self.color,
        )
        embed.set_footer(text=f"De vraag was: {question}")
        await context.send(embed=embed)

    # @commands.hybrid_command(
    #     name="bitcoin",
    #     description="Get the current price of bitcoin.",
    # )
    # async def bitcoin(self, context: Context) -> None:
    #     """
    #     Get the current price of bitcoin.

    #     :param context: The hybrid command context.
    #     """
    #     # This will prevent your bot from stopping everything when doing a web request - see: https://discordpy.readthedocs.io/en/stable/faq.html#how-do-i-make-a-web-request
    #     async with aiohttp.ClientSession() as session:
    #         async with session.get(
    #             "https://api.coindesk.com/v1/bpi/currentprice/BTC.json"
    #         ) as request:
    #             if request.status == 200:
    #                 data = await request.json()
    #                 embed = discord.Embed(
    #                     title="Bitcoin price",
    #                     description=f"The current price is {data['bpi']['USD']['rate']} :dollar:",
    #                     color=self.color,
    #                 )
    #             else:
    #                 embed = discord.Embed(
    #                     title="Error!",
    #                     description="There is something wrong with the API, please try again later",
    #                     color=self.color,
    #                 )
    #             await context.send(embed=embed)

    @app_commands.command(
        name="feedback", description="Dien feedback in voor de eigenaars van de bot."
    )
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: i.user.id)
    async def feedback(self, interaction: discord.Interaction) -> None:
        """
        Submit a feedback for the owners of the bot.

        :param context: The hybrid command context.
        """
        feedback_form = FeedbackForm()
        await interaction.response.send_modal(feedback_form)

        await feedback_form.wait()
        interaction = feedback_form.interaction
        await interaction.response.send_message(
            embed=discord.Embed(
                description="Bedankt voor je feedback, de eigenaren zijn op de hoogte gesteld.",
                color=self.color,
            )
        )

        app_owner = (await self.bot.application_info()).owner
        await app_owner.send(
            embed=discord.Embed(
                title="Nieuwe Feedback",
                description=f"{interaction.user} (<@{interaction.user.id}>) heeft nieuwe feedback ingediend:\n```\n{feedback_form.answer}\n```",
                color=self.color,
            )
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Log when a member leaves the server."""
        self.bot.logger.info(f"{member} has left the server.")
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = member.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        # title="Gebruiker heeft de server verlaten",
                        description=f"**{member.mention if member else 'Unknown'} "
                        f"({member.name if member else 'Unknown'}) heeft de server verlaten**\n",
                        color=discord.Color.red(),
                        timestamp=discord.datetime.now(
                            pytz.timezone("Europe/Amsterdam")
                        ),
                    )
                    if member:
                        log_embed.set_author(
                            name=member.name, icon_url=member.display_avatar.url
                        )
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                    await log_channel.send(embed=log_embed)
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Log when a member is updated, specifically for role changes."""
        self.bot.logger.info(f"{before} has been updated.")
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = before.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    role_changes = []
                    if before.roles != after.roles:
                        added_roles = [
                            role for role in after.roles if role not in before.roles
                        ]
                        removed_roles = [
                            role for role in before.roles if role not in after.roles
                        ]
                        if added_roles:
                            role_changes.append(
                                f":white_check_mark: {', '.join(role.name for role in added_roles)}"
                            )
                        if removed_roles:
                            role_changes.append(
                                f":no_entry: {', '.join(role.name for role in removed_roles)}"
                            )
                    else:
                        return
                    log_embed = discord.Embed(
                        # title=f"{before.name}",
                        description=f"**:writing_hand: {before.mention if before else 'Unknown'} is bijgewerkt.** \n"
                        f"**Rollen:**\n{chr(10).join(role_changes) if role_changes else 'Geen veranderingen in rollen.'}",
                        color=discord.Color.orange(),
                        timestamp=discord.datetime.now(
                            pytz.timezone("Europe/Amsterdam")
                        ),
                    )
                    log_embed.set_author(
                        name=before.name,
                        icon_url=before.display_avatar.url if before else None,
                    )
                    if before:
                        log_embed.set_thumbnail(url=before.display_avatar.url)
                    await log_channel.send(embed=log_embed)
                except Exception as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

    @commands.command(name="testleave")
    @commands.is_owner()
    async def test_leave(self, context: Context) -> None:
        """Test command to simulate a member leaving the server."""
        await self.on_member_remove(context.author)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle app command errors, including cooldown messages."""
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ Je kunt dit commando pas weer gebruiken over **{error.retry_after:.0f} seconden**.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot) -> None:
    """Add the General cog to the bot."""
    await bot.add_cog(General(bot))
