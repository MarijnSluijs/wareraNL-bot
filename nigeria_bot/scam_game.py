"""Nigerian Scam-Economy — a satirical economy game for the Nigeria guild.

Three ways to make (and lose) Naira:

``/scam``
    A personal, cooldown-gated gamble.  Mostly small wins, sometimes a
    jackpot, sometimes it blows up in your face.

``/quickscam``
    A short pooled job: one player fronts the setup cost, everybody else buys
    in during a 10-minute window, then the whole pot is multiplied by a random
    outcome and paid back out in proportion to each contribution.

``/invest``
    The Fully Trustworthy Royal Investment Fund — a Ponzi scheme that pays
    generous interest right up until the moment it doesn't.  Players choose
    when to walk away.

Everything is persisted in ``database/nigeria.db`` so balances, open
operations and fund positions survive restarts.  Open operations and pending
fund events are reconciled on startup, so a restart mid-window still resolves
instead of silently swallowing everyone's stake.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import quickscam_templates as qs

logger = logging.getLogger("nigeria_bot.scam_game")

# ── Configuration ─────────────────────────────────────────────────────────────

GAME_CHANNEL_ID  = 1534999547514716220   # where the game commands may be used
RULES_CHANNEL_ID = 1534999477436551180   # where /scamrules may be used
RULES_CHANNEL_URL = (
    "https://discord.com/channels/1495375733323989074/1534999477436551180"
)
GAME_CHANNEL_URL = (
    "https://discord.com/channels/1495375733323989074/1534999547514716220"
)

CURRENCY = "Naira"
START_BALANCE = 1_000

# /scam has no hard cooldown — instead your odds recover over time.  Scamming
# again immediately is allowed but a bad idea; see scam_readiness().
# How often a player may hold out the hat.
# ── Jail ──────────────────────────────────────────────────────────────────
# A scam loss you cannot cover in cash gets you arrested, and the unpaid
# amount becomes your bribe.  This is what stops the fund being a bunker:
# hiding everything in the fund no longer makes a failed scam free, it just
# means you have to withdraw to buy your way out.
JAIL_ENABLED              = True
# Poverty is judged on cash + fund, so 0 cash and a fat fund is not "broke".
INDIGENT_WEALTH_THRESHOLD = 1_000
INDIGENT_MAX_JAIL_MINUTES = 40
WEALTHY_MAX_JAIL_MINUTES  = 240   # four hours
APPEAL_DURATION_SECONDS   = 60

BEG_COOLDOWN_HOURS = 1.0
BEG_MIN_DONATION   = 1
# One-click amounts on a begging post; anything else goes via the modal.
BEG_PRESETS        = (1, 5, 10, 50, 100)

SCAM_BREAKEVEN_HOURS = 2.0   # odds are an even bet again (zero expected profit)
SCAM_FULL_HOURS      = 3.0   # fully recovered, normal odds
# Readiness value at which expected profit is exactly zero, solved from the
# outcome table (see _SCAM_COLD_SCALING / SCAM_COLD_LOSS_MULT).
_BREAKEVEN_READINESS = 0.465

# Quick scams are template-driven (see quickscam_templates.py): stake limits,
# sign-up window, participant caps and odds all come from whichever template
# gets rolled.  What is global is the trigger cooldown — the right to *create*
# an opportunity for the whole server is the scarce thing, not the buy-in.
#
# 2 hours rather than the 6 the design note suggested.  With one operation at a
# time and an average sign-up window of ~40 minutes, the single slot already
# caps the game at roughly 36 operations a day; a 6-hour personal timer just
# means somebody who is online for one evening gets a single trigger and then
# bounces off the command.  Scarcity should come from the slot everyone is
# competing for, not from a private stopwatch.
QUICKSCAM_TRIGGER_COOLDOWN_HOURS = 2.0

# Once every seat is taken there is nothing left to wait for — a full house
# just leaves the room staring at a countdown nobody can act on.
QUICKSCAM_FULL_HOUSE_MINUTES = 2

# ⚠️ ASSUMPTION.  An Extreme Failure arrests participants, but the design note
# never says what the bribe should be.  It is what the operation cost you —
# floored, because a free seat's arrest would otherwise carry a zero bribe and
# jail would be a formality rather than a consequence.
EXTREME_FAILURE_MIN_BRIBE = 400

# Quoted by /scamrules.  Defined here rather than imported from scam_targets,
# which imports *this* module — the rules text must never create a cycle.
INTEL_MAX_CHARGES     = 3
INTEL_RECHARGE_HOURS  = 2.0
FAKE_COVER_DEPOSIT    = 500
PROTECTED_WEALTH_FLOOR = 1_000

# Mirrors ATTEMPT_COOLDOWN_MINUTES in scam_targets.py; kept here so the
# rules text can quote it without importing that module (which imports
# this one).
TARGET_ATTEMPT_COOLDOWN = 15

_EMBED_GOLD = discord.Colour(0xD4AF37)
_EMBED_GREEN = discord.Colour(0x2ECC71)
_EMBED_RED = discord.Colour(0xE74C3C)
_EMBED_GREY = discord.Colour(0x95A5A6)


def money(amount: float) -> str:
    """Format an amount as ``1.234 Naira`` (Dutch-style thousands separators)."""
    return f"{int(round(amount)):,}".replace(",", ".") + f" {CURRENCY}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Schema ────────────────────────────────────────────────────────────────────

async def setup_schema(conn: aiosqlite.Connection) -> None:
    """Create the game tables.  Safe to call on every startup."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_players (
            discord_user_id TEXT PRIMARY KEY,
            balance         INTEGER NOT NULL DEFAULT 0,
            invested        INTEGER NOT NULL DEFAULT 0,
            total_earned    INTEGER NOT NULL DEFAULT 0,
            total_lost      INTEGER NOT NULL DEFAULT 0,
            scams_run       INTEGER NOT NULL DEFAULT 0,
            last_scam_at    TEXT,
            created_at      TEXT NOT NULL
        )
    """)
    # Every movement of a player's money, so /balance can answer "where did
    # that go?" without anybody scrolling back through a busy channel.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_ledger (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            amount          INTEGER NOT NULL,
            kind            TEXT NOT NULL,
            detail          TEXT,
            at              TEXT NOT NULL
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scam_ledger_user"
        " ON scam_ledger(discord_user_id, id DESC)"
    )
    # One row per gamble: what it was *worth* taking, and what it actually
    # paid.  Kept apart from the ledger because a single play can move money
    # several times (stake out, payout in) while being one decision.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_plays (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            kind            TEXT NOT NULL,
            expected        REAL NOT NULL,
            actual          INTEGER NOT NULL,
            detail          TEXT,
            at              TEXT NOT NULL
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scam_plays_user"
        " ON scam_plays(discord_user_id)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_operations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     TEXT NOT NULL,
            channel_id   TEXT NOT NULL,
            message_id   TEXT,
            initiator_id TEXT NOT NULL,
            title        TEXT NOT NULL,
            blurb        TEXT NOT NULL,
            resolve_at   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'open'
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_operation_entries (
            operation_id    INTEGER NOT NULL,
            discord_user_id TEXT NOT NULL,
            amount          INTEGER NOT NULL,
            PRIMARY KEY (operation_id, discord_user_id)
        )
    """)
    # Quick scams became template-driven: which template was rolled, and when
    # each player joined (the pyramid pays by join order, so the order has to
    # survive a restart rather than being inferred from stake size).
    for column in (
        "template_id TEXT",
        "initiator_paid INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await conn.execute(f"ALTER TABLE scam_operations ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    try:
        await conn.execute(
            "ALTER TABLE scam_operation_entries ADD COLUMN joined_at TEXT"
        )
    except Exception:
        pass
    # One row per posted /beg, so a single static button custom_id can route a
    # donation to the right beggar by looking up the message it was clicked on.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_begs (
            message_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            beggar_id  TEXT NOT NULL,
            created_at TEXT NOT NULL,
            total      INTEGER NOT NULL DEFAULT 0,
            donors     INTEGER NOT NULL DEFAULT 0
        )
    """)
    for column in (
        "last_beg_at TEXT",
        "donated_total INTEGER NOT NULL DEFAULT 0",
        "received_total INTEGER NOT NULL DEFAULT 0",
        "begs_posted INTEGER NOT NULL DEFAULT 0",
        "donations_made INTEGER NOT NULL DEFAULT 0",
        "jail_until TEXT",
        "jailed_at TEXT",
        "bribe_remaining INTEGER NOT NULL DEFAULT 0",
        "jail_sentence TEXT",
        "times_jailed INTEGER NOT NULL DEFAULT 0",
        "last_quickscam_at TEXT",
        "scam_help_seen INTEGER NOT NULL DEFAULT 0",
        # Owned by the targets module, but get_player selects them — declaring
        # them here too removes the load-order dependency between the schemas.
        "fake_target_until TEXT",
        "last_target_at TEXT",
        "intel_lock_until TEXT",
        "bails_given INTEGER NOT NULL DEFAULT 0",
        "bails_received INTEGER NOT NULL DEFAULT 0",
        "bribes_paid INTEGER NOT NULL DEFAULT 0",
        "appeals_won INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await conn.execute(f"ALTER TABLE scam_players ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    await conn.commit()


# ── Player helpers ────────────────────────────────────────────────────────────

async def get_player(conn: aiosqlite.Connection, user_id: str) -> dict:
    """Return the player row, creating it with the starting balance if new."""
    async with conn.execute(
        "SELECT discord_user_id, balance, invested, total_earned, total_lost,"
        " scams_run, last_scam_at, last_quickscam_at, fake_target_until,"
        " scam_help_seen, last_target_at, intel_lock_until, target_lock_until,"
        " silenced_until FROM scam_players WHERE discord_user_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        await conn.execute(
            "INSERT INTO scam_players (discord_user_id, balance, created_at)"
            " VALUES (?, ?, ?)",
            (user_id, START_BALANCE, _iso(_now())),
        )
        await conn.commit()
        return {
            "discord_user_id": user_id, "balance": START_BALANCE, "invested": 0,
            "total_earned": 0, "total_lost": 0, "scams_run": 0, "last_scam_at": None,
            "last_quickscam_at": None, "fake_target_until": None,
            "scam_help_seen": 0, "last_target_at": None,
            "intel_lock_until": None, "target_lock_until": None,
            "silenced_until": None, "is_new": True,
        }
    # Every cooldown column belongs here.  A caller that reads a field this
    # query forgot gets None back and silently skips its cooldown — which is
    # exactly how the quick scam trigger limit stopped working.
    return {
        "discord_user_id": row[0], "balance": int(row[1]), "invested": int(row[2]),
        "total_earned": int(row[3]), "total_lost": int(row[4]),
        "scams_run": int(row[5]), "last_scam_at": row[6],
        "last_quickscam_at": row[7], "fake_target_until": row[8],
        "scam_help_seen": int(row[9] or 0), "last_target_at": row[10],
        "intel_lock_until": row[11], "target_lock_until": row[12],
        "silenced_until": row[13], "is_new": False,
    }


async def maybe_send_help(
    interaction: discord.Interaction, conn: aiosqlite.Connection, player: dict
) -> None:
    """Show the command guide once, to a player's first Scam Economy action.

    Sent as a *follow-up* and never awaited before the action itself, so a new
    player's first command still does what they asked — the help arrives
    alongside the result rather than instead of it.
    """
    if player.get("scam_help_seen"):
        return
    uid = str(interaction.user.id)
    await conn.execute(
        "UPDATE scam_players SET scam_help_seen = 1 WHERE discord_user_id = ?",
        (uid,),
    )
    await conn.commit()
    embed = scamhelp_embed()
    embed.title = "🇳🇬 WELCOME TO THE NIGERIAN SCAM ECONOMY"
    embed.description = (
        f"You have **{money(START_BALANCE)}** and several questionable ways "
        "to use it.\n\nHere is the quick command guide — you can always use "
        "**/scamhelp** again later.\n\n"
    ) + embed.description
    try:
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception:
        logger.debug("scam_game: could not deliver first-time help to %s", uid)


async def adjust_balance(
    conn: aiosqlite.Connection, user_id: str, delta: int,
    reason: str = "other", detail: Optional[str] = None,
) -> int:
    """Apply *delta* to a player's balance (never below zero); return the new value.

    Every cash movement in the game funnels through here, so this is also
    where the ledger is written.  ``reason`` defaults to ``"other"`` rather
    than being required: a call site that forgets it still records the money,
    which is far better than silently losing a row from the history.
    """
    await get_player(conn, user_id)
    if delta >= 0:
        await conn.execute(
            "UPDATE scam_players SET balance = balance + ?, total_earned = total_earned + ?"
            " WHERE discord_user_id = ?",
            (delta, delta, user_id),
        )
    else:
        # MAX() keeps a player from going into debt — the joke stops being funny
        # when someone can never dig themselves out again.
        await conn.execute(
            "UPDATE scam_players SET balance = MAX(0, balance + ?),"
            " total_lost = total_lost + ? WHERE discord_user_id = ?",
            (delta, -delta, user_id),
        )
    if delta:
        await conn.execute(
            "INSERT INTO scam_ledger (discord_user_id, amount, kind, detail, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(user_id), int(delta), reason, detail, _iso(_now())),
        )
    await conn.commit()
    async with conn.execute(
        "SELECT balance FROM scam_players WHERE discord_user_id = ?", (user_id,)
    ) as cur:
        return int((await cur.fetchone())[0])


async def record_ledger(
    conn: aiosqlite.Connection, user_id: str, amount: int,
    reason: str, detail: Optional[str] = None,
) -> None:
    """Log a movement that did not go through :func:`adjust_balance`.

    Used for changes to a player's *fund position* — that money never touches
    their cash balance, but it is very much theirs and "where did it go?" is
    exactly the question the history exists to answer.
    """
    if not amount:
        return
    await conn.execute(
        "INSERT INTO scam_ledger (discord_user_id, amount, kind, detail, at)"
        " VALUES (?, ?, ?, ?, ?)",
        (str(user_id), int(amount), reason, detail, _iso(_now())),
    )


async def record_play(
    conn: aiosqlite.Connection, user_id: str, kind: str,
    expected: float, actual: int, detail: Optional[str] = None,
) -> None:
    """Log one gamble's expected value against what it really paid.

    Comparing the two over a career separates *skill* (picking bets worth
    taking) from *luck* (how the dice actually fell) — which is otherwise
    impossible to tell apart in a game this swingy.
    """
    await conn.execute(
        "INSERT INTO scam_plays (discord_user_id, kind, expected, actual, detail, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(user_id), kind, float(expected), int(actual), detail, _iso(_now())),
    )


async def play_stats(
    conn: aiosqlite.Connection, user_id: str
) -> list[tuple[str, int, float, int]]:
    """``(kind, plays, total expected, total actual)`` per kind, then a total."""
    rows: list[tuple[str, int, float, int]] = []
    async with conn.execute(
        "SELECT kind, COUNT(*), COALESCE(SUM(expected), 0), COALESCE(SUM(actual), 0)"
        " FROM scam_plays WHERE discord_user_id = ? GROUP BY kind ORDER BY COUNT(*) DESC",
        (str(user_id),),
    ) as cur:
        async for r in cur:
            rows.append((str(r[0]), int(r[1]), float(r[2]), int(r[3])))
    return rows


PLAY_LABELS = {
    "scam": "💸 Solo scams",
    "quickscam": "🤝 Quick scams",
    "target": "🎯 Target attempts",
}


# How each ledger kind is rendered in /balance.
LEDGER_LABELS = {
    "scam":             ("💸", "Solo scam"),
    "scam_bribe":       ("🚔", "Bribe paid from a failed scam"),
    "quickscam_stake":  ("🤝", "Quick scam stake"),
    "quickscam_payout": ("🤝", "Quick scam payout"),
    "quickscam_setup":  ("🤝", "Quick scam setup cost"),
    "target_cost":      ("🎯", "Target attempt"),
    "target_payout":    ("🎯", "Target payout"),
    "target_counter":   ("👑", "Counter-scammed by a mark"),
    "intel":            ("🔎", "Intel mission"),
    "beg_sent":         ("🪙", "Donation given"),
    "beg_received":     ("🪙", "Donation received"),
    "fund_deposit":     ("🏦", "Into the fund"),
    "fund_withdraw":    ("🏦", "Out of the fund"),
    "fund_tax":         ("😰", "Anti-panic tax"),
    "fund_dividend":    ("🏦", "Fund dividend"),
    "fund_gift":        ("🏦", "Roger's promotional money"),
    "fund_position":    ("🏦", "Fund position change"),
    "fund_tax_audit":   ("🧾", "Federal tax audit"),
    "jail_bribe":       ("🚔", "Bribe paid"),
    "bail_paid":        ("🤝", "Bail paid for somebody"),
    "bail_received":    ("🤝", "Bailed out"),
    "fake_deposit":     ("🎭", "Disguise cover deposit"),
    "fake_refund":      ("🎭", "Cover deposit returned"),
    "fake_win":         ("🎭", "Robbed somebody while disguised"),
    "fake_loss":        ("🎭", "Robbed by a fake target"),
    "counter_stake":    ("🎭", "Counter-scam stake"),
    "counter_win":      ("🎭", "Counter-scam winnings"),
    "counter_loss":     ("🎭", "Lost to a counter-scam"),
    "other":            ("•", "Adjustment"),
}


LEDGER_SHOWN = 10


def _signed_money(amount: float) -> str:
    return ("+" if amount >= 0 else "−") + money(abs(amount))


def _target_readiness(player: dict) -> str:
    """When this player may next work a mark.

    Two separate things gate the board — the ordinary attempt cooldown and the
    Intel action lock — and either can be the binding one.  Quoting only the
    attempt cooldown would tell somebody they are ready when they are not.
    """
    now = _now()
    waits: list[tuple[datetime, str]] = []

    last = player.get("last_target_at")
    if last:
        ready = _parse(last) + timedelta(minutes=TARGET_ATTEMPT_COOLDOWN)
        if ready > now:
            waits.append((ready, "attempt cooldown"))

    lock = player.get("intel_lock_until")
    if lock:
        until = _parse(lock)
        if until > now:
            waits.append((until, "intel team still returning"))

    # Roas blocks the entire board, which outranks the ordinary pause.
    blocked = player.get("target_lock_until")
    if blocked:
        until = _parse(blocked)
        if until > now:
            waits.append((until, "🚫 Roas is blocking the road"))

    if not waits:
        if not last:
            return "**Ready** — never worked a mark yet." + _silence_line(player)
        return (
            f"**Ready now.** Last attempt <t:{int(_parse(last).timestamp())}:R>."
            + _silence_line(player)
        )

    ready, why = max(waits, key=lambda w: w[0])
    mins = int((ready - now).total_seconds() // 60)
    secs = int((ready - now).total_seconds() % 60)
    left = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
    line = (
        f"⏳ **{left}** — next attempt <t:{int(ready.timestamp())}:R>\n"
        f"_Blocked by: {why}._"
    )
    if last:
        line += f"\nLast attempt <t:{int(_parse(last).timestamp())}:R>"
    return line + _silence_line(player)


def _silence_line(player: dict) -> str:
    """Babu's decree, if it is still running."""
    until = player.get("silenced_until")
    if not until:
        return ""
    when = _parse(until)
    if when <= _now():
        return ""
    return (
        f"\n🤐 **Silenced in the game channel** until "
        f"<t:{int(when.timestamp())}:R> — commands still work."
    )


def _ledger_line(entry: tuple[int, str, Optional[str], str]) -> str:
    """One history row: when, what, and how much moved."""
    amount, kind, detail, at = entry
    icon, label = LEDGER_LABELS.get(kind, LEDGER_LABELS["other"])
    sign = "+" if amount > 0 else "−"
    when = int(_parse(at).timestamp())
    tail = f" · _{detail}_" if detail else ""
    return (
        f"{icon} **{sign}{money(abs(amount))}** — {label}{tail} "
        f"· <t:{when}:R>"
    )


async def ledger_for(
    conn: aiosqlite.Connection, user_id: str, limit: int = 10
) -> list[tuple[int, str, Optional[str], str]]:
    """The player's most recent money movements, newest first."""
    rows = []
    async with conn.execute(
        "SELECT amount, kind, detail, at FROM scam_ledger"
        " WHERE discord_user_id = ? ORDER BY id DESC LIMIT ?",
        (str(user_id), limit),
    ) as cur:
        async for r in cur:
            rows.append((int(r[0]), str(r[1]), r[2], str(r[3])))
    return rows





_EMBED_MESSAGE_LIMIT = 5800   # Discord allows 6000 across all embeds; leave slack
_EMBEDS_PER_MESSAGE  = 10


def _embed_length(embed: discord.Embed) -> int:
    return (
        len(embed.title or "")
        + len(embed.description or "")
        + sum(len(f.name) + len(f.value) for f in embed.fields)
        + len(getattr(embed.footer, "text", "") or "")
    )


def batch_embeds(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Split embeds into messages that fit Discord's limits.

    The cap is 6000 characters across *all* embeds in a single message, not
    per embed — which is what made a grown-up /scamrules fail outright with a
    400 rather than simply truncating.
    """
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    used = 0
    for embed in embeds:
        size = _embed_length(embed)
        if current and (
            used + size > _EMBED_MESSAGE_LIMIT
            or len(current) >= _EMBEDS_PER_MESSAGE
        ):
            batches.append(current)
            current, used = [], 0
        current.append(embed)
        used += size
    if current:
        batches.append(current)
    return batches


# ── Jail state ────────────────────────────────────────────────────────────────

_SENTENCES = [
    "17 years", "Life imprisonment", "Four consecutive life sentences",
    "300 years", "Until the sun goes out", "Twelve years of hard paperwork",
    "One (1) eternity", "99 years and a strongly worded letter",
]


async def total_wealth(conn: aiosqlite.Connection, user_id: str) -> int:
    """Cash plus fund stake — the figure poverty is judged on."""
    p = await get_player(conn, user_id)
    return p["balance"] + p["invested"]


async def get_jail(conn: aiosqlite.Connection, user_id: str) -> Optional[dict]:
    """Return the player's jail record, or None if they are free.

    Releases them automatically when their sentence has run out, so no
    background task is strictly required for correctness — the loop just makes
    the release announcement timely.
    """
    async with conn.execute(
        "SELECT jail_until, jailed_at, bribe_remaining, jail_sentence"
        " FROM scam_players WHERE discord_user_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    until = _parse(str(row[0]))
    if until <= _now():
        await release_player(conn, user_id)
        return None
    return {
        "until": until,
        "jailed_at": row[1],
        "bribe": int(row[2] or 0),
        "sentence": row[3] or "17 years",
    }


def is_indigent(wealth: int, bribe: int) -> bool:
    """True when a player should get the lenient treatment.

    Being under the wealth threshold counts, but so does being handed a bribe
    larger than everything you own — otherwise somebody worth 1.176 with a
    2.417 bribe gets the *wealthy* sentence and no right of appeal despite
    having no possible way to pay.
    """
    return wealth < INDIGENT_WEALTH_THRESHOLD or bribe > wealth


async def arrest_player(
    conn: aiosqlite.Connection, user_id: str, bribe: int, wealth: int
) -> dict:
    """Jail a player with an outstanding bribe. Returns the jail record."""
    indigent = is_indigent(wealth, bribe)
    minutes = (
        INDIGENT_MAX_JAIL_MINUTES if indigent else WEALTHY_MAX_JAIL_MINUTES
    )
    until = _now() + timedelta(minutes=minutes)
    sentence = random.choice(_SENTENCES)
    await conn.execute(
        "UPDATE scam_players SET jail_until = ?, jailed_at = ?,"
        " bribe_remaining = ?, jail_sentence = ?,"
        " times_jailed = times_jailed + 1 WHERE discord_user_id = ?",
        (_iso(until), _iso(_now()), bribe, sentence, user_id),
    )
    await conn.commit()
    return {"until": until, "bribe": bribe, "sentence": sentence,
            "minutes": minutes, "indigent": indigent}


async def release_player(conn: aiosqlite.Connection, user_id: str) -> None:
    """Free a player and clear any outstanding bribe."""
    await conn.execute(
        "UPDATE scam_players SET jail_until = NULL, jailed_at = NULL,"
        " bribe_remaining = 0, jail_sentence = NULL WHERE discord_user_id = ?",
        (user_id,),
    )
    await conn.commit()


async def jail_block_embed(jail: dict, action: str) -> discord.Embed:
    """The 'you are in jail' refusal shown when a jailed player tries to work."""
    return discord.Embed(
        title="🚔 You are in a cell",
        description=(
            f"You cannot {action} from custody.\n\n"
            f"**Outstanding bribe:** {money(jail['bribe'])}\n"
            f"**Sentence:** {jail['sentence']}\n"
            f"**Released** <t:{int(jail['until'].timestamp())}:R>\n\n"
            "Pay with `/paybribe` (withdraw from the fund first if you must), "
            "get somebody to `/bail` you, or plead your case with `/appeal`."
        ),
        colour=_EMBED_RED,
    )


async def require_not_impersonating(
    interaction: discord.Interaction, conn: aiosqlite.Connection, action: str
) -> bool:
    """Block a player who is currently posing as a fake target.

    Imported lazily: scam_targets imports this module, so a module-level
    import here would be circular.
    """
    try:
        from nigeria_bot.scam_targets import impersonating
    except Exception:
        return True
    fake = await impersonating(conn, str(interaction.user.id))
    if fake is None:
        return True
    await interaction.response.send_message(
        embed=discord.Embed(
            title="🎭 You are in character",
            description=(
                f"You are currently posing as **{fake['emoji']} {fake['name']}** "
                f"and cannot {action} until that plays out.\n\n"
                "Wait for somebody to take the bait, or for your cover to "
                "expire."
            ),
            colour=_EMBED_GREY,
        ),
        ephemeral=True,
    )
    return False


async def require_free(
    interaction: discord.Interaction, conn: aiosqlite.Connection, action: str
) -> bool:
    """Return True if the player may act; otherwise reply and return False."""
    if not JAIL_ENABLED:
        return True
    jail = await get_jail(conn, str(interaction.user.id))
    if jail is None:
        return True
    await interaction.response.send_message(
        embed=await jail_block_embed(jail, action), ephemeral=True
    )
    return False


# ── /scam outcome table ───────────────────────────────────────────────────────

_SCAM_JACKPOT = [
    "A Dutch pension fund wired you their entire quarterly budget after you "
    "signed the email “Crown Prince, Royal Treasury of Lagos”.",
    "You sold the same non-existent oil platform to four different investors "
    "in one afternoon.",
    "A crypto influencer paid you upfront to be “the African distribution "
    "partner” of a coin that does not exist.",
]
_SCAM_BIG = [
    "You convinced a Dutchman to pay “royal administration fees”.",
    "Someone paid the customs clearance charge on a shipment of gold that is "
    "still, sadly, entirely theoretical.",
    "A retired dentist from Utrecht believed the inheritance letter.",
]
_SCAM_SMALL = [
    "You sold a “diplomatic courier certificate” printed at an internet café.",
    "Someone paid the small handling fee. Only the small one. They were careful.",
    "You charged a stranger for a visa appointment that does not exist.",
]
_SCAM_NOTHING = [
    "You sent 400 emails. Everyone had already heard this one.",
    "Your target replied only to correct your spelling.",
    "The internet café lost power halfway through your pitch.",
]
_SCAM_FAIL = [
    "Your target was also a scammer. You paid the fee.",
    "You accidentally wired the “processing fee” to yourself, minus bank charges.",
    "You bought a mailing list. It was a list of fraud investigators.",
]
_SCAM_DISASTER = [
    "You tried to scam an undercover officer and had to buy your way out.",
    "Your business partner took the laptop, the phone and the petty cash.",
    "You paid a large bribe to a man who turned out to be a bus driver.",
]

# (label, weight, min, max, flavour pool, colour)
_SCAM_OUTCOMES = [
    ("SCAM JACKPOT",  2,  5_000, 15_000, _SCAM_JACKPOT,  _EMBED_GOLD),
    ("SCAM SUCCESSFUL", 13,  800,  2_500, _SCAM_BIG,     _EMBED_GREEN),
    ("SMALL SCORE",   35,    150,    700, _SCAM_SMALL,   _EMBED_GREEN),
    ("NO RESULT",     25,      0,      0, _SCAM_NOTHING, _EMBED_GREY),
    ("SCAM FAILED",   22,   -600,   -100, _SCAM_FAIL,    _EMBED_RED),
    ("TOTAL DISASTER", 3, -2_500, -1_000, _SCAM_DISASTER, _EMBED_RED),
]


# Weight multipliers applied when a player is completely "cold" — i.e. scamming
# again the instant their last attempt finished.  Wins become rare, failures
# common, and (via SCAM_COLD_LOSS_MULT) more expensive.  These interpolate
# linearly towards 1.0 as readiness climbs back to full.
_SCAM_COLD_SCALING = {
    "SCAM JACKPOT":    0.00,   # no jackpots at all while burnt out
    "SCAM SUCCESSFUL": 0.05,
    "SMALL SCORE":     0.35,
    "NO RESULT":       2.00,
    "SCAM FAILED":     1.50,
    "TOTAL DISASTER":  1.50,
}
# How much harder losses bite at zero readiness (1.0 = unchanged).
SCAM_COLD_LOSS_MULT = 2.5
# Ceiling on a single scam loss.  Without it the cold multiplier stacks on the
# disaster range and produces ~6.250 losses — far more than any of the outcome
# bands advertise, and more than most players could ever pay.
MAX_SCAM_LOSS = 2_500


def scam_readiness(last_scam_at: Optional[str], now: Optional[datetime] = None) -> float:
    """Return how recovered a player is, 0.0 (just scammed) → 1.0 (full odds).

    Replaces the old hard cooldown: ``/scam`` may always be run, but the marks
    are wary if you have just worked the phones.  The curve is pinned to two
    points so the behaviour is easy to explain:

    * at :data:`SCAM_BREAKEVEN_HOURS` the expected profit is exactly zero —
      an even bet, worth taking only if you feel lucky;
    * at :data:`SCAM_FULL_HOURS` the odds are back to normal.
    """
    if not last_scam_at:
        return 1.0
    now = now or _now()
    hours = (now - _parse(last_scam_at)).total_seconds() / 3600
    if hours >= SCAM_FULL_HOURS:
        return 1.0
    if hours <= 0:
        return 0.0
    if hours < SCAM_BREAKEVEN_HOURS:
        return _BREAKEVEN_READINESS * (hours / SCAM_BREAKEVEN_HOURS)
    span = SCAM_FULL_HOURS - SCAM_BREAKEVEN_HOURS
    progress = (hours - SCAM_BREAKEVEN_HOURS) / span
    return _BREAKEVEN_READINESS + (1.0 - _BREAKEVEN_READINESS) * progress


def _scam_weights(readiness: float) -> list[float]:
    """Outcome weights at a given readiness (shared by rolling and reporting)."""
    r = max(0.0, min(1.0, readiness))
    return [
        o[1] * (_SCAM_COLD_SCALING.get(o[0], 1.0)
                + (1.0 - _SCAM_COLD_SCALING.get(o[0], 1.0)) * r)
        for o in _SCAM_OUTCOMES
    ]


def scam_odds(readiness: float) -> tuple[float, float]:
    """Return (win_chance, loss_chance) as percentages at *readiness*.

    Reported to players instead of the raw readiness factor — readiness is an
    internal weight multiplier, and showing it as "0% odds" would wrongly imply
    winning is impossible when a cold scam still pays out about one time in
    eight.
    """
    weights = _scam_weights(readiness)
    total = sum(weights) or 1.0
    win = sum(w for w, o in zip(weights, _SCAM_OUTCOMES) if o[2] > 0)
    lose = sum(w for w, o in zip(weights, _SCAM_OUTCOMES) if o[3] < 0)
    return win / total * 100, lose / total * 100


def _expected_loss(lo: int, hi: int, mult: float) -> float:
    """Mean of ``min(MAX_SCAM_LOSS, |d| * mult)`` for ``d`` uniform on lo..hi.

    The cap has to be applied *inside* the average, not after it: at a cold
    readiness most of the range is clipped, and averaging first would report a
    far worse expected loss than the game can actually deal you.
    """
    a, b = sorted((abs(lo), abs(hi)))
    if mult <= 0:
        return 0.0
    threshold = MAX_SCAM_LOSS / mult          # |d| above this gets clipped
    if b <= threshold:
        return mult * (a + b) / 2
    if a >= threshold:
        return float(MAX_SCAM_LOSS)
    share_below = (threshold - a) / (b - a)
    return (
        share_below * mult * (a + threshold) / 2
        + (1 - share_below) * MAX_SCAM_LOSS
    )


def scam_expected_value(readiness: float) -> float:
    """What a `/scam` at this readiness is worth, before it is rolled."""
    r = max(0.0, min(1.0, readiness))
    weights = _scam_weights(r)
    total = sum(weights) or 1.0
    loss_mult = 1.0 + (SCAM_COLD_LOSS_MULT - 1.0) * (1.0 - r)
    ev = 0.0
    for weight, outcome in zip(weights, _SCAM_OUTCOMES):
        _label, _w, lo, hi, _pool, _colour = outcome
        p = weight / total
        if lo == hi == 0:
            continue
        if lo >= 0:
            ev += p * (lo + hi) / 2
        else:
            ev -= p * _expected_loss(lo, hi, loss_mult)
    return ev


def roll_scam(readiness: float = 1.0) -> tuple[str, int, str, discord.Colour]:
    """Pick a random /scam outcome, scaled by *readiness* (0.0–1.0).

    Returns (label, delta, flavour, colour).
    """
    r = max(0.0, min(1.0, readiness))
    weights = _scam_weights(r)
    choice = random.choices(range(len(_SCAM_OUTCOMES)), weights=weights, k=1)[0]
    label, _w, lo, hi, pool, colour = _SCAM_OUTCOMES[choice]
    delta = 0 if lo == hi == 0 else random.randint(lo, hi)
    if delta < 0:
        loss_mult = 1.0 + (SCAM_COLD_LOSS_MULT - 1.0) * (1.0 - r)
        delta = -min(MAX_SCAM_LOSS, int(round(-delta * loss_mult)))
    return label, delta, random.choice(pool), colour


def _join_line(mention: str, stake: int, total: int) -> str:
    """Announcement line for a buy-in, noting the running total on a top-up."""
    if stake == 0:
        return f"🆓 {mention} took a free seat"
    if total > stake:
        return (
            f"💰 {mention} added **{money(stake)}** — now in for {money(total)}"
        )
    return f"💰 {mention} joined for **{money(stake)}**"


def _join_summary(mention: str, stake: int, total: int, op: dict) -> str:
    """One line per buy-in.

    Joins used to reprint the whole operation card, which on a fifteen-seat
    template meant the same wall of text fifteen times.  The card itself is
    edited in place instead, so this only has to carry what changed.
    """
    tpl = op["template"]
    entries = op["entries"]
    pot = sum(a for _u, a in entries)
    chance = qs.success_chance(tpl, len(entries), pot)
    unix = int(_parse(op["resolve_at"]).timestamp())
    return (
        f"{_join_line(mention, stake, total)} · "
        f"**{len(entries)}/{tpl['max_participants']}** seats · "
        f"pot **{money(pot)}** · odds **{chance * 100:.0f}%** · "
        f"closes <t:{unix}:R>"
    )


# ── Channel gating ────────────────────────────────────────────────────────────

def _wrong_channel_embed(target: str) -> discord.Embed:
    return discord.Embed(
        title="Wrong channel",
        description=(
            f"The Nigerian Scam-Economy is played in {target}.\n"
            "Take your business there — this street is watched."
        ),
        colour=_EMBED_RED,
    )


async def _require_channel(
    interaction: discord.Interaction, channel_id: int, link: str
) -> bool:
    """Return True if the command may run here; otherwise reply and return False."""
    here = interaction.channel_id
    parent = getattr(interaction.channel, "parent_id", None)
    if here == channel_id or parent == channel_id:
        return True
    await interaction.response.send_message(
        embed=_wrong_channel_embed(link), ephemeral=True
    )
    return False


# ── Join button for operations ────────────────────────────────────────────────

_MODAL_LABEL_LIMIT = 45


def _amount_label(prefix: str, balance: int) -> str:
    """Build a modal field label showing the player's balance.

    Discord caps modal labels at 45 characters, so fall back progressively
    rather than letting a rich player's balance push it over the limit and
    make the modal fail to open.
    """
    for candidate in (
        f"{prefix} — you have {money(balance)}",
        f"{prefix} — you have {int(balance):,}".replace(",", "."),
        f"You have {money(balance)}",
        prefix,
    ):
        if len(candidate) <= _MODAL_LABEL_LIMIT:
            return candidate
    return prefix[:_MODAL_LABEL_LIMIT]


class JoinStakeModal(discord.ui.Modal, title="Join the quick scam"):
    def __init__(
        self, cog: "ScamGameCog", operation_id: int, tpl: dict, balance: int = 0
    ) -> None:
        super().__init__(title="Join the quick scam")
        self.cog = cog
        self.operation_id = operation_id
        lo, hi = tpl["min_stake"], tpl["max_stake"]
        self.amount = discord.ui.TextInput(
            label=_amount_label("Stake", balance),
            placeholder=f"{lo}–{hi} — you have {balance}",
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.amount.value or "").strip().replace(".", "").replace(",", "")
        try:
            stake = int(raw)
        except ValueError:
            await interaction.response.send_message(
                f"❌ `{self.amount.value}` is not a number.", ephemeral=True
            )
            return
        await self.cog.handle_join(interaction, self.operation_id, stake)


class OperationView(discord.ui.View):
    """Join buttons attached to an open operation announcement.

    Persistent (``timeout=None`` + fixed ``custom_id``s, registered via
    ``bot.add_view``) so the buttons keep working across bot restarts.  A
    non-persistent view only lives in the process that created it, so after a
    redeploy every previously posted button would silently do nothing and
    Discord would show "the application did not respond".

    The operation id is deliberately *not* baked into the view: only one
    operation runs at a time, so the handler simply looks up whichever one is
    currently open.  That also means a button on an older, already-resolved
    announcement still behaves sensibly.

    The free seat is a separate button rather than "enter 0" in the modal —
    free entry only exists on some templates, and a button that is simply
    absent is clearer than one that rejects you.
    """

    def __init__(self, free_entry: bool = False) -> None:
        super().__init__(timeout=None)
        if not free_entry:
            self.remove_item(self.join_free)

    @staticmethod
    async def _lookup(interaction: discord.Interaction):
        """Return ``(cog, operation_id, template)`` or ``None`` after replying."""
        cog = interaction.client.get_cog("scam_game")
        if cog is None:
            logger.error("scam_game: cog not found while handling join button")
            await interaction.response.send_message(
                "❌ The game is not available right now.", ephemeral=True
            )
            return None
        operation_id = await cog.open_operation_id()
        if operation_id is None:
            await interaction.response.send_message(
                "❌ There is no quick scam running at the moment. "
                "Start one with `/quickscam`.",
                ephemeral=True,
            )
            return None
        op = await cog._operation_state(operation_id)
        return cog, operation_id, op["template"]

    @discord.ui.button(
        label="💰 Join quick scam",
        style=discord.ButtonStyle.success,
        custom_id="scam:join_operation",
    )
    async def join(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        logger.info(
            "scam_game: join button pressed by %s (%s)",
            interaction.user, interaction.user.id,
        )
        found = await self._lookup(interaction)
        if not found:
            return
        cog, operation_id, tpl = found
        player = await get_player(cog.conn, str(interaction.user.id))
        await interaction.response.send_modal(
            JoinStakeModal(cog, operation_id, tpl, player["balance"])
        )

    @discord.ui.button(
        label="🆓 Join for free",
        style=discord.ButtonStyle.secondary,
        custom_id="scam:join_operation_free",
    )
    async def join_free(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        logger.info(
            "scam_game: free join pressed by %s (%s)",
            interaction.user, interaction.user.id,
        )
        found = await self._lookup(interaction)
        if not found:
            return
        cog, operation_id, tpl = found
        if not tpl["free_entry"]:
            await interaction.response.send_message(
                "❌ This operation has no free seats.", ephemeral=True
            )
            return
        await cog.handle_join(interaction, operation_id, 0)



# ── /beg flavour ──────────────────────────────────────────────────────────────

_BEG_PLEAS = [
    "I have spent weeks scamming Dutchmen and somehow **I** am the one who "
    "ended up broke. The irony has not escaped me. It has, however, escaped "
    "my wallet.",
    "I put everything into the Fully Trustworthy Royal Investment Fund. I "
    "would very much like to speak to somebody about this.",
    "I told a Dutchman his inheritance was ready. He told me mine was too. "
    "We were both lying. He was better at it.",
    "I went greedy on a whale at five percent. Do not do what I did. Learn "
    "from me. Fund me.",
    "My prince has left. My treasury is a coat pocket. My coat is also gone.",
    "I cannot afford the stamp for the letter asking you for money. Consider "
    "this message the stamp.",
    "Please. I have children. Well — I have a *photograph* of children that I "
    "use in emails. But the sentiment stands.",
    "Business is slow. Turns out everybody in this country has already been "
    "contacted by a Nigerian prince, and several of them were me.",
    "I am not asking for charity. I am asking for an **investment** in a "
    "very promising individual. Returns are guaranteed.* \n*Not guaranteed.",
    "The helicopter left without me. That was my helicopter. I had one job: "
    "be on the helicopter.",
]

_BEG_TITLES = [
    "🥺 A HUMBLE REQUEST",
    "🥺 ONE OF OUR PRINCES IS DESTITUTE",
    "🥺 AN APPEAL TO YOUR BETTER NATURE",
    "🥺 TEMPORARILY EMBARRASSED MILLIONAIRE",
    "🥺 CROWDFUNDING A COMEBACK",
]

_DONATION_LINES = [
    "{donor} has taken pity on {beggar} and handed over **{amount}**.",
    "{donor} slipped {beggar} **{amount}**, no questions asked.",
    "{donor} has invested **{amount}** in the future of {beggar}. Bold.",
    "{donor} gave {beggar} **{amount}** and immediately regretted it.",
    "**{amount}** from {donor} to {beggar}. The prince weeps with gratitude.",
]


def beg_embed(
    member: discord.abc.User, balance: int, plea: str, title: str,
    total: int = 0, donors: int = 0, invested: int = 0,
) -> discord.Embed:
    """Render a begging post, including anything raised so far."""
    embed = discord.Embed(
        title=title,
        description=(
            f"{member.mention} is begging.\n\n_{plea}_\n\n"
            f"**Cash in hand: {money(balance)}**\n"
            f"**In the fund: {money(invested)}**"
        ),
        colour=_EMBED_GREY,
    )
    if total:
        embed.add_field(
            name="Raised so far",
            value=(
                f"**{money(total)}** from {donors} "
                f"{'donor' if donors == 1 else 'donors'}"
            ),
            inline=False,
        )
    embed.set_footer(
        text=(
            "Anyone can donate — tap an amount below, or “Other amount…” "
            f"for anything from {BEG_MIN_DONATION} {CURRENCY} up."
        )
    )
    return embed


class DonateModal(discord.ui.Modal, title="Donate to this poor soul"):
    def __init__(
        self, cog: "ScamGameCog", message_id: str, balance: int = 0
    ) -> None:
        super().__init__(title="Donate to this poor soul")
        self.cog = cog
        self.message_id = message_id
        self.amount = discord.ui.TextInput(
            label=_amount_label("Donation", balance),
            placeholder=f"Minimum {BEG_MIN_DONATION} — you have {balance}",
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.amount.value or "").strip().replace(".", "").replace(",", "")
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                f"❌ `{self.amount.value}` is not a number.", ephemeral=True
            )
            return
        await self.cog.handle_donation(interaction, self.message_id, amount)


class BegView(discord.ui.View):
    """Donate buttons under a begging post.

    Five one-click amounts plus a free-text option, because most donations are
    small and making somebody open a modal to type "5" is friction for no gain.

    Persistent with static ``custom_id``s: several people may be begging at
    once, so the beggar is resolved from the *message* the button was clicked
    on (see the ``scam_begs`` table) rather than being baked into the id.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for amount in BEG_PRESETS:
            self.add_item(self._preset_button(amount))
        self.add_item(self._custom_button())

    def _preset_button(self, amount: int) -> discord.ui.Button:
        button = discord.ui.Button(
            label=f"{amount}",
            emoji="💝",
            style=discord.ButtonStyle.success,
            custom_id=f"scam:beg_donate:{amount}",
            row=0,
        )

        async def callback(interaction: discord.Interaction, _amount=amount) -> None:
            cog = interaction.client.get_cog("scam_game")
            if cog is None:
                await interaction.response.send_message(
                    "❌ The game is not available right now.", ephemeral=True
                )
                return
            await cog.handle_donation(
                interaction, str(interaction.message.id), _amount
            )

        button.callback = callback
        return button

    def _custom_button(self) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Other amount…",
            emoji="✏️",
            style=discord.ButtonStyle.primary,
            custom_id="scam:beg_donate:custom",
            row=1,
        )

        async def callback(interaction: discord.Interaction) -> None:
            cog = interaction.client.get_cog("scam_game")
            if cog is None:
                await interaction.response.send_message(
                    "❌ The game is not available right now.", ephemeral=True
                )
                return
            player = await get_player(cog.conn, str(interaction.user.id))
            await interaction.response.send_modal(
                DonateModal(cog, str(interaction.message.id), player["balance"])
            )

        button.callback = callback
        return button


# ── Cog ───────────────────────────────────────────────────────────────────────

class ScamGameCog(commands.Cog, name="scam_game"):
    """The Nigerian Scam-Economy."""

    def __init__(self, bot: commands.Bot, conn: aiosqlite.Connection) -> None:
        self.bot = bot
        self.conn = conn
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        await setup_schema(self.conn)

    # ── startup reconcile ─────────────────────────────────────────────

    async def reconcile(self) -> None:
        """Resume open operations after a restart.

        Besides re-arming the resolution timer, this re-attaches a fresh
        :class:`OperationView` to the announcement message.  Any message posted
        by an older build carries that build's component ids; editing the
        message swaps in components with the current ``custom_id`` so the Join
        button starts working again instead of silently timing out.
        """
        async with self.conn.execute(
            "SELECT id, resolve_at, channel_id, message_id"
            " FROM scam_operations WHERE status = 'open'"
        ) as cur:
            rows = [
                (int(r[0]), str(r[1]), str(r[2]), r[3]) async for r in cur
            ]
        for op_id, resolve_at, channel_id, message_id in rows:
            delay = (_parse(resolve_at) - _now()).total_seconds()
            logger.info(
                "scam_game: resuming operation %d (resolves in %.0fs)", op_id, delay
            )
            asyncio.create_task(self._resolve_later(op_id, max(0.0, delay)))
            if message_id and delay > 0:
                asyncio.create_task(
                    self._refresh_operation_message(op_id, channel_id, str(message_id))
                )

    async def _refresh_open_operation(self, operation_id: int) -> None:
        """Update the stored announcement for an operation, if we still have it."""
        async with self.conn.execute(
            "SELECT channel_id, message_id FROM scam_operations WHERE id = ?",
            (operation_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row[1]:
            return
        await self._refresh_operation_message(operation_id, str(row[0]), str(row[1]))

    async def _refresh_operation_message(
        self, operation_id: int, channel_id: str, message_id: str
    ) -> None:
        """Re-attach a live Join button to an operation announcement."""
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                return
            message = await channel.fetch_message(int(message_id))
            op = await self._operation_state(operation_id)
            if not op or op["status"] != "open":
                return
            await message.edit(
                embed=self._operation_embed(op, getattr(channel, "guild", None)),
                view=OperationView(op["template"]["free_entry"]),
            )
            logger.info(
                "scam_game: refreshed join button on message %s for operation %d",
                message_id, operation_id,
            )
        except discord.NotFound:
            logger.info("scam_game: operation message %s no longer exists", message_id)
        except Exception:
            logger.exception(
                "scam_game: failed to refresh operation message %s", message_id
            )

    async def _resolve_later(self, operation_id: int, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.resolve_operation(operation_id)
        except Exception:
            logger.exception("scam_game: failed to resolve operation %d", operation_id)

    # ── /scam ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="scam", description="Try to scam somebody out of their Naira."
    )
    async def scam(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        if not await require_free(interaction, self.conn, "run a scam"):
            return
        if not await require_not_impersonating(
            interaction, self.conn, "run a scam"
        ):
            return
        uid = str(interaction.user.id)
        async with self._lock:
            player = await get_player(self.conn, uid)

            # No cooldown — you may always try. Your odds just depend on how
            # long it has been since the last attempt.
            readiness = scam_readiness(player["last_scam_at"])
            scam_time = _now()
            label, delta, flavour, colour = roll_scam(readiness)
            arrest_info = None
            if delta < 0:
                # Pay what you can in cash; anything left over is what the
                # authorities want as a bribe.
                owed = -delta
                paid = min(owed, player["balance"])
                shortfall = owed - paid
                if paid:
                    await adjust_balance(self.conn, uid, -paid, "scam_bribe")
                delta = -paid
                if shortfall > 0 and JAIL_ENABLED:
                    wealth = player["balance"] + player["invested"]
                    if wealth >= INDIGENT_WEALTH_THRESHOLD:
                        # Rich on paper, broke in hand — that is exactly the
                        # dodge this is here to catch.
                        arrest_info = await arrest_player(
                            self.conn, uid, shortfall, wealth
                        )
                new_balance = (await get_player(self.conn, uid))["balance"]
            else:
                new_balance = await adjust_balance(self.conn, uid, delta, "scam", label)
            await self.conn.execute(
                "UPDATE scam_players SET last_scam_at = ?, scams_run = scams_run + 1"
                " WHERE discord_user_id = ?",
                (_iso(scam_time), uid),
            )
            await record_play(
                self.conn, uid, "scam",
                scam_expected_value(readiness), delta, label,
            )
            await self.conn.commit()

        if delta > 0:
            change = f"**Proceeds: +{money(delta)}**"
        elif delta < 0:
            change = f"**Losses: -{money(-delta)}**"

        else:
            change = "**Proceeds: nothing at all**"

        # Tell them what the odds were, and what going straight back in costs.
        win_pct, lose_pct = scam_odds(readiness)
        full_win, _full_lose = scam_odds(1.0)
        if readiness >= 0.999:
            heat = f"You were fresh — full odds ({win_pct:.0f}% win chance)."
        elif readiness >= _BREAKEVEN_READINESS:
            heat = (
                f"You were still warm — {win_pct:.0f}% win / {lose_pct:.0f}% lose "
                f"(normally {full_win:.0f}% win)."
            )
        else:
            heat = (
                f"⚠️ You went back in too soon — only a {win_pct:.0f}% win chance "
                f"against {lose_pct:.0f}% lose (normally {full_win:.0f}% win), "
                "and failures cost more."
            )
        breakeven_at = int((scam_time + timedelta(hours=SCAM_BREAKEVEN_HOURS)).timestamp())
        full_at = int((scam_time + timedelta(hours=SCAM_FULL_HOURS)).timestamp())

        embed = discord.Embed(
            title=label,
            description=(
                f"{flavour}\n\n{change}\nNew balance: {money(new_balance)}"
            ),
            colour=colour,
        )
        embed.add_field(
            name="Your odds",
            value=(
                f"{heat}\n"
                f"You can scam again whenever you like, but right now the marks "
                f"are wary — going again immediately, you will **almost always "
                f"lose**.\n"
                f"• Even bet <t:{breakeven_at}:R>\n"
                f"• Full odds <t:{full_at}:R>"
            ),
            inline=False,
        )
        if player["is_new"]:
            embed.set_footer(
                text=f"Welcome! You started with {money(START_BALANCE)}."
            )

        embeds = [embed]
        if arrest_info:
            fresh = await get_player(self.conn, uid)
            embeds.append(discord.Embed(
                title="🚨 YOU HAVE BEEN ARRESTED",
                description=(
                    "Your latest business venture has attracted unwanted "
                    "attention from the authorities. You could not settle up in "
                    "cash, so they have taken an interest in you personally.\n\n"
                    f"💰 **Required bribe:** {money(arrest_info['bribe'])}\n"
                    f"💵 **Cash available:** {money(fresh['balance'])}\n"
                    f"📈 **Investment fund:** {money(fresh['invested'])}\n\n"
                    f"🚔 **Sentence:** {arrest_info['sentence']}\n"
                    f"Eligible for release "
                    f"<t:{int(arrest_info['until'].timestamp())}:R>\n\n"
                    "You cannot scam, start or join a quick scam, or work a "
                    "target until this is settled.\n"
                    "`/paybribe` · `/invest withdraw` · `/appeal` — or wait for "
                    "somebody to `/bail` you."
                ),
                colour=_EMBED_RED,
            ))
        await interaction.response.send_message(embeds=embeds)
        await maybe_send_help(interaction, self.conn, player)


    # ── /beg ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="beg", description="Publicly beg your fellow princes for money."
    )
    async def beg(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        uid = str(interaction.user.id)

        async with self._lock:
            player = await get_player(self.conn, uid)
            async with self.conn.execute(
                "SELECT last_beg_at FROM scam_players WHERE discord_user_id = ?",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
            last = row[0] if row else None
            if last:
                ready = _parse(last) + timedelta(hours=BEG_COOLDOWN_HOURS)
                if ready > _now():
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="🥺 Some dignity, please",
                            description=(
                                "You have only just finished begging. Even the "
                                "desperate observe a decent interval.\n\n"
                                f"You may beg again <t:{int(ready.timestamp())}:R>."
                            ),
                            colour=_EMBED_GREY,
                        ),
                        ephemeral=True,
                    )
                    return
            await self.conn.execute(
                "UPDATE scam_players SET last_beg_at = ?,"
                " begs_posted = begs_posted + 1 WHERE discord_user_id = ?",
                (_iso(_now()), uid),
            )
            await self.conn.commit()

        plea = random.choice(_BEG_PLEAS)
        title = random.choice(_BEG_TITLES)
        await interaction.response.send_message(
            embed=beg_embed(
                interaction.user, player["balance"], plea, title,
                invested=player["invested"],
            ),
            view=BegView(),
        )
        try:
            message = await interaction.original_response()
            await self.conn.execute(
                "INSERT OR REPLACE INTO scam_begs"
                " (message_id, channel_id, beggar_id, created_at, total, donors)"
                " VALUES (?, ?, ?, ?, 0, 0)",
                (str(message.id), str(interaction.channel_id), uid, _iso(_now())),
            )
            await self.conn.commit()
        except Exception:
            logger.exception("scam_game: could not record the begging post")

    async def handle_donation(
        self, interaction: discord.Interaction, message_id: str, amount: int
    ) -> None:
        """Move money from the clicker to whoever posted that begging message."""
        donor_id = str(interaction.user.id)

        if amount < BEG_MIN_DONATION:
            await interaction.response.send_message(
                f"❌ The smallest donation is {money(BEG_MIN_DONATION)}.",
                ephemeral=True,
            )
            return

        async with self._lock:
            async with self.conn.execute(
                "SELECT beggar_id, total, donors FROM scam_begs WHERE message_id = ?",
                (message_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await interaction.response.send_message(
                    "❌ This appeal is no longer being tracked.", ephemeral=True
                )
                return
            beggar_id, total, donors = str(row[0]), int(row[1]), int(row[2])

            if beggar_id == donor_id:
                await interaction.response.send_message(
                    "❌ Donating to yourself achieves nothing, financially or "
                    "spiritually.",
                    ephemeral=True,
                )
                return

            donor = await get_player(self.conn, donor_id)
            if donor["balance"] < amount:
                await interaction.response.send_message(
                    f"❌ You only have {money(donor['balance'])}.", ephemeral=True
                )
                return

            await adjust_balance(self.conn, donor_id, -amount, "beg_sent")
            new_balance = await adjust_balance(self.conn, beggar_id, amount, "beg_received")
            # A donation isn't a scam loss/gain, so track it separately from
            # total_earned / total_lost — otherwise generosity would look
            # identical to being bad at scamming.
            await self.conn.execute(
                "UPDATE scam_players SET donated_total = donated_total + ?,"
                " donations_made = donations_made + 1 WHERE discord_user_id = ?",
                (amount, donor_id),
            )
            await self.conn.execute(
                "UPDATE scam_players SET received_total = received_total + ?"
                " WHERE discord_user_id = ?",
                (amount, beggar_id),
            )
            total += amount
            donors += 1
            await self.conn.execute(
                "UPDATE scam_begs SET total = ?, donors = ? WHERE message_id = ?",
                (total, donors, message_id),
            )
            await self.conn.commit()

        beggar = (
            interaction.guild.get_member(int(beggar_id))
            if interaction.guild else None
        )
        beggar_name = beggar.mention if beggar else f"<@{beggar_id}>"
        line = random.choice(_DONATION_LINES).format(
            donor=interaction.user.mention, beggar=beggar_name, amount=money(amount),
        )
        embed = discord.Embed(
            title="💝 A DONATION",
            description=(
                f"{line}\n\n"
                f"Raised by this appeal: **{money(total)}** from {donors} "
                f"{'donor' if donors == 1 else 'donors'}\n"
                f"Their balance is now {money(new_balance)}."
            ),
            colour=_EMBED_GREEN,
        )
        await interaction.response.send_message(embed=embed)

        # keep the original appeal's running total up to date
        try:
            if interaction.message is not None and interaction.message.embeds:
                original = interaction.message.embeds[0]
                beggar_row = await get_player(self.conn, beggar_id)
                updated = beg_embed(
                    beggar or interaction.user, new_balance,
                    "", original.title or "🥺 A HUMBLE REQUEST",
                    total=total, donors=donors,
                    invested=beggar_row["invested"],
                )
                updated.description = (original.description or "").split(
                    "\n\n**Cash in hand:"
                )[0] + (
                    f"\n\n**Cash in hand: {money(new_balance)}**\n"
                    f"**In the fund: {money(beggar_row['invested'])}**"
                )
                await interaction.message.edit(embed=updated, view=BegView())
        except Exception:
            logger.debug("scam_game: could not update the begging post total")


    # ── /begboard ─────────────────────────────────────────────────────

    @app_commands.command(
        name="begboard",
        description="Who gives the most, and who takes the most.",
    )
    async def begboard(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        await interaction.response.defer()

        async def top(column: str, extra: str) -> list[tuple]:
            rows: list[tuple] = []
            async with self.conn.execute(
                f"SELECT discord_user_id, {column}, {extra} FROM scam_players"
                f" WHERE {column} > 0 ORDER BY {column} DESC LIMIT 10"
            ) as cur:
                async for r in cur:
                    rows.append((str(r[0]), int(r[1]), int(r[2] or 0)))
            return rows

        givers = await top("donated_total", "donations_made")
        takers = await top("received_total", "begs_posted")

        def render(rows: list[tuple], unit: str) -> str:
            if not rows:
                return "_Nobody yet._"
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            out = []
            for i, (uid, total, count) in enumerate(rows, 1):
                member = (
                    interaction.guild.get_member(int(uid))
                    if interaction.guild else None
                )
                name = member.display_name if member else f"Prince {uid[-4:]}"
                prefix = medals.get(i, f"`{i:>2}.`")
                label = unit if count == 1 else unit + "s"
                out.append(
                    f"{prefix} **{name}** — {money(total)} _({count} {label})_"
                )
            return "\n".join(out)

        embed = discord.Embed(
            title="💝 THE BEGGING BOARD",
            description=(
                "Who funds this economy, and who lives off it.\n"
                "Donations are tracked separately from scam winnings."
            ),
            colour=_EMBED_GOLD,
        )
        embed.add_field(
            name="😇 Most generous",
            value=render(givers, "donation"),
            inline=False,
        )
        embed.add_field(
            name="🥺 Most supported",
            value=render(takers, "appeal"),
            inline=False,
        )

        async with self.conn.execute(
            "SELECT COALESCE(SUM(donated_total), 0), COALESCE(SUM(donations_made), 0)"
            " FROM scam_players"
        ) as cur:
            row = await cur.fetchone()
        moved, count = int(row[0] or 0), int(row[1] or 0)
        if moved:
            embed.set_footer(
                text=f"{money(moved)} handed over across {count} donations."
            )
        await interaction.followup.send(embed=embed)

    # ── /balance ──────────────────────────────────────────────────────

    @app_commands.command(
        name="balance", description="Check your Naira balance."
    )
    @app_commands.describe(player="Optional: look up somebody else.")
    async def balance(
        self, interaction: discord.Interaction, player: Optional[discord.Member] = None
    ) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        target = player or interaction.user
        row = await get_player(self.conn, str(target.id))
        total = row["balance"] + row["invested"]
        embed = discord.Embed(
            title=f"Accounts of {target.display_name}",
            colour=_EMBED_GOLD,
        )
        embed.add_field(name="Cash", value=money(row["balance"]), inline=True)
        embed.add_field(name="In the fund", value=money(row["invested"]), inline=True)
        embed.add_field(name="Net worth", value=f"**{money(total)}**", inline=True)
        embed.add_field(
            name="Career",
            value=(
                f"Scams attempted: {row['scams_run']}\n"
                f"Total earned: {money(row['total_earned'])}\n"
                f"Total lost: {money(row['total_lost'])}"
            ),
            inline=False,
        )

        async with self.conn.execute(
            "SELECT donated_total, received_total FROM scam_players"
            " WHERE discord_user_id = ?",
            (str(target.id),),
        ) as cur:
            charity = await cur.fetchone()
        if charity and (charity[0] or charity[1]):
            embed.add_field(
                name="Charity",
                value=(
                    f"Donated to others: {money(charity[0] or 0)}\n"
                    f"Received from others: {money(charity[1] or 0)}"
                ),
                inline=False,
            )

        if row["last_scam_at"]:
            last = _parse(row["last_scam_at"])
            readiness = scam_readiness(row["last_scam_at"])
            win_pct, _lose = scam_odds(readiness)
            full_win, _ = scam_odds(1.0)
            lines = [f"Last `/scam`: <t:{int(last.timestamp())}:R>"]
            if readiness >= 0.999:
                lines.append(f"Odds: **full strength** ({win_pct:.0f}% win chance)")
            else:
                lines.append(
                    f"Odds: **{win_pct:.0f}% win chance** "
                    f"(back to {full_win:.0f}% at full strength)"
                )
                full_at = last + timedelta(hours=SCAM_FULL_HOURS)
                if readiness < _BREAKEVEN_READINESS:
                    even_at = last + timedelta(hours=SCAM_BREAKEVEN_HOURS)
                    lines.append(f"Even bet <t:{int(even_at.timestamp())}:R>")
                lines.append(f"Full odds <t:{int(full_at.timestamp())}:R>")
            embed.add_field(name="Scam readiness", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Scam readiness",
                value="Never scammed yet — full odds waiting for you.",
                inline=False,
            )
        embed.add_field(
            name="Target board", value=_target_readiness(row), inline=False
        )
        async with self.conn.execute(
            "SELECT times_jailed, bribes_paid, bails_given, bails_received,"
            " appeals_won FROM scam_players WHERE discord_user_id = ?",
            (str(target.id),),
        ) as cur:
            jrow = await cur.fetchone() or (0, 0, 0, 0, 0)
        jailed, bribes, given, received, appeals = (int(x or 0) for x in jrow)
        if jailed or given or received:
            bits = [f"Times arrested: **{jailed}**"]
            if bribes:
                bits.append(f"Bribes paid: {money(bribes)}")
            if appeals:
                bits.append(f"Appeals won: **{appeals}**")
            if received:
                bits.append(f"Bailed out by others: **{received}**")
            if given:
                bits.append(f"Bailed somebody else out: **{given}**")
            embed.add_field(name="🚔 Criminal record", value="\n".join(bits),
                            inline=False)
        else:
            embed.add_field(
                name="🚔 Criminal record",
                value="_Spotless. Suspiciously so._", inline=False,
            )

        plays = await play_stats(self.conn, str(target.id))
        if plays:
            lines = []
            tot_n = tot_ev = tot_real = 0
            for kind, n, ev, real in plays:
                tot_n += n; tot_ev += ev; tot_real += real
                lines.append(
                    f"{PLAY_LABELS.get(kind, kind)} — {n} play"
                    f"{'s' if n != 1 else ''}: expected "
                    f"{_signed_money(ev)}, got {_signed_money(real)}"
                )
            luck = tot_real - tot_ev
            if tot_n:
                verdict = (
                    "the dice have been kind" if luck > 0
                    else "the dice have not been kind"
                )
                lines.append(
                    f"\n**Across {tot_n} plays:** expected "
                    f"{_signed_money(tot_ev)}, got {_signed_money(tot_real)}\n"
                    f"🎲 **{_signed_money(luck)} versus expectation** — "
                    f"{verdict}."
                )
            embed.add_field(
                name="🎲 Luck", value="\n".join(lines), inline=False
            )
        history = await ledger_for(self.conn, str(target.id), LEDGER_SHOWN)
        if history:
            # A field caps at 1024 characters and some template names are long,
            # so drop the oldest rows rather than letting Discord reject the
            # whole embed.
            lines: list[str] = []
            for entry in history:
                line = _ledger_line(entry)
                if sum(len(x) + 1 for x in lines) + len(line) > 1000:
                    break
                lines.append(line)
            embed.add_field(
                name=f"📒 Last {len(lines)} movements",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="📒 Recent movements",
                value="_Nothing yet. Try `/scam`._",
                inline=False,
            )
        # Private: this is a status lookup rather than a game action, and the
        # movement history is a page of somebody's personal finances. Posting
        # that publicly buried the channel and told everyone else nothing they
        # wanted. Game *actions* stay public — that is where the drama is.
        await interaction.response.send_message(embed=embed, ephemeral=True)
        # `row` may belong to somebody else — the help flag must come from the
        # person who actually ran the command.
        await maybe_send_help(
            interaction, self.conn,
            row if target == interaction.user
            else await get_player(self.conn, str(interaction.user.id)),
        )

    # ── /leaderboard ──────────────────────────────────────────────────

    @app_commands.command(
        name="leaderboard", description="The richest princes in the realm."
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        await interaction.response.defer()
        rows: list[tuple[str, int, int]] = []
        async with self.conn.execute(
            "SELECT discord_user_id, balance, invested FROM scam_players"
            " ORDER BY (balance + invested) DESC LIMIT 15"
        ) as cur:
            async for r in cur:
                rows.append((str(r[0]), int(r[1]), int(r[2])))

        if not rows:
            await interaction.followup.send(
                "Nobody has scammed anybody yet. Disappointing."
            )
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for i, (uid, bal, inv) in enumerate(rows, 1):
            member = interaction.guild.get_member(int(uid)) if interaction.guild else None
            name = member.display_name if member else f"Unknown prince ({uid[-4:]})"
            prefix = medals.get(i, f"`{i:>2}.`")
            # No plus sign: the figure in front is already net worth, so "+"
            # read as though the fund sat on top of it.
            extra = f" _({money(inv)} of it in the fund)_" if inv else ""
            marker = " ⬅️" if uid == str(interaction.user.id) else ""
            lines.append(f"{prefix} **{name}** — {money(bal + inv)}{extra}{marker}")

        # If the caller did not make the cut, tell them where they actually
        # stand — a top-15 board that silently omits you is the one thing it
        # cannot afford to leave out.
        me = str(interaction.user.id)
        async with self.conn.execute(
            "SELECT COUNT(*) FROM scam_players"
        ) as cur:
            field = int((await cur.fetchone())[0])
        if me not in {uid for uid, _b, _i in rows}:
            player = await get_player(self.conn, me)
            mine = player["balance"] + player["invested"]
            async with self.conn.execute(
                "SELECT COUNT(*) FROM scam_players WHERE (balance + invested) > ?",
                (mine,),
            ) as cur:
                rank = int((await cur.fetchone())[0]) + 1
            behind = rows[-1][1] + rows[-1][2] - mine
            extra = (
                f" _({money(player['invested'])} of it in the fund)_"
                if player["invested"] else ""
            )
            lines.append(
                f"\n`{rank:>2}.` **You** — {money(mine)}{extra}\n"
                f"_{money(max(0, behind))} short of the board._"
            )

        await interaction.followup.send(embed=discord.Embed(
            title="👑 The richest princes",
            description="\n".join(lines),
            colour=_EMBED_GOLD,
        ).set_footer(
            text=f"{len(rows)} of {field} princes shown"
        ))

    # ── /joinquickscam ────────────────────────────────────────────────

    @app_commands.command(
        name="joinquickscam",
        description="Buy into the running quick scam.",
    )
    @app_commands.describe(
        amount=f"Your stake in {CURRENCY}. Leave empty for a free seat.",
    )
    async def joinquickscam(
        self,
        interaction: discord.Interaction,
        amount: Optional[app_commands.Range[int, 0, None]] = None,
    ) -> None:
        """Join without the button.

        Slash commands are routed by name rather than by a component id, so
        this always works — including on an announcement whose button predates
        the current build.
        """
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        operation_id = await self.open_operation_id()
        if operation_id is None:
            await interaction.response.send_message(
                "❌ There is no quick scam running. Start one with `/quickscam`.",
                ephemeral=True,
            )
            return
        await self.handle_join(interaction, operation_id, amount or 0)

    # ── /quickscam ────────────────────────────────────────────────────

    @app_commands.command(
        name="quickscam",
        description="Trigger a random quick scam the whole channel can buy into.",
    )
    @app_commands.describe(
        stake=(
            "Optional: buy in for this much straight away. Trimmed down if the "
            "rolled operation caps lower."
        )
    )
    async def quickscam(
        self,
        interaction: discord.Interaction,
        stake: Optional[app_commands.Range[int, 1, None]] = None,
    ) -> None:
        """Roll a template out of the pool and open it for sign-ups.

        Triggering costs nothing but the cooldown — the scarce thing is the
        right to *create* an opportunity for everyone, not the buy-in.  The
        initiator then decides whether to join like anybody else, which is what
        keeps the trigger from being strictly better than participating.
        """
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        if not await require_free(interaction, self.conn, "start a quick scam"):
            return
        if not await require_not_impersonating(
            interaction, self.conn, "start a quick scam"
        ):
            return
        uid = str(interaction.user.id)

        async with self._lock:
            # Exactly one operation may be running at a time, game-wide — two
            # competing pots would just split the players and halve the drama.
            # Rather than only refusing, re-post the running one with live Join
            # buttons so the answer to "can I start one?" is "here, join this".
            existing_id = await self.open_operation_id()
            if existing_id is not None:
                existing = await self._operation_state(existing_id)
                await interaction.response.send_message(
                    content=(
                        f"❌ {interaction.user.mention}, a quick scam is already "
                        "running — buy into this one instead!"
                    ),
                    embed=self._operation_embed(existing, interaction.guild),
                    view=OperationView(existing["template"]["free_entry"]),
                )
                return

            player = await get_player(self.conn, uid)
            ready_at = self._quickscam_ready_at(player)
            if ready_at and ready_at > _now():
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="⏳ No new opportunities yet",
                        description=(
                            f"You may trigger one quick scam every "
                            f"**{QUICKSCAM_TRIGGER_COOLDOWN_HOURS:g} hours**.\n\n"
                            f"**Your next trigger:** "
                            f"<t:{int(ready_at.timestamp())}:R>\n\n"
                            "Somebody else can still start one — and you can join "
                            "whatever turns up. In the meantime there is `/scam`, "
                            "the board at `/targets`, and the fund."
                        ),
                        colour=_EMBED_GREY,
                    ),
                    ephemeral=True,
                )
                return

            tpl = qs.pick_template()

            # A handful of templates charge the initiator up front.  Being too
            # poor to pay must never block a trigger (free-entry operations are
            # exactly what a broke player needs), so the cost — and with it the
            # bonus — is simply waived.
            cost = tpl["initiator_cost"]
            paid = 1 if (cost and player["balance"] >= cost) else 0
            if paid:
                await adjust_balance(self.conn, uid, -cost, "quickscam_setup", tpl["name"])
            elif cost:
                cost = 0

            resolve_at = _now() + timedelta(minutes=tpl["signup_minutes"])
            cur = await self.conn.execute(
                "INSERT INTO scam_operations"
                " (guild_id, channel_id, initiator_id, title, blurb, resolve_at,"
                "  status, template_id, initiator_paid)"
                " VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (
                    str(interaction.guild_id), str(interaction.channel_id), uid,
                    tpl["name"], tpl["description"], _iso(resolve_at),
                    tpl["id"], paid,
                ),
            )
            op_id = int(cur.lastrowid)
            await self.conn.execute(
                "UPDATE scam_players SET last_quickscam_at = ?"
                " WHERE discord_user_id = ?",
                (_iso(_now()), uid),
            )

            # An up-front stake saves a click, but it is chosen *before* the
            # template is known, so it rarely fits the rolled band exactly.
            # Trim it down to the most this operation — and this wallet — will
            # take rather than dropping it, so a mismatch costs no second click.
            # Trimming only ever goes *down*: a player who asked to spend 200 is
            # never talked into a 1.000 minimum they did not agree to.
            staked_up_front = 0
            stake_note = ""
            if stake:
                balance = player["balance"] - (tpl["initiator_cost"] if paid else 0)
                placed = min(stake, tpl["max_stake"], balance)
                if placed < tpl["min_stake"]:
                    if balance < tpl["min_stake"]:
                        stake_note = (
                            f"\n_(Your {money(stake)} stake was not placed — "
                            f"**{tpl['name']}** starts at "
                            f"{money(tpl['min_stake'])} and you have "
                            f"{money(balance)}.)_"
                        )
                    else:
                        stake_note = (
                            f"\n_(Your {money(stake)} stake was not placed — "
                            f"**{tpl['name']}** starts at "
                            f"{money(tpl['min_stake'])}. Use the button if you "
                            "want in for more than you asked for.)_"
                        )
                else:
                    await adjust_balance(self.conn, uid, -placed, "quickscam_stake", tpl["name"])
                    await self.conn.execute(
                        "INSERT INTO scam_operation_entries"
                        " (operation_id, discord_user_id, amount, joined_at)"
                        " VALUES (?, ?, ?, ?)",
                        (op_id, uid, placed, _iso(_now())),
                    )
                    staked_up_front = placed
                    if placed < stake:
                        reason = (
                            f"**{tpl['name']}** caps stakes at "
                            f"{money(tpl['max_stake'])}"
                            if placed == tpl["max_stake"]
                            else "that is all you have"
                        )
                        stake_note = (
                            f"\n_(Trimmed from your {money(stake)} — {reason}.)_"
                        )
            await self.conn.commit()

        op = await self._operation_state(op_id)
        embed = self._operation_embed(op, interaction.guild)

        headline = (
            f"⚡ {interaction.user.mention} has turned up a "
            f"**{qs.RARITY_BADGE[tpl['rarity']]}** opportunity: "
            f"{tpl['emoji']} **{tpl['name']}** — sign-ups are open!"
        )
        if tpl["rarity"] in qs.LOUD_RARITIES:
            headline = (
                f"🚨 **@here — a {qs.RARITY_BADGE[tpl['rarity']]} quick scam "
                f"has appeared!** 🚨\n"
                f"{interaction.user.mention} has turned up {tpl['emoji']} "
                f"**{tpl['name']}**. This does not happen often."
            )
        if paid:
            headline += (
                f"\n_(They paid {money(tpl['initiator_cost'])} in setup costs "
                "to open it.)_"
            )
        elif tpl["initiator_cost"]:
            headline += (
                f"\n_(They could not cover the "
                f"{money(tpl['initiator_cost'])} setup cost, so it was waived — "
                "and so is their initiator bonus.)_"
            )
        if staked_up_front:
            headline += f"\nThey are in for **{money(staked_up_front)}**."
        headline += stake_note

        await interaction.response.send_message(
            content=headline,
            embed=embed,
            view=OperationView(tpl["free_entry"]),
        )
        try:
            msg = await interaction.original_response()
            await self.conn.execute(
                "UPDATE scam_operations SET message_id = ? WHERE id = ?",
                (str(msg.id), op_id),
            )
            await self.conn.commit()
        except Exception:
            logger.debug("scam_game: could not store operation message id")

        asyncio.create_task(
            self._resolve_later(op_id, (resolve_at - _now()).total_seconds())
        )

    @staticmethod
    def _quickscam_ready_at(player: dict) -> Optional[datetime]:
        last = player.get("last_quickscam_at")
        if not last:
            return None
        return _parse(last) + timedelta(hours=QUICKSCAM_TRIGGER_COOLDOWN_HOURS)

    async def open_operation_id(self) -> Optional[int]:
        """Return the id of the single currently-open operation, if any."""
        async with self.conn.execute(
            "SELECT id FROM scam_operations WHERE status = 'open'"
            " ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def _operation_state(self, operation_id: int) -> Optional[dict]:
        """Return an operation with its template and participants.

        ``entries`` is ordered by join time, because the pyramid template pays
        by join order — sorting by stake would silently reshuffle who counts as
        an early investor.
        """
        async with self.conn.execute(
            "SELECT id, initiator_id, title, blurb, resolve_at, status,"
            " template_id, initiator_paid"
            " FROM scam_operations WHERE id = ?",
            (operation_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        entries: list[tuple[str, int]] = []
        async with self.conn.execute(
            "SELECT discord_user_id, amount FROM scam_operation_entries"
            " WHERE operation_id = ? ORDER BY COALESCE(joined_at, ''), rowid",
            (operation_id,),
        ) as cur:
            async for r in cur:
                entries.append((str(r[0]), int(r[1])))
        # Operations opened before the template rework have no template_id;
        # fall back to the gentlest common one so they still resolve sanely
        # instead of crashing the resolver on a None lookup.
        tpl = qs.get(str(row[6] or "")) or qs.BY_ID["titles"]
        return {
            "id": int(row[0]), "initiator_id": str(row[1]), "title": str(row[2]),
            "blurb": str(row[3]), "resolve_at": str(row[4]), "status": str(row[5]),
            "template": tpl, "initiator_paid": bool(row[7]),
            "entries": entries,
        }

    def _operation_embed(self, op: dict, guild: Optional[discord.Guild]) -> discord.Embed:
        """Render the operation announcement: the pitch, the terms, the room."""
        tpl = op["template"]
        unix = int(_parse(op["resolve_at"]).timestamp())
        entries = op["entries"]
        pot = sum(a for _u, a in entries)
        chance = qs.success_chance(tpl, len(entries), pot)
        extreme = qs.extreme_chance(tpl, len(entries), pot)
        minor, _standard, big = tpl["fail_losses"]

        lines = []
        for i, (uid, amount) in enumerate(entries):
            member = guild.get_member(int(uid)) if guild else None
            name = member.display_name if member else f"Prince {uid[-4:]}"
            crown = " 👑" if uid == op["initiator_id"] else ""
            stake = money(amount) if amount else "_free seat_"
            # Only the pyramid actually pays by position, so only the pyramid
            # advertises it.
            seat = f"`#{i + 1}` " if tpl["payout_by_order"] else ""
            lines.append(f"{seat}**{name}**{crown} — {stake}")

        embed = discord.Embed(
            title=f"{tpl['emoji']} {tpl['name']}",
            description=(
                f"{qs.RARITY_BADGE[tpl['rarity']]}  ·  Risk: **{tpl['risk']}**\n\n"
                f"{tpl['description']}\n\n"
                f"⏳ Closes <t:{unix}:R>"
            ),
            colour=qs.RARITY_COLOUR[tpl["rarity"]],
        )
        embed.add_field(
            name="💵 Terms",
            value=(
                f"Stake: {qs.stake_hint(tpl)} {CURRENCY}\n"
                f"Seats: **{len(entries)}/{tpl['max_participants']}**\n"
                f"Return on success: **×{tpl['payout_min']:g}"
                + (f"–×{tpl['payout_max']:g}" if tpl["payout_max"] != tpl["payout_min"] else "")
                + "** _(includes your stake)_\n"
                + (
                    "On an ordinary failure you lose "
                    f"**{minor * 100:.0f}–{big * 100:.0f}%** of your stake.\n"
                )
                + (
                    f"💥 **{extreme * 100:.0f}% chance of total catastrophe** — "
                    f"everything gone, and a **{tpl['arrest_chance'] * 100:.0f}% "
                    "chance of arrest each.\n"
                )
                + f"Current odds of success: **{chance * 100:.0f}%**"
            ),
            inline=False,
        )
        embed.add_field(name="👥 How the room changes it", value=tpl["crowd_note"], inline=False)
        if tpl["initiator_bonus_note"]:
            embed.add_field(
                name="👑 Initiator", value=tpl["initiator_bonus_note"], inline=False
            )
        embed.add_field(
            name=f"Signed up: {len(entries)} · pot {money(pot)}",
            value="\n".join(lines) or "_Nobody yet._",
            inline=False,
        )
        embed.set_footer(
            text="Odds and payouts update as people join. Nobody is obliged to."
        )
        return embed

    async def handle_join(
        self, interaction: discord.Interaction, operation_id: int, stake: int
    ) -> None:
        """Validate and record a buy-in (``stake = 0`` means a free seat).

        Note there is deliberately no impersonation check here.  A player
        posing as a fake target may *join* a quick scam — only *starting* one
        is blocked.  Being undercover should keep you out of the spotlight,
        not bench you from the rest of the economy for three hours.
        """
        if not await require_free(interaction, self.conn, "join a quick scam"):
            return
        uid = str(interaction.user.id)

        async with self._lock:
            op = await self._operation_state(operation_id)
            if not op or op["status"] != "open":
                await interaction.response.send_message(
                    "❌ That quick scam has already been carried out.",
                    ephemeral=True,
                )
                return
            tpl = op["template"]
            already = next((a for u, a in op["entries"] if u == uid), None)
            seats_used = len(op["entries"])

            if already is None and seats_used >= tpl["max_participants"]:
                await interaction.response.send_message(
                    f"❌ **{tpl['name']}** is full — all "
                    f"{tpl['max_participants']} seats are taken. "
                    "Wait for the next one.",
                    ephemeral=True,
                )
                return

            if stake == 0:
                if not tpl["free_entry"]:
                    await interaction.response.send_message(
                        f"❌ **{tpl['name']}** has no free seats. The minimum "
                        f"stake is {money(tpl['min_stake'])}.",
                        ephemeral=True,
                    )
                    return
                if already is not None:
                    await interaction.response.send_message(
                        "❌ You are already in this one.", ephemeral=True
                    )
                    return
            else:
                # A top-up compounds, so the *total* is what has to fit the
                # template's band — otherwise five 500s would sneak past a
                # 750 cap one click at a time.
                total = (already or 0) + stake
                if total < tpl["min_stake"]:
                    await interaction.response.send_message(
                        f"❌ The minimum stake on **{tpl['name']}** is "
                        f"{money(tpl['min_stake'])}"
                        + (f" — you are at {money(already)}." if already else "."),
                        ephemeral=True,
                    )
                    return
                if total > tpl["max_stake"]:
                    room = tpl["max_stake"] - (already or 0)
                    await interaction.response.send_message(
                        f"❌ **{tpl['name']}** caps each player at "
                        f"{money(tpl['max_stake'])}."
                        + (
                            f" You are already in for {money(already)}, so you "
                            f"can add at most {money(room)}."
                            if already else ""
                        ),
                        ephemeral=True,
                    )
                    return

                player = await get_player(self.conn, uid)
                if player["balance"] < stake:
                    await interaction.response.send_message(
                        f"❌ That costs {money(stake)} and you have "
                        f"{money(player['balance'])}.",
                        ephemeral=True,
                    )
                    return
                await adjust_balance(self.conn, uid, -stake, "quickscam_stake", tpl["name"])

            await self.conn.execute(
                "INSERT INTO scam_operation_entries"
                " (operation_id, discord_user_id, amount, joined_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(operation_id, discord_user_id) DO UPDATE SET"
                " amount = amount + excluded.amount",
                (operation_id, uid, stake, _iso(_now())),
            )
            await self.conn.commit()
            op = await self._operation_state(operation_id)
            staked_total = next((a for u, a in op["entries"] if u == uid), stake)
            filled = await self._maybe_close_early(op)

        # A one-line receipt here; the original announcement is edited in
        # place below so its pot, odds, countdown and Join button stay correct
        # without another copy of the card landing in the channel.
        await interaction.response.send_message(
            content=(
                _join_summary(interaction.user.mention, stake, staked_total, op)
                + (
                    f"\n🔒 **Every seat is taken** — the operation runs in "
                    f"**{QUICKSCAM_FULL_HOUSE_MINUTES} minutes**."
                    if filled else ""
                )
            )
        )
        await self._refresh_open_operation(op["id"])

    async def _maybe_close_early(self, op: dict) -> bool:
        """Cut the sign-up window short once the operation is full.

        Returns True if the deadline actually moved.  The already-scheduled
        resolver is left alone rather than cancelled: it will fire at the
        original time, find the operation already resolved, and return.  That
        is simpler than tracking task handles and is safe because
        :meth:`resolve_operation` refuses to run twice.
        """
        tpl = op["template"]
        if len(op["entries"]) < tpl["max_participants"]:
            return False
        new_at = _now() + timedelta(minutes=QUICKSCAM_FULL_HOUSE_MINUTES)
        if _parse(op["resolve_at"]) <= new_at:
            return False        # already closing sooner than that
        await self.conn.execute(
            "UPDATE scam_operations SET resolve_at = ? WHERE id = ? AND status = 'open'",
            (_iso(new_at), op["id"]),
        )
        await self.conn.commit()
        op["resolve_at"] = _iso(new_at)
        logger.info(
            "scam_game: operation %d is full — closing in %d minutes",
            op["id"], QUICKSCAM_FULL_HOUSE_MINUTES,
        )
        asyncio.create_task(
            self._resolve_later(op["id"], (new_at - _now()).total_seconds())
        )
        return True

    async def resolve_operation(self, operation_id: int) -> None:
        """Roll the operation's outcome, pay everyone out, and announce it.

        One roll picks between three top-level outcomes — success, ordinary
        failure and extreme failure.  Extreme failure is a *direct* probability
        rather than a share of the failures, so a 60/10 template really does
        blow up one time in ten and not one failure in four.
        """
        async with self._lock:
            op = await self._operation_state(operation_id)
            if not op or op["status"] != "open":
                return
            async with self.conn.execute(
                "SELECT channel_id FROM scam_operations WHERE id = ?",
                (operation_id,),
            ) as cur:
                row = await cur.fetchone()
            channel_id = str(row[0])

            await self.conn.execute(
                "UPDATE scam_operations SET status = 'resolved' WHERE id = ?",
                (operation_id,),
            )
            await self.conn.commit()

            tpl = op["template"]
            entries = op["entries"]
            initiator_id = op["initiator_id"]
            paid_entries = [(u, a) for u, a in entries if a > 0]
            free_entries = [u for u, a in entries if a == 0]
            pot = sum(a for _u, a in paid_entries)

            if not entries:
                await self._announce_empty(channel_id, tpl)
                return

            n = len(entries)
            chance = qs.success_chance(tpl, n, pot)
            outcome = qs.roll_outcome(tpl, n, pot)
            success = outcome == "success"
            rare = (
                success and tpl["rare_chance"] > 0
                and random.random() < tpl["rare_chance"]
            )
            # One shared roll, so a range-payout template gives the whole room
            # the same multiplier — it is one operation, not one per person.
            roll = random.random()

            severity = None
            loss_pct = 0.0
            if outcome == "ordinary":
                key, severity, idx = qs.roll_severity()
                loss_pct = tpl["fail_losses"][idx]
            elif outcome == "extreme":
                loss_pct = 1.0

            # The bonus requires the initiator to have actually bought in.
            # Otherwise triggering would be free money on every success, and
            # the whole point is that starting an operation creates an
            # opportunity for the room rather than a private income.
            initiator_in = any(u == initiator_id for u, _a in entries)
            bonus_paid = 0
            refund = 0
            if success:
                if op["initiator_paid"] and tpl["initiator_refund"]:
                    refund = tpl["initiator_cost"]
                if initiator_in and (op["initiator_paid"] or not tpl["initiator_cost"]):
                    bonus_paid = tpl["initiator_bonus"]

            payouts: list[tuple[str, int, int, bool]] = []
            for order, (user_id, amount) in enumerate(paid_entries):
                if success:
                    mult = qs.payout_multiplier(
                        tpl, order=order, participants=n, rare=rare, roll=roll,
                    )
                else:
                    mult = 1.0 - loss_pct
                got = int(round(amount * mult))
                payouts.append((user_id, amount, got, False))

            for user_id in free_entries:
                got = 0
                if success and rare and tpl["free_rare_max"]:
                    got = random.randint(tpl["free_rare_min"], tpl["free_rare_max"])
                elif success and tpl["free_success_chance"] > 0:
                    if random.random() < tpl["free_success_chance"]:
                        got = random.randint(
                            tpl["free_payout_min"], tpl["free_payout_max"]
                        )
                payouts.append((user_id, 0, got, True))

            # The bonus is a flat sum from the operation, never a cut of
            # anyone's stake, so no participant is worse off for having
            # somebody else start it.
            for i, (user_id, staked, got, is_free) in enumerate(payouts):
                extra = 0
                if user_id == initiator_id:
                    extra = bonus_paid + refund
                total = got + extra
                if total:
                    await adjust_balance(self.conn, user_id, total, "quickscam_payout", tpl["name"])
                payouts[i] = (user_id, staked, got + extra, is_free)
                if staked:
                    # EV is computed from the *final* room: that is the bet
                    # they were actually in by the time it resolved.
                    order = next(
                        (o for o, (u, _a) in enumerate(paid_entries) if u == user_id),
                        0,
                    )
                    ev = staked * (
                        qs.expected_multiplier(
                            tpl, order=order, participants=n, total_invested=pot
                        ) - 1.0
                    )
                    await record_play(
                        self.conn, user_id, "quickscam", ev,
                        (got + extra) - staked, tpl["name"],
                    )

            arrested: list[tuple[str, dict]] = []
            if outcome == "extreme":
                arrested = await self._arrest_participants(tpl, payouts)
            await self.conn.commit()

        await self._announce_resolution(
            channel_id, op, tpl, payouts, chance,
            outcome=outcome, rare=rare, severity=severity, loss_pct=loss_pct,
            pot=pot, bonus=bonus_paid, refund=refund, arrested=arrested,
        )
        await self._tell_the_fund(tpl, outcome=outcome, rare=rare)

    async def _arrest_participants(
        self, tpl: dict, payouts: list[tuple[str, int, int, bool]]
    ) -> list[tuple[str, dict]]:
        """Roll arrest independently for every participant after a catastrophe.

        Free seats are included on purpose: they were part of the operation
        even if they staked nothing.  The bribe is what the operation cost
        them, floored so a free-seat arrest still means something — the spec
        does not set a figure, and a zero bribe would make jail a formality.
        """
        out: list[tuple[str, dict]] = []
        for user_id, staked, _got, _is_free in payouts:
            if random.random() >= tpl["arrest_chance"]:
                continue
            if await get_jail(self.conn, user_id):
                continue  # already inside; one sentence at a time
            player = await get_player(self.conn, user_id)
            wealth = player["balance"] + player["invested"]
            bribe = max(EXTREME_FAILURE_MIN_BRIBE, staked)
            jail = await arrest_player(self.conn, user_id, bribe, wealth)
            out.append((user_id, jail))
        return out

    async def _tell_the_fund(self, tpl: dict, *, outcome: str, rare: bool) -> None:
        """Report the outcome to Roger, who takes it personally.

        The fund's risk level reacts to how the room's scams are going — a
        rare success reassures him into recklessness, a catastrophe scares
        him. Rarity is irrelevant; only the outcome counts.
        """
        fund = self.bot.get_cog("royal_fund")
        if fund is None:
            return
        if outcome == "success":
            key = "rare_success" if rare else "success"
        elif outcome == "extreme":
            key = "extreme"
        else:
            key = "failure"
        await fund.note_quick_scam(key, tpl["id"])

    async def _announce_empty(self, channel_id: str, tpl: dict) -> None:
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        try:
            await channel.send(embed=discord.Embed(
                title=f"{tpl['emoji']} {tpl['name']} — CALLED OFF",
                description=(
                    "Sign-ups closed with nobody in the room.\n\n"
                    "The opportunity evaporates, as opportunities do."
                ),
                colour=_EMBED_GREY,
            ))
        except discord.HTTPException:
            logger.warning("scam_game: could not announce empty operation")

    async def _announce_resolution(
        self, channel_id: str, op: dict, tpl: dict,
        payouts: list[tuple[str, int, int, bool]], chance: float, *,
        outcome: str, rare: bool, severity: Optional[str], loss_pct: float,
        pot: int, bonus: int, refund: int,
        arrested: list[tuple[str, dict]],
    ) -> None:
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            logger.warning("scam_game: channel %s gone, cannot announce", channel_id)
            return
        guild = getattr(channel, "guild", None)
        initiator_id = op["initiator_id"]

        def name_of(user_id: str) -> str:
            member = guild.get_member(int(user_id)) if guild else None
            return member.display_name if member else f"Prince {user_id[-4:]}"

        if outcome == "extreme":
            scene = tpl["extreme_message"]
            label, colour = "CATASTROPHE", _EMBED_RED
        elif rare:
            scene = tpl["rare_success_message"]
            label, colour = "RARE SUCCESS", _EMBED_GOLD
        elif outcome == "success":
            scene = tpl["success_message"]
            label, colour = "SUCCESS", _EMBED_GREEN
        else:
            scene = tpl["failure_message"]
            label, colour = "FAILED", _EMBED_RED

        lines = []
        for user_id, staked, got, is_free in sorted(payouts, key=lambda x: -x[2]):
            crown = " 👑" if user_id == initiator_id else ""
            cuffs = " 🚔" if any(u == user_id for u, _j in arrested) else ""
            if is_free:
                lines.append(
                    f"🆓 **{name_of(user_id)}**{crown}{cuffs} — free seat, "
                    + (f"received **{money(got)}**" if got else "received nothing")
                )
                continue
            net = got - staked
            sign = "+" if net >= 0 else "−"
            lines.append(
                f"**{name_of(user_id)}**{crown}{cuffs} — staked {money(staked)}, "
                f"back {money(got)} (**{sign}{money(abs(net))}**)"
            )

        detail = [f"Pot: **{money(pot)}** · odds were **{chance * 100:.0f}%**"]
        if outcome == "ordinary" and severity:
            detail.append(
                f"{severity} — everyone loses **{loss_pct * 100:.0f}%** of "
                "their stake"
            )
        elif outcome == "extreme":
            detail.append("**Every stake is gone.**")
        if bonus:
            detail.append(f"Initiator's bonus: **{money(bonus)}**")
        if refund:
            detail.append(f"Setup cost refunded: **{money(refund)}**")

        embed = discord.Embed(
            title=f"{tpl['emoji']} {label} — {tpl['name']}",
            description=(
                f"{scene}\n\n"
                + "\n".join(detail) + "\n\n"
                + "\n".join(lines)
            ),
            colour=colour,
        )
        if outcome == "extreme":
            pct = int(tpl["arrest_chance"] * 100)
            if arrested:
                who = ", ".join(f"**{name_of(u)}**" for u, _j in arrested)
                embed.add_field(
                    name=f"🚔 ARRESTED ({pct}% each)",
                    value=(
                        f"{who} — picked up in the raid.\n"
                        "`/paybribe` to buy your way out, `/appeal` if you "
                        "cannot afford it, or wait out the sentence."
                    ),
                    inline=False,
                )
            else:
                embed.add_field(
                    name=f"🚔 NOBODY WAS ARRESTED ({pct}% each)",
                    value="Everybody involved got away. This time.",
                    inline=False,
                )
        embed.set_footer(text=f"{qs.RARITY_BADGE[tpl['rarity']]} quick scam")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("scam_game: could not announce operation %d", op["id"])

    # ── /scamhelp ─────────────────────────────────────────────────────

    @app_commands.command(
        name="scamhelp",
        description="Short guide to every Scam Economy command.",
    )
    async def scamhelp(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        # Private: a command list is reference material, not an event. Posting
        # it publicly pushed the board and the running operations off screen
        # for everyone else.
        await interaction.response.send_message(
            embed=scamhelp_embed(), ephemeral=True
        )

    # ── /prestigestatus, /prestigeinvest ──────────────────────────────
    # Announced now, built later: the rules already promise an end goal, and a
    # command that answers "not yet" is friendlier than one that does not exist.

    @app_commands.command(
        name="prestigestatus",
        description="Nigeria's Prestige Project and who has funded it.",
    )
    async def prestigestatus(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🏗️ PRESTIGE PROJECT",
                description=(
                    "Nigeria's great national Prestige Project has not been "
                    "announced yet.\n\n"
                    "Soon, Princes will be able to permanently sacrifice their "
                    "fortunes to construct something worthy of the Oba.\n\n"
                    "Use this command again after the Prestige update."
                ),
                colour=_EMBED_GOLD,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="prestigeinvest",
        description="Permanently contribute Naira to the Prestige Project.",
    )
    async def prestigeinvest(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🏗️ PRESTIGE PROJECT NOT YET OPEN",
                description=(
                    "The Oba is not yet accepting donations.\n\n"
                    "Your Naira remains temporarily safe.\n\n"
                    "**No money was deducted.**"
                ),
                colour=_EMBED_GREY,
            ),
            ephemeral=True,
        )

    # ── /scamrules ────────────────────────────────────────────────────

    @app_commands.command(
        name="scamrules",
        description="How the Nigerian Scam-Economy works.",
    )
    async def scamrules(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, RULES_CHANNEL_ID, RULES_CHANNEL_URL):
            return
        await interaction.response.defer()
        for batch in batch_embeds(scamrules_embeds()):
            await interaction.followup.send(embeds=batch)


# ── Rules copy ────────────────────────────────────────────────────────────────

def scamhelp_embed() -> discord.Embed:
    """The short command guide.

    Deliberately one line per command: a new player wants to know what exists,
    not how any of it is balanced.  `/scamrules` is where the detail lives.
    """
    return discord.Embed(
        title="🇳🇬 NIGERIAN SCAM ECONOMY — QUICK HELP",
        description=(
            f"You start with **{money(START_BALANCE)}**.\n\n"
            "Make money, lose money, scam your fellow Princes and try not to "
            "get arrested.\n\n"
            "💸 **/scam** — your personal scam. Your odds recover over time.\n"
            "🤝 **/quickscam** — start a shared scam operation.\n"
            "💰 **/joinquickscam** — buy into the running Quick Scam.\n\n"
            "🎯 **/targets** — open the shared Target Board.\n"
            "🔎 **Intel** — the button on a target: improves its public odds "
            "and privately checks whether it may be another player.\n"
            "🔎 **/intelstatus** — check your Intel Charges.\n"
            "🎯 **/targethelp** — the detailed Target / Intel / Fake Target "
            "explanation.\n"
            "🎭 **/faketarget** — pose as a target and scam another Prince.\n"
            "🎭 **/cancelfake** — abandon your disguise.\n\n"
            "🏦 **/invest deposit** — put money into Roger's Fund.\n"
            "🏦 **/invest withdraw** — take money out.\n"
            "🏦 **/invest status** — Fund value, risk, investors, your position.\n\n"
            "💰 **/balance** — check your Naira.\n"
            "🏆 **/leaderboard** — the richest Princes.\n"
            "🪙 **/beg** — publicly beg the Council of Princes.\n"
            "📋 **/begboard** — biggest donors and biggest beggars.\n\n"
            "🚔 **/paybribe** — pay your own bribe.\n"
            "🤝 **/bail** — pay somebody else's bribe.\n"
            "⚖️ **/appeal** — plead your case and let the room vote.\n\n"
            "🏗️ **/prestigestatus** — Nigeria's future Prestige Project.\n"
            "🏗️ **/prestigeinvest** — permanently contribute toward it.\n\n"
            "📜 **/scamrules** — the full game rules."
        ),
        colour=_EMBED_GOLD,
    ).set_footer(text="No real money is involved. Obviously.")


def scamrules_embeds() -> list[discord.Embed]:
    """The canonical rules, one embed per section."""
    E = discord.Embed
    return [
        E(
            title="🇳🇬 THE NIGERIAN SCAM ECONOMY",
            description=(
                f"Everyone starts with **{money(START_BALANCE)}**.\n\n"
                "Make a fortune through scams, shared operations, targets and "
                "highly questionable investments.\n\n"
                "There are also plenty of ways to lose it again.\n\n"
                "_All money is fictional._"
            ),
            colour=_EMBED_GOLD,
        ),
        E(
            title="1️⃣ /SCAM — WORK ALONE",
            description=(
                "Use **/scam** whenever you want.\n\n"
                "There is no hard cooldown, but your odds recover over roughly "
                f"**{SCAM_FULL_HOURS:g} hours**. Trying again immediately is "
                "allowed — it is simply much more dangerous.\n\n"
                "Possible outcomes include small wins, larger payouts, nothing "
                "at all, losses, rare jackpots and serious failures.\n\n"
                "**A scam going badly does not automatically jail you.** You "
                "are arrested only if a scam goes sideways and you do not have "
                "enough **cash on hand** to cover the resulting bribe.\n\n"
                "Money sitting in Roger's Fund does **not** count as cash on "
                "hand, and is never withdrawn automatically to keep you out of "
                "prison.\n\n"
                "If you have the cash, the bribe is simply paid and you stay "
                "free. Normal scam losses never create debt."
            ),
            colour=_EMBED_GREEN,
        ),
        E(
            title="2️⃣ QUICK SCAMS — WORK TOGETHER",
            description=(
                "Use **/quickscam** to trigger a random shared operation. Only "
                "**one** can run at a time. Everyone else uses "
                "**/joinquickscam** to buy in before the timer closes.\n\n"
                f"There are **{len(qs.TEMPLATES)} different operations**, and "
                "each has its own entry cost, duration, participant limit, "
                "odds, payout and special rules. On some, extra investors "
                "*lower* everyone's chances. On others a crowd is the entire "
                "pitch. Two of them want a *specific* amount of money in the "
                "pot and get worse if you overfund them. **Read the post.**\n\n"
                "Results: success · rare success / jackpot · partial loss · "
                "major loss · **Extreme Failure**.\n\n"
                "An Extreme Failure wipes every stake and can get participants "
                "**arrested** — including anyone who took a free seat.\n\n"
                f"Your **/quickscam** trigger has a "
                f"**{QUICKSCAM_TRIGGER_COOLDOWN_HOURS:g}-hour cooldown**."
            ),
            colour=_EMBED_GOLD,
        ),
        E(
            title="3️⃣ /TARGETS — THE SHARED MARKS",
            description=(
                "The board holds **three shared targets**. Everybody works the "
                "same board, so one player's actions can improve, damage or "
                "remove a mark for everyone else.\n\n"
                "Each card shows its tier, odds, attempt cost, payout, "
                "attempts, any pot, and special rules where they exist.\n\n"
                "🛡️ **Careful** — better odds, smaller payout\n"
                "🎯 **Normal** — standard odds and payout\n"
                "🤑 **Greedy** — usually worse odds, larger payout\n\n"
                "_“Usually” is doing real work there — every persona sets its "
                "own odds and multipliers, and several break the pattern "
                "completely._\n\n"
                f"There is a **{TARGET_ATTEMPT_COOLDOWN}-minute cooldown** "
                "between your target attempts.\n\n"
                "**Fail** and your attempt cost may feed the mark's pot; after "
                "enough attempts the mark disappears. **Succeed** and you take "
                "the payout *plus* the whole pot, and the mark leaves.\n\n"
                "🟢 **Ordinary** · 🔵 **Great Catch** · 🟣 **Rare** · "
                "🐋 **Whale** (leaves after **1 hour**) · 🦄 **Legendary** "
                "(may use completely unique rules)\n\n"
                "Use **/targethelp** for the detailed system."
            ),
            colour=_EMBED_GOLD,
        ),
        E(
            title="4️⃣ 🔎 INTEL",
            description=(
                f"You hold **{INTEL_MAX_CHARGES} Intel Charges** and regain "
                f"**+1 every {INTEL_RECHARGE_HOURS:g} hours**.\n\n"
                "**Cost by tier**\n"
                "🟢 Ordinary — **75 Naira**\n"
                "🔵 Great Catch — **125 Naira**\n"
                "🟣 Rare — **200 Naira**\n"
                "🐋 Whale — **300 Naira**\n"
                "🦄 Legendary — **no Intel at all**\n\n"
                "A target can receive a maximum of **2 Intel missions total**.\n\n"
                "Intel can improve a target's public odds *for everybody*, "
                "trigger a **Major Intelligence Breakthrough**, and privately "
                "tell you whether the mark looks REAL or FAKE. Your report "
                "states how reliable it is.\n\n"
                "Afterwards your target actions are locked for **2 minutes** — "
                "but other players can already use the improved public odds. "
                "Intel does **not** consume your 15-minute attempt cooldown.\n\n"
                "Use **/intelstatus** to check your charges privately."
            ),
            colour=_EMBED_GREY,
        ),
        E(
            title="5️⃣ 🎭 FAKE TARGETS — PLAYER VS PLAYER",
            description=(
                "Some targets are secretly other players.\n\n"
                "**/faketarget** enters the hidden disguise queue.\n"
                f"Cover deposit **{money(FAKE_COVER_DEPOSIT)}** · personal "
                "cooldown **6 hours** · disguise lasts up to **3 hours** · at "
                "most **2** fakes are ever active.\n\n"
                "**Your disguise is random.** You do not choose who you become.\n\n"
                "While disguised you cannot `/scam`, attack targets, "
                "counter-scam or start a Quick Scam. You *can* gather Intel on "
                "other marks, join Quick Scams and use the Fund.\n\n"
                "**The first attack always ends the disguise.** Attacking one "
                "blind gives you a chance to notice the trap:\n"
                "🛡️ Careful **30%** · 🎯 Normal **25%** · 🤑 Greedy **20%**\n"
                "Public Intel on that mark improves those odds.\n\n"
                "**Escape** and you lose only the attempt cost; the faker is "
                "exposed and forfeits their deposit.\n"
                "**Get caught** and they take your attempt cost *and* part of "
                "your exposed wealth.\n\n"
                "**/cancelfake** abandons a disguise after a **5-minute** "
                "wind-up. You stay vulnerable throughout it and lose the "
                "deposit if it completes."
            ),
            colour=_EMBED_RED,
        ),
        E(
            title="6️⃣ 🎭 COUNTER-SCAM",
            description=(
                "If your private Intel says a mark may be fake, you may be "
                "offered a **Counter-Scam**. The report shows its reliability, "
                "the false-lead risk, the operational stake and your real "
                "overall success chance.\n\n"
                "🌟 Verified Fake — **60%** overall takedown\n"
                "🟡 Strong Lead saying fake — **40%** overall\n\n"
                "**Take them down** and your stake is returned, you take their "
                "500 Naira cover deposit, and you seize part of their exposed "
                "wealth.\n\n"
                "**Fail against a real fake** and they counter-scam you "
                "instead.\n\n"
                "**Accuse a genuine mark** and the counter-scam fails "
                "automatically — you simply lose the stake.\n\n"
                "_All PvP results are announced publicly._"
            ),
            colour=_EMBED_RED,
        ),
        E(
            title="7️⃣ 💰 EXPOSED WEALTH",
            description=(
                "For Fake Target and Counter-Scam PvP:\n\n"
                "**Exposed Wealth = cash + your Royal Investment Fund "
                "position**\n\n"
                "Putting money into Roger's Fund does **not** protect it from "
                "another Prince.\n\n"
                "PvP takes cash first. If that is not enough, part of your Fund "
                "position is **forcibly liquidated** — no withdrawal tax, no "
                "confirmation.\n\n"
                f"Percentage losses always leave you your final "
                f"**{money(PROTECTED_WEALTH_FLOOR)}** of total exposed wealth."
            ),
            colour=_EMBED_GOLD,
        ),
        E(
            title="8️⃣ 🏦 THE ROYAL INVESTMENT FUND",
            description=(
                "**/invest deposit** · **/invest withdraw** · "
                "**/invest status**\n\n"
                "The Fund has **5 risk levels**. As risk rises, events happen "
                "faster, gains and losses become more extreme, dangerous events "
                "get more common, and collapse becomes possible.\n\n"
                "The Fund can appreciate, depreciate, pay dividends, charge "
                "fees, redistribute money between investors, trigger special "
                "events — or **completely collapse**.\n\n"
                "At high risk, withdrawals face an **Anti-Panic Tax**.\n\n"
                "Money inside the Fund is part of your investment position, not "
                "your cash. A **Total Collapse** wipes it.\n\n"
                "_Roger remains professionally optimistic._"
            ),
            colour=_EMBED_RED,
        ),
        E(
            title="9️⃣ 🚔 JAIL, BRIBES AND BAIL",
            description=(
                "A serious failed scam can create a bribe. **You are only "
                "arrested when you cannot pay it from cash on hand.**\n\n"
                "If you have the cash, it is paid immediately and you stay "
                "free. If you do not, you may be jailed — and while jailed, "
                "major scam activities and target actions are blocked.\n\n"
                "💸 **/paybribe** — pay your own outstanding bribe\n"
                "🤝 **/bail** — pay another player's bribe\n"
                "⚖️ **/appeal** — publicly plead your case and let the room vote\n\n"
                "Your total wealth **includes your Fund position** when working "
                "out whether you are genuinely broke. Hiding money in Roger's "
                "Fund does not qualify you for poor-player treatment.\n\n"
                "Genuinely broke Princes get more lenient treatment and are "
                "never forced into debt."
            ),
            colour=_EMBED_RED,
        ),
        E(
            title="🔟 🪙 BEGGING + LEADERBOARDS",
            description=(
                "**/beg** — publicly ask other Princes for money\n"
                "**/begboard** — who has donated most, and who has received most\n"
                "**/balance** — check your finances\n"
                "**/leaderboard** — the richest Princes"
            ),
            colour=_EMBED_GREY,
        ),
        E(
            title="🏗️ HOW DO YOU WIN?",
            description=(
                "A permanent end goal is being added to the game:\n\n"
                "**THE PRESTIGE PROJECT**\n\n"
                "Nigeria will build an enormous, completely unnecessary "
                "national monument to honour the Oba. Players will be able to "
                "permanently contribute their Naira toward it.\n\n"
                "The money is a donation. **You do not get it back.**\n\n"
                "The idea:\n"
                "• contribute part of your fortune to the national project\n"
                "• compete to become its greatest patron\n"
                "• reach a major personal contribution target and/or help "
                "complete the project\n"
                "• earn permanent prestige for doing something financially "
                "indefensible\n\n"
                "🏗️ **/prestigestatus** — the project, progress, top "
                "contributors and your lifetime contribution\n"
                "💸 **/prestigeinvest** — permanently contribute\n\n"
                "⚠️ Prestige contributions will be **irreversible**.\n\n"
                "_The exact project, victory condition and target amount will "
                "be announced in a future update._"
            ),
            colour=_EMBED_GOLD,
        ),
        E(
            title="📌 QUICK COMMAND LIST",
            description=(
                "`/scamhelp` · `/scamrules`\n\n"
                "`/scam`\n\n"
                "`/quickscam` · `/joinquickscam`\n\n"
                "`/targets` · `/targethelp` · `/intelstatus` · `/faketarget` · "
                "`/cancelfake`\n\n"
                "`/invest deposit` · `/invest withdraw` · `/invest status`\n\n"
                "`/balance` · `/leaderboard`\n\n"
                "`/beg` · `/begboard`\n\n"
                "`/paybribe` · `/bail` · `/appeal`\n\n"
                "`/prestigestatus` · `/prestigeinvest`"
            ),
            colour=_EMBED_GREY,
        ).set_footer(text="No real money is involved. Obviously."),
    ]


async def setup(bot: commands.Bot, conn: aiosqlite.Connection) -> ScamGameCog:
    """Create, register and reconcile the game cog."""
    cog = ScamGameCog(bot, conn)
    await bot.add_cog(cog)
    return cog
