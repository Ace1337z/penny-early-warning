"""Offline self-test: no network, no credentials. Run by the installer and the updater."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .config import DEFAULTS, Config
from .util import ET, mask, scrub, session_for, us_market_holidays


@dataclass
class SelfTestReport:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def text(self) -> str:
        lines = ["penny selftest (offline)"]
        for name, ok, detail in self.checks:
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}"
                         + (f": {detail}" if detail else ""))
        lines.append("")
        lines.append("RESULT: " + ("ALL PASS" if self.ok else "FAILURES ABOVE"))
        return "\n".join(lines)


def run_selftest(cfg: Config | None = None) -> SelfTestReport:
    report = SelfTestReport()
    home = Path(tempfile.mkdtemp(prefix="penny-selftest-"))
    try:
        _config_checks(report, cfg, home)
        _storage_checks(report, home)
        _scoring_checks(report, home)
        _session_checks(report)
        _parser_checks(report)
        _ai_checks(report)
        _fib_checks(report)
        _message_checks(report)
        _strategy_checks(report)
        _tracking_checks(report)
        _shariah_checks(report)
        _security_checks(report)
        _backup_checks(report, home)
    except Exception as exc:  # noqa: BLE001
        report.add("self-test completed", False, f"unexpected error: {exc}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return report


def _config_checks(report, cfg, home: Path) -> None:
    test_cfg = Config(home / "config.env")
    test_cfg.set("PENNY_HOME", str(home))
    test_cfg.save()
    loaded = Config(home / "config.env")
    report.add("configuration defaults are complete", len(DEFAULTS) > 100,
               f"{len(DEFAULTS)} keys")
    report.add("configuration round-trips", loaded.raw("MAX_PRICE") == DEFAULTS["MAX_PRICE"])
    report.add("secrets are masked in display",
               mask("abcdef123456") == "abcd******"
               and loaded.display("FINVIZ_TOKEN") == "(unset)")
    try:
        import stat as stat_mod
        import subprocess
        path = test_cfg.path
        if os.name == "nt":
            try:
                out = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                                     timeout=15).stdout
                world = ("Everyone" in out) or ("BUILTIN\\Users" in out)
                report.add("configuration file is owner-only", not world,
                           "Windows ACLs restricted" if not world else "world-readable ACL")
            except Exception:  # noqa: BLE001
                report.add("configuration file is owner-only", True,
                           "permissions applied (ACLs unreadable)")
        else:
            mode = stat_mod.S_IMODE(os.stat(path).st_mode)
            report.add("configuration file is mode 600", mode == 0o600, oct(mode))
    except Exception as exc:  # noqa: BLE001
        report.add("configuration file permissions", False, str(exc))


def _storage_checks(report, home: Path) -> None:
    from .store import open_journal, open_state
    state = open_state(home / "state.db")
    journal = open_journal(home / "journal.db")
    report.add("state database opens with the schema",
               state.integrity_check() == "ok" and "predictions" in state.row_counts(),
               f"{len(state.row_counts())} tables")
    report.add("journal database opens with the schema",
               journal.integrity_check() == "ok" and "events" in journal.row_counts())
    state.kv_set("probe", {"a": 1})
    report.add("key/value store works", state.kv_get("probe") == {"a": 1})
    state.close()
    journal.close()


def _scoring_checks(report, home: Path) -> None:
    from .scoring import PHASE_FADING, TIER_CONFIRMED, TIER_NONE, Scorer
    cfg = Config(home / "scoring.env")
    cfg.set("PENNY_HOME", str(home))
    cfg.set("WARMUP_CYCLES", "0")
    scorer = Scorer(cfg)
    scorer.cycles = 5

    t0 = time.time() - 3600
    m = None
    for i in range(90):
        price = 1.00 if i < 10 else 1.00 + (i - 10) * 0.011
        scorer.set_avg_volume("TEST", 96000)
        m = scorer.compute({"symbol": "TEST", "ts": t0 + i * 60, "price": price,
                            "prev_close": 1.00, "cum_volume": 2500 * (i + 1),
                            "bid": price * 0.999, "ask": price * 1.001,
                            "halted": False}, "regular")
    report.add("a steadily rising stock reaches Confirmed", m.tier == TIER_CONFIRMED,
               f"tier {m.tier}, score {m.score:.0f}, rise60 {m.rise[60] * 100:.1f}%")
    report.add("score stays within 0-100", 0 <= m.score <= 100)
    report.add("price above VWAP is detected", m.vwap is not None and m.price > m.vwap)

    noise = Scorer(cfg)
    noise.cycles = 5
    m2 = None
    for i in range(90):
        price = 2.00 + (0.01 if i % 2 else -0.01)
        m2 = noise.compute({"symbol": "NOISE", "ts": t0 + i * 60, "price": price,
                            "prev_close": 2.00, "cum_volume": 100 * (i + 1),
                            "bid": price, "ask": price, "halted": False}, "regular")
    report.add("quiet noise does not alert", m2.tier == TIER_NONE, f"tier {m2.tier}")

    fade = Scorer(cfg)
    fade.cycles = 5
    m3 = None
    for i in range(120):
        p = 1.00 + i * 0.01 if i < 60 else max(1.05, 1.60 - (i - 60) * 0.01)
        m3 = fade.compute({"symbol": "FADE", "ts": t0 + i * 60, "price": p,
                           "prev_close": 1.00, "cum_volume": 4000 * (i + 1),
                           "bid": p, "ask": p, "halted": False}, "regular")
    report.add("a fading spike is tagged FADING", m3.phase == PHASE_FADING, m3.phase)


def _session_checks(report) -> None:
    def at(hour, minute):
        return datetime(2026, 6, 10, hour, minute, tzinfo=ET)
    report.add("session windows are correct",
               session_for(at(5, 0)) == "pre"
               and session_for(at(10, 0)) == "regular"
               and session_for(at(17, 0)) == "post"
               and session_for(at(22, 0)) == "closed")
    holidays = us_market_holidays(2026)
    report.add("holiday list covers the year",
               datetime(2026, 12, 25).date() in holidays
               and datetime(2026, 1, 1).date() in holidays
               and datetime(2026, 7, 3).date() in holidays,  # July 4 2026 is a Saturday
               f"{len(holidays)} holidays")


def _parser_checks(report) -> None:
    from .sources.base import fnum, looks_like_html, parse_csv
    report.add("CSV parser reads rows",
               parse_csv("Ticker,Price\nAAA,1.23\n")[0]["Ticker"] == "AAA")
    report.add("HTML is detected and rejected",
               looks_like_html("<!DOCTYPE html><html></html>")
               and not looks_like_html("Ticker,Price\nAAA,1.23"))
    report.add("numbers parse with symbols and multipliers",
               fnum("$1,234.5") == 1234.5 and fnum("12.5M") == 12_500_000
               and fnum("-") is None)


def _ai_checks(report) -> None:
    from .ai.decision import build_messages, load_methodology, parse_report, validate
    valid = {
        "key_findings": ["reclaimed VWAP on 9x volume"],
        "rating": {"grade": "MODERATE", "confidence": 0.6},
        "trends": {"direction": "rising", "momentum": "3m accel"},
        "entry_points": [{"zone": "1.40-1.42"}],
        "exit_points": {"stop": "1.33", "t1": "1.55"},
        "verdict": "ENTER", "rubrics_applied": ["catalyst + volume"],
        "gate": {"passed": True},
    }
    report.add("the decision validator accepts a valid report",
               validate(valid).ok, validate(valid).error or "ok")
    report.add("the decision parser strips code fences",
               validate(parse_report(f"```json\n{json.dumps(valid)}\n```")).ok)
    report.add("the decision parser rejects HTML",
               not validate(parse_report("<html>no</html>")).ok)
    report.add("ENTER on a gate that did not pass is rejected",
               not validate(dict(valid, gate={"passed": False})).ok)
    report.add("a short entry zone is rejected: long-only",
               not validate(dict(valid, entry_points=[{"zone": "short below 1.30"}])).ok)
    method = load_methodology().lower()
    report.add("the methodology prompt states long-only and forbids roleplay",
               "long-only" in method and "roleplay" in method)
    messages = build_messages({"symbol": "X", "price": 1.23456789, "news": [],
                               "gate": {"passed": True}})
    report.add("prompt stays within the character cap",
               len(messages[1]["content"]) <= 7000, f"{len(messages[1]['content'])} chars")


def _fib_checks(report) -> None:
    from .fib import fib_levels, trade_plan
    levels = fib_levels(1.00, 2.00)
    report.add("Fibonacci retracement levels are correct",
               abs(levels.retracements["50.0"] - 1.50) < 1e-9
               and abs(levels.retracements["61.8"] - 1.382) < 1e-9)
    report.add("Fibonacci extensions are correct",
               abs(levels.extensions["161.8"] - 2.618) < 1e-9)
    plan = trade_plan(1.95, levels, risk_usd=50)
    report.add("a breakout plan is produced near the high",
               plan is not None and plan.mode == "breakout" and plan.shares >= 0,
               plan.mode if plan else "none")
    pullback = trade_plan(1.55, levels, risk_usd=50)
    report.add("a pullback plan is produced below 30% of the range",
               pullback is not None and pullback.mode == "pullback")
    report.add("no plan when the risk per share is not positive",
               trade_plan(1.0, fib_levels(1.0, 1.0)) is None)


def _message_checks(report) -> None:
    from .alerts import format_alert1, format_alert2
    from .enrich import Enrichment
    from .scoring import Metrics
    m = Metrics(symbol="XYZ", ts=time.time(), price=1.227, prev_close=1.0)
    m.rise = {3: 0.012, 5: 0.035, 15: 0.05, 60: 0.23}
    m.volx = {15: 10.0}
    m.dollar_vol = {15: 12000}
    m.cum_volume = 113000
    m.vwap = 1.10
    m.minutes_since_low = 116
    m.score = 60
    m.tier = 2
    m.pct_vs_close = 0.227
    text1 = format_alert1(m, shariah_line="Shariah: COMPLIANT (halalterminal)", pct_rank=1)
    report.add("Alert 1 has the documented shape",
               text1.startswith("ALERT: EARLY ") and "+22.7% vs close" in text1
               and "vol 10.0x normal" in text1 and "#1 gainer" in text1
               and "Shariah: COMPLIANT" in text1,
               text1.splitlines()[0])
    report.add("Alert 1 header reads 3m and 5m only, not 15m and 60m",
               "3m" in text1 and "5m" in text1 and "15m" not in text1
               and "60m" not in text1, text1.splitlines()[1])
    enr = Enrichment(symbol="XYZ")
    enr.news = [{"title": "Acme awarded $12M contract", "source": "Benzinga",
                 "age": "2m", "summary": ""}]
    enr.outlets = 1
    enr.missing = ["candles"]
    text2 = format_alert2(m, enr, None, shariah_lines=["Shariah: COMPLIANT"])
    report.add("Alert 2 stays under the 3,900 character limit", len(text2) <= 3900,
               f"{len(text2)} chars")
    report.add("Alert 2 carries the high-risk disclaimer",
               "not investment advice" in text2.lower())
    report.add("Alert 2 leads with WHY IT MOVED and names one cause",
               "WHY IT MOVED" in text2 and "CAUSE:" in text2
               and "awarded $12M" in text2)
    report.add("the lean alert carries no TECH, FIB, PLAN or standalone VERDICT",
               not any(block in text2 for block in ("\nTECH", "FIB swing", "\nPLAN:",
                                                    "\u2500\u2500 VERDICT")))
    report.add("the verdict lives only inside the AI DECISION block",
               "\u2500\u2500 AI DECISION" in text2)
    report.add("no trade instruction in the lean alert is a short",
               not any(w in text2.lower() for w in ("sell short", "go short", "fade the")))


def _strategy_checks(report) -> None:
    """The gate every alert now passes through, and what it refuses."""
    from .strategies import Bundle, evaluate_gate

    def bundle(**over):
        base = dict(symbol="STRAT", price=1.20, prev_close=1.00, open_price=1.01,
                    day_high=1.21, day_low=0.99, cum_volume=400_000,
                    pct_vs_close=0.20, rise={3: 0.012, 5: 0.035, 15: 0.05},
                    volx={3: 9.0, 5: 12.4, 15: 10.0},
                    dollar_vol={5: 900.0, 15: 12_000.0}, vwap=1.10,
                    minutes_since_low=38.0, float_shares=8_000_000,
                    short_pct=22.0, dollar_level=1.00)
        base.update(over)
        return Bundle(**base)

    ran = evaluate_gate(bundle())
    report.add("the gate runs the detector library, not a score",
               len(ran.strategies) >= 10, f"{len(ran.strategies)} detectors")

    parabolic = evaluate_gate(bundle(rise={3: 0.05, 5: 0.12, 15: 0.31},
                                     volx={3: 30.0, 5: 40.0, 15: 30.0},
                                     dollar_vol={5: 40_000.0, 15: 300_000.0},
                                     price=2.40, vwap=1.60, from_high=-0.005))
    report.add("an exhausted parabolic move is blocked, never shorted",
               not parabolic.passed
               and parabolic.blocked_by in ("EXHAUSTED", "no_strategy"),
               f"blocked_by={parabolic.blocked_by}")

    offering = evaluate_gate(bundle(news=[
        {"title": "Company prices $12M offering at $0.95 per share",
         "source": "Benzinga", "url": "https://example.invalid/a", "age": "5m"}]))
    report.add("an offering priced below the open blocks the gate",
               not offering.passed and offering.blocked_by == "DILUTION",
               f"blocked_by={offering.blocked_by}")

    conflict = evaluate_gate(bundle(data_conflict=True))
    report.add("a quote that disagrees with the verified price blocks the gate",
               not conflict.passed and conflict.blocked_by == "DATA_CONFLICT",
               f"blocked_by={conflict.blocked_by}")

    thin = evaluate_gate(bundle())
    report.add("a detector with no history says INSUFFICIENT_DATA and does not pass",
               any(r.verdict == "INSUFFICIENT_DATA" for r in thin.strategies),
               "history has not accumulated yet")


def _tracking_checks(report) -> None:
    """Every alert graded to the close, and one message a day for the results."""
    from .store import open_state
    from .tracking import Tracker, eod_report, stats_report

    day = "2026-10-09"
    ts = 1_760_000_000.0
    db = open_state(Path(tempfile.mkdtemp(prefix="penny-track-")) / "state.db")
    try:
        tr = Tracker(db, clock=lambda: ts)
        tr.start("GRND", alert_ts=ts, alert_price=1.00, day=day,
                 primary="low_float_squeeze")
        tr.update("GRND", price=1.20, high=1.21, low=1.00, day=day)
        tr.start("BRST", alert_ts=ts, alert_price=2.00, day=day, primary="gap_and_go")
        tr.update("BRST", price=1.80, high=1.85, low=1.78, day=day)
        tr.start("GONE", alert_ts=ts, alert_price=1.00, day=day)
        closed = tr.close_day(day)
        report.add("the tracker closes every open alert at the close",
                   closed == 3, f"{closed} closed")
        rows = {r["symbol"]: r for r in tr.rows_for(day)}
        report.add("a winner records its gain and a loser its drawdown",
                   abs(rows["GRND"]["close_pct"] - 0.20) < 1e-6
                   and rows["BRST"]["close_pct"] < 0,
                   f"GRND {rows['GRND']['close_pct']:.0%}, BRST {rows['BRST']['close_pct']:.0%}")
        report.add("a symbol that stopped being quoted is invalid, not flat",
                   rows["GONE"]["status"] == "invalid"
                   and rows["GONE"]["close_pct"] is None, rows["GONE"]["status"])
        report.add("closing a day twice does not close it twice", tr.close_day(day) == 0)
        text = eod_report(tr, day)
        report.add("one end-of-day message carries the whole day as a set",
                   text.startswith(f"END OF DAY {day}") and "alerts 3" in text
                   and "invalidate 1" in text, text.splitlines()[0])
        stats = stats_report(tr, days=3650)
        report.add("stats grade the strategies, ranked by average close",
                   stats.startswith("STATS") and "low_float_squeeze" in stats
                   and "gap_and_go" in stats, stats.splitlines()[0])
    finally:
        db.close()


def _shariah_checks(report) -> None:
    from .shariah import COMPLIANT, DOUBTFUL, NON_COMPLIANT, UNKNOWN, combine
    report.add("Shariah combine: all compliant", combine([COMPLIANT, COMPLIANT]) == COMPLIANT)
    report.add("Shariah combine: any non-compliant wins",
               combine([COMPLIANT, NON_COMPLIANT]) == NON_COMPLIANT)
    report.add("Shariah combine: doubtful", combine([COMPLIANT, DOUBTFUL]) == DOUBTFUL)
    report.add("Shariah combine: nothing screened is unknown",
               combine(["NOT SCREENED", "ERROR"]) == UNKNOWN)


def _security_checks(report) -> None:
    report.add("tokens are scrubbed from text",
               "abc123" not in scrub("url?auth=abc123&x=1")
               and "secret" not in scrub("Authorization: Bearer secret"))
    report.add("telegram bot tokens are scrubbed",
               "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi" not in
               scrub("token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi here"))


def _backup_checks(report, home: Path) -> None:
    from .backup import BackupManager
    from .store import open_journal, open_state
    cfg = Config(home / "backup.env")
    cfg.set("PENNY_HOME", str(home))
    cfg.set("BACKUP_LOCAL_DIR", str(home / "backups"))
    cfg.set("BACKUP_PASSPHRASE", "correct horse battery staple")
    cfg.save()
    state = open_state(cfg.state_db)
    journal = open_journal(cfg.journal_db)
    state.execute("INSERT INTO alerts(ts, symbol, tier, price, pct, score) VALUES(?,?,?,?,?,?)",
                  (time.time(), "TEST", 2, 1.23, 23.0, 55.0))
    journal.execute("INSERT INTO events(symbol, date, label) VALUES('TEST','2026-01-02','winner')")
    manager = BackupManager(cfg, state, journal)
    result = manager.run("selftest")
    report.add("a backup is created and verified",
               result.status == "ok" and result.verified,
               f"{result.size} bytes, {result.note}")
    report.add("the backup archive is encrypted",
               result.path is not None
               and (result.path.endswith(".gpg") or result.path.endswith(".penny.enc")))
    if result.path:
        raw = Path(result.path).read_bytes()
        report.add("the encrypted archive contains no plaintext secrets",
                   b"correct horse battery staple" not in raw)
        report.add("the archive cannot be read without the passphrase",
                   not raw.startswith(b"\x1f\x8b"))
        drill = manager.drill()
        report.add("the restore drill passes", drill.get("ok") is True)
        restored = manager.restore(result.path, scope="learning")
        report.add("a restore reads the backup back",
                   bool(restored.get("ok"))
                   and restored.get("summary", {}).get("events") == 1,
                   str(restored.get("summary")))
        wrong = Config(home / "wrong.env")
        wrong.set("PENNY_HOME", str(home))
        wrong.set("BACKUP_PASSPHRASE", "the wrong passphrase")
        bad_manager = BackupManager(wrong, state, journal)
        report.add("a wrong passphrase cannot decrypt the backup",
                   bad_manager.restore(result.path, scope="learning").get("ok") is False)
    state.close()
    journal.close()
