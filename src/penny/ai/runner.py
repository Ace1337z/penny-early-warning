"""AI panel runner: active panel, fallbacks, shadow candidates, caching, telemetry (6.11)."""

from __future__ import annotations

import concurrent.futures as futures
import logging
import random
import time
from typing import Optional

from ..util import scrub
from .evaluate import BASELINE_MODELS
from .panel import (AggregateForecast, AnalysisCache, ModelForecast, aggregate,
                    build_messages, parse_forecast)

log = logging.getLogger(__name__)


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
              timeout: Optional[float] = None) -> ModelForecast:
        timeout = timeout or self.cfg.float("AI_TIMEOUT", 45.0)
        max_tokens = self.cfg.int("AI_MAX_TOKENS", 900)
        result = self.gateway.chat(model, messages, max_tokens=max_tokens, temperature=0.2,
                                   timeout=timeout, json_mode=True, retries=0)
        fc = parse_forecast(model, result.text)
        if not fc.valid and result.ok:
            # At most one retry for a model that returned invalid output (rule 5).
            self._log_call(model, kind, result, invalid=True)
            self.record_tokens(model, kind, result.tokens, calls=1, invalid=True)
            result = self.gateway.chat(model, messages, max_tokens=max_tokens, temperature=0.2,
                                       timeout=timeout, json_mode=True, retries=0)
            fc = parse_forecast(model, result.text)
        self._log_call(model, kind, result, invalid=not fc.valid)
        self.record_tokens(model, kind, result.tokens, calls=1, invalid=not fc.valid)
        fc.tokens = result.tokens
        fc.latency_ms = result.latency_ms
        if not result.ok:
            fc.error = result.error
        return fc

    def _call_many(self, models: list[str], messages: list[dict], kind: str,
                   timeout: Optional[float] = None) -> list[ModelForecast]:
        if not models:
            return []
        timeout = timeout or self.cfg.float("AI_TIMEOUT", 45.0)
        out: list[ModelForecast] = []
        with futures.ThreadPoolExecutor(max_workers=max(1, len(models))) as pool:
            futs = {pool.submit(self._call, m, messages, kind, timeout): m for m in models}
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
        panel = self.active_panel()
        forecasts = self._call_many(panel, messages, kind)

        valid = [f for f in forecasts if f.valid]
        if len(valid) < 2:
            for model in self.fallbacks():
                if len(valid) >= 2:
                    break
                if model in panel:
                    continue
                fc = self._call(model, messages, kind + ":fallback")
                forecasts.append(fc)
                if fc.valid:
                    valid.append(fc)

        agg = aggregate(forecasts)
        if not any(self._is_eligible(m) for m in agg.models):
            agg.validated = False
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
