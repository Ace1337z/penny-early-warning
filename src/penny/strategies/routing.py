"""Catalyst-aware routing (Spec 3.3).

Not all catalysts are equal, and neither is the size a setup deserves. This
module turns the *class* of the driver into a tier and a size multiplier:

    Tier 1  contract / FDA / direct earnings PR   full size, standard rules
    Tier 2  sector / sympathy                     standard rules, 0.75x size
    Tier 3  no catalyst / pure tape / rumor        0.50x size, and continuation
                                                   breakouts are vetoed after
                                                   10:30 ET

The tier is derived from the same evidence the rest of the pipeline uses -- the
ranked news classes and the filings -- so it never invents a driver. It is pure:
same inputs, same tier.
"""

from __future__ import annotations

from typing import Optional

from .news_cause import HARD, MEDIUM, SOFT

TIER_1 = 1
TIER_2 = 2
TIER_3 = 3

TIER_SIZE = {TIER_1: 1.0, TIER_2: 0.75, TIER_3: 0.5}
TIER_NAMES = {TIER_1: "1", TIER_2: "2", TIER_3: "3"}

# Tier 1 headline markers: a contract, an FDA/clinical action or a direct
# earnings release -- things that reprice the company itself.
_T1_MARKERS = (
    "contract", "awarded", "wins", "order", "purchase order", "partnership",
    "fda", "approval", "clearance", "phase 1", "phase 2", "phase 3",
    "clinical", "trial", "topline", "results", "earnings", "guidance",
    "acquisi", "merger", "tender offer", "buyout", "license agreement",
    "supply agreement", "definitive agreement", "uplisting", "dividend",
)
# Tier 2 markers: the driver is a peer or the sector, not this company.
_T2_MARKERS = (
    "sector", "peer", "sympathy", "sector rally", "follows", "amid",
    "after peer", "in sympathy",
)

# After this many minutes past midnight... no: after 10:30 ET a Tier 3
# continuation breakout is refused -- a pure-tape breakout with no driver that
# late is a chase into lunch.
TIER3_CONT_CUTOFF_MINUTES = 10 * 60 + 30


def _has(headline: str, markers) -> bool:
    h = (headline or "").lower()
    return any(m in h for m in markers)


def catalyst_tier(b, ranked_catalysts: Optional[list[dict]] = None) -> int:
    """Tier this symbol's driver: 1 (company), 2 (sector), or 3 (tape/rumor).

    A HARD or MEDIUM company-specific event is Tier 1. A sector/sympathy read
    is Tier 2. Anything else -- no named cause, SOFT chatter, or a pure tape
    mechanism -- is Tier 3.
    """
    ranked = ranked_catalysts
    if ranked is None:
        ranked = getattr(b, "ranked_catalysts", None) or []
    # Best real catalyst first.
    for e in ranked:
        cls = e.get("class")
        headline = str(e.get("headline") or "")
        if cls == HARD or (cls == MEDIUM and _has(headline, _T1_MARKERS)):
            return TIER_1
        if cls in (HARD, MEDIUM) and _has(headline, _T2_MARKERS):
            return TIER_2
    # A MEDIUM that is neither a Tier 1 marker nor sector-flavoured: treat as
    # company-specific (a real, if softer, driver).
    for e in ranked:
        if e.get("class") == MEDIUM:
            return TIER_1
    return TIER_3


def size_multiplier(tier: int) -> float:
    """The sizing multiplier for a tier (Spec 3.3)."""
    return TIER_SIZE.get(tier, 0.5)


def continuation_allowed(tier: int, now_et, mode: str = "CONTINUATION") -> bool:
    """Spec 3.3: no Tier 3 continuation breakout after 10:30 ET.

    Reversal plays and Tier 1/2 continuations are unaffected. A Tier 3
    continuation before the cutoff is allowed; after it, refused.
    """
    if tier != TIER_3 or mode != "CONTINUATION":
        return True
    if now_et is None:
        return True
    minutes = now_et.hour * 60 + now_et.minute
    return minutes < TIER3_CONT_CUTOFF_MINUTES


def routing_line(tier: int, multiplier: float) -> str:
    """One line for the alert: the tier and its sizing."""
    label = TIER_NAMES.get(tier, "3")
    if multiplier >= 1.0:
        size = "full size"
    else:
        size = f"{multiplier:.2f}x size"
    return f"TIER {label} -- {size}"
