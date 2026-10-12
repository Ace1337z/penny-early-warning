"""Cause-first news: classify, rank, and pick exactly ONE reason a stock moved.

The spec replaces the news block with a single cause. That is not only cosmetic
-- a list of headlines is a list of things the reader has to re-rank themselves at
the moment they are trying to act, and most items in it did not cause the move.

Classification is by what the item actually *is*, because that is what decides
whether the move can continue:

  HARD (1.0)    an event that changes the business or the share count
  MEDIUM (0.6)  a real event with a counterparty or a number attached
  SOFT (0.25)   routine updates that historically do not move a float
  DILUTION      an offering priced below the open -- a long blocker, never a cause

A catalyst alone is not a setup: `catalyst_volume` requires volume behind it.
The weight here ranks causes *among those that already passed*.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

HARD = "HARD"
MEDIUM = "MEDIUM"
SOFT = "SOFT"
DILUTION = "DILUTION"
UNEXPLAINED = "UNEXPLAINED"

# Spec 2.0 A: mechanism classes. A tape mechanism is what the pipeline names
# when the news names nothing -- it is a label on the same evidence the
# detectors read, not a cause -- but it still decays, because a
# quiet-accumulation read from three hours ago is weaker evidence than a
# three-minute-old one. Each decays faster than a HARD catalyst (180m): the
# tape that proves a mechanism can reverse in minutes; an FDA approval cannot.
MECHANISM_HALF_LIFE_MIN = {
    "SILENT_ACCUMULATION": 60.0,
    "SYMPATHY": 45.0,
    "SQUEEZE": 90.0,
}

WEIGHTS = {HARD: 1.0, MEDIUM: 0.6, SOFT: 0.25, DILUTION: -1.0}

# A cause must also be recent enough to plausibly have *caused* the move rather
# than described it. The decay curve is the primary test; this is the outer bound
# that stops a HARD catalyst at 09:30 being named as the cause of a 15:00 move.
CAUSE_MAX_AGE_H = 4.0

# Feature 15: exponential decay replaces the old flat "24h and still counts"
# window. A press release at 09:35 and one at 15:00 are not the same setup, and
# a flat window treated them as identical right up until the alert stopped. The
# half-life is how long a catalyst keeps half its pull:
#
#   HARD     180m   an FDA approval still moves a float three hours later
#   MEDIUM   120m   a priced partnership is news, then it is history
#   SOFT      30m   a conference presentation is over before the second candle
#   DILUTION  no decay  a dilution does not get less bad with age
#
# At two half-lives the weight is a quarter; a HARD catalyst at 360 minutes is
# therefore worth what a fresh SOFT one is, which is exactly right -- by then
# that is all it is.
HALF_LIFE_MIN = {HARD: 180.0, MEDIUM: 120.0, SOFT: 30.0, DILUTION: 0.0}

# Outlets that are usually first and usually right about a press release.
SOURCE_TRUST = {"benzinga": 0.9, "pr newswire": 0.95, "business wire": 0.95,
                "globe newswire": 0.95, "accesswire": 0.9, "sec": 1.0,
                "nasdaq": 0.8, "reuters": 0.95, "yahoo": 0.6, "finviz": 0.5}

_HARD = re.compile(
    r"\b(fda (approv|clear)|approval|approved|clearance|approved|orphan drug|"
    r"breakthrough therapy|contract (award|won)|award(s|ed)? (a |an )?\$?\d|(awarded|wins|won) a|tender offer| "
    r"acquisition|acquires|merger|reverse split (effective|executed|approved)|"
    r"beats (estimates|expectations)|tops estimates|raises guidance|"
    r"guidance raised|upbeat guidance|profit warning|going concern|delist)", re.I)

_MEDIUM = re.compile(
    r"\b(partnership|collaboration|agreement with|licens(e|ing) deal|"
    r"insider (buy|purchase)|buys shares|purchased shares|"
    r"analyst (upgrade|raises)|price target|upgrades to|"
    r"patent (grant|awarded)|issued patent|"
    r"offering priced|prices offering|public offering priced|"
    r"shares priced at|units priced at)", re.I)

_SOFT = re.compile(
    r"\b(provides update|to present at|will present|regains compliance|"
    r"regained compliance|conference|webcast|investor day|"
    r"schedule|upcoming|announces (a )?(date|event))\b", re.I)

# Word order varies a great deal in real press-release headlines ("prices $12M
# offering", "offering priced at", "$12M public offering"), so this matches the
# noun and the pricing verb independently rather than as fixed phrases.
_DILUTION = re.compile(
    r"\b(offering|offerings|underwrit\w*)\b|"
    r"reverse split|going concern|"
    r"\bdelist\w*\b|\bnasdaq deficiency\b|"
    r"\b(prices|priced)\b.{0,40}\b(share|shares|unit|units|common stock)\b|"
    r"\b(share|shares|unit|units|common stock)\b.{0,40}\b(price|priced)\b|"
    r"\bdilutive\b|\bATM\b", re.I)


@dataclass
class Cause:
    kind: str
    text: str
    source: str = ""
    url: str = ""
    age_minutes: Optional[float] = None
    cls: str = UNEXPLAINED
    weight: float = 0.0
    confidence: str = "low"
    supporting: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _age_minutes(item: dict, now_ts: Optional[float]) -> Optional[float]:
    """Minutes since publication. `age` is precomputed by the Enricher as "2h"."""
    age = item.get("age")
    if age:
        m = re.match(r"(\d+)\s*([smhd])", str(age))
        if m:
            n, unit = int(m.group(1)), m.group(2)
            return n * {"s": 1 / 60.0, "m": 1.0, "h": 60.0, "d": 1440.0}[unit]
    published = str(item.get("published") or "")
    if published.isdigit():
        ts = int(published)
        if ts > 10_000_000_000:
            ts //= 1000
        return max(0.0, (time.time() - ts) / 60.0)
    return None


def classify(text: str) -> str:
    """One class per item. Negative checks run first: an offering PR often also
    contains the word "approval" somewhere in the body."""
    t = _norm(text)
    if _DILUTION.search(t):
        return DILUTION
    if _HARD.search(t):
        return HARD
    if _MEDIUM.search(t):
        return MEDIUM
    if _SOFT.search(t):
        return SOFT
    return SOFT


def trust_of(source: str) -> float:
    return SOURCE_TRUST.get(_norm(source).lower(), 0.7)


def effective_weight(base_weight: float, cls: str, age_minutes: Optional[float],
                     half_life: Optional[dict] = None) -> float:
    """base_weight * 2 ** (-age / half_life): the weight halves every half_life.

    Written as 2**(-t/T) rather than exp(-t/T) because the config key is named
    HALF_LIFE and the spec's own test ("HARD catalyst at 180m has ~0.5 effective
    weight") only holds if 180 minutes is literally the half-life. exp(-t/T)
    would put it at 0.37 and make every key a lie about what it controls.

    DILUTION has a half-life of zero, which would divide by zero, so it is
    handled before the formula: a dilution never decays. So does an item with an
    unknown age -- an undated headline keeps full weight rather than being
    silently discounted to nothing, because the honest read is "we do not know
    how old this is", and the price action already priced in whatever it was.
    """
    half = (half_life or HALF_LIFE_MIN)
    if cls == DILUTION or age_minutes is None or float(age_minutes) < 0:
        return float(base_weight)
    if cls in MECHANISM_HALF_LIFE_MIN:
        half = MECHANISM_HALF_LIFE_MIN
    hl = float(half.get(cls, 0) or 0)
    if hl <= 0:
        return float(base_weight)
    return float(base_weight) * (2.0 ** (-float(age_minutes) / hl))


def rank_catalysts(b, max_age_min: float = 1440.0) -> tuple:
    """Rank the news for this symbol.

    Returns (best, ranked, dilution_reason). `dilution_reason` is non-empty when
    an offering is priced below today's open, which the gate treats as a hard
    long blocker.
    """
    ranked: list[dict] = []
    dilution_reason = ""
    now_ts = b.now_et.timestamp() if b.now_et is not None else None
    prev = b.prev_close or 0.0
    open_px = b.open_price or prev

    for item in (b.news or []):
        headline = _norm(item.get("title"))
        if not headline:
            continue
        age = _age_minutes(item, now_ts)
        if age is not None and age > max_age_min:
            continue
        cls = classify(headline)
        src = _norm(item.get("source"))
        base = WEIGHTS[cls]
        entry = {
            "headline": headline,
            "source": src,
            "url": str(item.get("url") or ""),
            "published": str(item.get("published") or ""),
            "age_minutes": age if age is not None else 9999.0,
            "class": cls,
            "base_weight": base,
            "weight": effective_weight(base, cls, age),
            "trust": trust_of(src),
        }
        if cls == DILUTION:
            # Priced below the open is the disqualifying case. An offering
            # announced but not yet priced is bearish but not immediately fatal,
            # so only a price under the open blocks.
            # Only a per-share price counts. "$12M offering" contains a 12.00 in
            # most formats and would otherwise always look "below the open".
            nums = [float(n) for n in re.findall(
                r"(?:\$|per share[^\d]{0,12})?(\d\.\d{1,2})(?![\dM%])", headline)]
            nums = [n for n in nums if 0.01 <= n < 1000]
            if nums and open_px and min(nums) < open_px:
                dilution_reason = (f"offering priced at ${min(nums):.2f} "
                                   f"below the ${open_px:.2f} open")
            entry["weight"] = -1.0
        entry["effective_weight"] = entry["weight"]
        # Rank by decayed weight times outlet trust. Ranking on raw weight would
        # put a six-hour-old approval above a fresh one from a worse outlet
        # purely because approvals weigh more, which is the opposite of right.
        entry["rank"] = entry["weight"] * entry["trust"]
        ranked.append(entry)

    ranked.sort(key=lambda e: (e["effective_weight"], e["rank"]), reverse=True)
    best = ranked[0] if ranked else None
    return best, ranked, dilution_reason


def extract_cause(catalysts: list[dict], b, filings: Optional[list] = None) -> Cause:
    """Exactly ONE cause. Never a list, never a dump.

    A headline only counts if it plausibly *caused* the move: recent, a real class,
    and close enough in time to the volume to be the trigger rather than
    background colour. Otherwise the honest answer is "none found", and saying so
    is more useful than attaching a plausible-looking PR.
    """
    ranked = catalysts or []
    max_age_h = CAUSE_MAX_AGE_H
    for e in ranked:
        if e["class"] == DILUTION:
            continue
        if e["age_minutes"] > max_age_h * 60:
            continue
        # The 4h window above is the outer bound. Inside it the *class* decides:
        # a HARD event (an FDA approval, a tender offer) is nameable for the
        # whole window even after its decayed weight drops below the floor --
        # decay is a ranking signal, not a nameability test. A HARD catalyst at
        # the 4h edge is 0.40, and the old flat 0.6 floor silently dropped it,
        # which is how a 3h-old tender offer came back as "unidentified". A
        # MEDIUM event still has to hold the floor; SOFT never qualifies.
        if e["class"] != HARD and e["weight"] < 0.6:
            continue
        conf = "high" if (e["weight"] >= 1.0 and e["trust"] >= 0.9) else "medium"
        supporting = ""
        if filings:
            recent = [f for f in filings if str(f.get("form", "")).upper() in
                      ("8-K", "6-K", "S-1", "424B5", "S-3")]
            if recent:
                f = recent[0]
                supporting = f"{f.get('form')} filed {f.get('date')}"
                if f.get("items"):
                    supporting += f" items {f['items']}"
        return Cause(kind="CATALYST",
                     text=e["headline"], source=e["source"], url=e["url"],
                     age_minutes=e["age_minutes"], cls=e["class"], weight=e["weight"],
                     confidence=conf, supporting=supporting)

    detail = []
    if b.float_shares:
        detail.append(f"low float {b.float_shares / 1e6:.1f}M")
    if b.short_pct:
        detail.append(f"short float {b.short_pct:.1f}%")
    if b.volx_of(5):
        detail.append(f"volx {b.volx_of(5):.1f}")
    read = ("move is technical (" + ", ".join(detail) + ")") if detail else "move is technical"
    return Cause(kind=UNEXPLAINED,
                 text="none found in the last 24h",
                 source="", url="", age_minutes=None, cls=UNEXPLAINED, weight=0.0,
                 confidence="low",
                 supporting=f"{read} - no fundamental driver. Treat as unconfirmed; "
                            f"expect fast reversals.")


def cause_lines(cause: Cause) -> list[str]:
    """Render the WHY IT MOVED block. Exactly one CAUSE line, always."""
    if cause.kind == UNEXPLAINED:
        return [f"CAUSE: {cause.text}.", f"READ: {cause.supporting}"]
    lines = [f"CAUSE: {cause.text}"]
    age = ""
    if cause.age_minutes is not None:
        age = f", {_age_text(cause.age_minutes)} ago"
    src = cause.source or "unknown"
    if cause.url:
        lines.append(f"SOURCE: {src}{age} - {cause.url}")
    else:
        lines.append(f"SOURCE: {src}{age}")
    detail = f", {cause.cls.lower()}, effective weight {cause.weight:.2f}" if cause.weight else ""
    lines.append(f"CONFIDENCE: {cause.confidence}{detail}")
    if cause.supporting:
        lines.append(f"SUPPORTING: {cause.supporting}")
    return lines


def _age_text(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"
