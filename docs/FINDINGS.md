# Findings — items to confirm during build (documentation section 11)

Each item below has a default adopted in the code when it could not be resolved offline.
Anything marked **TO CONFIRM** depends on live accounts (Moomoo, Finviz, the AI gateway,
Shariah services) and should be verified on the VPS with `penny doctor` and the acceptance
tests in section 10. Nothing here is a secret.

Legend: **DEFAULT ADOPTED** — the code follows the documented default; **TO CONFIRM** —
needs a live check.

---

## C1 — Moomoo US quote access and auth method
- **Question:** does the account have US quote access through the API (including `pre_*`
  and `after_*` fields for small caps), and which auth method works headless?
- **Default adopted:** API-key authentication (AppKey + uploaded public key), Ed25519 by
  default with RSA-SHA256 selectable via `MOOMOO_ALGO`. Snapshot parsing reads `last_price`,
  `prev_close_price`, `volume`, the `pre_*` and `after_*` sets, `bid_price`/`ask_price`,
  `total_market_val`, `outstanding_shares`, `sec_status`/`suspension`.
- **Status:** **TO CONFIRM.** Stop and escalate if the account has no US quote access.
- **Where:** `src/penny/sources/moomoo.py` (signing, `snapshot`, `_normalise`).

## C2 — Reproducing the app's pre/post Top Gainers
- **Question:** which call reproduces the app's pre/post-market Top Gainers for all US
  stocks — `stock-screen` with a pre/post change factor, or `plate-stock` on a US
  all-stocks plate?
- **Default adopted:** `plate-stock` with `plate_code` from `MOVERS_PLATE` (default `US`)
  and `sort_field` chosen by session (`PRE_CHANGE_RATE` / `CHANGE_RATE` /
  `AFTER_CHANGE_RATE`). Until parity is confirmed, the **L2 universe sweep still runs**, so
  movers are never the only source.
- **Status:** **TO CONFIRM** (acceptance test 5, mover parity).

## C3 — Does snapshot `volume` already include extended hours?
- **Question:** is `volume` the full-day cumulative volume including pre/post?
- **Default adopted:** treat `pre_volume + volume + after_volume` as the cumulative day
  volume when the session fields are present, so Volx and dollar volume are consistent
  across sessions.
- **Status:** **TO CONFIRM** against a live snapshot at 09:00 and 17:00 ET.

## C4 — Safe request rate and latency from the VPS
- **Question:** what request rate is safe, and what latency is achievable?
- **Default adopted:** ≤ 1 snapshot call per second; movers polled every `MOVERS_POLL_SECONDS`
  (20 s); universe swept every `UNIVERSE_POLL_SECONDS` (30 s) during sessions, hourly when
  closed. Batches of up to `UNIVERSE_BATCH` (400) codes per snapshot call. HTTP 429 waits
  `Retry-After` or `2^n` seconds.
- **Status:** **TO CONFIRM** on the VPS (acceptance tests 1, 9).

## C5 — WebSocket push
- **Question:** authentication and subscription quota for WebSocket push.
- **Default adopted:** **REST polling only** in v1.
- **Status:** not implemented by design.

## C6 — Finviz Elite filter codes, columns, links and limits
- **Question:** valid filter codes for price under $10 and common stocks only; columns/view
  with Avg Volume, Rel Volume, Float, Short Float, Market Cap, Change, Price; multi-ticker
  `t=`; extended-hours prices; news; insiders; groups/calendar/latest-filings columns;
  request limits.
- **Default adopted:** filters `sh_price_u10,ind_stocksonly` and view `v=111`
  (`FINVIZ_FILTERS`, `FINVIZ_VIEW`); the fixed links in section 5.2 with the token read
  **per request** from `FINVIZ_TOKEN`; HTML or HTTP 401/403 means the token is invalid.
  If average volume is missing, the baseline falls back to 300 shares/min. Unsupported
  elements are skipped and listed under "missing". Finviz calls are capped by
  `FINVIZ_MAX_PER_MIN` (20) and cached.
- **Status:** **TO CONFIRM** (acceptance tests 2, 6, 7).
- **Where:** `src/penny/sources/finviz.py`.

## C7 — Moomoo History K-Line parameters and coverage
- **Question:** parameters, limits and 1-minute extended-hours coverage.
- **Default adopted:** History K-Line with `extended_time=1`; fall back to **Alpaca**, then
  **Yahoo**. If none return data, no technicals/plan and the message says so.
- **Status:** **TO CONFIRM.**

## C8 — Moomoo Short Interest / Daily Short Volume fields and coverage
- **Question:** fields, freshness and small-cap coverage.
- **Default adopted:** use the Moomoo short endpoints when they answer, otherwise the
  **FINRA** daily short-volume file and the Finviz short-float figure.
- **Status:** **TO CONFIRM.**

## C9 — AI gateway base URL, model ids, rate limits, JSON mode
- **Question:** exact base URL, model ids, per-model limits, JSON-mode support.
- **Default adopted:** tolerant JSON parsing (code fences stripped, unknown enums mapped to
  `unclear`, bad prices dropped), 3 parallel panel calls plus shadow calls, `AI_TIMEOUT`
  (45 s). The panel survives a failing model. Model ids come from the catalogue (Appendix A)
  and the live list from `GET {AI_BASE_URL}/v1/models`.
- **Status:** **TO CONFIRM.**

## C10 — Early-close days and holidays
- **Question:** early closes and the holiday calendar.
- **Default adopted:** a built-in holiday list for the current year (fixed holidays with
  Saturday→Friday and Sunday→Monday observance, plus MLK, Presidents', Memorial, Labor,
  Thanksgiving). **Early closes are treated as normal days** in v1.
- **Status:** built-in; confirm the list for the year in use.
- **Where:** `us_market_holidays()` in `src/penny/util.py`.

## C11 — FINRA file timing and column order
- **Question:** publication timing and column order.
- **Default adopted:** the documented daily short-volume file; the adapter parses by header
  name and fails soft (listed under "missing") if the shape changes.
- **Status:** **TO CONFIRM.**
- **Where:** `src/penny/sources/finra.py`.

## C12 — Moomoo snapshot for SPY/QQQ/IWM
- **Question:** are `US.SPY`, `US.QQQ`, `US.IWM` available in the snapshot?
- **Default adopted:** try Moomoo first; fall back to **Yahoo** index proxies
  (`^GSPC`, `^IXIC`, `^VIX`) for the market context.
- **Status:** **TO CONFIRM.**

## C13 — halal.sh API
- **Question:** endpoints, fields, rate limits, the plan needed, and terms for personal
  automation.
- **Default adopted:** halal.sh used when `HALALSH_API_KEY` is set; **Musaffa** alone when
  halal.sh is absent; `UNKNOWN` when neither returns a result.
- **Status:** **TO CONFIRM** (including the terms of use).

## C14 — The second Shariah source
- **Question:** the request named "mustafa.com" (a site that refuses automated access); the
  halal-screening service found is **Musaffa (musaffa.com)** — confirm which is meant.
- **Default adopted:** **Musaffa** (Screening API). `MUSAFFA_API_KEY` + `MUSAFFA_BASE_URL`.
- **Status:** **TO CONFIRM.**

## C15 — Coverage and "not screened"
- **Question:** how many sub-$10 / newly listed stocks each source screens; how each reports
  "not screened".
- **Default adopted:** `UNKNOWN` when nothing is returned; the combined status follows the
  6.17 rules (agreement → COMPLIANT; any NON-COMPLIANT wins; otherwise DOUBTFUL; nothing →
  UNKNOWN).
- **Status:** **TO CONFIRM.**

## C16 — AI gateway model list and the multiplier meaning
- **Question:** does `GET {AI_BASE_URL}/v1/models` list models? What do the colour groups
  mean? What does the multiplier measure?
- **Default adopted:** treat the multiplier as a **usage-cost factor**; exclude routing
  aliases such as `auto`; exclude models above `AI_MAX_MULTIPLIER` (4) unless listed in
  `AI_PREMIUM_MODELS`. If the list cannot be read, tell the user to enter models manually
  (`/models set id:multiplier,...`). Colour groups are recorded as notes only.
- **Status:** **TO CONFIRM.**

## C17 — Off-server backup destination and Telegram document size
- **Question:** the off-server destination and its credentials; does the critical archive
  fit Telegram's ~50 MB document limit?
- **Default adopted:** local backup always; off-server optional but strongly recommended
  (the installer and `doctor` warn when missing); the Telegram document backup is skipped
  with a warning when the archive exceeds ~45 MB.
- **Status:** **TO CONFIRM** (set `BACKUP_REMOTE` and `BACKUP_PASSPHRASE`).

## C18 — Encryption tool on the VPS
- **Question:** is `age` or GnuPG available?
- **Default adopted:** **GnuPG symmetric** (AES-256) when `gpg` is present; otherwise an
  internal AES-GCM scheme with a PBKDF2-HMAC-SHA256 key (300,000 iterations) and a
  `PENNYBKP1` header. Either way the archive cannot be read without the passphrase.
- **Status:** implemented with fallback; confirm `gpg` on the VPS.
- **Where:** `src/penny/backup.py`.

---

## Acceptance test results

Run on the development machine (Python 3.12, Windows) with `penny selftest`,
`penny simulate` and `pytest`.

### Offline self-test (`penny selftest`) — ALL PASS
Configuration defaults and round-trip; owner-only permissions; state and journal schemas
open; a rising stock reaches Confirmed; score stays 0–100; price above VWAP detected; quiet
noise does not alert; a fading spike is tagged FADING; session windows; the holiday list;
CSV/HTML parsing and number formats; the AI parser (valid object, code fences, HTML
rejection, unknown enums, bad prices); aggregation majorities/medians; the prompt character
cap; Fibonacci levels, extensions and trade plans; Alert 1 shape and Alert 2 length and
disclaimer; the model catalogue (load, persist, pool excludes aliases and over-limit
models); the composite formula and the Wilson interval; the Shariah combine rules; secret
scrubbing; and the backup round-trip (create, verify, encrypted, no plaintext secrets,
restore drill, restore, wrong passphrase rejected).

### Offline simulation (`penny simulate`) — ALL PASS
Synthetic market: a grinder, a burst, a spike that fades and 60 noise stocks, with
synthetic index data, a fake AI and a fake Telegram. Verified results: no alerts on noise
stocks; the grinder alerts at Early build under +60% and reaches Confirmed before +100%;
the burst alerts within 8 minutes of starting and under +50%; every alert receives
enrichment and a forecast (Alert 2 sent); Fibonacci levels and a plan appear; market context
appears in the AI input; a forced price mismatch produces the DATA MISMATCH line; forecasts
are stored for all candidates and evaluated at their horizons against the simulated prices;
the leaderboard ranks the deliberately accurate fake model above the deliberately wrong one;
a model that does not beat the naive baselines is not activated; automatic selection returns
a three-model panel or keeps the current one; the cache reuses an analysis within the cache
window; the AI panel survives a failing model; parsers reject HTML and missing columns;
staged elimination runs; tracker events fire; cycle time averages ~7 ms for ~63 symbols
(well under 500 ms); and the Shariah combine rules behave as specified.

### Not yet run (need live accounts / a VPS)
Acceptance tests 2 (token change), 3 (installer on a fresh VPS), 4 (`doctor` all PASS),
5 (mover parity), 6 (replay), 8 (failure drills), 9 (5-day soak), 11 (full off-server
backup/restore across servers), 12 (model catalogue against the real provider). Run these
on the VPS once the credentials are in place.
