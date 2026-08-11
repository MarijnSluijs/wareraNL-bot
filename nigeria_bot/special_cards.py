"""The /special card catalogue — pure data plus the generation engine.

This module holds *no* game logic and touches neither Discord nor the
database.  Every card is a dict: what it costs, where it can appear, how
often, and what one line of text describes it.  The engine in
``special_game.py`` reads this and never hardcodes a number, so retuning the
system is an edit here.

Structure
---------
``CARDS``            logical card id -> definition.  A logical card exists
                     exactly once even when it appears in two tiers.
``PLACEMENTS``       tier -> rarity -> [(card_id, weight)], derived from the
                     cards' own ``placements`` so the two can never disagree.
``RARITY_BUCKETS``   tier -> rarity -> probability of that bucket.

Generation is two rolls, per the spec: first the rarity bucket, then a
weighted pick inside it.  Rarity is deliberately not a measure of power —
it is how often a mechanic is *fun* to see, which is why direct PvP cards
carry high internal weights.
"""

from __future__ import annotations

import random
from typing import Callable, Iterable, Optional

# ── Tiers and rarity ──────────────────────────────────────────────────────────

BUDGET, PREMIUM, PLATINUM = "budget", "premium", "platinum"
TIERS = (BUDGET, PREMIUM, PLATINUM)

TIER_LABEL = {
    BUDGET:   "🪙 BUDGET",
    PREMIUM:  "💼 PREMIUM",
    PLATINUM: "💎 PLATINUM",
}

COMMON, RARE, VERY_RARE, EXTREME_RARE = "common", "rare", "very_rare", "extreme_rare"

RARITY_LABEL = {
    COMMON:       "Common",
    RARE:         "Rare",
    VERY_RARE:    "Very Rare",
    EXTREME_RARE: "Extreme Rare",
}
RARITY_EMOJI = {
    COMMON: "⚪", RARE: "🔵", VERY_RARE: "🟣", EXTREME_RARE: "☢️",
}

# Bucket probabilities per tier (spec §7).  These must sum to 1.0 per tier;
# the module asserts it at import rather than silently skewing generation.
RARITY_BUCKETS = {
    BUDGET:   {COMMON: 0.80, RARE: 0.18, VERY_RARE: 0.02},
    PREMIUM:  {COMMON: 0.76, RARE: 0.20, VERY_RARE: 0.04},
    PLATINUM: {COMMON: 0.62, RARE: 0.25, VERY_RARE: 0.08, EXTREME_RARE: 0.05},
}

# ── Durations (spec §6), in minutes unless named otherwise ────────────────────

SPECIAL_COOLDOWN_HOURS   = 2
PUBLIC_EVENT_MINUTES     = 3     # clickable bait / micro-event signup
TIP_JAR_MINUTES          = 2     # Royal Tip Jar overrides the default
DUEL_RESPONSE_MINUTES    = 5     # direct challenge response
HIDDEN_TRAP_HOURS        = 6     # "next action" traps
PERSONAL_BUFF_HOURS      = 12    # personal protection/buff

# ── Activity windows (spec §3) ────────────────────────────────────────────────

ACTIVE_WINDOW_HOURS      = 8     # the default "recently active"
ACTIVE_SHORT_HOURS       = 3     # Personal Grudge, Lucky Lottery
ACTIVE_LONG_HOURS        = 24    # Cash Predator, Panama Papers, wealth cards

# ── Protected floors (spec §4) ────────────────────────────────────────────────

CASH_FLOOR_DEFAULT       = 1_000
CASH_FLOOR_PREDATOR      = 2_500
WEALTH_FLOOR_DEFAULT     = 1_000
WEALTH_FLOOR_GRUDGE      = 5_000
WEALTH_FLOOR_PRINCE      = 2_500
WEALTH_FLOOR_WELFARE     = 2_500
WEALTH_FLOOR_WHALE       = 5_000
FUND_FLOOR_ACQUISITION   = 2_500
FUND_FLOOR_NATIONALISE   = 2_500
FUND_FLOOR_OFFSHORE      = 5_000


def _card(
    card_id: str,
    name: str,
    *,
    placements: list[tuple[str, str, float]],
    cost: int = 0,
    cost_label: Optional[str] = None,
    one_line: str = "",
    emoji: str = "🎴",
    visibility: str = "public",
    confirm: bool = False,
    needs: Iterable[str] = (),
) -> dict:
    """One logical card.

    ``placements`` is a list of ``(tier, rarity, weight)``.  A card listed
    twice is the *same* logical card in two pools — never two cards — which is
    what makes the "no duplicate in one offer" rule expressible at all.

    ``needs`` names the eligibility predicates the engine must satisfy before
    the card may be offered; see ``ELIGIBILITY`` in special_game.py.
    """
    return {
        "id": card_id,
        "name": name,
        "emoji": emoji,
        "placements": placements,
        "cost": cost,
        "cost_label": cost_label or ("FREE" if cost == 0 else f"{cost:,}".replace(",", ".")),
        "one_line": one_line,
        "visibility": visibility,
        "confirm": confirm,
        "needs": tuple(needs),
    }


# ── The catalogue ─────────────────────────────────────────────────────────────
# Ordered as in the spec: budget-origin, premium-origin, platinum-origin.
# Cross-tier cards are declared once, under the tier they originate in, with
# both placements listed.

_ALL: list[dict] = [

    # ========================= BUDGET-ORIGIN =========================

    _card(
        "special_cash_injection", "Cash Injection", emoji="💵",
        placements=[(BUDGET, COMMON, 0.90)], cost=100,
        one_line="Immediate cash injection of 200–1.000 Naira, heavily weighted low.",
        visibility="public",
    ),
    _card(
        "special_lucky_man", "Lucky Man", emoji="🍀",
        placements=[(BUDGET, COMMON, 1.00)], cost=100,
        one_line="Your next /scam within 12 hours uses maximum 3-hour odds. It can still fail.",
        visibility="private_then_public",
    ),
    _card(
        "special_intelligence_leak", "Intelligence Leak", emoji="🔎",
        placements=[(BUDGET, COMMON, 0.90)], cost=200,
        one_line="Restore 2 Intel charges immediately, capped at 3/3.",
        visibility="private", needs=["intel_not_full"],
    ),
    _card(
        "special_sticky_fingers", "Sticky Fingers", emoji="🥷",
        placements=[(BUDGET, COMMON, 1.40)], cost=250,
        one_line="Rob a random recent player of 10% of their cash, max 2.500. They keep 1.000.",
        visibility="public", needs=["victim_cash"],
    ),
    _card(
        "special_fake_news", "Fake News", emoji="📢",
        placements=[(BUDGET, COMMON, 1.00)], cost=0,
        one_line="Publish an official-looking economic announcement. No mechanical effect whatsoever.",
        visibility="public",
    ),
    _card(
        "special_tax_audit", "Tax Audit", emoji="🧾",
        placements=[(BUDGET, COMMON, 1.00)], cost=100,
        one_line="6h trap: the next other fund withdrawal above 1.000 loses 20%, max 2.000, destroyed.",
        visibility="private_then_public", needs=["trap_free"],
    ),
    _card(
        "special_counterfeit_naira", "Counterfeit Naira", emoji="💵",
        placements=[(BUDGET, COMMON, 1.20)], cost=100,
        one_line="6h trap: skim exactly 500 off the next other /scam or real target reward above 1.000.",
        visibility="private_then_public", needs=["trap_free"],
    ),
    _card(
        "special_counterfeit_detector", "Counterfeit Detector", emoji="🛡️",
        placements=[(BUDGET, COMMON, 0.90)], cost=250,
        one_line="12h: your next fake-target encounter is detected safely. Operating cost still applies.",
        visibility="private", needs=["no_detector"],
    ),
    _card(
        "special_royal_tip_jar", "Royal Tip Jar", emoji="🫙",
        placements=[(BUDGET, COMMON, 1.20)], cost=0,
        one_line="2-minute public raffle at 200 a ticket. One contributor takes 80%, you take 20%.",
        visibility="public",
    ),
    _card(
        "special_unknown_caller", "Unknown Caller", emoji="📞",
        placements=[(BUDGET, COMMON, 1.40)], cost=50,
        one_line="Public call. Whoever answers first either gains 1.000 or loses up to 750 to you.",
        visibility="public",
    ),
    _card(
        "special_suspicious_tikkie", "Suspicious Tikkie", emoji="💳",
        placements=[(BUDGET, COMMON, 1.30)], cost=50,
        one_line="Public bait. The first click either gains 500 or loses up to 500 to you.",
        visibility="public",
    ),
    _card(
        "special_dropped_wallet", "Dropped Wallet", emoji="👛",
        placements=[(BUDGET, COMMON, 1.30)], cost=0,
        one_line="Public wallet: 75% the finder gets 500 and may return half, 25% they are arrested.",
        visibility="public",
    ),
    _card(
        "special_phishing_test", "Phishing Test", emoji="🐟",
        placements=[(BUDGET, COMMON, 1.40)], cost=50,
        one_line="An obvious fake login. The first other player to click loses up to 500 to you.",
        visibility="public",
    ),
    _card(
        "special_marktplaats_deal", "Marktplaats Deal", emoji="🚲",
        placements=[(BUDGET, COMMON, 1.30)], cost=150,
        one_line="List a bicycle for 500. The buyer gets 1.000 back 60% of the time — otherwise you keep it.",
        visibility="public",
    ),
    _card(
        "special_trickle_up_economics", "Trickle-Up Economics", emoji="💸",
        placements=[(BUDGET, COMMON, 1.20)], cost=100,
        one_line="6h trap: every donation in the next /beg session is redirected to you. No cap.",
        visibility="private_then_public", needs=["trap_free", "begging_flow_free"],
    ),
    _card(
        "special_suspicious_activity_report", "Suspicious Activity Report", emoji="🕵️",
        placements=[(BUDGET, RARE, 0.80)], cost=0,
        one_line="A private snapshot naming exactly which marks on the board are fake right now.",
        visibility="private",
    ),
    _card(
        "special_police_informant", "Police Informant", emoji="🚔",
        placements=[(BUDGET, RARE, 1.20)], cost=150,
        one_line="6h trap: the next other player to fail a real mark is arrested. You are named afterwards.",
        visibility="private_then_public", needs=["trap_free"],
    ),
    _card(
        "special_mystery_box", "Mystery Box", emoji="📦",
        placements=[(BUDGET, RARE, 1.30)], cost=250,
        one_line="Somebody else opens a box worth −2.000 to +4.000. You split anything positive.",
        visibility="public",
    ),
    _card(
        "special_beggar_king", "Beggar King", emoji="👑",
        placements=[(BUDGET, RARE, 1.40), (PREMIUM, COMMON, 1.20)], cost=250,
        one_line="6h trap: the next /beg reverses its first 3 donations — the beggar pays the donors.",
        visibility="private_then_public", needs=["trap_free", "begging_flow_free"],
    ),
    _card(
        "special_ponzi_pitch", "Ponzi Pitch", emoji="📈",
        placements=[(BUDGET, RARE, 1.40), (PREMIUM, COMMON, 1.30)], cost=500,
        one_line="Pick one of three top-10 fund investors and take 1.000 out of their position, as cash.",
        visibility="public", needs=["fund_victim_1000"],
    ),
    _card(
        "special_snitch", "Snitch", emoji="🐀",
        placements=[(BUDGET, RARE, 1.50), (PREMIUM, COMMON, 1.40)], cost=500,
        one_line="Pick a recently active player and have them arrested. You are publicly named.",
        visibility="public", needs=["arrestable"],
    ),
    _card(
        "special_wrong_account", "Wrong Account", emoji="🔄",
        placements=[(BUDGET, VERY_RARE, 1.00)], cost=500,
        one_line="A banking error swaps the entire cash balances of two random recent players.",
        visibility="public", needs=["two_actives"],
    ),

    # ========================= PREMIUM-ORIGIN ========================

    _card(
        "special_nigerian_insurance_policy", "Nigerian Insurance Policy", emoji="🛡️",
        placements=[(PREMIUM, COMMON, 0.80)], cost=1_000,
        one_line="1h of uncapped cover for involuntary losses to the system. Not for PvP or spending.",
        visibility="private_then_public", needs=["no_insurance"],
    ),
    _card(
        "special_highwayman", "Highwayman", emoji="🏴‍☠️",
        placements=[(PREMIUM, COMMON, 1.30)], cost=500,
        one_line="6h trap: intercept 50% of the next other successful /scam or real target reward, max 2.500.",
        visibility="private_then_public", needs=["trap_free"],
    ),
    _card(
        "special_forced_scam_duel", "Forced Scam Duel", emoji="⚔️",
        placements=[(PREMIUM, COMMON, 1.50), (PLATINUM, COMMON, 1.50)], cost=750,
        one_line="Force a recent player into a duel. The winner takes 25% of the loser's wealth, max 5.000.",
        visibility="public", needs=["duel_opponent"],
    ),
    _card(
        "special_open_scam_duel", "Open Scam Duel", emoji="🤺",
        placements=[(PREMIUM, COMMON, 1.40)], cost=250, cost_label="250 + wager",
        one_line="Offer an open duel for 1.000, 2.000 or 3.000. The first to accept matches your wager.",
        visibility="public",
    ),
    _card(
        "special_get_out_of_jail_free", "Get Out of Jail Free", emoji="🎫",
        placements=[(PREMIUM, COMMON, 0.80)], cost=0,
        one_line="Keep this card until your next arrest, which it cancels immediately. It does not stack.",
        visibility="private_then_public", needs=["no_jail_card"],
    ),
    _card(
        "special_roger_has_been_reassured", "Roger Has Been Reassured", emoji="📈",
        placements=[(PREMIUM, COMMON, 0.70)], cost=750,
        one_line="Talk Roger down: the fund's risk level drops by one immediately.",
        visibility="public", needs=["risk_above_1"],
    ),
    _card(
        "special_roger_is_nervous", "Roger Is Nervous", emoji="📉",
        placements=[(PREMIUM, COMMON, 0.80)], cost=750,
        one_line="Show Roger some worrying charts: the fund's risk level rises by one immediately.",
        visibility="public", needs=["risk_below_max"],
    ),
    _card(
        "special_cash_predator", "Cash Predator", emoji="💵",
        placements=[(PREMIUM, COMMON, 1.50), (PLATINUM, COMMON, 1.40)], cost=1_250,
        one_line="Pick one of the five richest recent players. 65% to take 30% of their cash, max 7.500.",
        visibility="public", needs=["predator_victim"],
    ),
    _card(
        "special_scam_olympics", "Scam Olympics", emoji="🏅",
        placements=[(PREMIUM, COMMON, 1.30)], cost=500, cost_label="500 + buy-in",
        one_line="Open a contest at 1.000 or 2.000 a head, up to ten. One winner takes 90% of the pot.",
        visibility="public",
    ),
    _card(
        "special_anonymous_benefactor", "Anonymous Benefactor", emoji="❤️",
        placements=[(PREMIUM, COMMON, 0.70)], cost=500,
        one_line="Give 5.000 to a random struggling player. Nobody is told it was you.",
        visibility="public_anonymous", needs=["poor_player"],
    ),
    _card(
        "special_reverse_robin_hood", "Reverse Robin Hood", emoji="🤑",
        placements=[(PREMIUM, COMMON, 0.90)], cost=250,
        one_line="Take 100 from every poor active player and hand the lot to the richest one.",
        visibility="public", needs=["poor_player"],
    ),
    _card(
        "special_robin_hood_returns", "Robin Hood Returns", emoji="🏹",
        placements=[(PREMIUM, COMMON, 0.90)], cost=750,
        one_line="Take 500 from every cash-heavy player and split it among everyone below 5.000.",
        visibility="public", needs=["robin_hood_pair"],
    ),
    _card(
        "special_false_investment_fraud", "False Investment Fraud", emoji="🏦",
        placements=[(PREMIUM, COMMON, 1.30), (PLATINUM, COMMON, 1.20)], cost=1_000,
        one_line="Pick one of three top-10 investors and take 2.000 out of their fund position, as cash.",
        visibility="public", needs=["fund_victim_4500"],
    ),
    _card(
        "special_welfare_fraud", "Welfare Fraud", emoji="🚨",
        placements=[(PREMIUM, COMMON, 1.20)], cost=750,
        one_line="6h trap: the next player worth over 5.000 who begs is fined up to 2.000, paid to you.",
        visibility="private_then_public", needs=["trap_free"],
    ),
    _card(
        "special_hostile_acquisition", "Hostile Acquisition", emoji="🏦",
        placements=[(PREMIUM, RARE, 1.40), (PLATINUM, COMMON, 1.20)], cost=1_500,
        one_line="Pick one of three top-10 investors and move 20% of their position into yours, max 5.000.",
        visibility="public", needs=["fund_victim_acquire"],
    ),
    _card(
        "special_professional_guarantee", "Professional Guarantee", emoji="🎯",
        placements=[(PREMIUM, RARE, 1.00)], cost=1_000,
        one_line="12h: your next attempt on a real, non-legendary mark simply succeeds.",
        visibility="private_then_public", needs=["no_guarantee"],
    ),
    _card(
        "special_counter_intelligence_sweep", "Counter-Intelligence Sweep", emoji="🧹",
        placements=[(PREMIUM, RARE, 0.80)], cost=750,
        one_line="Clear every fake target off the board. Their deposits are destroyed, nobody is arrested.",
        visibility="public", needs=["fakes_exist"],
    ),
    _card(
        "special_fog_of_war", "Fog of War", emoji="🌫️",
        placements=[(PREMIUM, RARE, 0.90)], cost=500,
        one_line="For 15 minutes every displayed target chance reads ???. The real odds do not move.",
        visibility="public", needs=["no_fog"],
    ),
    _card(
        "special_unleash_the_muggers", "Unleash the Muggers", emoji="🔪",
        placements=[(PREMIUM, RARE, 1.40)], cost=1_500,
        one_line="3h: every 10 minutes one of the richest players may be mugged for up to 750.",
        visibility="public", needs=["no_muggers"],
    ),
    _card(
        "special_nigerian_scamming_crash_course", "Nigerian Scamming Crash Course", emoji="🎓",
        placements=[(PREMIUM, RARE, 1.30)], cost=750,
        one_line="Reset your own fake-target cooldown, and double the theft on the next fake you run.",
        visibility="private", needs=["no_crash_course"],
    ),
    _card(
        "special_mass_phishing_campaign", "Mass Phishing Campaign", emoji="📲",
        placements=[(PREMIUM, RARE, 1.40), (PLATINUM, COMMON, 1.30)], cost=750,
        one_line="Five named players each get a button. Everyone who clicks pays you up to 500.",
        visibility="public", needs=["phishing_targets"],
    ),
    _card(
        "special_portfolio_shuffle", "Portfolio Shuffle", emoji="🔄",
        placements=[(PREMIUM, RARE, 1.10)], cost=500,
        one_line="Roger drags two spreadsheet rows: two random investors swap fund positions entirely.",
        visibility="public", needs=["two_investors"],
    ),
    _card(
        "special_lucky_lottery", "Lucky Lottery", emoji="🎰",
        placements=[(PREMIUM, VERY_RARE, 1.00)], cost=500,
        one_line="A random player active in the last 3 hours — possibly you — receives 10.000.",
        visibility="public", needs=["lottery_pool"],
    ),
    _card(
        "special_nationalisation", "Nationalisation", emoji="☭",
        placements=[(PREMIUM, VERY_RARE, 0.80)], cost=1_000,
        one_line="Take up to 600 from each of the top 5 fund positions and split it among the rest.",
        visibility="public", needs=["nationalisable"],
    ),
    _card(
        "special_prince_for_a_day", "Prince for a Day", emoji="👑",
        placements=[(PREMIUM, VERY_RARE, 1.40), (PLATINUM, COMMON, 1.40)], cost=1_000,
        one_line="Take 5.000 now. For 3 hours every theft from you destroys a second, equal amount.",
        visibility="public", needs=["not_prince"],
    ),
    _card(
        "special_personal_grudge", "Personal Grudge", emoji="😡",
        placements=[(PREMIUM, VERY_RARE, 1.30), (PLATINUM, COMMON, 1.50)], cost=3_000,
        one_line="For 2 hours, every involuntary loss your chosen victim suffers is doubled and burned.",
        visibility="public", needs=["grudge_victim"],
    ),

    # ========================= PLATINUM-ORIGIN =======================

    _card(
        "special_eat_the_rich_cash", "Eat the Rich — Cash", emoji="💰",
        placements=[(PLATINUM, COMMON, 1.20)], cost=3_000,
        one_line="Seize 40% of the richest player's cash above 2.500, max 15.000. You keep 70%.",
        visibility="public", confirm=True, needs=["rich_cash_holder"],
    ),
    _card(
        "special_whale_harpoon", "Whale Harpoon", emoji="🐋",
        placements=[(PLATINUM, COMMON, 1.10)], cost=4_000,
        one_line="75% to take 25% of the wealthiest player's wealth above 5.000, max 10.000.",
        visibility="public", needs=["whale"],
    ),
    _card(
        "special_great_art_heist", "Great Art Heist", emoji="🖼️",
        placements=[(PLATINUM, COMMON, 1.30)], cost=1_000, cost_label="1.000 stake",
        one_line="Crew of up to 4 at 1.000 each: 35% pays ×2.5, 20% pays ×4, 20% ends in arrests.",
        visibility="public",
    ),
    _card(
        "special_nigerian_government_coup", "Nigerian Government Coup", emoji="🏛️",
        placements=[(PLATINUM, COMMON, 1.20)], cost=1_000, cost_label="1.000 stake",
        one_line="Crew of up to 4 at 1.000 each: 25% pays ×4, 30% pays ×1.3, 20% ends in arrests.",
        visibility="public",
    ),
    _card(
        "special_diplomatic_kidnapping", "Diplomatic Kidnapping", emoji="🕴️",
        placements=[(PLATINUM, COMMON, 1.30)], cost=1_000, cost_label="1.000 stake",
        one_line="Crew of up to 3 at 1.000 each: 35% pays ×5, and 40% ends with everyone arrested.",
        visibility="public",
    ),
    _card(
        "special_asset_freeze", "Asset Freeze", emoji="🔒",
        placements=[(PLATINUM, COMMON, 0.80)], cost=750,
        one_line="Block a chosen top investor from depositing or withdrawing for 2 hours.",
        visibility="public", needs=["freezable"],
    ),
    _card(
        "special_burn_notice", "Burn Notice", emoji="🧨",
        placements=[(PLATINUM, COMMON, 1.10)], cost=750,
        one_line="A chosen top-5 player loses half of each of their next 3 earnings, burned, within 6h.",
        visibility="public", needs=["burn_victim"],
    ),
    _card(
        "special_mass_arrest", "Mass Arrest", emoji="🚔",
        placements=[(PLATINUM, COMMON, 1.20)], cost=1_000,
        one_line="Three random recently active players are arrested immediately.",
        visibility="public", needs=["three_arrestable"],
    ),
    _card(
        "special_ponzi_launch_party", "Ponzi Launch Party", emoji="🧨",
        placements=[(PLATINUM, COMMON, 1.30)], cost=250, cost_label="250 + 500 buy-in",
        one_line="Public scheme for 3–6 investors at 500. One takes 60%, you take 20%, 20% burns.",
        visibility="public",
    ),
    _card(
        "special_operation_clean_board", "Operation Clean Board", emoji="🧹",
        placements=[(PLATINUM, RARE, 0.90)], cost=1_500,
        one_line="Raid the board: every fake is removed, their deposits are yours, and the owners are jailed.",
        visibility="public",
    ),
    _card(
        "special_seize_the_offshore_accounts", "Seize the Offshore Accounts", emoji="🏝️",
        placements=[(PLATINUM, RARE, 1.20)], cost=3_000,
        one_line="Seize 30% of the largest fund position above 5.000, max 15.000. You keep 70%.",
        visibility="public", confirm=True, needs=["offshore_victim"],
    ),
    _card(
        "special_nigerian_stimulus_package", "Nigerian Stimulus Package", emoji="🇳🇬",
        placements=[(PLATINUM, RARE, 0.80)], cost=1_500,
        one_line="Every recent player below 5.000 receives cash equal to their wealth, capped at 2.500.",
        visibility="public", needs=["poor_player"],
    ),
    _card(
        "special_economic_russian_roulette", "Economic Russian Roulette", emoji="🔫",
        placements=[(PLATINUM, RARE, 1.30)], cost=500,
        one_line="Six recent players, you included. One loses up to 3.000; half is split, half is burned.",
        visibility="public", needs=["six_actives"],
    ),
    _card(
        "special_the_big_short", "The Big Short", emoji="📉",
        placements=[(PLATINUM, RARE, 0.90)], cost=3_000,
        one_line="Bet that the next natural fund event is bad. If it is, you collect 7.500.",
        visibility="public", needs=["no_big_short"],
    ),
    _card(
        "special_royal_bank_robbery", "Royal Bank Robbery", emoji="🏦",
        placements=[(PLATINUM, RARE, 1.10)], cost=4_000,
        one_line="60% to pull 5.000 straight out of the fund. Every position shrinks to pay for it.",
        visibility="public", needs=["fund_5000"],
    ),
    _card(
        "special_panama_papers", "Panama Papers", emoji="🧳",
        placements=[(PLATINUM, RARE, 1.20)], cost=1_000,
        one_line="Every recent player holding over 10.000 cash is named and arrested. Including you.",
        visibility="public", needs=["papers_targets"],
    ),
    _card(
        "special_scamtopian_paradise", "Scamtopian Paradise", emoji="🏝️",
        placements=[(PLATINUM, RARE, 0.90)], cost=750,
        one_line="Reset the fake-target cooldown for every player in the game at once.",
        visibility="public",
    ),
    _card(
        "special_the_return_of_carl_marx", "THE RETURN OF CARL MARX", emoji="☭",
        placements=[(PLATINUM, VERY_RARE, 1.50)], cost=5_000,
        one_line="Every investor who held a position when this offer appeared ends up with the same.",
        visibility="public", confirm=True, needs=["marx_cohort"],
    ),
    _card(
        "special_the_great_cash_reset", "THE GREAT CASH RESET", emoji="💥",
        placements=[(PLATINUM, VERY_RARE, 1.00)], cost=1_500,
        one_line="Destroy 20% of everyone's cash above 10.000. You get nothing and are not exempt.",
        visibility="public", confirm=True,
    ),
    _card(
        "special_warren_buffett_consultancy_call", "Warren Buffett Consultancy Call", emoji="📞",
        placements=[(PLATINUM, VERY_RARE, 0.80)], cost=2_500,
        one_line="Reset fund risk to 1 and clear collapse pressure. No lost value comes back.",
        visibility="public", needs=["risk_3_plus"],
    ),
    _card(
        "special_nuclear_bomb", "Nuclear Bomb", emoji="☢️",
        placements=[(PLATINUM, EXTREME_RARE, 1.00)], cost=20_000,
        one_line="Collapse the entire Royal Investment Fund. Every position goes to zero. You gain nothing.",
        visibility="public", confirm=True, needs=["fund_10000"],
    ),
]

CARDS: dict[str, dict] = {c["id"]: c for c in _ALL}

# The ten cards the spec deliberately places in two adjacent pools, derived
# from the catalogue rather than restated — a card that gains or loses a
# placement stays consistent with this list automatically.
OVERLAP_CARDS = tuple(
    c["id"] for c in _ALL if len(c["placements"]) > 1
)


def _build_placements() -> dict[str, dict[str, list[tuple[str, float]]]]:
    out: dict[str, dict[str, list[tuple[str, float]]]] = {
        t: {r: [] for r in RARITY_BUCKETS[t]} for t in TIERS
    }
    for card in _ALL:
        for tier, rarity, weight in card["placements"]:
            if rarity not in out[tier]:
                raise ValueError(
                    f"{card['id']}: {tier} has no {rarity} bucket"
                )
            out[tier][rarity].append((card["id"], weight))
    return out


PLACEMENTS = _build_placements()


# ── Generation ────────────────────────────────────────────────────────────────

def _weighted(items: list[tuple[str, float]], rng: random.Random) -> str:
    total = sum(w for _, w in items)
    roll = rng.random() * total
    for card_id, weight in items:
        roll -= weight
        if roll <= 0:
            return card_id
    return items[-1][0]


def pick_card(
    tier: str,
    *,
    eligible: Callable[[str], bool] = lambda _cid: True,
    exclude: Iterable[str] = (),
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Roll one card for one tier: rarity bucket first, then a weighted pick.

    ``exclude`` carries the cards already chosen for the other tiers, which is
    how the "same logical card never twice in one offer" rule is enforced —
    the excluded card is simply not in the bucket when we draw.

    Returns None only when *no* card in the whole tier can be offered, which
    the caller must treat as "regenerate" rather than "show an empty slot".
    """
    rng = rng or random
    exclude = set(exclude)

    def bucket(rarity: str) -> list[tuple[str, float]]:
        return [
            (cid, w) for cid, w in PLACEMENTS[tier][rarity]
            if cid not in exclude and eligible(cid)
        ]

    # Only roll among buckets that actually have something in them, keeping
    # the configured proportions between the survivors (spec §17).
    buckets = {r: bucket(r) for r in RARITY_BUCKETS[tier]}
    live = {r: p for r, p in RARITY_BUCKETS[tier].items() if buckets[r]}
    if not live:
        return None

    roll = rng.random() * sum(live.values())
    for rarity, share in live.items():
        roll -= share
        if roll <= 0:
            return _weighted(buckets[rarity], rng)
    return _weighted(buckets[list(live)[-1]], rng)


def generate_offer(
    *,
    eligible: Callable[[str], bool] = lambda _cid: True,
    rng: Optional[random.Random] = None,
) -> Optional[dict[str, str]]:
    """One Budget + one Premium + one Platinum, no logical duplicates.

    Returns None when a tier cannot be filled at all; the caller decides
    whether that means "try again later" or "offer what we have".
    """
    rng = rng or random
    chosen: dict[str, str] = {}
    for tier in TIERS:
        card_id = pick_card(
            tier, eligible=eligible, exclude=chosen.values(), rng=rng
        )
        if card_id is None:
            return None
        chosen[tier] = card_id
    return chosen


def rarity_of(card_id: str, tier: str) -> str:
    for placed_tier, rarity, _w in CARDS[card_id]["placements"]:
        if placed_tier == tier:
            return rarity
    # A card shown in a tier it was never placed in is a bug, not a display
    # problem — fall back to its first placement rather than crashing a menu.
    return CARDS[card_id]["placements"][0][1]


# ── Flavour pools ─────────────────────────────────────────────────────────────

FAKE_NEWS = [
    "Royal Investment Fund declared fully solvent after nobody tried withdrawing at the same time.",
    "Council denies 14.000 Naira disappeared. The correct figure is still being recalculated.",
    "Nigeria's sovereign credit rating upgraded from QUESTIONABLE to PROBABLY FINE.",
    "Central Bank defeats inflation by printing smaller numbers on the banknotes.",
    "Aap Industries denies its latest financial statement was written in crayon.",
    "Roger confirms every Fund Naira is backed by at least one other Naira somewhere.",
    "Economists announce strong growth after opening the spreadsheet at 125% zoom.",
    "Strategic bicycle reserve reclassified as fixed assets.",
    "Royal Investment Fund passes independent audit. Auditor immediately deposits entire salary.",
    "Emergency press conference cancelled after officials forgot what the emergency was.",
    "Unemployment falls sharply after inactive spreadsheet rows are deleted.",
    "Treasury locates missing 3.000 Naira under 'miscellaneous Prince expenses'.",
    "Nigerian GDP revised upward after the Royal Tip Jar is accidentally counted twice.",
    "Roger rejects rumours of a bank run while sprinting toward the bank.",
    "Government promises no new taxes until somebody uses the next command.",
]

ROGER_QUOTES = {
    "special": [
        "Never let a cooldown stand between you and a bad financial decision.",
        "If an opportunity was worth taking once, regulations probably allow you to take it twice.",
        "Diversification means committing two unrelated scams before lunch.",
        "The market rewards speed. Especially before anyone notices.",
        "The best time to use a Special was two hours ago. The second-best time is immediately.",
    ],
    "fake": [
        "Reputation is temporary. A new moustache is forever.",
        "If they recognised the last profile, simply become somebody else.",
        "A fake identity only needs confidence, a flag and one spelling mistake.",
        "Never reuse the same scam without changing the profile picture.",
        "Trust is a renewable resource if you use somebody else's.",
    ],
    "intel": [
        "Research is just guessing with paperwork.",
        "Three pieces of intelligence are enough to prove whatever you already believed.",
        "Always investigate before investing. Unless I am the investment.",
        "Knowledge is power. Power should be monetised before it expires.",
        "The best intelligence is confidential, preferably because nobody verified it.",
    ],
    "scam": [
        "Never trust free financial advice. That will be 500 Naira.",
        "Always keep 500 Naira liquid for unexpected fees. Thank you for demonstrating.",
        "My advice is to avoid hidden charges. Consultancy fee: 500 Naira.",
    ],
}

# Rock-paper-scissors for both duel cards.  Paperwork beats Phishing beats
# Prince beats Paperwork: bureaucracy defeats technology, technology defeats
# royalty, royalty defeats bureaucracy.
DUEL_MOVES = {
    "paperwork": ("🧾", "Paperwork"),
    "phishing":  ("💻", "Phishing"),
    "prince":    ("👑", "Prince"),
}
DUEL_BEATS = {"paperwork": "phishing", "phishing": "prince", "prince": "paperwork"}


def duel_winner(a: str, b: str) -> Optional[bool]:
    """True if move ``a`` wins, False if ``b`` wins, None on a tie."""
    if a == b:
        return None
    return DUEL_BEATS[a] == b


# ── Self-checks ───────────────────────────────────────────────────────────────
# These run at import.  A misconfigured pool is far cheaper to catch here than
# to notice weeks later as a card nobody ever saw.

for _tier, _buckets in RARITY_BUCKETS.items():
    _total = sum(_buckets.values())
    if abs(_total - 1.0) > 1e-9:
        raise ValueError(f"{_tier} rarity buckets sum to {_total}, not 1.0")
    for _rarity in _buckets:
        if not PLACEMENTS[_tier][_rarity]:
            raise ValueError(f"{_tier}/{_rarity} has no cards")

for _card_def in _ALL:
    if not _card_def["one_line"]:
        raise ValueError(f"{_card_def['id']} has no one-line effect")
    if len(_card_def["placements"]) > 2:
        raise ValueError(f"{_card_def['id']} is placed in more than two pools")

del _tier, _buckets, _total, _rarity, _card_def
