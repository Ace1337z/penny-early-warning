"""Configuration: one env-style file, defaults for every key, live reload, masked output."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Optional

from .util import mask

log = logging.getLogger(__name__)


def restrict_permissions(path: Path) -> None:
    """Owner-only access: mode 600 on POSIX, an ACL for the current user on Windows."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                       capture_output=True, timeout=15, check=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not restrict %s with icacls: %s", path, exc)

# Keys whose values are secrets (never shown, scrubbed from logs).
SECRET_KEYS = {
    "FINVIZ_TOKEN",
    "TELEGRAM_TOKEN",
    "AI_KEY",
    "ALPACA_KEY",
    "ALPACA_SECRET",
    "FINNHUB_KEY",
    "HALALTERMINAL_API_KEY",
    "BACKUP_PASSPHRASE",
    "GITHUB_TOKEN",
}

# Structural settings that need a restart when changed.
RESTART_KEYS = {"DATA_PROVIDER", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"}

DEFAULTS: dict[str, str] = {
    # --- Feed (Finviz) ---
    "DATA_PROVIDER": "finviz",
    "MOVERS_POLL_SECONDS": "20",
    "UNIVERSE_POLL_SECONDS": "30",
    "EXTRA_SYMBOLS": "",
    # --- Finviz ---
    "FINVIZ_TOKEN": "",
    "FINVIZ_BASE": "https://elite.finviz.com",
    "FINVIZ_VIEW": "111",
    "FINVIZ_FILTERS": "sh_price_u10,ind_stocksonly",
    "FINVIZ_COLUMNS": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66",
    "FINVIZ_SIGNALS": "top_gainers,new_high,most_active,unusual_volume,overbought,oversold,insider_buying",
    "FINVIZ_MOVERS_TTL": "15",
    "FINVIZ_QUOTE_BATCH": "40",
    # --- Other sources ---
    "ALPACA_KEY": "",
    "ALPACA_SECRET": "",
    "ALPACA_FEED": "iex",
    "FINNHUB_KEY": "",
    "SEC_USER_AGENT": "",
    # --- Telegram ---
    "TELEGRAM_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    # --- Shariah ---
    "HALALTERMINAL_API_KEY": "",
    "HALALTERMINAL_BASE_URL": "https://api.halalterminal.com",
    "SHARIAH_MODE": "tag",
    "SHARIAH_TTL_DAYS": "7",
    "SHARIAH_MAX_CALLS_MONTH": "0",
    # --- Backup ---
    "BACKUP_LOCAL_DIR": "",
    "BACKUP_REMOTE": "",
    "BACKUP_PASSPHRASE": "",
    "BACKUP_HOURLY": "1",
    "BACKUP_TELEGRAM": "0",
    "BACKUP_KEEP_HOURLY": "48",
    "BACKUP_KEEP_DAILY": "14",
    "BACKUP_KEEP_WEEKLY": "8",
    "BACKUP_KEEP_MONTHLY": "12",
    "BACKUP_MAX_AGE_HOURS": "26",
    # --- AI ---
    "AI_BASE_URL": "",
    "AI_KEY": "",
    "AI_MODELS": "deepseek-v4.1-flash,kimi-k3,glm-5.3",
    "AI_FALLBACK_MODELS": "deepseek-v4-pro,minimax-m3",
    "AI_CANDIDATES": "",
    "AI_TIMEOUT": "45",
    "AI_CACHE_MINUTES": "10",
    "AI_MAX_TOKENS": "900",
    "SHADOW_SAMPLE_RATE": "0.2",
    "TRACKER_AI": "1",
    "MODEL_SELECTION": "auto",
    "MIN_EVALUATED": "100",
    "SELECTION_WINDOW_DAYS": "0",
    "AI_MODEL_CATALOG": "models_catalog.json",
    "AI_DISCOVERY": "auto",
    "AI_MAX_MULTIPLIER": "4",
    "AI_PREMIUM_MODELS": "",
    "PREMIUM_SAMPLE_RATE": "0.1",
    "MODEL_HALVING_STAGES": "30,60,100",
    "MARKET_SYMBOLS": "SPY,QQQ,IWM",
    "TOKEN_NOTIFY_DAILY": "0",
    "TOKEN_LIMIT_DAILY": "0",
    # --- Behaviour ---
    "MAX_PRICE": "10",
    "MIN_PRICE": "0.10",
    "ALERT_MIN_TIER": "2",
    "REALERT_SECONDS": "3600",
    "ALERT_EXTENDED": "0",
    "EXTENDED_PCT": "100",
    "WARMUP_CYCLES": "2",
    "WORKERS": "8",
    "RISK_USD": "50",
    "BASELINE_FALLBACK": "300",
    "BASELINE_MIN_PER_MIN": "20",
    "FINVIZ_MAX_PER_MIN": "20",
    "RETENTION_DAYS": "365",
    "UNIVERSE_BATCH": "400",
    "MOVERS_TOP_N": "200",
    "STATE_HOURS": "6",
    "SAMPLE_SECONDS": "55",
    # --- Tier thresholds (6.5) ---
    "TIER1_RISE15": "3",
    "TIER1_VOLX15": "3",
    "TIER1_DOLLAR15": "3000",
    "TIER2_RISE60": "8",
    "TIER2_RISE15": "5",
    "TIER2_VOLX15": "10",
    "TIER2_DOLLAR15": "10000",
    "TIER3_RISE60": "15",
    "TIER3_VOLX15": "20",
    "TIER3_ACCEL": "1.0",
    "TIER3_FROM_HIGH": "-8",
    "TIER3_DOLLAR15": "25000",
    # --- Score weights (6.5) ---
    "SCORE_W_RISE": "30",
    "SCORE_W_VOLX": "30",
    "SCORE_W_ACCEL": "10",
    "SCORE_W_VWAP": "10",
    "SCORE_W_FROMHIGH": "10",
    "SCORE_W_DOLLAR": "10",
    "SCORE_RISE_CAP": "50",
    "SCORE_VOLX_CAP": "2",
    "SCORE_ACCEL_CAP": "2",
    "SCORE_FROMHIGH_CAP": "15",
    "SCORE_DOLLAR_CAP": "100000",
    # --- Install / updater ---
    "PENNY_URL": "https://github.com/Ace1337z/penny-early-warning",
    "PENNY_VERSION": "main",
    "INSTALL_DIR": "/opt/penny",
    "PENNY_HOME": "",
    "LOG_LEVEL": "INFO",
}

GROUPS: dict[str, list[str]] = {
    "Feed": ["DATA_PROVIDER", "MOVERS_POLL_SECONDS", "UNIVERSE_POLL_SECONDS",
             "EXTRA_SYMBOLS"],
    "Finviz": ["FINVIZ_TOKEN", "FINVIZ_BASE", "FINVIZ_VIEW", "FINVIZ_FILTERS", "FINVIZ_COLUMNS",
               "FINVIZ_SIGNALS", "FINVIZ_MOVERS_TTL", "FINVIZ_QUOTE_BATCH"],
    "Other sources": ["ALPACA_KEY", "ALPACA_SECRET", "ALPACA_FEED", "FINNHUB_KEY", "SEC_USER_AGENT"],
    "Telegram": ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"],
    "Shariah": ["HALALTERMINAL_API_KEY", "HALALTERMINAL_BASE_URL",
                "SHARIAH_MODE", "SHARIAH_TTL_DAYS", "SHARIAH_MAX_CALLS_MONTH"],
    "Backup": ["BACKUP_LOCAL_DIR", "BACKUP_REMOTE", "BACKUP_PASSPHRASE", "BACKUP_HOURLY",
               "BACKUP_TELEGRAM", "BACKUP_KEEP_HOURLY", "BACKUP_KEEP_DAILY",
               "BACKUP_KEEP_WEEKLY", "BACKUP_KEEP_MONTHLY", "BACKUP_MAX_AGE_HOURS"],
    "AI": ["AI_BASE_URL", "AI_KEY", "AI_MODELS", "AI_FALLBACK_MODELS", "AI_CANDIDATES",
           "AI_TIMEOUT", "AI_CACHE_MINUTES", "SHADOW_SAMPLE_RATE", "TRACKER_AI",
           "MODEL_SELECTION", "MIN_EVALUATED", "SELECTION_WINDOW_DAYS", "AI_MODEL_CATALOG",
           "AI_DISCOVERY", "AI_MAX_MULTIPLIER", "AI_PREMIUM_MODELS", "PREMIUM_SAMPLE_RATE",
           "MODEL_HALVING_STAGES", "MARKET_SYMBOLS", "TOKEN_NOTIFY_DAILY", "TOKEN_LIMIT_DAILY"],
    "Behaviour": ["MAX_PRICE", "MIN_PRICE", "ALERT_MIN_TIER", "REALERT_SECONDS", "ALERT_EXTENDED",
                  "EXTENDED_PCT", "WARMUP_CYCLES", "WORKERS", "RISK_USD", "BASELINE_FALLBACK",
                  "FINVIZ_MAX_PER_MIN", "RETENTION_DAYS"],
    "Install": ["PENNY_URL", "PENNY_VERSION", "INSTALL_DIR", "PENNY_HOME", "LOG_LEVEL"],
}

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def _default_home() -> Path:
    env = os.environ.get("PENNY_HOME")
    if env:
        return Path(env)
    return Path.cwd()


class Config:
    """Live-reloading configuration backed by a single key=value file."""

    def __init__(self, path: str | os.PathLike | None = None):
        if path is None:
            path = os.environ.get("PENNY_CONFIG") or (_default_home() / "config.env")
        self.path = Path(path)
        self._values: dict[str, str] = dict(DEFAULTS)
        self._mtime: float = 0.0
        self._loaded_ok = False
        self.load()

    # -- loading ------------------------------------------------------------
    def load(self) -> None:
        values = dict(DEFAULTS)
        if self.path.exists():
            try:
                for raw in self.path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("export "):
                        line = line[7:]
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    values[key] = val
            except OSError as exc:
                log.error("cannot read config %s: %s", self.path, exc)
        self._values = values
        self._mtime = self._stat_mtime()
        self._loaded_ok = True

    def _stat_mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def reload_if_changed(self) -> bool:
        """Re-read the file when its mtime changed. Returns True when reloaded."""
        mtime = self._stat_mtime()
        if mtime != self._mtime and mtime > 0:
            log.info("configuration changed on disk; reloading %s", self.path)
            self.load()
            return True
        return False

    # -- accessors ----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, DEFAULTS.get(key, default))

    def raw(self, key: str) -> str:
        return self._values.get(key, DEFAULTS.get(key, ""))

    def int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(str(self.raw(key)).strip()))
        except (ValueError, TypeError):
            return default

    def float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(str(self.raw(key)).strip())
        except (ValueError, TypeError):
            return default

    def bool(self, key: str, default: bool = False) -> bool:
        v = str(self.raw(key)).strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        return default

    def list(self, key: str) -> list[str]:
        raw = str(self.raw(key)).strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def float_list(self, key: str) -> list[float]:
        out = []
        for x in self.list(key):
            try:
                out.append(float(x))
            except ValueError:
                pass
        return out

    def int_list(self, key: str) -> list[int]:
        out = []
        for x in self.list(key):
            try:
                out.append(int(float(x)))
            except ValueError:
                pass
        return out

    def set(self, key: str, value: Any) -> None:
        self._values[key] = "" if value is None else str(value)

    def as_dict(self) -> dict[str, str]:
        return dict(self._values)

    def is_secret(self, key: str) -> bool:
        return key in SECRET_KEYS

    def display(self, key: str) -> str:
        v = str(self.raw(key))
        if key in SECRET_KEYS:
            return mask(v)
        return v

    def secrets(self) -> list[str]:
        return [str(self.raw(k)) for k in SECRET_KEYS if self.raw(k)]

    # -- writing ------------------------------------------------------------
    def save(self) -> None:
        lines: list[str] = [
            "# Penny Stock Early-Warning System configuration.",
            "# Mode 600. Never commit this file. Replace values, then the service",
            "# picks them up on the next cycle without a restart (except structural keys).",
            "",
        ]
        known = set()
        for group, keys in GROUPS.items():
            lines.append(f"# --- {group} ---")
            for k in keys:
                lines.append(f"{k}={self.raw(k)}")
                known.add(k)
            lines.append("")
        extra = {k: v for k, v in self._values.items() if k not in known}
        if extra:
            lines.append("# --- Extra ---")
            for k, v in sorted(extra.items()):
                lines.append(f"{k}={v}")
            lines.append("")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        restrict_permissions(tmp)
        os.replace(tmp, self.path)
        restrict_permissions(self.path)
        self._mtime = self._stat_mtime()

    def ensure_exists(self) -> None:
        if not self.path.exists():
            self.save()

    # -- derived paths ------------------------------------------------------
    @property
    def home(self) -> Path:
        base = self.raw("PENNY_HOME") or str(self.path.parent)
        p = Path(base)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def data_path(self, *parts: str) -> Path:
        p = self.home.joinpath(*parts)
        return p

    @property
    def state_db(self) -> Path:
        return self.data_path("state.db")

    @property
    def journal_db(self) -> Path:
        return self.data_path("journal.db")

    @property
    def bars_dir(self) -> Path:
        p = self.data_path("bars")
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def catalog_path(self) -> Path:
        p = Path(self.raw("AI_MODEL_CATALOG"))
        if not p.is_absolute():
            p = self.home / p
        return p

    def summary(self) -> str:
        lines = []
        for group, keys in GROUPS.items():
            lines.append(f"[{group}]")
            for k in keys:
                lines.append(f"  {k} = {self.display(k)}")
            lines.append("")
        return "\n".join(lines)
