"""AI gateway: endpoint discovery for OpenAI-compatible providers.

Covers the OpenCode-style case: a base URL that may or may not include `/v1`,
providers with no `/models` endpoint, and bodies where `max_tokens` is rejected
in favour of `max_completion_tokens`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from penny.ai.gateway import AIGateway, ChatResult


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeRequests:
    """Records requests and answers only the URLs a handler accepts."""

    def __init__(self, get_handler=None, post_handler=None):
        self.get_handler = get_handler
        self.post_handler = post_handler
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return self.get_handler(url)

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append((url, json))
        return self.post_handler(url, json)


@pytest.fixture()
def patched(monkeypatch):
    holder = {}

    def install(fake):
        holder["fake"] = fake
        monkeypatch.setattr("penny.ai.gateway.requests", fake, raising=False)
        # gateway imports requests lazily inside the method, so patch the module.
        import sys
        monkeypatch.setitem(sys.modules, "requests", fake)
        return fake

    return install


def _cfg(cfg, base):
    cfg.set("AI_BASE_URL", base)
    cfg.set("AI_KEY", "sk-test")
    return cfg


# --- URL normalization -----------------------------------------------------

@pytest.mark.parametrize("entered,first", [
    ("https://gw.example.com", "https://gw.example.com/v1"),
    ("https://gw.example.com/", "https://gw.example.com/v1"),
    ("https://gw.example.com/v1", "https://gw.example.com/v1"),
    ("https://gw.example.com/v1/", "https://gw.example.com/v1"),
    ("https://gw.example.com/v1/chat/completions", "https://gw.example.com/v1"),
    ("https://gw.example.com/openai", "https://gw.example.com/openai/v1"),
])
def test_endpoint_candidates(cfg, entered, first):
    gw = AIGateway(_cfg(cfg, entered))
    assert gw.endpoints[0] == f"{first}/chat/completions"
    for endpoint in gw.endpoints:
        assert "/v1/v1/" not in endpoint


def test_configured_requires_both_url_and_key(cfg):
    gateway = AIGateway(cfg)
    assert not gateway.configured
    cfg.set("AI_BASE_URL", "https://gw.example.com")
    assert not gateway.configured
    cfg.set("AI_KEY", "sk-test")
    assert gateway.configured


# --- live behaviour with a fake transport ----------------------------------

def test_chat_skips_json_mode_when_provider_rejects_it(cfg, patched):
    attempts = []

    def post(url, body):
        attempts.append(body)
        if "response_format" in body:
            return _Response(400, text="response_format is not supported")
        return _Response(200, {"choices": [{"message": {"content": "OK"}}],
                               "usage": {"total_tokens": 3}})

    patched(_FakeRequests(post_handler=post))
    gw = AIGateway(_cfg(cfg, "https://gw.example.com/v1"))
    result = gw.chat("some-model", [{"role": "user", "content": "hi"}], json_mode=True)
    assert result.ok and result.text == "OK"
    assert len(attempts) >= 2
    assert "response_format" not in attempts[-1]


def test_chat_uses_max_completion_tokens_when_max_tokens_rejected(cfg, patched):
    bodies = []

    def post(url, body):
        bodies.append(body)
        if "max_tokens" in body:
            return _Response(400, text="'max_tokens' is not supported")
        return _Response(200, {"choices": [{"message": {"content": "OK"}}],
                               "usage": {"total_tokens": 3}})

    patched(_FakeRequests(post_handler=post))
    gw = AIGateway(_cfg(cfg, "https://gw.example.com/v1"))
    result = gw.chat("gpt-5", [{"role": "user", "content": "hi"}], json_mode=False)
    assert result.ok
    assert "max_completion_tokens" in bodies[-1]
    assert "max_tokens" not in bodies[-1]


def test_chat_tries_v1_then_bare_base(cfg, patched):
    """A provider whose routes are mounted at the root (no /v1) still works."""
    def post(url, body):
        if url.endswith("/v1/chat/completions"):
            return _Response(404, text="not found")
        return _Response(200, {"choices": [{"message": {"content": "OK"}}],
                               "usage": {"total_tokens": 1}})

    patched(_FakeRequests(post_handler=post))
    gw = AIGateway(_cfg(cfg, "https://gw.example.com"))
    result = gw.chat("m", [{"role": "user", "content": "hi"}])
    assert result.ok


def test_list_models_tolerates_missing_endpoint(cfg, patched):
    def get(url):
        return _Response(404, text="not found")

    patched(_FakeRequests(get_handler=get))
    gw = AIGateway(_cfg(cfg, "https://gw.example.com/v1"))
    with pytest.raises(RuntimeError):
        gw.list_models()
    # And the chat path must still be usable with hand-set model ids.
    assert gw.endpoints


def test_401_is_reported_as_key_problem(cfg, patched):
    def get(url):
        return _Response(401, text="unauthorized")

    patched(_FakeRequests(get_handler=get))
    gw = AIGateway(_cfg(cfg, "https://gw.example.com/v1"))
    with pytest.raises(RuntimeError) as exc:
        gw.list_models()
    assert "AI_KEY" in str(exc.value)
