"""Custom OpenAI-compatible provider: panel sync and self-healing.

Covers the failure mode where alerts carry no AI text because the shipped
example model ids are absent from the operator's provider, and where a
`/set AI_MODELS` fix would otherwise be shadowed by the stored panel.
"""

from __future__ import annotations

from penny.ai.catalog import ModelCatalog
from penny.ai.gateway import FakeGateway
from penny.ai.runner import AIRunner


class _FakeDB:
    def __init__(self):
        self.kv: dict[str, object] = {}
        self.rows: list[tuple] = []

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = value

    def execute(self, sql, params=()):
        self.rows.append((sql, params))

        class _Cur:
            lastrowid = len(self.rows)
        return _Cur()

    def scalar(self, sql, params=(), default=None):
        return default


def _runner(cfg, gateway, db):
    catalog = ModelCatalog(cfg.path.parent / "models.json")
    return AIRunner(cfg, gateway, catalog, db, secrets=[])


# --- /set AI_MODELS takes effect without a restart -------------------------

def test_set_models_syncs_the_stored_panel(cfg):
    """A corrected model list must replace the DB snapshot, not be shadowed."""
    from penny.commands import CommandHandler
    from penny.telegram import FakeTelegram

    db = _FakeDB()
    db.kv_set("active_panel", ["deepseek-v4.1-flash", "kimi-k3", "glm-5.3"])
    telegram = FakeTelegram(cfg)
    handler = CommandHandler(cfg, db, None, telegram)

    handler.cmd_set(["AI_MODELS", "provider-a,provider-b,provider-c"])

    assert db.kv_get("active_panel") == ["provider-a", "provider-b", "provider-c"]
    assert db.kv_get("active_panel_source") == ["provider-a", "provider-b", "provider-c"]


def test_set_unrelated_key_does_not_touch_the_panel(cfg):
    from penny.commands import CommandHandler
    from penny.telegram import FakeTelegram

    db = _FakeDB()
    db.kv_set("active_panel", ["keep-me"])
    handler = CommandHandler(cfg, db, None, FakeTelegram(cfg))

    handler.cmd_set(["RISK_USD", "75"])

    assert db.kv_get("active_panel") == ["keep-me"]


# --- self-healing when none of the configured ids exist --------------------

def test_discovery_adopts_provider_models_when_none_match(cfg):
    cfg.set("AI_MODELS", "deepseek-v4.1-flash,kimi-k3,glm-5.3")
    db = _FakeDB()
    db.kv_set("active_panel", ["deepseek-v4.1-flash", "kimi-k3", "glm-5.3"])
    gateway = FakeGateway(cfg, models=["provider-a", "provider-b", "provider-c"])
    runner = _runner(cfg, gateway, db)

    result = runner.discovery()

    assert result["adopted_panel"] == ["provider-a", "provider-b", "provider-c"]
    assert db.kv_get("active_panel") == ["provider-a", "provider-b", "provider-c"]
    assert runner.active_panel() == ["provider-a", "provider-b", "provider-c"]


def test_discovery_keeps_a_matching_panel(cfg):
    cfg.set("AI_MODELS", "provider-a,provider-b")
    db = _FakeDB()
    db.kv_set("active_panel", ["provider-a", "provider-b"])
    gateway = FakeGateway(cfg, models=["provider-a", "provider-b", "other"])
    runner = _runner(cfg, gateway, db)

    result = runner.discovery()

    assert result["adopted_panel"] == []
    assert db.kv_get("active_panel") == ["provider-a", "provider-b"]


def test_adopt_skips_models_that_fail_their_test(cfg):
    db = _FakeDB()
    db.kv_set("active_panel", ["nope-1", "nope-2", "nope-3"])
    gateway = FakeGateway(cfg, models=["dead-model", "live-model"])
    gateway.fail("dead-model")
    runner = _runner(cfg, gateway, db)

    adopted = runner._adopt_panel(["dead-model", "live-model"])

    assert adopted == ["live-model"]
    assert runner.active_panel() == ["live-model"]


def test_discovery_without_a_model_list_does_not_adopt(cfg):
    db = _FakeDB()
    db.kv_set("active_panel", ["kept"])
    gateway = FakeGateway(cfg, listing_error="HTTP 404 (no /models)")
    runner = _runner(cfg, gateway, db)

    result = runner.discovery()

    assert result["unavailable_list"] is True
    assert result["adopted_panel"] == []
    assert db.kv_get("active_panel") == ["kept"]


# --- doctor pinpoints the "example ids" failure ----------------------------

def test_doctor_flags_example_ids_that_the_provider_lacks(cfg):
    from penny.doctor import run_doctor
    from penny.runtime import build
    from penny.telegram import FakeTelegram

    cfg.set("AI_BASE_URL", "https://gw.example.com/v1")
    cfg.set("AI_KEY", "sk-x")
    cfg.set("AI_MODELS", "deepseek-v4.1-flash,kimi-k3,glm-5.3")

    gateway = FakeGateway(cfg, models=["provider-a", "provider-b"])
    runtime = build(cfg, with_engine=False, console_logs=False,
                    fake={"gateway": gateway, "telegram": FakeTelegram(cfg)})
    try:
        report = run_doctor(runtime, test_models=True)
    finally:
        runtime.close()

    text = report.text()
    assert "AI models configured" in text
    assert "provider-a" in text
    assert not report.ok


def test_doctor_passes_with_the_providers_real_ids(cfg):
    from penny.doctor import run_doctor
    from penny.runtime import build
    from penny.telegram import FakeTelegram

    cfg.set("AI_BASE_URL", "https://gw.example.com/v1")
    cfg.set("AI_KEY", "sk-x")
    cfg.set("AI_MODELS", "provider-a,provider-b")
    cfg.set("AI_FALLBACK_MODELS", "")

    gateway = FakeGateway(cfg, models=["provider-a", "provider-b"])
    runtime = build(cfg, with_engine=False, console_logs=False,
                    fake={"gateway": gateway, "telegram": FakeTelegram(cfg)})
    try:
        report = run_doctor(runtime, test_models=True)
    finally:
        runtime.close()

    text = report.text()
    assert "AI model provider-a: 40 ms" in text
    assert "AI models configured" not in text
