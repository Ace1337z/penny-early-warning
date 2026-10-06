"""Live checker: checks every source, Telegram, the databases and every AI model (10.4)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .util import session_for

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    optional: bool = False

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        return "SKIP" if self.optional else "FAIL"


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", optional: bool = False) -> None:
        self.checks.append(Check(name, ok, detail, optional))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if not c.optional)

    def text(self) -> str:
        lines = ["penny doctor"]
        for c in self.checks:
            lines.append(f"  [{c.status}] {c.name}: {c.detail}")
        lines.append("")
        lines.append("RESULT: " + ("ALL PASS" if self.ok else "FAILURES ABOVE"))
        return "\n".join(lines)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.optional]


def run_doctor(runtime, *, test_models: bool = True) -> DoctorReport:
    cfg = runtime.cfg
    report = DoctorReport()

    # -- configuration ------------------------------------------------------
    required = {
        "FINVIZ_TOKEN": "Finviz Elite token",
        "TELEGRAM_TOKEN": "Telegram bot token",
        "TELEGRAM_CHAT_ID": "Telegram chat id",
    }
    missing = [label for key, label in required.items() if not str(cfg.raw(key)).strip()]
    note = ""
    if missing:
        note = " (set them from the bot with: /set KEY VALUE)"
    report.add("configuration", not missing,
               "all required values present" if not missing
               else "missing: " + ", ".join(missing) + note)

    # Things that are convenient to have but can be added later from the bot.
    for key, label in (("AI_BASE_URL", "AI gateway base URL"), ("AI_KEY", "AI gateway key"),
                       ("SEC_USER_AGENT", "SEC contact (User-Agent)")):
        report.add(f"configuration: {label}", bool(str(cfg.raw(key)).strip()),
                   "set" if str(cfg.raw(key)).strip()
                   else f"not set yet (send /set {key} VALUE)", optional=True)

    mode = str(cfg.raw("SHARIAH_MODE") or "tag")
    report.add("shariah mode", mode in ("tag", "only_compliant", "hide_noncompliant", "off"),
               f"{mode}; source configured: "
               f"{'halalterminal.com' if cfg.raw('HALALTERMINAL_API_KEY') else '-'}")

    # -- databases ----------------------------------------------------------
    for name, db in (("state database", runtime.state), ("journal database", runtime.journal_db)):
        try:
            integrity = db.integrity_check()
            report.add(name, integrity == "ok", f"integrity {integrity}, "
                                                f"{len(db.row_counts())} tables")
        except Exception as exc:  # noqa: BLE001
            report.add(name, False, str(exc))

    # -- sources ------------------------------------------------------------
    source_specs = [
        ("Finviz", runtime.finviz, False),
        ("Alpaca (optional)", runtime.alpaca, True),
        ("Finnhub (optional)", runtime.finnhub, True),
        ("SEC EDGAR", runtime.sec, False),
        ("FINRA (optional)", runtime.finra, True),
        ("Yahoo (optional)", runtime.yahoo, True),
    ]
    for name, client, optional in source_specs:
        if client is None:
            report.add(name, False, "not configured", optional=True)
            continue
        try:
            ok, detail = client.health()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, str(exc)
        report.add(name, ok, detail, optional=optional)

    # -- Telegram -----------------------------------------------------------
    try:
        ok, detail = runtime.telegram.health()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, str(exc)
    report.add("Telegram", ok, detail)

    # -- Shariah ------------------------------------------------------------
    if runtime.shariah is None:
        report.add("Shariah sources", False, "not configured (optional)", optional=True)
    else:
        for name, ok, detail in runtime.shariah.health():
            report.add(f"Shariah: {name}", ok, detail, optional=True)

    # -- AI models ----------------------------------------------------------
    if not getattr(runtime.gateway, "configured", False):
        report.add("AI gateway", False,
                   "not configured yet (send /set AI_BASE_URL ... then /set AI_KEY ...)",
                   optional=True)
    else:
        gateway = runtime.gateway
        models: list[str] = []
        try:
            models = gateway.list_models()
            report.add("AI model list", bool(models),
                       f"{len(models)} model(s) listed")
        except Exception as exc:  # noqa: BLE001
            # Many OpenAI-compatible gateways (OpenCode-style) have no /models
            # endpoint. That is not fatal: test the configured ids directly.
            report.add("AI model list", False,
                       f"{exc} (optional: not all providers expose /models)",
                       optional=True)
        targets = list(dict.fromkeys(
            cfg.list("AI_MODELS") + cfg.list("AI_FALLBACK_MODELS")))
        if test_models:
            if not targets:
                targets = models[:3]
            available = set(models)
            # If the provider advertises its models and NONE of the configured ids
            # are among them, the panel can never answer: that is a real failure,
            # not an optional one. (Example ids shipped in the default config are
            # the usual cause.)
            if models and not (available & set(targets)):
                report.add("AI models configured", False,
                           "none of " + ", ".join(targets[:4])
                           + " are offered by the provider; set real ids, e.g. "
                           + ", ".join(models[:4]), optional=False)
            for model in targets:
                if models and model not in available:
                    report.add(f"AI model {model}", False,
                               "not in the provider's list; available: "
                               + ", ".join(models[:8]), optional=True)
                    continue
                try:
                    result = gateway.test_model(model)
                    report.add(f"AI model {model}", result.ok,
                               f"{result.latency_ms:.0f} ms" if result.ok else result.error)
                except Exception as exc:  # noqa: BLE001
                    report.add(f"AI model {model}", False, str(exc))

    # -- backup -------------------------------------------------------------
    if runtime.backup:
        age = runtime.backup.last_verified_age_hours()
        max_age = cfg.float("BACKUP_MAX_AGE_HOURS", 26)
        configured = bool(cfg.raw("BACKUP_REMOTE")) or bool(cfg.raw("BACKUP_LOCAL_DIR"))
        report.add("backup", True,
                   f"last verified {age:.1f} h ago" if age is not None else "no verified backup yet",
                   optional=not configured)
        if not cfg.raw("BACKUP_REMOTE"):
            report.add("off-server backup", False,
                       "no off-server destination configured (strongly recommended)",
                       optional=True)
        if cfg.raw("BACKUP_REMOTE") and not cfg.raw("BACKUP_PASSPHRASE"):
            report.add("backup passphrase", False,
                       "required when an off-server destination is used", optional=False)

    report.add("session", True, f"current session: {session_for()}")
    return report
