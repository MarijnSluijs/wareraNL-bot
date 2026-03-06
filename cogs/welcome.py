"""
Member verification and welcome flow.

Commands and listeners:
  !postwelcome              — (admin) post the welcome message with verification buttons
  on_member_join            — automatically prompts new members to verify
  /nickname (user, nickname) — change a member's server nickname
    /approve (in_game_id, reason) — approve a pending verification request
  /deny (reason)            — deny a pending verification request
    /embassyapprove (country, in_game_id) — approve an embassy membership request
"""

import asyncio
import datetime
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from services.api_client import APIClient

logger = logging.getLogger("discord_bot")


class WelcomeView(discord.ui.View):
    """
    Persistent view containing the three verification buttons.

    Using timeout=None and custom_id makes these buttons persist
    across bot restarts - they'll still work after the bot reconnects.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Nederlander",
        style=discord.ButtonStyle.success,
        custom_id="welcome_citizen",
        emoji="🇳🇱",
    )
    async def citizen_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle citizen verification request."""
        await interaction.response.send_modal(
            VerificationQuestionnaireModal("citizen")
        )

    @discord.ui.button(
        label="Belgian",
        style=discord.ButtonStyle.success,
        custom_id="welcome_belgian",
        emoji="🇧🇪",
    )
    async def belgian_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle Belgian verification request."""
        await interaction.response.send_modal(
            VerificationQuestionnaireModal("belgian")
        )

    @discord.ui.button(
        label="Foreigner",
        style=discord.ButtonStyle.primary,
        custom_id="welcome_foreigner",
        emoji="🌍",
    )
    async def foreigner_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle foreigner verification request."""
        await interaction.response.send_modal(
            VerificationQuestionnaireModal("foreigner")
        )

    @discord.ui.button(
        label="Embassy Request",
        style=discord.ButtonStyle.danger,
        custom_id="welcome_embassy",
        emoji="🚨",
    )
    async def embassy_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle embassy request."""
        await interaction.response.send_modal(
            VerificationQuestionnaireModal("embassy")
        )


class VerificationQuestionnaireModal(discord.ui.Modal):
    """Questionnaire shown to users before opening a verification ticket."""

    def __init__(self, request_type: str):
        self.request_type = request_type
        is_english = request_type in {"belgian", "foreigner", "embassy"}
        super().__init__(
            title="Verification Questionnaire" if is_english else "Verificatie Vragenlijst"
        )

        self.warera_name = discord.ui.TextInput(
            label="WarEra username" if is_english else "WarEra gebruikersnaam",
            placeholder=(
                "Enter your in-game name"
                if is_english
                else "Vul je in-game naam in"
            ),
            required=True,
            max_length=64,
        )
        self.profile_link = discord.ui.TextInput(
            label=(
                "URL to your in-game profile or your user ID"
                if is_english
                else "Profiel-URL of gebruikers-ID"
            ),
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Paste your profile URL or user ID"
                if is_english
                else "Plak je profiellink of gebruikers-ID"
            ),
            required=True,
            max_length=500,
        )
        self.extra_info = discord.ui.TextInput(
            label="Additional info" if is_english else "Aanvullende info",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Optional: extra context for the moderators"
                if is_english
                else "Optioneel: extra context voor de moderators"
            ),
            required=False,
            max_length=500,
        )

        self.add_item(self.warera_name)
        self.add_item(self.profile_link)

        self.embassy_country = None
        if self.request_type == "embassy":
            self.embassy_country = discord.ui.TextInput(
                label="Country",
                placeholder="Which country is this embassy request for?",
                required=True,
                max_length=64,
            )
            self.add_item(self.embassy_country)

        self.add_item(self.extra_info)

    async def on_submit(self, interaction: discord.Interaction):
        is_english = self.request_type in {"belgian", "foreigner", "embassy"}

        raw_profile_value = str(self.profile_link).strip().strip("<>")
        profile_value_for_admins = raw_profile_value
        if raw_profile_value and "://" not in raw_profile_value:
            profile_value_for_admins = (
                f"https://app.warera.io/user/{raw_profile_value}"
            )

        questionnaire_answers = {
            (
                "WarEra username" if is_english else "WarEra gebruikersnaam"
            ): str(self.warera_name).strip(),
            (
                "URL to your in-game profile or your user ID"
                if is_english
                else "Profiel-URL of gebruikers-ID"
            ): profile_value_for_admins,
        }
        if self.embassy_country:
            questionnaire_answers["Country"] = str(self.embassy_country).strip()

        extra = str(self.extra_info).strip()
        if extra:
            questionnaire_answers[
                "Additional info" if is_english else "Aanvullende info"
            ] = extra

        await create_verification_channel(
            interaction,
            self.request_type,
            questionnaire_answers=questionnaire_answers,
        )


async def create_verification_channel(
    interaction: discord.Interaction,
    request_type: str,
    questionnaire_answers: dict[str, str] | None = None,
) -> None:
    """
    Create a private verification ticket channel for the user.

    Args:
        interaction: The button interaction from the user
        request_type: One of "citizen", "foreigner", or "embassy"

    The channel is only visible to:
    - The requesting user
    - The bot itself
    - The relevant moderator roles (Border Control or Embassy handlers)
    """
    user = interaction.user
    guild = interaction.guild
    config = getattr(interaction.client, "config", {}) or {}
    logger.info(
        f"Creating verification channel for {user.name} ({request_type}) in guild {guild.name}"
    )

    # check if the user already has the requested role to prevent duplicate requests
    role_id = None
    if request_type == "citizen":
        role_id = config.get("roles", {}).get("nederlander")
    elif request_type == "belgian":
        role_id = config.get("roles", {}).get("belgian")
    elif request_type == "foreigner":
        role_id = config.get("roles", {}).get("foreigner")

    if role_id:
        role = guild.get_role(role_id)
        if role and role in user.roles:
            await interaction.response.send_message(
                f"You already have the {role.name} role and cannot create a new request.",
                ephemeral=True,
            )
            return

    # Also check actual existing channels (covers bot restarts and manual channel cleanup)
    channels_cfg = config.get("channels", {})
    verification_cat_id = channels_cfg.get("verification")
    verification_category = (
        guild.get_channel(verification_cat_id) if verification_cat_id else None
    )
    channels_to_check = (
        verification_category.channels if verification_category else guild.text_channels
    )

    username_slug = user.name.lower().replace(" ", "-")
    known_prefixes = ("citizen-", "belgian-", "foreigner-", "embassy-")
    existing_channel = None
    for channel in channels_to_check:
        topic = channel.topic or ""
        name = channel.name.lower()
        # Prefer exact user-id match in topic; fallback to username pattern in channel name
        if f"User ID: {user.id}" in topic or (
            name.endswith(f"-{username_slug}") and name.startswith(known_prefixes)
        ):
            existing_channel = channel
            break

    if existing_channel:
        await interaction.response.send_message(
            f"Je hebt al een open ticket: {existing_channel.mention}. Los dit eerst op voordat je een nieuw ticket aanmaakt.",
            ephemeral=True,
        )
        return

    # Generate unique ticket ID (stored in central config if present)
    ticket_id = None
    try:
        if "ticket_counter" in config:
            config["ticket_counter"] = int(config.get("ticket_counter", 0)) + 1
            ticket_id = config["ticket_counter"]
    except Exception:
        ticket_id = None

    if ticket_id is None:
        # fallback: use timestamp
        ticket_id = int(datetime.datetime.utcnow().timestamp())

    # Configure channel properties based on request type
    roles_cfg = config.get("roles", {})
    if request_type == "citizen":
        channel_name = f"citizen-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("border_control")]
        embed_color = discord.Color.green()
        request_title = "Verificatieverzoek Nederlanderschap"
    elif request_type == "belgian":
        channel_name = f"belgian-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("border_control")]
        embed_color = discord.Color.green()
        request_title = "Belgian Citizenship Verification Request"
    elif request_type == "foreigner":
        channel_name = f"foreigner-{ticket_id}-{user.name}"
        role_ids = [roles_cfg.get("border_control")]
        embed_color = discord.Color.blue()
        request_title = "Foreigner Verification Request"
    else:  # embassy
        channel_name = f"embassy-{ticket_id}-{user.name}"
        # Embassy requests notify multiple high-level roles
        role_ids = [
            roles_cfg.get("minister_foreign_affairs"),
            roles_cfg.get("president"),
            roles_cfg.get("vice_president"),
        ]
        embed_color = discord.Color.red()
        request_title = "Emergency Embassy Request"

    # Sanitize channel name (Discord requires lowercase, no spaces, max 100 chars)
    channel_name = channel_name.lower().replace(" ", "-")[:100]

    # Get the category to create the channel in (if configured)
    category = None
    verification_cat = channels_cfg.get("verification")
    if verification_cat:
        category = guild.get_channel(verification_cat)

    # Set up channel permissions
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_messages=True,
            embed_links=True,
        ),
    }

    # Grant access to the relevant moderator roles
    for role_id in role_ids:
        if role_id:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    use_application_commands=True,
                )

    # Check if bot has permission to create channels in the category
    if category:
        bot_permissions = category.permissions_for(guild.me)
        if not bot_permissions.manage_channels:
            await interaction.response.send_message(
                f"Ik heb geen toestemming om kanalen aan te maken in de **{category.name}** categorie.\n\n"
                "**Oplossing:** Ga naar kanaalinstellingen > Rechten > Voeg de botrol toe met 'Kanalen beheren' ingeschakeld.",
                ephemeral=True,
            )
            return

    # Create the ticket channel
    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Verification request by {user.name} | Type: {request_type} | ID: {ticket_id} | User ID: {user.id}",
        )
    except discord.Forbidden as e:
        error_msg = (
            "Ik heb geen toestemming om kanalen aan te maken.\n\n"
            "**Mogelijke oplossingen:**\n"
            "• Zorg dat de bot 'Kanalen beheren' toestemming heeft op de hele server\n"
        )
        if category:
            error_msg += f"• Voeg de bot toe aan de **{category.name}** categorie met 'Kanalen beheren' toestemming\n"
        error_msg += f"\n**Fout:** {e}"
        await interaction.response.send_message(error_msg, ephemeral=True)
        return

    # Build list of role mentions to ping
    role_mentions = []
    for role_id in role_ids:
        if role_id:
            role = guild.get_role(role_id)
            if role:
                role_mentions.append(role.mention)

    # Create the ticket embed with request details
    embed = discord.Embed(
        title=f"📋 {request_title}",
        description=f"**Gebruiker:** {user.mention}\n**Type:** {request_type.title()}\n**Ticket ID:** #{ticket_id}",
        color=embed_color,
        timestamp=datetime.datetime.now(datetime.UTC),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="Instructies voor Moderators",
        value="Gebruik `/approve` om dit verzoek goed te keuren\nGebruik `/deny` om dit verzoek af te wijzen",
        inline=False,
    )
    embed.set_footer(text=f"User ID: {user.id}")

    # Send the ticket message, pinging relevant moderators
    mention_text = " ".join(role_mentions) if role_mentions else ""
    await channel.send(content=mention_text, embed=embed)

    if questionnaire_answers:
        questionnaire_embed = discord.Embed(
            title="🧾 Ingevulde Vragenlijst",
            color=embed_color,
            timestamp=datetime.datetime.now(datetime.UTC),
        )
        for label, value in questionnaire_answers.items():
            questionnaire_embed.add_field(
                name=label,
                value=value[:1024] if value else "-",
                inline=False,
            )
        await channel.send(embed=questionnaire_embed)

    if request_type == "citizen":
        instruction_text = (
            "Hallo, stuur alsjeblieft een screenshot van je WarEra profiel om je verificatieverzoek af te ronden."
        )
    else:
        instruction_text = (
            "Hello, please send a screenshot of your WarEra profile to complete your verification request."
        )

    instructions_embed = discord.Embed(
        description=instruction_text,
        color=embed_color,
    )
    await channel.send(content=user.mention, embed=instructions_embed)

    # Confirm to the user (only they can see this response)
    if request_type == "citizen":
        await interaction.response.send_message(
            f"Je verificatiekanaal is aangemaakt: {channel.mention}\n"
            "Wacht op een moderator om je verzoek te beoordelen.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Your verification channel has been created: {channel.mention}\n"
            "Please wait for a moderator to review your request.",
            ephemeral=True,
        )


class Welcome(commands.Cog, name="welcome"):
    """Cog for welcome messages and verification system."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.bot.logger.info("Welcome cog initialized")
        # Add the persistent view when the cog is loaded
        self.bot.add_view(WelcomeView(bot))
        # Use the central bot configuration
        self.config = getattr(self.bot, "config", {}) or {}
        # Per-(guild, country) locks to prevent duplicate embassy channel creation
        self._embassy_locks: dict[str, asyncio.Lock] = {}
        self._approval_db = None

    @property
    def _client(self):
        return getattr(self.bot, "_ext_client", None)

    async def _get_approval_db(self):
        """Return shared external DB, or lazily create one as fallback."""
        shared = getattr(self.bot, "_ext_db", None)
        if shared is not None:
            return shared
        if self._approval_db is None:
            from services.db import Database

            db_path = self.config.get("external_db_path", "database/external.db")
            self._approval_db = Database(db_path)
            await self._approval_db.setup()
        return self._approval_db

    async def _store_identity_link(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        in_game_id: str,
        request_type: str,
        nationality: str,
        embassy_country: str | None = None,
    ) -> None:
        """Persist Discord ↔ in-game identity mapping for approved users."""
        db = await self._get_approval_db()
        approved_at = datetime.datetime.now(datetime.UTC).isoformat()
        await db.upsert_identity_link(
            discord_user_id=str(member.id),
            guild_id=str(interaction.guild.id),
            in_game_user_id=in_game_id,
            nationality=nationality,
            request_type=request_type,
            embassy_country=embassy_country,
            approved_by_discord_id=str(interaction.user.id),
            approved_at=approved_at,
        )

    async def _validate_identity_link_target(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        in_game_id: str,
    ) -> None:
        """Ensure new mapping does not conflict with existing records."""
        db = await self._get_approval_db()
        guild_id = str(interaction.guild.id)
        discord_id = str(member.id)

        existing_for_discord = await db.get_identity_link_by_discord(
            discord_user_id=discord_id,
            guild_id=guild_id,
        )
        if (
            existing_for_discord
            and existing_for_discord.get("in_game_user_id") != in_game_id
        ):
            raise ValueError(
                "Deze Discord gebruiker is al gekoppeld aan een ander in-game ID. "
                "Werk de mapping eerst handmatig bij om fouten te voorkomen."
            )

        existing_for_ingame = await db.get_identity_links_by_ingame(
            in_game_user_id=in_game_id,
            guild_id=guild_id,
        )
        conflicting_discord = next(
            (
                link.get("discord_user_id")
                for link in existing_for_ingame
                if link.get("discord_user_id") != discord_id
            ),
            None,
        )
        if conflicting_discord:
            raise ValueError(
                "Dit in-game ID is al gekoppeld aan een andere Discord gebruiker: "
                f"<@{conflicting_discord}> (`{conflicting_discord}`)."
            )

    @staticmethod
    def _normalize_ingame_id(in_game_id: str) -> str:
        """Normalize and validate in-game ID or WarEra profile URL input."""
        raw_value = str(in_game_id).strip()
        if not raw_value:
            raise ValueError("In-game ID cannot be empty.")

        # Accept direct profile links like: https://app.warera.io/user/{id}
        match = re.match(
            r"^https?://app\.warera\.io/user/([^/?#]+)(?:[/?#].*)?$",
            raw_value,
            flags=re.IGNORECASE,
        )
        if match:
            normalized = match.group(1).strip()
        else:
            normalized = raw_value
            if "://" in raw_value:
                raise ValueError(
                    "Invalid WarEra profile URL. Use `https://app.warera.io/user/{id}` or provide the raw in-game ID."
                )

        if not normalized:
            raise ValueError("Could not extract an in-game ID from the provided input.")
        if len(normalized) > 64:
            raise ValueError("In-game ID is too long (max 64 characters).")
        return normalized

    # def cog_load(self) -> None:
    #     """Start the scheduled tasks when the cog is loaded."""
    #     self.daily_bezoeker_ping.start()

    # def cog_unload(self) -> None:
    #     """Cancel scheduled tasks when the cog is unloaded."""
    #     self.daily_bezoeker_ping.cancel()

    @commands.command(
        name="postwelcome",
        description="Post the welcome message with verification buttons (admin only)",
    )
    @commands.has_permissions(administrator=True)
    async def post_welcome(self, ctx: commands.Context):
        # Create the welcome embed
        embed = discord.Embed(
            title="🇳🇱 Welcome to Nederland!",
            description=self.config.get("welcome_message", "Welcome!"),
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.UTC),
        )
        embed.set_thumbnail(
            url="https://3.bp.blogspot.com/-x8PxTZ-frT8/VzhaiN0qnTI/AAAAAAAAskA/BFXeRJND8YU3oUxBBqq6Ny9ITeWpq5BuACKgB/s1600/NEDERLAND.%2BWAPEN%2B%25281%2529.png"
        )
        # embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        # embed.set_footer(text=f"Member #{self.bot.guild.member_count}")

        # Send welcome message with verification buttons
        channel_id = self.bot.config.get("channels", {}).get("welcome_buttons")
        if not channel_id:
            await ctx.send("Welcome channel ID not configured in bot config.")
            return

        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            await ctx.send(
                "Welcome channel not found. Please check the channel ID in bot config."
            )
            return

        await channel.send(embed=embed, view=WelcomeView(self.bot))

    # @tasks.loop(time=datetime.time(19, 0))  # Runs daily at 19:00
    # async def daily_bezoeker_ping(self):
    #     """Send a daily ping to the bezoeker role in the welcome channel."""
    #     try:
    #         # Get the welcome channel id from bot config
    #         welcome_channel_id = self.bot.config.get("channels", {}).get("welcome_buttons")
    #         if not welcome_channel_id:
    #             self.bot.logger.warning("Welcome channel ID not configured")
    #             return

    #         # Find the welcome channel across all guilds the bot is in
    #         for guild in self.bot.guilds:
    #             channel = guild.get_channel(welcome_channel_id)
    #             if channel:
    #                 # Get the bezoeker role
    #                 bezoeker_role_id = self.bot.config.get("roles", {}).get("bezoeker")
    #                 if not bezoeker_role_id:
    #                     self.bot.logger.warning("Bezoeker role ID not configured")
    #                     return

    #                 role = guild.get_role(bezoeker_role_id)
    #                 if not role:
    #                     self.bot.logger.warning(f"Bezoeker role not found in guild {guild.name}")
    #                     return

    #                 # Send the ping
    #                 await channel.send(f"{role.mention} please use one of the above buttons to claim your role.")
    #                 self.bot.logger.info(f"Sent daily bezoeker ping in {guild.name}")
    #                 return

    #         self.bot.logger.warning(f"Welcome channel {welcome_channel_id} not found in any guild")
    #     except Exception as e:
    #         self.bot.logger.error(f"Error sending daily bezoeker ping: {e}")

    # @daily_bezoeker_ping.before_loop
    # async def before_daily_ping(self):
    #     """Ensure the bot is ready before starting the scheduled task."""
    #     await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """
        Send a welcome message when a new member joins the server.

        The message includes the configured welcome text and the
        three verification buttons (Citizen, Foreigner, Embassy).
        """

        # Skip if no welcome channel is configured
        welcome_channel_id = self.bot.config.get("channels", {}).get("welcome_buttons")
        if not welcome_channel_id:
            return

        channel = member.guild.get_channel(welcome_channel_id)
        if not channel:
            return

        default_role_id = self.bot.config.get("roles", {}).get("bezoeker")
        if default_role_id:
            role = member.guild.get_role(default_role_id)
            if role:
                await member.add_roles(role)

        embed = discord.Embed(
            title="🇳🇱 Welcome to Nederland!",
            description=f"Welcome {member.mention}! We're glad to have you here.\n\nPlease head over to <#{welcome_channel_id}> and click one of the buttons to verify your status and gain access to the rest of the server!",
            color=int(self.bot.config.get("colors", {}).get("primary", "0x154273"), 16),
        )
        # optionally send to a dedicated welcome/announcement channel if configured
        extra_welcome = self.bot.config.get("channels", {}).get("welcome_message")
        if extra_welcome:
            ch = member.guild.get_channel(extra_welcome)
            if ch:
                await ch.send(embed=embed)

        # # Create the welcome embed
        # embed = discord.Embed(
        #     title="🇳🇱 Welcome to Nederland!",
        #     description=self.config.get("welcome_message", "Welcome!"),
        #     color=discord.Color.gold(),
        #     timestamp=datetime.datetime.now(datetime.UTC)
        # )
        # embed.set_thumbnail(url=member.display_avatar.url)
        # embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        # embed.set_footer(text=f"Member #{member.guild.member_count}")

        # # Send welcome message with verification buttons
        # await channel.send(content=member.mention, embed=embed, view=WelcomeView(self.bot))

    @app_commands.command(
        name="nickname", description="Stel de bijnaam van een gebruiker in op de server"
    )
    @app_commands.describe(
        user="De gebruiker van wie je de bijnaam wilt wijzigen",
        nickname="De nieuwe bijnaam",
    )
    @commands.has_permissions(manage_nicknames=True)
    async def nickname(
        self, interaction: discord.Interaction, user: discord.Member, nickname: str
    ):
        """
        Change a user's nickname in the server.

        :param interaction: The interaction that triggered the command.
        :param user: The member whose nickname is to be changed.
        :param nickname: The new nickname to set.
        """
        try:
            await user.edit(
                nick=nickname, reason=f"Nickname changed by {interaction.user.name}"
            )
            await interaction.response.send_message(
                f"Bijnaam van {user.mention} is succesvol gewijzigd naar **{nickname}**.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Ik heb geen toestemming om de bijnaam van deze gebruiker te wijzigen.",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Bijnaam wijzigen mislukt: {e}", ephemeral=True
            )

        # Log to the government log channel
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="Nickname aangepast",
                        description=f"**User:** {user.mention} ({user.name})\n",
                        color=discord.Color.green(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_thumbnail(url=user.display_avatar.url)
                    log_embed.set_footer(
                        text=f"Veranderd door {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    await log_channel.send(embed=log_embed)
                    _log_posted = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

    async def _get_nickname(self, in_game_id: str) -> str | None:
        # Ensure the shared API client is available. The ServiceCoordinator cog
        # initializes `bot._ext_client` asynchronously; wait for that if present.
        client = self._client
        if client is None:
            ready_event = getattr(self.bot, "_ext_services_ready", None)
            if ready_event is not None:
                try:
                    await asyncio.wait_for(ready_event.wait(), timeout=5.0)
                except Exception:
                    # timed out or other error; continue to check client below
                    pass
                client = self._client

        if client is None:
            raise RuntimeError("API client is not available. Cannot fetch username.")

        try:
            params = {"input": json.dumps({"userId": in_game_id})}
            user_info: dict = await client.get("/user.getUserLite", params=params)
            # Defensive extraction in case the API returns unexpected shapes
            nickname = (
                user_info.get("result", {})
                .get("data", {})
                .get("username")
            )
            if not nickname:
                raise ValueError("username not found in API response")
        except Exception as e:
            self.bot.logger.error(f"Error fetching username for in-game ID {in_game_id}: {e}")
            raise ValueError("Failed to fetch username from API for the provided in-game ID.")
            return

    @app_commands.command(
        name="approve", description="Keur een verificatieverzoek goed"
    )
    @app_commands.describe(
        in_game_id="In-game ID of profiel-URL (https://app.warera.io/user/{id})",
        reason="Interne reden voor goedkeuring (niet zichtbaar voor de gebruiker)",
        nickname="[Optioneel]: Gebruikersnaam van de speler"
    )
    async def approve(
        self,
        interaction: discord.Interaction,
        in_game_id: str,
        nickname: str = None,
        reason: str = "Geen reden opgegeven",
    ):
        """
        Approve a verification request in the current ticket channel.
        """
        channel = interaction.channel
        try:
            in_game_id = self._normalize_ingame_id(in_game_id)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        
        if nickname is None:
            try: 
                nickname = await self._get_nickname(in_game_id)
            except Exception as e:
                await interaction.response.send_message(
                    f"Failed to retrieve username for in-game ID: {e}", ephemeral=True
                )
                return

        # Verify this is a ticket channel
        if not channel.name.startswith(
            ("citizen-", "foreigner-", "belgian-")
        ):
            await interaction.response.send_message(
                "This command can only be used in verification channels.",
                ephemeral=True,
            )
            return

        # Check if the user has permission to moderate
        mod_roles = [
            self.config["roles"]["border_control"],
            self.config["roles"]["minister_foreign_affairs"],
            self.config["roles"]["president"],
            self.config["roles"]["vice_president"],
        ]

        user_role_ids = [role.id for role in interaction.user.roles]
        has_permission = any(
            role_id in user_role_ids for role_id in mod_roles if role_id
        )

        if not has_permission and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        # Extract user ID from channel topic
        topic = channel.topic or ""
        user_id = None
        for part in topic.split("|"):
            if "User ID:" in part:
                try:
                    user_id = int(part.split(":")[-1].strip())
                except ValueError:
                    logger.debug("Could not parse user ID from topic part: %r", part)

        if not user_id:
            await interaction.response.send_message(
                "Could not find the user for this request. Please check manually.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(user_id)
        if not member:
            await interaction.response.send_message(
                "The user is no longer in the server.", ephemeral=True
            )
            return
        
        try:
            await member.edit(nick=nickname)
        except Exception as e:
            await interaction.response.send_message(f"Failed to edit member nickname: {e}")
            self.bot.logger.error(f"Failed to edit member nickname: {e}")
            return

        try:
            await self._validate_identity_link_target(
                interaction=interaction,
                member=member,
                in_game_id=in_game_id,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        except Exception as e:
            self.bot.logger.error(
                "Failed identity validation in /approve: %s", e, exc_info=True
            )
            await interaction.response.send_message(
                "Kon identity mapping niet valideren door een interne fout.",
                ephemeral=True,
            )
            return

        # Determine which role to grant based on request type
        request_type = channel.name.split("-")[0]
        role_to_give = None

        if request_type == "citizen":
            role_to_give = interaction.guild.get_role(
                self.config["roles"]["nederlander"]
            )
        elif request_type == "belgian":
            role_to_give = interaction.guild.get_role(self.config["roles"]["belgian"])
        elif request_type == "foreigner":
            role_to_give = interaction.guild.get_role(self.config["roles"]["foreigner"])

        # Attempt to assign the role
        if role_to_give:
            try:
                await member.add_roles(role_to_give)
                self.bot.logger.info(
                    f"Assigned role {role_to_give.name} to {member.name} for {request_type} verification"
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"I don't have permission to assign the {role_to_give.name} role. "
                    "Make sure my bot role is **higher** than this role in Server Settings > Roles.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.response.send_message(
                    f"Failed to assign role: {e}", ephemeral=True
                )
                return
            except Exception as e:
                await interaction.response.send_message(
                    f"An unexpected error occurred while assigning the role: {e}",
                    ephemeral=True,
                )
                return

        # Remove old role
        old_role_id = self.config["roles"]["bezoeker"]
        old_role = interaction.guild.get_role(old_role_id)
        if old_role:
            try:
                await member.remove_roles(old_role)
            except discord.Forbidden:
                self.bot.logger.error(
                    f"Could not remove role {old_role.name} from {member.name} due to permission issues."
                )
            except discord.HTTPException as e:
                self.bot.logger.error(
                    f"Failed to remove role {old_role.name} from {member.name}: {e}"
                )

        db_saved = True
        try:
            nationality = {
                "citizen": "nederlander",
                "belgian": "belgian",
                "foreigner": "foreigner",
            }.get(request_type, request_type)
            await self._store_identity_link(
                interaction=interaction,
                member=member,
                in_game_id=in_game_id,
                request_type=request_type,
                nationality=nationality,
            )
        except Exception as e:
            db_saved = False
            self.bot.logger.error("Failed to persist identity link in /approve: %s", e)

        # Notify the user of approval
        if not request_type == "citizen":
            user_embed = discord.Embed(
                title="✅ Request Approved!",
                description=f"Your {request_type} verification request has been approved!",
                color=discord.Color.green(),
            )
            if role_to_give:
                user_embed.add_field(
                    name="Role Granted", value=role_to_give.mention, inline=False
                )

            user_embed.set_footer(text="This channel will be deleted in 30 seconds.")

            await channel.send(content=member.mention, embed=user_embed)

        # Log to the government log channel
        log_posted = False
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="✅ Verificatie Goedgekeurd",
                        description=(
                            f"**Gebruiker:** {member.mention} ({member.name})\n"
                            f"**Type:** {request_type.title()}\n"
                            f"**Reden:** {reason}"
                        ),
                        color=discord.Color.green(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.set_footer(
                        text=f"Goedgekeurd door {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    if role_to_give:
                        log_embed.add_field(
                            name="Rol Toegewezen",
                            value=role_to_give.mention,
                            inline=True,
                        )
                    await log_channel.send(embed=log_embed)
                    log_posted = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

        # Confirm to the moderator
        mod_embed = discord.Embed(
            title="📝 Goedkeuring Geregistreerd",
            description=(
                f"**Gebruiker:** {member.mention}\n"
                f"**Type:** {request_type}\n"
                f"**In-game ID:** `{in_game_id}`\n"
                f"**Reden:** {reason}"
            ),
            color=discord.Color.green(),
        )
        mod_embed.set_footer(text=f"Goedgekeurd door {interaction.user.name}")

        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if not log_posted and log_channel_id:
            mod_embed.add_field(
                name="⚠️ Waarschuwing",
                value="Kon niet in het logkanaal posten",
                inline=False,
            )
        if not db_saved:
            mod_embed.add_field(
                name="⚠️ Database",
                value="Rol is toegekend, maar identity mapping kon niet worden opgeslagen.",
                inline=False,
            )

        await interaction.response.send_message(embed=mod_embed, ephemeral=True)

        if request_type == "citizen":
            # Build contextual links from config when available
            cfg_channels = self.bot.config.get("channels", {})
            handleiding_ch = cfg_channels.get("handleiding")
            roles_ch = cfg_channels.get("roles_claim")
            support_ch = cfg_channels.get("vragen")

            refferer_name = interaction.user.nick or "2sa"
            parts = [f"Welkom {member.mention} in WarEra Nederland!\n\n"]
            if handleiding_ch:
                parts.append(f"Om je op weg te helpen, bekijk onze <#{handleiding_ch}>")
            if roles_ch:
                parts.append(f" en claim je rollen in <#{roles_ch}>")
            if support_ch:
                parts.append(f". Voor vragen kun terecht in <#{support_ch}>")
            parts.append(
                f".\n\nAls laatste: je kan op je profiel bij `Settings > Referrals` een referrer opgeven, vul hier het liefst een **Nederlander** in (bijvoorbeeld *{refferer_name}*), dan krijgen jij en de referrer muntjes."
            )

            welcome_embed = discord.Embed(
                title="Welkom Nederlander! 🇳🇱",
                description="".join(parts),
                color=discord.Color.gold(),
            )
            welcome_embed.set_thumbnail(url=member.display_avatar.url)
            welcome_embed.set_footer(
                text="Dit kanaal zal worden verwijderd over 1 uur."
            )
            self.bot.logger.info(
                f"Sending welcome message to {member.name} in {interaction.guild.name}"
            )
            await channel.send(content=member.mention, embed=welcome_embed)

        # Delete the ticket channel after a delay
        if not request_type == "citizen":
            await asyncio.sleep(30)
        else:
            await asyncio.sleep(
                3600
            )  # Give new citizens more time to read the welcome message
        try:
            await channel.delete(
                reason=f"Verificatie goedgekeurd door {interaction.user.name}"
            )
        except (discord.NotFound, discord.Forbidden) as e:
            self.bot.logger.error(f"Could not delete channel: {e}")

    @app_commands.command(name="deny", description="Wijs een verificatieverzoek af")
    @app_commands.describe(
        reason="Interne reden voor afwijzing (niet zichtbaar voor de gebruiker)"
    )
    async def deny(
        self, interaction: discord.Interaction, reason: str = "Geen reden opgegeven"
    ):
        """
        Deny a verification request in the current ticket channel.
        """

        channel = interaction.channel

        # Verify this is a ticket channel
        if not channel.name.startswith(("citizen-", "foreigner-", "embassy-", "belgian-")):
            await interaction.response.send_message(
                "Dit commando kan alleen worden gebruikt in verificatiekanalen.",
                ephemeral=True,
            )
            return

        # Check if the user has permission to moderate
        mod_roles = [
            self.config["roles"]["border_control"],
            self.config["roles"]["minister_foreign_affairs"],
            self.config["roles"]["president"],
            self.config["roles"]["vice_president"],
        ]

        user_role_ids = [role.id for role in interaction.user.roles]
        has_permission = any(
            role_id in user_role_ids for role_id in mod_roles if role_id
        )

        if not has_permission and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Je hebt geen toestemming om dit commando te gebruiken.", ephemeral=True
            )
            return

        # Extract user ID from channel topic
        topic = channel.topic or ""
        user_id = None
        for part in topic.split("|"):
            if "User ID:" in part:
                try:
                    user_id = int(part.split(":")[-1].strip())
                except ValueError:
                    logger.debug("Could not parse user ID from topic part: %r", part)

        member = interaction.guild.get_member(user_id) if user_id else None
        request_type = channel.name.split("-")[0]

        # Notify the user of denial
        user_embed = discord.Embed(
            title="❌ Request Denied",
            description=f"Your {request_type} verification request has been denied.",
            color=discord.Color.red(),
        )
        user_embed.set_footer(text="This channel will be deleted in 30 seconds.")

        if member:
            await channel.send(content=member.mention, embed=user_embed)
        else:
            await channel.send(embed=user_embed)

        # Log to the government log channel
        log_posted = False
        log_channel_id = self.bot.config.get("channels", {}).get("logs")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(log_channel_id)
            if log_channel:
                try:
                    log_embed = discord.Embed(
                        title="❌ Verificatie Afgewezen",
                        description=(
                            f"**Gebruiker:** {member.mention if member else 'Onbekend'} "
                            f"({member.name if member else 'Onbekend'})\n"
                            f"**Type:** {request_type.title()}\n"
                            f"**Reden:** {reason}"
                        ),
                        color=discord.Color.red(),
                        timestamp=datetime.datetime.now(datetime.UTC),
                    )
                    if member:
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                    log_embed.set_footer(
                        text=f"Afgewezen door {interaction.user.name}",
                        icon_url=interaction.user.display_avatar.url,
                    )
                    await log_channel.send(embed=log_embed)
                    log_posted = True
                except (discord.Forbidden, discord.HTTPException) as e:
                    self.bot.logger.error(f"Failed to post to log channel: {e}")

        # Confirm to the moderator
        mod_embed = discord.Embed(
            title="📝 Afwijzing Geregistreerd",
            description=f"**Gebruiker:** {member.mention if member else 'Onbekend'}\n"
            f"**Type:** {request_type}\n"
            f"**Reden:** {reason}",
            color=discord.Color.red(),
        )
        mod_embed.set_footer(text=f"Afgewezen door {interaction.user.name}")

        if not log_posted and log_channel_id:
            mod_embed.add_field(
                name="⚠️ Waarschuwing",
                value="Kon niet in het logkanaal posten",
                inline=False,
            )

        await interaction.response.send_message(embed=mod_embed, ephemeral=True)

        # Delete the ticket channel after a delay
        await asyncio.sleep(30)
        try:
            await channel.delete(
                reason=f"Verificatie afgewezen door {interaction.user.name}"
            )
        except (discord.NotFound, discord.Forbidden) as e:
            self.bot.logger.error(f"Could not delete channel: {e}")

    @app_commands.command(
        name="embassyapprove", description="Keur een ambassadeverzoek goed"
    )
    @app_commands.describe(
        country="Land van het ambassadeverzoek",
        in_game_id="In-game ID of profiel-URL (https://app.warera.io/user/{id})",
    )
    async def embassy_approve(
        self,
        interaction: discord.Interaction,
        country: str,
        in_game_id: str,
    ):
        """
        Approve an embassy request and assign the corresponding role.

        This command is similar to /approve but also assigns the specific embassy role.
        """
        import traceback

        try:
            # avoid "The application did not respond" (Discord requires a response within 3s)
            await interaction.response.defer(ephemeral=True)
            try:
                in_game_id = self._normalize_ingame_id(in_game_id)
            except ValueError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            # quick trace so you can see the command started
            self.bot.logger.info(
                f"embassy_approve started by {interaction.user} for country={country}"
            )

            # helper to reply whether we've already deferred
            async def reply(content=None, **kwargs):
                if interaction.response.is_done():
                    await interaction.followup.send(content, **kwargs)
                else:
                    await interaction.response.send_message(content, **kwargs)

            channel = interaction.channel
            guild = interaction.guild

            minister_role = interaction.guild.get_role(
                self.config["roles"]["government"]
            )
            president_role = interaction.guild.get_role(
                self.config["roles"]["president"]
            )
            vice_president_role = interaction.guild.get_role(
                self.config["roles"]["vice_president"]
            )

            # Check if the user has permission to moderate
            mod_roles = [
                self.config["roles"]["government"],
                self.config["roles"]["president"],
                self.config["roles"]["vice_president"],
            ]

            user_role_ids = [role.id for role in interaction.user.roles]
            has_permission = any(
                role_id in user_role_ids for role_id in mod_roles if role_id
            )

            if (
                not has_permission
                and not interaction.user.guild_permissions.administrator
            ):
                await interaction.response.send_message(
                    "You don't have permission to use this command.", ephemeral=True
                )
                return

            self.bot.logger.debug(
                f"looking for user ID in channel topic: {channel.topic}"
            )
            # Extract user ID from channel topic
            topic = channel.topic or ""
            user_id = None
            for part in topic.split("|"):
                if "User ID:" in part:
                    try:
                        user_id = int(part.split(":")[-1].strip())
                    except ValueError:
                        self.bot.logger.debug("Could not parse user ID from topic part: %r", part)

            if not user_id:
                await interaction.response.send_message(
                    "Kon de gebruiker voor dit verzoek niet vinden. Controleer dit handmatig.",
                    ephemeral=True,
                )
                return

            member = interaction.guild.get_member(user_id)
            if not member:
                await interaction.response.send_message(
                    "De gebruiker is niet meer op de server.", ephemeral=True
                )
                return

            try:
                await self._validate_identity_link_target(
                    interaction=interaction,
                    member=member,
                    in_game_id=in_game_id,
                )
            except ValueError as e:
                await reply(str(e), ephemeral=True)
                return

            # Attempt to assign the embassy role based on country
            self.bot.logger.debug(f"Assigning embassy role for country: {country}")
            embassy_role_id = self.bot.config.get("roles", {}).get(
                "buitenlandse_diplomaat"
            )
            embassy_role = (
                interaction.guild.get_role(embassy_role_id) if embassy_role_id else None
            )

            try:
                await member.add_roles(embassy_role)
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"I don't have permission to assign the {embassy_role.name} role. "
                    "Make sure my bot role is **higher** than this role in Server Settings > Roles.",
                    ephemeral=True,
                )
                return

            # remove visitor role
            old_role_id = self.config["roles"]["bezoeker"]
            old_role = interaction.guild.get_role(old_role_id)
            if old_role:
                try:
                    await member.remove_roles(old_role)
                except discord.Forbidden:
                    self.bot.logger.error(
                        f"Could not remove role {old_role.name} from {member.name} due to permission issues."
                    )
                except discord.HTTPException as e:
                    self.bot.logger.error(
                        f"Failed to remove role {old_role.name} from {member.name}: {e}"
                    )

            # Check if the embassy channel exists — guarded by a per-country lock to
            # prevent a race condition when two moderators approve simultaneously.
            lock_key = f"{interaction.guild_id}:{country.lower()}"
            async with self._embassy_locks.setdefault(lock_key, asyncio.Lock()):
                self.bot.logger.debug(
                    f"Checking for existing embassy channel for country: {country}"
                )
                embassy_channel = None
                for channel in interaction.guild.channels:
                    if (
                        channel.name == f"{country.lower()}-embassy"
                        or channel.name == f"{country.lower()}-ambassade"
                    ):
                        embassy_channel = channel
                        break

                if not embassy_channel:
                    # Create the embassy channel
                    self.bot.logger.debug(
                        f"Creating embassy channel for country: {country}"
                    )
                    channel_name = f"{country.lower()}-embassy"
                    # choose a category from config when available
                    cat_id = self.bot.config.get("channels", {}).get(
                        "embassy_category"
                    ) or self.bot.config.get("channels", {}).get("verification")
                    category = interaction.guild.get_channel(cat_id) if cat_id else None

                    # Set up channel permissions
                    overwrites = {
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        minister_role: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, read_message_history=True
                        ),
                        president_role: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, read_message_history=True
                        ),
                        vice_president_role: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, read_message_history=True
                        ),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            manage_channels=True,
                            manage_messages=True,
                            embed_links=True,
                        ),
                    }

                    try:
                        channel = await guild.create_text_channel(
                            name=channel_name,
                            category=category,
                            overwrites=overwrites,
                            topic=f"Embassy channel for {country}",
                        )
                        embassy_channel = channel
                    except discord.Forbidden as e:
                        error_msg = (
                            "Ik heb geen toestemming om kanalen aan te maken.\n\n"
                            "**Mogelijke oplossingen:**\n"
                            "• Zorg dat de bot 'Kanalen beheren' toestemming heeft op de hele server\n"
                        )
                        if category:
                            error_msg += f"• Voeg de bot toe aan de **{category.name}** categorie met 'Kanalen beheren' toestemming\n"
                        error_msg += f"\n**Fout:** {e}"
                        await interaction.response.send_message(error_msg, ephemeral=True)
                        return

            if embassy_channel:
                self.bot.logger.debug("Failed to edit member nickname: {e}"
                    f"Setting permissions for member {member} in embassy channel {embassy_channel.name}"
                )
                # try:
                await embassy_channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )
                # except discord.Forbidden:
                #     self.bot.logger.error(
                #         f"Could not set permissions for {member.name} in {embassy_channel.name} due to permission issues."
                #     )
                # except discord.HTTPException as e:
                #     self.bot.logger.error(
                #         f"Failed to set permissions for {member.name} in {embassy_channel.name}: {e}"
                #     )
            self.bot.logger.debug(
                f"Successfully approved embassy request for {member.name} and assigned role {embassy_role.name}"
            )

            db_saved = True
            try:
                await self._store_identity_link(
                    interaction=interaction,
                    member=member,
                    in_game_id=in_game_id,
                    request_type="embassy",
                    nationality=country.strip().lower(),
                    embassy_country=country.strip(),
                )
            except Exception as e:
                db_saved = False
                self.bot.logger.error(
                    "Failed to persist identity link in /embassyapprove: %s", e
                )

            confirmation_embed = discord.Embed(
                title=f"Welcome to {country.title()} Embassy! 🇳🇱",
            )
            # send confirmation in embassy channel
            await embassy_channel.send(
                content=f"{member.mention} {minister_role.mention}",
                embed=confirmation_embed,
            )

            response_text = (
                f"Successfully approved embassy request for {member.mention} and assigned role {embassy_role.mention}. "
                f"Access to the embassy channel {embassy_channel.mention} has been granted."
            )
            if not db_saved:
                response_text += (
                    "\n⚠️ Identity mapping could not be saved to the database."
                )
            await reply(response_text)

            # Log to the government log channel
            log_channel_id = self.bot.config.get("channels", {}).get("logs")
            if log_channel_id:
                log_channel = interaction.guild.get_channel(log_channel_id)
                if log_channel:
                    try:
                        log_embed = discord.Embed(
                            title="✅ Ambassadeverzoek Goedgekeurd",
                            description=f"**Gebruiker:** {member.mention} ({member.name})\n"
                            f"**Land:** {country.title()}\n",
                            color=discord.Color.green(),
                            timestamp=datetime.datetime.now(datetime.UTC),
                        )
                        log_embed.set_thumbnail(url=member.display_avatar.url)
                        log_embed.set_footer(
                            text=f"Goedgekeurd door {interaction.user.name}",
                            icon_url=interaction.user.display_avatar.url,
                        )
                        await log_channel.send(embed=log_embed)
                        _log_posted = True
                    except (discord.Forbidden, discord.HTTPException) as e:
                        self.bot.logger.error(f"Failed to post to log channel: {e}")

            # Delete the ticket channel after a delay
            await asyncio.sleep(30)
            try:
                await interaction.channel.delete(
                    reason=f"Embassy request approved by {interaction.user.name}"
                )
            except (discord.NotFound, discord.Forbidden) as e:
                self.bot.logger.error(f"Could not delete channel: {e}")

        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            self.bot.logger.error("Unhandled error in embassy_approve", exc_info=True)
            try:
                if (
                    interaction
                    and hasattr(interaction, "response")
                    and interaction.response.is_done()
                ):
                    await interaction.followup.send(
                        "An internal error occurred while running this command.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        "An internal error occurred while running this command.",
                        ephemeral=True,
                    )
            except Exception:
                traceback.print_exc()
            return

    @commands.command(name="testwelcome")
    @commands.is_owner()
    async def testwelcome(self, context: commands.Context):
        """Simulate a member join for testing"""
        await self.on_member_join(context.author)


async def setup(bot) -> None:
    await bot.add_cog(Welcome(bot))
