"""Assembly: build every component from configuration (used by the CLI and the service)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .ai.catalog import ModelCatalog
from .ai.gateway import AIGateway
from .backup import BackupManager
from .commands import CommandHandler
from .config import Config
from .engine import Engine
from .journal import Journal
from .logging_setup import setup_logging, update_secrets
from .shariah import HalalShSource, MusaffaSource, ShariahService
from .sources.alpaca import AlpacaClient
from .sources.finra import FinraClient
from .sources.finnhub import FinnhubClient
from .sources.finviz import FinvizClient
from .sources.moomoo import MoomooClient
from .sources.sec import SecClient
from .sources.yahoo import YahooClient
from .store import open_journal, open_state
from .telegram import TelegramClient

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    cfg: Config
    state: object
    journal_db: object
    telegram: object
    moomoo: Optional[object] = None
    finviz: Optional[object] = None
    alpaca: Optional[object] = None
    finnhub: Optional[object] = None
    sec: Optional[object] = None
    finra: Optional[object] = None
    yahoo: Optional[object] = None
    gateway: Optional[object] = None
    catalog: Optional[ModelCatalog] = None
    shariah: Optional[ShariahService] = None
    journal: Optional[Journal] = None
    backup: Optional[BackupManager] = None
    engine: Optional[Engine] = None
    commands: Optional[CommandHandler] = None
    components: list = field(default_factory=list)

    def close(self) -> None:
        for db in (self.state, self.journal_db):
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass


def build(cfg: Config, *, with_engine: bool = True, fake: Optional[dict] = None,
          console_logs: bool = True) -> Runtime:
    """Build the runtime. `fake` injects test doubles for offline simulation."""
    fake = fake or {}
    setup_logging(cfg.home, cfg.raw("LOG_LEVEL") or "INFO", cfg.secrets(),
                  console=console_logs)
    update_secrets(cfg.secrets())

    state = open_state(cfg.state_db)
    journal_db = open_journal(cfg.journal_db)

    secrets = cfg.secrets()
    telegram = fake.get("telegram") or TelegramClient(cfg, secrets)
    moomoo = fake.get("moomoo") or (MoomooClient(cfg, secrets) if cfg.raw("MOOMOO_API_KEY") else None)
    finviz = fake.get("finviz") or (FinvizClient(cfg, secrets) if cfg.raw("FINVIZ_TOKEN") else None)
    alpaca = fake.get("alpaca") or AlpacaClient(cfg, secrets)
    finnhub = fake.get("finnhub") or FinnhubClient(cfg, secrets)
    sec = fake.get("sec") or SecClient(cfg, secrets)
    finra = fake.get("finra") or FinraClient(cfg, secrets)
    yahoo = fake.get("yahoo") or YahooClient(cfg, secrets)
    gateway = fake.get("gateway") or (AIGateway(cfg, secrets) if cfg.raw("AI_BASE_URL") else None)

    catalog = ModelCatalog(cfg.catalog_path)

    shariah = fake.get("shariah")
    if shariah is None:
        sources = [HalalShSource(cfg, secrets), MusaffaSource(cfg, secrets)]
        if any(getattr(s, "configured", False) for s in sources):
            shariah = ShariahService(cfg, state, sources, telegram)

    journal = Journal(cfg, journal_db, alpaca=alpaca, yahoo=yahoo, moomoo=moomoo)
    backup = BackupManager(cfg, state, journal_db, telegram, secrets)

    runtime = Runtime(cfg=cfg, state=state, journal_db=journal_db, telegram=telegram,
                      moomoo=moomoo, finviz=finviz, alpaca=alpaca, finnhub=finnhub,
                      sec=sec, finra=finra, yahoo=yahoo, gateway=gateway, catalog=catalog,
                      shariah=shariah, journal=journal, backup=backup)
    runtime.components = [c for c in (moomoo, finviz, alpaca, finnhub, sec, finra, yahoo)
                          if c is not None]

    if with_engine:
        engine = Engine(cfg, state, journal_db, telegram, moomoo=moomoo, finviz=finviz,
                        alpaca=alpaca, finnhub=finnhub, sec=sec, finra=finra, yahoo=yahoo,
                        gateway=gateway, catalog=catalog, shariah=shariah, backup=backup)
        if engine.runner is not None:
            from .ai.catalog import INITIAL_FALLBACKS, INITIAL_PANEL
            if not state.kv_get("active_panel"):
                state.kv_set("active_panel", cfg.list("AI_MODELS") or INITIAL_PANEL)
            if not state.kv_get("fallbacks"):
                state.kv_set("fallbacks", cfg.list("AI_FALLBACK_MODELS") or INITIAL_FALLBACKS)
            if not state.kv_get("candidates"):
                state.kv_set("candidates", catalog.pool(cfg.float("AI_MAX_MULTIPLIER", 4),
                                                        cfg.list("AI_PREMIUM_MODELS")))
        runtime.engine = engine
        runtime.commands = CommandHandler(cfg, state, journal, telegram, engine=engine,
                                          runner=engine.runner, catalog=catalog,
                                          shariah=shariah, backup=backup)
    return runtime
