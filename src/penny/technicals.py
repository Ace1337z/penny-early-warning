"""Technical indicators and timeframe summaries (6.9)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .util import ET, median, safe_div

log = logging.getLogger(__name__)


def ema(values: list[float], span: int) -> list[float]:
    """Exponential moving average, span form, adjust=False."""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder-smoothed RSI (alpha = 1/period)."""
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def resample(bars: list[dict], minutes: int) -> list[dict]:
    """Resample 1-minute bars into N-minute bars (bucket by epoch)."""
    if not bars or minutes <= 1:
        return list(bars)
    bucket = minutes * 60
    groups: dict[int, list[dict]] = {}
    for b in bars:
        key = (int(b["time"]) // bucket) * bucket
        groups.setdefault(key, []).append(b)
    out = []
    for key in sorted(groups):
        chunk = groups[key]
        out.append({
            "time": key,
            "open": chunk[0]["open"],
            "high": max(x["high"] for x in chunk),
            "low": min(x["low"] for x in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(x["volume"] or 0 for x in chunk),
        })
    return out


def anchored_vwap(bars: list[dict], anchor_hour: int = 4) -> Optional[float]:
    """VWAP anchored at 04:00 ET each day, from candle data."""
    if not bars:
        return None
    day = datetime.fromtimestamp(bars[-1]["time"], tz=ET).date()
    anchor = datetime(day.year, day.month, day.day, anchor_hour, 0, tzinfo=ET).timestamp()
    pv = 0.0
    vol = 0.0
    for b in bars:
        if b["time"] < anchor:
            continue
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        v = b["volume"] or 0.0
        pv += typical * v
        vol += v
    if vol <= 0:
        return None
    return pv / vol


def volume_spike(bars: list[dict]) -> Optional[float]:
    """Mean volume of the last 5 one-minute bars / median of the prior 60."""
    if len(bars) < 10:
        return None
    recent = [b["volume"] or 0.0 for b in bars[-5:]]
    prior = [b["volume"] or 0.0 for b in bars[-65:-5]]
    med = median(prior)
    if not med:
        return None
    return safe_div(sum(recent) / len(recent), med, None)


def pivots(bars: list[dict], side: int = 2) -> tuple[list[float], list[float]]:
    """Pivot lows and highs with `side` bars on each side."""
    lows: list[float] = []
    highs: list[float] = []
    for i in range(side, len(bars) - side):
        window = bars[i - side:i + side + 1]
        low = bars[i]["low"]
        high = bars[i]["high"]
        if low is not None and low == min(w["low"] for w in window):
            lows.append(low)
        if high is not None and high == max(w["high"] for w in window):
            highs.append(high)
    return lows, highs


def trend_of(closes: list[float]) -> str:
    if len(closes) < 21:
        return "flat"
    e9 = ema(closes, 9)
    e20 = ema(closes, 20)
    close = closes[-1]
    if e9[-1] > e20[-1] and close > e20[-1]:
        return "up"
    if e9[-1] < e20[-1]:
        return "down"
    return "flat"


@dataclass
class TimeframeRead:
    name: str
    trend: str = "flat"
    rsi: Optional[float] = None
    close: Optional[float] = None
    ema9: Optional[float] = None
    ema20: Optional[float] = None

    def as_dict(self) -> dict:
        return {"trend": self.trend, "rsi": round(self.rsi, 1) if self.rsi is not None else None}


@dataclass
class Technicals:
    timeframes: dict[str, TimeframeRead] = field(default_factory=dict)
    vwap: Optional[float] = None
    volume_spike: Optional[float] = None
    support: list[float] = field(default_factory=list)
    resistance: list[float] = field(default_factory=list)
    swing_low: Optional[float] = None
    swing_high: Optional[float] = None
    swing_range_pct: Optional[float] = None
    price: Optional[float] = None

    def summary_text(self) -> str:
        parts = []
        for name in ("15m", "5m", "1m"):
            tf = self.timeframes.get(name)
            if tf:
                rsi_txt = f" RSI {tf.rsi:.0f}" if tf.rsi is not None else ""
                parts.append(f"{name} {tf.trend}{rsi_txt}")
        if self.vwap:
            parts.append(f"VWAP {self.vwap:.4f}")
        if self.volume_spike:
            parts.append(f"vol spike {self.volume_spike:.1f}x")
        if self.support:
            parts.append("sup " + "/".join(f"{x:.3f}" for x in self.support[-3:]))
        if self.resistance:
            parts.append("res " + "/".join(f"{x:.3f}" for x in self.resistance[-3:]))
        return " | ".join(parts) if parts else "no data"

    def as_dict(self) -> dict:
        return {
            "timeframes": {k: v.as_dict() for k, v in self.timeframes.items()},
            "vwap": round(self.vwap, 5) if self.vwap else None,
            "volume_spike": round(self.volume_spike, 2) if self.volume_spike else None,
            "support": [round(x, 4) for x in self.support[-3:]],
            "resistance": [round(x, 4) for x in self.resistance[-3:]],
            "swing": {"low": self.swing_low, "high": self.swing_high,
                      "range_pct": self.swing_range_pct},
        }


def analyze(bars_1m: list[dict], price: Optional[float] = None) -> Technicals:
    """Full technical read from 1-minute bars including pre/post (6.9)."""
    tech = Technicals(price=price)
    if not bars_1m:
        return tech
    bars_1m = sorted(bars_1m, key=lambda b: b["time"])
    closes = [b["close"] for b in bars_1m if b.get("close")]

    bars_5m = resample(bars_1m, 5)
    bars_15m = resample(bars_1m, 15)

    for name, bars in (("1m", bars_1m), ("5m", bars_5m), ("15m", bars_15m)):
        cl = [b["close"] for b in bars if b.get("close")]
        tf = TimeframeRead(name=name)
        tf.trend = trend_of(cl)
        r = rsi(cl, 14)
        tf.rsi = next((v for v in reversed(r) if v is not None), None)
        if cl:
            tf.close = cl[-1]
            if len(cl) >= 9:
                tf.ema9 = ema(cl, 9)[-1]
            if len(cl) >= 20:
                tf.ema20 = ema(cl, 20)[-1]
        tech.timeframes[name] = tf

    tech.vwap = anchored_vwap(bars_1m, 4)
    tech.volume_spike = volume_spike(bars_1m)

    lows, highs = pivots(bars_5m, 2)
    tech.support = lows[-3:]
    tech.resistance = highs[-3:]

    # Swing: lowest low in the last 240 one-minute bars, then the highest high after it.
    window = bars_1m[-240:]
    if window:
        low_idx = min(range(len(window)), key=lambda i: window[i]["low"])
        after = window[low_idx:]
        low = window[low_idx]["low"]
        high = max(b["high"] for b in after)
        if price and price > 0 and (high - low) < 0.05 * price:
            tech.swing_low = None
            tech.swing_high = None
            tech.swing_range_pct = None
        else:
            tech.swing_low = low
            tech.swing_high = high
            tech.swing_range_pct = safe_div(high - low, low, 0.0) * 100.0
    return tech
