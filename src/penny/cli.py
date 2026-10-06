"""Command line interface: setup, set, config, run, doctor, simulate, status, models, journal."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .config import DEFAULTS, GROUPS, SECRET_KEYS, Config
from .util import mask, session_for

log = logging.getLogger(__name__)

PROMPTS: dict[str, tuple[str, bool, str]] = {
    "FINVIZ_TOKEN": ("Finviz Elite API token (all market data: universe, movers, quotes)", True,
                     "Finviz Elite account, API/export section"),
    "TELEGRAM_TOKEN": ("Telegram bot token (alerts and commands)", True, "@BotFather"),
    "TELEGRAM_CHAT_ID": ("Telegram chat id", True, "detected automatically after a message"),
    "AI_BASE_URL": ("AI gateway base URL (OpenAI-compatible, e.g. https://host/v1)", False,
                    "the provider's base URL; /set can also change it later"),
    "AI_KEY": ("AI gateway key", False, "the AI gateway account; can also be set later with /set"),
    "AI_MODELS": ("Active panel model ids, comma separated", False,
                  "e.g. model-a,model-b,model-c"),
    "SEC_USER_AGENT": ("SEC contact as 'Name email' (required for SEC EDGAR)", False,
                       "your own name and email"),
    "ALPACA_KEY": ("Alpaca key id (optional: candles fallback, news)", False, "Alpaca dashboard"),
    "ALPACA_SECRET": ("Alpaca secret (optional)", False, "Alpaca dashboard"),
    "FINNHUB_KEY": ("Finnhub key (optional: news)", False, "finnhub.io"),
    "HALALTERMINAL_API_KEY": ("halalterminal.com API key (optional: Shariah status)", False,
                              "halalterminal.com dashboard"),
    "BACKUP_PASSPHRASE": ("Backup passphrase (required for off-server backups)", False,
                          "chosen by you; store it safely"),
    "BACKUP_REMOTE": ("Off-server backup destination (rclone remote or a directory)", False,
                      "your object storage, SFTP or cloud drive"),
    "GITHUB_TOKEN": ("GitHub read-only token (only for a private repository)", False,
                     "GitHub settings"),
}

# Only Telegram is required at install time, so the bot is reachable and every other
# key can be entered later with /set. Everything else here is optional.
SETUP_ORDER = [
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "FINVIZ_TOKEN", "AI_BASE_URL", "AI_KEY",
    "AI_MODELS", "SEC_USER_AGENT", "ALPACA_KEY", "ALPACA_SECRET", "FINNHUB_KEY",
    "HALALTERMINAL_API_KEY", "BACKUP_PASSPHRASE", "BACKUP_REMOTE", "GITHUB_TOKEN",
]


# --- helpers ----------------------------------------------------------------

def _config(args) -> Config:
    path = getattr(args, "config", None)
    return Config(path) if path else Config()


def _ask(prompt: str, *, secret: bool = False, current: str = "",
         required: bool = False, default: str = "") -> str:
    shown = ""
    if current:
        shown = f" [{mask(current) if secret else current}]"
    elif default:
        shown = f" [{default}]"
    suffix = "" if required else " (optional, Enter to skip)"
    try:
        if secret:
            value = getpass.getpass(f"{prompt}{shown}{suffix}: ")
        else:
            value = input(f"{prompt}{shown}{suffix}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return current or default
    value = value.strip()
    if not value:
        return current or default
    return value


def cmd_setup(args) -> int:
    cfg = _config(args)
    cfg.ensure_exists()
    print("Penny Stock Early-Warning System setup")
    print(f"configuration file: {cfg.path}")
    if cfg.path.exists():
        print("existing values are shown masked; press Enter to keep them.\n")

    non_interactive = args.non_interactive or not sys.stdin.isatty()
    if non_interactive:
        print("non-interactive mode: reading values from the environment")
        for key in SETUP_ORDER:
            env = os.environ.get(key)
            if env:
                cfg.set(key, env)
        cfg.save()
        print("configuration written from environment values.")
        _post_setup_checks(cfg, args)
        return 0

    for key in SETUP_ORDER:
        prompt, required, where = PROMPTS[key]
        current = cfg.raw(key)
        label = f"{prompt} ({where})"
        if key == "TELEGRAM_CHAT_ID" and not current:
            value = _detect_chat_id(cfg)
            if value:
                cfg.set(key, value)
                continue
        value = _ask(label, secret=key in SECRET_KEYS, current=current, required=required)
        if value:
            cfg.set(key, value)

    if cfg.raw("AI_BASE_URL") and cfg.raw("AI_KEY"):
        _discover_models(cfg)

    cfg.save()
    print(f"\nconfiguration written to {cfg.path} (mode 600)")
    print("Anything you skipped can be set later from Telegram: /set KEY VALUE, /keys to list.")
    _post_setup_checks(cfg, args)
    return 0


def _detect_chat_id(cfg: Config) -> Optional[str]:
    token = cfg.raw("TELEGRAM_TOKEN")
    if not token:
        return None
    print("\nSend any message to your Telegram bot now; waiting up to 60 seconds...")
    from .telegram import TelegramClient
    client = TelegramClient(cfg, cfg.secrets())
    chat_id = client.detect_chat_id(attempts=30, timeout=2)
    if chat_id:
        print(f"detected chat id {chat_id}")
    else:
        print("could not detect the chat id; you can enter it manually")
    return chat_id


def _discover_models(cfg: Config) -> None:
    from .ai.catalog import ModelCatalog
    from .ai.gateway import AIGateway
    gateway = AIGateway(cfg, cfg.secrets())
    catalog = ModelCatalog(cfg.catalog_path)
    try:
        models = gateway.list_models()
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not read the model list from the provider ({exc}).")
        print("Enter model ids manually (comma separated, optionally id:multiplier).")
        raw = input("model ids: ").strip()
        if raw:
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                model, _, mult = token.partition(":")
                try:
                    catalog.add(model, float(mult or 1.0))
                except ValueError:
                    catalog.add(model, 1.0)
            cfg.set("AI_MODELS", ",".join(t.split(":")[0] for t in raw.split(",") if t.strip()))
        return
    known = [m for m in models if catalog.get(m)]
    unknown = [m for m in models if not catalog.get(m)]
    print(f"\nThe provider lists {len(models)} models.")
    if unknown:
        print("Not in your catalogue (not used until you confirm them and give a multiplier):")
        for m in unknown[:20]:
            print(f"  {m}")
        print("Add with: penny models add ID MULTIPLIER")
    if known:
        default = cfg.raw("AI_MODELS")
        print(f"current active panel: {default}")
        choice = input("keep the current panel? [Y/n]: ").strip().lower()
        if choice == "n":
            raw = input("active panel model ids (comma separated): ").strip()
            if raw:
                cfg.set("AI_MODELS", raw)


def _post_setup_checks(cfg: Config, args) -> None:
    if getattr(args, "skip_doctor", False):
        return
    print("\nrunning the offline self-test...")
    from .selftest import run_selftest
    report = run_selftest(cfg)
    print(report.text())
    if getattr(args, "doctor", False):
        print("\nrunning the live checker...")
        from .doctor import run_doctor
        from .runtime import build
        runtime = build(cfg, with_engine=False, console_logs=False)
        try:
            print(run_doctor(runtime).text())
        finally:
            runtime.close()


def cmd_set(args) -> int:
    cfg = _config(args)
    key = args.key.upper()
    if key not in DEFAULTS and not args.force:
        print(f"unknown key {key}. Use --force to add a new key anyway.")
        return 2
    value = args.value
    if value is None:
        current = cfg.raw(key)
        value = _ask(key, secret=key in SECRET_KEYS, current=current)
    cfg.set(key, value)
    cfg.save()
    if key in SECRET_KEYS:
        print(f"{key} updated (value hidden).")
    else:
        print(f"{key} = {value}")
    if key in ("FINVIZ_TOKEN", "AI_KEY", "AI_BASE_URL", "HALALTERMINAL_API_KEY"):
        print("The running service picks up the new value on the next cycle; no restart needed.")
    return 0


def cmd_config_show(args) -> int:
    cfg = _config(args)
    if args.key:
        key = args.key.upper()
        print(f"{key} = {cfg.display(key)}")
        return 0
    print(cfg.summary())
    return 0


def cmd_run(args) -> int:
    cfg = _config(args)
    from .runtime import build
    runtime = build(cfg)
    engine = runtime.engine
    if args.once:
        stats = engine.cycle()
        print(json.dumps(stats, indent=2))
        runtime.close()
        return 0
    import threading
    thread = None
    if runtime.commands is not None:
        thread = threading.Thread(target=runtime.commands.run_forever, daemon=True,
                                  name="commands")
        thread.start()
    try:
        engine.run_forever(poll=args.poll)
    except KeyboardInterrupt:
        engine.stop()
    finally:
        if runtime.commands is not None:
            runtime.commands.stop()
        runtime.close()
    return 0


def cmd_doctor(args) -> int:
    cfg = _config(args)
    from .doctor import run_doctor
    from .runtime import build
    runtime = build(cfg, with_engine=args.with_engine, console_logs=False)
    try:
        report = run_doctor(runtime, test_models=not args.no_models)
    finally:
        runtime.close()
    print(report.text())
    if not report.ok and args.reenter:
        keys: list[str] = []
        for check in report.failures():
            key = _key_for_check(check.name)
            if key and key not in keys:
                keys.append(key)
        # A failing AI section usually needs both the URL and the key, so offer both.
        if any(k in ("AI_BASE_URL", "AI_KEY") for k in keys):
            for k in ("AI_BASE_URL", "AI_KEY"):
                if k not in keys:
                    keys.append(k)
        for key in keys:
            value = _ask(f"new value for {key}", secret=key in SECRET_KEYS,
                         current=cfg.raw(key))
            if value:
                cfg.set(key, value)
        cfg.save()
        print("credentials updated; run penny doctor again.")
    return 0 if report.ok else 1


def _key_for_check(name: str) -> Optional[str]:
    mapping = {
        "Finviz": "FINVIZ_TOKEN",
        "Telegram": "TELEGRAM_TOKEN",
        "SEC EDGAR": "SEC_USER_AGENT",
        "Alpaca (optional)": "ALPACA_KEY",
        "Finnhub (optional)": "FINNHUB_KEY",
    }
    if name in mapping:
        return mapping[name]
    if name == "AI model list" or name == "AI models":
        return "AI_BASE_URL"
    if name.startswith("AI model") or name == "AI gateway":
        return "AI_KEY"
    if name.startswith("Shariah: halalterminal"):
        return "HALALTERMINAL_API_KEY"
    return None


def cmd_simulate(args) -> int:
    from .simulation import run_simulation
    report = run_simulation(minutes=args.minutes, home=args.home)
    print(report.text())
    if args.verbose:
        print("\n--- messages ---")
        for m in report.messages[-12:]:
            print("-" * 40)
            print(m)
    return 0 if report.ok else 1


def cmd_selftest(args) -> int:
    cfg = _config(args)
    from .selftest import run_selftest
    report = run_selftest(cfg)
    print(report.text())
    return 0 if report.ok else 1


def cmd_status(args) -> int:
    cfg = _config(args)
    from .runtime import build
    runtime = build(cfg, with_engine=True, console_logs=False)
    try:
        print(runtime.engine.status_text())
        age = runtime.backup.last_verified_age_hours() if runtime.backup else None
        print(f"backup: {age:.1f} h since last verified" if age is not None
              else "backup: none yet")
        counts = runtime.state.row_counts()
        print("state tables: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    finally:
        runtime.close()
    return 0


def cmd_models(args) -> int:
    cfg = _config(args)
    from .ai.catalog import ModelCatalog
    from .ai.leaderboard import apply_selection, leaderboard_text
    from .runtime import build
    runtime = build(cfg, with_engine=True, console_logs=False)
    try:
        catalog = runtime.catalog
        if args.action == "add":
            if not args.model or args.multiplier is None:
                print("usage: penny models add ID MULTIPLIER")
                return 2
            catalog.add(args.model, args.multiplier)
            print(f"added {args.model} at {args.multiplier}x")
            return 0
        if args.action == "remove":
            ok = catalog.remove(args.model)
            print(f"removed {args.model}" if ok else f"{args.model} not in the catalogue")
            return 0
        if args.action == "enable" or args.action == "disable":
            ok = catalog.enable(args.model, args.action == "enable")
            print(f"{args.action}d {args.model}" if ok else f"{args.model} not found")
            return 0
        if args.action == "refresh":
            if runtime.engine.runner is None:
                print("AI gateway not configured")
                return 1
            result = runtime.engine.runner.discovery(None)
            print(json.dumps({k: v for k, v in result.items() if k != "listed"}, indent=2))
            print(f"listed {len(result['listed'])} models")
            return 0
        if args.action == "apply":
            panel, changed = apply_selection(runtime.state, cfg, catalog, None)
            print(f"panel {'changed to' if changed else 'unchanged:'} {', '.join(panel)}")
            return 0
        print(leaderboard_text(runtime.state, cfg, catalog, limit=args.limit))
    finally:
        runtime.close()
    return 0


def cmd_journal(args) -> int:
    cfg = _config(args)
    from .runtime import build
    runtime = build(cfg, with_engine=False, console_logs=False)
    journal = runtime.journal
    try:
        if args.action == "add":
            journal.add_event(args.symbol, args.date, args.label,
                              pattern=args.pattern or "", peak_pct=args.peak,
                              entry=args.entry, exit_price=args.exit,
                              notes=args.notes or "")
            print("event added")
        elif args.action == "import":
            text = Path(args.file).read_text(encoding="utf-8")
            count = journal.import_batch(text)
            print(f"imported {count} events")
        elif args.action == "list":
            for row in journal.events(limit=args.limit):
                print(f"{row['date']} {row['symbol']:<6} {row['label']:<7} "
                      f"{row['pattern'] or '':<8} {row['peak_pct'] or ''}")
        elif args.action == "download":
            bars = journal.download_bars(args.symbol, args.date)
            print(f"downloaded {len(bars)} bars")
        elif args.action == "metrics":
            m = journal.annotate_metrics(args.symbol, args.date)
            print(json.dumps(m.as_dict(), indent=2) if m else "no bars for that event")
        elif args.action == "discover":
            for item in journal.discover(args.symbol, days=args.days):
                print(f"{item['date']} {item['gain_pct']:+.1f}%")
        elif args.action == "rules":
            results = journal.rule_search(limit=args.limit)
            labels = journal.labels()
            print(f"trust: {labels['trust']} "
                  f"({labels['winners']} winners, {labels['losers']} losers)")
            for r in results[:args.top]:
                print(f"{r.score:+.3f}  {r.describe():<48} "
                      f"hit {r.fired_winners}/{r.winners} capture "
                      f"{(r.capture or 0) * 100:.0f}% false "
                      f"{(r.false_rate or 0) * 100:.0f}%")
        else:
            print(journal.summary_text())
    finally:
        runtime.close()
    return 0


def cmd_backup(args) -> int:
    cfg = _config(args)
    from .runtime import build
    runtime = build(cfg, with_engine=False, console_logs=False)
    try:
        if args.action == "now":
            result = runtime.backup.run(args.kind)
            print(f"{result.status}: {result.path} ({result.size / 1024:.0f} KB) "
                  f"{result.note}")
            return 0 if result.status == "ok" else 1
        if args.action == "list":
            print(runtime.backup.health_text())
            return 0
        if args.action == "drill":
            result = runtime.backup.drill()
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("ok") else 1
    finally:
        runtime.close()
    return 0


def cmd_restore(args) -> int:
    cfg = _config(args)
    from .runtime import build
    runtime = build(cfg, with_engine=False, console_logs=False)
    try:
        print(f"restoring '{args.source}' (scope {args.scope}) - this is destructive")
        if not args.yes:
            confirm = input("type RESTORE to continue: ").strip()
            if confirm != "RESTORE":
                print("aborted")
                return 1
        result = runtime.backup.restore(args.source, scope=args.scope)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1
    finally:
        runtime.close()


def cmd_update(args) -> int:
    """Download the newest archive over the install directory; keep the previous version."""
    import shutil
    import tarfile
    import tempfile
    import urllib.request

    cfg = _config(args)
    base = str(cfg.raw("PENNY_URL") or "").rstrip("/")
    if not base:
        print("PENNY_URL is not set")
        return 2
    version = args.version or cfg.raw("PENNY_VERSION") or "main"
    install_dir = Path(args.install_dir or cfg.raw("INSTALL_DIR") or Path.cwd())
    url = f"{base}/archive/refs/{'tags' if version != 'main' else 'heads'}/{version}.tar.gz"
    print(f"downloading {url}")

    headers = {}
    token = cfg.raw("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and args.private:
        headers["Authorization"] = f"token {token}"

    workdir = Path(tempfile.mkdtemp(prefix="penny-update-"))
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            archive = workdir / "src.tar.gz"
            archive.write_bytes(resp.read())
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(workdir / "src")
        roots = [p for p in (workdir / "src").iterdir() if p.is_dir()]
        if not roots:
            print("archive was empty")
            return 1
        new_src = roots[0]

        if not args.no_backup:
            from .runtime import build
            runtime = build(cfg, with_engine=False, console_logs=False)
            try:
                runtime.backup.run("pre-update")
            finally:
                runtime.close()

        stamp = time.strftime("%Y%m%d-%H%M%S")
        rollback_dir = install_dir / ".rollback" / stamp
        if install_dir.exists():
            rollback_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(install_dir, rollback_dir,
                            ignore=shutil.ignore_patterns("data", "logs", "backups",
                                                          "config.env", ".venv", ".rollback"),
                            dirs_exist_ok=True)
        for item in new_src.iterdir():
            target = install_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        cfg.set("PENNY_VERSION", version)
        cfg.save()
        print(f"updated to {version}; previous version kept at {rollback_dir}")
        print("reinstalling dependencies and running the self-test...")
        os.system(f"cd {install_dir} && .venv/bin/pip install -r requirements.txt")
        from .selftest import run_selftest
        report = run_selftest(cfg)
        print(report.text())
        return 0 if report.ok else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def cmd_rollback(args) -> int:
    cfg = _config(args)
    install_dir = Path(args.install_dir or cfg.raw("INSTALL_DIR") or Path.cwd())
    rollback_root = install_dir / ".rollback"
    if not rollback_root.exists():
        print("no rollback available")
        return 1
    versions = sorted([p for p in rollback_root.iterdir() if p.is_dir()])
    if not versions:
        print("no rollback available")
        return 1
    chosen = versions[-1] if not args.version else rollback_root / args.version
    if not chosen.exists():
        print(f"rollback {args.version} not found")
        return 1
    for item in chosen.iterdir():
        target = install_dir / item.name
        if item.is_dir():
            import shutil
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            import shutil
            shutil.copy2(item, target)
    print(f"rolled back to {chosen.name}")
    return 0


def cmd_version(args) -> int:
    print(f"penny-early-warning {__version__}")
    return 0


def cmd_detect_chat(args) -> int:
    """Wait for a message to the bot and store the chat id in the configuration file."""
    cfg = _config(args)
    if not cfg.raw("TELEGRAM_TOKEN"):
        print("TELEGRAM_TOKEN is not set; run penny setup first")
        return 2
    from .telegram import TelegramClient
    print("Send any message to your Telegram bot now; waiting up to 90 seconds...")
    client = TelegramClient(cfg, cfg.secrets())
    chat_id = client.detect_chat_id(attempts=45, timeout=2)
    if not chat_id:
        print("could not detect the chat id; set it with: penny set TELEGRAM_CHAT_ID <id>")
        return 1
    cfg.set("TELEGRAM_CHAT_ID", str(chat_id))
    cfg.save()
    print(f"detected chat id {chat_id} and stored it in {cfg.path}")
    return 0


# --- argument parsing -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="penny",
                                     description="Penny Stock Early-Warning System")
    parser.add_argument("--config", help="path to the configuration file")
    parser.add_argument("--version", action="version", version=f"penny {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="interactive setup wizard (asks for all keys first)")
    p.add_argument("--non-interactive", action="store_true",
                   help="read values from environment variables")
    p.add_argument("--doctor", action="store_true", help="run the live checker at the end")
    p.add_argument("--skip-doctor", action="store_true")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("set", help="change one setting")
    p.add_argument("key")
    p.add_argument("value", nargs="?")
    p.add_argument("--force", action="store_true", help="add a key not in the defaults")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("config", help="show settings")
    p.add_argument("action", choices=["show"], nargs="?", default="show")
    p.add_argument("key", nargs="?")
    p.set_defaults(func=cmd_config_show)

    p = sub.add_parser("run", help="run the service")
    p.add_argument("--once", action="store_true", help="run a single cycle and exit")
    p.add_argument("--poll", type=float, default=None, help="cycle interval in seconds")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("doctor", help="live checker: every source, Telegram and every AI model")
    p.add_argument("--no-models", action="store_true", help="skip the per-model test calls")
    p.add_argument("--with-engine", action="store_true")
    p.add_argument("--reenter", action="store_true", help="offer to re-enter failed credentials")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("simulate", help="offline simulation with a synthetic market")
    p.add_argument("--minutes", type=int, default=150)
    p.add_argument("--home", default=None)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("selftest", help="offline self-test")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("status", help="print status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("models", help="model leaderboard and catalogue")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "refresh", "add", "remove", "enable", "disable", "apply"])
    p.add_argument("model", nargs="?")
    p.add_argument("multiplier", nargs="?", type=float)
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("journal", help="journal tools")
    p.add_argument("action", nargs="?", default="summary",
                   choices=["summary", "add", "import", "list", "download", "metrics",
                            "discover", "rules"])
    p.add_argument("symbol", nargs="?")
    p.add_argument("date", nargs="?")
    p.add_argument("label", nargs="?")
    p.add_argument("--pattern", default="")
    p.add_argument("--peak", type=float)
    p.add_argument("--entry", type=float)
    p.add_argument("--exit", type=float)
    p.add_argument("--notes", default="")
    p.add_argument("--file")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--top", type=int, default=15)
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("backup", help="backups")
    p.add_argument("action", nargs="?", default="now", choices=["now", "list", "drill"])
    p.add_argument("--kind", default="manual")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore a backup (destructive)")
    p.add_argument("source", help="a path, a remote, or 'latest'")
    p.add_argument("--scope", default="full",
                   choices=["full", "learning", "journal"])
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("update", help="download and install the newest version")
    p.add_argument("--version", default=None)
    p.add_argument("--install-dir", default=None)
    p.add_argument("--private", action="store_true", help="use the GitHub read-only token")
    p.add_argument("--no-backup", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("rollback", help="restore the previous version")
    p.add_argument("--version", default=None)
    p.add_argument("--install-dir", default=None)
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("version", help="print the version")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("detect-chat", help="read the Telegram chat id from the next bot message")
    p.set_defaults(func=cmd_detect_chat)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.exception("command failed: %s", exc)
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
