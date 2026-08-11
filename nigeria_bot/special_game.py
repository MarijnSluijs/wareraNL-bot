"""/special — the three-offer event system, and Roger's recurring advice.

Shape of the thing
------------------
``/special`` shows one Budget, one Premium and one Platinum card.  The three
are generated once and *persisted*: reopening the menu shows the same three
until one is activated, so there is no reroll fishing.  Activating pays the
cost, runs the card and starts a two-hour cooldown; merely looking costs
nothing.

Everything a card can do lives in a resolver registered in ``RESOLVERS``.  A
resolver receives a small context object and returns what to say.  Adding a
card is a catalogue entry plus a resolver — never a change to this file's
control flow.

Roger's Advice is not a card and not part of any pool.  It ships here because
one of its outcomes resets /special.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from nigeria_bot import special_cards as sc
from nigeria_bot.special_effects import (
    add_effect,
    arm_trap,
    consume_effect,
    end_effect,
    get_effect,
    give_cash,
    global_effect,
    take_cash,
    take_trap,
    take_wealth,
    trap_armed,
    wealth_of,
)
from nigeria_bot.scam_game import (
    GAME_CHANNEL_ID,
    GAME_CHANNEL_URL,
    _EMBED_GOLD,
    _EMBED_GREEN,
    _EMBED_GREY,
    _EMBED_RED,
    _ack,
    _iso,
    _now,
    _parse,
    _reply,
    _require_channel,
    adjust_balance,
    arrest_player,
    get_jail,
    get_player,
    money,
    record_ledger,
    release_player,
    require_free,
    total_wealth,
)

logger = logging.getLogger("nigeria_bot.special_game")

# ── Configuration ─────────────────────────────────────────────────────────────

SPECIAL_COOLDOWN = timedelta(hours=sc.SPECIAL_COOLDOWN_HOURS)

# Roger's advice event (spec §10).  Deliberately frequent: it is a recurring
# gimmick, not a daily headline.
ROGER_GAP_NORMAL   = (30, 60)      # minutes
ROGER_GAP_BUSY     = (25, 45)
ROGER_WINDOW       = timedelta(minutes=5)
ROGER_BUSY_PLAYERS = 5             # distinct actors in the last 15 minutes
ROGER_BUSY_WINDOW  = timedelta(minutes=15)
ROGER_SCAM_CHANCE  = 0.25
ROGER_FEE          = 500

_EMBED_PURPLE = discord.Colour.from_rgb(155, 89, 182)


# ── Schema ────────────────────────────────────────────────────────────────────

async def setup_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS special_offers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id     TEXT NOT NULL,
            generated_at  TEXT NOT NULL,
            budget_card   TEXT NOT NULL,
            premium_card  TEXT NOT NULL,
            platinum_card TEXT NOT NULL,
            cohort        TEXT,          -- Carl Marx investor snapshot
            status        TEXT NOT NULL DEFAULT 'open',
            consumed_card TEXT,
            consumed_at   TEXT
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS special_offers_open"
        " ON special_offers (player_id, status)"
    )
    # Public clickable events: bait, raffles, campaigns, duels.  One row per
    # live event, with the claim state in the row so "first click wins" is a
    # conditional UPDATE rather than a lock held across a Discord round-trip.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS special_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            actor_id    TEXT NOT NULL,
            channel_id  TEXT,
            message_id  TEXT,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open',
            claimed_by  TEXT,
            payload     TEXT NOT NULL DEFAULT '{}'
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS special_event_entries (
            event_id    INTEGER NOT NULL,
            user_id     TEXT NOT NULL,
            amount      INTEGER NOT NULL DEFAULT 0,
            payload     TEXT NOT NULL DEFAULT '{}',
            joined_at   TEXT NOT NULL,
            PRIMARY KEY (event_id, user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS special_roger (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            next_at     TEXT,
            event_id    INTEGER,
            opened_at   TEXT,
            expires_at  TEXT,
            claimed_by  TEXT,
            status      TEXT
        )
    """)
    await conn.execute(
        "INSERT OR IGNORE INTO special_roger (id, status) VALUES (1, 'idle')"
    )
    for column in (
        "special_cooldown_until TEXT",
        # Last meaningful game action, which is what every "recently active"
        # window in the spec measures.  Kept here rather than derived from the
        # per-system timestamps so one cheap column answers all of them.
        "last_action_at TEXT",
    ):
        try:
            await conn.execute(f"ALTER TABLE scam_players ADD COLUMN {column}")
        except Exception:
            pass
    await conn.commit()


# ── Activity ──────────────────────────────────────────────────────────────────

async def touch(conn: aiosqlite.Connection, user_id: str) -> None:
    """Record a meaningful game action.

    Called from every system that counts as activity.  Ordinary chat does not
    reach here on purpose (spec §3): being present in the channel should not
    make somebody a target.
    """
    await conn.execute(
        "UPDATE scam_players SET last_action_at = ? WHERE discord_user_id = ?",
        (_iso(_now()), str(user_id)),
    )


async def actives(
    conn: aiosqlite.Connection, hours: float = sc.ACTIVE_WINDOW_HOURS, *,
    exclude: Optional[str] = None, min_cash: Optional[int] = None,
) -> list[str]:
    since = _iso(_now() - timedelta(hours=hours))
    sql = ("SELECT discord_user_id FROM scam_players"
           " WHERE last_action_at IS NOT NULL AND last_action_at >= ?")
    args: list = [since]
    if min_cash is not None:
        sql += " AND balance > ?"
        args.append(min_cash)
    sql += " ORDER BY last_action_at DESC"
    async with conn.execute(sql, args) as cur:
        out = [str(r[0]) async for r in cur]
    return [u for u in out if u != str(exclude)]


async def free_actives(
    conn: aiosqlite.Connection, hours: float = sc.ACTIVE_WINDOW_HOURS, *,
    exclude: Optional[str] = None,
) -> list[str]:
    """Active and not already in jail — the pool every arrest card draws from."""
    out = []
    for uid in await actives(conn, hours, exclude=exclude):
        if await get_jail(conn, uid) is None:
            out.append(uid)
    return out


async def richest(
    conn: aiosqlite.Connection, hours: float, *, exclude: Optional[str] = None,
    limit: int = 5, by: str = "balance",
) -> list[tuple[str, int]]:
    """Top holders by cash or by total wealth, among the recently active."""
    pool = await actives(conn, hours, exclude=exclude)
    if not pool:
        return []
    marks = ",".join("?" * len(pool))
    column = "balance" if by == "balance" else "balance + invested"
    async with conn.execute(
        f"SELECT discord_user_id, {column} FROM scam_players"
        f" WHERE discord_user_id IN ({marks}) ORDER BY 2 DESC LIMIT ?",
        (*pool, limit),
    ) as cur:
        return [(str(r[0]), int(r[1])) async for r in cur]


# ── Offer sets ────────────────────────────────────────────────────────────────

async def open_offer(conn: aiosqlite.Connection, uid: str) -> Optional[dict]:
    async with conn.execute(
        "SELECT id, budget_card, premium_card, platinum_card, cohort,"
        " generated_at FROM special_offers"
        " WHERE player_id = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        (str(uid),),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "cards": {sc.BUDGET: row[1], sc.PREMIUM: row[2], sc.PLATINUM: row[3]},
        "cohort": json.loads(row[4]) if row[4] else [],
        "generated_at": row[5],
    }


async def ready_at(conn: aiosqlite.Connection, uid: str) -> Optional[datetime]:
    async with conn.execute(
        "SELECT special_cooldown_until FROM scam_players WHERE discord_user_id = ?",
        (str(uid),),
    ) as cur:
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    when = _parse(row[0])
    return when if when > _now() else None


# ── Eligibility ───────────────────────────────────────────────────────────────
# Each predicate answers "could this card do anything at all right now?".  A
# card that would fizzle is never offered — a wasted slot is worse than a
# narrower pool (spec §17).

async def _eligibility_context(conn: aiosqlite.Connection, uid: str) -> dict:
    """One snapshot of the world, so 60 predicates do not run 60 queries."""
    from nigeria_bot import royal_fund as rf
    from nigeria_bot import scam_targets as st

    fund_positions = await rf.positions(conn)
    state = await rf.get_state(conn)
    cash, own_fund = await wealth_of(conn, uid)
    others_fund = [(u, a) for u, a in fund_positions if u != str(uid)]
    top10 = [u for u, _a in fund_positions[:10] if u != str(uid)]
    top10_amounts = {u: a for u, a in fund_positions[:10]}

    active8 = await actives(conn, sc.ACTIVE_WINDOW_HOURS, exclude=uid)
    active3 = await actives(conn, sc.ACTIVE_SHORT_HOURS, exclude=uid)
    active24 = await actives(conn, sc.ACTIVE_LONG_HOURS, exclude=uid)
    free8 = await free_actives(conn, sc.ACTIVE_WINDOW_HOURS, exclude=uid)

    rich_cash = await richest(conn, sc.ACTIVE_LONG_HOURS, exclude=uid, limit=5)
    rich_wealth = await richest(
        conn, sc.ACTIVE_LONG_HOURS, exclude=uid, limit=5, by="wealth"
    )

    poor = []
    for other in await actives(conn, sc.ACTIVE_WINDOW_HOURS):
        if await total_wealth(conn, other) < 5_000:
            poor.append(other)

    fakes = [t for t in await st.active_targets(conn) if t["is_fake"]]
    intel = await st.intel_state(conn, uid)

    return {
        "uid": str(uid), "cash": cash, "own_fund": own_fund,
        "fund_total": sum(a for _u, a in fund_positions),
        "fund_positions": fund_positions, "others_fund": others_fund,
        "top10": top10, "top10_amounts": top10_amounts,
        "risk": int(state["risk"]), "max_risk": 5,
        "active8": active8, "active3": active3, "active24": active24,
        "free8": free8, "rich_cash": rich_cash, "rich_wealth": rich_wealth,
        "poor": poor, "fakes": fakes, "intel": intel["charges"],
        "conn": conn,
    }


def _needs(ctx: dict, key: str) -> bool:
    fund_pos = dict(ctx["fund_positions"])
    top10_others = [u for u in ctx["top10"]]

    def top10_with(minimum: int) -> list[str]:
        return [u for u in top10_others if fund_pos.get(u, 0) >= minimum]

    return {
        "intel_not_full":    lambda: ctx["intel"] < 3,
        "victim_cash":       lambda: bool(ctx["active8"]),
        "trap_free":         lambda: True,       # checked per-trap at activation
        "begging_flow_free": lambda: True,
        "no_detector":       lambda: True,
        "no_insurance":      lambda: True,
        "no_guarantee":      lambda: True,
        "no_jail_card":      lambda: True,
        "no_crash_course":   lambda: True,
        "not_prince":        lambda: True,
        "no_big_short":      lambda: True,
        "no_fog":            lambda: True,
        "no_muggers":        lambda: True,
        "arrestable":        lambda: bool(ctx["free8"]),
        "three_arrestable":  lambda: len(ctx["free8"]) >= 3,
        "two_actives":       lambda: len(ctx["active8"]) >= 2,
        "six_actives":       lambda: len(ctx["active8"]) >= 5,
        "duel_opponent":     lambda: bool(ctx["active8"]),
        "predator_victim":   lambda: bool(ctx["rich_cash"]),
        "burn_victim":       lambda: bool(ctx["rich_wealth"]),
        "rich_cash_holder":  lambda: any(c > 2_500 for _u, c in ctx["rich_cash"]),
        "whale":             lambda: any(w > 5_000 for _u, w in ctx["rich_wealth"]),
        "grudge_victim":     lambda: bool(ctx["active3"]),
        "lottery_pool":      lambda: bool(ctx["active3"]) or True,
        "phishing_targets":  lambda: bool(ctx["active8"]),
        "poor_player":       lambda: bool(ctx["poor"]),
        "robin_hood_pair":   lambda: bool(ctx["poor"]),
        "two_investors":     lambda: len(ctx["fund_positions"]) >= 2,
        "fund_victim_1000":  lambda: bool(top10_with(1_000)),
        "fund_victim_4500":  lambda: bool(top10_with(4_500)),
        "fund_victim_acquire": lambda: bool(top10_with(FUND_ACQUIRE_MIN)),
        "offshore_victim":   lambda: any(
            a > sc.FUND_FLOOR_OFFSHORE for u, a in ctx["others_fund"]
        ),
        "freezable":         lambda: bool(ctx["others_fund"]),
        "nationalisable":    lambda: len(ctx["fund_positions"]) >= 6,
        "marx_cohort":       lambda: len(ctx["fund_positions"]) >= 2,
        "fund_5000":         lambda: ctx["fund_total"] >= 5_000,
        "fund_10000":        lambda: ctx["fund_total"] >= 10_000,
        "risk_above_1":      lambda: ctx["risk"] > 1,
        "risk_below_max":    lambda: ctx["risk"] < ctx["max_risk"],
        "risk_3_plus":       lambda: ctx["risk"] >= 3,
        "papers_targets":    lambda: True,
        "fakes_exist":       lambda: bool(ctx["fakes"]),
    }.get(key, lambda: True)()


FUND_ACQUIRE_MIN = sc.FUND_FLOOR_ACQUISITION + 500   # something must be takeable


async def eligible_cards(conn: aiosqlite.Connection, uid: str) -> set[str]:
    """Every card that could actually do something for this player right now."""
    ctx = await _eligibility_context(conn, uid)
    out = set()
    for cid, card in sc.CARDS.items():
        if all(_needs(ctx, need) for need in card["needs"]):
            out.add(cid)
    # Personal, non-stackable things are asked directly rather than snapshotted,
    # because they are cheap and the answer must be exact.
    for cid, kind in PERSONAL_BLOCKERS.items():
        if await get_effect(conn, kind, subject_id=str(uid)):
            out.discard(cid)
    for cid, kind in TRAP_BLOCKERS.items():
        if await trap_armed(conn, kind):
            out.discard(cid)
    for cid, kind in GLOBAL_BLOCKERS.items():
        if await global_effect(conn, kind):
            out.discard(cid)
    # The two begging-flow traps rewrite the same thing and cannot coexist.
    if await trap_armed(conn, "trickle_up") or await trap_armed(conn, "beggar_king"):
        out.discard("special_trickle_up_economics")
        out.discard("special_beggar_king")
    return out


PERSONAL_BLOCKERS = {
    "special_counterfeit_detector": "counterfeit_detector",
    "special_nigerian_insurance_policy": "insurance",
    "special_professional_guarantee": "professional_guarantee",
    "special_get_out_of_jail_free": "jail_card",
    "special_nigerian_scamming_crash_course": "crash_course",
    "special_prince_for_a_day": "prince_for_a_day",
    "special_the_big_short": "big_short",
}
TRAP_BLOCKERS = {
    "special_tax_audit": "tax_audit",
    "special_counterfeit_naira": "counterfeit_naira",
    "special_highwayman": "highwayman",
    "special_police_informant": "police_informant",
    "special_welfare_fraud": "welfare_fraud",
}
GLOBAL_BLOCKERS = {
    "special_fog_of_war": "fog_of_war",
    "special_unleash_the_muggers": "muggers",
}


# ── Resolver plumbing ─────────────────────────────────────────────────────────

class Ctx:
    """Everything a resolver is allowed to know.

    Resolvers never touch the interaction directly: they return text, and the
    caller decides where it goes.  That keeps "what the card does" separate
    from "who gets told", which is the only reason the visibility rules in
    spec §11 can be applied uniformly.
    """

    def __init__(self, cog: "SpecialCog", conn, actor_id: str, card: dict,
                 offer: Optional[dict] = None, choice: Optional[str] = None,
                 amount: int = 0, guild: Optional[discord.Guild] = None):
        self.cog = cog
        self.conn = conn
        self.actor = str(actor_id)
        self.card = card
        self.offer = offer or {}
        self.choice = choice
        self.amount = amount
        self.guild = guild

    def who(self, user_id: str) -> str:
        return f"<@{user_id}>"


class Result:
    """What a resolver produced: something public, something private, or both."""

    def __init__(self, *, public: Optional[str] = None,
                 private: Optional[str] = None,
                 title: Optional[str] = None,
                 colour: discord.Colour = _EMBED_GOLD,
                 view: Optional[discord.ui.View] = None,
                 store_message: bool = False):
        self.public = public
        self.private = private
        self.title = title
        self.colour = colour
        self.view = view
        self.store_message = store_message


RESOLVERS: dict[str, Callable[[Ctx], Awaitable[Result]]] = {}


def resolver(card_id: str):
    def wrap(fn):
        RESOLVERS[card_id] = fn
        return fn
    return wrap


def _weighted_pick(table: list[tuple[int, float]]) -> int:
    roll = random.random()
    for value, share in table:
        roll -= share
        if roll <= 0:
            return value
    return table[-1][0]


SPECIAL_ARREST_BRIBE = 500


async def jail_player(conn, user_id: str, *, bribe: int = SPECIAL_ARREST_BRIBE) -> str:
    """Arrest through the normal pipeline and describe what happened.

    Get Out of Jail Free is spent inside ``arrest_player`` itself, so every
    arrest in the game honours it and this function only has to read the flag
    back — checking for the card a second time here would find it already
    gone and report a release that had in fact happened.
    """
    wealth = await total_wealth(conn, user_id)
    jail = await arrest_player(conn, user_id, bribe, wealth)
    if jail.get("released"):
        return (f"🎫 <@{user_id}> was arrested, presented a suspiciously "
                "official card and walked straight back out.")
    return f"🚓 <@{user_id}> has been arrested."


# ── Resolvers: immediate cash ─────────────────────────────────────────────────

@resolver("special_cash_injection")
async def _cash_injection(ctx: Ctx) -> Result:
    amount = _weighted_pick(
        [(200, 0.40), (300, 0.30), (500, 0.20), (750, 0.08), (1_000, 0.02)]
    )
    await give_cash(ctx.conn, ctx.actor, amount, reason="special_gain",
                    detail="Cash Injection")
    return Result(
        title="💵 CASH INJECTION",
        public=(f"{ctx.who(ctx.actor)} has secured emergency liquidity.\n"
                f"💰 **+{money(amount)}**\n"
                "Nobody has asked where it came from."),
        colour=_EMBED_GREEN,
    )


@resolver("special_fake_news")
async def _fake_news(ctx: Ctx) -> Result:
    return Result(
        title="📢 BREAKING NEWS",
        public=random.choice(sc.FAKE_NEWS),
        colour=_EMBED_GREY,
    )


@resolver("special_lucky_man")
async def _lucky_man(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "lucky_man", owner_id=ctx.actor,
                     subject_id=ctx.actor, hours=sc.PERSONAL_BUFF_HOURS)
    return Result(
        title="🍀 LUCKY MAN",
        private=("Your next `/scam` will use the maximum 3-hour odds, any time "
                 "in the next **12 hours**.\n\nIt can still fail. It is simply "
                 "failing at the best possible rate."),
    )


@resolver("special_intelligence_leak")
async def _intel_leak(ctx: Ctx) -> Result:
    from nigeria_bot import scam_targets as st

    before = (await st.intel_state(ctx.conn, ctx.actor))["charges"]
    after = min(st.INTEL_MAX_CHARGES, before + 2)
    await ctx.conn.execute(
        "UPDATE scam_players SET intel_charges = ?,"
        " intel_next_charge_at = CASE WHEN ? >= ? THEN NULL"
        " ELSE intel_next_charge_at END WHERE discord_user_id = ?",
        (after, after, st.INTEL_MAX_CHARGES, ctx.actor),
    )
    return Result(
        title="🔎 INTELLIGENCE LEAK",
        private=("Two fresh Intel charges have arrived through completely "
                 f"legitimate channels.\n\n**Intel:** {before}/3 → **{after}/3**"),
    )


@resolver("special_sticky_fingers")
async def _sticky_fingers(ctx: Ctx) -> Result:
    pool = []
    for uid in await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS, exclude=ctx.actor):
        cash, _f = await wealth_of(ctx.conn, uid)
        if cash > sc.CASH_FLOOR_DEFAULT:
            pool.append(uid)
    if not pool:
        return Result(
            title="🥷 STICKY FINGERS",
            public=(f"{ctx.who(ctx.actor)} went looking for a pocket to pick "
                    "and found an economy of paupers."),
            colour=_EMBED_GREY,
        )
    victim = random.choice(pool)
    cash, _f = await wealth_of(ctx.conn, victim)
    amount = min(cash // 10, 2_500, cash - sc.CASH_FLOOR_DEFAULT)
    taken = await take_cash(ctx.conn, victim, amount,
                            floor=sc.CASH_FLOOR_DEFAULT,
                            reason="special_theft", detail="Sticky Fingers")
    await give_cash(ctx.conn, ctx.actor, taken, reason="special_gain",
                    detail="Sticky Fingers")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, taken, domain="cash",
                                       detail="Sticky Fingers")
    return Result(
        title="🥷 STICKY FINGERS",
        public=(f"{ctx.who(ctx.actor)} accidentally bumped into "
                f"{ctx.who(victim)}.\n"
                f"Shortly afterwards, {ctx.who(victim)} noticed "
                f"**{money(taken)}** was missing.\n"
                f"💰 {ctx.who(ctx.actor)} **+{money(taken)}**" + extra),
        colour=_EMBED_RED,
    )


@resolver("special_lucky_lottery")
async def _lottery(ctx: Ctx) -> Result:
    pool = await actives(ctx.conn, sc.ACTIVE_SHORT_HOURS)
    if ctx.actor not in pool:
        pool.append(ctx.actor)
    winner = random.choice(pool)
    await give_cash(ctx.conn, winner, 10_000, reason="special_gain",
                    detail="Lucky Lottery")
    return Result(
        title="🎰 LUCKY LOTTERY",
        public=("Nigeria has selected a winner using the internationally "
                "recognised method of pressing RANDOM.\n\n"
                f"🏆 {ctx.who(winner)}: **+{money(10_000)}**\n\n"
                f"{ctx.who(ctx.actor)} paid 500 Naira to make this somebody's "
                "problem."),
        colour=_EMBED_GREEN,
    )


@resolver("special_anonymous_benefactor")
async def _benefactor(ctx: Ctx) -> Result:
    pool = [u for u in await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS,
                                     exclude=ctx.actor)
            if await total_wealth(ctx.conn, u) < 5_000]
    if not pool:
        return Result(title="❤️ ANONYMOUS BENEFACTOR",
                      public="Nigeria could not locate anybody sufficiently "
                             "poor. The donation has been quietly withdrawn.",
                      colour=_EMBED_GREY)
    recipient = random.choice(pool)
    await give_cash(ctx.conn, recipient, 5_000, reason="special_gain",
                    detail="Anonymous Benefactor")
    return Result(
        title="❤️ ANONYMOUS BENEFACTOR",
        public=("A recently-active Prince in financial distress has received "
                f"an unexpected donation.\n\n💰 {ctx.who(recipient)}: "
                f"**+{money(5_000)}**\n\n"
                "The donor has chosen to remain anonymous.\n"
                "They are definitely watching this message."),
        colour=_EMBED_GREEN,
    )


@resolver("special_nigerian_stimulus_package")
async def _stimulus(ctx: Ctx) -> Result:
    paid, total = 0, 0
    for uid in await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS):
        wealth = await total_wealth(ctx.conn, uid)
        if wealth >= 5_000:
            continue
        grant = min(wealth, 2_500)
        if grant <= 0:
            continue
        await give_cash(ctx.conn, uid, grant, reason="special_gain",
                        detail="Stimulus Package")
        paid += 1
        total += grant
    return Result(
        title="🇳🇬 NIGERIAN STIMULUS PACKAGE",
        public=("Government economists have concluded that poor Princes would "
                "benefit from having more money.\n"
                "A revolutionary policy has therefore been enacted:\n"
                "**We gave them money.**\n\n"
                f"Recipients: **{paid}**\n💰 Total stimulus: **{money(total)}**"),
        colour=_EMBED_GREEN,
    )


@resolver("special_reverse_robin_hood")
async def _reverse_robin(ctx: Ctx) -> Result:
    pool, taken_total = [], 0
    for uid in await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS):
        if await total_wealth(ctx.conn, uid) < 5_000:
            pool.append(uid)
    for uid in pool:
        taken_total += await take_cash(
            ctx.conn, uid, 100, floor=sc.WEALTH_FLOOR_DEFAULT,
            reason="special_loss", detail="Reverse Robin Hood")
    top = await richest(ctx.conn, sc.ACTIVE_WINDOW_HOURS, limit=1)
    if not top:
        return Result(title="🤑 REVERSE ROBIN HOOD",
                      public="Nobody was rich enough to receive the proceeds.",
                      colour=_EMBED_GREY)
    rich_id = top[0][0]
    await give_cash(ctx.conn, rich_id, taken_total, reason="special_gain",
                    detail="Reverse Robin Hood")
    return Result(
        title="🤑 REVERSE ROBIN HOOD",
        public=("Nigeria has successfully implemented trickle-up economics.\n\n"
                f"**{len(pool)}** poor Princes contributed **{money(taken_total)}**.\n"
                f"👑 {ctx.who(rich_id)} receives the entire amount.\n\n"
                "Inequality has been restored."),
        colour=_EMBED_RED,
    )


@resolver("special_robin_hood_returns")
async def _robin_hood(ctx: Ctx) -> Result:
    rich, poor = [], []
    for uid in await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS):
        cash, _f = await wealth_of(ctx.conn, uid)
        if cash > 7_500:
            rich.append(uid)
        if await total_wealth(ctx.conn, uid) < 5_000:
            poor.append(uid)
    if not rich or not poor:
        return Result(title="🏹 ROBIN HOOD RETURNS",
                      public="Robin Hood found either nobody to rob or nobody "
                             "to give to, and went home.",
                      colour=_EMBED_GREY)
    pool = 0
    for uid in rich:
        pool += await take_cash(ctx.conn, uid, 500,
                                floor=sc.WEALTH_FLOOR_DEFAULT,
                                reason="special_loss", detail="Robin Hood")
    share, left = divmod(pool, len(poor))
    for i, uid in enumerate(poor):
        # The remainder goes to the earliest recipients rather than being
        # dropped, so the pool is redistributed to the exact Naira.
        await give_cash(ctx.conn, uid, share + (1 if i < left else 0),
                        reason="special_gain", detail="Robin Hood")
    return Result(
        title="🏹 ROBIN HOOD RETURNS",
        public=("A temporary outbreak of redistribution has occurred.\n\n"
                f"💸 **{len(rich)}** cash-heavy Princes contributed 500 each.\n"
                f"💰 **{len(poor)}** poorer Princes split **{money(pool)}**.\n\n"
                "Robin Hood has left before the tax authorities arrive."),
        colour=_EMBED_GOLD,
    )


@resolver("special_the_great_cash_reset")
async def _cash_reset(ctx: Ctx) -> Result:
    async with ctx.conn.execute(
        "SELECT discord_user_id, balance FROM scam_players WHERE balance > 10000"
    ) as cur:
        rows = [(str(r[0]), int(r[1])) async for r in cur]
    destroyed = 0
    for uid, cash in rows:
        cut = (cash - 10_000) // 5          # 20% of everything above 10.000
        if cut > 0:
            await adjust_balance(ctx.conn, uid, -cut, "special_loss",
                                 "The Great Cash Reset")
            destroyed += cut
    return Result(
        title="💥 THE GREAT CASH RESET",
        public=("Government economists have discovered that several Princes "
                "possess far too much Cash.\n\n"
                "🔥 **20% of all Cash above 10.000 has been removed from "
                "circulation.**\n\n"
                f"Total Naira destroyed: **{money(destroyed)}**\n"
                f"{ctx.who(ctx.actor)} receives: **0 Naira**\n\n"
                "Inflation has officially been defeated."),
        colour=_EMBED_RED,
    )


@resolver("special_wrong_account")
async def _wrong_account(ctx: Ctx) -> Result:
    pool = await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS, exclude=ctx.actor)
    balances = {}
    for uid in pool:
        cash, _f = await wealth_of(ctx.conn, uid)
        balances[uid] = cash
    if len(pool) < 2:
        return Result(title="🔄 WRONG ACCOUNT",
                      public="The bank could not find two accounts to confuse.",
                      colour=_EMBED_GREY)
    # A swap between two near-identical balances is not a joke, it is a no-op.
    pairs = [(a, b) for i, a in enumerate(pool) for b in pool[i + 1:]
             if abs(balances[a] - balances[b]) >= 2_000]
    a, b = random.choice(pairs) if pairs else random.sample(pool, 2)
    before_a, before_b = balances[a], balances[b]
    await ctx.conn.execute(
        "UPDATE scam_players SET balance = ? WHERE discord_user_id = ?",
        (before_b, a))
    await ctx.conn.execute(
        "UPDATE scam_players SET balance = ? WHERE discord_user_id = ?",
        (before_a, b))
    await record_ledger(ctx.conn, a, before_b - before_a, "special_swap",
                        "Wrong Account")
    await record_ledger(ctx.conn, b, before_a - before_b, "special_swap",
                        "Wrong Account")
    return Result(
        title="🔄 WRONG ACCOUNT",
        public=("A routine banking error has occurred.\n\n"
                f"{ctx.who(a)}: {money(before_a)} → **{money(before_b)}**\n"
                f"{ctx.who(b)}: {money(before_b)} → **{money(before_a)}**\n\n"
                "The bank considers the matter resolved."),
        colour=_EMBED_GOLD,
    )


@resolver("special_economic_russian_roulette")
async def _roulette(ctx: Ctx) -> Result:
    pool = await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS)
    if ctx.actor in pool:
        pool.remove(ctx.actor)
    cohort = ([ctx.actor] + pool)[:6]
    if len(cohort) < 6:
        cohort = cohort + [u for u in pool if u not in cohort][:6 - len(cohort)]
    loser = random.choice(cohort)
    cash, _f = await wealth_of(ctx.conn, loser)
    loss = await take_cash(ctx.conn, loser, min(3_000, cash - sc.CASH_FLOOR_DEFAULT),
                           floor=sc.CASH_FLOOR_DEFAULT,
                           reason="special_loss", detail="Russian Roulette")
    others = [u for u in cohort if u != loser]
    payout = loss // 2
    share, left = divmod(payout, len(others)) if others else (0, 0)
    for i, uid in enumerate(others):
        await give_cash(ctx.conn, uid, share + (1 if i < left else 0),
                        reason="special_gain", detail="Russian Roulette")
    extra = await ctx.cog.extra_losses(ctx.conn, loser, loss, domain="cash",
                                       detail="Russian Roulette")
    return Result(
        title="🔫 ECONOMIC RUSSIAN ROULETTE",
        public=(f"{len(cohort)} recently-active Princes have been selected for "
                "a financial experiment.\n\n"
                f"💀 {ctx.who(loser)} loses **{money(loss)}**.\n"
                f"💰 Each of the other {len(others)} receives **{money(share)}**.\n"
                f"🔥 **{money(loss - payout)}** destroyed.\n\n"
                "Participation was mandatory." + extra),
        colour=_EMBED_RED,
    )


# ── Resolvers: the fund ───────────────────────────────────────────────────────

async def _fund_victims(conn, actor: str, minimum: int) -> list[str]:
    """Three random top-10 investors who have enough to be worth robbing."""
    from nigeria_bot import royal_fund as rf

    pool = [u for u, a in (await rf.positions(conn))[:10]
            if u != str(actor) and a >= minimum]
    random.shuffle(pool)
    return pool[:3]


@resolver("special_ponzi_pitch")
async def _ponzi_pitch(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    victim = ctx.choice
    await rf._add_position(ctx.conn, victim, -1_000)
    await record_ledger(ctx.conn, victim, -1_000, "special_fund_loss", "Ponzi Pitch")
    await give_cash(ctx.conn, ctx.actor, 1_000, reason="special_gain",
                    detail="Ponzi Pitch")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, 1_000, detail="Ponzi Pitch")
    return Result(
        title="📈 PONZI PITCH",
        public=(f"{ctx.who(ctx.actor)} presented {ctx.who(victim)} with an "
                "exclusive investment opportunity.\n"
                "It had charts. It had projections. It had absolutely no "
                "underlying assets.\n\n"
                f"📉 {ctx.who(victim)} Fund: **−{money(1_000)}**\n"
                f"💰 {ctx.who(ctx.actor)} Cash: **+{money(1_000)}**" + extra),
        colour=_EMBED_RED,
    )


@resolver("special_false_investment_fraud")
async def _false_investment(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    victim = ctx.choice
    await rf._add_position(ctx.conn, victim, -2_000)
    await record_ledger(ctx.conn, victim, -2_000, "special_fund_loss",
                        "False Investment Fraud")
    await give_cash(ctx.conn, ctx.actor, 2_000, reason="special_gain",
                    detail="False Investment Fraud")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, 2_000,
                                       detail="False Investment Fraud")
    return Result(
        title="🏦 FALSE INVESTMENT FRAUD",
        public=(f"{ctx.who(victim)} was offered access to a private Nigerian "
                "investment vehicle.\nThe vehicle exists.\n"
                "Unfortunately, it is currently driving away with their money.\n\n"
                f"📉 {ctx.who(victim)} Fund: **−{money(2_000)}**\n"
                f"💰 {ctx.who(ctx.actor)} Cash: **+{money(2_000)}**" + extra),
        colour=_EMBED_RED,
    )


@resolver("special_hostile_acquisition")
async def _hostile_acquisition(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    victim = ctx.choice
    held = dict(await rf.positions(ctx.conn)).get(victim, 0)
    amount = max(0, min(held // 5, 5_000, held - sc.FUND_FLOOR_ACQUISITION))
    if amount <= 0:
        return Result(title="🏦 HOSTILE ACQUISITION",
                      public=f"{ctx.who(victim)}'s portfolio turned out to be "
                             "too small to bother acquiring.",
                      colour=_EMBED_GREY)
    # A position transfer: the fund total is deliberately untouched (spec §4A).
    await rf._add_position(ctx.conn, victim, -amount)
    await rf._add_position(ctx.conn, ctx.actor, amount)
    await record_ledger(ctx.conn, victim, -amount, "special_fund_loss",
                        "Hostile Acquisition")
    await record_ledger(ctx.conn, ctx.actor, amount, "special_fund_gain",
                        "Hostile Acquisition")
    return Result(
        title="🏦 HOSTILE ACQUISITION",
        public=(f"{ctx.who(ctx.actor)} has acquired a significant minority "
                f"interest in {ctx.who(victim)}'s portfolio.\n"
                "“Acquired” is being used very loosely.\n\n"
                f"📉 {ctx.who(victim)} Fund: **−{money(amount)}**\n"
                f"📈 {ctx.who(ctx.actor)} Fund: **+{money(amount)}**\n\n"
                "_The fund's total is unchanged. Only its owners are._"),
        colour=_EMBED_GOLD,
    )


@resolver("special_seize_the_offshore_accounts")
async def _offshore(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    holders = [(u, a) for u, a in await rf.positions(ctx.conn) if u != ctx.actor]
    if not holders:
        return Result(title="🏝️ SEIZE THE OFFSHORE ACCOUNTS",
                      public="No offshore structures were located.",
                      colour=_EMBED_GREY)
    victim, held = holders[0]
    exposed = max(0, held - sc.FUND_FLOOR_OFFSHORE)
    seized = min(int(exposed * 0.30), 15_000)
    if seized <= 0:
        return Result(title="🏝️ SEIZE THE OFFSHORE ACCOUNTS",
                      public=f"{ctx.who(victim)}'s account was entirely below "
                             "the protected threshold.",
                      colour=_EMBED_GREY)
    share = int(seized * 0.70)
    sink = seized - share
    await rf._add_position(ctx.conn, victim, -seized)
    await rf._add_position(ctx.conn, ctx.actor, share)
    await record_ledger(ctx.conn, victim, -seized, "special_fund_loss", "Offshore seizure")
    await record_ledger(ctx.conn, ctx.actor, share, "special_fund_gain", "Offshore seizure")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, seized, detail="Offshore seizure")
    return Result(
        title="🏝️ SEIZE THE OFFSHORE ACCOUNTS",
        public=("Investigators have located "
                f"{ctx.who(victim)}'s entirely ordinary offshore financial "
                f"structure.\n\nTotal seized: **{money(seized)}**\n"
                f"📈 {ctx.who(ctx.actor)} Fund: **+{money(share)}**\n"
                f"🔥 Destroyed: **{money(sink)}**\n\n"
                "The first 5.000 Naira of the account remains mysteriously "
                "untouchable." + extra),
        colour=_EMBED_RED,
    )


@resolver("special_portfolio_shuffle")
async def _portfolio_shuffle(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    holders = await rf.positions(ctx.conn)
    if len(holders) < 2:
        return Result(title="🔄 PORTFOLIO SHUFFLE",
                      public="Roger found only one row to drag.",
                      colour=_EMBED_GREY)
    amounts = dict(holders)
    ids = [u for u, _a in holders]
    pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
             if abs(amounts[a] - amounts[b]) >= 2_000]
    a, b = random.choice(pairs) if pairs else random.sample(ids, 2)
    await rf._set_position(ctx.conn, a, amounts[b])
    await rf._set_position(ctx.conn, b, amounts[a])
    await record_ledger(ctx.conn, a, amounts[b] - amounts[a], "special_fund_swap",
                        "Portfolio Shuffle")
    await record_ledger(ctx.conn, b, amounts[a] - amounts[b], "special_fund_swap",
                        "Portfolio Shuffle")
    return Result(
        title="🔄 PORTFOLIO SHUFFLE",
        public=("Roger has accidentally dragged two rows in the investment "
                "spreadsheet.\n\n"
                f"{ctx.who(a)} Fund: {money(amounts[a])} → **{money(amounts[b])}**\n"
                f"{ctx.who(b)} Fund: {money(amounts[b])} → **{money(amounts[a])}**\n\n"
                "_Fund total remains unchanged._"),
        colour=_EMBED_GOLD,
    )


@resolver("special_nationalisation")
async def _nationalisation(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    holders = await rf.positions(ctx.conn)
    top5, rest = holders[:5], holders[5:]
    if not top5 or not rest:
        return Result(title="☭ NATIONALISATION",
                      public="There were not enough distinct classes to "
                             "expropriate.",
                      colour=_EMBED_GREY)
    pool = 0
    for uid, held in top5:
        take = max(0, min(600, held - sc.FUND_FLOOR_NATIONALISE))
        if take:
            await rf._add_position(ctx.conn, uid, -take)
            await record_ledger(ctx.conn, uid, -take, "special_fund_loss",
                                "Nationalisation")
            pool += take
    if pool <= 0:
        return Result(title="☭ NATIONALISATION",
                      public="The top investors were already too poor to tax.",
                      colour=_EMBED_GREY)
    share, left = divmod(pool, len(rest))
    for i, (uid, _held) in enumerate(rest):
        got = share + (1 if i < left else 0)
        await rf._add_position(ctx.conn, uid, got)
        await record_ledger(ctx.conn, uid, got, "special_fund_gain", "Nationalisation")
    return Result(
        title="☭ NATIONALISATION",
        public=("For approximately thirty seconds, the means of production "
                "have become negotiable.\n\n"
                "Top fund investors contributed up to 600 Naira each.\n"
                f"💸 Total redistributed: **{money(pool)}**\n"
                f"💰 Split among **{len(rest)}** other investors.\n\n"
                "Roger is checking whether this is legal."),
        colour=_EMBED_GOLD,
    )


@resolver("special_the_return_of_carl_marx")
async def _carl_marx(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    # The cohort was frozen when the *offer* was generated, so nobody can see
    # Marx on their menu and then deposit 1 Naira to join the redistribution.
    cohort = [str(u) for u in ctx.offer.get("cohort", [])]
    if not cohort:
        return Result(title="☭ THE RETURN OF CARL MARX",
                      public="The proletariat could not be located.",
                      colour=_EMBED_GREY)
    held = dict(await rf.positions(ctx.conn))
    total = sum(held.get(u, 0) for u in cohort)
    share, left = divmod(total, len(cohort))
    for i, uid in enumerate(cohort):
        target = share + (1 if i < left else 0)
        before = held.get(uid, 0)
        await rf._set_position(ctx.conn, uid, target)
        if target != before:
            await record_ledger(ctx.conn, uid, target - before,
                                "special_fund_swap", "Carl Marx")
    return Result(
        title="☭ THE RETURN OF CARL MARX",
        public=("Roger has briefly lost control of the means of production.\n\n"
                "The eligible fund investor cohort has been equalised.\n"
                f"Participants: **{len(cohort)}**\n"
                f"Equal position: approximately **{money(share)}** each\n\n"
                "A player who deposited 1 Naira before this offer appeared is "
                "now extremely interested in political theory.\n"
                "Roger is typing."),
        colour=_EMBED_PURPLE,
    )


@resolver("special_royal_bank_robbery")
async def _bank_robbery(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    total = await rf.fund_total(ctx.conn)
    if random.random() >= 0.60:
        return Result(
            title="🚪 WRONG VAULT",
            public=(f"{ctx.who(ctx.actor)} tried to rob the Royal Investment "
                    "Fund itself.\n\nThe robbery failed. The fund is intact.\n"
                    f"{ctx.who(ctx.actor)}'s {money(4_000)} activation cost is not."),
            colour=_EMBED_GREY,
        )
    # Proportional so every investor pays their share of exactly 5.000.
    factor = max(0.0, (total - 5_000) / total) if total else 0.0
    await rf.scale_fund(ctx.conn, factor)
    await give_cash(ctx.conn, ctx.actor, 5_000, reason="special_gain",
                    detail="Royal Bank Robbery")
    return Result(
        title="💰 THE VAULT IS OPEN",
        public=(f"**{money(5_000)}** has disappeared from the Royal Investment "
                f"Fund.\nIt has reappeared in {ctx.who(ctx.actor)}'s cash "
                "balance.\n\nEvery investor checks their position "
                "simultaneously."),
        colour=_EMBED_RED,
    )


@resolver("special_roger_has_been_reassured")
async def _roger_calm(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    old, new, _p = await rf.move_risk(ctx.conn, -1, reason="Roger Has Been Reassured")
    return Result(
        title="📈 ROGER HAS BEEN REASSURED",
        public=(f"{ctx.who(ctx.actor)} has spent {money(750)} explaining the "
                "situation to Roger very slowly.\n\n"
                f"**Fund Risk: {old} → {new}**\n\n"
                "Roger is calm again. This is probably temporary."),
        colour=_EMBED_GREEN,
    )


@resolver("special_roger_is_nervous")
async def _roger_nervous(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    old, new, _p = await rf.move_risk(ctx.conn, 1, reason="Roger Is Nervous")
    return Result(
        title="📉 ROGER IS NERVOUS",
        public=(f"{ctx.who(ctx.actor)} has shown Roger several deeply "
                "concerning charts.\n\n"
                f"**Fund Risk: {old} → {new}**\n\nRoger is typing."),
        colour=_EMBED_RED,
    )


@resolver("special_warren_buffett_consultancy_call")
async def _buffett(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    state = await rf.get_state(ctx.conn)
    before = int(state["risk"])
    await rf.set_state(ctx.conn, risk=1, collapse_pressure=0)
    return Result(
        title="📞 WARREN BUFFETT CONSULTANCY CALL",
        public=(f"{ctx.who(ctx.actor)} has retained outside financial "
                "expertise.\nThe consultant reviewed the Royal Investment Fund "
                "for seventeen seconds.\n\nHis recommendation:\n"
                "“**Stop doing that.**”\n\n"
                f"Fund Risk: **{before} → 1**\n"
                "Collapse Pressure: **CLEARED**\n"
                "Fund value restored: **0 Naira**"),
        colour=_EMBED_GREEN,
    )


@resolver("special_nuclear_bomb")
async def _nuke(ctx: Ctx) -> Result:
    from nigeria_bot import royal_fund as rf

    # Reuses the canonical collapse rather than zeroing rows by hand, so the
    # fund's own invariants, risk reset and pressure clearing all still hold.
    destroyed, investors = await rf.do_collapse(ctx.conn)
    return Result(
        title="☢️ THE NUCLEAR OPTION",
        public=(f"{ctx.who(ctx.actor)} pressed the button.\n"
                "This was not a metaphor.\n\n"
                "💥 **THE ROYAL INVESTMENT FUND HAS COLLAPSED**\n\n"
                f"Fund value destroyed: **{money(destroyed)}**\n"
                f"Investors wiped out: **{investors}**\n"
                "Risk level: **1**\n"
                f"{ctx.who(ctx.actor)} receives: **0**\n\n"
                "Roger has stopped typing."),
        colour=_EMBED_RED,
    )


@resolver("special_the_big_short")
async def _big_short(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "big_short", owner_id=ctx.actor,
                     subject_id=ctx.actor)
    return Result(
        title="📉 THE BIG SHORT",
        public=(f"{ctx.who(ctx.actor)} has placed {money(3_000)} on Roger "
                "making a terrible financial decision.\n\n"
                "The next **natural** Royal Investment Fund event will settle "
                "the bet. Anything a Special does to the fund does not count."),
        colour=_EMBED_GREY,
    )


@resolver("special_asset_freeze")
async def _asset_freeze(ctx: Ctx) -> Result:
    victim = ctx.choice
    await add_effect(ctx.conn, "asset_freeze", owner_id=ctx.actor,
                     subject_id=victim, hours=2)
    return Result(
        title="🔒 ASSET FREEZE",
        public=(f"{ctx.who(ctx.actor)} has frozen {ctx.who(victim)}'s Royal "
                "Investment Fund access for **2 hours**.\n\n"
                "Deposits: **BLOCKED**\nWithdrawals: **BLOCKED**\n\n"
                "Existing investment remains exposed to normal fund movement. "
                "Roger does not accept excuses."),
        colour=_EMBED_RED,
    )


# ── Resolvers: traps and personal effects ─────────────────────────────────────

def _trap_result(title: str, body: str) -> Result:
    return Result(title=title, private=body)


@resolver("special_tax_audit")
async def _tax_audit(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "tax_audit", ctx.actor, hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "🧾 TAX AUDIT ARMED",
        "The next **other** fund withdrawal above 1.000 Naira within 6 hours "
        "will be audited.\n\n20% of it, up to 2.000, is confiscated and "
        "destroyed. You are named publicly when it fires.",
    )


@resolver("special_counterfeit_naira")
async def _counterfeit(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "counterfeit_naira", ctx.actor,
                   hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "💵 COUNTERFEIT NAIRA TRAP ARMED",
        "The next **other** `/scam` or real target reward above 1.000 Naira "
        "within 6 hours will be inspected, and exactly 500 of it will turn out "
        "to be yours.",
    )


@resolver("special_highwayman")
async def _highwayman(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "highwayman", ctx.actor, hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "🏴‍☠️ HIGHWAYMAN ARMED",
        "For 6 hours you will intercept **50%** of the next other successful "
        "`/scam` or real target reward, up to 2.500 Naira.",
    )


@resolver("special_police_informant")
async def _informant(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "police_informant", ctx.actor,
                   hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "🚔 POLICE INFORMANT ARMED",
        "For 6 hours, the next other player to fail against a real mark will "
        "be arrested.\n\nThe police will name you. They always do.",
    )


@resolver("special_welfare_fraud")
async def _welfare_fraud(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "welfare_fraud", ctx.actor, hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "🚨 WELFARE FRAUD TRAP ARMED",
        "For 6 hours, the next player worth more than 5.000 Naira who uses "
        "`/beg` will be investigated and fined up to 2.000 — paid to you.\n\n"
        "Genuinely poor beggars do not trip it.",
    )


@resolver("special_trickle_up_economics")
async def _trickle_up(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "trickle_up", ctx.actor, hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "💸 TRICKLE-UP ECONOMICS ARMED",
        "For 6 hours, every donation in the next `/beg` session is redirected "
        "to you. There is no cap.\n\nThe routing stays secret until the session "
        "ends, so nobody is warned halfway through.",
    )


@resolver("special_beggar_king")
async def _beggar_king(ctx: Ctx) -> Result:
    await arm_trap(ctx.conn, "beggar_king", ctx.actor, hours=sc.HIDDEN_TRAP_HOURS)
    return _trap_result(
        "👑 BEGGAR KING ARMED",
        "For 6 hours, the next `/beg` session has its first **3** donation "
        "attempts reversed: the beggar pays the donor instead, up to 1.000 "
        "each.",
    )


@resolver("special_counterfeit_detector")
async def _detector(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "counterfeit_detector", owner_id=ctx.actor,
                     subject_id=ctx.actor, hours=sc.PERSONAL_BUFF_HOURS)
    return _trap_result(
        "🛡️ COUNTERFEIT DETECTOR",
        "Protection armed for **12 hours**. Your next fake-target encounter is "
        "detected safely.\n\nReal marks do not consume it. The normal operating "
        "cost still applies.",
    )


@resolver("special_nigerian_insurance_policy")
async def _insurance(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "insurance", owner_id=ctx.actor,
                     subject_id=ctx.actor, hours=1)
    return _trap_result(
        "🛡️ NIGERIAN INSURANCE POLICY",
        "Coverage active for **1 hour**, with no total cap.\n\n"
        "Qualifying involuntary losses to the system are reimbursed. "
        "Voluntary spending, stakes, deposits and anything another player "
        "takes from you are all excluded — read the small print.",
    )


@resolver("special_professional_guarantee")
async def _guarantee(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "professional_guarantee", owner_id=ctx.actor,
                     subject_id=ctx.actor, hours=sc.PERSONAL_BUFF_HOURS)
    return _trap_result(
        "🎯 PROFESSIONAL GUARANTEE",
        "For **12 hours**, your next attempt on a real, non-legendary mark "
        "simply succeeds.\n\nFake targets do not consume it.",
    )


@resolver("special_get_out_of_jail_free")
async def _jail_card(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "jail_card", owner_id=ctx.actor,
                     subject_id=ctx.actor)
    return _trap_result(
        "🎫 GET OUT OF JAIL FREE",
        "Stored until your next arrest, which it cancels on the spot — "
        "including a forced one.\n\nIt does not expire and it does not stack.",
    )


@resolver("special_nigerian_scamming_crash_course")
async def _crash_course(ctx: Ctx) -> Result:
    await ctx.conn.execute(
        "UPDATE scam_players SET fake_target_until = NULL"
        " WHERE discord_user_id = ?", (ctx.actor,),
    )
    await add_effect(ctx.conn, "crash_course", owner_id=ctx.actor,
                     subject_id=ctx.actor)
    return _trap_result(
        "🎓 NIGERIAN SCAMMING CRASH COURSE",
        "Congratulations. You have completed twelve minutes of advanced "
        "professional training.\n\n"
        "✅ Fake-target cooldown reset\n"
        "✅ Your next fake-target theft is **doubled**\n\n"
        "The bonus is tied to that next disguise and does not stack.",
    )


@resolver("special_prince_for_a_day")
async def _prince(ctx: Ctx) -> Result:
    await give_cash(ctx.conn, ctx.actor, 5_000, reason="special_gain",
                    detail="Prince for a Day")
    await add_effect(ctx.conn, "prince_for_a_day", owner_id=ctx.actor,
                     subject_id=ctx.actor, hours=3)
    return Result(
        title="👑 PRINCE FOR A DAY",
        public=(f"{ctx.who(ctx.actor)} has accepted the Crown.\n\n"
                f"💰 Royal allowance: **+{money(5_000)}**\n\n"
                "For the next **3 hours**, every successful theft from "
                f"{ctx.who(ctx.actor)} destroys a second, equal amount on top. "
                "The thief does not get that part — nobody does.\n"
                "Extra-loss floor: 2.500 total wealth.\n\n"
                "🔪 Please form an orderly queue."),
        colour=_EMBED_GOLD,
    )


@resolver("special_personal_grudge")
async def _grudge(ctx: Ctx) -> Result:
    victim = ctx.choice
    await add_effect(ctx.conn, "personal_grudge", owner_id=ctx.actor,
                     subject_id=victim, hours=2)
    return Result(
        title="😡 PERSONAL GRUDGE",
        public=(f"{ctx.who(ctx.actor)} has decided that {ctx.who(victim)} has "
                "had things far too easy.\n\n"
                "For **2 hours**, every qualifying involuntary loss "
                f"{ctx.who(victim)} suffers comes with an equal extra loss. "
                "The extra amount is destroyed.\n"
                "Protection stops at 5.000 total wealth.\n\n"
                "This is apparently personal."),
        colour=_EMBED_RED,
    )


@resolver("special_burn_notice")
async def _burn_notice(ctx: Ctx) -> Result:
    victim = ctx.choice
    await add_effect(ctx.conn, "burn_notice", owner_id=ctx.actor,
                     subject_id=victim, hours=sc.HIDDEN_TRAP_HOURS, charges=3)
    return Result(
        title="🧨 BURN NOTICE",
        public=(f"{ctx.who(ctx.actor)} has placed {ctx.who(victim)}'s finances "
                "under special supervision.\n\n"
                "For the next **6 hours**, their next **3** qualifying "
                "earnings lose **50%** to destruction.\n\n"
                "Nobody collects it. That is the point."),
        colour=_EMBED_RED,
    )


@resolver("special_fog_of_war")
async def _fog(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "fog_of_war", owner_id=ctx.actor, minutes=15)
    return Result(
        title="🌫️ FOG OF WAR",
        public=(f"{ctx.who(ctx.actor)} has made the target board considerably "
                "less informative.\n\n"
                "For **15 minutes**, every displayed chance now reads:\n"
                "# ???\n\n"
                "_The actual odds have not changed. Only your ability to read "
                "them._"),
        colour=_EMBED_GREY,
    )


# ── Resolvers: arrests ────────────────────────────────────────────────────────

@resolver("special_snitch")
async def _snitch(ctx: Ctx) -> Result:
    victim = ctx.choice
    line = await jail_player(ctx.conn, victim)
    return Result(
        title="🐀 SNITCH",
        public=(f"{line}\n\nAuthorities have confirmed the anonymous source "
                f"was {ctx.who(ctx.actor)}.\n"
                "It was anonymous for approximately four seconds."),
        colour=_EMBED_RED,
    )


@resolver("special_mass_arrest")
async def _mass_arrest(ctx: Ctx) -> Result:
    pool = await free_actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS, exclude=ctx.actor)
    victims = random.sample(pool, min(3, len(pool)))
    lines = [await jail_player(ctx.conn, uid) for uid in victims]
    return Result(
        title="🚔 MASS ARREST",
        public=("Nigeria has completed a highly targeted operation against "
                "three randomly selected people.\n\n" + "\n".join(lines) +
                f"\n\n{ctx.who(ctx.actor)} funded the operation."),
        colour=_EMBED_RED,
    )


@resolver("special_panama_papers")
async def _panama(ctx: Ctx) -> Result:
    named = []
    for uid in await actives(ctx.conn, sc.ACTIVE_LONG_HOURS):
        cash, _f = await wealth_of(ctx.conn, uid)
        # The activator is deliberately not exempt.
        if cash > 10_000 and await get_jail(ctx.conn, uid) is None:
            named.append(uid)
    if not named:
        return Result(
            title="🧳 THE PANAMA PAPERS",
            public=("A confidential collection of Nigerian financial documents "
                    "has leaked.\n\nIt reveals that nobody currently has any "
                    "money worth hiding.\n\nRoger is relieved."),
            colour=_EMBED_GREY,
        )
    lines = [await jail_player(ctx.conn, uid) for uid in named]
    return Result(
        title="🧳 THE PANAMA PAPERS",
        public=("A confidential collection of Nigerian financial documents has "
                "leaked.\n\nThe following recently-active Princes were "
                "discovered holding more than 10.000 Cash:\n\n"
                + "\n".join(lines) +
                "\n\n🚔 All named individuals are being held pending an "
                "extremely serious financial investigation.\n"
                "Roger denies knowing why his name appears seventeen times."),
        colour=_EMBED_RED,
    )


# ── Resolvers: wealth PvP ─────────────────────────────────────────────────────

@resolver("special_cash_predator")
async def _cash_predator(ctx: Ctx) -> Result:
    victim = ctx.choice
    if random.random() >= 0.65:
        return Result(
            title="🏃 CASH PREDATOR FAILED",
            public=(f"{ctx.who(victim)} noticed the robbery attempt and left "
                    f"at speed.\n\n{ctx.who(ctx.actor)} loses only the "
                    f"{money(1_250)} activation cost."),
            colour=_EMBED_GREY,
        )
    cash, _f = await wealth_of(ctx.conn, victim)
    want = min(int(cash * 0.30), 7_500, cash - sc.CASH_FLOOR_PREDATOR)
    taken = await take_cash(ctx.conn, victim, want, floor=sc.CASH_FLOOR_PREDATOR,
                            reason="special_theft", detail="Cash Predator")
    await give_cash(ctx.conn, ctx.actor, taken, reason="special_gain",
                    detail="Cash Predator")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, taken, domain="cash",
                                       detail="Cash Predator")
    return Result(
        title="💰 CASH PREDATOR SUCCESS",
        public=(f"{ctx.who(ctx.actor)} steals **{money(taken)}** from "
                f"{ctx.who(victim)}.\n\n"
                "Crime continues to outperform savings accounts." + extra),
        colour=_EMBED_RED,
    )


@resolver("special_eat_the_rich_cash")
async def _eat_the_rich(ctx: Ctx) -> Result:
    top = await richest(ctx.conn, sc.ACTIVE_LONG_HOURS, exclude=ctx.actor, limit=1)
    if not top:
        return Result(title="💰 EAT THE RICH",
                      public="Nigeria could not identify anybody worth eating.",
                      colour=_EMBED_GREY)
    victim, cash = top[0]
    exposure = max(0, cash - sc.CASH_FLOOR_PREDATOR)
    seized = await take_cash(ctx.conn, victim, min(int(exposure * 0.40), 15_000),
                             floor=sc.CASH_FLOOR_PREDATOR,
                             reason="special_theft", detail="Eat the Rich")
    share = int(seized * 0.70)
    await give_cash(ctx.conn, ctx.actor, share, reason="special_gain",
                    detail="Eat the Rich")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, seized, domain="cash",
                                       detail="Eat the Rich")
    return Result(
        title="💰 EAT THE RICH",
        public=(f"Nigeria has identified {ctx.who(victim)} as possessing an "
                "unreasonable amount of liquid confidence.\n\n"
                f"Total seized: **{money(seized)}**\n"
                f"💰 {ctx.who(ctx.actor)}: **+{money(share)}**\n"
                f"🔥 Destroyed: **{money(seized - share)}**\n\n"
                "The first 2.500 Cash remains protected." + extra),
        colour=_EMBED_RED,
    )


@resolver("special_whale_harpoon")
async def _harpoon(ctx: Ctx) -> Result:
    top = await richest(ctx.conn, sc.ACTIVE_LONG_HOURS, exclude=ctx.actor,
                        limit=1, by="wealth")
    if not top:
        return Result(title="🐋 WHALE HARPOON",
                      public="The sea was empty.", colour=_EMBED_GREY)
    victim, wealth = top[0]
    if random.random() >= 0.75:
        return Result(
            title="🌊 THE WHALE ESCAPES",
            public=(f"{ctx.who(ctx.actor)} harpooned at {ctx.who(victim)} and "
                    f"missed.\n\n{ctx.who(victim)} keeps everything. "
                    f"{ctx.who(ctx.actor)} loses the {money(4_000)} activation "
                    "cost."),
            colour=_EMBED_GREY,
        )
    exposure = max(0, wealth - sc.WEALTH_FLOOR_WHALE)
    taken = await take_wealth(ctx.conn, victim, min(int(exposure * 0.25), 10_000),
                              floor=sc.WEALTH_FLOOR_WHALE,
                              reason="special_theft", detail="Whale Harpoon")
    await give_cash(ctx.conn, ctx.actor, taken, reason="special_gain",
                    detail="Whale Harpoon")
    extra = await ctx.cog.extra_losses(ctx.conn, victim, taken,
                                       detail="Whale Harpoon")
    return Result(
        title="🐋 DIRECT HIT",
        public=(f"{ctx.who(ctx.actor)} located the wealthiest available Prince "
                f"and did not hesitate.\n\n"
                f"💸 {ctx.who(victim)}: **−{money(taken)}** total wealth\n"
                f"💰 {ctx.who(ctx.actor)}: **+{money(taken)}** cash\n\n"
                "_Cash first, then whatever had to be liquidated._" + extra),
        colour=_EMBED_RED,
    )


# ── Resolvers: the target board ───────────────────────────────────────────────

@resolver("special_suspicious_activity_report")
async def _sar(ctx: Ctx) -> Result:
    from nigeria_bot import scam_targets as st

    lines = []
    for t in await st.active_targets(ctx.conn):
        verdict = "🎭 **FAKE**" if t["is_fake"] else "✅ Genuine"
        lines.append(f"{t['emoji']} **{t['name']}** — {verdict}")
    if not lines:
        lines = ["_The board is empty._"]
    return Result(
        title="🕵️ SUSPICIOUS ACTIVITY REPORT",
        private=("Current target board assessment:\n\n" + "\n".join(lines) +
                 "\n\n_This is a snapshot only. Marks that appear later are "
                 "not covered, and this report never names the player behind a "
                 "disguise._"),
    )


@resolver("special_counter_intelligence_sweep")
async def _sweep(ctx: Ctx) -> Result:
    from nigeria_bot import scam_targets as st

    cog = ctx.cog.bot.get_cog("scam_targets")
    fakes = [t for t in await st.active_targets(ctx.conn) if t["is_fake"]]
    for t in fakes:
        # The deposit is destroyed, not seized: this card removes fraud, it
        # does not profit from it.
        await cog._end_fake(str(t["fake_owner_id"]))
        await cog._retire_target(t, "swept")
    return Result(
        title="🧹 COUNTER-INTELLIGENCE SWEEP",
        public=(f"Nigeria has cleared **{len(fakes)}** fraudulent target(s) "
                "from the board.\n"
                "Their cover deposits have been destroyed.\n"
                "**No arrests were made** and no names were taken.\n\n"
                "The remaining scams are officially considered legitimate."),
        colour=_EMBED_GOLD,
    )


@resolver("special_operation_clean_board")
async def _clean_board(ctx: Ctx) -> Result:
    from nigeria_bot import scam_targets as st

    cog = ctx.cog.bot.get_cog("scam_targets")
    fakes = [t for t in await st.active_targets(ctx.conn) if t["is_fake"]]
    if not fakes:
        await give_cash(ctx.conn, ctx.actor, 750, reason="special_gain",
                        detail="Clean Board grant")
        return Result(
            title="🧹 OPERATION CLEAN BOARD",
            public=("Authorities completed an exhaustive investigation.\n"
                    "**No fraudulent targets were discovered.**\n\n"
                    f"Roger is deeply moved by {ctx.who(ctx.actor)}'s "
                    "commitment to public safety.\n"
                    f"🎁 Community Protection Grant: **+{money(750)}**"),
            colour=_EMBED_GREEN,
        )
    deposits = 0
    owners: list[str] = []
    for t in fakes:
        deposits += int(t["cover_deposit"] or 0)
        owner = str(t["fake_owner_id"])
        if owner not in owners:
            owners.append(owner)
        await cog._end_fake(owner)
        await cog._retire_target(t, "raided")
    await give_cash(ctx.conn, ctx.actor, deposits, reason="special_gain",
                    detail="Seized cover deposits")
    arrests = [await jail_player(ctx.conn, o) for o in owners]
    return Result(
        title="🧹 OPERATION CLEAN BOARD",
        public=("Nigerian authorities conducted a coordinated raid on the "
                "target board.\n\n"
                f"🎭 Fake targets removed: **{len(fakes)}**\n"
                f"💰 Seized deposits paid to {ctx.who(ctx.actor)}: "
                f"**{money(deposits)}**\n\n" + "\n".join(arrests) +
                "\n\nThe remaining scams are officially legitimate."),
        colour=_EMBED_RED,
    )


@resolver("special_scamtopian_paradise")
async def _scamtopia(ctx: Ctx) -> Result:
    await ctx.conn.execute(
        "UPDATE scam_players SET fake_target_until = NULL"
        " WHERE fake_target_until IS NOT NULL"
    )
    return Result(
        title="🏝️ SCAMTOPIAN PARADISE",
        public=(f"{ctx.who(ctx.actor)} has temporarily abolished professional "
                "standards in the fraud industry.\n\n"
                "🎭 **ALL FAKE-TARGET COOLDOWNS HAVE BEEN RESET**\n\n"
                "Every Prince may immediately put their latest fraudulent "
                "ideas into practice.\nPlease scam responsibly."),
        colour=_EMBED_GOLD,
    )


# ── Public events ─────────────────────────────────────────────────────────────
# A public event is a row plus a message with buttons.  All the concurrency
# lives in SQL: "first click wins" is a conditional UPDATE, and joining is an
# INSERT that a primary key rejects on the second attempt.  No lock is held
# across a Discord round-trip, so a slow API call can never wedge an event.

EVENT_HANDLERS: dict[str, dict] = {}


def event_handler(kind: str, *, buttons: list[tuple[str, str, str]],
                  on_click=None, on_expire=None):
    """Register a public event kind.

    ``buttons`` is a list of ``(action, label, style)``.  ``on_click`` returns
    a private string for the clicker (or None); ``on_expire`` resolves the
    whole event when the window closes.
    """
    EVENT_HANDLERS[kind] = {
        "buttons": buttons, "on_click": on_click, "on_expire": on_expire,
    }


_STYLES = {
    "green": discord.ButtonStyle.success,
    "red": discord.ButtonStyle.danger,
    "blue": discord.ButtonStyle.primary,
    "grey": discord.ButtonStyle.secondary,
}


class SpecialEventButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"spev:(?P<event_id>[0-9]+):(?P<action>[a-z_]+)",
):
    """A button on a public /special event.

    Dynamic because the event id has to live in the custom_id, and persistent
    because a three-minute window that spans a redeploy must not go dead —
    a dead bait button looks exactly like a bait that nobody fell for.
    """

    def __init__(self, event_id: int, action: str, label: str = "Click",
                 style: str = "blue") -> None:
        self.event_id = event_id
        self.action = action
        super().__init__(
            discord.ui.Button(
                label=label[:80], style=_STYLES.get(style, discord.ButtonStyle.primary),
                custom_id=f"spev:{event_id}:{action}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["event_id"]), match["action"],
                   label=item.label or "Click")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ack(interaction):
            return
        cog = interaction.client.get_cog("special_game")
        if cog is None:
            await _reply(interaction, content="❌ The game is not available "
                         "right now.", ephemeral=True)
            return
        await cog.handle_event_click(interaction, self.event_id, self.action)


def event_view(event_id: int, kind: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action, label, style in EVENT_HANDLERS[kind]["buttons"]:
        view.add_item(SpecialEventButton(event_id, action, label, style))
    return view


async def get_event(conn, event_id: int) -> Optional[dict]:
    async with conn.execute(
        "SELECT id, kind, actor_id, channel_id, message_id, expires_at, status,"
        " claimed_by, payload FROM special_events WHERE id = ?", (event_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    return {"id": int(row[0]), "kind": row[1], "actor": str(row[2]),
            "channel_id": row[3], "message_id": row[4], "expires_at": row[5],
            "status": row[6], "claimed_by": row[7],
            "payload": json.loads(row[8] or "{}")}


async def claim_event(conn, event_id: int, user_id: str) -> bool:
    """Atomic first-click claim.  Exactly one caller can ever get True."""
    cur = await conn.execute(
        "UPDATE special_events SET status = 'claimed', claimed_by = ?"
        " WHERE id = ? AND status = 'open'", (str(user_id), event_id),
    )
    await conn.commit()
    return cur.rowcount == 1


async def entries_of(conn, event_id: int) -> list[tuple[str, int]]:
    async with conn.execute(
        "SELECT user_id, amount FROM special_event_entries"
        " WHERE event_id = ? ORDER BY joined_at", (event_id,),
    ) as cur:
        return [(str(r[0]), int(r[1])) async for r in cur]


async def join_event(conn, event_id: int, user_id: str, amount: int = 0,
                     **payload) -> bool:
    try:
        await conn.execute(
            "INSERT INTO special_event_entries"
            " (event_id, user_id, amount, payload, joined_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_id, str(user_id), amount, json.dumps(payload), _iso(_now())),
        )
        return True
    except Exception:
        return False       # already in: the primary key said so


async def close_event(conn, event_id: int, status: str = "resolved") -> None:
    await conn.execute(
        "UPDATE special_events SET status = ? WHERE id = ?", (status, event_id),
    )


# ── Public event: single-claim bait ───────────────────────────────────────────
# Unknown Caller, Suspicious Tikkie, Phishing Test, Marktplaats, Mystery Box
# and Dropped Wallet all share one shape: a public message, one winner, and a
# roll that decides whether the clicker profits or regrets it.

async def _bait_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    if uid == event["actor"]:
        return "❌ You cannot answer your own call."
    if not await claim_event(conn, event["id"], uid):
        return "❌ Somebody else got there first."
    kind = event["kind"]
    actor = event["actor"]
    cash, _f = await wealth_of(conn, uid)

    if kind == "unknown_caller":
        if random.random() < 0.50:
            await give_cash(conn, uid, 1_000, reason="special_gain",
                            detail="Unknown Caller")
            body = ("📞 **CALL ANSWERED**\n"
                    f"{cog.who(uid)} answered.\n"
                    "A confused Prince apologised for calling the wrong "
                    f"number.\n💰 **+{money(1_000)}**")
        else:
            taken = await take_cash(conn, uid, min(750, cash - sc.CASH_FLOOR_DEFAULT),
                                    floor=sc.CASH_FLOOR_DEFAULT,
                                    reason="special_theft", detail="Unknown Caller")
            await give_cash(conn, actor, taken, reason="special_gain",
                            detail="Unknown Caller")
            body = ("📞 **CALL ANSWERED**\n"
                    f"{cog.who(uid)} answered.\n"
                    "“Good afternoon, this is definitely your bank.”\n"
                    f"It was not.\n💸 **{money(taken)}** transferred to "
                    f"{cog.who(actor)}.")
            body += await cog.extra_losses(conn, uid, taken, domain="cash",
                                           detail="Unknown Caller")

    elif kind == "suspicious_tikkie":
        if random.random() < 0.50:
            await give_cash(conn, uid, 500, reason="special_gain",
                            detail="Suspicious Tikkie")
            body = ("✅ **VERIFICATION COMPLETE**\n"
                    f"Against all expectations, {cog.who(uid)} receives "
                    f"**{money(500)}**.")
        else:
            taken = await take_cash(conn, uid, min(500, cash - sc.CASH_FLOOR_DEFAULT),
                                    floor=sc.CASH_FLOOR_DEFAULT,
                                    reason="special_theft", detail="Suspicious Tikkie")
            await give_cash(conn, actor, taken, reason="special_gain",
                            detail="Suspicious Tikkie")
            body = ("❌ **VERIFICATION COMPLETE**\n"
                    f"{cog.who(uid)} has successfully verified that "
                    f"{cog.who(actor)} now owns **{money(taken)}** of their "
                    "Naira.")
            body += await cog.extra_losses(conn, uid, taken, domain="cash",
                                           detail="Suspicious Tikkie")

    elif kind == "phishing_test":
        taken = await take_cash(conn, uid, min(500, cash - sc.CASH_FLOOR_DEFAULT),
                                floor=sc.CASH_FLOOR_DEFAULT,
                                reason="special_theft", detail="Phishing Test")
        await give_cash(conn, actor, taken, reason="special_gain",
                        detail="Phishing Test")
        body = ("🐟 **PHISHING TEST FAILED**\n"
                f"{cog.who(uid)} clicked the link.\n"
                f"💸 **{money(taken)}** transferred to {cog.who(actor)}.\n"
                "Cybersecurity training will continue.")
        body += await cog.extra_losses(conn, uid, taken, domain="cash",
                                       detail="Phishing Test")

    elif kind == "marktplaats":
        # The buyer really does pay first — that is the whole joke.
        paid = await take_cash(conn, uid, 500, floor=0,
                               reason="special_purchase", detail="Marktplaats")
        if paid < 500:
            await give_cash(conn, uid, paid, reason="special_refund",
                            detail="Marktplaats")
            await close_event(conn, event["id"], "expired")
            return "❌ You cannot afford the 500 Naira asking price."
        if random.random() < 0.60:
            await give_cash(conn, uid, 1_000, reason="special_gain",
                            detail="Marktplaats resale")
            body = ("🚲 **DEAL COMPLETED**\n"
                    "Against all available evidence, the bicycle existed.\n"
                    f"💰 {cog.who(uid)} receives **{money(1_000)}** in resale "
                    "value.")
        else:
            await give_cash(conn, actor, 500, reason="special_gain",
                            detail="Marktplaats")
            body = ("🚲 **MARKTPLAATS MOMENT**\n"
                    f"{cog.who(uid)} paid **{money(500)}**.\n"
                    "The seller has deleted their account.\n"
                    f"💰 {cog.who(actor)} **+{money(500)}**")

    elif kind == "mystery_box":
        gross = _weighted_pick([
            (-2_000, 0.05), (-1_000, 0.10), (-500, 0.15), (500, 0.25),
            (1_000, 0.25), (2_000, 0.15), (4_000, 0.05),
        ])
        if gross > 0:
            half = gross // 2
            await give_cash(conn, uid, gross - half, reason="special_gain",
                            detail="Mystery Box")
            await give_cash(conn, actor, half, reason="special_gain",
                            detail="Mystery Box")
            body = ("📦 **MYSTERY BOX OPENED**\n"
                    f"The box contained **{money(gross)}**.\n"
                    f"🎁 {cog.who(uid)}: **+{money(gross - half)}**\n"
                    f"👑 {cog.who(actor)}: **+{money(half)}**")
        else:
            lost = await take_cash(conn, uid, -gross, floor=sc.CASH_FLOOR_DEFAULT,
                                   reason="special_loss", detail="Mystery Box")
            body = ("📦 **MYSTERY BOX OPENED**\n"
                    "The box contained a financial obligation.\n"
                    f"💸 {cog.who(uid)}: **−{money(lost)}**\n"
                    f"{cog.who(actor)} has no legal connection to the box.")
            body += await cog.extra_losses(conn, uid, lost, domain="cash",
                                           detail="Mystery Box")

    elif kind == "dropped_wallet":
        if random.random() < 0.25:
            line = await jail_player(conn, uid)
            body = ("🚔 **POLICE STING**\n"
                    f"{cog.who(uid)} picked up the wallet.\n"
                    f"Unfortunately, the wallet was evidence.\n{line}")
        else:
            await join_event(conn, event["id"], uid, 500)
            await conn.commit()
            await cog.post(
                f"👛 **WALLET FOUND**\n{cog.who(uid)} found **{money(500)}**.\n"
                "_They have three minutes to decide whether anybody needs to "
                "know._",
                view=event_view(event["id"], "wallet_choice"),
            )
            return None      # resolved by the follow-up choice
    else:
        return None

    await close_event(conn, event["id"])
    await conn.commit()
    await cog.post(body)
    return None


async def _bait_expire(cog, conn, event: dict) -> None:
    quips = {
        "unknown_caller": "📞 The unknown number rang out. Nobody in Nigeria "
                          "answers the phone any more.",
        "suspicious_tikkie": "💳 The payment request expired unclicked. "
                             "Cybersecurity awareness is improving.",
        "phishing_test": "🐟 Nobody clicked the obviously fake login. "
                         "Disappointing for everyone involved.",
        "marktplaats": "🚲 The bicycle listing expired. It was probably not "
                       "even a bicycle.",
        "mystery_box": "📦 Nobody opened the box. It has been returned to "
                       "wherever boxes come from.",
        "dropped_wallet": "👛 The wallet sat on the pavement untouched, which "
                          "nobody expected.",
    }
    await close_event(conn, event["id"], "expired")
    await conn.commit()
    await cog.post(quips.get(event["kind"], "The moment passed."))


for _kind, _label, _style in (
    ("unknown_caller", "📞 PICK UP", "green"),
    ("suspicious_tikkie", "VERIFY ACCOUNT", "green"),
    ("phishing_test", "LOG IN NOW", "green"),
    ("marktplaats", "BUY NOW", "green"),
    ("mystery_box", "OPEN THE BOX", "blue"),
    ("dropped_wallet", "DEFINITELY MINE", "grey"),
):
    event_handler(_kind, buttons=[("claim", _label, _style)],
                  on_click=_bait_click, on_expire=_bait_expire)
del _kind, _label, _style


# ── Public event: the wallet's keep-or-return choice ──────────────────────────

async def _wallet_choice_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    finder = [u for u, _a in await entries_of(conn, event["id"])]
    if not finder or uid != finder[0]:
        return "❌ That is not your wallet to decide about."
    return None      # the action is read by handle_event_click


async def _wallet_settle(cog, conn, event: dict, keep: bool) -> None:
    entries = await entries_of(conn, event["id"])
    if not entries or event["status"] != "open":
        return
    finder = entries[0][0]
    actor = event["actor"]
    await close_event(conn, event["id"])
    if keep:
        await give_cash(conn, finder, 500, reason="special_gain",
                        detail="Dropped Wallet")
        body = f"💰 {cog.who(finder)} keeps all **{money(500)}**."
    else:
        await give_cash(conn, finder, 250, reason="special_gain",
                        detail="Dropped Wallet")
        await give_cash(conn, actor, 250, reason="special_gain",
                        detail="Dropped Wallet")
        body = ("🤝 **A RARE DISPLAY OF HONESTY**\n"
                f"{cog.who(finder)} returned the wallet to {cog.who(actor)}, "
                "who generously allowed them to keep half.\n"
                f"💰 {cog.who(finder)} **+{money(250)}**\n"
                f"💰 {cog.who(actor)} **+{money(250)}**")
    await conn.commit()
    await cog.post(body)


async def _wallet_expire(cog, conn, event: dict) -> None:
    # Spec: an unanswered choice defaults to KEEP.  Silence is an answer.
    await _wallet_settle(cog, conn, event, keep=True)


event_handler(
    "wallet_choice",
    buttons=[("keep", "💰 KEEP IT", "green"), ("give", "🤝 RETURN IT", "grey")],
    on_click=_wallet_choice_click, on_expire=_wallet_expire,
)


# ── Public event: pooled entry (tip jar, olympics, ponzi party) ───────────────

POOLED = {
    # kind: (stake, min_entrants, max_entrants, winner_share, actor_share)
    "tip_jar":     (200, 2, 25, 0.80, 0.20),
    "olympics":    (0,   2, 10, 0.90, 0.00),
    "ponzi_party": (500, 3,  6, 0.60, 0.20),
}


async def _pooled_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    kind = event["kind"]
    stake, _lo, hi, _w, _a = POOLED[kind]
    stake = int(event["payload"].get("stake", stake))
    if uid == event["actor"] and not event["payload"].get("actor_may_join", True):
        return "❌ You are already running this one."
    entries = await entries_of(conn, event["id"])
    if any(u == uid for u, _a in entries):
        return "❌ You are already in."
    if len(entries) >= hi:
        return f"❌ All {hi} places are taken."
    cash, _f = await wealth_of(conn, uid)
    if cash < stake:
        return (f"❌ That costs {money(stake)} and you have {money(cash)}.")
    await adjust_balance(conn, uid, -stake, "special_stake", kind)
    if not await join_event(conn, event["id"], uid, stake):
        await adjust_balance(conn, uid, stake, "special_refund", kind)
        return "❌ You are already in."
    await conn.commit()
    await cog.post(f"➕ {cog.who(uid)} is in for **{money(stake)}**.")
    return None


async def _pooled_expire(cog, conn, event: dict) -> None:
    kind = event["kind"]
    _s, lo, _hi, winner_share, actor_share = POOLED[kind]
    entries = await entries_of(conn, event["id"])
    await close_event(conn, event["id"])
    pot = sum(a for _u, a in entries)

    if len(entries) < lo:
        for uid, amount in entries:
            await give_cash(conn, uid, amount, reason="special_refund", detail=kind)
        await conn.commit()
        await cog.post(
            f"🫙 **NOT ENOUGH INTEREST** — only {len(entries)} joined.\n"
            "Every stake has been refunded. The setup cost has not."
            if kind != "tip_jar" else
            "🫙 **TIP JAR CLOSED**\nNobody managed to create a sufficiently "
            "competitive tipping market.\nAll contributions have been refunded."
        )
        return

    winner = random.choice([u for u, _a in entries])
    win = int(pot * winner_share)
    owner = int(pot * actor_share)
    sink = pot - win - owner
    await give_cash(conn, winner, win, reason="special_gain", detail=kind)
    if owner:
        await give_cash(conn, event["actor"], owner, reason="special_gain",
                        detail=kind)
    await conn.commit()

    titles = {
        "tip_jar": "🫙 **TIP JAR CLOSED**",
        "olympics": "🏅 **SCAM OLYMPICS CHAMPION**",
        "ponzi_party": "🧨 **THE PONZI HAS MATURED**",
    }
    lines = [titles[kind],
             f"Entrants: **{len(entries)}** · Pot: **{money(pot)}**",
             f"🏆 {cog.who(winner)}: **+{money(win)}**"]
    if owner:
        lines.append(f"👑 {cog.who(event['actor'])}: **+{money(owner)}**")
    if sink:
        lines.append(f"🔥 Destroyed: **{money(sink)}**")
    await cog.post("\n".join(lines))


event_handler("tip_jar", buttons=[("join", "🫙 TIP 200", "green")],
              on_click=_pooled_click, on_expire=_pooled_expire)
event_handler("olympics", buttons=[("join", "🏅 ENTER", "green")],
              on_click=_pooled_click, on_expire=_pooled_expire)
event_handler("ponzi_party", buttons=[("join", "🧨 INVEST 500", "green")],
              on_click=_pooled_click, on_expire=_pooled_expire)


# ── Public event: mass phishing ───────────────────────────────────────────────

async def _phishing_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    targets = event["payload"].get("targets", [])
    if uid not in targets:
        return "❌ This campaign is not aimed at you. Count yourself lucky."
    if not await join_event(conn, event["id"], uid, 0):
        return "❌ You have already verified your account. Once was enough."
    cash, _f = await wealth_of(conn, uid)
    taken = await take_cash(conn, uid, min(500, cash - sc.CASH_FLOOR_DEFAULT),
                            floor=sc.CASH_FLOOR_DEFAULT,
                            reason="special_theft", detail="Mass Phishing")
    await give_cash(conn, event["actor"], taken, reason="special_gain",
                    detail="Mass Phishing")
    extra = await cog.extra_losses(conn, uid, taken, domain="cash",
                                   detail="Mass Phishing")
    await conn.commit()
    await cog.post(
        f"🐟 {cog.who(uid)} verified their account.\n"
        f"Unfortunately, they verified it with {cog.who(event['actor'])}.\n"
        f"💸 **{money(taken)}** transferred." + extra
    )
    return None


async def _phishing_expire(cog, conn, event: dict) -> None:
    entries = await entries_of(conn, event["id"])
    await close_event(conn, event["id"])
    await conn.commit()
    await cog.post(
        "📲 **PHISHING CAMPAIGN ENDED**\n"
        f"Victims: **{len(entries)}** of "
        f"{len(event['payload'].get('targets', []))} targeted."
        + ("\nNot one person clicked. Nigeria is learning." if not entries else "")
    )


event_handler("mass_phishing", buttons=[("verify", "VERIFY ACCOUNT", "green")],
              on_click=_phishing_click, on_expire=_phishing_expire)


# ── Public event: duels ───────────────────────────────────────────────────────

async def _duel_pay(cog, conn, winner: str, loser: str, *, wager: int = 0,
                    steal: bool = False) -> str:
    if steal:
        wealth = await total_wealth(conn, loser)
        want = min(int(wealth * 0.25), 5_000)
        taken = await take_wealth(conn, loser, want,
                                  floor=sc.WEALTH_FLOOR_DEFAULT,
                                  reason="special_duel", detail="Scam Duel")
        await give_cash(conn, winner, taken, reason="special_gain",
                        detail="Scam Duel")
        extra = await cog.extra_losses(conn, loser, taken, detail="Scam Duel")
        return (f"💸 **{money(taken)}** transferred from {cog.who(loser)} to "
                f"{cog.who(winner)}." + extra)
    await give_cash(conn, winner, wager * 2, reason="special_gain",
                    detail="Scam Duel")
    return f"🏆 {cog.who(winner)} takes the **{money(wager * 2)}** pot."


async def _resolve_duel(cog, conn, event: dict, actor_move: str,
                        other: str, other_move: str) -> None:
    actor = event["actor"]
    await close_event(conn, event["id"])
    a_emoji, a_name = sc.DUEL_MOVES[actor_move]
    b_emoji, b_name = sc.DUEL_MOVES[other_move]
    head = (f"⚔️ **DUEL RESULT**\n"
            f"{cog.who(actor)}: {a_emoji} {a_name}\n"
            f"{cog.who(other)}: {b_emoji} {b_name}\n")
    won = sc.duel_winner(actor_move, other_move)
    wager = int(event["payload"].get("wager", 0))
    if won is None:
        if wager:
            await give_cash(conn, actor, wager, reason="special_refund", detail="Duel")
            await give_cash(conn, other, wager, reason="special_refund", detail="Duel")
        await conn.commit()
        await cog.post(head + "\nBoth Princes chose identically. Nobody wins, "
                              "and everybody claims this was intentional."
                       + ("\nBoth wagers refunded." if wager else ""))
        return
    winner, loser = (actor, other) if won else (other, actor)
    line = await _duel_pay(cog, conn, winner, loser, wager=wager,
                           steal=not wager)
    await conn.commit()
    await cog.post(head + f"\n🏆 {cog.who(winner)} wins.\n" + line)


async def _forced_duel_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    if uid != event["payload"].get("victim"):
        return "❌ You were not the one challenged."
    return None      # move is read from the action in handle_event_click


async def _forced_duel_expire(cog, conn, event: dict) -> None:
    # No answer is a random answer, so refusing to click is not a defence.
    victim = event["payload"]["victim"]
    move = random.choice(list(sc.DUEL_MOVES))
    await cog.post(f"⌛ {cog.who(victim)} did not respond. A weapon has been "
                   "chosen on their behalf.")
    await _resolve_duel(cog, conn, event, event["payload"]["actor_move"],
                        victim, move)


async def _open_duel_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    if uid == event["actor"]:
        return "❌ You cannot accept your own challenge."
    wager = int(event["payload"]["wager"])
    cash, _f = await wealth_of(conn, uid)
    if cash < wager:
        return f"❌ Matching that wager costs {money(wager)} and you have {money(cash)}."
    if not await claim_event(conn, event["id"], uid):
        return "❌ Somebody else accepted first."
    await adjust_balance(conn, uid, -wager, "special_stake", "Open Duel")
    await conn.commit()
    return None


async def _open_duel_expire(cog, conn, event: dict) -> None:
    wager = int(event["payload"]["wager"])
    await close_event(conn, event["id"], "expired")
    await give_cash(conn, event["actor"], wager, reason="special_refund",
                    detail="Open Duel")
    await conn.commit()
    await cog.post(
        f"🤺 **NO TAKERS**\nNobody accepted {cog.who(event['actor'])}'s "
        f"challenge.\nThe {money(wager)} wager is refunded. The "
        f"{money(250)} setup fee is not."
    )


_MOVE_BUTTONS = [(k, f"{e} {n}", "blue") for k, (e, n) in sc.DUEL_MOVES.items()]
event_handler("forced_duel", buttons=_MOVE_BUTTONS,
              on_click=_forced_duel_click, on_expire=_forced_duel_expire)
event_handler("open_duel", buttons=[("accept", "🤺 ACCEPT CHALLENGE", "red")],
              on_click=_open_duel_click, on_expire=_open_duel_expire)
event_handler("open_duel_move", buttons=_MOVE_BUTTONS,
              on_click=None, on_expire=None)


# ── Public event: special operations ──────────────────────────────────────────
# Art Heist, Coup and Kidnapping differ only in their outcome tables, so they
# share every line of code below.

OPERATIONS = {
    "art_heist": {
        "title": "🖼️ SPECIAL OPERATION: GREAT ART HEIST",
        "blurb": "is assembling a crew.",
        "button": "🖼️ JOIN HEIST",
        "max": 4,
        "outcomes": [
            (0.35, 2.5, False, "🖼️ **CLEAN HEIST**\nThe crew escaped with the "
                               "collection."),
            (0.20, 4.0, False, "🎨 **MASTERPIECE FOUND**\nThe painting was "
                               "apparently worth considerably more than "
                               "expected."),
            (0.25, 0.0, False, "🧱 **HEIST FAILED**\nThe painting was bolted to "
                               "the wall. All stakes lost."),
            (0.20, 0.0, True,  "🚔 **POLICE STING**\nThe gallery was an "
                               "elaborate police operation."),
        ],
    },
    "coup": {
        "title": "🏛️ SPECIAL OPERATION: NIGERIAN GOVERNMENT COUP",
        "blurb": "is recruiting a provisional government.",
        "button": "🏛️ JOIN COUP",
        "max": 4,
        "outcomes": [
            (0.25, 4.0, False, "🏛️ **THE COUP SUCCEEDS**\nThe new government "
                               "recognises all participants as early "
                               "investors."),
            (0.30, 1.3, False, "📜 **PARTIAL SUCCESS**\nThe coup has produced a "
                               "committee."),
            (0.25, 0.3, False, "📉 **COUP MOSTLY FAILED**\nParticipants recover "
                               "30% of their stakes."),
            (0.20, 0.0, True,  "🚔 **COUNTER-COUP**\nThe old government has "
                               "returned."),
        ],
    },
    "kidnapping": {
        "title": "🕴️ SPECIAL OPERATION: DIPLOMATIC KIDNAPPING",
        "blurb": "has proposed an extremely unconventional diplomatic "
                 "initiative.",
        "button": "🕴️ JOIN",
        "max": 3,
        "outcomes": [
            (0.35, 5.0, False, "🕴️ **DIPLOMATIC BREAKTHROUGH**\nThe negotiation "
                               "was successful for reasons nobody will put in "
                               "writing."),
            (0.25, 0.0, False, "📞 **NEGOTIATIONS COLLAPSE**\nAll stakes lost."),
            (0.40, 0.0, True,  "🚔 **INTERNATIONAL INCIDENT**\nThe operation has "
                               "attracted official attention."),
        ],
    },
}
OPERATION_STAKE = 1_000


async def _operation_click(cog, conn, event: dict, uid: str) -> Optional[str]:
    spec = OPERATIONS[event["kind"]]
    entries = await entries_of(conn, event["id"])
    if any(u == uid for u, _a in entries):
        return "❌ You are already on the crew."
    if len(entries) >= spec["max"]:
        return f"❌ The crew is full at {spec['max']}."
    cash, _f = await wealth_of(conn, uid)
    if cash < OPERATION_STAKE:
        return (f"❌ The stake is {money(OPERATION_STAKE)} and you have "
                f"{money(cash)}.")
    await adjust_balance(conn, uid, -OPERATION_STAKE, "special_stake",
                         event["kind"])
    if not await join_event(conn, event["id"], uid, OPERATION_STAKE):
        await adjust_balance(conn, uid, OPERATION_STAKE, "special_refund",
                             event["kind"])
        return "❌ You are already on the crew."
    await conn.commit()
    await cog.post(f"➕ {cog.who(uid)} joins the crew "
                   f"({len(entries) + 1}/{spec['max']}).")
    return None


async def _operation_expire(cog, conn, event: dict) -> None:
    spec = OPERATIONS[event["kind"]]
    entries = await entries_of(conn, event["id"])
    await close_event(conn, event["id"])
    if not entries:
        await conn.commit()
        await cog.post("The operation was called off before it began.")
        return

    roll, cumulative = random.random(), 0.0
    multiplier, arrest, blurb = 0.0, False, ""
    for share, mult, arrests, text in spec["outcomes"]:
        cumulative += share
        if roll <= cumulative:
            multiplier, arrest, blurb = mult, arrests, text
            break

    lines = [blurb, ""]
    for uid, staked in entries:
        payout = int(staked * multiplier)
        if payout:
            # Gross system payout, run through the earnings modifiers so a
            # Burn Notice still bites on a heist.
            net, mod_lines = await cog.settle_reward(
                conn, uid, payout, kind="operation", detail=spec["title"])
            await give_cash(conn, uid, net, reason="special_payout",
                            detail=event["kind"])
            lines.append(f"💰 {cog.who(uid)}: **+{money(net)}**")
            lines.extend(mod_lines)
        else:
            lines.append(f"💸 {cog.who(uid)}: stake lost")
    if arrest:
        for uid, _staked in entries:
            lines.append(await jail_player(conn, uid))
    await conn.commit()
    await cog.post("\n".join(lines))


for _kind, _spec in OPERATIONS.items():
    event_handler(_kind, buttons=[("join", _spec["button"], "green")],
                  on_click=_operation_click, on_expire=_operation_expire)
del _kind, _spec


# ── Resolvers that open a public event ────────────────────────────────────────

def _opens(card_id: str, kind: str, *, title: str, body: str,
           minutes: float = sc.PUBLIC_EVENT_MINUTES, payload_fn=None):
    """Register a resolver whose whole job is to post a public event."""

    @resolver(card_id)
    async def _open(ctx: Ctx, _kind=kind, _title=title, _body=body,
                    _minutes=minutes, _payload_fn=payload_fn) -> Result:
        payload = await _payload_fn(ctx) if _payload_fn else {}
        if payload is None:
            return Result(title=_title,
                          public="The moment passed before it began.",
                          colour=_EMBED_GREY)
        event_id = await ctx.cog.open_event(
            ctx.conn, _kind, ctx.actor, minutes=_minutes, **payload
        )
        text = _body.format(actor=ctx.who(ctx.actor), **payload) if payload \
            else _body.format(actor=ctx.who(ctx.actor))
        return Result(title=_title, public=text,
                      view=event_view(event_id, _kind), store_message=True)

    return _open


_opens("special_unknown_caller", "unknown_caller",
       title="📞 UNKNOWN CALLER",
       body=("An unknown Nigerian number is calling.\nThis is probably fine.\n\n"
             "_First to pick up gets whatever is on the other end. "
             "{actor} cannot answer._"))

_opens("special_suspicious_tikkie", "suspicious_tikkie",
       title="💳 SUSPICIOUS TIKKIE",
       body=("Please verify your Royal Naira account by approving this "
             "entirely normal payment request.\n\n_Posted by {actor}, who "
             "cannot click it._"))

_opens("special_phishing_test", "phishing_test",
       title="🐟 PHISHING TEST",
       body=("**URGENT:** Your Royal Naira account must be re-verified "
             "immediately.\n\n_This is a test. {actor} is running it. "
             "The consequences are not a test._"))

_opens("special_marktplaats_deal", "marktplaats",
       title="🚲 MARKTPLAATS",
       body=("**Gazelle bicycle. Almost new.**\nOnly used by an elderly lady "
             "to cycle to church.\n\nPrice: **500 Naira**\n_Sold by {actor}._"))

_opens("special_mystery_box", "mystery_box",
       title="📦 MYSTERY BOX",
       body=("Nobody knows what is inside.\n{actor} has generously volunteered "
             "somebody else to find out.\n\n_Anything good is split with them. "
             "Anything bad is not._"))

_opens("special_dropped_wallet", "dropped_wallet",
       title="👛 DROPPED WALLET",
       body=("Somebody appears to have dropped a wallet.\nSurely nobody will "
             "mind.\n\n_Dropped by {actor}, who is watching from a distance._"))

_opens("special_royal_tip_jar", "tip_jar",
       title="🫙 THE ROYAL TIP JAR",
       body=("Throw in exactly **200 Naira**.\n\nOne contributor wins **80%**. "
             "{actor} receives **20%** for providing the jar — and may tip like "
             "anybody else.\n\n_Closes in 2 minutes. Fewer than two "
             "contributors and everybody gets their money back._"),
       minutes=sc.TIP_JAR_MINUTES)


async def _phishing_targets(ctx: Ctx) -> Optional[dict]:
    pool = []
    for uid in await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS, exclude=ctx.actor):
        cash, _f = await wealth_of(ctx.conn, uid)
        if cash > sc.CASH_FLOOR_DEFAULT:
            pool.append(uid)
    if not pool:
        pool = await actives(ctx.conn, sc.ACTIVE_WINDOW_HOURS, exclude=ctx.actor)
    if not pool:
        return None
    picked = random.sample(pool, min(5, len(pool)))
    return {"targets": picked,
            "target_list": " ".join(f"<@{u}>" for u in picked)}


_opens("special_mass_phishing_campaign", "mass_phishing",
       title="📲 MASS PHISHING CAMPAIGN",
       body=("{actor} has launched an urgent security-verification campaign.\n\n"
             "**Targeted Princes:** {target_list}\n\n"
             "_Only those named can click. Everybody else is safe, and may "
             "watch._"),
       payload_fn=_phishing_targets)


async def _olympics_payload(ctx: Ctx) -> dict:
    return {"stake": ctx.amount or 1_000, "buyin": money(ctx.amount or 1_000)}


_opens("special_scam_olympics", "olympics",
       title="🏅 THE FIRST NIGERIAN SCAM OLYMPICS",
       body=("{actor} has opened registration.\n\n"
             "Buy-in: **{buyin}** · Maximum entrants: **10**\n"
             "One random champion takes **90%** of the pot. Roger keeps the "
             "rest as an administrative fee and destroys it.\n\n"
             "_The International Olympic Committee has not responded._"),
       payload_fn=_olympics_payload)

_opens("special_ponzi_launch_party", "ponzi_party",
       title="🧨 PONZI LAUNCH PARTY",
       body=("{actor} is launching a revolutionary new investment model.\n\n"
             "Setup fee already paid: **250** · Buy-in: **500**\n"
             "Minimum 3, maximum 6 participants.\n\n"
             "One investor takes **60%**, {actor} takes **20%**, and **20%** "
             "goes wherever Ponzi money goes.\n"
             "_{actor} may join for 500 like anybody else._"))


for _op_card, _op_kind in (
    ("special_great_art_heist", "art_heist"),
    ("special_nigerian_government_coup", "coup"),
    ("special_diplomatic_kidnapping", "kidnapping"),
):
    _spec = OPERATIONS[_op_kind]

    @resolver(_op_card)
    async def _open_operation(ctx: Ctx, _kind=_op_kind, _spec=_spec) -> Result:
        event_id = await ctx.cog.open_event(ctx.conn, _kind, ctx.actor,
                                            minutes=sc.PUBLIC_EVENT_MINUTES)
        # The activation cost *is* the stake, so the activator is already in.
        await join_event(ctx.conn, event_id, ctx.actor, OPERATION_STAKE)
        return Result(
            title=_spec["title"],
            public=(f"{ctx.who(ctx.actor)} {_spec['blurb']}\n\n"
                    f"Stake: **{money(OPERATION_STAKE)}** · Max crew: "
                    f"**{_spec['max']}**\n"
                    f"{ctx.who(ctx.actor)} is already in.\n\n"
                    "_It can go ahead solo. That has never stopped anybody._"),
            view=event_view(event_id, _kind), store_message=True,
        )

del _op_card, _op_kind, _spec


@resolver("special_forced_scam_duel")
async def _forced_duel(ctx: Ctx) -> Result:
    victim = ctx.choice
    actor_move = random.choice(list(sc.DUEL_MOVES))
    event_id = await ctx.cog.open_event(
        ctx.conn, "forced_duel", ctx.actor, minutes=sc.DUEL_RESPONSE_MINUTES,
        victim=victim, actor_move=actor_move,
    )
    return Result(
        title="⚔️ FORCED SCAM DUEL",
        public=(f"{ctx.who(ctx.actor)} has challenged {ctx.who(victim)}.\n\n"
                "Choose your weapon:\n"
                "🧾 **Paperwork** beats 💻 **Phishing** beats 👑 **Prince** "
                "beats 🧾 **Paperwork**\n\n"
                f"{ctx.who(victim)} has **5 minutes**. No response means a "
                "weapon is chosen for them.\n\n"
                "The winner takes **25%** of the loser's wealth, up to 5.000. "
                f"{ctx.who(ctx.actor)} has already chosen and is just as "
                "exposed."),
        view=event_view(event_id, "forced_duel"), store_message=True,
    )


@resolver("special_open_scam_duel")
async def _open_duel(ctx: Ctx) -> Result:
    wager = ctx.amount or 1_000
    await adjust_balance(ctx.conn, ctx.actor, -wager, "special_stake",
                         "Open Duel wager")
    actor_move = random.choice(list(sc.DUEL_MOVES))
    event_id = await ctx.cog.open_event(
        ctx.conn, "open_duel", ctx.actor, minutes=sc.DUEL_RESPONSE_MINUTES,
        wager=wager, actor_move=actor_move,
    )
    return Result(
        title="🤺 OPEN SCAM DUEL",
        public=(f"{ctx.who(ctx.actor)} is offering a **{money(wager)}** duel.\n\n"
                "The first player to accept matches the wager. Winner takes "
                "both stakes; a tie refunds both.\n\n"
                "_Expires in 5 minutes. The 250 setup fee is already gone "
                "either way._"),
        view=event_view(event_id, "open_duel"), store_message=True,
    )


# ── Unleash the Muggers ───────────────────────────────────────────────────────

@resolver("special_unleash_the_muggers")
async def _muggers(ctx: Ctx) -> Result:
    await add_effect(ctx.conn, "muggers", owner_id=ctx.actor, hours=3,
                     successes=0, stolen=0, paid=0, destroyed=0)
    return Result(
        title="🔪 UNLEASH THE MUGGERS",
        public=(f"{ctx.who(ctx.actor)} has funded three hours of "
                "entrepreneurial street activity.\n\n"
                "Every **10 minutes**, one of the current top-5 cash holders "
                "may be mugged for up to **750**.\n"
                f"A third of anything taken finds its way to "
                f"{ctx.who(ctx.actor)}. The rest vanishes into the informal "
                "economy.\n\n"
                "_Only successful muggings are reported. The failures are "
                "nobody's business._"),
        colour=_EMBED_RED,
    )


# ── Choosing a target / an amount ─────────────────────────────────────────────
# Options are built when the player opens the card, never at offer-generation
# time (spec §2.6): a list of victims chosen two hours ago would be a list of
# people who have since gone to bed.

async def _pick_fund(ctx: Ctx, minimum: int) -> list[tuple[str, str]]:
    from nigeria_bot import royal_fund as rf

    held = dict(await rf.positions(ctx.conn))
    pool = await _fund_victims(ctx.conn, ctx.actor, minimum)
    return [(u, f"position {money(held.get(u, 0))}") for u in pool]


async def _pick_actives(ctx: Ctx, hours: float, *, free_only: bool = False
                        ) -> list[tuple[str, str]]:
    pool = (await free_actives(ctx.conn, hours, exclude=ctx.actor) if free_only
            else await actives(ctx.conn, hours, exclude=ctx.actor))
    random.shuffle(pool)
    return [(u, "recently active") for u in pool[:3]]


async def _pick_rich(ctx: Ctx, by: str, limit: int) -> list[tuple[str, str]]:
    rows = await richest(ctx.conn, sc.ACTIVE_LONG_HOURS, exclude=ctx.actor,
                         limit=limit, by=by)
    label = "cash" if by == "balance" else "total wealth"
    return [(u, f"{money(v)} {label}") for u, v in rows]


async def _pick_investors(ctx: Ctx) -> list[tuple[str, str]]:
    from nigeria_bot import royal_fund as rf

    rows = [(u, a) for u, a in await rf.positions(ctx.conn) if u != ctx.actor]
    return [(u, f"position {money(a)}") for u, a in rows[:3]]


PICKERS: dict[str, Callable[[Ctx], Awaitable[list[tuple[str, str]]]]] = {
    "special_ponzi_pitch": lambda c: _pick_fund(c, 1_000),
    "special_false_investment_fraud": lambda c: _pick_fund(c, 4_500),
    "special_hostile_acquisition": lambda c: _pick_fund(c, FUND_ACQUIRE_MIN),
    "special_snitch": lambda c: _pick_actives(c, sc.ACTIVE_WINDOW_HOURS,
                                              free_only=True),
    "special_forced_scam_duel": lambda c: _pick_actives(c, sc.ACTIVE_WINDOW_HOURS),
    "special_personal_grudge": lambda c: _pick_actives(c, sc.ACTIVE_SHORT_HOURS),
    "special_cash_predator": lambda c: _pick_rich(c, "balance", 5),
    "special_burn_notice": lambda c: _pick_rich(c, "wealth", 5),
    "special_asset_freeze": _pick_investors,
}

# Cards where the player picks a number instead of a person.
AMOUNTS: dict[str, tuple[str, list[int]]] = {
    "special_open_scam_duel": ("Wager", [1_000, 2_000, 3_000]),
    "special_scam_olympics": ("Buy-in", [1_000, 2_000]),
}


# ── The /special menu ─────────────────────────────────────────────────────────

def card_embed(card: dict, tier: str) -> discord.Embed:
    rarity = sc.rarity_of(card["id"], tier)
    embed = discord.Embed(
        title=f"{card['emoji']} {card['name']}",
        description=card["one_line"],
        colour={
            sc.COMMON: _EMBED_GREY, sc.RARE: discord.Colour.blue(),
            sc.VERY_RARE: _EMBED_PURPLE, sc.EXTREME_RARE: _EMBED_RED,
        }[rarity],
    )
    embed.set_author(name=f"{sc.TIER_LABEL[tier]} · "
                          f"{sc.RARITY_EMOJI[rarity]} {sc.RARITY_LABEL[rarity]}")
    embed.add_field(name="Cost", value=f"**{card['cost_label']}**", inline=True)
    return embed


class SpecialView(discord.ui.View):
    """The three-card menu.

    Ephemeral and short-lived on purpose: the *offer* is persisted in the
    database, so a lost view costs nothing — running `/special` again shows
    the same three cards.
    """

    def __init__(self, cog: "SpecialCog", uid: str, offer: dict) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.uid = uid
        self.offer = offer
        for tier in sc.TIERS:
            card = sc.CARDS[offer["cards"][tier]]
            self.add_item(self._choose(tier, card))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message(
                "❌ That is not your offer.", ephemeral=True)
            return False
        return True

    def _choose(self, tier: str, card: dict) -> discord.ui.Button:
        button = discord.ui.Button(
            label=f"{sc.TIER_LABEL[tier].split()[1].title()}: {card['name']}"[:80],
            emoji=card["emoji"],
            style=discord.ButtonStyle.primary,
        )

        async def go(interaction: discord.Interaction) -> None:
            await self.cog.begin_activation(interaction, self.offer, tier)

        button.callback = go
        return button

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary,
                       row=1)
    async def close(self, interaction: discord.Interaction,
                    _b: discord.ui.Button) -> None:
        # Explicitly harmless: closing the menu costs nothing and keeps the
        # same three cards, which is the whole anti-reroll promise.
        await interaction.response.edit_message(
            content="Offer kept. Run `/special` again whenever you like — it "
                    "will be the same three cards.",
            embeds=[], view=None,
        )


class ChoiceView(discord.ui.View):
    """Second step: pick a victim, or pick a wager."""

    def __init__(self, cog: "SpecialCog", uid: str, offer: dict, tier: str,
                 options: list[tuple[str, str]], *, amounts: bool = False,
                 label: str = "Target") -> None:
        super().__init__(timeout=300)
        self.cog, self.uid, self.offer, self.tier = cog, uid, offer, tier
        self.amounts = amounts
        select = discord.ui.Select(
            placeholder=f"Choose a {label.lower()}…",
            options=[
                discord.SelectOption(
                    label=(value if amounts else "…")[:100],
                    value=str(value), description=note[:100],
                )
                for value, note in options
            ],
        )
        select.callback = self._picked
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.uid

    async def _picked(self, interaction: discord.Interaction) -> None:
        value = self.select.values[0]
        await self.cog.finish_activation(
            interaction, self.offer, self.tier,
            choice=None if self.amounts else value,
            amount=int(value) if self.amounts else 0,
        )


class ConfirmView(discord.ui.View):
    """The last chance for the irreversible cards (spec §2.7)."""

    def __init__(self, cog: "SpecialCog", uid: str, offer: dict, tier: str,
                 choice: Optional[str], amount: int) -> None:
        super().__init__(timeout=120)
        self.cog, self.uid, self.offer, self.tier = cog, uid, offer, tier
        self.choice, self.amount = choice, amount

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.uid

    @discord.ui.button(label="Yes. Do it.", style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction,
                 _b: discord.ui.Button) -> None:
        await self.cog.finish_activation(
            interaction, self.offer, self.tier, choice=self.choice,
            amount=self.amount, confirmed=True,
        )

    @discord.ui.button(label="On reflection, no",
                       style=discord.ButtonStyle.secondary)
    async def stop_it(self, interaction: discord.Interaction,
                      _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Nothing has been spent and your offer is untouched.",
            embeds=[], view=None,
        )


# ── The cog ───────────────────────────────────────────────────────────────────

class SpecialCog(commands.Cog, name="special_game"):
    def __init__(self, bot: commands.Bot, conn: aiosqlite.Connection) -> None:
        self.bot = bot
        self.conn = conn
        self._lock = asyncio.Lock()
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    # ── small helpers ─────────────────────────────────────────────────
    @staticmethod
    def who(user_id: str) -> str:
        return f"<@{user_id}>"

    async def post(self, body: str, *, title: Optional[str] = None,
                   colour: discord.Colour = _EMBED_GOLD,
                   view: Optional[discord.ui.View] = None):
        """Publish to the game channel.

        Delivery failure is logged and swallowed: the economy has already
        committed by the time we get here, and undoing money because Discord
        hiccuped would be far worse than a missing message (spec §18).
        """
        channel = self.bot.get_channel(GAME_CHANNEL_ID)
        if channel is None:
            return None
        embed = discord.Embed(title=title, description=body, colour=colour) \
            if title else None
        try:
            if embed is not None:
                return await channel.send(embed=embed, view=view)
            return await channel.send(content=body, view=view)
        except discord.HTTPException:
            logger.warning("special: could not post to the game channel")
            return None

    async def extra_losses(self, conn, victim: str, base: int, *,
                           domain: str = "wealth",
                           detail: Optional[str] = None) -> str:
        from nigeria_bot.special_effects import on_loss

        lines = await on_loss(conn, victim, base, domain=domain, detail=detail)
        return ("\n" + "\n".join(lines)) if lines else ""

    async def settle_reward(self, conn, uid: str, gross: int, *, kind: str,
                            detail: Optional[str] = None):
        from nigeria_bot.special_effects import on_reward

        return await on_reward(conn, uid, gross, kind=kind, detail=detail)

    # ── events ────────────────────────────────────────────────────────
    async def open_event(self, conn, kind: str, actor: str, *,
                         minutes: float, **payload) -> int:
        cur = await conn.execute(
            "INSERT INTO special_events"
            " (kind, actor_id, channel_id, created_at, expires_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (kind, str(actor), str(GAME_CHANNEL_ID), _iso(_now()),
             _iso(_now() + timedelta(minutes=minutes)), json.dumps(payload)),
        )
        return int(cur.lastrowid)

    async def handle_event_click(self, interaction: discord.Interaction,
                                 event_id: int, action: str) -> None:
        uid = str(interaction.user.id)
        async with self._lock:
            event = await get_event(self.conn, event_id)
            if event is None:
                await _reply(interaction, content="❌ That event is long gone.",
                             ephemeral=True)
                return
            if event["status"] != "open":
                await _reply(interaction,
                             content="❌ That one is already closed.",
                             ephemeral=True)
                return
            if _parse(event["expires_at"]) <= _now():
                await _reply(interaction, content="❌ Too late.", ephemeral=True)
                return
            if await get_jail(self.conn, uid) is not None:
                await _reply(interaction,
                             content="❌ You are in jail. You cannot join in.",
                             ephemeral=True)
                return

            await touch(self.conn, uid)

            # Duels resolve straight off the button that was pressed.
            if event["kind"] == "forced_duel" and action in sc.DUEL_MOVES:
                if uid != event["payload"].get("victim"):
                    await _reply(interaction,
                                 content="❌ You were not the one challenged.",
                                 ephemeral=True)
                    return
                await _resolve_duel(self, self.conn, event,
                                    event["payload"]["actor_move"], uid, action)
                await _reply(interaction, content="Weapon chosen.",
                             ephemeral=True)
                return
            if event["kind"] == "open_duel_move" and action in sc.DUEL_MOVES:
                if uid != event["payload"].get("opponent"):
                    await _reply(interaction, content="❌ Not your duel.",
                                 ephemeral=True)
                    return
                await _resolve_duel(self, self.conn, event,
                                    event["payload"]["actor_move"], uid, action)
                await _reply(interaction, content="Weapon chosen.",
                             ephemeral=True)
                return
            if event["kind"] == "wallet_choice":
                entries = await entries_of(self.conn, event_id)
                if not entries or uid != entries[0][0]:
                    await _reply(interaction,
                                 content="❌ That is not your wallet.",
                                 ephemeral=True)
                    return
                await _wallet_settle(self, self.conn, event, keep=action == "keep")
                await _reply(interaction, content="Decision made.",
                             ephemeral=True)
                return

            handler = EVENT_HANDLERS.get(event["kind"], {}).get("on_click")
            if handler is None:
                await _reply(interaction, content="❌ Nothing to do here.",
                             ephemeral=True)
                return
            refusal = await handler(self, self.conn, event, uid)
            await self.conn.commit()

        if refusal:
            await _reply(interaction, content=refusal, ephemeral=True)
        else:
            await _reply(interaction, content="✅ Done.", ephemeral=True)

        # An accepted open duel needs both weapons; ask for them now.
        if event["kind"] == "open_duel":
            await self._start_open_duel_moves(event_id)

    async def _start_open_duel_moves(self, event_id: int) -> None:
        event = await get_event(self.conn, event_id)
        if event is None or event["status"] != "claimed":
            return
        opponent = event["claimed_by"]
        payload = dict(event["payload"])
        payload["opponent"] = opponent
        move_id = await self.open_event(
            self.conn, "open_duel_move", event["actor"],
            minutes=sc.DUEL_RESPONSE_MINUTES, **payload,
        )
        await self.conn.commit()
        await self.post(
            f"🤺 {self.who(opponent)} accepted {self.who(event['actor'])}'s "
            f"**{money(int(payload['wager']))}** duel.\n"
            f"{self.who(opponent)}, choose your weapon — 5 minutes.",
            view=event_view(move_id, "open_duel_move"),
        )

    # ── /special ──────────────────────────────────────────────────────
    @app_commands.command(
        name="special",
        description="Three one-off opportunities. One choice. Two-hour cooldown.",
    )
    async def special(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID,
                                      GAME_CHANNEL_URL):
            return
        if not await require_free(interaction, self.conn, "use a Special"):
            return
        uid = str(interaction.user.id)
        async with self._lock:
            await get_player(self.conn, uid)
            offer = await open_offer(self.conn, uid)
            if offer is None:
                when = await ready_at(self.conn, uid)
                if when:
                    await _reply(
                        interaction,
                        embed=discord.Embed(
                            title="⏳ NOTHING ON OFFER YET",
                            description=(
                                "Your contacts need time to arrange the next "
                                "round of opportunities.\n\n"
                                f"**Ready:** <t:{int(when.timestamp())}:R>\n\n"
                                "_A Special every 2 hours. Roger occasionally "
                                "resets it for people who ask nicely._"
                            ),
                            colour=_EMBED_GREY,
                        ),
                        ephemeral=True,
                    )
                    return
                offer = await self._generate(uid)
                await self.conn.commit()
                if offer is None:
                    await _reply(
                        interaction,
                        content="❌ Nigeria has nothing to offer you right now. "
                                "Try again in a few minutes.",
                        ephemeral=True,
                    )
                    return

        embeds = [card_embed(sc.CARDS[offer["cards"][t]], t) for t in sc.TIERS]
        await _reply(
            interaction,
            content=("**Pick one.** The other two are gone the moment you do, "
                     "and the two-hour clock starts on activation — not on "
                     "looking."),
            embeds=embeds,
            view=SpecialView(self, uid, offer),
            ephemeral=True,
        )

    async def _generate(self, uid: str) -> Optional[dict]:
        from nigeria_bot import royal_fund as rf

        allowed = await eligible_cards(self.conn, uid)
        cards = sc.generate_offer(eligible=lambda cid: cid in allowed)
        if cards is None:
            return None
        # Carl Marx equalises the cohort as it stood *when the offer appeared*,
        # so the snapshot has to be taken now — otherwise seeing the card is an
        # invitation to deposit 1 Naira and join the payout.
        cohort = []
        if sc.CARDS["special_the_return_of_carl_marx"]["id"] in cards.values():
            cohort = [u for u, _a in await rf.positions(self.conn)]
        cur = await self.conn.execute(
            "INSERT INTO special_offers (player_id, generated_at, budget_card,"
            " premium_card, platinum_card, cohort) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, _iso(_now()), cards[sc.BUDGET], cards[sc.PREMIUM],
             cards[sc.PLATINUM], json.dumps(cohort)),
        )
        return {"id": int(cur.lastrowid), "cards": cards, "cohort": cohort,
                "generated_at": _iso(_now())}

    # ── activation ────────────────────────────────────────────────────
    async def begin_activation(self, interaction: discord.Interaction,
                               offer: dict, tier: str) -> None:
        """Step one: ask for whatever the card still needs to know."""
        uid = str(interaction.user.id)
        card = sc.CARDS[offer["cards"][tier]]

        if card["id"] in AMOUNTS:
            label, values = AMOUNTS[card["id"]]
            await interaction.response.edit_message(
                content=f"**{card['emoji']} {card['name']}** — choose your "
                        f"{label.lower()}.",
                embeds=[], view=ChoiceView(
                    self, uid, offer, tier,
                    [(str(v), money(v)) for v in values],
                    amounts=True, label=label,
                ),
            )
            return

        picker = PICKERS.get(card["id"])
        if picker is not None:
            ctx = Ctx(self, self.conn, uid, card, offer,
                      guild=interaction.guild)
            options = await picker(ctx)
            if not options:
                await interaction.response.edit_message(
                    content="❌ There is nobody eligible for that card right "
                            "now. Nothing has been spent — your offer is "
                            "unchanged.",
                    embeds=[], view=None,
                )
                return
            named = []
            for user_id, note in options:
                member = interaction.guild.get_member(int(user_id)) \
                    if interaction.guild else None
                named.append((user_id, note, member.display_name if member
                              else f"Prince {user_id[-4:]}"))
            view = ChoiceView(self, uid, offer, tier,
                              [(u, n) for u, n, _d in named])
            for option, (_u, _n, display) in zip(view.select.options, named):
                option.label = display[:100]
            await interaction.response.edit_message(
                content=f"**{card['emoji']} {card['name']}** — choose your "
                        "target.",
                embeds=[], view=view,
            )
            return

        await self.finish_activation(interaction, offer, tier)

    async def finish_activation(self, interaction: discord.Interaction,
                                offer: dict, tier: str, *,
                                choice: Optional[str] = None,
                                amount: int = 0,
                                confirmed: bool = False) -> None:
        """Step two: revalidate, charge, run, commit, announce.

        Validation happens *again* here even though generation filtered for it,
        because minutes may have passed and the world moves (spec §2.5).  A
        failed validation costs nothing at all — no charge, no cooldown, and
        the offer stays exactly where it was.
        """
        uid = str(interaction.user.id)
        card = sc.CARDS[offer["cards"][tier]]

        if card["confirm"] and not confirmed:
            await interaction.response.edit_message(
                content=(f"⚠️ **{card['emoji']} {card['name']}** — "
                         f"{card['one_line']}\n\n"
                         f"This costs **{card['cost_label']}** and cannot be "
                         "undone. Still want to?"),
                embeds=[],
                view=ConfirmView(self, uid, offer, tier, choice, amount),
            )
            return

        async with self._lock:
            live = await open_offer(self.conn, uid)
            if live is None or live["id"] != offer["id"]:
                await interaction.response.edit_message(
                    content="❌ That offer has already been used.",
                    embeds=[], view=None)
                return

            total_cost = card["cost"] + (amount if card["id"] in AMOUNTS else 0)
            player = await get_player(self.conn, uid)
            if player["balance"] < total_cost:
                await interaction.response.edit_message(
                    content=(f"❌ **{card['name']}** costs "
                             f"{money(total_cost)} and you have "
                             f"{money(player['balance'])}.\n\n"
                             "_Nothing was spent. Your offer is still open and "
                             "your cooldown has not started._"),
                    embeds=[], view=None)
                return

            allowed = await eligible_cards(self.conn, uid)
            if card["id"] not in allowed:
                await interaction.response.edit_message(
                    content=(f"❌ **{card['name']}** cannot be used right now — "
                             "the situation it needed has changed.\n\n"
                             "_Nothing was spent and your offer is unchanged._"),
                    embeds=[], view=None)
                return

            # Everything below this line is one transaction: pay, consume the
            # offer, start the cooldown, run the card.  A Discord failure
            # afterwards must not undo any of it.
            if card["cost"]:
                await adjust_balance(self.conn, uid, -card["cost"],
                                     "special_cost", card["name"])
            await self.conn.execute(
                "UPDATE special_offers SET status = 'consumed',"
                " consumed_card = ?, consumed_at = ? WHERE id = ?",
                (card["id"], _iso(_now()), offer["id"]),
            )
            await self.conn.execute(
                "UPDATE scam_players SET special_cooldown_until = ?"
                " WHERE discord_user_id = ?",
                (_iso(_now() + SPECIAL_COOLDOWN), uid),
            )
            await touch(self.conn, uid)

            ctx = Ctx(self, self.conn, uid, card, offer, choice=choice,
                      amount=amount, guild=interaction.guild)
            try:
                result = await RESOLVERS[card["id"]](ctx)
            except Exception:
                logger.exception("special: %s blew up during activation",
                                 card["id"])
                await self.conn.rollback()
                await interaction.response.edit_message(
                    content="❌ Something went wrong and nothing was charged. "
                            "Please tell Marijn which card you picked.",
                    embeds=[], view=None)
                return
            await self.conn.commit()

        ready = _now() + SPECIAL_COOLDOWN
        private = result.private or (
            f"**{card['emoji']} {card['name']}** activated."
            + (f"\n\n{result.public}" if result.public and not result.view else "")
        )
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                title=result.title or f"{card['emoji']} {card['name']}",
                description=private + f"\n\n_Next Special "
                                      f"<t:{int(ready.timestamp())}:R>._",
                colour=result.colour,
            ),
            view=None,
        )
        if result.public:
            message = await self.post(result.public, title=result.title,
                                      colour=result.colour, view=result.view)
            if message is not None and result.store_message:
                # Remember which message carries the buttons so the expiry
                # sweep can grey them out instead of leaving a live-looking
                # card under a finished event.
                await self.conn.execute(
                    "UPDATE special_events SET message_id = ? WHERE id ="
                    " (SELECT MAX(id) FROM special_events WHERE actor_id = ?)",
                    (str(message.id), uid),
                )
                await self.conn.commit()

    # ── background work ───────────────────────────────────────────────
    @tasks.loop(seconds=20)
    async def tick(self) -> None:
        """One clock for everything that has to happen on its own.

        Deliberately a single loop: three independent loops racing for the
        same lock produced nothing but contention, and 20 seconds is well
        inside the tolerance of a three-minute event window.
        """
        try:
            await self._expire_events()
        except Exception:
            logger.exception("special: event expiry failed")
        try:
            await self._mug()
        except Exception:
            logger.exception("special: mugging tick failed")
        try:
            await self._reveal_beg_traps()
        except Exception:
            logger.exception("special: beg reveal failed")
        try:
            await self._roger()
        except Exception:
            logger.exception("special: roger tick failed")

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _expire_events(self) -> None:
        async with self.conn.execute(
            "SELECT id FROM special_events WHERE status = 'open'"
            " AND expires_at <= ?", (_iso(_now()),),
        ) as cur:
            ids = [int(r[0]) async for r in cur]
        for event_id in ids:
            async with self._lock:
                event = await get_event(self.conn, event_id)
                if event is None or event["status"] != "open":
                    continue
                handler = EVENT_HANDLERS.get(event["kind"], {}).get("on_expire")
                if handler is None:
                    await close_event(self.conn, event_id, "expired")
                    await self.conn.commit()
                    continue
                await handler(self, self.conn, event)
                await self.conn.commit()
            await self._disable_buttons(event)

    async def _disable_buttons(self, event: dict) -> None:
        if not event.get("message_id") or not event.get("channel_id"):
            return
        channel = self.bot.get_channel(int(event["channel_id"]))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(event["message_id"]))
            await message.edit(view=None)
        except (discord.HTTPException, discord.NotFound):
            pass

    BEG_SESSION_QUIET = timedelta(minutes=10)

    async def _reveal_beg_traps(self) -> None:
        """Publish Trickle-Up once the beg session it hijacked has gone quiet.

        The reveal is the payoff, but it has to come *after* the donations:
        announcing mid-session would warn everybody else and the trap would
        only ever catch one person.  A session counts as over when nothing has
        been donated into it for ten minutes.
        """
        cutoff = _iso(_now() - self.BEG_SESSION_QUIET)
        async with self.conn.execute(
            "SELECT id, owner_id, subject_id, payload FROM special_effects"
            " WHERE kind = 'trickle_live' AND status = 'active'"
            " AND created_at <= ?", (cutoff,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            effect_id, owner, message_id = int(row[0]), str(row[1]), str(row[2])
            total = json.loads(row[3] or "{}").get("total", 0)
            await consume_effect(self.conn, effect_id)
            async with self.conn.execute(
                "SELECT beggar_id FROM scam_begs WHERE message_id = ?",
                (message_id,),
            ) as beg_cur:
                beg = await beg_cur.fetchone()
            await self.conn.commit()
            if not total:
                continue
            beggar = str(beg[0]) if beg else "somebody"
            await self.post(
                f"{self.who(beggar)} raised **{money(total)}** from the "
                "community.\n\nDue to an innovative restructuring of the "
                f"welfare system, the entire amount was redirected to "
                f"{self.who(owner)}.\n\n"
                f"💰 {self.who(owner)} **+{money(total)}**\n\n"
                "Economists assure us it will eventually trickle back down.",
                title="💸 TRICKLE-UP ECONOMICS",
                colour=_EMBED_RED,
            )

    async def _mug(self) -> None:
        """Unleash the Muggers: one roll every ten minutes while it runs."""
        effect = await global_effect(self.conn, "muggers")
        if effect is None:
            await self._finish_muggers()
            return
        payload = effect["payload"]
        last = payload.get("last_tick")
        if last and _parse(last) + timedelta(minutes=10) > _now():
            return
        async with self._lock:
            payload["last_tick"] = _iso(_now())
            targets = await richest(self.conn, sc.ACTIVE_WINDOW_HOURS,
                                    exclude=effect["owner_id"], limit=5)
            hit = None
            if targets and random.random() < 0.65:
                victim, _cash = random.choice(targets)
                taken = await take_cash(
                    self.conn, victim, 750, floor=sc.CASH_FLOOR_PREDATOR,
                    reason="special_theft", detail="Muggers")
                if taken > 0:
                    share = taken // 3
                    await give_cash(self.conn, effect["owner_id"], share,
                                    reason="special_gain", detail="Muggers")
                    payload["successes"] = payload.get("successes", 0) + 1
                    payload["stolen"] = payload.get("stolen", 0) + taken
                    payload["paid"] = payload.get("paid", 0) + share
                    payload["destroyed"] = (payload.get("destroyed", 0)
                                            + taken - share)
                    hit = (victim, taken, share, taken - share)
            await self.conn.execute(
                "UPDATE special_effects SET payload = ? WHERE id = ?",
                (json.dumps(payload), effect["id"]),
            )
            await self.conn.commit()
        if hit:
            victim, taken, share, sink = hit
            extra = await self.extra_losses(self.conn, victim, taken,
                                            domain="cash", detail="Muggers")
            await self.conn.commit()
            await self.post(
                f"🔪 **MUGGING SUCCESSFUL**\n{self.who(victim)} has been "
                f"relieved of **{money(taken)}**.\n"
                f"💰 {self.who(effect['owner_id'])}: **+{money(share)}**\n"
                f"🔥 Into the informal economy: **{money(sink)}**" + extra
            )

    async def _finish_muggers(self) -> None:
        """Post the summary once, when the three hours are up."""
        async with self.conn.execute(
            "SELECT id, owner_id, payload FROM special_effects"
            " WHERE kind = 'muggers' AND status = 'expired'"
            " AND payload NOT LIKE '%\"summarised\"%'",
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            payload = json.loads(row[2] or "{}")
            payload["summarised"] = True
            await self.conn.execute(
                "UPDATE special_effects SET payload = ? WHERE id = ?",
                (json.dumps(payload), int(row[0])),
            )
            await self.conn.commit()
            await self.post(
                "🔪 **THE MUGGERS HAVE GONE HOME**\n"
                f"Successful muggings: **{payload.get('successes', 0)}**\n"
                f"Total stolen: **{money(payload.get('stolen', 0))}**\n"
                f"Paid to {self.who(str(row[1]))}: "
                f"**{money(payload.get('paid', 0))}**\n"
                f"Destroyed: **{money(payload.get('destroyed', 0))}**"
            )

    # ── Roger's personal advice ───────────────────────────────────────
    async def _roger_state(self) -> dict:
        async with self.conn.execute(
            "SELECT next_at, event_id, expires_at, claimed_by, status"
            " FROM special_roger WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        return {"next_at": row[0], "event_id": row[1], "expires_at": row[2],
                "claimed_by": row[3], "status": row[4]}

    async def _busy(self) -> bool:
        since = _iso(_now() - ROGER_BUSY_WINDOW)
        async with self.conn.execute(
            "SELECT COUNT(*) FROM scam_players WHERE last_action_at >= ?",
            (since,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) >= ROGER_BUSY_PLAYERS

    async def _schedule_roger(self) -> None:
        lo, hi = ROGER_GAP_BUSY if await self._busy() else ROGER_GAP_NORMAL
        when = _now() + timedelta(minutes=random.uniform(lo, hi))
        await self.conn.execute(
            "UPDATE special_roger SET next_at = ?, status = 'idle',"
            " event_id = NULL, expires_at = NULL, claimed_by = NULL"
            " WHERE id = 1", (_iso(when),),
        )
        await self.conn.commit()

    async def _roger(self) -> None:
        state = await self._roger_state()
        if state["status"] == "open":
            if _parse(state["expires_at"]) <= _now():
                await self.conn.execute(
                    "UPDATE special_roger SET status = 'expired' WHERE id = 1")
                await self.conn.commit()
                await self.post(
                    "🧓 Roger waited five minutes, took the silence personally "
                    "and went back inside."
                )
                await self._schedule_roger()
            return
        if not state["next_at"]:
            await self._schedule_roger()
            return
        if _parse(state["next_at"]) > _now():
            return

        expires = _now() + ROGER_WINDOW
        await self.conn.execute(
            "UPDATE special_roger SET status = 'open', expires_at = ?,"
            " claimed_by = NULL WHERE id = 1", (_iso(expires),),
        )
        await self.conn.commit()
        await self.post(
            "Roger has been watching the Nigerian economy and believes "
            "somebody could benefit from his professional expertise.\n\n"
            "_First Prince to click gets the consultation. "
            "Offer expires in 5 minutes._",
            title="🧓 ROGER OFFERS HIS PERSONAL ADVICE",
            colour=_EMBED_GOLD,
            view=RogerView(),
        )

    async def claim_roger(self, interaction: discord.Interaction) -> None:
        uid = str(interaction.user.id)
        async with self._lock:
            cur = await self.conn.execute(
                "UPDATE special_roger SET status = 'claimed', claimed_by = ?"
                " WHERE id = 1 AND status = 'open'", (uid,),
            )
            if cur.rowcount != 1:
                await self.conn.commit()
                await _reply(interaction,
                             content="🧓 Roger is already advising somebody "
                                     "else.", ephemeral=True)
                return
            await get_player(self.conn, uid)
            await touch(self.conn, uid)
            body = await self._roger_outcome(uid)
            await self.conn.commit()
        await self._schedule_roger()
        await _reply(interaction, content="🧓 Roger takes you aside.",
                     ephemeral=True)
        await self.post(body, title="🧓 ROGER'S PERSONAL ADVICE",
                        colour=_EMBED_GOLD)

    async def _roger_outcome(self, uid: str) -> str:
        from nigeria_bot import scam_targets as st

        who = self.who(uid)
        if random.random() < ROGER_SCAM_CHANCE:
            cash, _f = await wealth_of(self.conn, uid)
            fee = min(ROGER_FEE, max(0, cash))    # no floor: Roger has no shame
            if fee:
                await adjust_balance(self.conn, uid, -fee, "special_loss",
                                     "Roger's consultancy fee")
            quote = random.choice(sc.ROGER_QUOTES["scam"])
            tail = ("\nRoger has successfully charged 0 Naira and calls this a "
                    "long-term client relationship." if fee == 0 else "")
            return (f"{who} accepted Roger's consultation.\n"
                    f"💸 Consultancy fee collected: **{money(fee)}**\n\n"
                    f"Roger says:\n> “{quote}”{tail}")

        # Helpful: pick uniformly among the outcomes that would do something,
        # and fall back to a harmless no-op so the 25% scam rate never drifts.
        useful = []
        if await ready_at(self.conn, uid):
            useful.append("special")
        async with self.conn.execute(
            "SELECT fake_target_until FROM scam_players WHERE discord_user_id = ?",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
        if row and row[0] and _parse(row[0]) > _now():
            useful.append("fake")
        intel = await st.intel_state(self.conn, uid)
        if intel["charges"] < st.INTEL_MAX_CHARGES:
            useful.append("intel")

        noop = not useful
        pick = random.choice(useful or ["special", "fake", "intel"])

        if pick == "special":
            await self.conn.execute(
                "UPDATE scam_players SET special_cooldown_until = NULL"
                " WHERE discord_user_id = ?", (uid,))
            headline = "⏱️ `/special` cooldown **RESET**."
        elif pick == "fake":
            await self.conn.execute(
                "UPDATE scam_players SET fake_target_until = NULL"
                " WHERE discord_user_id = ?", (uid,))
            headline = "🎭 Fake-target cooldown **RESET**."
        else:
            await self.conn.execute(
                "UPDATE scam_players SET intel_charges = ?,"
                " intel_next_charge_at = NULL WHERE discord_user_id = ?",
                (st.INTEL_MAX_CHARGES, uid))
            headline = f"🔎 Intel refilled to **{st.INTEL_MAX_CHARGES}/3**."

        quote = random.choice(sc.ROGER_QUOTES[pick])
        tail = ("\n\n_Roger has successfully improved something that was "
                "already fine._" if noop else "")
        return (f"{who} accepted Roger's consultation.\n{headline}\n\n"
                f"Roger says:\n> “{quote}”{tail}")


class RogerView(discord.ui.View):
    """One button, persistent, claimed by conditional UPDATE."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="ACCEPT ROGER'S ADVICE", emoji="🧓",
                       style=discord.ButtonStyle.success,
                       custom_id="special:roger_accept")
    async def accept(self, interaction: discord.Interaction,
                     _b: discord.ui.Button) -> None:
        if not await _ack(interaction):
            return
        cog = interaction.client.get_cog("special_game")
        if cog is None:
            await _reply(interaction, content="❌ Roger is unavailable.",
                         ephemeral=True)
            return
        await cog.claim_roger(interaction)


async def setup(bot: commands.Bot, conn: aiosqlite.Connection) -> SpecialCog:
    await setup_schema(conn)
    from nigeria_bot.special_effects import setup_schema as effects_schema
    await effects_schema(conn)
    cog = SpecialCog(bot, conn)
    await bot.add_cog(cog)
    return cog
