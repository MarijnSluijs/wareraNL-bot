"""Timed effects, hidden traps, and the settlement order they resolve in.

This module is the *seam* between /special and the rest of the game.  The scam,
target, fund, beg and jail systems call into here at a handful of well-defined
moments and get back a modified number plus some lines to print; they never
need to know which cards exist.

Two storage shapes cover every card:

``special_effects``   a timed thing attached to somebody — a buff on its owner
                      (Lucky Man), a debuff on a victim (Burn Notice), or a
                      global switch with no subject at all (Fog of War).
``special_traps``     an armed one-shot waiting for somebody *else* to do
                      something.  Traps are hidden while armed and public when
                      they fire.

Both persist absolute UTC timestamps, so a restart mid-effect changes nothing.

Import direction
----------------
This module imports scam_game at module level; scam_game imports *this* one
lazily inside its hook functions.  That keeps the cycle from forming while
letting the effects code use the ordinary money helpers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

from nigeria_bot.scam_game import (
    _iso,
    _now,
    _parse,
    adjust_balance,
    money,
    record_ledger,
)

logger = logging.getLogger("nigeria_bot.special_effects")


# ── Schema ────────────────────────────────────────────────────────────────────

async def setup_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS special_effects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            owner_id    TEXT,              -- who benefits / who cast it
            subject_id  TEXT,              -- who it is applied to (NULL = global)
            created_at  TEXT NOT NULL,
            expires_at  TEXT,              -- NULL = until consumed
            charges     INTEGER,           -- NULL = unlimited within the window
            payload     TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'active'
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS special_effects_lookup"
        " ON special_effects (kind, status, subject_id)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS special_traps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            owner_id    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            payload     TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'armed'
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS special_traps_lookup"
        " ON special_traps (kind, status)"
    )
    await conn.commit()


# ── Effects ───────────────────────────────────────────────────────────────────

async def add_effect(
    conn: aiosqlite.Connection, kind: str, *,
    owner_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    minutes: Optional[float] = None,
    hours: Optional[float] = None,
    charges: Optional[int] = None,
    **payload,
) -> int:
    expires = None
    if hours is not None:
        expires = _iso(_now() + timedelta(hours=hours))
    elif minutes is not None:
        expires = _iso(_now() + timedelta(minutes=minutes))
    cur = await conn.execute(
        "INSERT INTO special_effects"
        " (kind, owner_id, subject_id, created_at, expires_at, charges, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, owner_id, subject_id, _iso(_now()), expires, charges,
         json.dumps(payload)),
    )
    return int(cur.lastrowid)


async def get_effect(
    conn: aiosqlite.Connection, kind: str, *,
    subject_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> Optional[dict]:
    """The live effect of this kind, or None.

    Expiry is evaluated here rather than by a sweeper: an effect whose window
    has passed is simply not returned, so a bot that was down for an hour
    cannot leak an extra hour of protection.
    """
    sql = ["SELECT id, kind, owner_id, subject_id, created_at, expires_at,"
           " charges, payload FROM special_effects WHERE kind = ?"
           " AND status = 'active'"]
    args: list = [kind]
    if subject_id is not None:
        sql.append("AND subject_id = ?")
        args.append(str(subject_id))
    if owner_id is not None:
        sql.append("AND owner_id = ?")
        args.append(str(owner_id))
    sql.append("ORDER BY id DESC")
    async with conn.execute(" ".join(sql), args) as cur:
        rows = await cur.fetchall()
    for row in rows:
        if row[5] and _parse(row[5]) <= _now():
            await conn.execute(
                "UPDATE special_effects SET status = 'expired' WHERE id = ?",
                (row[0],),
            )
            continue
        return {
            "id": int(row[0]), "kind": row[1], "owner_id": row[2],
            "subject_id": row[3], "created_at": row[4], "expires_at": row[5],
            "charges": row[6], "payload": json.loads(row[7] or "{}"),
        }
    return None


async def global_effect(conn: aiosqlite.Connection, kind: str) -> Optional[dict]:
    """A switch with no subject: Fog of War, Unleash the Muggers."""
    return await get_effect(conn, kind, subject_id=None)


async def consume_effect(conn: aiosqlite.Connection, effect_id: int) -> None:
    await conn.execute(
        "UPDATE special_effects SET status = 'consumed' WHERE id = ?",
        (effect_id,),
    )


async def spend_charge(conn: aiosqlite.Connection, effect: dict) -> int:
    """Burn one charge; return how many remain.  Zero ends the effect."""
    left = int(effect["charges"] or 1) - 1
    if left <= 0:
        await consume_effect(conn, effect["id"])
        return 0
    await conn.execute(
        "UPDATE special_effects SET charges = ? WHERE id = ?",
        (left, effect["id"]),
    )
    return left


async def end_effect(conn: aiosqlite.Connection, kind: str, **where) -> None:
    effect = await get_effect(conn, kind, **where)
    if effect:
        await consume_effect(conn, effect["id"])


# ── Traps ─────────────────────────────────────────────────────────────────────

async def arm_trap(
    conn: aiosqlite.Connection, kind: str, owner_id: str, *,
    hours: float, **payload,
) -> int:
    cur = await conn.execute(
        "INSERT INTO special_traps (kind, owner_id, created_at, expires_at, payload)"
        " VALUES (?, ?, ?, ?, ?)",
        (kind, str(owner_id), _iso(_now()),
         _iso(_now() + timedelta(hours=hours)), json.dumps(payload)),
    )
    return int(cur.lastrowid)


async def trap_armed(conn: aiosqlite.Connection, kind: str) -> Optional[dict]:
    """Peek at an armed trap without firing it (used for eligibility)."""
    async with conn.execute(
        "SELECT id, kind, owner_id, expires_at, payload FROM special_traps"
        " WHERE kind = ? AND status = 'armed' ORDER BY id ASC", (kind,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        if _parse(row[3]) <= _now():
            await conn.execute(
                "UPDATE special_traps SET status = 'expired' WHERE id = ?",
                (row[0],),
            )
            continue
        return {"id": int(row[0]), "kind": row[1], "owner_id": str(row[2]),
                "expires_at": row[3], "payload": json.loads(row[4] or "{}")}
    return None


async def take_trap(
    conn: aiosqlite.Connection, kind: str, victim_id: str
) -> Optional[dict]:
    """Claim a trap for this victim, atomically, or return None.

    The ARMED -> TRIGGERED flip happens *before* any money moves (spec §18), so
    two simultaneous qualifying actions cannot both fire the same one-shot.
    A trap never catches its own owner.
    """
    trap = await trap_armed(conn, kind)
    if trap is None or trap["owner_id"] == str(victim_id):
        return None
    cur = await conn.execute(
        "UPDATE special_traps SET status = 'triggered'"
        " WHERE id = ? AND status = 'armed'", (trap["id"],),
    )
    if cur.rowcount != 1:
        return None
    return trap


# ── Money helpers with explicit floors ────────────────────────────────────────

async def wealth_of(conn: aiosqlite.Connection, user_id: str) -> tuple[int, int]:
    async with conn.execute(
        "SELECT balance, invested FROM scam_players WHERE discord_user_id = ?",
        (str(user_id),),
    ) as cur:
        row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


async def take_cash(
    conn: aiosqlite.Connection, victim: str, amount: int, *,
    floor: int = 0, reason: str = "special_loss", detail: Optional[str] = None,
) -> int:
    """Cash-only loss that never breaches ``floor``.  Returns what was taken."""
    cash, _fund = await wealth_of(conn, victim)
    take = max(0, min(int(amount), cash - floor))
    if take <= 0:
        return 0
    await adjust_balance(conn, victim, -take, reason, detail)
    return take


async def take_wealth(
    conn: aiosqlite.Connection, victim: str, amount: int, *,
    floor: int = 0, reason: str = "special_loss", detail: Optional[str] = None,
) -> int:
    """Cash first, then forced fund liquidation, never breaching ``floor``.

    Mirrors the PvP seizure the target board already uses, but takes the floor
    as an argument: /special cards protect at 1.000, 2.500 and 5.000 depending
    on the card, and a single hardcoded floor cannot serve all three.
    """
    cash, fund = await wealth_of(conn, victim)
    take = max(0, min(int(amount), (cash + fund) - floor))
    if take <= 0:
        return 0
    from_cash = min(cash, take)
    from_fund = take - from_cash
    await conn.execute(
        "UPDATE scam_players SET balance = balance - ?, invested = invested - ?"
        " WHERE discord_user_id = ?",
        (from_cash, from_fund, str(victim)),
    )
    # Logged by hand: the cash half bypassed adjust_balance and the fund half
    # never touches cash at all, so neither would otherwise reach the ledger.
    await record_ledger(conn, victim, -take, reason, detail)
    if from_fund:
        # A forced sale, not a fund loss: the money left the position but the
        # fund did not lose it — another player took it.  Booking it as
        # principal keeps `/fundluck` from blaming Roger for a mugging.
        from nigeria_bot import royal_fund as rf
        await rf.record_pnl(conn, victim, -from_fund,
                            f"Forced liquidation — {detail or reason}",
                            kind="withdraw")
    return take


async def give_cash(
    conn: aiosqlite.Connection, user_id: str, amount: int, *,
    reason: str = "special_gain", detail: Optional[str] = None,
) -> None:
    if amount > 0:
        await adjust_balance(conn, user_id, int(amount), reason, detail)


# ── Settlement: rewards ───────────────────────────────────────────────────────
# Spec §5.1/§5.2.  Order is fixed: destruction first, then percentage
# interception, then fixed interception.  Nothing may intercept more than
# what is actually left.

REWARD_KINDS_TRAPPABLE = {"scam", "target"}   # what Highwayman/Counterfeit see


async def on_reward(
    conn: aiosqlite.Connection, user_id: str, gross: int, *,
    kind: str, detail: Optional[str] = None,
) -> tuple[int, list[str]]:
    """Run a payout through every modifier that can touch it.

    ``kind`` is one of ``scam``, ``target``, ``quickscam``, ``operation``.
    Returns ``(net, public_lines)``.  The caller pays out the net and prints
    the lines; it does not need to know what hit it.
    """
    uid = str(user_id)
    net = int(gross)
    lines: list[str] = []
    if net <= 0:
        return net, lines

    # 1) Burn Notice — source-side destruction, before anyone intercepts.
    burn = await get_effect(conn, "burn_notice", subject_id=uid)
    if burn and kind in {"scam", "target", "quickscam", "operation"}:
        destroyed = net // 2
        if destroyed > 0:
            net -= destroyed
            left = await spend_charge(conn, burn)
            lines.append(
                f"🔥 **BURN NOTICE** — {money(destroyed)} of that was destroyed "
                f"before it reached you. Charges remaining: **{left}**"
            )

    # 2) Highwayman — percentage interception, capped.
    if kind in REWARD_KINDS_TRAPPABLE and net > 0:
        trap = await take_trap(conn, "highwayman", uid)
        if trap:
            stolen = min(net // 2, 2_500)
            if stolen > 0:
                net -= stolen
                await give_cash(conn, trap["owner_id"], stolen,
                                reason="special_highwayman", detail=detail)
                lines.append(
                    f"🏴‍☠️ **HIGHWAY ROBBERY** — <@{trap['owner_id']}> was waiting "
                    f"beside the road and took **{money(stolen)}**."
                )

    # 3) Counterfeit Naira — fixed interception, only above the threshold.
    if kind in REWARD_KINDS_TRAPPABLE and gross > 1_000 and net > 0:
        trap = await take_trap(conn, "counterfeit_naira", uid)
        if trap:
            stolen = min(500, net)
            net -= stolen
            await give_cash(conn, trap["owner_id"], stolen,
                            reason="special_counterfeit", detail=detail)
            lines.append(
                f"💵 **COUNTERFEIT NAIRA** — {money(stolen)} of your earnings "
                "turned out to be printed in Microsoft Paint. "
                f"<@{trap['owner_id']}> has the real ones."
            )

    return max(0, net), lines


# ── Settlement: losses ────────────────────────────────────────────────────────

async def on_loss(
    conn: aiosqlite.Connection, user_id: str, base_loss: int, *,
    domain: str = "wealth", involuntary: bool = True,
    detail: Optional[str] = None,
) -> list[str]:
    """Apply the extra-copy modifiers to a loss that has *already* happened.

    Each modifier destroys one further copy of the ORIGINAL loss — additive,
    never compounding, and each stopping at its own floor (spec §5.1).  With
    both Prince and Grudge live, a 1.000 theft costs the victim at most 3.000,
    not 4.000, and the attacker still receives only the original 1.000.
    """
    uid = str(user_id)
    lines: list[str] = []
    if base_loss <= 0 or not involuntary:
        return lines

    take = take_cash if domain == "cash" else take_wealth

    prince = await get_effect(conn, "prince_for_a_day", subject_id=uid)
    if prince:
        extra = await take(conn, uid, base_loss, floor=2_500,
                           reason="special_prince_tax", detail=detail)
        if extra > 0:
            lines.append(
                f"👑 **Royal Vulnerability** — a further {money(extra)} was "
                "destroyed on the way out."
            )

    grudge = await get_effect(conn, "personal_grudge", subject_id=uid)
    if grudge:
        extra = await take(conn, uid, base_loss, floor=5_000,
                           reason="special_grudge", detail=detail)
        if extra > 0:
            lines.append(
                f"😡 **Personal Grudge** — a further {money(extra)} was "
                "destroyed. Somebody really does not like you."
            )
    return lines


# Spec §5.3: what the insurance policy will and will not touch.  Named
# explicitly because "involuntary" alone is too loose — a quick scam stake is
# involuntarily lost but was voluntarily placed.
INSURED_KINDS = {"scam", "target_penalty", "npc_penalty"}


async def on_insurable_loss(
    conn: aiosqlite.Connection, user_id: str, amount: int, *, kind: str,
) -> Optional[int]:
    """Reimburse a covered system/NPC loss.  Returns the amount paid, or None.

    Deliberately narrow: PvP transfers, extra-copy destruction, activation
    costs, stakes, deposits, donations and bribes are all excluded, so the
    policy cannot be farmed by losing money on purpose.
    """
    if amount <= 0 or kind not in INSURED_KINDS:
        return None
    policy = await get_effect(conn, "insurance", subject_id=str(user_id))
    if not policy:
        return None
    await give_cash(conn, user_id, amount, reason="special_insurance", detail=kind)
    return amount


# ── Hook: arrests ─────────────────────────────────────────────────────────────

async def on_arrest(conn: aiosqlite.Connection, user_id: str) -> bool:
    """Spend a Get Out of Jail Free card if the player holds one.

    The arrest is allowed to happen first and is then undone, because the
    public arrest message is half the fun and the card's own message reads as
    a reply to it.
    """
    from nigeria_bot.scam_game import release_player

    card = await get_effect(conn, "jail_card", subject_id=str(user_id))
    if not card:
        return False
    await consume_effect(conn, card["id"])
    await release_player(conn, user_id)
    return True


# ── Hook: display ─────────────────────────────────────────────────────────────

async def odds_hidden(conn: aiosqlite.Connection) -> bool:
    """Fog of War.  A pure renderer question — no target state is touched."""
    return await global_effect(conn, "fog_of_war") is not None


async def fund_frozen(conn: aiosqlite.Connection, user_id: str) -> Optional[dict]:
    return await get_effect(conn, "asset_freeze", subject_id=str(user_id))
