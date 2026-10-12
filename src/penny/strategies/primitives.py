"""Primary detectors that need nothing beyond the per-cycle quote.

These are the ones that can run for every symbol the screener ranks, on every
poll, with no enrichment and therefore no extra fetch. That matters: the gate has
to be able to reject a symbol *before* spending a TwelveData call on it.

Anything here must be provable from `rise3`/`rise5`/`volx3`/`volx5`,
`from_high`, `vwap`, `cum_volume` and `minutes_since_low`. If a detector needs
bar shape or news, it belongs in one of the enriched modules instead.
"""

from __future__ import annotations

from .base import Bundle, StrategyResult, clamp, failed, insufficient, passed
from .news_cause import rank_catalysts


def _hours(b: Bundle) -> float:
    """ET hour as a float, for the time-of-day rules."""
    if b.now_et is None:
        return 12.0
    return b.now_et.hour + b.now_et.minute / 60.0


def dollar_breakout(b: Bundle) -> StrategyResult:
    """Reclaiming a whole dollar level ($1.00 by default).

    The sub-$1 name has a wide, well-watched level, so holding it tends to draw
    more flow than an arbitrary round number. Enriched: needs the open and
    today's volume to confirm the reclaim is holding, not a wick.
    """
    level = b.dollar_level
    price = b.price
    prev = b.prev_close or 0.0
    if prev >= level and not (b.open_price or 0.0) < level:
        return failed("dollar_breakout", [],
                      [f"prior close {prev:.2f} not < {level:.2f}"],
                      {"prev_close": prev, "level": level})
    if price < level:
        return failed("dollar_breakout", [],
                      [f"price {price:.4f} below {level:.2f}"],
                      {"price": price, "level": level})

    score = 35.0 + min(25.0, abs(b.pct)) + min(20.0, (b.volx_of(5) - 4.0) * 5.0)
    if b.float_shares and b.float_shares <= 20_000_000:
        score += 10
    reasons = [f"holding above {level:.2f}", f"{b.rise_of(5) * 100:+.1f}% in 5m",
               f"volx {b.volx_of(5):.1f}"]
    if b.above_vwap:
        reasons.append("above VWAP")
    return passed("dollar_breakout", score, reasons,
                  {"price": price, "level": level, "volx5": b.volx_of(5)})


def catalyst_volume(b: Bundle) -> StrategyResult:
    """Catalyst + volume. The mandatory consumer: other detectors read its output.

    A catalyst with no volume behind it is a spread, not a setup, so the volume
    confirmation is a hard condition rather than a score bonus.
    """
    if b.news is None:
        return insufficient("catalyst_volume", "news not fetched")
    best, ranked, dilution = rank_catalysts(b)
    if dilution:
        from .base import blocked_by_dilution
        return blocked_by_dilution("catalyst_volume", dilution)

    volx = max(b.volx_of(3), b.volx_of(5), b.volx_of(15))
    dollars = max(b.dollars_of(3), b.dollars_of(5), b.dollars_of(15))
    if volx < 3.0 or dollars < 150_000:
        return failed("catalyst_volume", [],
                      [f"volx {volx:.1f}x < 3.0", f"15m dollars ${dollars:,.0f} < $150,000"],
                      {"volx": volx, "dollars": dollars, "catalysts": len(ranked)})
    if not ranked:
        return failed("catalyst_volume", [], ["no catalyst in the last 24h"],
                      {"volx": volx, "catalysts": 0})

    top = ranked[0]
    # top["weight"] is already the decayed weight (news_cause.effective_weight),
    # so this score is on how much pull the catalyst has left, not on its class.
    score = 20.0 + top["weight"] * 40.0 + min(20.0, (volx - 3.0) * 4.0)
    if top["age_minutes"] <= 120:
        score += 20
    return passed("catalyst_volume", score,
                  [top["headline"][:80],
                   f"{top['class'].lower()} catalyst, effective weight {top['weight']:.2f}",
                   f"volx {volx:.1f}"],
                  {"top": top, "volx": volx, "n_catalysts": len(ranked)})


def low_float_squeeze(b: Bundle) -> StrategyResult:
    """Thin float, crowded short, hard catalyst. Momentum, not thesis."""
    if b.float_shares is None:
        return insufficient("low_float_squeeze", "float unknown")
    if b.float_shares > 20_000_000:
        return failed("low_float_squeeze", [],
                      [f"float {b.float_shares / 1e6:.1f}M > 20M"],
                      {"float": b.float_shares})
    if b.short_pct is None and b.short_ratio is None:
        return insufficient("low_float_squeeze", "no short data")

    crowded = (b.short_pct or 0.0) >= 15.0 or (b.short_ratio or 0.0) >= 3.0
    if not crowded:
        return failed("low_float_squeeze", [],
                      [f"short float {b.short_pct or 0:.1f}% < 15%",
                       f"short ratio {b.short_ratio or 0:.2f} < 3.0"],
                      {"short_pct": b.short_pct, "short_ratio": b.short_ratio})
    if b.volx_of(5) < 5.0:
        return failed("low_float_squeeze", [], [f"volx {b.volx_of(5):.1f} < 5.0"],
                      {"volx5": b.volx_of(5)})
    if b.pct < 10.0:
        return failed("low_float_squeeze", [], [f"move {b.pct:+.1f}% < +10%"],
                      {"pct": b.pct})
    if b.news is None:
        return insufficient("low_float_squeeze", "news not fetched")
    # A thin float and a crowded short explain why a stock *can* move; without a
    # catalyst it is usually just a stock that already ran. The squeeze needs a
    # reason, not only the fuel.
    _, catalysts, _ = rank_catalysts(b)
    if not catalysts:
        return failed("low_float_squeeze", [], ["no catalyst in the last 24h"],
                      {"short_pct": b.short_pct, "float": b.float_shares})

    score = 30.0 + (20_000_000 - b.float_shares) / 20_000_000 * 20.0
    score += min(25.0, b.short_pct or 0.0) + min(15.0, (b.volx_of(5) - 5.0) * 2.0)
    return passed("low_float_squeeze", score,
                  [f"float {b.float_shares / 1e6:.1f}M",
                   f"short float {b.short_pct or 0:.1f}%",
                   f"volx {b.volx_of(5):.1f}"],
                  {"float": b.float_shares, "short_pct": b.short_pct,
                   "volx5": b.volx_of(5)})


def float_rotation(b: Bundle) -> StrategyResult:
    """Volume has already turned the float over 1.5 times -- the squeeze is running."""
    if b.float_shares is None:
        return insufficient("float_rotation", "float unknown")
    if b.float_shares > 20_000_000:
        return failed("float_rotation", [], [f"float {b.float_shares / 1e6:.1f}M > 20M"],
                      {"float": b.float_shares})
    rotation = b.cum_volume / b.float_shares if b.float_shares else 0.0
    if rotation < 1.5:
        return failed("float_rotation", [], [f"rotation {rotation:.2f}x < 1.5x"],
                      {"rotation": rotation})
    if not b.above_vwap:
        return failed("float_rotation", [], ["price below VWAP"], {"rotation": rotation})

    score = 40.0 + min(30.0, (rotation - 1.5) * 20.0)
    if rotation >= 3.0:
        score += 15
    return passed("float_rotation", score,
                  [f"rotation {rotation:.2f}x float", "price above VWAP",
                   f"volx {b.volx_of(5):.1f}"],
                  {"rotation": rotation, "float": b.float_shares})


def vwap_reclaim(b: Bundle) -> StrategyResult:
    """Back below VWAP for a while, then reclaimed on volume.

    Metrics-only: no "minutes spent below VWAP" counter exists, so this uses the
    bar series when available. Without bars it reports INSUFFICIENT_DATA rather
    than substituting time-since-low, which measures a different thing entirely
    and would pass every stock that merely bounced off its low.
    """
    if b.vwap is None:
        return insufficient("vwap_reclaim", "no VWAP yet")
    if b.bars_1m is None:
        return insufficient("vwap_reclaim", "candles not fetched")
    if not b.above_vwap:
        return failed("vwap_reclaim", [], ["price is below VWAP, nothing reclaimed"], {})

    from .candles import clean_bars, minutes_below
    bars = clean_bars(b.bars_1m)
    below = minutes_below(bars, b.vwap, before_last=True)
    if below < 10:
        return failed("vwap_reclaim", [], [f"only {below}m below VWAP, need 10"],
                      {"below_minutes": below})
    volx = b.volx_of(3) or b.volx_of(5)
    if volx < 2.0:
        return failed("vwap_reclaim", [], [f"reclaim volx {volx:.1f} < 2.0"],
                      {"volx": volx})

    score = 35.0 + min(25.0, below * 1.5) + min(20.0, volx * 5.0)
    reasons = [f"was {below}m below VWAP", f"reclaimed on volx {volx:.1f}"]
    if b.prev_close and b.price > b.prev_close:
        score += 20
        reasons.append("above prior close")
    return passed("vwap_reclaim", score, reasons,
                  {"below_minutes": below, "volx": volx})


def failed_breakout(b: Bundle) -> StrategyResult:
    """Broke a level, got pulled back through it, reclaimed it on volume.

    The high-edge setup in this library: the failed break traps breakout buyers
    and hands the level back as fuel, and the stop is unambiguous because it
    sits under the washout low.
    """
    if b.bars_1m is None:
        return insufficient("failed_breakout", "candles not fetched")
    from .detectors import evaluate_failed_breakout
    return evaluate_failed_breakout(b)


def first_green_day(b: Bundle) -> StrategyResult:
    """Flat or red for a week, today is the first big green day."""
    if len(b.daily_history) < 6:
        return insufficient("first_green_day",
                            f"only {len(b.daily_history)} prior sessions recorded, need 6")
    return first_green_day_from_history(b)


def first_green_day_from_history(b: Bundle) -> StrategyResult:
    lookback = 5
    hist = b.daily_history[-6:]
    closes = [h["close"] for h in hist if h.get("close")]
    if len(closes) < 6:
        return insufficient("first_green_day", "incomplete daily history")
    prior5 = closes[-5:]
    prev_day = closes[-6]
    net = sum((c / prev_day - 1.0) for c in prior5) if prev_day else 0.0
    best = max(prior5)
    if net > 0 and best >= prev_day * 1.05:
        return failed("first_green_day", [],
                      [f"prior 5d net {net * 100:+.1f}% (not red/flat)"],
                      {"net5": net})
    if b.pct < 15.0:
        return failed("first_green_day", [], [f"move {b.pct:+.1f}% < +15%"], {"pct": b.pct})
    volx = max(b.volx_of(3), b.volx_of(5), b.volx_of(15))
    if volx < 3.0:
        return failed("first_green_day", [], [f"volx {volx:.1f} < 3.0"], {"volx": volx})
    if not b.above_vwap:
        return failed("first_green_day", [], ["price below VWAP"], {})
    if (b.minutes_since_low or 0.0) < 30.0:
        return failed("first_green_day", [],
                      [f"low set {b.minutes_since_low or 0:.0f}m ago, need 30m"],
                      {"minutes_since_low": b.minutes_since_low})

    score = 40.0 + min(30.0, b.pct) + min(20.0, (volx - 3.0) * 4.0)
    return passed("first_green_day", score,
                  [f"prior 5d net {net * 100:+.1f}%", f"first +{b.pct:.0f}% day",
                   f"low set {b.minutes_since_low:.0f}m ago"],
                  {"net5": net, "pct": b.pct, "volx": volx})
