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


# --- the panel rebinds after a key change ------------------------------------

class FakeGateway:
    configured = True

    def __init__(self, models):
        self.models = list(models)
        self.forgot = False

    def forget_endpoint(self):
        self.forgot = True

    def list_models(self, timeout=15.0):
        return list(self.models)

    def test_model(self, model, timeout=20.0):
        class R:
            ok = True
            latency_ms = 1.0
        return R()


class FakeCatalog:
    def get(self, model_id):
        return None

    def add(self, model_id, multiplier, enabled=True, notes=""):
        class E:
            available = True
            latency_ms = 1.0
        return E()

    def save(self):
        pass

    def is_alias(self, model_id):
        return False

    def pool(self, *a, **kw):
        return []


def _runner(cfg, state, models):
    from penny.ai.runner import AIRunner
    return AIRunner(cfg, FakeGateway(models), FakeCatalog(), state)


def test_the_provider_identity_changes_when_only_the_key_changes(cfg, state):
    r = _runner(cfg, state, ["m1"])
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old")
    before = r.provider_id()
    cfg.set("AI_KEY", "new")
    assert r.provider_id() != before
    assert "old" not in r.provider_id() and "new" not in r.provider_id()


def test_a_key_change_resets_the_panel_to_the_new_provider_models(cfg, state):
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old")
    cfg.set("AI_MODELS", "old-model-a,old-model-b")
    r = _runner(cfg, state, ["elf/new-model-1", "elf/new-model-2"])
    r.db.kv_set("active_panel", ["old-model-a", "old-model-b"])
    r.db.kv_set("ai_provider", r.provider_id())

    cfg.set("AI_KEY", "new")
    panel = r.reset_after_credential_change()
    # The stale panel is gone; ids come from the provider that owns the new key.
    assert "old-model-a" not in panel
    assert panel == ["elf/new-model-1", "elf/new-model-2"]
    assert r.db.kv_get("active_panel") == ["elf/new-model-1", "elf/new-model-2"]


def test_a_key_change_falls_back_to_the_configured_models(cfg, state):
    """A provider that exposes no /models must not leave the panel empty."""
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old")
    cfg.set("AI_MODELS", "configured-a,configured-b")
    r = _runner(cfg, state, [])

    class NoList(FakeGateway):
        def list_models(self, timeout=15.0):
            raise RuntimeError("no /models endpoint")

    r.gateway = NoList([])
    cfg.set("AI_KEY", "new")
    assert r.reset_after_credential_change() == ["configured-a", "configured-b"]


def test_a_key_change_clears_the_analysis_cache(cfg, state):
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old")
    r = _runner(cfg, state, [])
    r.cache.put("ABCD", {"n_models": 1}, price=1.0, tier=2)
    cfg.set("AI_KEY", "new")
    r.reset_after_credential_change()
    assert r.cache.get("ABCD", price=1.0, tier=2) is None


def test_a_key_change_tells_the_gateway_to_forget_its_endpoint(cfg, state):
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old")
    r = _runner(cfg, state, [])
    cfg.set("AI_KEY", "new")
    r.reset_after_credential_change()
    assert r.gateway.forgot is True


def test_sync_provider_notices_a_key_only_change(cfg, state):
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "old")
    cfg.set("AI_MODELS", "old-a,old-b")
    r = _runner(cfg, state, ["elf/fresh"])
    r.db.kv_set("active_panel", ["old-a", "old-b"])
    r.db.kv_set("ai_provider", r.provider_id())
    cfg.set("AI_KEY", "new")
    assert r.sync_provider() is True
    assert "old-a" not in r.active_panel()


def test_sync_provider_is_a_no_op_when_nothing_changed(cfg, state):
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    cfg.set("AI_KEY", "same")
    r = _runner(cfg, state, ["elf/fresh"])
    r.db.kv_set("ai_provider", r.provider_id())
    assert r.sync_provider() is False


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

class _Runner:
    def __init__(self, panel):
        self.panel = panel
        self.calls = 0

    def reset_after_credential_change(self):
        self.calls += 1
        return list(self.panel)


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


def _handler(cfg, *, runner=None, shariah=None):
    from penny.commands import CommandHandler
    tg = _Telegram()
    h = CommandHandler(cfg, db=_DB(), journal=None, telegram=tg,
                       runner=runner, shariah=shariah)
    return h, tg


def test_setting_the_ai_key_rebinds_the_panel(cfg):
    runner = _Runner(["elf/new-1", "elf/new-2"])
    handler, tg = _handler(cfg, runner=runner)
    handler.cmd_set(["AI_KEY", "brand-new-key"])
    assert runner.calls == 1
    assert any("AI panel reloaded" in m for m in tg.outbox)
    assert any("elf/new-1" in m for m in tg.outbox)


def test_setting_the_base_url_rebinds_the_panel(cfg):
    runner = _Runner(["m1"])
    handler, _tg = _handler(cfg, runner=runner)
    handler.cmd_set(["AI_BASE_URL", "https://other.example.com"])
    assert runner.calls == 1


def test_setting_the_halal_key_clears_the_compliance_cache(cfg):
    shariah = _Shariah()
    handler, tg = _handler(cfg, shariah=shariah)
    handler.cmd_set(["HALALTERMINAL_API_KEY", "brand-new-key"])
    assert shariah.cleared == 1
    assert any("Compliance cache cleared (7 row(s))" in m for m in tg.outbox)


def test_setting_an_unrelated_key_does_not_rebind_anything(cfg):
    runner = _Runner(["m1"])
    shariah = _Shariah()
    handler, _tg = _handler(cfg, runner=runner, shariah=shariah)
    handler.cmd_set(["MAX_PRICE", "9"])
    assert runner.calls == 0
    assert shariah.cleared == 0


def test_a_provider_that_lists_nothing_tells_the_user_what_to_do(cfg):
    handler, tg = _handler(cfg, runner=_Runner([]))
    handler.cmd_set(["AI_KEY", "k"])
    assert any("/set AI_MODELS" in m for m in tg.outbox)


def test_the_new_key_is_saved_and_the_old_one_is_gone(cfg):
    runner = _Runner(["m1"])
    handler, _tg = _handler(cfg, runner=runner)
    cfg.set("AI_KEY", "old-key-value")
    handler.cmd_set(["AI_KEY", "new-key-value"])
    assert cfg.raw("AI_KEY") == "new-key-value"
    assert "new-key-value" not in cfg.display("AI_KEY")
