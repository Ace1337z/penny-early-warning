# Penny Stock Early-Warning System

**Catches sub-$10 US stocks while a move is still *building*, not after it has already run.**

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-422%20passing-brightgreen)](#testing)
[![Self-test](https://img.shields.io/badge/offline%20self--test-passing-brightgreen)](#testing)
[![Platform](https://img.shields.io/badge/platform-linux-lightgrey?logo=linux&logoColor=white)](#requirements)
[![Not financial advice](https://img.shields.io/badge/%E2%9A%A0-not%20financial%20advice-red)](#disclaimer)

A server-side system that watches **every US stock priced under $10** during the
pre-market, regular and post-market sessions (04:00-20:00 ET; Finviz carries no
overnight prints), and alerts you on Telegram the moment a stock starts
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
ALERT: EARLY  ABCD  $1.42
bid $1.41 / ask $1.43 (spread 1.4%) | 15m $1,100,000
+31.5% vs close | +1.2% 3m | +3.5% 5m
vol 12.4x normal | day 8.9M | above VWAP
low 38m ago | score 74 | #1 gainer
STRATEGY: first_green_day (78) + low_float_squeeze (61)
Shariah: COMPLIANT (halalterminal)
```

Line two is the **book**: bid, ask, the spread and the 15-minute dollar volume - the
cost of getting in and out, before any analysis. A feed with no bid/ask says
`book n/a` rather than printing a spread of zero.

A few seconds later an **Alert 2** leads with *why the stock moved*, then the
evidence. The decision block is edited in as soon as the model answers:

```text
DETAIL  🟢 ABCD  $1.42  +31.5% vs close
PULSE: above VWAP
SETUP: EARLY - qualifies for alerts

── WHY IT MOVED ────
CAUSE: ABCD announces pricing of $12.0 million registered direct offering
SOURCE: Benzinga, 2h ago
CONFIDENCE: high
SUPPORTING: 8-K 2026-10-05 items 1.01, 9.01

── STRATEGIES ──────
STRATEGY: first_green_day (78) + low_float_squeeze (61)
ENTRY TRIGGER: first_pullback (74)

── MARKET BACKDROP ─
MARKET SPY +0.42%, QQQ +0.61%, IWM +0.28% | RISK-ON

── EVIDENCE ────────
Finviz feed verified
MORE NEWS (2 more from 2 outlet(s))
- ...
FILINGS (last 5 days)
- 8-K 2026-10-05 items 1.01,9.01
SHORT/FLOAT: short float 8.4% (finviz 2026-10-05) | float 18,500,000
Shariah: COMPLIANT (halalterminal)

── AI DECISION ─────
VERDICT: ENTER
RATING: MODERATE | confidence 0.62
KEY FINDINGS:
  - reclaimed VWAP on 9x volume and held it for 12 minutes
  - float still tight at 18.5M with the ATM removed
ENTRY POINTS (LONG)
  1.4200-1.4600   first pullback that holds VWAP
EXIT POINTS
  stop 1.3157 | T1 1.5972 | T2 1.7457
APPLIED: catalyst + volume; float < 20M

Screener and decision support only. No orders are placed. Not investment advice.
```

Reading it fast:

- **WHY IT MOVED** names exactly **one** cause, with its outlet, age and
  confidence - or says `none found` rather than inventing one.
- **AI DECISION** is the only place the verdict appears: `ENTER` / `WAIT` /
  `PASS`, with the entry and exit levels that justify it.
- **STRATEGY** names the detector setups that passed, with the entry trigger
  when one has not fired yet.
- **SETUP** tells you why this is an alert - or, when the tier is `NONE`, exactly
  which rule is not met yet (e.g. *"volume 1.6x is under 3x"*).
- **MARKET BACKDROP** shows index direction and the risk-on/off regime, because
  small caps follow it.
- Every alert is graded to the close and reported once, after the session, in
  the **end-of-day report** (`/eod`): the day's results as a set, an `ABORTS`
  section for setups the invalidation watcher killed, `BY AI RATING` showing
  what the AI claimed next to what the tape delivered, and a post-mortem line
  for every loser.

The system **places no orders** - it tells you, you trade manually.

Both alerts arrive with **inline buttons** (Re-check, Halal, Menu) so
you can act on a symbol without typing anything.

---

## How it works

```text
 Finviz Elite ------> the single market feed: universe rows are the whole
                     sub-$10 quote feed, plus market movers (top gainers,
                     new high, unusual volume, overbought, oversold,
                     most active, insider buying) and the intraday (1m/5m)
                     top-gainers screens that catch a stock being bought
                     right now, before it reaches the daily list
                            |
                            v
                    quote normalization
                            v
            scoring engine (rolling windows, tiers, score)
                            | ranked
                            v
   LONG-ONLY STRATEGY GATE (18 detectors, cause-first)
                            | gate passes
                            v
        ALERT 1 (Telegram, instant, no AI)
                            v
   parallel enrichment: Finviz verification - candles (TwelveData/Alpaca/Yahoo) - news -
   filings (SEC, Finviz) - short interest/volume - market context
                            v
        ALERT 2 (cause first, "AI unavailable" placeholder)
                            v
      ONE AI decision call ---> DECISION REPORT ---> ALERT 2 edited
                            v
    per-alert tracker (quotes) ---> invalidation watcher ---> one ABORT, ever
                            v
    END OF DAY report, once a day: results, ABORTS, BY AI RATING
    (claimed vs actual), POST-MORTEMS (one line per loser)
                            v
                /stats grades each strategy
```

The engine ranks stocks by **momentum quality**, not the % level:

| Signal | What it measures |
|---|---|
| **Rise** | price change over 5 / 15 / 60 / 240 minute windows |
| **Volx** | volume in the window vs the stock's normal rate |
| **Accel** | is the move speeding up or flattening? |
| **VWAP** | is price holding above the session's volume-weighted average? |
| **Tier** | WATCH -> EARLY BUILD -> CONFIRMED, with a 0-100 score and a phase (BUILDING / EXTENDED / FADING) |

### Reading an alert fast

| Line | What it tells you |
|---|---|
| `WHY IT MOVED` | Exactly one cause (outlet, age, confidence), or `none found` |
| `STRATEGY` | The detector setups that passed, and the entry trigger if one has not fired |
| `AI DECISION` | The one report: `ENTER` / `WAIT` / `PASS`, with the entry and exit levels |
| `SETUP` | Why this is an alert - or, for a `NONE` tier, which threshold is not met |
| `MARKET BACKDROP` | Index direction and risk-on/off, which drives small-cap appetite |

A **`NONE` tier** is not an error. The alert bar is `WATCH` and above; `NONE` means
the message came from a manual `/check`, a resumed halt, or a signal override, and
the `SETUP` line names the rule that is not met yet.

### Watching a build before it alerts

`/builds` (and the periodic feed) shows stocks that are accumulating below the alert
bar. `coiled 96/100` is progress toward the tier-2 gates - not a confidence score -
and each line names exactly what is still missing:

```text
BUILDING NOW - watch, not a buy signal
ABCD   $1.27   +26.7% vs close  [coiled 96/100]
  +26.7% vs close | +3.7% 15m | 9.0x vol | $17.1K 15m | above VWAP
  to alert: volume 9.0x -> 10x
WXYZ   $1.14   +14.2% vs close  [building 74/100]
  +14.2% vs close | +2.1% 15m | 4.0x vol | $6.9K 15m | above VWAP
  to alert: volume 4.0x -> 10x; $6.9K -> $10K
```

Stages are `warming` -> `building` -> `accelerating` -> `coiled` (nearly at the
alert bar). Tune it with `BUILD_FEED`, `BUILD_FEED_SECONDS`, `BUILD_TOP_N`,
`BUILD_MIN_SCORE`, `BUILD_RISE15`, `BUILD_VOLX15`, `BUILD_DOLLAR15` and
`BUILD_MIN_MINUTES`. The periodic Telegram digest is **off by default**
(`BUILD_FEED=0`) because a notification every 15 minutes reads as noise; the same
list is always available on demand with `/builds`.

Every alert is tracked from the alert price to the close and graded once, at the end of
the day. `/eod` prints that day's report; `/stats` ranks the detectors by what they
actually produced.

---

## Features

- **Whole-universe watching** - market-mover screens (top gainers, new highs, unusual
  volume, overbought, oversold, most active, insider buying) plus the **intraday
  top-gainers screens** (1m/3m/5m), which surface a stock that is being bought *right now*,
  plus a full sweep of sub-$10 US stocks, in all three sessions, so nothing depends on a
  single screener's ranking.
- **Long-only, always** - the tool never proposes a short. Exhaustion and distribution
  can only produce `WAIT` / `PASS` or an exit warning. An offering priced below the open
  blocks the gate outright (`DILUTION`); a quote that disagrees with the verified price
  blocks it too (`DATA_CONFLICT`).
- **A strategy gate between the screener and the alert** - 18 named detectors (first
  green day, low-float squeeze, $1 breakout, VWAP reclaim, failed breakout, WORG, day-3
  surge, float rotation, and six entry triggers). A stock has to *match a setup* before
  it becomes an alert, so the alerts that arrive are setups rather than movers. Detector
  failures are shown, so you can see what the gate examined. Off by default
  (`STRATEGY_GATE=0`) while you compare it against the old tier path; when set to 1 it
  vetoes on the verdicts the poll pass can reach (the reality and exhaustion blockers)
  and fails closed - a gate that cannot be evaluated suppresses the alert and `/status`
  counts it, rather than letting it through ungated.
- **One AI decision per alert** - a single call returns one DECISION REPORT: the verdict
  (`ENTER` / `WAIT` / `PASS`), the rating, the findings, and the entry and exit levels.
  No panel, no leaderboard, no agreement score, no persona. A report that claims `ENTER`
  on a gate that did not pass is rejected, not repaired.
- **Cause-first alerts** - every message names exactly one reason the stock moved, with
  its outlet, age and confidence, or says `none found`. A news dump is not a cause.
- **Per-alert tracking with an end-of-day report** - every alerted name is measured from
  the alert price to the close, and the day's alerts are reported once, as a set
  (`/eod`). `/stats` ranks the detectors by what they actually produced.
- **A reality gate before the alert** - a quote that cannot be filled never becomes an
  alert. A spread over `SPREAD_MAX_PCT` (1.5%) blocks as `WIDE_SPREAD`; thin 15-minute
  dollar volume, an average trade size under `MIN_AVG_TRADE_SIZE` ($200) or fewer than
  `MIN_TRADES_15M` prints blocks as `ILLIQUID`; a feed with no bid/ask is
  `INSUFFICIENT_DATA`, never a pass. The exit is checked too: a $500 exit that needs more
  than `EXIT_LIQ_500_MAX_SEC` (300s) or slips over `EXIT_LIQ_MAX_SLIP_PCT` (1.0%) blocks
  as `ILLIQUID`.
- **One ABORT per alert** - after the alert, six invalidation triggers watch the quotes:
  a 5-minute close under the alert VWAP, a break of the alert candle's low, a halt, a
  spread that stays wide for `INVALIDATION_SPREAD_CONSEC_MIN` minutes, a negative
  catalyst, and exhaustion near the day high on declining volume. The first one to fire
  aborts the setup - status `aborted`, one message (`action: do not enter / exit if
  holding`, never a short) - and the abort appears in the EOD's `ABORTS` section.
- **Confidence that answers to the tape** - once a (rating, strategy) bucket has
  `CALIBRATION_MIN_SAMPLES` (30) closed alerts, the confidence shown is clamped to that
  bucket's realised T1 hit rate plus `CALIBRATION_CLAMP_MARGIN` (0.15). The EOD's
  `BY AI RATING` section puts what was claimed next to what actually happened.
- **Context blocks for the reader and the model** - historical analogs (nearest tracked
  alerts by feature vector, suppressed below `ANALOG_MIN_K` samples), a multi-timeframe
  block for the AI only (daily MA and range, 60-minute trend, weekly float turnover,
  marked "not shown to the user"), and a sector heatmap line that appears only when a
  sector is genuinely hot (median >= `SECTOR_MIN_HOT_PCT`, or `SECTOR_TOP_N` names on
  the movers list).
- **Post-mortems on every loser** - the EOD ends with one line per losing alert naming
  the rubric clause that should have blocked it - or stating that nothing in the rubric
  covered the loss, which is itself the finding. `/postmortem SYM [DATE]` prints the
  full paragraph for one alert (`POSTMORTEM_ENABLED=1`).
- **Catalysts that fade on a schedule** - a cause's weight decays on its class's
  half-life: `HALF_LIFE_HARD` (180m), `HALF_LIFE_MEDIUM` (120m), `HALF_LIFE_SOFT` (30m).
  A dilution never decays - a bad offering does not get better with age - and a cause
  older than 4 hours is never named as the cause at all.
- **Pre-market at the first sign of a move** - in the pre-market session the alert fires
  as soon as the stock clears `PREMARKET_PCT` (default +10%) *with* the volume to back it,
  instead of waiting for the 15-minute window that used to arrive at ~+20%.
- **Alerts on a 3-5 minute burst, not a 15-minute one** - the tier gates measure a
  15-minute rise and a 60-minute move and demand 10x normal 15-minute volume, so Alert 1
  and Alert 2 could not go out until the burst was over - by which time the reader was
  looking at the give-back. A fast lane reads the **3-minute or 5-minute** window
  instead (`FAST_RISE3` / `FAST_RISE5`, either one qualifying is enough, with volume
  checked on whichever window triggered). Replayed against a stock that was flat for
  16 minutes then printed +3% in three minutes on heavy volume, the fast lane alerts at
  **t+18m** and the tier gates **never fire at all**. The 60-minute requirement is
  deliberately absent: a stock that was flat all day and suddenly runs is exactly the
  penny mover worth catching. Guards keep it honest - the volume multiple and dollar
  volume must confirm (a percentage on no trade is a spread), the move must not already
   be spent (`FAST_MAX_FROM_HIGH`), and there is a cumulative volume floor
   (`FAST_MIN_VOLUME`). A fast alert is tagged `[FAST]`, carries a `WHY NOW` block in
   Alert 2, and is never described as "below the alert bar". Set `FAST_ON=0` to turn the
   whole lane off and restore the old 15/60-minute behaviour.
- **Prices that match the clock** - Finviz keeps `Price`/`Change` frozen at the regular
  close after 16:00 ET, so the after-hours columns are used from 16:00 until the next
  open (including the whole weekend), and `session_for()` knows a Saturday is `closed`.
  TwelveData supplies the freshest single-symbol price, volume and 1-minute candles, so
  the gate's detectors and the cause ranking are built from *now* rather than from
  whatever candle set happened to be cached.
- **On-demand checks for any symbol** - `/check SYM` fetches the symbol live (including
  after-hours) rather than reading the last cycle's cache, so it works for NVDA or AAPL
  just as well as for a sub-$10 name.
- **Momentum-build watch feed** - the alert bar is a *confirmed* tier-2 move. Stocks that
  are clearly accumulating (rising volume, price above VWAP) but sit just below that bar
  are surfaced separately, with the exact tier-2 gate still missing, so quiet grinds are
  not invisible until the burst. The periodic notification is off by default; `/builds`
  shows the same list on demand.
- **Two-stage alerts** - instant Alert 1, then a fact-rich Alert 2 enriched in parallel.
- **AI decision support** - one model, one call per alert, on any OpenAI-compatible endpoint. It never places an order.
- **Catalyst research** - news, SEC filings, short interest/volume, float, insiders.
- **Internal entry gate** - reward:risk is checked against `MIN_RR` (default 2:1)
  before a level set may back an `ENTER`, but the levels live inside the decision
  block rather than in a separate plan block, so the alert stays lean.
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
| AI gateway base URL, key, model id | recommended | the one decision call per alert (any OpenAI-compatible endpoint) |
| SEC contact (`Name email`) | recommended | SEC EDGAR requests |
| halalterminal.com API key | optional | Shariah screening |
| TwelveData | recommended | fresh single-symbol price, volume and 1-minute candles |
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

### Using any OpenAI-compatible provider

The decision call talks to any OpenAI-compatible `/chat/completions` endpoint
(the same shape OpenCode's `openai-compatible` provider uses). Base URL, key and
one model id are enough.

```text
/set AI_BASE_URL https://your-provider.example/api/v1
/setkey AI_KEY sk-...
/set AI_MODEL your-model-id
```

`AI_BASE_URL` may be written **with or without** `/v1`, and a pasted
`/chat/completions` suffix is stripped. Both `{base}/v1` and `{base}` are tried.
A missing `GET /models` is normal and never fatal.

If `AI_MODEL` still holds the shipped example id, `penny doctor` says so and
lists what your provider does offer. Set it by hand:

```text
/set AI_MODEL your-model-id
```

Changes apply from the next cycle - no restart.

From the bot, `/model` does both in one command: `/model` shows the running id
(and the provider's list when it can be read), `/model new-id` switches it -
persisted, applied on the next alert, no restart. `/set AI_MODEL new-id` does
the same.

Response handling covers the shapes providers really return: `content` as a
string, `content` as a list of parts, and reasoning models that leave `content`
null while answering in `reasoning_content`. An empty or unusable completion is
reported as an error, not a silent success.

There is exactly one call per alert, bounded by `AI_TIMEOUT` (default 45 s): a
slow endpoint is dropped instead of retried for every body variant, and a
rejected report degrades to `PASS` rather than to an entry. So a slow provider
costs you the decision, never the alert.

```text
/set AI_TIMEOUT 45      # seconds for the one decision call
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
| **Finviz** | token, base URL, view (`152`, the custom view), filters, columns, mover + intraday signals, extended-hours preference |
| **Other sources** | TwelveData (key, base URL, per-minute/per-day budgets), Alpaca, Finnhub, SEC contact |
| **Telegram** | bot token and chat id |
| **Shariah** | keys, `SHARIAH_MODE`, TTL, monthly call limit |
| **Backup** | local dir, off-server destination, passphrase, schedule, retention |
| **AI** | gateway URL, key, model id, timeout, token cap |
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
/builds                  momentum building below the alert bar
/watch SYM ENTRY [STOP] [TARGET]
/unwatch SYM
/watchlist
/check SYM               full analysis on demand
/market                  market context now
/eod                    today's alert results, alert price to close
/eod YYYY-MM-DD          the same report for a past day
/stats                   strategies graded by what they actually produced
/tracked                 today's tracked alerts, still open
/postmortem SYM [DATE]   the full post-mortem for one losing alert (default today)
/model [MODEL-ID]        show the decision model, or switch it (no restart)
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

- Alerts and `/check` - **Re-check**, **Halal**, **Menu** (a single row).
- `/top` and `/builds` - up to six symbols packed three per row, plus **Refresh**.
- `/market`, `/status`, `/eod` - **Refresh**, **Menu**.
- The main menu is two rows of three; **More** opens Status, Keys, Backup and Help.
- Tapping a button edits the message in place instead of posting a new one.

Alerts, `/check` and `/builds` are HTML-formatted (bold headers, monospace
symbols, direction and score meters) with all dynamic text escaped. Lower-risk
notices (feed health, digests) stay plain text.

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
| `penny models` | inspect the configured decision model; `refresh` re-reads the provider's list |
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
- every alert gets a cause-first Alert 2 and is entered in the tracker
- a valid Decision Report passes validation; a missing required field, an `ENTER`
  on a gate that did not pass, an `ENTER` with no rubric, a short entry zone, a
  `SHORT` verdict and HTML in place of JSON are all rejected
- a parabolic move, an offering priced below the open and a quote that disagrees
  with the verified price each block the gate
- the tracker closes every open alert at the close, marks one that stopped being
  quoted `invalid` rather than flat, and reports the day once - with the `ABORTS`,
  `BY AI RATING` and `POST-MORTEMS` sections
- a wide spread blocks as `WIDE_SPREAD`, a thin market or an unfilled $500 exit
  as `ILLIQUID`, and a feed with no book is `INSUFFICIENT_DATA`, never a pass
- a 5-minute close under the alert VWAP invalidates the setup exactly once
- the multi-timeframe block is AI-only and says `unknown` without daily history;
  a hot sector gets a line and a cold sector gets none
- the cache works, staged elimination runs, and cycle time stays well under 500 ms

Current status: `selftest` **ALL PASS** (79 checks), `simulate` **ALL PASS** (49 checks),
`pytest` **422 passed** (1 skipped).

---

## Security

- **No secret is ever committed.** `.gitignore` covers `.env`, `config.env`, keys, PEM
  files, databases and downloaded data.
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
  engine.py                poll loop, alert rules, build feed, digests, tracking, end of day
  scoring.py               rolling state, Rise/Volx/Accel/VWAP, tiers, score, phase, builds
   sources/                 finviz, twelvedata, alpaca, finnhub, sec, finra, yahoo
   strategies/             base, candles, primitives, detectors, news_cause, gate,
                           spread_liquidity (the reality gate)
   context/                postmortem, calibration, analogs, liquidity,
                           multi_timeframe, sector_heatmap (context blocks)
  tracking.py              per-alert tracking, aborts, calibrated confidence, EOD sections
  tracking_invalidation.py the six invalidation triggers (T1-T6)
  ai/                      gateway, decision (the one call), prompts/methodology_v1.md
  shariah.py               halalterminal.com, screening rules, modes, cache
  enrich.py                parallel enrichment: verification, candles, news, filings, float
  alerts.py                Alert 1 / Alert 2 / build feed formatting + the AI fact bundle
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
