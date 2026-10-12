"""A custom OpenAI-compatible provider, and the one model id it is given.

Covers the failure mode where alerts carry no AI text because the shipped
example model id is absent from the operator's provider, and pins that `/set
AI_MODEL` takes effect without a restart.
"""

from __future__ import annotations

from penny.ai.gateway import FakeGateway


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


# --- /set AI_MODEL takes effect without a restart ----------------------------

def test_set_model_is_applied_immediately(cfg):
    from penny.commands import CommandHandler
    from penny.telegram import FakeTelegram

    db = _FakeDB()
    telegram = FakeTelegram(cfg)
    handler = CommandHandler(cfg, db, None, telegram)

    handler.cmd_set(["AI_MODEL", "provider-a"])

    assert cfg.raw("AI_MODEL") == "provider-a"
    assert any("AI_MODEL" in m for m in telegram.outbox)


def test_set_unrelated_key_does_not_touch_the_model(cfg):
    from penny.commands import CommandHandler
    from penny.telegram import FakeTelegram

    before = cfg.raw("AI_MODEL")
    CommandHandler(cfg, _FakeDB(), None, FakeTelegram(cfg)).cmd_set(["RISK_USD", "75"])

    assert cfg.raw("AI_MODEL") == before


def test_a_shipped_example_id_the_provider_lacks_yields_no_decision(cfg):
    """The exact complaint this module exists for: ids that cannot answer."""
    from penny.ai.decision import decide

    cfg.set("AI_MODEL", "deepseek-v4.1-flash")
    gateway = FakeGateway(cfg, models=["provider-a", "provider-b"])
    gateway.fail("deepseek-v4.1-flash")

    facts = {"symbol": "TEST", "price": 1.0, "news": [],
             "gate": {"passed": True, "primary": None, "entry_trigger": None,
                      "gate_score": 70.0, "blocked_by": ""}}
    d = decide(gateway, facts, model="deepseek-v4.1-flash")

    assert not d.ok
    assert d.verdict == "PASS"


def test_a_working_model_produces_one_decision(cfg):
    from penny.ai.decision import decide

    cfg.set("AI_MODEL", "provider-a")
    gateway = FakeGateway(cfg, models=["provider-a", "provider-b"])

    facts = {"symbol": "TEST", "price": 1.0, "news": [],
             "gate": {"passed": True, "primary": [], "triggers": [], "blockers": []}}
    d = decide(gateway, facts, model=cfg.raw("AI_MODEL"))

    assert d.ok, d.error
    assert d.verdict == "ENTER"
    # One alert, one call.
    assert len(gateway.calls) == 1


# --- doctor pinpoints the "example id" failure ------------------------------

def test_doctor_flags_an_example_id_that_the_provider_lacks(cfg):
    from penny.doctor import run_doctor
    from penny.runtime import build
    from penny.telegram import FakeTelegram

    cfg.set("AI_BASE_URL", "https://gw.example.com/v1")
    cfg.set("AI_KEY", "sk-x")
    cfg.set("AI_MODEL", "deepseek-v4.1-flash")

    gateway = FakeGateway(cfg, models=["provider-a", "provider-b"])
    runtime = build(cfg, with_engine=False, console_logs=False,
                    fake={"gateway": gateway, "telegram": FakeTelegram(cfg)})
    try:
        report = run_doctor(runtime, test_models=True)
    finally:
        runtime.close()

    text = report.text()
    assert "AI model configured" in text
    assert "provider-a" in text
    assert not report.ok


def test_doctor_passes_with_the_providers_real_id(cfg):
    from penny.doctor import run_doctor
    from penny.runtime import build
    from penny.telegram import FakeTelegram

    cfg.set("AI_BASE_URL", "https://gw.example.com/v1")
    cfg.set("AI_KEY", "sk-x")
    cfg.set("AI_MODEL", "provider-a")

    gateway = FakeGateway(cfg, models=["provider-a", "provider-b"])
    runtime = build(cfg, with_engine=False, console_logs=False,
                    fake={"gateway": gateway, "telegram": FakeTelegram(cfg)})
    try:
        report = run_doctor(runtime, test_models=True)
    finally:
        runtime.close()

    text = report.text()
    assert "AI model provider-a: 40 ms" in text
    assert "AI model configured" not in text
