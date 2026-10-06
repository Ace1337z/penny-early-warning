"""Journal: events, bar download, metrics, rule search and auto-logging (6.14)."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .sources.base import SourceError
from .util import ET, median, safe_div, to_et

log = logging.getLogger(__name__)

PATTERNS = ("burst", "grinder", "other", "unknown")

# Rule search grid (6.14).
RULE_WINDOWS = (5, 10, 15, 30, 60)
RULE_RISE = (0.06, 0.10, 0.15, 0.25)
RULE_VOLUME = (3, 10, 30, 100)
RULE_DOLLAR = (2000, 10000)
RULE_ABOVE_VWAP = (False, True)

PRELIMINARY_WINNERS = 30
PRELIMINARY_LOSERS = 100
FIRM_WINNERS = 100
FIRM_LOSERS = 300


@dataclass
class EventMetrics:
    symbol: str = ""
    date: str = ""
    reference_close: Optional[float] = None
    peak_price: Optional[float] = None
    peak_pct: Optional[float] = None
    peak_time: Optional[str] = None
    time_first_10: Optional[str] = None
    minutes_to_peak_from_10: Optional[float] = None
    minutes_to_half_gain: Optional[float] = None
    preopen_pct: Optional[float] = None
    regular_close_pct: Optional[float] = None
    drawdown_after_peak: Optional[float] = None
    next_day_high_pct: Optional[float] = None
    next_day_close_pct: Optional[float] = None
    pattern: str = "unknown"

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class RuleResult:
    window: int
    rise: float
    volume: float
    dollar: float
    above_vwap: bool
    fired_winners: int = 0
    winners: int = 0
    capture: Optional[float] = None
    already_up: Optional[float] = None
    false_alerts: int = 0
    losers: int = 0
    false_rate: Optional[float] = None
    score: Optional[float] = None
    label: str = "PRELIMINARY"

    def key(self) -> tuple:
        return (self.window, self.rise, self.volume, self.dollar, self.above_vwap)

    def describe(self) -> str:
        return (f"{self.window}m rise>={self.rise * 100:.0f}% volx>={self.volume} "
                f"${self.dollar:,.0f} vwap={'yes' if self.above_vwap else 'no'}")

    def as_dict(self) -> dict:
        return {**{k: v for k, v in self.__dict__.items()},
                "description": self.describe()}


class Journal:
    def __init__(self, cfg, db, *, alpaca=None, yahoo=None):
        self.cfg = cfg
        self.db = db
        self.alpaca = alpaca
        self.yahoo = yahoo

    # -- events -------------------------------------------------------------
    def add_event(self, symbol: str, day: str, label: str, *, pattern: str = "",
                  peak_pct: Optional[float] = None, entry: Optional[float] = None,
                  exit_price: Optional[float] = None, notes: str = "",
                  source: str = "user", shariah: str = "",
                  overwrite: bool = False) -> int:
        symbol = symbol.upper().strip()
        label = (label or "").lower()
        if label not in ("winner", "loser", "w", "l"):
            raise ValueError("label must be winner or loser")
        label = "winner" if label in ("winner", "w") else "loser"
        pattern = pattern if pattern in PATTERNS else "unknown"
        existing = self.db.query_one("SELECT * FROM events WHERE symbol=? AND date=?",
                                     (symbol, day))
        if existing is not None:
            if existing["source"] == "user" and not overwrite:
                return int(existing["id"])
            self.db.execute(
                "UPDATE events SET label=?, pattern=?, peak_pct=?, entry=?, exit=?, notes=?, "
                "source=?, shariah=? WHERE id=?",
                (label, pattern, peak_pct, entry, exit_price, notes, source, shariah,
                 existing["id"]))
            return int(existing["id"])
        cur = self.db.execute(
            "INSERT INTO events(symbol, date, label, pattern, peak_pct, entry, exit, notes, "
            "source, fetched, shariah) VALUES(?,?,?,?,?,?,?,?,?,0,?)",
            (symbol, day, label, pattern, peak_pct, entry, exit_price, notes, source, shariah))
        return int(cur.lastrowid or 0)

    def import_batch(self, text: str) -> int:
        """Batch text file: lines `SYMBOL DATE w|l [pattern] [peak%]` (6.14)."""
        count = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            symbol, day, label = parts[0], parts[1], parts[2]
            pattern = parts[3] if len(parts) > 3 else ""
            peak = None
            if len(parts) > 4:
                try:
                    peak = float(parts[4].replace("%", ""))
                except ValueError:
                    peak = None
            try:
                self.add_event(symbol, day, label, pattern=pattern, peak_pct=peak,
                               source="user")
                count += 1
            except ValueError as exc:
                log.warning("journal import skipped '%s': %s", line, exc)
        return count

    def events(self, label: Optional[str] = None, limit: int = 500) -> list:
        if label:
            return self.db.query("SELECT * FROM events WHERE label=? ORDER BY date DESC LIMIT ?",
                                 (label, limit))
        return self.db.query("SELECT * FROM events ORDER BY date DESC LIMIT ?", (limit,))

    def labels(self) -> dict:
        rows = self.db.query("SELECT label, COUNT(*) AS c FROM events GROUP BY label")
        out = {r["label"]: r["c"] for r in rows}
        winners = out.get("winner", 0)
        losers = out.get("loser", 0)
        if winners < PRELIMINARY_WINNERS or losers < PRELIMINARY_LOSERS:
            trust = "PRELIMINARY"
        elif winners < FIRM_WINNERS or losers < FIRM_LOSERS:
            trust = "MODERATE"
        else:
            trust = "FIRM"
        return {"winners": winners, "losers": losers, "trust": trust}

    # -- bar download -------------------------------------------------------
    def bar_path(self, symbol: str, day: str) -> Path:
        p = self.cfg.bars_dir / symbol.upper() / f"{day}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def download_bars(self, symbol: str, event_day: str) -> list[dict]:
        """1-minute bars for event day -3 to +4, stored per day (6.14)."""
        try:
            day = date.fromisoformat(event_day)
        except ValueError:
            return []
        collected: list[dict] = []
        for offset in range(-3, 5):
            d = day + timedelta(days=offset)
            if d.weekday() >= 5:
                continue
            path = self.bar_path(symbol, d.isoformat())
            if path.exists() and path.stat().st_size > 0:
                continue
            bars = self._fetch_day(symbol, d)
            if not bars:
                continue
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["time", "open", "high", "low", "close", "volume"])
                for b in bars:
                    writer.writerow([b["time"], b["open"], b["high"], b["low"], b["close"],
                                     b["volume"]])
            collected.extend(bars)
        self.db.execute("UPDATE events SET fetched=1 WHERE symbol=? AND date=?",
                        (symbol.upper(), event_day))
        return collected

    def _fetch_day(self, symbol: str, d: date) -> list[dict]:
        start = datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET)
        end = start + timedelta(hours=16)
        if self.alpaca and getattr(self.alpaca, "configured", False):
            try:
                return self.alpaca.bars(symbol, start, end)
            except Exception as exc:  # noqa: BLE001
                log.debug("alpaca bars failed for %s %s: %s", symbol, d, exc)
        if self.yahoo:
            try:
                return self.yahoo.bars(symbol, days=7)
            except Exception as exc:  # noqa: BLE001
                log.debug("yahoo bars failed for %s %s: %s", symbol, d, exc)
        return []

    def load_bars(self, symbol: str, day: str) -> list[dict]:
        path = self.bar_path(symbol, day)
        if not path.exists():
            return []
        out = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    out.append({"time": int(row["time"]), "open": float(row["open"]),
                                "high": float(row["high"]), "low": float(row["low"]),
                                "close": float(row["close"]), "volume": float(row["volume"])})
                except (ValueError, KeyError, TypeError):
                    continue
        return out

    # -- metrics ------------------------------------------------------------
    def metrics_for(self, symbol: str, day: str) -> Optional[EventMetrics]:
        bars = self.load_bars(symbol, day)
        if not bars:
            return None
        return self.compute_metrics(symbol, day, bars)

    def compute_metrics(self, symbol: str, day: str, bars: list[dict]) -> EventMetrics:
        """Frame: previous day's post-market through the event day (6.14)."""
        bars = sorted(bars, key=lambda b: b["time"])
        event = date.fromisoformat(day)
        prev = event - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)

        ref_close = None
        for b in bars:
            d = to_et(datetime.fromtimestamp(b["time"], tz=ET)).date()
            if d < event and b["close"]:
                ref_close = b["close"]
        if ref_close is None and bars:
            ref_close = bars[0]["open"]

        event_bars = [b for b in bars
                      if to_et(datetime.fromtimestamp(b["time"], tz=ET)).date() == event]
        m = EventMetrics(symbol=symbol, date=day, reference_close=ref_close)
        if not event_bars or not ref_close:
            return m

        peak_bar = max(event_bars, key=lambda b: b["high"])
        m.peak_price = peak_bar["high"]
        m.peak_pct = (m.peak_price / ref_close - 1) * 100
        m.peak_time = to_et(datetime.fromtimestamp(peak_bar["time"], tz=ET)).strftime("%H:%M")

        first10 = None
        for b in event_bars:
            if b["high"] and b["high"] / ref_close - 1 >= 0.10:
                first10 = b
                break
        if first10:
            m.time_first_10 = to_et(datetime.fromtimestamp(first10["time"], tz=ET)).strftime("%H:%M")
            m.minutes_to_peak_from_10 = (peak_bar["time"] - first10["time"]) / 60.0
            half = ref_close + 0.5 * (m.peak_price - ref_close)
            for b in event_bars:
                if b["time"] >= first10["time"] and b["high"] and b["high"] >= half:
                    m.minutes_to_half_gain = (b["time"] - first10["time"]) / 60.0
                    break
        if m.minutes_to_half_gain is not None:
            m.pattern = "burst" if m.minutes_to_half_gain <= 30 else "grinder"
        elif m.peak_pct and m.peak_pct >= 10:
            m.pattern = "other"

        pre = [b for b in event_bars
               if to_et(datetime.fromtimestamp(b["time"], tz=ET)).hour < 9]
        if pre:
            m.preopen_pct = (pre[-1]["close"] / ref_close - 1) * 100
        regular = [b for b in event_bars
                   if 9 <= to_et(datetime.fromtimestamp(b["time"], tz=ET)).hour < 16]
        if regular:
            m.regular_close_pct = (regular[-1]["close"] / ref_close - 1) * 100
        after_peak = [b for b in event_bars if b["time"] >= peak_bar["time"]]
        if after_peak:
            trough = min(b["low"] for b in after_peak)
            m.drawdown_after_peak = (trough / m.peak_price - 1) * 100

        next_day = event + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        nxt = [b for b in bars
               if to_et(datetime.fromtimestamp(b["time"], tz=ET)).date() == next_day]
        if nxt:
            event_close = regular[-1]["close"] if regular else event_bars[-1]["close"]
            m.next_day_high_pct = (max(b["high"] for b in nxt) / event_close - 1) * 100
            m.next_day_close_pct = (nxt[-1]["close"] / event_close - 1) * 100
        return m

    def annotate_metrics(self, symbol: str, day: str) -> Optional[EventMetrics]:
        m = self.metrics_for(symbol, day)
        if m is None:
            return None
        self.db.execute("UPDATE events SET metrics=?, pattern=? WHERE symbol=? AND date=?",
                        (json.dumps(m.as_dict()), m.pattern, symbol.upper(), day))
        return m

    def discover(self, symbol: str, days: int = 30) -> list[dict]:
        """List a ticker's three biggest-gain days from daily bars (6.14)."""
        bars = []
        if self.yahoo:
            try:
                bars = self.yahoo.bars(symbol, days=min(days, 60), interval="1d")
            except Exception:  # noqa: BLE001
                bars = []
        if not bars:
            return []
        gains = []
        for i in range(1, len(bars)):
            prev = bars[i - 1]["close"]
            if prev:
                gains.append({"date": datetime.fromtimestamp(bars[i]["time"], tz=ET).strftime("%Y-%m-%d"),
                              "gain_pct": (bars[i]["close"] / prev - 1) * 100})
        gains.sort(key=lambda g: g["gain_pct"], reverse=True)
        return gains[:3]

    # -- rule search --------------------------------------------------------
    def rule_search(self, limit: int = 200, progress=None) -> list[RuleResult]:
        """Evaluate the full rule grid against journal events (6.14)."""
        rows = self.db.query("SELECT * FROM events ORDER BY date DESC LIMIT ?", (limit,))
        winners = [r for r in rows if r["label"] == "winner"]
        losers = [r for r in rows if r["label"] == "loser"]
        results: list[RuleResult] = []

        winner_series = self._series_for(winners)
        loser_series = self._series_for(losers)
        mixed_sources = len({r["source"] for r in rows}) > 1

        for window in RULE_WINDOWS:
            for rise in RULE_RISE:
                for volume in RULE_VOLUME:
                    for dollar in RULE_DOLLAR:
                        for above_vwap in RULE_ABOVE_VWAP:
                            rule = RuleResult(window=window, rise=rise, volume=volume,
                                              dollar=dollar, above_vwap=above_vwap)
                            captures = []
                            for series in winner_series:
                                fill = self._first_fill(series, rule)
                                if fill is None:
                                    captures.append(0.0)
                                    continue
                                peak = series["peak"]
                                ref = series["ref_close"]
                                denom = peak - ref
                                capture = 0.0 if denom <= 0 else (peak - fill) / denom
                                captures.append(max(-1.0, min(1.0, capture)))
                                rule.fired_winners += 1
                                rule.already_up = ((rule.already_up or 0) +
                                                   (fill / ref - 1)) / max(1, rule.fired_winners)
                            rule.winners = len(winner_series)
                            rule.capture = (sum(captures) / len(captures)) if captures else None
                            false = sum(1 for series in loser_series
                                        if self._first_fill(series, rule) is not None)
                            rule.losers = len(loser_series)
                            rule.false_alerts = false
                            rule.false_rate = (false / len(loser_series)) if loser_series else None
                            if rule.capture is not None:
                                penalty = 0.5 * (rule.false_rate or 0.0)
                                rule.score = rule.capture - penalty
                            rule.label = self.labels()["trust"]
                            if mixed_sources:
                                rule.label += " (mixed sources)"
                            results.append(rule)
        results.sort(key=lambda r: (r.score is None, -(r.score or 0)))
        return self._dedupe(results)

    @staticmethod
    def _dedupe(results: list[RuleResult]) -> list[RuleResult]:
        seen: set[tuple] = set()
        out = []
        for r in results:
            sig = (round(r.capture or 0, 4), round(r.false_rate or -1, 4))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(r)
        return out

    def _series_for(self, rows) -> list[dict]:
        series = []
        for row in rows:
            bars = self.load_bars(row["symbol"], row["date"])
            if not bars:
                continue
            m = self.compute_metrics(row["symbol"], row["date"], bars)
            if m.reference_close is None or m.peak_price is None:
                continue
            series.append({"bars": bars, "ref_close": m.reference_close,
                           "peak": m.peak_price, "date": row["date"], "symbol": row["symbol"]})
        return series

    def _first_fill(self, series: dict, rule: RuleResult) -> Optional[float]:
        """First minute where every condition holds; the fill is the next bar's open."""
        bars = series["bars"]
        ref = series["ref_close"]
        for i in range(rule.window, len(bars) - 1):
            window = bars[i - rule.window:i + 1]
            low = min(b["low"] for b in window)
            cur = bars[i]["close"]
            if not low or not cur:
                continue
            rise = cur / low - 1
            vol_added = max(0.0, bars[i]["volume"])
            volx = safe_div(vol_added, 1.0, 0.0)
            dollar_vol = vol_added * cur
            # Volume multiple uses the day's median minute volume as the baseline.
            med = median([b["volume"] for b in bars[:i]] or [1.0]) or 1.0
            volx = safe_div(vol_added, med, 0.0)
            if rise < rule.rise or volx < rule.volume or dollar_vol < rule.dollar:
                continue
            if rule.above_vwap:
                pv = sum(((b["high"] + b["low"] + b["close"]) / 3) * b["volume"]
                         for b in bars[:i + 1])
                vol = sum(b["volume"] for b in bars[:i + 1])
                if not vol or cur <= pv / vol:
                    continue
            return bars[i + 1]["open"] or cur
        return None

    # -- reports ------------------------------------------------------------
    def summary_text(self) -> str:
        labels = self.labels()
        lines = [f"JOURNAL: {labels['winners']} winners, {labels['losers']} losers "
                 f"({labels['trust']})"]
        for row in self.events(limit=5):
            lines.append(f"  {row['date']} {row['symbol']} {row['label']} "
                         f"{row['pattern'] or ''} {row['peak_pct'] or ''}")
        return "\n".join(lines)
