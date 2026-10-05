"""Telegram command handling: only from the configured chat id (6.12)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from .ai.leaderboard import apply_selection, leaderboard_text
from .alerts import tier_name
from .util import money, pct, session_for

log = logging.getLogger(__name__)

HELP = """penny commands
/top - champions and hidden gems
/watch SYM ENTRY [STOP] [TARGET] - add to the watchlist
/unwatch SYM - remove from the watchlist
/watchlist - current watchlist
/check SYM - full analysis on demand
/market - market context now
/models - leaderboard and active panel
/models refresh - re-read the provider's model list
/models add ID MULTIPLIER - confirm a new model
/models remove ID - drop a model
/setmodels A,B,C - set the panel manually
/halal SYM - Shariah status now
/backup now - run a backup
/backups - recent backups and destination health
/status - uptime, session, cycle, tokens, backups
/help - this message"""


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
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- loop ---------------------------------------------------------------
    def poll_once(self) -> int:
        updates = self.telegram.get_updates(offset=self._offset, timeout=5)
        handled = 0
        for upd in updates:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            if not self._authorized(chat_id):
                log.warning("ignoring command from unauthorized chat %s", chat_id)
                continue
            self.handle(text)
            handled += 1
        return handled

    def run_forever(self, poll_seconds: float = 2.0) -> None:
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

    def _reply(self, text: str) -> None:
        try:
            self.telegram.send(text)
        except Exception as exc:  # noqa: BLE001
            log.debug("reply failed: %s", exc)

    # -- dispatch -----------------------------------------------------------
    def handle(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        try:
            handler = {
                "/top": self.cmd_top,
                "/watch": self.cmd_watch,
                "/unwatch": self.cmd_unwatch,
                "/watchlist": self.cmd_watchlist,
                "/check": self.cmd_check,
                "/market": self.cmd_market,
                "/models": self.cmd_models,
                "/setmodels": self.cmd_setmodels,
                "/halal": self.cmd_halal,
                "/backup": self.cmd_backup,
                "/backups": self.cmd_backups,
                "/status": self.cmd_status,
                "/help": self.cmd_help,
                "/start": self.cmd_help,
            }.get(cmd)
            if handler is None:
                self._reply(f"Unknown command {cmd}. /help for the list.")
                return
            handler(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("command %s failed: %s", cmd, exc)
            self._reply(f"Command failed: {exc}")

    # -- commands -----------------------------------------------------------
    def cmd_help(self, args) -> None:
        self._reply(HELP)

    def cmd_top(self, args) -> None:
        metrics = getattr(self.engine, "_last_metrics", None) if self.engine else None
        if not metrics:
            self._reply("No scored symbols yet (market may be closed).")
            return
        ranked = self.engine.scorer.rank(metrics)
        lines = ["TOP"]
        if ranked["champions"]:
            lines.append("Champions:")
            for m in ranked["champions"]:
                lines.append(f"  {m.symbol} {money(m.price)} "
                             f"{pct((m.pct_vs_close or 0) * 100)} score {m.score:.0f} "
                             f"{tier_name(m.tier)}"
                             + (" [EXTENDED]" if m.phase == "EXTENDED" else "")
                             + (" [FADING]" if m.phase == "FADING" else ""))
        if ranked["hidden_gems"]:
            lines.append("Hidden gems:")
            for m in ranked["hidden_gems"]:
                lines.append(f"  {m.symbol} {money(m.price)} "
                             f"{pct((m.pct_vs_close or 0) * 100)} score {m.score:.0f}")
        if len(lines) == 1:
            lines.append("  nothing at tier >= 2 yet")
        self._reply("\n".join(lines))

    def cmd_watch(self, args) -> None:
        if not args:
            self._reply("usage: /watch SYM ENTRY [STOP] [TARGET]")
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
            self._reply("usage: /watch SYM ENTRY [STOP] [TARGET]")
            return
        self.db.execute(
            "INSERT INTO watch(symbol, entry, stop, target, ts) VALUES(?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET entry=excluded.entry, stop=excluded.stop, "
            "target=excluded.target, ts=excluded.ts",
            (symbol, entry, stop, target, time.time()))
        self._reply(f"watching {symbol}: entry {money(entry)}"
                    + (f", stop {money(stop)}" if stop else "")
                    + (f", target {money(target)}" if target else ""))

    def cmd_unwatch(self, args) -> None:
        if not args:
            self._reply("usage: /unwatch SYM")
            return
        self.db.execute("DELETE FROM watch WHERE symbol=?", (args[0].upper(),))
        self._reply(f"stopped watching {args[0].upper()}")

    def cmd_watchlist(self, args) -> None:
        rows = self.db.query("SELECT * FROM watch ORDER BY ts")
        if not rows:
            self._reply("watchlist is empty")
            return
        metrics = {m.symbol: m for m in (getattr(self.engine, "_last_metrics", []) or [])}
        lines = ["WATCHLIST"]
        for row in rows:
            m = metrics.get(row["symbol"])
            price = m.price if m else None
            pl = None
            if price and row["entry"]:
                pl = (price / row["entry"] - 1) * 100
            lines.append(f"  {row['symbol']} entry {money(row['entry'])} "
                         + (f"stop {money(row['stop'])} " if row["stop"] else "")
                         + (f"target {money(row['target'])} " if row["target"] else "")
                         + (f"now {money(price)} ({pct(pl)})" if price else "no price yet"))
        self._reply("\n".join(lines))

    def cmd_check(self, args) -> None:
        if not args:
            self._reply("usage: /check SYM")
            return
        symbol = args[0].upper()
        self._reply(f"running a full analysis for {symbol}...")
        threading.Thread(target=self._run_check, args=(symbol,), daemon=True).start()

    def _run_check(self, symbol: str) -> None:
        if not self.engine:
            self._reply("engine not available")
            return
        try:
            from .alerts import build_ai_facts, format_alert2
            from .scoring import Metrics
            quote = self.engine._quotes_by_symbol.get(symbol)
            metrics = {m.symbol: m for m in (self.engine._last_metrics or [])}
            m = metrics.get(symbol)
            if m is None and quote:
                m = self.engine.scorer.compute(quote, session_for())
            if m is None:
                self._reply(f"{symbol}: no quote available right now")
                return
            enr = self.engine.enricher.enrich(symbol, self.engine._quote_for(m), m)
            market = self.engine.market.get(force=True)
            shariah = self.engine._shariah_status(symbol, enr.filings, True)
            shariah_lines = self.engine.shariah.detail_lines(shariah) if self.engine.shariah else []
            risk_usd = self.cfg.float("RISK_USD", 50)
            facts = build_ai_facts(m, enr, market, session=session_for(),
                                   time_to_horizon=self.engine._time_to_horizons())
            reaction = ""
            price_fc = ""
            validated = False
            if self.runner:
                agg, _ = self.runner.analyze(symbol=symbol, facts=facts, price=m.price,
                                             tier=m.tier, kind="check", force=True)
                from .ai.panel import price_forecast_text, reaction_text
                reaction = reaction_text(agg)
                price_fc = price_forecast_text(agg, m.price)
                validated = agg.validated
            text = format_alert2(m, enr, market, shariah_lines=shariah_lines,
                                 reaction=reaction, price_forecast=price_fc,
                                 ai_running=False, validated=validated, risk_usd=risk_usd)
            self._reply(text)
        except Exception as exc:  # noqa: BLE001
            log.exception("check failed for %s: %s", symbol, exc)
            self._reply(f"check failed: {exc}")

    def cmd_market(self, args) -> None:
        if not self.engine:
            self._reply("engine not available")
            return
        ctx = self.engine.market.get(force=True)
        lines = [ctx.headline()]
        for sym, q in ctx.quotes.items():
            lines.append(f"  {sym} {money(q.get('price'))} {pct(q.get('change_pct'))} "
                         f"15m {pct(q.get('change_15m'))} 60m {pct(q.get('change_60m'))}")
        for sym, lv in ctx.index_levels.items():
            lines.append(f"  {sym} {lv.get('price'):,.2f} {pct(lv.get('change_pct'))}")
        if ctx.sectors:
            lines.append("sectors: " + ", ".join(
                f"{s['name']} {pct(s['change_pct'])}" for s in ctx.sectors[:6]))
        if ctx.events:
            lines.append("events: " + "; ".join(
                f"{e.get('time')} {e.get('event')}" for e in ctx.events[:4]))
        if ctx.news:
            lines.append("news: " + " | ".join(ctx.news[:3]))
        if ctx.missing:
            lines.append("missing: " + ", ".join(ctx.missing))
        self._reply("\n".join(lines))

    def cmd_models(self, args) -> None:
        if not self.catalog or not self.runner:
            self._reply("models are not configured")
            return
        sub = args[0].lower() if args else ""
        if sub == "refresh":
            result = self.runner.discovery(self.telegram)
            self._reply("model list refreshed.\n"
                        f"listed: {len(result['listed'])}\n"
                        f"failed test: {', '.join(result['failed_tests']) or 'none'}\n"
                        f"new (confirm with /models add): "
                        f"{', '.join(result['unknown_to_user']) or 'none'}")
            return
        if sub == "add":
            if len(args) < 3:
                self._reply("usage: /models add ID MULTIPLIER")
                return
            try:
                multiplier = float(args[2])
            except ValueError:
                self._reply("multiplier must be a number")
                return
            self.catalog.add(args[1], multiplier)
            self._reply(f"added {args[1]} at {multiplier}x")
            return
        if sub == "remove":
            if len(args) < 2:
                self._reply("usage: /models remove ID")
                return
            ok = self.catalog.remove(args[1])
            self._reply(f"removed {args[1]}" if ok else f"{args[1]} was not in the catalogue")
            return
        if sub == "apply":
            panel, changed = apply_selection(self.db, self.cfg, self.catalog, self.telegram)
            self._reply(f"panel {'changed to ' if changed else 'unchanged: '}"
                        + ", ".join(panel))
            return
        if sub == "set":
            ids = []
            for token in (args[1] if len(args) > 1 else "").split(","):
                if ":" in token:
                    model, _, mult = token.partition(":")
                    self.catalog.add(model, float(mult or 1.0))
                ids.append(token.split(":")[0])
            self._reply("model ids recorded: " + ", ".join(ids))
            return
        self._reply(leaderboard_text(self.db, self.cfg, self.catalog))

    def cmd_setmodels(self, args) -> None:
        if not args:
            self._reply("usage: /setmodels A,B,C")
            return
        models = [m.strip() for m in args[0].split(",") if m.strip()]
        if len(models) < 1:
            self._reply("usage: /setmodels A,B,C")
            return
        self.db.kv_set("active_panel", models)
        self._reply("active panel set to: " + ", ".join(models))
        if self.backup:
            self.backup.run("panel-change")

    def cmd_halal(self, args) -> None:
        if not args:
            self._reply("usage: /halal SYM")
            return
        symbol = args[0].upper()
        if not self.shariah:
            self._reply("Shariah screening is not configured")
            return
        status = self.shariah.status(symbol, fetch=True)
        self._reply("\n".join(self.shariah.detail_lines(status)))

    def cmd_backup(self, args) -> None:
        if not self.backup:
            self._reply("backup is not configured")
            return
        sub = args[0].lower() if args else "now"
        if sub != "now":
            self._reply("usage: /backup now")
            return
        self._reply("running a backup...")
        result = self.backup.run("manual")
        self._reply(f"backup {result.status}: {result.size / 1024:.0f} KB "
                    f"({result.note or 'ok'})")

    def cmd_backups(self, args) -> None:
        if not self.backup:
            self._reply("backup is not configured")
            return
        self._reply(self.backup.health_text())

    def cmd_status(self, args) -> None:
        if not self.engine:
            self._reply("engine not available")
            return
        self._reply(self.engine.status_text())
