"""Alert rendering.

The shape of an alert is a decision about what the reader has time for. Someone
reading a Telegram message on a phone, deciding in seconds, can absorb a setup
name, one reason it moved, and one verdict. They cannot absorb a technicals
table and a Fibonacci grid and a five-model consensus and still act.

So this module renders:

  Alert 1  the trigger. What fired, the numbers behind it, which strategy
           recognised it, and the trigger's own line.
  Alert 2  why it moved -- exactly one cause -- the market backdrop, the
           evidence, and the decision.

The technicals, the Fibonacci grid and the trade plan are still *computed*
inside enrichment, because detectors that need them read them there. They are
simply no longer printed. Entry and exit levels reach the reader through the AI
decision block instead, which states them as a judgement rather than as a grid
of derived numbers they would have to interpret themselves.

Cause-first news is the other half of this. A list of headlines is a list of
things to re-rank at the moment of acting, and most items in it did not cause
the move, so exactly one cause is named -- outlet, timestamp, URL, confidence
-- or the move is reported as unexplained. See `strategies.news_cause`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .enrich import Enricher
from .scoring import (PHASE_EXTENDED, PHASE_FADING, TIER_CONFIRMED, TIER_EARLY,
                      TIER_NONE, TIER_WATCH, Metrics)
from .strategies import cause_lines, extract_cause, gate_summary_lines, rank_catalysts
from .strategies.news_cause import UNEXPLAINED
from .util import compact, money, num, pct, safe_div

log = logging.getLogger(__name__)

MAX_ALERT1 = 1200
MAX_ALERT2 = 3900

TIER_NAMES = {TIER_CONFIRMED: "CONFIRMED", TIER_EARLY: "EARLY",
              TIER_WATCH: "WATCH", TIER_NONE: "NONE"}

DISCLAIMER = ("Screener and decision support only. No orders are placed. "
              "Not investment advice.")


def tier_name(tier: int) -> str:
    return TIER_NAMES.get(tier, "NONE")


def phase_tag(m: Metrics) -> str:
    if getattr(m, "phase", "") == PHASE_EXTENDED:
        return "  [EXTENDED]"
    if getattr(m, "phase", "") == PHASE_FADING:
        return "  [FADING]"
    return ""


# --------------------------------------------------------------------------
# the small HTML/plain renderer
# --------------------------------------------------------------------------

class _Msg:
    """Telegram message builder that either escapes for HTML or leaves it plain."""

    def __init__(self, html: bool = False):
        self.html = html
        self.parts: list[str] = []

    def line(self, *segs) -> None:
        """A line is a sequence of (style, text) or plain strings."""
        buf = []
        for seg in segs:
            if isinstance(seg, tuple):
                style, text = (list(seg) + [""])[:2]
                buf.append(self._style(str(text), style))
            else:
                buf.append(self._style(str(seg), ""))
        self.parts.append("".join(buf))

    def _style(self, text: str, style: str) -> str:
        if not self.html or not style:
            return self._esc(text)
        if style == "i":
            return f"<i>{self._esc(text)}</i>"
        if style == "b":
            return f"<b>{self._esc(text)}</b>"
        if style == "code":
            return f"<code>{self._esc(text)}</code>"
        if style == "pre":
            return f"<pre>{self._esc(text)}</pre>"
        return self._esc(text)

    @staticmethod
    def _esc(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def blank(self) -> None:
        self.parts.append("")

    def text(self) -> str:
        return "\n".join(self.parts)


def divider(label: str) -> str:
    """The `──── LABEL ────` rule used between sections."""
    return f"──── {label} " + "─" * max(2, 26 - len(label))


def arrow_for(pct_value: Optional[float]) -> str:
    if pct_value is None:
        return "•"
    return "▲" if pct_value >= 0 else "▼"


def bar(frac: float, width: int = 10) -> str:
    frac = max(0.0, min(1.0, frac or 0.0))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def _pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:+.1f}%"


def _setup_reason(m: Metrics) -> str:
    """Why this tier, in one line, for the alert header."""
    if m.tier >= TIER_CONFIRMED:
        return ""
    if m.volx.get(15, 0) < 3:
        return f"volx {num(m.volx.get(15, 0), 1)}x is under 3x"
    if m.pct_vs_close is None:
        return "no prior close"
    if m.vwap and m.price < m.vwap:
        return "below VWAP"
    if m.pct_vs_close * 100 < 10:
        return f"only {_pct(m.pct_vs_close)} vs close"
    return ""


def _fast_reason(m: Metrics) -> str:
    """The numbers that fired the fast lane, spelled out.

    This exists because the fast lane fires below every tier, so the header says
    "FAST" and the usual tier explanation is absent. Without this the reader has
    a number with no stated trigger.
    """
    parts = [f"{_pct(m.rise.get(3, 0))} in 3m",
             f"volx {num(m.volx.get(3, 0), 1)}x",
             f"{_pct(m.rise.get(5, 0))} in 5m",
             f"volx {num(m.volx.get(5, 0), 1)}x"]
    return " | ".join(parts)


def _pulse(m: Metrics) -> str:
    """One-line read of the shape of the move right now."""
    bits = []
    if m.rise.get(3, 0) > 0.02:
        bits.append("accelerating")
    elif m.rise.get(5, 0) > 0.03 and m.rise.get(3, 0) < 0.005:
        bits.append("front-loading")
    elif m.from_high is not None and m.from_high < -0.05:
        bits.append("off the high")
    if m.vwap and m.price > m.vwap:
        bits.append("above VWAP")
    else:
        bits.append("below VWAP")
    return "PULSE: " + ", ".join(bits)


# --------------------------------------------------------------------------
# cause-first news
# --------------------------------------------------------------------------

def _mechanism_block(src, gate=None) -> list[str]:
    """The MECHANISM line for a move with no named cause (Spec 2.0 A).

    Exactly one mechanism, or the honest "unidentified": a tape mechanism is
    what the detectors can prove without a headline, and it replaces the old
    "CAUSE: none found" hole. The gate's own results are preferred -- they are
    the verdict the reader already saw -- and a fresh run of the mechanism
    detectors covers the no-gate path.
    """
    from .strategies.detectors import MECHANISM_NAMES, silent_accumulation, \
        sympathy_move, short_squeeze_mechanics
    mech = None
    if gate is not None:
        for r in getattr(gate, "strategies", None) or []:
            if getattr(r, "verdict", "") == "PASS" and r.name in MECHANISM_NAMES:
                mech = r
                break
    elif src is not None:
        for fn in (silent_accumulation, sympathy_move, short_squeeze_mechanics):
            try:
                r = fn(src)
            except Exception:  # noqa: BLE001
                r = None
            if r is not None and r.verdict == "PASS":
                mech = r
                break
    label = MECHANISM_LABELS.get(mech.name, mech.name) if mech is not None else ""
    if mech is not None:
        detail = ", ".join((mech.reasons or [])[:2]) or "no news 24h"
        return [f"MECHANISM: {label} ({detail})"]
    return ["MECHANISM: unidentified \u2014 treat as speculative"]


MECHANISM_LABELS = {
    "silent_accumulation": "silent accumulation",
    "sympathy_move": "sympathy move",
    "short_squeeze_mechanics": "short squeeze mechanics",
}


def _news_checked(m: Metrics, enr: Optional[Enrichment], bundle=None) -> bool:
    """Was the news actually fetched for this symbol? (Not-checked stays quiet.)"""
    if bundle is not None and getattr(bundle, "news", None) is not None:
        return True
    return enr is not None and enr.news is not None


def _avg_trade_size(m: Metrics) -> Optional[float]:
    """Dollars per print over the last 15m -- retail churn vs real size."""
    trades = getattr(m, "trades_15m", None)
    if not trades or trades <= 0:
        return None
    return round((m.dollar_vol.get(15, 0.0) or 0.0) / trades, 0)


def _float_rotation(m: Metrics, enr: Optional[Enrichment]) -> Optional[dict]:
    """Float turnover so far, and whether the pace is still rising."""
    if enr is None or not enr.float_shares or enr.float_shares <= 0 or not m.cum_volume:
        return None
    rot = m.cum_volume / enr.float_shares
    vol15_shares = ((m.dollar_vol.get(15, 0.0) / m.price) if m.price else 0.0)
    rot_15m_ago = max(0.0, rot - (vol15_shares / enr.float_shares))
    return {"rate": round(rot, 2), "rising": bool(rot > rot_15m_ago)}


def _news_checked(m: Metrics, enr: Optional[Enrichment], bundle=None) -> bool:
    """Was the news actually fetched for this symbol? (Not-checked stays quiet.)"""
    if bundle is not None and getattr(bundle, "news", None) is not None:
        return True
    return enr is not None and enr.news is not None


def _cause_bundle(m: Metrics, enr: Optional[Enrichment], news: list):
    """A minimal Bundle for cause ranking, matching what the gate reads."""
    from .strategies.base import Bundle
    src = Bundle(symbol=m.symbol, price=m.price, prev_close=m.prev_close,
                 open_price=(enr.finviz_row or {}).get("Open") if enr and enr.finviz_row else None,
                 float_shares=enr.float_shares if enr is not None else None,
                 short_pct=None, news=news,
                 vwap=m.vwap, volx=dict(m.volx or {}), rise=dict(m.rise or {}),
                 dollar_vol=dict(m.dollar_vol or {}))
    try:
        src.open_price = float(src.open_price)
    except (TypeError, ValueError):
        src.open_price = None
    return src


def named_cause(m: Metrics, enr: Optional[Enrichment], bundle=None) -> Optional[dict]:
    """The named cause as a plain dict, or None (Spec 2.0 B: AI facts).

    {"headline", "outlet", "age_minutes", "class"} -- the raw fields the model
    needs to judge the driver, without the renderer's opinion attached.
    """
    news = None
    if bundle is not None and getattr(bundle, "news", None) is not None:
        news = bundle.news
    elif enr is not None:
        news = enr.news
    if news is None:
        return None
    try:
        src = bundle if bundle is not None else _cause_bundle(m, enr, news)
        _, ranked, _ = rank_catalysts(src)
        cause = extract_cause(ranked, src, filings=enr.filings if enr else None)
    except Exception:  # noqa: BLE001
        return None
    if cause.kind == UNEXPLAINED:
        return None
    return {"headline": cause.text, "outlet": cause.source,
            "age_minutes": cause.age_minutes, "class": cause.cls,
            "url": cause.url}


def cause_block(m: Metrics, enr: Optional[Enrichment], bundle=None) -> list[str]:
    """The WHY IT MOVED body: exactly one cause, or exactly one mechanism.

    Spec 2.0 A: cause is a label, not a gate. A move with no named catalyst is
    not unexplained -- the detectors name the mechanism that ran it
    (accumulation, sympathy, squeeze), and when even that cannot be said, the
    block says so honestly instead of the old "none found" shrug.
    """
    news = None
    if bundle is not None and getattr(bundle, "news", None) is not None:
        news = bundle.news
    elif enr is not None:
        news = enr.news
    if news is None:
        return ["CAUSE: not checked yet."]

    src = bundle
    if src is None:
        # Build a minimal Bundle from the enrichment so the ranking and the age
        # filters apply the same way they do inside the gate. It has to carry the
        # news: without it there is nothing to rank and every alert would read
        # "none found" on a stock that had just been handed a catalyst.
        src = _cause_bundle(m, enr, news)

    try:
        _, ranked, _ = rank_catalysts(src)
        cause = extract_cause(ranked, src, filings=enr.filings if enr else None)
        if cause.kind != UNEXPLAINED:
            return cause_lines(cause)
        # Spec 2.0 A: no cause named -- name the mechanism that ran the move,
        # or say honestly that none can be named. The mechanism block is the
        # *reading*; a fetched headline is still evidence, so the freshest one
        # is shown below it rather than discarded. Without this a real PR that
        # ranked SOFT or fell outside the naming window vanished entirely, and
        # the alert looked like the feed had returned nothing.
        lines = _mechanism_block(src)
        headline = _news_fallback(ranked)
        if headline:
            lines = lines + headline
        return lines
    except Exception as exc:  # noqa: BLE001
        log.debug("cause block failed for %s: %s", m.symbol, exc)
        return ["CAUSE: could not determine."]


def _news_fallback(ranked: list) -> list[str]:
    """The freshest fetched headline, shown when none was named the cause.

    One line, not a dump: the same discipline the cause block keeps. The class
    is printed so the reader can see the system read the headline and judged it
    background (SOFT) or older than the naming window, rather than missing it.
    """
    if not ranked:
        return []
    top = ranked[0]
    age = top.get("age_minutes")
    when = ""
    if isinstance(age, (int, float)) and age < 9999:
        when = f", {_age_text(float(age))} ago"
    src = top.get("source") or "unknown"
    cls = str(top.get("class") or "").lower()
    return [f"NEWS ({cls}){when} - {src}: {top.get('headline', '')}"]


def _age_text(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def book_line(m: Metrics) -> str:
    """Bid, ask, spread and 15-minute dollar volume on one line.

    The reader is about to place an order on a sub-$10 stock, so the cost of
    getting in and out is part of the setup, not a footnote. Absent book data
    says so plainly rather than printing a spread of zero.
    """
    dollar = m.dollar_vol.get(15)
    tail = "" if dollar is None else " | 15m " + money(dollar)
    if m.bid is None or m.ask is None:
        return "book n/a" + tail
    mid = (m.bid + m.ask) / 2
    if not mid:
        return "book n/a" + tail
    spread = (m.ask - m.bid) / mid * 100
    return f"bid {money(m.bid)} / ask {money(m.ask)} (spread {spread:.1f}%){tail}"


# --------------------------------------------------------------------------
# alert 1 -- the trigger
# --------------------------------------------------------------------------

def format_alert1(m: Metrics, *, shariah_line: str = "Shariah: checking...",
                  pct_rank: Optional[int] = None, resumed: bool = False,
                  fast: bool = False, html: bool = False, gate=None,
                  no_news: bool = False) -> str:
    """Instant Alert 1: no AI, no enrichment. The trigger and nothing else."""
    msg = _Msg(html)
    head = f"{tier_name(m.tier)}  {m.symbol}  {money(m.price)}{phase_tag(m)}"
    if no_news:
        # Spec 2.0 A: a cause-free alert says so in the headline. The reader
        # decides in the first second whether to open a tape-driven setup.
        head += "  [NO-NEWS]"
    if resumed:
        head += "  RESUMED FROM HALT"
    elif fast:
        # A fast-lane alert fires below every tier. Headlining it "NONE" is
        # self-contradictory -- the reader just received an alert saying the
        # stock is none of the alert tiers.
        head = f"FAST  {m.symbol}  {money(m.price)}{phase_tag(m)}"
    msg.line(("b", f"ALERT: {head}"))
    msg.line(book_line(m))

    if fast:
        # This fired on the 3-5 minute lane, so the 3m and 5m moves are the ones
        # that matter and lead. The wider windows still follow: a reader deciding
        # on this needs the 60m context too, just not before the trigger.
        msg.line(("b", _pct(m.pct_vs_close)), " vs close | ",
                 _pct(m.rise.get(3, 0)), " 3m | ", _pct(m.rise.get(5, 0)), " 5m | ",
                 _pct(m.rise.get(15, 0)), " 15m | ", _pct(m.rise.get(60, 0)), " 60m",
                 "   [FAST]")
    else:
        # Spec 11.1: the alert path reads 3m and 5m only. 15m and 60m are still
        # computed for the detectors and the tier path, but printing them here is
        # what made the header read like a summary of a move that was already over.
        msg.line(("b", _pct(m.pct_vs_close)), " vs close | ",
                 _pct(m.rise.get(3, 0)), " 3m | ", _pct(m.rise.get(5, 0)), " 5m")

    vol = f"vol {num(m.volx.get(15, 0), 1)}x normal | day {compact(m.cum_volume)}"
    vol += " | above VWAP" if m.vwap and m.price > m.vwap else " | below VWAP"
    msg.line(vol)

    low_line = (f"low {int(m.minutes_since_low)}m ago"
                if m.minutes_since_low is not None else "low n/a")
    low_line += f" | score {m.score:.0f}"
    if pct_rank:
        low_line += f" | #{pct_rank} gainer"
    msg.line(low_line)

    if gate is not None:
        for line in gate_block(gate, fast=fast):
            msg.line(line)
    msg.line(shariah_line)

    text = msg.text()
    if len(text) > MAX_ALERT1:
        text = _truncate(text, MAX_ALERT1)
    return text


def gate_block(gate, fast: bool = False) -> list[str]:
    """STRATEGY and ENTRY TRIGGER, the two lines that name the setup.

    A number is not a setup. This is what makes the alert say *which* pattern
    was recognised, and when a trigger fired, the specific price action to wait
    for before entering.
    """
    if gate is None:
        return []
    lines = []
    summary = gate_summary_lines(gate, limit=3)
    if summary:
        lines.append(("STRATEGY: " + " | ".join(summary)).strip())
    if gate.entry_trigger:
        trig = gate.by_name(gate.entry_trigger)
        detail = f" ({trig.reasons[0]})" if trig and trig.reasons else ""
        lines.append(f"ENTRY TRIGGER: {gate.entry_trigger}{detail}")
    elif fast:
        lines.append("ENTRY TRIGGER: live burst - enter on a hold above the last 3m high")
    else:
        lines.append("ENTRY TRIGGER: none - enter on a pullback that holds VWAP")
    return lines


# --------------------------------------------------------------------------
# alert 2 -- cause, evidence, decision
# --------------------------------------------------------------------------

def format_alert2(m: Metrics, enr: Optional[Enrichment], market, *,
                  shariah_lines: Optional[list[str]] = None,
                  gate=None, bundle=None, decision: str = "",
                  fast: bool = False, html: bool = False) -> str:
    """Alert 2: the cause first, then the evidence, then one decision.

    No technicals block, no Fibonacci grid, no standalone trade plan, no
    separate VERDICT heading. The verdict lives inside the AI DECISION block,
    alongside the entry and exit levels that justify it -- a verdict on its own
    is not actionable, and a plan on its own is not a decision.
    """
    msg = _Msg(html)
    pv = m.pct_vs_close * 100 if m.pct_vs_close is not None else None

    # -- headline -----------------------------------------------------------
    msg.line(("b", f"DETAIL  {arrow_for(pv)} {m.symbol}  {money(m.price)}  "
                   f"{_pct(m.pct_vs_close)} vs close{phase_tag(m)}"))
    pulse = _pulse(m)
    if _news_checked(m, enr, bundle) and named_cause(m, enr, bundle) is None:
        # Spec 2.0 A: the cause-free move says so on the pulse line.
        pulse += ", no catalyst \u2014 tape-driven"
    msg.line(pulse)
    setup = "SETUP: FAST" if fast else f"SETUP: {tier_name(m.tier)}"
    reason = _setup_reason(m)
    if fast:
        setup += " - live 3-5m burst, the 15m/60m tier gates have not filled yet"
    elif reason:
        setup += f" - below the alert bar ({reason})"
    elif m.tier >= TIER_EARLY:
        setup += " - qualifies for alerts"
    msg.line(("b", "SETUP: "), setup.split("SETUP: ", 1)[1])

    if gate is not None:
        summary = gate_summary_lines(gate, limit=6)
        if summary:
            msg.line(("b", "STRATEGY: "), " | ".join(summary))

    # -- WHY IT MOVED: first, and exactly one cause -------------------------
    msg.blank()
    msg.line(divider("WHY IT MOVED"))
    for line in cause_block(m, enr, bundle):
        if line.startswith("CAUSE:"):
            msg.line(("b", line))
        else:
            msg.line(line)

    if fast:
        msg.blank()
        msg.line(divider("WHY NOW"))
        msg.line("3-5 minute burst - fired ahead of the 15m/60m gates")
        msg.line(_fast_reason(m))

    # -- market backdrop ----------------------------------------------------
    if market is not None:
        msg.blank()
        msg.line(divider("MARKET BACKDROP"))
        msg.line(market.headline())

    # -- evidence -----------------------------------------------------------
    msg.blank()
    msg.line(divider("EVIDENCE"))
    if enr is not None:
        msg.line(Enricher.verification_line(enr))

    if enr is not None and enr.filings:
        msg.blank()
        msg.line(("b", "FILINGS (last 5 days)"))
        for f in enr.filings[:5]:
            extra = f" items {f['items']}" if f.get("items") else ""
            msg.line("- ", f"{f.get('form')} {f.get('date')}{extra}")

    if enr is not None:
        short_text = Enricher.short_text(enr)
        if short_text:
            msg.blank()
            msg.line(("b", "SHORT/FLOAT: "), short_text)

        if enr.insiders:
            msg.blank()
            msg.line(("b", "INSIDERS"))
            for i in enr.insiders[:3]:
                msg.line("- ", f"{i.get('date')} {i.get('owner')} {i.get('transaction')} "
                               f"{i.get('value')}")

    if shariah_lines:
        msg.blank()
        for line in shariah_lines:
            msg.line(line)

    # -- the single AI decision ---------------------------------------------
    msg.blank()
    msg.line(divider("AI DECISION"))
    if decision:
        for line in _ai_lines(decision):
            msg.line(line)
    else:
        msg.line("AI unavailable - the strategy block above is the whole case.")

    if enr is not None and enr.missing:
        msg.blank()
        msg.line(("b", "MISSING: "), ", ".join(enr.missing))

    msg.blank()
    msg.line(("i", DISCLAIMER))
    text = msg.text()
    if len(text) > MAX_ALERT2:
        text = _truncate(text, MAX_ALERT2)
    return text


def _ai_lines(text: str) -> list[str]:
    return [ln for ln in str(text).split("\n") if ln.strip()]


def _truncate(text: str, limit: int) -> str:
    """Cut over-long text on a line boundary rather than mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.6:
        cut = cut[:nl]
    return cut.rstrip() + "\n[truncated]"


# --------------------------------------------------------------------------
# build feed (unchanged in spirit: quiet accumulations, not alerts)
# --------------------------------------------------------------------------

def _build_detail(m: Metrics, score: float, above_vwap: bool) -> str:
    bits = [f"{_pct(m.pct_vs_close)} vs close",
            f"volx {num(m.volx.get(15, 0), 1)}x"]
    if m.vwap:
        bits.append("above VWAP" if above_vwap else "below VWAP")
    if m.from_high is not None:
        bits.append(f"{_pct(m.from_high)} from high")
    return " | ".join(bits) + f" | score {score:.0f}"


def format_build_feed(signals, *, limit: int = 6, html: bool = True) -> str:
    msg = _Msg(html)
    msg.line(("b", "BUILDS"), f" - quiet accumulation, {limit} max")
    for i, sig in enumerate(signals[:limit], 1):
        m = sig.metrics
        above = bool(m.vwap and m.price > m.vwap)
        msg.line(f"{i}. ", ("b", f"{m.symbol} "), money(m.price), "  ",
                 _build_detail(m, sig.score, above))
    msg.blank()
    msg.line(("i", DISCLAIMER))
    return msg.text()


def format_build_digest(signals, *, seconds: int = 0, limit: int = 6,
                        html: bool = True) -> str:
    if not signals:
        return ""
    msg = _Msg(html)
    msg.line(("b", "BUILDS"), f" ({limit} shown, refreshed {seconds}s)")
    for i, sig in enumerate(signals[:limit], 1):
        m = sig.metrics
        above = bool(m.vwap and m.price > m.vwap)
        msg.line(f"{i}. ", ("b", f"{m.symbol} "), money(m.price), "  ",
                 _build_detail(m, sig.score, above))
    return msg.text()


# --------------------------------------------------------------------------
# AI facts -- the single input to the one AI call
# --------------------------------------------------------------------------

def build_ai_facts(m: Metrics, enr: Optional[Enrichment], market, *,
                   session: str, pct_rank: Optional[int] = None,
                   time_to_horizon: Optional[dict] = None,
                   gate=None, bundle=None, context: Optional[dict] = None) -> dict:
    """Everything the AI is told, as a dict it will be handed verbatim.

    The point of a single call is that the model sees one view of the setup. A
    panel that each read a different subset could contradict itself in the same
    message; here there is exactly one set of facts and one verdict.
    """
    facts = {
        "symbol": m.symbol,
        "price": m.price,
        "prev_close": m.prev_close,
        "pct_vs_close": _pct(m.pct_vs_close),
        "rise3": _pct(m.rise.get(3, 0)),
        "rise5": _pct(m.rise.get(5, 0)),
        "rise15": _pct(m.rise.get(15, 0)),
        "rise60": _pct(m.rise.get(60, 0)),
        "volx3": num(m.volx.get(3, 0), 1),
        "volx5": num(m.volx.get(5, 0), 1),
        "volx15": num(m.volx.get(15, 0), 1),
        "dollar_vol_15m": money(m.dollar_vol.get(15, 0)),
        "day_volume": compact(m.cum_volume),
        "vwap": m.vwap,
        "above_vwap": bool(m.vwap and m.price > m.vwap),
        "from_high": _pct(m.from_high),
        "minutes_since_low": (int(m.minutes_since_low)
                              if m.minutes_since_low is not None else None),
        "session": session,
        # Spec 2.0 B: the tier and the gate verdict are gone on purpose. The
        # model judges the setup, not the gate's opinion of the setup -- feeding
        # the verdict back is what produced the mirror (criterion 7).
        "cause": cause_block(m, enr, bundle),
        "cause_detail": named_cause(m, enr, bundle),
        # The inputs the adversarial judgment needs and the rules do not show:
        # who is actually trading, how fast the float is turning over, and what
        # the rest of the sector is doing.
        "avg_trade_size": _avg_trade_size(m),
        "float_rotation": _float_rotation(m, enr),
        "sector_peers": (getattr(enr, "sector_peers", None)
                         if enr is not None else None),
        "market": (market.headline() if market is not None else "unknown"),
        "float_shares": (enr.float_shares if enr is not None else None),
        "sector": (enr.sector if enr is not None else ""),
        "time_to_horizon": time_to_horizon or {},
    }
    # The AI-only context. Attached under one key rather than merged into the
    # top level, so nothing here can be mistaken for an alert line: the renderer
    # only ever reads the top-level keys, and the prompt tells the model this
    # section is for reasoning and not for the reader.
    if context:
        facts["context"] = context
    return facts
