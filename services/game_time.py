"""WarEra game-clock helpers.

The game does not use plain UTC calendar days/weeks for damage accounting:

* A **game day** runs from 02:00 UTC to 02:00 UTC.  Damage done at 01:30 UTC
  on the 5th still belongs to the game day labelled ``2026-08-04``.
* A **game week** starts Monday 02:00 UTC — that is when the API's
  ``weeklyUserDamages`` counter resets to zero.  Note the week boundary is
  also a day boundary, which is what makes the daily-damage deltas in
  :mod:`services.db.damage_history` line up cleanly.

Everything that buckets damage into days or weeks must go through these
helpers so the Discord commands, the website and the fetcher all agree on
where a boundary sits.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# Hour (UTC) at which a new game day — and, on Mondays, a new game week — starts.
GAME_DAY_START_HOUR = 2


def _shift(dt: datetime | None = None) -> datetime:
    """Return *dt* (default: now) shifted back by the game-day offset.

    After shifting, ordinary calendar-date arithmetic gives game-day answers.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc) - timedelta(hours=GAME_DAY_START_HOUR)


def game_day(dt: datetime | None = None) -> str:
    """Return the ``YYYY-MM-DD`` label of the game day containing *dt*."""
    return _shift(dt).strftime("%Y-%m-%d")


def game_week_start(dt: datetime | None = None) -> str:
    """Return the ``YYYY-MM-DD`` date of the Monday starting *dt*'s game week."""
    shifted = _shift(dt)
    monday = shifted.date() - timedelta(days=shifted.weekday())
    return monday.isoformat()


def week_start_of_day(game_date: str) -> str:
    """Return the game-week start (Monday) for a ``YYYY-MM-DD`` game day."""
    d = date.fromisoformat(game_date)
    return (d - timedelta(days=d.weekday())).isoformat()


def iso_week_label(week_start: str) -> str:
    """Return a human ``YYYY-Www`` label for a ``YYYY-MM-DD`` week start."""
    try:
        d = date.fromisoformat(week_start)
    except (TypeError, ValueError):
        return week_start or ""
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def game_day_shift_days(game_date: str, days: int) -> str:
    """Return the game day *days* away from *game_date* (negative = earlier)."""
    return (date.fromisoformat(game_date) + timedelta(days=days)).isoformat()


def fmt_game_day(game_date: str) -> str:
    """Return a Dutch-ish display label (``4 augustus 2026``) for a game day."""
    months = (
        "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    )
    try:
        d = date.fromisoformat(game_date)
    except (TypeError, ValueError):
        return game_date or ""
    return f"{d.day} {months[d.month - 1]} {d.year}"


def fmt_week_range(week_start: str) -> str:
    """Return ``4 aug – 10 aug 2026`` style label for a game week."""
    short = (
        "jan", "feb", "mrt", "apr", "mei", "jun",
        "jul", "aug", "sep", "okt", "nov", "dec",
    )
    try:
        start = date.fromisoformat(week_start)
    except (TypeError, ValueError):
        return week_start or ""
    end = start + timedelta(days=6)
    return (
        f"{start.day} {short[start.month - 1]} – "
        f"{end.day} {short[end.month - 1]} {end.year}"
    )
