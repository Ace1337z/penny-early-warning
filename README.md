# Penny Stock Early-Warning System

**Catches sub-$10 US stocks while a move is still *building*, not after it has already run.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-100%20passing-brightgreen)](#testing)
[![Self-test](https://img.shields.io/badge/offline%20self--test-passing-brightgreen)](#testing)
[![Platform](https://img.shields.io/badge/platform-linux-lightgrey?logo=linux&logoColor=white)](#requirements)
[![Not financial advice](https://img.shields.io/badge/%E2%9A%A0-not%20financial%20advice-red)](#disclaimer)

A server-side system that watches **every US stock priced under $10** across pre-market,
regular and post-market sessions, and alerts you on Telegram the moment a stock starts
building a move on rising volume - early enough to enter manually.

Market-mover screeners rank by **% change**, so a stock only appears after it has already
moved, and you can realistically watch only the top 5-10 names. Slow "grinders"
(+20% early, then a steady climb to +200%) never stand out; fast "bursts" (+70% in 20
minutes) are seen too late. This system watches the whole universe continuously and scores
momentum **quality and acceleration** rather than the % level alone.

---

## Table of contents

- [Example alert](#example-alert)
- [How it works](#how-it-works)
- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration](#configuration)
- [Telegram commands](#telegram-commands)
- [Command line](#command-line)
- [Testing](#testing)
- [Security](#security)
- [Project layout](#project-layout)
- [Updating and rollback](#updating-and-rollback)
- [Documentation](#documentation)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Example alert

An **Alert 1** is sent instantly, with no AI and no waiting for enrichment:

```text
ALERT: EARLY BUILD  ABCD  $1.42
+31.5% vs close | +6.8% 15m | +24.1% 60m
vol 12.4x normal | day 8.9M | above VWAP
low 38m ago | score 74 | #1 gainer
Shariah: COMPLIANT (halalterminal)
```

A few seconds later an **Alert 2** follows with the facts first, then the AI blocks
(which are edited in as soon as the models answer):

```text
DETAIL EARLY BUILD ABCD $1.42 +31.5% vs close

market: SPY +0.42%, QQQ +0.61%, IWM +0.28% | regime risk-on
Finviz feed verified
NEWS (3 items from 2 outlet(s)):
- ABCD announces pricing of $12.0 million registered direct offering (2h) - Benzinga
FILINGS (last 5 days):
- 8-K 2026-10-05 items 1.01,9.01
SHORT/FLOAT: short float 8.4% (finviz 2026-10-05) | float 18,500,000
Shariah: COMPLIANT (halalterminal)
TECH: 15m up RSI 71 | 5m up RSI 68 | 1m up RSI 74 | VWAP 1.2900 | vol spike 11.2x
FIB swing 1.0500 -> 1.4800
  retrace 23.6% 1.3785 | 38.2% 1.3157 | 50% 1.2650 | 61.8% 1.2143 | 78.6% 1.1420
  extensions 127.2% 1.5972 | 161.8% 1.7457
PLAN: BREAKOUT plan | entry 1.4200-1.4600 | stop 1.3157 | T1 1.5972 | T2 1.7457
      | R:R 2.4:1 | 172 shares for $50 risk
REACTION: strong/up/grinder/sustained | confidence 0.71 | agreement 0.83 across 3 model(s)
catalyst: $12M offering priced, volume 12x
reasons: offering removes funding overhang; float still tight; above VWAP all session
PRICE FORECAST: +15m 1.4700 (+3.5%) | +60m 1.5900 (+12.0%) | session end 1.7800 (+25.4%)
expected peak 1.8200 | expected low 1.3600

High-risk penny stock. Automated screening and forecasts are estimates, not financial advice.
```

The system **places no orders** - it tells you, you trade manually.

Both alerts arrive with **inline buttons** (Re-check, Halal, Market, Menu) so
you can act on a symbol without typing anything.

---

## How it works

```text
 Finviz Elite ------> the single market feed: universe rows are the whole
                     sub-$10 quote feed, plus market movers (top gainers,
                     new high, unusual volume, overbought, oversold,
                     most active, insider buying)
                            |
                            v
                    quote normalization
                            v
            scoring engine (rolling windows, tiers, score)
                            | tier rises
                            v
        ALERT 1 (Telegram, instant, no AI)
                            v
   parallel enrichment: Finviz verification - candles (Alpaca/Yahoo) - news -
   filings (SEC, Finviz) - short interest/volume - market context
                            v
            technicals + Fibonacci + trade plan
                            v
        ALERT 2 (facts first, "AI running")
                            v
   AI active panel ---> reaction + price forecast ---> ALERT 2 edited
   other candidates run in shadow ---> forecasts stored
                            v
   forecast evaluator (+15m, +60m, session end) ---> model leaderboard
                            v
            active panel = the three most accurate models
```

The engine ranks stocks by **momentum quality**, not the % level:

| Signal | What it measures |
|---|---|
| **Rise** | price change over 5 / 15 / 60 / 240 minute windows |
| **Volx** | volume in the window vs the stock's normal rate |
| **Accel** | is the move speeding up or flattening? |
| **VWAP** | is price holding above the session's volume-weighted average? |
| **Tier** | WATCH -> EARLY BUILD -> CONFIRMED, with a 0-100 score and a phase (BUILDING / EXTENDED / FADING) |

The AI panel does not just answer once. Every candidate is forecast, and each forecast is
**evaluated at its horizon** against what actually happened. The three models with the best
track record form the active panel; models that fail to beat simple baselines are not
activated.

---

## Features

- **Whole-universe watching** - market-mover screens (top gainers, new highs, unusual
  volume, overbought, oversold, most active, insider buying) plus a full sweep of sub-$10
  US stocks, in all three sessions, so nothing depends on a single screener's ranking.
- **Two-stage alerts** - instant Alert 1, then a fact-rich Alert 2 enriched in parallel.
- **AI panel with a leaderboard** - parallel model calls, shadow candidates, stored
  forecasts, horizon evaluation, automatic selection of the best three, and a cost
  multiplier so unknown models are never used before you confirm them.
- **Catalyst research** - news, SEC filings, short interest/volume, float, insiders.
- **Fibonacci entry plan** - retracements, extensions, stop, targets and position sizing.
- **Shariah screening** - halalterminal.com, cached, shown on every alert, with
  `tag` / `only_compliant` / `hide_noncompliant` / `off` modes.
- **Self-learning journal** - log historical winners and losers, download their bars,
  compute metrics and search rule thresholds, with trust levels
  (`PRELIMINARY` -> `MODERATE` -> `FIRM`).
- **Backups that survive a dead server** - scheduled local + off-server encrypted
  snapshots, verification, retention and a weekly restore drill.
- **Telegram + CLI** - alerts, commands, digests and a full command line.
- **Runs offline for testing** - a synthetic market, a fake AI and a fake Telegram, so the
  whole pipeline is exercised with no network and no credentials.

---

## Requirements

- A Linux VPS (Ubuntu 24.04 LTS tested), 2 vCPU / 2 GB RAM / 25 GB SSD, public IPv4,
  outbound HTTPS.
- Python 3.10+ (3.12 recommended).
- Accounts and keys. **Only Telegram is required at install time**; every other key can be
  added later from the chat with `/set KEY VALUE` (see `/keys`).

| Credential | Required | Used for |
|---|---|---|
| Telegram bot token + chat id | yes (to reach the bot) | alerts and commands |
| Finviz Elite token | yes | the entire market feed: universe, movers, quotes, news, filings, sector, calendar |
| AI gateway base URL, key, model ids | recommended | reaction and price forecasts (any OpenAI-compatible endpoint) |
| SEC contact (`Name email`) | recommended | SEC EDGAR requests |
| halalterminal.com API key | optional | Shariah screening |
| Alpaca / Finnhub | optional | candle fallback, extra news |
| Backup passphrase + destination | optional | encrypted off-server backups |

---

## Install

Create an **empty** repo, then run the installer **directly** (not piped into `bash`, so it
can read your answers). It asks for every credential *first*, before anything is
downloaded, and it is idempotent - running it again keeps existing values (shown masked;
pressing Enter keeps them).

```bash
wget -O install.sh https://raw.githubusercontent.com/Ace1337z/penny-early-warning/main/install.sh
bash install.sh
```

`wget -O install.sh` forces the exact filename. Without `-O`, a second download is saved
as `install.sh.1`, `install.sh.2`, ... and `bash install.sh` would keep running the stale
first copy.

The installer installs system packages, downloads the source, creates a virtual
environment, writes `config.env` (mode 600), detects the Telegram chat id, installs the
systemd unit, runs the offline self-test and the live `doctor`, and offers to start the
service. Only the **Telegram bot token and chat id** are required at install time - every
other credential (Finviz, Alpaca, Finnhub, SEC, halalterminal, AI, backup) is optional and
can be added later straight from the Telegram bot with `/set`.

**Non-interactive:**

```bash
PENNY_NONINTERACTIVE=1 \
TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... \
bash install.sh
```

Add the optional keys later (from the configured Telegram chat):

```text
/set FINVIZ_TOKEN <token>
/setkey AI_KEY <key>
/set AI_BASE_URL https://your-gateway/v1
/keys
```

**Manual install:**

```bash
git clone https://github.com/Ace1337z/penny-early-warning.git
cd penny-early-warning
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env && chmod 600 config.env
.venv/bin/penny setup        # wizard: Telegram first, other keys optional
.venv/bin/penny doctor       # live checker: PASS/FAIL per service
.venv/bin/penny selftest     # offline self-test
.venv/bin/penny simulate     # offline simulation (no network, no credentials)
```

---

## Configuration

Every setting lives in one env-style file (mode 600), re-read whenever its modification
time changes - most changes apply from the next cycle without a restart. See
[`config.env.example`](config.env.example) for the full list with defaults.

| Group | Highlights |
|---|---|
| **Feed** | `DATA_PROVIDER` (Finviz), mover signals/filters, poll intervals, extra symbols |
| **Finviz** | token, base URL, view, filters, columns |
| **Other sources** | Alpaca, Finnhub, SEC contact |
| **Telegram** | bot token and chat id |
| **Shariah** | keys, `SHARIAH_MODE`, TTL, monthly call limit |
| **Backup** | local dir, off-server destination, passphrase, schedule, retention |
| **AI** | gateway URL/key, panel, fallbacks, cache, shadow sample rate, discovery, multiplier limit |
| **Behaviour** | price range, alert tiers, re-alert window, warm-up, workers, risk size |
| **Tuning** | tier thresholds and score weights |

Change one setting (secrets use hidden input):

```bash
penny set FINVIZ_TOKEN          # rotate the Finviz token with no restart
penny set MAX_PRICE 5
penny config show               # list settings, secrets masked to 4 chars
```

Structural settings that may need a restart: `DATA_PROVIDER`, `TELEGRAM_TOKEN`,
`TELEGRAM_CHAT_ID`.

---

## Telegram commands

Accepted **only from the configured chat id**.

Send `/start` to get a persistent button keyboard, then use the inline
buttons under every reply to drill in without typing.

```text
/start                   welcome + persistent button keyboard
/top                     champions and hidden gems (one tap per symbol)
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
/set KEY VALUE           change any setting at runtime (alias /setkey)
/keys                    list settable keys (secrets masked)
/halal SYM               Shariah status now
/backup now
/backups                 recent backups and destination health
/status
/help
```

**Interactive replies.** Every alert and command reply carries inline
buttons, so a symbol is always one tap away:

- Alerts and `/check` - **Re-check**, **Halal**, **Market**, **Menu**.
- `/top` - one button per ranked symbol plus **Refresh**.
- `/market`, `/status`, `/models` - **Refresh**, **Market**, **Menu**.
- Tapping a button edits the message in place instead of posting a new one.

Messages are HTML-formatted (bold headers, monospace symbols, direction and
score meters). Alert bodies stay plain text so nothing needs escaping.

---

## Command line

| Command | Purpose |
|---|---|
| `penny setup` | interactive wizard; asks for every key first, then runs the self-test |
| `penny set KEY [VALUE]` | change one setting (hidden input for secrets) |
| `penny config show [KEY]` | list settings with secrets masked |
| `penny run` | run the service (engine + Telegram command poller) |
| `penny doctor` | live checker: every source, Telegram and every AI model, PASS/FAIL |
| `penny selftest` | offline self-test (no network, no credentials) |
| `penny simulate` | offline simulation with a synthetic market |
| `penny status` | uptime, session, cycle, tokens, backups |
| `penny models` | model leaderboard; `add` / `remove` / `enable` / `disable` / `apply` / `refresh` |
| `penny journal ...` | add/import/list events, download bars, metrics, rule search, discover |
| `penny backup now\|list\|drill` | create, list and verify backups |
| `penny restore SOURCE --scope full\|learning\|journal` | restore a backup (destructive) |
| `penny update` / `penny rollback` | install a new version / restore the previous one |
| `penny detect-chat` | read the Telegram chat id from the next message to the bot |

`--config <path>` selects a configuration file (default `$PENNY_HOME/config.env`).

---

## Testing

```bash
penny selftest      # offline: config, storage, scoring, parsers, AI, fib, messages, backups
penny simulate      # synthetic market: grinder, burst, fading spike, ~60 noise stocks
pytest -q           # unit tests
```

`penny simulate` runs the **real engine** against a synthetic market with a fake AI and a
fake Telegram, and asserts the documented acceptance results:

- no alerts on the ~60 noise stocks
- the grinder alerts at Early build under +60%, and reaches Confirmed before +100%
- the burst alerts within 8 minutes of starting and under +50%
- the fading spike is tagged `FADING` and does not need an alert
- every alert gets enrichment, a Fibonacci plan and market context in the AI input
- forecasts are stored for all candidates and evaluated at their horizons
- the leaderboard ranks the deliberately accurate fake model above the wrong one
- a model that does not beat the baselines is not activated
- the cache works, staged elimination runs, and cycle time stays well under 500 ms

Current status: `selftest` **ALL PASS**, `simulate` **ALL PASS**, `pytest` **100 passed**.

---

## Security

- **No secret is ever committed.** `.gitignore` covers `.env`, `config.env`, keys, PEM
  files, databases, downloaded data and the model catalogue.
- Secrets are entered with hidden input, stored only in the configuration file (mode 600)
  or a restricted key file, and are **never** passed as command-line arguments.
- Tokens and keys are **scrubbed** from logs, errors and messages.
- Telegram commands are accepted only from the configured chat id.
- Backups never contain secrets by default; off-server archives are always encrypted with
  your passphrase (GnuPG symmetric, or an internal AES-GCM fallback).

---

## Project layout

```text
install.sh                 installer (asks for credentials first)
update.sh                  updater with rollback
deploy/penny.service       systemd unit
config.env.example         every setting, no real values
src/penny/
  cli.py                   command line
  runtime.py               builds every component from configuration
  engine.py                poll loop, alert rules, digests, tracking, end of day
  scoring.py               rolling state, Rise/Volx/Accel/VWAP, tiers, score, phase
  sources/                 finviz, alpaca, finnhub, sec, finra, yahoo
  ai/                      catalog, gateway, panel, evaluate, leaderboard, runner
  shariah.py               halalterminal.com, screening rules, modes, cache
  enrich.py                parallel enrichment, technicals, Fibonacci, plan
  alerts.py                Alert 1 / Alert 2 formatting and the AI fact bundle
  telegram.py              Telegram client + offline fake
  ui.py                    HTML escaping, inline/reply keyboards, callback codes
  journal.py               events, bar download, metrics, rule search
  backup.py                backup, verification, retention, restore, drill
  commands.py              Telegram command handling
  doctor.py                live checker
  selftest.py              offline self-test
  simulation.py            offline simulation with a synthetic market
tests/                     unit tests
docs/RUNBOOK.md            start, stop, update, rollback, change keys, restore
docs/FINDINGS.md           the items to confirm during build and their defaults
```

---

## Updating and rollback

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

- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) - operations: start, stop, update, rollback, change
  any key or token, restore a backup, troubleshooting.
- [`docs/FINDINGS.md`](docs/FINDINGS.md) - the items to confirm during build and the
  default adopted for each.

---

## Disclaimer

This software is a screening and estimation tool. Penny stocks are extremely volatile and
easy to manipulate, and every automated signal here can be wrong. Nothing it produces is
financial advice, and the Shariah status is not a religious ruling - consult a qualified
scholar. The system places no orders and never touches your brokerage account. Use it at
your own risk and only with money you can afford to lose.

## License

MIT. See [LICENSE](LICENSE).
