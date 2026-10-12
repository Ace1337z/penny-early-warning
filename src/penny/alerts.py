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

def cause_block(m: Metrics, enr: Optional[Enrichment], bundle=None) -> list[str]:
    """The WHY IT MOVED body: exactly one cause, or an honest 'none found'.

    `bundle` is the gate's Bundle for this symbol. When present it carries the
    same news the detectors ranked, so the cause and the gate cannot disagree
    about which headline was the catalyst.
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
        from .strategies.base import Bundle
        src = Bundle(symbol=m.symbol, price=m.price, prev_close=m.prev_close,
                     open_price=(enr.finviz_row or {}).get("Open") if enr.finviz_row else None,
                     float_shares=enr.float_shares, short_pct=None, news=news,
                     vwap=m.vwap, volx=dict(m.volx or {}),
                     rise=dict(m.rise or {}))
        try:
            src.open_price = float(src.open_price)
        except (TypeError, ValueError):
            src.open_price = None

    try:
        _, ranked, _ = rank_catalysts(src)
        cause = extract_cause(ranked, src, filings=enr.filings if enr else None)
        return cause_lines(cause)
    except Exception as exc:  # noqa: BLE001
        log.debug("cause block failed for %s: %s", m.symbol, exc)
        return ["CAUSE: could not determine."]


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
                  fast: bool = False, html: bool = False, gate=None) -> str:
    """Instant Alert 1: no AI, no enrichment. The trigger and nothing else."""
    msg = _Msg(html)
    head = f"{tier_name(m.tier)}  {m.symbol}  {money(m.price)}{phase_tag(m)}"
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
    msg.line(_pulse(m))
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
        "tier": tier_name(m.tier),
        "session": session,
        "gate": gate.as_dict() if gate is not None else None,
        "cause": cause_block(m, enr, bundle),
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
