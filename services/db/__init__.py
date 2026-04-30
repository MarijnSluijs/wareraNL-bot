"""WarEra bot database module.

The :class:`Database` class is the single entry point for all DB operations.
It is composed of domain-specific mixins:

===================  ========================================
Mixin                Tables
===================  ========================================
:mod:`.state`        ``poll_state``, ``jobs``
:mod:`.production`   ``country_snapshots``, ``specialization_top``, ``deposit_top``
:mod:`.citizens`     ``citizen_levels``, ``citizen_weekly_damages``
:mod:`.events`       ``seen_articles``, ``seen_events``, ``war_events``
:mod:`.luck`         ``citizen_luck``
:mod:`.resistance`   ``resistance_state``
:mod:`.mus_registry` ``known_mus``
:mod:`.battle_drops`    ``battle_drops``
:mod:`.battle_rankings` ``battle_hits``, ``processed_battles``
:mod:`.article_tips`    ``article_tips``
:mod:`.company_bonus`   ``company_bonus_watchers``, ``company_bonus_alerts``
:mod:`.gems`            ``event_gems``
:mod:`.tx_cache`        ``player_tx_cache``
:mod:`.trades`          ``item_trades``
===================  ========================================
Usage::

    db = Database("database/external.db")
    await db.setup()
    await db.set_poll_state("my_key", "value")
    await db.close()
"""

from .article_tips import ArticleTipsMixin
from .base import DatabaseBase
from .gems import GemsMixin
from .discord_allies import DiscordAlliesMixin
from .battle_drops import BattleDropsMixin
from .battle_rankings import BattleRankingsMixin
from .citizens import CitizensMixin
from .company_bonus import CompanyBonusMixin
from .company_move_advice import CompanyMoveAdviceMixin
from .pill_reminders import PillRemindersMixin
from .pill_tracking import PillTrackingMixin
from .trades import TradesMixin
from .tx_cache import TxCacheMixin
from .events import EventsMixin
from .giveaways_db import GiveawaysMixin
from .identities import IdentityLinksMixin
from .luck import LuckMixin
from .mus_registry import MusRegistryMixin
from .production import ProductionMixin
from .resistance import ResistanceMixin
from .state import StateMixin
from .wealth import WealthMixin


class Database(
    GemsMixin,
    DiscordAlliesMixin,
    StateMixin,
    ProductionMixin,
    CitizensMixin,
    IdentityLinksMixin,
    EventsMixin,
    GiveawaysMixin,
    LuckMixin,
    ResistanceMixin,
    MusRegistryMixin,
    BattleDropsMixin,
    BattleRankingsMixin,
    ArticleTipsMixin,
    CompanyBonusMixin,
    CompanyMoveAdviceMixin,
    PillRemindersMixin,
    PillTrackingMixin,
    TxCacheMixin,
    TradesMixin,
    WealthMixin,
    DatabaseBase,
):
    """Async SQLite database for the WarEra Discord bot.

    Call :meth:`setup` once before using any other method, and :meth:`close`
    when done (or use as an async context manager).
    """


__all__ = ["Database"]
