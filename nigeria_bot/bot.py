"""Nigerian WarEra Discord — verification bot.

Handles verification of Nigerian citizens, Dutch citizens, and embassy members.
Run with: python nigeria_bot/bot.py

Required environment variables:
  TOKEN_NIGERIA   Discord bot token for this application.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands

# Allow importing from the project root (services/, etc.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("nigeria_bot")


class NigeriaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._reconciled_deletions = False

    async def setup_hook(self) -> None:
        # Open persistent DB
        from nigeria_bot.db import open_db
        self.nigeria_db = await open_db()

        # Load the verification cog
        import nigeria_bot.cog as cog_module
        await self.add_cog(cog_module.VerificationCog(self, self.nigeria_db))

        # Load the Nigerian Scam-Economy game
        import nigeria_bot.scam_game as scam_game
        self.scam_game = await scam_game.setup(self, self.nigeria_db)
        import nigeria_bot.scam_targets as scam_targets
        self.scam_targets = await scam_targets.setup(self, self.nigeria_db)
        import nigeria_bot.scam_jail as scam_jail
        self.scam_jail = await scam_jail.setup(self, self.nigeria_db)
        # The fund owns /invest and its own risk-level scheduler.
        import nigeria_bot.royal_fund as royal_fund
        self.royal_fund = await royal_fund.setup(self, self.nigeria_db)
        # /special sits on top of all of the above: its cards reach into the
        # scam, target, fund and jail systems, so it loads last.
        import nigeria_bot.special_game as special_game
        self.special_game = await special_game.setup(self, self.nigeria_db)

        # /fabrieken — reads the hourly company census written by the
        # data-fetcher container into database/external.db.
        import nigeria_bot.fabrieken as fabrieken
        await fabrieken.setup(self)

        # /damage-projection — reads the hourly alliance/citizen sweep written
        # by the data-fetcher container into database/external.db.
        import nigeria_bot.damage_projection as damage_projection
        await damage_projection.setup(self)

        # /productie — same company_census data as /fabrieken, grouped by item
        # for a country, alliance, or the whole game.
        import nigeria_bot.productie as productie
        await productie.setup(self)

        # /oliegebruik — live API calls (bunker status isn't in the hourly
        # sweep), scoped to Nigeria-controlled regions.
        import nigeria_bot.oliegebruik as oliegebruik
        await oliegebruik.setup(self)

        # Register persistent views so buttons survive restarts
        from nigeria_bot.cog import VerificationView, TicketActionView
        self.add_view(VerificationView())
        self.add_view(TicketActionView())
        from nigeria_bot.scam_game import BegView, OperationView
        # free_entry=True so *both* join custom_ids are registered — a view
        # built without the free seat never registers that button, and every
        # free seat posted before the restart would go dead.
        self.add_view(OperationView(free_entry=True))
        self.add_view(BegView())
        from nigeria_bot.scam_targets import CounterScamButton, TargetBoardView
        self.add_view(TargetBoardView())
        # The Counter-Scam offer carries its report id in the custom_id, so it
        # cannot be a fixed persistent view — register the template instead, or
        # every offer issued before a restart answers with nothing at all.
        self.add_dynamic_items(CounterScamButton)
        from nigeria_bot.scam_jail import AppealView
        self.add_view(AppealView())
        from nigeria_bot.special_game import RogerView, SpecialEventButton
        self.add_view(RogerView())
        # Public /special events carry their id in the custom_id, so the
        # template is registered rather than a fixed view — otherwise a
        # three-minute bait posted before a redeploy would go dead and look
        # exactly like a bait nobody fell for.
        self.add_dynamic_items(SpecialEventButton)

        # Sync slash commands to the guild
        from nigeria_bot.cog import GUILD_ID
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Slash commands synced to guild %d", GUILD_ID)

    async def on_ready(self) -> None:
        logger.info("Nigeria bot ready — logged in as %s", self.user)
        # on_ready can fire again on gateway reconnects — only resume pending
        # ticket deletions once per process, not on every reconnect.
        if not self._reconciled_deletions:
            self._reconciled_deletions = True
            from nigeria_bot.cog import _reconcile_pending_deletions
            await _reconcile_pending_deletions(self, self.nigeria_db)
            # Resume any scam operation whose window elapsed while we were down.
            try:
                await self.scam_game.reconcile()
            except Exception:
                logger.exception("Failed to reconcile scam operations")


def main() -> None:
    token = os.environ.get("TOKEN_NIGERIA")
    if not token:
        logger.error("TOKEN_NIGERIA environment variable not set")
        sys.exit(1)

    bot = NigeriaBot()
    asyncio.run(bot.start(token))


if __name__ == "__main__":
    main()
