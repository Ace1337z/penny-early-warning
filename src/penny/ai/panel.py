"""AI panel: prompt building, tolerant parsing, aggregation and the analysis cache (6.11)."""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..util import clamp, parse_json_lenient

log = logging.getLogger(__name__)

HORIZONS = ("15m", "60m", "session_end")

ENUMS = {
    "strength": ("strong", "moderate", "weak"),
    "direction": ("up", "down", "mixed"),
    "shape": ("burst", "grinder", "fade"),
    "durability": ("fade-prone", "uncertain", "sustained"),
}

FLAG_VALUES = ("dilution", "reverse_split", "halt_risk", "wide_spread", "pump_pattern",
               "low_float", "squeeze_setup", "no_catalyst")

MAX_PROMPT_CHARS = 7000

SYSTEM_PROMPT = (
    "You are a trading analyst for US penny stocks (under $10). You receive one JSON object "
    "of facts about a stock that is building a move. Predict (a) how the market will react to "
    "the move and its catalyst and (b) the price at three horizons.\n"
    "Rules: use only the supplied facts; if the news does not explain the move, say so and "
    "lower confidence; never invent news or price levels unrelated to the data; give numeric "
    "prices for all three horizons; do not output a risk level.\n"
    "Reply with ONE JSON object and nothing else, with exactly these keys:\n"
    '{"strength": "strong|moderate|weak", "direction": "up|down|mixed", '
    '"shape": "burst|grinder|fade", "durability": "fade-prone|uncertain|sustained", '
    '"confidence": 0.0-1.0, "catalyst": "<=160 chars", "sentiment": -1.0-1.0, '
    '"forecast": {"15m": {"price": number}, "60m": {"price": number}, '
    '"session_end": {"price": number}}, "expected_peak": number, "expected_low": number, '
    '"market_effect": "<=120 chars", "reasons": ["<=3 short items"], '
    '"flags": ["dilution|reverse_split|halt_risk|wide_spread|pump_pattern|low_float|'
    'squeeze_setup|no_catalyst"], "change_view": "what would change the view"}'
)


@dataclass
class ModelForecast:
    model: str
    valid: bool = False
    error: str = ""
    strength: str = "unclear"
    direction: str = "unclear"
    shape: str = "unclear"
    durability: str = "unclear"
    confidence: float = 0.0
    catalyst: str = ""
    sentiment: float = 0.0
    forecast: dict[str, float] = field(default_factory=dict)
    expected_peak: Optional[float] = None
    expected_low: Optional[float] = None
    market_effect: str = ""
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    change_view: str = ""
    tokens: int = 0
    latency_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "model": self.model, "valid": self.valid, "strength": self.strength,
            "direction": self.direction, "shape": self.shape, "durability": self.durability,
            "confidence": round(self.confidence, 2), "catalyst": self.catalyst,
            "sentiment": round(self.sentiment, 2), "forecast": self.forecast,
            "expected_peak": self.expected_peak, "expected_low": self.expected_low,
            "market_effect": self.market_effect, "reasons": self.reasons, "flags": self.flags,
            "change_view": self.change_view,
        }


@dataclass
class AggregateForecast:
    n_models: int = 0
    models: list[str] = field(default_factory=list)
    agreement: float = 0.0
    strength: str = "unclear"
    direction: str = "unclear"
    shape: str = "unclear"
    durability: str = "unclear"
    confidence: float = 0.0
    catalyst: str = ""
    sentiment: float = 0.0
    forecast: dict[str, float] = field(default_factory=dict)
    expected_peak: Optional[float] = None
    expected_low: Optional[float] = None
    market_effect: str = ""
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    change_view: str = ""
    validated: bool = True

    def as_dict(self) -> dict:
        return {
            "n_models": self.n_models, "models": self.models,
            "agreement": round(self.agreement, 2), "strength": self.strength,
            "direction": self.direction, "shape": self.shape, "durability": self.durability,
            "confidence": round(self.confidence, 2), "catalyst": self.catalyst,
            "sentiment": round(self.sentiment, 2), "forecast": self.forecast,
            "expected_peak": self.expected_peak, "expected_low": self.expected_low,
            "market_effect": self.market_effect, "reasons": self.reasons, "flags": self.flags,
            "change_view": self.change_view, "validated": self.validated,
        }


# --- prompt -----------------------------------------------------------------

def build_messages(facts: dict, max_chars: int = MAX_PROMPT_CHARS) -> list[dict]:
    """Compact prompt: only the documented fields, rounded numbers, no raw candles."""
    payload = _compact(facts)
    text = json.dumps(payload, separators=(",", ":"), default=str)
    # Trim news first, then optional fields, until the cap is met.
    if len(text) > max_chars and payload.get("news"):
        while payload.get("news") and len(text) > max_chars:
            payload["news"] = payload["news"][:-1]
            text = json.dumps(payload, separators=(",", ":"), default=str)
    for optional in ("insider", "short", "filings", "reasons_why"):
        if len(text) <= max_chars:
            break
        payload.pop(optional, None)
        text = json.dumps(payload, separators=(",", ":"), default=str)
    if len(text) > max_chars:
        text = text[:max_chars]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]


def _round(x: Any, digits: int = 4) -> Any:
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        return round(float(x), digits)
    return x


def _compact(facts: dict) -> dict:
    out: dict[str, Any] = {}
    for key, value in facts.items():
        if value is None:
            continue
        if isinstance(value, float):
            out[key] = _round(value)
        elif isinstance(value, dict):
            out[key] = {k: _round(v) for k, v in value.items() if v is not None}
        elif isinstance(value, list):
            out[key] = value
        else:
            out[key] = value
    if "news" in out and isinstance(out["news"], list):
        trimmed = []
        for item in out["news"][:6]:
            if isinstance(item, dict):
                trimmed.append({
                    "title": str(item.get("title", ""))[:200],
                    "source": item.get("source"),
                    "age": item.get("age"),
                    "summary": str(item.get("summary", ""))[:200],
                })
            else:
                trimmed.append(str(item)[:200])
        out["news"] = trimmed
    return out


# --- parsing ----------------------------------------------------------------

def parse_forecast(model: str, text: str) -> ModelForecast:
    """Tolerant parse: strip fences, take first { to last }, clamp, drop bad prices."""
    fc = ModelForecast(model=model)
    try:
        data = parse_json_lenient(text)
    except (ValueError, TypeError) as exc:
        fc.error = f"unparseable: {exc}"
        return fc
    if not isinstance(data, dict):
        fc.error = "not a JSON object"
        return fc

    for key, allowed in ENUMS.items():
        raw = str(data.get(key, "")).strip().lower()
        fc.__dict__[key] = raw if raw in allowed else "unclear"

    try:
        fc.confidence = clamp(float(data.get("confidence", 0.0)), 0.0, 1.0)
    except (TypeError, ValueError):
        fc.confidence = 0.0
    try:
        fc.sentiment = clamp(float(data.get("sentiment", 0.0)), -1.0, 1.0)
    except (TypeError, ValueError):
        fc.sentiment = 0.0

    fc.catalyst = str(data.get("catalyst") or "")[:160]
    fc.market_effect = str(data.get("market_effect") or "")[:120]
    fc.change_view = str(data.get("change_view") or "")[:200]

    raw_forecast = data.get("forecast") or {}
    prices: dict[str, float] = {}
    if isinstance(raw_forecast, dict):
        for horizon in HORIZONS:
            entry = raw_forecast.get(horizon)
            value = None
            if isinstance(entry, dict):
                value = entry.get("price")
            elif isinstance(entry, (int, float)):
                value = entry
            try:
                fval = float(value)
            except (TypeError, ValueError):
                continue
            if fval > 0:
                prices[horizon] = fval
    fc.forecast = prices

    for attr in ("expected_peak", "expected_low"):
        try:
            val = float(data.get(attr))
            if val > 0:
                setattr(fc, attr, val)
        except (TypeError, ValueError):
            pass

    reasons = data.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    fc.reasons = [str(r)[:120] for r in reasons][:3]

    flags = data.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    fc.flags = [str(f).strip().lower() for f in flags
                if str(f).strip().lower() in FLAG_VALUES]

    fc.valid = bool(prices) and fc.direction != "unclear"
    if not fc.valid:
        fc.error = "missing forecast prices or direction"
    return fc


# --- aggregation ------------------------------------------------------------

def _majority(values: list[str]) -> tuple[str, float]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return "unclear", 0.0
    winner = max(counts, key=lambda k: counts[k])
    return winner, counts[winner] / len(values)


def aggregate(forecasts: list[ModelForecast]) -> AggregateForecast:
    valid = [f for f in forecasts if f.valid]
    agg = AggregateForecast(n_models=len(valid), models=[f.model for f in valid])
    if not valid:
        # No model answered: anything shown must be labelled unvalidated.
        agg.validated = False
        return agg

    shares = []
    for field_name in ENUMS:
        winner, share = _majority([getattr(f, field_name) for f in valid])
        setattr(agg, field_name, winner)
        shares.append(share)
    agg.agreement = sum(shares) / len(shares) if shares else 0.0

    mean_conf = sum(f.confidence for f in valid) / len(valid)
    agg.confidence = clamp(mean_conf * (0.6 + 0.4 * agg.agreement), 0.0, 1.0)
    agg.sentiment = statistics.median([f.sentiment for f in valid])

    for horizon in HORIZONS:
        values = [f.forecast[horizon] for f in valid if horizon in f.forecast]
        if values:
            agg.forecast[horizon] = round(statistics.median(values), 4)

    peaks = [f.expected_peak for f in valid if f.expected_peak]
    lows = [f.expected_low for f in valid if f.expected_low]
    agg.expected_peak = round(statistics.median(peaks), 4) if peaks else None
    agg.expected_low = round(statistics.median(lows), 4) if lows else None

    flags: list[str] = []
    for f in valid:
        for flag in f.flags:
            if flag not in flags and flag != "none":
                flags.append(flag)
    agg.flags = flags

    reasons: list[str] = []
    for f in valid:
        for r in f.reasons:
            if r not in reasons:
                reasons.append(r)
            if len(reasons) >= 4:
                break
        if len(reasons) >= 4:
            break
    agg.reasons = reasons

    first = valid[0]
    agg.catalyst = first.catalyst
    agg.market_effect = first.market_effect
    agg.change_view = first.change_view
    return agg


def reaction_text(agg: AggregateForecast) -> str:
    if agg.n_models == 0:
        return "AI unavailable"
    bits = [
        f"{agg.strength}/{agg.direction}/{agg.shape}/{agg.durability}",
        f"confidence {agg.confidence:.2f}",
        f"agreement {agg.agreement:.2f} across {agg.n_models} model(s)",
    ]
    lines = ["REACTION: " + " | ".join(bits)]
    if agg.catalyst:
        lines.append(f"catalyst: {agg.catalyst}")
    if agg.reasons:
        lines.append("reasons: " + "; ".join(agg.reasons))
    if agg.sentiment:
        lines.append(f"sentiment {agg.sentiment:+.2f}")
    if agg.flags:
        lines.append("flags: " + ", ".join(agg.flags))
    if agg.change_view:
        lines.append(f"changes view: {agg.change_view}")
    return "\n".join(lines)


def price_forecast_text(agg: AggregateForecast, price0: float, label: str = "") -> str:
    if not agg.forecast:
        return ""
    def as_pct(p: Optional[float]) -> str:
        if not p or not price0:
            return "n/a"
        return f"{p:.4f} ({(p / price0 - 1) * 100:+.1f}%)"
    lines = [
        f"PRICE FORECAST{label}: "
        f"+15m {as_pct(agg.forecast.get('15m'))} | "
        f"+60m {as_pct(agg.forecast.get('60m'))} | "
        f"session end {as_pct(agg.forecast.get('session_end'))}",
        f"expected peak {agg.expected_peak:.4f}" if agg.expected_peak else "expected peak n/a",
        f"expected low {agg.expected_low:.4f}" if agg.expected_low else "expected low n/a",
    ]
    if agg.market_effect:
        lines.append(f"market effect: {agg.market_effect}")
    return " | ".join(lines[:3]) + ("\n" + lines[3] if len(lines) > 3 else "")


# --- cache ------------------------------------------------------------------

class AnalysisCache:
    """One analysis per stock per alert window (6.11 rule 2)."""

    def __init__(self, ttl_minutes: float = 10.0):
        self.ttl = ttl_minutes * 60.0
        self._data: dict[str, tuple[float, dict]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, symbol: str, *, price: float, tier: int, news_key: str = "") -> Optional[dict]:
        item = self._data.get(symbol)
        if not item:
            self.misses += 1
            return None
        ts, entry = item
        if time.time() - ts > self.ttl:
            self.misses += 1
            return None
        if tier > entry.get("tier", 0):
            self.misses += 1
            return None
        if news_key and news_key != entry.get("news_key"):
            self.misses += 1
            return None
        last_price = entry.get("price") or 0
        if last_price and price and abs(price / last_price - 1.0) >= 0.10:
            self.misses += 1
            return None
        self.hits += 1
        return entry.get("result")

    def put(self, symbol: str, result: dict, *, price: float, tier: int, news_key: str = "") -> None:
        self._data[symbol] = (time.time(), {
            "result": result, "price": price, "tier": tier, "news_key": news_key})

    def clear(self) -> None:
        self._data.clear()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0
