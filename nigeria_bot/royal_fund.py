"""The Royal Investment Fund — a Ponzi scheme with a risk dial.

The old fund was a slot machine on a timer: every 45–180 minutes it paid
interest, did nothing, lost a chunk, or vanished.  This one is a *state
machine*.  Roger runs the fund, Roger gets bored when nothing goes wrong, and
everything the players do — depositing, withdrawing, hoarding, quick-scamming
— pushes his Risk Level around.

    Risk 1  🟢 Fully Trustworthy        event every 30 min
    Risk 2  🟢 Slightly Exposed         event every 20 min
    Risk 3  🟡 Roger Is Concerned       event every 15 min
    Risk 4  🟠 Roger Is Getting Desperate  event every 10 min
    Risk 5  🔴 ABSOLUTELY FINE          event every 3–5 min

Higher risk means faster events, wilder swings, and — only at Risk 4 and 5 —
a chance the whole thing goes to zero before an event even resolves.

**The accounting invariant.**  §4 of the design demands that the sum of every
investor's position always equals the fund's total value.  Rather than police
that with reconciliation, the total is *defined* as ``SUM(scam_players.
invested)``.  There is no second number to drift, so the invariant cannot be
violated — only positions are ever written.

Money that enters or leaves the *game* (dividends, Roger's promotional cash,
tax confiscations) moves between a position and a player's cash balance and is
always described as such below.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .scam_game import (
    record_ledger,
    CURRENCY,
    GAME_CHANNEL_ID,
    GAME_CHANNEL_URL,
    _EMBED_GOLD,
    _EMBED_GREEN,
    _EMBED_GREY,
    _EMBED_RED,
    _iso,
    _now,
    _parse,
    _require_channel,
    adjust_balance,
    get_player,
    money,
    require_free,
)

logger = logging.getLogger("nigeria_bot.royal_fund")


# ── Configuration ─────────────────────────────────────────────────────────────

RISK_NAMES = {
    1: "Fully Trustworthy",
    2: "Slightly Exposed",
    3: "Roger Is Concerned",
    4: "Roger Is Getting Desperate",
    5: "ABSOLUTELY FINE",
}
RISK_DOT = {1: "🟢", 2: "🟢", 3: "🟡", 4: "🟠", 5: "🔴"}
RISK_METER = {
    1: "🟩⬜⬜⬜⬜",
    2: "🟩🟩⬜⬜⬜",
    3: "🟨🟨🟨⬜⬜",
    4: "🟧🟧🟧🟧⬜",
    5: "🟥🟥🟥🟥🟥",
}
RISK_COLOUR = {
    1: discord.Colour(0x2ECC71),
    2: discord.Colour(0x27AE60),
    3: discord.Colour(0xF1C40F),
    4: discord.Colour(0xE67E22),
    5: discord.Colour(0xE74C3C),
}

# Minutes between fund events, per risk level.  Risk 5 is a range.
EVENT_INTERVAL = {1: (30, 30), 2: (20, 20), 3: (15, 15), 4: (10, 10), 5: (3, 5)}

# Share of normal events drawn from the Core pool; the rest come from Special.
CORE_SHARE = {1: 0.70, 2: 0.65, 3: 0.55, 4: 0.50, 5: 0.30}

# Pre-event true-collapse roll.  Impossible below Risk 4 by design.
COLLAPSE_CHANCE = {4: 0.005, 5: 0.04}
COLLAPSE_PRESSURE_MAX = 8          # → 12% maximum collapse chance at Risk 5
MATCH_EVENT_CHANCE = 0.50          # chance a pending quick-scam match fires
MATCH_HOLD_MINUTES = 60            # §10.5: an unusable match waits this long
RECENT_EVENTS_KEPT = 5             # §17: shown on the status card
HOURLY_REPORT_MINUTES = 60         # §18

FLOW_WINDOW_MINUTES = 15           # deposit/withdrawal buckets
HOURLY_CHECK_MINUTES = 60
CAPITAL_CALL_MINUTES = 10
CAPITAL_CALL_TARGET = 10_000
CAMPAIGN_MINUTES = 10

# §12.  The tax is *destroyed*, not kept by the fund: the player's position
# and the fund total both fall by the gross amount, and only the net reaches
# their balance.  Leaving is free until Roger is actually in trouble.
ANTI_PANIC_TAX = {1: 0.00, 2: 0.00, 3: 0.00, 4: 0.05, 5: 0.10}

# §9.1 hourly structural check: fund value → (risk-up chance, risk-down chance).
#
# TUNED.  The spec's curve started at 5% for a small fund, which made a
# freshly-collapsed fund essentially inert — measured over 40 simulated days it
# parked at Risk 1 for 62% of the time against a 25–35% target, and Risk 2
# never became the "most common healthy state" §22 asks for.  Flattening the
# low end (5→12%, 7→14%, 10→16%) gets a rebuilding fund moving again without
# touching the high end, where the big-fund danger is supposed to live.
EXPOSURE_TABLE = [
    (5_000,   0.12, 0.10),
    (10_000,  0.14, 0.09),
    (20_000,  0.16, 0.08),
    (35_000,  0.18, 0.06),
    (50_000,  0.24, 0.04),
    (75_000,  0.35, 0.02),
    (math.inf, 0.45, 0.01),
]
# §9.1 modifiers: passive calming is weaker where Roger is already committed.
#
# TUNED.  Risk 3 and 4 were the *thinnest* slices in testing (the fund raced
# through them to 5), so their passive calming is stronger than the spec's:
# it slows the climb and gives 3 and 4 somewhere to sit.  Risk 4's halving is
# undone entirely, which is what pulled collapses back from every 0,73 days to
# roughly one a day.
RISK_DOWN_SCALE = {1: 0.0, 2: 0.5, 3: 1.5, 4: 1.0, 5: 0.0}

# §9.2 investor concentration, checked largest-share-first.
CONCENTRATION = [(0.80, 0.15), (0.60, 0.10), (0.40, 0.05)]
MANY_INVESTORS_BONUS = -0.05       # 5+ unique investors
MANY_INVESTORS_MIN = 5
HOURLY_UP_CLAMP = (0.0, 0.60)

# §9.3 complacency
COMPLACENCY_AFTER_HOURS = 3
COMPLACENCY_PER_HOUR = 0.02
COMPLACENCY_MAX = 0.10

# §9.4 deposits calm Roger
DEPOSIT_CALM_PER_100 = 0.01
DEPOSIT_CALM_MAX = 0.20
# TUNED.  §9.1 already halves *hourly* risk-down at Risk 2 with the stated
# reason that "Risk 2 should not constantly fall back to 1" — but deposit-driven
# calming was left at full strength, and it turned out to be the single largest
# downward force in the whole system (223 of 285 measured risk-drops).  Halving
# it at Risk 2 applies the spec's own reasoning to the mechanic that actually
# causes the problem.
DEPOSIT_CALM_SCALE = {1: 0.0, 2: 0.5, 3: 1.0, 4: 0.50, 5: 0.25}

# §9.5 withdrawals scare Roger: (threshold, risk-up chance)
WITHDRAW_SCARE_TABLE = [
    (500,    0.00),
    (1_000,  0.05),
    (2_000,  0.10),
    (3_500,  0.20),
    (5_000,  0.30),
    (10_000, 0.40),
    (math.inf, 0.60),
]
WITHDRAW_SCARE_CAP = 0.80

# §9.6 quick scam outcomes → (risk delta, chance).  Rarity is ignored; only
# how the operation went matters.
SCAM_RISK_EFFECT = {
    "success":      (-1, 0.04),
    "rare_success": (-1, 0.40),
    "failure":      (+1, 0.10),
    "extreme":      (+1, 0.50),
}
RISK_DOWN_FROM_SCAMS = {1: 1.0, 2: 1.0, 3: 1.0, 4: 0.50, 5: 0.25}


# §14.  Roger never lets a risk change pass without commentary.
TRANSITION_QUOTES = {
    (1, 2): "The fund was getting boring.",
    (2, 3): "I am not worried. I am merely monitoring things aggressively.",
    (3, 4): "Nobody withdraw anything. That is not advice, that is a request.",
    (4, 5): "EVERYTHING IS ABSOLUTELY FINE. NOBODY TOUCH THE WITHDRAW BUTTON.",
    (2, 1): "See? Completely trustworthy again. As advertised.",
    (3, 2): "Good. Good. I was never actually concerned.",
    (4, 3): "Excellent. The emergency is over. There was never an emergency.",
    (5, 4): "WE ARE SAVED. Temporarily.",
    (5, 3): "I HAVE NEVER DOUBTED THIS FUND FOR A SINGLE SECOND.",
}

# §17/§18.  What Roger is visibly doing at each risk level.
ROGER_STATUS = {
    1: "Roger is relaxed.",
    2: "Roger is watching the numbers.",
    3: "Roger has opened three spreadsheets.",
    4: "Roger is making phone calls.",
    5: "Roger is online.",
}


def _pct(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


def _signed(pct: float) -> str:
    return f"{'+' if pct >= 0 else ''}{pct * 100:.1f}%"


# ── Event definitions ─────────────────────────────────────────────────────────
# Every event is data.  `kind` names the mechanic; the engine in
# :func:`_apply_event` knows how to run each one.  `risk` movement, cooldowns
# and eligibility are declarative so no event needs its own code path.

_EVENT_FIELDS = {
    "risk": None,            # which risk level's pool this belongs to
    "pool": "core",          # "core" | "special"
    "weight": 0.0,           # weight inside that pool
    "kind": "scale",
    "lo": 0.0, "hi": 0.0,    # primary range (fraction, or Naira for flat kinds)
    "lo2": 0.0, "hi2": 0.0,  # secondary range for coin-flip events
    "p": 0.5,                # coin-flip probability of the first branch
    "risk_delta": 0,         # deterministic risk movement
    "risk_chance": None,     # (probability, delta) — rolled movement
    "risk_5050": False,      # audit scare: 50% −1 / 50% +1
    "pressure": 0,           # collapse pressure added at Risk 5
    "cooldown": 0,           # minutes before this event may recur
    "min_investors": 1,
    "min_position": 0,       # smallest position that makes a target eligible
    "amount": 0,             # flat Naira for cash awards / calls
    "match": None,           # quick-scam template id this event answers
}


def _ev(id: str, emoji: str, name: str, description: str, roger: str, **kw) -> dict:
    unknown = [k for k in kw if k not in _EVENT_FIELDS]
    if unknown:
        raise ValueError(f"fund event {id!r} has unknown fields {unknown}")
    return {
        "id": id, "emoji": emoji, "name": name,
        "description": description, "roger": roger,
        **_EVENT_FIELDS, **kw,
    }


EVENTS: list[dict] = [

    # ══════════════════════════════════════════════════════════════════════
    # RISK 1 — CORE
    # ══════════════════════════════════════════════════════════════════════
    _ev("r1_dividend", "💰", "MODEST DIVIDEND",
        "Another entirely ordinary and completely legitimate financial period "
        "concludes.",
        "Money went in and more money came out. I see no reason to investigate "
        "further.",
        risk=1, pool="core", weight=22, kind="dividend", lo=0.01, hi=0.035),
    _ev("r1_appreciation", "📈", "HEALTHY APPRECIATION",
        "Several investments quietly increase in value. Nobody is entirely sure "
        "which investments those were.",
        "This is what happens when you let professionals like me press the "
        "buttons.",
        risk=1, pool="core", weight=22, kind="scale", lo=0.02, hi=0.06),
    _ev("r1_correction", "📉", "MINOR MARKET CORRECTION",
        "A few holdings decline. Roger assures everyone this is perfectly "
        "normal.",
        "It only becomes a loss if you emotionally acknowledge it.",
        risk=1, pool="core", weight=23, kind="scale", lo=-0.05, hi=-0.02),
    _ev("r1_interest", "🏦", "INTEREST INCOME",
        "Some money was apparently left in an actual bank account earning "
        "interest. This surprises everyone, including Roger.",
        "Sometimes forgetting to invest the money is my best strategy.",
        risk=1, pool="core", weight=10, kind="scale", lo=0.01, hi=0.03),
    _ev("r1_admin", "🧾", "ADMINISTRATIVE EXPENSES",
        "Lawyers, accountants and several unexplained invoices are paid. One "
        "invoice simply reads 'Roger — expenses.'",
        "Transparency is surprisingly expensive.",
        risk=1, pool="core", weight=10, kind="scale", lo=-0.03, hi=-0.01),
    _ev("r1_writeoff", "🗑️", "ROUTINE WRITE-OFF",
        "One of Roger's supposedly guaranteed short-term investments is quietly "
        "removed from the books.",
        "Small losses are how you know the investments are real.",
        risk=1, pool="core", weight=8, kind="scale", lo=-0.06, hi=-0.03),
    _ev("r1_teller", "🫴", "CORRUPT BANK TELLER",
        "The Royal Fund's trusted bank teller has accepted so many bribes that "
        "he has finally decided to start taking his own.",
        "He usually takes bribes for us. Apparently he has decided to diversify.",
        risk=1, pool="core", weight=5, kind="flat_loss_one",
        lo=200, hi=500, min_position=500),

    # ── RISK 1 — SPECIAL ──────────────────────────────────────────────────
    # TUNED: the three Risk-1 escapes carry 6x their specified weight.  §2 says
    # Risk 1 should "feel steady, but Roger becomes bored and pushes toward more
    # risk" — at the original weights he almost never did, and Risk 1 became an
    # absorbing state.  They are now the bulk of the Risk-1 Special pool, which
    # is the stated feel rather than a departure from it.
    _ev("r1_bored", "😴", "ROGER GETS BORED",
        "Nothing has gone wrong for far too long. Roger concludes the fund is "
        "being managed too conservatively.",
        "If nothing is going wrong, we clearly aren't taking enough risk.",
        risk=1, pool="special", weight=60, kind="nothing",
        risk_delta=+1, cooldown=30),
    _ev("r1_growth", "📊", "GROWTH TARGETS",
        "Roger discovers that competing funds have graphs which rise more "
        "quickly and announces new growth targets.",
        "Eight percent is nice. Twenty percent is more.",
        risk=1, pool="special", weight=42, kind="scale", lo=0.01, hi=0.03,
        risk_delta=+1, cooldown=30),
    _ev("r1_executive", "🎩", "EXECUTIVE EXPANSION",
        "Roger opens three new offices, hires two advisers and buys a very "
        "impressive desk. None of this generates revenue.",
        "Successful financial institutions have expensive furniture.",
        risk=1, pool="special", weight=36, kind="scale", lo=-0.03, hi=-0.01,
        risk_delta=+1, cooldown=30),
    _ev("r1_diversify", "🤝", "DIVERSIFICATION INCENTIVE",
        "Roger introduces a revolutionary diversification policy. The largest "
        "investor was not consulted.",
        "Your money is still here. It simply belongs slightly less to you.",
        risk=1, pool="special", weight=15, kind="transfer_down",
        lo=100, hi=500, min_investors=2, min_position=250, cooldown=30),
    _ev("r1_lottery", "🎲", "INVESTOR LOTTERY",
        "Roger launches a lottery designed to encourage smaller investors to "
        "remain engaged.",
        "Even small investors deserve the opportunity to eventually lose "
        "meaningful amounts of money.",
        risk=1, pool="special", weight=15, kind="lottery",
        lo=100, hi=750, min_investors=2, min_position=250, cooldown=30),
    _ev("r1_rounding", "🧮", "ROUNDING ERROR",
        "The accounting system encounters a small rounding discrepancy. The "
        "discrepancy happens to have an owner.",
        "With numbers this large, eventually some of them end up belonging to "
        "the wrong person.",
        risk=1, pool="special", weight=14, kind="transfer_random",
        lo=100, hi=500, min_investors=2, cooldown=30),
    _ev("r1_investor_month", "🏆", "INVESTOR OF THE MONTH",
        "Roger launches a customer-retention programme and awards the newest "
        "serious investor.",
        "Loyalty is important. Recent loyalty is easier to remember.",
        risk=1, pool="special", weight=16, kind="cash_award",
        amount=1_000, cooldown=60),
    _ev("r1_campaign", "💳", "DEPOSIT CAMPAIGN",
        "Roger decides the best way to reward confidence is to subsidise anyone "
        "giving him more money.",
        "For a limited time, I will personally reward anyone willing to give me "
        "more money.",
        risk=1, pool="special", weight=17, kind="campaign",
        lo=0.05, amount=500, cooldown=90),

    # ══════════════════════════════════════════════════════════════════════
    # RISK 2 — CORE
    # ══════════════════════════════════════════════════════════════════════
    _ev("r2_dividend", "💰", "DIVIDEND PAYOUT",
        "The fund posts another respectable return. Roger immediately takes "
        "full credit.",
        "I personally generated this return by approving its distribution.",
        risk=2, pool="core", weight=23, kind="dividend", lo=0.02, hi=0.06),
    _ev("r2_strong", "📈", "STRONG MARKET DAY",
        "Several holdings rise at the same time. Roger claims this was entirely "
        "anticipated.",
        "The market has finally recognised my vision. I will determine what "
        "that vision was later.",
        risk=2, pool="core", weight=24, kind="scale", lo=0.04, hi=0.07),
    _ev("r2_bad_day", "📉", "UNFORTUNATE TRADING DAY",
        "Several positions move in the wrong direction at once.",
        "The investment performed exactly as expected, but unfortunately in the "
        "opposite direction.",
        risk=2, pool="core", weight=25, kind="scale", lo=-0.08, hi=-0.04),
    _ev("r2_interest", "🏦", "ROUTINE INTEREST & INCOME",
        "Some boring but legitimate income reaches the Royal Fund.",
        "Boring income is still income. Please don't tell anyone I said that.",
        risk=2, pool="core", weight=10, kind="scale", lo=0.01, hi=0.04),
    _ev("r2_opcosts", "🧾", "OPERATING COSTS",
        "The cost of running several investment vehicles is deducted from the "
        "fund.",
        "Running seventeen companies takes administration. Especially when "
        "eleven share an address.",
        risk=2, pool="core", weight=8, kind="scale", lo=-0.05, hi=-0.02),
    _ev("r2_writedown", "📉", "ASSET WRITE-DOWN",
        "An investment valued by somebody's cousin is reassessed by an actual "
        "accountant.",
        "The asset did not lose value. The previous value simply turned out to "
        "be fictional.",
        risk=2, pool="core", weight=5, kind="scale", lo=-0.08, hi=-0.04),
    _ev("r2_vip_fees", "💼", "VIP MANAGEMENT FEES",
        "Roger introduces premium management fees for the people wealthy enough "
        "to appreciate premium service.",
        "Premium investors receive premium financial services. Unfortunately "
        "those services have premium fees.",
        risk=2, pool="core", weight=5, kind="flat_loss_top3",
        lo=200, hi=500, min_position=500),

    # ── RISK 2 — SPECIAL ──────────────────────────────────────────────────
    _ev("r2_compounding", "🤑", "ROGER DISCOVERS COMPOUNDING",
        "Roger learns that reinvesting profits can create exponential growth. "
        "He skips the chapter about exponential risk.",
        "If money makes money, more money obviously makes even more money.",
        risk=2, pool="special", weight=7, kind="nothing",
        risk_delta=+1, cooldown=30),
    _ev("r2_international", "🧳", "INTERNATIONAL EXPANSION",
        "Roger opens investment vehicles in several jurisdictions where "
        "regulation is described as flexible.",
        "Every country has different regulations. That's why we use many "
        "countries.",
        risk=2, pool="special", weight=7, kind="scale", lo=0.02, hi=0.05,
        risk_delta=+1, cooldown=30),
    _ev("r2_compliance", "🛡️", "COMPLIANCE CLEANUP",
        "Roger reluctantly hires lawyers and accountants to make several parts "
        "of the fund appear significantly more legitimate.",
        "Apparently compliance is cheaper than prison. Barely.",
        risk=2, pool="special", weight=4, kind="scale", lo=-0.04, hi=-0.02,
        risk_delta=-1, cooldown=60),
    _ev("r2_bicycles", "🚲", "BICYCLE PORTFOLIO",
        "Roger acquires a suspiciously large quantity of second-hand Dutch "
        "bicycles. Their provenance is described as complicated.",
        "You call them stolen bicycles. I call them mobile Dutch securities.",
        risk=2, pool="special", weight=10, kind="coin", p=0.80,
        lo=0.03, hi=0.08, lo2=-0.05, hi2=-0.02, cooldown=30, match="bicycle"),
    _ev("r2_gouda", "🧀", "GOUDA FUTURES",
        "Roger invests in Gouda futures after discovering that Dutch citizens "
        "continue to eat cheese.",
        "Dutch people have eaten cheese for centuries. The fundamentals are "
        "excellent.",
        risk=2, pool="special", weight=10, kind="coin", p=0.80,
        lo=0.03, hi=0.10, lo2=-0.06, hi2=-0.03, cooldown=30, match="gouda"),
    _ev("r2_diversify", "🤝", "DIVERSIFICATION INCENTIVE",
        "Roger attempts to reduce concentration risk by redistributing some "
        "concentration.",
        "Concentration risk has been solved by taking some of your "
        "concentration.",
        risk=2, pool="special", weight=10, kind="transfer_down",
        lo=100, hi=500, min_investors=2, min_position=250, cooldown=30),
    _ev("r2_lottery", "🎲", "INVESTOR LOTTERY",
        "Roger announces another highly scientific diversification mechanism.",
        "Wealth distribution is much more exciting when conducted randomly.",
        risk=2, pool="special", weight=10, kind="lottery",
        lo=100, hi=750, min_investors=2, min_position=250, cooldown=30),
    _ev("r2_rounding", "🧮", "ROUNDING ERROR",
        "The fund's accounting software places several numbers in the wrong "
        "place.",
        "Accounting is just probability with invoices.",
        risk=2, pool="special", weight=9, kind="transfer_random",
        lo=100, hi=500, min_investors=2, cooldown=30),
    _ev("r2_investor_month", "🏆", "INVESTOR OF THE MONTH",
        "Roger congratulates the newest serious investor for demonstrating "
        "exceptional recent loyalty.",
        "Congratulations on being the investor whose name I remember.",
        risk=2, pool="special", weight=10, kind="cash_award",
        amount=1_000, cooldown=60),
    _ev("r2_campaign", "💳", "DEPOSIT CAMPAIGN",
        "Roger temporarily increases promotional spending to attract fresh "
        "capital.",
        "Give me money now and I will give you slightly more money "
        "immediately. Sustainable.",
        risk=2, pool="special", weight=10, kind="campaign",
        lo=0.07, amount=750, cooldown=90),
    _ev("r2_panic", "📞", "WITHDRAWAL PANIC",
        "Several investors begin asking about withdrawals at the same time. "
        "Roger repeatedly explains that nobody should panic.",
        "Nobody panic. Especially not anyone currently pressing Withdraw.",
        risk=2, pool="special", weight=11, kind="panic",
        lo=0.10, amount=20, cooldown=90),
    _ev("r2_anchor", "🐋", "ANCHOR INVESTOR CONFIDENCE",
        "Roger points to a major investor as evidence that the fund must be "
        "trustworthy.",
        "Would one person really put that much money here if this was unsafe?",
        risk=2, pool="special", weight=2, kind="anchor",
        risk_chance=(0.50, -1), cooldown=60),

    # ══════════════════════════════════════════════════════════════════════
    # RISK 3 — CORE
    # ══════════════════════════════════════════════════════════════════════
    _ev("r3_dividend", "💰", "AGGRESSIVE DIVIDEND",
        "Roger announces a larger-than-usual distribution to reassure "
        "investors.",
        "Nothing restores confidence like returning some of the money.",
        risk=3, pool="core", weight=24, kind="dividend", lo=0.03, hi=0.10),
    _ev("r3_appreciation", "📈", "STRONG APPRECIATION",
        "Several of the fund's more questionable investments suddenly perform "
        "very well.",
        "The strategy is working. I advise everyone not to ask which strategy.",
        risk=3, pool="core", weight=28, kind="scale", lo=0.05, hi=0.12),
    _ev("r3_correction", "📉", "SIGNIFICANT CORRECTION",
        "The market moves against several Royal Fund positions at once.",
        "Eleven percent sounds much less frightening if you say 'temporary "
        "correction'.",
        risk=3, pool="core", weight=28, kind="scale", lo=-0.11, hi=-0.05),
    _ev("r3_legal", "🧾", "LEGAL & OPERATIONAL COSTS",
        "Legal costs, compliance work and unexplained operational expenses "
        "increase.",
        "The lawyers say this expense prevents much larger expenses later. Very "
        "reassuring.",
        risk=3, pool="core", weight=12, kind="scale", lo=-0.07, hi=-0.03),
    _ev("r3_bad_position", "💣", "BAD POSITION WRITTEN OFF",
        "One increasingly indefensible investment is finally removed from the "
        "balance sheet.",
        "The position has not failed. We have simply stopped including it in "
        "calculations.",
        risk=3, pool="core", weight=8, kind="scale", lo=-0.14, hi=-0.07),

    # ── RISK 3 — SPECIAL ──────────────────────────────────────────────────
    _ev("r3_double_down", "🚀", "ROGER DOUBLES DOWN",
        "Roger identifies a highly profitable opportunity and dramatically "
        "increases exposure.",
        "Risk is simply profit before it has happened.",
        risk=3, pool="special", weight=5, kind="scale", lo=0.08, hi=0.15,
        risk_delta=+1, cooldown=30),
    _ev("r3_delever", "🧯", "EMERGENCY DELEVERAGING",
        "Roger sells several risky positions at a loss before they become an "
        "even larger problem.",
        "We have successfully lost money to avoid losing more money.",
        risk=3, pool="special", weight=5, kind="scale", lo=-0.07, hi=-0.03,
        risk_delta=-1, cooldown=60),
    _ev("r3_audit_scare", "🔎", "AUDIT SCARE",
        "Auditors begin reviewing several Royal Fund transactions. Nobody knows "
        "whether the cleanup will reassure them or reveal something worse.",
        "An audit is gambling where the jackpot is staying out of prison.",
        risk=3, pool="special", weight=4, kind="scale", lo=-0.06, hi=-0.03,
        risk_5050=True, cooldown=60),
    _ev("r3_capital_call", "🚨", "EMERGENCY CAPITAL CALL",
        "Roger announces that the fund urgently requires new capital for "
        "reasons he refuses to describe as urgent.",
        "This is not a bailout. I merely require ten thousand Naira "
        "immediately.",
        risk=3, pool="special", weight=6, kind="capital_call",
        amount=CAPITAL_CALL_TARGET, cooldown=120),
    _ev("r3_panic", "📞", "WITHDRAWAL PANIC",
        "One withdrawal causes several other investors to inspect the "
        "withdrawal button.",
        "Every withdrawal is entirely manageable provided nobody else sees it.",
        risk=3, pool="special", weight=6, kind="panic",
        lo=0.15, amount=20, cooldown=90),
    _ev("r3_agriculture", "🌿", "MYSTERIOUS AGRICULTURAL INVESTMENT",
        "Roger invests in several hectares of extremely profitable crops whose "
        "exact species are unavailable for comment.",
        "Agriculture has existed for thousands of years. Clearly low risk.",
        risk=3, pool="special", weight=9, kind="coin", p=0.50,
        lo=0.05, hi=0.15, lo2=-0.10, hi2=-0.05, cooldown=30,
        match="cooperative"),
    _ev("r3_aap", "🦍", "AAP INDUSTRIES PARTNERSHIP",
        "The Royal Fund enters a lucrative commercial arrangement with Aap "
        "Industries. Nobody receives a copy of the contract.",
        "The contract is confidential because confidence is easier without "
        "documents.",
        risk=3, pool="special", weight=9, kind="scale", lo=0.06, hi=0.15,
        risk_chance=(0.15, +1), cooldown=30, match="pharma"),
    _ev("r3_winner", "🎯", "ROGER PICKS A WINNER",
        "Roger privately allocates one investor's money to a special high-yield "
        "opportunity.",
        "Congratulations. I risked your money better than everyone else's.",
        risk=3, pool="special", weight=8, kind="pick_winner",
        lo=0.15, hi=0.25, cooldown=30),
    _ev("r3_loser", "🪦", "ROGER PICKS A LOSER",
        "A single personalised investment performs significantly worse than the "
        "rest of the fund.",
        "The good news is that this particular problem is highly personalised.",
        risk=3, pool="special", weight=9, kind="pick_loser",
        lo=0.08, hi=0.15, cooldown=60),
    _ev("r3_margin_call", "☎️", "MARGIN CALL",
        "One of Roger's personalised investment vehicles receives an unfortunate "
        "call from its lender.",
        "Apparently lenders can ask for their money back too.",
        risk=3, pool="special", weight=6, kind="margin_call",
        lo=0.05, hi=0.12, cooldown=60),
    _ev("r3_asset", "📦", "FORGOTTEN ASSET DISCOVERED",
        "Auditors discover that one of Roger's many shell companies "
        "accidentally owns something valuable.",
        "This is why I establish so many companies. Eventually one accidentally "
        "owns something.",
        risk=3, pool="special", weight=9, kind="scale", lo=0.10, hi=0.20,
        cooldown=30),
    _ev("r3_liability", "🕳️", "FORGOTTEN LIABILITY DISCOVERED",
        "Unfortunately, another shell company also has several unpaid "
        "obligations nobody remembered.",
        "Assets are investments. Liabilities are surprises.",
        risk=3, pool="special", weight=9, kind="scale", lo=-0.15, hi=-0.08,
        cooldown=30),
    _ev("r3_govt", "🏛️", "GOVERNMENT CONTRACT",
        "The Nigerian government awards the Royal Fund a contract for "
        "unspecified strategic financial services. Roger is both contractor and "
        "one of the officials approving it.",
        "There is no conflict of interest. I am interested in both sides.",
        risk=3, pool="special", weight=6, kind="scale", lo=0.05, hi=0.12,
        cooldown=30),
    _ev("r3_creative", "🧾", "CREATIVE ACCOUNTING",
        "Roger revalues several assets upward. No actual economic activity has "
        "occurred.",
        "Nothing changed except the number that tells us how rich we are.",
        risk=3, pool="special", weight=4, kind="scale", lo=0.08, hi=0.15,
        risk_delta=+1, cooldown=30),
    _ev("r3_tax", "🧾", "FEDERAL TAX AUDIT",
        "Nigerian tax investigators compare Royal Fund investors with "
        "mysteriously undeclared cash balances.",
        "I specifically told everyone money inside my fund was more "
        "tax-efficient.",
        risk=3, pool="special", weight=4, kind="tax_audit",
        lo=0.30, hi=0.50, amount=2_000, cooldown=720),
    _ev("r3_akwabi", "✈️", "PRINCE AKWABI'S STRATEGIC WITHDRAWAL",
        "Roger's trusted uncle Prince Akwabi offers to safeguard a large "
        "portion of the fund during regulatory attention. Shortly afterwards, "
        "his phone number stops working.",
        "Akwabi assures me the money is completely safe. He has also changed "
        "his phone number.",
        risk=3, pool="special", weight=1, kind="scale", lo=-0.55, hi=-0.45,
        risk_delta=-1, cooldown=360),

    # ══════════════════════════════════════════════════════════════════════
    # RISK 4 — CORE
    # ══════════════════════════════════════════════════════════════════════
    _ev("r4_dividend", "💰", "DESPERATION DIVIDEND",
        "Roger pays an unusually large dividend to prove the fund remains "
        "healthy.",
        "A collapsing fund would never pay twelve percent. Think about that.",
        risk=4, pool="core", weight=23, kind="dividend", lo=0.05, hi=0.12),
    _ev("r4_up", "📈", "VIOLENT APPRECIATION",
        "Several high-risk investments move sharply upward.",
        "WE ARE BACK. Please ignore the previous twenty minutes.",
        risk=4, pool="core", weight=30, kind="scale", lo=0.06, hi=0.15),
    _ev("r4_down", "📉", "VIOLENT DEPRECIATION",
        "Several positions move sharply downward before Roger can explain why.",
        "This number is temporary. Very temporary. Hopefully.",
        risk=4, pool="core", weight=30, kind="scale", lo=-0.15, hi=-0.07),
    _ev("r4_expenses", "🧾", "EMERGENCY EXPENSES",
        "Roger begins calling lawyers, consultants and people who only work "
        "after midnight.",
        "You would be amazed what lawyers charge when you call them at night.",
        risk=4, pool="core", weight=17, kind="scale", lo=-0.10, hi=-0.04),

    # ── RISK 4 — SPECIAL ──────────────────────────────────────────────────
    _ev("r4_leverage", "🧨", "AGGRESSIVE LEVERAGE",
        "Roger discovers that borrowed money can also be invested. He "
        "immediately borrows a lot of it.",
        "Why invest one Naira when a bank will lend you three more?",
        risk=4, pool="special", weight=7, kind="scale", lo=0.12, hi=0.25,
        risk_chance=(0.35, +1), cooldown=30),
    _ev("r4_bank_run", "🏃", "EARLY BANK RUN",
        "Several investors or creditors attempt to withdraw money "
        "simultaneously. Roger insists there is no bank run.",
        "Nobody is running. Several investors are simply leaving very quickly.",
        risk=4, pool="special", weight=7, kind="scale", lo=-0.20, hi=-0.10,
        risk_chance=(0.25, +1), cooldown=60),
    _ev("r4_regulator", "🚨", "REGULATORY INVESTIGATION",
        "Authorities begin investigating suspicious financial activity "
        "connected to the fund.",
        "They only call it suspicious because they do not understand advanced "
        "finance.",
        risk=4, pool="special", weight=7, kind="scale", lo=-0.18, hi=-0.08,
        risk_chance=(0.30, +1), cooldown=60),
    _ev("r4_credit", "🏦", "EMERGENCY CREDIT LINE",
        "Roger secures a large emergency loan. The interest rate is not "
        "disclosed.",
        "Liquidity crisis solved. Repayment is a problem for future Roger.",
        risk=4, pool="special", weight=7, kind="scale", lo=0.10, hi=0.20,
        risk_chance=(0.20, +1), cooldown=30),
    _ev("r4_asset_sale", "💼", "SECRET ASSET SALE",
        "Roger quietly liquidates several risky holdings to strengthen the "
        "balance sheet.",
        "We did not sell because we needed money. We sold because we wanted "
        "money urgently.",
        risk=4, pool="special", weight=5, kind="scale", lo=0.05, hi=0.12,
        risk_chance=(0.25, -1), cooldown=60),
    _ev("r4_accountant", "🧑‍💼", "COMPETENT ACCOUNTANT",
        "A competent accountant takes control and immediately demands painful "
        "corrective action.",
        "He claims the fund is healthier. I personally feel significantly "
        "worse.",
        risk=4, pool="special", weight=2, kind="scale", lo=-0.10, hi=-0.05,
        risk_delta=-2, cooldown=360),
    _ev("r4_capital_call", "🚨", "EMERGENCY CAPITAL CALL",
        "Roger announces another entirely non-emergency request for immediate "
        "capital.",
        "I need ten thousand Naira. There is no emergency. Please hurry.",
        risk=4, pool="special", weight=7, kind="capital_call",
        amount=CAPITAL_CALL_TARGET, cooldown=120),
    _ev("r4_whale_rescue", "🐋", "WHALE RESCUE",
        "Roger pays a major investor a large personal dividend in the hope that "
        "they keep their money invested.",
        "Maintaining confidence is expensive when the confident person owns "
        "half the fund.",
        risk=4, pool="special", weight=8, kind="whale_rescue",
        lo=0.10, hi=0.10, cooldown=60),
    _ev("r4_winner", "🎯", "ROGER PICKS A WINNER",
        "Roger selects one investor for a highly successful emergency "
        "investment.",
        "I have strategically selected somebody to have a good day.",
        risk=4, pool="special", weight=6, kind="pick_winner",
        lo=0.15, hi=0.25, cooldown=30),
    _ev("r4_loser", "🪦", "ROGER PICKS A LOSER",
        "Roger isolates one particularly unfortunate position and writes it "
        "down heavily.",
        "Statistically, someone had to absorb the problem.",
        risk=4, pool="special", weight=6, kind="pick_loser",
        lo=0.10, hi=0.20, cooldown=60),
    _ev("r4_cash_reserve", "💰", "HIDDEN CASH RESERVE",
        "Roger discovers a large amount of forgotten cash stored somewhere it "
        "absolutely should not have been.",
        "This is why you never throw away an old suitcase.",
        risk=4, pool="special", weight=7, kind="scale", lo=0.10, hi=0.25,
        cooldown=30),
    _ev("r4_side_bet", "🔥", "MAJOR SIDE BET",
        "Roger risks a significant portion of the fund on one opportunity.",
        "Diversification is useful until you become extremely confident.",
        risk=4, pool="special", weight=8, kind="coin", p=0.50,
        lo=0.20, hi=0.30, lo2=-0.25, hi2=-0.15, cooldown=30),
    _ev("r4_whistleblower", "🕵️", "WHISTLEBLOWER",
        "A former employee leaks internal Royal Fund information.",
        "Transparency becomes dangerous when people can see things.",
        risk=4, pool="special", weight=4, kind="whistleblower",
        amount=30, risk_chance=(0.40, +1), cooldown=60),
    _ev("r4_tax", "🧾", "FEDERAL TAX AUDIT",
        "Tax investigators compare Royal Fund investors with mysteriously "
        "undeclared cash holdings.",
        "I specifically told everyone money inside my fund was more "
        "tax-efficient.",
        risk=4, pool="special", weight=4, kind="tax_audit",
        lo=0.30, hi=0.50, amount=2_000, cooldown=720),
    _ev("r4_akwabi", "✈️", "PRINCE AKWABI'S STRATEGIC WITHDRAWAL",
        "Prince Akwabi offers to move a large portion of the fund somewhere "
        "safer. His phone stops working shortly afterwards.",
        "Akwabi assures me the money is completely safe. He has also changed "
        "his phone number.",
        risk=4, pool="special", weight=2, kind="scale", lo=-0.55, hi=-0.45,
        risk_delta=-1, cooldown=360),
    _ev("r4_aap", "🦍", "EMERGENCY AAP INDUSTRIES DEAL",
        "Aap Industries agrees to a large short-notice transaction through "
        "Rotterdam.",
        "The containers are mostly legal. Mostly.",
        risk=4, pool="special", weight=8, kind="scale", lo=0.15, hi=0.30,
        cooldown=30, match="pharma"),
    _ev("r4_broker", "🏃", "CORRUPT BROKER DISAPPEARS",
        "One of Roger's trusted intermediaries stops responding shortly after "
        "receiving a substantial transfer.",
        "I am sure he is merely travelling somewhere without extradition.",
        risk=4, pool="special", weight=5, kind="scale", lo=-0.15, hi=-0.08,
        cooldown=60),

    # ══════════════════════════════════════════════════════════════════════
    # RISK 5 — CORE
    # ══════════════════════════════════════════════════════════════════════
    _ev("r5_dividend", "💸", "PANIC DIVIDEND",
        "Roger pays an enormous dividend to demonstrate that everything is "
        "completely under control.",
        "FOURTEEN PERCENT. DOES THAT LOOK LIKE PANIC TO YOU?",
        risk=5, pool="core", weight=25, kind="dividend", lo=0.08, hi=0.15),
    _ev("r5_up", "🚀", "WILD APPRECIATION",
        "The Royal Fund suddenly makes an absurd amount of money in a matter of "
        "minutes.",
        "WE ARE SO BACK. I HAVE ALWAYS BEEN CALM.",
        risk=5, pool="core", weight=30, kind="scale", lo=0.10, hi=0.25),
    _ev("r5_down", "📉", "WILD DEPRECIATION",
        "The fund loses a frightening amount of value almost immediately.",
        "DO NOT LOOK AT THE GRAPH. THE GRAPH IS BEING NEGATIVE.",
        risk=5, pool="core", weight=30, kind="scale", lo=-0.25, hi=-0.10),
    _ev("r5_costs", "🧾", "EMERGENCY COSTS",
        "Emergency legal and financial services begin charging emergency "
        "prices.",
        "Apparently emergency lawyers require emergency prices.",
        risk=5, pool="core", weight=15, kind="scale", lo=-0.15, hi=-0.08),

    # ── RISK 5 — SPECIAL ──────────────────────────────────────────────────
    _ev("r5_brilliant", "🎰", "ROGER'S LAST BRILLIANT IDEA",
        "Roger announces what he describes as the best investment idea he has "
        "ever had.",
        "THIS IDEA IS SO GOOD I DON'T EVEN UNDERSTAND IT.",
        risk=5, pool="special", weight=8, kind="coin", p=0.50,
        lo=0.25, hi=0.45, lo2=-0.40, hi2=-0.20, cooldown=30),
    _ev("r5_all_in", "🚀", "ALL-IN INVESTMENT",
        "Roger places an alarming share of remaining liquidity into one "
        "opportunity.",
        "DIVERSIFICATION IS COWARDICE.",
        risk=5, pool="special", weight=8, kind="coin", p=0.50,
        lo=0.20, hi=0.40, lo2=-0.30, hi2=-0.15, cooldown=30),
    _ev("r5_max_leverage", "🧨", "MAXIMUM LEVERAGE",
        "Roger borrows against assets that have already been used as collateral "
        "elsewhere.",
        "THE BANKS DO NOT NEED TO KNOW THE SAME ASSET SECURES FOUR LOANS.",
        risk=5, pool="special", weight=7, kind="scale", lo=0.20, hi=0.35,
        pressure=3, cooldown=60),
    _ev("r5_bank_run", "🏃", "FULL BANK RUN",
        "Withdrawals accelerate. Roger begins personally messaging investors "
        "asking them to remain calm.",
        "PLEASE STOP WITHDRAWING. THIS MESSAGE IS COMPLETELY UNRELATED TO "
        "LIQUIDITY.",
        risk=5, pool="special", weight=8, kind="scale", lo=-0.35, hi=-0.15,
        cooldown=60),
    _ev("r5_offshore_found", "📂", "OFFSHORE ACCOUNT FOUND",
        "An offshore account belonging to one of the fund's companies is "
        "rediscovered.",
        "I KNEW THE MONEY WAS SAFE. I JUST DIDN'T KNOW WHERE.",
        risk=5, pool="special", weight=6, kind="scale", lo=0.15, hi=0.30,
        cooldown=30),
    _ev("r5_offshore_frozen", "🚔", "OFFSHORE ACCOUNT FROZEN",
        "Regulators discover another offshore account before Roger can move the "
        "money.",
        "THE MONEY STILL EXISTS. THIS IS IMPORTANT. WE JUST CANNOT TOUCH IT.",
        risk=5, pool="special", weight=6, kind="scale", lo=-0.30, hi=-0.15,
        cooldown=30),
    _ev("r5_miracle", "🧮", "ACCOUNTING MIRACLE",
        "The accounting team discovers that several liabilities may have been "
        "entered twice. Nobody reopens the spreadsheet.",
        "THE SPREADSHEET IS GREEN. NOBODY TOUCH THE SPREADSHEET.",
        risk=5, pool="special", weight=6, kind="scale", lo=0.20, hi=0.40,
        cooldown=30),
    _ev("r5_disaster", "📉", "ACCOUNTING DISASTER",
        "An accountant discovers an extra zero in an extremely unfortunate "
        "column.",
        "THERE WERE A LOT OF ZEROS. ANYONE COULD HAVE DONE THIS.",
        risk=5, pool="special", weight=6, kind="scale", lo=-0.40, hi=-0.20,
        cooldown=30),
    _ev("r5_aap", "🦍", "EMERGENCY AAP INDUSTRIES DEAL",
        "Aap Industries agrees to an enormous short-notice transaction through "
        "Rotterdam.",
        "THE CONTAINERS ARE ALMOST ENTIRELY LEGAL.",
        risk=5, pool="special", weight=6, kind="scale", lo=0.15, hi=0.35,
        cooldown=30, match="pharma"),
    _ev("r5_rumours", "🗞️", "COLLAPSE RUMOURS",
        "Discord fills with messages asking whether the Royal Fund is about to "
        "collapse, causing exactly the behaviour everyone feared.",
        "THE FUND WOULD BE SAFE IF EVERYONE STOPPED ASKING WHETHER IT WAS SAFE.",
        risk=5, pool="special", weight=6, kind="scale", lo=-0.20, hi=-0.10,
        pressure=2, cooldown=60),
    _ev("r5_whale_rescue", "🐋", "WHALE RESCUE",
        "Roger makes a large personal payment to an important investor in the "
        "hope that they do not withdraw.",
        "PLEASE ENJOY THIS COMPLETELY NORMAL LARGE PAYMENT AND REMAIN WHERE YOU "
        "ARE.",
        risk=5, pool="special", weight=6, kind="whale_rescue",
        lo=0.10, hi=0.10, cooldown=60),
    _ev("r5_plea", "🙏", "ROGER'S PERSONAL PLEA",
        "Roger privately begs a major investor not to pull their money out and "
        "transfers them a large personal payment.",
        "PLEASE. JUST LEAVE IT IN THERE. I AM BEGGING YOU PROFESSIONALLY.",
        risk=5, pool="special", weight=5, kind="personal_plea", amount=5_000),
    _ev("r5_mgmt_fee", "💸", "EMERGENCY MANAGEMENT FEE",
        "Roger identifies an urgent need for additional executive liquidity and "
        "an investor who currently has money.",
        "I have not stolen your money. I have temporarily moved it somewhere "
        "more personally useful.",
        risk=5, pool="special", weight=4, kind="emergency_fee",
        lo=2_000, hi=3_000, min_position=4_000, cooldown=120),
    _ev("r5_whale_panic", "💔", "WHALE PANIC",
        "Roger calls one of the fund's biggest investors to tell them not to "
        "panic. This has the opposite effect.",
        "I ASKED OUR BIGGEST INVESTOR NOT TO PANIC AND APPARENTLY THIS CAUSED "
        "PANIC.",
        risk=5, pool="special", weight=6, kind="whale_panic",
        lo=0.10, hi=0.20, cooldown=60),
    _ev("r5_bailout", "🏛️", "EMERGENCY GOVERNMENT BAILOUT",
        "Nigeria declares the Royal Fund TOO LEGITIMATE TO FAIL and provides "
        "emergency support.",
        "THE TAXPAYERS HAVE VOLUNTARILY RESTORED CONFIDENCE. LEGALLY "
        "VOLUNTARILY.",
        risk=5, pool="special", weight=1, kind="scale", lo=0.05, hi=0.15,
        risk_delta=-2, cooldown=360),
    _ev("r5_fire_sale", "🔥", "FIRE SALE",
        "Roger sells anything for which he can still find a buyer.",
        "LIQUIDITY HAS IMPROVED BECAUSE WE NO LONGER OWN MOST OF THE ASSETS.",
        risk=5, pool="special", weight=2, kind="scale", lo=-0.20, hi=-0.10,
        risk_delta=-1, cooldown=360),
    _ev("r5_legal_rescue", "🧑‍⚖️", "LAST-MINUTE LEGAL RESCUE",
        "Lawyers restructure enough obligations to keep the fund alive.",
        "THE LAWYERS SAY WE SURVIVE. I HAVE NEVER LOVED EXPENSIVE PEOPLE MORE.",
        risk=5, pool="special", weight=1, kind="scale", lo=-0.20, hi=-0.10,
        risk_delta=-2, cooldown=360),
    _ev("r5_coin_flip", "🎲", "ROGER'S EMERGENCY COIN FLIP",
        "Roger concludes that traditional analysis is taking too long.",
        "AT THIS POINT A COIN HAS SIMILAR QUALIFICATIONS TO THE BOARD.",
        risk=5, pool="special", weight=8, kind="coin_dividend",
        p=0.50, lo=0.15, lo2=-0.10, cooldown=30),
]

BY_ID = {e["id"]: e for e in EVENTS}
if len(BY_ID) != len(EVENTS):
    seen: set[str] = set()
    dupes = [e["id"] for e in EVENTS if e["id"] in seen or seen.add(e["id"])]
    raise ValueError(f"duplicate fund event ids: {dupes}")

# §10.  Quick-scam template id → the fund events that answer it, one per risk
# level.  The Aap operation deliberately has two answers: the Risk-3
# partnership and the louder Risk 4–5 emergency deal.
MATCH_EVENTS: dict[str, list[str]] = {}
for _e in EVENTS:
    if _e["match"]:
        MATCH_EVENTS.setdefault(_e["match"], []).append(_e["id"])


def pool(risk: int, kind: str) -> list[dict]:
    return [e for e in EVENTS if e["risk"] == risk and e["pool"] == kind]


# ── Schema ────────────────────────────────────────────────────────────────────

async def setup_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_state (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            risk              INTEGER NOT NULL DEFAULT 1,
            collapse_pressure INTEGER NOT NULL DEFAULT 0,
            lifecycle         INTEGER NOT NULL DEFAULT 1,
            next_event_at     TEXT,
            last_event_at     TEXT,
            last_event_id     TEXT,
            last_hourly_at    TEXT,
            last_window_at    TEXT,
            safe_hours        INTEGER NOT NULL DEFAULT 0,
            panic_until       TEXT,
            panic_bonus       REAL NOT NULL DEFAULT 0,
            leak_until        TEXT,
            campaign_until    TEXT,
            campaign_pct      REAL NOT NULL DEFAULT 0,
            campaign_cap      INTEGER NOT NULL DEFAULT 0,
            call_until        TEXT,
            call_target       INTEGER NOT NULL DEFAULT 0,
            call_raised       INTEGER NOT NULL DEFAULT 0,
            pending_match     TEXT,
            match_expires_at  TEXT,
            last_report_at    TEXT,
            collapses         INTEGER NOT NULL DEFAULT 0,
            events_run        INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Older installs predate some columns; add them in place.
    for column in (
        "match_expires_at TEXT", "last_report_at TEXT",
    ):
        try:
            await conn.execute(f"ALTER TABLE fund_state ADD COLUMN {column}")
        except Exception:
            pass  # column already present
    await conn.execute(
        "INSERT OR IGNORE INTO fund_state (id, risk) VALUES (1, 1)"
    )
    # Every deposit and withdrawal, so the 15-minute window maths in §9.4/§9.5
    # can be recomputed exactly rather than accumulated into counters that a
    # restart would lose.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_flows (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            amount          INTEGER NOT NULL,   -- +deposit / −withdrawal
            kind            TEXT NOT NULL,      -- normal | campaign | call
            at              TEXT NOT NULL
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fund_flows_at ON fund_flows(at)"
    )
    # Per-player attribution: who was in which fund, what they put in, and
    # every single thing that happened to it afterwards.  Unlike ``fund_flows``
    # this survives a collapse — a wiped fund is precisely the history a player
    # most wants to be able to look back at.  Tagged with the lifecycle so
    # "this fund" and "every fund" are the same query with one clause changed.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_ledger (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            lifecycle       INTEGER NOT NULL,
            discord_user_id TEXT NOT NULL,
            kind            TEXT NOT NULL,   -- deposit|withdraw|event|cash
            delta           INTEGER NOT NULL,
            label           TEXT NOT NULL,
            at              TEXT NOT NULL
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fund_ledger_user"
        " ON fund_ledger(discord_user_id, id DESC)"
    )
    # Tracking starts today, but positions do not: everyone already in the
    # fund gets an opening balance so their position adds up and `/fundluck`
    # does not greet a 20.000-Naira investor with "you have never invested".
    # Guarded on the table being empty, so it can only ever happen once.
    async with conn.execute("SELECT EXISTS (SELECT 1 FROM fund_ledger)") as cur:
        seeded = bool((await cur.fetchone())[0])
    try:
        if not seeded:
            async with conn.execute(
                "SELECT lifecycle FROM fund_state WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            life = int(row[0]) if row else 1
            async with conn.execute(
                "SELECT discord_user_id, invested FROM scam_players"
                " WHERE invested > 0"
            ) as cur:
                carried = [(str(r[0]), int(r[1])) async for r in cur]
            for user_id, held in carried:
                await conn.execute(
                    "INSERT INTO fund_ledger"
                    " (lifecycle, discord_user_id, kind, delta, label, at)"
                    " VALUES (?, ?, 'deposit', ?, ?, ?)",
                    (life, user_id, held,
                     "Position carried over — tracking started here",
                     _iso(_now())),
                )
            if carried:
                logger.info("royal_fund: opened the ledger with %d position(s)",
                            len(carried))
    except aiosqlite.Error:
        # Never let bookkeeping stop the bot from booting.
        logger.exception("royal_fund: could not seed the fund ledger")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_cooldowns (
            event_id TEXT PRIMARY KEY,
            until    TEXT NOT NULL
        )
    """)
    # Roger only begs each investor once per lifecycle.
    # §17: the last few events, so the status card can show what just happened.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_recent (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            emoji    TEXT NOT NULL,
            name     TEXT NOT NULL,
            summary  TEXT NOT NULL,
            at       TEXT NOT NULL
        )
    """)
    # §13: a Deposit Campaign only matches money above the position a player
    # held when the campaign opened, so withdraw-then-redeposit earns nothing.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_campaign_baseline (
            discord_user_id TEXT PRIMARY KEY,
            position        INTEGER NOT NULL,
            matched         INTEGER NOT NULL DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_pleas (
            lifecycle       INTEGER NOT NULL,
            discord_user_id TEXT NOT NULL,
            PRIMARY KEY (lifecycle, discord_user_id)
        )
    """)
    await conn.commit()


# ── State helpers ─────────────────────────────────────────────────────────────

_STATE_COLUMNS = (
    "risk", "collapse_pressure", "lifecycle", "next_event_at", "last_event_at",
    "last_event_id", "last_hourly_at", "last_window_at", "safe_hours",
    "panic_until", "panic_bonus", "leak_until", "campaign_until",
    "campaign_pct", "campaign_cap", "call_until", "call_target", "call_raised",
    "pending_match", "match_expires_at", "last_report_at", "collapses",
    "events_run",
)


async def get_state(conn: aiosqlite.Connection) -> dict:
    async with conn.execute(
        f"SELECT {', '.join(_STATE_COLUMNS)} FROM fund_state WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return {"risk": 1, "collapse_pressure": 0, "lifecycle": 1}
    return dict(zip(_STATE_COLUMNS, row))


async def set_state(conn: aiosqlite.Connection, **kw) -> None:
    if not kw:
        return
    sets = ", ".join(f"{k} = ?" for k in kw)
    await conn.execute(
        f"UPDATE fund_state SET {sets} WHERE id = 1", tuple(kw.values())
    )
    await conn.commit()


async def positions(conn: aiosqlite.Connection) -> list[tuple[str, int]]:
    """Every non-zero position, largest first.  Their sum *is* the fund."""
    rows: list[tuple[str, int]] = []
    async with conn.execute(
        "SELECT discord_user_id, invested FROM scam_players"
        " WHERE invested > 0 ORDER BY invested DESC"
    ) as cur:
        async for r in cur:
            rows.append((str(r[0]), int(r[1])))
    return rows


async def fund_total(conn: aiosqlite.Connection) -> int:
    async with conn.execute(
        "SELECT COALESCE(SUM(invested), 0) FROM scam_players WHERE invested > 0"
    ) as cur:
        row = await cur.fetchone()
    return int(row[0])


async def _lifecycle(conn: aiosqlite.Connection) -> int:
    async with conn.execute(
        "SELECT lifecycle FROM fund_state WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 1


async def record_pnl(
    conn: aiosqlite.Connection, user_id: str, delta: int, label: str, *,
    kind: str = "event", lifecycle: Optional[int] = None,
) -> None:
    """Attribute one movement of one player's fund money to a cause.

    ``kind`` separates *principal* from *performance*, which is the whole point
    of the table: ``deposit``/``withdraw`` are money the player chose to move,
    ``event``/``cash`` are things that happened to them.  Only the latter two
    are profit or loss — otherwise depositing 10.000 would read as a 10.000
    gain.
    """
    if not delta:
        return
    await conn.execute(
        "INSERT INTO fund_ledger"
        " (lifecycle, discord_user_id, kind, delta, label, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            int(lifecycle if lifecycle is not None else await _lifecycle(conn)),
            str(user_id), kind, int(delta), label, _iso(_now()),
        ),
    )


async def _position_of(conn: aiosqlite.Connection, user_id: str) -> int:
    async with conn.execute(
        "SELECT invested FROM scam_players WHERE discord_user_id = ?",
        (str(user_id),),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _set_position(
    conn: aiosqlite.Connection, user_id: str, value: int, *,
    label: str = "Unattributed fund movement", kind: str = "event",
    was: Optional[int] = None, lifecycle: Optional[int] = None,
) -> None:
    before = await _position_of(conn, user_id) if was is None else int(was)
    after = max(0, int(value))
    await conn.execute(
        "UPDATE scam_players SET invested = ? WHERE discord_user_id = ?",
        (after, str(user_id)),
    )
    await record_pnl(conn, user_id, after - before, label,
                     kind=kind, lifecycle=lifecycle)


async def _add_position(
    conn: aiosqlite.Connection, user_id: str, delta: int, *,
    label: str = "Unattributed fund movement", kind: str = "event",
    lifecycle: Optional[int] = None,
) -> None:
    """Move one position and record *why* it moved.

    The ledger row is written from the before/after positions rather than from
    ``delta``, because the floor at zero means a request to take 5.000 off a
    3.000 position only ever removes 3.000 — and a ledger that recorded the
    request instead of the movement would slowly drift away from the position
    it claims to explain.

    ``label`` has a deliberately conspicuous default: every position in the
    game moves through this function, so a caller that forgets to say what it
    is doing shows up in `/fundluck` as an unexplained line rather than
    silently vanishing from a player's profit and loss.
    """
    before = await _position_of(conn, user_id)
    after = max(0, before + int(delta))
    await conn.execute(
        "UPDATE scam_players SET invested = ? WHERE discord_user_id = ?",
        (after, str(user_id)),
    )
    await record_pnl(conn, user_id, after - before, label,
                     kind=kind, lifecycle=lifecycle)


async def scale_fund(
    conn: aiosqlite.Connection, factor: float, *,
    label: str = "Fund revaluation",
) -> int:
    """Multiply every position by *factor*.  Returns the net change in Naira.

    Positions are the only stored quantity, so scaling them *is* revaluing the
    fund — there is no separate total that could drift out of step.
    """
    before = await fund_total(conn)
    life = await _lifecycle(conn)
    for user_id, amount in await positions(conn):
        await _set_position(conn, user_id, int(round(amount * factor)),
                            label=label, was=amount, lifecycle=life)
    after = await fund_total(conn)
    return after - before


async def on_cooldown(conn: aiosqlite.Connection, event_id: str) -> bool:
    async with conn.execute(
        "SELECT until FROM fund_cooldowns WHERE event_id = ?", (event_id,)
    ) as cur:
        row = await cur.fetchone()
    return bool(row and _parse(row[0]) > _now())


async def start_cooldown(
    conn: aiosqlite.Connection, event_id: str, minutes: int
) -> None:
    if minutes <= 0:
        return
    await conn.execute(
        "INSERT INTO fund_cooldowns (event_id, until) VALUES (?, ?)"
        " ON CONFLICT(event_id) DO UPDATE SET until = excluded.until",
        (event_id, _iso(_now() + timedelta(minutes=minutes))),
    )


async def record_flow(
    conn: aiosqlite.Connection, user_id: str, amount: int, kind: str = "normal"
) -> None:
    await conn.execute(
        "INSERT INTO fund_flows (discord_user_id, amount, kind, at)"
        " VALUES (?, ?, ?, ?)",
        (str(user_id), int(amount), kind, _iso(_now())),
    )


# ── Per-player fund history (/fundluck) ───────────────────────────────────────

def _delta(amount: int) -> str:
    """A signed amount, using a real minus sign so columns line up."""
    return ("+" if amount >= 0 else "−") + money(abs(amount))


def _ratio(pnl: int, deposited: int) -> str:
    """Profit as a share of what was handed over, or nothing if that is zero."""
    if deposited <= 0:
        return ""
    pct = f"{abs(pnl) / deposited * 100:.1f}"
    return f" ({'+' if pnl >= 0 else '−'}{pct}% of what you put in)"


async def fund_luck(conn: aiosqlite.Connection, user_id: str) -> dict:
    """Everything `/fundluck` needs about one player, in two passes.

    The whole report is derived from ``fund_ledger`` rather than from counters
    kept alongside it: a counter can disagree with its own history after a
    crash halfway through an event, and the one number a player will check
    against reality is the position sitting in `/balance`.
    """
    uid = str(user_id)
    life = await _lifecycle(conn)

    rows: list[tuple[int, str, int, str, str]] = []
    async with conn.execute(
        "SELECT lifecycle, kind, delta, label, at FROM fund_ledger"
        " WHERE discord_user_id = ? ORDER BY id",
        (uid,),
    ) as cur:
        async for r in cur:
            rows.append((int(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4])))

    def totals(subset: list) -> dict:
        # "position" and "cash" are kept apart because they answer different
        # questions: one is what the fund did to the money still inside it,
        # the other is dividends, Roger's gifts and confiscations, which never
        # show up in a position at all.
        return {
            "deposited": sum(d for _l, k, d, _n, _a in subset if k == "deposit"),
            "withdrawn": -sum(d for _l, k, d, _n, _a in subset if k == "withdraw"),
            "position_pnl": sum(d for _l, k, d, _n, _a in subset if k == "event"),
            "cash_pnl": sum(d for _l, k, d, _n, _a in subset if k == "cash"),
            "pnl": sum(d for _l, k, d, _n, _a in subset if k in ("event", "cash")),
        }

    current = [r for r in rows if r[0] == life]
    causes: dict[str, list[int]] = {}
    for _l, kind, delta, label, _at in current:
        if kind in ("event", "cash"):
            causes.setdefault(label, []).append(delta)

    moves = [r for r in rows if r[1] in ("event", "cash")]
    best = max(moves, key=lambda r: r[2], default=None)
    worst = min(moves, key=lambda r: r[2], default=None)

    return {
        "lifecycle": life,
        "position": await _position_of(conn, uid),
        "current": totals(current),
        "lifetime": totals(rows),
        "funds": len({r[0] for r in rows if r[1] == "deposit"}),
        # Grouped, because a single fund can run hundreds of events and a
        # scrolling list of −40 Naira revaluations answers nothing.  Biggest
        # absolute effect first: that is the question actually being asked.
        "causes": sorted(
            ((label, sum(v), len(v)) for label, v in causes.items()),
            key=lambda c: -abs(c[1]),
        ),
        "best": (best[3], best[2], best[4]) if best and best[2] > 0 else None,
        "worst": (worst[3], worst[2], worst[4]) if worst and worst[2] < 0 else None,
        "any": bool(rows),
    }


def _verdict(pnl: int, deposited: int) -> str:
    """One line of Roger, calibrated to how badly it has gone."""
    if deposited <= 0:
        return "_You have never given Roger anything. This is the single best investment decision in this server._"
    ratio = pnl / deposited
    if ratio >= 0.50:
        return "> **Roger:** \"I told you. I *told* you. Nobody ever believes me until it works.\""
    if ratio >= 0.10:
        return "> **Roger:** \"A solid, respectable return. Please do not withdraw it.\""
    if ratio > -0.05:
        return "> **Roger:** \"Broadly flat. In this economy that is practically heroic.\""
    if ratio > -0.30:
        return "> **Roger:** \"Every portfolio has a difficult period. Yours is ongoing.\""
    if ratio > -0.75:
        return "> **Roger:** \"I would describe this as a learning experience for both of us.\""
    return "> **Roger:** \"Let us never speak of this again.\""


# ── Eligibility & selection ───────────────────────────────────────────────────

def _weighted_choice(items: list[tuple[str, int]], invert: bool = False) -> str:
    """Pick a user id weighted by position size (or inversely)."""
    if not items:
        raise ValueError("no candidates")
    if invert:
        biggest = max(a for _u, a in items)
        weights = [max(1.0, biggest - a + 1) for _u, a in items]
    else:
        weights = [float(max(1, a)) for _u, a in items]
    return random.choices([u for u, _a in items], weights=weights, k=1)[0]


async def _eligible(
    conn: aiosqlite.Connection, event: dict, state: dict
) -> bool:
    """Whether *event* can run right now.

    §6: an ineligible or cooling-down Special never renormalises the pool — the
    caller falls back to a Core event instead, so a heavily-gated pool cannot
    quietly concentrate probability on its unrestricted members.
    """
    if await on_cooldown(conn, event["id"]):
        return False
    holders = await positions(conn)
    if len(holders) < event["min_investors"]:
        return False
    kind = event["kind"]

    if kind in ("flat_loss_one", "flat_loss_top3", "emergency_fee"):
        return any(a >= event["min_position"] for _u, a in holders)
    if kind in ("transfer_down", "lottery"):
        return (
            len(holders) >= 2
            and any(a >= event["min_position"] for _u, a in holders)
        )
    if kind == "transfer_random":
        return len(holders) >= 2
    if kind == "anchor":
        total = sum(a for _u, a in holders)
        return bool(total and holders[0][1] / total >= 0.40)
    if kind == "cash_award":
        return await _recent_serious_investor(conn) is not None
    if kind == "tax_audit":
        return await _tax_candidates(conn, event["amount"]) != []
    if kind == "personal_plea":
        return await _plea_candidate(conn, state) is not None
    # §11: a timed effect selected while already running falls back to Core
    # rather than silently restarting its own clock.
    if kind == "capital_call":
        return not _active(state.get("call_until")) and not _active(
            state.get("campaign_until")
        )
    if kind == "campaign":
        return not _active(state.get("campaign_until")) and not _active(
            state.get("call_until")
        )
    if kind == "panic":
        return not _active(state.get("panic_until"))
    if kind == "whistleblower":
        return not _active(state.get("leak_until"))
    return True


def _active(ts: Optional[str]) -> bool:
    return bool(ts) and _parse(ts) > _now()


async def _recent_serious_investor(conn: aiosqlite.Connection) -> Optional[str]:
    """Most recent player whose net deposit was at least 500 Naira."""
    async with conn.execute(
        "SELECT discord_user_id FROM fund_flows"
        " WHERE amount >= 500 ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    uid = str(row[0])
    async with conn.execute(
        "SELECT invested FROM scam_players WHERE discord_user_id = ?", (uid,)
    ) as cur:
        pos = await cur.fetchone()
    return uid if pos and int(pos[0]) > 0 else None


async def _tax_candidates(
    conn: aiosqlite.Connection, floor: int
) -> list[tuple[str, int]]:
    """Fund investors holding more than the protected cash floor."""
    rows: list[tuple[str, int]] = []
    async with conn.execute(
        "SELECT discord_user_id, balance FROM scam_players"
        " WHERE invested > 0 AND balance > ?",
        (floor,),
    ) as cur:
        async for r in cur:
            rows.append((str(r[0]), int(r[1])))
    return rows


async def _plea_candidate(
    conn: aiosqlite.Connection, state: dict
) -> Optional[str]:
    """Largest investor who has not been begged this lifecycle."""
    async with conn.execute(
        "SELECT discord_user_id FROM fund_pleas WHERE lifecycle = ?",
        (state.get("lifecycle", 1),),
    ) as cur:
        done = {str(r[0]) async for r in cur}
    for uid, _amount in await positions(conn):
        if uid not in done:
            return uid
    return None


# ── The engine ────────────────────────────────────────────────────────────────

class FundResult:
    """What an event did, in a form the announcement can render."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.delta: int = 0             # net change in fund value
        self.cash: int = 0              # cash created for players
        self.risk_delta: int = 0
        self.pressure: int = 0
        self.colour: Optional[discord.Colour] = None
        # Compact form for the status card's "recent events" list (§17).
        self.headline: str = ""

    def line(self, text: str) -> None:
        self.lines.append(text)


async def apply_event(
    conn: aiosqlite.Connection, event: dict, state: dict,
    guild: Optional[discord.Guild] = None,
) -> FundResult:
    """Run one event's money and ownership effects.  Called under the lock.

    Every branch touches positions only (or moves money between a position and
    a cash balance), which is what keeps ``SUM(positions) == fund value`` true
    by construction rather than by reconciliation.
    """
    res = FundResult()
    kind = event["kind"]
    holders = await positions(conn)
    total = sum(a for _u, a in holders)
    # What `/fundluck` will call this later, in the player's own history.
    tag = f"{event['emoji']} {event['name']}"

    def name_of(uid: str) -> str:
        member = guild.get_member(int(uid)) if guild else None
        return member.display_name if member else f"Prince {uid[-4:]}"

    # ── proportional revaluation ──────────────────────────────────────
    if kind in ("scale", "coin"):
        if kind == "coin":
            good = random.random() < event["p"]
            pct = _pct(event["lo"], event["hi"]) if good else _pct(
                event["lo2"], event["hi2"]
            )
        else:
            pct = _pct(event["lo"], event["hi"])
        res.delta = await scale_fund(conn, 1 + pct, label=tag)
        res.line(
            f"Fund value **{_signed(pct)}** → {money(total + res.delta)}"
        )
        res.headline = _signed(pct)
        res.colour = _EMBED_GREEN if pct >= 0 else _EMBED_RED

    # ── cash dividends: fund principal untouched ──────────────────────
    elif kind == "dividend":
        pct = _pct(event["lo"], event["hi"])
        paid = 0
        for uid, amount in holders:
            cut = int(round(amount * pct))
            if cut > 0:
                await adjust_balance(conn, uid, cut, "fund_dividend", event["name"])
                await record_pnl(conn, uid, cut, tag, kind="cash")
                paid += cut
        res.cash = paid
        res.line(
            f"**{pct * 100:.1f}%** paid out as **cash** to "
            f"{len(holders)} investor(s) — {money(paid)} total."
        )
        res.line("_Your invested principal is untouched._")
        res.headline = f"{pct * 100:.1f}% cash dividend"
        res.colour = _EMBED_GREEN

    elif kind == "coin_dividend":
        if random.random() < event["p"]:
            paid = 0
            for uid, amount in holders:
                cut = int(round(amount * event["lo"]))
                if cut > 0:
                    await adjust_balance(conn, uid, cut, "fund_dividend", event["name"])
                    await record_pnl(conn, uid, cut, tag, kind="cash")
                    paid += cut
            res.cash = paid
            res.line(
                f"🪙 Heads. **{event['lo'] * 100:.0f}%** paid out as cash — "
                f"{money(paid)}."
            )
            res.colour = _EMBED_GREEN
        else:
            res.delta = await scale_fund(conn, 1 + event["lo2"], label=tag)
            res.line(
                f"🪙 Tails. Fund value **{_signed(event['lo2'])}** → "
                f"{money(total + res.delta)}"
            )
            res.colour = _EMBED_RED

    # ── flat losses from a position ───────────────────────────────────
    elif kind == "flat_loss_one":
        pick = [(u, a) for u, a in holders if a >= event["min_position"]]
        uid = random.choice(pick)[0]
        pos = dict(holders)[uid]
        take = min(pos, random.randint(int(event["lo"]), int(event["hi"])))
        await _add_position(conn, uid, -take, label=tag)
        await record_ledger(conn, uid, -take, "fund_position", event["name"])
        res.delta = -take
        res.line(f"**{name_of(uid)}** is down **{money(take)}** from their position.")
        res.colour = _EMBED_RED

    elif kind == "flat_loss_top3":
        picks = [(u, a) for u, a in holders if a >= event["min_position"]][:3]
        removed = 0
        for uid, pos in picks:
            take = min(pos, random.randint(int(event["lo"]), int(event["hi"])))
            await _add_position(conn, uid, -take, label=tag)
            await record_ledger(conn, uid, -take, "fund_position", event["name"])
            removed += take
            res.line(f"**{name_of(uid)}** charged **{money(take)}**.")
        res.delta = -removed
        res.colour = _EMBED_RED

    elif kind == "emergency_fee":
        pick = [(u, a) for u, a in holders if a >= event["min_position"]][:3]
        uid = _weighted_choice(pick)
        pos = dict(holders)[uid]
        want = random.randint(int(event["lo"]), int(event["hi"]))
        # Never strip a target below 1.000 — the fee is a mugging, not an exit.
        take = max(0, min(want, pos - 1_000))
        if take <= 0:
            res.line("Roger reconsiders. Briefly.")
        else:
            await _add_position(conn, uid, -take, label=tag)
            await record_ledger(conn, uid, -take, "fund_position", event["name"])
            res.delta = -take
            res.line(
                f"**{name_of(uid)}** has had **{money(take)}** relocated "
                "somewhere more personally useful to Roger."
            )
        res.colour = _EMBED_RED

    # ── ownership transfers: fund total unchanged ─────────────────────
    elif kind == "transfer_down":
        big_id, big_amount = holders[0]
        small = [
            (u, a) for u, a in holders
            if u != big_id and a >= event["min_position"]
        ]
        if not small:
            res.line("Nobody qualifies. Roger keeps the concentration.")
        else:
            recipient = min(small, key=lambda x: x[1])[0]
            want = random.randint(int(event["lo"]), int(event["hi"]))
            move = min(want, int(big_amount * 0.10))
            if move <= 0:
                res.line("The largest investor is too small to redistribute.")
            else:
                await _add_position(conn, big_id, -move, label=tag)
                await _add_position(conn, recipient, move, label=tag)
                await record_ledger(conn, big_id, -move, "fund_position", event["name"])
                await record_ledger(conn, recipient, move, "fund_position", event["name"])
                res.line(
                    f"**{money(move)}** of ownership moved from "
                    f"**{name_of(big_id)}** to **{name_of(recipient)}**."
                )
                res.line("_Fund value unchanged. Only the owner changed._")
        res.colour = _EMBED_GOLD

    elif kind == "transfer_random":
        a_id, b_id = random.sample([u for u, _a in holders], 2)
        pos = dict(holders)[a_id]
        move = min(pos, random.randint(int(event["lo"]), int(event["hi"])))
        await _add_position(conn, a_id, -move, label=tag)
        await _add_position(conn, b_id, move, label=tag)
        await record_ledger(conn, a_id, -move, "fund_position", event["name"])
        await record_ledger(conn, b_id, move, "fund_position", event["name"])
        res.line(
            f"**{money(move)}** has quietly become **{name_of(b_id)}**'s "
            f"instead of **{name_of(a_id)}**'s."
        )
        res.line("_Fund value unchanged._")
        res.colour = _EMBED_GOLD

    # ── position gains that grow the fund ─────────────────────────────
    elif kind == "lottery":
        pick = [(u, a) for u, a in holders if a >= event["min_position"]]
        uid = _weighted_choice(pick, invert=True)
        gain = random.randint(int(event["lo"]), int(event["hi"]))
        await _add_position(conn, uid, gain, label=tag)
        await record_ledger(conn, uid, gain, "fund_position", event["name"])
        res.delta = gain
        res.line(f"🎉 **{name_of(uid)}** wins **{money(gain)}** into their position.")
        res.colour = _EMBED_GOLD

    elif kind == "pick_winner":
        uid = _weighted_choice(holders)
        pos = dict(holders)[uid]
        pct = _pct(event["lo"], event["hi"])
        gain = int(round(pos * pct))
        await _add_position(conn, uid, gain, label=tag)
        await record_ledger(conn, uid, gain, "fund_position", event["name"])
        res.delta = gain
        res.line(
            f"🎯 **{name_of(uid)}** alone gains **{pct * 100:.0f}%** "
            f"(+{money(gain)})."
        )
        res.colour = _EMBED_GOLD

    elif kind in ("pick_loser", "margin_call", "whale_panic"):
        if kind == "pick_loser":
            uid = random.choice([u for u, _a in holders])
        else:
            candidates = holders[:3] if kind == "whale_panic" else holders
            uid = _weighted_choice(candidates)
        pos = dict(holders)[uid]
        pct = _pct(event["lo"], event["hi"])
        loss = min(pos, int(round(pos * pct)))
        await _add_position(conn, uid, -loss, label=tag)
        await record_ledger(conn, uid, -loss, "fund_position", event["name"])
        res.delta = -loss
        res.line(
            f"**{name_of(uid)}** alone loses **{pct * 100:.0f}%** "
            f"(−{money(loss)})."
        )
        res.colour = _EMBED_RED

    # ── whale rescue: cash out of fund assets, everyone else diluted ──
    elif kind == "whale_rescue":
        uid = _weighted_choice(holders[:3])
        pos = dict(holders)[uid]
        payout = int(round(pos * _pct(event["lo"], event["hi"])))
        if payout <= 0 or total <= 0:
            res.line("There is nothing left to reassure anybody with.")
        else:
            await adjust_balance(conn, uid, payout, "fund_dividend", event["name"])
            await record_pnl(conn, uid, payout, tag, kind="cash")
            # The cash comes out of fund assets, so every remaining position
            # shrinks proportionally — including the whale's.
            await scale_fund(conn, max(0.0, (total - payout) / total), label=tag)
            res.cash = payout
            res.delta = await fund_total(conn) - total
            res.line(
                f"🐋 **{name_of(uid)}** receives **{money(payout)}** in cash "
                "to keep them calm."
            )
            res.line(
                "_Paid out of fund assets — every position, including theirs, "
                "shrinks to cover it._"
            )
        res.colour = _EMBED_GOLD

    # ── external cash: Roger's own (allegedly) money ──────────────────
    elif kind == "cash_award":
        uid = await _recent_serious_investor(conn)
        await adjust_balance(conn, uid, event["amount"], "fund_gift", event["name"])
        await record_pnl(conn, uid, event["amount"], tag, kind="cash")
        res.cash = event["amount"]
        res.line(
            f"🏆 **{name_of(uid)}** receives **{money(event['amount'])}** in "
            "cash from Roger. The fund is untouched."
        )
        res.colour = _EMBED_GOLD

    elif kind == "personal_plea":
        uid = await _plea_candidate(conn, state)
        await adjust_balance(conn, uid, event["amount"], "fund_gift", event["name"])
        await record_pnl(conn, uid, event["amount"], tag, kind="cash")
        await conn.execute(
            "INSERT OR IGNORE INTO fund_pleas (lifecycle, discord_user_id)"
            " VALUES (?, ?)",
            (state.get("lifecycle", 1), uid),
        )
        res.cash = event["amount"]
        res.line(
            f"🙏 **{name_of(uid)}** receives **{money(event['amount'])}** of "
            "Roger's own money. Please stay."
        )
        res.line("_Fund value unchanged. Roger is now poorer and no calmer._")
        res.colour = _EMBED_GOLD

    # ── cash confiscation: positions untouched ────────────────────────
    elif kind == "tax_audit":
        candidates = await _tax_candidates(conn, event["amount"])
        chosen = random.sample(candidates, min(len(candidates), random.randint(1, 3)))
        taken_total = 0
        for uid, balance in chosen:
            exposed = balance - event["amount"]
            take = int(round(exposed * _pct(event["lo"], event["hi"])))
            if take <= 0:
                continue
            await adjust_balance(conn, uid, -take, "fund_tax_audit", event["name"])
            await record_pnl(conn, uid, -take, tag, kind="cash")
            taken_total += take
            res.line(f"**{name_of(uid)}** loses **{money(take)}** in cash.")
        res.cash = -taken_total
        res.line(
            f"_Only **cash** above {money(event['amount'])} was taken. "
            "Invested positions were not touched — which Roger will now claim "
            "he predicted._"
        )
        res.colour = _EMBED_RED

    # ── timed windows ─────────────────────────────────────────────────
    elif kind == "campaign":
        until = _now() + timedelta(minutes=CAMPAIGN_MINUTES)
        # §13: snapshot everyone's position now, so only genuinely new money
        # gets matched and withdraw→redeposit earns nothing.
        await conn.execute("DELETE FROM fund_campaign_baseline")
        for uid, pos in holders:
            await conn.execute(
                "INSERT INTO fund_campaign_baseline (discord_user_id, position)"
                " VALUES (?, ?)", (uid, pos),
            )
        await set_state(
            conn,
            campaign_until=_iso(until),
            campaign_pct=event["lo"],
            campaign_cap=event["amount"],
        )
        res.line(
            f"💳 For **{CAMPAIGN_MINUTES} minutes**, every new deposit is "
            f"matched **{event['lo'] * 100:.0f}%** — up to "
            f"{money(event['amount'])} of free money per player."
        )
        res.line(f"Closes <t:{int(until.timestamp())}:R>. `/invest deposit`")
        res.colour = _EMBED_GOLD

    elif kind == "panic":
        until = _now() + timedelta(minutes=int(event["amount"]))
        await set_state(conn, panic_until=_iso(until), panic_bonus=event["lo"])
        res.line(
            f"📞 For **{int(event['amount'])} minutes**, withdrawals frighten "
            f"Roger **{event['lo'] * 100:.0f} points** more than usual."
        )
        res.colour = _EMBED_RED

    elif kind == "whistleblower":
        until = _now() + timedelta(minutes=int(event["amount"]))
        await set_state(conn, leak_until=_iso(until))
        res.line(
            f"🕵️ For **{int(event['amount'])} minutes**, `/invest status` shows "
            "the fund's internal collapse pressure and next event time."
        )
        res.colour = _EMBED_GOLD

    elif kind == "capital_call":
        until = _now() + timedelta(minutes=CAPITAL_CALL_MINUTES)
        await set_state(
            conn,
            call_until=_iso(until), call_target=event["amount"], call_raised=0,
        )
        res.line(
            f"🚨 **{money(event['amount'])}** of new deposits is required "
            f"within **{CAPITAL_CALL_MINUTES} minutes**."
        )
        res.line(
            f"Deadline <t:{int(until.timestamp())}:R>. "
            "**Reach it and Roger calms down by one level. Miss it and he "
            "does the opposite.**"
        )
        res.line("_Normal fund events are paused until this resolves._")
        res.colour = _EMBED_RED

    elif kind == "anchor":
        res.line("Roger gestures at the fund's largest investor, reassured.")
        res.colour = _EMBED_GREEN

    elif kind == "nothing":
        res.colour = _EMBED_GREY

    else:
        raise ValueError(f"unknown fund event kind {kind!r}")

    await conn.commit()
    return res


# ── Risk movement ─────────────────────────────────────────────────────────────

async def move_risk(
    conn: aiosqlite.Connection, delta: int, *, reason: str = ""
) -> tuple[int, int, int]:
    """Apply a risk change.  Returns ``(old, new, pressure_added)``.

    §3 step 12: at Risk 5 there is nowhere further up, so anything trying to
    raise risk becomes Collapse Pressure instead — which is how a long, ugly
    Risk-5 episode gets progressively more likely to end in zero.
    """
    state = await get_state(conn)
    old = int(state["risk"])
    if delta > 0 and old >= 5:
        added = min(delta, COLLAPSE_PRESSURE_MAX - int(state["collapse_pressure"]))
        added = max(0, added)
        if added:
            await set_state(
                conn, collapse_pressure=int(state["collapse_pressure"]) + added
            )
        return old, old, added
    new = max(1, min(5, old + delta))
    if new == old:
        return old, old, 0
    fields = {"risk": new}
    if new > old:
        fields["safe_hours"] = 0        # §9.3 complacency resets on any rise
    await set_state(conn, **fields)
    logger.info(
        "royal_fund: risk %d → %d (%s)", old, new, reason or "event"
    )
    return old, new, 0


async def add_pressure(conn: aiosqlite.Connection, amount: int) -> int:
    state = await get_state(conn)
    if int(state["risk"]) < 5 or amount <= 0:
        return 0
    before = int(state["collapse_pressure"])
    after = min(COLLAPSE_PRESSURE_MAX, before + amount)
    await set_state(conn, collapse_pressure=after)
    return after - before


def risk_block(old: int, new: int, reason: str = "") -> str:
    """§14.  Risk never moves silently — this block always accompanies it."""
    if new > old:
        header = "📈 **ROYAL FUND EXPOSURE INCREASED**"
    else:
        header = "📉 **ROYAL FUND EXPOSURE DECREASED**"
    quote = TRANSITION_QUOTES.get((old, new))
    if quote is None:
        # Multi-step jumps (a competent accountant, a bailout) have no scripted
        # line; fall back to the nearest single step in the same direction.
        step = new + (1 if new > old else -1)
        quote = TRANSITION_QUOTES.get((step, new), ROGER_STATUS[new])
    lines = [
        header,
        f"Previous: {RISK_METER[old]} **{old}/5 — {RISK_NAMES[old]}**",
        f"New:      {RISK_METER[new]} **{new}/5 — {RISK_NAMES[new]}**",
    ]
    if reason:
        lines.append(f"**Reason:** {reason}")
    lines.append(f"_Events now every {_interval_text(new)}._")
    lines.append(f"> **Roger:** \"{quote}\"")
    return "\n".join(lines)


def pressure_block() -> str:
    """§15.  Shown when something tries to push past Risk 5."""
    return (
        "⚠️ **ROYAL FUND PRESSURE INCREASED**\n"
        f"{RISK_METER[5]} **5/5 — {RISK_NAMES[5]}**\n"
        "The fund cannot become any more exposed.\n"
        "Unfortunately, this does not mean things cannot become worse.\n"
        "> **Roger:** \"I SAID FIVE WAS THE MAXIMUM. I DID NOT SAY THERE WAS "
        "NOTHING ABOVE IT.\""
    )


def _interval_text(risk: int) -> str:
    lo, hi = EVENT_INTERVAL[risk]
    return f"{lo} minutes" if lo == hi else f"{lo}–{hi} minutes"


def exposure_footer(risk: int, next_event_at: Optional[str] = None) -> str:
    """§16.  Every fund message ends with this."""
    line = (
        f"**Fund Exposure:** {RISK_METER[risk]} "
        f"**{risk}/5 — {RISK_NAMES[risk]}**"
    )
    if next_event_at:
        try:
            line += (
                f"\n**Next fund event:** "
                f"<t:{int(_parse(next_event_at).timestamp())}:R>"
            )
        except ValueError:
            pass
    return line


def exposure_meter(risk: int) -> str:
    """Plain-text variant for embed footers, which do not render markdown."""
    return f"Fund Exposure: {RISK_METER[risk]} {risk}/5 — {RISK_NAMES[risk]}"


# ── Collapse ──────────────────────────────────────────────────────────────────

def collapse_chance(risk: int, pressure: int) -> float:
    base = COLLAPSE_CHANCE.get(risk, 0.0)
    if risk == 5:
        return min(0.12, base + pressure / 100.0)
    return base


async def do_collapse(
    conn: aiosqlite.Connection, *, label: str = "💥 TOTAL COLLAPSE"
) -> tuple[int, int]:
    """Wipe the fund.  Returns ``(value destroyed, investors wiped)``."""
    holders = await positions(conn)
    total = sum(a for _u, a in holders)
    state = await get_state(conn)
    # Booked against the fund that died, not the one that replaces it: the
    # lifecycle counter goes up a few lines below, and a collapse filed under
    # the *new* fund would open every survivor's next fund at a loss.
    dying = int(state.get("lifecycle", 1))
    for user_id, amount in holders:
        await record_pnl(conn, user_id, -amount, label, lifecycle=dying)
    await conn.execute("UPDATE scam_players SET invested = 0 WHERE invested > 0")
    await set_state(
        conn,
        risk=1,
        collapse_pressure=0,
        lifecycle=int(state.get("lifecycle", 1)) + 1,
        safe_hours=0,
        panic_until=None, panic_bonus=0.0,
        leak_until=None,
        campaign_until=None, campaign_pct=0.0, campaign_cap=0,
        call_until=None, call_target=0, call_raised=0,
        pending_match=None, match_expires_at=None,
        next_event_at=None, last_report_at=None,
        collapses=int(state.get("collapses", 0)) + 1,
    )
    # §7: every timed effect and flow counter is cleared with the lifecycle.
    await conn.execute("DELETE FROM fund_cooldowns")
    await conn.execute("DELETE FROM fund_flows")
    await conn.execute("DELETE FROM fund_recent")
    await conn.execute("DELETE FROM fund_campaign_baseline")
    await conn.commit()
    return total, len(holders)


def collapse_embed(total: int, investors: int) -> discord.Embed:
    embed = discord.Embed(
        title="🚨 THE ROYAL INVESTMENT FUND HAS COLLAPSED",
        description=(
            "Following several unexpected market developments, regulatory "
            "misunderstandings and accounting events, the fund has ceased to "
            "exist in its previous financial form.\n\n"
            f"**All investor positions have been wiped out.**\n"
            f"Destroyed: **{money(total)}** across {investors} investor(s).\n\n"
            "> **Roger:** \"THIS WAS NOT A COLLAPSE. IT WAS A COMPLETE "
            "STRATEGIC RESET.\"\n\n"
            f"{RISK_DOT[1]} The fund reopens at **Risk 1 — {RISK_NAMES[1]}**. "
            "Roger would like everyone to know that he has learned from this."
        ),
        colour=_EMBED_RED,
    )
    embed.set_footer(text=exposure_meter(1))
    return embed


# ── External risk factors (§9) ────────────────────────────────────────────────

async def hourly_exposure_check(conn: aiosqlite.Connection) -> Optional[str]:
    """§9.1–9.3: the structural drift that keeps a big, quiet fund from being
    safe forever.  Returns a short description if the risk moved."""
    holders = await positions(conn)
    total = sum(a for _u, a in holders)
    if total <= 0:
        return None
    state = await get_state(conn)
    risk = int(state["risk"])

    up, down = next(
        (u, d) for cap, u, d in EXPOSURE_TABLE if total < cap
    )

    # §9.2 concentration — one whale makes Roger reckless.
    if holders:
        share = holders[0][1] / total
        for threshold, bonus in CONCENTRATION:
            if share >= threshold:
                up += bonus
                break
    if len(holders) >= MANY_INVESTORS_MIN:
        up += MANY_INVESTORS_BONUS

    # §9.3 complacency — a long quiet spell is itself a risk factor.
    safe_hours = int(state["safe_hours"])
    complacency = 0.0
    if risk <= 2 and safe_hours >= COMPLACENCY_AFTER_HOURS:
        complacency = min(
            COMPLACENCY_MAX,
            (safe_hours - COMPLACENCY_AFTER_HOURS + 1) * COMPLACENCY_PER_HOUR,
        )
        up += complacency

    up = max(HOURLY_UP_CLAMP[0], min(HOURLY_UP_CLAMP[1], up))
    down *= RISK_DOWN_SCALE.get(risk, 1.0)

    await set_state(
        conn,
        last_hourly_at=_iso(_now()),
        safe_hours=(safe_hours + 1) if risk <= 2 else 0,
    )

    if random.random() < up:
        old, new, pressure = await move_risk(conn, +1, reason="hourly exposure")
        if pressure:
            return "structural exposure added collapse pressure"
        if new != old:
            return f"structural exposure pushed risk to {new}"
    elif down > 0 and random.random() < down:
        old, new, _p = await move_risk(conn, -1, reason="hourly calm")
        if new != old:
            return f"a quiet hour brought risk down to {new}"
    return None


async def flow_window_check(conn: aiosqlite.Connection) -> Optional[str]:
    """§9.4/§9.5: net deposits calm Roger, net withdrawals frighten him.

    Per-player net flow is summed separately for positives and negatives, so a
    single player cycling money in and out cannot manufacture either signal.
    """
    state = await get_state(conn)
    risk = int(state["risk"])
    since = _now() - timedelta(minutes=FLOW_WINDOW_MINUTES)

    per_player: dict[str, int] = {}
    async with conn.execute(
        "SELECT discord_user_id, amount, kind FROM fund_flows WHERE at >= ?",
        (_iso(since),),
    ) as cur:
        async for uid, amount, flow_kind in cur:
            # Campaign and capital-call money is excluded from the *calming*
            # signal (it is already rewarded), but a withdrawal is a withdrawal.
            if flow_kind != "normal" and int(amount) > 0:
                continue
            per_player[str(uid)] = per_player.get(str(uid), 0) + int(amount)

    deposits = sum(v for v in per_player.values() if v > 0)
    withdrawals = -sum(v for v in per_player.values() if v < 0)
    await set_state(conn, last_window_at=_iso(_now()))

    # Withdrawals are checked first: a stampede should not be cancelled out by
    # somebody topping up in the same quarter hour.
    if withdrawals > 0:
        chance = next(c for cap, c in WITHDRAW_SCARE_TABLE if withdrawals < cap)
        if _active(state.get("panic_until")):
            chance += float(state.get("panic_bonus") or 0.0)
        chance = min(WITHDRAW_SCARE_CAP, chance)
        if chance > 0 and random.random() < chance:
            old, new, pressure = await move_risk(
                conn, +1, reason="withdrawal pressure"
            )
            if pressure:
                return "withdrawals added collapse pressure"
            if new != old:
                return f"withdrawals of {money(withdrawals)} pushed risk to {new}"
            return None

    if deposits > 0:
        scale = DEPOSIT_CALM_SCALE.get(risk, 1.0)
        chance = min(DEPOSIT_CALM_MAX, deposits / 100.0 * DEPOSIT_CALM_PER_100)
        chance *= scale
        if chance > 0 and random.random() < chance:
            old, new, _p = await move_risk(conn, -1, reason="deposits")
            if new != old:
                return f"deposits of {money(deposits)} calmed Roger to risk {new}"
    return None


async def on_quick_scam(conn: aiosqlite.Connection, outcome: str) -> Optional[str]:
    """§9.6: hook called by the quick scam resolver.

    ``outcome`` is one of ``success`` / ``rare_success`` / ``failure`` /
    ``extreme``.  Rarity is deliberately ignored — only how badly it went.
    """
    if outcome not in SCAM_RISK_EFFECT:
        return None
    state = await get_state(conn)
    if await fund_total(conn) <= 0:
        return None
    risk = int(state["risk"])
    delta, chance = SCAM_RISK_EFFECT[outcome]
    if delta < 0:
        # Calming a committed Roger is harder; frightening him never is.
        chance *= RISK_DOWN_FROM_SCAMS[risk]
    if random.random() >= chance:
        return None
    old, new, pressure = await move_risk(conn, delta, reason=f"quick scam {outcome}")
    if pressure:
        return "the quick scam added collapse pressure to the fund"
    if new != old:
        return f"the quick scam moved the fund to risk {new}"
    return None


# ── Cog ───────────────────────────────────────────────────────────────────────

class RoyalFundCog(commands.Cog, name="royal_fund"):
    """Roger's fund: the risk dial, the event scheduler and `/invest`."""

    def __init__(self, bot: commands.Bot, conn: aiosqlite.Connection) -> None:
        self.bot = bot
        self.conn = conn
        self._lock = asyncio.Lock()

    async def cog_load(self) -> None:
        await setup_schema(self.conn)
        await self._catch_up()
        self.fund_tick.start()

    async def _catch_up(self) -> None:
        """§20: restore state without replaying downtime.

        A deadline that elapsed while the bot was offline becomes *one* event a
        minute or two from now, never the fifteen that "should" have fired.
        """
        state = await get_state(self.conn)
        nxt = state.get("next_event_at")
        if nxt and _parse(nxt) <= _now():
            when = _now() + timedelta(minutes=random.randint(1, 3))
            await set_state(self.conn, next_event_at=_iso(when))
            logger.info(
                "royal_fund: missed event deadline while offline — one "
                "catch-up scheduled for %s", when.isoformat(timespec="seconds")
            )
        # A stale report timestamp would otherwise fire a bulletin instantly.
        last_report = state.get("last_report_at")
        if not last_report or _parse(last_report) <= _now() - timedelta(
            minutes=HOURLY_REPORT_MINUTES
        ):
            await set_state(self.conn, last_report_at=_iso(_now()))

    def cog_unload(self) -> None:
        self.fund_tick.cancel()

    # ── scheduling ────────────────────────────────────────────────────

    async def _schedule_next(self, risk: Optional[int] = None) -> datetime:
        if risk is None:
            risk = int((await get_state(self.conn))["risk"])
        lo, hi = EVENT_INTERVAL[risk]
        when = _now() + timedelta(minutes=random.randint(lo, hi))
        await set_state(self.conn, next_event_at=_iso(when))
        return when

    async def _announce(self, embed: discord.Embed) -> None:
        channel = self.bot.get_channel(GAME_CHANNEL_ID)
        if channel is None:
            logger.warning("royal_fund: game channel missing")
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.warning("royal_fund: could not announce fund event")

    def _guild(self) -> Optional[discord.Guild]:
        channel = self.bot.get_channel(GAME_CHANNEL_ID)
        return getattr(channel, "guild", None)

    @tasks.loop(seconds=20)
    async def fund_tick(self) -> None:
        """Drive every timer: capital calls, flow windows, hourly checks, events.

        Runs far more often than any single deadline so a Risk-5 three-minute
        cadence stays accurate without a dedicated timer per effect.
        """
        try:
            await self._resolve_capital_call()
            await self._maybe_flow_window()
            await self._maybe_hourly()
            await self._maybe_report()
            await self._maybe_event()
        except Exception:
            logger.exception("royal_fund: tick failed")

    @fund_tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_hourly(self) -> None:
        state = await get_state(self.conn)
        last = state.get("last_hourly_at")
        if last and _parse(last) + timedelta(minutes=HOURLY_CHECK_MINUTES) > _now():
            return
        async with self._lock:
            before = int((await get_state(self.conn))["risk"])
            note = await hourly_exposure_check(self.conn)
            after = int((await get_state(self.conn))["risk"])
        if note and after != before:
            await self._announce_risk_change(before, after, note)

    async def _maybe_flow_window(self) -> None:
        state = await get_state(self.conn)
        last = state.get("last_window_at")
        if last and _parse(last) + timedelta(minutes=FLOW_WINDOW_MINUTES) > _now():
            return
        if await fund_total(self.conn) <= 0:
            await set_state(self.conn, last_window_at=_iso(_now()))
            return
        async with self._lock:
            before = int((await get_state(self.conn))["risk"])
            note = await flow_window_check(self.conn)
            after = int((await get_state(self.conn))["risk"])
        if note and after != before:
            await self._announce_risk_change(before, after, note)

    async def _announce_risk_change(self, old: int, new: int, why: str) -> None:
        state = await get_state(self.conn)
        embed = discord.Embed(
            description=(
                risk_block(old, new, why)
                + "\n\n"
                + exposure_footer(new, state.get("next_event_at"))
            ),
            colour=RISK_COLOUR[new],
        )
        await self._announce(embed)

    async def _announce_pressure(self) -> None:
        """§15: risk tried to rise past 5 and became collapse pressure."""
        await self._announce(discord.Embed(
            description=pressure_block(), colour=RISK_COLOUR[5]
        ))

    async def _remember(self, event: dict, result: "FundResult") -> None:
        """Keep the last few events for the status card (§17)."""
        summary = result.headline or (
            result.lines[0].replace("**", "").replace("_", "")
            if result.lines else "resolved"
        )
        await self.conn.execute(
            "INSERT INTO fund_recent (emoji, name, summary, at)"
            " VALUES (?, ?, ?, ?)",
            (event["emoji"], event["name"], summary[:120], _iso(_now())),
        )
        await self.conn.execute(
            "DELETE FROM fund_recent WHERE id NOT IN"
            " (SELECT id FROM fund_recent ORDER BY id DESC LIMIT ?)",
            (RECENT_EVENTS_KEPT,),
        )
        await self.conn.commit()

    async def recent_events(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        async with self.conn.execute(
            "SELECT emoji, name, summary FROM fund_recent ORDER BY id DESC LIMIT ?",
            (RECENT_EVENTS_KEPT,),
        ) as cur:
            async for r in cur:
                rows.append((str(r[0]), str(r[1]), str(r[2])))
        return rows

    # ── capital call ──────────────────────────────────────────────────

    async def _resolve_capital_call(self) -> None:
        state = await get_state(self.conn)
        until = state.get("call_until")
        if not until or _parse(until) > _now():
            return
        async with self._lock:
            state = await get_state(self.conn)
            if not state.get("call_until") or _parse(state["call_until"]) > _now():
                return
            raised = int(state.get("call_raised") or 0)
            target = int(state.get("call_target") or CAPITAL_CALL_TARGET)
            met = raised >= target
            await set_state(self.conn, call_until=None, call_target=0, call_raised=0)
            old, new, pressure = await move_risk(
                self.conn, -1 if met else +1,
                reason="capital call " + ("met" if met else "missed"),
            )
            await self._schedule_next()

        body = (
            f"Raised **{money(raised)}** of **{money(target)}**.\n\n"
            + (
                "✅ The fund has been recapitalised. Roger is visibly relieved "
                "and has already begun spending it."
                if met else
                "❌ Not enough. Roger says this changes nothing, while making "
                "several phone calls that suggest otherwise."
            )
        )
        if old != new:
            body += "\n\n" + risk_block(
                old, new, "capital call " + ("met" if met else "missed")
            )
        elif pressure:
            body += "\n\n_Roger is already at maximum risk. This became "\
                    "collapse pressure instead._"
        embed = discord.Embed(
            title="🚨 EMERGENCY CAPITAL CALL — "
                  + ("GOAL REACHED" if met else "GOAL MISSED"),
            description=body,
            colour=_EMBED_GREEN if met else _EMBED_RED,
        )
        embed.set_footer(text=exposure_meter(new))
        await self._announce(embed)
        if pressure:
            await self._announce_pressure()

    # ── the main event ────────────────────────────────────────────────

    async def _maybe_event(self) -> None:
        state = await get_state(self.conn)
        # §3: a capital call pauses the normal scheduler entirely.
        if _active(state.get("call_until")):
            return
        if not state.get("next_event_at"):
            if await fund_total(self.conn) > 0:
                await self._schedule_next()
            return
        if _parse(state["next_event_at"]) > _now():
            return
        if await fund_total(self.conn) <= 0:
            # §3 step 1: an empty fund has no drama. Leave the scheduler idle.
            await set_state(self.conn, next_event_at=None)
            return
        await self.run_event()

    async def _select_event(self, state: dict) -> Optional[dict]:
        """§3 steps 4–8: matching opportunity, then Core vs Special, then weight.

        An ineligible Special falls back to a Core event of the same risk
        rather than rerolling the Special pool — rerolling would quietly
        concentrate probability onto whichever Specials have no gates.
        """
        risk = int(state["risk"])

        # §10.  A stored match gets one 50% roll *when it is actually usable*.
        # If the fund is at the wrong risk level or the event is cooling down
        # the opportunity is kept — up to an hour — rather than wasted, which
        # is what makes the scam/fund jokes land as often as intended.
        match_id = state.get("pending_match")
        if match_id:
            expires = state.get("match_expires_at")
            if expires and _parse(expires) <= _now():
                await set_state(self.conn, pending_match=None, match_expires_at=None)
            else:
                candidates = [
                    BY_ID[e] for e in MATCH_EVENTS.get(match_id, [])
                    if e in BY_ID and BY_ID[e]["risk"] == risk
                ]
                usable = [
                    c for c in candidates if await _eligible(self.conn, c, state)
                ]
                if usable:
                    await set_state(
                        self.conn, pending_match=None, match_expires_at=None
                    )
                    if random.random() < MATCH_EVENT_CHANCE:
                        return random.choice(usable)

        want_core = random.random() < CORE_SHARE[risk]
        if not want_core:
            specials = pool(risk, "special")
            if specials:
                choice = random.choices(
                    specials, weights=[e["weight"] for e in specials], k=1
                )[0]
                if await _eligible(self.conn, choice, state):
                    return choice
            # fall through to Core

        cores = pool(risk, "core")
        for _try in range(8):
            choice = random.choices(
                cores, weights=[e["weight"] for e in cores], k=1
            )[0]
            if await _eligible(self.conn, choice, state):
                return choice
        return None

    async def run_event(self) -> None:
        """One turn of the fund: collapse check, event, risk movement."""
        async with self._lock:
            state = await get_state(self.conn)
            risk = int(state["risk"])
            pressure = int(state["collapse_pressure"])
            total = await fund_total(self.conn)
            if total <= 0:
                await set_state(self.conn, next_event_at=None)
                return

            # §3 steps 2–3: the collapse roll happens *before* the event, so a
            # Risk-5 fund can die without any warning shot.
            chance = collapse_chance(risk, pressure)
            if chance > 0 and random.random() < chance:
                destroyed, investors = await do_collapse(self.conn)
                await self._announce(collapse_embed(destroyed, investors))
                return

            event = await self._select_event(state)
            if event is None:
                await self._schedule_next(risk)
                return

            before_total = total
            result = await apply_event(
                self.conn, event, state, self._guild()
            )
            # The Big Short settles here and nowhere else: this is the only
            # path a *natural* event takes.  Anything a /special card does to
            # the fund deliberately does not count as Roger's decision.
            short_lines = await self._settle_big_shorts(before_total)
            await start_cooldown(self.conn, event["id"], event["cooldown"])
            await self._remember(event, result)
            await set_state(
                self.conn,
                last_event_at=_iso(_now()),
                last_event_id=event["id"],
                events_run=int(state.get("events_run", 0)) + 1,
            )

            # §3 steps 10–13: event risk movement, then Risk-5 pressure decay.
            note = ""
            delta = event["risk_delta"] + result.risk_delta
            if event["risk_5050"]:
                delta += random.choice((-1, +1))
            if event["risk_chance"]:
                p, d = event["risk_chance"]
                if random.random() < p:
                    delta += d
            added = event["pressure"] + result.pressure
            hit_ceiling = False
            if added and await add_pressure(self.conn, added):
                note = ("\n\n_Something about this has made collapse "
                        "meaningfully more likely._")

            old, new, from_delta = (risk, risk, 0)
            if delta:
                old, new, from_delta = await move_risk(
                    self.conn, delta, reason=event["id"]
                )
            hit_ceiling = bool(from_delta)

            # A survived Risk-5 event bleeds one point of pressure back off.
            if new >= 5:
                st = await get_state(self.conn)
                if int(st["collapse_pressure"]) > 0:
                    await set_state(
                        self.conn,
                        collapse_pressure=int(st["collapse_pressure"]) - 1,
                    )

            next_at = _iso(await self._schedule_next(new))
            after_total = await fund_total(self.conn)

        embed = discord.Embed(
            title=f"{event['emoji']} {event['name']}",
            description=(
                f"{event['description']}\n\n"
                f"> **Roger:** \"{event['roger']}\"\n\n"
                + "\n".join(result.lines)
                + (
                    f"\n\n{risk_block(old, new, event['name'].lower())}"
                    if old != new else ""
                )
                + note
                + ("\n\n" + "\n".join(short_lines) if short_lines else "")
                + "\n\n" + exposure_footer(new, next_at)
            ),
            colour=result.colour or RISK_COLOUR[new],
        )
        embed.add_field(name="Fund value", value=money(after_total), inline=True)
        embed.add_field(
            name="Investors",
            value=str(len(await positions(self.conn))),
            inline=True,
        )
        await self._announce(embed)
        if hit_ceiling:
            await self._announce_pressure()

    async def _settle_big_shorts(self, before_total: int) -> list[str]:
        """Pay out every open Big Short against this event's direction.

        "Negative" means the fund is worth less after the event than before —
        measured on the total, because that is the thing a bet on Roger's
        judgement is actually about.
        """
        from nigeria_bot import special_effects as fx

        async with self.conn.execute(
            "SELECT id, owner_id FROM special_effects"
            " WHERE kind = 'big_short' AND status = 'active'"
        ) as cur:
            bets = [(int(r[0]), str(r[1])) async for r in cur]
        if not bets:
            return []
        after = await fund_total(self.conn)
        fell = after < before_total
        lines = []
        for effect_id, owner in bets:
            await fx.consume_effect(self.conn, effect_id)
            if fell:
                await fx.give_cash(self.conn, owner, 7_500,
                                   reason="special_gain", detail="The Big Short")
                lines.append(
                    f"📉 **THE BIG SHORT PAYS** — the fund fell, and <@{owner}> "
                    f"collects **{money(7_500)}**."
                )
            else:
                lines.append(
                    f"📈 **THE BIG SHORT FAILS** — <@{owner}>'s "
                    f"{money(3_000)} position expires worthless."
                )
        await self.conn.commit()
        return lines

    # ── quick scam hook ───────────────────────────────────────────────

    async def note_quick_scam(self, outcome: str, template_id: str) -> None:
        """Called by the quick scam resolver: risk movement + match memory."""
        try:
            async with self._lock:
                before = int((await get_state(self.conn))["risk"])
                note = await on_quick_scam(self.conn, outcome)
                after = int((await get_state(self.conn))["risk"])
                if template_id in MATCH_EVENTS:
                    await set_state(self.conn, pending_match=template_id)
            if note and after != before:
                await self._announce_risk_change(before, after, note)
        except Exception:
            logger.exception("royal_fund: quick scam hook failed")

    # ── hourly public report (§18) ────────────────────────────────────

    async def _maybe_report(self) -> None:
        state = await get_state(self.conn)
        last = state.get("last_report_at")
        if last and _parse(last) + timedelta(minutes=HOURLY_REPORT_MINUTES) > _now():
            return
        holders = await positions(self.conn)
        if not holders:
            # §18/§19: no fund, no bulletin. Roger has nothing to boast about.
            await set_state(self.conn, last_report_at=_iso(_now()))
            return
        await set_state(self.conn, last_report_at=_iso(_now()))
        await self._announce(await self._report_embed(holders, state))

    async def _report_embed(
        self, holders: list[tuple[str, int]], state: dict
    ) -> discord.Embed:
        risk = int(state["risk"])
        total = sum(a for _u, a in holders)
        guild = self._guild()
        embed = discord.Embed(
            title="🏦 ROYAL INVESTMENT FUND — HOURLY REPORT",
            description=(
                f"**Total Fund Value:** {money(total)}\n"
                f"**Total Investors:** {len(holders)}\n"
                f"**Fund Exposure:** {RISK_METER[risk]} "
                f"**{risk}/5 — {RISK_NAMES[risk]}**\n"
                f"**Current Withdrawal Tax:** "
                f"{ANTI_PANIC_TAX.get(risk, 0) * 100:.0f}%\n\n"
                f"_{ROGER_STATUS[risk]}_"
                + _last_event_line(state)
            ),
            colour=RISK_COLOUR[risk],
        )
        for name, value in _position_fields(holders, total, guild):
            embed.add_field(name=name, value=value, inline=False)
        effects = _effect_lines(state)
        if effects:
            embed.add_field(
                name="Active Effects", value="\n".join(effects), inline=False
            )
        embed.add_field(
            name="Roger's Investment Advice",
            value=f"> {_advice(total, risk)}",
            inline=False,
        )
        embed.set_footer(text=exposure_meter(risk))
        return embed

    # ── /invest ───────────────────────────────────────────────────────

    invest_group = app_commands.Group(
        name="invest",
        description="Deposit into or withdraw from the Royal Investment Fund.",
    )

    @invest_group.command(
        name="status", description="Roger's fund: risk level, value and exposure."
    )
    async def invest_status(self, interaction: discord.Interaction) -> None:
        await self._invest(interaction, "status", None)

    @invest_group.command(name="deposit", description="Put money into the fund.")
    @app_commands.describe(
        amount=(
            f"Amount in {CURRENCY} (leave empty to deposit everything you have)."
        )
    )
    async def invest_deposit(
        self, interaction: discord.Interaction,
        amount: Optional[app_commands.Range[int, 1, None]] = None,
    ) -> None:
        await self._invest(interaction, "deposit", amount)

    @invest_group.command(
        name="withdraw", description="Take money out while you still can."
    )
    @app_commands.describe(
        amount=f"Amount in {CURRENCY} (leave empty to withdraw everything)."
    )
    async def invest_withdraw(
        self, interaction: discord.Interaction,
        amount: Optional[app_commands.Range[int, 1, None]] = None,
    ) -> None:
        await self._invest(interaction, "withdraw", amount)

    # ── /fundluck ─────────────────────────────────────────────────────

    @app_commands.command(
        name="fundluck",
        description="What Roger's Fund has actually done to your money.",
    )
    @app_commands.describe(player="Optional: look up somebody else's luck.")
    async def fundluck(
        self, interaction: discord.Interaction,
        player: Optional[discord.Member] = None,
    ) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        who = player or interaction.user
        data = await fund_luck(self.conn, str(who.id))
        await interaction.response.send_message(
            embed=self._luck_embed(who, data), ephemeral=True
        )

    def _luck_embed(self, who: discord.abc.User, data: dict) -> discord.Embed:
        cur, life = data["current"], data["lifetime"]
        pnl = cur["pnl"]
        colour = (
            _EMBED_GREEN if pnl > 0 else _EMBED_RED if pnl < 0 else _EMBED_GREY
        )

        if not data["any"]:
            return discord.Embed(
                title=f"🎲 FUND LUCK — {who.display_name}",
                description=(
                    "You have never put a single Naira into the Royal "
                    "Investment Fund.\n\n"
                    "Statistically, this makes you the most successful "
                    "investor in Nigeria.\n\n"
                    "`/invest deposit` to ruin that record."
                ),
                colour=_EMBED_GREY,
            )

        embed = discord.Embed(
            title=f"🎲 FUND LUCK — {who.display_name}",
            description=(
                f"_Fund #{data['lifecycle']} — the one currently running._\n\n"
                + _verdict(pnl, cur["deposited"])
            ),
            colour=colour,
        )

        split = ""
        if cur["cash_pnl"]:
            # Worth separating: a player whose position looks fine can still be
            # badly down because the fund kept auditing their cash.
            split = (
                f"Value of your position: **{_delta(cur['position_pnl'])}**\n"
                f"Cash paid to you / taken: **{_delta(cur['cash_pnl'])}**\n"
            )
        embed.add_field(
            name="📊 THIS FUND",
            value=(
                f"Deposited: **{money(cur['deposited'])}**\n"
                f"Withdrawn: **{money(cur['withdrawn'])}**\n"
                f"Position now: **{money(data['position'])}**\n"
                f"➖➖➖\n"
                + split
                + f"**Profit / loss: {_delta(pnl)}**{_ratio(pnl, cur['deposited'])}"
            ),
            inline=False,
        )

        causes = data["causes"]
        if causes:
            shown = causes[:10]
            lines = [
                f"`{_delta(net):>16}`  {label}"
                + (f" ×{count}" if count > 1 else "")
                for label, net, count in shown
            ]
            rest = causes[len(shown):]
            if rest:
                lines.append(
                    f"_…and {len(rest)} other cause(s), netting "
                    f"{_delta(sum(n for _l, n, _c in rest))}._"
                )
            embed.add_field(
                name="🎯 WHAT CAUSED IT", value="\n".join(lines), inline=False
            )
        elif cur["deposited"]:
            embed.add_field(
                name="🎯 WHAT CAUSED IT",
                value="_Nothing has happened to your money yet. Enjoy it._",
                inline=False,
            )

        embed.add_field(
            name="🏛️ EVERY FUND SO FAR",
            value=(
                f"Funds invested in: **{data['funds']}**\n"
                f"Total deposited: **{money(life['deposited'])}**\n"
                f"Total withdrawn: **{money(life['withdrawn'])}**\n"
                f"➖➖➖\n"
                f"**Lifetime profit / loss: {_delta(life['pnl'])}**"
                + _ratio(life["pnl"], life["deposited"])
            ),
            inline=False,
        )

        extremes = []
        if data["best"]:
            label, delta, at = data["best"]
            extremes.append(
                f"🍀 Best: **{_delta(delta)}** — {label} "
                f"(<t:{int(_parse(at).timestamp())}:R>)"
            )
        if data["worst"]:
            label, delta, at = data["worst"]
            extremes.append(
                f"💀 Worst: **{_delta(delta)}** — {label} "
                f"(<t:{int(_parse(at).timestamp())}:R>)"
            )
        if extremes:
            embed.add_field(
                name="📈 YOUR RECORDS", value="\n".join(extremes), inline=False
            )

        embed.set_footer(
            text="Deposits and withdrawals are your own money moving — "
                 "only what Roger did to it counts as profit or loss."
        )
        return embed

    async def _invest(
        self, interaction: discord.Interaction, action: str,
        amount: Optional[int],
    ) -> None:
        if not await _require_channel(interaction, GAME_CHANNEL_ID, GAME_CHANNEL_URL):
            return
        # Withdrawing is the documented way out of a cell: the arrest notice,
        # the jail refusal and /scamrules all tell you to free up cash and
        # `/paybribe`.  Blocking it made that advice impossible to follow and
        # could strand a player with a payable bribe they could not reach.
        # Depositing stays blocked — it cannot help you, and putting money
        # beyond your own reach while jailed is not a decision worth allowing.
        if action == "deposit" and not await require_free(
            interaction, self.conn, "put money into the fund"
        ):
            return
        uid = str(interaction.user.id)

        if action in ("deposit", "withdraw"):
            from nigeria_bot import special_effects as fx
            frozen = await fx.fund_frozen(self.conn, uid)
            if frozen:
                until = _parse(frozen["expires_at"])
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="🔒 YOUR ASSETS ARE FROZEN",
                        description=(
                            "Somebody has persuaded the authorities to take an "
                            "interest in your account.\n\n"
                            "**Deposits:** BLOCKED\n**Withdrawals:** BLOCKED\n"
                            f"**Thawing:** <t:{int(until.timestamp())}:R>\n\n"
                            "_Your position is still fully exposed to whatever "
                            "Roger does in the meantime. That is the cruel "
                            "part._"
                        ),
                        colour=_EMBED_RED,
                    ),
                    ephemeral=True,
                )
                return

        if action == "withdraw":
            await self._withdraw(interaction, uid, amount)
            return

        note: Optional[str] = None
        async with self._lock:
            state = await get_state(self.conn)
            player = await get_player(self.conn, uid)

            if action == "deposit":
                # No amount means "all of it".  Resolved here rather than at
                # the command, so it reads the balance inside the lock and
                # cannot hand Roger money that was spent while the slash
                # command was still being typed.
                all_in = amount is None
                if all_in:
                    amount = player["balance"]
                if amount <= 0:
                    await interaction.response.send_message(
                        "❌ You have no cash to deposit." if all_in else
                        f"❌ You only have {money(player['balance'])}.",
                        ephemeral=True,
                    )
                    return
                if player["balance"] < amount:
                    await interaction.response.send_message(
                        f"❌ You only have {money(player['balance'])}.",
                        ephemeral=True,
                    )
                    return

                # §19: money arriving into a dead fund starts a new lifecycle.
                # The reset happens *before* the money lands so the deposit is
                # booked into the fund it actually opens, and so a stale
                # capital call left over from the dead fund cannot claim it.
                reborn = await fund_total(self.conn) <= 0
                if reborn:
                    await self._restart_lifecycle()
                    state = await get_state(self.conn)
                from nigeria_bot.special_game import touch
                await touch(self.conn, uid)
                await adjust_balance(self.conn, uid, -amount, "fund_deposit")
                await _add_position(self.conn, uid, amount,
                                    label="Deposit", kind="deposit")

                flow_kind = "normal"
                bits = [
                    f"You hand Roger **{money(amount)}**. He writes it in a "
                    "notebook and assures you it is completely safe."
                ]

                if _active(state.get("call_until")):
                    flow_kind = "call"
                    raised = int(state.get("call_raised") or 0) + amount
                    target = int(state.get("call_target") or CAPITAL_CALL_TARGET)
                    await set_state(self.conn, call_raised=raised)
                    bits.append(
                        f"🚨 Counted toward the capital call: "
                        f"**{money(raised)} / {money(target)}**"
                        + (" — **target reached!**" if raised >= target else "")
                    )
                elif _active(state.get("campaign_until")):
                    flow_kind = "campaign"
                    bonus = await self._campaign_match(uid, state)
                    if bonus > 0:
                        bits.append(
                            f"💳 Campaign bonus: **+{money(bonus)}** "
                            f"({float(state['campaign_pct']) * 100:.0f}% of "
                            "genuinely new money)."
                        )
                    else:
                        bits.append(
                            "_No campaign bonus: only money above the position "
                            "you held when the campaign opened is matched._"
                        )
                await record_flow(self.conn, uid, amount, flow_kind)
                await self.conn.commit()

                if reborn:
                    bits.append(
                        "\n🏦 **The fund is open again.** Roger resets to "
                        f"{RISK_DOT[1]} **Risk 1 — {RISK_NAMES[1]}** and "
                        "promises that this time will be different."
                    )
                if all_in:
                    # Said once, plainly: a fund position is not cash on hand,
                    # so an all-in deposit is exactly how a failed scam turns
                    # into a cell.  Worth a line, not a confirmation dialog —
                    # they asked for a shortcut, not an argument.
                    bits.append(
                        "\n⚠️ **You now have no cash at all.** A bribe can only "
                        "be paid from cash, and your fund position does not "
                        "count — the next scam that goes wrong puts you in a "
                        "cell. `/invest withdraw` is how you get out."
                    )
                note = "\n".join(bits)

            player = await get_player(self.conn, uid)
            state = await get_state(self.conn)
            holders = await positions(self.conn)

        # The full card is a page of one person's finances; reprinting it in
        # the channel on every deposit buried everything else. The mover gets
        # the detail privately, the room gets one line.
        embeds = await self._status_embeds(
            interaction, uid, player, state, holders, note
        )
        await interaction.response.send_message(embeds=embeds, ephemeral=True)
        # Only an actual deposit gets announced.  `/invest status` reaches here
        # too, with no amount at all — announcing that as a deposit crashed the
        # command outright.
        if action == "deposit" and amount:
            await self._announce_flow(
                interaction, uid, amount, state, holders, deposit=True
            )

    async def _announce_flow(
        self, interaction: discord.Interaction, uid: str, amount: int,
        state: dict, holders: list[tuple[str, int]], *, deposit: bool,
    ) -> None:
        """One public line: who moved money, which way, and where that leaves
        the fund.  Deliberately not an embed — this is a ticker, not a report."""
        risk = int(state["risk"])
        total = sum(a for _u, a in holders)
        who = interaction.user.mention
        if deposit:
            line = (
                f"🏦 {who} put **{money(amount)}** into the Royal Fund."
            )
            if _active(state.get("call_until")):
                raised = int(state.get("call_raised") or 0)
                target = int(state.get("call_target") or CAPITAL_CALL_TARGET)
                line += (
                    f" 🚨 Capital call: **{money(raised)} / {money(target)}**."
                )
        else:
            line = f"🏦 {who} pulled **{money(amount)}** out of the Royal Fund."
        line += (
            f" Fund is now **{money(total)}** across {len(holders)} investor"
            f"{'s' if len(holders) != 1 else ''} · "
            f"{RISK_DOT[risk]} Risk {risk}/5."
        )
        channel = self.bot.get_channel(GAME_CHANNEL_ID)
        if channel is None:
            return
        try:
            await channel.send(line)
        except discord.HTTPException:
            logger.warning("royal_fund: could not announce a fund movement")

    async def _campaign_match(self, uid: str, state: dict) -> int:
        """§13: match only net-new money above the campaign-start position.

        Without the baseline, a player could withdraw and redeposit the same
        Naira all window and mint free money out of Roger's promotion.
        """
        pct = float(state.get("campaign_pct") or 0.0)
        cap = int(state.get("campaign_cap") or 0)
        async with self.conn.execute(
            "SELECT position, matched FROM fund_campaign_baseline"
            " WHERE discord_user_id = ?", (uid,),
        ) as cur:
            row = await cur.fetchone()
        baseline, matched = (int(row[0]), int(row[1])) if row else (0, 0)
        async with self.conn.execute(
            "SELECT invested FROM scam_players WHERE discord_user_id = ?", (uid,),
        ) as cur:
            pos = int((await cur.fetchone())[0])
        # Bonuses already paid sit inside the position, so they must come back
        # out before measuring "new money" — otherwise each match grows the
        # base for the next one and the campaign quietly compounds itself.
        new_money = max(0, pos - baseline - matched)
        earned = min(cap, int(round(new_money * pct)))
        bonus = max(0, earned - matched)
        if bonus > 0:
            await _add_position(self.conn, uid, bonus,
                                label="💳 Deposit Campaign match")
            await self.conn.execute(
                "UPDATE fund_campaign_baseline SET matched = ?"
                " WHERE discord_user_id = ?", (matched + bonus, uid),
            )
        return bonus

    async def _restart_lifecycle(self) -> None:
        """§19: first deposit into an empty fund begins a fresh lifecycle."""
        state = await get_state(self.conn)
        await set_state(
            self.conn,
            risk=1, collapse_pressure=0, safe_hours=0,
            lifecycle=int(state.get("lifecycle", 1)) + 1,
            panic_until=None, panic_bonus=0.0, leak_until=None,
            campaign_until=None, campaign_pct=0.0, campaign_cap=0,
            call_until=None, call_target=0, call_raised=0,
            pending_match=None, match_expires_at=None,
            next_event_at=_iso(_now() + timedelta(minutes=EVENT_INTERVAL[1][0])),
            last_report_at=_iso(_now()),
        )
        await self.conn.execute("DELETE FROM fund_cooldowns")
        await self.conn.execute("DELETE FROM fund_recent")
        await self.conn.execute("DELETE FROM fund_campaign_baseline")
        await self.conn.commit()
        logger.info("royal_fund: new lifecycle started")

    # ── withdrawals (§12) ─────────────────────────────────────────────

    async def _withdraw(
        self, interaction: discord.Interaction, uid: str, amount: Optional[int]
    ) -> None:
        state = await get_state(self.conn)
        player = await get_player(self.conn, uid)
        held = player["invested"]
        take = held if amount is None else min(amount, held)
        if take <= 0:
            await interaction.response.send_message(
                "❌ You have nothing in the fund.", ephemeral=True
            )
            return

        risk = int(state["risk"])
        rate = ANTI_PANIC_TAX.get(risk, 0.0)
        if rate <= 0:
            await self._do_withdraw(interaction, uid, take, 0, risk)
            return

        # §12: the tax must be shown *before* the withdrawal is confirmed.
        tax = int(round(take * rate))
        embed = discord.Embed(
            title="😰 ROGER'S ANTI-PANIC TAX",
            description=(
                "Withdrawing during a period of completely unjustified "
                "investor panic carries an emergency confidence-preservation "
                "charge.\n\n"
                f"Withdrawal: **{money(take)}**\n"
                f"Anti-Panic Tax ({rate * 100:.0f}% at Risk {risk}): "
                f"**−{money(tax)}**\n"
                f"You receive: **{money(take - tax)}**\n\n"
                "> **Roger:** \"If your withdrawal contributes to the panic, "
                "you can at least help pay for the panic.\""
            ),
            colour=_EMBED_RED,
        )
        await interaction.response.send_message(
            embed=embed,
            view=WithdrawConfirmView(self, uid, take, tax, risk),
            ephemeral=True,
        )

    async def _do_withdraw(
        self, interaction: discord.Interaction, uid: str,
        take: int, tax: int, risk: int, *, followup: bool = False,
    ) -> None:
        async with self._lock:
            player = await get_player(self.conn, uid)
            take = min(take, player["invested"])
            if take <= 0:
                msg = "❌ You have nothing left in the fund."
                if followup:
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
                return
            tax = int(round(take * ANTI_PANIC_TAX.get(risk, 0.0)))
            await _add_position(self.conn, uid, -take,
                                label="Withdrawal", kind="withdraw")
            from nigeria_bot.special_game import touch
            await touch(self.conn, uid)
            # The tax is destroyed, not banked: the position falls by the gross
            # amount and only the net reaches the player.
            audit = 0
            auditor = None
            if take > 1_000:
                from nigeria_bot import special_effects as fx
                trap = await fx.take_trap(self.conn, "tax_audit", uid)
                if trap:
                    # The full amount still leaves the fund; the penalty is
                    # taken out of what reaches the player, and destroyed.
                    audit = min(int(take * 0.20), 2_000)
                    auditor = trap["owner_id"]
            await adjust_balance(self.conn, uid, take - tax - audit,
                                 "fund_withdraw")
            # Neither charge ever reaches the player, so neither is visible in
            # the principal they withdrew — but both are money the fund cost
            # them, and `/fundluck` is where that has to show up.
            await record_pnl(self.conn, uid, -tax, "😰 Anti-Panic Tax",
                             kind="cash")
            await record_pnl(self.conn, uid, -audit, "🧾 Emergency Tax Audit",
                             kind="cash")
            await record_flow(self.conn, uid, -take, "normal")
            await self.conn.commit()
            player = await get_player(self.conn, uid)
            state = await get_state(self.conn)
            holders = await positions(self.conn)

        note = (
            f"You withdraw **{money(take)}**. Roger looks hurt by your lack of "
            "faith."
        )
        if tax:
            note += (
                f"\n\n😰 **Anti-Panic Tax: −{money(tax)}** — you bank "
                f"**{money(take - tax)}**. The tax is gone; it does not stay "
                "in the fund."
            )
        if audit:
            note += (
                f"\n\n🧾 **EMERGENCY TAX AUDIT: −{money(audit)}** — the "
                "paperwork was unusually legible."
            )
        embeds = await self._status_embeds(
            interaction, uid, player, state, holders, note
        )
        if followup:
            await interaction.followup.send(embeds=embeds, ephemeral=True)
        else:
            await interaction.response.send_message(embeds=embeds, ephemeral=True)
        # The gross figure is what left the fund, and that is the room's
        # business. The anti-panic tax stays in the private card.
        await self._announce_flow(
            interaction, uid, take, state, holders, deposit=False
        )
        if audit:
            # An audit is public: it names the filer, which is the entire
            # social point of the card.
            channel = self.bot.get_channel(GAME_CHANNEL_ID)
            if channel is not None:
                with contextlib.suppress(discord.HTTPException):
                    await channel.send(embed=discord.Embed(
                        title="🧾 EMERGENCY TAX AUDIT",
                        description=(
                            f"<@{uid}> attempted to withdraw **{money(take)}** "
                            "from the Royal Investment Fund.\n"
                            "Unfortunately, the paperwork was unusually "
                            "legible.\n\n"
                            f"🔥 **{money(audit)}** confiscated and destroyed.\n"
                            f"💰 Net withdrawal: **{money(take - tax - audit)}**"
                            f"\n\nAuthorities confirm <@{auditor}> filed the "
                            "suspicious activity report."
                        ),
                        colour=_EMBED_RED,
                    ))

    # ── status card (§17) ─────────────────────────────────────────────

    async def _status_embeds(
        self, interaction: discord.Interaction, uid: str, player: dict,
        state: dict, holders: list[tuple[str, int]], note: Optional[str],
    ) -> list[discord.Embed]:
        risk = int(state["risk"])
        total = sum(a for _u, a in holders)
        guild = interaction.guild
        tax = ANTI_PANIC_TAX.get(risk, 0.0)
        share = (player["invested"] / total * 100) if total else 0.0

        main = discord.Embed(
            title="🏦 ROYAL INVESTMENT FUND",
            description=(
                (note + "\n\n" if note else "")
                + f"**Fund Value:** {money(total)}\n"
                + f"**Total Investors:** {len(holders)}\n"
                + f"**Exposure:** {RISK_METER[risk]} "
                  f"**{risk}/5 — {RISK_NAMES[risk]}**\n"
                + f"**Withdrawal Tax:** {tax * 100:.0f}%\n\n"
                + f"_{ROGER_STATUS[risk]} Events every {_interval_text(risk)}._"
                + _last_event_line(state)
            ),
            colour=RISK_COLOUR[risk],
        )
        async with self.conn.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM fund_ledger"
            " WHERE discord_user_id = ? AND lifecycle = ?"
            " AND kind IN ('event', 'cash')",
            (uid, int(state.get("lifecycle", 1))),
        ) as cur:
            mine = int((await cur.fetchone())[0])
        main.add_field(
            name="Your Position",
            value=(
                f"Investment: **{money(player['invested'])}**\n"
                f"Ownership: **{share:.1f}%**\n"
                f"Cash: {money(player['balance'])}\n"
                f"Profit / loss this fund: **{_delta(mine)}** · `/fundluck`"
            ),
            inline=False,
        )

        effects = _effect_lines(state)
        if effects:
            main.add_field(
                name="Active Effects", value="\n".join(effects), inline=False
            )

        # The whistleblower is the only way to see the machinery.
        if _active(state.get("leak_until")):
            pressure = int(state["collapse_pressure"])
            nxt = state.get("next_event_at")
            main.add_field(
                name="🕵️ LEAKED INTERNAL DOCUMENTS",
                value=(
                    f"Leaked Collapse Pressure: **+{pressure}%**\n"
                    f"Collapse chance next event: "
                    f"**{collapse_chance(risk, pressure) * 100:.1f}%**\n"
                    + (
                        f"Next event: <t:{int(_parse(nxt).timestamp())}:R>"
                        if nxt else "Next event: _scheduler idle_"
                    )
                ),
                inline=False,
            )

        recent = await self.recent_events()
        if recent:
            main.add_field(
                name="Recent Fund Events",
                value="\n".join(f"{e} {n} — {s}" for e, n, s in recent),
                inline=False,
            )
        main.set_footer(text=exposure_meter(risk))

        # §17: show *everybody*. Positions spill into a second embed rather
        # than silently truncating the list.
        embeds = [main]
        fields = _position_fields(holders, total, guild)
        overflow: list[tuple[str, str]] = []
        for i, (name, value) in enumerate(fields):
            if i == 0 and len(main) + len(name) + len(value) < 5800:
                main.add_field(name=name, value=value, inline=False)
            else:
                overflow.append((name, value))
        if overflow:
            extra = discord.Embed(colour=RISK_COLOUR[risk])
            for name, value in overflow:
                extra.add_field(name=name, value=value, inline=False)
            embeds.append(extra)
        return embeds


class WithdrawConfirmView(discord.ui.View):
    """§12: an explicit yes after the tax has been shown.

    Not persistent on purpose — a confirmation that outlives the bot would let
    somebody accept a tax rate that no longer applies.
    """

    def __init__(
        self, cog: "RoyalFundCog", uid: str, take: int, tax: int, risk: int
    ) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.uid = uid
        self.take = take
        self.tax = tax
        self.risk = risk

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.uid

    @discord.ui.button(label="Withdraw anyway", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        self.stop()
        await self.cog._do_withdraw(
            interaction, self.uid, self.take, self.tax, self.risk, followup=True
        )

    @discord.ui.button(label="Leave it in", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        await interaction.response.edit_message(
            content="Roger exhales.", embed=None, view=None
        )


# ── shared rendering helpers ──────────────────────────────────────────────────

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _position_fields(
    holders: list[tuple[str, int]], total: int,
    guild: Optional[discord.Guild],
) -> list[tuple[str, str]]:
    """All investor positions, chunked to stay under Discord's field limit.

    Everyone is named.  This list is posted publicly, so a viewer-relative
    label like "You" reads as *whoever is looking* — half the channel would
    see somebody else's position and think it was theirs.  The reader's own
    numbers already have their own field above.
    """
    lines = []
    for i, (uid, held) in enumerate(holders, 1):
        member = guild.get_member(int(uid)) if guild else None
        name = member.display_name if member else f"Prince {uid[-4:]}"
        prefix = _MEDALS.get(i, f"`{i:>2}.`")
        pct = (held / total * 100) if total else 0
        lines.append(f"{prefix} **{name}** — {money(held)} — {pct:.1f}%")
    if not lines:
        return [(
            "All Investor Positions",
            "_Nobody. Roger is bored, and a bored Roger is a dangerous Roger._",
        )]
    out: list[tuple[str, str]] = []
    chunk: list[str] = []
    for line in lines:
        if sum(len(x) + 1 for x in chunk) + len(line) > 1000:
            out.append((
                "All Investor Positions" if not out else "…continued",
                "\n".join(chunk),
            ))
            chunk = []
        chunk.append(line)
    if chunk:
        out.append((
            "All Investor Positions" if not out else "…continued",
            "\n".join(chunk),
        ))
    return out


def _effect_lines(state: dict) -> list[str]:
    """The timed effects currently running, with their remaining time."""
    out = []
    if _active(state.get("panic_until")):
        out.append(
            f"📞 Withdrawal Panic — "
            f"<t:{int(_parse(state['panic_until']).timestamp())}:R>"
        )
    if _active(state.get("campaign_until")):
        out.append(
            f"💳 Deposit Campaign ("
            f"{float(state.get('campaign_pct') or 0) * 100:.0f}% match) — "
            f"<t:{int(_parse(state['campaign_until']).timestamp())}:R>"
        )
    if _active(state.get("call_until")):
        out.append(
            f"🚨 Capital Call — {money(int(state.get('call_raised') or 0))} / "
            f"{money(int(state.get('call_target') or 0))} — "
            f"<t:{int(_parse(state['call_until']).timestamp())}:R>"
        )
    if _active(state.get("leak_until")):
        out.append(
            f"🕵️ Whistleblower — "
            f"<t:{int(_parse(state['leak_until']).timestamp())}:R>"
        )
    return out


def _last_event_line(state: dict) -> str:
    """When Roger last did something, and when he is due to again.

    Knowing the cadence is useless without knowing where you are in it — "an
    event every 20 minutes" tells you nothing if the last one was 19 minutes
    ago and you are about to be caught holding.
    """
    bits = []
    last = state.get("last_event_at")
    if last:
        ago = int((_now() - _parse(last)).total_seconds() // 60)
        last_id = state.get("last_event_id")
        ev = BY_ID.get(str(last_id or ""))
        name = f" ({ev['emoji']} {ev['name'].title()})" if ev else ""
        bits.append(
            "**Last event:** "
            + ("just now" if ago < 1 else
               f"{ago} minute{'s' if ago != 1 else ''} ago")
            + name
        )
    nxt = state.get("next_event_at")
    if nxt:
        try:
            bits.append(
                f"**Next event:** <t:{int(_parse(nxt).timestamp())}:R>"
            )
        except ValueError:
            pass
    return ("\n\n" + "\n".join(bits)) if bits else ""


def _advice(total: int, risk: int) -> str:
    """§18: Roger's context-sensitive investment advice."""
    if risk >= 5:
        return ("IF EVERYBODY JUST LEAVES THEIR MONEY WHERE IT IS, WE MAY ALL "
                "SURVIVE THIS.")
    if risk == 4:
        return "My professional advice is that nobody withdraw anything. Please."
    if risk == 3:
        return ("I remain confident. I have simply begun checking the "
                "spreadsheet much more often.")
    if total < 5_000:
        return ("This fund is embarrassingly empty. Are Nigeria's princes all "
                "pussies? Put some money in.")
    if total > 50_000:
        return "That is an alarming amount of money to have entrusted to one man."
    if total >= 10_000:
        return "A respectable amount of money. Finally something worth mismanaging."
    return "Money is arriving. Slowly. Like everything in this country."


async def setup(bot: commands.Bot, conn: aiosqlite.Connection) -> RoyalFundCog:
    cog = RoyalFundCog(bot, conn)
    await bot.add_cog(cog)
    return cog
