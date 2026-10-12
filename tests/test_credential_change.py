"""Replacing a credential must take effect at once, without a restart.

Each of these pins a cache that used to be built with the *old* key and survive
its replacement: the gateway's resolved endpoint, the runner's panel of model ids,
and the symbol-keyed compliance cache.
"""

from __future__ import annotations

import pytest

from penny.ai.gateway import AIGateway
from penny.shariah import (COMPLIANT, ERROR, HalalTerminalSource, ShariahService,
                            SourceResult)


# --- the gateway re-discovers after the key changes -------------------------

def test_a_new_key_invalidates_the_cached_endpoint(cfg):
    gw = AIGateway(cfg)
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old-key")
    gw._base = "https://gw.example.com/v1"
    gw._base_identity = gw.identity
    assert gw.bases() == ["https://gw.example.com/v1"]   # cached while unchanged

    cfg.set("AI_KEY", "new-key")
    # The endpoint the old key resolved to must not be reused for the new one.
    assert gw.bases() != ["https://gw.example.com/v1"]
    assert gw._base is None
    assert "https://gw.example.com/v1" in gw.bases()    # rediscovered


def test_a_new_base_url_also_invalidates_the_cached_endpoint(cfg):
    gw = AIGateway(cfg)
    cfg.set("AI_BASE_URL", "https://a.example.com")
    cfg.set("AI_KEY", "k")
    gw._base = "https://a.example.com/v1"
    gw._base_identity = gw.identity
    cfg.set("AI_BASE_URL", "https://b.example.com")
    assert gw.bases() != ["https://a.example.com/v1"]
    assert "https://b.example.com/v1" in gw.bases()


def test_forget_endpoint_forces_rediscovery(cfg):
    gw = AIGateway(cfg)
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "k")
    gw._base = "https://gw.example.com/v1"
    gw._base_identity = gw.identity
    gw.forget_endpoint()
    assert gw._base is None
    assert len(gw.bases()) == 2


def test_the_key_is_never_embedded_in_the_identity(cfg):
    gw = AIGateway(cfg)
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "super-secret-value")
    assert "super-secret-value" not in gw.identity


# --- the compliance cache no longer swallows a bad key -----------------------

class Source:
    name = "fake"
    configured = True

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def lookup(self, symbol):
        self.calls += 1
        return self.results.pop(0) if self.results else SourceResult(self.name, ERROR)


def _svc(cfg, state, source):
    return ShariahService(cfg, state, [source])


def test_a_failed_lookup_is_not_stored_as_a_cached_answer(cfg, state):
    src = Source([SourceResult("fake", ERROR, detail={"error": "bad key"})])
    svc = _svc(cfg, state, src)
    svc.status("ABCD", fetch=True)
    # Nothing persisted: the failure is retried on the next check instead of
    # being served as a fresh "result" for the whole TTL.
    assert svc.cached("ABCD") == {}
    assert src.calls == 1
    svc.status("ABCD", fetch=True)
    assert src.calls == 2


def test_a_failed_lookup_does_not_poison_an_earlier_good_result(cfg, state):
    good = SourceResult("fake", COMPLIANT)
    src = Source([good, SourceResult("fake", ERROR, detail={"error": "expired"})])
    svc = _svc(cfg, state, src)
    svc.status("ABCD", fetch=True)
    assert svc.cached("ABCD")["fake"].status == COMPLIANT
    svc.status("ABCD", fetch=True)
    # The stored good result survives; the failure is not written over it.
    assert svc.cached("ABCD")["fake"].status == COMPLIANT


def test_changing_the_key_clears_the_compliance_cache(cfg, state):
    src = Source([SourceResult("fake", COMPLIANT)])
    svc = _svc(cfg, state, src)
    cfg.set("HALALTERMINAL_API_KEY", "old")
    svc.status("ABCD", fetch=True)
    assert svc.cached("ABCD")

    cleared = svc.reset_after_credential_change()
    assert cleared >= 1
    assert svc.cached("ABCD") == {}


def test_the_monthly_call_counter_resets_with_the_key(cfg, state):
    src = Source([SourceResult("fake", COMPLIANT)])
    svc = _svc(cfg, state, src)
    cfg.set("SHARIAH_MAX_CALLS_MONTH", "5")
    svc.status("ABCD", fetch=True)
    svc.status("EFGH", fetch=True)
    assert svc._calls
    svc.reset_after_credential_change()
    assert svc._calls == {}


def test_the_source_reads_the_key_on_every_call(cfg):
    """The client must not hold the key from construction time."""
    src = HalalTerminalSource(cfg)
    cfg.set("HALALTERMINAL_API_KEY", "key-one")
    assert src.configured is True
    cfg.set("HALALTERMINAL_API_KEY", "")
    assert src.configured is False
    cfg.set("HALALTERMINAL_API_KEY", "key-two")
    assert src.configured is True


# --- /set does the rebinding for you ----------------------------------------

class _Gateway:
    """Stands in for the engine's gateway; records the endpoint reset."""

    configured = True

    def __init__(self):
        self.forgot = 0

    def forget_endpoint(self):
        self.forgot += 1
        return True


class _Shariah:
    def __init__(self):
        self.cleared = 0

    def reset_after_credential_change(self):
        self.cleared += 1
        return 7


class _Telegram:
    def __init__(self):
        self.outbox = []

    def send(self, text, **kw):
        self.outbox.append(text)
        return 1

    def delete(self, *a, **kw):
        return True


class _DB:
    def execute(self, sql, params=()):
        return None


def _handler(cfg, *, gateway=None, shariah=None):
    from penny.commands import CommandHandler
    tg = _Telegram()
    engine = None
    if gateway is not None:
        class _Engine:
            pass
        engine = _Engine()
        engine.gateway = gateway
    h = CommandHandler(cfg, db=_DB(), journal=None, telegram=tg,
                       engine=engine, shariah=shariah)
    return h, tg


def test_setting_the_ai_key_rebinds_the_endpoint(cfg):
    gateway = _Gateway()
    handler, tg = _handler(cfg, gateway=gateway)
    handler.cmd_set(["AI_KEY", "brand-new-key"])
    assert gateway.forgot == 1
    assert any("AI endpoint rebound" in m for m in tg.outbox)


def test_setting_the_base_url_rebinds_the_endpoint(cfg):
    gateway = _Gateway()
    handler, _tg = _handler(cfg, gateway=gateway)
    handler.cmd_set(["AI_BASE_URL", "https://other.example.com"])
    assert gateway.forgot == 1


def test_setting_the_halal_key_clears_the_compliance_cache(cfg):
    shariah = _Shariah()
    handler, tg = _handler(cfg, shariah=shariah)
    handler.cmd_set(["HALALTERMINAL_API_KEY", "brand-new-key"])
    assert shariah.cleared == 1
    assert any("Compliance cache cleared (7 row(s))" in m for m in tg.outbox)


def test_setting_an_unrelated_key_does_not_rebind_anything(cfg):
    gateway = _Gateway()
    shariah = _Shariah()
    handler, _tg = _handler(cfg, gateway=gateway, shariah=shariah)
    handler.cmd_set(["MAX_PRICE", "9"])
    assert gateway.forgot == 0
    assert shariah.cleared == 0


def test_a_gateway_that_lists_nothing_still_rebinds_cleanly(cfg):
    """No /models endpoint is normal; the rebind must still succeed."""
    class _NoList(_Gateway):
        def forget_endpoint(self):
            self.forgot += 1
            return False

    gateway = _NoList()
    handler, tg = _handler(cfg, gateway=gateway)
    handler.cmd_set(["AI_KEY", "k"])
    assert gateway.forgot == 1
    assert any("AI_KEY" in m for m in tg.outbox)


def test_the_new_key_is_saved_and_the_old_one_is_gone(cfg):
    handler, _tg = _handler(cfg, gateway=_Gateway())
    cfg.set("AI_KEY", "old-key-value")
    handler.cmd_set(["AI_KEY", "new-key-value"])
    assert cfg.raw("AI_KEY") == "new-key-value"
    assert "new-key-value" not in cfg.display("AI_KEY")
