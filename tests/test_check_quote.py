"""`/check` quote freshness: fetch on demand, including outside the sub-$10 universe."""

from __future__ import annotations

from penny.engine import Engine


class _FakeFinviz:
    def __init__(self):
        self.calls: list[list[str]] = []

    def snapshot(self, symbols, session=None):
        self.calls.append([str(s).upper() for s in symbols])
        rows = {s.upper(): {"symbol": s.upper(), "price": 1.23, "cum_volume": 5000.0,
                            "prev_close": 1.00, "avg_volume": 100000.0, "ts": 1.0,
                            "session": session} for s in symbols}
        return [rows[str(s).upper()] for s in symbols]

    def normalize_row(self, row, session):
        return None


def _engine(cfg):
    finviz = _FakeFinviz()
    eng = Engine(cfg, None, None, None, finviz=finviz)
    return eng, finviz


def test_check_quote_fetches_a_symbol_outside_the_universe(cfg):
    """NVDA/AAPL are never in the sub-$10 universe; /check must still quote them."""
    eng, finviz = _engine(cfg)
    eng._quotes_by_symbol = {}  # nothing cached this cycle
    quote = eng.check_quote("nvda")
    assert quote is not None
    assert quote["symbol"] == "NVDA"
    assert quote["price"] == 1.23
    assert finviz.calls == [["NVDA"]]


def test_check_quote_prefers_the_fresh_print_over_the_cached_one(cfg):
    eng, finviz = _engine(cfg)
    eng._quotes_by_symbol = {"ABCD": {"symbol": "ABCD", "price": 9.99}}
    quote = eng.check_quote("ABCD")
    # The on-demand fetch wins, so the price is current (including after-hours).
    assert quote["price"] == 1.23
    assert finviz.calls == [["ABCD"]]


def test_check_quote_falls_back_to_cache_when_the_feed_fails(cfg):
    eng, finviz = _engine(cfg)

    def boom(symbols, session=None):
        raise RuntimeError("network down")

    finviz.snapshot = boom
    eng._quotes_by_symbol = {"ABCD": {"symbol": "ABCD", "price": 9.99}}
    assert eng.check_quote("ABCD")["price"] == 9.99


def test_check_quote_is_none_for_a_blank_symbol(cfg):
    eng, _ = _engine(cfg)
    assert eng.check_quote("  ") is None
