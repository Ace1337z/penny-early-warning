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
        "MOOMOO_API_KEY": "Moomoo AppKey",
        "FINVIZ_TOKEN": "Finviz Elite token",
        "TELEGRAM_TOKEN": "Telegram bot token",
        "TELEGRAM_CHAT_ID": "Telegram chat id",
        "AI_BASE_URL": "AI gateway base URL",
        "AI_KEY": "AI gateway key",
        "SEC_USER_AGENT": "SEC contact (User-Agent)",
    }
    missing = [label for key, label in required.items() if not str(cfg.raw(key)).strip()]
    report.add("configuration", not missing,
               "all required values present" if not missing
               else "missing: " + ", ".join(missing))

    mode = str(cfg.raw("SHARIAH_MODE") or "tag")
    report.add("shariah mode", mode in ("tag", "only_compliant", "hide_noncompliant", "off"),
               f"{mode}; sources configured: "
               f"{'halal.sh' if cfg.raw('HALALSH_API_KEY') else '-'}, "
               f"{'Musaffa' if cfg.raw('MUSAFFA_API_KEY') else '-'}")

    # -- databases ----------------------------------------------------------
    for name, db in (("state database", runtime.state), ("journal database", runtime.journal_db)):
        try:
            integrity = db.integrity_check()
            report.add(name, integrity == "ok", f"integrity {integrity}, "
                                                f"{len(db.row_counts())} tables")
        except Exception as exc:  # noqa: BLE001
            report.add(name, False, str(exc))

    # -- clock --------------------------------------------------------------
    if runtime.moomoo:
        offset = runtime.moomoo.sync_clock()
        report.add("clock sync (Moomoo)", abs(offset) <= 5000,
                   f"offset {offset} ms (Moomoo rejects over 5000 ms)")

    # -- sources ------------------------------------------------------------
    source_specs = [
        ("Moomoo", runtime.moomoo, False),
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
    if runtime.gateway is None:
        report.add("AI gateway", False, "not configured")
    else:
        try:
            models = runtime.gateway.list_models()
            report.add("AI model list", bool(models), f"{len(models)} model(s) listed")
        except Exception as exc:  # noqa: BLE001
            report.add("AI model list", False,
                       f"{exc} (send /models set id:multiplier,id:multiplier)", optional=True)
            models = []
        if test_models:
            targets = list(dict.fromkeys(cfg.list("AI_MODELS") + cfg.list("AI_FALLBACK_MODELS")))
            if not targets and models:
                targets = models[:3]
            for model in targets:
                try:
                    result = runtime.gateway.test_model(model)
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
