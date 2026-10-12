"""Enriched detectors: those that need 1m/5m candle shape.

Everything here is gated on `bundle.bars_1m`, which only exists for symbols that
have been through `Enricher.enrich()`. That is the cost control -- the engine
polls every symbol every cycle, so bar-based detection happens after a symbol has
already earned the fetch by clearing the cheap gate.
"""

from __future__ import annotations

from typing import Optional

from .base import Bundle, StrategyResult, clamp, failed, insufficient, passed, triggered
from .candles import (FIVE_MIN, aggregate, avg_volume, clean_bars, held_above, is_green,
                      lower_highs, minutes_below, sweep_level)


def _bars(b: Bundle, minutes: int = FIVE_MIN) -> list[dict]:
    return aggregate(clean_bars(b.bars_1m), minutes)


def _levels(b: Bundle) -> list[tuple[str, float]]:
    """Breakout levels worth watching, in the order a trader would check them."""
    out: list[tuple[str, float]] = []
    if b.day_high:
        out.append(("prior high", b.day_high))
    if b.vwap is not None:
        out.append(("VWAP", b.vwap))
    if b.prev_close:
        out.append(("prior close", b.prev_close))
    return out


def evaluate_failed_breakout(b: Bundle) -> StrategyResult:
    """A 5m close above L, a close back below it, then a reclaim on 2x volume.

    Requires: broke L on a 5m close -> failed back below -> a green 5m close
    above L on at least 2x the breakdown candle's volume -> still holding.
    """
    bars = _bars(b)
    if len(bars) < 6:
        return insufficient("failed_breakout", f"only {len(bars)} 5m bars")
    if b.volx_of(5) < 2.0:
        return failed("failed_breakout", [], [f"session volx {b.volx_of(5):.1f} < 2.0"],
                      {"volx5": b.volx_of(5)})

    best = None
    for name, level in _levels(b):
        # Four consecutive bars: break, fail, washout, reclaim. The last valid
        # start is therefore len(bars) - 4, and the earliest is 1 -- a level can
        # be broken on the second bar of the series, which is exactly what a
        # stock that gaps up and immediately fails its own open looks like.
        for i in range(1, max(1, len(bars) - 3)):
            brk, fail, wash, recl = bars[i], bars[i + 1], bars[i + 2], bars[i + 3]
            if not (brk["c"] >= level and brk["o"] <= level):
                continue                       # C1: traded up through the level
                                              #     (>=, not >: a break that closes
                                              #     exactly on the level still
                                              #     trapped the breakout buyers)
            if not fail["c"] < level:
                continue                       # C2: it failed within a candle or two
            if wash["l"] > level * 0.995:
                continue                       # C3: a real washout, not a tick
            if not (recl["c"] > level and is_green(recl)):
                continue                       # C4: reclaimed on a green close
            if brk["v"] <= 0 or recl["v"] < 2.0 * brk["v"]:
                continue                       # C5: the reclaim is the bigger candle
            cand = (name, level, i, brk["v"], recl["v"])
            if best is None or cand[3] < best[3]:
                best = cand
            break

    if best is None:
        return failed("failed_breakout", ["no 5m break-fail-reclaim sequence"], [],
                      {"levels": [n for n, _ in _levels(b)]})

    name, level, idx, brk_v, recl_v = best
    ratio = recl_v / brk_v if brk_v else 0.0
    score = 45.0 + min(30.0, (ratio - 2.0) * 12.0)
    return passed("failed_breakout", score,
                  [f"5m broke {level:.4f} ({name})", f"reclaimed on {ratio:.1f}x vol",
                   f"washout {level * 0.995:.4f}"],
                  {"level": level, "level_name": name, "reclaim_volx": round(ratio, 2),
                   "index": idx})


def red_to_green(b: Bundle) -> StrategyResult:
    """Opened red, washed out in the morning, reclaimed the prior close on volume."""
    prev = b.prev_close
    if prev is None or b.open_price is None:
        return insufficient("red_to_green", "no open/prior close")
    if not (b.open_price < prev):
        return failed("red_to_green", [],
                      [f"opened {b.open_price:.4f} not below prior close {prev:.4f}"],
                      {"open": b.open_price, "prev_close": prev})
    if not b.price > prev:
        return failed("red_to_green", [], [f"price {b.price:.4f} not above {prev:.4f}"],
                      {"price": b.price, "prev_close": prev})
    if not b.above_vwap:
        return failed("red_to_green", [], ["price below VWAP at the cross"], {})

    volx = max(b.volx_of(3), b.volx_of(5))
    if volx < 2.0:
        return failed("red_to_green", [], [f"cross volx {volx:.1f} < 2.0"], {"volx": volx})
    if (b.minutes_since_low or 0.0) < 15.0:
        return failed("red_to_green", [],
                      [f"morning low only {b.minutes_since_low or 0:.0f}m ago"],
                      {"minutes_since_low": b.minutes_since_low})

    wash = (prev - (b.day_low or prev)) / prev * 100 if prev else 0.0
    score = 30.0 + min(25.0, wash) + min(20.0, volx * 5.0)
    if b.above_vwap:
        score += 15
    return passed("red_to_green", score,
                  [f"opened red at {b.open_price:.4f}", f"reclaimed {prev:.4f}",
                   f"washout {wash:.1f}%", f"volx {volx:.1f}"],
                  {"prev_close": prev, "open": b.open_price, "volx": volx, "washout": wash})


def worg(b: Bundle) -> StrategyResult:
    """Weak Open Red-to-Green: strong recent closes, a fresh multi-week high,
    opened red, reclaimed the prior close above VWAP on volume."""
    hist = b.daily_history
    if len(hist) < 6:
        return insufficient("worg", f"only {len(hist)} prior sessions, need 6")
    strength, multiweek = worg_context(hist)
    if strength < 0.90:
        return failed("worg", [],
                      [f"prior close {strength * 100:.0f}% of recent high, need 90%"],
                      {"strength": strength})
    if not multiweek:
        return failed("worg", [], ["no fresh multi-week high in the last 5 sessions"],
                      {"strength": strength})
    r2g = red_to_green(b)
    if r2g.verdict != "PASS":
        # WORG is red_to_green plus context. If the reversal is not there, WORG is
        # not either, and the reason is the reversal's reason.
        return failed("worg", [], [f"red-to-green: {c}" for c in r2g.failed_on] or ["not reversed"],
                      {"strength": strength})

    wash = ((b.prev_close or 0) - (b.day_low or b.prev_close or 0)) / max(b.prev_close or 1, 1e-9) * 100
    score = 35.0 + min(25.0, wash) + min(20.0, r2g.score * 0.5)
    if strength >= 0.95:
        score += 20
    else:
        score += 20
    return passed("worg", score,
                  [f"prior close {strength * 100:.0f}% of recent high",
                   "fresh multi-week breakout", f"reclaimed {b.prev_close:.4f} on volume"],
                  {"strength": strength, "multiweek": multiweek})


def worg_context(hist: list[dict]) -> tuple[float, bool]:
    """(prior close strength vs recent high, fresh multi-week breakout flag)."""
    if len(hist) < 6:
        return 0.0, False
    recent_high = max(h.get("high") or h.get("close") or 0.0 for h in hist[-5:])
    prior_close = hist[-1].get("close") or 0.0
    if not recent_high or not prior_close:
        return 0.0, False
    strength = prior_close / recent_high
    # A multi-week high: the most recent close is the highest of the last 5.
    closes = [h.get("close") or 0.0 for h in hist[-5:]]
    multiweek = closes[-1] >= max(closes) if closes else False
    return strength, multiweek


def bull_flag(b: Bundle) -> StrategyResult:
    """Sharp pole, a tight consolidation on declining volume, then a breakout."""
    bars = _bars(b)
    if len(bars) < 8:
        return insufficient("bull_flag", f"only {len(bars)} 5m bars")

    for i in range(len(bars) - 2, 3, -1):
        pole = bars[:i]
        pole_pct = (pole[-1]["c"] / pole[0]["o"] - 1.0) * 100 if pole else 0.0
        if pole_pct < 15.0:
            continue
        cons = bars[i:i + 4]
        if len(cons) < 3:
            continue
        if not all(c["l"] >= (b.vwap or 0) for c in cons):
            continue                           # held VWAP throughout
        flag_high = max(c["h"] for c in cons)
        brk = bars[i + len(cons)]
        if not brk["c"] > flag_high:
            continue
        if avg_volume(cons) <= 0 or brk["v"] < 2.0 * avg_volume(cons):
            continue
        score = 35.0 + min(20.0, pole_pct) + min(20.0, (brk["v"] / max(avg_volume(cons), 1) - 2) * 5)
        if b.float_shares and b.float_shares <= 15_000_000:
            score += 10
        return passed("bull_flag", score,
                      [f"pole +{pole_pct:.0f}%", f"consolidated {len(cons)} bars",
                       f"broke {flag_high:.4f} on {brk['v'] / max(avg_volume(cons), 1):.1f}x vol"],
                      {"pole_pct": pole_pct, "flag_high": flag_high})
    return failed("bull_flag", [], ["no clean pole + consolidation + breakout"], {})


def day3_surge(b: Bundle) -> StrategyResult:
    """Day-1 spike, day-2 held it, day-3 retesting the day-1 high."""
    hist = b.daily_history
    if len(hist) < 2:
        return insufficient("day3_surge", f"only {len(hist)} prior sessions, need 2")
    d1, d2 = hist[-2], hist[-1]
    d1_pct = (d1.get("close") or 0) / (d1.get("open") or 1) * 100 - 100 if d1.get("open") else 0
    if d1_pct < 20.0:
        return failed("day3_surge", [], [f"day-1 move {d1_pct:+.0f}% < +20%"],
                      {"day1_pct": d1_pct})
    if not ((d2.get("low") or 0) >= (d1.get("low") or 0)):
        return failed("day3_surge", [], ["day-2 made a lower low than day-1"], {})
    if d1.get("volume") and d2.get("volume") and d2["volume"] < 0.4 * d1["volume"]:
        return failed("day3_surge", [],
                      [f"day-2 volume {d2['volume'] / max(d1['volume'], 1) * 100:.0f}% of day-1"],
                      {})
    if not b.day_high or not (b.price >= (d1.get("high") or 1e9) * 0.98):
        return failed("day3_surge", [], ["not retesting the day-1 high"], {})

    score = 40.0 + (20 if b.above_vwap else 0)
    return passed("day3_surge", score,
                  [f"day-1 +{d1_pct:.0f}%", "day-2 held", f"retesting {d1.get('high', 0):.2f}"],
                  {"day1_pct": d1_pct})


# -- mechanism detectors (Spec 2.0 A): fire when NO cause is named -----------

MECHANISM_NAMES = ("silent_accumulation", "sympathy_move", "short_squeeze_mechanics")

# The same ceiling the reality gate uses; a detector reads only its Bundle, so
# the spread ceiling is baked rather than plumbed through evaluate_gate twice.
MECHANISM_SPREAD_MAX_PCT = 1.5


def _no_named_cause(b: Bundle) -> Optional[bool]:
    """Is there a named cause in the news? True/False when news was fetched.

    A mechanism fires *because* no cause was named -- so a report the detectors
    cannot read (news=None) is unknown, not absent, and the mechanism stays
    silent rather than claiming the move is unexplained.
    """
    if b.news is None:
        return None
    from .news_cause import rank_catalysts, extract_cause, UNEXPLAINED
    try:
        _, ranked, _ = rank_catalysts(b)
        return extract_cause(ranked, b).kind == UNEXPLAINED
    except Exception:  # noqa: BLE001
        return None


def silent_accumulation(b: Bundle) -> StrategyResult:
    """The tape explains the move without a headline (Spec 2.0 A).

    Rotation that keeps accelerating while price holds above VWAP is someone
    working an order, and a press-release-only filter suppresses exactly that
    forever. Fires only when no cause was named -- with a headline on the tape
    this is catalyst_volume's territory, and a mechanism must not outscore a
    named event.

    "Rotation rising" is read as acceleration: the 5m window moving more volume
    than the 15m window means the float is turning over faster now than it was
    across the hour's run.
    """
    if b.news is None:
        return insufficient("silent_accumulation", "news not fetched")
    if b.float_shares is None or b.float_shares <= 0 or not b.cum_volume:
        return insufficient("silent_accumulation", "float unknown")
    if _no_named_cause(b) is False:
        return failed("silent_accumulation", [], ["a cause is named"], {})
    if not b.above_vwap:
        return failed("silent_accumulation", [], ["price below VWAP"], {})
    volx5 = b.volx_of(5)
    if volx5 < 3.0:
        return failed("silent_accumulation", [], [f"volx {volx5:.1f}x < 3.0"],
                      {"volx5": volx5})
    rot = b.cum_volume / b.float_shares
    # Rotation rate rising: the newest 15m added more rotation than the window
    # before it. dollar_vol_15 / price is the share count of the last 15m, so
    # the rotation added recently is that over the float.
    vol15_shares = (b.dollars_of(15) / b.price) if b.price else 0.0
    rot_15m_ago = max(0.0, rot - (vol15_shares / b.float_shares))
    if rot <= rot_15m_ago:
        return failed("silent_accumulation", [],
                      [f"rotation flat or falling ({rot:.2f}x vs {rot_15m_ago:.2f}x)"],
                      {"rotation": rot})
    score = 40.0 + min(25.0, (volx5 - 3.0) * 4.0) + min(15.0, rot * 15.0)
    return passed("silent_accumulation", score,
                  [f"float rotation {rot:.2f}x, rising",
                   "no news in 24h", f"volx {volx5:.1f}x", "above VWAP"],
                  {"rotation": rot, "volx5": volx5})


def sympathy_move(b: Bundle) -> StrategyResult:
    """A sector peer is running and this name is lagging behind it (2.0 A).

    Fires without a named cause of its own: the peer's move *is* the context
    the press-release filter cannot see. Needs peer moves, which the
    enrichment carries when a peer feed is wired; without it this honestly
    reports INSUFFICIENT_DATA.
    """
    peers = b.sector_peers
    if peers is None:
        return insufficient("sympathy_move", "peer moves not fetched")
    if b.news is None:
        return insufficient("sympathy_move", "news not fetched")
    if _no_named_cause(b) is False:
        return failed("sympathy_move", [], ["a cause is named"], {})
    target15 = (b.rise_of(15) or 0.0) * 100.0
    up = sorted((p for p in peers.values() if p is not None), reverse=True)
    if not up:
        return failed("sympathy_move", [], ["no sector peer moved"], {})
    if not (up and max(up) >= 2.0 * max(target15, 1.0)):
        return failed("sympathy_move", [],
                      [f"best peer {max(up or [0]):+.1f}% not >= 2x the target's "
                       f"{target15:+.1f}% 15m move"], {})
    if (sum(up) / len(up)) <= 0:
        return failed("sympathy_move", [], ["sector median not positive"], {})
    score = 35.0 + min(20.0, max(up) - target15) + min(15.0, b.volx_of(5) * 3.0)
    return passed("sympathy_move", score,
                  [f"peer {max(up):+.1f}% vs +{target15:.1f}% 15m",
                   f"sector median {sum(up) / len(up):+.1f}%"],
                  {"peer_pct": max(up), "target_15m": target15})


def short_squeeze_mechanics(b: Bundle) -> StrategyResult:
    """Crowded short, heavy volume, no headline: the squeeze IS the cause (2.0 A).

    Distinct from low_float_squeeze, which needs a thin float and a big move:
    this fires on the crowd and the tape alone, before the float numbers
    arrive, because a mechanical squeeze has no press release to wait for.
    """
    if b.news is None:
        return insufficient("short_squeeze_mechanics", "news not fetched")
    if _no_named_cause(b) is False:
        return failed("short_squeeze_mechanics", [], ["a cause is named"], {})
    if b.short_pct is None:
        return insufficient("short_squeeze_mechanics", "no short data")
    floor = getattr(b, "short_float_min", 8.0)
    if b.short_pct < floor:
        return failed("short_squeeze_mechanics", [],
                      [f"short float {b.short_pct:.1f}% < {floor:.0f}%"],
                      {"short_pct": b.short_pct})
    volx = max(b.volx_of(5), b.volx_of(15))
    if volx < 5.0:
        return failed("short_squeeze_mechanics", [],
                      [f"volx {volx:.1f}x < 5.0"], {"volx5": volx})
    if b.spread_pct is None:
        return insufficient("short_squeeze_mechanics", "spread unknown")
    if b.spread_pct > MECHANISM_SPREAD_MAX_PCT:
        return failed("short_squeeze_mechanics", [],
                      [f"spread {b.spread_pct:.1f}% > {MECHANISM_SPREAD_MAX_PCT}%"],
                      {"spread_pct": b.spread_pct})
    score = 40.0 + min(25.0, b.short_pct) + min(15.0, (volx - 5.0) * 2.0)
    return passed("short_squeeze_mechanics", score,
                  [f"short float {b.short_pct:.1f}%", f"no headline, volx {volx:.1f}x",
                   f"spread {b.spread_pct:.1f}%"],
                  {"short_pct": b.short_pct, "volx5": volx})


def sympathy_play(b: Bundle) -> StrategyResult:
    """A sector leader ran on a hard catalyst; this is the cheaper, lagging peer."""
    if not b.sector:
        return insufficient("sympathy_play", "no sector")
    if b.pct < 5.0 or b.pct >= 15.0:
        return failed("sympathy_play", [],
                      [f"move {b.pct:+.1f}% not in the 5-15% sympathy band"],
                      {"pct": b.pct})
    if b.float_shares is None or b.float_shares > 30_000_000:
        return failed("sympathy_play", [], ["float missing or > 30M"], {})
    if b.volx_of(5) < 2.0:
        return failed("sympathy_play", [], [f"volx {b.volx_of(5):.1f} < 2.0"], {})
    leader = getattr(b, "sector_leader", None)
    if leader is None:
        return insufficient("sympathy_play", "no sector leader mapped today")
    if leader["pct"] < 15.0:
        return failed("sympathy_play", [],
                      [f"sector leader +{leader['pct']:.0f}% < +15%"], leader)
    score = 30.0 + (15 if b.pct < leader["pct"] else 0) + min(15.0, (b.volx_of(5) - 2) * 5)
    return passed("sympathy_play", score,
                  [f"{leader['symbol']} +{leader['pct']:.0f}% on a hard catalyst",
                   f"peer +{b.pct:.0f}% in {b.sector}", f"float {b.float_shares / 1e6:.1f}M"],
                  {"leader": leader})


# --- triggers: they upgrade a primary, never pass alone ---------------------

def first_pullback(b: Bundle) -> StrategyResult:
    """Pulled back to VWAP on lower volume, reclaimed on higher volume."""
    if b.vwap is None:
        return insufficient("first_pullback", "no VWAP")
    if b.above_vwap:
        dist = (b.price - b.vwap) / b.vwap if b.vwap else 1.0
        if dist > 0.005:
            return failed("first_pullback", [], [f"price {dist * 100:.1f}% above VWAP"], {})
    pull = (b.vwap - b.day_low) / b.vwap if b.vwap and b.day_low else 0.0
    volx = b.volx_of(3)
    if volx < 1.5:
        return failed("first_pullback", [], [f"reclaim volx {volx:.1f} < 1.5"], {"volx": volx})
    score = 40.0 + (20 if b.above_vwap else 0)
    if pull < 0.5:
        score += 20
    return triggered("first_pullback", score,
                     [f"pullback {pull * 100:.1f}% to VWAP", f"reclaimed on volx {volx:.1f}"],
                     {"pullback": pull, "volx": volx})


def mss_break(b: Bundle) -> StrategyResult:
    """A market structure shift: broke the most recent swing high on volume."""
    bars = _bars(b)
    if len(bars) < 6:
        return insufficient("mss_break", f"only {len(bars)} 5m bars")
    look = bars[:-1]
    swing = max(c["h"] for c in look[-8:]) if look else 0.0
    last = bars[-1]
    if not (last["c"] > swing):
        return failed("mss_break", [], [f"no break of {swing:.4f}"], {"swing": swing})
    volx = last["v"] / max(avg_volume(bars[:-1], 5), 1)
    if volx < 1.5:
        return failed("mss_break", [], [f"break volx {volx:.1f} < 1.5"], {"volx": volx})
    score = 40.0 + min(30.0, (volx - 1.5) * 15)
    if b.above_vwap:
        score += 15
    return triggered("mss_break", score,
                     [f"broke swing high {swing:.4f}", f"on {volx:.1f}x vol"],
                     {"swing": swing, "volx": volx})


def liquidity_grab(b: Bundle) -> StrategyResult:
    """Wicked under a recent low and closed back above it."""
    bars = _bars(b)
    if len(bars) < 6:
        return insufficient("liquidity_grab", f"only {len(bars)} 5m bars")
    prior = bars[:-1]
    swing_low = min(c["l"] for c in prior[-8:]) if prior else 0.0
    last = bars[-1]
    if not (last["l"] < swing_low and last["c"] > swing_low):
        return failed("liquidity_grab", [], [f"no sweep of {swing_low:.4f}"],
                      {"swing_low": swing_low})
    volx = last["v"] / max(avg_volume(prior, 5), 1)
    if volx < 1.5:
        return failed("liquidity_grab", [], [f"grab volx {volx:.1f} < 1.5"], {"volx": volx})
    score = 40.0 + min(30.0, volx * 15)
    if is_green(last):
        score += 10
    return triggered("liquidity_grab", score,
                     [f"swept {swing_low:.4f}", f"closed back above on {volx:.1f}x vol"],
                     {"swing_low": swing_low, "volx": volx})


def v_reversal(b: Bundle) -> StrategyResult:
    """Dead-cat bounce: dropped hard off the high, now reclaiming."""
    if b.from_high is None:
        return insufficient("v_reversal", "no session high")
    drop = -b.from_high * 100
    if drop < 6.0:
        return failed("v_reversal", [], [f"drop {drop:.1f}% < 6%"], {"drop": drop})
    volx = b.volx_of(3)
    if volx < 1.5:
        return failed("v_reversal", [], [f"reclaim volx {volx:.1f} < 1.5"], {"volx": volx})
    score = 35.0 + min(25.0, drop * 2.5) + min(20.0, volx * 10)
    if drop >= 10.0:
        score += 20
    return triggered("v_reversal", score, [f"dropped {drop:.0f}% off the high",
                                           f"reclaiming on volx {volx:.1f}"],
                     {"drop": drop, "volx": volx})


def exhaustion_blocker(b: Bundle) -> StrategyResult:
    """Refuse a new long when a move is already spent.

    This is the long-only safety blocker. It replaces any 'short signal': the
    answer to an exhausted move is 'do not buy', never 'sell'.
    """
    if b.day_high is None or b.price is None or not b.day_high:
        return insufficient("exhaustion_block", "no session high")
    near = b.price >= b.day_high * 0.95
    if not near:
        return failed("exhaustion_block", [], ["price not near the session high"], {})
    volx = max(b.volx_of(3), b.volx_of(5))
    declining = volx < 3.0
    if not (near and declining):
        return failed("exhaustion_block", [], [f"volume still confirming (volx {volx:.1f})"],
                      {"volx": volx})
    score = clamp(b.minutes_since_low * 2 + (1.0 - min(volx / 5.0, 1.0)) * 50)
    from .base import blocked
    return blocked("exhaustion_block",
                   [f"near the session high with volx {volx:.1f} - move is spent"],
                   {"volx": volx, "from_high": b.from_high})