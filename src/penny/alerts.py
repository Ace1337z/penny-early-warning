"""Alert message formatting and the AI fact bundle (6.11, 6.12)."""

from __future__ import annotations

import logging
from typing import Optional

from .enrich import Enricher, Enrichment
from .scoring import (Metrics, PHASE_EXTENDED, PHASE_FADING, TIER_CONFIRMED,
                      TIER_EARLY, TIER_WATCH)
from .util import compact, money, num, pct

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


def format_alert1(m: Metrics, *, shariah_line: str = "Shariah: checking...",
                  pct_rank: Optional[int] = None, resumed: bool = False) -> str:
    """Instant Alert 1: no AI, no enrichment (6.12)."""
    header = f"{tier_name(m.tier)}  {m.symbol}  {money(m.price)}{phase_tag(m)}"
    if resumed:
        header += "  RESUMED FROM HALT"
    low_line = (f"low {int(m.minutes_since_low)}m ago"
                if m.minutes_since_low is not None else "low n/a")
    low_line += f" | score {m.score:.0f}"
    if pct_rank:
        low_line += f" | #{pct_rank} gainer"
    lines = [
        f"ALERT: {header}",
        f"{pct(m.pct_vs_close * 100 if m.pct_vs_close is not None else None)} vs close"
        f" | {pct(m.rise.get(15, 0) * 100)} 15m | {pct(m.rise.get(60, 0) * 100)} 60m",
        f"vol {num(m.volx.get(15, 0), 1)}x normal | day {compact(m.cum_volume)}"
        + (" | above VWAP" if m.vwap and m.price > m.vwap else " | below VWAP"),
        low_line,
        shariah_line,
    ]
    return "\n".join(lines)


def format_alert2(m: Metrics, enr: Enrichment, market, *,
                  shariah_lines: Optional[list[str]] = None,
                  reaction: str = "",
                  price_forecast: str = "",
                  ai_running: bool = True,
                  validated: bool = True,
                  risk_usd: float = 50.0) -> str:
    """Alert 2: facts first, then the AI blocks (edited in later). Max 3,900 chars."""
    blocks: list[str] = []
    head = (f"{tier_name(m.tier)} {m.symbol} {money(m.price)} "
            f"{pct(m.pct_vs_close * 100 if m.pct_vs_close is not None else None)} vs close"
            f"{phase_tag(m)}")
    blocks.append(f"DETAIL {head}")

    # Market context line.
    if market is not None:
        blocks.append(market.headline())

    # Verification.
    blocks.append(Enricher.verification_line(enr))

    # News.
    if enr.news:
        lines = [f"NEWS ({len(enr.news)} items from {enr.outlets} outlet(s)):"]
        for item in enr.news[:6]:
            age = f" ({item['age']})" if item.get("age") else ""
            src = f" - {item.get('source')}" if item.get("source") else ""
            lines.append(f"- {item.get('title', '')[:150]}{age}{src}")
            if item.get("summary"):
                lines.append(f"  {item['summary'][:180]}")
        blocks.append("\n".join(lines))

    # Filings.
    if enr.filings:
        lines = ["FILINGS (last 5 days):"]
        for f in enr.filings[:5]:
            extra = f" items {f['items']}" if f.get("items") else ""
            lines.append(f"- {f.get('form')} {f.get('date')}{extra}")
        blocks.append("\n".join(lines))

    # Short data / float.
    short_text = Enricher.short_text(enr)
    if short_text:
        blocks.append("SHORT/FLOAT: " + short_text)

    # Insiders.
    if enr.insiders:
        lines = ["INSIDERS:"]
        for i in enr.insiders[:3]:
            lines.append(f"- {i.get('date')} {i.get('owner')} {i.get('transaction')} "
                         f"{i.get('value')}")
        blocks.append("\n".join(lines))

    # Shariah detail.
    if shariah_lines:
        blocks.append("\n".join(shariah_lines))

    # Technicals.
    if enr.technicals:
        blocks.append("TECH: " + enr.technicals.summary_text())
    else:
        blocks.append("TECH: no candle data")

    # Fibonacci.
    if enr.fib:
        r = enr.fib.retracements
        e = enr.fib.extensions
        blocks.append(
            f"FIB swing {enr.fib.swing_low:.4f} -> {enr.fib.swing_high:.4f}\n"
            f"  retrace 23.6% {r['23.6']:.4f} | 38.2% {r['38.2']:.4f} | 50% {r['50.0']:.4f} | "
            f"61.8% {r['61.8']:.4f} | 78.6% {r['78.6']:.4f}\n"
            f"  extensions 127.2% {e['127.2']:.4f} | 161.8% {e['161.8']:.4f}")

    # Trade plan.
    if enr.plan:
        blocks.append("PLAN: " + enr.plan.text(risk_usd))

    # AI blocks.
    if reaction:
        blocks.append(reaction)
    if price_forecast:
        blocks.append(price_forecast + ("" if validated else " (unvalidated)"))
    elif ai_running:
        blocks.append("AI: running...")

    # Missing sources.
    if enr.missing:
        blocks.append("missing: " + ", ".join(enr.missing))

    blocks.append(DISCLAIMER)
    text = "\n\n".join(b for b in blocks if b)
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


def build_ai_facts(m: Metrics, enr: Enrichment, market, *, session: str,
                   pct_rank: Optional[int] = None,
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
