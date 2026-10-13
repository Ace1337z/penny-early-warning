"""The live engine: poll loop, alert rules, digests, tracking, end of day (6.3-6.15)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .ai.decision import decide, render as render_decision, render_rules_fallback
from .alerts import (build_ai_facts, format_alert1, format_alert2, format_build_digest,
                     tier_name)
from .context.sector_heatmap import SectorHeatmap
from .enrich import Enricher
from .fib import reentry_guidance
from .market_context import MarketContextProvider
from .scoring import (PHASE_EXTENDED, PHASE_FADING, TIER_EARLY, Metrics, Scorer)
from .strategies.wiring import run_gate
from .tracking import Tracker, eod_report, stats_report
from .sources.finviz import FinvizTokenRejected, avg_volume_shares, row_get, row_num
from .ui import button, cb, inline
from datetime import datetime, timedelta, timezone

from .util import (SESSION_CLOSED, age_str, is_trading_day, money, pct, safe_div,
                   session_end, session_for, to_et)

log = logging.getLogger(__name__)

EXCLUDED_SUFFIXES = {"W", "WS", "WT", "U", "UN", "R", "RT", "P", "PR", "A", "B", "C"}
SPLIT_ARTIFACT_PCT = 400.0

# Gate verdicts the poll-loop pass cannot reach, because the Bundle it builds
# carries no candles and no news (see Engine._run_gate). They are reported by
# the detectors honestly -- "no 1m bars", "news not fetched" -- but as a verdict
# they are a missing input, not a decision, so they must not veto here.
POLL_UNDECIDABLE = frozenset({"no_strategy", "no_catalyst"})

# How long a gate-refused symbol goes without a second enrichment attempt
# (Spec 1.1 enriches candidates ahead of Alert 1, so a refusing setup would
# otherwise be re-fetched on every cycle it stays a candidate). Ceiling: the
# cooldown also delays a setup whose news arrives inside it; move it to config
# when a live deployment needs to tune that trade-off.
GATE_REFUSE_COOLDOWN_S = 300.0


def alert_menu(symbol: str) -> dict:
    """Inline buttons under an alert so a symbol is one tap away."""
    return inline([
        [button(f"\U0001F504 Re-check", cb("check", symbol)),
         button("\U0001F54C Halal", cb("halal", symbol)),
         button("\U0001F3E0 Menu", cb("menu"))],
    ])


def _no_news_tag(gate) -> bool:
    """Spec 2.0 A: the [NO-NEWS] tag, true when a tape mechanism fired.

    Only an enriched gate can decide this -- the mechanism detectors need the
    news *fetched* before "no headline" is a fact rather than a gap -- so an
    advisory or poll-time gate never tags, and neither invents certainty.
    """
    from .strategies.detectors import MECHANISM_NAMES
    if gate is None:
        return False
    return any(getattr(r, "verdict", "") == "PASS"
               and r.name in MECHANISM_NAMES
               for r in getattr(gate, "strategies", None) or [])


def _cause_present(bundle) -> bool:
    """Spec 2.0 D: was a cause named? The (cause_present, rating) bucket half."""
    if bundle is None or getattr(bundle, "news", None) is None:
        return False
    try:
        from .strategies.news_cause import extract_cause, rank_catalysts
        _, ranked, _ = rank_catalysts(bundle)
        return extract_cause(ranked, bundle).kind != "UNEXPLAINED"
    except Exception:  # noqa: BLE001
        return False


class Engine:
    """Discover movers, sweep the universe, score, alert, enrich, forecast."""

    def __init__(self, cfg, db, journal, telegram, *, finviz=None,
                 alpaca=None, finnhub=None, sec=None, finra=None, yahoo=None,
                 twelvedata=None,
                 gateway=None, catalog=None, shariah=None, backup=None, clock=None):
        self.cfg = cfg
        self.db = db
        self.journal = journal
        self.telegram = telegram
        self.finviz = finviz
        self.alpaca = alpaca
        self.finnhub = finnhub
        self.sec = sec
        self.finra = finra
        self.yahoo = yahoo
        self.twelvedata = twelvedata
        self.gateway = gateway
        self.catalog = catalog
        self.shariah = shariah
        self.backup = backup
        self.clock = clock or time.time

        self.scorer = Scorer(cfg, clock=self.clock)
        self.tracker = Tracker(db, clock=self.clock)
        self.market = MarketContextProvider(cfg, finviz=finviz, yahoo=yahoo)
        self.enricher = Enricher(cfg, finviz=finviz, alpaca=alpaca,
                                 finnhub=finnhub, sec=sec, finra=finra, yahoo=yahoo,
                                 twelvedata=twelvedata, market=self.market)
        self.gateway = gateway          # one call per alert; see ai/decision.py
        self.tokens_used = 0     # cumulative, for the status line
        self.heatmap = SectorHeatmap(
            min_hot_pct=cfg.float("SECTOR_MIN_HOT_PCT", 2.0),
            top_n=cfg.int("SECTOR_TOP_N", 3),
            refresh_seconds=cfg.int("SECTOR_HEATMAP_REFRESH_SECONDS", 300),
            clock=self.clock)
        # Feature 4: one watcher per alerted setup, and one abort message each.
        self._invalidations: dict[str, object] = {}
        self._last_invalidation_check = 0.0
        self._calibration: dict = {}
        self._calibration_day = ""

        self.universe: list[str] = []
        self.avg_volume: dict[str, float] = {}
        self._universe_ts = 0.0
        self._universe_rows: dict[str, dict] = {}
        self._mover_rows: dict[str, dict] = {}
        self._movers: list[str] = []
        self._movers_ts = 0.0
        self._quotes_by_symbol: dict[str, dict] = {}
        self._last_metrics: list[Metrics] = []
        self._builds: list = []
        self._gates: dict[str, object] = {}
        self._gate_rejects: dict[str, dict] = {}
        # Spec 3: per-symbol ARMED setups awaiting a closed-1m-bar confirmation.
        # Empty unless ARMED_CONFIRM=1, so the default path is unchanged.
        self._armed: dict[str, object] = {}
        self._split_cache: dict[str, bool] = {}
        self._last_alert: dict[str, tuple[float, int]] = {}
        self._alert_ids: dict[str, int] = {}
        self._halt_resume: set[str] = set()
        self._tracker_state: dict[str, dict] = {}
        self._stop = threading.Event()
        self._finviz_notified = False
        self._feed_failures = 0
        self._feed_notified = False
        self._last_cycle: dict = {}
        self._last_digest_key = ""
        self._last_build_ts = 0.0
        self._last_build_ids: set[str] = set()
        self._last_eod_day = ""
        self._last_hourly_backup = 0.0
        self._last_full_backup = 0.0
        self._sizing_fh = None
        self.started_at = self.clock()
        self.running = False
        self.forced_session: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def cycle_interval(self) -> float:
        return float(max(5, self.cfg.int("UNIVERSE_POLL_SECONDS", 30)))

    def _in_session(self) -> bool:
        return (self.forced_session or session_for()) != SESSION_CLOSED

    # -- universe -----------------------------------------------------------
    def build_universe(self, force: bool = False) -> list[str]:
        """Refresh the sub-price universe. The same screener call returns the
        price/volume columns, so its rows double as the quote feed each cycle."""
        now = self.clock()
        interval = (self.cfg.int("UNIVERSE_POLL_SECONDS", 30)
                    if self._in_session() else 3600)
        if not force and self.universe and now - self._universe_ts < interval:
            return self.universe
        symbols: list[str] = []
        if self.finviz:
            try:
                rows = self.finviz.universe(ttl=0)
                min_p = self.cfg.float("MIN_PRICE", 0.1)
                max_p = self.cfg.float("MAX_PRICE", 10.0)
                fresh: dict[str, dict] = {}
                for row in rows:
                    ticker = (row_get(row, "Ticker") or "").upper().strip()
                    if not ticker or not ticker.isalpha() or len(ticker) > 5:
                        continue
                    if ticker in EXCLUDED_SUFFIXES:
                        continue
                    price = row_num(row, "Price")
                    if price is not None and not (min_p <= price <= max_p):
                        continue
                    symbols.append(ticker)
                    fresh[ticker] = row
                    avg = avg_volume_shares(row)
                    if avg:
                        self.avg_volume[ticker] = avg
                if fresh:
                    self._universe_rows = fresh
                self._finviz_notified = False
            except FinvizTokenRejected:
                self._notify_finviz_rejected()
            except Exception as exc:  # noqa: BLE001
                log.warning("universe build failed: %s", exc)
        for extra in self.cfg.list("EXTRA_SYMBOLS"):
            if extra.upper() not in symbols:
                symbols.append(extra.upper())
        if symbols:
            self.universe = sorted(set(symbols))
            self._universe_ts = now
        return self.universe

    def _notify_finviz_rejected(self) -> None:
        if self._finviz_notified:
            return
        self._finviz_notified = True
        self._safe_send("Finviz token rejected; run: penny set FINVIZ_TOKEN")

    # -- one cycle ----------------------------------------------------------
    def cycle(self) -> dict:
        started = time.monotonic()
        self.cfg.reload_if_changed()
        session = self.forced_session or session_for()
        stats = {"session": session, "quotes": 0, "alerts": 0, "symbols": 0, "ms": 0.0}

        if session == SESSION_CLOSED:
            self.scorer.prune()
            self._maybe_end_of_day()
            stats["ms"] = (time.monotonic() - started) * 1000
            self._last_cycle = stats
            return stats

        self.build_universe()
        symbols = self._symbols_for_cycle(session)
        quotes = self._fetch_quotes(symbols, session)
        stats["quotes"] = len(quotes)
        if not quotes:
            self._feed_failures += 1
            if self._feed_failures >= 5 and not self._feed_notified:
                self._feed_notified = True
                self._safe_send("Feed failing: 5 consecutive cycles returned no quotes.")
        else:
            if self._feed_notified:
                self._feed_notified = False
                self._safe_send("Feed recovered.")
            self._feed_failures = 0

        self._quotes_by_symbol = {q["symbol"]: q for q in quotes}
        # The heatmap is built from the movers only -- a stock that is not on
        # the list did not move today, and counting the whole universe would
        # make every sector look flat.
        self.heatmap.update([q for q in quotes if q["symbol"] in set(self._movers)])
        metrics_list: list[Metrics] = []
        for quote in quotes:
            if quote.get("halted"):
                self.scorer.mark_halted(quote["symbol"], True)
                continue
            m = self.scorer.compute(quote, session)
            if m is None:
                continue
            metrics_list.append(m)
            self._record_daily(m)

        stats["symbols"] = len(self.scorer.states)
        ranked = self.scorer.rank(metrics_list)
        pct_rank = ranked["pct_rank"]
        self._last_metrics = metrics_list

        for m in ranked["by_score"]:
            fast = self._fast_burst(m, session)
            if not self._alert_candidate(m, session, fast=fast):
                continue
            # The strategy gate. Metrics-only detectors run here, on every poll,
            # with no extra fetch. The enrichment primaries run later, in
            # _fire_alert, where a fetch is already being paid for.
            gate, bundle = self._run_gate(m, metrics_list)
            if self._gate_vetoes(m, gate, stats):
                continue
            if self._fire_alert(m, session, pct_rank, fast=fast, gate=gate, bundle=bundle):
                stats["alerts"] += 1

        self._update_tracker(metrics_list)
        self._check_invalidations(metrics_list)
        self._record_daily_close(metrics_list)
        self._track_watchlist(metrics_list, session)
        self._maybe_build_feed(session, metrics_list)
        self._maybe_digest(session, metrics_list)
        self.scorer.cycle()
        self.scorer.prune()
        self._maybe_backups()
        self._maybe_end_of_day()

        stats["ms"] = (time.monotonic() - started) * 1000
        self._record_cycle(stats)
        self._last_cycle = stats
        return stats

    def _symbols_for_cycle(self, session: str) -> list[str]:
        symbols: list[str] = list(self.universe)
        now = self.clock()
        interval = self.cfg.int("MOVERS_POLL_SECONDS", 20)
        if self.finviz and (not self._movers_ts or now - self._movers_ts >= interval):
            try:
                rows = self.finviz.movers(
                    session, top_n=self.cfg.int("MOVERS_TOP_N", 200))
                self._mover_rows = {}
                self._movers = []
                for row in rows:
                    ticker = (row_get(row, "Ticker") or "").upper().strip()
                    if not ticker:
                        continue
                    self._mover_rows[ticker] = row
                    self._movers.append(ticker)
                self._movers_ts = now
            except FinvizTokenRejected:
                self._notify_finviz_rejected()
            except Exception as exc:  # noqa: BLE001
                log.debug("movers fetch failed: %s", exc)
        for sym in self._movers:
            if sym not in symbols:
                symbols.append(sym)
        return symbols

    def _fetch_quotes(self, symbols: list[str], session: str) -> list[dict]:
        """Build quotes from the rows already fetched this cycle (universe + movers),
        and only call Finviz again for symbols neither screen returned."""
        if not self.finviz:
            return []
        rows: dict[str, dict] = {}
        rows.update(self._universe_rows)
        rows.update(self._mover_rows)
        quotes: list[dict] = []
        missing: list[str] = []
        for symbol in symbols:
            row = rows.get(symbol)
            if row is None:
                missing.append(symbol)
                continue
            try:
                quote = self.finviz.normalize_row(row, session)
            except Exception as exc:  # noqa: BLE001
                log.debug("quote normalize failed for %s: %s", symbol, exc)
                quote = None
            if quote:
                quotes.append(quote)
        batch = max(1, self.cfg.int("FINVIZ_QUOTE_BATCH", 40))
        for i in range(0, len(missing), batch):
            chunk = missing[i:i + batch]
            try:
                quotes.extend(self.finviz.snapshot(chunk, session))
            except FinvizTokenRejected:
                self._notify_finviz_rejected()
            except Exception as exc:  # noqa: BLE001
                log.warning("snapshot failed for %d symbols: %s", len(chunk), exc)
        out = []
        min_p = self.cfg.float("MIN_PRICE", 0.1)
        max_p = self.cfg.float("MAX_PRICE", 10.0)
        for q in quotes:
            price = q.get("price")
            if not price or not (min_p <= price <= max_p):
                continue
            avg = self.avg_volume.get(q["symbol"]) or q.get("avg_volume")
            if avg:
                self.scorer.set_avg_volume(q["symbol"], avg)
            out.append(q)
        return out

    # -- alert rules --------------------------------------------------------
    def _reached_alert_bar(self, m: Metrics, session: str, fast: bool) -> bool:
        """Has this stock actually reached the alert bar yet?

        Spec 1.1: the alert fires the moment the strategy gate passes, and the
        15m and 60m windows come out of the alert path entirely. With the gate
        mandatory, the gate IS the bar: it decides on the full picture ahead of
        Alert 1 (see `_fire_alert`, where enrichment now runs first), so the
        tier windows leave the path as designed. Candidacy here is deliberately
        broad -- the poll pass still prunes the reality blockers cheaply, and
        everything else is the gate's call, not the tiers'.

        With the gate off the tier path is in charge: the tier, the fast-burst
        fallback and the pre-market rule decide, and both fallbacks are weaker
        than the tier they replace.
        """
        if self._gate_is_mandatory():
            return True
        min_tier = max(TIER_EARLY, self.cfg.int("ALERT_MIN_TIER", 2))
        if m.tier >= min_tier:
            return True
        return fast or self._premarket_breakout(m, session)

    def _alert_candidate(self, m: Metrics, session: str,
                         fast: Optional[bool] = None) -> bool:
        if fast is None:
            fast = self._fast_burst(m, session)
        if not self.scorer.warm:
            return False
        st = self.scorer.state(m.symbol)
        if not self._reached_alert_bar(m, session, fast):
            return False
        if m.tier <= st.last_alert_tier:
            if self.clock() - st.last_alert_ts < self.cfg.int("REALERT_SECONDS", 3600):
                return False
        if (m.phase == PHASE_EXTENDED and not self.cfg.bool("ALERT_EXTENDED")
                and m.pct_vs_close is not None
                and m.pct_vs_close * 100 >= self.cfg.float("EXTENDED_PCT", 100)):
            return False
        if self._possible_split_artifact(m):
            log.info("%s: possible split artifact; not alerting", m.symbol)
            return False
        if self.shariah and self.shariah.enabled:
            status = self._shariah_status(m.symbol)
            allowed, reason = self.shariah.should_alert(status)
            if not allowed:
                self.db.execute(
                    "INSERT INTO suppressed(ts, symbol, tier, reason, price, pct) "
                    "VALUES(?,?,?,?,?,?)",
                    (self.clock(), m.symbol, m.tier, reason, m.price, m.pct_vs_close))
                return False
        return True

    def _premarket_breakout(self, m: Metrics, session: str) -> bool:
        """Pre-market: alert at +10% when volume backs it, instead of at +20%.

        The rise windows are thin before 09:30, so a stock gapping at 04:00 often
        never reached the 15-minute volume multiplier the tier needs, and the
        first alert only fired once the move was already ~20%. Volume still has
        to confirm: a headline percentage on no trade is a spread, not a setup.
        """
        if session != "pre" or m.pct_vs_close is None:
            return False
        if m.pct_vs_close * 100.0 < self.cfg.float("PREMARKET_PCT", 10):
            return False
        return (m.volx.get(15, 0.0) >= self.cfg.float("PREMARKET_VOLX15", 3)
                and m.dollar_vol.get(15, 0.0) >= self.cfg.float("PREMARKET_DOLLAR15", 5000))
    def _fast_burst(self, m: Metrics, session: str) -> bool:
        """Alert on a 3-5 minute burst instead of waiting for the 15/60-minute gates.

        `tier_for` measures a 15-minute rise and a 60-minute move, and tier 2 also
        demands 10x normal 15-minute volume. Every one of those windows has to fill
        before the tier is reached, which is exactly the definition of late: the
        burst is over and the alert arrives on the give-back. This lane reads the
        same raw samples over 3 and 5 minutes, so the alert lands while the move
        is still happening.

        Three guards keep it honest. The move must be real flow and not a spread
        on no trade, so the volume multiple and dollar volume both have to
        confirm. It must not already be spent, so a stock parked below its high
        after an earlier run does not re-alert. And it must be liquid enough to
        get into, so there is a floor on cumulative volume.

        There is deliberately no 15 or 60 minute requirement. Those are the
        windows that made the old alerts late, and a stock flat all day that
        suddenly prints 3% in three minutes is exactly the case worth catching --
        it is the penny mover this tool exists for.

        This can only *add* alerts; the tier path is untouched, so a mis-set
        FAST_* threshold costs extra messages, never a missed one.
        """
        if not self.cfg.bool("FAST_ON", True):
            return False
        rise3 = self.cfg.float("FAST_RISE3", 2) / 100.0
        rise5 = self.cfg.float("FAST_RISE5", 3) / 100.0
        if rise3 <= 0 and rise5 <= 0:
            return False                       # both windows off: the fast lane is off
        if m.pct_vs_close is None or m.pct_vs_close <= 0:
            return False
        # 3m OR 5m, not both. A stock up 4% in five minutes has not necessarily
        # moved 2% in the last three -- it printed most of that run two minutes
        # ago and is holding. Requiring both would drop exactly the slower
        # builders this lane exists to catch.
        r3 = m.rise.get(3, 0.0) or 0.0
        r5 = m.rise.get(5, 0.0) or 0.0
        hot3 = rise3 > 0 and r3 >= rise3
        hot5 = rise5 > 0 and r5 >= rise5
        if not (hot3 or hot5):
            return False

        # Volume is judged on the window that actually triggered. Checking both
        # would fail a 5-minute-only builder on its empty 3-minute volume, which
        # is the same mistake the tiers already made at 15 minutes.
        if hot3:
            if m.volx.get(3, 0.0) < self.cfg.float("FAST_VOLX3", 5):
                return False
            if m.dollar_vol.get(3, 0.0) < self.cfg.float("FAST_DOLLAR3", 3000):
                return False
        else:
            if m.volx.get(5, 0.0) < self.cfg.float("FAST_VOLX5", 5):
                return False
        # A burst that has already given the move back is not a fresh entry.
        from_high = self.cfg.float("FAST_MAX_FROM_HIGH", -6) / 100.0
        if from_high < 0 and m.from_high is not None and m.from_high < from_high:
            return False
        min_vol = self.cfg.float("FAST_MIN_VOLUME", 50000)
        if min_vol > 0 and (m.cum_volume or 0.0) < min_vol:
            return False
        # Same reasoning as the tiers: a move under VWAP is a bounce, not a build.
        if m.vwap is not None and m.price <= m.vwap:
            return False
        return True

    def fast_burst(self, m: Metrics, session: str) -> bool:
        """`_fast_burst` for callers outside the engine (e.g. `/check`)."""
        return self._fast_burst(m, session)

    def _possible_split_artifact(self, m: Metrics) -> bool:
        if m.pct_vs_close is None or m.pct_vs_close * 100 < SPLIT_ARTIFACT_PCT:
            return False
        cached = self._split_cache.get(m.symbol)
        if cached is not None:
            return cached
        result = False
        if self.sec:
            try:
                # Spec 1.1: reverse splits keep a 90-day lookback; everything
                # else expires after 14 days. `classify` reuses the same rules
                # as the gate, so the split-artifact guard and the SEC veto
                # never disagree about a 1-for-60 print.
                from .strategies import toxicity
                from .util import to_et
                filings = self.sec.filings(
                    m.symbol, days=toxicity.REVERSE_SPLIT_WINDOW_DAYS)
                for i, f in enumerate(filings[:4]):
                    if str(f.get("form") or "").upper() in ("8-K", "6-K") and f.get("url"):
                        f = dict(f)
                        f["text"] = self.sec.filing_text(f) or ""
                        filings[i] = f
                risk = toxicity.classify(filings, today=to_et().date())
                result = (risk.risk == toxicity.REVERSE_SPLIT_RECENT
                          or any(str(f.get("form") or "").upper() == "25"
                                 for f in filings))
            except Exception:  # noqa: BLE001
                result = False
        self._split_cache[m.symbol] = result
        return result

    def _fire_alert(self, m: Metrics, session: str, pct_rank: dict,
                    fast: bool = False, gate=None, bundle=None) -> bool:
        # Spec 1.1: with the gate mandatory, the alert fires the moment the
        # gate passes -- and the gate can only pass on candles, news and float,
        # which the poll pass does not have (POLL_UNDECIDABLE). Enrichment
        # therefore moves ahead of Alert 1: the fetch the detail thread used to
        # pay for is paid here, for candidates only, and the gate decides
        # before anything is sent. A refused setup costs one API call; an
        # ungated alert costs the premise.
        enr = None
        if self._gate_is_mandatory() and gate is not None:
            refused = self._gate_rejects.get(m.symbol)
            if refused and self.clock() - refused["ts"] < GATE_REFUSE_COOLDOWN_S:
                return False
            try:
                enr = self.enricher.enrich(m.symbol, self._quote_for(m), m)
            except Exception as exc:  # noqa: BLE001
                # Cannot decide -- the alert does not go. Fail closed.
                log.exception("enrichment failed for %s -- alert suppressed: %s",
                              m.symbol, exc)
                return False
            gate, bundle = self._recheck_gate(m, gate, bundle, enr)
            if not gate.passed:
                return False

        # Spec 3: with ARMED_CONFIRM on, a passing setup is ARMED and only
        # fires once a closed 1m bar confirms. The stage-2 check needs bars, and
        # the enriched candidate above already fetched them.
        if self._armed_confirm_enabled():
            hold = self._armed_decision(m, gate, bundle)
            if hold is not None:
                log.debug("%s: held by confirmation machine (%s)", m.symbol, hold)
                return False

        st = self.scorer.state(m.symbol)
        rank = pct_rank.get(m.symbol)
        resumed = st.halt_resume_pending
        st.halt_resume_pending = False

        shariah_status = self._shariah_status(m.symbol)
        shariah_line = "Shariah: checking..."
        if self.shariah and self.shariah.enabled and shariah_status.get("combined") != "UNKNOWN":
            shariah_line = self.shariah.one_line(shariah_status)

        text1 = format_alert1(m, shariah_line=shariah_line, pct_rank=rank, resumed=resumed,
                              fast=fast, html=True, gate=gate,
                              no_news=_no_news_tag(gate), bundle=bundle)
        msg_id = None
        try:
            msg_id = self.telegram.send(text1, markup=alert_menu(m.symbol), html=True)
        except Exception as exc:  # noqa: BLE001
            log.error("alert 1 send failed: %s", exc)

        alert_id = self._save_alert(m, msg_id, shariah_status)
        st.last_alert_ts = self.clock()
        st.last_alert_tier = m.tier
        st.last_tier = m.tier
        self._alert_ids[m.symbol] = alert_id
        # Start tracking before the detail thread, so a slow AI call cannot leave
        # a gap between the alert and the first tracked quote.
        try:
            self.tracker.start(
                m.symbol, alert_ts=self.clock(), alert_price=m.price,
                primary=getattr(gate, "primary", None),
                entry_trigger=getattr(gate, "entry_trigger", None),
                gate_score=getattr(gate, "gate_score", None),
                plan=getattr(bundle, "plan", None) if bundle is not None else None)
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker start failed for %s: %s", m.symbol, exc)
        # Feature 4: put the setup on the invalidation watch. Registered here,
        # at the alert, because the two numbers it needs -- the low of the 1m
        # candle and the VWAP -- describe this moment and no later one.
        self._register_invalidation(m, bundle=bundle)
        log.info("ALERT %s tier %d score %.1f", m.symbol, m.tier, m.score)

        # The enrichment (when the gate is mandatory) is already in hand: hand
        # it to the detail thread so Alert 2 does not pay for the same fetch.
        threading.Thread(target=self._alert_detail_worker,
                         args=(m, alert_id, msg_id, session, rank, shariah_status, fast,
                               gate, bundle, enr),
                         daemon=True, name=f"detail-{m.symbol}").start()
        return True

    def _save_alert(self, m: Metrics, msg_id: Optional[int], shariah_status) -> int:
        combined = (shariah_status or {}).get("combined")
        cur = self.db.execute(
            "INSERT INTO alerts(ts, symbol, tier, price, pct, score, msg_id, shariah) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self.clock(), m.symbol, m.tier, m.price,
             m.pct_vs_close * 100 if m.pct_vs_close is not None else None, m.score, msg_id,
             combined))
        return int(cur.lastrowid or 0)

    # -- detail + AI --------------------------------------------------------
    def _alert_detail_worker(self, m: Metrics, alert_id: int, msg_id, session: str,
                             rank, shariah_status, fast: bool = False,
                             gate=None, bundle=None, enr=None) -> None:
        try:
            self._send_detail(m, alert_id, msg_id, session, rank, fast=fast,
                              gate=gate, bundle=bundle, enr=enr)
        except Exception as exc:  # noqa: BLE001
            log.exception("detail worker failed for %s: %s", m.symbol, exc)

    def _send_detail(self, m: Metrics, alert_id: int, msg_id, session: str, rank,
                     fast: bool = False, gate=None, bundle=None, enr=None) -> None:
        if enr is None:
            enr = self.enricher.enrich(m.symbol, self._quote_for(m), m)
        market = self.market.get()

        # Now that the symbol has been enriched, re-run the gate with the inputs
        # that were unavailable in the poll loop: candles, news, filings, float.
        # The metrics-only pass could only see a fraction of the picture, so this
        # is the verdict that counts. A hard blocker found here still holds.
        if gate is not None:
            gate, bundle = self._recheck_gate(m, gate, bundle, enr)

        shariah = self._shariah_status(m.symbol, filings=enr.filings, fetch=True)
        shariah_lines = self.shariah.detail_lines(shariah) if self.shariah else []
        if msg_id and self.shariah and shariah.get("combined") != "UNKNOWN":
            try:
                self.telegram.edit(msg_id, format_alert1(
                    m, shariah_line=self.shariah.one_line(shariah), pct_rank=rank,
                    fast=fast, html=True, no_news=_no_news_tag(gate)),
                    markup=alert_menu(m.symbol), html=True)
            except Exception:  # noqa: BLE001
                pass
        if alert_id and shariah.get("combined"):
            self._store_alert_shariah(alert_id, shariah)

        # Phase 2 context, computed here because enrichment is the first moment
        # candles and news exist. Best-effort: a context failure must not cost the
        # alert or the decision that goes with it.
        try:
            context = self._build_context(m, enr=enr, gate=gate)
        except Exception as exc:  # noqa: BLE001
            log.warning("context build failed for %s: %s", m.symbol, exc)
            context = {}
        self._write_tracker_context(m, enr, context)

        facts = build_ai_facts(m, enr, market, session=session, pct_rank=rank,
                               time_to_horizon=self._time_to_horizons(),
                               gate=gate, bundle=bundle, context=context)
        detail_id = None
        try:
            detail_id = self.telegram.send(format_alert2(
                m, enr, market, shariah_lines=shariah_lines, gate=gate,
                bundle=bundle, fast=fast, html=True),
                markup=alert_menu(m.symbol), html=True)
        except Exception as exc:  # noqa: BLE001
            log.error("alert 2 send failed: %s", exc)

        # One call, one decision. It is made after the placeholder is already in
        # the chat, so a slow provider delays the verdict but never the alert.
        decision_text = ""
        decision = None
        try:
            decision = decide(self.gateway, facts,
                              model=self.cfg.raw("AI_MODEL") or "",
                              deadline=self.clock() + self.cfg.float("AI_TIMEOUT", 45.0),
                              gate_passed=(gate.passed if gate is not None else None),
                              min_additive=self.cfg.bool("AI_MIN_ADDITIVE", True))
            decision_text = render_decision(decision)
            self.tokens_used += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("AI decision failed for %s: %s", m.symbol, exc)
        if not decision_text:
            # Spec 2.0 C: the pipeline still has a view. An unavailable call or
            # a rejected report degrades to the rules-only block -- never to a
            # placeholder, never to a silent PASS.
            decision_text = render_rules_fallback(
                m, gate, bundle,
                source=(decision.source if decision is not None else "rules"),
                reason=(decision.error if decision is not None else "no decision"),
                cfg=self.cfg)

        text2 = format_alert2(m, enr, market, shariah_lines=shariah_lines, gate=gate,
                              bundle=bundle, decision=decision_text,
                              fast=fast, html=True)
        if detail_id:
            try:
                self.telegram.edit(detail_id, text2, markup=alert_menu(m.symbol), html=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("alert 2 edit failed: %s", exc)
        else:
            self._safe_send(text2)

        if alert_id:
            self.db.execute("UPDATE alerts SET alert2_id=? WHERE id=?", (detail_id, alert_id))
        if decision is not None and getattr(decision, "ok", False):
            self._store_decision(decision, facts)
            # Feature 5: the number the reader actually saw, which is the
            # calibrated one, not the model's claim. Claimed-vs-actual is the
            # whole point, so the two must not be the same column.
            if decision.clamp_flag:
                log.info("confidence clamped for %s: %.2f -> %.2f", m.symbol,
                         decision.confidence, decision.confidence_calibrated)
            try:
                self.tracker.write_confidence(
                    m.symbol, decision.confidence_calibrated or decision.confidence)
            except Exception as exc:  # noqa: BLE001
                log.debug("confidence write failed for %s: %s", m.symbol, exc)
            # The rating and verdict travel with the alert so the EOD can show
            # what was claimed next to what happened, and the post-mortem can
            # quote the AI's own findings rather than reconstruct them.
            try:
                self.tracker.write_decision(
                    m.symbol, rating=getattr(decision, "rating", "") or "",
                    verdict=getattr(decision, "verdict", "") or "",
                    findings=getattr(decision, "key_findings", None) or [],
                    source="ai", cause_present=_cause_present(bundle))
            except Exception as exc:  # noqa: BLE001
                log.debug("decision write failed for %s: %s", m.symbol, exc)
        elif decision is not None:
            # Spec 2.0 D: provenance is recorded even when the AI did not
            # deliver -- a rules-only or rejected report has a hit rate too, and
            # /stats can only separate the buckets if the row says which spoke.
            try:
                self.tracker.write_decision(
                    m.symbol, rating="", verdict=decision.verdict,
                    findings=[], source=decision.source,
                    cause_present=_cause_present(bundle))
            except Exception as exc:  # noqa: BLE001
                log.debug("decision write failed for %s: %s", m.symbol, exc)

    def _write_tracker_context(self, m: Metrics, enr, context: dict) -> None:
        """Persist the quote reality and the AI context onto the alert row."""
        if not context:
            return
        q = self._quotes_by_symbol.get(m.symbol) or {}
        from .strategies.spread_liquidity import avg_trade_size
        dollars = float(m.dollar_vol.get(15, 0.0) or 0.0)
        trades = getattr(m, "trades_15m", None)
        try:
            self.tracker.write_context(
                m.symbol,
                bid=m.bid, ask=m.ask, spread_pct=m.spread_pct,
                dollar_vol_15m=dollars,
                avg_trade_size=avg_trade_size(dollars, trades),
                sector=str(getattr(enr, "sector", "") or "") or None,
                sector_hot=bool(context.get("sector_hot")),
                exit_liq_2k_seconds=((context.get("exit_liquidity") or {})
                                     .get("2000", {}).get("seconds")),
                analogs=(context.get("analogs") or {}).get("summary"))
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker context write failed for %s: %s", m.symbol, exc)

    def _store_decision(self, decision, facts: dict) -> None:
        """Record the one report, so /stats can be checked against what was sent."""
        try:
            self.db.execute(
                "INSERT INTO ai_calls(ts, model, kind, ok, latency_ms, tokens, error) "
                "VALUES(?,?,?,?,?,?,?)",
                (self.clock(), decision.model or "unknown", "decision",
                 1 if decision.ok else 0, decision.latency_ms, 0, decision.error or ""))
        except Exception as exc:  # noqa: BLE001
            log.debug("decision log failed for %s: %s", facts.get("symbol"), exc)

    def _quote_for(self, m: Metrics) -> dict:
        quote = self._quotes_by_symbol.get(m.symbol)
        if quote:
            return quote
        return {"symbol": m.symbol, "price": m.price, "cum_volume": m.cum_volume,
                "prev_close": m.prev_close, "float_shares": m.float_shares,
                "market_cap": m.market_cap, "ts": m.ts, "halted": m.halted}

    def check_quote(self, symbol: str) -> Optional[dict]:
        """A fresh quote for an interactive `/check`.

        The cycle only quotes the sub-$10 universe and the mover screens, so a
        normal stock (NVDA, AAPL) has no cached quote at all, and a cached row can
        be up to one cycle old. This fetches the symbol on demand with the current
        session, so the price reflects the live (including after-hours) print.
        """
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            return None
        cached = self._quotes_by_symbol.get(symbol)
        if not self.finviz:
            return cached
        session = self.forced_session or session_for()
        try:
            quotes = self.finviz.snapshot([symbol], session)
        except FinvizTokenRejected:
            self._notify_finviz_rejected()
            return cached
        except Exception as exc:  # noqa: BLE001
            log.warning("check quote fetch failed for %s: %s", symbol, exc)
            return cached
        for quote in quotes:
            if str(quote.get("symbol") or "").upper() == symbol:
                return quote
        return cached


    def _time_to_horizons(self) -> dict:
        now = self.clock()
        end = session_end(datetime.fromtimestamp(now, tz=timezone.utc))
        return {"15m": 15, "60m": 60,
                "session_end": max(0, int((end.timestamp() - now) / 60))}

    # -- tracking -----------------------------------------------------------
    def _track_watchlist(self, metrics_list: list[Metrics], session: str) -> None:
        rows = self.db.query("SELECT * FROM watch")
        if not rows:
            return
        by_symbol = {m.symbol: m for m in metrics_list}
        now = self.clock()
        for row in rows:
            m = by_symbol.get(row["symbol"])
            if m is None:
                continue
            state = self._tracker_state.setdefault(row["symbol"], {})
            for event, cooldown, text in self._tracker_events(m, row, state):
                if now - state.get(event, 0) < cooldown:
                    continue
                state[event] = now
                self._safe_send(self._tracker_message(m, row, text))

    def _tracker_events(self, m: Metrics, watch, state: dict):
        out = []
        price = m.price
        entry = watch["entry"]
        last_high = state.get("_high", entry or price)
        if entry and price > last_high * 1.03 and m.volx.get(15, 0) >= 3:
            state["_high"] = price
            out.append(("new_high", 300, f"NEW HIGH {money(price)}"))
        elif entry and price > last_high:
            state["_high"] = price
        if m.vwap:
            was_above = state.get("_above_vwap")
            is_above = price > m.vwap
            if was_above is not None and is_above != was_above:
                out.append(("vwap_lost" if not is_above else "vwap_reclaimed", 300,
                            "VWAP LOST" if not is_above else "VWAP RECLAIMED"))
            state["_above_vwap"] = is_above
        if m.from_high is not None and m.from_high <= -0.12:
            out.append(("fading", 600, f"FADING {pct(m.from_high * 100)} from high"))
        if (m.from_high is not None and m.from_high <= -0.10
                and m.rise.get(5, 0) >= 0.03 and m.volx.get(5, 0) >= 5):
            out.append(("volume_returning", 600, "VOLUME RETURNING"))
        if watch["stop"] and price <= watch["stop"]:
            out.append(("stop", 600, f"STOP TOUCHED {money(price)}"))
        if watch["target"] and price >= watch["target"]:
            out.append(("target", 600, f"TARGET REACHED {money(price)}"))
        return out

    def _tracker_message(self, m: Metrics, watch, text: str) -> str:
        entry = watch["entry"]
        lines = [f"TRACKER {m.symbol} {money(m.price)}  {text}"]
        if entry:
            lines.append(f"P/L {pct((m.price / entry - 1) * 100)} vs entry {money(entry)}")
        if m.vwap:
            lines.append(reentry_guidance(m.price, m.vwap, None))
        return "\n".join(lines)

    # -- digests / end of day ----------------------------------------------
    # -- tracking -----------------------------------------------------------
    def _update_tracker(self, metrics: list[Metrics]) -> None:
        """Fold this cycle's quotes into every open alert.

        Only symbols with an open tracked alert are touched. Finviz already
        returns a session high and low per row, so max gain, drawdown and the
        stop/target tests need no extra fetch -- the TwelveData quota is not
        spent here, which is the whole reason tracking reads quotes rather than
        re-fetching candles.
        """
        if not self.cfg.bool("TRACKER_ENABLED", True):
            return
        open_syms = self.tracker.open_symbols()
        if not open_syms:
            return
        wanted = set(open_syms)
        for m in metrics:
            if m.symbol not in wanted:
                continue
            q = self._quotes_by_symbol.get(m.symbol) or {}
            self.tracker.update(m.symbol, price=m.price,
                                high=q.get("high_price"), low=q.get("low_price"))

    # -- invalidation (Feature 4) -------------------------------------------
    def _register_invalidation(self, m: Metrics, bundle=None, enr=None) -> None:
        """Put an alerted setup on the invalidation watch, once, at alert time.

        The two numbers captured here -- the low of the 1m candle and the VWAP at
        the alert -- are the setup's own structure, and they are only knowable
        now. A later cycle cannot reconstruct what the alert was looking at.
        """
        from .tracking_invalidation import InvalidationState
        if not self.cfg.bool("INVALIDATION_ALERTS", True):
            return
        if m.symbol in self._invalidations:
            return
        alert_low = None
        bars = list(getattr(bundle, "bars_1m", None) or [])
        if not bars and enr is not None:
            bars = list(getattr(enr, "bars_1m", None) or [])
        if bars:
            try:
                last = bars[-1]
                alert_low = min(float(last.get("low") or m.price),
                                float(last.get("close") or m.price))
            except (TypeError, ValueError, AttributeError):
                alert_low = None
        now = self.clock()
        state = InvalidationState(
            symbol=m.symbol, day=to_et().strftime("%Y-%m-%d"),
            alert_ts=now, alert_price=m.price,
            alert_time_low=alert_low, vwap_ref=m.vwap)
        state.bucket = int(now // 300)
        state.last_cum = float(getattr(m, "cum_volume", 0.0) or 0.0)
        # news_ts starts at 0, not now, so the first scan runs immediately: that
        # fetch is what separates "seen at the alert" from "published since",
        # and the alert own headlines are seeded below so they cannot re-fire.
        if enr is not None:
            for item in getattr(enr, "news", None) or ():
                title = str(item.get("title") or "").strip()
                if title:
                    state.seen_news.append(title)
        self._invalidations[m.symbol] = state

    def _negative_catalyst(self, state) -> str:
        """A dilutive headline published since the alert. Once found, once only.

        Checked on the invalidation cadence rather than every poll, and only for
        symbols actually on the watch -- usually none, and when there are a couple
        this is a couple of news calls a minute, which is what the alert path
        already spends.
        """
        from .strategies.news_cause import DILUTION, classify
        if state.fired or state.pending:
            return state.pending
        if self.clock() - state.news_ts < 300:
            return ""
        state.news_ts = self.clock()
        items: list = []
        if self.enricher is not None:
            try:
                items = self.enricher.news(state.symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("invalidation news failed for %s: %s", state.symbol, exc)
                return ""
        for item in items or ():
            title = str(item.get("title") or "").strip()
            if not title or title in state.seen_news:
                continue
            state.seen_news.append(title)
            if classify(title) == DILUTION:
                state.pending = title[:140]
                return state.pending
        return ""

    def _send_abort(self, state, m: Metrics, reason: str, key: str) -> None:
        """Record the abort on the tracker and send exactly one message."""
        from .tracking_invalidation import abort_message
        text = abort_message(m.symbol, m.price, state.alert_price, reason, key, self.clock())
        self._safe_send(text)
        try:
            self.tracker.abort(m.symbol, reason=reason, ts=self.clock())
        except Exception as exc:  # noqa: BLE001
            log.debug("tracker abort write failed for %s: %s", m.symbol, exc)
        state.fired = True
        self._invalidations.pop(m.symbol, None)

    def _check_invalidations(self, metrics: list[Metrics]) -> None:
        """Re-evaluate every watched setup. First trigger wins; one message each."""
        if not self._invalidations:
            return
        now = self.clock()
        if now - self._last_invalidation_check < self.cfg.int("INVALIDATION_CHECK_SECONDS", 30):
            return
        self._last_invalidation_check = now
        from .tracking_invalidation import check
        spread_max = self.cfg.float("SPREAD_MAX_PCT", 1.5)
        consec = self.cfg.int("INVALIDATION_SPREAD_CONSEC_MIN", 2)
        bucket = int(now // 300)
        for m in metrics:
            state = self._invalidations.get(m.symbol)
            if state is None or state.fired:
                continue
            q = self._quotes_by_symbol.get(m.symbol) or {}

            # A completed 5m bar of volume per symbol, accumulated from the quotes
            # already polled. Tracking reads quotes rather than refetching candles so
            # the TwelveData quota stays untouched, and the same rule holds here.
            cum = float(q.get("cum_volume") or m.cum_volume or 0.0)
            if state.last_cum and cum > state.last_cum:
                state.bucket_vol += cum - state.last_cum
            state.last_cum = cum
            state.bucket_close = m.price
            if bucket > state.bucket:
                state.volumes.append(state.bucket_vol)
                state.bucket_vol = 0.0

            result = check(
                state, price=m.price, now=now, vwap=m.vwap,
                spread_pct=getattr(m, "spread_pct", None),
                halted=bool(q.get("halted") or m.halted),
                negative_catalyst=self._negative_catalyst(state),
                day_high=m.day_high,
                five_min_volume=(state.volumes[-1] if len(state.volumes) >= 2 else None),
                prior_five_min_volume=(state.volumes[-2] if len(state.volumes) >= 2 else None),
                spread_max_pct=spread_max, spread_consec_min=consec,
                five_min_close_below_vwap=self._closed_5m_below_vwap(state, bucket))
            if result is None:
                continue
            key, reason = result
            self._send_abort(state, m, reason, key)

    def _closed_5m_below_vwap(self, state, bucket: int) -> bool:
        """Did a completed 5m bar close below the setup VWAP reference?

        Tracked from the polled prices rather than refetched candles: a bar closes
        when a poll lands in the next five-minute bucket, and that poll price is the
        close. The first bar is ignored -- there is no completed bar yet, and
        treating the alert price as a close would abort every setup seconds after
        alerting it.
        """
        if bucket <= state.bucket:
            return False
        closed = state.bucket_close
        state.bucket = bucket
        if state.bar_count == 0:
            state.bar_count = 1
            return False
        state.bar_count += 1
        return closed is not None and state.vwap_ref is not None and float(closed) < float(state.vwap_ref)

    def _record_daily_close(self, metrics: list[Metrics]) -> None:
        """Accumulate today's OHLC per symbol so prior sessions exist tomorrow.

        This is what makes first_green_day, day3_surge and WORG possible at all.
        They read prior completed sessions, and there is no free source for
        history here, so the system builds its own from the quotes it is already
        polling. Until enough days have accumulated those detectors report
        INSUFFICIENT_DATA rather than guessing.
        """
        day = to_et().strftime("%Y-%m-%d")
        for m in metrics:
            q = self._quotes_by_symbol.get(m.symbol) or {}
            op = q.get("open_price") or m.prev_close
            hi = max(m.day_high or m.price, q.get("high_price") or 0.0) or m.price
            lo = q.get("low_price") or m.price
            try:
                self.db.execute(
                    "INSERT INTO daily_close(symbol, day, open, high, low, close, volume) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol, day) DO UPDATE SET "
                    "high=MAX(daily_close.high, excluded.high), "
                    "low=MIN(daily_close.low, excluded.low), "
                    "close=excluded.close, volume=MAX(daily_close.volume, excluded.volume)",
                    (m.symbol, day, op, hi, lo, m.price, m.cum_volume))
            except Exception as exc:  # noqa: BLE001
                log.debug("daily_close write failed for %s: %s", m.symbol, exc)

    # -- strategy gate -------------------------------------------------------
    def _gate_is_mandatory(self) -> bool:
        """STRATEGY_GATE=0 leaves the old tier path running, for comparison."""
        return self.cfg.bool("STRATEGY_GATE", False)

    def _armed_confirm_enabled(self) -> bool:
        """ARMED_CONFIRM=0 (default) keeps the single-shot alert path.

        With it on, a passing setup is ARMED on the first cycle and only fires
        an alert once a CLOSED 1m bar confirms above the armed level (Spec 3).
        The flag exists so the two-step flow can be compared against the
        one-step flow and rolled back without a code change.
        """
        return self.cfg.bool("ARMED_CONFIRM", False)

    def _armed_chase_pct(self) -> float:
        from .strategies.confirmation import CHASE_PCT
        return self.cfg.float("ARMED_CHASE_PCT", CHASE_PCT)

    def _armed_ttl_s(self) -> float:
        from .strategies.confirmation import ARMED_TTL_S
        return self.cfg.float("ARMED_TTL_S", ARMED_TTL_S)

    def _armed_decision(self, m: Metrics, gate, bundle) -> Optional[str]:
        """Spec 3 Stage 1/2. Returns None to fire now, or a reason to hold.

        Called from `_fire_alert` with ARMED_CONFIRM on. On a fresh setup it
        arms (no fire, reason "armed"); on an armed setup it confirms from the
        last closed 1m bar and either fires (returns None) or holds. It never
        fires twice for one armed setup and never fires without a confirmation.
        """
        from .strategies.confirmation import arm, confirm
        phase = getattr(bundle, "supernova_phase", None)
        existing = self._armed.get(m.symbol)

        if existing is not None:
            # If the phase has turned dangerous, abandon the armed setup.
            if phase not in ("PRE_BREAKOUT", "FRONT_SIDE"):
                self._armed.pop(m.symbol, None)
                return f"disarmed, phase {phase}"
            conf = confirm(existing, bundle, now_ts=self.clock(),
                           chase_pct=self._armed_chase_pct(), ttl_s=self._armed_ttl_s())
            if conf.entered:
                self._armed.pop(m.symbol, None)
                self._note_armed(m, "ENTER", conf.as_dict())
                return None          # confirmed -- let the alert fire
            if conf.state in ("NO_TRADE", "REJECT"):
                self._armed.pop(m.symbol, None)
            self._note_armed(m, conf.state, conf.as_dict())
            return f"{conf.state.lower()}: {conf.reasons[0] if conf.reasons else ''}"

        setup = arm(bundle, gate, phase=phase, now_ts=self.clock())
        if setup is None:
            # Not an armable phase -- with the flag on, a dangerous phase does
            # not fire a fresh long at all.
            return f"phase {phase} not armable"
        self._armed[m.symbol] = setup
        self._note_armed(m, "ARMED", setup.as_dict())
        return f"armed above {setup.trigger:.4f}"

    def _note_armed(self, m: Metrics, state: str, detail: dict) -> None:
        """Record an ARMED/ENTER/NO_TRADE decision for review (Spec 3)."""
        try:
            self.db.execute(
                "INSERT INTO screener_decisions(ts, symbol, reason_code, detail, price) "
                "VALUES(?,?,?,?,?)",
                (self.clock(), m.symbol, f"PHASE_{state}",
                 json.dumps(detail, default=str), m.price))
        except Exception as exc:  # noqa: BLE001
            log.debug("armed decision insert failed: %s", exc)

    def _gate_vetoes(self, m: Metrics, gate, stats: dict) -> bool:
        """Does the mandatory gate stop this alert? Extracted so it can be tested.

        Three rules, in the order they matter:

          - The advisory gate never vetoes. With STRATEGY_GATE=0 the tier path
            is still the filter and the gate is a report.
          - An evaluation that failed is not a gate that passed. Returning
            "no gate to enforce" used to send the alert out ungated, a silent
            bypass of the one filter the system is built around, so this fails
            closed and counts itself. A skipped alert costs an opportunity; a
            bypassed gate costs the premise.
          - `no_strategy` and `no_catalyst` rest on inputs this pass does not
            have (no 1m candles, no news), so they are missing data rather than
            decisions and must not veto: enforcing them would reject every
            candidate before `_fire_alert` could enrich it, muting the system
            instead of filtering it. The rest -- the reality and exhaustion
            blockers, decidable from price and volume alone -- vetoes now, and
            the enriched pass (Spec 1.1: it runs ahead of Alert 1) settles
            what is left.
        """
        if not self._gate_is_mandatory():
            return False
        if gate is None:
            stats["gate_errors"] = stats.get("gate_errors", 0) + 1
            return True
        if not gate.passed and gate.blocked_by not in POLL_UNDECIDABLE:
            self._note_gate_reject(m, gate)
            return True
        return False

    def _run_gate(self, m: Metrics, metrics_list: Optional[list] = None):
        """Evaluate the metrics-only gate for one symbol.

        Returns (gate, bundle). Both are None when the gate is switched off.
        No fetch happens here: the detectors that need bars report
        INSUFFICIENT_DATA and are re-run in `_fire_alert`, where enrichment is
        already being paid for on the detail thread.
        """
        if not self._gate_is_mandatory():
            return None, None
        try:
            gate, bundle = run_gate(m, cfg=self.cfg, now=to_et(),
                                    history=self._daily_history(m.symbol))
        except Exception as exc:  # noqa: BLE001
            # Loud on purpose: with the gate mandatory this error now means
            # alerts are being suppressed (cycle() fails closed), so a quiet
            # warning would look identical to a quiet market.
            log.exception("gate error for %s -- alerts for it are suppressed: %s",
                          m.symbol, exc)
            return None, None
        self._gates[m.symbol] = gate
        return gate, bundle

    def _recheck_gate(self, m: Metrics, gate, bundle, enr):
        """Re-evaluate the gate now that enrichment exists.

        The poll-loop pass sees price, volume and VWAP only. This pass sees the
        candles, news, filings and float that make most of the detectors
        decidable, and it is the one that decides whether the alert stands.

        `bundle` is reused when present so the caller's reference stays live.
        """
        try:
            new_gate, new_bundle = run_gate(
                m, enr=enr, cfg=self.cfg, now=to_et(),
                history=self._daily_history(m.symbol))
        except Exception as exc:  # noqa: BLE001
            log.warning("gate recheck failed for %s: %s", m.symbol, exc)
            return gate, bundle
        self._gates[m.symbol] = new_gate
        if not new_gate.passed:
            self._note_gate_reject(m, new_gate)
            log.info("gate withdrew %s: blocked_by=%s primary=%s", m.symbol,
                     new_gate.blocked_by, new_gate.primary)
        return new_gate, new_bundle

    # -- AI context (Features 6, 8, 9, 10) ------------------------------------
    def _minutes_since_open(self) -> float:
        """Minutes elapsed since 09:30 ET, clamped to the session."""
        now = to_et()
        minutes = now.hour * 60 + now.minute - (9 * 60 + 30)
        return float(max(0, min(390, minutes)))

    def _regime_bucket(self) -> int:
        """-1, 0 or +1 from the market backdrop 1h return.

        The analog feature vector wants a single signed bucket rather than a
        float: "the market was down" and "the market was down 0.8%" should land
        on the same historical neighbors.
        """
        try:
            snapshot = self.market.get()
        except Exception as exc:  # noqa: BLE001
            log.debug("regime read failed: %s", exc)
            return 0
        try:
            change = float(getattr(snapshot, "change_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0
        if change > 0.25:
            return 1
        if change < -0.25:
            return -1
        return 0

    def _sector_floats(self, sector: str) -> dict:
        """symbol -> float for the peers on the movers list in one sector."""
        wanted = str(sector or "").strip().lower()
        if not wanted:
            return {}
        out: dict = {}
        for quote in self.heatmap.rows:
            name = str(quote.get("sector") or "").strip().lower()
            if name != wanted:
                continue
            flt = quote.get("float_shares")
            try:
                if flt and float(flt) > 0:
                    out[quote.get("symbol")] = float(flt)
            except (TypeError, ValueError):
                continue
        return out

    def _analog_pool(self, exclude: Optional[str] = None) -> list[dict]:
        """Closed alerts inside the analog lookback window, oldest first.

        Read from the tracker rather than a separate store: these rows already
        carry the outcome, and a second copy of the same history is a second
        thing that can be stale.
        """
        days = self.cfg.int("ANALOG_LOOKBACK_DAYS", 540)
        cutoff = (to_et() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            rows = self.db.query(
                "SELECT * FROM alert_tracking WHERE day>=? AND status='closed' "
                "AND symbol<>? ORDER BY day ASC",
                (cutoff, str(exclude or '').upper()))
        except Exception as exc:  # noqa: BLE001
            log.debug("analog pool read failed: %s", exc)
            return []
        return [dict(r) for r in rows]

    def _calibration_table(self) -> dict:
        """Per-bucket confidence priors, rebuilt once a day.

        Cached because it is a full-table aggregation and it is read on the alert
        path; rebuilding it per alert would turn one cheap query into a scan for
        every alerted name. Invalidated when the day rolls over.
        """
        day = to_et().strftime("%Y-%m-%d")
        if self._calibration_day == day and self._calibration:
            return self._calibration
        from .context.calibration import build_table
        window = self.cfg.int("CALIBRATION_WINDOW_DAYS", 90)
        rows = self.tracker.recent(window) if self.tracker else []
        self._calibration = build_table(
            rows, window_days=window,
            min_samples=self.cfg.int("CALIBRATION_MIN_SAMPLES", 30))
        self._calibration_day = day
        return self._calibration

    def _prior_for_strategy(self, primary: str) -> Optional[dict]:
        """The strategy's pooled prior, across every AI rating it was given.

        A single bucket on the strategy is what is knowable before the call. It is
        weaker than a rating-keyed bucket and deliberately so: pooling trades some
        resolution for a sample big enough to exist at all, and with 30 alerts in
        the whole system a per-strategy-per-rating table would be four rows of one
        observation each.
        """
        table = self._calibration_table()
        # Key shape is RATING|primary|cause (Spec 2.0 D1), so the strategy is
        # the middle segment; matching on both pipes pools across the cause
        # split, which is what a pre-call prior wants -- the cause is knowable
        # before the call, the rating is not.
        groups = [v for k, v in table.items()
                  if f"|{str(primary or '')}|" in k]
        groups = [g for g in groups if g.get("sufficient")]
        if not groups:
            return None
        alerts = sum(int(g.get("alerts") or 0) for g in groups)
        return {
            "alerts": alerts,
            "sufficient": True,
            "t1_hit_rate": safe_div(
                sum(float(g["t1_hit_rate"]) * int(g["alerts"])
                    for g in groups if g.get("t1_hit_rate") is not None), alerts),
            "stop_hit_rate": safe_div(
                sum(float(g["stop_hit_rate"]) * int(g["alerts"])
                    for g in groups if g.get("stop_hit_rate") is not None), alerts),
        }

    def _build_context(self, m: Metrics, enr=None, gate=None) -> dict:
        """The AI-only context: multi-timeframe, analogs, sector, exit liquidity.

        None of it is rendered into Alert 1 or Alert 2. Three of the four appear
        in the AI DECISION block as facts -- a sampled analog hit rate and an
        exit-time estimate are evidence the reader acts on, not decoration -- but
        the multi-timeframe block stays prompt-only, because five timeframes is
        the thing the lean alert was built to stop printing.
        """
        from .context import analogs as analogs_mod
        from .context import liquidity as liq_mod
        from .context import multi_timeframe as mtf
        from .context.sector_heatmap import sector_line

        context: dict = {}
        render_lines: list[str] = []
        history = self._daily_history(m.symbol) if m.symbol else []

        if self.cfg.bool("MTF_CONTEXT", True):
            week_volume = float(sum(float(r.get("volume") or 0) for r in history[-5:]))
            context["multi_timeframe"] = mtf.build_context(
                daily=history, week_volume=week_volume,
                float_shares=m.float_shares, day_high=m.day_high,
                prev_close=m.prev_close, rise_60=m.rise.get(60))

        sector = str(getattr(enr, "sector", "") or "")
        if sector:
            line = sector_line(self.heatmap, sector, m.float_shares,
                               self._sector_floats(sector))
            if line:
                context["sector"] = line
                context["sector_hot"] = True
                render_lines.append(line)

        bars = list(getattr(enr, "bars_1m", None) or []) if enr is not None else []
        est = liq_mod.estimate(bars, m.price)
        if est:
            context["exit_liquidity"] = est
            line = liq_mod.line(est)
            if line:
                render_lines.append(line)

        pool = self._analog_pool(m.symbol)
        if pool:
            target = analogs_mod.features_of({
                "float_shares": m.float_shares,
                "pct_vs_close": (m.pct_vs_close or 0.0) * 100.0,
                "volx": m.volx.get(5, 0.0),
                "minutes_since_open": self._minutes_since_open(),
                "regime": self._regime_bucket(),
            })
            if target:
                neighbors, _d = analogs_mod.find_neighbors(
                    target, pool, k=self.cfg.int("ANALOG_K", 20))
                summary = analogs_mod.summarize(
                    neighbors, min_k=self.cfg.int("ANALOG_MIN_K", 8))
                if summary:
                    context["analogs"] = {"summary": summary}
                    render_lines.append(analogs_mod.analog_line(summary))

        # The rating is not known until after the call, so the prior is looked up
        # on the strategy alone. The rating-keyed clamp still happens: it runs in
        # decision.py against the same table, after the rating exists.
        primary = getattr(gate, "primary", None) if gate is not None else None
        if primary:
            prior = self._prior_for_strategy(primary)
            if prior:
                context["calibration_prior"] = prior
                context["calibration_label"] = primary
                context["calibration_margin"] = self.cfg.float(
                    "CALIBRATION_CLAMP_MARGIN", 0.15)

        if render_lines:
            context["render_lines"] = render_lines
        return context

    def _daily_history(self, symbol: str) -> list[dict]:
        """Prior completed sessions for a symbol, oldest first.

        Empty until `daily_close` has been populated, which happens as the
        system runs. Detectors that need it say INSUFFICIENT_DATA rather than
        guessing; they will start working on their own once there is history.
        """
        if not symbol:
            return []
        try:
            rows = self.db.query(
                "SELECT day, open, high, low, close, volume FROM daily_close "
                "WHERE symbol=? AND day < ? ORDER BY day ASC",
                (symbol.upper(), to_et().strftime("%Y-%m-%d")))
        except Exception as exc:  # noqa: BLE001
            log.debug("daily history %s failed: %s", symbol, exc)
            return []
        return [dict(r) for r in rows if r["close"] is not None]

    def _note_gate_reject(self, m: Metrics, gate) -> None:
        """Record why a stock was held back, so `/builds` and `/stats` can say so.

        The rejection is kept even though nothing was sent: a setup that was
        rejected for `no_catalyst` yesterday and passes today is a better signal
        than the alert itself.
        """
        if gate is None:
            return
        self._gate_rejects[m.symbol] = {
            "ts": self.clock(),
            "blocked_by": gate.blocked_by,
            "primary": gate.primary,
            "score": round(gate.gate_score, 1),
            "price": m.price,
            # Flat list of the failed conditions, capped at four. It reads like
            # `[f for r in ... if r.failed_on]`, which named `f` without ever
            # binding it and raised NameError on the first rejection -- so the
            # record that was supposed to explain a held-back setup was itself
            # taking the cycle down. Strings keep the row reportable.
            "failed_on": [f for r in gate.strategies for f in r.failed_on][:4],
        }

        # Spec 1.3: an SEC veto is a fail-closed decision and is logged with its
        # reason code for `/builds`-style review, even though nothing was sent.
        if gate.blocked_by and str(gate.blocked_by).startswith("SEC_"):
            try:
                self.db.execute(
                    "INSERT INTO screener_decisions(ts, symbol, reason_code, "
                    "detail, price) VALUES(?,?,?,?,?)",
                    (self.clock(), m.symbol, gate.blocked_by,
                     gate.primary, m.price))
            except Exception as exc:  # noqa: BLE001
                log.debug("screener_decisions insert failed: %s", exc)

    def _maybe_build_feed(self, session: str, metrics: list[Metrics]) -> None:
        """Surface quiet accumulations the alert rules stay silent on.

        The build *list* is always refreshed so `/builds` stays live; only the
        periodic Telegram notification is optional. It was on by default and read
        as noise, so it is now off unless asked for.
        """
        try:
            builds = self.scorer.build_candidates(metrics)
        except Exception as exc:  # noqa: BLE001
            log.debug("build scan failed: %s", exc)
            return
        self._builds = builds
        if not self.cfg.bool("BUILD_FEED", False):
            return
        if session not in ("pre", "regular", "post"):
            return
        if not builds:
            # Forget the last set so a build that reappears is announced again.
            self._last_build_ids = set()
            return

        now = self.clock()
        every = max(60, self.cfg.int("BUILD_FEED_SECONDS", 900))
        current = {b.metrics.symbol for b in builds[:self.cfg.int("BUILD_TOP_N", 6)]}
        fresh = current - self._last_build_ids
        due = now - self._last_build_ts >= every
        # Always speak up for a brand-new build; otherwise keep the cadence and
        # only repeat when the set of names changed.
        if not fresh and not (due and current != self._last_build_ids):
            return
        self._last_build_ts = now
        self._last_build_ids = current
        note = format_build_digest(builds, seconds=every,
                                   limit=self.cfg.int("BUILD_TOP_N", 6), html=True)
        if note:
            self._safe_send(note)

    def _maybe_digest(self, session: str, metrics: list[Metrics]) -> None:
        if session not in ("pre", "regular", "post") or not metrics:
            return
        key = f"{to_et().strftime('%Y-%m-%d')}:{session}"
        if self._last_digest_key == key:
            return
        self._last_digest_key = key
        ranked = self.scorer.rank(metrics)
        lines = [f"DIGEST {session.upper()} {to_et().strftime('%H:%M')} ET"]
        if ranked["champions"]:
            lines.append("Champions:")
            for m in ranked["champions"]:
                lines.append(f"  {m.symbol} {money(m.price)} "
                             f"{pct((m.pct_vs_close or 0) * 100)} score {m.score:.0f} "
                             f"tier {tier_name(m.tier)}")
        if ranked["hidden_gems"]:
            lines.append("Hidden gems:")
            for m in ranked["hidden_gems"]:
                lines.append(f"  {m.symbol} {money(m.price)} "
                             f"{pct((m.pct_vs_close or 0) * 100)} score {m.score:.0f}")
        if len(lines) > 1:
            self._safe_send("\n".join(lines))

    def _maybe_end_of_day(self) -> None:
        if self.forced_session is not None:
            return
        now = to_et()
        day = now.strftime("%Y-%m-%d")
        if now.hour != 20 or now.minute < 5 or self._last_eod_day == day:
            return
        self._last_eod_day = day
        rows = self.db.query("SELECT * FROM daily WHERE day=?", (day,))
        winners = 0
        for row in rows:
            if (row["max_tier"] or 0) >= TIER_EARLY:
                label = "winner" if (row["peak_pct"] or 0) >= 50 else "loser"
                if label == "winner":
                    winners += 1
                self.journal.execute(
                    "INSERT INTO events(symbol, date, label, peak_pct, source, fetched) "
                    "VALUES(?,?,?,?,?,0) ON CONFLICT(symbol, date) DO NOTHING",
                    (row["symbol"], day, label, row["peak_pct"], "auto"))
        alerts_today = self.db.scalar("SELECT COUNT(*) FROM alerts WHERE ts>=?",
                                      (self.clock() - 86400,), default=0)
        last_backup = self.db.scalar("SELECT MAX(ts) FROM backups", default=None)
        backup_txt = f"{age_str(self.clock() - last_backup)} ago" if last_backup else "none"

        # Close the tracked alerts first, so the report counts them correctly.
        self.tracker.close_day(day)

        if self.cfg.bool("EOD_REPORT_ENABLED", True):
            # ONE message a day. Per-alert result messages train the reader to
            # ignore results; the day's alerts are better judged as a set.
            self._safe_send(eod_report(self.tracker, day))
        else:
            self._safe_send(
                f"END OF DAY {day}\n"
                f"alerts: {alerts_today} | tier>=2 stocks: {len(rows)} | "
                f"winners: {winners}\n"
                f"last backup: {backup_txt}")

    def _record_daily(self, m: Metrics) -> None:
        day = to_et().strftime("%Y-%m-%d")
        peak = (m.pct_vs_close or 0) * 100
        row = self.db.query_one("SELECT * FROM daily WHERE day=? AND symbol=?", (day, m.symbol))
        if row is None:
            self.db.execute(
                "INSERT INTO daily(day, symbol, max_tier, peak_pct, first_ts, first_price, "
                "first_tier) VALUES(?,?,?,?,?,?,?)",
                (day, m.symbol, m.tier, peak, self.clock(), m.price, m.tier))
        else:
            self.db.execute(
                "UPDATE daily SET max_tier=MAX(max_tier,?), peak_pct=MAX(peak_pct,?) "
                "WHERE day=? AND symbol=?", (m.tier, peak, day, m.symbol))

    # -- backups ------------------------------------------------------------
    def _maybe_backups(self) -> None:
        if self.backup is None or self.forced_session is not None:
            return
        now = to_et()
        if now.hour == 20 and now.minute >= 30 and self._last_full_backup != now.timetz().replace(
                hour=20, minute=0, second=0, microsecond=0):
            pass
        day = now.strftime("%Y-%m-%d")
        if now.hour == 20 and now.minute >= 30 and self._last_full_backup != day:
            self._last_full_backup = day
            try:
                self.backup.run("daily")
            except Exception as exc:  # noqa: BLE001
                log.warning("daily backup failed: %s", exc)
        elif self.cfg.bool("BACKUP_HOURLY", True) and self._in_session():
            if self.clock() - self._last_hourly_backup >= 3600:
                self._last_hourly_backup = self.clock()
                try:
                    self.backup.run("hourly")
                except Exception as exc:  # noqa: BLE001
                    log.warning("hourly backup failed: %s", exc)

    # -- records ------------------------------------------------------------
    def _record_cycle(self, stats: dict) -> None:
        rss = 0.0
        try:
            import psutil
            rss = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
        api_calls = sum(getattr(src, "api_calls", 0) for src in self._sources())
        self.db.execute("INSERT OR REPLACE INTO cycles(ts, ms, quotes, rss_mb, api_calls, alerts) "
                        "VALUES(?,?,?,?,?,?)",
                        (self.clock(), stats.get("ms"), stats.get("quotes", 0), rss,
                         api_calls, stats.get("alerts", 0)))
        self._write_sizing(stats, rss, api_calls)

    def _sources(self) -> list:
        return [s for s in (self.finviz, self.alpaca, self.finnhub, self.sec,
                            self.finra, self.yahoo) if s is not None]

    def _write_sizing(self, stats: dict, rss: float, api_calls: int) -> None:
        if self._sizing_fh is None:
            path = self.cfg.data_path("sizing.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            self._sizing_fh = open(path, "a", encoding="utf-8")
        record = {"ts": self.clock(), "session": stats.get("session"), "ms": stats.get("ms"),
                  "symbols": stats.get("symbols"), "quotes": stats.get("quotes"),
                  "rss_mb": round(rss, 1), "api_calls": api_calls,
                  "alerts": stats.get("alerts", 0)}
        self._sizing_fh.write(json.dumps(record) + "\n")
        self._sizing_fh.flush()

    # -- forecasts ----------------------------------------------------------
    def _actual_price(self, symbol: str, due_ts: float) -> Optional[float]:
        st = self.scorer.states.get(symbol)
        if st and st.samples:
            best = None
            for s in st.samples:
                if s.ts <= due_ts + 120:
                    best = s.price
                else:
                    break
            if best:
                return best
        return None

    # -- shariah ------------------------------------------------------------
    def _shariah_status(self, symbol: str, filings=None, fetch: bool = True) -> dict:
        if not self.shariah or not self.shariah.enabled:
            return {"symbol": symbol, "combined": "UNKNOWN", "sources": {}}
        try:
            return self.shariah.status(symbol, filings, fetch)
        except Exception as exc:  # noqa: BLE001
            log.debug("shariah lookup failed for %s: %s", symbol, exc)
            return {"symbol": symbol, "combined": "UNKNOWN", "sources": {}}

    def _store_alert_shariah(self, alert_id: int, status: dict) -> None:
        self.db.execute("UPDATE alerts SET shariah=? WHERE id=?",
                        (status.get("combined"), alert_id))

    # -- helpers ------------------------------------------------------------
    def _safe_send(self, text: str) -> None:
        try:
            self.telegram.send(text)
        except Exception as exc:  # noqa: BLE001
            log.debug("telegram send failed: %s", exc)

    def status_text(self) -> str:
        s = self._last_cycle or {}
        rss = 0.0
        try:
            import psutil
            rss = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
        decisions = self.db.scalar(
            "SELECT COUNT(*) FROM ai_calls WHERE kind='decision'", default=0)
        rejected = self.db.scalar(
            "SELECT COUNT(*) FROM ai_calls WHERE kind='decision' AND ok=0", default=0)
        gate_on = self.cfg.bool("STRATEGY_GATE", False)
        gate_state = ("on" if gate_on else "off")
        last_backup = self.db.scalar("SELECT MAX(ts) FROM backups", default=None)
        backup_txt = f"{age_str(self.clock() - last_backup)} ago" if last_backup else "none"
        return (
            f"STATUS\n"
            f"uptime {age_str(self.clock() - self.started_at)} | session {session_for()} | "
            f"trading day {is_trading_day()}\n"
            f"symbols {s.get('symbols', 0)} | cycle {s.get('ms', 0):.0f} ms | "
            f"quotes {s.get('quotes', 0)} | memory {rss:.0f} MB\n"
            f"alerts this cycle {s.get('alerts', 0)} | strategy gate {gate_state}"
            # A fail-closed gate that cannot be evaluated drops alerts on
            # purpose; say so rather than let the bot look like a quiet morning.
            + (f" | gate errors {s['gate_errors']}" if s.get('gate_errors') else "")
            + f"\n"
            f"decisions {decisions} (rejected {rejected}) | last backup: {backup_txt}")

    def run_forever(self, poll: Optional[float] = None) -> None:
        self.running = True
        self._safe_send("penny early-warning online.")
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except Exception as exc:  # noqa: BLE001
                log.exception("cycle failed: %s", exc)
            elapsed = time.monotonic() - started
            interval = poll or self.cycle_interval()
            self._stop.wait(max(0.5, interval - elapsed))
        self.running = False
        if self._sizing_fh:
            self._sizing_fh.close()
            self._sizing_fh = None
