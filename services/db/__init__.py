"""WarEra bot database module.

The :class:`Database` class is the single entry point for all DB operations.
It is composed of domain-specific mixins:

===================  ========================================
Mixin                Tables
===================  ========================================
:mod:`.state`        ``poll_state``, ``jobs``
:mod:`.production`   ``country_snapshots``, ``specialization_top``, ``deposit_top``
:mod:`.citizens`     ``citizen_levels``
:mod:`.events`       ``seen_articles``, ``seen_events``, ``war_events``
:mod:`.luck`         ``citizen_luck``
:mod:`.resistance`   ``resistance_state``
===================  ========================================

Usage::

    db = Database("database/external.db")
    await db.setup()
    await db.set_poll_state("my_key", "value")
    await db.close()
"""

from .base import DatabaseBase
from .state import StateMixin
from .production import ProductionMixin
from .citizens import CitizensMixin
from .events import EventsMixin
from .luck import LuckMixin
from .resistance import ResistanceMixin


class Database(
    StateMixin,
    ProductionMixin,
    CitizensMixin,
    EventsMixin,
    LuckMixin,
    ResistanceMixin,
    DatabaseBase,
):
    """Async SQLite database for the WarEra Discord bot.

    Call :meth:`setup` once before using any other method, and :meth:`close`
    when done (or use as an async context manager).
    """


__all__ = ["Database"]
