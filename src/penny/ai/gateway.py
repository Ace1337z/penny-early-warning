"""AI gateway client: OpenAI-compatible chat completions and model listing (5.3, 6.19)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from ..util import scrub

log = logging.getLogger(__name__)


@dataclass
class ChatResult:
    model: str
    text: str = ""
    tokens: int = 0
    latency_ms: float = 0.0
    ok: bool = False
    error: str = ""
    invalid: bool = False

    def as_dict(self) -> dict:
        return {"model": self.model, "ok": self.ok, "tokens": self.tokens,
                "latency_ms": round(self.latency_ms, 1), "error": self.error}


class AIGateway:
    """POST {AI_BASE_URL}/v1/chat/completions with a Bearer key."""

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self._secrets = list(secrets or [])

    @property
    def base(self) -> str:
        return str(self.cfg.raw("AI_BASE_URL") or "").rstrip("/")

    @property
    def key(self) -> str:
        return str(self.cfg.raw("AI_KEY") or "")

    @property
    def configured(self) -> bool:
        return bool(self.base and self.key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    # -- model listing ------------------------------------------------------
    def list_models(self, timeout: float = 15.0) -> list[str]:
        """Read the provider's model list. Raises on error/empty/unsupported (6.19 step 5)."""
        if not self.configured:
            raise RuntimeError("ai: AI_BASE_URL or AI_KEY not set")
        import requests
        url = f"{self.base}/v1/models"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(scrub(f"ai: model list request failed: {exc}", self._secrets)) from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"ai: model list HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("ai: model list was not JSON") from exc
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise RuntimeError("ai: model list has an unexpected shape")
        ids = []
        for item in items:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
        if not ids:
            raise RuntimeError("ai: model list was empty")
        return ids

    # -- chat ---------------------------------------------------------------
    def chat(self, model: str, messages: list[dict], *, max_tokens: int = 900,
             temperature: float = 0.2, timeout: float = 45.0, json_mode: bool = True,
             retries: int = 1) -> ChatResult:
        if not self.configured:
            return ChatResult(model=model, ok=False, error="ai: not configured")
        import requests
        url = f"{self.base}/v1/chat/completions"
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        last = ChatResult(model=model, ok=False)
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                resp = requests.post(url, headers=self._headers(), json=body, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                last = ChatResult(model=model, ok=False,
                                  latency_ms=(time.monotonic() - started) * 1000,
                                  error=scrub(str(exc), self._secrets))
                if attempt >= retries:
                    return last
                continue
            latency = (time.monotonic() - started) * 1000

            if resp.status_code == 400 and json_mode and attempt == 0:
                # Provider may not support JSON mode; retry once without it.
                body.pop("response_format", None)
                continue
            if resp.status_code >= 400:
                return ChatResult(model=model, ok=False, latency_ms=latency,
                                  error=scrub(f"HTTP {resp.status_code} {resp.text[:200]}",
                                              self._secrets))
            try:
                data = resp.json()
            except ValueError:
                return ChatResult(model=model, ok=False, latency_ms=latency,
                                  error="invalid JSON envelope")
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                return ChatResult(model=model, ok=False, latency_ms=latency,
                                  error="unexpected response shape")
            tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
            return ChatResult(model=model, text=text or "", tokens=tokens,
                              latency_ms=latency, ok=True)
        return last

    def test_model(self, model: str, timeout: float = 20.0) -> ChatResult:
        """Minimal test call: reply OK, at most 10 tokens (6.19 step 2)."""
        return self.chat(model, [{"role": "user", "content": "Reply with OK only."}],
                         max_tokens=10, temperature=0.0, timeout=timeout, json_mode=False,
                         retries=0)

    def health(self, model: Optional[str] = None) -> tuple[bool, str]:
        if not self.configured:
            return False, "AI_BASE_URL or AI_KEY not set"
        try:
            models = self.list_models()
        except Exception as exc:  # noqa: BLE001
            models = []
            listing_error = scrub(str(exc), self._secrets)
        else:
            listing_error = ""
        target = model or (models[0] if models else str(self.cfg.raw("AI_MODELS") or "").split(",")[0])
        if not target:
            return False, listing_error or "no model to test"
        result = self.test_model(target.strip())
        if result.ok:
            extra = f"; model list ok ({len(models)})" if models else f"; {listing_error}"
            return True, f"model {target} answered in {result.latency_ms:.0f} ms{extra}"
        return False, f"model {target}: {result.error}"


class FakeGateway:
    """Deterministic fake AI for offline simulation and tests (10.1).

    Behaviour is chosen per model name so the acceptance test can prove that a
    deliberately accurate model outranks a deliberately wrong one.
    """

    def __init__(self, cfg=None, models: Optional[list[str]] = None,
                 market=None, listing_error: Optional[str] = None):
        self.cfg = cfg
        self.market = market
        self.models = models or [
            "deepseek-v4.1-flash", "kimi-k3", "glm-5.3", "deepseek-v4-pro", "minimax-m3",
            "glm-5.1", "hy3", "kimi-k2.6", "deepseek-v4-flash", "glm-5.2",
            "mimo-v2.5-pro", "kimi-k2.7-code", "kimi-k2.7-code-highspeed",
            "deepseek-v4-pro-0813", "deepseek-v4-flash-0731", "glm-5.3-flash",
            "deepseek-v4-flash-vision-exp", "glm-5.3-flashx", "deepseek-v4-mod",
            "glm-5.2-mod", "deepseek-v4.1-mod", "glm-5.3-mod", "kimi-k3-mod",
            "hy4", "mimo-v2.6-flash", "mimo-v2.6-pro", "glm-5.3-flash-mod",
            "glm-5.3-flashx-mod",
        ]
        self.listing_error = listing_error
        self.calls: list[dict] = []
        self._fail_models: set[str] = set()
        self._invalid_models: set[str] = set()

    # -- test doubles -------------------------------------------------------
    def fail(self, model: str) -> None:
        self._fail_models.add(model)

    def make_invalid(self, model: str) -> None:
        self._invalid_models.add(model)

    def list_models(self, timeout: float = 15.0) -> list[str]:
        if self.listing_error:
            raise RuntimeError(self.listing_error)
        return list(self.models)

    def test_model(self, model: str, timeout: float = 20.0) -> ChatResult:
        if model in self._fail_models:
            return ChatResult(model=model, ok=False, error="fake: model unavailable")
        return ChatResult(model=model, text="OK", tokens=3, latency_ms=40.0, ok=True)

    def chat(self, model: str, messages: list[dict], *, max_tokens: int = 900,
             temperature: float = 0.2, timeout: float = 45.0, json_mode: bool = True,
             retries: int = 1) -> ChatResult:
        self.calls.append({"model": model, "max_tokens": max_tokens})
        if model in self._fail_models:
            return ChatResult(model=model, ok=False, error="fake: model unavailable")
        if model in self._invalid_models:
            return ChatResult(model=model, text="<html>not json</html>", tokens=20,
                              latency_ms=100.0, ok=True, invalid=True)

        price = self._price_from_prompt(messages)
        bias = self._bias_for(model)
        # A deliberately accurate model follows the simulated path; a wrong one inverts it.
        if bias == "accurate":
            f15 = price * 1.04
            f60 = price * 1.12
            send = price * 1.20
        elif bias == "wrong":
            f15 = price * 0.80
            f60 = price * 0.70
            send = price * 0.65
        else:
            f15 = price * 1.01
            f60 = price * 1.02
            send = price * 1.03
        payload = {
            "strength": "moderate", "direction": "up", "shape": "grinder",
            "durability": "uncertain", "confidence": 0.6,
            "catalyst": "fake catalyst", "sentiment": 0.4,
            "forecast": {"15m": {"price": round(f15, 4)},
                         "60m": {"price": round(f60, 4)},
                         "session_end": {"price": round(send, 4)}},
            "expected_peak": round(price * 1.25, 4),
            "expected_low": round(price * 0.95, 4),
            "market_effect": "fake market", "reasons": ["r1", "r2"],
            "flags": ["pump_pattern"], "change_view": "volume fades",
        }
        return ChatResult(model=model, text=json.dumps(payload), tokens=420,
                          latency_ms=300.0, ok=True)

    @staticmethod
    def _price_from_prompt(messages: list[dict]) -> float:
        for m in messages:
            content = m.get("content") or ""
            if isinstance(content, str) and '"price"' in content:
                try:
                    start = content.find("{")
                    data = json.loads(content[start:content.rfind("}") + 1])
                    return float(data.get("price") or 1.0)
                except (ValueError, TypeError):
                    continue
        return 1.0

    @staticmethod
    def _bias_for(model: str) -> str:
        if model in {"oracle", "accurate", "perfect"}:
            return "accurate"
        if model in {"wrong", "inverse", "bad"}:
            return "wrong"
        return "neutral"

    def health(self, model: Optional[str] = None) -> tuple[bool, str]:
        return True, "fake gateway"
