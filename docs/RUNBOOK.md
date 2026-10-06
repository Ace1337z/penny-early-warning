# Runbook

Day-to-day operations for the Penny Stock Early-Warning System. Paths assume the default
install directory `/opt/penny`; substitute yours.

Throughout, `PENNY` means the installed command:

```bash
PENNY=/opt/penny/.venv/bin/penny
CONFIG=/opt/penny/config.env
```

---

## 1. Start, stop, restart, status

```bash
sudo systemctl start penny        # start
sudo systemctl stop penny         # stop
sudo systemctl restart penny      # restart
sudo systemctl status penny       # is it running?
journalctl -u penny -f            # follow the logs
journalctl -u penny --since "1 hour ago" -p warning   # warnings and errors
```

Without systemd:

```bash
$PENNY --config $CONFIG run       # run in the foreground
$PENNY --config $CONFIG run --once   # a single cycle, then exit (smoke test)
```

From Telegram, `/status` shows uptime, session, cycle time, symbols, quotes, memory,
tokens and the last backup.

---

## 2. Check the system

```bash
$PENNY --config $CONFIG selftest   # offline: no network, no credentials
$PENNY --config $CONFIG doctor     # live: every source, Telegram and every AI model
$PENNY --config $CONFIG simulate   # offline synthetic-market acceptance run
```

`doctor` prints `PASS`/`FAIL` per service and a final result. Re-run it after changing any
credential. `doctor --reenter` offers to re-enter the credentials that failed.

---

## 3. Change a key or a token

Every setting lives in `$CONFIG` (mode 600). Most changes apply from the next cycle
without a restart.

**One setting, hidden input for secrets:**

```bash
$PENNY --config $CONFIG set FINVIZ_TOKEN
$PENNY --config $CONFIG set AI_KEY
$PENNY --config $CONFIG set MAX_PRICE 5
```

**Show settings (secrets masked to the first four characters):**

```bash
$PENNY --config $CONFIG config show
$PENNY --config $CONFIG config show FINVIZ_TOKEN
```

**Change several at once / full wizard (keeps existing values on Enter):**

```bash
$PENNY --config $CONFIG setup
```

**By editing the file directly** (the service re-reads it when the modification time
changes):

```bash
nano $CONFIG          # then save; no restart needed for non-structural keys
```

Structural settings that may need a restart: `DATA_PROVIDER`, `TELEGRAM_TOKEN`,
`TELEGRAM_CHAT_ID`.

### Finviz token rotation

The Finviz links never change; only the `auth=` token does. Change it at any time:

```bash
$PENNY --config $CONFIG set FINVIZ_TOKEN
```

The next Finviz request uses the new token. Neither the old nor the new token appears in
logs or messages. If a token is rejected, you get **one** Telegram message and the Finviz
steps pause until the token changes; alerts continue.

### AI models

```bash
$PENNY --config $CONFIG models                 # leaderboard
$PENNY --config $CONFIG models refresh         # re-read the provider's list
$PENNY --config $CONFIG models add ID 1.5      # confirm a new model and its multiplier
$PENNY --config $CONFIG models remove ID
$PENNY --config $CONFIG models apply           # run the automatic selection now
```

Or from Telegram: `/models`, `/models refresh`, `/models add ID MULT`, `/models remove ID`,
`/setmodels A,B,C`. A listed model that is not in your catalogue is **not used** until you
confirm it, which prevents unknown cost.

`/models refresh` also self-heals the shipped example ids: if none of the configured
models exist on your provider, it adopts the provider's own working models and reports
what it changed. `/set AI_MODELS a,b,c` applies from the next cycle without a restart.

---

## 4. Update and roll back

```bash
bash update.sh              # newest version
bash update.sh v1.1.0       # a specific tag
bash update.sh --rollback   # restore the previous version
```

The updater takes a backup first, extracts the new archive over the install directory
(configuration, databases and bar files are untouched), reinstalls dependencies, runs the
self-test, restarts the service and keeps the previous version under `.rollback/`.

If an update fails: `bash update.sh --rollback`, then check `journalctl -u penny -n 100`.

---

## 5. Backups

Automatic: a full backup daily at 20:30 ET, a light backup of the two databases every hour
during sessions, an immediate light backup after any journal change or panel change, and
before every update. Off-server copies are encrypted with `BACKUP_PASSPHRASE`.

```bash
$PENNY --config $CONFIG backup now            # a manual backup
$PENNY --config $CONFIG backup list           # recent backups and destination health
$PENNY --config $CONFIG backup drill          # weekly restore drill (also automatic)
```

From Telegram: `/backup now`, `/backups`.

- Verification after every backup: checksum, decryption test, integrity check, row-count
  comparison with the live database.
- Retention: hourly 48, daily 14, weekly 8, monthly 12. Nothing is pruned until a newer
  backup is verified, and the last 3 verified backups are never pruned.
- If there is no verified backup within `BACKUP_MAX_AGE_HOURS` (26), you get a Telegram
  alert, repeated daily until one succeeds.

**Configure an off-server destination** (strongly recommended; the installer warns when it
is missing):

```bash
$PENNY --config $CONFIG set BACKUP_REMOTE "s3:my-bucket/penny"   # an rclone remote
$PENNY --config $CONFIG set BACKUP_PASSPHRASE                   # hidden input
```

---

## 6. Restore

Restore is **command line only** (it is destructive) and is not available through
Telegram.

```bash
sudo systemctl stop penny
$PENNY --config $CONFIG restore latest --scope full
$PENNY --config $CONFIG restore /path/to/penny-daily-....tar.gz --scope learning
$PENNY --config $CONFIG restore s3:my-bucket/penny/penny-daily-....gpg --scope full
sudo systemctl start penny
$PENNY --config $CONFIG doctor
```

Scopes:

- `full` — the two databases, bar files and tuned settings
- `learning` — journal, forecasts, leaderboard, panel and tuned settings (not bar files)
- `journal` — the journal database only

A **safety snapshot** of whatever exists is taken first (in the local backup directory).
The passphrase must match the one used when the backup was made; without it an encrypted
backup cannot be read.

**On a new server:** run the installer and answer **yes** to "restore the learning state
from an existing backup"; it asks for the backup location and passphrase and restores
before the first run.

---

## 7. Journal and rule tuning

```bash
# Guided entry
$PENNY --config $CONFIG journal add SYMBOL 2026-03-04 winner --pattern burst --peak 180

# Batch import (lines: SYMBOL DATE w|l [pattern] [peak%])
$PENNY --config $CONFIG journal import --file events.txt

$PENNY --config $CONFIG journal list
$PENNY --config $CONFIG journal download SYMBOL 2026-03-04   # bars: event day -3 .. +4
$PENNY --config $CONFIG journal metrics SYMBOL 2026-03-04    # peak, pattern, drawdown...
$PENNY --config $CONFIG journal discover SYMBOL --days 30    # biggest-gain days
$PENNY --config $CONFIG journal rules                        # search the rule grid
```

Rule results are labelled `PRELIMINARY` until there are 30 winners and 100 losers,
`MODERATE` until 100 winners and 300 losers, then `FIRM`. Adjust the tier thresholds in
`$CONFIG` and re-run `journal rules`.

---

## 8. Troubleshooting

| Symptom | Check / fix |
|---|---|
| No alerts during a session | `$PENNY doctor`; is `DATA_PROVIDER` reachable? Is the session open? Check `/status` and the log for feed failures |
| "Feed failing: 5 consecutive cycles returned no quotes" | check outbound HTTPS and the Finviz token; the universe screener *is* the quote feed for sub-$10 names, so a rejected token means no feed |
| "Finviz token rejected" | `$PENNY set FINVIZ_TOKEN` or `/set FINVIZ_TOKEN <token>` from the bot; alerts continue meanwhile |
| Finviz rate-limited (`429`) | lower `FINVIZ_MAX_PER_MIN` (default 20) and `UNIVERSE_POLL_SECONDS`; the client caches and back-fills |
| Telegram commands ignored | the chat id must match `TELEGRAM_CHAT_ID`; re-run `$PENNY detect-chat` |
| No AI text on alerts | `$PENNY doctor`; check `AI_BASE_URL`/`AI_KEY`; the usual cause is `AI_MODELS` still holding the shipped example ids, which your provider does not offer |
| Example model ids still configured | `/models refresh` detects it, adopts the provider's working models and reports them; or set them: `/set AI_MODELS a,b,c` (no restart needed) |
| Provider cannot list models | that is normal for many OpenAI-compatible gateways; set ids by hand: `/set AI_MODELS a,b,c` or `$PENNY models add ID MULT` |
| `/set AI_MODELS` had no effect | fixed: `/set` now updates the stored panel too. On an older build, restart the service |
| Provider returns `content: null` | reasoning models put the answer in `reasoning_content`; the gateway reads that. An empty completion is now a reported error |
| "BACKUP OVERDUE" | `$PENNY backup now`; check `BACKUP_REMOTE` and the passphrase |
| High memory | lower `WORKERS`; check `sizing.log` and the `cycles` table |
| Service keeps restarting | `journalctl -u penny -n 100`; run `$PENNY selftest` |

Useful log greps:

```bash
journalctl -u penny | grep -i "finviz\|alpaca\|halalterminal\|backup\|alert"
```

---

## 9. Housekeeping

- Retention: sizing records and AI call logs older than `RETENTION_DAYS` (365) are
  deleted; bar files are kept; the sizing log rotates weekly.
- Time sync is mandatory — keep chrony running.
- The configuration file is mode 600; keep it that way. Never commit it.
- Periodically run `backup drill` and, after any change, `doctor`.
