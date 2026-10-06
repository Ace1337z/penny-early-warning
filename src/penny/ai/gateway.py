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


def _text_from_content(content: Any) -> str:
    """Normalize the `content` field, which is a string on some providers and a
    list of parts (`[{"type": "text", "text": "..."}]`) on others."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, dict) and isinstance(value.get("value"), str):
                    parts.append(value["value"])
        return "".join(parts)
    if isinstance(content, dict):
        value = content.get("text") or content.get("value")
        return value if isinstance(value, str) else ""
    return ""


def _extract_text(data: Any) -> Optional[str]:
    """Pull the assistant text out of an OpenAI-compatible chat response.

    Handles the shapes seen across OpenCode-style providers: a plain string
    `content`, a list of text parts, and reasoning models that leave `content`
    null while putting the answer in `reasoning_content` or `reasoning`.
    Returns None only when the envelope itself is unusable.
    """
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        # Some gateways answer a bare {message: ...} or {output_text: ...}.
        fallback = data.get("message")
        if isinstance(fallback, dict):
            for key in ("content", "reasoning_content", "reasoning", "text"):
                text = _text_from_content(fallback.get(key))
                if text.strip():
                    return text
        for key in ("output_text", "text"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message") if isinstance(first.get("message"), dict) else first
    for key in ("content", "reasoning_content", "reasoning", "text"):
        text = _text_from_content(message.get(key))
        if text.strip():
            return text
    # A choice-level text (some gateways flatten the message away).
    text = _text_from_content(first.get("text"))
    return text if text.strip() or "content" in message else None


class AIGateway:
    """OpenAI-compatible chat client (the style OpenCode's `openai-compatible` uses).

    Provider base URLs are written in several ways (`https://host`, `https://host/v1`,
    or with a gateway prefix such as `https://host/openai`), and many gateways do not
    implement `GET /models` at all. The client therefore tries the plausible endpoints
    in order, remembers the one that answers, treats a missing model list as optional,
    and adapts the request body when a provider rejects `max_tokens`/`temperature`/
    JSON mode (as the newest OpenAI models do).
    """

    def __init__(self, cfg, secrets: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self._secrets = list(secrets or [])
        self._base: Optional[str] = None  # resolved endpoint, cached after a success

    def _entered_base(self) -> str:
        raw = str(self.cfg.raw("AI_BASE_URL") or "").strip().rstrip("/")
        for suffix in ("/chat/completions", "/completions", "/models", "/responses"):
            if raw.endswith(suffix):
                raw = raw[: -len(suffix)].rstrip("/")
        return raw

    def bases(self) -> list[str]:
        """Plausible gateway roots, in the order they should be tried."""
        if self._base:
            return [self._base]
        raw = self._entered_base()
        if not raw:
            return []
        candidates = [raw] if raw.endswith("/v1") else [f"{raw}/v1", raw]
        out: list[str] = []
        for c in candidates:
            if c and c not in out:
                out.append(c)
        return out

    @property
    def base(self) -> str:
        bases = self.bases()
        return bases[0] if bases else ""

    @property
    def models_url(self) -> str:
        return f"{self.base}/models"

    @property
    def chat_url(self) -> str:
        return f"{self.base}/chat/completions"

    @property
    def endpoints(self) -> list[str]:
        return [f"{b}/chat/completions" for b in self.bases()]

    @property
    def key(self) -> str:
        return str(self.cfg.raw("AI_KEY") or "")

    @property
    def configured(self) -> bool:
        return bool(self._entered_base() and self.key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    # -- model listing ------------------------------------------------------
    def list_models(self, timeout: float = 15.0) -> list[str]:
        """Read the provider's model list.

        Many OpenAI-compatible gateways (and OpenCode-style setups) do not expose
        `/models`; that is normal and never fatal. Raises only when nothing works,
        with the last error, so the caller can fall back to the configured ids.
        """
        if not self.configured:
            raise RuntimeError("ai: AI_BASE_URL or AI_KEY not set")
        import requests
        last_error = "ai: no endpoint to try"
        for base in self.bases():
            url = f"{base}/models"
            try:
                resp = requests.get(url, headers=self._headers(), timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = scrub(f"ai: could not reach {url}: {exc}", self._secrets)
                continue
            if resp.status_code in (401, 403):
                raise RuntimeError(scrub(
                    f"ai: HTTP {resp.status_code} at {url} (check AI_KEY)",
                    self._secrets))
            if resp.status_code >= 400:
                last_error = scrub(
                    f"ai: model list HTTP {resp.status_code} at {url}: "
                    f"{resp.text[:200]}", self._secrets)
                continue
            try:
                data = resp.json()
            except ValueError:
                last_error = f"ai: model list at {url} was not JSON"
                continue
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list):
                last_error = f"ai: model list at {url} has an unexpected shape"
                continue
            ids = []
            for item in items:
                if isinstance(item, str):
                    ids.append(item)
                elif isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
            if ids:
                self._base = base
                return ids
            last_error = f"ai: model list at {url} was empty"
        raise RuntimeError(last_error)

    # -- chat ---------------------------------------------------------------
    def _build_body(self, model: str, messages: list[dict], *, max_tokens: int,
                    temperature: float, json_mode: bool,
                    token_field: str = "max_tokens",
                    use_temperature: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "messages": messages,
                                token_field: max_tokens}
        if use_temperature:
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, model: str, messages: list[dict], *, max_tokens: int = 900,
             temperature: float = 0.2, timeout: float = 45.0, json_mode: bool = True,
             retries: int = 1, url: Optional[str] = None) -> ChatResult:
        if not self.configured:
            return ChatResult(model=model, ok=False, error="ai: not configured")
        import requests

        endpoints = [url] if url else self.endpoints
        if not endpoints:
            return ChatResult(model=model, ok=False, error="ai: AI_BASE_URL is not set")

        # Adaptations tried on a 400/404/422, in order: drop JSON mode, use
        # max_completion_tokens, drop temperature (newest OpenAI models reject
        # some of these).
        variants = [
            {"json_mode": json_mode, "token_field": "max_tokens", "use_temperature": True},
            {"json_mode": False, "token_field": "max_tokens", "use_temperature": True},
            {"json_mode": False, "token_field": "max_completion_tokens", "use_temperature": True},
            {"json_mode": False, "token_field": "max_completion_tokens", "use_temperature": False},
            {"json_mode": False, "token_field": "max_tokens", "use_temperature": False},
        ]
        seen: list[str] = []
        last_error = "ai: no endpoint accepted the request"
        for v in variants:
            sig = (v["json_mode"], v["token_field"], v["use_temperature"])
            if sig in seen:
                continue
            seen.append(sig)
            body = self._build_body(model, messages, max_tokens=max_tokens,
                                    temperature=temperature, **v)
            for endpoint in endpoints:
                for attempt in range(retries + 1):
                    started = time.monotonic()
                    try:
                        resp = requests.post(endpoint, headers=self._headers(),
                                             json=body, timeout=timeout)
                    except Exception as exc:  # noqa: BLE001
                        result = ChatResult(
                            model=model, ok=False,
                            latency_ms=(time.monotonic() - started) * 1000,
                            error=scrub(f"{exc} (at {endpoint})", self._secrets))
                        if attempt >= retries:
                            break
                        continue
                    latency = (time.monotonic() - started) * 1000

                    if resp.status_code in (400, 404, 405, 415, 422):
                        # Wrong endpoint or an unsupported body field; try the next
                        # endpoint/variant. Keep the message for the final error.
                        last_error = scrub(
                            f"HTTP {resp.status_code} at {endpoint}: {resp.text[:200]}",
                            self._secrets)
                        break
                    if resp.status_code >= 500:
                        if attempt < retries:
                            time.sleep(min(2 ** attempt, 4))
                            continue
                        return ChatResult(model=model, ok=False, latency_ms=latency,
                                          error=scrub(f"HTTP {resp.status_code} at "
                                                      f"{endpoint}: {resp.text[:200]}",
                                                      self._secrets))
                    if resp.status_code >= 400:
                        return ChatResult(model=model, ok=False, latency_ms=latency,
                                          error=scrub(f"HTTP {resp.status_code} at "
                                                      f"{endpoint}: {resp.text[:200]}",
                                                      self._secrets))
                    try:
                        data = resp.json()
                    except ValueError:
                        return ChatResult(model=model, ok=False, latency_ms=latency,
                                          error=f"invalid JSON envelope at {endpoint}")
                    text = _extract_text(data)
                    if text is None:
                        return ChatResult(model=model, ok=False, latency_ms=latency,
                                          error=f"unexpected response shape at {endpoint}")
                    if not text.strip():
                        # Reachable, non-empty envelope but no usable text (e.g. a
                        # reasoning model that only emitted its scratchpad). Report
                        # it as an error instead of a silent empty success.
                        return ChatResult(model=model, ok=False, latency_ms=latency,
                                          error=f"empty completion at {endpoint}")
                    tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
                    # Remember the endpoint that worked so later calls skip discovery.
                    marker = "/chat/completions"
                    if endpoint.endswith(marker):
                        self._base = endpoint[: -len(marker)]
                    return ChatResult(model=model, text=text, tokens=tokens,
                                      latency_ms=latency, ok=True)
        return ChatResult(model=model, ok=False, error=last_error)

    def test_model(self, model: str, timeout: float = 20.0) -> ChatResult:
        """Minimal test call: reply OK, at most 16 tokens (6.19 step 2)."""
        return self.chat(model, [{"role": "user", "content": "Reply with OK only."}],
                         max_tokens=16, temperature=0.0, timeout=timeout, json_mode=False,
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
        target = model or (models[0] if models
                           else str(self.cfg.raw("AI_MODELS") or "").split(",")[0].strip())
        if not target:
            return False, listing_error or "no model to test"
        result = self.test_model(target)
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

    @property
    def configured(self) -> bool:
        """The fake is always usable; it never needs a base URL or key."""
        return True

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
            "catalyst": "fake catalyst", "why_now": "fake reason it is moving now",
            "urgency": "now", "sentiment": 0.4,
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
