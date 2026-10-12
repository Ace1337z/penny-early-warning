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
    # Custom view: the Overview view (111) ignores `c=` and omits float, short
    # float and average volume, so the report/verify columns must use 152.
    "FINVIZ_VIEW": "152",
    "FINVIZ_FILTERS": "sh_price_u10,ind_stocksonly",
    # 0..66 (custom view) plus the price/volume columns the quote shape reads:
    # 67 Volume, 71/72 After-Hours Close/Change, 81 Prev Close, 86/87/88 Open/High/Low,
    # 90..99 intraday performance. Without 67 the cumulative volume is always 0,
    # which zeroes Volx and stops every tier from ever being reached.
    "FINVIZ_COLUMNS": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67,71,72,81,86,87,88,90,91,92,93,94,95,96,97,98,99",
    "FINVIZ_SIGNALS": "top_gainers,new_high,most_active,unusual_volume,overbought,oversold,insider_buying",
    # Intraday top-gainers screens (Elite). These surface a stock that is being
    # bought *right now*, before it reaches the daily top-gainers list. Only the
    # top_gainers/top_losers bases honour a timeframe suffix.
    #
    # Short timeframes first: 1m/3m/5m catch a jump as it happens. The 15m screen
    # is deliberately *not* the default — by the time a name reaches it the stock
    # has often already moved 20%+, which is the "alert arrives late" complaint.
    "FINVIZ_INTRADAY_SIGNALS": "top_gainers_1m,top_gainers_3m,top_gainers_5m",
    "FINVIZ_EXTENDED_HOURS": "1",
    "FINVIZ_MOVERS_TTL": "15",
    "FINVIZ_QUOTE_BATCH": "40",
    # --- TwelveData (fresh price / volume / candles) ---
    # Finviz is the screener and the extended-hours source (it publishes After-Hours
    # Close/Change). TwelveData is the *freshness* source: the free plan's 1-minute
    # bars are regular-session only, so it wins on latest price/volume/candles and
    # Finviz keeps the overnight print.
    "TWELVEDATA_KEY": "",
    "TWELVEDATA_BASE": "https://api.twelvedata.com",
    "TWELVEDATA_MAX_PER_MIN": "8",
    "TWELVEDATA_MAX_PER_DAY": "800",
    "TWELVEDATA_MAX_BARS": "500",
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
    "AI_MODEL": "deepseek-v4.1-flash",
    "AI_TIMEOUT": "45",
    "AI_MAX_TOKENS": "1800",
    # Spec 2.0 B/E: the AI is adversarial, not a second vote on the gate.
    # AI_MIN_ADDITIVE=0 accepts reports that merely restate the gate;
    # RULES_FALLBACK_VERDICT=0 strips the rules-only fallback down to WAIT.
    # AI_ADVERSARIAL records which prompt is in force (v2 is the only prompt
    # today); it is listed so /keys shows the posture, not to switch prompts.
    "AI_ADVERSARIAL": "1",
    "AI_MIN_ADDITIVE": "1",
    "RULES_FALLBACK_VERDICT": "1",
    "MARKET_SYMBOLS": "SPY,QQQ,IWM",
    "TOKEN_NOTIFY_DAILY": "0",
    "TOKEN_LIMIT_DAILY": "0",
    # --- Behaviour ---
    "MAX_PRICE": "10",
    "MIN_PRICE": "0.10",
    "ALERT_MIN_TIER": "3",
    "REALERT_SECONDS": "3600",
    "ALERT_EXTENDED": "0",
    "EXTENDED_PCT": "100",
    "WARMUP_CYCLES": "2",
    "WORKERS": "8",
    "RISK_USD": "50",
    # Minimum reward:risk for a plan to be actionable. Below this the plan still
    # renders but is marked no-trade, and the AI panel must not recommend ENTER.
    "MIN_RR": "2.5",
    "BASELINE_FALLBACK": "300",
    "BASELINE_MIN_PER_MIN": "20",
    "FINVIZ_MAX_PER_MIN": "30",
    # --- Momentum-build watch feed (tier 1 before the alert bar) ---
    "BUILD_FEED": "0",
    "BUILD_FEED_SECONDS": "900",
    "BUILD_TOP_N": "6",
    "BUILD_MIN_SCORE": "35",
    "BUILD_RISE15": "1.5",
    "BUILD_VOLX15": "2",
    "BUILD_DOLLAR15": "5000",
    "BUILD_MIN_MINUTES": "10",
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
    # Fast alert lane. The tier gates above measure a 15-minute rise and a
    # 60-minute move, so by the time they fire the burst is already over -- the
    # alert was arriving at ~+20% on a stock that had already given back most of
    # it. These read the 3- and 5-minute windows instead: a stock that prints
    # this much in five minutes, on this much volume, is still tradable when the
    # message lands. Cleared to 0 to fall back to the tier gates alone.
    # FAST_ON=0 is the single switch that turns the whole lane off.
    "FAST_ON": "1",
    "FAST_RISE3": "5",
    "FAST_VOLX3": "5",
    "FAST_DOLLAR3": "3000",
    "FAST_RISE5": "7",
    "FAST_VOLX5": "5",
    "FAST_MAX_FROM_HIGH": "2",
    "FAST_MIN_VOLUME": "100000",
    # Pre-market gate: alert as soon as the move clears PREMARKET_PCT with the
    # volume to back it, rather than waiting for the tier's 15-minute rise window
    # to catch up (which historically meant the alert arrived at ~+20%).
    "PREMARKET_PCT": "15",
    "PREMARKET_VOLX15": "3",
    "PREMARKET_DOLLAR15": "5000",
    # --- Strategy gate (long-only) ---------------------------------------
    # STRATEGY_GATE=0 keeps every stock that clears the fast lane. 1 routes
    # alerts through the strategy detectors instead.
    "STRATEGY_GATE": "1",
    "GATE_MIN_SCORE": "55",
    # Cause is a label, not a gate (Spec 2.0 A): CAUSE_REQUIRED=1 restores the
    # old behaviour of refusing every alert without a named catalyst, which
    # filtered out the mechanical and social moves this tool exists to catch.
    # Dilution and DATA_CONFLICT still block regardless of this key.
    "CAUSE_REQUIRED": "0",
    "GATE_MAX_PRIMARY": "3",
    # --- Reality gate: spread + liquidity (Feature 1) ---
    # A setup you cannot fill is not a setup. The spread ceiling is the hard
    # one; the three volume numbers describe whether anyone is actually here.
    "SPREAD_MAX_PCT": "0.8",
    "MIN_DOLLAR_VOL_15M": "300000",
    "MIN_AVG_TRADE_SIZE": "500",
    "MIN_TRADES_15M": "40",
    # --- Invalidation / abort alerts (Feature 4) ---
    "INVALIDATION_ALERTS": "1",
    "INVALIDATION_CHECK_SECONDS": "30",
    "INVALIDATION_SPREAD_CONSEC_MIN": "2",
    # --- Confidence calibration (Feature 5) ---
    "CALIBRATION_MIN_SAMPLES": "30",
    "CALIBRATION_WINDOW_DAYS": "90",
    "CALIBRATION_CLAMP_MARGIN": "0.15",
    # --- Historical analogs (Feature 6) ---
    "ANALOG_K": "20",
    "ANALOG_MIN_K": "8",
    "ANALOG_LOOKBACK_DAYS": "540",
    # --- Multi-timeframe context (Feature 8) ---
    "MTF_CONTEXT": "1",
    # --- Sector heatmap (Feature 9) ---
    "SECTOR_MIN_HOT_PCT": "2.0",
    "SECTOR_TOP_N": "3",
    "SECTOR_HEATMAP_REFRESH_SECONDS": "300",
    # --- Exit liquidity (Feature 10) ---
    "EXIT_LIQ_500_MAX_SEC": "60",
    "EXIT_LIQ_MAX_SLIP_PCT": "0.5",
    # --- Post-mortems (Feature 14) ---
    "POSTMORTEM_ENABLED": "1",
    "POSTMORTEM_IN_EOD": "1",
    # --- Catalyst decay half-lives, minutes (Feature 15) ---
    "HALF_LIFE_HARD": "180",
    "HALF_LIFE_MEDIUM": "120",
    "HALF_LIFE_SOFT": "30",
    # --- First green day ---
    "FGD_LOOKBACK_DAYS": "5",
    "FGD_MIN_PCT": "15",
    "FGD_MIN_VOLX": "3.0",
    "FGD_MIN_LOW_AGE": "30",
    "FGD_MIN_HISTORY_DAYS": "6",
    # --- Low float squeeze ---
    "FLOAT_MAX": "20000000",
    # Spec 2.0 E: the squeeze floor drops from 15% to 8% so the mechanical
    # short-squeeze detector fires before the crowd is already committed.
    "SHORT_FLOAT_MIN": "8.0",
    "SHORT_RATIO_MIN": "3.0",
    # --- $1 breakout ---
    "DOLLAR_LEVEL": "1.00",
    "DOLLAR_HOLD_MINUTES": "15",
    "DOLLAR_MIN_VOLX": "4.0",
    "DOLLAR_MIN_DOLLAR_15M": "250000",
    # --- Catalyst ---
    "CATALYST_MAX_AGE_MIN": "1440",
    "CATALYST_MIN_VOLX": "3.0",
    "CATALYST_MIN_DOLLAR_15M": "150000",
    # --- News cause ---
    "CAUSE_ALIGN_WINDOW_MIN": "30",
    "CAUSE_MAX_AGE_H": "4",
    "CAUSE_SINGLE": "1",
    # --- Float rotation ---
    "FLOAT_ROTATION_MIN": "1.5",
    "FLOAT_ROTATION_STRONG": "3.0",
    "FLOAT_ROTATION_MAX_FLOAT": "20000000",
    # --- Gap and go ---
    "GAP_MIN_PCT": "5.0",
    "GAP_PREMARKET_RVOL": "2.0",
    # --- First pullback ---
    "PULLBACK_VWAP_TOLERANCE": "0.005",
    "PULLBACK_MAX_DEPTH": "0.50",
    "PULLBACK_RECLAIM_VOLX": "1.5",
    # --- Bull flag ---
    "BULLFLAG_MIN_POLE_PCT": "15",
    "BULLFLAG_CONSOLIDATION_MIN": "3",
    "BULLFLAG_CONSOLIDATION_MAX": "8",
    "BULLFLAG_BREAKOUT_VOLX": "2.0",
    # --- Red to green ---
    "R2G_MAX_MORNING_MINUTES": "30",
    "R2G_MIN_LOW_AGE": "15",
    "R2G_CROSS_VOLX": "2.0",
    # --- WORG ---
    "WORG_PRIOR_CLOSE_STRENGTH": "0.90",
    "WORG_MULTIWEEK_BREAKOUT": "1",
    # --- VWAP reclaim ---
    "VWAP_RECLAIM_MIN_BELOW": "10",
    "VWAP_RECLAIM_VOLX": "2.0",
    "VWAP_RECLAIM_HOLD_CANDLES": "3",
    # --- Day 3 surge ---
    "DAY3_MIN_DAY1_PCT": "20",
    "DAY3_MIN_DAY2_VOL_RATIO": "0.40",
    "DAY3_PREMARKET_VOL_MULT": "1.0",
    # --- Sympathy ---
    "SYMPATHY_LEADER_MIN_MCAP": "500000000",
    "SYMPATHY_LEADER_MIN_PCT": "15",
    "SYMPATHY_PEER_MAX_PCT": "15",
    "SYMPATHY_PEER_MIN_PCT": "5",
    "SYMPATHY_MIN_ROTATION": "5.0",
    # --- Failed breakout ---
    "FAILED_BREAKOUT_VOLX": "2.0",
    "FAILED_BREAKOUT_RECLAIM_CANDLES": "5",
    # --- MSS / liquidity grab ---
    "MSS_LOOKBACK": "8",
    "LIQUIDITY_GRAB_VOLX": "1.5",
    # --- Dead-cat bounce trigger ---
    "V_REVERSAL_MIN_DROP": "6.0",
    "V_REVERSAL_RECLAIM_VOLX": "1.5",
    # --- Exhaustion blocker (long-only safety) ---
    "EXHAUSTION_HIGH_PROXIMITY": "0.05",
    "EXHAUSTION_MIN_MINUTES": "20",
    "EXHAUSTION_VOLUME_DECLINE": "0.70",
    # --- Time-of-day filter ---
    "TIME_FILTER_ENABLED": "1",
    # --- Tracking and EOD ---
    "EOD_REPORT_TIME": "16:05",
    "EOD_REPORT_ENABLED": "1",
    "STATS_WINDOW_DAYS": "30",
    "TRACKER_ENABLED": "1",

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
               "FINVIZ_SIGNALS", "FINVIZ_INTRADAY_SIGNALS", "FINVIZ_EXTENDED_HOURS",
               "FINVIZ_MOVERS_TTL", "FINVIZ_QUOTE_BATCH"],
    "Other sources": ["ALPACA_KEY", "ALPACA_SECRET", "ALPACA_FEED", "FINNHUB_KEY", "SEC_USER_AGENT",
                      "TWELVEDATA_KEY", "TWELVEDATA_BASE", "TWELVEDATA_MAX_PER_MIN",
                      "TWELVEDATA_MAX_PER_DAY", "TWELVEDATA_MAX_BARS"],
    "Telegram": ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"],
    "Shariah": ["HALALTERMINAL_API_KEY", "HALALTERMINAL_BASE_URL",
                "SHARIAH_MODE", "SHARIAH_TTL_DAYS", "SHARIAH_MAX_CALLS_MONTH"],
    "Backup": ["BACKUP_LOCAL_DIR", "BACKUP_REMOTE", "BACKUP_PASSPHRASE", "BACKUP_HOURLY",
               "BACKUP_TELEGRAM", "BACKUP_KEEP_HOURLY", "BACKUP_KEEP_DAILY",
               "BACKUP_KEEP_WEEKLY", "BACKUP_KEEP_MONTHLY", "BACKUP_MAX_AGE_HOURS"],
    "AI": ["AI_BASE_URL", "AI_KEY", "AI_MODEL", "AI_TIMEOUT", "AI_MAX_TOKENS",
           "AI_ADVERSARIAL", "AI_MIN_ADDITIVE", "RULES_FALLBACK_VERDICT",
           "MARKET_SYMBOLS", "TOKEN_NOTIFY_DAILY", "TOKEN_LIMIT_DAILY"],
    "Behaviour": ["MAX_PRICE", "MIN_PRICE", "ALERT_MIN_TIER", "REALERT_SECONDS", "ALERT_EXTENDED",
                  "EXTENDED_PCT", "WARMUP_CYCLES", "WORKERS", "RISK_USD", "MIN_RR", "BASELINE_FALLBACK",
                  "PREMARKET_PCT", "PREMARKET_VOLX15", "PREMARKET_DOLLAR15",
                  "FINVIZ_MAX_PER_MIN", "RETENTION_DAYS"],
    "Fast alerts": ["FAST_ON", "FAST_RISE3", "FAST_VOLX3", "FAST_DOLLAR3", "FAST_RISE5",
                    "FAST_VOLX5", "FAST_MAX_FROM_HIGH", "FAST_MIN_VOLUME"],
    "Strategy gate": ["STRATEGY_GATE", "GATE_MIN_SCORE", "CAUSE_REQUIRED",
                      "GATE_MAX_PRIMARY", "TIME_FILTER_ENABLED",
                      "SPREAD_MAX_PCT", "MIN_DOLLAR_VOL_15M", "MIN_AVG_TRADE_SIZE",
                      "MIN_TRADES_15M", "EXIT_LIQ_500_MAX_SEC", "EXIT_LIQ_MAX_SLIP_PCT",
                      "SHORT_FLOAT_MIN", "AI_ADVERSARIAL", "AI_MIN_ADDITIVE",
                      "RULES_FALLBACK_VERDICT",
                      "HALF_LIFE_HARD", "HALF_LIFE_MEDIUM", "HALF_LIFE_SOFT"],
    "Strategies": ["FGD_LOOKBACK_DAYS", "FGD_MIN_PCT", "FGD_MIN_VOLX", "FGD_MIN_LOW_AGE",
                   "FGD_MIN_HISTORY_DAYS", "FLOAT_MAX", "SHORT_FLOAT_MIN", "SHORT_RATIO_MIN",
                   "SQUEEZE_MIN_VOLX", "DOLLAR_LEVEL", "DOLLAR_HOLD_MINUTES", "DOLLAR_MIN_VOLX",
                   "DOLLAR_MIN_DOLLAR_15M", "CATALYST_MAX_AGE_MIN", "CATALYST_MIN_VOLX",
                   "CATALYST_MIN_DOLLAR_15M", "CAUSE_ALIGN_WINDOW_MIN", "CAUSE_MAX_AGE_H",
                   "CAUSE_SINGLE", "FLOAT_ROTATION_MIN", "FLOAT_ROTATION_STRONG",
                   "FLOAT_ROTATION_MAX_FLOAT", "GAP_MIN_PCT", "GAP_PREMARKET_RVOL",
                   "PULLBACK_VWAP_TOLERANCE", "PULLBACK_MAX_DEPTH", "PULLBACK_RECLAIM_VOLX",
                   "BULLFLAG_MIN_POLE_PCT", "BULLFLAG_CONSOLIDATION_MIN",
                   "BULLFLAG_CONSOLIDATION_MAX", "BULLFLAG_BREAKOUT_VOLX", "R2G_MAX_MORNING_MINUTES",
                   "R2G_MIN_LOW_AGE", "R2G_CROSS_VOLX", "WORG_PRIOR_CLOSE_STRENGTH",
                   "WORG_MULTIWEEK_BREAKOUT", "VWAP_RECLAIM_MIN_BELOW", "VWAP_RECLAIM_VOLX",
                   "VWAP_RECLAIM_HOLD_CANDLES", "DAY3_MIN_DAY1_PCT", "DAY3_MIN_DAY2_VOL_RATIO",
                   "DAY3_PREMARKET_VOL_MULT", "SYMPATHY_LEADER_MIN_MCAP",
                   "SYMPATHY_LEADER_MIN_PCT", "SYMPATHY_PEER_MAX_PCT", "SYMPATHY_PEER_MIN_PCT",
                   "SYMPATHY_MIN_ROTATION", "FAILED_BREAKOUT_VOLX",
                   "FAILED_BREAKOUT_RECLAIM_CANDLES", "MSS_LOOKBACK", "LIQUIDITY_GRAB_VOLX",
                   "V_REVERSAL_MIN_DROP", "V_REVERSAL_RECLAIM_VOLX", "EXHAUSTION_HIGH_PROXIMITY",
                   "EXHAUSTION_MIN_MINUTES", "EXHAUSTION_VOLUME_DECLINE"],
    "Tracking base": ["TRACKER_ENABLED", "EOD_REPORT_ENABLED", "EOD_REPORT_TIME"],
    "Build feed": ["BUILD_FEED", "BUILD_FEED_SECONDS", "BUILD_TOP_N", "BUILD_MIN_SCORE",
                   "BUILD_RISE15", "BUILD_VOLX15", "BUILD_DOLLAR15", "BUILD_MIN_MINUTES"],
    "Tracking": ["TRACKER_ENABLED", "EOD_REPORT_ENABLED", "EOD_REPORT_TIME", "STATS_WINDOW_DAYS",
                 "INVALIDATION_ALERTS", "INVALIDATION_CHECK_SECONDS",
                 "INVALIDATION_SPREAD_CONSEC_MIN", "POSTMORTEM_ENABLED", "POSTMORTEM_IN_EOD"],
    "AI context": ["CALIBRATION_MIN_SAMPLES", "CALIBRATION_WINDOW_DAYS",
                   "CALIBRATION_CLAMP_MARGIN", "ANALOG_K", "ANALOG_MIN_K",
                   "ANALOG_LOOKBACK_DAYS", "MTF_CONTEXT", "SECTOR_MIN_HOT_PCT",
                   "SECTOR_TOP_N", "SECTOR_HEATMAP_REFRESH_SECONDS"],
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

    def summary(self) -> str:
        lines = []
        for group, keys in GROUPS.items():
            lines.append(f"[{group}]")
            for k in keys:
                lines.append(f"  {k} = {self.display(k)}")
            lines.append("")
        return "\n".join(lines)
