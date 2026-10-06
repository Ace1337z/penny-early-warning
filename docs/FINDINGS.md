# Findings — items to confirm during build (documentation section 11)

Each item below has a default adopted in the code when it could not be resolved offline.
Anything marked **TO CONFIRM** depends on live accounts (Finviz Elite, Alpaca/Yahoo, the AI
gateway, halalterminal.com) and should be verified on the VPS with `penny doctor` and the
acceptance tests in section 10. Nothing here is a secret.

Legend: **DEFAULT ADOPTED** — the code follows the documented default; **TO CONFIRM** —
needs a live check.

---

## C1 — Market feed: Finviz universe rows as the quote feed
- **Question:** can the sub-$10 Finviz screener rows serve as *both* the universe and the
  per-cycle quote feed (price, previous close, cumulative volume, open/high/low, market cap,
  float, avg volume, rel volume, sector)?
- **Default adopted:** yes — `FinvizClient.universe()` returns the screened rows and
  `normalize_row()` maps each to the system's quote shape. Finviz has no bid/ask and no
  per-session split price, so `bid`/`ask` stay empty. The previous close is derived from the
  `Change` percentage (`price / (1 + change/100)`), falling back to a `Previous Close`
  column. `DATA_PROVIDER` defaults to `finviz`; there is **no second quote feed**.
- **Status:** **TO CONFIRM** on the VPS (acceptance tests 1, 5).
- **Where:** `src/penny/sources/finviz.py` (`universe`, `snapshot`, `normalize_row`),
  `src/penny/engine.py` (`build_universe`, `_fetch_quotes`).

## C2 — Reproducing the app's pre/post Top Gainers
- **Question:** do the Finviz market-mover signals (`ta_topgainers`, `ta_newhigh`,
  `ta_mostactive`, `ta_unusualvolume`, `ta_overbought`, `ta_oversold`, `it_latestbuys`)
  reproduce the mover coverage the old Moomoo plate call provided, including pre/post?
- **Default adopted:** `FinvizClient.movers()` runs every configured signal in
  `FINVIZ_SIGNALS` (default: top gainers, new high, most active, unusual volume, overbought,
  oversold, insider buying) and de-duplicates by ticker. The **sub-$10 universe sweep still
  runs every cycle**, so movers are never the only source.
- **Status:** **TO CONFIRM** (acceptance test 5, mover parity).

## C3 — Does the Finviz `Volume` column include extended hours?
- **Question:** is the screener's `Volume` the full-day cumulative volume, and does it
  include pre/post? Is it refreshed during extended sessions?
- **Default adopted:** treat `Volume` as the cumulative day volume and use it for Volx and
  dollar volume. Extended-session prices are not split out (Finviz returns one price), so
  the session is recorded from the clock, not from the row.
- **Status:** **TO CONFIRM** against a live screener call at 09:00 and 17:00 ET.

## C4 — Safe request rate and latency from the VPS
- **Question:** what Finviz request rate is safe, and what latency is achievable?
- **Default adopted:** `FINVIZ_MAX_PER_MIN` (20) global rate limit; movers polled every
  `MOVERS_POLL_SECONDS` (20 s) with a `FINVIZ_MOVERS_TTL` (15 s) cache; universe/quote feed
  refreshed every `UNIVERSE_POLL_SECONDS` (30 s) during sessions, hourly when closed. Targeted
  snapshots batch `FINVIZ_QUOTE_BATCH` (40) tickers per `t=` call. HTTP 429 waits
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
- **Default adopted:** filters `sh_price_u10,ind_stocksonly` and the full custom view
  `c=0..66` (`FINVIZ_FILTERS`, `FINVIZ_COLUMNS`) so Avg Volume, Rel Volume, Float, Short
  Float, Market Cap and Sector are present; mover screens add `s=<signal>` from
  `FINVIZ_SIGNALS`. The fixed links in section 5.2 are used with the token read **per
  request** from `FINVIZ_TOKEN`; HTML or HTTP 401/403 means the token is invalid. If average
  volume is missing, the baseline falls back to 300 shares/min. Unsupported elements are
  skipped and listed under "missing". Finviz calls are capped by `FINVIZ_MAX_PER_MIN` (20)
  and cached.
- **Status:** **TO CONFIRM** (acceptance tests 2, 6, 7). The mover `s=` codes and the exact
  column-index mapping are the main things to verify against a live Elite account.
- **Where:** `src/penny/sources/finviz.py` (`SIGNALS`, `screener`, `movers`, `normalize_row`).

## C7 — Candle source (Alpaca, then Yahoo)
- **Question:** parameters, limits and 1-minute extended-hours coverage for the candle
  fallback chain.
- **Default adopted:** Finviz carries no OHLC history, so candles come from **Alpaca**
  (`_candles`) first, then **Yahoo**. If neither returns data, no technicals/plan and the
  message says so.
- **Status:** **TO CONFIRM.**
- **Where:** `src/penny/enrich.py`, `src/penny/sources/alpaca.py`, `src/penny/sources/yahoo.py`.

## C8 — Short interest / daily short volume
- **Question:** fields, freshness and small-cap coverage from the remaining sources.
- **Default adopted:** the **FINRA** daily short-volume file and the Finviz short-float and
  float figures (columns in `FINVIZ_COLUMNS`); there is no broker short endpoint any more.
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

## C12 — Index quotes for SPY/QQQ/IWM
- **Question:** does the Finviz screener/snapshot return SPY/QQQ/IWM so the market context
  and regime can be computed?
- **Default adopted:** try Finviz `snapshot(("SPY","QQQ","IWM"))` first (targeted `t=` call);
  fall back to **Yahoo** index proxies (`^GSPC`, `^IXIC`, `^VIX`) for the market context.
- **Status:** **TO CONFIRM.**
- **Where:** `src/penny/market_context.py`.

## C13 — halalterminal.com API
- **Question:** the exact screen route, request/response fields, rate limits, the plan
  needed, and terms for personal automation.
- **Default adopted:** a single source, `HalalTerminalSource` (`name = "halalterminal"`),
  base `HALALTERMINAL_BASE_URL` (default `https://api.halalterminal.com`), key in the
  `X-API-Key` header from `HALALTERMINAL_API_KEY`. It tries **`POST /api/screen/{SYMBOL}`**
  first and falls back to **`GET /api/screen?symbol=`**. The parser accepts a tolerant set of
  fields (`is_compliant`, `shariah_compliance_status`, `business_screen_pass`,
  `financial_screen_pass`, `purification_rate`, `compliance_explanation`) and maps unknown
  shapes to `UNKNOWN`. Cached for `SHARIAH_TTL_DAYS` (7) and refreshed on a new 10-K/10-Q.
- **Status:** **TO CONFIRM** — the exact route and response shape are the main unknowns;
  check the plan's docs and the terms of use, then confirm with `penny doctor`.
- **Where:** `src/penny/shariah.py` (`HalalTerminalSource`).

## C14 — Single Shariah source (by decision)
- **Question:** the earlier request named two providers (halal.sh and "mustafa.com"); the
  owner has since directed that **halalterminal.com only** be used.
- **Default adopted:** exactly one source. `combine()` still supports several, but the
  runtime builds one `HalalTerminalSource`; `UNKNOWN` when no result is returned. halal.sh
  and Musaffa are no longer referenced anywhere.
- **Status:** implemented; confirm the halalterminal plan covers the whole sub-$10 universe.

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
