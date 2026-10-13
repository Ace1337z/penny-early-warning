"""Deterministic SEC filing risk classification (fail-closed).

Every strategy and alert reads the same verdict from this module, so a reverse
split or an active offering vetoes the same way everywhere: through the gate,
before the symbol is sent to the AI or to Telegram.

Windows (Spec 1.1):
  - everything is judged against the last 14 days, except
  - reverse splits, which are judged against the last 90 days (a split's effect
    on the share count and the chart does not expire as fast as a news cycle).

A filing whose date cannot be parsed is skipped, not trusted: a 2019 filing
with an unreadable date proves nothing about today and vetoing on it would be
worse than ignoring it, because the reader could not tell why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# Windows
GENERAL_WINDOW_DAYS = 14
REVERSE_SPLIT_WINDOW_DAYS = 90

# Risk classes
ACTIVE_FINANCING = "ACTIVE_FINANCING"
REVERSE_SPLIT_RECENT = "REVERSE_SPLIT_RECENT"
RESALE_OVERHANG = "RESALE_OVERHANG"
ATM_ACTIVE = "ATM_ACTIVE"
CLEAN_OR_PASSIVE = "CLEAN_OR_PASSIVE"

# A HARD VETO ends alert generation immediately, regardless of score.
HARD_VETO_CLASSES = frozenset({ACTIVE_FINANCING, REVERSE_SPLIT_RECENT, RESALE_OVERHANG})
ELEVATED_CLASSES = frozenset({ATM_ACTIVE})

REASONS = {
    ACTIVE_FINANCING: "SEC_VETO_OFFERING",
    REVERSE_SPLIT_RECENT: "SEC_VETO_REVERSE_SPLIT",
    RESALE_OVERHANG: "SEC_VETO_RESALE",
    ATM_ACTIVE: "SEC_ATM_ACTIVE",
}

LABELS = {
    ACTIVE_FINANCING: "active financing (offering)",
    REVERSE_SPLIT_RECENT: "reverse split <90d",
    RESALE_OVERHANG: "resale overhang",
    ATM_ACTIVE: "active ATM program",
    CLEAN_OR_PASSIVE: "no financing risk found",
}

# Forms that always carry a financing story when recent.
OFFERING_FORMS = ("424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8")
SHELF_FORMS = ("S-3", "F-3")          # passive unless ATM/resale is named
REGISTRATION_FORMS = ("S-1", "F-1")   # an S-1 is an offering unless it is resale

# 8-K items that name the financing event itself.
FINANCING_ITEMS = {"3.02", "1.01", "2.03", "2.04"}
SPLIT_ITEMS = {"5.03"}

_RE_WS = re.compile(r"\s+")

# Keywords, matched against a lower-cased, whitespace-collapsed text blob built
# from the filing's form, items and any fetched text. Exact-ish so a clean S-3
# is not vetoed because the word "warrant" appears in a boilerplate cover page.
_ATM_RE = re.compile(
    r"\bat[ -]the[ -]market\b|\batm\b|\bsales agreement\b|"
    r"equity distribution program|controlled equity offering")
_RESALE_RE = re.compile(r"\b(resale|selling stockholder|selling security holder"
                        r"|resale prospectus)\b")
_SPLIT_RE = re.compile(r"\breverse (stock )?split\b|1-for-\d+|\b\d+\s+for\s+1\b")
_FINANCING_RE = re.compile(
    r"\b(offering|underwritten|registered direct|equity line|convertible|"
    r"warrant|private placement|pre-funded|unit offering|purchase agreement)\b")

# Scored worst-first, so a reverse split beats an offering when both are present.
_PRIORITY = [
    REVERSE_SPLIT_RECENT, ACTIVE_FINANCING, RESALE_OVERHANG, ATM_ACTIVE,
]


@dataclass
class ToxScore:
    risk: str = CLEAN_OR_PASSIVE
    reason_code: Optional[str] = None
    label: str = LABELS[CLEAN_OR_PASSIVE]
    evidence: list[str] = field(default_factory=list)
    today: Optional[date] = None

    @property
    def veto(self) -> bool:
        return self.risk in HARD_VETO_CLASSES

    @property
    def elevated(self) -> bool:
        return self.risk in ELEVATED_CLASSES

    def as_dict(self) -> dict:
        return {"class": self.risk, "reason_code": self.reason_code,
                "veto": self.veto, "elevated": self.elevated,
                "evidence": self.evidence[:6]}


def normalize_filing_date(value) -> Optional[date]:
    """Accept ISO (2026-10-13), Finviz (10/13/2026 or 10/13/26) or a date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _blob(filing: dict) -> str:
    parts = [
        str(filing.get("form") or ""),
        str(filing.get("items") or ""),
        str(filing.get("title") or ""),
        str(filing.get("description") or ""),
        str(filing.get("text") or ""),
    ]
    return _RE_WS.sub(" ", " ".join(parts)).strip().lower()


def _split_item_codes(items) -> list[str]:
    """Normalise "1.01, 2.03 9.01" into a set of item codes like "3.02"."""
    out = []
    for tok in re.split(r"[,\s;]+", str(items or "")):
        tok = tok.strip()
        if re.fullmatch(r"\d\.\d{2}", tok):
            out.append(tok)
    return out


def _window_days(risk_class: str) -> int:
    return (REVERSE_SPLIT_WINDOW_DAYS
            if risk_class == REVERSE_SPLIT_RECENT else GENERAL_WINDOW_DAYS)


def classify(filings: Optional[list], *, today: Optional[date] = None) -> ToxScore:
    """Score exactly one risk class for a list of filings.

    Pure and deterministic: same filings, same verdict, every session.
    """
    if not filings:
        return ToxScore()
    today = today or date.today()
    found: dict[str, list[str]] = {}
    skipped = 0

    for filing in filings:
        fdate = normalize_filing_date(filing.get("date"))
        if fdate is None:
            skipped += 1
            continue
        age_days = (today - fdate).days
        if age_days < 0:
            continue                     # a future-dated filing proves nothing now
        blob = _blob(filing)
        form = str(filing.get("form") or "").upper()
        items = _split_item_codes(filing.get("items"))

        # Reverse split: 90-day window, reached through 8-K 5.03 or 6-K.
        if age_days <= _window_days(REVERSE_SPLIT_RECENT):
            split_form = form in ("8-K", "6-K")
            if split_form and (form == "6-K" or "5.03" in items):
                if _SPLIT_RE.search(blob):
                    _add(found, REVERSE_SPLIT_RECENT, filing)

        # Everything else uses the 14-day window.
        if age_days > GENERAL_WINDOW_DAYS:
            continue

        if form in OFFERING_FORMS:
            if _RESALE_RE.search(blob):
                _add(found, RESALE_OVERHANG, filing)
            elif _ATM_RE.search(blob):
                _add(found, ATM_ACTIVE, filing)
            else:
                _add(found, ACTIVE_FINANCING, filing)
        elif form in REGISTRATION_FORMS:
            if _RESALE_RE.search(blob):
                _add(found, RESALE_OVERHANG, filing)
            elif _ATM_RE.search(blob):
                _add(found, ATM_ACTIVE, filing)
            else:
                _add(found, ACTIVE_FINANCING, filing)
        elif form in SHELF_FORMS:
            if _RESALE_RE.search(blob):
                _add(found, RESALE_OVERHANG, filing)
            elif _ATM_RE.search(blob):
                _add(found, ATM_ACTIVE, filing)
            else:
                continue                 # passive shelf: not a veto, not elevated
        elif form == "8-K":
            if "3.02" in items:
                _add(found, ACTIVE_FINANCING, filing)
            elif any(item in FINANCING_ITEMS for item in items):
                if _FINANCING_RE.search(blob):
                    _add(found, ACTIVE_FINANCING, filing)

    for risk in _PRIORITY:
        if risk in found:
            return ToxScore(
                risk=risk, reason_code=REASONS[risk], label=LABELS[risk],
                evidence=found[risk], today=today)
    return ToxScore(today=today)


def _add(found: dict[str, list[str]], risk: str, filing: dict) -> None:
    when = normalize_filing_date(filing.get("date"))
    ev = "%s %s" % (filing.get("form"), when.isoformat() if when else "?")
    items = str(filing.get("items") or "").strip()
    if items:
        ev += " (items %s)" % items
    found.setdefault(risk, [])
    if len(found[risk]) < 3:
        found[risk].append(ev)


def within_windows(filings: Optional[list], *, today: Optional[date] = None) -> list[dict]:
    """The filings a reader should know about: 14 days, splitting aside.

    The 90-day reverse-split lookback stays inside `classify`; this is the list
    that lands on an alert card, so it must stay short and recent.
    """
    if not filings:
        return []
    today = today or date.today()
    out = []
    for f in filings:
        fdate = normalize_filing_date(f.get("date"))
        if fdate is None:
            continue
        if (today - fdate).days > GENERAL_WINDOW_DAYS:
            continue
        out.append(f)
    out.sort(key=lambda f: str(f.get("date") or ""), reverse=True)
    return out