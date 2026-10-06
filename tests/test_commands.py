"""Interactive Telegram UI: markup helpers and command callbacks."""

from __future__ import annotations

from penny import ui
from penny.commands import CommandHandler
from penny.telegram import FakeTelegram
from penny.scoring import Metrics


def test_html_escaping_and_helpers():
    assert ui.h("<b>&") == "&lt;b&gt;&amp;"
    assert ui.bold("x") == "<b>x</b>"
    assert ui.code("ABC") == "<code>ABC</code>"


def test_arrow_and_bar():
    assert ui.arrow_for(1.2).endswith("\U0001F7E2")
    assert ui.arrow_for(-1.2).endswith("\U0001F534")
    assert ui.arrow_for(0.0).endswith("\u26AA")
    assert ui.arrow_for(None) == "-"
    assert len(ui.bar(0.5, width=8)) == 8
    assert ui.bar(None, width=8) == "\u2591" * 8


def test_inline_drops_empty_rows():
    markup = ui.inline([[ui.button("A", "a")], [], [None]])
    assert markup == {"inline_keyboard": [[{"text": "A", "callback_data": "a"}]]}
    assert ui.inline([[]]) is None


def test_callback_roundtrip():
    assert ui.cb("check", "ABCD") == "check:ABCD"
    assert ui.cb("top") == "top"
    assert ui.parse_cb("check:ABCD") == ("check", "ABCD")
    assert ui.parse_cb("top") == ("top", "")


# --- callback handling ------------------------------------------------------

class _FakeScorer:
    def rank(self, metrics, top_n=10):
        return {"champions": list(metrics), "hidden_gems": [],
                "pct_rank": {}, "by_score": list(metrics)}


class _FakeMarket:
    class _Ctx:
        regime = "risk-on"
        quotes = {"SPY": {"price": 500.0, "change_pct": 0.5, "change_15m": 0.1,
                          "change_60m": 0.2}}
        index_levels = {}
        sectors = [{"name": "Tech", "change_pct": 1.1}]
        events = []
        news = ["headline"]
        missing = []

    def get(self, force=False):
        return self._Ctx()


class _FakeEngine:
    def __init__(self):
        self.scorer = _FakeScorer()
        self.market = _FakeMarket()
        self._last_metrics = [Metrics(symbol="ABCD", ts=0.0, price=1.42, cum_volume=1000,
                                      pct_vs_close=0.31, score=74.0)]

    def status_text(self):
        return "STATUS\nuptime 1h | session regular"


def _handler(tmp_path, cfg):
    telegram = FakeTelegram(cfg)
    handler = CommandHandler(cfg, db=_FakeDB(), journal=None, telegram=telegram,
                             engine=_FakeEngine())
    return handler, telegram


class _FakeDB:
    def __init__(self):
        self.watch = {}

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO watch"):
            self.watch[params[0]] = params
        elif sql.startswith("DELETE FROM watch"):
            self.watch.pop(params[0], None)

    def query(self, sql, params=()):
        return []

    def kv_get(self, key):
        return None

    def kv_set(self, key, value):
        pass


def test_top_callback_sends_buttons(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    cfg.set("TELEGRAM_CHAT_ID", "1")
    handler._handle_callback({"id": "c1", "data": "top",
                              "message": {"message_id": 5, "chat": {"id": "1"}}})
    assert telegram.callbacks  # acknowledged
    assert telegram.keyboards[-1] is not None
    # The message was edited in place rather than sent anew.
    assert telegram.edits and telegram.edits[-1]["id"] == 5


def test_market_callback_is_escaped_and_structured(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    cfg.set("TELEGRAM_CHAT_ID", "1")
    handler._handle_callback({"id": "c1", "data": "market",
                              "message": {"message_id": 7, "chat": {"id": "1"}}})
    text = telegram.edits[-1]["text"]
    assert "MARKET" in text
    assert "SPY" in text
    assert "<b>" in text


def test_unauthorized_callback_is_ignored(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    cfg.set("TELEGRAM_CHAT_ID", "1")
    handled = handler._handle_callback({"id": "c1", "data": "top",
                                        "message": {"message_id": 5,
                                                    "chat": {"id": "999"}}})
    assert handled is False
    assert not telegram.edits


def test_buttons_are_added_to_command_replies(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    handler.cmd_top([])
    assert telegram.keyboards[-1] is not None
    handler.cmd_market([])
    assert telegram.keyboards[-1] is not None
    handler.cmd_keys([])
    assert telegram.keyboards[-1] is not None


def test_top_menu_has_one_button_per_symbol(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    handler.cmd_top([])
    rows = telegram.keyboards[-1]["inline_keyboard"]
    labels = [b["text"] for row in rows for b in row]
    assert any("ABCD" in label for label in labels)
    datas = [b["callback_data"] for row in rows for b in row]
    assert "check:ABCD" in datas
    assert "top" in datas  # refresh


def test_alert_menu_is_tappable():
    from penny.commands import CommandHandler
    from penny.engine import alert_menu
    from penny.ui import parse_cb

    menu = alert_menu("ABCD")
    datas = [b["callback_data"] for row in menu["inline_keyboard"] for b in row]
    assert "check:ABCD" in datas
    assert "halal:ABCD" in datas
    assert "market" in datas
    assert parse_cb("check:ABCD") == ("check", "ABCD")


def test_check_menu_round_trips_symbol(cfg, tmp_path):
    handler, _ = _handler(tmp_path, cfg)
    menu = handler._check_menu("ABCD")
    datas = [b["callback_data"] for row in menu["inline_keyboard"] for b in row]
    assert "check:ABCD" in datas
    assert "halal:ABCD" in datas


def test_start_sends_reply_keyboard(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    handler.cmd_start([])
    # First message carries the persistent reply keyboard.
    first = telegram.messages[0]["markup"]
    assert first and "keyboard" in first
    labels = [b["text"] for row in first["keyboard"] for b in row]
    assert "/top" in labels and "/help" in labels


def test_unknown_command_offers_help(cfg, tmp_path):
    handler, telegram = _handler(tmp_path, cfg)
    handler.handle("/nope")
    assert "Unknown command" in telegram.outbox[-1]
    assert telegram.keyboards[-1] is not None
