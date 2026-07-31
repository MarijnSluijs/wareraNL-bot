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
from .eco_donations import EcoDonationsMixin
from .battle_drops import BattleDropsMixin
from .battle_rankings import BattleRankingsMixin
from .citizens import CitizensMixin
from .company_bonus import CompanyBonusMixin
from .company_move_advice import CompanyMoveAdviceMixin
from .daily_dmg import DailyDmgMixin
from .discord_allies import DiscordAlliesMixin
from .division_overrides import DivisionOverridesMixin
from .events import EventsMixin
from .freshness import FreshnessMixin
from .gems import GemsMixin
from .giveaways_db import GiveawaysMixin
from .identities import IdentityLinksMixin
from .level5_notified import Level5NotifiedMixin
from .luck import LuckMixin
from .mu_subscriptions import MuSubscriptionsMixin
from .mus_registry import MusRegistryMixin
from .pill_reminders import PillRemindersMixin
from .pill_tracking import PillTrackingMixin
from .production import ProductionMixin
from .resistance import ResistanceMixin
from .state import StateMixin
from .trades import TradesMixin
from .tx_cache import TxCacheMixin
from .war_guild import WarGuildMixin
from .war_status import WarStatusMixin
from .wealth import WealthMixin


class Database(
    MuSubscriptionsMixin,
    EcoDonationsMixin,
    DailyDmgMixin,
    GemsMixin,
    DiscordAlliesMixin,
    Level5NotifiedMixin,
    StateMixin,
    ProductionMixin,
    CitizensMixin,
    IdentityLinksMixin,
    EventsMixin,
    GiveawaysMixin,
    LuckMixin,
    ResistanceMixin,
    MusRegistryMixin,
    DivisionOverridesMixin,
    BattleDropsMixin,
    BattleRankingsMixin,
    ArticleTipsMixin,
    CompanyBonusMixin,
    CompanyMoveAdviceMixin,
    PillRemindersMixin,
    PillTrackingMixin,
    TxCacheMixin,
    TradesMixin,
    WarGuildMixin,
    WarStatusMixin,
    WealthMixin,
    FreshnessMixin,
    DatabaseBase,
):
    """Async SQLite database for the WarEra Discord bot.

    Call :meth:`setup` once before using any other method, and :meth:`close`
    when done (or use as an async context manager).
    """


__all__ = ["Database"]
