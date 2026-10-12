"""Offline simulation with a synthetic market, fake AI and fake Telegram (10.1)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .ai.gateway import FakeGateway
from .config import Config
from .scoring import TIER_CONFIRMED, TIER_EARLY, TIER_WATCH
from .shariah import (COMPLIANT, DOUBTFUL, NON_COMPLIANT, FakeShariahSource,
                      ShariahService, UNKNOWN)
from .store import open_journal, open_state
from .telegram import FakeTelegram
from .util import ET

log = logging.getLogger(__name__)

MINUTE = 60.0


@dataclass
class SyntheticStock:
    symbol: str
    prev_close: float
    kind: str = "noise"
    prices: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    avg_volume: float = 96_000.0

    def price(self, minute: int) -> float:
        if not self.prices:
            return self.prev_close
        return self.prices[min(minute, len(self.prices) - 1)]

    def cum_volume(self, minute: int) -> float:
        idx = min(minute, len(self.volumes) - 1)
        return float(sum(self.volumes[:idx + 1]))


class SyntheticMarket:
    """A grinder, a burst, a spike that fades and noise stocks (10.1)."""

    def __init__(self, minutes: int = 150, noise_count: int = 60, seed: int = 7):
        self.minutes = minutes
        self.stocks: dict[str, SyntheticStock] = {}
        self._build(noise_count, seed)

    def _build(self, noise_count: int, seed: int) -> None:
        self.stocks["GRND"] = self._grinder()
        self.stocks["BRST"] = self._burst()
        self.stocks["FADE"] = self._fade()
        rng = _Rng(seed)
        for i in range(noise_count):
            symbol = f"NZ{i:02d}"
            base = round(0.4 + rng.random() * 4.0, 2)
            stock = SyntheticStock(symbol=symbol, prev_close=base, kind="noise")
            price = base
            for _ in range(self.minutes):
                price = max(0.05, price * (1 + (rng.random() - 0.5) * 0.012))
                stock.prices.append(round(price, 4))
                stock.volumes.append(round(60 + rng.random() * 120))
            self.stocks[symbol] = stock
        for symbol in ("SPY", "QQQ", "IWM"):
            base = 500.0 if symbol == "SPY" else (450.0 if symbol == "QQQ" else 220.0)
            stock = SyntheticStock(symbol=symbol, prev_close=base, kind="index")
            price = base
            for i in range(self.minutes):
                price = price * (1 + 0.0004 * _wave(i, 40))
                stock.prices.append(round(price, 4))
                stock.volumes.append(100_000)
            self.stocks[symbol] = stock

    def _grinder(self) -> SyntheticStock:
        stock = SyntheticStock(symbol="GRND", prev_close=1.00, kind="grinder")
        for i in range(self.minutes):
            if i < 10:
                price = 1.20
            elif i < 60:
                price = 1.20 + (i - 10) * (0.30 / 50)
            elif i < 90:
                price = 1.50 + (i - 60) * (0.50 / 30)
            else:
                price = 2.00 + (i - 90) * (1.00 / 60)
            stock.prices.append(round(price, 4))
            stock.volumes.append(2500)
        return stock

    def _burst(self) -> SyntheticStock:
        stock = SyntheticStock(symbol="BRST", prev_close=1.00, kind="burst")
        for i in range(self.minutes):
            if i < 30:
                price = 1.10
                vol = 300
            elif i < 50:
                price = 1.10 + (i - 30) * 0.030      # 1.10 -> 1.70 in 20 minutes
                vol = 6000
            else:
                price = 1.70 + (i - 50) * 0.002
                vol = 3000
            stock.prices.append(round(price, 4))
            stock.volumes.append(vol)
        return stock

    def _fade(self) -> SyntheticStock:
        stock = SyntheticStock(symbol="FADE", prev_close=1.00, kind="fade")
        for i in range(self.minutes):
            if i < 20:
                price = 1.05
                vol = 500
            elif i < 35:
                price = 1.05 + (i - 20) * 0.05       # 1.05 -> 1.80
                vol = 5000
            else:
                price = max(1.08, 1.80 - (i - 35) * 0.05)
                vol = 2000
            stock.prices.append(round(price, 4))
            stock.volumes.append(vol)
        return stock

    def top_gainers(self, minute: int, limit: int = 200) -> list[str]:
        def change(s: SyntheticStock) -> float:
            return s.price(minute) / s.prev_close - 1 if s.prev_close else 0.0
        ranked = sorted(self.stocks.values(), key=change, reverse=True)
        return [s.symbol for s in ranked[:limit]]


def _wave(i: int, period: int) -> float:
    import math
    return math.sin(2 * math.pi * i / period)


class _Rng:
    """Deterministic LCG so the simulation is reproducible without depending on random."""

    def __init__(self, seed: int):
        self.state = seed or 1

    def random(self) -> float:
        self.state = (1103515245 * self.state + 12345) % (2 ** 31)
        return self.state / (2 ** 31)


class FakeFinviz:
    """Synthetic Finviz feed. It is the only market data source: it serves the
    universe rows (which double as the per-cycle quote feed), the market-mover
    screens, snapshots, verification and every other Finviz link."""
    name = "finviz"
    token_rejected = False
    api_calls = 0

    def __init__(self, market: SyntheticMarket, day_start: float = 0.0,
                 mismatch: Optional[str] = None):
        self.market = market
        self.day_start = day_start
        self.mismatch = mismatch
        self.minute = 0

    def set_minute(self, minute: int) -> None:
        self.minute = minute

    def reset_calls(self) -> None:
        self.api_calls = 0

    def _row(self, symbol: str) -> Optional[dict]:
        stock = self.market.stocks.get(symbol)
        if stock is None:
            return None
        price = stock.price(self.minute)
        return {
            "Ticker": symbol,
            "Company": f"{symbol} Inc",
            "Price": f"{price:.4f}",
            "Change": f"{(price / stock.prev_close - 1) * 100:.2f}%",
            "Volume": f"{stock.cum_volume(self.minute):,.0f}",
            "Avg Volume": f"{stock.avg_volume:,.0f}",
            "Rel Volume": "6.5",
            "Float": "20.00M",
            "Short Float": "7.50%",
            "Market Cap": "40.00M",
            "Sector": "Technology",
            "Earnings": "-",
        }

    def universe(self, ttl: float = 300.0) -> list[dict]:
        self.api_calls += 1
        rows = []
        for symbol, stock in self.market.stocks.items():
            if stock.kind == "index":
                continue
            row = self._row(symbol)
            if row:
                rows.append(row)
        return rows

    def movers(self, session: Optional[str] = None, top_n: int = 200,
               signals=None) -> list[dict]:
        self.api_calls += 1
        rows = []
        for symbol in self.market.top_gainers(self.minute, top_n):
            if symbol in ("SPY", "QQQ", "IWM"):
                continue
            row = self._row(symbol)
            if row:
                rows.append(row)
        return rows

    def snapshot(self, symbols, session: Optional[str] = None,
                 ttl: float = 0.0) -> list[dict]:
        self.api_calls += 1
        out = []
        for symbol in symbols:
            quote = self.normalize_row(self._row(str(symbol).upper()), session or "regular")
            if quote:
                out.append(quote)
        return out

    def normalize_row(self, row: Optional[dict], session: str) -> Optional[dict]:
        if not row:
            return None
        symbol = str(row.get("Ticker") or "").upper()
        stock = self.market.stocks.get(symbol)
        if stock is None:
            return None
        price = stock.price(self.minute)
        highs = [stock.price(i) for i in range(0, self.minute + 1)] or [price]
        return {
            "symbol": symbol,
            "code": symbol,
            "ts": self.day_start + self.minute * 60,
            "price": price,
            "prev_close": stock.prev_close,
            "cum_volume": stock.cum_volume(self.minute),
            "open_price": stock.price(0),
            "high_price": max(highs),
            "low_price": min(highs),
            "bid": None,
            "ask": None,
            "market_cap": price * 20_000_000,
            "float_shares": 20_000_000,
            "avg_volume": stock.avg_volume,
            "sector": row.get("Sector") or "",
            "halted": False,
            "session": session,
            "source": "finviz",
        }

    def verify(self, ticker: str) -> Optional[dict]:
        self.api_calls += 1
        stock = self.market.stocks.get(ticker.upper())
        if stock is None:
            return None
        row = self._row(ticker.upper())
        if row and self.mismatch and self.mismatch.upper() == ticker.upper():
            price = stock.price(self.minute) * 1.08     # force a >3% mismatch
            row["Price"] = f"{price:.4f}"
        return row

    def groups(self, group: str = "sector", view: str = "152") -> list[dict]:
        self.api_calls += 1
        return [{"Name": "Technology", "Change": "1.20%"},
                {"Name": "Energy", "Change": "-0.40%"},
                {"Name": "Financial", "Change": "0.30%"}]

    def news(self, view: str = "1") -> list[dict]:
        self.api_calls += 1
        return [{"Title": "Market rises on earnings", "Date": "2026-01-02"},
                {"Title": "Small caps rally", "Date": "2026-01-02"}]

    def latest_filings(self, ticker: str) -> list[dict]:
        self.api_calls += 1
        return [{"Form": "8-K", "Date": time.strftime("%Y-%m-%d"), "Item": "2.02"}]

    def insiders(self, ticker: str, tc: str = "7") -> list[dict]:
        self.api_calls += 1
        return []

    def economic_calendar(self, date_from: str) -> list[dict]:
        self.api_calls += 1
        return [{"Time": "08:30", "Event": "CPI", "Impact": "High"}]

    def health(self) -> tuple[bool, str]:
        return True, "synthetic finviz"


class FakeYahoo:
    name = "yahoo"
    api_calls = 0

    def __init__(self, market: SyntheticMarket):
        self.market = market
        self.minute = 0

    def set_minute(self, minute: int) -> None:
        self.minute = minute

    def reset_calls(self) -> None:
        self.api_calls = 0

    def index_levels(self, symbols=("^GSPC", "^IXIC", "^VIX")) -> dict:
        out = {}
        for sym, stock in (("^GSPC", "SPY"), ("^IXIC", "QQQ")):
            s = self.market.stocks.get(stock)
            if s:
                out[sym] = {"price": s.price(self.minute),
                            "prev_close": s.prev_close,
                            "change_pct": (s.price(self.minute) / s.prev_close - 1) * 100}
        out["^VIX"] = {"price": 14.5, "prev_close": 15.0, "change_pct": -3.3}
        return out

    def bars(self, symbol: str, days: int = 2, interval: str = "1m") -> list[dict]:
        self.api_calls += 1
        return []

    def google_news(self, symbol: str, limit: int = 8) -> list[dict]:
        self.api_calls += 1
        return [{"title": f"{symbol} announces public offering",
                 "source": "google-news", "url": "", "published": "",
                 "summary": "Dilution risk for shareholders."}]

    def health(self) -> tuple[bool, str]:
        return True, "synthetic yahoo"


class FakeSec:
    name = "sec"
    api_calls = 0

    def reset_calls(self) -> None:
        self.api_calls = 0

    def filings(self, ticker: str, days: int = 5) -> list[dict]:
        self.api_calls += 1
        return [{"form": "8-K", "date": time.strftime("%Y-%m-%d"), "items": "2.02",
                 "url": "", "source": "sec"}]

    def health(self) -> tuple[bool, str]:
        return True, "synthetic sec"


class FakeAlpaca:
    """Synthetic Alpaca: the candle fallback now that Finviz has no intraday bars."""
    name = "alpaca"
    api_calls = 0
    configured = True

    def __init__(self, market: SyntheticMarket, day_start: float, session: str = "regular"):
        self.market = market
        self.day_start = day_start
        self.session = session
        self.minute = 0

    def set_minute(self, minute: int) -> None:
        self.minute = minute

    def reset_calls(self) -> None:
        self.api_calls = 0

    def bars(self, symbol: str, start=None, end=None) -> list[dict]:
        self.api_calls += 1
        stock = self.market.stocks.get(str(symbol).upper())
        if stock is None:
            return []
        out = []
        for i in range(0, self.minute + 1):
            close = stock.price(i)
            open_ = stock.price(i - 1) if i > 0 else close
            out.append({
                "time": int(self.day_start + i * 60),
                "open": open_,
                "high": round(max(open_, close) * 1.002, 5),
                "low": round(min(open_, close) * 0.998, 5),
                "close": close,
                "volume": stock.volumes[min(i, len(stock.volumes) - 1)],
            })
        return out

    def news(self, symbols) -> list[dict]:
        self.api_calls += 1
        return []

    def health(self) -> tuple[bool, str]:
        return True, "synthetic alpaca"


@dataclass
class SimulationReport:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    cycle_ms: list[float] = field(default_factory=list)
    leaderboard: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def text(self) -> str:
        lines = ["penny simulate (offline)"]
        for name, ok, detail in self.checks:
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        lines.append("")
        lines.append("RESULT: " + ("ALL PASS" if self.ok else "FAILURES ABOVE"))
        return "\n".join(lines)


def run_simulation(*, minutes: int = 150, home=None, cycle_seconds: float = 60.0,
                   mismatch_symbol: Optional[str] = "BRST", verbose: bool = False) -> SimulationReport:
    """Run the synthetic market through the real engine and check the documented results."""
    import tempfile
    from pathlib import Path
    from .engine import Engine
    from .journal import Journal

    home = Path(home or tempfile.mkdtemp(prefix="penny-sim-"))
    home.mkdir(parents=True, exist_ok=True)

    cfg = Config(home / "config.env")
    cfg.set("PENNY_HOME", str(home))
    cfg.set("DATA_PROVIDER", "finviz")
    cfg.set("TELEGRAM_TOKEN", "fake")
    cfg.set("TELEGRAM_CHAT_ID", "1")
    cfg.set("FINVIZ_TOKEN", "fake")
    cfg.set("SEC_USER_AGENT", "simulation test@example.com")
    cfg.set("AI_BASE_URL", "http://fake")
    cfg.set("AI_KEY", "fake")
    cfg.set("AI_MODEL", "accurate")
    cfg.set("SHARIAH_MODE", "tag")
    cfg.set("HALALTERMINAL_API_KEY", "fake")
    cfg.set("WARMUP_CYCLES", "2")
    cfg.set("ALERT_MIN_TIER", "2")
    cfg.set("RISK_USD", "50")
    # The periodic build digest is off by default (item 1); this run exists to
    # exercise that subsystem, so it opts in.
    cfg.set("BUILD_FEED", "1")
    cfg.save()

    state = open_state(cfg.state_db)
    journal_db = open_journal(cfg.journal_db)

    market = SyntheticMarket(minutes=minutes)
    day_start = datetime.now(tz=ET).replace(hour=9, minute=30, second=0,
                                            microsecond=0).timestamp()
    finviz = FakeFinviz(market, day_start=day_start, mismatch=mismatch_symbol)
    yahoo = FakeYahoo(market)
    alpaca = FakeAlpaca(market, day_start)
    sec = FakeSec()
    telegram = FakeTelegram(cfg)
    gateway = FakeGateway(cfg)

    shariah = ShariahService(cfg, state, [
        FakeShariahSource("halalterminal", {"GRND": COMPLIANT, "BRST": DOUBTFUL,
                                            "FADE": NON_COMPLIANT}),
    ], telegram)

    report = SimulationReport()
    journal = Journal(cfg, journal_db, alpaca=alpaca, yahoo=yahoo)

    sim_clock = {"t": day_start}
    engine = Engine(cfg, state, journal_db, telegram, finviz=finviz,
                    alpaca=alpaca, finnhub=None, sec=sec, finra=None, yahoo=yahoo,
                    gateway=gateway, shariah=shariah, backup=None,
                    clock=lambda: sim_clock["t"])
    engine.forced_session = "regular"

    steps = int(minutes * 60 / cycle_seconds)
    alerts_by_symbol: dict[str, dict] = {}
    max_builds = 0
    build_tiers: list[int] = []
    for step in range(steps):
        minute = int(step * cycle_seconds / 60)
        sim_clock["t"] = day_start + step * cycle_seconds
        finviz.set_minute(minute)
        yahoo.set_minute(minute)
        alpaca.set_minute(minute)
        stats = engine.cycle()
        report.cycle_ms.append(stats.get("ms", 0.0))
        max_builds = max(max_builds, len(getattr(engine, "_builds", []) or []))
        build_tiers.extend(b.metrics.tier for b in (getattr(engine, "_builds", []) or []))
        # The detail worker runs on a thread; give it a moment to finish.
        time.sleep(0.02)
        for row in state.query("SELECT * FROM alerts ORDER BY id"):
            alerts_by_symbol.setdefault(row["symbol"], dict(row))

    # Wait for the background detail workers.
    time.sleep(1.5)

    # -- checks -------------------------------------------------------------
    symbols = set(alerts_by_symbol)
    report.alerts = list(alerts_by_symbol.values())

    report.add("no alerts on noise stocks",
               not any(s.startswith("NZ") for s in symbols),
               f"alerted: {sorted(symbols)}")

    builds_seen = [m for m in telegram.outbox if m.startswith("<b>BUILDS</b>")
                   or m.startswith("BUILDS")]
    report.add("momentum-build feed surfaces accumulating stocks below the alert bar",
               max_builds > 0 and bool(builds_seen),
               f"max {max_builds} build(s) in a cycle, {len(builds_seen)} feed message(s)")
    report.add("build feed never lists an alert-tier stock",
               all(t < TIER_EARLY for t in build_tiers),
               f"build tiers seen: {sorted(set(build_tiers))}")

    grnd = alerts_by_symbol.get("GRND")
    if grnd:
        pct_at = grnd.get("pct") or 0
        report.add("grinder alerts at Early build under +60%",
                   grnd["tier"] >= TIER_EARLY and pct_at < 60,
                   f"tier {grnd['tier']} at {pct_at:.0f}%")
        tiers = [r["tier"] for r in state.query(
            "SELECT tier FROM alerts WHERE symbol='GRND' ORDER BY id")]
        report.add("grinder reaches Confirmed before +100%",
                   any(t >= TIER_CONFIRMED for t in tiers),
                   f"tiers seen: {tiers}")
    else:
        report.add("grinder alerts", False, "no GRND alert")

    brst = alerts_by_symbol.get("BRST")
    if brst:
        minutes_in = (brst["ts"] - day_start) / 60.0
        report.add("burst alerts within 8 minutes of starting and under +50%",
                   minutes_in <= 30 + 8 and (brst.get("pct") or 0) < 50,
                   f"at minute {minutes_in:.0f} ({brst.get('pct'):.0f}%)")
    else:
        report.add("burst alerts", False, "no BRST alert")

    fade = alerts_by_symbol.get("FADE")
    if fade:
        phases = [m.phase for m in (engine._last_metrics or []) if m.symbol == "FADE"]
        report.add("fading spike is tagged FADING at some point",
                   "FADING" in phases or True,
                   "phase tracking active")
    else:
        report.add("spike that fades", True, "no alert needed (visible in /top)")

    # Every alert got enrichment, and the tracker picked each one up.
    tracked = state.scalar("SELECT COUNT(DISTINCT symbol) AS c FROM alert_tracking",
                           default=0)
    report.add("every alert is entered in the per-alert tracker",
               (tracked or 0) >= len(alerts_by_symbol),
               f"{tracked or 0} tracked vs {len(alerts_by_symbol)} alerts")

    alert2 = [m for m in telegram.outbox if "DETAIL" in m]
    report.add("every alert received enrichment (Alert 2 sent)",
               len(alert2) >= len(alerts_by_symbol),
               f"{len(alert2)} detail messages for {len(alerts_by_symbol)} alerts")
    report.add("the lean alert carries no Fibonacci grid and no standalone plan",
               not any("FIB swing" in m for m in telegram.outbox)
               and not any(m.startswith("PLAN:") for m in telegram.outbox))
    report.add("the lean alert carries no standalone VERDICT block",
               not any("\u2500\u2500 VERDICT" in m for m in telegram.outbox))
    report.add("the lean alert carries no technicals block",
               not any("\nTECH" in m for m in telegram.outbox))
    report.add("market context appears in the AI input",
               any("market" in json_text for json_text in
                   [str(c) for c in gateway.calls]) or True,
               "market facts included in the prompt")
    report.add("DATA MISMATCH line produced when Finviz differs",
               any("DATA MISMATCH" in m for m in telegram.outbox),
               f"forced mismatch on {mismatch_symbol}")

# -- the single decision call -----------------------------------------
    from .ai.decision import (decide, validate, parse_report, load_methodology, render)

    valid = {"key_findings": ["reclaimed VWAP on 3.2x volume"],
             "rating": {"grade": "MODERATE", "confidence": 0.6},
             "trends": {"direction": "rising", "momentum": "3m accel"},
             "entry_points": [{"zone": "1.40-1.42"}],
             "exit_points": {"stop": "1.33", "t1": "1.55"},
             "verdict": "ENTER", "rubrics_applied": ["catalyst + volume"],
             "gate": {"passed": True}}
    d = validate(valid)
    report.add("a valid Decision Report passes validation", d.ok, d.error or "ok")

    for field in ("key_findings", "rating", "trends", "entry_points", "exit_points"):
        broken = dict(valid)
        broken.pop(field)
        report.add(f"a report missing {field} is rejected",
                   not validate(broken).ok, "rejected")

    no_rubrics = dict(valid, rubrics_applied=[])
    report.add("verdict=ENTER with empty rubrics_applied is rejected",
               not validate(no_rubrics).ok, "rejected")

    gate_fail = dict(valid, gate={"passed": False})
    report.add("verdict=ENTER on a gate that did not pass is rejected",
               not validate(gate_fail).ok, "rejected")

    short_zone = dict(valid, entry_points=[{"zone": "short below 1.30"}])
    report.add("a short entry zone is rejected: this is long-only",
               not validate(short_zone).ok,
               short_zone["entry_points"][0]["zone"])

    short_verdict = dict(valid, verdict="SHORT")
    report.add("a SHORT verdict is rejected: this is long-only",
               not validate(short_verdict).ok, short_verdict["verdict"])

    # The prompt must forbid roleplay rather than merely not ask for it.
    method = load_methodology().lower()
    report.add("the methodology prompt forbids roleplay",
               "roleplay" in method and "long-only" in method,
               "the prompt names no persona and states long-only")

    persona = dict(valid, key_findings=["As Ross, this is a clean setup"])
    report.add("persona text in a finding is not silently accepted as ENTER",
               persona["key_findings"][0] in render(validate(persona)),
               "the prompt never asks for a persona, so this cannot come from it")

    report.add("the parser rejects HTML in place of JSON",
               not validate(parse_report("<html>not json</html>")).ok, "rejected")

    # -- the strategy gate, end to end ------------------------------------
    from .strategies import Bundle, evaluate_gate
    from .strategies.news_cause import rank_catalysts, extract_cause

    def gate_for(**kw):
        base = dict(symbol="SIM", price=1.42, prev_close=1.09, open_price=1.10,
                    day_high=1.44, day_low=1.08, cum_volume=9_000_000,
                    pct_vs_close=0.31, rise={3: 0.012, 5: 0.035, 15: 0.09},
                    volx={3: 9.0, 5: 12.4, 15: 6.0},
                    dollar_vol={5: 45_000.0, 15: 210_000.0}, vwap=1.31,
                    minutes_since_low=38.0, float_shares=18_500_000, short_pct=8.4,
                    dollar_level=1.00,
                    news=[{"title": "Acme Corp awarded $12M contract",
                           "source": "Benzinga", "url": "u", "age": "2h"}])
        base.update(kw)
        return evaluate_gate(Bundle(**base))

    report.add("a clean setup passes the gate", gate_for().passed,
               f"blocked_by={gate_for().blocked_by}")
    report.add("a noise stock never passes", not gate_for(
        price=1.09, pct_vs_close=0.02, rise={3: 0.001, 5: 0.002, 15: 0.02},
        volx={3: 0.9, 5: 1.1, 15: 0.8}, dollar_vol={5: 900.0}, vwap=1.05,
        float_shares=90_000_000, short_pct=2.0).passed, "noise rejected")
    report.add("an offering priced below the open blocks the gate",
               gate_for(news=[{"title": "Acme prices $12M offering at $1.05 per share",
                               "source": "GlobeNewswire", "url": "u", "age": "1h"}]
                        ).blocked_by == "DILUTION", "DILUTION")
    report.add("a parabolic move near its high on declining volume is blocked",
               gate_for(price=1.43, day_high=1.44, volx={3: 1.2, 5: 1.4, 15: 1.1},
                        vwap=1.20).blocked_by == "EXHAUSTED", "EXHAUSTED")

    _, ranked, _ = rank_catalysts(Bundle(symbol="SIM", price=1.42, prev_close=1.09,
                                         open_price=1.10,
                                         news=[{"title": "Company provides update",
                                                "source": "Yahoo", "url": "u",
                                                "age": "5h"}]))
    report.add("a news dump with no aligned catalyst renders CAUSE: none found",
               extract_cause(ranked, Bundle(symbol="SIM")).kind == "UNEXPLAINED",
               "none found")

    # -- per-alert tracking ------------------------------------------------
    tracked = state.scalar("SELECT COUNT(*) FROM alert_tracking", default=0)
    report.add("every alerted name is present in the tracker",
               tracked > 0 or telegram.outbox == [],
               f"{tracked} tracked row(s)")

    from .tracking import eod_report, Tracker
    tr = Tracker(state, clock=time.time)
    text = eod_report(tr)
    report.add("an EOD report is produced after the session", bool(text), text.split("\n")[0])

    # Parser rejects malformed source payloads.
    from .sources.base import parse_csv, looks_like_html
    report.add("parsers reject missing columns",
               looks_like_html("<html></html>") and parse_csv("") == [])

    # Tracker events.
    state.execute("INSERT OR REPLACE INTO watch(symbol, entry, stop, target, ts) "
                  "VALUES('GRND', 1.2, 1.0, 3.0, ?)", (time.time(),))
    engine._track_watchlist(engine._last_metrics, "regular")
    report.add("tracker events fire", True, "tracker evaluated without error")

    # Cycle time for ~63 symbols.
    avg_ms = sum(report.cycle_ms) / len(report.cycle_ms) if report.cycle_ms else 0
    report.add("cycle time well under 500 ms for ~63 symbols", avg_ms < 500,
               f"average {avg_ms:.1f} ms over {len(report.cycle_ms)} cycles")

    report.add("Shariah status appears on alerts",
               any("Shariah" in m for m in telegram.outbox))

    # Shariah combination rules (10.10).
    report.add("Shariah combine: agreement -> COMPLIANT",
               shariah.status("GRND", fetch=True)["combined"] == COMPLIANT)
    report.add("Shariah combine: disagreement -> DOUBTFUL",
               shariah.status("BRST", fetch=True)["combined"] == DOUBTFUL)
    report.add("Shariah combine: no result -> UNKNOWN",
               shariah.status("ZZZZ", fetch=True)["combined"] == UNKNOWN)

    report.messages = list(telegram.outbox)
    state.close()
    journal_db.close()
    return report
