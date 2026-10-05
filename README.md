# Penny Stock Early-Warning System

A server-side system that watches **every US stock priced under $10** during pre-market,
regular and post-market sessions and alerts you on Telegram when a stock starts
**building** a price move on rising volume — early enough to enter manually.

It scores momentum *quality and acceleration* (not the % level alone), so a slow
"grinder" (+20% early, then a steady climb) is caught while it is still building, and a
fast "burst" is caught within minutes of starting. After the alert it explains the likely
catalyst, forecasts the market's reaction and where the price may go, and proposes a
Fibonacci-based entry, stop and targets.

> **High risk.** Penny stocks are extremely volatile. Everything here is automated
> screening and estimation, **not financial advice**, and not a religious ruling.
> The system places no orders; you trade manually.

---

## What it does

```
 Moomoo OpenAPI ──► mover discovery (ranked list + full universe sweep)
                            │
                            ▼
                    quote normalization
                            ▼
            scoring engine (rolling windows, tiers, score)
                            │ tier rises
                            ▼
        ALERT 1 (Telegram, instant, no AI)
                            ▼
   parallel enrichment: Finviz verification · candles · news ·
   filings (SEC, Finviz) · short interest/volume · market context
                            ▼
            technicals + Fibonacci + trade plan
                            ▼
        ALERT 2 (facts first, "AI running")
                            ▼
   AI active panel ──► reaction + price forecast ──► ALERT 2 edited
   other candidates run in shadow ──► forecasts stored
                            ▼
   forecast evaluator (+15m, +60m, session end) ──► model leaderboard
                            ▼
            active panel = the three most accurate models
```

- **Storage:** a state database, a journal database and 1-minute bar files.
- **Shariah status** (halal.sh + Musaffa, cached) is shown on every alert screen.
- **Backups:** scheduled snapshots to a second location and off-server, with a weekly
  restore drill, so a server failure or a bad update never erases what the system learned.
- **Interfaces:** Telegram (alerts + commands) and the command line.

---

## Requirements

- A Linux VPS (Ubuntu 24.04 LTS tested), 2 vCPU / 2 GB RAM / 25 GB SSD, public IPv4,
  outbound HTTPS. Time sync (chrony/NTP) is **mandatory** — Moomoo rejects requests when
  the clock is off by more than 5 seconds.
- Python 3.10+ (3.12 recommended).
- Accounts and keys:
  - **Moomoo OpenAPI** AppKey + a key pair (mover discovery, snapshots, short data)
  - **Finviz Elite** API token (universe, verification, news, filings, sector, calendar)
  - **Telegram** bot token + chat id (alerts and commands)
  - **AI gateway** base URL, key and model ids (reaction and price forecasts)
  - **SEC** contact string, `Name email` (required for SEC EDGAR)
  - optional: Alpaca, Finnhub, halal.sh, Musaffa; a backup passphrase and off-server
    destination.

---

## Install

Download the installer and run it **directly** (not piped into `bash`, so it can read
your answers). It asks for every credential *first*, before anything is downloaded.

```bash
wget https://raw.githubusercontent.com/<USER>/penny-early-warning/main/install.sh
bash install.sh
```

The installer is idempotent: running it again keeps existing values (shown masked;
pressing Enter keeps them). It installs packages, downloads the source, creates a virtual
environment, writes `config.env` (mode 600), generates a Moomoo key pair when needed,
detects the Telegram chat id, installs the systemd unit, runs the offline self-test and
the live `doctor`, and offers to start the service.

**Non-interactive** (for automation):

```bash
PENNY_NONINTERACTIVE=1 \
MOOMOO_API_KEY=... FINVIZ_TOKEN=... TELEGRAM_TOKEN=... AI_BASE_URL=... AI_KEY=... \
SEC_USER_AGENT="Your Name you@example.com" \
bash install.sh
```

### Manual install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env && chmod 600 config.env
.venv/bin/penny setup            # interactive wizard (asks for all keys first)
.venv/bin/penny doctor           # live checker: PASS/FAIL per service
.venv/bin/penny selftest         # offline self-test
.venv/bin/penny simulate         # offline simulation (no network, no credentials)
```

---

## Command line

| Command | Purpose |
|---|---|
| `penny setup` | interactive wizard; asks for every key first, then runs the self-test |
| `penny set KEY [VALUE]` | change one setting (hidden input for secrets) |
| `penny config show [KEY]` | list settings with secrets masked (first 4 characters) |
| `penny run` | run the service (engine + Telegram command poller) |
| `penny doctor` | live checker: every source, Telegram and every AI model, PASS/FAIL |
| `penny selftest` | offline self-test (no network, no credentials) |
| `penny simulate` | offline simulation with a synthetic market |
| `penny status` | uptime, session, cycle, tokens, backups |
| `penny models` | model leaderboard; `add`/`remove`/`enable`/`disable`/`apply`/`refresh` |
| `penny journal ...` | add/import/list events, download bars, metrics, rule search, discover |
| `penny backup now\|list\|drill` | create, list and verify backups |
| `penny restore SOURCE --scope full\|learning\|journal` | restore a backup (destructive) |
| `penny update` / `penny rollback` | install a new version / restore the previous one |
| `penny keygen` | generate a Moomoo key pair and print the public key |
| `penny detect-chat` | read the Telegram chat id from the next message to the bot |

`--config <path>` selects a configuration file (default `$PENNY_HOME/config.env`).

---

## Telegram commands

Accepted **only from the configured chat id**.

```
/top                     champions and hidden gems
/watch SYM ENTRY [STOP] [TARGET]
/unwatch SYM
/watchlist
/check SYM               full analysis on demand
/market                  market context now
/models                  leaderboard and active panel
/models refresh          re-read the provider's model list
/models add ID MULT
/models remove ID
/setmodels A,B,C         set the panel manually
/halal SYM               Shariah status now
/backup now
/backups                 recent backups and destination health
/status
/help
```

---

## Configuration

Every setting lives in one env-style file (mode 600), re-read whenever its modification
time changes — most changes apply from the next cycle without a restart. See
[`config.env.example`](config.env.example) for the full list with defaults. Groups:

- **Feed** — `DATA_PROVIDER`, Moomoo key/path, mover method, poll intervals, extra symbols
- **Finviz** — token, base URL, view, filters, columns
- **Other sources** — Alpaca, Finnhub, SEC contact
- **Telegram** — bot token and chat id
- **Shariah** — halal.sh/Musaffa keys, `SHARIAH_MODE` (tag | only_compliant |
  hide_noncompliant | off), TTL and monthly call limit
- **Backup** — local dir, off-server destination, passphrase, schedule and retention
- **AI** — gateway URL/key, panel, fallbacks, cache, shadow sample rate, discovery,
  multiplier limit, premium models
- **Behaviour** — price range, alert tiers, re-alert window, warm-up, workers, risk size
- **Tier thresholds** and **score weights** — the tunables from the design

**Structural settings** (`DATA_PROVIDER`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) may need a
restart; everything else applies live.

Change the Finviz token at any time — the next Finviz request uses the new token and no
restart occurs:

```bash
penny set FINVIZ_TOKEN
```

---

## Security

- **No secret is ever committed.** `.gitignore` covers `.env`, `config.env`, keys, PEM
  files, databases, downloaded data and the model catalogue.
- Secrets are entered with hidden input, stored only in the configuration file (mode 600)
  or a restricted key file, never passed as command-line arguments.
- Tokens and keys are **scrubbed** from logs, errors and messages.
- Telegram commands are accepted only from the configured chat id.
- Backups never contain secrets by default; off-server archives are always encrypted with
  your passphrase.

---

## Testing

```bash
penny selftest      # offline: config, storage, scoring, parsers, AI, fib, messages, backups
penny simulate      # synthetic market: grinder, burst, fading spike, ~60 noise stocks
pytest -q           # unit tests
```

`penny simulate` runs the real engine against a synthetic market with a fake AI and a fake
Telegram and checks the documented acceptance results (no alerts on noise; the grinder
alerts at Early under +60% and Confirmed before +100%; the burst alerts within 8 minutes
and under +50%; enrichment, Fibonacci, plan and market context appear; forecasts are
evaluated and the leaderboard ranks an accurate fake model above a wrong one; a model that
does not beat the baselines is not activated; the cache works; staged elimination runs;
cycle time stays well under 500 ms for ~60 symbols).

---

## Project layout

```
install.sh                 installer (asks for credentials first)
update.sh                  updater with rollback
deploy/penny.service       systemd unit
config.env.example         every setting, no real values
src/penny/
  cli.py                   command line
  runtime.py               builds every component from configuration
  engine.py                poll loop, alert rules, digests, tracking, end of day
  scoring.py               rolling state, Rise/Volx/Accel/VWAP, tiers, score, phase
  sources/                 moomoo, finviz, alpaca, finnhub, sec, finra, yahoo
  ai/                      catalog, gateway, panel, evaluate, leaderboard, runner
  shariah.py               halal.sh + Musaffa, combine rules, modes, cache
  enrich.py                parallel enrichment, technicals, Fibonacci, plan
  alerts.py                Alert 1 / Alert 2 formatting and the AI fact bundle
  telegram.py              Telegram client + offline fake
  journal.py               events, bar download, metrics, rule search
  backup.py                backup, verification, retention, restore, drill
  commands.py              Telegram command handling
  doctor.py                live checker
  selftest.py              offline self-test
  simulation.py            offline simulation with a synthetic market
tests/                     unit tests
docs/RUNBOOK.md            start, stop, update, rollback, change keys, restore
docs/FINDINGS.md           the section 11 items to confirm and their defaults
```

---

## Updates

```bash
bash update.sh              # newest version
bash update.sh v1.1.0       # a specific tag
bash update.sh --rollback   # restore the previous version
```

The updater takes a backup first, extracts the new archive over the install directory
(configuration, databases and bar files are untouched), reinstalls dependencies, runs the
self-test, restarts the service and keeps the previous version for rollback.

---

## Documentation

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — operations: start, stop, update, rollback,
  change any key or token, restore a backup, troubleshooting.
- [`docs/FINDINGS.md`](docs/FINDINGS.md) — the items to confirm during build (section 11)
  and the default adopted for each.

## License

MIT.
