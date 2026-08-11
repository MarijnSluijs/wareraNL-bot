"""Quick scam templates — the rotating pool of pooled operations (v2).

A quick scam is not one generic pot with a random multiplier.  Every trigger
rolls a *template* out of this pool, and each one is its own little gambling
problem: different sign-up window, stake limits, odds, and — most importantly —
a different reason to think twice before joining.

The design goal is that players should not automatically want to buy into every
operation.  That only works if the participant count pulls in different
directions from template to template:

============================  =============================================
Template                      More participants…
============================  =============================================
Kangaroo / Cooperative / ING  hurt — extra investors attract scrutiny
Pyramid / Lolmanism           help — a crowd *is* the pitch
Diamond Rotation              help the odds, dilute the payout
Romance / Pharmaceutical      pay more, but blow up more often
Gouda / Battle Bounty         want a *specific* amount of money, not maximum
Offshore Amnesty              actively screw each other
============================  =============================================

**Outcomes.**  Every operation resolves into exactly one of four states, drawn
from a single roll:

``✅ Success``
    Then a second roll may upgrade it to ``🌟 Rare Success``.
``❌ Ordinary Failure``
    Probability is whatever is left over.  A severity roll (25% minor / 50%
    standard / 25% big) decides how much of the stake is lost.
``💥 Extreme Failure``
    A *direct* top-level probability, not a share of failures.  Everybody
    loses 100%, and then every participant — including free seats — rolls
    independently for arrest.

Everything here is data.  :mod:`nigeria_bot.scam_game` reads these dicts and
never hardcodes a number, so rebalancing after live testing means editing this
file and nothing else.

Payout multipliers **include the stake**: ``1.5x`` on 1.000 Naira returns 1.500
Naira, i.e. 500 profit.  Failure percentages are what you *lose*.
"""

from __future__ import annotations

import math
import random
from typing import Optional

import discord

# ── Rarity ────────────────────────────────────────────────────────────────────
# Share of all triggers that should land on each rarity.  Individual spawn
# weights are derived from these at import time, so adding a template to a
# rarity re-divides that rarity's share instead of inflating it.

RARITY_SHARE = {
    "common":   0.50,
    "uncommon": 0.32,
    "rare":     0.15,
    "special":  0.03,
}

RARITY_BADGE = {
    "common":   "⚪ Common",
    "uncommon": "🔵 Uncommon",
    "rare":     "🟣 Rare",
    "special":  "🌟 SPECIAL",
}

RARITY_COLOUR = {
    "common":   discord.Colour(0xBDC3C7),
    "uncommon": discord.Colour(0x3498DB),
    "rare":     discord.Colour(0x9B59B6),
    "special":  discord.Colour(0xF1C40F),
}

# Sign-up window by rarity.  A template may override it (the Rare tier is
# specified as "default 30, individual templates may use 20–40").
RARITY_SIGNUP = {"common": 10, "uncommon": 20, "rare": 30, "special": 60}

# Per-participant arrest chance when an operation ends in Extreme Failure.
RARITY_ARREST = {"common": 0.10, "uncommon": 0.20, "rare": 0.50, "special": 0.80}

# Anything above "rare" deserves a shout in the channel when it appears.
LOUD_RARITIES = ("rare", "special")

# Severity of an ordinary failure: how likely each band is.  The loss attached
# to each band is per-template.
SEVERITY = (
    ("minor",    "🟡 Minor failure",    0.25),
    ("standard", "🔴 Standard failure", 0.50),
    ("big",      "☠️ Big failure",      0.25),
)


# ── Template construction ─────────────────────────────────────────────────────

_REQUIRED = (
    "id", "emoji", "name", "rarity", "description", "risk",
    "min_stake", "max_stake", "max_participants",
    "base_chance", "extreme_chance", "fail_losses",
    "payout_min", "payout_max", "rare_chance", "rare_payout", "crowd_note",
    "success_message", "failure_message", "rare_success_message",
    "extreme_message",
)

_DEFAULTS = {
    # Sign-up minutes; None means "use the rarity default".
    "signup_minutes": None,
    # Per-participant arrest chance on Extreme Failure; None = rarity default.
    "arrest_chance": None,

    # ── success chance mechanics (a template uses at most one) ──
    # Absolute chance keyed by the participant count at which it starts.
    "chance_table": None,
    # (delta per participant beyond the first, cap) — a gentle crowd bonus.
    "chance_per_participant": None,
    # [(total invested, absolute chance), …]; the highest match wins.
    "funding_thresholds": None,
    # [(upper bound of band, chance), …]; the first band that contains the
    # total wins.  Used where there is a *sweet spot* rather than "more money
    # is better" — see the cheese.
    "funding_bands": None,
    "chance_cap": 0.95,

    # ── extreme failure mechanics ──
    # Absolute chance keyed by participant count at which it starts.
    "extreme_table": None,
    # [(total invested, chance), …]; the highest match wins.
    "extreme_funding": None,
    # The Dutch takeover fixes *ordinary* failure and gives everything else to
    # Extreme.  Nothing else should need this.
    "ordinary_fixed": None,

    # ── payout mechanics ──
    # Per-join-order multipliers (the pyramid): index 0 joined first.
    "payout_by_order": None,
    # Multiplier keyed by participant count (the diamond rotation).
    "payout_table": None,
    # (lo, hi) range keyed by the participant count at which it starts.
    "payout_bands": None,

    # ── free seats ──
    "free_entry": False,
    "free_success_chance": 0.0,
    "free_payout_min": 0,
    "free_payout_max": 0,
    "free_rare_min": 0,
    "free_rare_max": 0,

    # Charged to whoever triggers the operation; refunded on success when
    # `initiator_refund` is set.  Waived (along with the bonus) if they cannot
    # afford it, so being broke never blocks a trigger.
    "initiator_cost": 0,
    "initiator_refund": False,
    "initiator_bonus": 0,
    "initiator_bonus_note": None,
}


def _t(**kw) -> dict:
    missing = [f for f in _REQUIRED if f not in kw]
    if missing:
        raise ValueError(f"quick scam template {kw.get('id')!r} missing {missing}")
    unknown = [k for k in kw if k not in _REQUIRED and k not in _DEFAULTS]
    if unknown:
        raise ValueError(f"quick scam template {kw.get('id')!r} has unknown {unknown}")
    out = {**_DEFAULTS, **kw}
    if out["signup_minutes"] is None:
        out["signup_minutes"] = RARITY_SIGNUP[out["rarity"]]
    if out["arrest_chance"] is None:
        out["arrest_chance"] = RARITY_ARREST[out["rarity"]]
    return out


TEMPLATES: list[dict] = [

    # ══════════════════════════════════════════════════════════════════════════
    # ⚪ COMMON — quick, accessible, small-to-medium upside, real downside
    # ══════════════════════════════════════════════════════════════════════════

    _t(
        id="bicycle",
        emoji="🚲",
        name="Operation Bicycle Repatriation",
        rarity="common",
        risk="🟡 Low–Moderate",
        description=(
            "Previously undiscovered royal records prove that thousands of "
            "bicycles currently located in the Netherlands legally belong to "
            "Nigerian noble families.\n\n"
            "Funding is required to return these priceless cultural artefacts "
            "to their rightful owners."
        ),
        min_stake=100, max_stake=750, max_participants=8,
        base_chance=0.72,
        chance_table={1: 0.72, 6: 0.70, 7: 0.68, 8: 0.66},
        crowd_note=(
            "The first five investors are free. From the sixth onwards "
            "Rotterdam customs starts noticing the volume — every extra "
            "shipment costs 2% off the odds."
        ),
        payout_min=1.35, payout_max=1.60,
        extreme_chance=0.06,
        fail_losses=(0.25, 0.35, 0.55),
        rare_chance=0.04, rare_payout=2.75,
        success_message=(
            "A container containing 74 Dutch bicycles successfully leaves "
            "Rotterdam under the designation:\n\n"
            "**“Historical Royal Transportation Equipment.”**\n\n"
            "Nobody asks further questions."
        ),
        failure_message=(
            "Rotterdam customs asks why the “repatriated Nigerian cultural "
            "artefacts” still have Dutch locks, insurance stickers and several "
            "angry Dutch owners attached to them.\n\n"
            "Part of the shipment is detained and the operation takes a loss."
        ),
        rare_success_message=(
            "🚲 **CULTURAL RESTITUTION APPROVED**\n\n"
            "Dutch authorities officially recognise the bicycles as displaced "
            "Nigerian heritage. The shipment is released and transport costs "
            "are reimbursed."
        ),
        extreme_message=(
            "🚲💥 **OPERATION BICYCLE REPATRIATION HAS COLLAPSED**\n\n"
            "Dutch authorities discover that the supposed Nigerian royal "
            "archives were printed yesterday. Rotterdam customs seizes the "
            "entire shipment while Nigerian diplomats deny knowing what a "
            "bicycle is."
        ),
    ),
    _t(
        id="tikkie",
        emoji="📱",
        name="National Tikkie Recovery Program",
        rarity="common",
        risk="🟢 Low",
        description=(
            "The Council of Princes discovers that Dutch citizens collectively "
            "owe Nigeria an enormous amount in unpaid Tikkies.\n\n"
            "A mass recovery campaign begins."
        ),
        min_stake=50, max_stake=500, max_participants=15,
        base_chance=0.78,
        chance_per_participant=(0.01, 0.07),
        crowd_note=(
            "Every extra pair of hands sends more payment requests: **+1%** "
            "success per additional participant, up to +7%. Bring friends."
        ),
        free_entry=True,
        free_success_chance=0.30, free_payout_min=25, free_payout_max=150,
        free_rare_min=300, free_rare_max=300,
        payout_min=1.18, payout_max=1.40,
        extreme_chance=0.05,
        fail_losses=(0.15, 0.25, 0.45),
        rare_chance=0.02, rare_payout=3.25,
        success_message=(
            "Dutch citizens begin paying their overdue **Royal Administrative "
            "Tikkies** simply to make the notifications stop."
        ),
        failure_message=(
            "4.800 Tikkies are sent. Three are paid. Two of those were paid by "
            "members of the Council of Princes themselves.\n\n"
            "The recovery department reports a financial setback."
        ),
        rare_success_message=(
            "📱 **THE GREAT TIKKIE**\n\n"
            "Somebody accidentally sends a payment request directly to the "
            "Dutch treasury. An intern approves it. Nigeria withdraws the money "
            "before anyone notices."
        ),
        extreme_message=(
            "📱💥 **THE TIKKIE CAMPAIGN HAS BEEN TRACED**\n\n"
            "Thousands of suspicious payment requests attract Dutch financial "
            "investigators. Several accounts are frozen and the Council of "
            "Princes suddenly claims the campaign was “community outreach”."
        ),
    ),
    _t(
        id="polder",
        emoji="🌊",
        name="Nigerian Polder Initiative",
        rarity="common",
        risk="🟡 Moderate",
        description=(
            "Dutch investors are asked to finance Nigeria's first polder.\n\n"
            "The main economic argument is: *“The Netherlands has polders and "
            "the Netherlands is rich.”*"
        ),
        min_stake=100, max_stake=750, max_participants=10,
        base_chance=0.66,
        funding_thresholds=[(2_000, 0.71), (4_000, 0.76)],
        crowd_note=(
            "Dutch engineers respect a budget. **2.000 Naira** in the pot lifts "
            "the odds to 71%, **4.000** to 76%. Headcount is irrelevant — only "
            "the total matters."
        ),
        payout_min=1.35, payout_max=1.70,
        extreme_chance=0.07,
        fail_losses=(0.30, 0.45, 0.65),
        rare_chance=0.05, rare_payout=2.75,
        success_message=(
            "The feasibility report concludes that the Nigerian polder is "
            "unnecessary, impractical and “potentially innovative.”\n\n"
            "This is sufficient to secure Dutch funding."
        ),
        failure_message=(
            "Dutch engineers discover a minor technical problem:\n\n"
            "**Abuja is nowhere near the sea.**\n\n"
            "Construction is postponed and part of the budget disappears into "
            "consultancy costs."
        ),
        rare_success_message=(
            "🌊 **DUTCH CONSULTANCY MIRACLE**\n\n"
            "The project is reclassified as a *“Transformative "
            "Climate-Resilience Hydrological Pilot.”* Nobody knows what that "
            "means. The budget doubles."
        ),
        extreme_message=(
            "🌊💥 **THE POLDER HAS BECOME A FINANCIAL SINKHOLE**\n\n"
            "Dutch engineers arrive in Abuja and finally ask the question "
            "nobody considered during planning:\n\n"
            "*“Where exactly is the water?”*\n\n"
            "Auditors freeze the project immediately."
        ),
    ),
    _t(
        id="titles",
        emoji="👑",
        name="Royal Title Subscription Service",
        rarity="common",
        risk="🟢 Low",
        description=(
            "The Council of Princes begins selling entirely legitimate "
            "Nigerian nobility subscriptions.\n\n"
            "Available packages include **Prince Basic**, **Prince Premium**, "
            "**Grand Prince Gold** and **Platinum Prince+**.\n\n"
            "Diplomatic immunity sold separately."
        ),
        min_stake=50, max_stake=400, max_participants=15,
        base_chance=0.82,
        crowd_note="Headcount changes nothing. Everyone gets a certificate.",
        free_entry=True,
        free_success_chance=0.25, free_payout_min=25, free_payout_max=100,
        free_rare_min=250, free_rare_max=250,
        payout_min=1.15, payout_max=1.35,
        extreme_chance=0.05,
        fail_losses=(0.10, 0.20, 0.40),
        rare_chance=0.03, rare_payout=3.0,
        success_message=(
            "Eleven new European princes receive impressive-looking "
            "certificates before lunch.\n\nNobody checks whether the titles are "
            "legally recognised."
        ),
        failure_message=(
            "Several customers discover that **Prince Premium** does not "
            "actually include diplomatic immunity.\n\n"
            "The Council of Princes disconnects its customer-service line and "
            "refunds as little as possible."
        ),
        rare_success_message=(
            "👑 **ROYALTY BOOM**\n\n"
            "A minor European celebrity posts their Nigerian noble certificate "
            "online. Applications explode overnight."
        ),
        extreme_message=(
            "👑💥 **THE NOBILITY MARKET HAS COLLAPSED**\n\n"
            "Hundreds of newly created European princes simultaneously attempt "
            "to use their certificates for diplomatic immunity. Authorities "
            "begin asking where exactly the Grand Prince Platinum Office is "
            "located.\n\nThe Council deletes the customer database."
        ),
    ),
    _t(
        id="canada",
        emoji="🍁",
        name="Guaranteed Canadian Protection Package",
        rarity="common",
        risk="🟢 Low",
        description=(
            "Nigeria offers comprehensive military protection against the "
            "growing threat of Canadian invasion.\n\n"
            "Nobody has evidence Canada intends to invade. Nigeria considers "
            "this proof that the attack will be unexpected."
        ),
        min_stake=100, max_stake=600, max_participants=12,
        base_chance=0.84,
        crowd_note="Headcount changes nothing. The threat is equally imaginary either way.",
        free_entry=True,
        free_success_chance=0.0,
        free_rare_min=100, free_rare_max=250,
        payout_min=1.12, payout_max=1.30,
        extreme_chance=0.06,
        fail_losses=(0.15, 0.25, 0.50),
        rare_chance=0.03, rare_payout=4.5,
        success_message=(
            "The customer agrees that the lack of an immediate Canadian threat "
            "is exactly why preparations must begin now."
        ),
        failure_message=(
            "The prospective client checks a map. Canada appears to be several "
            "thousand kilometres away.\n\nThe protection agreement is cancelled."
        ),
        rare_success_message=(
            "🍁🚨 **CANADIAN EMERGENCY**\n\n"
            "A Canadian player posts a vaguely threatening Discord message. "
            "Nigeria presents this as undeniable proof that its intelligence "
            "assessment was correct. Demand explodes."
        ),
        extreme_message=(
            "🍁💥 **CANADA HAS RESPONDED**\n\n"
            "The Canadian embassy discovers that Nigeria has been selling "
            "military protection against a nonexistent Canadian invasion. "
            "Explaining that the threat was invented does not improve the "
            "situation.\n\n**All protection premiums are seized.**"
        ),
    ),
    _t(
        id="vlk",
        emoji="🐸",
        name="Voet Likkende Kikker Partij Election Fund",
        rarity="common",
        risk="🟢 Low",
        description=(
            "Nigerian campaign consultants approach members of the Voet "
            "Likkende Kikker Partij and ask them to donate to a completely "
            "legitimate fundraising drive for the party's next election.\n\n"
            "Fortunately, financial due diligence is not considered a core "
            "party value."
        ),
        min_stake=50, max_stake=600, max_participants=12,
        base_chance=0.80,
        chance_per_participant=(0.005, 0.04),
        crowd_note=(
            "🐸 A bigger congregation looks more legitimate: **+1%** success "
            "for every two extra donors, up to 84%."
        ),
        payout_min=1.25, payout_max=1.50,
        extreme_chance=0.05,
        fail_losses=(0.20, 0.35, 0.55),
        rare_chance=0.04, rare_payout=2.75,
        success_message=(
            "VLK supporters enthusiastically donate to the **“National "
            "Amphibious Election Victory Fund.”**\n\n"
            "Nobody asks who registered the bank account."
        ),
        failure_message=(
            "A party member finally asks why every campaign donation is being "
            "transferred to Abuja.\n\n"
            "The fundraising page is temporarily taken offline."
        ),
        rare_success_message=(
            "🐸 **THE FROG WAVE**\n\n"
            "Lolman publicly endorses the fundraising drive without remembering "
            "who organised it. Donations triple overnight."
        ),
        extreme_message=(
            "🐸💥 **ELECTION FINANCE HAS NOTICED**\n\n"
            "Regulators trace the campaign account directly to the Council of "
            "Princes. The fundraising platform freezes everything and party "
            "officials suddenly claim they have never heard of Nigeria."
        ),
    ),
    _t(
        id="statiegeld",
        emoji="♻️",
        name="Royal Statiegeld Recovery Program",
        rarity="common",
        risk="🟢 Low",
        description=(
            "Nigeria discovers that millions of Dutch bottles and cans "
            "technically contain unclaimed Nigerian recyclable assets.\n\n"
            "The Council of Princes launches a cross-border statiegeld "
            "recovery operation."
        ),
        min_stake=50, max_stake=500, max_participants=12,
        base_chance=0.79,
        chance_table={1: 0.79, 5: 0.82, 9: 0.84},
        crowd_note=(
            "♻️ More hands, more bottles: 79% up to four collectors, 82% from "
            "five, 84% from nine."
        ),
        payout_min=1.20, payout_max=1.45,
        extreme_chance=0.05,
        fail_losses=(0.15, 0.30, 0.50),
        rare_chance=0.03, rare_payout=3.0,
        success_message=(
            "Truckloads of bottles pass through Dutch deposit machines and the "
            "tiny payments begin adding up surprisingly quickly."
        ),
        failure_message=(
            "Several supermarket machines reject the returned bottles as "
            "“unrecognised international royal property.”\n\n"
            "A disappointing amount of plastic returns to Nigeria."
        ),
        rare_success_message=(
            "♻️ **THE GREAT STATIEGELD LOOPHOLE**\n\n"
            "A software error allows the same batch of bottles to be processed "
            "twice before anyone notices.\n\nReturns explode."
        ),
        extreme_message=(
            "♻️💥 **EVERY BARCODE IS IDENTICAL**\n\n"
            "A supermarket employee notices that 4.000 supposedly different "
            "bottles all carry the same barcode.\n\n"
            "The recycling network is frozen and investigators seize the "
            "recovery fund."
        ),
    ),
    _t(
        id="marktplaats",
        emoji="📦",
        name="Marktplaats Shipping Insurance Scheme",
        rarity="common",
        risk="🟡 Moderate",
        description=(
            "A collection of suspiciously cheap Marktplaats goods is repeatedly "
            "reported as “lost during international shipment to Nigeria.”\n\n"
            "Insurance claims follow."
        ),
        min_stake=100, max_stake=800, max_participants=4,
        base_chance=0.72,
        chance_table={1: 0.72, 3: 0.68, 4: 0.62},
        crowd_note=(
            "⚠️ **Only four seats, and they get worse.** Two claimants or "
            "fewer: 72%. A third drops it to 68%, a fourth to 62% — "
            "simultaneous claims make the pattern easier to spot."
        ),
        payout_min=1.40, payout_max=1.75,
        extreme_chance=0.07,
        fail_losses=(0.30, 0.45, 0.70),
        rare_chance=0.05, rare_payout=3.0,
        success_message=(
            "The package is declared lost somewhere between Utrecht and Lagos. "
            "The insurer pays before anyone asks why the tracking number never "
            "existed."
        ),
        failure_message=(
            "The insurer requests shipping proof for a bicycle that appears to "
            "have been sold three times in the same afternoon.\n\n"
            "Part of the claim is rejected."
        ),
        rare_success_message=(
            "📦 **DOUBLE REFUND**\n\n"
            "Both the seller and the insurer independently issue compensation "
            "for the same missing item.\n\nNobody communicates with anyone else "
            "in time to stop the payout."
        ),
        extreme_message=(
            "📦💥 **THE CLAIMS HAVE BEEN LINKED**\n\n"
            "Fraud investigators discover that every “lost shipment” used the "
            "same forwarding address and nearly identical paperwork.\n\n"
            "All claims are frozen."
        ),
    ),
    _t(
        id="romance",
        emoji="❤️",
        name="Nigerian Princess Romance Operation",
        rarity="common",
        risk="🟡 Variable",
        description=(
            "Nigerian princes create profiles pretending to be beautiful "
            "Nigerian princesses and begin courting Dutch WarEra players who "
            "have not left their basements since the previous election cycle.\n\n"
            "Eventually there is only one problem:\n\n"
            "*“My prince, I only need money for the flight ticket ❤️”*"
        ),
        min_stake=100, max_stake=700, max_participants=8,
        base_chance=0.74,
        crowd_note=(
            "❤️ **More princesses, more money — and more evidence.** The odds "
            "never move, but payouts climb with the crowd (×1.35–1.55 solo up "
            "to ×1.75–2.10 at seven) and so does catastrophe: 5% at two, 10% "
            "at eight."
        ),
        payout_bands={1: (1.35, 1.55), 3: (1.45, 1.70), 5: (1.60, 1.90),
                      7: (1.75, 2.10)},
        payout_min=1.35, payout_max=2.10,
        extreme_chance=0.05,
        extreme_table={1: 0.05, 3: 0.06, 5: 0.08, 7: 0.10},
        fail_losses=(0.25, 0.40, 0.65),
        rare_chance=0.04, rare_payout=3.5,
        success_message=(
            "A lovestruck Dutch player transfers money for a flight ticket, "
            "visa processing and one extremely believable airport fee.\n\n"
            "The princess promises to arrive very soon."
        ),
        failure_message=(
            "One romantic target requests a live video call.\n\n"
            "The “princess” claims the palace webcam is temporarily undergoing "
            "royal maintenance."
        ),
        rare_success_message=(
            "❤️💰 **LOVE-STRUCK WHALE**\n\n"
            "One target pays for business class, visa fees, palace luggage and "
            "a “temporary royal security deposit.”\n\n"
            "He asks whether the wedding can be next week."
        ),
        extreme_message=(
            "📺🚨 **THE ROMANCE OPERATION HAS BEEN EXPOSED**\n\n"
            "A Dutch investigative television crew arrives in Abuja after "
            "several victims compare screenshots.\n\n"
            "Unfortunately, eight different princesses used the same payment "
            "account."
        ),
    ),
    _t(
        id="proxy_survival",
        emoji="🇱🇺",
        name="Proxy Survival Consultancy",
        rarity="common",
        risk="🟢 Low",
        description=(
            "Nigerian experts offer Dutch players stuck in Luxembourg a "
            "professional consultancy package explaining how to successfully "
            "receive support from the Netherlands while living in a proxy.\n\n"
            "The report contains:\n"
            "1. Ask politely.\n2. Wait.\n3. Ask again.\n"
            "4. Watch the Netherlands fight somewhere else."
        ),
        min_stake=100, max_stake=600, max_participants=8,
        base_chance=0.76,
        chance_per_participant=(0.01, 0.06),
        crowd_note=(
            "A bigger client list makes the consultancy look real: **+1%** per "
            "additional participant, up to 82%."
        ),
        payout_min=1.30, payout_max=1.60,
        extreme_chance=0.05,
        fail_losses=(0.25, 0.40, 0.60),
        rare_chance=0.05, rare_payout=2.8,
        success_message=(
            "The consultancy successfully teaches Luxembourg how to draft "
            "twelve increasingly polite support requests and remain optimistic "
            "throughout."
        ),
        failure_message=(
            "Luxembourg follows the consultancy advice exactly.\n\n"
            "Dutch support still does not arrive.\n\n"
            "Clients begin requesting a partial refund."
        ),
        rare_success_message=(
            "🇱🇺🚨 **DUTCH SUPPORT ACTUALLY ARRIVES**\n\n"
            "Nobody had modelled this possibility.\n\n"
            "Demand for Nigerian proxy consultancy immediately explodes."
        ),
        extreme_message=(
            "🇱🇺💥 **THE CONSULTANCY HAS BEEN LEAKED**\n\n"
            "The full report reaches Dutch officials, including the appendix "
            "titled “What To Do When The Netherlands Ignores You Again.”\n\n"
            "Clients panic, accounts are frozen and everybody asks for their "
            "money back."
        ),
    ),
    _t(
        id="gouda",
        emoji="🧀",
        name="Gouda Cheese Investment Fund",
        rarity="common",
        risk="🟡 Moderate",
        description=(
            "A Nigerian investment vehicle begins stockpiling Gouda after "
            "internal research reaches a groundbreaking conclusion:\n\n"
            "*“Dutch people like cheese.”*\n\nTherefore cheese prices can only "
            "go up."
        ),
        min_stake=100, max_stake=800, max_participants=8,
        base_chance=0.62,
        funding_bands=[(1_500, 0.62), (3_500, 0.78), (5_000, 0.70),
                       (math.inf, 0.60)],
        crowd_note=(
            "🧀 **There is a right amount of cheese.** Under 1.500 in the pot "
            "is 62%; **1.500–3.500 is the sweet spot at 78%**; 3.501–5.000 "
            "falls to 70%; above 5.000 drops to 60% *and* raises catastrophe "
            "from 6% to 8%.\n"
            "Piling in more money can make this operation worse for everyone."
        ),
        payout_min=1.35, payout_max=1.75,
        extreme_chance=0.06,
        extreme_funding=[(5_000, 0.08)],
        fail_losses=(0.30, 0.50, 0.75),
        rare_chance=0.05, rare_payout=3.5,
        success_message=(
            "A temporary Gouda shortage pushes prices upward and the Nigerian "
            "cheese reserve is sold at a respectable profit."
        ),
        failure_message=(
            "The investment prospectus omitted refrigeration, storage and the "
            "minor issue that cheese does not appreciate forever.\n\n"
            "Margins melt rapidly."
        ),
        rare_success_message=(
            "🧀📈 **THE GREAT CHEESE SQUEEZE**\n\n"
            "A completely inexplicable Gouda shortage sends prices through the "
            "roof.\n\nNigeria briefly becomes a major cheese power."
        ),
        extreme_message=(
            "🧀💥 **THE CHEESE RESERVE HAS TURNED**\n\n"
            "Refrigeration fails in the main warehouse during an unusually warm "
            "week.\n\nThe investment is now technically still measured in "
            "tonnes, but nobody considers it an asset anymore."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # 🔵 UNCOMMON — a meaningful gamble with stronger mechanics
    # ══════════════════════════════════════════════════════════════════════════

    _t(
        id="kangaroo",
        emoji="🦘",
        name="Abuja Kangaroo Investment Fund",
        rarity="uncommon",
        risk="🟠 High",
        description=(
            "Dutch President Naud Muscater receives an investment proposal to "
            "establish the world's premier kangaroo breeding industry outside "
            "Abuja.\n\nThe business plan cites Australia fifteen times."
        ),
        min_stake=250, max_stake=2_000, max_participants=6,
        base_chance=0.48,
        chance_table={1: 0.48, 4: 0.44, 5: 0.39, 6: 0.33},
        crowd_note=(
            "⚠️ **A crowd is dangerous here.** Three investors or fewer keeps "
            "the odds at 48%; the fourth drops it to 44%, the fifth to 39%, the "
            "sixth to 33%. Too many investors, too much Dutch scrutiny."
        ),
        payout_min=1.90, payout_max=2.70,
        extreme_chance=0.09,
        fail_losses=(0.45, 0.65, 0.85),
        rare_chance=0.05, rare_payout=4.5,
        success_message=(
            "Naud approves the first stage of the Abuja Kangaroo Development "
            "Program.\n\nNobody has yet asked where Nigeria plans to acquire the "
            "kangaroos."
        ),
        failure_message=(
            "Naud finally asks to inspect the kangaroo breeding facility. "
            "Investigators find an empty field, three fences and a suspiciously "
            "Australian-looking PowerPoint presentation."
        ),
        rare_success_message=(
            "🦘 **MARSUPIAL ECONOMIC MIRACLE**\n\n"
            "The Dutch government classifies the farm as sustainable African "
            "agricultural innovation. Major development funding is approved "
            "before anyone visits Abuja."
        ),
        extreme_message=(
            "🦘💥 **THE KANGAROO FRAUD HAS BEEN EXPOSED**\n\n"
            "A Dutch inspection delegation arrives at the Abuja breeding "
            "facility. They discover three fences, an empty field and exactly "
            "zero kangaroos.\n\nSubsidy-fraud investigators raid the project."
        ),
    ),
    _t(
        id="ing",
        emoji="🏦",
        name="The Frozen ING Royal Account",
        rarity="uncommon",
        risk="🔴 Very High",
        description=(
            "A forgotten Nigerian royal account containing millions has "
            "supposedly been discovered at ING.\n\n"
            "Unfortunately, the account requires several final, extremely final "
            "and absolutely last administration fees."
        ),
        min_stake=300, max_stake=2_500, max_participants=4,
        base_chance=0.38,
        chance_table={1: 0.38, 3: 0.33, 4: 0.26},
        crowd_note=(
            "⚠️ Two investors is the sweet spot (38%). A third drops it to 33%, "
            "a fourth to 26% — ING starts counting the intermediaries."
        ),
        payout_min=2.20, payout_max=3.40,
        extreme_chance=0.10,
        fail_losses=(0.65, 0.80, 0.95),
        initiator_cost=150, initiator_bonus=300,
        initiator_bonus_note="150 Naira processing cost up front, 300 back on success.",
        rare_chance=0.05, rare_payout=5.5,
        success_message=(
            "ING accepts `FINAL_FINAL_ROYAL_VERIFICATION_v7.pdf`.\n\n"
            "The royal account is released."
        ),
        failure_message=(
            "ING informs you that legitimate banking procedures usually do not "
            "require seventeen intermediary accounts and a Telegram contact "
            "named **OfficialBankManager2**.\n\nThe account remains frozen."
        ),
        rare_success_message=(
            "🏦 **ACCOUNTING MIRACLE**\n\n"
            "ING accidentally releases the royal account *and* another account "
            "with almost the same name. Nobody notices until both have reached "
            "Lagos."
        ),
        extreme_message=(
            "🏦💥 **ING FRAUD CONTROL HAS ENTERED THE CHAT**\n\n"
            "`FINAL_FINAL_ROYAL_VERIFICATION_v7.pdf` does not survive "
            "professional scrutiny. Every intermediary account is frozen and "
            "transaction logs are handed directly to investigators."
        ),
    ),
    _t(
        id="proxy",
        emoji="📜",
        name="Dutch Proxy Independence Consultancy",
        rarity="uncommon",
        risk="🟡 Moderate",
        description=(
            "Nigeria sells an expensive consultancy report proving beyond "
            "reasonable doubt that Nigeria is definitely not a Dutch proxy."
        ),
        min_stake=250, max_stake=1_500, max_participants=6,
        base_chance=0.56,
        crowd_note="Headcount changes nothing. The report says what it says.",
        payout_min=1.65, payout_max=2.15,
        extreme_chance=0.07,
        fail_losses=(0.35, 0.50, 0.70),
        initiator_cost=250, initiator_refund=True, initiator_bonus=300,
        initiator_bonus_note=(
            "250 Naira setup costs up front — returned on success, plus a "
            "300 Naira consultancy fee."
        ),
        rare_chance=0.06, rare_payout=3.5,
        success_message=(
            "The final 97-page report proves Nigeria's complete independence.\n\n"
            "Primary evidence: *“Nigeria independently decided to accept Dutch "
            "money for this report.”*"
        ),
        failure_message=(
            "Reviewers discover a sentence stating: *“Nigeria should obtain "
            "Dutch permission before taking independent action.”*\n\n"
            "The report is withdrawn."
        ),
        rare_success_message=(
            "📜 **FULL INDEPENDENCE CERTIFIED**\n\n"
            "International observers issue Nigeria an official **DEFINITELY NOT "
            "A PROXY** certificate.\n\nIt was printed in Amsterdam."
        ),
        extreme_message=(
            "📜💥 **THE INDEPENDENCE REPORT HAS LEAKED**\n\n"
            "Investigators discover an internal draft containing the line:\n\n"
            "*“Please send to the Dutch government for approval before "
            "publication.”*\n\n"
            "The consultancy accounts are frozen. Nigeria immediately releases "
            "a statement confirming this proves its independence."
        ),
    ),
    _t(
        id="pyramid",
        emoji="🔺",
        name="Egyptian Pyramid Investment Opportunity",
        rarity="uncommon",
        risk="🟠 High",
        description=(
            "Nigeria presents Egypt with a revolutionary financial structure in "
            "which earlier investors benefit from later investors.\n\n"
            "The diagram happens to be triangular."
        ),
        min_stake=100, max_stake=1_500, max_participants=6,
        base_chance=0.28,
        chance_table={1: 0.28, 2: 0.43, 3: 0.56, 4: 0.66, 5: 0.73, 6: 0.77},
        crowd_note=(
            "🔺 **Join order decides your payout.** More investors make the "
            "scheme far more likely to work (28% solo → 77% at six), but the "
            "returns shrink as you go down the pyramid: "
            "×2.4 · ×2.0 · ×1.7 · ×1.45 · ×1.25 · ×1.10.\n"
            "Get in early, or get in safe. Not both."
        ),
        payout_by_order=[2.4, 2.0, 1.7, 1.45, 1.25, 1.10],
        payout_min=1.10, payout_max=2.4,
        extreme_chance=0.08,
        fail_losses=(0.50, 0.70, 0.90),
        rare_chance=0.05, rare_payout=1.0,   # +1.0x on top of each order tier
        success_message=(
            "Egyptian financiers admit that although the structure is clearly a "
            "pyramid scheme, it is at least a *well-organised* pyramid "
            "scheme.\n\nEarly investors receive their returns."
        ),
        failure_message=(
            "Egyptian investors take one look at the diagram.\n\n"
            "Nigeria is accused of stealing several thousand years of Egyptian "
            "intellectual property."
        ),
        rare_success_message=(
            "🔺 **PYRAMID PERFECTION**\n\n"
            "Egyptian experts apply actual pyramid mathematics to the scheme. "
            "Somehow this improves the returns dramatically."
        ),
        extreme_message=(
            "🔺💥 **EGYPT HAS RECOGNISED THE STRUCTURE**\n\n"
            "Unfortunately, they recognise it slightly too well. Egyptian "
            "financial regulators raid the operation for operating an "
            "unauthorised pyramid.\n\n"
            "Nigeria protests that Egypt cannot simultaneously own the "
            "intellectual property and outlaw its use."
        ),
    ),
    _t(
        id="cooperative",
        emoji="🌿",
        name="Totally Legit Agricultural Cooperative",
        rarity="uncommon",
        risk="🔴 Very High",
        description=(
            "The Totally Legit Party opens investment in several hectares of "
            "highly profitable **Mysterious Plants**.\n\n"
            "The prospectus refuses to identify the crop."
        ),
        min_stake=200, max_stake=2_000, max_participants=6,
        base_chance=0.45,
        chance_table={1: 0.45, 4: 0.40, 5: 0.34, 6: 0.27},
        crowd_note=(
            "⚠️ **A crowd is dangerous here.** Up to three investors: 45%. The "
            "fourth drops it to 40%, the fifth to 34%, the sixth to 27%. More "
            "investors, more unwanted attention."
        ),
        payout_min=2.00, payout_max=3.00,
        extreme_chance=0.10,
        fail_losses=(0.60, 0.80, 0.95),
        initiator_bonus=250,
        initiator_bonus_note="250 Naira on success for TLP Administrative Services.",
        rare_chance=0.05, rare_payout=5.0,
        success_message=(
            "The harvest of Mysterious Plants is purchased by an equally "
            "mysterious foreign customer.\n\nNobody asks further questions."
        ),
        failure_message=(
            "Authorities ask the Totally Legit Party exactly what is being "
            "cultivated.\n\nThe official response: *“Agricultural products.”*\n\n"
            "This does not satisfy them."
        ),
        rare_success_message=(
            "🌿 **UNEXPECTED PHARMACEUTICAL DEMAND**\n\n"
            "An international company offers to purchase the entire harvest at "
            "an extraordinary price. Everyone agrees not to identify the plants."
        ),
        extreme_message=(
            "🌿💥 **THE AGRICULTURAL COOPERATIVE HAS BEEN RAIDED**\n\n"
            "Inspectors finally demand a precise answer to the question:\n"
            "*“What exactly are these plants?”*\n\n"
            "The Totally Legit Party's response of “agricultural ones” fails to "
            "satisfy them.\n\n"
            "**The entire harvest and all project funds are seized.**"
        ),
    ),
    _t(
        id="landswap",
        emoji="🗺️",
        name="Strategic Land Swap Nobody Understands",
        rarity="uncommon",
        risk="🟠 High",
        description=(
            "Nigeria proposes a multinational territorial exchange involving "
            "Nigeria, Egypt, Libya, Cyprus, South Sudan and several countries "
            "that have not yet been informed.\n\n"
            "The official diagram contains seventeen arrows and the words "
            "**TRUST THE PROCESS**."
        ),
        min_stake=250, max_stake=2_000, max_participants=8,
        base_chance=0.48,
        funding_thresholds=[(3_000, 0.53), (6_000, 0.58), (10_000, 0.63)],
        crowd_note=(
            "Diplomacy runs on budget, not headcount. **3.000** in the pot "
            "lifts the odds to 53%, **6.000** to 58%, **10.000** to 63%."
        ),
        payout_min=1.70, payout_max=2.30,
        extreme_chance=0.08,
        fail_losses=(0.45, 0.65, 0.85),
        rare_chance=0.05, rare_payout=4.0,
        success_message=(
            "Every participating country signs the agreement.\n\n"
            "Nobody knows exactly who now owns what. Nigeria declares victory."
        ),
        failure_message=(
            "A diplomat asks somebody to explain the seventeen arrows.\n\n"
            "Nobody can. Talks collapse."
        ),
        rare_success_message=(
            "🗺️ **GEOPOLITICAL GENIUS**\n\n"
            "Every participating country believes it gained territory. "
            "Independent cartographers refuse to comment.\n\n"
            "Nigeria republishes the map with the caption **TRUST THE PROCESS**."
        ),
        extreme_message=(
            "🗺️💥 **NOBODY TRUSTED THE PROCESS**\n\n"
            "A diplomat finally follows all seventeen arrows on the official "
            "map. Three countries discover they have apparently traded the same "
            "territory to each other.\n\n"
            "Emergency talks begin and every project account is frozen."
        ),
    ),
    _t(
        id="debt",
        emoji="💰",
        name="Nigerian Prince Debt Consolidation Scheme",
        rarity="uncommon",
        risk="🟠 High",
        description=(
            "The Council of Princes finally proposes a solution to Nigeria's "
            "outstanding debts:\n\n"
            "Take out one enormous new loan and use it to repay all the smaller "
            "old loans."
        ),
        min_stake=200, max_stake=2_500, max_participants=8,
        base_chance=0.55,
        chance_table={1: 0.55, 4: 0.45, 7: 0.32},
        crowd_note=(
            "⚠️ Up to three creditors: 55%. Four to six: 45%. Seven or eight: "
            "32%. Every extra creditor is another person who might read the "
            "terms."
        ),
        payout_min=1.70, payout_max=2.50,
        extreme_chance=0.09,
        fail_losses=(0.55, 0.75, 0.95),
        initiator_bonus=300,
        initiator_bonus_note="300 Naira on success for arranging the facility.",
        rare_chance=0.04, rare_payout=4.2,
        success_message=(
            "Several old debts are successfully repaid using one significantly "
            "larger new debt.\n\nThe Council celebrates a historic reduction in "
            "the number of outstanding loans."
        ),
        failure_message=(
            "The Council discovers a technical issue:\n\n"
            "**The new loan also needs to be repaid.**\n\n"
            "A second debt-consolidation proposal is immediately commissioned."
        ),
        rare_success_message=(
            "💰 **DEBT NEUTRALISATION ACHIEVED**\n\n"
            "So many debts are consolidated that nobody can determine who still "
            "owes whom. Several creditors simply stop asking."
        ),
        extreme_message=(
            "💰💥 **ALL CREDITORS HAVE CALLED AT ONCE**\n\n"
            "The Council successfully consolidated every existing debt into one "
            "enormous new loan. Unfortunately, the new lender immediately "
            "requests repayment.\n\nSo do all the old lenders.\n\n"
            "**Every available account is frozen.**"
        ),
    ),
    _t(
        id="bounty",
        emoji="💥",
        name="Definitely Real Battle Bounty Fund",
        rarity="uncommon",
        risk="🟠 High",
        description=(
            "Nigeria informs Dutch citizens that the enormous São Tomé and "
            "Príncipe vs Madagascar War will definitely begin tonight.\n\n"
            "Probably.\n\n"
            "Donations are urgently required to fund a Dutch bounty. All money "
            "is promised to be funnelled directly back to Dutch fighters."
        ),
        min_stake=250, max_stake=2_000, max_participants=10,
        base_chance=0.45,
        funding_bands=[(2_000, 0.45), (5_000, 0.55), (math.inf, 0.65)],
        crowd_note=(
            "💥 A bigger bounty is more believable: under 2.000 is 45%, "
            "2.000–4.999 is 55%, 5.000+ is 65%.\n"
            "**But above 8.000 the catastrophe risk jumps from 8% to 12%** — "
            "that much money attracts the wrong attention."
        ),
        payout_min=1.80, payout_max=2.50,
        extreme_chance=0.08,
        extreme_funding=[(8_000, 0.12)],
        fail_losses=(0.45, 0.65, 0.90),
        rare_chance=0.05, rare_payout=4.0,
        success_message=(
            "Dutch citizens donate enthusiastically to the promised bounty fund "
            "before checking whether São Tomé and Madagascar are actually "
            "fighting."
        ),
        failure_message=(
            "Several donors open WarEra and discover there is currently no "
            "battle.\n\nNigeria explains that the battle is “strategically "
            "delayed.”\n\nThis is not fully convincing."
        ),
        rare_success_message=(
            "💥⚔️ **THE BATTLE ACTUALLY STARTED**\n\n"
            "Against all expectations, fighting really does break out. The "
            "previously fake bounty suddenly looks visionary and donations "
            "surge."
        ),
        extreme_message=(
            "💥🚨 **THERE WAS NEVER A BATTLE**\n\n"
            "Donors compare screenshots, contact the Dutch government and "
            "discover that the entire “historic war” existed only in a Nigerian "
            "announcement.\n\nThe bounty fund is seized."
        ),
    ),
    _t(
        id="belgianroad",
        emoji="🇧🇪",
        name="Nigerian Belgian Road Improvement Initiative",
        rarity="uncommon",
        risk="🟠 High",
        description=(
            "Belgian investors are offered access to Nigeria's world-renowned "
            "expertise in road construction.\n\n"
            "This expertise appears to come primarily from having seen several "
            "roads before.\n\n"
            "A consortium of Nigerian consultancy and building firms promises "
            "to finally fix Belgium's roads."
        ),
        min_stake=500, max_stake=2_500, max_participants=6,
        base_chance=0.58,
        funding_thresholds=[(3_000, 0.63), (7_000, 0.68)],
        crowd_note=(
            "🚧 More funding lets the consortium hire increasingly real "
            "engineers: **3.000** in the pot lifts the odds to 63%, **7.000** "
            "to 68%."
        ),
        payout_min=1.70, payout_max=2.30,
        extreme_chance=0.08,
        fail_losses=(0.40, 0.60, 0.80),
        rare_chance=0.05, rare_payout=3.8,
        success_message=(
            "A Belgian municipality approves the consultancy contract after "
            "deciding that literally any new road strategy is worth trying."
        ),
        failure_message=(
            "Belgian officials request examples of successful Nigerian road "
            "projects.\n\nThe consultancy responds with several photographs "
            "downloaded from another country."
        ),
        rare_success_message=(
            "🚧 **BELGIUM HAS A SMOOTH ROAD**\n\n"
            "One completed section contains no potholes whatsoever.\n\n"
            "International media arrive to document the unprecedented "
            "infrastructure event."
        ),
        extreme_message=(
            "🚧💥 **THE CONSTRUCTION CONSORTIUM HAS BEEN INSPECTED**\n\n"
            "Belgian inspectors discover that the Nigerian building firm owns "
            "no road equipment, no asphalt plant and one wheelbarrow.\n\n"
            "Fraud prosecutors freeze the project."
        ),
    ),
    _t(
        id="pharma",
        emoji="💊",
        name="Rotterdam Pharmaceutical Logistics Partnership",
        rarity="uncommon",
        risk="🔴 Very High",
        description=(
            "The Totally Legit Party partners with Aap Industries to move "
            "completely legitimate pharmaceutical products through Rotterdam "
            "harbour.\n\nNobody needs to know what the pills contain."
        ),
        min_stake=500, max_stake=2_500, max_participants=6,
        base_chance=0.58,
        crowd_note=(
            "💊 **More hands move more merchandise.** Payouts climb with the "
            "crowd (×1.80–2.20 at two, ×2.20–2.80 at six) — and so does the "
            "chance customs opens a container: 8% → 10% → 12%."
        ),
        payout_bands={1: (1.80, 2.20), 3: (2.00, 2.50), 5: (2.20, 2.80)},
        payout_min=1.80, payout_max=2.80,
        extreme_chance=0.08,
        extreme_table={1: 0.08, 3: 0.10, 5: 0.12},
        fail_losses=(0.55, 0.75, 0.95),
        rare_chance=0.05, rare_payout=4.5,
        success_message=(
            "The containers pass through Rotterdam under routine pharmaceutical "
            "paperwork and Aap Industries distributes the cargo before anyone "
            "asks difficult questions."
        ),
        failure_message=(
            "Rotterdam customs delays the shipment and requests pharmaceutical "
            "documentation.\n\nAap Industries produces a folder labelled "
            "**“Definitely Medicines.”**\n\nThe shipment remains under review."
        ),
        rare_success_message=(
            "💊📈 **UNEXPECTED PHARMACEUTICAL DEMAND**\n\n"
            "A sudden shortage sends wholesale prices upward just as the "
            "shipment clears the harbour.\n\nThe operation becomes enormously "
            "profitable."
        ),
        extreme_message=(
            "💊🚨 **ROTTERDAM CUSTOMS HAS OPENED THE CONTAINER**\n\n"
            "Aap Industries denies ownership. The Totally Legit Party denies "
            "knowing Aap Industries. The Nigerian government denies knowing "
            "what pills are.\n\nNobody finds these statements convincing."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # 🟣 RARE — event-like, high variance, often socially strategic
    # ══════════════════════════════════════════════════════════════════════════

    _t(
        id="turtle",
        emoji="🐢",
        name="Strategic Turtle Reserve",
        rarity="rare",
        risk="🟡 Deceptively Safe",
        signup_minutes=40,
        description=(
            "Nigeria has quietly accumulated a strategic reserve of valuable "
            "turtles.\n\nExperts predict a major international turtle shortage. "
            "Nobody knows what strategic role turtles perform."
        ),
        min_stake=250, max_stake=2_000, max_participants=8,
        base_chance=0.78,
        crowd_note=(
            "🐢 Headcount changes nothing. This is by far the safest Rare — but "
            "it still carries **Rare-tier consequences**: a catastrophe here "
            "means a 50% chance of arrest, same as the others."
        ),
        payout_min=1.15, payout_max=1.35,
        extreme_chance=0.08,
        fail_losses=(0.25, 0.50, 0.75),
        rare_chance=0.08, rare_payout=6.5,
        success_message=(
            "Mild international turtle demand produces a small but respectable "
            "return.\n\nEconomists refuse to explain the market."
        ),
        failure_message=(
            "Economists confirm there is currently no turtle shortage.\n\n"
            "Nigeria now owns hundreds of financially useless but otherwise "
            "healthy turtles."
        ),
        rare_success_message=(
            "🐢🚀 **THE GLOBAL TURTLE MARKET HAS EXPLODED**\n\n"
            "Turtle prices increase by hundreds of percent overnight. Nigeria's "
            "accidental strategic reserve suddenly becomes one of the nation's "
            "most valuable assets."
        ),
        extreme_message=(
            "🐢💥 **THE STRATEGIC TURTLE RESERVE HAS ESCAPED**\n\n"
            "Hundreds of supposedly valuable strategic turtles disappear from "
            "government storage overnight. Wildlife authorities arrive.\n\n"
            "Nobody can explain why Nigeria was speculating on turtles in the "
            "first place."
        ),
    ),
    _t(
        id="lolmanism",
        emoji="🐸",
        name="Official Recognition of Lolmanism",
        rarity="rare",
        risk="🔴 Extreme",
        description=(
            "Lolman, leader of the Voet Likkende Kikker Partij, is offered "
            "formal Nigerian recognition of Lolmanism as a state-recognised "
            "religion.\n\nPremium recognition includes the title **SUPREME "
            "AMPHIBIOUS PROPHET**."
        ),
        min_stake=500, max_stake=3_500, max_participants=7,
        base_chance=0.25,
        chance_table={1: 0.25, 2: 0.30, 3: 0.35, 4: 0.40, 5: 0.45, 6: 0.50,
                      7: 0.55},
        crowd_note=(
            "🐸 **Bring everybody.** Each extra believer adds 5% — 25% solo, "
            "55% at seven. Extra participants can be presented as proof of an "
            "existing congregation."
        ),
        payout_min=2.80, payout_max=4.50,
        extreme_chance=0.14,
        fail_losses=(0.70, 0.90, 1.00),
        initiator_bonus=750,
        initiator_bonus_note="750 Naira on success for serving as High Priest.",
        rare_chance=0.08, rare_payout=7.0,
        success_message=(
            "🐸 Lolmanism receives official Nigerian recognition.\n\n"
            "Lolman is formally declared **SUPREME AMPHIBIOUS PROPHET OF THE "
            "FEDERAL REPUBLIC**."
        ),
        failure_message=(
            "Lolman discovers several theological errors in Nigeria's "
            "application.\n\nMost seriously, the sacred foot-licking ritual has "
            "been performed backwards.\n\nRecognition is denied."
        ),
        rare_success_message=(
            "🐸 **THE GREAT AMPHIBIOUS AWAKENING**\n\n"
            "Lolmanism spreads through the Council of Princes. Nigeria "
            "establishes a Ministry of Amphibious Affairs. Donations begin "
            "pouring in."
        ),
        extreme_message=(
            "🐸💥 **THE LOLMANIST SCHISM**\n\n"
            "Authorities discover that millions have been collected for the "
            "recognition of a religion whose central theology appears to "
            "consist largely of suspicious amphibian rituals.\n\n"
            "The Ministry of Amphibious Affairs is raided before it has "
            "technically been established."
        ),
    ),
    _t(
        id="southsudan",
        emoji="💎",
        name="South Sudan Strategic Resource Opportunity",
        rarity="rare",
        risk="☠️ Catastrophic",
        signup_minutes=20,
        description=(
            "Nigeria informs Egypt that it can arrange privileged access to "
            "incredibly valuable strategic resources in South Sudan.\n\n"
            "Nigeria does not own the resources. South Sudan has not "
            "necessarily agreed."
        ),
        min_stake=750, max_stake=4_000, max_participants=5,
        base_chance=0.30,
        chance_table={1: 0.30, 3: 0.25, 4: 0.19, 5: 0.13},
        crowd_note=(
            "☠️ **Keep it small.** Two stakeholders or fewer: 30%. Three: 25%. "
            "Four: 19%. Five: 13%. Every extra stakeholder makes the diplomatic "
            "fiction harder to maintain."
        ),
        payout_min=3.50, payout_max=5.50,
        extreme_chance=0.16,
        fail_losses=(0.75, 0.95, 1.00),
        rare_chance=0.06, rare_payout=8.0,
        success_message=(
            "Egypt approves the strategic-resource agreement.\n\n"
            "Nigeria congratulates all parties and promises to notify South "
            "Sudan shortly."
        ),
        failure_message=(
            "Egypt asks South Sudan to confirm the agreement.\n\n"
            "South Sudan responds: *“What agreement?”*\n\n"
            "Negotiations collapse immediately."
        ),
        rare_success_message=(
            "💎 **DIPLOMATIC MASTERPIECE**\n\n"
            "Egypt, South Sudan and Nigeria each sign slightly different "
            "versions of the agreement. Every country believes it received the "
            "best deal. Nobody wants to reopen negotiations."
        ),
        extreme_message=(
            "💎💥 **SOUTH SUDAN HAS FINALLY BEEN INFORMED**\n\n"
            "Egyptian officials contact South Sudan to celebrate the "
            "agreement.\n\nSouth Sudan responds: *“You sold WHAT?”*\n\n"
            "The resource deal collapses immediately. Border authorities seize "
            "all associated funds."
        ),
    ),
    _t(
        id="offshore",
        emoji="🏝️",
        name="Council of Princes Offshore Amnesty",
        rarity="rare",
        risk="☠️ Extremely High",
        description=(
            "The Nigerian government announces a temporary amnesty allowing "
            "princes to repatriate their mysterious offshore fortunes.\n\n"
            "Nobody asks why Nigerian princes all apparently have offshore "
            "fortunes.\n\n"
            "The offer is highly attractive — as long as too many princes do "
            "not use it at once."
        ),
        min_stake=1_000, max_stake=4_000, max_participants=5,
        base_chance=0.55,
        chance_table={1: 0.55, 2: 0.50, 3: 0.44, 4: 0.37, 5: 0.30},
        crowd_note=(
            "🏝️ **Every extra prince screws the rest of you.** Solo is 55% "
            "success at 10% catastrophe; at five it is 30% at 22%.\n"
            "Joining a full amnesty is not just bad for you — it is bad for "
            "everybody already in it."
        ),
        payout_min=2.20, payout_max=3.20,
        extreme_chance=0.10,
        extreme_table={1: 0.10, 2: 0.12, 3: 0.15, 4: 0.18, 5: 0.22},
        fail_losses=(0.60, 0.85, 1.00),
        rare_chance=0.06, rare_payout=5.5,
        success_message=(
            "The amnesty works. Offshore funds quietly return to Nigeria, minus "
            "several entirely legitimate administrative deductions."
        ),
        failure_message=(
            "A compliance officer asks for documentation proving the offshore "
            "fortune belongs to a legitimate Nigerian prince.\n\n"
            "The submitted family tree contains several suspiciously repeated "
            "names."
        ),
        rare_success_message=(
            "🏝️💰 **THE FORGOTTEN ISLAND ACCOUNT**\n\n"
            "One prince discovers an offshore account nobody remembered "
            "existed. The repatriated fortune is far larger than expected."
        ),
        extreme_message=(
            "🏝️🚨 **INTERNATIONAL COMPLIANCE HAS CONNECTED THE DOTS**\n\n"
            "Too many princes attempt to move too much offshore money at once. "
            "Banks coordinate, regulators share records and every transfer is "
            "frozen simultaneously."
        ),
    ),

    # ══════════════════════════════════════════════════════════════════════════
    # 🌟 SPECIAL — server-wide stupidity with enormous consequences
    # ══════════════════════════════════════════════════════════════════════════

    _t(
        id="diamond",
        emoji="💎",
        name="The Great Diamond Rotation",
        rarity="special",
        risk="☠️ Completely Irresponsible",
        description=(
            "Nigeria develops an ingenious strategic-resource plan.\n\n"
            "Diamonds are moved between several allied nations until every "
            "country supposedly ends up in a superior strategic position. After "
            "enough transfers, nobody remembers who owned the diamonds "
            "originally."
        ),
        min_stake=1_000, max_stake=6_000, max_participants=4,
        base_chance=0.16,
        chance_table={1: 0.16, 2: 0.23, 3: 0.30, 4: 0.36},
        payout_table={1: 7.0, 2: 5.5, 3: 4.5, 4: 3.8},
        crowd_note=(
            "💎 **Every seat is a trade-off.** More rotators make the scheme "
            "safer but dilute the return for *everyone*:\n"
            "1 → 16% at ×7.0 · 2 → 23% at ×5.5 · 3 → 30% at ×4.5 · "
            "4 → 36% at ×3.8.\n"
            "20% catastrophe chance, and an arrest is **80% likely** if it "
            "comes."
        ),
        payout_min=3.80, payout_max=7.0,
        extreme_chance=0.20,
        fail_losses=(0.85, 1.00, 1.00),
        rare_chance=0.08, rare_payout=12.0,
        success_message=(
            "💎 **THE ROTATION IS COMPLETE**\n\n"
            "Every participating country ends up with exactly the strategic "
            "position it was promised. The fact that the plan actually worked "
            "somehow makes it look even more suspicious."
        ),
        failure_message=(
            "After eleven transfers, three intermediaries and two emergency "
            "treaties, nobody can determine where the diamonds currently are.\n\n"
            "Every country insists somebody else has them."
        ),
        rare_success_message=(
            "💎🚀 **PERFECT DIAMOND ROTATION**\n\n"
            "The diamonds pass through so many countries, markets and "
            "agreements that the operation accidentally generates profit at "
            "every stage.\n\nEveryone ends richer."
        ),
        extreme_message=(
            "💎🚨 **THE GREAT DIAMOND ROTATION HAS COLLAPSED**\n\n"
            "Investigators reconstruct the entire transaction chain. This is "
            "unfortunate.\n\n"
            "The diamonds are seized. Every account involved is frozen. Several "
            "governments suddenly deny ever having heard of Nigeria."
        ),
    ),
    _t(
        id="takeover",
        emoji="🇳🇬",
        name="Dutch Government Takeover",
        rarity="special",
        risk="☠️ Regime-Ending",
        description=(
            "Through months of careful infiltration, Nigerian officials "
            "allegedly acquire sympathetic contacts inside the Dutch Congress, "
            "military units and Discord moderation team.\n\n"
            "For one brief moment, the Council of Princes believes it can seize "
            "control of the Netherlands, declare it a temporary Nigerian proxy "
            "and empty the Dutch treasury before anybody understands what "
            "happened.\n\n"
            "President Naud Muscater intends to stop this with everything he "
            "has."
        ),
        min_stake=2_000, max_stake=5_000, max_participants=8,
        base_chance=0.10,
        chance_per_participant=(0.02, 0.14),
        funding_thresholds=None,
        chance_cap=0.32,
        crowd_note=(
            "🚨 **The most dangerous operation in the game.** 10% base, **+2%** "
            "per extra conspirator (capped at 24%), and **+8%** if the "
            "operation raises more than 25.000 Naira. Maximum 32%.\n"
            "Ordinary failure is fixed at 15% — **everything else is total "
            "catastrophe**, with an **80% chance of arrest** each. At 10% "
            "success that is a 75% chance this ends in prison."
        ),
        payout_min=5.0, payout_max=5.0,
        extreme_chance=0.0,          # derived: 1 − success − ordinary_fixed
        ordinary_fixed=0.15,
        fail_losses=(0.70, 0.80, 0.90),
        rare_chance=0.10, rare_payout=8.0,
        success_message=(
            "🇳🇬🇳🇱 **THE NETHERLANDS HAS FALLEN**\n\n"
            "Nigerian infiltrators secure the key institutions. The Netherlands "
            "is temporarily declared a Nigerian proxy state and emergency "
            "transfers begin leaving the Dutch treasury before anyone can "
            "reverse the decision."
        ),
        failure_message=(
            "🇳🇱 **THE COUP HAS FAILED**\n\n"
            "At the final moment, President Naud Muscater discovers the "
            "Nigerian infiltration network. Several officials defect, Discord "
            "moderators change the channel permissions and Dutch military units "
            "remain unexpectedly loyal.\n\n"
            "The operation collapses. Somehow, you escape with a small portion "
            "of your possessions.\n\n"
            "**YOU SHOULD CONSIDER YOURSELF EXTREMELY LUCKY.**"
        ),
        rare_success_message=(
            "👑🇳🇬 **TOTAL NIGERIAN CONTROL**\n\n"
            "The takeover succeeds so completely that even the Dutch "
            "bureaucracy starts processing Nigerian orders as routine "
            "paperwork.\n\n"
            "The treasury is opened, the proxy declaration is certified and "
            "several Dutch officials congratulate Nigeria on a smooth "
            "transition."
        ),
        extreme_message=(
            "🚨🇳🇬🇳🇱 **THE NIGERIAN COUP HAS BEEN CRUSHED**\n\n"
            "President Naud Muscater declares a national emergency. Nigerian "
            "infiltrators are removed from Congress. Military units surround "
            "government buildings. The Discord moderation team performs the "
            "decisive counterattack by removing several roles.\n\n"
            "Every operational account is seized.\n\n"
            "**Authorities now possess the complete participant list.**"
        ),
    ),
]

BY_ID = {t["id"]: t for t in TEMPLATES}
if len(BY_ID) != len(TEMPLATES):
    seen: set[str] = set()
    raise ValueError(
        "duplicate template ids: "
        + str([t["id"] for t in TEMPLATES if t["id"] in seen or seen.add(t["id"])])
    )

# Derived spawn weights: each rarity's share, split evenly inside the rarity.
_COUNTS: dict[str, int] = {}
for _tpl in TEMPLATES:
    _COUNTS[_tpl["rarity"]] = _COUNTS.get(_tpl["rarity"], 0) + 1
for _tpl in TEMPLATES:
    _tpl["spawn_weight"] = RARITY_SHARE[_tpl["rarity"]] / _COUNTS[_tpl["rarity"]]


def pick_template() -> dict:
    """Roll a random template, weighted by rarity."""
    return random.choices(
        TEMPLATES, weights=[t["spawn_weight"] for t in TEMPLATES], k=1
    )[0]


def get(template_id: str) -> Optional[dict]:
    return BY_ID.get(template_id)


# ── Odds ──────────────────────────────────────────────────────────────────────

def _from_table(table: dict, count: int):
    """Look up a per-headcount table whose keys are *starting* counts."""
    applicable = [k for k in table if k <= max(1, count)]
    return table[max(applicable)] if applicable else None


def success_chance(tpl: dict, participants: int, total_invested: int) -> float:
    """The operation's live success chance for the current sign-up state.

    Four mechanisms, applied in order.  Most templates use exactly one; the
    Dutch takeover is the only one that stacks a headcount bonus and a funding
    bonus, which is why they are additive rather than exclusive.
    """
    chance = tpl["base_chance"]

    table = tpl["chance_table"]
    if table:
        found = _from_table(table, participants)
        if found is not None:
            chance = found

    per = tpl["chance_per_participant"]
    if per:
        step, cap = per
        chance += min(cap, max(0, participants - 1) * step)

    thresholds = tpl["funding_thresholds"]
    if thresholds:
        for amount, value in thresholds:
            if total_invested >= amount:
                chance = value

    bands = tpl["funding_bands"]
    if bands:
        for upper, value in bands:
            if total_invested <= upper:
                chance = value
                break

    # The takeover's funding bonus is additive on top of its headcount bonus.
    if tpl["id"] == "takeover" and total_invested > 25_000:
        chance += 0.08

    return max(0.01, min(tpl["chance_cap"], chance))


def extreme_chance(tpl: dict, participants: int, total_invested: int) -> float:
    """Chance the operation ends in total catastrophe.

    This is a *direct* top-level outcome probability, not a share of failures —
    an operation with 60% success and 10% extreme failure has 30% ordinary
    failure, not 4%.
    """
    if tpl["ordinary_fixed"] is not None:
        # Everything that is neither success nor the fixed ordinary slice.
        success = success_chance(tpl, participants, total_invested)
        return max(0.0, 1.0 - success - tpl["ordinary_fixed"])

    chance = tpl["extreme_chance"]
    table = tpl["extreme_table"]
    if table:
        found = _from_table(table, participants)
        if found is not None:
            chance = found
    funding = tpl["extreme_funding"]
    if funding:
        for amount, value in funding:
            if total_invested >= amount:
                chance = value
    return max(0.0, min(1.0, chance))


def roll_outcome(tpl: dict, participants: int, total_invested: int) -> str:
    """One roll partitioned into ``success`` / ``extreme`` / ``ordinary``."""
    success = success_chance(tpl, participants, total_invested)
    extreme = extreme_chance(tpl, participants, total_invested)
    r = random.random()
    if r < success:
        return "success"
    if r < success + extreme:
        return "extreme"
    return "ordinary"


def roll_severity() -> tuple[str, str, int]:
    """Pick an ordinary-failure severity. Returns ``(key, label, index)``."""
    keys = [s[0] for s in SEVERITY]
    weights = [s[2] for s in SEVERITY]
    key = random.choices(keys, weights=weights, k=1)[0]
    idx = keys.index(key)
    return key, SEVERITY[idx][1], idx


def payout_multiplier(
    tpl: dict, *, order: int, participants: int, rare: bool, roll: float
) -> float:
    """Multiplier for one participant on a successful operation.

    ``roll`` is a single 0–1 value drawn once per operation, so everyone shares
    the same luck on templates with a payout *range* — one operation, one
    outcome.  ``order`` is the 0-based join order, used only by the pyramid.
    """
    by_order = tpl["payout_by_order"]
    if by_order:
        base = by_order[min(order, len(by_order) - 1)]
        # The pyramid's rare success adds a flat +1.0x to every tier rather
        # than replacing the tier, which would erase the join-order mechanic.
        return base + (tpl["rare_payout"] if rare else 0.0)

    if rare and tpl["rare_payout"]:
        return tpl["rare_payout"]

    table = tpl["payout_table"]
    if table:
        found = _from_table(table, participants)
        if found is not None:
            return found

    bands = tpl["payout_bands"]
    if bands:
        found = _from_table(bands, participants)
        if found is not None:
            lo, hi = found
            return lo + (hi - lo) * roll

    lo, hi = tpl["payout_min"], tpl["payout_max"]
    return lo + (hi - lo) * roll


def expected_multiplier(
    tpl: dict, *, order: int, participants: int, total_invested: int
) -> float:
    """What one Naira staked on this operation is worth, before it resolves.

    Averages over all three outcomes: success (with its rare upgrade),
    ordinary failure (across the three severities), and extreme failure, which
    returns nothing.  ``roll=0.5`` is the mean of a uniform payout range.
    """
    success = success_chance(tpl, participants, total_invested)
    extreme = extreme_chance(tpl, participants, total_invested)
    ordinary = max(0.0, 1.0 - success - extreme)

    rare_p = tpl["rare_chance"]
    win = (
        rare_p * payout_multiplier(
            tpl, order=order, participants=participants, rare=True, roll=0.5)
        + (1 - rare_p) * payout_multiplier(
            tpl, order=order, participants=participants, rare=False, roll=0.5)
    )
    kept = sum(
        share * (1.0 - loss)
        for (_key, _label, share), loss in zip(SEVERITY, tpl["fail_losses"])
    )
    return success * win + ordinary * kept          # extreme contributes zero


def stake_hint(tpl: dict) -> str:
    """One line describing what it costs to get in."""
    bits = [f"**{tpl['min_stake']:,}**–**{tpl['max_stake']:,}**".replace(",", ".")]
    if tpl["free_entry"]:
        bits.append("or join **free**")
    return " ".join(bits)
