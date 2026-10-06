"""The live engine: poll loop, alert rules, digests, tracking, end of day (6.3-6.15)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .ai.evaluate import evaluate_due, store_predictions
from .ai.panel import AggregateForecast, ModelForecast, price_forecast_text, reaction_text
from .alerts import (build_ai_facts, format_alert1, format_alert2, format_build_digest,
                     tier_name)
from .enrich import Enricher
from .fib import reentry_guidance
from .market_context import MarketContextProvider
from .scoring import (PHASE_EXTENDED, PHASE_FADING, TIER_EARLY, Metrics, Scorer)
from .sources.finviz import FinvizTokenRejected, row_get, row_num
from .ui import button, cb, inline
from .util import (SESSION_CLOSED, age_str, is_trading_day, money, pct, session_end,
                   session_for, to_et)

log = logging.getLogger(__name__)

EXCLUDED_SUFFIXES = {"W", "WS", "WT", "U", "UN", "R", "RT", "P", "PR", "A", "B", "C"}
SPLIT_ARTIFACT_PCT = 400.0


def alert_menu(symbol: str) -> dict:
    """Inline buttons under an alert so a symbol is one tap away."""
    return inline([
        [button(f"\U0001F504 Re-check", cb("check", symbol)),
         button("\U0001F54C Halal", cb("halal", symbol)),
         button("\U0001F3E0 Menu", cb("menu"))],
    ])


class Engine:
    """Discover movers, sweep the universe, score, alert, enrich, forecast."""

    def __init__(self, cfg, db, journal, telegram, *, finviz=None,
                 alpaca=None, finnhub=None, sec=None, finra=None, yahoo=None,
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
        self.gateway = gateway
        self.catalog = catalog
        self.shariah = shariah
        self.backup = backup
        self.clock = clock or time.time

        self.scorer = Scorer(cfg, clock=self.clock)
        self.market = MarketContextProvider(cfg, finviz=finviz, yahoo=yahoo)
        self.enricher = Enricher(cfg, finviz=finviz, alpaca=alpaca,
                                 finnhub=finnhub, sec=sec, finra=finra, yahoo=yahoo,
                                 market=self.market)
        self.runner = None
        if gateway is not None and catalog is not None:
            from .ai.runner import AIRunner
            self.runner = AIRunner(cfg, gateway, catalog, db, telegram=telegram,
                                   secrets=cfg.secrets())

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
                    avg = row_num(row, "Avg Volume")
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
            if self._alert_candidate(m, session):
                self._fire_alert(m, session, pct_rank)
                stats["alerts"] += 1

        self._track_watchlist(metrics_list, session)
        self._maybe_build_feed(session, metrics_list)
        self._maybe_digest(session, metrics_list)
        self._evaluate_forecasts()
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
    def _alert_candidate(self, m: Metrics, session: str) -> bool:
        min_tier = max(TIER_EARLY, self.cfg.int("ALERT_MIN_TIER", 2))
        if m.tier < min_tier or not self.scorer.warm:
            return False
        st = self.scorer.state(m.symbol)
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

    def _possible_split_artifact(self, m: Metrics) -> bool:
        if m.pct_vs_close is None or m.pct_vs_close * 100 < SPLIT_ARTIFACT_PCT:
            return False
        cached = self._split_cache.get(m.symbol)
        if cached is not None:
            return cached
        result = False
        if self.sec:
            try:
                for filing in self.sec.filings(m.symbol, days=5):
                    form = str(filing.get("form") or "").upper()
                    if "REVERSE" in str(filing.get("items") or "").upper() or form in ("25",):
                        result = True
                        break
            except Exception:  # noqa: BLE001
                result = False
        self._split_cache[m.symbol] = result
        return result

    def _fire_alert(self, m: Metrics, session: str, pct_rank: dict) -> None:
        st = self.scorer.state(m.symbol)
        rank = pct_rank.get(m.symbol)
        resumed = st.halt_resume_pending
        st.halt_resume_pending = False

        shariah_status = self._shariah_status(m.symbol)
        shariah_line = "Shariah: checking..."
        if self.shariah and self.shariah.enabled and shariah_status.get("combined") != "UNKNOWN":
            shariah_line = self.shariah.one_line(shariah_status)

        text1 = format_alert1(m, shariah_line=shariah_line, pct_rank=rank, resumed=resumed,
                              html=True)
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
        log.info("ALERT %s tier %d score %.1f", m.symbol, m.tier, m.score)

        threading.Thread(target=self._alert_detail_worker,
                         args=(m, alert_id, msg_id, session, rank, shariah_status),
                         daemon=True, name=f"detail-{m.symbol}").start()

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
                             rank, shariah_status) -> None:
        try:
            self._send_detail(m, alert_id, msg_id, session, rank)
        except Exception as exc:  # noqa: BLE001
            log.exception("detail worker failed for %s: %s", m.symbol, exc)

    def _send_detail(self, m: Metrics, alert_id: int, msg_id, session: str, rank) -> None:
        enr = self.enricher.enrich(m.symbol, self._quote_for(m), m)
        market = self.market.get()

        shariah = self._shariah_status(m.symbol, filings=enr.filings, fetch=True)
        shariah_lines = self.shariah.detail_lines(shariah) if self.shariah else []
        if msg_id and self.shariah and shariah.get("combined") != "UNKNOWN":
            try:
                self.telegram.edit(msg_id, format_alert1(
                    m, shariah_line=self.shariah.one_line(shariah), pct_rank=rank,
                    html=True), markup=alert_menu(m.symbol), html=True)
            except Exception:  # noqa: BLE001
                pass
        if alert_id and shariah.get("combined"):
            self._store_alert_shariah(alert_id, shariah)

        risk_usd = self.cfg.float("RISK_USD", 50)
        facts = build_ai_facts(m, enr, market, session=session, pct_rank=rank,
                               time_to_horizon=self._time_to_horizons())
        detail_id = None
        try:
            detail_id = self.telegram.send(format_alert2(
                m, enr, market, shariah_lines=shariah_lines, ai_running=True,
                risk_usd=risk_usd, html=True), markup=alert_menu(m.symbol), html=True)
        except Exception as exc:  # noqa: BLE001
            log.error("alert 2 send failed: %s", exc)

        agg = AggregateForecast()
        forecasts: list[ModelForecast] = []
        if self.runner is not None:
            try:
                agg, forecasts = self.runner.analyze(
                    symbol=m.symbol, facts=facts, price=m.price, tier=m.tier,
                    news_key=enr.news_key(), kind="alert")
                shadow = self.runner.shadow(symbol=m.symbol, facts=facts,
                                            exclude=set(agg.models))
                forecasts.extend(shadow)
            except Exception as exc:  # noqa: BLE001
                log.warning("AI analysis failed for %s: %s", m.symbol, exc)

        reaction = reaction_text(agg)
        price_fc = price_forecast_text(agg, m.price)
        text2 = format_alert2(m, enr, market, shariah_lines=shariah_lines, reaction=reaction,
                              price_forecast=price_fc, ai_running=False, agg=agg,
                              validated=agg.validated, risk_usd=risk_usd, html=True)
        if detail_id:
            try:
                self.telegram.edit(detail_id, text2, markup=alert_menu(m.symbol), html=True)
            except Exception as exc:  # noqa: BLE001
                log.debug("alert 2 edit failed: %s", exc)
        else:
            self._safe_send(text2)

        if alert_id:
            self.db.execute("UPDATE alerts SET alert2_id=? WHERE id=?", (detail_id, alert_id))
            self._store_predictions(alert_id, m, session, forecasts, agg, enr)

    def _store_predictions(self, alert_id: int, m: Metrics, session: str,
                           forecasts: list[ModelForecast], agg: AggregateForecast,
                           enr) -> None:
        if not forecasts:
            return
        st = self.scorer.state(m.symbol)
        try:
            store_predictions(
                self.db, alert_id=alert_id, symbol=m.symbol, ts=self.clock(), session=session,
                price0=m.price, forecasts=forecasts, aggregate=agg,
                active_models=set(agg.models), rise15=m.rise.get(15, 0.0),
                low4h=st.day_low if st.day_low else None)
        except Exception as exc:  # noqa: BLE001
            log.warning("storing predictions failed for %s: %s", m.symbol, exc)

    def _quote_for(self, m: Metrics) -> dict:
        quote = self._quotes_by_symbol.get(m.symbol)
        if quote:
            return quote
        return {"symbol": m.symbol, "price": m.price, "cum_volume": m.cum_volume,
                "prev_close": m.prev_close, "float_shares": m.float_shares,
                "market_cap": m.market_cap, "ts": m.ts, "halted": m.halted}

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
        if self.runner is not None and self.cfg.bool("TRACKER_AI", True):
            comment = self._tracker_comment(m, watch)
            if comment:
                lines.append(comment)
        return "\n".join(lines)

    def _tracker_comment(self, m: Metrics, watch) -> str:
        return ""

    # -- digests / end of day ----------------------------------------------
    def _maybe_build_feed(self, session: str, metrics: list[Metrics]) -> None:
        """Surface quiet accumulations the alert rules stay silent on.

        A gainers screen only shows a stock after it has moved, and the alert bar
        is tier 2. This keeps the tier-1 builds on the radar with a throttled,
        low-noise digest of *new* names (so it does not repeat every cycle).
        """
        if not self.cfg.bool("BUILD_FEED", True) or session not in ("pre", "regular", "post"):
            return
        try:
            builds = self.scorer.build_candidates(metrics)
        except Exception as exc:  # noqa: BLE001
            log.debug("build scan failed: %s", exc)
            return
        self._builds = builds
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
        tokens = self.runner.tokens_today() if self.runner else 0
        weighted = self.runner.weighted_tokens_today() if self.runner else 0
        last_backup = self.db.scalar("SELECT MAX(ts) FROM backups", default=None)
        backup_txt = f"{age_str(self.clock() - last_backup)} ago" if last_backup else "none"
        self._safe_send(
            f"END OF DAY {day}\n"
            f"alerts: {alerts_today} | tier>=2 stocks: {len(rows)} | winners: {winners}\n"
            f"tokens: {tokens:,} (weighted {weighted:,}) | last backup: {backup_txt}")
        self._evaluate_forecasts()
        try:
            from .ai.leaderboard import staged_elimination
            if self.runner:
                staged_elimination(self.db, self.cfg, self.catalog)
        except Exception as exc:  # noqa: BLE001
            log.debug("staged elimination failed: %s", exc)

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

    def _evaluate_forecasts(self) -> None:
        try:
            evaluate_due(self.db, self._actual_price, now=self.clock())
        except Exception as exc:  # noqa: BLE001
            log.debug("forecast evaluation failed: %s", exc)

    # -- shariah ------------------------------------------------------------
    def _shariah_status(self, symbol: str, filings=None, fetch: bool = True) -> dict:
        if not self.shariah or not self.shariah.enabled:
            return {"symbol": symbol, "combined": "UNKNOWN", "sources": {}}
        try:
            return self.shariah.status(symbol, filings, True)
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
        tokens = self.runner.tokens_today() if self.runner else 0
        weighted = self.runner.weighted_tokens_today() if self.runner else 0
        hit = f"{self.runner.cache.hit_rate:.0%}" if self.runner else "n/a"
        last_backup = self.db.scalar("SELECT MAX(ts) FROM backups", default=None)
        panel = ", ".join(self.runner.active_panel()) if self.runner else "n/a"
        mult = (sum(self.catalog.multiplier_of(m) for m in self.runner.active_panel())
                if self.runner and self.catalog else 0.0)
        backup_txt = f"{age_str(self.clock() - last_backup)} ago" if last_backup else "none"
        return (
            f"STATUS\n"
            f"uptime {age_str(self.clock() - self.started_at)} | session {session_for()} | "
            f"trading day {is_trading_day()}\n"
            f"symbols {s.get('symbols', 0)} | cycle {s.get('ms', 0):.0f} ms | "
            f"quotes {s.get('quotes', 0)} | memory {rss:.0f} MB\n"
            f"alerts this cycle {s.get('alerts', 0)} | AI cache hit {hit}\n"
            f"tokens today {tokens:,} (weighted {weighted:,}) | panel {panel} ({mult:.2f}x)\n"
            f"last backup: {backup_txt}")

    def run_forever(self, poll: Optional[float] = None) -> None:
        self.running = True
        self._safe_send("penny early-warning online.")
        if self.runner and getattr(self.gateway, "configured", True):
            try:
                self.runner.discovery(self.telegram)
            except Exception as exc:  # noqa: BLE001
                log.warning("model discovery failed: %s", exc)
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
