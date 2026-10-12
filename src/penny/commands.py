"""Telegram command handling: only from the configured chat id (6.12)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Optional

from .ai.decision import decide, render as render_decision
from .strategies.wiring import run_gate
from .tracking import eod_report, stats_report, summary_line
from .alerts import format_build_feed, tier_name
from .ui import (arrow_for, bar, bold, button, cb, code, divider, h, inline,
                 parse_cb, reply_keyboard)
from .util import money, num, pct, session_for, to_et

log = logging.getLogger(__name__)

HELP = """<b>Penny Early-Warning</b>
Tap a button below, or type a command.

<b>Live</b>
/top - champions and hidden gems
/builds - motion building below the alert bar
/market - market context now
/status - system health

<b>Symbols</b>
/check SYM - full analysis on demand
/watch SYM ENTRY [STOP] [TARGET]
/unwatch SYM
/watchlist

<b>Results</b>
/eod - tracked results for a day
/eod YYYY-MM-DD - results for a specific day
/stats - performance by strategy
/tracked - alerts still open today

<b>Keys</b>
/set KEY VALUE - change any setting
/keys - what is configured

<b>Maintenance</b>
/halal SYM - Shariah status
/backup now
/help"""

# Command menu published to Telegram's UI.
COMMANDS: list[tuple[str, str]] = [
    ("start", "open the button menu"),
    ("top", "champions and hidden gems"),
    ("builds", "momentum building below the alert bar"),
    ("market", "market context now"),
    ("status", "system health"),
    ("check", "full analysis for a symbol"),
    ("watchlist", "your watchlist"),
    ("eod", "tracked results for a day"),
    ("stats", "performance by strategy"),
    ("tracked", "alerts still open today"),
    ("keys", "what is configured"),
    ("backup", "run a backup now"),
    ("help", "all commands"),
]

MAIN_MENU = inline([
    [button("\U0001F4C8 Top", cb("top")), button("\U0001F331 Builds", cb("builds")),
     button("\U0001F30E Market", cb("market"))],
    [button("\U0001F440 Watchlist", cb("watch")), button("\U0001F4CA Results", cb("eod")),
     button("\u22EF More", cb("more"))],
])

MORE_MENU = inline([
    [button("Status", cb("status")), button("Keys", cb("keys")),
     button("\U0001F4BE Backup", cb("backup"))],
    [button("\u2753 Help", cb("help")), button("\U0001F3E0 Menu", cb("menu"))],
])

MARKET_MENU = inline([
    [button("\U0001F504 Refresh", cb("market")), button("Status", cb("status")),
     button("\U0001F3E0 Menu", cb("menu"))],
])

STATUS_MENU = inline([
    [button("\U0001F504 Refresh", cb("status")), button("Market", cb("market")),
     button("\U0001F3E0 Menu", cb("menu"))],
])

RESULTS_MENU = inline([
    [button("\U0001F504 Refresh", cb("eod")), button("Stats", cb("stats")),
     button("\U0001F3E0 Menu", cb("menu"))],
])


class CommandHandler:
    def __init__(self, cfg, db, journal, telegram, *, engine=None, runner=None,
                 catalog=None, shariah=None, backup=None):
        self.cfg = cfg
        self.db = db
        self.journal = journal
        self.telegram = telegram
        self.engine = engine
        self.runner = runner
        self.catalog = catalog
        self.shariah = shariah
        self.backup = backup
        self._offset = 0
        self._last_message_id = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- loop ---------------------------------------------------------------
    def poll_once(self) -> int:
        updates = self.telegram.get_updates(offset=self._offset, timeout=5)
        handled = 0
        for upd in updates:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            callback = upd.get("callback_query")
            if callback:
                if self._handle_callback(callback):
                    handled += 1
                continue
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            if not self._authorized(chat_id):
                log.warning("ignoring command from unauthorized chat %s", chat_id)
                continue
            self._last_message_id = int(msg.get("message_id") or 0)
            self.handle(text)
            handled += 1
        return handled

    def _handle_callback(self, callback: dict) -> bool:
        """A button tap: acknowledge it, then run the same handler as the command."""
        msg = callback.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not self._authorized(chat_id):
            log.warning("ignoring callback from unauthorized chat %s", chat_id)
            return False
        data = str(callback.get("data") or "")
        action, arg = parse_cb(data)
        message_id = msg.get("message_id")
        try:
            self.telegram.answer_callback(str(callback.get("id") or ""))
        except Exception:  # noqa: BLE001
            pass
        handler = self._CALLBACKS.get(action)
        if handler is None:
            self._reply("That button is no longer valid.", markup=MAIN_MENU)
            return True
        try:
            handler(self, arg, message_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("callback %s failed: %s", action, exc)
            self._reply(f"Command failed: {h(exc)}")
        return True

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        try:
            self.telegram.set_commands(COMMANDS)
        except Exception:  # noqa: BLE001
            pass
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                log.debug("command poll failed: %s", exc)
            self._stop.wait(poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def _authorized(self, chat_id: str) -> bool:
        configured = str(self.cfg.raw("TELEGRAM_CHAT_ID") or "")
        return bool(configured) and chat_id == configured

    def _reply(self, text: str, markup: Optional[dict] = None, *,
               html: bool = True) -> None:
        try:
            self.telegram.send(text, markup=markup, html=html, silent=True)
        except Exception as exc:  # noqa: BLE001
            log.debug("reply failed: %s", exc)

    def _typing(self) -> None:
        try:
            self.telegram.send_chat_action("typing")
        except Exception:  # noqa: BLE001
            pass

    def _render(self, text: str, markup: Optional[dict] = None,
                message_id: Optional[int] = None, *, html: bool = True) -> None:
        """Edit the tapped message in place when we have its id, else send new."""
        if message_id:
            try:
                if self.telegram.edit(int(message_id), text, markup=markup, html=html):
                    return
            except Exception:  # noqa: BLE001
                pass
        self._reply(text, markup=markup, html=html)

    # -- callback actions ---------------------------------------------------
    def _cb_top(self, arg: str, mid: Optional[int]) -> None:
        self._render(self._render_top_text(), self._top_menu(), mid)

    def _cb_builds(self, arg: str, mid: Optional[int]) -> None:
        self._render(self._render_builds_text(), self._build_menu(), mid)

    def _cb_market(self, arg: str, mid: Optional[int]) -> None:
        self._typing()
        self._render(self._render_market_text(), MARKET_MENU, mid)

    def _cb_status(self, arg: str, mid: Optional[int]) -> None:
        text = self.engine.status_text() if self.engine else "engine not available"
        self._render(text, STATUS_MENU, mid)

    def _cb_eod(self, arg: str, mid: Optional[int]) -> None:
        tracker = getattr(self.engine, "tracker", None) if self.engine else None
        if tracker is None:
            self._render("Tracking is not available.", RESULTS_MENU, mid)
            return
        self._render(eod_report(tracker, (arg or "").strip() or self._default_eod_day()),
                     RESULTS_MENU, mid)

    def _cb_stats(self, arg: str, mid: Optional[int]) -> None:
        tracker = getattr(self.engine, "tracker", None) if self.engine else None
        if tracker is None:
            self._render("Tracking is not available.", RESULTS_MENU, mid)
            return
        self._render(stats_report(tracker, self.cfg.int("STATS_WINDOW_DAYS", 30)),
                     RESULTS_MENU, mid)

    @staticmethod
    def _default_eod_day() -> str:
        """Today once the session is done, otherwise the previous session.

        Asking at 09:15 should return yesterday, not an empty report for a
        session that has not started.
        """
        now = to_et()
        if now.hour >= 16:
            return now.strftime("%Y-%m-%d")
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")

    def _cb_keys(self, arg: str, mid: Optional[int]) -> None:
        self._render(self._render_keys_text(), MAIN_MENU, mid)

    def _cb_watch(self, arg: str, mid: Optional[int]) -> None:
        self._render(self._render_watchlist_text(), MAIN_MENU, mid)

    def _cb_backup(self, arg: str, mid: Optional[int]) -> None:
        if not self.backup:
            self._render("Backup is not configured.", MAIN_MENU, mid)
            return
        self._typing()
        result = self.backup.run("manual")
        self._render(f"Backup {h(result.status)}: {result.size / 1024:.0f} KB "
                     f"({h(result.note or 'ok')})", MAIN_MENU, mid)

    def _cb_help(self, arg: str, mid: Optional[int]) -> None:
        self._render(HELP, MAIN_MENU, mid)

    def _cb_more(self, arg: str, mid: Optional[int]) -> None:
        text = (f"{bold('MORE')}\nStatus, keys, backups and help.")
        self._render(text, MORE_MENU, mid)

    def _cb_check(self, arg: str, mid: Optional[int]) -> None:
        symbol = (arg or "").strip().upper()
        if not symbol:
            self._render("Send <code>/check SYM</code> to analyse a symbol.",
                         MAIN_MENU, mid)
            return
        if mid:
            self.telegram.edit(int(mid), f"Running a full analysis for "
                                         f"{bold(symbol)}...", html=True)
        else:
            self._reply(f"Running a full analysis for {bold(symbol)}...")
        threading.Thread(target=self._run_check, args=(symbol,), daemon=True).start()

    def _cb_halal(self, arg: str, mid: Optional[int]) -> None:
        symbol = (arg or "").strip().upper()
        if not symbol:
            self._render("Send <code>/halal SYM</code>.", MAIN_MENU, mid)
            return
        if not self.shariah:
            self._render("Shariah screening is not configured.", MAIN_MENU, mid)
            return
        text = self.cmd_halal_text(symbol)
        self._render(text, self._check_menu(symbol), mid)

    _CALLBACKS = {
        "top": _cb_top,
        "builds": _cb_builds,
        "market": _cb_market,
        "status": _cb_status,
        "eod": _cb_eod,
        "stats": _cb_stats,
        "tracked": lambda a, m: self._render(self._render_tracked_text(), RESULTS_MENU, m),
        "keys": _cb_keys,
        "watch": _cb_watch,
        "backup": _cb_backup,
        "help": _cb_help,
        "more": _cb_more,
        "check": _cb_check,
        "halal": _cb_halal,
        "menu": _cb_help,
    }

    # -- dispatch -----------------------------------------------------------
    def handle(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        try:
            handler = {
                "/top": self.cmd_top,
                "/builds": self.cmd_builds,
                "/watch": self.cmd_watch,
                "/unwatch": self.cmd_unwatch,
                "/watchlist": self.cmd_watchlist,
                "/check": self.cmd_check,
                "/market": self.cmd_market,
                "/eod": self.cmd_eod,
                "/stats": self.cmd_stats,
                "/tracked": self.cmd_tracked,
                "/set": self.cmd_set,
                "/setkey": self.cmd_set,
                "/keys": self.cmd_keys,
                "/halal": self.cmd_halal,
                "/backup": self.cmd_backup,
                "/backups": self.cmd_backups,
                "/status": self.cmd_status,
                "/help": self.cmd_help,
                "/start": self.cmd_start,
            }.get(cmd)
            if handler is None:
                self._reply(f"Unknown command {code(cmd)}. Try /help.", markup=MAIN_MENU)
                return
            handler(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed: %s", cmd, exc)
            self._reply(f"Command failed: {h(exc)}")

    # -- commands -----------------------------------------------------------
    def cmd_help(self, args) -> None:
        self._reply(HELP, markup=MAIN_MENU)

    def cmd_start(self, args) -> None:
        """Welcome + a persistent reply keyboard for one-tap navigation."""
        greeting = (f"{bold('Penny Early-Warning')}\n"
                    f"Live penny-stock screening. Buttons below, or tap "
                    f"<code>/help</code> for everything.")
        try:
            self.telegram.send(greeting, html=True,
                               markup=reply_keyboard([
                                   ["/top", "/builds", "/market"],
                                   ["/watchlist", "/eod", "/help"],
                               ]))
        except TypeError:
            # A client that does not accept markup yet.
            self._reply(greeting, markup=MAIN_MENU)
            return
        self._reply(HELP, markup=MAIN_MENU)

    # -- shared renderers ---------------------------------------------------
    def _render_top_text(self) -> str:
        metrics = getattr(self.engine, "_last_metrics", None) if self.engine else None
        if not metrics:
            return (f"{bold('TOP')}\nNo scored symbols yet. The market may be closed, "
                    f"or the first cycles are still warming up.")
        ranked = self.engine.scorer.rank(metrics)
        lines = [bold("TOP MOVES")]
        if ranked["champions"]:
            lines.append(divider("Champions"))
            for m in ranked["champions"]:
                lines.append(self._stock_line(m))
        if ranked["hidden_gems"]:
            lines.append(divider("Hidden gems"))
            for m in ranked["hidden_gems"]:
                lines.append(self._stock_line(m, with_tier=False))
        if len(lines) == 1:
            lines.append("Nothing at tier \u2265 2 yet.")
        lines.append("\U0001F4A1 Tap a symbol below for the full analysis")
        return "\n".join(lines)

    def _stock_line(self, m, with_tier: bool = True) -> str:
        pv = m.pct_vs_close * 100 if m.pct_vs_close is not None else None
        tag = ""
        if with_tier:
            tag = " " + h(tier_name(m.tier))
            if m.phase == "EXTENDED":
                tag += " [ext]"
            elif m.phase == "FADING":
                tag += " [fade]"
        return (f"{arrow_for(pv)} {code(m.symbol)} {money(m.price)} "
                f"{pct(pv)}{tag} \u00b7 {bar((m.score or 0) / 100)} {m.score:.0f}")

    @staticmethod
    def _symbol_rows(symbols: list[str], per_row: int = 3) -> list[list[dict]]:
        """Chunk symbols into compact rows so a long list stays few rows."""
        rows: list[list[dict]] = []
        for i in range(0, len(symbols), per_row):
            rows.append([button(f"{s}", cb("check", s))
                         for s in symbols[i:i + per_row]])
        return rows

    def _top_menu(self) -> dict:
        """Buttons that open a full analysis for each ranked symbol."""
        metrics = getattr(self.engine, "_last_metrics", None) if self.engine else None
        rows: list[list[dict]] = []
        if metrics:
            ranked = self.engine.scorer.rank(metrics)
            picks = list(ranked.get("champions") or []) + list(ranked.get("hidden_gems") or [])
            rows = self._symbol_rows([m.symbol for m in picks[:6]], per_row=3)
        return inline(rows + [
            [button("\U0001F504 Refresh", cb("top")), button("\U0001F331 Builds", cb("builds")),
             button("\U0001F3E0 Menu", cb("menu"))],
        ]) or MAIN_MENU

    def cmd_top(self, args) -> None:
        self._reply(self._render_top_text(), markup=self._top_menu())

    def _render_builds_text(self) -> str:
        """Stocks accumulating below the alert bar, with what is still missing."""
        metrics = getattr(self.engine, "_last_metrics", None) if self.engine else None
        if not metrics:
            return (f"{bold('MOMENTUM BUILDS')}\nNo scored symbols yet. The market may be "
                    f"closed, or the first cycles are still warming up.")
        builds = getattr(self.engine, "_builds", None)
        if builds is None and getattr(self.engine, "scorer", None):
            try:
                builds = self.engine.scorer.build_candidates(metrics)
            except Exception:  # noqa: BLE001
                builds = []
        builds = builds or []
        if not builds:
            return (f"{bold('MOMENTUM BUILDS')}\nNothing building below the alert bar "
                    f"right now - that is expected on a quiet tape.")
        lines = [bold("MOMENTUM BUILDS"), "Accumulating, not yet alert-tier."]
        for sig in builds[:self.cfg.int("BUILD_TOP_N", 6)]:
            m = sig.metrics
            lines.append(f"{arrow_for((m.pct_vs_close or 0) * 100)} {code(m.symbol)} "
                         f"{h(money(m.price))} {h(pct((m.pct_vs_close or 0) * 100))} "
                         f"\u00b7 {h(sig.stage.lower())} {sig.score:.0f} "
                         f"\u00b7 {bar(sig.score / 100)}")
            detail = [f"15m {h(pct((m.rise.get(15, 0) or 0) * 100))}",
                      f"vol {h(num(m.volx.get(15, 0), 1))}x",
                      "above VWAP" if sig.above_vwap else "below VWAP"]
            lines.append("   " + " \u00b7 ".join(detail))
            if sig.missing:
                lines.append(f"   \u2192 to alert: {h('; '.join(sig.missing))}")
        lines.append("\U0001F50D Tap a symbol for the full analysis")
        return "\n".join(lines)

    def _build_menu(self) -> dict:
        metrics = getattr(self.engine, "_last_metrics", None) if self.engine else None
        builds = getattr(self.engine, "_builds", None) or []
        picks = [b.metrics.symbol for b in builds[:6]]
        rows = self._symbol_rows(picks, per_row=3)
        return inline(rows + [
            [button("\U0001F504 Refresh", cb("builds")), button("\U0001F4C8 Top", cb("top")),
             button("\U0001F3E0 Menu", cb("menu"))],
        ]) or MAIN_MENU

    def cmd_builds(self, args) -> None:
        self._reply(self._render_builds_text(), markup=self._build_menu())

    def cmd_watch(self, args) -> None:
        if not args:
            self._reply("Usage: <code>/watch SYM ENTRY [STOP] [TARGET]</code>")
            return
        symbol = args[0].upper()
        def num(i):
            if len(args) > i:
                try:
                    return float(args[i])
                except ValueError:
                    return None
            return None
        entry, stop, target = num(1), num(2), num(3)
        if entry is None:
            self._reply("Usage: <code>/watch SYM ENTRY [STOP] [TARGET]</code>")
            return
        self.db.execute(
            "INSERT INTO watch(symbol, entry, stop, target, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET entry=excluded.entry, stop=excluded.stop, "
            "target=excluded.target, ts=excluded.ts",
            (symbol, entry, stop, target, time.time()))
        self._reply(f"Watching {bold(symbol)}: entry {h(money(entry))}"
                    + (f", stop {h(money(stop))}" if stop else "")
                    + (f", target {h(money(target))}" if target else ""),
                    markup=MAIN_MENU)

    def cmd_unwatch(self, args) -> None:
        if not args:
            self._reply("Usage: <code>/unwatch SYM</code>")
            return
        self.db.execute("DELETE FROM watch WHERE symbol=?", (args[0].upper(),))
        self._reply(f"Stopped watching {bold(args[0].upper())}", markup=MAIN_MENU)

    def _render_watchlist_text(self) -> str:
        rows = self.db.query("SELECT * FROM watch ORDER BY ts")
        if not rows:
            return (f"{bold('WATCHLIST')}\nEmpty. Add one with "
                    f"<code>/watch SYM ENTRY [STOP] [TARGET]</code>.")
        metrics = {m.symbol: m for m in (getattr(self.engine, "_last_metrics", []) or [])}
        lines = [bold("WATCHLIST")]
        for row in rows:
            m = metrics.get(row["symbol"])
            price = m.price if m else None
            pl = None
            if price and row["entry"]:
                pl = (price / row["entry"] - 1) * 100
            extra = []
            if row["stop"]:
                extra.append(f"stop {h(money(row['stop']))}")
            if row["target"]:
                extra.append(f"target {h(money(row['target']))}")
            tail = (f"now {h(money(price))} {arrow_for(pl)} {h(pct(pl))}"
                    if price else "no price yet")
            lines.append(f"{arrow_for(pl)} {code(row['symbol'])} "
                         f"entry {h(money(row['entry']))}"
                         + (" \u00b7 " + " \u00b7 ".join(extra) if extra else "")
                         + f" \u00b7 {tail}")
        return "\n".join(lines)

    def cmd_watchlist(self, args) -> None:
        self._reply(self._render_watchlist_text(), markup=MAIN_MENU)

    def cmd_check(self, args) -> None:
        if not args:
            self._reply("Usage: <code>/check SYM</code>", markup=MAIN_MENU)
            return
        symbol = args[0].upper()
        self._reply(f"Running a full analysis for {bold(symbol)}...")
        threading.Thread(target=self._run_check, args=(symbol,), daemon=True).start()

    def _run_check(self, symbol: str) -> None:
        if not self.engine:
            self._reply("Engine not available.")
            return
        try:
            from .alerts import build_ai_facts, format_alert2
            started = time.monotonic()
            stage = {"t": started}

            def mark(name: str) -> None:
                now = time.monotonic()
                log.info("check %s: %s in %.1fs", symbol, name, now - stage["t"])
                stage["t"] = now

            # Fetch the symbol now: the cached cycle quote is up to one cycle old
            # and does not exist at all for a stock outside the sub-$10 universe
            # (NVDA, AAPL), which used to answer "no quote available".
            quote = self.engine.check_quote(symbol)
            session = session_for()
            metrics = {m.symbol: m for m in (self.engine._last_metrics or [])}
            m = metrics.get(symbol)
            if quote is not None:
                # Recompute from the fresh quote so the price and the windows
                # reflect the current (including after-hours) print.
                avg = quote.get("avg_volume")
                if avg and float(avg) > 0:
                    self.engine.scorer.set_avg_volume(symbol, float(avg))
                fresh = self.engine.scorer.compute(quote, session)
                if fresh is not None:
                    m = fresh
            if m is None:
                self._reply(f"{bold(symbol)}: no quote available right now.",
                            markup=MAIN_MENU)
                return
            enr = self.engine.enricher.enrich(symbol, quote or self.engine._quote_for(m), m)
            mark("enrichment")
            # Keep the hourly market data cached; only the 30s index quotes
            # really need to be fresh, and forcing everything cost several
            # rate-limited Finviz calls on every /check.
            market = self.engine.market.get(force_quotes=True)
            mark("market context")
            shariah = self.engine._shariah_status(symbol, enr.filings, True)
            shariah_lines = self.engine.shariah.detail_lines(shariah) if self.engine.shariah else []
            mark("shariah")
            risk_usd = self.cfg.float("RISK_USD", 50)
            # Run the same gate the alert path uses, so /check and the live
            # alert cannot disagree about whether this is a setup.
            gate, bundle = run_gate(m, enr=enr, cfg=self.cfg, now=to_et())
            facts = build_ai_facts(m, enr, market, session=session_for(),
                                   time_to_horizon=self.engine._time_to_horizons(),
                                   gate=gate, bundle=bundle)
            decision = decide(getattr(self.engine, "gateway", None), facts,
                              model=self.cfg.raw("AI_MODEL") or "")
            mark("ai decision")
            log.info("check %s: total %.1fs", symbol, time.monotonic() - started)
            text = format_alert2(m, enr, market, shariah_lines=shariah_lines,
                                 gate=gate, bundle=bundle,
                                 decision=render_decision(decision),
                                 fast=self.engine.fast_burst(m, session), html=True)
            self._reply(text, markup=self._check_menu(symbol), html=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("check failed for %s: %s", symbol, exc)
            self._reply(f"Check failed: {h(exc)}")

    @staticmethod
    def _check_menu(symbol: str) -> dict:
        return inline([
            [button("\U0001F504 Re-check", cb("check", symbol)),
             button("\U0001F54C Halal", cb("halal", symbol)),
             button("\U0001F3E0 Menu", cb("menu"))],
        ])

    def _render_market_text(self) -> str:
        if not self.engine:
            return "Engine not available."
        ctx = self.engine.market.get(force=True)
        lines = [bold("MARKET")]
        regime = (ctx.regime or "unknown").upper()
        lines.append(f"Regime: {h(regime)} \u00b7 {h(session_for())}")
        if ctx.quotes or ctx.index_levels:
            lines.append(divider())
        for sym, q in ctx.quotes.items():
            ch = q.get("change_pct")
            lines.append(f"{arrow_for(ch)} {code(sym)} {h(money(q.get('price')))} "
                         f"{h(pct(ch))} \u00b7 15m {h(pct(q.get('change_15m')))} "
                         f"\u00b7 60m {h(pct(q.get('change_60m')))}")
        for sym, lv in ctx.index_levels.items():
            ch = lv.get("change_pct")
            price = lv.get("price")
            price_txt = f"{price:,.2f}" if isinstance(price, (int, float)) else "-"
            lines.append(f"{arrow_for(ch)} {code(sym)} {price_txt} {h(pct(ch))}")
        if ctx.sectors:
            lines.append(divider("Sectors"))
            for s in ctx.sectors[:6]:
                ch = s.get("change_pct")
                lines.append(f"{arrow_for(ch)} {h(s['name'])} {h(pct(ch))}")
        if ctx.events:
            lines.append(divider("Events"))
            for e in ctx.events[:4]:
                lines.append(f"\u2022 {h(e.get('time'))} {h(e.get('event'))}")
        if ctx.news:
            lines.append(divider("News"))
            for n in ctx.news[:3]:
                lines.append(f"\u2022 {h(n)}")
        if ctx.missing:
            lines.append(divider())
            lines.append(f"\u26A0\uFE0F missing: {h(', '.join(ctx.missing))}")
        return "\n".join(lines)

    def cmd_market(self, args) -> None:
        if not self.engine:
            self._reply("Engine not available.")
            return
        self._reply(self._render_market_text(), markup=MARKET_MENU)

    def cmd_eod(self, args) -> None:
        """The day's tracked results. Defaults to the last completed session."""
        tracker = getattr(self.engine, "tracker", None) if self.engine else None
        if tracker is None:
            self._reply("Tracking is not available.")
            return
        day = (args[0].strip() if args and args[0] else "") or self._default_eod_day()
        self._reply(eod_report(tracker, day), markup=RESULTS_MENU)

    def cmd_stats(self, args) -> None:
        """Performance grouped by the strategy that fired.

        Grouped rather than averaged, because the actionable question is which
        detector to keep, and an overall average hides a consistently flat one.
        """
        tracker = getattr(self.engine, "tracker", None) if self.engine else None
        if tracker is None:
            self._reply("Tracking is not available.")
            return
        self._reply(stats_report(tracker, self.cfg.int("STATS_WINDOW_DAYS", 30)),
                    markup=RESULTS_MENU)

    def cmd_tracked(self, args) -> None:
        """Alerts still open right now, and where each has got to."""
        self._reply(self._render_tracked_text(), markup=RESULTS_MENU)

    def _render_tracked_text(self) -> str:
        tracker = getattr(self.engine, "tracker", None) if self.engine else None
        if tracker is None:
            return "Tracking is not available."
        rows = tracker.rows_for(limit=20)
        if not rows:
            return "No alerts tracked today."
        still_open = sum(1 for r in rows if r.get("status") == "open")
        lines = [f"TRACKED TODAY - {still_open} open of {len(rows)}", ""]
        lines.extend(summary_line(r) for r in rows)
        return "\n".join(lines)

    def cmd_set(self, args) -> None:
        """Set any configuration value from the bot. Secrets are saved, not echoed."""
        from .config import DEFAULTS, SECRET_KEYS, RESTART_KEYS
        if len(args) < 2:
            self._reply("Usage: <code>/set KEY VALUE</code> "
                        "(e.g. <code>/set FINVIZ_TOKEN abc123</code>)")
            return
        key = args[0].upper()
        value = " ".join(args[1:]).strip()
        if key not in DEFAULTS:
            known = ", ".join(sorted(k for k in DEFAULTS if k in SECRET_KEYS))
            self._reply(f"Unknown key {code(key)}.\nSettable secrets: {h(known)}")
            return
        was_secret = key in SECRET_KEYS
        self.cfg.set(key, value)
        try:
            self.cfg.save()
        except Exception as exc:  # noqa: BLE001
            self._reply(f"Could not save {code(key)}: {h(exc)}")
            return
        note = self._apply_credential_change(key)
        try:
            from .logging_setup import update_secrets
            update_secrets(self.cfg.secrets())
        except Exception:  # noqa: BLE001
            pass
        # Remove the message that carried the secret so it does not linger in history.
        if was_secret:
            try:
                self.telegram.delete(getattr(self, "_last_message_id", 0))
            except Exception:  # noqa: BLE001
                pass
            shown = self.cfg.display(key)
            self._reply(f"\u2705 {code(key)} saved (now {h(shown)}).")
        else:
            self._reply(f"\u2705 {code(key)} = {h(value)}")
        if key in RESTART_KEYS:
            self._reply(f"\u26A0\uFE0F {code(key)} takes effect after the next restart "
                        f"(<code>sudo systemctl restart penny</code>).")
        if note:
            self._reply(note)

    def _apply_credential_change(self, key: str) -> str:
        """Rebind cached state after a credential is replaced, in place.

        Saving a new key is not enough on its own. The AI gateway remembers which
        endpoint answered and the runner remembers which model ids that endpoint
        served, while the Shariah cache is keyed only by symbol. Each of those
        caches was built with the *old* key, so a replaced key produced a system
        that looked configured but never called the new credential -- it needed a
        restart to recover. Returns a short note for the user, or "".
        """
        if key in ("AI_KEY", "AI_BASE_URL") and self.engine is not None:
            try:
                forget = self.engine.gateway.forget_endpoint()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not rebind the AI endpoint after %s: %s", key, exc)
                return ""
            if forget:
                # The gateway caches which base answered and which model ids it
                # served, both under the old key. A replaced key otherwise leaves
                # a system that looks configured but never calls the new one.
                return "AI endpoint rebound to the new credential."
        if key == "HALALTERMINAL_API_KEY" and self.shariah is not None:
            try:
                cleared = self.shariah.reset_after_credential_change()
            except Exception as exc:  # noqa: BLE001
                log.warning("could not clear the compliance cache: %s", exc)
                return ""
            return f"\u2705 Compliance cache cleared ({cleared} row(s)); re-screening on next check."
        return ""

    def _render_keys_text(self) -> str:
        """Show which settings are configured, masking every secret."""
        important = [
            "FINVIZ_TOKEN", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "AI_BASE_URL", "AI_KEY",
            "HALALTERMINAL_API_KEY", "SEC_USER_AGENT", "BACKUP_PASSPHRASE", "BACKUP_REMOTE",
        ]
        lines = [bold("KEYS")]
        for key in important:
            value = str(self.cfg.raw(key))
            ok = bool(value.strip())
            mark = "\u2705" if ok else "\u274C"
            shown = h(self.cfg.display(key)) if ok else "-"
            lines.append(f"{mark} {code(key)} = {shown}")
        lines.append(divider())
        lines.append("Send <code>/set KEY VALUE</code> to change any of the above.")
        return "\n".join(lines)

    def cmd_keys(self, args) -> None:
        self._reply(self._render_keys_text(), markup=MAIN_MENU)

    def cmd_halal_text(self, symbol: str) -> str:
        status = self.shariah.status(symbol, fetch=True)
        lines = [bold(f"SHARIAH {symbol}")] + self.shariah.detail_lines(status)
        return "\n".join(h(line) for line in lines)

    def cmd_halal(self, args) -> None:
        if not args:
            self._reply("Usage: <code>/halal SYM</code>", markup=MAIN_MENU)
            return
        symbol = args[0].upper()
        if not self.shariah:
            self._reply("Shariah screening is not configured.", markup=MAIN_MENU)
            return
        self._reply(self.cmd_halal_text(symbol), markup=self._check_menu(symbol))

    def cmd_backup(self, args) -> None:
        if not self.backup:
            self._reply("Backup is not configured.")
            return
        sub = args[0].lower() if args else "now"
        if sub != "now":
            self._reply("Usage: <code>/backup now</code>")
            return
        self._typing()
        result = self.backup.run("manual")
        self._reply(f"\u2705 Backup {h(result.status)}: {result.size / 1024:.0f} KB "
                    f"({h(result.note or 'ok')})", markup=MAIN_MENU)

    def cmd_backups(self, args) -> None:
        if not self.backup:
            self._reply("Backup is not configured.")
            return
        self._reply(self.backup.health_text(), html=False, markup=MAIN_MENU)

    def cmd_status(self, args) -> None:
        if not self.engine:
            self._reply("Engine not available.")
            return
        self._reply(self.engine.status_text(), html=False, markup=STATUS_MENU)
