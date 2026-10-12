"""Invalidation: watch every alerted setup after the alert, and abort it once.

An alert is a statement about a window of time. It was true at 10:58 and it
stops being true the moment the 5m close goes under the VWAP the setup was
bought against, or the spread blows out, or the company files something that
changes the story. Without this the reader is left holding a message that used
to be advice and is now history.

Six triggers, first one wins, and exactly one message per alert:

    T1  5m close below the VWAP reference captured at alert time
    T2  price below the low of the 1m candle at the alert
    T3  the symbol halted
    T4  spread above SPREAD_MAX_PCT for INVALIDATION_SPREAD_CONSEC_MIN minutes
    T5  a post-alert NEGATIVE catalyst (unpriced offering, reverse split, going concern)
    T6  within 5% of the session high for 20+ minutes on declining 5m volume

Ordering matters and is deliberate: T1 and T2 are price facts available on every
cycle, so they are checked first and cost nothing. T5 reads the news the
enricher already fetched for the alert, so it costs nothing extra either. T3 and
T4 come last because they need state this module keeps.

One abort, ever. If the setup later re-validates, that is a fresh alert from the
normal path and a new tracker row -- never a "resume" of the dead one, because a
resume message tells the reader a trade is live when it is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .util import money, pct, to_et

log = logging.getLogger(__name__)

REASON_RUBRIC = {
    "vwap": "rubric R1 clause",
    "alert_low": "rubric R1 clause",
    "halt": "rubric R3 clause",
    "spread": "rubric R2 clause",
    "catalyst": "rubric R4 clause",
    "exhausted": "rubric R1 clause",
}


@dataclass
class InvalidationState:
    """Per-alert memory. The alert_time_low and vwap reference never change."""

    symbol: str
    day: str
    alert_ts: float
    alert_price: float
    alert_time_low: Optional[float] = None
    vwap_ref: Optional[float] = None
    wide_since: float = 0.0
    near_high_minutes: float = 0.0
    fired: bool = False
    notes: list = field(default_factory=list)
    # Rolling 5m volume samples, oldest first. T6 needs to compare the current
    # five minutes against the one before it, and the polls are the only place
    # that comparison can be made without refetching candles.
    volumes: list = field(default_factory=list)
    # Headlines already accounted for, so T5 fires once and only once per item.
    seen_news: list = field(default_factory=list)
    # The dilutive headline T5 found, held so the abort is not re-detected on a
    # later poll after the watcher is torn down.
    pending: str = ""
    news_ts: float = 0.0
    # Rolling 5m bookkeeping, filled in by the engine's poll loop. last_cum is the
    # cumulative volume at the previous poll, so the difference is what traded in
    # the interval; bucket_vol accumulates that into a whole 5m bar.
    last_cum: float = 0.0
    bucket: int = 0
    bucket_vol: float = 0.0
    bucket_close: Optional[float] = None
    bar_count: int = 0


def check(state: InvalidationState, *, price: Optional[float], now: float,
          vwap: Optional[float] = None, spread_pct: Optional[float] = None,
          halted: bool = False, negative_catalyst: str = "",
          day_high: Optional[float] = None, five_min_volume: Optional[float] = None,
          prior_five_min_volume: Optional[float] = None,
          spread_max_pct: float = 1.5, spread_consec_min: int = 2,
          near_high_minutes: int = 20, min_gap_pct: float = 5.0,
          five_min_close_below_vwap: bool = False) -> Optional[tuple]:
    """Evaluate the six triggers. Returns (reason_key, message) or None.

    Each trigger is a hard fact about price, quote state or news. None of them
    can produce a short instruction: the outcome is always "do not enter / exit
    if holding", which is the only long-only-safe action when a setup dies.
    """
    if state.fired or price is None:
        return None

    # T1: a 5m close below the VWAP the setup was bought against. Checked
    # before T2 because a closed 5m bar is a stronger statement than a print.
    ref = vwap if vwap is not None else state.vwap_ref
    if five_min_close_below_vwap and ref is not None and price < ref:
        state.fired = True
        return ("vwap", "5m close below VWAP %.4f" % ref)

    # T2: below the low of the candle the alert fired on. That low is the
    # structure's floor; under it, the setup is not a setup.
    if state.alert_time_low is not None and price < state.alert_time_low:
        state.fired = True
        return ("alert_low", "price %.2f below the %.2f low at alert"
                % (price, state.alert_time_low))

    # T3: a halt. The reader cannot act on a stock that is not trading, and the
    # abort is the last message that reaches them before they assume it still is.
    if halted:
        state.fired = True
        return ("halt", "trading halted")

    # T4: a wide spread that persists. One poll of noise is not a fact; the
    # consecutive-minute requirement is what makes this a condition rather than
    # a glitch.
    if spread_pct is not None and spread_pct > spread_max_pct:
        if not state.wide_since:
            state.wide_since = now
        elif now - state.wide_since >= spread_consec_min * 60.0:
            state.fired = True
            return ("spread", "spread %.2f%% for %d minutes" % (spread_pct, spread_consec_min))
    else:
        state.wide_since = 0.0

    # T5: the story changed after the alert. Named explicitly because "the
    # company did something" is not actionable without saying what.
    if negative_catalyst:
        state.fired = True
        return ("catalyst", negative_catalyst)

    # T6: the exhaustion pattern the detector cannot see after the fact, because
    # by then the gate is no longer re-run for a stock that is already tracked.
    near = False
    if day_high and price and float(day_high) > 0:
        near = price >= float(day_high) * (1.0 - min_gap_pct / 100.0)
    if near and (five_min_volume or 0.0) > 0 and (prior_five_min_volume or 0.0) > 0:
        if five_min_volume < prior_five_min_volume:
            state.near_high_minutes += 1
        else:
            state.near_high_minutes = 0
        if state.near_high_minutes >= near_high_minutes:
            state.fired = True
            return ("exhausted", "within %.0f%% of the high on declining 5m volume"
                    % min_gap_pct)
    else:
        state.near_high_minutes = 0

    return None


def abort_message(symbol: str, price: float, alert_price: float, reason: str,
                  reason_key: str = "", ts: Optional[float] = None) -> str:
    """The one ABORT message.

    Stated as an instruction and never as an opinion: do not enter, or exit if
    holding. There is no "consider" here, because a setup that has been
    invalidated does not have a second opinion worth hearing.
    """
    moment = datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else None
    when = to_et(moment).strftime("%H:%M ET")
    rubric = REASON_RUBRIC.get(reason_key, "")
    tail = " (%s)" % rubric if rubric else ""
    move = ((price / alert_price - 1.0) * 100.0) if alert_price else 0.0
    return "\n".join([
        "\u2500\u2500 ABORT \u2500\u2500\u2500\u2500",
        "%s  %s   setup invalidated %s" % (symbol, money(price), when),
        "reason: %s%s" % (reason, tail),
        "action: do not enter / exit if holding",
        "since alert: %s  (alert %s \u2192 %s)"
        % (pct(move), money(alert_price), money(price)),
    ])
