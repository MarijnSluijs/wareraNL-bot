"""Shared marks — the second game mode of the Nigerian Scam-Economy.

Three targets sit on a public board at any time.  Everyone works the *same*
three, which is what makes it social even when players are hours apart:

* A **failed** attempt hands your stake to that target's pot, so the next
  person to try inherits a fatter prize — paid for by your mistake.
* Each failure also makes the mark warier, so the pot grows exactly as the
  odds get worse.  Somebody eventually has to decide the risk is worth it.
* A **success** takes the mark off the board, pot included.  Whoever strikes
  can walk off with a pot other people funded.
* Too many failures and the mark reports it: the pot is destroyed and nobody
  gets it.  A pot can never grow forever.

Occasionally a **whale** appears: enormous payout, terrible odds, and a
countdown before it vanishes.

When a target leaves the board its slot goes quiet for a while before a new
mark turns up (see :data:`RESPAWN_MIN_MINUTES`), so the board isn't an
infinite conveyor belt.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from nigeria_bot.scam_game import (
    GAME_CHANNEL_ID,
    GAME_CHANNEL_URL,
    INTEL_MAX_CHARGES,
    INTEL_PER_DAY,
    INTEL_RECHARGE_HOURS,
    TARGET_ATTEMPT_COOLDOWN,
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
    require_free,
    get_player,
    get_jail,
    arrest_player,
    EXTREME_FAILURE_MIN_BRIBE,
    record_ledger,
    record_play,
    money,
)
from nigeria_bot import special_effects as fx


async def _touch(conn, user_id: str) -> None:
    """Mark this player as recently active for the /special targeting windows."""
    from nigeria_bot.special_game import touch
    await touch(conn, user_id)

logger = logging.getLogger("nigeria_bot.scam_targets")

# ── Configuration ─────────────────────────────────────────────────────────────

BOARD_SLOTS = 3

# A slot stays empty this long after its mark leaves, so the board breathes
# instead of instantly replacing everything.
RESPAWN_MIN_MINUTES = 12
RESPAWN_MAX_MINUTES = 40

# Per-player pause between attempts.  Without this one rich player could work
# the whole board alone; the point is that marks are shared.
# Defined in scam_game so /scamrules can quote it without a circular import.
ATTEMPT_COOLDOWN_MINUTES = TARGET_ATTEMPT_COOLDOWN

# Every failure makes the mark this much warier (percentage points).
SUSPICION_PER_FAILURE = 0.07
# ...but never quite impossible.
MIN_CHANCE = 0.05

# The board gets buried when the channel is busy.  If nobody has worked a mark
# for a while *and* enough messages have pushed the board out of view, it is
# re-posted so people can actually see it again.  Both conditions must hold —
# bumping a board that is still on screen would just be spam.
BUMP_AFTER_MESSAGES = 8
BUMP_IDLE_MINUTES   = 20

# ── Fake targets (asynchronous PvP) ───────────────────────────────────────────
# A player can pose as a mark.  On the board they are indistinguishable from a
# real one — that is the whole point — so everything a fake needs (profile,
# odds, investigation bonus) lives in the same table as a genuine target.
FAKE_TARGET_COOLDOWN_HOURS = 6
FAKE_TARGET_DURATION_MIN   = 180        # 3 hours, or the persona's own expiry
FAKE_COVER_DEPOSIT         = 500
FAKE_QUEUE_TIMEOUT_MIN     = 60
FAKE_CANCEL_WINDUP_MIN     = 5
# The board has three slots and at least one of them is always genuine.
MAX_ACTIVE_FAKES           = 2

# A blind attacker's chance of smelling a rat and walking away, by approach.
BLIND_ESCAPE = {"careful": 0.30, "normal": 0.25, "greedy": 0.20}
# Public Intel helps even when the mark turns out to be fake, so buying Intel
# is never wasted: half the public bonus, capped.
BLIND_ESCAPE_INTEL_SHARE = 0.50
BLIND_ESCAPE_INTEL_CAP   = 0.10

# A successful fake takes this share of the victim's exposed wealth...
FAKE_THEFT_PCT = (0.30, 0.50)
# ...capped by the tier it was *disguised* as, so a fake Vatou cannot steal
# whale money while pretending to be a 400-Naira mark.
FAKE_THEFT_CAP = {
    "ordinary": 1_500, "great_catch": 2_500, "rare": 4_000, "whale": 7_500,
}
# Counter-scam outcomes.
COUNTER_WIN_THEFT   = (0.25, 0.40)
COUNTER_WIN_CAP     = 5_000
COUNTER_LOSS_THEFT  = (0.20, 0.30)
COUNTER_LOSS_CAP    = 3_000
# PvP never takes a player below this in cash + fund combined.
PROTECTED_WEALTH = 1_000
# What a caught impersonator owes to get back out.  Pitched at the cover
# deposit: being unmasked should cost about what the disguise cost to set up,
# on top of losing the deposit itself.
FAKE_ARREST_BRIBE = 500

# ── Intel ─────────────────────────────────────────────────────────────────────
# Intel is a limited resource rather than a cooldown.  Charges regenerate
# serially, so spending all three costs a full recharge cycle each.
#
# The cap stays at the number of board slots: scouting the whole board once is
# a legitimate opening move, and holding more than there are marks to spend it
# on would only mean hoarding.  The *rate* is what rations Intel — at one
# charge every four hours a player gets six a day rather than twelve.
#
# There used to be a two-minute lock on target actions after each mission as
# well.  It was removed: two minutes never changed a decision, it just made
# people sit and wait, and being told "your investigators are still returning"
# right after paying for intel reads as a punishment for using the feature.
# The daily budget carries the whole cost now.
# INTEL_MAX_CHARGES, INTEL_RECHARGE_HOURS and INTEL_PER_DAY are imported from
# scam_game: /scamrules quotes them and that module cannot import this one.
# A mark may only be scouted twice, ever, and only once per player.  Without
# this a group could buy identity checks until a fake was guaranteed exposed.
INTEL_MISSIONS_PER_TARGET = 2
# Refuse Intel on a mark that will leave before the investigators get back.
INTEL_MIN_REMAINING_SECONDS = 120

INTEL_COST = {
    "ordinary": 75, "great_catch": 125, "rare": 200, "whale": 300,
    "legendary": 0,          # unavailable
}
# Base public odds gained, per tier.
INTEL_GAIN = {
    "ordinary": (0.08, 0.15), "great_catch": (0.06, 0.12),
    "rare": (0.05, 0.10), "whale": (0.03, 0.07),
}
INTEL_BREAKTHROUGH_CHANCE = {
    "ordinary": 0.05, "great_catch": 0.08, "rare": 0.12, "whale": 0.10,
}
INTEL_BREAKTHROUGH_BONUS = {
    "ordinary": (0.05, 0.10), "great_catch": (0.08, 0.12),
    "rare": (0.10, 0.15), "whale": (0.12, 0.18),
}
# Total public bonus a single mark can ever accumulate.  Keeps a 4% whale from
# turning into a money printer while leaving a breakthrough worth having.
INTEL_CAP = {
    "ordinary": 0.25, "great_catch": 0.22, "rare": 0.20, "whale": 0.18,
    "legendary": 0.0,
}
# Intel may lift an approach to 95%, but never *lowers* one that is already
# naturally above it.
INTEL_ODDS_CEILING = 0.95

# Identity report classes: (key, share, reliability)
INTEL_REPORTS = (
    ("verified",     0.20, 1.00),
    ("strong",       0.60, 0.80),
    ("inconclusive", 0.20, None),
)
COUNTER_TAKEDOWN_VERIFIED = 0.60
COUNTER_TAKEDOWN_STRONG   = 0.50   # ×0.80 reliability = 40% overall
COUNTER_STAKE_MIN         = 100

# ── Tiers ─────────────────────────────────────────────────────────────────────
# Every mark belongs to one of five tiers.  The tier is what makes a mark's
# quality legible at a glance, and it is also where system-wide rules live —
# whale expiry, Intel prices, Intel caps and fake-theft caps are all keyed off
# it rather than being duplicated into every persona.

TIERS = ("ordinary", "great_catch", "rare", "whale", "legendary")

TIER_LABEL = {
    "ordinary":    "🟢 ORDINARY",
    "great_catch": "🔵 GREAT CATCH",
    "rare":        "🟣 RARE",
    "whale":       "🐋 WHALE",
    "legendary":   "🦄 LEGENDARY",
}
TIER_EMOJI = {
    "ordinary": "🟢", "great_catch": "🔵", "rare": "🟣",
    "whale": "🐋", "legendary": "🦄",
}
TIER_WEIGHTS = [
    ("ordinary", 45), ("great_catch", 30), ("rare", 15),
    ("whale", 8), ("legendary", 4),
]
_TIER_COLOUR = {
    "ordinary":    discord.Colour(0x2ECC71),
    "great_catch": discord.Colour(0x3498DB),
    "rare":        discord.Colour(0x9B59B6),
    "whale":       discord.Colour(0x1ABC9C),
    "legendary":   discord.Colour(0xF1C40F),
}

# Whales are a fleeting opportunity, not a permanent fixture.  One hour, flat —
# failed attempts and Intel never reset it.
WHALE_EXPIRY_MINUTES = 60
EXPIRING_TIERS = ("whale",)
# Below this the countdown is shown with a warning marker.
WHALE_URGENT_MINUTES = 10

# Default careful payout multiplier by tier.  Careful used to sit at ×0.50–0.55
# across the roster, which made it almost never worth taking; ×0.70 keeps it
# visibly weaker than Normal while making "safer" a real option.
TIER_CAREFUL_MULT = {
    "ordinary": 0.70, "great_catch": 0.70, "rare": 0.70,
    "whale": 0.60, "legendary": 0.70,
}

# approach → (chance shift, payout multiplier, label, icon)
# The shift/multiplier fallback is only used by marks with no per-approach
# config of their own; every persona in the roster carries its own.
APPROACHES: dict[str, tuple[float, float, str]] = {
    "careful": (+0.15, 0.70, "Careful"),
    "normal":  (0.00, 1.00, "Normal"),
    "greedy":  (-0.20, 2.00, "Greedy"),
}
APPROACH_ICON = {"careful": "🛡️", "normal": "🎯", "greedy": "🤑"}

_STATUS_LABELS = [
    (0, "Fresh mark"),
    (1, "Slightly wary"),
    (2, "Suspicious"),
    (3, "Very suspicious"),
    (4, "About to call the police"),
]


def status_label(failures: int) -> str:
    label = _STATUS_LABELS[0][1]
    for threshold, text in _STATUS_LABELS:
        if failures >= threshold:
            label = text
    return label


# ── Target archetypes ─────────────────────────────────────────────────────────
# Each mark is a full profile: who they are, and what actually happens when a
# scam lands or falls flat.  The per-outcome text is what makes a hit feel like
# a scene rather than a dice roll, so every archetype carries its own.

_ARCH_FIELDS = (
    "emoji", "name", "full_name", "age", "location", "description", "status",
    "tier", "chance", "payout_min", "payout_max", "attempt_cost",
    "max_failures", "success_text", "failure_text",
)

# Optional per-mark mechanics.  The originals all behave the same way — a
# single chance, shifted by the global APPROACHES table, decaying as the mark
# gets suspicious.  The newer personas each bend one of those rules, and the
# point of them is that they bend *different* ones.
_ARCH_DEFAULTS = {
    # A short label for what makes this mark exploitable, shown on the board.
    "trait": None,
    # Country flag shown on the card header.
    "flag": "",
    # Short label for the (disabled) row-header button, which is narrow.
    "short_name": None,
    # Bullets rendered in the card's ⚙️ SPECIAL RULES box.  A mechanic that
    # matters to a decision must appear here — it may never live only in code.
    "special_rules": (),
    # Per-approach payout *ranges*, replacing the multiplier formula entirely.
    # Only Darkodor uses this: a legendary jackpot is not a multiple of an
    # ordinary payout.
    "approach_payouts": None,
    # Intel cannot touch this mark at all.
    "intel_immune": False,
    # Whether a player posing as a fake may be handed this persona.  Marks
    # whose mechanic needs repeated attempts cannot work as a fake, because a
    # fake resolves on the first attack.
    "fake_eligible": True,
    # Chance a failed attempt triggers the mark's own counter-scam (Chinedu).
    "npc_counter_chance": 0.0,
    "npc_counter_steal": (0, 0),

    # ── expansion-pack mechanics ──
    # {approach: [odds after 0 failures, after 1, …]} — a per-approach ladder.
    # Covers marks that harden (Beitsas' rage) and marks that suddenly open up
    # (Merel's sniper window) with one mechanism.
    "approach_ladder": None,
    # Weighted payout bands per approach, replacing the multiplier entirely:
    # {approach: [(lo, hi, weight), …]}.  Utopia's account roulette.
    "payout_bands": None,
    # This mark never accumulates a pot from failed attempts.
    "no_pot": False,
    # Fraction of *total* wealth (cash + fund) destroyed by a failure.
    "wealth_loss_pct": 0.0,
    # A failure jails you regardless of whether you could pay.
    "always_arrest": False,
    # (chance, lo, hi) — the mark robs you back out of cash on a failure.
    "reverse_scam": None,
    # Flat bonus paid on a successful attempt made in the final-attempt state.
    "final_bonus": 0,
    # Naira already sitting in the pot when this mark appears.
    "seed_pot": 0,
    # ({approach: chance}, (lo, hi)) — failures feed cash into the pot, but
    # never on the first attempt.
    "collateral": None,
    # Flat bonus per failure so far, paid to whoever finally succeeds.
    "bonus_per_failure": 0,
    # (fraction, cap) skimmed off every *other* mark's payout while active.
    "shadow_network": None,
    # Overrides the tier's expiry (MVC escapes in two hours, not one).
    "expiry_minutes": 0,
    # ({approach: carrot chance}, (carrot lo, hi), (stick lo, hi))
    "carrot_stick": None,
    # (chance, lo, hi) — a bonus rolled on success.
    "success_bonus": None,
    # Success refills the player's Intel charges to full.
    "intel_refill": False,
    # A failure blocks *every* target attempt for this many minutes.
    "global_lock_minutes": 0,
    # Success silences the player in the game channel for this many minutes.
    "silence_minutes": 0,
    # (other arch_id, bonus) — extra odds while that mark is also on the board.
    "rival": None,
    # Relative likelihood of being picked *within its own tier*.  Everything
    # defaults to 1.0, which reproduces the old uniform draw exactly; only a
    # mark that should be conspicuously more or less common than its tier-mates
    # needs to say so.
    "spawn_weight": 1.0,
    # Success clears the player's own action cooldowns (Marijn's mod powers).
    # Deliberately narrow: punishments and shared timers are never touched.
    "reset_cooldowns": False,
    # A successful takedown puts the whole board on Heat for this long.
    "board_heat_minutes": 0,
    # {approach: (chance, payout multiplier, flavour emoji)}.  How `chance` is
    # read depends on `approach_mode`:
    #   "absolute" — it *is* the success chance
    #   "multiply" — multiplied onto the mark's current base chance
    #   "shift"    — added to the mark's current base chance
    "approaches": None,
    "approach_mode": "absolute",
    # Base chance after 0, 1, 2, … failures.  Lets a mark get harder (Koen
    # waking up) or easier (Sachiko forgetting) instead of just decaying.
    "chance_ladder": None,
    "chance_cap": 0.95,
    # What a failed attempt adds to the mark's pot.  Defaults to the attempt
    # cost, i.e. the money actually lost; Gerard pays out more than goes in,
    # which is the compensation for how long he takes.
    "pot_per_failure": None,
    # Any attempt at all removes the mark, win or lose.
    "one_shot": False,
    # False for marks with fixed per-approach odds: their limit is the attempt
    # count, not rising suspicion.
    "decays": True,
    # Flavour for the failure report when the odds move in the player's favour.
    "warms_up": False,
}

# Relative likelihood within the rare pool (ignored for ordinary tiers).
_RARE_WEIGHTS = {
    "Crypto whale": 30,
    "Yacht owner": 30,
    "King Nicolas": 26,
    "Prince Akwabi": 8,   # ultra-rare
    "Admiral Ape": 22,
    "Henk de Postzegelkoning": 18,
}


def _slug(name: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


def _arch(**kw) -> dict:
    missing = [f for f in _ARCH_FIELDS if f not in kw]
    if missing:
        raise ValueError(f"archetype missing {missing}")
    unknown = [
        k for k in kw
        if k not in _ARCH_FIELDS and k not in _ARCH_DEFAULTS and k != "arch_id"
    ]
    if unknown:
        raise ValueError(f"archetype {kw['name']!r} has unknown {unknown}")
    out = {**_ARCH_DEFAULTS, **kw}
    out.setdefault("arch_id", _slug(kw["name"]))
    if out["pot_per_failure"] is None:
        out["pot_per_failure"] = out["attempt_cost"]
    if out["tier"] not in TIERS:
        raise ValueError(f"archetype {out['name']!r} has unknown tier {out['tier']!r}")
    if out["short_name"] is None:
        out["short_name"] = out["name"].split()[0]
    # Careful's multiplier is a tier-level rule, so it cannot drift persona by
    # persona.  Absolute-mode marks carry (chance, mult, emoji) triples; the
    # dynamic modes carry (modifier, mult, emoji).  Either way slot 1 is the
    # payout multiplier and gets normalised.
    if out["approaches"] and out["approach_payouts"] is None:
        c = out["approaches"]["careful"]
        out["approaches"] = dict(out["approaches"])
        out["approaches"]["careful"] = (c[0], TIER_CAREFUL_MULT[out["tier"]], c[2])
    # A whale is a whale everywhere: one hour, always shown.
    if out["tier"] in EXPIRING_TIERS:
        out["special_rules"] = tuple(out["special_rules"]) + (
            f"🐋 Whale target — leaves the board {WHALE_EXPIRY_MINUTES} minutes "
            "after appearing, whatever else happens.",
        )
    return out


_ARCHETYPES: list[dict] = [
    _arch(
        emoji="🚜", name="Boer Geert", full_name="Geert-Jan Harmsen",
        age=63, location="Varsseveld", tier="rare",
        description=(
            "An old-school farmer from the Achterhoek who distrusts banks, "
            "politicians, consultants, and anyone without a tractor. He owns "
            "valuable machinery, prefers cash deals, and believes every problem "
            "can be solved with diesel."
        ),
        status="Highly suspicious",
        chance=0.28, payout_min=1000, payout_max=3200,
        attempt_cost=250, max_failures=3,
        success_text=(
            "You tell Geert that the Nigerian royal family urgently needs a "
            "reliable Dutch tractor for a ceremonial state visit.\n\n"
            "He refuses to believe a word of your story, but becomes interested "
            "when you say the payment can be kept off the books."
        ),
        failure_text=(
            "You ask Geert to pay royal administration fees before the tractor "
            "can be exported.\n\nGeert asks whether the fees can be paid in red "
            "diesel. When you say no, he calls you an amateur and blocks your "
            "number."
        ),
        flag="🇳🇱",
        short_name="Geert",
        trait="🚜 Cash & Diesel",
        decays=False,
        approaches={"careful": (0.4, 0.7, "🧾"), "normal": (0.28, 1.0, "🚜"), "greedy": (0.14, 2.0, "💰")},
    ),
    _arch(
        emoji="🎓", name="Fleur the first-year", full_name="Fleur de Jong",
        age=19, location="Utrecht", tier="ordinary",
        description=(
            "A first-year communication student who has lived in Utrecht for "
            "three weeks and already calls herself a local. She has very little "
            "money, but enters every giveaway she sees on Instagram."
        ),
        status="Easily excited",
        chance=0.78, payout_min=80, payout_max=300,
        attempt_cost=25, max_failures=5,
        success_text=(
            "You tell Fleur that she has won an all-inclusive influencer trip "
            "to Lagos.\n\nShe only needs to pay a small fee for “royal airport "
            "verification”, and immediately posts about the trip on her "
            "Instagram Story."
        ),
        failure_text=(
            "You tell Fleur she has won a luxury festival trip to Nigeria.\n\n"
            "Unfortunately she has 14 Naira left until her student finance "
            "arrives. She asks whether she can pay you in exposure instead."
        ),
        flag="🇳🇱",
        short_name="Fleur",
        trait="📱 Giveaway Addict",
        decays=False,
        approaches={"careful": (0.9, 0.7, "🎁"), "normal": (0.78, 1.0, "📱"), "greedy": (0.58, 1.5, "✈️")},
    ),
    _arch(
        emoji="📈", name="Crypto Mats", full_name="Mats van der Velde",
        age=28, location="Amsterdam", tier="whale",
        description=(
            "A self-proclaimed crypto millionaire, business coach, and founder "
            "of six companies with no functioning website. His profile picture "
            "was taken next to a rented Lamborghini in Dubai."
        ),
        status="Looking for the next moonshot",
        chance=0.24, payout_min=1800, payout_max=5500,
        attempt_cost=500, max_failures=2,
        success_text=(
            "You offer Mats early access to **RoyalNaira Inu**, a revolutionary "
            "AI-powered Web3 currency backed by Nigerian diamonds.\n\nAfter "
            "hearing the words “exclusive”, “presale” and “100x potential”, he "
            "transfers the money without asking a single useful question."
        ),
        failure_text=(
            "Mats discovers that RoyalNaira Inu has no white paper, no roadmap "
            "and no development team.\n\nHe does not expose the scam. He is "
            "simply angry because he planned to launch the exact same coin next "
            "week."
        ),
        flag="🇳🇱",
        short_name="Mats",
        trait="🚀 Moonshot Brain",
        decays=False,
        approaches={"careful": (0.18, 0.6, "📊"), "normal": (0.24, 1.0, "📈"), "greedy": (0.38, 1.6, "🚀")},
        special_rules=(
            "Mats is **more** vulnerable to Greedy than to Normal or Careful.",
            "“Exclusive”, “presale” and “100x” are his weakness.",
        ),
    ),
    _arch(
        emoji="🛒", name="Henk from Marktplaats", full_name="Henk de Bruin",
        age=54, location="Amersfoort", tier="great_catch",
        description=(
            "Sells a Gazelle bicycle as “almost new”, even though the front "
            "wheel is missing in every photo. Negotiates over every euro and "
            "trusts nobody who starts a message with “Hello friend”."
        ),
        status="Open to offers, suspicious of everyone",
        chance=0.46, payout_min=450, payout_max=1000,
        attempt_cost=125, max_failures=4,
        success_text=(
            "You tell Henk that a Nigerian royal collector wants authentic Dutch "
            "bicycles for a private museum.\n\nHe is not interested until you "
            "offer the full asking price without negotiating, then pays a "
            "“refundable international reservation fee”."
        ),
        failure_text=(
            "You ask Henk to pay a small administration fee before the royal "
            "courier can collect his bicycle.\n\nHenk replies: “Scammer. Cash "
            "only. Pick-up only. Final price.” He then sends you an offer 25 "
            "euros below your own asking price."
        ),
        flag="🇳🇱",
        short_name="Henk M.",
        trait="🛒 Cash Only, Pick-Up Only",
        decays=False,
        approaches={"careful": (0.58, 0.7, "🤝"), "normal": (0.46, 1.0, "🛒"), "greedy": (0.2, 1.8, "💶")},
    ),
    _arch(
        emoji="👵", name="Gerda de Vries", full_name="Gerda de Vries",
        age=78, location="Apeldoorn", tier="ordinary",
        description=(
            "Collects Douwe Egberts points, always answers the telephone, and "
            "still keeps emergency cash in a biscuit tin. She appears easy to "
            "fool, but watches *Radar* and *Opsporing Verzocht* every week."
        ),
        status="Friendly but experienced",
        chance=0.7, payout_min=180, payout_max=500,
        attempt_cost=75, max_failures=4,
        success_text=(
            "You pretend to be Gerda's grandson and claim you urgently need "
            "money for a new telephone.\n\nGerda finds it strange that her "
            "grandson suddenly has a Nigerian accent, but pays anyway because "
            "“young people are always having technical problems”."
        ),
        failure_text=(
            "Gerda keeps you talking for twenty minutes and asks detailed "
            "questions about the family.\n\nYou eventually discover she already "
            "called the police from her landline while pretending not to "
            "understand you."
        ),
        flag="🇳🇱",
        short_name="Gerda",
        trait="☎️ Friendly but Experienced",
        decays=False,
        approaches={"careful": (0.82, 0.7, "🍪"), "normal": (0.7, 1.0, "☎️"), "greedy": (0.4, 1.7, "💸")},
    ),
    _arch(
        emoji="⚽", name="Wesley from the stands", full_name="Wesley Verhoeven",
        age=32, location="Amsterdam", tier="great_catch",
        description=(
            "Owns three football shirts, has received two stadium bans, and has "
            "one cousin who “knows someone”. Difficult to fool unless the scam "
            "involves exclusive match tickets."
        ),
        status="Interested in tickets, hostile to paperwork",
        chance=0.52, payout_min=500, payout_max=1400,
        attempt_cost=175, max_failures=3,
        success_text=(
            "You offer Wesley two exclusive VIP tickets for a sold-out European "
            "match — lounge access, free beer, and a personal meeting with a "
            "player whose name you spell incorrectly.\n\nHe pays before asking "
            "which stadium the match is in."
        ),
        failure_text=(
            "You offer Wesley VIP tickets for a match that was played last "
            "month.\n\nHe notices immediately, insults your entire operation, "
            "and asks whether you can still arrange away tickets for Feyenoord."
        ),
        flag="🇳🇱",
        short_name="Wesley",
        trait="🎟️ Ticket Motivated",
        decays=False,
        approaches={"careful": (0.6, 0.7, "🎫"), "normal": (0.52, 1.0, "⚽"), "greedy": (0.35, 1.8, "🏆")},
    ),
    # ── whales: temporary, enormous, dreadful odds ────────────────────────
    _arch(
        emoji="🐳", name="Crypto whale", full_name="Thijs Bakker-Okonkwo",
        age=34, location="Monaco (on paper)", tier="whale",
        description=(
            "Made it all in eighteen months and believes that makes him hard to "
            "fool. Answers only between flights, and only in voice notes."
        ),
        status="Impossible to reach, briefly reachable",
        chance=0.14, payout_min=6_000, payout_max=10_000,
        attempt_cost=1_000, max_failures=2,
        success_text=(
            "You reach him during a layover and offer a sovereign-backed "
            "Nigerian diamond tranche with a closing window of forty minutes."
            "\n\nHe wires the funds from the lounge without reading the "
            "prospectus, which is a photograph of a different prospectus."
        ),
        failure_text=(
            "His family office answers instead of him. They ask three questions "
            "about custody arrangements.\n\nYou answer none of them and hang up "
            "while a real lawyer is being fetched."
        ),
        flag="🇲🇨",
        short_name="Crypto whale",
        trait="🐋 Eighteen-Month Fortune",
        decays=False,
        approaches={"careful": (0.22, 0.6, "🧾"), "normal": (0.14, 1.0, "🐋"), "greedy": (0.08, 2.0, "💎")},
    ),
    _arch(
        emoji="🛥️", name="Yacht owner", full_name="Sander de Wit",
        age=51, location="Nowhere, for tax reasons", tier="whale",
        description=(
            "Technically a resident of nowhere, financially a resident of your "
            "inbox. Owns a boat he has never personally steered."
        ),
        status="Unreachable, but bored",
        chance=0.12, payout_min=7_000, payout_max=12_000,
        attempt_cost=1_200, max_failures=2,
        success_text=(
            "You offer him a mooring concession at a Nigerian royal marina that "
            "does not exist, complete with a rendering you made in ten minutes."
            "\n\nHe is delighted, and asks only whether the berth is wide enough."
        ),
        failure_text=(
            "His captain looks up the marina and finds a beach.\n\nYou are "
            "removed from the group chat before you can explain that the "
            "harbour is planned for later this year."
        ),
        flag="🇲🇨",
        short_name="Yacht owner",
        trait="🛥️ Floating Tax Structure",
        decays=False,
        approaches={"careful": (0.2, 0.6, "📄"), "normal": (0.12, 1.0, "🛥️"), "greedy": (0.07, 2.0, "💎")},
    ),
    _arch(
        emoji="🇫🇷", name="Étienne Beaumont", full_name="Étienne Beaumont",
        age=52, location="Paris, France", tier="rare",
        description=(
            "Deputy Director of International Administrative Cooperation. "
            "Étienne considers every meeting beneath him, every form "
            "incorrectly completed, and every foreign official insufficiently "
            "educated. He refuses to speak English until money is mentioned."
        ),
        status="Offended by your application",
        chance=0.2, payout_min=1_500, payout_max=4_000,
        attempt_cost=450, max_failures=3,
        success_text=(
            "You present Étienne with a forged request for “urgent royal "
            "diplomatic processing”.\n\nHe immediately points out that your form "
            "uses the wrong font, the wrong paper size and an unacceptable "
            "shade of blue — but he is impressed that you included six "
            "unnecessary stamps, and approves the payment to demonstrate that "
            "French bureaucracy remains superior."
        ),
        failure_text=(
            "Étienne does not appear to notice that the entire proposal is "
            "fraudulent.\n\nHe rejects it because page seven was not initialled "
            "in the bottom-left corner and your title does not contain enough "
            "hyphens."
        ),
        flag="🇫🇷",
        short_name="Étienne",
        trait="🍷 Old Wine, Older Money",
        decays=False,
        approaches={"careful": (0.3, 0.7, "🍷"), "normal": (0.2, 1.0, "🏛️"), "greedy": (0.12, 2.0, "💰")},
    ),
    _arch(
        emoji="🇪🇬", name="Mahmoud Hassan", full_name="Mahmoud “Twenty Phones” Hassan",
        age=35, location="Cairo, Egypt", tier="rare",
        description=(
            "Runs a mobile-phone shop and appears to answer twenty devices "
            "simultaneously. Every phone is “almost new”, every account belongs "
            "to a cousin, and every suspicious transaction is apparently part "
            "of a family business."
        ),
        status="Already speaking to six other princes",
        chance=0.29, payout_min=1_200, payout_max=3_000,
        attempt_cost=450, max_failures=3,
        success_text=(
            "You offer Mahmoud a shipment of exclusive royal smartphones that "
            "allegedly cannot be traced by any government.\n\nHe does not "
            "believe you, but buys the shipment anyway because he already has "
            "customers waiting — and pays from four different accounts."
        ),
        failure_text=(
            "You try to convince Mahmoud that one of his bank accounts has been "
            "frozen.\n\nHe checks three phones, calls two cousins, and discovers "
            "the problem before you finish your sentence. He then offers to "
            "sell you a replacement identity at a “special friend price”."
        ),
        flag="🇪🇬",
        short_name="Mahmoud",
        trait="🏺 Antiquities Enthusiast",
        decays=False,
        approaches={"careful": (0.4, 0.7, "🏺"), "normal": (0.29, 1.0, "📜"), "greedy": (0.17, 2.0, "💰")},
    ),
    _arch(
        emoji="🇩🇪", name="Reggie Krämer", full_name="Reggie Krämer",
        age=44, location="Düsseldorf, Germany", tier="great_catch",
        description=(
            "A logistics manager, amateur spreadsheet designer, and firm "
            "believer that jokes should be submitted at least three working "
            "days in advance. He was named Reggie by his British mother. He "
            "considers this deeply inefficient."
        ),
        status="Waiting for a serious proposal",
        chance=0.42, payout_min=700, payout_max=1_700,
        attempt_cost=300, max_failures=3,
        success_text=(
            "You describe the scam as a “cross-border liquidity optimisation "
            "process”.\n\nReggie requests a schedule, a risk matrix and a "
            "colour-coded spreadsheet. Yours contains fourteen tabs and no "
            "useful information. He is impressed by the organisation and pays."
        ),
        failure_text=(
            "You open the conversation with a joke about German efficiency.\n\n"
            "Reggie pauses for eleven seconds, asks you to explain why it was "
            "funny, and ends the call before you reach the actual scam."
        ),
        flag="🇩🇪",
        short_name="Reggie",
        trait="🍺 Enthusiastically Uninformed",
        decays=False,
        approaches={"careful": (0.57, 0.7, "📋"), "normal": (0.42, 1.0, "🍺"), "greedy": (0.25, 1.8, "💰")},
    ),
    _arch(
        emoji="🇸🇪", name="Will Andersson", full_name="Will Andersson",
        age=39, location="Uppsala, Sweden", tier="great_catch",
        description=(
            "An insurance administrator, punctual taxpayer, and owner of four "
            "identical grey sweaters. Will is so honest that he assumes other "
            "people probably are too. Unfortunately, he reads every document "
            "from beginning to end."
        ),
        status="Calmly reviewing the terms",
        chance=0.58, payout_min=350, payout_max=900,
        attempt_cost=200, max_failures=3,
        success_text=(
            "You send Will a detailed agreement for a “Royal Nigerian Savings "
            "Partnership”.\n\nHe reads all thirty-two pages, corrects two "
            "spelling mistakes, and politely asks whether the interest is "
            "taxable. After you answer “probably”, he transfers the money."
        ),
        failure_text=(
            "Will carefully reviews your agreement and discovers that section "
            "14 contradicts section 27.\n\nHe does not accuse you of fraud. He "
            "simply sends back a twelve-page document containing suggested "
            "revisions."
        ),
        flag="🇸🇪",
        short_name="Will",
        trait="🧊 Polite To A Fault",
        decays=False,
        approaches={"careful": (0.71, 0.7, "🤝"), "normal": (0.58, 1.0, "🧊"), "greedy": (0.35, 1.8, "💰")},
    ),
    _arch(
        emoji="👑", name="Prince Akwabi",
        full_name="HRH Prince Akwabi Nwosu", age=61, location="Abuja, Nigeria",
        tier="whale",
        description=(
            "A fabulously wealthy Nigerian prince with twelve honorary titles, "
            "several offshore accounts, and decades of experience in suspicious "
            "international transactions. He has heard every scam imaginable — "
            "and invented several of them himself."
        ),
        status="Evaluating your technique",
        chance=0.06, payout_min=8_000, payout_max=20_000,
        attempt_cost=600, max_failures=2,
        success_text=(
            "You inform Prince Akwabi that a distant relative has left him a "
            "substantial royal inheritance.\n\nHe recognises the scam "
            "immediately, but is so impressed by the forged documents that he "
            "decides to invest in your operation — and offers you a junior "
            "position in his organisation."
        ),
        failure_text=(
            "Prince Akwabi listens politely while you explain that his royal "
            "fortune has been frozen.\n\nHe then points out seven errors in your "
            "story, corrects the formatting of your email, and sends you an "
            "invoice for consultancy services."
        ),
        flag="🇳🇬",
        short_name="Akwabi",
        trait="👑 Rival Royalty",
        decays=False,
        approaches={"careful": (0.1, 0.6, "🤝"), "normal": (0.06, 1.0, "👑"), "greedy": (0.03, 2.0, "💎")},
    ),
    _arch(
        emoji="🤴", name="King Nicolas",
        full_name="HM King Nicolas the Unprepared", age=34,
        location="Brussels, Belgium", tier="rare",
        description=(
            "A reasonably wealthy king with an impressive palace, several "
            "ceremonial titles, and almost no understanding of money. He signs "
            "documents without reading them and believes “financial due "
            "diligence” is a type of royal banquet."
        ),
        status="Delighted to meet another royal",
        chance=0.82, payout_min=1_200, payout_max=3_500,
        attempt_cost=800, max_failures=3,
        success_text=(
            "You introduce yourself as the Nigerian Minister of International "
            "Royal Cooperation.\n\nNicolas is delighted to meet someone with "
            "even more unnecessary titles than himself. He pays for membership "
            "of the “International Association of Legitimate Monarchs” and asks "
            "when he receives his certificate."
        ),
        failure_text=(
            "You ask Nicolas to transfer a small royal verification fee.\n\n"
            "He happily agrees, but accidentally sends the money to his palace "
            "gardener instead. The gardener refuses to return it and has now "
            "declared himself Duke of Brussels."
        ),
        flag="🇧🇪",
        short_name="Nicolas",
        trait="👑 Certifiably Gullible",
        decays=False,
        approaches={"careful": (0.92, 0.7, "📜"), "normal": (0.82, 1.0, "👑"), "greedy": (0.5, 1.8, "💰")},
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # The rebalanced roster: every mark below sets its own odds per approach,
    # so "careful is safest, greedy pays most" stops being universally true.
    # Read the board before clicking.
    # ══════════════════════════════════════════════════════════════════════════

    # ── 🇳🇱 Netherlands ───────────────────────────────────────────────────
    _arch(
        emoji="🇳🇱", name="Naud Muscator", full_name="Naud Muscator",
        age=43, location="The Hague", tier="great_catch",
        trait="🍔 Food Motivated",
        description=(
            "President of the Netherlands, permanently surrounded by advisers, "
            "briefing papers and emergency snacks. Very difficult to fool "
            "politically, but his judgement deteriorates rapidly when food is "
            "involved. Cabinet meetings have allegedly been delayed because "
            "somebody mentioned bitterballen."
        ),
        status="Hungry, but surrounded by advisers",
        chance=0.22, payout_min=650, payout_max=1_500,
        attempt_cost=175, max_failures=3, decays=False,
        approaches={
            "careful": (0.30, 0.70, "🍽️"),
            "normal":  (0.22, 1.00, "🥪"),
            "greedy":  (0.13, 2.00, "🍔"),
        },
        success_text=(
            "You invite Naud to an emergency Nigerian diplomatic summit with "
            "unlimited bitterballen.\n\nHe transfers the required catering "
            "deposit before his advisers finish reading the invitation."
        ),
        failure_text=(
            "An adviser discovers that the Abuja Presidential Snack Authority "
            "does not exist.\n\nNaud still asks whether the bitterballen are "
            "available."
        ),
        flag="🇳🇱",
        short_name="Naud",
    ),
    _arch(
        emoji="📋", name="Diligent Doubt", full_name="Diligent Doubt",
        age=38, location="The Hague", tier="great_catch",
        trait="📋 Due Diligence",
        description=(
            "Methodical, diligent and profoundly unexciting. Reads every "
            "attachment, checks every number twice and responds to suspicious "
            "proposals with numbered follow-up questions."
        ),
        status="Reviewing page 14 of your supporting documentation",
        chance=0.41, payout_min=500, payout_max=1_300,
        attempt_cost=225, max_failures=4, decays=False,
        approaches={
            "careful": (0.62, 0.70, "📋"),
            "normal":  (0.41, 1.00, "📑"),
            "greedy":  (0.07, 2.00, "🚀"),
        },
        success_text=(
            "Your 37-page **Bilateral Administrative Cost Reconciliation "
            "Framework** contains enough paperwork to satisfy him."
        ),
        failure_text=(
            "He finds a 0,4% discrepancy between page six and Appendix C and "
            "sends eleven requested corrections."
        ),
        flag="🇳🇱",
        short_name="Diligent",
    ),
    _arch(
        emoji="🦍", name="Admiral Ape", full_name="Admiral Ape",
        age=0, location="The Hague", tier="whale",
        trait="💰 Treasury Whale",
        description=(
            "Decorated admiral, senior cabinet member and inexplicably wealthy "
            "ape entrusted with the Dutch treasury. Age unknown. Species: ape, "
            "apparently."
        ),
        status="Guarding the treasury and watching you very closely",
        chance=0.11, payout_min=3_000, payout_max=9_000,
        attempt_cost=650, max_failures=2, decays=False,
        approaches={
            "careful": (0.20, 0.70, "🧾"),
            "normal":  (0.11, 1.00, "💼"),
            "greedy":  (0.03, 2.20, "💎"),
        },
        success_text=(
            "Against every expectation, Admiral Ape approves the Nigerian "
            "strategic-resource bond purchase.\n\nSeveral accountants faint."
        ),
        failure_text=(
            "He stamps the proposal **REJECTED**, eats the corner of the "
            "document and has security escort you out."
        ),
        flag="🇳🇱",
        short_name="Admiral Ape",
    ),
    _arch(
        emoji="🪙", name="Jan de Zuinigerd", full_name="Jan Bakhuizen",
        age=61, location="Almere", tier="ordinary",
        trait="🪙 Pathologically Frugal",
        description=(
            "Jan has money. Jan simply does not believe anyone else should "
            "have it. He still remembers being overcharged €0,25 at a petrol "
            "station in 2009."
        ),
        status="Asking whether the administration fee can be waived",
        chance=0.50, payout_min=150, payout_max=550,
        attempt_cost=50, max_failures=5, decays=False,
        approaches={
            "careful": (0.73, 0.70, "🧾"),
            "normal":  (0.50, 1.00, "🪙"),
            "greedy":  (0.05, 2.00, "💸"),
        },
        success_text=(
            "Jan discovers the promised refund exceeds the processing fee by "
            "eleven Naira.\n\nAfter extensive calculation, he pays."
        ),
        failure_text=(
            "A seventeen-message negotiation collapses over three Naira."
        ),
        flag="🇳🇱",
        short_name="Jan",
    ),
    _arch(
        emoji="✉️", name="Henk de Postzegelkoning", full_name="Henk Roozendaal",
        age=71, location="Haarlem", tier="whale",
        trait="✉️ Penny Whale",
        description=(
            "Henk quietly owns one of Europe's most valuable private stamp "
            "collections. Nobody outside philately has heard of him, and "
            "contacting him costs almost nothing."
        ),
        status="Online on a stamp-collecting forum since 06:14",
        chance=0.04, payout_min=3_000, payout_max=10_000,
        attempt_cost=25, max_failures=4, decays=False,
        approaches={
            "careful": (0.07, 0.70, "🔎"),
            "normal":  (0.04, 1.00, "✉️"),
            "greedy":  (0.015, 2.00, "💎"),
        },
        success_text=(
            "You convince Henk that Nigeria has discovered an extremely rare "
            "colonial postage issue."
        ),
        failure_text=(
            "He looks at the photo for four seconds.\n\n"
            "*“Wrong perforation pattern.”*"
        ),
        flag="🇳🇱",
        short_name="Henk P.",
        special_rules=(
            "Extremely cheap access, extremely low odds.",
        ),
    ),
    _arch(
        emoji="🪤", name="Zwieber the Hobo", full_name="Zwieber",
        age=42, location="Utrecht", tier="ordinary",
        trait="🪙 Nothing Left To Lose",
        description=(
            "Extremely trusting, painfully naive and convinced his luck is "
            "finally about to change. Unfortunately, the only money in his "
            "pocket is what he earned earlier today selling his bicycle on "
            "Marktplaats."
        ),
        status="Holding 247 Naira and feeling unusually optimistic",
        chance=0.90, payout_min=100, payout_max=300,
        attempt_cost=0, max_failures=5, decays=False,
        approaches={
            "careful": (0.97, 0.70, "🤝"),
            "normal":  (0.90, 1.00, "💬"),
            "greedy":  (0.55, 1.50, "🚀"),
        },
        success_text=(
            "Zwieber happily hands over everything he has, because he knows "
            "this opportunity will finally turn his life around.\n\n"
            "“Everything” turns out to be today's bicycle money."
        ),
        failure_text=(
            "He enthusiastically agrees, checks his pockets and remembers he "
            "already spent the money on groceries.\n\nHe apologises for wasting "
            "your time."
        ),
        flag="🇳🇱",
        short_name="Zwieber",
    ),
    _arch(
        emoji="🐌", name="Gerard van de VvE", full_name="Gerard Sluiter",
        age=64, location="Amstelveen", tier="rare",
        trait="🐌 Nog één klein puntje…",
        description=(
            "Gerard owns several apartments and considerably more free time "
            "than is healthy. He has chaired his VvE for fourteen years and has "
            "never allowed a discussion to end when another meeting could "
            "theoretically be scheduled.\n\n"
            "He doesn't reject your scam. He doesn't accept it. He asks for a "
            "revised proposal."
        ),
        status="Has requested a revised proposal before the next VvE meeting. Again",
        chance=0.08, payout_min=1_200, payout_max=3_200,
        attempt_cost=150, max_failures=10, decays=False,
        # Every failure feeds 150% of the operating cost into the pot — the
        # compensation for how long he takes, and the reason anyone bothers.
        pot_per_failure=225,
        approaches={
            "careful": (0.08, 0.70, "🧐"),
            "normal":  (0.08, 1.00, "📋"),
            "greedy":  (0.08, 2.00, "💰"),
        },
        success_text=(
            "After countless meetings, Gerard finally replies:\n\n"
            "**“Prima. Akkoord.”**\n\n"
            "Nobody celebrates the money. Everyone celebrates that Gerard is "
            "finally gone."
        ),
        failure_text=(
            "Gerard sends a fourteen-paragraph email and proposes discussing "
            "the matter at the next meeting.\n\n"
            "Gerard remains on the board. Of course he does."
        ),
        flag="🇳🇱",
        short_name="Gerard",
        fake_eligible=False,
        special_rules=(
            "All three approaches always have exactly 8% success chance.",
            "Every failed attempt adds 225 Naira to Gerard's pot.",
            "The pot is paid on top of the payout and is never multiplied.",
            "Gerard survives up to 10 failed attempts.",
        ),
    ),

    # ── 🇫🇷 France ────────────────────────────────────────────────────────
    _arch(
        emoji="☁️", name="Vatou", full_name="Vatou",
        age=29, location="France", tier="ordinary",
        trait="☁️ Cloud Poor",
        description=(
            "Builds first and asks questions later. Extremely easy to convince "
            "that anything is a necessary development expense, but almost all "
            "his money already goes to cloud subscriptions and AI services."
        ),
        status="Deploying directly to production",
        chance=0.76, payout_min=100, payout_max=400,
        attempt_cost=75, max_failures=4, decays=False,
        approaches={
            "careful": (0.86, 0.70, "🧪"),
            "normal":  (0.76, 1.00, "☁️"),
            "greedy":  (0.35, 2.00, "🚀"),
        },
        success_text=(
            "Vatou pays for a **Nigerian High-Performance Cloud Compatibility "
            "License**, assuming an AI agent probably installed it."
        ),
        failure_text=(
            "He agrees immediately, but his card is declined because six cloud "
            "providers already charged him."
        ),
        flag="🇫🇷",
        short_name="Vatou",
    ),
    _arch(
        emoji="🦚", name="Armand de Prestige", full_name="Armand Lavigne",
        age=46, location="Paris", tier="great_catch",
        trait="🦚 Too Important For Small Deals",
        description=(
            "Armand believes he operates at a financial level ordinary people "
            "cannot comprehend. Cheap opportunities offend him. Safe "
            "opportunities bore him."
        ),
        status="Waiting to hear whether he qualifies for your exclusive offer",
        chance=0.50, payout_min=450, payout_max=1_000,
        attempt_cost=300, max_failures=3, decays=False,
        # Greedy is his *best* odds — and the multiplier is deliberately
        # smaller than the usual ×2 so it stays a choice rather than a freebie.
        approaches={
            "careful": (0.27, 0.70, "🤏"),
            "normal":  (0.50, 1.00, "🥂"),
            "greedy":  (0.62, 1.40, "💎"),
        },
        success_text=(
            "You explain the investment is normally restricted to heads of "
            "state and royal families.\n\n*“Naturally.”*"
        ),
        failure_text=(
            "You offer him a safe, conservative investment.\n\n"
            "*“Do I look like a retail investor?”*"
        ),
        flag="🇫🇷",
        short_name="Armand",
        special_rules=(
            "Greedy has Armand's **highest** success chance.",
            "His greedy multiplier is deliberately smaller than usual.",
            "Luxury and exclusivity make him easier, not harder.",
        ),
    ),

    # ── 🇨🇲 Cameroon ──────────────────────────────────────────────────────
    _arch(
        emoji="💥", name="Tio Men", full_name="Tio Men",
        age=36, location="Douala", tier="great_catch",
        trait="💥 Loose Cannon",
        description=(
            "Answers simple questions with voice notes, threats, unrelated "
            "business ideas and occasionally all three at once."
        ),
        status="Sent you six voice messages. None answer your question",
        chance=0.44, payout_min=500, payout_max=1_200,
        attempt_cost=200, max_failures=3, decays=False,
        approaches={
            "careful": (0.28, 0.70, "🧊"),
            "normal":  (0.44, 1.00, "📞"),
            "greedy":  (0.18, 2.00, "💥"),
        },
        success_text=(
            "You stop explaining and simply tell Tio Men there's money to be "
            "made.\n\nSomewhere around minute three of his reply, he agrees."
        ),
        failure_text=(
            "He interrupts your careful explanation after twelve seconds and "
            "sends a voice note that causes your lawyer to leave the chat."
        ),
        flag="🇨🇲",
        short_name="Tio Men",
        special_rules=(
            "Careful has Tio Men's **worst** odds. That is intentional — he "
            "interrupts anyone who slows down.",
            "Normal is his best chance. Greedy pays double, if you dare.",
        ),
    ),

    # ── 🇱🇺 Luxembourg ────────────────────────────────────────────────────
    _arch(
        emoji="🍌", name="Party Banana", full_name="Party Banana",
        age=34, location="Luxembourg City", tier="great_catch",
        trait="📈 Smart Money Syndrome",
        description=(
            "Party Banana actually understands money reasonably well. "
            "Unfortunately, he understands it well enough to be dangerously "
            "confident in his own brilliance."
        ),
        status=(
            "Explaining why he already understood the investment before you "
            "finished explaining it"
        ),
        chance=0.62, payout_min=600, payout_max=1_500,
        attempt_cost=350, max_failures=3, decays=False,
        approaches={
            "careful": (0.55, 0.70, "📊"),
            "normal":  (0.62, 1.00, "📈"),
            "greedy":  (0.38, 1.60, "🍌"),
        },
        success_text=(
            "Halfway through the pitch he interrupts:\n\n"
            "*“Yes, yes, I understand the arbitrage.”*\n\n"
            "There is no arbitrage."
        ),
        failure_text="For once, he actually checks the numbers.",
        flag="🇱🇺",
        short_name="Banana",
        special_rules=(
            "Careful has Party Banana's **worst** odds — give him time and he "
            "checks the numbers himself.",
            "Normal is his best chance. Greedy pays 1,6× and he usually spots "
            "it.",
        ),
    ),

    # ── 🇬🇧 United Kingdom ────────────────────────────────────────────────
    _arch(
        emoji="🏰", name="Archibald Worthington III",
        full_name="Archibald Worthington III",
        age=72, location="Oxfordshire", tier="whale",
        trait="🏰 Old Money, Expensive Access",
        description=(
            "Lives in a house large enough to contain rooms he has never "
            "personally entered. Doesn't understand modern finance and assumes "
            "“the people who do that sort of thing” handle it.\n\n"
            "Unfortunately, getting through those people is expensive."
        ),
        status="His secretary has forwarded your letter to someone who handles financial matters",
        chance=0.55, payout_min=2_000, payout_max=5_000,
        attempt_cost=1_500, max_failures=3, decays=False,
        approaches={
            "careful": (0.70, 0.70, "🖋️"),
            "normal":  (0.55, 1.00, "📜"),
            "greedy":  (0.18, 2.00, "🏇"),
        },
        success_text=(
            "Your embossed proposal uses the phrase **Commonwealth Development "
            "Opportunity**.\n\nArchibald tells his accountant to “sort it out.”"
        ),
        failure_text=(
            "Archibald himself is willing to pay.\n\nHis solicitor is not."
        ),
        flag="🇬🇧",
        short_name="Archibald",
        special_rules=(
            "Access is unusually expensive.",
        ),
    ),
    _arch(
        emoji="🔥", name="Gary Last Call", full_name="Gary Pemberton",
        age=41, location="Heathrow Airport", tier="rare",
        trait="🔥 NOW OR NEVER — one attempt only",
        description=(
            "Gary has twelve minutes before boarding, 4% battery and no time "
            "whatsoever for proper due diligence.\n\n"
            "**One attempt. That is all anybody gets.**"
        ),
        status="FINAL CALL — gate closes in 12 minutes",
        chance=0.58, payout_min=700, payout_max=1_700,
        attempt_cost=150, max_failures=1, decays=False, one_shot=True,
        approaches={
            "careful": (0.68, 0.70, "🧳"),
            "normal":  (0.58, 1.00, "🎫"),
            "greedy":  (0.32, 1.80, "🔥"),
        },
        success_text=(
            "Gary starts asking a question. Final boarding is announced.\n\n"
            "*“Whatever. Just send me the payment link.”*"
        ),
        failure_text=(
            "He promises to investigate after security.\n\nHis plane leaves."
        ),
        flag="🇬🇧",
        short_name="Gary",
        special_rules=(
            "**One total attempt.**",
            "Any attempt removes Gary, win or lose.",
        ),
    ),

    # ── 🇩🇪 Germany ───────────────────────────────────────────────────────
    _arch(
        emoji="🏎️", name="Felix Autobahn", full_name="Felix Brandt",
        age=35, location="Frankfurt", tier="great_catch",
        trait="🏎️ Premium Means Better",
        description=(
            "Treats speed limits as personal insults. Anything labelled "
            "**Premium**, **Performance** or **Limited Edition** immediately "
            "becomes more credible."
        ),
        status="Currently doing 217 km/h while reading your proposal",
        chance=0.48, payout_min=600, payout_max=1_500,
        attempt_cost=300, max_failures=3, decays=False,
        approaches={
            "careful": (0.50, 0.70, "🛞"),
            "normal":  (0.48, 1.00, "🚗"),
            "greedy":  (0.30, 1.80, "🏎️"),
        },
        success_text=(
            "Felix buys the **Nigerian Presidential Autobahn Performance "
            "Package** after hearing the word “exclusive.”"
        ),
        failure_text=(
            "Your documentation claims an 84% increase in both horsepower "
            "*and* fuel economy.\n\nEven Felix becomes suspicious."
        ),
        flag="🇩🇪",
        short_name="Felix",
    ),
    _arch(
        emoji="🏛️", name="Taru the Terrible", full_name="Taru Vogel",
        age=51, location="Berlin", tier="ordinary",
        trait="🏛️ Institutionally Immovable",
        description=(
            "Somehow survived multiple restructurings, investigations, missed "
            "deadlines and at least one meeting he was supposed to organise "
            "himself.\n\nHe is not functioning particularly well. This does not "
            "make him easy to scam."
        ),
        status="Has forwarded your proposal to the department that originally forwarded it to him",
        chance=0.12, payout_min=250, payout_max=900,
        attempt_cost=0, max_failures=2, decays=False,
        approaches={
            "careful": (0.18, 0.70, "🗂️"),
            "normal":  (0.12, 1.00, "🏛️"),
            "greedy":  (0.04, 2.00, "📢"),
        },
        success_text=(
            "Your vague administrative invoice becomes attached to an "
            "unrelated government payment batch."
        ),
        failure_text=(
            "Taru tries to identify the responsible department and eventually "
            "forwards your request back to himself."
        ),
        flag="🇩🇪",
        short_name="Taru",
    ),

    # ── 🇧🇪 Belgium ───────────────────────────────────────────────────────
    _arch(
        emoji="🚴", name="Wouter de Wielertoerist", full_name="Wouter Claes",
        age=44, location="Leuven", tier="great_catch",
        trait="🚴 Marginal Gains",
        description=(
            "Owns a bicycle worth more than his first car and happily spends "
            "hundreds to save several grams."
        ),
        status="Comparing the weight of two identical bottle cages",
        chance=0.61, payout_min=400, payout_max=1_200,
        attempt_cost=250, max_failures=4, decays=False,
        approaches={
            "careful": (0.64, 0.70, "📐"),
            "normal":  (0.61, 1.00, "🚴"),
            "greedy":  (0.38, 1.80, "💨"),
        },
        success_text=(
            "You sell him **Nigerian Royal Aerodynamic Valve Caps**, allegedly "
            "reducing rotational drag by 0,7%.\n\nHe asks whether a more "
            "expensive version exists."
        ),
        failure_text=(
            "Your testing graph accidentally shows his bicycle travelling "
            "faster than sound."
        ),
        flag="🇧🇪",
        short_name="Wouter",
    ),
    _arch(
        emoji="💤", name="Koen van de Gemeente", full_name="Koen Verlinden",
        age=49, location="Mechelen", tier="great_catch",
        trait="💤 Slowly Waking Up — 3 attempts total",
        description=(
            "Has processed roughly the same forms for twenty-three years. The "
            "first suspicious invoice may simply get stamped.\n\n"
            "**Every failed attempt wakes him up.** 76% → 43% → 17%."
        ),
        status="Has not yet realised this file is unusual",
        chance=0.76, payout_min=450, payout_max=1_100,
        attempt_cost=175, max_failures=3,
        chance_ladder=[0.76, 0.43, 0.17], chance_cap=0.90,
        approach_mode="multiply",
        approaches={
            "careful": (1.15, 0.70, "🖊️"),
            "normal":  (1.00, 1.00, "📄"),
            "greedy":  (0.55, 1.70, "🗃️"),
        },
        success_text="**STAMP. FORWARD. PAID.**",
        failure_text=(
            "Koen pauses and reads the document again.\n\n"
            "Koen has become slightly more attentive."
        ),
        flag="🇧🇪",
        short_name="Koen",
        special_rules=(
            "Every failed attempt makes Koen **harder** to scam: 76% → 43% → 17%.",
            "Three attempts total.",
            "The card and buttons always show his current odds.",
        ),
    ),
    _arch(
        emoji="🏘️", name="Thierry de Vastgoedoom", full_name="Thierry Deleu",
        age=63, location="Brussels", tier="ordinary",
        trait="🏘️ Asset Rich, Cash Poor",
        description=(
            "Owns seventeen apartments, two commercial buildings and something "
            "in France he hasn't visited since 2014.\n\n"
            "Net worth: millionaire. Bank balance: less impressive."
        ),
        status="Net worth: enormous. Available balance: concerning",
        chance=0.69, payout_min=250, payout_max=750,
        attempt_cost=275, max_failures=4, decays=False,
        approaches={
            "careful": (0.80, 0.70, "🔑"),
            "normal":  (0.69, 1.00, "🏘️"),
            "greedy":  (0.37, 2.00, "🏦"),
        },
        success_text=(
            "Thierry loves your investment but can only scrape together a few "
            "hundred Naira, because virtually his entire fortune is made of "
            "buildings."
        ),
        failure_text="Apartment sixteen needs a new roof.",
        flag="🇧🇪",
        short_name="Thierry",
    ),
    _arch(
        emoji="🎰", name="Kevin de Kraslotkoning", full_name="Kevin Peeters",
        age=27, location="Genk", tier="ordinary",
        trait="🎰 This Could Be The One",
        description=(
            "Perpetually one investment away from becoming rich. Previous "
            "strategies include scratch cards, football bets, questionable "
            "crypto and imported energy drinks."
        ),
        status="Has a good feeling about this one",
        chance=0.82, payout_min=75, payout_max=300,
        attempt_cost=25, max_failures=5, decays=False,
        approaches={
            "careful": (0.65, 0.70, "🧾"),
            "normal":  (0.82, 1.00, "🎰"),
            "greedy":  (0.80, 1.50, "🚀"),
        },
        success_text=(
            "You promise an 800% return.\n\n"
            "*“Bro I swear this is exactly what I've been waiting for.”*\n\n"
            "Unfortunately, he only has 184 Naira."
        ),
        failure_text=(
            "He desperately wants to invest but spent today's money on scratch "
            "cards."
        ),
        flag="🇧🇪",
        short_name="Kevin",
        special_rules=(
            "Normal and Greedy both have **better** odds than Careful.",
            "That is intentional. Kevin is a get-rich-quick addict.",
        ),
    ),

    # ── 🇯🇵 Japan ─────────────────────────────────────────────────────────
    _arch(
        emoji="🍪", name="Sachiko Tanaka", full_name="Sachiko Tanaka",
        age=84, location="Osaka", tier="ordinary",
        trait="🍪 Have We Met Before?",
        description=(
            "Kind, trusting and constantly offering visitors tea and biscuits.\n\n"
            "**Every failed attempt makes her easier**, because she forgets why "
            "she was suspicious. 28% → 42% → 59% → 76%."
        ),
        status="Offers you another biscuit and asks whether you've visited before",
        chance=0.28, payout_min=250, payout_max=700,
        attempt_cost=100, max_failures=4,
        chance_ladder=[0.28, 0.42, 0.59, 0.76], warms_up=True,
        approach_mode="shift",
        approaches={
            "careful": (+0.10, 0.70, "🍵"),
            "normal":  (0.00, 1.00, "🍪"),
            "greedy":  (-0.15, 1.70, "💰"),
        },
        success_text=(
            "Sachiko suddenly decides she remembers you as “the nice investment "
            "gentleman.”\n\nYou have never successfully met."
        ),
        failure_text=(
            "She becomes suspicious and hangs up.\n\n"
            "Ten minutes later, she has mostly forgotten why."
        ),
        flag="🇯🇵",
        short_name="Sachiko",
        special_rules=(
            "Every failed attempt makes Sachiko **easier**: 28% → 42% → 59% → 76%.",
            "The card and buttons always show her current odds.",
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # 🐋 WHALE / 🟣 RARE / 🦄 LEGENDARY additions
    # ══════════════════════════════════════════════════════════════════════════
    _arch(
        emoji="👑", name="Prince Chinedu Okafor", full_name="Chinedu Okafor",
        age=54, location="Lagos", tier="whale", flag="🇳🇬",
        short_name="Chinedu", trait="👑 Scammer's Instinct",
        description=(
            "The original Nigerian prince, still working the same email he "
            "wrote in 2003. He recognises the trade because he invented it — "
            "and he does not appreciate the competition."
        ),
        status="Reading your message with professional interest",
        chance=0.10, payout_min=2_000, payout_max=6_000,
        attempt_cost=150, max_failures=3, decays=False,
        # Fake targets resolve on the first attack, which would leave his
        # counter-scam with nothing to trigger on.
        fake_eligible=False,
        npc_counter_chance=0.20, npc_counter_steal=(100, 400),
        approaches={
            "careful": (0.17, 0.60, "🤝"),
            "normal":  (0.10, 1.00, "👑"),
            "greedy":  (0.03, 2.00, "💎"),
        },
        special_rules=(
            "Every **failed** attempt has a 20% chance to trigger Chinedu's "
            "own counter-scam.",
            "His counter-scam takes a further 100–400 Naira from your cash.",
        ),
        success_text=(
            "You send Chinedu a proposal so brazen that he assumes only a "
            "genuine head of state would dare.\n\n"
            "He transfers the facilitation fee out of professional respect."
        ),
        failure_text=(
            "Chinedu reads your message twice, then replies with a corrected "
            "version containing better grammar, a more plausible sum and his "
            "own account number."
        ),
    ),
    _arch(
        emoji="🏦", name="Walter P. Sterling", full_name="Walter Prescott Sterling",
        age=58, location="Zurich", tier="rare", flag="🇨🇭",
        short_name="Walter", trait="🏦 Professional Standards",
        description=(
            "A private banker who has spent thirty years moving other people's "
            "money very quietly. He expects paperwork, discretion and correct "
            "formatting — and rewards all three generously."
        ),
        status="Reviewing your documentation with a fountain pen",
        chance=0.68, payout_min=1_800, payout_max=3_500,
        attempt_cost=1_500, max_failures=3, decays=False,
        approaches={
            "careful": (0.85, 0.70, "📋"),
            "normal":  (0.68, 1.00, "🏦"),
            "greedy":  (0.25, 1.80, "💰"),
        },
        success_text=(
            "Your submission is immaculate: correct letterhead, plausible "
            "provenance, three notarised signatures and no urgency "
            "whatsoever.\n\nWalter processes it without comment."
        ),
        failure_text=(
            "Walter returns your file with one sentence underlined in "
            "pencil.\n\nHe does not say which part was wrong. He assumes you "
            "will know."
        ),
    ),
    _arch(
        emoji="🦄", name="Former President Darkodor", full_name="Darkodor",
        age=0, location="Unknown", tier="legendary", flag="🇳🇱",
        short_name="Darkodor", trait="🦄 The Unicorn",
        description=(
            "A legendary former president from an earlier age of WarEra. "
            "Seeing him online at all is considered a statistical anomaly."
        ),
        status="Last seen online several governments ago",
        chance=0.015, payout_min=15_000, payout_max=30_000,
        attempt_cost=50, max_failures=1, decays=False, one_shot=True,
        intel_immune=True, fake_eligible=False,
        # Explicitly below every other legendary: the expansion asks for
        # Darkodor to stay the single rarest, not merely tie for it.
        spawn_weight=0.7,
        approaches={
            "careful": (0.020, 0.70, "🛡️"),
            "normal":  (0.015, 1.00, "🎯"),
            "greedy":  (0.010, 2.00, "🤑"),
        },
        # A legendary jackpot is not a multiple of an ordinary payout, so each
        # approach carries its own range and the multiplier formula is bypassed.
        approach_payouts={
            "careful": (10_000, 20_000),
            "normal":  (15_000, 30_000),
            "greedy":  (25_000, 40_000),
        },
        special_rules=(
            "🦄 Legendary target. He turns up almost never.",
            "**One total attempt** — success or failure removes him.",
            "Each approach has its own jackpot payout range, not a multiplier.",
            "🔒 **Completely immune to Intel.** It cannot be bought, gathered "
            "or applied to him.",
        ),
        success_text=(
            "Against everything the historical record suggests, Darkodor "
            "replies.\n\nHe reads your proposal, says “sure, why not”, and "
            "transfers a sum that briefly destabilises the Naira."
        ),
        failure_text=(
            "Darkodor reads your message.\n\nHe does not reply. He does not "
            "block you. He simply returns to whatever a former president does "
            "for the rest of eternity."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # MARIJN EXPANSION
    # Two real NPC marks: a legendary who pays in time rather than money, and
    # a rare one who is a policeman.
    # ══════════════════════════════════════════════════════════════════════════

    _arch(
        emoji="👁️", name="Prins Marijn de Echte",
        full_name="Prins Marijn de Echte",
        age=0, location="Nederland", tier="legendary", flag="🇳🇱",
        short_name="Marijn", trait="👁️ The All-Knowing Discord Admin",
        description=(
            "The all-knowing Discord admin. Surprisingly chill for a Discord "
            "mod. Unfortunately, he can see everything."
        ),
        status="Already read the logs",
        # No cash at all: the reward is time, and time is not a payout.
        chance=0.75, payout_min=0, payout_max=0,
        attempt_cost=200, max_failures=2, decays=False,
        fake_eligible=False, no_pot=True,
        # ~2.9x Darkodor's weight, at the top of the band the expansion asks
        # for.  It is expressed relative to a Darkodor who was himself nudged
        # down to stay the single rarest legendary, so the two constraints
        # ("2-3x Darkodor" and "Darkodor strictly rarest") both hold.
        spawn_weight=2.0,
        # There is no clever approach against somebody who can read the logs.
        approaches={
            "careful": (0.75, 1.00, "🛡️"),
            "normal":  (0.75, 1.00, "📋"),
            "greedy":  (0.75, 1.00, "💰"),
        },
        reset_cooldowns=True, intel_refill=True, always_arrest=True,
        special_rules=(
            "🦄 Legendary, but far less shy than Darkodor.",
            "**All three approaches are exactly 75%.** There is no clever "
            "angle against a man who can read the logs.",
            "💰 **No cash reward whatsoever.** Success pays in time instead: "
            "every one of your personal cooldowns is cleared and Intel is "
            "restored to 3/3.",
            "🔨 **Mod ban hammer** — every failed attempt is an immediate "
            "arrest.",
            "Jail, bribes and shared timers are never reset. Mod powers have "
            "limits.",
        ),
        success_text=(
            "**Prins Marijn de Echte has reviewed the logs and decided to "
            "allow it.**\n\nHe does not transfer any money. He does "
            "something considerably more useful."
        ),
        failure_text=(
            "**Marijn reviewed the logs.**\n\nBehaviour: POOR. He supplies "
            "the timestamp, the message ID, the full transcript and several "
            "screenshots.\n\n_\u201cPlease familiarize yourself with the "
            "server rules.\u201d_"
        ),
    ),
    _arch(
        emoji="🕶️", name="Undercover Cop", full_name="Undercover Cop",
        age=0, location="United States", tier="rare", flag="🇺🇸",
        short_name="the cop", trait="🚔 On the Job",
        description=(
            "Claims to be an ordinary American businessman. Keeps asking "
            "whether you have committed any crimes recently."
        ),
        status="Sunglasses indoors, third hour",
        chance=0.50, payout_min=1_500, payout_max=1_500,
        attempt_cost=100, max_failures=3, decays=False,
        fake_eligible=False, always_arrest=True, board_heat_minutes=30,
        approaches={
            "careful": (0.75, 1.00, "🛡️"),
            "normal":  (0.50, 1.00, "📋"),
            "greedy":  (0.30, 1.00, "💰"),
        },
        # Flat sums rather than multipliers: the greedy line is the evidence
        # locker, not a bigger slice of the same envelope.
        approach_payouts={
            "careful": (600, 600),
            "normal":  (1_500, 1_500),
            "greedy":  (3_000, 3_000),
        },
        special_rules=(
            "🚔 **On the job** — every failed attempt is an immediate arrest. "
            "There is no bribe negotiation with a man wearing a wire.",
            "🔥 **Taking him down puts the board on Heat for 30 minutes.** The "
            "next player to fail against a *real* mark gets one 50% chance of "
            "being arrested for it.",
            "🎭 Fake targets neither trigger Heat nor suffer it.",
        ),
        success_text=(
            "**The ordinary American businessman was not an ordinary American "
            "businessman.**\n\nHe was, however, carrying the operation's "
            "cash. Every nearby police radio has suddenly become very loud."
        ),
        failure_text=(
            "**He has produced a badge.**\n\nIn hindsight, the wire taped to "
            "his chest was somewhat suspicious."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # SMALL TARGETS EXPANSION PACK
    # Thirteen marks that each bend one rule of the board.
    # ══════════════════════════════════════════════════════════════════════════

    _arch(
        emoji="🤐", name="Babu", full_name="Babu, King of the Belgians",
        age=0, location="Brussels", tier="great_catch", flag="🇧🇪",
        short_name="Babu", trait="🤐 The Mute King",
        description=(
            "A Belgian king of remarkably few words. Perhaps because he has "
            "nothing to say. Perhaps because everyone who does business with "
            "him mysteriously stops talking too."
        ),
        status="Saying nothing, at length",
        chance=0.55, payout_min=800, payout_max=2_000,
        attempt_cost=250, max_failures=3, decays=False, fake_eligible=False,
        silence_minutes=30,
        approaches={
            "careful": (0.70, 0.70, "🤫"),
            "normal":  (0.55, 1.00, "🤐"),
            "greedy":  (0.32, 1.80, "📢"),
        },
        special_rules=(
            "Success **silences you in this channel for 30 minutes**. "
            "Commands still work; talking does not.",
            "The rest of the server is unaffected. Failure silences nobody.",
        ),
        success_text=(
            "Babu transferred the money.\n\nMoments later, an official royal "
            "decree arrived.\n\n**You have been silenced in this channel for "
            "30 minutes.** You may continue scamming.\n\nQuietly.\n\n"
            "> Babu says nothing."
        ),
        failure_text=(
            "Babu stares at you in complete silence.\n\nSomehow, the "
            "conversation is over."
        ),
    ),
    _arch(
        emoji="🕵️", name="Diablo", full_name="Diablo",
        age=0, location="Somewhere in the Netherlands", tier="rare", flag="🇩🇪",
        short_name="Diablo", trait="🕵️ Definitely Not Espionage",
        description=(
            "Diablo has infiltrated the Netherlands and may be conducting a "
            "dangerous German intelligence operation. Alternatively he may "
            "just be an ordinary German looking for a fun time. Nobody has "
            "actually checked."
        ),
        status="Definitely not taking notes",
        chance=0.60, payout_min=1_200, payout_max=3_200,
        attempt_cost=250, max_failures=2, decays=False, fake_eligible=False,
        wealth_loss_pct=0.20,
        approaches={
            "careful": (0.76, 0.70, "📋"),
            "normal":  (0.60, 1.00, "🕵️"),
            "greedy":  (0.40, 1.80, "💣"),
        },
        special_rules=(
            "☠️ **Every failure wires 20% of your total wealth to Germany** — "
            "cash *and* fund position, with no cap and no protected floor.",
        ),
        success_text=(
            "Diablo turns out to be a surprisingly generous tourist.\n\n"
            "Nobody is entirely sure whether you stopped German espionage or "
            "simply robbed a German."
        ),
        failure_text=(
            "Diablo noticed something was wrong.\n\n"
            "> Diablo: “I have absolutely no idea what you are talking about.”"
        ),
    ),
    _arch(
        emoji="👥", name="Utopia", full_name="Utopia",
        age=0, location="United States", tier="rare", flag="🇺🇸",
        short_name="Utopia", trait="👥 Rules Are Optional",
        description=(
            "Utopia has money, influence and considerably more accounts than "
            "anybody can explain. Normal rules appear to be more of a "
            "suggestion where he comes from."
        ),
        status="Logged in on several accounts at once",
        chance=0.50, payout_min=100, payout_max=10_000,
        attempt_cost=300, max_failures=2, decays=False, fake_eligible=False,
        no_pot=True, always_arrest=True,
        approaches={
            "careful": (0.64, 0.70, "🔍"),
            "normal":  (0.50, 1.00, "👥"),
            "greedy":  (0.35, 1.80, "🚨"),
        },
        # Which account you happen to open decides everything; greed opens the
        # better ones.  No multiplier and no pot — the band *is* the payout.
        payout_bands={
            "careful": [(100, 1_000, 55), (1_001, 3_000, 30),
                        (3_001, 6_000, 13), (6_001, 10_000, 2)],
            "normal":  [(100, 1_000, 40), (1_001, 3_000, 35),
                        (3_001, 6_000, 20), (6_001, 10_000, 5)],
            "greedy":  [(100, 1_000, 25), (1_001, 3_000, 30),
                        (3_001, 6_000, 32), (6_001, 10_000, 13)],
        },
        special_rules=(
            "Payout is a random account, **100–10.000 Naira**. Greed opens the "
            "better ones. No multiplier, no pot.",
            "🚔 **Every failure gets you arrested**, whether or not you could "
            "have paid.",
        ),
        success_text=(
            "You found one of Utopia's accounts.\n\n"
            "> Utopia: “That wasn't even my main.”"
        ),
        failure_text=(
            "Your attempt failed. Unfortunately, the administrators "
            "investigated the situation and reached the obvious conclusion:\n\n"
            "**You are the cheater.**\n\n> Utopia: “Skill issue.”"
        ),
    ),
    _arch(
        emoji="🥐", name="Louis G. Boulanger", full_name="Louis Guillaume Boulanger",
        age=57, location="Luxembourg", tier="ordinary", flag="🇱🇺",
        short_name="Louis", trait="🥐 Presidential Pastry Chef",
        description=(
            "Once a humble Belgian small-town baker who somehow accidentally "
            "became President. He has since retired to Luxembourg, where his "
            "bakery has six subsidiaries and the croissants are registered as "
            "intellectual property."
        ),
        status="Booking everything as miscellaneous flour expenses",
        chance=0.64, payout_min=250, payout_max=750,
        attempt_cost=75, max_failures=4, decays=False,
        approaches={
            "careful": (0.78, 0.70, "🧾"),
            "normal":  (0.64, 1.00, "🥐"),
            "greedy":  (0.38, 1.70, "💶"),
        },
        success_text=(
            "Louis hands over the money without much resistance. Apparently it "
            "was booked as **“miscellaneous flour expenses.”**\n\n"
            "> Louis: “Please don't tell the Luxembourg tax authorities about "
            "the second bakery.”"
        ),
        failure_text=(
            "Your elaborate financial scheme is defeated by a man whose "
            "primary qualification is knowing the wholesale price of flour."
        ),
    ),
    _arch(
        emoji="💻", name="Nigerian Yahooboy", full_name="Yahooboy",
        age=31, location="Lagos", tier="ordinary", flag="🇳🇬",
        short_name="Yahooboy", trait="💻 Scammer's Instinct",
        description=(
            "An experienced Nigerian internet scammer who has spent years "
            "perfecting romance scams, fake invoices and questionable emails. "
            "Getting money out of him is possible — but he knows every trick "
            "in the book."
        ),
        status="Composing his forty-third email of the morning",
        chance=0.58, payout_min=350, payout_max=900,
        attempt_cost=100, max_failures=4, decays=False,
        reverse_scam=(0.50, 150, 400),
        approaches={
            "careful": (0.74, 0.70, "📨"),
            "normal":  (0.58, 1.00, "💻"),
            "greedy":  (0.34, 1.70, "💳"),
        },
        special_rules=(
            "Every failure has a **50% chance he scams you back** for 150–400 "
            "Naira out of your cash.",
        ),
        success_text=(
            "Against all professional expectations, the Yahooboy approves the "
            "transfer.\n\nHe appears more impressed than angry."
        ),
        failure_text=(
            "The Yahooboy recognises the scam immediately.\n\nMostly because "
            "he wrote it."
        ),
    ),
    _arch(
        emoji="🎯", name="Merel", full_name="Merel",
        age=27, location="Netherlands", tier="great_catch", flag="🇳🇱",
        short_name="Merel", trait="🎯 Marketplace Sniper",
        description=(
            "Once one of the Netherlands' most feared marketplace snipers. If "
            "equipment appeared below market value, Merel had usually bought "
            "it before you finished reading the listing. Currently "
            "unavailable for unspecified administrative reasons. #FreeMerel"
        ),
        status="Appeals remain pending",
        chance=0.20, payout_min=700, payout_max=1_700,
        attempt_cost=175, max_failures=3, decays=False,
        final_bonus=300,
        approaches={
            "careful": (0.28, 0.70, "🖱️"),
            "normal":  (0.20, 1.00, "🎯"),
            "greedy":  (0.10, 1.80, "⚡"),
        },
        # Impossible right up until the listing is about to expire, then wide
        # open — the whole joke is the last-second snipe.
        approach_ladder={
            "careful": [0.28, 0.28, 0.63],
            "normal":  [0.20, 0.20, 0.55],
            "greedy":  [0.10, 0.10, 0.45],
        },
        special_rules=(
            "🎯 **Sniper window.** The first two attempts are nearly hopeless. "
            "When only one attempt remains the odds jump to 63 / 55 / 45%.",
            "A successful final attempt pays **+300 Naira** on top.",
        ),
        success_text=(
            "For once, Merel was not the fastest person clicking the "
            "listing.\n\nSomewhere in the Netherlands, a marketplace "
            "notification arrives several seconds too late."
        ),
        failure_text=(
            "Everyone waited for the perfect moment.\n\nUnfortunately, Merel "
            "was also waiting for the perfect moment.\n\n**#FreeMerel**"
        ),
    ),
    _arch(
        emoji="💥", name="Euler", full_name="Euler",
        age=0, location="Argentina", tier="rare", flag="🇦🇷",
        short_name="Euler", trait="💥 Collateral Damage",
        description=(
            "An Argentinian damage machine who somehow became a Dutch folk "
            "hero by solving most geopolitical problems through excessive "
            "amounts of damage. You came here to scam him. Euler appears to "
            "have misunderstood this as a combat invitation."
        ),
        status="Waiting, calmly, for somebody to start something",
        chance=0.16, payout_min=300, payout_max=800,
        attempt_cost=100, max_failures=4, decays=False, fake_eligible=False,
        seed_pot=750,
        collateral=({"careful": 0.50, "normal": 0.65, "greedy": 0.80}, (500, 750)),
        approaches={
            "careful": (0.24, 0.70, "🛡️"),
            "normal":  (0.16, 1.00, "💥"),
            "greedy":  (0.08, 2.20, "⚔️"),
        },
        special_rules=(
            "🇳🇱 He arrives with a **750 Naira Dutch folk-hero bounty** already "
            "in his pot.",
            "The **first** attempt is safe. After that a failure has a "
            "50/65/80% chance of collateral damage: 500–750 Naira out of your "
            "cash, straight into his pot.",
            "If all four attempts fail, **the entire pot disappears with him**.",
        ),
        success_text=(
            "Against considerable odds, Euler has finally been caught.\n\n"
            "The Netherlands immediately commissions a statue."
        ),
        failure_text=(
            "The scam fails. Euler responds with what Dutch observers describe "
            "as a heroic defensive action."
        ),
    ),
    _arch(
        emoji="😡", name="Beitsas", full_name="Beitsas",
        age=52, location="Vilnius", tier="great_catch", flag="🇱🇹",
        short_name="Beitsas", trait="😡 Presidential Temper",
        description=(
            "Once President of Lithuania. Now mostly known for being angry "
            "about no longer being President of Lithuania. He insists his "
            "administration never ended. The administration strongly disagrees."
        ),
        status="Demanding the resignation of ministers who do not work for him",
        chance=0.44, payout_min=450, payout_max=1_200,
        attempt_cost=175, max_failures=3, decays=False,
        bonus_per_failure=200,
        approaches={
            "careful": (0.58, 0.70, "🤝"),
            "normal":  (0.44, 1.00, "🇱🇹"),
            "greedy":  (0.26, 1.80, "📣"),
        },
        approach_ladder={
            "careful": [0.58, 0.50, 0.42],
            "normal":  [0.44, 0.36, 0.28],
            "greedy":  [0.26, 0.18, 0.10],
        },
        special_rules=(
            "😡 **Presidential rage.** Every failure costs 8 points off every "
            "approach — and adds **+200 Naira** to whatever the eventual "
            "winner collects.",
        ),
        success_text=(
            "Beitsas finally signs the transfer.\n\nHe immediately announces "
            "that the transaction is illegitimate and that he remains the "
            "rightful owner of the money."
        ),
        failure_text=(
            "**“This would never have happened under my government.”**\n\n"
            "Beitsas rejects the proposal. His approval rating remains "
            "unavailable."
        ),
    ),
    _arch(
        emoji="🕴️", name="Prince MVC", full_name="Prince MVC",
        age=0, location="Nigeria", tier="legendary", flag="🇳🇬",
        short_name="MVC", trait="🕴️ The Shadow Network",
        description=(
            "Once Nigeria's feared ruler. Then somebody introduced him to an "
            "open window. Now retired from government, MVC spends his days "
            "enjoying a suspiciously vast private fortune. The government "
            "removed him; his influence apparently did not receive the memo."
        ),
        status="Retired, allegedly",
        chance=0.10, payout_min=4_000, payout_max=10_000,
        attempt_cost=150, max_failures=3, decays=False,
        fake_eligible=False, intel_immune=True,
        shadow_network=(0.15, 750),
        expiry_minutes=120,
        rival=("sultan-mostor", 0.05),
        approaches={
            "careful": (0.16, 0.70, "🤝"),
            "normal":  (0.10, 1.00, "🕴️"),
            "greedy":  (0.06, 1.80, "🪟"),
        },
        special_rules=(
            "🕴️ **The shadow network.** While MVC is on the board he skims "
            "**15% (max 750 Naira)** off every *other* mark's payout, into his "
            "Shadow Treasury. Whoever scams him takes the lot.",
            "His odds never improve, however many people fail.",
            "⏳ **He leaves after two hours** — and the entire Shadow Treasury "
            "leaves with him.",
            "🔒 Completely immune to Intel.",
        ),
        success_text=(
            "The old network finally fails to protect MVC's fortune.\n\n"
            "MVC denies that the money ever existed."
        ),
        failure_text=(
            "Your proposal reaches MVC.\n\nUnfortunately, it also reaches "
            "three former ministers, two businessmen and a man who claims not "
            "to work for the government anymore."
        ),
    ),
    _arch(
        emoji="🥕", name="Sultan Mostor", full_name="His Imperial Aquatic Highness Sultan Mostor I",
        age=0, location="Egypt", tier="whale", flag="🇪🇬",
        short_name="Mostor", trait="🥕 The Carrot & The Stick",
        description=(
            "Ruler of Egypt, master of the Nile and holder of considerably "
            "more titles than administrative responsibilities. Mostor prefers "
            "the carrot. But if you annoy him, there is always the stick."
        ),
        status="Distributor of Carrots, Protector of Twenty Legitimate Accounts",
        chance=0.38, payout_min=2_000, payout_max=5_000,
        attempt_cost=450, max_failures=3, decays=False, fake_eligible=False,
        carrot_stick=(
            {"careful": 0.70, "normal": 0.45, "greedy": 0.20},
            (100, 300), (500, 1_000),
        ),
        approaches={
            "careful": (0.54, 0.60, "🥕"),
            "normal":  (0.38, 1.00, "🌊"),
            "greedy":  (0.20, 1.90, "🪵"),
        },
        special_rules=(
            "🥕 **Carrot or stick.** A failure either refunds your whole 450 "
            "and tips you 100–300 more, or costs you a further 500–1.000.",
            "Careful gets the carrot 70% of the time. Greedy gets it 20%.",
        ),
        success_text=(
            "Against the advice of several advisers and at least one "
            "crocodile, Sultan Mostor approves the transfer.\n\n"
            "His titles remain unaffected."
        ),
        failure_text="Mostor rejects your scam.",
    ),
    _arch(
        emoji="💜", name="Prince Prince", full_name="Prince Prince",
        age=0, location="Nigeria / USA", tier="ordinary", flag="🇺🇸",
        short_name="Prince²", trait="☔ Purple Reign",
        description=(
            "The artist formerly known as Prince. Now known as Prince Prince, "
            "because apparently one Prince was not enough. His royal "
            "legitimacy remains questionable, but the forecast calls for a "
            "Purple Reign."
        ),
        status="Performing, possibly reigning",
        chance=0.62, payout_min=200, payout_max=650,
        attempt_cost=75, max_failures=4, decays=False,
        success_bonus=(0.20, 100, 250),
        approaches={
            "careful": (0.76, 0.70, "🎵"),
            "normal":  (0.62, 1.00, "💜"),
            "greedy":  (0.36, 1.70, "☔"),
        },
        special_rules=(
            "☔ A successful scam has a **20% chance** the purple reign becomes "
            "a green rain: **+100–250 Naira**.",
        ),
        success_text=(
            "Prince Prince approves the transfer.\n\nWhether this constitutes "
            "a scam, a performance fee or a royal subsidy remains unclear."
        ),
        failure_text=(
            "Your payment request is rejected because you addressed it to "
            "**Prince** rather than **Prince Prince**.\n\nApparently this "
            "matters."
        ),
    ),
    _arch(
        emoji="🐶", name="Puppaganda", full_name="Puppaganda",
        age=2, location="Netherlands", tier="ordinary", flag="🇳🇱",
        short_name="Puppa", trait="📰 Too Much Information",
        description=(
            "An extremely local journalist who apparently happens to be a "
            "puppy. Puppaganda talks a lot, writes even more, and has never "
            "encountered a neighbourhood event that could not become a "
            "fourteen-paragraph article."
        ),
        status="Filing 1.700 words about a parking dispute",
        chance=0.48, payout_min=125, payout_max=400,
        attempt_cost=75, max_failures=4, decays=False,
        intel_refill=True,
        approaches={
            "careful": (0.58, 0.70, "🗞️"),
            "normal":  (0.48, 1.00, "🐶"),
            "greedy":  (0.30, 1.70, "📢"),
        },
        special_rules=(
            "🔎 **Success refills your Intel charges to 3/3.** He tells you "
            "everything, at length, whether you asked or not.",
        ),
        success_text=(
            "Puppaganda was investigating your scam when he accidentally "
            "approved the payment.\n\nHe then published everything he learned "
            "during the investigation."
        ),
        failure_text=(
            "Puppaganda sees through your scam.\n\nUnfortunately, explaining "
            "why requires eleven paragraphs, four screenshots and interviews "
            "with three people who were not involved."
        ),
    ),
    _arch(
        emoji="🚫", name="Roas", full_name="Roas",
        age=0, location="Friesland, allegedly", tier="ordinary", flag="🇳🇱",
        short_name="Roas", trait="🚫 De Blokkeerfries",
        description=(
            "Apparently Dutch, although Roas insists she is “Frysian”. Nobody "
            "has yet established what practical difference this makes. Enjoys "
            "ice skating, suikerbrood and preventing other people from "
            "reaching their destination."
        ),
        status="Standing in the road on principle",
        chance=0.60, payout_min=300, payout_max=800,
        attempt_cost=75, max_failures=3, decays=False,
        global_lock_minutes=60,
        approaches={
            "careful": (0.76, 0.70, "🍞"),
            "normal":  (0.60, 1.00, "⛸️"),
            "greedy":  (0.34, 1.70, "🚫"),
        },
        special_rules=(
            "🚫 **A failure blocks you from every target on the board for 60 "
            "minutes.** Not just Roas. All of them.",
            "It does not block `/scam`, Intel, quick scams or the fund.",
        ),
        success_text=(
            "You distract Roas with discounted suikerbrood long enough to "
            "complete the transfer.\n\nFriesland remains inaccessible for "
            "unrelated reasons."
        ),
        failure_text=(
            "Roas sees through your scam.\n\nBefore you can try somebody else, "
            "she blocks the entire road. Roas claims this is an important "
            "Frysian tradition."
        ),
    ),
]

# Superseded by TIER_WEIGHTS above; kept only so nothing references a name
# that no longer exists.
_TIER_WEIGHTS = TIER_WEIGHTS

_ARCH_BY_ID = {a["arch_id"]: a for a in _ARCHETYPES}
if len(_ARCH_BY_ID) != len(_ARCHETYPES):
    raise ValueError("duplicate archetype arch_id")


def _dominated_approaches(arch: dict) -> list[str]:
    """Approaches on this mark that are worse than another on *both* axes.

    A mark whose Careful is both less likely *and* smaller-paying than its
    Normal offers a button that is never the right click.  That is a perfectly
    good trap — the roster deliberately breaks "careful is safest" — but only
    if the card says so.  Undocumented, it just reads as a bug, which is
    exactly how Tio Men's 28% Careful was first reported.
    """
    spec = arch.get("approaches")
    if not spec or arch.get("approach_payouts") or not arch.get("payout_max"):
        return []
    keys = ("careful", "normal", "greedy")
    dead = []
    for k in keys:
        for other in keys:
            a, b = spec[k], spec[other]
            if k != other and a[0] <= b[0] and a[1] <= b[1] and a[:2] != b[:2]:
                dead.append(k)
                break
    return dead


_UNDOCUMENTED = []
for _a in _ARCHETYPES:
    if not _dominated_approaches(_a):
        continue
    _text = " ".join(_a.get("special_rules") or ()).lower()
    if not any(w in _text for w in
               ("careful", "normal", "greedy", "all three approaches")):
        _UNDOCUMENTED.append(_a["name"])
if _UNDOCUMENTED:
    raise ValueError(
        "these marks invert the usual approach order without explaining it on "
        f"the card: {_UNDOCUMENTED}. Add a special_rules line naming the "
        "approach, or give it odds that are not strictly dominated."
    )


def _by_tier(tier: str) -> list[dict]:
    return [a for a in _ARCHETYPES if a["tier"] == tier]


def archetype_for(t: dict) -> Optional[dict]:
    """The archetype a live target was spawned from, if it still exists.

    Targets spawned before per-mark mechanics existed carry no ``arch_id``, and
    an archetype can be removed from the roster while one of its marks is still
    on the board.  Both cases fall back to the original global behaviour rather
    than failing.
    """
    return _ARCH_BY_ID.get(str(t.get("arch_id") or ""))


def approach_spec(t: dict, approach: str) -> tuple[float, float, str]:
    """``(success chance, payout multiplier, flavour emoji)`` for one approach.

    The board labels its buttons with the mark's own emoji, so the third value
    is flavour the roster carries rather than something currently rendered.

    The originals read the global :data:`APPROACHES` shift table.  The newer
    personas each carry their own, which is what lets greedy be Armand's *best*
    odds, careful be Diligent Doubt's, and every approach be identical on
    Gerard.
    """
    arch = archetype_for(t)
    spec = (arch or {}).get("approaches")
    if not spec:
        shift, mult, label = APPROACHES.get(approach, APPROACHES["normal"])
        return (
            max(MIN_CHANCE, min(0.95, t["chance"] + shift)),
            mult,
            label[:1],
        )

    value, mult, emoji = spec.get(approach, spec["normal"])

    # A per-approach ladder overrides everything else: it already encodes the
    # mark's whole progression, whether it hardens (rage) or opens up (sniper).
    ladder = arch.get("approach_ladder")
    if ladder and approach in ladder:
        steps = ladder[approach]
        base = steps[min(int(t.get("failures") or 0), len(steps) - 1)]
        base += float(t.get("investigation_bonus") or 0.0)
        base += float(t.get("rival_bonus") or 0.0)
        return max(0.01, min(arch["chance_cap"], base)), mult, emoji

    mode = arch["approach_mode"]
    if mode == "multiply":
        chance = t["chance"] * value
    elif mode == "shift":
        chance = t["chance"] + value
    else:
        # Absolute odds ignore the stored chance entirely, so an investigation
        # bonus (which the investigator folds into that column) has to be added
        # back explicitly or it would silently do nothing.
        chance = (
            value
            + float(t.get("investigation_bonus") or 0.0)
            + float(t.get("rival_bonus") or 0.0)
        )
    # MIN_CHANCE is a floor for *derived* odds — it exists so the old shift
    # table cannot push a wary mark to zero.  Declared odds are deliberate:
    # clamping Henk's 1,5% greedy up to 5% would make greedy his best line and
    # invert the whole point of him.  Only guard against genuinely negative
    # results from the shift/multiply modes.
    floor = 0.01 if mode == "absolute" else MIN_CHANCE
    return max(floor, min(arch["chance_cap"], chance)), mult, emoji


# ── Schema ────────────────────────────────────────────────────────────────────

async def setup_schema(conn: aiosqlite.Connection) -> None:
    """Create the target tables. Safe to call on every startup."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_targets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            slot          INTEGER NOT NULL,
            emoji         TEXT NOT NULL,
            name          TEXT NOT NULL,
            flavour       TEXT NOT NULL,
            tier          TEXT NOT NULL,
            chance        REAL NOT NULL,
            base_chance   REAL NOT NULL,
            payout_min    INTEGER NOT NULL,
            payout_max    INTEGER NOT NULL,
            attempt_cost  INTEGER NOT NULL,
            pot           INTEGER NOT NULL DEFAULT 0,
            failures      INTEGER NOT NULL DEFAULT 0,
            max_failures  INTEGER NOT NULL,
            expires_at    TEXT,
            created_at    TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'active'
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_target_slots (
            slot       INTEGER PRIMARY KEY,
            target_id  INTEGER,
            respawn_at TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_target_attempts (
            target_id       INTEGER NOT NULL,
            discord_user_id TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            lost            INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (target_id, discord_user_id)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_target_board (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            channel_id TEXT,
            message_id TEXT
        )
    """)
    await conn.execute(
        "INSERT OR IGNORE INTO scam_target_board (id, channel_id, message_id)"
        " VALUES (1, NULL, NULL)"
    )
    for column in (
        "is_fake INTEGER NOT NULL DEFAULT 0",
        "fake_owner_id TEXT",
        "cover_deposit INTEGER NOT NULL DEFAULT 0",
        "investigation_bonus REAL NOT NULL DEFAULT 0",
    ):
        try:
            await conn.execute(f"ALTER TABLE scam_targets ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    try:
        await conn.execute(
            "ALTER TABLE scam_players ADD COLUMN fake_target_until TEXT"
        )
    except Exception:
        pass
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_investigations (
            target_id       INTEGER NOT NULL,
            investigator_id TEXT NOT NULL,
            verdict         TEXT NOT NULL,
            accurate        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (target_id, investigator_id)
        )
    """)
    for column in (
        "intel_charges INTEGER", "intel_next_charge_at TEXT",
        # intel_lock_until (the old two-minute post-Intel lock) is gone; see
        # the note in scam_game's migration list.
        # Roas blocks the whole board, not just herself.
        "target_lock_until TEXT",
        # Babu's decree, enforced in the game channel only.
        "silenced_until TEXT",
    ):
        try:
            await conn.execute(f"ALTER TABLE scam_players ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    try:
        await conn.execute(
            "ALTER TABLE scam_targets ADD COLUMN intel_missions INTEGER NOT NULL DEFAULT 0"
        )
    except Exception:
        pass
    # One row per Intel report, so a Counter-Scam button can be bound to the
    # exact report that justified it and cannot be reused or forged.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_intel_reports (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id     INTEGER NOT NULL,
            user_id       TEXT NOT NULL,
            report_class  TEXT NOT NULL,
            claim         TEXT NOT NULL,
            reliability   REAL,
            takedown      REAL NOT NULL DEFAULT 0,
            overall       REAL NOT NULL DEFAULT 0,
            stake         INTEGER NOT NULL DEFAULT 0,
            consumed      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )
    """)
    # The hidden FIFO queue of players waiting for a board slot to disguise
    # into.  Queue state is private; only going live is announced, and only to
    # the player themselves.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS scam_fake_queue (
            owner_id      TEXT PRIMARY KEY,
            state         TEXT NOT NULL,     -- queued | active | cancelling
            deposit       INTEGER NOT NULL,
            queued_at     TEXT NOT NULL,
            queue_expires TEXT,
            target_id     INTEGER,
            cancel_at     TEXT
        )
    """)
    for column in ("posted_at TEXT", "last_attempt_at TEXT"):
        try:
            await conn.execute(f"ALTER TABLE scam_target_board ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    for column in (
        "full_name TEXT", "age INTEGER", "location TEXT",
        "status_flavour TEXT", "success_text TEXT", "failure_text TEXT",
        # Which archetype this mark came from, so its per-mark mechanics
        # (approach odds, chance ladder, pot rate) can be looked up live
        # instead of being frozen into the row at spawn time.
        "arch_id TEXT",
        "rival_bonus REAL NOT NULL DEFAULT 0",
        "warned INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await conn.execute(f"ALTER TABLE scam_targets ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    # Per-player attempt cooldown lives on the existing players table.
    try:
        await conn.execute(
            "ALTER TABLE scam_players ADD COLUMN last_target_at TEXT"
        )
    except Exception:
        pass  # column already present
    for slot in range(BOARD_SLOTS):
        await conn.execute(
            "INSERT OR IGNORE INTO scam_target_slots (slot, target_id, respawn_at)"
            " VALUES (?, NULL, NULL)",
            (slot,),
        )
    await conn.commit()


# ── Target creation ───────────────────────────────────────────────────────────

def _pick_archetype(
    force_rare: bool = False, *, fake_only: bool = False,
    exclude: Optional[set[str]] = None,
) -> dict:
    """Pick a mark to spawn: roll a tier, then a persona inside it.

    Tier first, persona second, so the *quality* mix of the board is a single
    tunable table rather than an emergent property of how many personas happen
    to sit in each band.  ``fake_only`` restricts the roll to personas a player
    may be disguised as — a fake resolves on the first attack, which some
    mechanics cannot survive.
    """
    exclude = exclude or set()

    def pool_for(tier: str) -> list[dict]:
        return [
            a for a in _ARCHETYPES
            if a["tier"] == tier
            and a["name"] not in exclude
            and (a["fake_eligible"] or not fake_only)
        ]

    tiers = [t for t, _w in TIER_WEIGHTS]
    weights = [w for _t, w in TIER_WEIGHTS]
    if force_rare:
        tiers, weights = ["rare", "whale"], [65, 35]

    for _attempt in range(12):
        tier = random.choices(tiers, weights=weights, k=1)[0]
        pool = pool_for(tier)
        if pool:
            # Weighted *within* the tier, not uniform: Marijn should be the
            # legendary you actually meet, while Darkodor stays the one people
            # talk about having seen once.
            return random.choices(
                pool, weights=[a["spawn_weight"] for a in pool], k=1
            )[0]
    # Every tier we rolled was empty (tiny roster, or everything excluded);
    # fall back to anything legal rather than failing to fill the slot.
    everything = [
        a for a in _ARCHETYPES
        if a["name"] not in exclude and (a["fake_eligible"] or not fake_only)
    ] or _ARCHETYPES
    return random.choices(
        everything, weights=[a["spawn_weight"] for a in everything], k=1
    )[0]


async def spawn_target(
    conn: aiosqlite.Connection, slot: int, *, force_rare: bool = False,
    fake_only: bool = False,
) -> dict:
    """Create a new target in *slot* and return it.

    Re-rolls if the archetype is already on the board — three identical Truuses
    would make the shared-pot mechanic meaningless (and look broken).
    """
    taken = {t["name"] for t in await active_targets(conn)}
    arch = _pick_archetype(force_rare, fake_only=fake_only, exclude=taken)

    # Whales are on a flat one-hour clock; nothing resets it.  A persona may
    # override the duration (MVC gets two hours to work his network).
    expires_at = None
    minutes = arch["expiry_minutes"] or (
        WHALE_EXPIRY_MINUTES if arch["tier"] in EXPIRING_TIERS else 0
    )
    if minutes:
        expires_at = _iso(_now() + timedelta(minutes=minutes))

    cur = await conn.execute(
        "INSERT INTO scam_targets"
        " (slot, emoji, name, flavour, tier, chance, base_chance, payout_min,"
        "  payout_max, attempt_cost, pot, failures, max_failures, expires_at,"
        "  created_at, status, full_name, age, location, status_flavour,"
        "  success_text, failure_text, arch_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 'active',"
        "         ?, ?, ?, ?, ?, ?, ?)",
        (slot, arch["emoji"], arch["name"], arch["description"], arch["tier"],
         arch["chance"], arch["chance"], arch["payout_min"], arch["payout_max"],
         arch["attempt_cost"], arch["max_failures"], expires_at, _iso(_now()),
         arch["full_name"], arch["age"], arch["location"], arch["status"],
         arch["success_text"], arch["failure_text"], arch["arch_id"]),
    )
    target_id = int(cur.lastrowid)
    # Seeded once, at spawn: Euler arrives with a bounty already on his head.
    if arch["seed_pot"]:
        await conn.execute(
            "UPDATE scam_targets SET pot = ? WHERE id = ?",
            (arch["seed_pot"], target_id),
        )
    await conn.execute(
        "UPDATE scam_target_slots SET target_id = ?, respawn_at = NULL WHERE slot = ?",
        (target_id, slot),
    )
    await conn.commit()
    await sync_rivalries(conn)
    return await get_target(conn, target_id)


async def sync_rivalries(conn: aiosqlite.Connection) -> None:
    """Apply the extra odds a mark gets while its rival is also on the board.

    Stored on the row rather than computed at render time so the card, the
    buttons and the roll all agree, and so it survives a restart.
    """
    live = await active_targets(conn)
    present = {str(t.get("arch_id") or "") for t in live}
    for t in live:
        arch = archetype_for(t)
        bonus = 0.0
        if arch and arch["rival"]:
            other, gain = arch["rival"]
            if other in present:
                bonus = gain
        if abs(float(t.get("rival_bonus") or 0.0) - bonus) > 1e-9:
            await conn.execute(
                "UPDATE scam_targets SET rival_bonus = ? WHERE id = ?",
                (bonus, t["id"]),
            )
    await conn.commit()


async def get_target(conn: aiosqlite.Connection, target_id: int) -> Optional[dict]:
    async with conn.execute(
        "SELECT id, slot, emoji, name, flavour, tier, chance, base_chance,"
        " payout_min, payout_max, attempt_cost, pot, failures, max_failures,"
        " expires_at, status, full_name, age, location, status_flavour,"
        " success_text, failure_text, is_fake, fake_owner_id, cover_deposit,"
        " investigation_bonus, arch_id, rival_bonus"
        " FROM scam_targets WHERE id = ?",
        (target_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    keys = ("id", "slot", "emoji", "name", "flavour", "tier", "chance",
            "base_chance", "payout_min", "payout_max", "attempt_cost", "pot",
            "failures", "max_failures", "expires_at", "status", "full_name",
            "age", "location", "status_flavour", "success_text",
            "failure_text", "is_fake", "fake_owner_id", "cover_deposit",
            "investigation_bonus", "arch_id", "rival_bonus")
    d = dict(zip(keys, row))
    for k in ("id", "slot", "payout_min", "payout_max", "attempt_cost", "pot",
              "failures", "max_failures", "is_fake", "cover_deposit"):
        d[k] = int(d[k] or 0)
    d["investigation_bonus"] = float(d["investigation_bonus"] or 0.0)
    d["rival_bonus"] = float(d["rival_bonus"] or 0.0)
    d["chance"] = float(d["chance"])
    d["base_chance"] = float(d["base_chance"])
    return d


async def active_targets(conn: aiosqlite.Connection) -> list[dict]:
    """Return the active targets currently on the board, by slot."""
    out: list[dict] = []
    async with conn.execute(
        "SELECT t.id FROM scam_target_slots s"
        " JOIN scam_targets t ON t.id = s.target_id"
        " WHERE t.status = 'active' ORDER BY s.slot"
    ) as cur:
        ids = [int(r[0]) async for r in cur]
    for tid in ids:
        t = await get_target(conn, tid)
        if t:
            out.append(t)
    return out



# ── Fake targets ──────────────────────────────────────────────────────────────

async def active_fake_target(conn: aiosqlite.Connection) -> Optional[dict]:
    """Return the fake target currently on the board, if any."""
    for t in await active_targets(conn):
        if t["is_fake"]:
            return t
    return None


async def impersonating(conn: aiosqlite.Connection, user_id: str) -> Optional[dict]:
    """Return the fake target this player is currently posing as, if any."""
    fake = await active_fake_target(conn)
    if fake and str(fake["fake_owner_id"]) == str(user_id):
        return fake
    return None


async def record_investigation(
    conn: aiosqlite.Connection, target_id: int, user_id: str,
    verdict: str, accurate: bool,
) -> None:
    await conn.execute(
        "INSERT INTO scam_investigations"
        " (target_id, investigator_id, verdict, accurate, created_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(target_id, investigator_id) DO UPDATE SET"
        " verdict = excluded.verdict, accurate = excluded.accurate",
        (target_id, str(user_id), verdict, 1 if accurate else 0, _iso(_now())),
    )
    await conn.commit()


async def knows_target_is_fake(
    conn: aiosqlite.Connection, target_id: int, user_id: str
) -> bool:
    """True if this player investigated and was correctly told it was fake."""
    async with conn.execute(
        "SELECT verdict, accurate FROM scam_investigations"
        " WHERE target_id = ? AND investigator_id = ?",
        (target_id, str(user_id)),
    ) as cur:
        row = await cur.fetchone()
    return bool(row and row[0] == "fake" and int(row[1]))




# ── Intel: charges, cost, odds ────────────────────────────────────────────────

async def intel_state(conn: aiosqlite.Connection, user_id: str) -> dict:
    """Charges and recharge timer for one player.

    Regeneration is *serial* and computed lazily from a single timestamp: how
    many whole recharge periods have elapsed since ``intel_next_charge_at``.
    No background task, and a restart cannot lose or duplicate a charge.
    """
    async with conn.execute(
        "SELECT intel_charges, intel_next_charge_at"
        " FROM scam_players WHERE discord_user_id = ?",
        (str(user_id),),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return {"charges": INTEL_MAX_CHARGES, "next_at": None}

    charges = INTEL_MAX_CHARGES if row[0] is None else int(row[0])
    next_at = _parse(row[1]) if row[1] else None
    now = _now()

    if charges < INTEL_MAX_CHARGES and next_at:
        period = timedelta(hours=INTEL_RECHARGE_HOURS)
        gained = 0
        while next_at <= now and charges + gained < INTEL_MAX_CHARGES:
            gained += 1
            next_at = next_at + period
        if gained:
            charges += gained
            new_next = None if charges >= INTEL_MAX_CHARGES else _iso(next_at)
            await conn.execute(
                "UPDATE scam_players SET intel_charges = ?,"
                " intel_next_charge_at = ? WHERE discord_user_id = ?",
                (charges, new_next, str(user_id)),
            )
            await conn.commit()
            next_at = None if charges >= INTEL_MAX_CHARGES else next_at

    return {"charges": charges, "next_at": next_at}


def _full_charges_at(state: dict) -> datetime:
    """When the player is back to a full hand.

    Recharging is serial, so this is the next charge plus one whole period for
    each one still missing behind it.  Worth showing: at four hours a charge,
    "next charge in 3h" understates the wait back to three by eight hours.
    """
    missing = max(0, INTEL_MAX_CHARGES - state["charges"] - 1)
    return state["next_at"] + timedelta(hours=INTEL_RECHARGE_HOURS) * missing


async def spend_intel_charge(conn: aiosqlite.Connection, user_id: str) -> None:
    """Consume one charge and start the recharge clock if it was full."""
    state = await intel_state(conn, user_id)
    charges = max(0, state["charges"] - 1)
    next_at = state["next_at"]
    if next_at is None:
        next_at = _now() + timedelta(hours=INTEL_RECHARGE_HOURS)
    await conn.execute(
        "UPDATE scam_players SET intel_charges = ?, intel_next_charge_at = ?"
        " WHERE discord_user_id = ?",
        (charges, _iso(next_at), str(user_id)),
    )



def intel_cost(tier: str) -> int:
    return INTEL_COST.get(tier, 0)


def apply_intel_ceiling(base: float, bonus: float) -> float:
    """§12: Intel lifts an approach toward 95%, but never drags one down.

    Zwieber's careful line is naturally 97%; capping the *result* at 95% would
    make buying Intel actively harmful, which is the opposite of the point.
    """
    if base > INTEL_ODDS_CEILING:
        return base
    return min(INTEL_ODDS_CEILING, base + bonus)


def roll_intel_gain(tier: str) -> tuple[float, bool, float]:
    """``(base gain, breakthrough?, breakthrough bonus)`` before the tier cap."""
    lo, hi = INTEL_GAIN.get(tier, (0.0, 0.0))
    base = random.uniform(lo, hi)
    broke = random.random() < INTEL_BREAKTHROUGH_CHANCE.get(tier, 0.0)
    extra = 0.0
    if broke:
        blo, bhi = INTEL_BREAKTHROUGH_BONUS.get(tier, (0.0, 0.0))
        extra = random.uniform(blo, bhi)
    return base, broke, extra


def roll_report_class() -> tuple[str, Optional[float]]:
    keys = [r[0] for r in INTEL_REPORTS]
    weights = [r[1] for r in INTEL_REPORTS]
    key = random.choices(keys, weights=weights, k=1)[0]
    return key, dict((r[0], r[2]) for r in INTEL_REPORTS)[key]


def counter_stake(t: dict) -> int:
    return max(COUNTER_STAKE_MIN, int(t["attempt_cost"]))


# ── Exposed wealth (§24/§25) ──────────────────────────────────────────────────

async def exposed_wealth(conn: aiosqlite.Connection, user_id: str) -> int:
    """Cash plus fund position.

    PvP measures wealth this way so hiding money in Roger's fund before a
    fight does not make you a smaller target.
    """
    async with conn.execute(
        "SELECT balance, invested FROM scam_players WHERE discord_user_id = ?",
        (str(user_id),),
    ) as cur:
        row = await cur.fetchone()
    return (int(row[0]) + int(row[1])) if row else 0


async def seize_wealth(
    conn: aiosqlite.Connection, victim: str, amount: int,
    *, to: Optional[str] = None, reason: str = "fake_loss",
    gain_reason: str = "fake_win", detail: Optional[str] = None,
) -> int:
    """Take *amount* from a player's cash, then their fund position.

    Cash first, and only what cash cannot cover is force-liquidated out of the
    fund.  A forced liquidation is an involuntary seizure, so it deliberately
    skips Roger's withdrawal tax, the 15-minute risk window and Withdrawal
    Panic — none of which should fire because somebody else robbed you.

    The fund's accounting invariant survives because positions *are* the fund:
    reducing the position reduces the total by exactly the same amount.
    Returns the amount actually taken.
    """
    async with conn.execute(
        "SELECT balance, invested FROM scam_players WHERE discord_user_id = ?",
        (str(victim),),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return 0
    cash, fund = int(row[0]), int(row[1])
    # Never strip a player below the protected floor across both pots.
    allowed = max(0, (cash + fund) - PROTECTED_WEALTH)
    take = max(0, min(int(amount), allowed))
    if take <= 0:
        return 0

    from_cash = min(cash, take)
    from_fund = take - from_cash
    await conn.execute(
        "UPDATE scam_players SET balance = balance - ?, invested = invested - ?"
        " WHERE discord_user_id = ?",
        (from_cash, from_fund, str(victim)),
    )
    # Logged explicitly: the cash half never went through adjust_balance for
    # the victim, and the fund half never touches cash at all.
    await record_ledger(conn, victim, -take, reason, detail)
    if from_fund:
        # Principal, not performance: the position shrank because somebody
        # else took the money, which is not something the fund did to them.
        from nigeria_bot import royal_fund as rf
        await rf.record_pnl(conn, victim, -from_fund,
                            f"Forced liquidation — {detail or reason}",
                            kind="withdraw")
    if to:
        await adjust_balance(conn, to, take, gain_reason, detail)
    if from_fund:
        logger.info(
            "scam_targets: PvP force-liquidated %d from %s's fund position",
            from_fund, victim,
        )
    return take


def _pct_of(wealth: int, span: tuple[float, float], cap: int) -> int:
    return min(cap, int(round(wealth * random.uniform(*span))))


# ── Presentation ──────────────────────────────────────────────────────────────

def _chance_after_failure(t: dict, arch: Optional[dict], failures: int) -> float:
    """The mark's new base chance once an attempt has just failed.

    Three behaviours, and the difference between them is most of what makes
    the roster interesting:

    * a **ladder** moves the odds to a scripted value — Koen wakes up, Sachiko
      forgets why she was suspicious;
    * **fixed-odds** marks do not move at all, because their limit is how many
      attempts they tolerate, not how wary they get;
    * everyone else gets the original flat suspicion decay.
    """
    bonus = float(t.get("investigation_bonus") or 0.0)
    if arch and arch.get("chance_ladder"):
        ladder = arch["chance_ladder"]
        base = ladder[min(failures, len(ladder) - 1)]
        return max(MIN_CHANCE, min(arch["chance_cap"], base + bonus))
    if arch and not arch.get("decays", True):
        return t["chance"]
    return max(MIN_CHANCE, t["chance"] - SUSPICION_PER_FAILURE)


def effective_chance(t: dict, approach: str) -> float:
    """Success chance for *approach* against this target, after clamping."""
    return approach_spec(t, approach)[0]


def approach_multiplier(t: dict, approach: str) -> float:
    return approach_spec(t, approach)[1]


def approach_preview(t: dict) -> str:
    """Show what each approach is actually worth against this mark.

    Without this the choice is a guess — and now that odds and multipliers are
    per-mark it is not even a guess a veteran could make from experience.
    """
    parts = []
    for key in ("careful", "normal", "greedy"):
        chance, mult, _emoji = approach_spec(t, key)
        label = APPROACHES[key][2].lower()
        shown = FOG_MASK if _FOG else f"{chance * 100:.0f}%"
        parts.append(f"{label} {shown} (×{mult:g} payout)")
    return " · ".join(parts)


def shows_countdown(t: dict) -> bool:
    """Whether this mark's expiry is public.

    Keyed off the *persona*, never the stored timestamp: every fake target
    carries an internal expiry, so rendering that would announce the trap.
    A persona either advertises a clock (whales, and MVC's two-hour escape)
    or it does not.
    """
    arch = archetype_for(t)
    if arch and arch["expiry_minutes"]:
        return True
    return tier_of(t) in EXPIRING_TIERS


def tier_of(t: dict) -> str:
    """The mark's tier, preferring the archetype over the stored column.

    Targets spawned before tiers were reworked still carry the old easy/medium/
    hard labels.  Reading the archetype first means those rows show the right
    tier immediately instead of needing a migration or a board wipe.
    """
    arch = archetype_for(t)
    if arch and arch["tier"] in TIER_LABEL:
        return arch["tier"]
    stored = t.get("tier")
    return stored if stored in TIER_LABEL else "ordinary"


def _countdown(expires_at: str) -> tuple[str, bool]:
    """``(human remaining, is urgent)`` for a mark on a clock."""
    left = _parse(expires_at) - _now()
    minutes = max(0, int(left.total_seconds() // 60))
    urgent = minutes < WHALE_URGENT_MINUTES
    return (f"{minutes}m" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"), urgent


def target_card(t: dict, index: int) -> discord.Embed:
    """One mark, one embed.

    The three cards all go through this renderer so the board reads as a set
    rather than three differently-shaped walls of text.  Everything that
    changes a decision — odds, cost, payout, pot, timer, special mechanics —
    is in a fixed place; flavour never hides a mechanic.
    """
    arch = archetype_for(t)
    tier = tier_of(t)
    flag = (arch or {}).get("flag", "")
    trait = (arch or {}).get("trait")

    embed = discord.Embed(
        title=f"{t['emoji']} #{index} {t['name'].upper()} {flag}".strip()
              + f"  •  {TIER_LABEL[tier]}",
        colour=_TIER_COLOUR.get(tier, _EMBED_GOLD),
    )

    body = []
    if trait:
        body.append(f"**{trait}**")
    body.append(f"*{t['flavour']}*")

    # The one information row: odds, cost, payout — always together.
    cost = "**FREE**" if not t["attempt_cost"] else f"**{money(t['attempt_cost'])}**"
    if arch and arch.get("approach_payouts"):
        payout = "**see approaches**"
    else:
        payout = f"**{money(t['payout_min'])} – {money(t['payout_max'])}**"
    body.append(
        f"\n🎯 **{effective_chance(t, 'normal') * 100:.1f}%**   "
        f"💸 {cost}   💰 {payout}"
    )

    lines = []
    for key in ("careful", "normal", "greedy"):
        chance, mult, _emoji = approach_spec(t, key)
        label = APPROACHES[key][2]
        if arch and arch.get("approach_payouts"):
            lo, hi = arch["approach_payouts"][key]
            tail = f"{money(lo)} – {money(hi)}"
        else:
            tail = f"×{mult:g}"
        lines.append(
            f"{APPROACH_ICON[key]} **{label}**  {_odds(chance)}  •  {tail}"
        )
    body.append("\n".join(lines))

    rules = list((arch or {}).get("special_rules", ()))
    if rules:
        body.append(
            "⚙️ **SPECIAL RULES**\n" + "\n".join(f"• {r}" for r in rules)
        )

    tail = []
    if t["pot"]:
        tail.append(f"💰 **Pot:** +{money(t['pot'])}")
    if t["expires_at"] and shows_countdown(t):
        left, urgent = _countdown(t["expires_at"])
        warn = "⚠️ " if urgent else ""
        icon = "🐋" if tier in EXPIRING_TIERS else "⏳"
        tail.append(f"{warn}{icon} **Leaves in:** {left}")
        if arch and arch["shadow_network"] and t["pot"]:
            tail.append(
                f"👑 **Shadow treasury:** {money(t['pot'])} "
                "_(leaves with him)_"
            )
    if arch and arch.get("one_shot"):
        tail.append("🔥 **ONE ATTEMPT — ever**")
    else:
        tail.append(f"🔥 **Attempts:** {t['failures']}/{t['max_failures']}")
    if t.get("status_flavour"):
        tail.append(f"📍 *{t['status_flavour']}*")
    body.append("\n".join(tail))

    embed.description = "\n\n".join(body)
    return embed


# Fog of War is a *renderer* switch and nothing else.  The flag is set
# immediately before a synchronous render and cleared immediately after, so no
# other coroutine can observe it, and no target's stored odds ever move — which
# is what the spec insists on: mask the display or cut the card, never touch
# the maths.
_FOG = False
FOG_MASK = "???"


def _odds(chance: float) -> str:
    """Percentages that stay readable at Darkodor's end of the scale."""
    if _FOG:
        return FOG_MASK
    pct = chance * 100
    # Darkodor lives at 1–2%, where rounding to whole percent loses the whole
    # distinction between his approaches.
    return f"{pct:g}%" if pct < 10 else f"{pct:.0f}%"


def board_header(targets: list[dict], slots_waiting: list[str]) -> discord.Embed:
    """The compact header. Deliberately short — the rules live in /targethelp."""
    if targets:
        n = len(targets)
        intro = (
            f"{'One mark is' if n == 1 else f'{n} marks are'} currently "
            "available. Everyone works the same board."
        )
    else:
        intro = "The street is quiet. No marks available right now."
    lines = [
        intro, "",
        "🛡️ **Careful** — safer, smaller payout",
        "🎯 **Normal** — standard odds and payout",
        "🤑 **Greedy** — riskier, larger payout",
        "🔎 **Intel** — buy information, and improve the odds for everybody",
    ]
    if slots_waiting:
        lines += ["", *slots_waiting]
    return discord.Embed(
        title="🎯 TARGET BOARD",
        description="\n".join(lines),
        colour=_EMBED_GOLD,
    )


def board_embeds(targets: list[dict], slots_waiting: list[str], *,
                 fog: bool = False, heat_until: Optional[datetime] = None
                 ) -> list[discord.Embed]:
    """Header plus one card per mark, in slot order."""
    global _FOG
    _FOG = fog
    try:
        out = [board_header(targets, slots_waiting)]
        for i, t in enumerate(targets, 1):
            out.append(target_card(t, i))
        if heat_until:
            # Display only: no mark's odds move because of Heat.
            out[0].description += (
                f"\n\n🔥 **TARGET BOARD HEAT — "
                f"{_mmss(heat_until - _now())} REMAINING**\n"
                "The next player to fail against a real mark carries a **50%** "
                "chance of arrest. 🎭 Fake targets are unaffected."
            )
        if fog:
            out[0].description += (
                "\n\n🌫️ **FOG OF WAR** — every chance on this board is "
                "hidden. The real odds have not changed."
            )
        return out
    finally:
        _FOG = False



# ── Intel presentation ────────────────────────────────────────────────────────

def _plain(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=_EMBED_GREY)


def _mmss(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    return f"{total // 60}:{total % 60:02d}"


def _hhmm(delta: timedelta) -> str:
    """Recharge waits run to hours now, and "213m" is not a readable wait."""
    total = max(0, int(delta.total_seconds()))
    hours, minutes = total // 3600, (total % 3600) // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _intel_report_embed(
    t: dict, tier: str, cost: int, report_class: str,
    reliability: Optional[float], claim: str,
    base: float, broke: bool, extra: float, gained: float,
    total_bonus: float, cap: float,
    takedown: float, overall: float, stake: int,
    charges: dict,
    odds_before: Optional[dict] = None,
    odds_after: Optional[dict] = None,
) -> discord.Embed:
    """The private report. Everything a decision needs, nothing public."""
    if report_class == "verified":
        title, colour = "🌟 VERIFIED INTELLIGENCE", _EMBED_GOLD
        head = ("Report type chance: **20%**\nReport reliability: **100%**")
    elif report_class == "strong":
        title, colour = "🟡 STRONG LEAD", discord.Colour(0xF1C40F)
        head = ("Report type chance: **60%**\nReport reliability: **80%**")
    else:
        title, colour = "❓ INCONCLUSIVE", _EMBED_GREY
        head = "Report type chance: **20%**"

    body = [f"**{t['emoji']} {t['name']}**  ·  {TIER_LABEL[tier]}", "", head, ""]

    if claim == "fake":
        if report_class == "verified":
            body.append(
                "Independent records confirm that **another player is behind "
                "this identity**.\n\n🎭 **ASSESSMENT: VERIFIED FAKE**"
            )
        else:
            body.append(
                "Your source believes this mark is:\n\n🎭 **LIKELY FAKE**\n\n"
                "False-lead risk: **20%**"
            )
    elif claim == "real":
        if report_class == "verified":
            body.append(
                "Independent records confirm that this identity is genuine.\n\n"
                "✅ **ASSESSMENT: REAL**"
            )
        else:
            body.append(
                "Your source believes this mark is:\n\n✅ **LIKELY REAL**\n\n"
                "False-lead risk: **20%**"
            )
    else:
        body.append(
            "Your investigators found useful operational information but "
            "could not verify whether this identity is genuine.\n\n"
            "**Identity assessment: UNKNOWN**"
        )

    body.append("")
    if broke:
        body.append(
            "🌟 **MAJOR INTELLIGENCE BREAKTHROUGH**\n"
            "Internal documents dramatically improve the operation.\n"
            f"Base intel gain: **+{base * 100:.0f}pp**\n"
            f"Breakthrough bonus: **+{extra * 100:.0f}pp**"
        )
    body.append(
        f"Public intel gained: **+{gained * 100:.0f}pp**\n"
        f"Public intel on this mark: **+{total_bonus * 100:.0f}pp / "
        f"+{cap * 100:.0f}pp maximum**\n"
        "_Everybody now sees the improved odds. They do not know who paid._"
    )
    if odds_before and odds_after:
        moved = [
            f"{APPROACHES[k][2]}: {before_v * 100:.0f}% → "
            f"**{odds_after[k] * 100:.0f}%**"
            for k, before_v in odds_before.items()
        ]
        body.append("**The board now reads:**\n" + "\n".join(moved))

    body.append("")
    if claim == "fake":
        body.append(
            f"**Counter-scam takedown chance:** {takedown * 100:.0f}%\n"
            f"**Overall success chance:** {overall * 100:.0f}%"
            + ("" if report_class == "verified" else
               f"\n_{reliability * 100:.0f}% report reliability × "
               f"{takedown * 100:.0f}% takedown._")
            + f"\n**Operational stake:** {money(stake)}"
        )
    else:
        body.append("**Counter-scam:** unavailable")

    embed = discord.Embed(
        title=title, description="\n".join(body), colour=colour
    )
    nxt = charges["next_at"]
    embed.set_footer(
        text=(
            f"Intel cost {cost} Naira · charges {charges['charges']}/"
            f"{INTEL_MAX_CHARGES}"
            + ("" if charges["charges"] >= INTEL_MAX_CHARGES or not nxt
               else f" · next charge in {_hhmm(nxt - _now())}")
        )
    )
    return embed


class CounterScamButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"counterscam:(?P<report_id>[0-9]+)",
):
    """The private Counter-Scam offer attached to a FAKE report.

    Dynamic rather than plain, because the report id has to live *in* the
    custom_id.  A normal view is held in memory and dies with the process: the
    old one also expired after 30 minutes, while a report stays valid as long
    as its mark is on the board — which for a whale is hours.  Either way the
    button went quietly dead and the next click drew "Roger did not respond in
    time" instead of an explanation.

    Everything that decides whether the button may fire — who owns the report,
    whether it has been used, whether the mark is still there — is read from
    the database at click time, so a reconstructed button is exactly as
    trustworthy as the original.
    """

    def __init__(self, report_id: int, label: str = "🎭 Counter-Scam") -> None:
        self.report_id = report_id
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.danger,
                custom_id=f"counterscam:{report_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["report_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await _ack(interaction):
            return
        cog = interaction.client.get_cog("scam_targets")
        if cog is None:
            await _reply(
                interaction,
                content="❌ The game is not available right now.",
                ephemeral=True,
            )
            return
        await cog.run_counter_scam(interaction, self.report_id)


def counter_scam_view(report_id: int, overall: float, stake: int) -> discord.ui.View:
    """A one-button view holding the offer for ``report_id``."""
    view = discord.ui.View(timeout=None)
    view.add_item(CounterScamButton(
        report_id,
        label=f"🎭 Counter-Scam · {overall * 100:.0f}% · {stake} Naira",
    ))
    return view


# ── Board buttons ─────────────────────────────────────────────────────────────

_BUTTON_STYLES = {
    "careful": discord.ButtonStyle.success,
    "normal":  discord.ButtonStyle.primary,
    "greedy":  discord.ButtonStyle.danger,
    "intel":   discord.ButtonStyle.secondary,
}


class TargetBoardView(discord.ui.View):
    """One action row per mark, plus a global control row.

    Row layout, per the board spec::

        [ 🐢 Name ] [ 🛡️ 86% ·×.7 ] [ 🎯 76% ·×1 ] [ 🤑 35% ·×2 ] [ 🔎 Intel ]

    The first button is disabled and exists purely so it is obvious which row
    belongs to which card.  Three marks fill three rows; the fourth row holds
    the board-wide controls, which is exactly Discord's five-row budget.

    Persistent by ``custom_id`` (``scamtarget:<slot>:<action>``), so the
    buttons keep working across restarts.  The id references the *slot* rather
    than a target id because slots are stable while marks come and go — and
    because only one board message ever carries buttons (it is edited in
    place), a click can never land on a mark that has since been replaced.

    Constructed with no arguments it builds every id purely so
    ``bot.add_view`` can register them; the posted board only shows rows for
    slots that actually have a mark.
    """

    def __init__(self, targets: Optional[list[dict]] = None) -> None:
        super().__init__(timeout=None)
        if targets is None:
            for slot in range(BOARD_SLOTS):
                for approach in APPROACHES:
                    self.add_item(self._button(slot, approach, f"Slot {slot + 1}"))
                self.add_item(self._button(slot, "intel", f"Slot {slot + 1}"))
            self._add_global_row()
            return

        for row, t in enumerate(targets[:BOARD_SLOTS]):
            arch = archetype_for(t)
            self.add_item(discord.ui.Button(
                label=f"{t['emoji']} {(arch or {}).get('short_name') or t['name']}"[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"scamtargetlabel:{t['slot']}",
                disabled=True,
                row=row,
            ))
            for approach in APPROACHES:
                chance, mult, _emoji = approach_spec(t, approach)
                # Darkodor's payouts are ranges, not multipliers, so quoting a
                # multiplier on his buttons would be a lie.
                tail = "" if (arch and arch.get("approach_payouts")) else f" ·×{mult:g}"
                self.add_item(self._button(
                    t["slot"], approach,
                    f"{APPROACH_ICON[approach]} {_odds(chance)}{tail}",
                    row=row,
                ))
            immune = bool(arch and arch.get("intel_immune"))
            self.add_item(self._button(
                t["slot"], "intel",
                "🔒 No Intel" if immune else "🔎 Intel",
                row=row, disabled=immune,
            ))
        self._add_global_row()

    def _add_global_row(self) -> None:
        """Board-wide actions, always on the last row."""
        row = BOARD_SLOTS
        pose = discord.ui.Button(
            label="🎭 Pose as Target",
            style=discord.ButtonStyle.secondary,
            custom_id="scamtarget:global:pose",
            row=row,
        )
        pose.callback = self._on_click
        self.add_item(pose)
        helper = discord.ui.Button(
            label="ℹ️ Target Help",
            style=discord.ButtonStyle.secondary,
            custom_id="scamtarget:global:help",
            row=row,
        )
        helper.callback = self._on_click
        self.add_item(helper)

    def _button(
        self, slot: int, action: str, label: str, *,
        row: Optional[int] = None, disabled: bool = False,
    ) -> discord.ui.Button:
        button = discord.ui.Button(
            label=label[:80],
            style=_BUTTON_STYLES.get(action, discord.ButtonStyle.secondary),
            custom_id=f"scamtarget:{slot}:{action}",
            row=min(slot if row is None else row, 4),
            disabled=disabled,
        )
        button.callback = self._on_click
        return button

    async def _on_click(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id", "")
        try:
            _prefix, slot_s, action = custom_id.split(":")
        except (ValueError, AttributeError):
            return
        logger.info(
            "scam_targets: board button %s pressed by %s (%s)",
            custom_id, interaction.user, interaction.user.id,
        )
        # Before anything else — every branch below queues on the game lock.
        if not await _ack(interaction):
            return
        cog = interaction.client.get_cog("scam_targets")
        if cog is None:
            await _reply(
                interaction,
                content="❌ The game is not available right now.",
                ephemeral=True,
            )
            return
        if slot_s == "global":
            if action == "pose":
                await cog.pose_as_target(interaction)
            else:
                await _reply(
                    interaction, embed=target_help_embed(), ephemeral=True
                )
            return
        slot = int(slot_s)
        if action == "intel":
            await cog.investigate_slot(interaction, slot)
        else:
            await cog.attempt_slot(interaction, slot, action)


def target_help_embed() -> discord.Embed:
    """`/targethelp` and the board's ℹ️ button.

    The board itself stays clean; the rules live here so a veteran never has
    to scroll past them.
    """
    return discord.Embed(
        title="🎯 TARGET HELP",
        description=(
            "🛡️ **Careful** — safer approach, smaller payout.\n"
            "🎯 **Normal** — standard odds and payout.\n"
            "🤑 **Greedy** — riskier approach, larger payout.\n\n"
            "**Which is best depends entirely on the mark.** Every persona "
            "sets its own odds *and* its own multipliers. Greed is the best "
            "line against a man too proud for small deals; one bureaucrat has "
            "identical odds on all three. Read the card.\n\n"
            "🔎 **Intel** — spend an Intel Charge plus Naira to improve a "
            "mark's public odds *for everybody*, and receive a private report "
            "on whether the identity is genuine. You hold "
            f"**{INTEL_MAX_CHARGES} charges**, regaining one every "
            f"**{INTEL_RECHARGE_HOURS:g} hours** — roughly "
            f"**{INTEL_PER_DAY:g} a day**. Investigating costs no time, so you "
            "may scout a mark and work it immediately.\n\n"
            "💰 **Pot** — every failed attempt feeds the mark's pot. Whoever "
            "eventually succeeds collects it on top of their payout, and the "
            "pot is **never** multiplied by the approach.\n\n"
            "🐋 **Whales** — very valuable, and they leave the board one hour "
            "after appearing whatever happens.\n\n"
            "🦄 **Legendary** — may ignore the normal rules entirely. They "
            "are rare, but not as rare as they used to be.\n\n"
            "🔥 **Board Heat** — taking down the 🕶️ Undercover Cop leaves the "
            "police watching for **30 minutes**. The next player to fail "
            "against a real mark carries one **50%** arrest roll. It resolves "
            "after everything else, never fires on somebody already in a cell, "
            "and is spent on the first eligible failure either way.\n\n"
            "🎭 **Fake targets** — some marks are other players in disguise. "
            "Blindly attacking one gives you a chance to walk away, but "
            "failing can cost part of your cash *and* your fund position.\n\n"
            "**Tiers:** "
            + " · ".join(TIER_LABEL[t] for t, _w in TIER_WEIGHTS)
        ),
        colour=_EMBED_GOLD,
    )


# ── Cog ───────────────────────────────────────────────────────────────────────

class ScamTargetsCog(commands.Cog, name="scam_targets"):
    """The shared-targets game mode."""

    def __init__(self, bot: commands.Bot, conn: aiosqlite.Connection) -> None:
        self.bot = bot
        self.conn = conn
        self._lock = asyncio.Lock()
        # How many messages have landed in the game channel since the board was
        # posted — i.e. how far it has been pushed up the screen.
        self._messages_since_board = 0

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Count channel traffic, and enforce Babu's decree.

        The silence is enforced by deleting messages rather than by a Discord
        timeout: a timeout would mute the player across the entire server for
        losing an argument with a Belgian king, which is far more than the joke
        is worth.  Bot commands keep working — you may scam, you may not speak.
        """
        if message.channel.id != GAME_CHANNEL_ID:
            return
        if not message.author.bot:
            async with self.conn.execute(
                "SELECT silenced_until FROM scam_players WHERE discord_user_id = ?",
                (str(message.author.id),),
            ) as cur:
                row = await cur.fetchone()
            if row and row[0] and _parse(row[0]) > _now():
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                else:
                    left = _parse(row[0])
                    try:
                        await message.author.send(embed=discord.Embed(
                            title="🤐 THE MUTE KING'S DECREE",
                            description=(
                                "You may scam. You may invest. You may "
                                "scheme.\n\nYou may not speak in the game "
                                f"channel until <t:{int(left.timestamp())}:R>."
                                "\n\n> Babu remains unavailable for comment."
                            ),
                            colour=_EMBED_GREY,
                        ))
                    except Exception:
                        pass
                return
        _channel_id, board_message_id = await self._board_state()
        if board_message_id and str(message.id) == str(board_message_id):
            return   # the board itself doesn't bury the board
        self._messages_since_board += 1

    async def cog_load(self) -> None:
        await setup_schema(self.conn)
        self.board_tick.start()

    def cog_unload(self) -> None:
        self.board_tick.cancel()

    # ── board maintenance ─────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def board_tick(self) -> None:
        """Expire whales and fill slots whose respawn time has come."""
        try:
            async with self._lock:
                announcements = await self._maintain_board()
        except Exception:
            logger.exception("scam_targets: board tick failed")
            return
        if announcements:
            channel = self.bot.get_channel(GAME_CHANNEL_ID)
            if channel is not None:
                try:
                    async with self._lock:
                        await self.post_board(channel, news=announcements)
                except discord.HTTPException:
                    logger.warning("scam_targets: could not post the board")
            return

        # Heat that ran its course without catching anybody is worth saying
        # out loud: players changed their behaviour for it.
        try:
            cooled = await self._expire_heat()
            if cooled:
                channel = self.bot.get_channel(GAME_CHANNEL_ID)
                if channel is not None:
                    await channel.send(cooled)
        except Exception:
            logger.exception("scam_targets: heat expiry failed")

        # Nothing changed — but the board may have been buried by chatter.
        try:
            async with self._lock:
                await self._maybe_bump_board()
        except discord.HTTPException:
            logger.warning("scam_targets: could not bump the board")
        except Exception:
            logger.exception("scam_targets: bump check failed")

    @board_tick.before_loop
    async def _before_board(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_bump_board(self) -> bool:
        """Re-post the board if it has gone quiet *and* been buried.

        Both conditions matter: bumping a board that is still on screen is
        just spam, and bumping one people are actively clicking would move it
        out from under them.
        """
        channel_id, message_id = await self._board_state()
        if not (channel_id and message_id):
            return False
        if self._messages_since_board < BUMP_AFTER_MESSAGES:
            return False
        if not await active_targets(self.conn):
            return False   # nothing worth advertising

        async with self.conn.execute(
            "SELECT posted_at, last_attempt_at FROM scam_target_board WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        posted_at = row[0] if row else None
        last_attempt = row[1] if row else None
        # "Quiet" = no attempts since the board went up; fall back to when it
        # was posted if nobody has ever worked it.
        reference = last_attempt or posted_at
        if reference and (_now() - _parse(reference)) < timedelta(
            minutes=BUMP_IDLE_MINUTES
        ):
            return False

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return False
        logger.info(
            "scam_targets: bumping the board (%d messages since it was posted, "
            "quiet since %s)", self._messages_since_board, reference,
        )
        await self.post_board(
            channel, news=["📌 *Still on the board — scrolled past, not gone.*"]
        )
        return True

    async def _maintain_board(self) -> list[str]:
        """Expire whales and disguises, finish cancellations, refill slots."""
        out: list[str] = []
        now = _now()

        # 1. disguises whose five-minute shutdown has completed
        async with self.conn.execute(
            "SELECT owner_id, target_id FROM scam_fake_queue"
            " WHERE state = 'cancelling' AND cancel_at IS NOT NULL"
            "   AND cancel_at <= ?", (_iso(now),),
        ) as cur:
            done = [(str(r[0]), r[1]) async for r in cur]
        for owner_id, target_id in done:
            t = await get_target(self.conn, int(target_id)) if target_id else None
            if t and t["status"] == "active":
                await self._retire_target(t, "fled")
            await self._end_fake(owner_id)
            await self.conn.commit()
            # The deposit is forfeited, and the board says nothing about who
            # it was: voluntarily walking away must not out anybody.
            await self._tell(owner_id, discord.Embed(
                title="🎭 DISGUISE ABANDONED",
                description=(
                    "You successfully dismantled the fake identity.\n\n"
                    f"Cover deposit lost: **{money(FAKE_COVER_DEPOSIT)}**\n\n"
                    "Your six-hour cooldown continues."
                ),
                colour=_EMBED_GREY,
            ))

        # 2a. a last call before an advertised clock runs out
        for t in await active_targets(self.conn):
            if not t["expires_at"] or not shows_countdown(t):
                continue
            left = (_parse(t["expires_at"]) - now).total_seconds() / 60
            if left <= 0 or left > WHALE_URGENT_MINUTES:
                continue
            async with self.conn.execute(
                "SELECT warned FROM scam_targets WHERE id = ?", (t["id"],)
            ) as cur:
                row = await cur.fetchone()
            if row and int(row[0] or 0):
                continue
            await self.conn.execute(
                "UPDATE scam_targets SET warned = 1 WHERE id = ?", (t["id"],)
            )
            await self.conn.commit()
            arch = archetype_for(t)
            if arch and arch["shadow_network"]:
                out.append(
                    f"🪟 **THE FALLEN DICTATOR IS PACKING** — MVC has decided "
                    "public scrutiny has gone on long enough.\n"
                    f"⏳ **Less than {WHALE_URGENT_MINUTES} minutes remain.** "
                    + (f"Whatever is left of the **{money(t['pot'])}** shadow "
                       "treasury leaves with him." if t["pot"] else "")
                )
            else:
                out.append(
                    f"⏳ **{t['emoji']} {t['name']} is leaving** — under "
                    f"{WHALE_URGENT_MINUTES} minutes left"
                    + (f", and the {money(t['pot'])} pot goes too." if t["pot"]
                       else ".")
                )

        # 2b. marks that ran out of time
        for t in await active_targets(self.conn):
            if not t["expires_at"] or _parse(t["expires_at"]) > now:
                continue
            if t["is_fake"]:
                # Nobody bit — the impostor gets their deposit back and walks
                # away. Their identity is never revealed.
                owner_id = str(t["fake_owner_id"])
                if t["cover_deposit"]:
                    await adjust_balance(self.conn, owner_id, t["cover_deposit"], "fake_refund")
                await self._end_fake(owner_id)
                await self._retire_target(t, "fled")
                await self.conn.commit()
                await self._tell(owner_id, discord.Embed(
                    title="🎭 DISGUISE EXPIRED",
                    description=(
                        "Nobody took the bait.\n\n"
                        f"Your **{money(t['cover_deposit'])}** cover deposit "
                        "has been returned. Your six-hour cooldown continues."
                    ),
                    colour=_EMBED_GREY,
                ))
                out.append(
                    f"⌛ **{t['emoji']} {t['name']}** lost interest and stopped "
                    "replying."
                )
                continue
            arch = archetype_for(t)
            if arch and arch["shadow_network"] and t["pot"]:
                out.append(
                    f"🪟 **MVC HAS LEFT THE BUILDING** — the fallen dictator "
                    "decided that two hours of public scrutiny was enough.\n"
                    f"**{money(t['pot'])} disappears through the old network.** "
                    "A nearby window is found open."
                )
            await self._retire_target(t, "fled")
            if arch and arch["shadow_network"]:
                pass          # already announced above
            elif tier_of(t) in EXPIRING_TIERS:
                out.append(
                    f"🐋 **THE WHALE GOT AWAY** — {t['emoji']} **{t['name']}** "
                    "has left the board"
                    + (f", and the {money(t['pot'])} pot went with them."
                       if t["pot"] else ".")
                )
            else:
                out.append(
                    f"⌛ **{t['emoji']} {t['name']}** is gone"
                    + (f" — the {money(t['pot'])} pot went with them."
                       if t["pot"] else ".")
                )

        await sync_rivalries(self.conn)
        await self._lift_expired_silences()

        # 3. refill every empty slot whose cooldown has elapsed
        async with self.conn.execute(
            "SELECT slot, target_id, respawn_at FROM scam_target_slots ORDER BY slot"
        ) as cur:
            slots = [(int(r[0]), r[1], r[2]) async for r in cur]

        for slot, target_id, respawn_at in slots:
            live = None
            if target_id is not None:
                live = await get_target(self.conn, int(target_id))
                if live and live["status"] != "active":
                    live = None
            if live is not None:
                continue
            if respawn_at is None:
                await self._schedule_respawn(slot)
                continue
            if _parse(respawn_at) > now:
                continue
            before = {t["id"] for t in await active_targets(self.conn)}
            # Every vacancy goes through the one filler, so a queued player
            # waiting to go undercover is considered here too.
            await self.fill_target_slot()
            for t in await active_targets(self.conn):
                if t["id"] in before:
                    continue
                tier = tier_of(t)
                if tier == "whale":
                    lead = f"🐋 **A whale has surfaced:** **{t['name']}**"
                elif tier == "legendary":
                    lead = f"🦄 **{t['name'].upper()} IS ONLINE.** This does not happen."
                elif tier == "rare":
                    lead = f"🟣 **Rare mark:** {t['emoji']} **{t['name']}**"
                else:
                    lead = f"🎯 **New mark:** {t['emoji']} **{t['name']}**"
                out.append(
                    f"{lead} — {effective_chance(t, 'normal') * 100:.0f}% chance, "
                    f"{money(t['payout_min'])}–{money(t['payout_max'])}, "
                    + ("free to try." if not t["attempt_cost"]
                       else f"{money(t['attempt_cost'])} a go.")
                )
        return out

    async def _lift_expired_silences(self) -> None:
        """Tell people when Babu's decree has run out, and tidy the row.

        Enforcement itself never needed this — `on_message` compares the
        stored timestamp on every message, so a restart mid-decree keeps
        working.  What was missing was the *end*: a silenced player otherwise
        has to keep testing whether they can talk yet.
        """
        async with self.conn.execute(
            "SELECT discord_user_id FROM scam_players"
            " WHERE silenced_until IS NOT NULL AND silenced_until <= ?",
            (_iso(_now()),),
        ) as cur:
            done = [str(r[0]) async for r in cur]
        if not done:
            return
        await self.conn.execute(
            "UPDATE scam_players SET silenced_until = NULL"
            " WHERE silenced_until IS NOT NULL AND silenced_until <= ?",
            (_iso(_now()),),
        )
        await self.conn.commit()
        for uid in done:
            await self._tell(uid, discord.Embed(
                title="🗣️ THE DECREE HAS EXPIRED",
                description=(
                    "You may speak in the game channel again.\n\n"
                    "> Babu has no further comment."
                ),
                colour=_EMBED_GREEN,
            ))

    async def _schedule_respawn(self, slot: int) -> None:
        minutes = random.randint(RESPAWN_MIN_MINUTES, RESPAWN_MAX_MINUTES)
        await self.conn.execute(
            "UPDATE scam_target_slots SET target_id = NULL, respawn_at = ?"
            " WHERE slot = ?",
            (_iso(_now() + timedelta(minutes=minutes)), slot),
        )
        await self.conn.commit()

    async def _retire_target(self, t: dict, status: str) -> None:
        await self.conn.execute(
            "UPDATE scam_targets SET status = ? WHERE id = ?", (status, t["id"])
        )
        await self.conn.commit()
        await self._schedule_respawn(t["slot"])


    # ── live board message ────────────────────────────────────────────

    async def _board_state(self) -> tuple[Optional[str], Optional[str]]:
        async with self.conn.execute(
            "SELECT channel_id, message_id FROM scam_target_board WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    async def _remember_board(self, channel_id: str, message_id: str) -> None:
        await self.conn.execute(
            "UPDATE scam_target_board SET channel_id = ?, message_id = ?,"
            " posted_at = ? WHERE id = 1",
            (channel_id, message_id, _iso(_now())),
        )
        await self.conn.commit()
        self._messages_since_board = 0

    # ── target board heat ─────────────────────────────────────────────
    # Stored as an ordinary global timed effect, so it survives a restart on
    # its absolute expiry and needs no table of its own.  Heat is a *single*
    # pending arrest roll with a deadline — not a debuff that keeps firing.

    HEAT_ARREST_CHANCE = 0.50

    async def heat(self) -> Optional[dict]:
        return await fx.global_effect(self.conn, "board_heat")

    async def _raise_heat(self, source_id: str, minutes: int,
                          target_id: int) -> bool:
        """Start or refresh Heat.  Returns True if it was a refresh.

        Heat never stacks: a second takedown while it is already running
        pushes the deadline out rather than creating a second roll.
        """
        live = await self.heat()
        if live:
            await self.conn.execute(
                "UPDATE special_effects SET expires_at = ?, created_at = ?"
                " WHERE id = ?",
                (_iso(_now() + timedelta(minutes=minutes)), _iso(_now()),
                 live["id"]),
            )
            return True
        await fx.add_effect(
            self.conn, "board_heat", owner_id=str(source_id), minutes=minutes,
            source_target=target_id,
        )
        return False

    async def _resolve_heat(self, uid: str) -> Optional[str]:
        """The Heat roll for one failed attempt on a real mark.

        Called last of all the failure effects, and only after every other
        mechanic has had its say — a player already arrested by Utopia, the
        informant or the mark itself neither rolls nor consumes the Heat, so
        it stays up for whoever fails next.
        """
        live = await self.heat()
        if live is None:
            return None
        if await get_jail(self.conn, uid) is not None:
            return None
        await fx.consume_effect(self.conn, live["id"])
        if random.random() >= self.HEAT_ARREST_CHANCE:
            return (
                "🏃 **CLOSE CALL** — you failed while the police were "
                "watching.\n🎲 Detection roll: **FAILED**. Somehow you got "
                "away.\n🔥 The board's Heat has ended."
            )
        player = await get_player(self.conn, uid)
        jail = await arrest_player(
            self.conn, uid, EXTREME_FAILURE_MIN_BRIBE,
            player["balance"] + player["invested"],
            reason="Caught working a mark while the board was on Heat",
        )
        return (
            "🔥 **THE POLICE WERE WATCHING** — you failed while the board was "
            "on Heat.\n🎲 Detection roll: **SUCCESS**\n"
            + ("🎫 You were arrested and immediately released."
               if jail.get("released") else
               f"🚔 **You have been arrested** — bribe "
               f"{money(jail['bribe'])}, released "
               f"<t:{int(jail['until'].timestamp())}:R>")
            + "\n🔥 The board's Heat has ended."
        )

    async def _expire_heat(self) -> Optional[str]:
        """Announce Heat that ran its full 30 minutes without catching anybody."""
        async with self.conn.execute(
            "SELECT id FROM special_effects WHERE kind = 'board_heat'"
            " AND status = 'active' AND expires_at IS NOT NULL"
            " AND expires_at <= ?", (_iso(_now()),),
        ) as cur:
            rows = [int(r[0]) async for r in cur]
        if not rows:
            return None
        for effect_id in rows:
            await self.conn.execute(
                "UPDATE special_effects SET status = 'expired' WHERE id = ?",
                (effect_id,),
            )
        await self.conn.commit()
        return random.choice([
            "🧊 **THE HEAT HAS DIED DOWN** — police interest in the board has "
            "faded. Apparently they discovered other crimes.\n"
            "✅ Failed scams no longer carry the extra arrest risk.",
            "🌬️ **THE STREETS ARE QUIET AGAIN** — thirty minutes passed "
            "without another Prince being caught.\n"
            "The authorities have apparently misplaced the case file.",
            "📻 **POLICE RADIO SILENCE** — the manhunt has been called off.\n"
            "The Princes may return to their regularly scheduled fraud.",
        ])

    async def _mark_board_activity(self) -> None:
        await self.conn.execute(
            "UPDATE scam_target_board SET last_attempt_at = ? WHERE id = 1",
            (_iso(_now()),),
        )
        await self.conn.commit()

    async def _build_board(self) -> tuple[discord.Embed, TargetBoardView]:
        targets = await active_targets(self.conn)
        waiting: list[str] = []
        async with self.conn.execute(
            "SELECT slot, target_id, respawn_at FROM scam_target_slots ORDER BY slot"
        ) as cur:
            rows = [(int(r[0]), r[1], r[2]) async for r in cur]
        live_ids = {t["id"] for t in targets}
        for _slot, target_id, _respawn_at in rows:
            if target_id is not None and int(target_id) in live_ids:
                continue
            # Deliberately vague: publishing the respawn time would give away
            # fake targets, because /faketarget fills a free pitch immediately.
            # A mark appearing well before its announced time could only be a
            # player in disguise.
            waiting.append("A new mark should turn up before long.")
        fog = await fx.odds_hidden(self.conn)
        live_heat = await self.heat()
        heat_until = _parse(live_heat["expires_at"]) if live_heat else None
        return (
            board_embeds(targets, waiting, fog=fog, heat_until=heat_until),
            TargetBoardView(targets),
        )

    async def refresh_board(self) -> None:
        """Rewrite the live board message in place.

        Only one message ever carries the buttons, so a click can never land on
        a mark that has already been replaced — the stale-component problem
        that plagues buttons posted once and left behind.
        """
        channel_id, message_id = await self._board_state()
        if not channel_id or not message_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return
        embeds, view = await self._build_board()
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(embeds=embeds, view=view)
        except discord.NotFound:
            await self._remember_board(channel_id, "")
        except Exception:
            logger.exception("scam_targets: could not refresh board message")

    async def _retire_old_board(self) -> None:
        """Strip the buttons from the previous board so only one is ever live."""
        old_channel_id, old_message_id = await self._board_state()
        if not (old_channel_id and old_message_id):
            return
        old_channel = self.bot.get_channel(int(old_channel_id))
        if old_channel is None:
            return
        try:
            old = await old_channel.fetch_message(int(old_message_id))
            await old.edit(view=None)
        except Exception:
            pass

    async def post_board(
        self,
        channel: discord.abc.Messageable,
        news: Optional[list[str]] = None,
    ) -> discord.Message:
        """Post a fresh board, with any news as a short line above it.

        The news is one line per event rather than its own embed: the board
        underneath already lists every mark in full, so repeating the stat
        block just made the message twice as long.

        Announcements and the board are deliberately the *same* message: it
        keeps the channel tidy, and it means the only message carrying buttons
        is always the newest one.
        """
        await self._retire_old_board()
        embeds, view = await self._build_board()
        message = await channel.send(
            content="\n".join(news) if news else None, embeds=embeds, view=view
        )
        await self._remember_board(str(channel.id), str(message.id))
        return message



    async def _resolve_fake(
        self, interaction: discord.Interaction, t: dict, uid: str, player: dict
    ) -> None:
        """A blind attack on a disguised player. Called under the lock.

        A blind attack *always* ends the disguise — the only question is
        whether the attacker smelled a rat before handing over serious money.
        Public intel helps here too, at half strength, so buying intel is
        never wasted just because the mark turned out to be a person.
        """
        owner_id = str(t["fake_owner_id"])
        tier = tier_of(t)
        approach = getattr(self, "_pending_approach", "normal")
        escape = BLIND_ESCAPE.get(approach, 0.25)
        bonus = min(
            BLIND_ESCAPE_INTEL_CAP,
            float(t["investigation_bonus"] or 0.0) * BLIND_ESCAPE_INTEL_SHARE,
        )
        escape = min(0.95, escape + bonus)
        escaped = random.random() < escape

        # A Counterfeit Detector turns any fake encounter into a clean escape.
        # It is checked before the roll matters and only ever fires on a fake,
        # so a run of real marks never quietly burns it.
        detector = await fx.get_effect(self.conn, "counterfeit_detector",
                                       subject_id=uid)
        detected = False
        if detector:
            escaped = True
            detected = True
            await fx.consume_effect(self.conn, detector["id"])

        await self.conn.execute(
            "UPDATE scam_players SET last_target_at = ? WHERE discord_user_id = ?",
            (_iso(_now()), uid),
        )
        cost = int(t["attempt_cost"])
        deposit = int(t["cover_deposit"] or 0)
        stolen = 0
        doubled = False

        if escaped:
            # The attacker walks, out of pocket for the attempt; the faker
            # loses their cover deposit for being spotted.
            if cost:
                await adjust_balance(self.conn, uid, -cost, "target_cost", t["name"])
        else:
            if cost:
                await adjust_balance(self.conn, uid, -cost, "fake_loss", t["name"])
                await adjust_balance(self.conn, owner_id, cost, "fake_win", "attempt cost")
            if deposit:
                await adjust_balance(
                    self.conn, owner_id, deposit, "fake_refund")
            wealth = await exposed_wealth(self.conn, uid)
            want = _pct_of(wealth, FAKE_THEFT_PCT,
                           FAKE_THEFT_CAP.get(tier, 1_500))
            # The Crash Course doubles what this disguise takes, once.  The
            # faker receives the whole doubled amount; no money is created.
            course = await fx.get_effect(self.conn, "crash_course",
                                         subject_id=owner_id)
            if course:
                want *= 2
                await fx.consume_effect(self.conn, course["id"])
            stolen = await seize_wealth(
                self.conn, uid, want, to=owner_id, detail=t["name"],
            )
            doubled = bool(course)
        # An escape means the attacker smelled a rat and named it publicly —
        # the faker is exposed just as surely as by a counter-scam, so it
        # carries the same consequence.
        fake_jailed = ""
        if escaped:
            if await get_jail(self.conn, owner_id) is None:
                caught = await arrest_player(
                    self.conn, owner_id, FAKE_ARREST_BRIBE,
                    await exposed_wealth(self.conn, owner_id),
                    reason=f"Impersonating {t['name']} on the target board",
                )
                fake_jailed = (
                    f"\n🚔 **They have been arrested** — bribe "
                    f"{money(caught['bribe'])}, released "
                    f"<t:{int(caught['until'].timestamp())}:R>"
                    if not caught.get("released") else
                    "\n🎫 They were arrested and immediately released, on "
                    "presentation of a suspiciously official card."
                )
            else:
                fake_jailed = "\n🚔 They were already in a cell."

        await self._end_fake(owner_id)
        await self._retire_target(t, "taken")
        await self.conn.commit()
        await self.fill_target_slot()
        await self.refresh_board()

        owner = (
            interaction.guild.get_member(int(owner_id))
            if interaction.guild else None
        )
        owner_name = owner.mention if owner else f"<@{owner_id}>"
        icon = APPROACH_ICON.get(approach, "🎯")
        label = APPROACHES.get(approach, APPROACHES["normal"])[2]

        if escaped:
            how = (
                "🛡️ Their **Counterfeit Detector** caught the fraud before a "
                "single Naira changed hands. The detector has been consumed."
                if detected else
                "Something felt wrong and they aborted the operation before "
                "transferring serious money."
            )
            embed = discord.Embed(
                title="⚠️ FAKE TARGET FLUSHED OUT",
                description=(
                    f"{interaction.user.mention} approached **{t['name']}** "
                    f"using {icon} **{label}**.\n\n"
                    f"{how}\n\n"
                    + ("" if detected else
                       f"Escape chance: **{escape * 100:.0f}%**\n")
                    + f"Attempt cost lost: **{money(cost)}**\n\n"
                    f"The mark was actually {owner_name} in disguise, and they "
                    f"lost their **{money(deposit)}** cover deposit."
                    + fake_jailed
                ),
                colour=_EMBED_GOLD,
            )
        else:
            embed = discord.Embed(
                title="💸 FAKE TARGET SUCCESS",
                description=(
                    f"{interaction.user.mention} tried to scam "
                    f"**{t['name']}**.\n\n"
                    f"The mark was actually {owner_name} in disguise.\n\n"
                    f"Approach: {icon} **{label}**\n"
                    f"Escape chance: **{escape * 100:.0f}%** — they did not get "
                    "away in time.\n\n"
                    f"Operational funds lost: **{money(cost)}**\n"
                    f"Additional wealth stolen: **{money(stolen)}**\n"
                    + ("🎓 **Practical exam: passed** — the Crash Course "
                       "doubled that.\n" if doubled else "")
                    + f"\n{owner_name} escaped with the money."
                ),
                colour=_EMBED_RED,
            )
        if not escaped and stolen:
            extra = await fx.on_loss(self.conn, uid, stolen, detail=t["name"])
            if extra:
                embed.description += "\n\n" + "\n".join(extra)
                await self.conn.commit()
        await _reply(interaction, embed=embed)

    # ── /targets ──────────────────────────────────────────────────────

    @app_commands.command(
        name="targets", description="See the three marks currently available."
    )
    async def targets(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        async with self._lock:
            await self._retire_old_board()
            embeds, view = await self._build_board()
        await _reply(interaction, embeds=embeds, view=view)
        try:
            message = await interaction.original_response()
            await self._remember_board(str(interaction.channel_id), str(message.id))
        except Exception:
            logger.debug("scam_targets: could not record the board message id")

    # ── /targethelp ───────────────────────────────────────────────────

    @app_commands.command(
        name="targethelp",
        description="How the target board works: approaches, Intel, pots, tiers.",
    )
    async def targethelp(self, interaction: discord.Interaction) -> None:
        # Private on purpose: the board stays clean for people who already
        # know, and nobody has to scroll past a rules dump to reach the marks.
        await _reply(
            interaction,
            embed=target_help_embed(), ephemeral=True,
        )

    # ── Fake targets: queue, activation, cancellation ─────────────────

    async def _fake_row(self, owner_id: str) -> Optional[dict]:
        async with self.conn.execute(
            "SELECT owner_id, state, deposit, queued_at, queue_expires,"
            " target_id, cancel_at FROM scam_fake_queue WHERE owner_id = ?",
            (str(owner_id),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "owner_id": str(row[0]), "state": row[1], "deposit": int(row[2]),
            "queued_at": row[3], "queue_expires": row[4],
            "target_id": row[5], "cancel_at": row[6],
        }

    async def _active_fake_count(self) -> int:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM scam_targets WHERE is_fake = 1 AND status = 'active'"
        ) as cur:
            return int((await cur.fetchone())[0])

    async def _end_fake(self, owner_id: str) -> None:
        await self.conn.execute(
            "DELETE FROM scam_fake_queue WHERE owner_id = ?", (str(owner_id),)
        )

    async def fill_target_slot(self) -> list[str]:
        """The **only** path that fills an empty board slot.

        Every vacancy — a mark taken, burned, expired, or a fake resolving —
        comes through here, which is what stops "spawn a replacement" logic
        drifting apart in five different places.  A queued player waiting to
        go undercover gets first refusal, but only while fewer than two fakes
        are already live: the board must always hold at least one real mark.
        """
        news: list[str] = []
        # Expired queue entries first, so a timed-out request cannot be
        # activated a moment before it is cleaned up.
        async with self.conn.execute(
            "SELECT owner_id, deposit FROM scam_fake_queue"
            " WHERE state = 'queued' AND queue_expires IS NOT NULL"
            "   AND queue_expires <= ?", (_iso(_now()),),
        ) as cur:
            stale = [(str(r[0]), int(r[1])) async for r in cur]
        for owner_id, deposit in stale:
            await adjust_balance(self.conn, owner_id, deposit, "fake_refund")
            await self._end_fake(owner_id)
            await self._tell(owner_id, discord.Embed(
                title="🎭 NO DISGUISE OPPORTUNITY FOUND",
                description=(
                    "No suitable slot opened on the target board in time.\n\n"
                    f"Your **{money(deposit)}** cover deposit has been returned.\n"
                    "No cooldown was started."
                ),
                colour=_EMBED_GREY,
            ))
        await self.conn.commit()

        async with self.conn.execute(
            "SELECT slot, target_id FROM scam_target_slots ORDER BY slot"
        ) as cur:
            rows = [(int(r[0]), r[1]) async for r in cur]
        live = {t["id"] for t in await active_targets(self.conn)}

        for slot, target_id in rows:
            if target_id is not None and int(target_id) in live:
                continue
            filled = await self._activate_queued_fake(slot)
            if not filled:
                await spawn_target(self.conn, slot)
        return news

    async def _activate_queued_fake(self, slot: int) -> bool:
        """Put the oldest eligible queued player into *slot*, if allowed."""
        if await self._active_fake_count() >= MAX_ACTIVE_FAKES:
            return False
        async with self.conn.execute(
            "SELECT owner_id, deposit FROM scam_fake_queue"
            " WHERE state = 'queued' ORDER BY queued_at LIMIT 5"
        ) as cur:
            waiting = [(str(r[0]), int(r[1])) async for r in cur]
        for owner_id, deposit in waiting:
            if await get_jail(self.conn, owner_id):
                continue          # temporarily incompatible; stays in the queue
            t = await spawn_target(self.conn, slot, fake_only=True)
            arch = archetype_for(t)
            # A disguise lasts three hours, or the persona's own public
            # expiry if that is shorter — a fake whale must still leave when
            # a real whale would, or the timer itself gives it away.
            expires = _now() + timedelta(minutes=FAKE_TARGET_DURATION_MIN)
            if t["expires_at"]:
                expires = min(expires, _parse(t["expires_at"]))
            await self.conn.execute(
                "UPDATE scam_targets SET is_fake = 1, fake_owner_id = ?,"
                " cover_deposit = ?, expires_at = ? WHERE id = ?",
                (owner_id, deposit, _iso(expires), t["id"]),
            )
            await self.conn.execute(
                "UPDATE scam_fake_queue SET state = 'active', target_id = ?"
                " WHERE owner_id = ?", (t["id"], owner_id),
            )
            await self.conn.execute(
                "UPDATE scam_players SET fake_target_until = ?"
                " WHERE discord_user_id = ?",
                (_iso(_now() + timedelta(hours=FAKE_TARGET_COOLDOWN_HOURS)),
                 owner_id),
            )
            await self.conn.commit()
            await self._tell(owner_id, discord.Embed(
                title="🎭 DISGUISE ACTIVE",
                description=(
                    f"You are now posing as:\n\n**{t['emoji']} {t['name']}**  ·  "
                    f"{TIER_LABEL[tier_of(t)]}\n\n"
                    f"Cover deposit: **{money(deposit)}**\n"
                    f"Maximum time remaining: "
                    f"<t:{int(expires.timestamp())}:R>\n\n"
                    "**You only get one shot.** The first player who attacks "
                    "your identity ends the disguise — whether they walk away "
                    "or you take them for everything.\n\n"
                    "✅ Allowed: intel on *other* marks, quick scams, the fund, "
                    "`/intelstatus`, `/cancelfake`\n"
                    "❌ Blocked: `/scam`, working marks, counter-scamming, "
                    "starting a quick scam"
                ),
                colour=_EMBED_GOLD,
            ))
            return True
        return False

    async def _tell(self, user_id: str, embed: discord.Embed) -> None:
        """Send a private message; a closed DM is not an error worth raising."""
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(
                int(user_id)
            )
            await user.send(embed=embed)
        except Exception:
            logger.info("scam_targets: could not DM %s", user_id)

    async def pose_as_target(self, interaction: discord.Interaction) -> None:
        """The board's 🎭 button. Same flow as /faketarget."""
        await self._queue_fake(interaction)

    @app_commands.command(
        name="faketarget",
        description="Pose as a mark and wait for somebody to try it on.",
    )
    async def faketarget(self, interaction: discord.Interaction) -> None:
        await self._queue_fake(interaction)

    async def _queue_fake(self, interaction: discord.Interaction) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        if not await require_free(interaction, self.conn, "pose as a mark"):
            return
        uid = str(interaction.user.id)

        async with self._lock:
            existing = await self._fake_row(uid)
            if existing:
                await _reply(
                    interaction,
                    embed=_plain("🎭 DISGUISE ALREADY IN PROGRESS",
                                 "You already have a disguise queued or "
                                 "active.\n\nNo additional deposit was taken."),
                    ephemeral=True,
                )
                return

            player = await get_player(self.conn, uid)
            last = player.get("fake_target_until")
            if last and _parse(last) > _now():
                await _reply(
                    interaction,
                    embed=_plain("🎭 DISGUISE NETWORK ON COOLDOWN",
                                 "You cannot pose as another mark yet.\n\n"
                                 f"**Ready** <t:{int(_parse(last).timestamp())}:R>\n\n"
                                 "No deposit taken."),
                    ephemeral=True,
                )
                return
            if player["balance"] < FAKE_COVER_DEPOSIT:
                await _reply(
                    interaction,
                    embed=_plain("💸 NOT ENOUGH CASH FOR A DISGUISE",
                                 f"Cover deposit required: "
                                 f"**{money(FAKE_COVER_DEPOSIT)}**\n"
                                 f"Your cash: **{money(player['balance'])}**\n\n"
                                 "No request was created."),
                    ephemeral=True,
                )
                return

            await adjust_balance(self.conn, uid, -FAKE_COVER_DEPOSIT, "fake_deposit")
            await self.conn.execute(
                "INSERT INTO scam_fake_queue"
                " (owner_id, state, deposit, queued_at, queue_expires)"
                " VALUES (?, 'queued', ?, ?, ?)",
                (uid, FAKE_COVER_DEPOSIT, _iso(_now()),
                 _iso(_now() + timedelta(minutes=FAKE_QUEUE_TIMEOUT_MIN))),
            )
            await self.conn.commit()

        # Deliberately never says whether a slot is free or how many fakes are
        # live — the response itself would leak the board's composition.
        await _reply(
            interaction,
            embed=discord.Embed(
                title="🎭 DISGUISE REQUEST ACCEPTED",
                description=(
                    f"Your **{money(FAKE_COVER_DEPOSIT)}** cover deposit has "
                    "been secured.\n\n"
                    "You are waiting for a suitable opening on the target "
                    f"board.\n\n**Maximum wait:** {FAKE_QUEUE_TIMEOUT_MIN} minutes\n\n"
                    "Your disguise identity will be random. You will be "
                    "messaged privately when you go live."
                ),
                colour=_EMBED_GOLD,
            ),
            ephemeral=True,
        )
        await self.fill_target_slot()
        await self.refresh_board()

    @app_commands.command(
        name="cancelfake",
        description="Abandon your disguise (five-minute wind-up while active).",
    )
    async def cancelfake(self, interaction: discord.Interaction) -> None:
        uid = str(interaction.user.id)
        async with self._lock:
            row = await self._fake_row(uid)
            if not row:
                await _reply(
                    interaction,
                    embed=_plain("🎭 NO DISGUISE",
                                 "You have no disguise queued or active."),
                    ephemeral=True,
                )
                return

            if row["state"] == "queued":
                # Nothing has gone live, so nothing is forfeited.
                await adjust_balance(self.conn, uid, row["deposit"])
                await self._end_fake(uid)
                await self.conn.commit()
                await _reply(
                    interaction,
                    embed=_plain("🎭 REQUEST WITHDRAWN",
                                 f"Your **{money(row['deposit'])}** deposit has "
                                 "been returned.\n\nNo cooldown was started."),
                    ephemeral=True,
                )
                return

            if row["state"] == "cancelling":
                left = _parse(row["cancel_at"]) - _now()
                await _reply(
                    interaction,
                    embed=_plain("🎭 SHUTDOWN ALREADY IN PROGRESS",
                                 f"**Time remaining:** {_mmss(left)}"),
                    ephemeral=True,
                )
                return

            done_at = _now() + timedelta(minutes=FAKE_CANCEL_WINDUP_MIN)
            await self.conn.execute(
                "UPDATE scam_fake_queue SET state = 'cancelling', cancel_at = ?"
                " WHERE owner_id = ?", (_iso(done_at), uid),
            )
            await self.conn.commit()

        await _reply(
            interaction,
            embed=discord.Embed(
                title="🎭 DISGUISE SHUTDOWN STARTED",
                description=(
                    "You have begun dismantling your fake identity.\n\n"
                    f"The mark disappears <t:{int(done_at.timestamp())}:R>.\n\n"
                    "**Until then the disguise stays fully active and "
                    "vulnerable.** If somebody attacks you first, that "
                    "resolves normally and the shutdown is cancelled.\n\n"
                    f"If the shutdown completes, your "
                    f"**{money(FAKE_COVER_DEPOSIT)}** cover deposit is "
                    "forfeited."
                ),
                colour=_EMBED_RED,
            ),
            ephemeral=True,
        )

    # ── Intel ─────────────────────────────────────────────────────────

    async def investigate_slot(
        self, interaction: discord.Interaction, slot: int
    ) -> None:
        async with self.conn.execute(
            "SELECT target_id FROM scam_target_slots WHERE slot = ?", (slot,)
        ) as cur:
            row = await cur.fetchone()
        target_id = int(row[0]) if row and row[0] is not None else None
        if target_id is None:
            await _reply(
                interaction,
                content="❌ That pitch is empty right now — check `/targets`.",
                ephemeral=True,
            )
            return
        await self.run_intel(interaction, target_id)

    async def run_intel(
        self, interaction: discord.Interaction, target_id: int
    ) -> None:
        """One Intel mission, validated in the order the design specifies.

        Every rejection happens *before* anything is spent: no charge, no
        Naira, no lock.  That matters because several rejections are things a
        player cannot see coming (somebody else took the second Intel slot a
        second earlier).
        """
        uid = str(interaction.user.id)
        if not await require_free(interaction, self.conn, "gather intel"):
            return

        async with self._lock:
            t = await get_target(self.conn, target_id)
            if not t or t["status"] != "active":
                await _reply(
                    interaction,
                    embed=_plain("🎯 TARGET NO LONGER AVAILABLE",
                                 "This mark has already left the board.\n\n"
                                 "No charge spent. No Naira spent."),
                    ephemeral=True,
                )
                return

            arch = archetype_for(t)
            tier = tier_of(t)
            if arch and arch["intel_immune"]:
                await _reply(
                    interaction,
                    embed=_plain("🦄 THE UNICORN CANNOT BE INVESTIGATED",
                                 "Your intelligence network has found no "
                                 "reliable evidence that Darkodor currently "
                                 "exists.\n\n🔎 Charge spent: 0\n💸 Naira spent: 0"),
                    ephemeral=True,
                )
                return

            state = await intel_state(self.conn, uid)

            if str(t["fake_owner_id"] or "") == uid:
                await _reply(
                    interaction,
                    embed=_plain("🎭 YOU CANNOT INVESTIGATE YOUR OWN DISGUISE",
                                 "That would be impressively inefficient.\n\n"
                                 "No charge spent. No Naira spent."),
                    ephemeral=True,
                )
                return

            async with self.conn.execute(
                "SELECT 1 FROM scam_investigations"
                " WHERE target_id = ? AND investigator_id = ?", (target_id, uid),
            ) as cur:
                already = await cur.fetchone()
            if already:
                await _reply(
                    interaction,
                    embed=_plain("🔎 YOU ALREADY INVESTIGATED THIS TARGET",
                                 "Your network has already worked this case.\n\n"
                                 "You cannot spend another Intel Charge on the "
                                 "same mark."),
                    ephemeral=True,
                )
                return

            if int(t.get("intel_missions") or 0) >= INTEL_MISSIONS_PER_TARGET:
                await _reply(
                    interaction,
                    embed=_plain("🔒 NO MORE INTEL AVAILABLE",
                                 "Two intelligence missions have already been "
                                 "completed against this mark. Further digging "
                                 "would attract too much attention.\n\n"
                                 "No charge spent. No Naira spent."),
                    ephemeral=True,
                )
                return

            if state["charges"] <= 0:
                nxt = state["next_at"]
                await _reply(
                    interaction,
                    embed=_plain("🔎 NO INTEL AVAILABLE",
                                 "Your investigators are all out in the field, "
                                 "and there is nobody left to send.\n\n"
                                 f"**Charges:** 0/{INTEL_MAX_CHARGES}\n"
                                 + (f"**Next charge:** <t:{int(nxt.timestamp())}:R>\n"
                                    f"**Back to {INTEL_MAX_CHARGES}/"
                                    f"{INTEL_MAX_CHARGES}:** "
                                    f"<t:{int(_full_charges_at(state).timestamp())}:R>\n"
                                    if nxt else "")
                                 + f"\n_One charge every "
                                 f"{INTEL_RECHARGE_HOURS:g} hours. Nothing else "
                                 "is blocked — you can still work the board._\n\n"
                                 "No Naira was spent."),
                    ephemeral=True,
                )
                return

            cost = intel_cost(tier)
            player = await get_player(self.conn, uid)
            if player["balance"] < cost:
                await _reply(
                    interaction,
                    embed=_plain("💸 NOT ENOUGH CASH",
                                 f"Intel on this mark costs **{money(cost)}**.\n"
                                 f"Your cash: **{money(player['balance'])}**\n\n"
                                 "No Intel Charge was spent."),
                    ephemeral=True,
                )
                return

            if t["expires_at"]:
                left = (_parse(t["expires_at"]) - _now()).total_seconds()
                if left < INTEL_MIN_REMAINING_SECONDS:
                    await _reply(
                        interaction,
                        embed=_plain("⏳ TOO LATE FOR INTEL",
                                     "This mark will leave before your "
                                     "investigators could return.\n\n"
                                     f"**Time remaining:** {int(max(0, left))}s\n\n"
                                     "No charge spent. No Naira spent."),
                        ephemeral=True,
                    )
                    return

            # ── everything validated: spend, then roll ──
            await adjust_balance(self.conn, uid, -cost, "intel", t["name"])
            await spend_intel_charge(self.conn, uid)
            await self.conn.execute(
                "UPDATE scam_targets SET intel_missions = intel_missions + 1"
                " WHERE id = ?", (target_id,),
            )

            base, broke, extra = roll_intel_gain(tier)
            cap = INTEL_CAP.get(tier, 0.0)
            before = float(t["investigation_bonus"] or 0.0)
            after = min(cap, before + base + extra)
            gained = max(0.0, after - before)
            # Snapshot the three approach chances either side of the mission,
            # so the report can show the actual movement rather than a
            # percentage-point figure the player has to apply themselves.
            odds_before = {k: approach_spec(t, k)[0]
                           for k in ("careful", "normal", "greedy")}
            t_after = dict(t, investigation_bonus=after)
            odds_after = {k: approach_spec(t_after, k)[0]
                          for k in ("careful", "normal", "greedy")}
            await self.conn.execute(
                "UPDATE scam_targets SET investigation_bonus = ? WHERE id = ?",
                (after, target_id),
            )

            report_class, reliability = roll_report_class()
            truth = "fake" if t["is_fake"] else "real"
            claim = "unknown"
            if report_class == "verified":
                claim = truth
            elif report_class == "strong":
                claim = truth if random.random() < reliability else (
                    "real" if truth == "fake" else "fake"
                )

            takedown = overall = 0.0
            stake = counter_stake(t)
            if claim == "fake":
                if report_class == "verified":
                    takedown, overall = COUNTER_TAKEDOWN_VERIFIED, COUNTER_TAKEDOWN_VERIFIED
                else:
                    takedown = COUNTER_TAKEDOWN_STRONG
                    overall = COUNTER_TAKEDOWN_STRONG * (reliability or 1.0)

            cur = await self.conn.execute(
                "INSERT INTO scam_intel_reports (target_id, user_id, report_class,"
                " claim, reliability, takedown, overall, stake, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (target_id, uid, report_class, claim, reliability, takedown,
                 overall, stake, _iso(_now())),
            )
            report_id = int(cur.lastrowid)
            await record_investigation(
                self.conn, target_id, uid, claim, report_class == "verified"
            )
            await _touch(self.conn, uid)
            await self.conn.commit()
            fresh = await intel_state(self.conn, uid)

        await self.refresh_board()

        embed = _intel_report_embed(
            t, tier, cost, report_class, reliability, claim,
            base, broke, extra, gained, after, cap,
            takedown, overall, stake, fresh,
            odds_before, odds_after,
        )
        view = None
        if claim == "fake":
            posing = await impersonating(self.conn, uid)
            if posing:
                embed.add_field(
                    name="🎭 COUNTER-SCAM UNAVAILABLE WHILE DISGUISED",
                    value=(
                        "You may gather Intel while posing as a mark, but you "
                        "cannot take one down until your own disguise ends."
                    ),
                    inline=False,
                )
            else:
                view = counter_scam_view(report_id, overall, stake)
        await _reply(
            interaction,
            embed=embed, view=view, ephemeral=True,
        )

    # ── /intelstatus ──────────────────────────────────────────────────

    @app_commands.command(
        name="intelstatus",
        description="Your intelligence network: charges and recharge timer.",
    )
    async def intelstatus(self, interaction: discord.Interaction) -> None:
        state = await intel_state(self.conn, str(interaction.user.id))
        full = state["charges"] >= INTEL_MAX_CHARGES
        embed = discord.Embed(
            title="🔎 INTELLIGENCE NETWORK",
            description=(
                f"**Charges:** {state['charges']}/{INTEL_MAX_CHARGES}\n"
                + (
                    "**Next charge:** FULL\n" if full
                    else f"**Next charge:** <t:{int(state['next_at'].timestamp())}:R>\n"
                    if state["next_at"] else ""
                )
                + (
                    "" if full or not state["next_at"] else
                    f"**Back to {INTEL_MAX_CHARGES}/{INTEL_MAX_CHARGES}:** "
                    + f"<t:{int(_full_charges_at(state).timestamp())}:R>\n"
                )
                + f"\n_One charge every {INTEL_RECHARGE_HOURS:g} hours, up to "
                f"{INTEL_MAX_CHARGES} — about **{INTEL_PER_DAY:g} missions a "
                "day**. Intel costs "
                + " · ".join(
                    f"{TIER_EMOJI[k]} {money(v)}"
                    for k, v in INTEL_COST.items() if v
                )
                + "._"
            ),
            colour=_EMBED_GOLD,
        )
        await _reply(interaction, embed=embed, ephemeral=True)

    # ── Counter-scam ──────────────────────────────────────────────────

    async def run_counter_scam(
        self, interaction: discord.Interaction, report_id: int
    ) -> None:
        """Act on a FAKE report.  Resolves the mark either way."""
        uid = str(interaction.user.id)
        async with self._lock:
            async with self.conn.execute(
                "SELECT target_id, report_class, claim, takedown, overall, stake,"
                " consumed, user_id FROM scam_intel_reports WHERE id = ?",
                (report_id,),
            ) as cur:
                rep = await cur.fetchone()
            # Each of these used to read "REPORT NO LONGER VALID"; a button that
            # refuses should say which of the three reasons it is refusing for.
            if not rep:
                await _reply(
                    interaction,
                    embed=_plain("🎯 REPORT GONE",
                                 "That intel report no longer exists.\n\n"
                                 "No operational funds were spent."),
                    ephemeral=True,
                )
                return
            if str(rep[7]) != uid:
                await _reply(
                    interaction,
                    embed=_plain("🎯 NOT YOUR REPORT",
                                 "That intel belongs to another investigator.\n\n"
                                 "No operational funds were spent."),
                    ephemeral=True,
                )
                return
            if int(rep[6]):
                await _reply(
                    interaction,
                    embed=_plain("🎯 ALREADY ACTED ON",
                                 "You have already used this report — a "
                                 "counter-scam fires once.\n\n"
                                 "No operational funds were spent.\n\n"
                                 "_Old buttons stay on screen; the report "
                                 "behind them does not._"),
                    ephemeral=True,
                )
                return
            target_id, report_class, _claim, takedown, overall, stake = (
                int(rep[0]), rep[1], rep[2], float(rep[3]), float(rep[4]), int(rep[5])
            )

            t = await get_target(self.conn, target_id)
            if not t or t["status"] != "active":
                await _reply(
                    interaction,
                    embed=_plain("🎯 TARGET NO LONGER AVAILABLE",
                                 "This mark has already disappeared.\n\n"
                                 "No operational funds were spent."),
                    ephemeral=True,
                )
                return

            player = await get_player(self.conn, uid)
            if player["balance"] < stake:
                await _reply(
                    interaction,
                    embed=_plain("💸 NOT ENOUGH CASH",
                                 f"The operational stake is **{money(stake)}** "
                                 f"and you have {money(player['balance'])}."),
                    ephemeral=True,
                )
                return

            await self.conn.execute(
                "UPDATE scam_intel_reports SET consumed = 1 WHERE id = ?",
                (report_id,),
            )
            await adjust_balance(self.conn, uid, -stake, "counter_stake", t["name"])
            await self.conn.execute(
                "UPDATE scam_players SET last_target_at = ? WHERE discord_user_id = ?",
                (_iso(_now()), uid),
            )

            tier = tier_of(t)
            owner_id = str(t["fake_owner_id"] or "")
            really_fake = bool(t["is_fake"])
            won = really_fake and random.random() < takedown

            outcome, moved, deposit = "false", 0, int(t["cover_deposit"] or 0)
            if really_fake and won:
                outcome = "win"
                await adjust_balance(self.conn, uid, stake, "counter_win", "stake refunded")
                if deposit:
                    await adjust_balance(self.conn, uid, deposit, "counter_win", "cover deposit seized")
                wealth = await exposed_wealth(self.conn, owner_id)
                moved = await seize_wealth(
                    self.conn, owner_id,
                    _pct_of(wealth, COUNTER_WIN_THEFT, COUNTER_WIN_CAP),
                    to=uid, reason="counter_loss", gain_reason="counter_win",
                    detail=t["name"],
                )
            elif really_fake:
                outcome = "lost"
                await adjust_balance(self.conn, owner_id, stake, "fake_win", "counter-scam stake")
                if deposit:
                    await adjust_balance(self.conn, owner_id, deposit, "fake_refund")
                wealth = await exposed_wealth(self.conn, uid)
                moved = await seize_wealth(
                    self.conn, uid,
                    _pct_of(wealth, COUNTER_LOSS_THEFT, COUNTER_LOSS_CAP),
                    to=owner_id, reason="counter_loss", gain_reason="counter_win",
                    detail=t["name"],
                )
            # a false accusation simply destroys the stake

            # Being taken down *is* being exposed: the disguise is destroyed
            # and the room is told exactly who was behind it.  Losing the
            # deposit was the only consequence; the cell is the rest of it.
            fake_jailed = ""
            if outcome == "win" and owner_id:
                if await get_jail(self.conn, owner_id) is None:
                    caught = await arrest_player(
                        self.conn, owner_id, FAKE_ARREST_BRIBE,
                        await exposed_wealth(self.conn, owner_id),
                        reason=f"Impersonating {t['name']} on the target board",
                    )
                    fake_jailed = (
                        f"\n🚔 **<@{owner_id}> has been arrested** — bribe "
                        f"{money(caught['bribe'])}, released "
                        f"<t:{int(caught['until'].timestamp())}:R>"
                        if not caught.get("released") else
                        f"\n🎫 <@{owner_id}> was arrested and immediately "
                        "released, on presentation of a suspiciously official "
                        "card."
                    )
                else:
                    fake_jailed = (f"\n🚔 <@{owner_id}> was already in a cell "
                                   "when the raid arrived.")

            if really_fake:
                await self._end_fake(owner_id)
                await self._retire_target(t, "taken")
            await self.conn.commit()

        # Tell the clicker first.  Refilling the slot and rebuilding the board
        # are several Discord round-trips, and the player who pressed the
        # button should not be the one waiting on them.
        await _reply(
            interaction,
            embed=_plain(
                "🎭 COUNTER-SCAM RESOLVED",
                "The result has been posted publicly.",
            ),
            ephemeral=True,
        )
        await self.fill_target_slot()
        await self.refresh_board()
        await self._announce_counter(
            interaction, t, tier, outcome, report_class,
            overall, stake, deposit, moved, owner_id, fake_jailed,
        )

    async def _announce_counter(
        self, interaction, t, tier, outcome, report_class,
        overall, stake, deposit, moved, owner_id, fake_jailed: str = "",
    ) -> None:
        who = interaction.user.mention
        faker = f"<@{owner_id}>" if owner_id else "somebody"
        if outcome == "win":
            embed = discord.Embed(
                title="🎭 COUNTER-SCAM SUCCESSFUL",
                description=(
                    f"{who} acted on intelligence against **{t['name']}**.\n\n"
                    f"The mark was actually {faker} in disguise.\n\n"
                    f"Intel: {'🌟 Verified fake' if report_class == 'verified' else '🟡 Strong lead'}\n"
                    f"Counter-scam odds: **{overall * 100:.0f}%**\n\n"
                    f"Operational stake: {money(stake)} — refunded\n"
                    f"Cover deposit seized: **{money(deposit)}**\n"
                    f"Additional wealth seized: **{money(moved)}**\n\n"
                    f"💰 **Total reward: {money(deposit + moved)}**\n\n"
                    "The disguise has been destroyed." + fake_jailed
                ),
                colour=_EMBED_GOLD,
            )
        elif outcome == "lost":
            embed = discord.Embed(
                title="💸 COUNTER-SCAM FAILED",
                description=(
                    f"{who} correctly identified **{t['name']}** as a fake run "
                    f"by {faker}.\n\n"
                    f"Counter-scam odds: **{overall * 100:.0f}%**\n\n"
                    "Unfortunately, the takedown failed.\n\n"
                    f"Operational funds lost: **{money(stake)}**\n"
                    f"Additional wealth stolen: **{money(moved)}**\n\n"
                    f"{faker} escaped with the money."
                ),
                colour=_EMBED_RED,
            )
        else:
            embed = discord.Embed(
                title="🚨 FALSE COUNTER-SCAM",
                description=(
                    f"{who} acted on an uncertain intel report and tried to "
                    f"expose **{t['name']}** as another scammer.\n\n"
                    f"Unfortunately, {t['name']} was completely real.\n\n"
                    "Report reliability: **80%**\n"
                    f"Operational funds lost: **{money(stake)}**\n\n"
                    f"The operation has been exposed. {t['name']} remains on "
                    "the board."
                ),
                colour=_EMBED_RED,
            )
        channel = self.bot.get_channel(GAME_CHANNEL_ID)
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logger.warning("scam_targets: could not post counter-scam result")

    async def attempt_slot(
        self, interaction: discord.Interaction, slot: int, approach: str
    ) -> None:
        """Board-button entry point: work whichever mark holds *slot*."""
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        async with self.conn.execute(
            "SELECT target_id FROM scam_target_slots WHERE slot = ?", (slot,)
        ) as cur:
            row = await cur.fetchone()
        target_id = int(row[0]) if row and row[0] is not None else None
        if target_id is None:
            await _reply(
                interaction,
                content="❌ That pitch is empty right now — check `/targets`.",
                ephemeral=True,
            )
            return
        await self._attempt(interaction, target_id, approach)

    async def _shadow_cut(self, t: dict, gross: int) -> tuple[int, int]:
        """Divert MVC's percentage off another mark's payout.

        A diversion, not new money: the winner simply receives less and the
        difference sits in MVC's pot until somebody takes him down — or until
        he leaves with it.  Returns ``(cut taken, treasury after)``.
        """
        if gross <= 0:
            return 0, 0
        for other in await active_targets(self.conn):
            if other["id"] == t["id"] or other["is_fake"]:
                continue
            arch = archetype_for(other)
            if not (arch and arch["shadow_network"]):
                continue
            pct, cap = arch["shadow_network"]
            cut = min(int(gross * pct), cap)
            if cut <= 0:
                return 0, 0
            treasury = other["pot"] + cut
            await self.conn.execute(
                "UPDATE scam_targets SET pot = ? WHERE id = ?",
                (treasury, other["id"]),
            )
            return cut, treasury
        return 0, 0

    async def _cheapest_affordable(self, balance: int) -> Optional[dict]:
        """Cheapest mark on the board this balance can still pay for."""
        affordable = [
            t for t in await active_targets(self.conn)
            if t["attempt_cost"] <= balance
        ]
        return min(affordable, key=lambda t: t["attempt_cost"]) if affordable else None

    async def _attempt(
        self, interaction: discord.Interaction, target_id: int, approach: str
    ) -> None:
        if not await require_free(interaction, self.conn, "work a mark"):
            return
        uid = str(interaction.user.id)
        approach_label = APPROACHES.get(approach, APPROACHES["normal"])[2]

        async with self._lock:
            t = await get_target(self.conn, target_id)
            if not t or t["status"] != "active":
                await _reply(
                    interaction,
                    content="❌ That mark is no longer available. Check `/targets`.",
                    ephemeral=True,
                )
                return

            if await impersonating(self.conn, uid):
                await _reply(
                    interaction,
                    content="❌ You are currently posing as a mark. Working the board "
                    "while impersonating one of its targets is a step too far, "
                    "even here.",
                    ephemeral=True,
                )
                return

            player = await get_player(self.conn, uid)

            # per-player pause
            async with self.conn.execute(
                "SELECT last_target_at FROM scam_players WHERE discord_user_id = ?",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
            last = row[0] if row else None
            # Roas supersedes the ordinary pause entirely — hers is longer and
            # applies to the whole board.
            async with self.conn.execute(
                "SELECT target_lock_until FROM scam_players WHERE discord_user_id = ?",
                (uid,),
            ) as cur:
                lock_row = await cur.fetchone()
            if lock_row and lock_row[0] and _parse(lock_row[0]) > _now():
                until = _parse(lock_row[0])
                await _reply(
                    interaction,
                    embed=discord.Embed(
                        title="🚫 THE ROAD IS BLOCKED",
                        description=(
                            "Roas is standing in the road and will not move.\n\n"
                            f"**Every target** is closed to you until "
                            f"<t:{int(until.timestamp())}:R>.\n\n"
                            "`/scam`, intel, quick scams and the fund all still "
                            "work — she is only blocking the board."
                        ),
                        colour=_EMBED_RED,
                    ),
                    ephemeral=True,
                )
                return
            if last:
                ready_at = _parse(last) + timedelta(minutes=ATTEMPT_COOLDOWN_MINUTES)
                if ready_at > _now():
                    embed = discord.Embed(
                        title="⏳ You are still lying low",
                        description=(
                            f"There is a **{ATTEMPT_COOLDOWN_MINUTES} minute** pause "
                            "between your attempts on the board, so no single "
                            "player can work every mark.\n\n"
                            f"**Your next attempt:** "
                            f"<t:{int(ready_at.timestamp())}:R>\n\n"
                            "In the meantime you can still use `/scam`, start a "
                            "`/quickscam`, or put money in the fund."
                        ),
                        colour=_EMBED_GREY,
                    )
                    await _reply(
                        interaction,
                        embed=embed, ephemeral=True,
                    )
                    return

            if player["balance"] < t["attempt_cost"]:
                short = t["attempt_cost"] - player["balance"]
                cheapest = await self._cheapest_affordable(player["balance"])
                embed = discord.Embed(
                    title="❌ You cannot afford this attempt",
                    description=(
                        f"Working **{t['emoji']} {t['name']}** costs "
                        f"**{money(t['attempt_cost'])}** per attempt — you pay it "
                        "whether or not the scam lands.\n\n"
                        f"**Your balance:** {money(player['balance'])}\n"
                        f"**You are short:** {money(short)}\n\n"
                        + (
                            f"You *can* afford **{cheapest['emoji']} "
                            f"{cheapest['name']}** at "
                            f"{money(cheapest['attempt_cost'])} a go.\n"
                            if cheapest else
                            "Every mark on the board is out of your price range "
                            "right now.\n"
                        )
                        + "Earn more with `/scam`, or take your money back out "
                        "of the fund with `/invest withdraw`."
                    ),
                    colour=_EMBED_RED,
                )
                await _reply(
                    interaction,
                    embed=embed, ephemeral=True,
                )
                return

            if t["is_fake"]:
                self._pending_approach = approach
                await self._resolve_fake(interaction, t, uid, player)
                return

            special_lines: list[str] = []
            arch = archetype_for(t)
            carrot = False
            collateral_gain = 0
            chance, pay_mult, _emoji = approach_spec(t, approach)
            # Worth taking? Success pays the mark plus the whole pot; failure
            # costs the attempt. Worked out before the roll so the stat cannot
            # be coloured by how it turned out.
            if arch and arch.get("approach_payouts"):
                lo, hi = arch["approach_payouts"][approach]
                mean_payout = (lo + hi) / 2
            else:
                mean_payout = (t["payout_min"] + t["payout_max"]) / 2 * pay_mult
            play_ev = (
                chance * (mean_payout + t["pot"])
                - (1 - chance) * t["attempt_cost"]
            )
            success = random.random() < chance
            # Professional Guarantee overrides the roll on real, non-legendary
            # marks only.  It is checked after the roll rather than instead of
            # it so the recorded expected value still reflects the real odds.
            if not success and tier_of(t) != "legendary":
                guarantee = await fx.get_effect(
                    self.conn, "professional_guarantee", subject_id=uid
                )
                if guarantee:
                    success = True
                    await fx.consume_effect(self.conn, guarantee["id"])
                    special_lines.append(
                        "🎯 **Professional Guarantee applied** — success was "
                        "contractually guaranteed."
                    )

            await self.conn.execute(
                "UPDATE scam_players SET last_target_at = ? WHERE discord_user_id = ?",
                (_iso(_now()), uid),
            )
            await _touch(self.conn, uid)
            await self.conn.execute(
                "INSERT INTO scam_target_attempts (target_id, discord_user_id, attempts, lost)"
                " VALUES (?, ?, 1, 0)"
                " ON CONFLICT(target_id, discord_user_id) DO UPDATE SET"
                " attempts = attempts + 1",
                (t["id"], uid),
            )

            if success:
                # The approach multiplier only ever scales the base payout —
                # the pot rides on top untouched, so nobody can farm failures
                # and then cash them in at ×2.
                #
                # A legendary jackpot is not a multiple of an ordinary payout,
                # so a mark with per-approach ranges uses those directly.
                if arch and arch.get("approach_payouts"):
                    lo, hi = arch["approach_payouts"][approach]
                    payout = random.randint(lo, hi)
                elif arch and arch.get("payout_bands"):
                    # Which account you opened *is* the payout; no multiplier.
                    bands = arch["payout_bands"][approach]
                    lo, hi, _w = random.choices(
                        bands, weights=[b[2] for b in bands], k=1
                    )[0]
                    payout = random.randint(lo, hi)
                else:
                    payout = int(round(
                        random.randint(t["payout_min"], t["payout_max"]) * pay_mult
                    ))

                extras: list[str] = []
                bonus = 0
                if arch:
                    # A last-second snipe pays extra for the timing.
                    if arch["final_bonus"] and t["failures"] >= t["max_failures"] - 1:
                        bonus += arch["final_bonus"]
                        extras.append(
                            f"🎯 Sniper bonus: **+{money(arch['final_bonus'])}**"
                        )
                    # Everything the mark's temper added while people failed.
                    if arch["bonus_per_failure"] and t["failures"]:
                        rage = arch["bonus_per_failure"] * t["failures"]
                        bonus += rage
                        extras.append(f"😡 Rage bonus: **+{money(rage)}**")
                    if arch["success_bonus"]:
                        p_bonus, blo, bhi = arch["success_bonus"]
                        if random.random() < p_bonus:
                            extra = random.randint(blo, bhi)
                            bonus += extra
                            extras.append(
                                "☔ **The purple reign becomes a green rain** — "
                                f"royalty payment **+{money(extra)}**"
                            )
                pot_paid = 0 if (arch and arch["no_pot"]) else t["pot"]
                total = payout + pot_paid + bonus

                # MVC's people take their cut before anybody sees the money.
                cut, treasury = await self._shadow_cut(t, total)
                total -= cut
                if cut:
                    extras.append(
                        f"🕴️ **MVC's shadow network:** −{money(cut)}\n"
                        f"👑 Shadow treasury: **{money(treasury)}**"
                    )

                # Everything the /special system can do to an earning happens
                # here, on the gross, before it lands.
                total, modifiers = await fx.on_reward(
                    self.conn, uid, total, kind="target", detail=t["name"]
                )
                special_lines.extend(modifiers)
                await adjust_balance(self.conn, uid, total, "target_payout", t["name"])
                await self._retire_target(t, "taken")
                await record_play(
                    self.conn, uid, "target", play_ev, total, t["name"],
                )
                if arch and arch["intel_refill"]:
                    await self.conn.execute(
                        "UPDATE scam_players SET intel_charges = ?,"
                        " intel_next_charge_at = NULL WHERE discord_user_id = ?",
                        (INTEL_MAX_CHARGES, uid),
                    )
                    extras.append(
                        f"🔎 **Intel fully recharged — {INTEL_MAX_CHARGES}/"
                        f"{INTEL_MAX_CHARGES}**"
                    )
                if arch and arch["reset_cooldowns"]:
                    # Player-owned action cooldowns only.  Jail, bribes,
                    # punishments and every shared timer (the quick scam
                    # window, the board's own clocks, Roger, the fund) are
                    # deliberately untouched: mod powers, not time travel.
                    await self.conn.execute(
                        "UPDATE scam_players SET last_scam_at = NULL,"
                        " last_quickscam_at = NULL, last_beg_at = NULL,"
                        " last_target_at = NULL, fake_target_until = NULL,"
                        " target_lock_until = NULL,"
                        " special_cooldown_until = NULL"
                        " WHERE discord_user_id = ?", (uid,),
                    )
                    extras.append(
                        "⚡ **ALL personal cooldowns reset** — `/scam`, "
                        "`/quickscam`, `/beg`, `/special`, the board and your "
                        "disguise are all ready right now.\n"
                        "_Jail, bribes and shared timers are untouched._"
                    )
                if arch and arch["board_heat_minutes"]:
                    refreshed = await self._raise_heat(
                        uid, arch["board_heat_minutes"], t["id"]
                    )
                    extras.append(
                        ("🚨 **THE HEAT INTENSIFIES** — police attention was "
                         "already high. It is now extremely personal.\n"
                         if refreshed else
                         "🔥 **THE TARGET BOARD IS NOW ON HEAT**\n")
                        + f"For **{arch['board_heat_minutes']} minutes**, the "
                        "next player to fail against a real mark carries a "
                        "**50%** chance of arrest.\n"
                        "🎭 Fake targets are unaffected. Heat does not stack."
                    )
                if arch and arch["silence_minutes"]:
                    until = _now() + timedelta(minutes=arch["silence_minutes"])
                    await self.conn.execute(
                        "UPDATE scam_players SET silenced_until = ?"
                        " WHERE discord_user_id = ?", (_iso(until), uid),
                    )
                    extras.append(
                        f"🤐 **Silenced in this channel** until "
                        f"<t:{int(until.timestamp())}:R>"
                    )
                result = ("success", payout, total, chance, extras)
            else:
                await adjust_balance(self.conn, uid, -t["attempt_cost"], "target_cost", t["name"])

                extras: list[str] = []
                if arch:
                    # Diablo wires a fifth of everything you own to Germany.
                    if arch["wealth_loss_pct"]:
                        wealth = await exposed_wealth(self.conn, uid)
                        want = int(wealth * arch["wealth_loss_pct"])
                        gone = await seize_wealth(
                            self.conn, uid, want, reason="target_cost",
                            detail=t["name"],
                        ) if want else 0
                        extras.append(
                            f"🇩🇪 Strategic capital transfer: **−{money(gone)}**"
                            if gone else
                            "🇩🇪 German intelligence prepares a transfer, then "
                            "discovers there is nothing left to transfer."
                        )
                    # The Yahooboy wrote this scam; sometimes he runs it back.
                    if arch["reverse_scam"]:
                        p_rev, rlo, rhi = arch["reverse_scam"]
                        if random.random() < p_rev:
                            fresh = await get_player(self.conn, uid)
                            hit = min(fresh["balance"], random.randint(rlo, rhi))
                            if hit:
                                await adjust_balance(
                                    self.conn, uid, -hit, "target_counter", t["name"])
                                extras.append(f"💻 Reverse scam: **−{money(hit)}**")
                            else:
                                extras.append(
                                    "💻 His counter-scam works perfectly. You "
                                    "have no cash left to take."
                                )
                    # Euler treats the second attempt onwards as a fight, and
                    # everything he takes goes into the bounty on his own head.
                    if arch["collateral"] and t["failures"] >= 1:
                        odds, (clo, chi) = arch["collateral"]
                        if random.random() < odds.get(approach, 0.0):
                            fresh = await get_player(self.conn, uid)
                            hit = min(fresh["balance"], random.randint(clo, chi))
                            if hit:
                                await adjust_balance(
                                    self.conn, uid, -hit, "target_cost", t["name"])
                                collateral_gain = hit
                                extras.append(
                                    f"💥 Collateral damage: **−{money(hit)}** "
                                    "— straight into his pot"
                                )
                    # Mostor either refunds you and tips you, or takes more.
                    if arch["carrot_stick"]:
                        odds, (klo, khi), (slo, shi) = arch["carrot_stick"]
                        if random.random() < odds.get(approach, 0.0):
                            tip = random.randint(klo, khi)
                            await adjust_balance(
                                self.conn, uid,
                                t["attempt_cost"] + tip, "target_payout", t["name"])
                            carrot = True
                            extras.append(
                                f"🥕 **The sultan shows mercy** — "
                                f"{money(t['attempt_cost'])} refunded and a "
                                f"royal carrot of **+{money(tip)}**"
                            )
                        else:
                            fresh = await get_player(self.conn, uid)
                            hit = min(fresh["balance"], random.randint(slo, shi))
                            if hit:
                                await adjust_balance(
                                    self.conn, uid, -hit, "target_cost", t["name"])
                            extras.append(
                                "🪵 **The stick.** Twenty completely unrelated "
                                "Egyptian accounts appear in your "
                                f"notifications: **−{money(hit)}**"
                            )
                    # Roas blocks the whole road, not just her own.
                    if arch["global_lock_minutes"]:
                        until = _now() + timedelta(
                            minutes=arch["global_lock_minutes"])
                        async with self.conn.execute(
                            "SELECT target_lock_until FROM scam_players"
                            " WHERE discord_user_id = ?", (uid,),
                        ) as cur:
                            row2 = await cur.fetchone()
                        # Never additive: a second block extends to the later
                        # of the two, it does not stack another hour on top.
                        if row2 and row2[0] and _parse(row2[0]) > until:
                            until = _parse(row2[0])
                        await self.conn.execute(
                            "UPDATE scam_players SET target_lock_until = ?"
                            " WHERE discord_user_id = ?", (_iso(until), uid),
                        )
                        extras.append(
                            "🚫 **Every target is blocked to you** until "
                            f"<t:{int(until.timestamp())}:R>"
                        )
                    # Utopia's administrators reach the obvious conclusion.
                    if arch["always_arrest"] and not await get_jail(self.conn, uid):
                        fresh = await get_player(self.conn, uid)
                        wealth = fresh["balance"] + fresh["invested"]
                        jail = await arrest_player(
                            self.conn, uid,
                            max(EXTREME_FAILURE_MIN_BRIBE, t["attempt_cost"]),
                            wealth,
                            reason=f"A failed attempt on {t['name']}",
                        )
                        extras.append(
                            "🚔 **YOU HAVE BEEN ARRESTED** — bribe "
                            f"{money(jail['bribe'])}, released "
                            f"<t:{int(jail['until'].timestamp())}:R>"
                            if not jail.get("released") else
                            "🚔 **YOU HAVE BEEN ARRESTED** — and immediately "
                            "released, on presentation of a suspiciously "
                            "official card."
                        )

                # Somebody may have been waiting for exactly this failure.
                # The mark's own consequences resolve first (spec §H); the
                # informant's tip-off lands on top of whatever is left.
                informant = await fx.take_trap(self.conn, "police_informant", uid)
                if informant and not await get_jail(self.conn, uid):
                    fresh = await get_player(self.conn, uid)
                    jail = await arrest_player(
                        self.conn, uid, EXTREME_FAILURE_MIN_BRIBE,
                        fresh["balance"] + fresh["invested"],
                        reason=f"Reported to the police after failing on "
                               f"{t['name']}",
                    )
                    special_lines.append(
                        f"🚔 **POLICE INFORMANT** — the authorities were "
                        f"already waiting. <@{informant['owner_id']}> provided "
                        "the tip-off, and remained anonymous for approximately "
                        "four seconds."
                        + ("\n🎫 You walked straight back out."
                           if jail.get("released") else "")
                    )

                # Heat is deliberately dead last (spec §4.7): it only rolls
                # for a player who is still free after everything above, and
                # a player who is already inside neither rolls nor burns it.
                heat_line = await self._resolve_heat(uid)
                if heat_line:
                    special_lines.append(heat_line)

                # Most marks bank exactly what was lost on them. Gerard banks
                # more, which is the whole reason anyone endures Gerard.
                pot_gain = (
                    arch["pot_per_failure"] if arch else t["attempt_cost"]
                )
                if arch and arch["no_pot"]:
                    pot_gain = 0
                if arch and arch["carrot_stick"] and carrot:
                    # The cost came back, so it cannot also feed the pot.
                    pot_gain = 0
                # Collateral is the pot on this mark; the attempt cost is not.
                if arch and arch["collateral"]:
                    pot_gain = collateral_gain
                new_pot = t["pot"] + pot_gain
                failures = t["failures"] + 1
                new_chance = _chance_after_failure(t, arch, failures)
                await self.conn.execute(
                    "UPDATE scam_targets SET pot = ?, failures = ?, chance = ?"
                    " WHERE id = ?",
                    (new_pot, failures, new_chance, t["id"]),
                )
                await self.conn.execute(
                    "UPDATE scam_target_attempts SET lost = lost + ?"
                    " WHERE target_id = ? AND discord_user_id = ?",
                    (t["attempt_cost"], t["id"], uid),
                )
                await self.conn.commit()
                # Some marks fight back. Chinedu invented this trade and does
                # not care for the competition.
                counter = 0
                if arch and arch["npc_counter_chance"] > 0:
                    if random.random() < arch["npc_counter_chance"]:
                        lo, hi = arch["npc_counter_steal"]
                        counter = min(
                            random.randint(lo, hi),
                            max(0, player["balance"] - t["attempt_cost"]),
                        )
                        if counter:
                            await adjust_balance(self.conn, uid, -counter, "target_counter", t["name"])
                burned = failures >= t["max_failures"]
                if burned:
                    await self._retire_target(t, "burned")
                await record_play(
                    self.conn, uid, "target", play_ev,
                    -(t["attempt_cost"] + counter), t["name"],
                )
                result = ("fail", new_pot, failures, chance, burned, new_chance,
                          counter, extras)
            await self.conn.commit()

        await self._mark_board_activity()
        await self.refresh_board()

        # ── report ────────────────────────────────────────────────────
        if result[0] == "success":
            _r, payout, total, used_chance, extras = result
            scene = t.get("success_text") or f"**{t['name']} fell for it.**"
            desc = (
                f"{scene}\n\n"
                f"Approach: {approach_label} ({used_chance * 100:.0f}% chance)\n"
                f"Payout: **+{money(payout)}**"
            )
            if total > payout:
                desc += f"\nPot collected: **+{money(total - payout)}**"
            if extras:
                desc += "\n\n" + "\n".join(extras)
            if special_lines:
                desc += "\n\n" + "\n".join(special_lines)
            desc += (
                f"\n\n**Total: +{money(total)}**\n\n"
                f"{t['name']} is off the board. A new mark will turn up shortly."
            )
            embed = discord.Embed(
                title=f"{t['emoji']} SCAM SUCCESSFUL — {t['name']}",
                description=desc,
                colour=_EMBED_GREEN,
            )
            await _reply(
                interaction,
                content=f"🎉 {interaction.user.mention} took down **{t['name']}**!",
                embed=embed,
            )
            return

        _r, new_pot, failures, used_chance, burned, new_chance, counter, extras = result
        scene = t.get("failure_text") or f"**{t['name']} does not believe you.**"
        desc = (
            f"{scene}\n\n"
            f"Approach: {approach_label} ({used_chance * 100:.0f}% chance)\n"
            f"You lose **{money(t['attempt_cost'])}**."
        )
        if extras:
            desc += "\n\n" + "\n".join(extras)
        if special_lines:
            desc += "\n\n" + "\n".join(special_lines)
        if counter:
            desc += (
                f"\n\n👑 **{t['name']} counter-scammed you** and lifted a "
                f"further **{money(counter)}** from your pocket while you were "
                "busy explaining yourself."
            )
        if burned:
            embed = discord.Embed(
                title=f"🚨 {t['name'].upper()} HAS WORKED IT OUT",
                description=(
                    desc + "\n\n"
                    f"That was one attempt too many. {t['name']} has reported it "
                    "and closed every account.\n\n"
                    f"The pot of **{money(new_pot)}** is gone. Nobody gets it.\n\n"
                    "A new mark will turn up shortly."
                ),
                colour=_EMBED_RED,
            )
        else:
            arch = archetype_for(t)
            if arch and arch.get("warms_up"):
                shift_note = (
                    f"They are **easier now**: the odds rise to "
                    f"**{new_chance * 100:.0f}%** "
                    f"({failures}/{t['max_failures']} attempts used)."
                )
            elif arch and arch.get("chance_ladder"):
                shift_note = (
                    f"They are **paying attention now**: the odds drop to "
                    f"**{new_chance * 100:.0f}%** "
                    f"({failures}/{t['max_failures']} attempts used)."
                )
            elif arch and not arch.get("decays", True):
                shift_note = (
                    f"The odds are unchanged — but that is "
                    f"**{failures} of {t['max_failures']}** attempts gone."
                )
            else:
                shift_note = (
                    f"They are now **{status_label(failures).lower()}**: chance "
                    f"drops to **{new_chance * 100:.0f}%** "
                    f"({failures}/{t['max_failures']} failures)."
                )
            embed = discord.Embed(
                title=f"{t['emoji']} SCAM FAILED — {t['name']}",
                description=(
                    desc + "\n\n"
                    f"The pot on {t['name']} rises to **{money(new_pot)}** — "
                    "waiting for whoever tries next.\n" + shift_note
                ),
                colour=_EMBED_RED,
            )
        await _reply(
            interaction,
            content=f"💸 {interaction.user.mention} failed on **{t['name']}**.",
            embed=embed,
        )



async def setup(bot: commands.Bot, conn: aiosqlite.Connection) -> ScamTargetsCog:
    cog = ScamTargetsCog(bot, conn)
    await bot.add_cog(cog)
    return cog
