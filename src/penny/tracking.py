"""Track every alerted name from the alert to the close, then report once.

The alert is a prediction. Without measuring it, there is no way to tell whether
the gate is finding winners or just finding things that moved, and a strategy
library that is never graded is a strategy library that only ever feels good.

Two design decisions worth stating, because both cost something:

  - Tracking runs off the quotes the engine already polls, not off re-fetched
    1m candles. Finviz returns a session high and low on every row, so max gain,
    drawdown, and the stop/target tests are exact at poll resolution -- and the
    TwelveData quota, 8 credits a minute on the free tier, is left alone. The
    cost is that a spike between two polls is invisible. At a 20-second cadence
    that is acceptable; at 60 seconds it would not be.

  - One message a day. Per-alert result messages train the reader to ignore
    results. A single end-of-day report makes the day's alerts as a set, which
    is how they should be judged anyway -- the question is not "did this one
    work" but "was today a good day".

Statuses: `open` while the session runs, `closed` after the close, and `invalid`
when the session ended with no price seen (a halt, or a symbol that stopped
being quoted). `invalid` exists so a missing close is never silently read as a
flat one, which would look like a 0% day and drag the averages.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from .util import UTC, money, now_et, pct, safe_div, to_et

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, db, clock=None):
        self.db = db
        self.clock = clock or (lambda: __import__("time").time())

    # -- lifecycle ---------------------------------------------------------
    def start(self, symbol: str, *, alert_ts: float, alert_price: float,
              day: Optional[str] = None, primary: Optional[str] = None,
              entry_trigger: Optional[str] = None, gate_score: Optional[float] = None,
              plan=None, plan_entry_low: Optional[float] = None,
              plan_entry_high: Optional[float] = None) -> Optional[int]:
        """Record an alert. One row per symbol per day; a re-alert refreshes it.

        `plan` is either a mapping ({"stop","t1","t2"}, the shape the tests and
        the older tier path used) or a `TradePlan` object (what the gate path
        hands over). Spec 4 replaced T1/T2 with structural overhead levels, so
        the two DB columns (kept for schema stability) now carry the first and
        second real resistance levels above the entry. Read both through one
        resolver so neither caller has to reshape it -- passing the object
        straight into `.get()` used to raise, and the broad except below turned
        that into a silently untracked alert.
        """
        def _field(name, *attrs):
            if isinstance(plan, dict):
                return plan.get(name)
            for a in attrs:
                v = getattr(plan, a, None)
                if v is not None:
                    return v
            return None

        def _overhead(i, fallback_name=None):
            if isinstance(plan, dict):
                return plan.get(fallback_name) if fallback_name else None
            over = getattr(plan, "overhead", None) or []
            return over[i] if len(over) > i else None

        day = day or to_et().strftime("%Y-%m-%d")
        if plan_entry_low is None:
            plan_entry_low = _field("entry_low", "entry_low", "entry")
        if plan_entry_high is None:
            plan_entry_high = _field("entry_high", "entry_high", "entry")
        try:
            self.db.execute(
                "INSERT INTO alert_tracking(symbol, day, alert_ts, alert_price, "
                "primary_strategy, entry_trigger, gate_score, plan_entry_low, "
                "plan_entry_high, plan_stop, plan_t1, plan_t2, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open') "
                "ON CONFLICT(symbol, day) DO UPDATE SET "
                "alert_ts=excluded.alert_ts, alert_price=excluded.alert_price, "
                "primary_strategy=excluded.primary_strategy, "
                "entry_trigger=excluded.entry_trigger, gate_score=excluded.gate_score, "
                "plan_entry_low=excluded.plan_entry_low, "
                "plan_entry_high=excluded.plan_entry_high, "
                "plan_stop=excluded.plan_stop, plan_t1=excluded.plan_t1, "
                "plan_t2=excluded.plan_t2, status='open'",
                (symbol.upper(), day, alert_ts, alert_price, primary, entry_trigger,
                 gate_score, plan_entry_low, plan_entry_high,
                 _field("stop", "stop"), _overhead(0, "t1"), _overhead(1, "t2")))
            row = self.db.query_one(
                "SELECT id FROM alert_tracking WHERE symbol=? AND day=?",
                (symbol.upper(), day))
            return int(row["id"]) if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker start failed for %s: %s", symbol, exc)
            return None

    def abort(self, symbol: str, *, reason: str, ts: Optional[float] = None,
              day: Optional[str] = None) -> None:
        """Mark a setup invalidated. Terminal, and it keeps the row.

        An invalidated setup still has an outcome -- the reader was told not to
        enter, and whether it went up afterwards is exactly what says whether
        that was the right call. So this sets `aborted` rather than closing the
        row, and the EOD report counts it in its own section.
        """
        day = day or to_et().strftime("%Y-%m-%d")
        try:
            self.db.execute(
                "UPDATE alert_tracking SET aborted=1, abort_ts=?, abort_reason=?, "
                "status='aborted' WHERE symbol=? AND day=? AND aborted=0",
                (ts if ts is not None else self.clock(), reason,
                 symbol.upper(), day))
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker abort failed for %s: %s", symbol, exc)

    def write_context(self, symbol: str, *, day: Optional[str] = None,
                      bid: Optional[float] = None, ask: Optional[float] = None,
                      spread_pct: Optional[float] = None,
                      dollar_vol_15m: Optional[float] = None,
                      avg_trade_size: Optional[float] = None,
                      sector: Optional[str] = None,
                      sector_hot: Optional[bool] = None,
                      exit_liq_2k_seconds: Optional[float] = None,
                      analogs: Optional[dict] = None) -> None:
        """Persist the quote reality and the AI context onto the alert row.

        Every value is optional and `None` is skipped rather than written, so a
        caller that only knows the bid and ask does not blank the columns a
        later, richer call filled in. The column set is the one declared in the
        schema; the migration adds them to older databases at open time.
        """
        cols = {
            "bid_at_alert": bid,
            "ask_at_alert": ask,
            "spread_pct_at_alert": spread_pct,
            "dollar_vol_15m_at_alert": dollar_vol_15m,
            "avg_trade_size_at_alert": avg_trade_size,
            "sector_at_alert": sector,
            "sector_hot": None if sector_hot is None else int(bool(sector_hot)),
            "exit_liq_2k_seconds": exit_liq_2k_seconds,
        }
        summary = analogs or {}
        if summary.get("t1_hit_rate") is not None:
            cols["analog_t1_hit_rate"] = summary["t1_hit_rate"]
        if summary.get("stop_hit_rate") is not None:
            cols["analog_stop_hit_rate"] = summary["stop_hit_rate"]
        if summary.get("median_peak_pct") is not None:
            cols["analog_median_peak_pct"] = summary["median_peak_pct"]
        pairs = [(k, v) for k, v in cols.items() if v is not None]
        if not pairs:
            return
        day = day or to_et().strftime("%Y-%m-%d")
        sql = ("UPDATE alert_tracking SET "
               + ", ".join("%s=?" % k for k, _ in pairs)
               + " WHERE symbol=? AND day=?")
        try:
            self.db.execute(sql, tuple(v for _, v in pairs) + (symbol.upper(), day))
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker context write failed for %s: %s", symbol, exc)

    def write_confidence(self, symbol: str, confidence: Optional[float], *,
                         day: Optional[str] = None) -> None:
        """Store the calibrated confidence shown to the reader, not the raw one."""
        if confidence is None:
            return
        day = day or to_et().strftime("%Y-%m-%d")
        try:
            self.db.execute(
                "UPDATE alert_tracking SET ai_confidence_calibrated=? "
                "WHERE symbol=? AND day=?", (float(confidence), symbol.upper(), day))
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker confidence write failed for %s: %s", symbol, exc)

    def write_decision(self, symbol: str, *, rating: Optional[str] = None,
                       verdict: Optional[str] = None,
                       findings: Optional[list] = None,
                       source: Optional[str] = None,
                       cause_present: Optional[bool] = None,
                       day: Optional[str] = None) -> None:
        """Record what the one AI call said about this alert.

        The EOD claimed-vs-actual block and the post-mortem both read these
        columns. Without them every row groups under "unrated" and the rating
        section would show claimed 0.00 against actual -- a section that is
        true only in the sense that it is empty. Findings are joined into one
        cell because the reader of a post-mortem wants the sentence, not a
        table. Spec 2.0 adds the provenance (`ai_source`) and whether a cause
        was named (`cause_present`), because a decision that came from the
        rules alone has a hit rate too, and the calibration has to be able to
        split its buckets by cause.
        """
        cols: dict = {}
        if rating:
            cols["ai_rating"] = str(rating)
        if verdict:
            cols["ai_verdict"] = str(verdict)
        if findings:
            cols["ai_key_findings"] = " | ".join(str(f) for f in findings)[:400]
        if source:
            cols["ai_source"] = str(source)
        if cause_present is not None:
            cols["cause_present"] = 1 if cause_present else 0
        if not cols:
            return
        day = day or to_et().strftime("%Y-%m-%d")
        sql = ("UPDATE alert_tracking SET "
               + ", ".join("%s=?" % k for k in cols)
               + " WHERE symbol=? AND day=?")
        try:
            self.db.execute(sql, tuple(cols.values()) + (symbol.upper(), day))
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker decision write failed for %s: %s", symbol, exc)

    def update(self, symbol: str, *, price: float, high: Optional[float] = None,
               low: Optional[float] = None, day: Optional[str] = None) -> None:
        """Fold one quote into the running high/low and recompute the extremes.

        Called for every alerted symbol on every cycle. Cheap on purpose: a
        single SELECT, a few comparisons and one UPDATE.
        """
        day = day or to_et().strftime("%Y-%m-%d")
        try:
            row = self.db.query_one(
                "SELECT id, alert_price, high_after, low_after, plan_stop, "
                "plan_t1, plan_t2, hit_stop, hit_t1, hit_t2 "
                "FROM alert_tracking WHERE symbol=? AND day=? AND status='open'",
                (symbol.upper(), day))
            if row is None:
                return

            entry = float(row["alert_price"] or price)
            hi = max(float(row["high_after"] or entry), high or price, price)
            lo = min(float(row["low_after"] or entry), low or price, price)

            hit_stop = row["hit_stop"] or (1 if row["plan_stop"] and lo <= row["plan_stop"] else 0)
            hit_t1 = row["hit_t1"] or (1 if row["plan_t1"] and hi >= row["plan_t1"] else 0)
            hit_t2 = row["hit_t2"] or (1 if row["plan_t2"] and hi >= row["plan_t2"] else 0)

            self.db.execute(
                "UPDATE alert_tracking SET high_after=?, low_after=?, last_price=?, "
                "max_gain_pct=?, max_drawdown_pct=?, close_pct=?, "
                "hit_stop=?, hit_t1=?, hit_t2=? WHERE id=?",
                (hi, lo, price,
                 safe_div(hi, entry, 1.0) - 1.0,
                 safe_div(lo, entry, 1.0) - 1.0,
                 safe_div(price, entry, 1.0) - 1.0,
                 hit_stop, hit_t1, hit_t2, row["id"]))
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker update failed for %s: %s", symbol, exc)

    def close_day(self, day: Optional[str] = None) -> int:
        """Stamp the close. Anything with no price seen becomes `invalid`."""
        day = day or to_et().strftime("%Y-%m-%d")
        try:
            rows = self.db.query(
                "SELECT id, last_price FROM alert_tracking "
                "WHERE day=? AND status='open'", (day,))
            for r in rows:
                status = "closed" if r["last_price"] is not None else "invalid"
                self.db.execute("UPDATE alert_tracking SET status=? WHERE id=?",
                                (status, r["id"]))
            return len(rows)
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker close failed: %s", exc)
            return 0

    # -- reads -------------------------------------------------------------
    def rows_for(self, day: Optional[str] = None, *, limit: int = 50) -> list[dict]:
        day = day or to_et().strftime("%Y-%m-%d")
        try:
            return [dict(r) for r in self.db.query(
                "SELECT * FROM alert_tracking WHERE day=? "
                "ORDER BY COALESCE(close_pct, max_gain_pct) DESC LIMIT ?",
                (day, limit))]
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker read failed: %s", exc)
            return []

    def open_symbols(self, day: Optional[str] = None) -> list[str]:
        day = day or to_et().strftime("%Y-%m-%d")
        try:
            return [r["symbol"] for r in self.db.query(
                "SELECT symbol FROM alert_tracking WHERE day=? AND status='open' "
                "ORDER BY symbol", (day,))]
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker open read failed: %s", exc)
            return []

    def recent(self, days: int = 30) -> list[dict]:
        cutoff = (to_et() - __import__("datetime").timedelta(days=days)
                  ).strftime("%Y-%m-%d")
        try:
            return [dict(r) for r in self.db.query(
                "SELECT * FROM alert_tracking WHERE day >= ? AND status='closed' "
                "ORDER BY day DESC, symbol", (cutoff,))]
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker recent read failed: %s", exc)
            return []


# --------------------------------------------------------------------------
# the one end-of-day message
# --------------------------------------------------------------------------

def summary_line(r: dict) -> str:
    """One row: what it did from the alert to the close."""
    sym = r["symbol"]
    close_pct = r.get("close_pct")
    max_gain = r.get("max_gain_pct")
    dd = r.get("max_drawdown_pct")
    bits = [f"{sym:<6} {pct((close_pct or 0) * 100):>7} close"]
    if max_gain is not None:
        bits.append(f"max {pct(max_gain * 100):>6}")
    if dd is not None:
        bits.append(f"low {pct(dd * 100):>6}")
    if r.get("primary_strategy"):
        bits.append(str(r["primary_strategy"])[:18])
    marks = "".join(c for c, f in (("S", r.get("hit_stop")), ("1", r.get("hit_t1")),
                                   ("2", r.get("hit_t2"))) if f)
    if marks:
        bits.append(f"[{marks}]")
    return "  ".join(bits)


def _aborts_section(rows: list) -> list:
    """ABORTS (n): every setup the invalidation watcher killed.

    Each line carries the time, the entry, where the price went anyway,
    and the reason -- because the question an abort answers is not "did
    it die" but "was dying the right call".
    """
    aborted = [r for r in rows if r.get("aborted")]
    if not aborted:
        return []
    out = ["ABORTS (%d)" % len(aborted)]
    for r in aborted:
        ts = r.get("abort_ts")
        when = ""
        if ts:
            try:
                when = to_et(datetime.fromtimestamp(float(ts), tz=UTC))
                when = when.strftime("%H:%M")
            except (TypeError, ValueError, OSError):
                when = ""
        out.append("  %-6s %s entry %s  now %s  %s" % (
            str(r.get("symbol"))[:6],
            when,
            money(r.get("alert_price")),
            money(r.get("last_price") or r.get("close_price")),
            str(r.get("abort_reason") or "unknown")[:48]))
    return out + [""]


def _rating_section(rows: list) -> list:
    """BY AI RATING: what the AI claimed, next to what the tape did.

    Claimed and actual sit side by side on purpose. A bucket claiming
    0.71 that hit T1 every time is over-cautious; one claiming 0.71
    that hit it 18% of the time is overconfident. Same number,
    opposite problem, and neither is visible in the averages above.
    """
    closed = [r for r in rows if r.get("status") == "closed"]
    groups: dict[str, list] = {}
    for r in closed:
        key = str(r.get("ai_rating") or "unrated").upper()
        groups.setdefault(key, []).append(r)
    if not groups:
        return []
    out = ["BY AI RATING"]
    for name, group in sorted(groups.items()):
        closes = [x.get("close_pct") or 0 for x in group]
        t1 = sum(1 for x in group if x.get("hit_t1"))
        actual = safe_div(t1, len(group))
        claimed = safe_div(sum(x.get("ai_confidence_calibrated") or 0
                               for x in group), len(group))
        out.append("  %-8s %2d alerts  avg close %+.1f%%  T1 %d/%d  "
                   "(claimed %.2f vs actual %.2f)"
                   % (name, len(group),
                      safe_div(sum(closes), len(closes)) * 100,
                      t1, len(group), claimed, actual))
    return out + [""]


def _postmortem_section(rows: list, limit: int = 10) -> list:
    """POST-MORTEMS: one line per losing alert, naming the clause that
    should have blocked it. Capped, because a bad day produces a dozen
    losses and a dozen lines of prose is how the numbers stop being
    read."""
    from .config import DEFAULTS
    from .context.postmortem import is_loser, short_line
    if str(DEFAULTS.get("POSTMORTEM_IN_EOD", "1")).strip() in ("0", "false"):
        return []
    losers = [r for r in rows if is_loser(r)]
    if not losers:
        return []
    out = ["POST-MORTEMS (%d)" % len(losers)]
    for r in losers[:limit]:
        out.append("  " + short_line(r))
    if len(losers) > limit:
        out.append("  ... and %d more (/postmortem SYM)"
                   % (len(losers) - limit))
    return out + [""]


def eod_report(tracker: Tracker, day: Optional[str] = None) -> str:
    """The single end-of-day message.

    Leads with the count, because that is the only number that is always true.
    Hit rates come after, and only over rows that actually closed.
    """
    day = day or to_et().strftime("%Y-%m-%d")
    rows = tracker.rows_for(day, limit=200)
    if not rows:
        return f"END OF DAY {day}\nno alerts."

    closed = [r for r in rows if r.get("status") == "closed"]
    invalid = [r for r in rows if r.get("status") == "invalid"]
    winners = [r for r in closed if (r.get("close_pct") or 0) > 0]
    losers = [r for r in closed if (r.get("close_pct") or 0) <= 0]
    stops = sum(1 for r in closed if r.get("hit_stop"))
    t1s = sum(1 for r in closed if r.get("hit_t1"))
    t2s = sum(1 for r in closed if r.get("hit_t2"))

    avg_close = safe_div(sum((r.get("close_pct") or 0) for r in closed), len(closed))
    avg_max = safe_div(sum((r.get("max_gain_pct") or 0) for r in closed), len(closed))
    best = max(closed, key=lambda r: r.get("close_pct") or 0, default=None)
    worst = min(closed, key=lambda r: r.get("close_pct") or 0, default=None)

    lines = [f"END OF DAY {day}", ""]
    aborted = [r for r in rows if r.get("aborted")]
    lines.append(f"alerts {len(rows)} | closed {len(closed)} | "
                 f"aborted {len(aborted)} | invalidate {len(invalid)}")
    lines.append(f"up {len(winners)} / down {len(losers)} | "
                 f"avg close {pct(avg_close * 100)} | avg max {pct(avg_max * 100)}")
    lines.append(f"stopped {stops} | hit T1 {t1s} | hit T2 {t2s}")
    if best and worst and len(closed) > 1:
        lines.append(f"best {best['symbol']} {pct((best.get('close_pct') or 0) * 100)} | "
                     f"worst {worst['symbol']} {pct((worst.get('close_pct') or 0) * 100)}")
    lines.append("")
    for r in rows[:20]:
        lines.append(summary_line(r))
    if len(rows) > 20:
        lines.append(f"... and {len(rows) - 20} more")
    lines.extend(_aborts_section(rows))
    lines.extend(_rating_section(rows))
    lines.extend(_postmortem_section(rows))
    lines.append("")
    lines.append("S=stop, 1=T1, 2=T2. Screener and decision support only.")
    return "\n".join(lines)


def stats_report(tracker: Tracker, days: int = 30) -> str:
    """Per-strategy performance over the window.

    Grouped by primary strategy because that is the actionable cut: if one
    detector is reliably flat, it should be tightened or switched off, and an
    overall average hides that completely.
    """
    rows = tracker.recent(days)
    if not rows:
        return f"STATS (last {days}d)\nno closed alerts yet."

    by_strategy: dict[str, list[dict]] = {}
    for r in rows:
        by_strategy.setdefault(str(r.get("primary_strategy") or "unknown"), []).append(r)

    lines = [f"STATS (last {days}d, {len(rows)} closed alerts)", ""]
    ranked = sorted(
        by_strategy.items(),
        key=lambda kv: safe_div(sum((x.get("close_pct") or 0) for x in kv[1]), len(kv[1])),
        reverse=True)
    for name, group in ranked:
        closes = [x.get("close_pct") or 0 for x in group]
        avg = safe_div(sum(closes), len(closes))
        hit = safe_div(sum(1 for c in closes if c > 0), len(closes))
        lines.append(f"{name:<20} n={len(group):>3} | avg {pct(avg * 100):>7} | "
                     f"win {hit * 100:>3.0f}%")

    all_closes = [r.get("close_pct") or 0 for r in rows]
    lines.append("")
    lines.append(f"all: avg {pct(safe_div(sum(all_closes), len(all_closes)) * 100)} | "
                 f"win {safe_div(sum(1 for c in all_closes if c > 0), len(all_closes)) * 100:.0f}%")

    # Spec 2.0 D: did the AI earn its call? Every closed alert sits in exactly
    # one provenance bucket, and the hit rates answer the only question that
    # matters about the model's place in the pipeline. Measured, not acted on.
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(_source_bucket(r), []).append(r)
    lines.append("")
    lines.append("BY SOURCE")
    for name in ("agreement", "disagreement", "rules_only", "ai_rejected"):
        group = by_source.get(name)
        if not group:
            continue
        closes = [x.get("close_pct") or 0 for x in group]
        avg = safe_div(sum(closes), len(group))
        hit = safe_div(sum(1 for c in closes if c > 0), len(closes))
        lines.append(f"{name:<14} n={len(group):>3} | avg {pct(avg * 100):>7} | "
                     f"win {hit * 100:>3.0f}%")
    return "\n".join(lines)


def _source_bucket(r: dict) -> str:
    """The provenance bucket for one closed alert (Spec 2.0 D).

    agreement    gate pass + the AI said ENTER
    disagreement gate pass + the AI declined (NO_TRADE/REJECT/ARMED)
    rules_only   the block was built from the rules
    ai_rejected  the model answered but its report was refused
    """
    src = str(r.get("ai_source") or "").lower()
    verdict = str(r.get("ai_verdict") or "").upper()
    if src == "ai" and verdict == "ENTER":
        return "agreement"
    if src == "ai" and verdict in ("NO_TRADE", "REJECT", "ARMED", "WAIT", "PASS"):
        return "disagreement"
    if src == "ai_rejected":
        return "ai_rejected"
    return "rules_only"