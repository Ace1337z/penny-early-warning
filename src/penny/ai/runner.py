"""AI panel runner: active panel, fallbacks, shadow candidates, caching, telemetry (6.11)."""

from __future__ import annotations

import concurrent.futures as futures
import logging
import random
import re
import time
from typing import Optional

from ..util import scrub
from .evaluate import BASELINE_MODELS
from .panel import (AggregateForecast, AnalysisCache, ModelForecast, aggregate,
                    build_messages, parse_forecast)
from .verdict import decide as decide_verdict

log = logging.getLogger(__name__)

# Provider error text saying "you asked for a model I do not serve" -- the shape
# seen when AI_BASE_URL/AI_KEY is pointed at a different OpenAI-compatible
# provider whose ids are unrelated to the configured ones.
_MODEL_MISSING = re.compile(
    r"model[^.\n]{0,80}(not\s+found|does\s+not\s+exist|unknown|is\s+not\s+available"
    r"|not\s+supported|invalid)|invalid\s+model|no\s+such\s+model|"
    r"HTTP\s+404|status[^0-9]{0,10}404", re.I)


class AIRunner:
    def __init__(self, cfg, gateway, catalog, db, telegram=None, secrets=None):
        self.cfg = cfg
        self.gateway = gateway
        self.catalog = catalog
        self.db = db
        self.telegram = telegram
        self.secrets = list(secrets or [])
        self.cache = AnalysisCache(cfg.float("AI_CACHE_MINUTES", 10))
        self._notified_day = ""

    # -- panel selection ----------------------------------------------------
    def active_panel(self) -> list[str]:
        return list(self.db.kv_get("active_panel") or self.cfg.list("AI_MODELS"))

    def fallbacks(self) -> list[str]:
        return list(self.cfg.list("AI_FALLBACK_MODELS"))

    def candidates(self) -> list[str]:
        cands = self.db.kv_get("candidates")
        if cands:
            return [c for c in cands if c not in BASELINE_MODELS.values()]
        return self.catalog.pool(self.cfg.float("AI_MAX_MULTIPLIER", 4),
                                 self.cfg.list("AI_PREMIUM_MODELS"))

    # -- provider identity --------------------------------------------------
    def provider_id(self) -> str:
        """Identify the *provider*, meaning the endpoint *and* the credential.

        Keying on the URL alone looked correct but was not: replacing only AI_KEY
        -- the common case, e.g. swapping in a new token for the same gateway --
        left the panel holding the previous key's model ids, every call failed
        with "model not found", and the panel stayed dead until a restart. A cheap
        digest keeps the key out of the stored value.
        """
        import hashlib
        base = str(self.cfg.raw("AI_BASE_URL") or "").strip().rstrip("/").lower()
        key = str(self.cfg.raw("AI_KEY") or "")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else ""
        return f"{base}|{digest}"

    def sync_provider(self) -> bool:
        """Reset the panel when AI_BASE_URL points somewhere new.

        `active_panel` and `candidates` are copies of the *previous* provider's
        model ids. Model names differ between OpenAI-compatible providers, so
        after a switch every call fails with "model not found" and the panel
        looks dead until someone remembers to re-set it. Rebinding the stored
        lists to the new endpoint makes a provider swap take effect on its own:
        the configured list is the fallback and the provider's own `/models`
        list (any ids, including `vendor/name`) is preferred when reachable.
        """
        pid = self.provider_id()
        if not pid:
            return False
        previous = self.db.kv_get("ai_provider")
        if pid == previous:
            return False
        self.db.kv_set("ai_provider", pid)
        if previous is None:
            return False  # first run: the panel as configured is already right
        panel = list(self.cfg.list("AI_MODELS"))
        self.db.kv_set("active_panel", panel)
        self.db.kv_set("active_panel_source", panel)
        self.db.kv_set("candidates", [])
        log.info("ai: provider changed %s -> %s; panel reset to %s",
                 previous, pid, panel)
        try:
            listed = self.gateway.list_models(timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.info("ai: no model list from the new provider: %s", exc)
            return True
        adopted = self._adopt_panel(listed)
        if adopted:
            log.info("ai: adopted panel %s from the new provider", adopted)
            if self.telegram:
                self.telegram.send(
                    "AI provider changed; panel is now: "
                    + ", ".join(adopted))
        return True

    def reset_after_credential_change(self) -> list[str]:
        """Force a rebind after AI_KEY / AI_BASE_URL was edited.

        `/set` calls this directly so the fix lands the moment the key is saved,
        rather than waiting for the next analysis to notice. Returns the panel now
        in force, which may be empty when the provider exposes no model list.
        """
        if hasattr(self.gateway, "forget_endpoint"):
            self.gateway.forget_endpoint()
        self.db.kv_set("ai_provider", self.provider_id())
        self.db.kv_set("ai_repair_for", "")
        self.cache.clear()
        panel = list(self.cfg.list("AI_MODELS"))
        self.db.kv_set("active_panel", panel)
        self.db.kv_set("active_panel_source", panel)
        self.db.kv_set("candidates", [])
        try:
            listed = self.gateway.list_models(timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.info("ai: no model list after a key change: %s", exc)
            return panel
        adopted = self._adopt_panel(listed)
        return adopted or panel
    def _missing_models(self, forecasts: list[ModelForecast]) -> bool:
        errors = [f.error for f in forecasts if not f.valid and f.error]
        return bool(errors) and all(_MODEL_MISSING.search(e) for e in errors)

    def _repair_panel(self) -> list[str]:
        """One attempt to swap in models this provider actually serves."""
        pid = self.provider_id()
        if self.db.kv_get("ai_repair_for") == pid:
            return []  # already tried for this endpoint
        self.db.kv_set("ai_repair_for", pid)
        try:
            listed = self.gateway.list_models(timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            log.info("ai: repair skipped, no model list: %s", scrub(str(exc), self.secrets))
            return []
        chosen = self._adopt_panel(listed)
        if chosen:
            log.info("ai: panel repaired for %s -> %s", pid, chosen)
        return chosen

    # -- token accounting ---------------------------------------------------
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def tokens_today(self) -> int:
        return int(self.db.scalar("SELECT SUM(tokens) FROM tokens WHERE day=?",
                                  (self._today(),), default=0) or 0)

    def weighted_tokens_today(self) -> int:
        return int(self.db.scalar("SELECT SUM(weighted) FROM tokens WHERE day=?",
                                  (self._today(),), default=0) or 0)

    def record_tokens(self, model: str, kind: str, tokens: int, calls: int = 1,
                      invalid: bool = False) -> None:
        multiplier = self.catalog.multiplier_of(model)
        self.db.execute(
            "INSERT INTO tokens(day, model, kind, tokens, calls, weighted, invalid) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(day, model, kind) DO UPDATE SET "
            "tokens=tokens+excluded.tokens, calls=calls+excluded.calls, "
            "weighted=weighted+excluded.weighted, invalid=invalid+excluded.invalid",
            (self._today(), model, kind, int(tokens), int(calls),
             int(tokens * multiplier), 1 if invalid else 0))
        self._check_notify()

    def _check_notify(self) -> None:
        notify = self.cfg.int("TOKEN_NOTIFY_DAILY", 0)
        if notify <= 0 or not self.telegram:
            return
        used = self.tokens_today()
        day = self._today()
        if used >= notify and self._notified_day != day:
            self._notified_day = day
            try:
                self.telegram.send(f"Token notice: {used:,} tokens used today "
                                   f"(your alert threshold is {notify:,}).")
            except Exception as exc:  # noqa: BLE001
                log.debug("token notice failed: %s", exc)

    def _limit_reached(self) -> bool:
        limit = self.cfg.int("TOKEN_LIMIT_DAILY", 0)
        return limit > 0 and self.tokens_today() >= limit

    # -- calls --------------------------------------------------------------
    def _log_call(self, model: str, kind: str, result, cached: bool = False,
                  invalid: bool = False) -> None:
        self.db.execute(
            "INSERT INTO ai_calls(ts, model, kind, ok, latency_ms, tokens, error, cached) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), model, kind, 1 if result.ok else 0, result.latency_ms,
             result.tokens, scrub(result.error, self.secrets), 1 if cached else 0))

    def _call(self, model: str, messages: list[dict], kind: str,
              timeout: Optional[float] = None,
              deadline: Optional[float] = None) -> ModelForecast:
        base_timeout = timeout or self.cfg.float("AI_TIMEOUT", 45.0)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ModelForecast(model=model, valid=False, error="ai: deadline exceeded")
            base_timeout = max(1.0, min(base_timeout, remaining))
        timeout = base_timeout
        max_tokens = self.cfg.int("AI_MAX_TOKENS", 900)
        result = self.gateway.chat(model, messages, max_tokens=max_tokens, temperature=0.2,
                                   timeout=timeout, json_mode=True, retries=0,
                                   deadline=deadline)
        fc = parse_forecast(model, result.text)
        if not fc.valid and result.ok:
            # At most one retry for a model that returned invalid output (rule 5).
            self._log_call(model, kind, result, invalid=True)
            self.record_tokens(model, kind, result.tokens, calls=1, invalid=True)
            if deadline is None or time.monotonic() < deadline:
                result = self.gateway.chat(model, messages, max_tokens=max_tokens,
                                           temperature=0.2, timeout=timeout, json_mode=True,
                                           retries=0, deadline=deadline)
                fc = parse_forecast(model, result.text)
            else:
                fc.error = fc.error or "ai: deadline exceeded"
        self._log_call(model, kind, result, invalid=not fc.valid)
        self.record_tokens(model, kind, result.tokens, calls=1, invalid=not fc.valid)
        fc.tokens = result.tokens
        fc.latency_ms = result.latency_ms
        if not result.ok:
            fc.error = result.error
        return fc

    def _call_many(self, models: list[str], messages: list[dict], kind: str,
                   timeout: Optional[float] = None,
                   deadline: Optional[float] = None) -> list[ModelForecast]:
        if not models:
            return []
        timeout = timeout or self.cfg.float("AI_TIMEOUT", 45.0)
        out: list[ModelForecast] = []
        with futures.ThreadPoolExecutor(max_workers=max(1, len(models))) as pool:
            futs = {pool.submit(self._call, m, messages, kind, timeout, deadline): m
                    for m in models}
            for fut in futures.as_completed(futs):
                model = futs[fut]
                try:
                    out.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    log.warning("ai call %s failed: %s", model, scrub(str(exc), self.secrets))
                    out.append(ModelForecast(model=model, valid=False, error=str(exc)))
        order = {m: i for i, m in enumerate(models)}
        out.sort(key=lambda f: order.get(f.model, 999))
        return out

    # -- analysis -----------------------------------------------------------
    def analyze(self, *, symbol: str, facts: dict, price: float, tier: int,
                news_key: str = "", kind: str = "alert",
                force: bool = False) -> tuple[AggregateForecast, list[ModelForecast]]:
        """Run the active panel (with fallbacks). Returns (aggregate, all forecasts)."""
        if not getattr(self.gateway, "configured", True):
            return AggregateForecast(), []
        if self._limit_reached():
            log.warning("TOKEN_LIMIT_DAILY reached; skipping AI call for %s", symbol)
            return AggregateForecast(), []

        if not force:
            cached = self.cache.get(symbol, price=price, tier=tier, news_key=news_key)
            if cached is not None:
                agg = AggregateForecast(**{k: v for k, v in cached.items()
                                           if k in AggregateForecast.__dataclass_fields__})
                agg.n_models = agg.n_models or len(agg.models)
                return agg, []

        messages = build_messages(facts)
        self.sync_provider()
        panel = self.active_panel()
        # One wall-clock budget for the whole panel, so a slow or hung provider
        # cannot turn a single /check into minutes of stacked 45s timeouts.
        budget = self.cfg.float("AI_DEADLINE", 60.0)
        deadline = time.monotonic() + budget if budget > 0 else None
        forecasts = self._call_many(panel, messages, kind, deadline=deadline)

        # Panel written for a different provider fails wholesale with
        # "model not found". Swap in ids this endpoint serves and retry once.
        if forecasts and not any(f.valid for f in forecasts) and self._missing_models(forecasts):
            repaired = self._repair_panel()
            if repaired and (deadline is None or time.monotonic() < deadline):
                panel = repaired
                forecasts = self._call_many(panel, messages, kind, deadline=deadline)

        valid = [f for f in forecasts if f.valid]
        if len(valid) < 2:
            pending = [m for m in self.fallbacks() if m not in panel]
            if deadline is None or time.monotonic() < deadline:
                fb = self._call_many(pending, messages, kind + ":fallback", deadline=deadline)
                for fc in fb:
                    forecasts.append(fc)
                    if fc.valid:
                        valid.append(fc)
            else:
                log.info("ai: deadline reached before fallbacks for %s", symbol)

        agg = aggregate(forecasts)
        if not any(self._is_eligible(m) for m in agg.models):
            agg.validated = False
        # The portfolio manager runs on the panel's output and the same facts the
        # models saw, so the ENTER/WAIT/PASS call can never be based on a number
        # that was not measured.
        decide_verdict(agg, facts, min_rr=self.cfg.float("MIN_RR", 2.0))
        self.cache.put(symbol, agg.as_dict(), price=price, tier=tier, news_key=news_key)
        return agg, forecasts

    def _is_eligible(self, model: str) -> bool:
        from .leaderboard import eligibility
        ok, _ = eligibility(self.db, model, self.cfg)
        return ok

    def shadow(self, *, symbol: str, facts: dict, exclude: Optional[set[str]] = None,
               kind: str = "shadow") -> list[ModelForecast]:
        """Run remaining candidates in shadow mode; forecasts are stored, never shown."""
        if not getattr(self.gateway, "configured", True):
            return []
        if self._limit_reached():
            return []
        exclude = set(exclude or set())
        candidates = [m for m in self.candidates() if m not in exclude]
        rate = self.cfg.float("SHADOW_SAMPLE_RATE", 0.2)
        panel = set(self.active_panel())
        selected = []
        for m in candidates:
            if m in panel:
                continue
            if m in set(self.cfg.list("AI_PREMIUM_MODELS")):
                if random.random() > self.cfg.float("PREMIUM_SAMPLE_RATE", 0.1):
                    continue
            elif random.random() > rate:
                continue
            selected.append(m)
        if not selected:
            return []
        return self._call_many(selected, build_messages(facts), kind)

    def _adopt_panel(self, listed: list[str]) -> list[str]:
        """Pick a working panel from the provider's own list.

        Prefers models already known to the catalogue, then any that answer a
        minimal test call, so a fresh install with example ids self-heals.
        """
        ordered = [m for m in listed if self.catalog.get(m) or self.catalog.is_alias(m)]
        ordered += [m for m in listed if m not in ordered]
        chosen: list[str] = []
        for model in ordered:
            if len(chosen) >= 3:
                break
            if model in chosen:
                continue
            try:
                if self.gateway.test_model(model).ok:
                    chosen.append(model)
                    if not self.catalog.get(model):
                        self.catalog.add(model, 1.0)
            except Exception:  # noqa: BLE001
                continue
        if chosen:
            self.db.kv_set("active_panel", chosen)
            self.db.kv_set("active_panel_source", chosen)
            self.catalog.save()
        return chosen

    # -- discovery ----------------------------------------------------------
    def discovery(self, telegram=None) -> dict:
        """Compare the provider's model list with the catalogue (6.19)."""
        result = {"listed": [], "missing_from_provider": [], "unknown_to_user": [],
                  "failed_tests": [], "unavailable_list": False, "message": "",
                  "adopted_panel": []}
        try:
            listed = self.gateway.list_models()
            result["listed"] = listed
        except Exception as exc:  # noqa: BLE001
            result["unavailable_list"] = True
            result["message"] = scrub(str(exc), self.secrets)
            listed = []

        listed_set = set(listed)
        if listed:
            result["missing_from_provider"] = [
                m for m in self.catalog.all_ids()
                if m not in listed_set and self.catalog.get(m) and self.catalog.get(m).enabled]
            result["unknown_to_user"] = [
                m for m in listed if self.catalog.get(m) is None
                and not self.catalog.is_alias(m)]
            # Self-correct the shipped example ids: if the active panel shares no
            # model with the provider, adopt the provider's own list so alerts get
            # AI text without the operator hunting for ids.
            panel = self.active_panel()
            if panel and not (listed_set & set(panel)):
                adopted = self._adopt_panel(listed)
                result["adopted_panel"] = adopted
                if adopted and telegram:
                    telegram.send("None of the configured models are offered by the "
                                  "provider. Switched the panel to:\n"
                                  + "\n".join(adopted))

        for model in self.candidates():
            test = self.gateway.test_model(model)
            entry = self.catalog.get(model)
            if entry:
                entry.available = test.ok
                entry.latency_ms = test.latency_ms
            if not test.ok:
                result["failed_tests"].append(model)

        if result["unavailable_list"] and telegram:
            telegram.send(
                "Could not read the model list from the provider. Send the available model ids "
                "with /models set id:multiplier,id:multiplier")
        if result["unknown_to_user"] and telegram:
            telegram.send("New models found that are not in your catalogue (not used until you "
                          "confirm them and give a multiplier):\n"
                          + "\n".join(f"/models add {m} MULTIPLIER" for m in result["unknown_to_user"]))
        self.catalog.save()
        return result
