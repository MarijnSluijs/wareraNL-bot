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

logger = logging.getLogger("discord_bot")


class MUOnboardingView(discord.ui.View):
    """
    Persistent view containing the three verification buttons.

    Using timeout=None and custom_id makes these buttons persist
    across bot restarts - they'll still work after the bot reconnects.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="MU Aanmelden",
        style=discord.ButtonStyle.success,
        custom_id="mu_creation",
        # emoji="🇳🇱",
    )
    async def mu_creation_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        """Handle citizen verification request."""
        await interaction.response.send_modal(
            MUApplicationModal()
        )


class MUApplicationModal(discord.ui.Modal):
    """Questionnaire shown to users before opening a verification ticket."""

    def __init__(self):
        super().__init__(
            title="MU Aanmelden"
        )

        self.warera_name = discord.ui.TextInput(
            label="WarEra gebruikersnaam",
            placeholder=(
                "Vul je in-game naam in"
            ),
            required=True,
            max_length=64,
        )
        self.mu_link = discord.ui.TextInput(
            label=(
                "Link naar de in-game pagina van de MU"
            ),
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Plak de MU link"
            ),
            required=True,
            max_length=500,
        )
        self.extra_info = discord.ui.TextInput(
            label="Aanvullende info",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Optioneel: extra info (bijv. mede-commandanten)"
            ),
            required=False,
            max_length=500,
        )

        self.add_item(self.warera_name)
        self.add_item(self.mu_link)
        self.add_item(self.extra_info)

    async def on_submit(self, interaction: discord.Interaction):
        questionnaire_answers = {
            (
                "WarEra gebruikersnaam"
            ): str(self.warera_name).strip(),
            (
                "MU URL"
            ): str(self.mu_link).strip(),
        }
        extra = str(self.extra_info).strip()
        if extra:
            questionnaire_answers[
                "Aanvullende info"
            ] = extra

        await create_mu_request_channel(
            interaction,
            questionnaire_answers=questionnaire_answers,
        )


async def create_mu_request_channel(
    interaction: discord.Interaction,
    questionnaire_answers: dict[str, str] | None = None,
) -> None:
    """
    Create a private mu request channel for the user.

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
        f"Creating verification channel for {user.name} (MU request) in guild {guild.name}"
    )

    ticket_id = int(datetime.datetime.utcnow().timestamp())

    channels_cfg = config.get("channels", {})

    # Configure channel properties based on request type
    roles_cfg = config.get("roles", {})
    
    channel_name = f"mu-{ticket_id}-{user.name}"
    # Embassy requests notify multiple high-level roles
    role_ids = [
        roles_cfg.get("officier"),
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
            topic=f"MU request by {user.name} | ID: {ticket_id} | User ID: {user.id}",
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
        description=f"**Gebruiker:** {user.mention}\n**Type:** MU aanvraag\n**Ticket ID:** #{ticket_id}",
        color=embed_color,
        timestamp=datetime.datetime.now(datetime.UTC),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(
        name="Instructies voor Moderators",
        value="Gebruik `/voegmu` om de MU toe te voegen",
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


    instructions_embed = discord.Embed(
        description="Bedankt voor je MU aanmelding! We nemen hem zo snel mogelijk in behandling."
        "Mocht je al een idee hebben voor een logo voor de MU, stuur die dan hier.",
        color=embed_color,
    )
    await channel.send(content=user.mention, embed=instructions_embed)

    
    await interaction.response.send_message(
        f"Je MU-aanvraag kanaal is aangemaakt: {channel.mention}\n"
        "Wacht op een moderator om je verzoek te beoordelen.",
        ephemeral=True,
    )


class MURequest(commands.Cog, name="murequest"):
    """Cog for welcome messages and verification system."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.bot.logger.info("MU Request cog initialized")
        # Add the persistent view when the cog is loaded
        self.bot.add_view(MUOnboardingView(bot))
        # Use the central bot configuration
        self.config = getattr(self.bot, "config", {}) or {}

    @commands.command(
        name="postmuapp",
        description="Post the MU application message with verification buttons (admin only)",
    )
    @commands.has_permissions(administrator=True)
    async def post_mu_application(self, ctx: commands.Context):
        # Create the welcome embed
        embed = discord.Embed(
            title="🇳🇱 MU Aanmelden",
            description="Klik op de onderstaande knop om je MU aan te melden bij de overheid.\n\n"
            "Momenteel zijn we **niet** op zoek naar nieuwe MU's, maar het staat je vrij om er eentje op te richten.",
            color=discord.Color.gold(),
            timestamp=datetime.datetime.now(datetime.UTC),
        )
        embed.set_thumbnail(
            url="https://jorisvanderbijl.nl/wp-content/uploads/2022/05/ministerie-defensie-logo-480x480.png"
        )
        # embed.set_author(name=member.name, icon_url=member.display_avatar.url)
        # embed.set_footer(text=f"Member #{self.bot.guild.member_count}")

        # Send welcome message with verification buttons
        channel_id = self.bot.config.get("channels", {}).get("mu_aanmelden")
        if not channel_id:
            await ctx.send("MU aanmelden channel ID not configured in bot config.")
            return

        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            await ctx.send(
                "MU aanmelden channel not found. Please check the channel ID in bot config."
            )
            return

        # clear channel to repost button
        await channel.purge(limit=1)

        await channel.send(embed=embed, view=MUOnboardingView(self.bot))



async def setup(bot) -> None:
    await bot.add_cog(MURequest(bot))
