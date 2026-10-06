"""Alert message formatting and the AI fact bundle (6.11, 6.12)."""

from __future__ import annotations

import logging
from html import escape as _escape
from typing import Optional

from .enrich import Enricher, Enrichment
from .scoring import (Metrics, PHASE_EXTENDED, PHASE_FADING, TIER_CONFIRMED, TIER_EARLY,
                      TIER_NONE, TIER_WATCH)
from .ui import arrow_for, bar, divider
from .util import compact, conviction_word, money, num, pct

log = logging.getLogger(__name__)

TIER_NAMES = {TIER_WATCH: "WATCH", TIER_EARLY: "EARLY BUILD", TIER_CONFIRMED: "CONFIRMED"}
MAX_ALERT2 = 3900

DISCLAIMER = ("High-risk penny stock. Automated screening and forecasts are estimates, "
              "not financial advice.")


def tier_name(tier: int) -> str:
    return TIER_NAMES.get(tier, "NONE")


def phase_tag(m: Metrics) -> str:
    if m.phase == PHASE_EXTENDED:
        return " [EXTENDED]"
    if m.phase == PHASE_FADING:
        return " [FADING]"
    return ""


# --- message builder: one structure, rendered plain or as Telegram HTML -------

class _Msg:
    """Accumulates lines whose dynamic parts are escaped only for HTML."""

    def __init__(self, html: bool = False):
        self.html = html
        self.lines: list[str] = []

    def line(self, *segs) -> None:
        parts: list[str] = []
        for seg in segs:
            if isinstance(seg, tuple):
                role, value = seg
                value = "" if value is None else str(value)
                if not self.html:
                    parts.append(value)
                elif role == "b":
                    parts.append(f"<b>{_escape(value, quote=False)}</b>")
                elif role in ("code", "mono"):
                    parts.append(f"<code>{_escape(value, quote=False)}</code>")
                elif role == "i":
                    parts.append(f"<i>{_escape(value, quote=False)}</i>")
                else:
                    parts.append(_escape(value, quote=False))
            elif seg is not None:
                value = str(seg)
                parts.append(_escape(value, quote=False) if self.html else value)
        self.lines.append("".join(parts))

    def blank(self) -> None:
        self.lines.append("")

    def text(self) -> str:
        return "\n".join(self.lines)


def _pct(x: Optional[float]) -> str:
    """`x` is a fraction (0.315) -> '+31.5%'."""
    return pct(x * 100 if x is not None else None)


def _setup_reason(m: Metrics) -> str:
    """Why this symbol did or did not reach an alert tier."""
    if m.tier != TIER_NONE:
        return ""
    rise15 = (m.rise.get(15, 0.0) or 0.0) * 100
    volx15 = m.volx.get(15, 0.0) or 0.0
    dv15 = m.dollar_vol.get(15, 0.0) or 0.0
    above = m.vwap is not None and m.price > m.vwap
    fails: list[str] = []
    if rise15 < 3:
        fails.append(f"15m move {rise15:+.1f}% is under +3%")
    if volx15 < 3:
        fails.append(f"volume {volx15:.1f}x is under 3x")
    if dv15 < 3000:
        fails.append(f"only {compact(dv15)} traded in 15m")
    if m.vwap is not None and not above:
        fails.append("price is below VWAP")
    return "; ".join(fails) if fails else "below every alert threshold"


def verdict_text(m: Metrics, enr: Enrichment, agg=None) -> str:
    """One fast-read action line: ENTER / EARLY / WAIT / AVOID, with levels."""
    direction = str(getattr(agg, "direction", "") or "")
    confidence = getattr(agg, "confidence", 0.0) or 0.0
    plan = enr.plan

    if direction == "down" and confidence >= 0.6:
        action, note = "AVOID", "the panel leans down with conviction"
    elif m.phase == PHASE_FADING:
        action, note = "AVOID", "momentum is fading off the high; wait for a reclaim"
    elif m.phase == PHASE_EXTENDED:
        action, note = "WAIT", "the move is already extended - do not chase"
    elif m.tier >= TIER_CONFIRMED:
        action, note = "ENTER", "momentum confirmed on volume above VWAP"
    elif m.tier == TIER_EARLY:
        action, note = "EARLY", "early build - size small, confirm with volume"
    elif m.tier == TIER_WATCH:
        action, note = "WATCH", "watch for the volume that confirms the move"
    else:
        action, note = "WAIT", "below the alert bar (see SETUP above)"

    parts: list[str] = []
    if plan and action in ("ENTER", "EARLY", "WAIT"):
        parts.append(f"buy {plan.entry_low:.4f}-{plan.entry_high:.4f}")
        parts.append(f"stop {plan.stop:.4f}")
        parts.append(f"T1 {plan.target1:.4f}")
    if direction in ("up", "down"):
        parts.append(f"panel {direction} ({conviction_word(confidence)})")
    head = f"{action} - {note}"
    return head + ("\n  " + " | ".join(parts) if parts else "")


def _pulse(m: Metrics) -> str:
    """The one-line numeric strip: move, volume multiple, VWAP side."""
    parts = [f"{_pct(m.rise.get(15, 0))} 15m",
             f"{_pct(m.rise.get(60, 0))} 60m",
             f"vol {num(m.volx.get(15, 0), 1)}x"]
    dv = m.dollar_vol.get(15)
    if dv:
        parts.append(f"${compact(dv)} 15m")
    parts.append("above VWAP" if (m.vwap and m.price > m.vwap) else "below VWAP")
    return "  |  ".join(parts)


def _why_lines(m: Metrics, enr: Enrichment, agg=None) -> tuple[list[tuple[str, str]], set[str]]:
    """Why the stock moved, most important first. Also returns the headlines shown."""
    out: list[tuple[str, str]] = []
    shown: set[str] = set()
    catalyst = str(getattr(agg, "catalyst", "") or "").strip()
    if catalyst:
        out.append(("CATALYST", catalyst))
    fresh = enr.news[:1]
    for item in fresh:
        title = str(item.get("title", ""))
        shown.add(title)
        age = f" ({item.get('age')})" if item.get("age") else ""
        src = f" - {item.get('source')}" if item.get("source") else ""
        out.append(("HEADLINE", f"{title[:160]}{age}{src}"))
    if not catalyst and not fresh:
        out.append(("NO CATALYST", "no fresh news found - this move may be purely technical"))
    # The shape of the move tells you whether it is early or already done.
    vs_close = m.pct_vs_close
    r15 = m.rise.get(15, 0.0) or 0.0
    r60 = m.rise.get(60, 0.0) or 0.0
    if r15 >= 0.03:
        hot = "still moving now"
    elif from_high := getattr(m, "from_high", None):
        hot = "off the high" if from_high < -0.02 else "holding near the high"
    else:
        hot = "cooling" if r60 > r15 else "flat in the last hour"
    out.append(("MOVE", f"{_pct(vs_close)} vs prior close | {_pct(r15)} 15m | "
                        f"{_pct(r60)} 60m | {hot}"))
    return out, shown


def _ai_lines(text: str) -> list[str]:
    """Indent a multi-line AI block for readability."""
    return [("  " + line) if line else "" for line in text.splitlines()]


def format_alert1(m: Metrics, *, shariah_line: str = "Shariah: checking...",
                  pct_rank: Optional[int] = None, resumed: bool = False,
                  html: bool = False) -> str:
    """Instant Alert 1: no AI, no enrichment (6.12)."""
    msg = _Msg(html)
    head = f"{tier_name(m.tier)}  {m.symbol}  {money(m.price)}{phase_tag(m)}"
    if resumed:
        head += "  RESUMED FROM HALT"
    msg.line(("b", f"ALERT: {head}"))
    msg.line(("b", _pct(m.pct_vs_close)), " vs close | ",
             _pct(m.rise.get(15, 0)), " 15m | ", _pct(m.rise.get(60, 0)), " 60m")
    vol = f"vol {num(m.volx.get(15, 0), 1)}x normal | day {compact(m.cum_volume)}"
    vol += " | above VWAP" if m.vwap and m.price > m.vwap else " | below VWAP"
    msg.line(vol)
    low_line = (f"low {int(m.minutes_since_low)}m ago"
                if m.minutes_since_low is not None else "low n/a")
    low_line += f" | score {m.score:.0f}"
    if pct_rank:
        low_line += f" | #{pct_rank} gainer"
    msg.line(low_line)
    msg.line(shariah_line)
    return msg.text()


def format_alert2(m: Metrics, enr: Enrichment, market, *,
                  shariah_lines: Optional[list[str]] = None,
                  reaction: str = "",
                  price_forecast: str = "",
                  verdict: str = "",
                  agg=None,
                  ai_running: bool = True,
                  validated: bool = True,
                  risk_usd: float = 50.0,
                  html: bool = False) -> str:
    """Alert 2: why it moved and what to do first, then the evidence. Max 3,900."""
    msg = _Msg(html)
    verdict = verdict or verdict_text(m, enr, agg)
    pv = m.pct_vs_close * 100 if m.pct_vs_close is not None else None

    # -- headline -----------------------------------------------------------
    msg.line(("b", f"DETAIL  {arrow_for(pv)} {m.symbol}  {money(m.price)}  "
                   f"{_pct(m.pct_vs_close)} vs close{phase_tag(m)}"))
    msg.line(_pulse(m))
    msg.line("momentum ", ("b", f"{m.score:.0f}"), " ", bar((m.score or 0) / 100.0))
    setup = f"SETUP: {tier_name(m.tier)}"
    reason = _setup_reason(m)
    if reason:
        setup += f" - below the alert bar ({reason})"
    elif m.tier >= TIER_EARLY:
        setup += " - qualifies for alerts"
    msg.line(("b", "SETUP: "), setup.split("SETUP: ", 1)[1])

    # -- the decision -------------------------------------------------------
    if verdict:
        first, _, rest = verdict.partition("\n")
        msg.blank()
        msg.line(divider("VERDICT"))
        msg.line(("b", "VERDICT: "), first)
        if rest.strip():
            msg.line(rest)

    # -- why it moved -------------------------------------------------------
    why, shown_titles = _why_lines(m, enr, agg)
    if why:
        msg.blank()
        msg.line(divider("WHY IT MOVED"))
        for label, value in why:
            msg.line(("b", f"{label}: "), value)

    # -- market backdrop (explains small-cap risk appetite) -----------------
    if market is not None:
        msg.blank()
        msg.line(divider("MARKET BACKDROP"))
        msg.line(market.headline())

    # -- evidence -----------------------------------------------------------
    msg.blank()
    msg.line(divider("EVIDENCE"))
    msg.line(Enricher.verification_line(enr))

    def group(label: str) -> None:
        # Every evidence block gets a blank line before it, so filings, short
        # interest, Shariah, technicals and the plan do not run together.
        msg.blank()
        msg.line(("b", label))

    if enr.news:
        rest = [n for n in enr.news if str(n.get("title", "")) not in shown_titles]
        if rest:
            group(f"MORE NEWS ({len(rest)} more from {enr.outlets} outlet(s))")
            for item in rest[:5]:
                age = f" ({item['age']})" if item.get("age") else ""
                src = f" - {item.get('source')}" if item.get("source") else ""
                msg.line("- ", f"{str(item.get('title', ''))[:150]}{age}{src}")
                if item.get("summary"):
                    msg.line("  ", str(item["summary"])[:180])

    if enr.filings:
        group("FILINGS (last 5 days)")
        for f in enr.filings[:5]:
            extra = f" items {f['items']}" if f.get("items") else ""
            msg.line("- ", f"{f.get('form')} {f.get('date')}{extra}")

    short_text = Enricher.short_text(enr)
    if short_text:
        msg.blank()
        msg.line(("b", "SHORT/FLOAT: "), short_text)

    if enr.insiders:
        group("INSIDERS")
        for i in enr.insiders[:3]:
            msg.line("- ", f"{i.get('date')} {i.get('owner')} {i.get('transaction')} "
                           f"{i.get('value')}")

    if shariah_lines:
        msg.blank()
        for line in shariah_lines:
            msg.line(line)

    msg.blank()
    if enr.technicals:
        msg.line(("b", "TECH: "), enr.technicals.summary_text())
    else:
        msg.line(("b", "TECH: "), "no candle data")

    if enr.fib:
        r = enr.fib.retracements
        e = enr.fib.extensions
        msg.blank()
        msg.line(("b", f"FIB swing {enr.fib.swing_low:.4f} -> {enr.fib.swing_high:.4f}"))
        msg.line("  retrace 23.6% ", f"{r['23.6']:.4f}", " | 38.2% ", f"{r['38.2']:.4f}",
                 " | 50% ", f"{r['50.0']:.4f}", " | 61.8% ", f"{r['61.8']:.4f}",
                 " | 78.6% ", f"{r['78.6']:.4f}")
        msg.line("  extensions 127.2% ", f"{e['127.2']:.4f}", " | 161.8% ",
                 f"{e['161.8']:.4f}")

    if enr.plan:
        msg.blank()
        msg.line(("b", "PLAN: "), enr.plan.text(risk_usd))

    # -- AI -----------------------------------------------------------------
    if reaction or price_forecast:
        msg.blank()
        msg.line(("b", "AI PANEL"))
        for line in _ai_lines(reaction):
            msg.line(line)
        if price_forecast:
            msg.blank()
            pf = price_forecast.rstrip().split("\n")
            if not validated:
                pf[0] = pf[0] + " (unvalidated)"
            for line in _ai_lines("\n".join(pf)):
                msg.line(line)
    elif ai_running:
        msg.blank()
        msg.line(("b", "AI PANEL"), " - running...")

    if enr.missing:
        msg.blank()
        msg.line(("b", "MISSING: "), ", ".join(enr.missing))

    msg.blank()
    msg.line(("i", DISCLAIMER))
    text = msg.text()
    if len(text) > MAX_ALERT2:
        text = _truncate(text, MAX_ALERT2)
    return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit - 40]
    nl = cut.rfind("\n")
    if nl > limit * 0.6:
        cut = cut[:nl]
    return cut + "\n...(truncated)"

def _build_detail(m: Metrics, score: float, above_vwap: bool) -> str:
    """The measurements that define the build, without the symbol."""
    bits = [f"{_pct(m.pct_vs_close)} vs close",
            f"{_pct(m.rise.get(15, 0))} 15m",
            f"{num(m.volx.get(15, 0), 1)}x vol"]
    if m.dollar_vol.get(15):
        bits.append(f"${compact(m.dollar_vol[15])} 15m")
    bits.append("above VWAP" if above_vwap else "below VWAP")
    return " | ".join(bits)


def format_build_feed(signals, *, limit: int = 6, html: bool = True) -> str:
    """Surfaced momentum builds: stocks accumulating before they hit the alert bar."""
    msg = _Msg(html)
    msg.line(("b", "BUILDING NOW - watch, not a buy signal"))
    for sig in signals[:limit]:
        m = sig.metrics
        msg.line(("b", f"{m.symbol}  {money(m.price)}  {_pct(m.pct_vs_close)} vs close"),
                 f"  [{sig.stage.lower()} {sig.score:.0f}/100]")
        msg.line("  " + _build_detail(m, sig.score, sig.above_vwap))
        if sig.missing:
            msg.line("  to alert: " + "; ".join(sig.missing))
    msg.line("These are not yet alert-tier. /check SYM for the full read.")
    return msg.text()


def format_build_digest(signals, *, seconds: int = 0, limit: int = 6,
                        html: bool = True) -> str:
    """A periodic digest of the current momentum builds, if any."""
    if not signals:
        return ""
    msg = _Msg(html)
    window = f" (last {max(1, seconds // 60)}m)" if seconds else ""
    msg.line(("b", f"MOMENTUM BUILDS{window}"))
    for sig in signals[:limit]:
        m = sig.metrics
        msg.line(("b", f"{m.symbol} "), f"{sig.stage.lower()} {sig.score:.0f}/100 | ",
                 _build_detail(m, sig.score, sig.above_vwap))
        if sig.missing:
            msg.line("  to alert: ", "; ".join(sig.missing))
    return msg.text()


def build_ai_facts(m: Metrics, enr: Enrichment, market, *, session: str,                   pct_rank: Optional[int] = None,
                   time_to_horizon: Optional[dict] = None,
                   trade_plan_dict: Optional[dict] = None) -> dict:
    """The compact JSON facts sent to the models (6.11)."""
    facts: dict = {
        "symbol": m.symbol,
        "session": session,
        "price": round(m.price, 5),
        "pct_vs_close": round(m.pct_vs_close * 100, 2) if m.pct_vs_close is not None else None,
        "rise15": round(m.rise.get(15, 0) * 100, 2),
        "rise60": round(m.rise.get(60, 0) * 100, 2),
        "volx15": round(m.volx.get(15, 0), 2),
        "accel": round(m.accel, 2),
        "above_vwap": bool(m.vwap and m.price > m.vwap),
        "from_high": round(m.from_high * 100, 2) if m.from_high is not None else None,
        "minutes_since_low": round(m.minutes_since_low, 1) if m.minutes_since_low else None,
        "tier": m.tier,
        "gainer_rank": pct_rank,
        "dollar_vol_15m": round(m.dollar_vol.get(15, 0)),
        "float": enr.float_shares,
        "market_cap": enr.market_cap,
        "sector": enr.sector or None,
    }
    if enr.news:
        facts["news"] = [{"title": n.get("title"), "source": n.get("source"),
                          "age": n.get("age"), "summary": n.get("summary")}
                         for n in enr.news[:6]]
    if enr.filings:
        facts["filings"] = [{"form": f.get("form"), "date": f.get("date"),
                             "items": f.get("items")} for f in enr.filings[:5]]
    if enr.short_interest or enr.short_volume:
        facts["short"] = {
            "interest": [{k: v for k, v in s.items() if k != "source"} for s in enr.short_interest[:3]],
            "volume": ({k: v for k, v in enr.short_volume.items() if k != "source"}
                       if enr.short_volume else None),
        }
    if enr.insiders:
        facts["insider"] = [{"date": i.get("date"), "owner": i.get("owner"),
                             "transaction": i.get("transaction"), "value": i.get("value")}
                            for i in enr.insiders[:3]]
    if enr.technicals:
        facts["technicals"] = enr.technicals.as_dict()
    if enr.fib:
        facts["fibonacci"] = enr.fib.as_dict()
    if trade_plan_dict:
        facts["trade_plan"] = trade_plan_dict
    elif enr.plan:
        facts["trade_plan"] = enr.plan.as_dict()
    if enr.verification:
        facts["finviz"] = {k: v for k, v in enr.verification.items()
                           if k in ("status", "authoritative", "price", "volume")}
    if market is not None:
        facts["market"] = market.as_prompt_dict()
    if time_to_horizon:
        facts["time_to_horizon"] = time_to_horizon
    return facts
