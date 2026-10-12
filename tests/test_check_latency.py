"""Interactive-path latency guards.

`/check` used to hang for minutes because (a) the AI gateway replayed every body
variant against an unreachable host, multiplying one 45s timeout into many, and
(b) the runner had no total budget and called fallbacks sequentially. These
tests pin the bound so the regression cannot return.
"""

from __future__ import annotations

import time

import pytest

from penny.ai.gateway import AIGateway, FakeGateway


# --- gateway: a hung host is not retried once per body variant --------------

class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _install(monkeypatch, post_handler, get_handler=None):
    class _FakeRequests:
        def __init__(self):
            self.posts: list[str] = []

        def get(self, url, headers=None, timeout=None):
            return get_handler(url) if get_handler else _Resp(404)

        def post(self, url, headers=None, json=None, timeout=None, **kw):
            self.posts.append(url)
            return post_handler(url, headers=headers, json=json, timeout=timeout, **kw)

    fake = _FakeRequests()
    monkeypatch.setattr("penny.ai.gateway.requests", fake, raising=False)
    import sys
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def test_transport_error_is_not_replayed_for_every_body_variant(cfg, monkeypatch):
    """A dead host must cost one request per endpoint, not one per variant."""
    def post(url, **kw):
        raise TimeoutError("host never answers")

    fake = _install(monkeypatch, post)
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "sk-test")
    gw = AIGateway(cfg)
    result = gw.chat("m", [{"role": "user", "content": "hi"}], timeout=0.01, retries=0)
    assert not result.ok
    # Two endpoints (bare + /v1), one request each, not five variants each.
    assert len(fake.posts) <= 2


def test_body_adaptation_still_happens_after_a_real_400(cfg, monkeypatch):
    """The optimization must not break the json_mode/token-field adaptation."""
    seen: list[dict] = []

    def post(url, **kw):
        # endpoint is the bare root (no /v1), provider rejects max_tokens
        body = {"max_tokens": "x"}
        seen.append(body)
        if url.endswith("/v1/chat/completions"):
            return _Resp(404, text="no /v1")
        return _Resp(400, text="'max_tokens' is not supported")

    fake = _install(monkeypatch, post)
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "sk-test")
    gw = AIGateway(cfg)
    result = gw.chat("m", [{"role": "user", "content": "hi"}], retries=0)
    assert not result.ok
    # A 400 is a response, so adaptation may still try several body shapes.
    assert len(fake.posts) >= 2


def test_deadline_stops_the_variant_loop(cfg, monkeypatch):
    calls = {"n": 0}

    def post(url, **kw):
        calls["n"] += 1
        time.sleep(0.02)
        return _Resp(400, text="bad field")

    _install(monkeypatch, post)
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "sk-test")
    gw = AIGateway(cfg)
    deadline = time.monotonic() + 0.05
    result = gw.chat("m", [{"role": "user", "content": "hi"}], retries=0,
                     deadline=deadline)
    assert not result.ok
    assert calls["n"] <= 4          # stopped early instead of finishing all variants


# --- the one decision call is bounded too -----------------------------------

class _SlowGateway(FakeGateway):
    """FakeGateway that takes `delay` seconds and respects a deadline."""

    def __init__(self, *a, delay=0.0, fail=(), **kw):
        super().__init__(*a, **kw)
        self.delay = delay
        for model in fail:
            self.fail(model)
        self.seen_deadlines: list[float | None] = []

    def chat(self, model, messages, *, timeout=45.0, deadline=None, **kw):
        self.seen_deadlines.append(deadline)
        if model in self._fail_models:
            return super().chat(model, messages, timeout=timeout, deadline=deadline, **kw)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                from penny.ai.gateway import ChatResult
                return ChatResult(model=model, ok=False, error="deadline exceeded")
            time.sleep(min(self.delay, remaining))
        else:
            time.sleep(self.delay)
        return super().chat(model, messages, timeout=timeout, deadline=deadline, **kw)


def _facts():
    return {"symbol": "TEST", "price": 1.0, "metrics": {}, "news": [],
            "cause": ["CAUSE: x"],
            "cause_detail": {"headline": "x", "outlet": "s",
                             "age_minutes": 2.0, "class": "HARD", "url": "u"}}


def _passing_report() -> dict:
    return {"catalyst_quality": "real",
            "strongest_risk": "volume rests on one print, not the tape",
            "key_findings": ["reclaimed VWAP on 9x volume"],
            "rating": {"grade": "MODERATE", "confidence": 0.6},
            "trends": {"direction": "rising", "momentum": "3m accel"},
            "entry_points": [{"zone": "1.00-1.02"}],
            "exit_points": {"stop": "0.95", "t1": "1.10"}, "verdict": "ENTER"}


def test_the_deadline_clamps_the_per_request_timeout(cfg, monkeypatch):
    """A long AI_TIMEOUT must not outlive the deadline the caller set."""
    from penny.ai.gateway import AIGateway
    cfg.set("AI_TIMEOUT", "45")
    seen: dict = {}

    class _Post:
        status_code = 500
        text = "boom"

        def json(self):
            return {}

    def post(url, headers=None, json=None, timeout=None, **kw):
        seen["timeout"] = timeout
        return _Post()

    fake = _install(monkeypatch, post)
    cfg.set("AI_BASE_URL", "https://gw.example.com/v1")
    cfg.set("AI_KEY", "sk-test")
    gw = AIGateway(cfg)
    deadline = time.monotonic() + 2.0
    gw.chat("m", [{"role": "user", "content": "hi"}], timeout=45.0, deadline=deadline)
    assert fake.posts
    assert seen["timeout"] <= 2.05


def test_one_slow_decision_call_costs_one_budget(cfg):
    """One call, one deadline: a 5s model must not become 5s x panel size."""
    from penny.ai.decision import decide
    cfg.set("AI_MODEL", "slow-a")
    cfg.set("AI_TIMEOUT", "5")
    gw = _SlowGateway(cfg, delay=5.0, models=["slow-a"])
    started = time.monotonic()
    decide(gw, _facts(), model="slow-a", deadline=time.monotonic() + 0.3)
    elapsed = time.monotonic() - started
    # Without the deadline each model would sleep the full 5s.
    assert elapsed < 2.0
    assert any(d is not None for d in gw.seen_deadlines)


def test_a_valid_report_comes_back_as_the_decision(cfg):
    from penny.ai.decision import decide
    cfg.set("AI_MODEL", "m")
    gw = FakeGateway(cfg, models=["m"])
    d = decide(gw, _facts(), model="m")
    assert d.ok, d.error
    assert d.verdict == "ENTER"


# --- enrichment: one Finviz verify per symbol -------------------------------

def test_finviz_verify_is_fetched_once(cfg):
    """short-interest and verification share one Finviz row, not two calls."""
    from penny.enrich import Enricher

    calls = {"n": 0}

    class _Finviz:
        def verify(self, symbol):
            calls["n"] += 1
            return {"Price": "1.10", "Volume": "1000000", "Short Float": "12.5"}

        def news(self, view):
            return []

        def latest_filings(self, symbol):
            return []

        def insiders(self, symbol):
            return []

    enricher = Enricher(cfg, finviz=_Finviz())
    quote = {"price": 1.10, "cum_volume": 1_000_000}
    enr = enricher.enrich("TEST", quote)
    assert calls["n"] == 1
    assert enricher.short_text(enr) and "short float 12.5%" in enricher.short_text(enr)
