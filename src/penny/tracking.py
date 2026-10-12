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
from typing import Optional

from .util import money, now_et, pct, safe_div, to_et

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, db, clock=None):
        self.db = db
        self.clock = clock or (lambda: __import__("time").time())

    # -- lifecycle ---------------------------------------------------------
    def start(self, symbol: str, *, alert_ts: float, alert_price: float,
              day: Optional[str] = None, primary: Optional[str] = None,
              entry_trigger: Optional[str] = None, gate_score: Optional[float] = None,
              plan: Optional[dict] = None, plan_entry_low: Optional[float] = None,
              plan_entry_high: Optional[float] = None) -> Optional[int]:
        """Record an alert. One row per symbol per day; a re-alert refreshes it."""
        day = day or to_et().strftime("%Y-%m-%d")
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
                 (plan or {}).get("stop"), (plan or {}).get("t1"),
                 (plan or {}).get("t2")))
            row = self.db.query_one(
                "SELECT id FROM alert_tracking WHERE symbol=? AND day=?",
                (symbol.upper(), day))
            return int(row["id"]) if row else None
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker start failed for %s: %s", symbol, exc)
            return None

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
    lines.append(f"alerts {len(rows)} | closed {len(closed)} | "
                 f"invalidate {len(invalid)}")
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
    return "\n".join(lines)