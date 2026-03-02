"""Shared app_commands checks for the WarEra Discord bot."""

import discord
from discord import app_commands

# Role IDs allowed to run privileged commands (in addition to the bot owner)
PRIVILEGED_ROLE_IDS: set[int] = {
    1451180288515506258,  # minister_foreign_affairs / ambassadeur
    1401530996725383178,  # president
    1401531414553428139,  # vice_president
    1458527742646816892,  # government
    1458427087189835776,  # commandant
    1475468331896148079,  # bot_ontwikkelaar
}


def has_privileged_role() -> app_commands.check:
    """app_commands check: owner OR one of the privileged roles (bypassed in test mode)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        bot = interaction.client
        # In test mode everyone is allowed
        if getattr(bot, "testing", False):
            return True
        # Bot owner is always allowed
        app_info = await bot.application_info()
        if interaction.user.id == app_info.owner.id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            user_role_ids = {r.id for r in interaction.user.roles}
            if user_role_ids & PRIVILEGED_ROLE_IDS:
                return True
        raise app_commands.MissingPermissions(["privileged_role"])
    return app_commands.check(predicate)
