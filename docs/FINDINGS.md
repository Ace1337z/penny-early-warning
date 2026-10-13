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
  `Change` percentage (`price / (1 + change/100)`), falling back to the `Prev Close` column
  (which is required once an extended-hours price is used). `DATA_PROVIDER` defaults to
  `finviz`; there is **no second quote feed**. The `Average Volume` column is in
  **thousands** and is multiplied by 1000 by `avg_volume_shares()`.
- **Status:** **TO CONFIRM** on the VPS (acceptance tests 1, 5); the column set and volume
  units were verified against a live Elite account (see C23).
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
- **Default adopted:** `Volume` is the cumulative day volume (used for Volx and dollar
  volume); `Relative Volume` is volume divided by average volume. In the **post** session
  Finviz keeps `Price`/`Change` at the regular close and the real move sits in
  `After-Hours Close`/`After-Hours Change`, so those are preferred when
  `FINVIZ_EXTENDED_HOURS=1` (default) and the move is expressed from the regular close so
  `% vs close` includes it. In **pre-market** Finviz already folds the pre-market print into
  `Price`/`Change` (the after-hours columns would be yesterday's), so no override is applied.
  The session is also recorded from the clock, not from the row.
- **Status:** **TO CONFIRM** against a live screener call at 09:00 and 17:00 ET.

## C4 — Safe request rate and latency from the VPS
- **Question:** what Finviz request rate is safe, and what latency is achievable?
- **Default adopted:** `FINVIZ_MAX_PER_MIN` (30) global rate limit; movers polled every
  `MOVERS_POLL_SECONDS` (20 s) with a `FINVIZ_MOVERS_TTL` (15 s) cache; universe/quote feed
  refreshed every `UNIVERSE_POLL_SECONDS` (30 s) during sessions, hourly when closed. Targeted
  snapshots batch `FINVIZ_QUOTE_BATCH` (40) tickers per `t=` call. HTTP 429 waits
  `Retry-After` or `2^n` seconds.
- **Measured:** against a live Elite account, 60 single-symbol exports paced at **30/min**
  completed with **zero 429s** and ~0.3 s median latency (first call ~1 s). The in-session
  cycle wants 7 daily + 2 intraday mover calls per 20 s plus one universe call per 30 s
  (~29/min), so the default was raised from 20 to 30 to stop the limiter queueing and
  stretching the cadence.
- **Status:** **TO CONFIRM** on the VPS (acceptance tests 1, 9); 429 handling is already
  in place if the Elite plan is stricter.

## C5 — WebSocket push
- **Question:** authentication and subscription quota for WebSocket push.
- **Default adopted:** **REST polling only** in v1.
- **Status:** not implemented by design.

## C6 — Finviz Elite filter codes, columns, links and limits
- **Question:** valid filter codes for price under $10 and common stocks only; columns/view
  with Avg Volume, Rel Volume, Float, Short Float, Market Cap, Change, Price; multi-ticker
  `t=`; extended-hours prices; news; insiders; groups/calendar/latest-filings columns;
  request limits.
- **Default adopted:** filters `sh_price_u10,ind_stocksonly` and the extended custom view
  `c=0..66,67,71,72,81,86,87,88,90..99` (84 columns; `FINVIZ_FILTERS`, `FINVIZ_COLUMNS`) so
  Avg Volume, Rel Volume, Float, Short Float, Market Cap and Sector **plus** Volume, Prev
  Close, After-Hours Close/Change, O/H/L and the intraday Performance columns are present;
  mover screens add `s=<signal>` from `FINVIZ_SIGNALS` and `FINVIZ_INTRADAY_SIGNALS`. The
  fixed links in section 5.2 are used with the token read **per request** from
  `FINVIZ_TOKEN`; HTML or HTTP 401/403 means the token is invalid. If average volume is
  missing, the baseline falls back to 300 shares/min. Unsupported elements are skipped and
  listed under "missing". Finviz calls are capped by `FINVIZ_MAX_PER_MIN` (30) and cached.
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
- **Resolved for any OpenAI-compatible provider:** the base URL may be entered with or
  without `/v1`, a pasted `/chat/completions` suffix is stripped, and both `{base}/v1`
  and `{base}` are tried. A missing `/models` is **optional, never fatal**. The request
  body is adapted when a provider rejects JSON mode, `max_tokens`, or `temperature`.
  Response text is read from a string `content`, a list of content parts, or
  `reasoning_content` when `content` is null; an empty completion is an error, not a
  silent success. If none of the configured model ids are offered by the provider (the
  usual cause of "no AI text"), `doctor` fails loudly and `/models refresh` adopts the
  provider's own working models. `/set AI_MODELS ...` also updates the stored panel, so
  the fix applies from the next cycle without a restart.
- **Where:** `src/penny/ai/gateway.py`, `src/penny/ai/runner.py`, `src/penny/doctor.py`,
  `src/penny/commands.py`, `src/penny/runtime.py`.
- **Status:** implemented and covered by tests; confirm against the live provider with
  `penny doctor` and `/models refresh`.

## C17 / C18 — Backup subsystem (retired)
- **Question:** the off-server backup destination and credentials, archive size versus
  Telegram's document limit, and which encryption tool (`age` or GnuPG) the VPS has.
- **Decision:** the whole backup subsystem was **removed**. There is no `penny backup`,
  `penny restore`, `backup.py`, scheduled snapshot, verify or restore drill any more. Data
  lives in the two SQLite databases under `PENNY_HOME`; keep the host itself backed up with
  whatever the operator's provider offers. `penny update` no longer takes a pre-update
  backup either.
- **Status:** removed.

## C19 — Surfacing momentum builds below the alert bar
- **Question:** the alert bar is tier 2 (`TIER_EARLY`), so slow accumulations (steady
  volume with a mild rise) and pre-burst coils never notify. Does that lose real
  "momentum build" stocks?
- **Default adopted:** a separate **momentum-build watch feed**. `Scorer.build_candidates()`
  ranks stocks that are below the alert bar but clearly accumulating (rise, relative volume
  and dollar volume above `BUILD_*` floors), with `BUILD_MIN_MINUTES` of history and not
  `EXTENDED`/`FADING`. Each signal gets a 0-100 **progress** score toward the tier-2 gates
  (its weakest gate sets the pace), a stage (`WARMING` -> `BUILDING` -> `ACCELERATING` ->
  `COILED`) and the exact gate still missing. Surfaced on demand with `/builds` and as a
  throttled digest (`BUILD_FEED_SECONDS`, default 900) that speaks immediately for a
  brand-new name and otherwise only when the set changes. It is explicitly **not a buy
  signal**.
- **Where:** `src/penny/scoring.py` (`BuildSignal`, `build_candidates`, `_build_stage`),
  `src/penny/alerts.py` (`format_build_feed`, `format_build_digest`),
  `src/penny/engine.py` (`_maybe_build_feed`), `src/penny/commands.py` (`/builds`).
- **Status:** implemented and covered by tests; tune thresholds on the VPS from the
  `cycles`/`daily` data and the forward-return evidence.

## C20 — Tracking a runner that leaves the sub-$10 band
- **Question:** the universe and mover screens both floor at `sh_price_u10`, and
  `_fetch_quotes` drops any price outside `[MIN_PRICE, MAX_PRICE]`. A sub-$10 stock that
  becomes a +200% runner crosses $10 and stops being quoted exactly when the move is
  largest. Does that break tracking and re-alerts?
- **Default adopted:** **not yet changed.** The alert still fires on the way up, but
  post-$10 tracking can go dark. The intended fix is a separate, higher ceiling for
  symbols already known to the scorer (a "tracked" set), so they keep being quoted after
  they leave the scanner band.
- **Status:** **OPEN** (not implemented; the user chose the build feed first).
- **Where:** `src/penny/engine.py` (`build_universe`, `_fetch_quotes`),
  `src/penny/sources/finviz.py` (`universe`, `movers`, `FINVIZ_FILTERS`).

## C21 — Finviz rate budget vs the poll cadence
- **Question:** one in-session cycle issues ~7 daily mover calls plus 2 intraday top-gainer
  calls plus 1 universe call (~29/min) on a shared limiter also used by market context,
  enrichment and `/check`. Under load the limiter queues and the 20-30 s cadence stretches.
  How much can be trimmed?
- **Default adopted:** **resolved for the steady state.** `FINVIZ_MAX_PER_MIN` is now **30**
  (was 20) after a live probe sustained 30/min with no 429s, which covers the ~29/min cycle
  plus a little headroom. The `/check` path no longer forces the hourly market data
  (sectors/events/news) — it refreshes only the 30 s index quotes — and the two Finviz
  verifications per symbol were collapsed into one shared row, so a manual check costs fewer
  rate-limited calls. Further optional reductions: fewer `FINVIZ_SIGNALS`, a longer
  `FINVIZ_MOVERS_TTL`, and skipping snapshots for symbols already present in the cycle's rows
  (already done in `_fetch_quotes`).
- **Status:** fixed (default raised); re-confirm on the VPS under a real session.
- **Where:** `src/penny/sources/finviz.py` (`movers`, `SIGNALS`), `src/penny/engine.py`
  (`_symbols_for_cycle`, `_fetch_quotes`), `src/penny/market_context.py` (`get`),
  `src/penny/enrich.py` (`enrich`).

## C22 — `/check` total latency
- **Question:** why did a manual `/check` run for minutes, and how is it bounded now?
- **Default adopted:** `/check` is four serial stages (enrich, market, Shariah, AI). Two
  could blow up: the gateway replayed **5 body variants x 2 endpoints** against an
  unreachable host (one hung 45 s request became many), and the runner had **no total
  budget** and called fallbacks **sequentially**. Fixes: a dead endpoint is marked and
  skipped instead of replayed; `AI_DEADLINE` (`AI_DEADLINE`, default 60 s) bounds the whole
  panel + fallbacks and clamps each call's timeout to the remaining time; fallbacks run in
  parallel; the hourly market data is no longer force-refreshed; and the double Finviz
  verification is shared. Per-stage timings are logged (`check SYM: <stage> in Ns`).
- **Status:** implemented and covered by `tests/test_check_latency.py`.
- **Where:** `src/penny/ai/gateway.py` (`chat`), `src/penny/ai/runner.py` (`_call`,
  `_call_many`, `analyze`), `src/penny/commands.py` (`_run_check`),
  `src/penny/market_context.py` (`get`), `src/penny/enrich.py` (`enrich`, `_short`).

## C23 — The missing `Volume` column and the intraday top-gainers screens
- **Question:** why did momentum builders (e.g. VCIG, +175% intraday) never alert or even
  appear in `/builds`, when the scanner was live and the feed looked healthy?
- **Root cause (confirmed live):** three separate faults stacked up.
  1. The default custom column set ended at index `66` (`Change`) and never requested `67`
     (`Volume`). `cum_volume` was therefore always `0`, which zeroed `volx` on every symbol -
     so no tier gate (`volx >= 3/10/20`) and no build gate (`volx15 >= 2`) could ever be met.
     The header also mismatched on three names (`Avg Volume` vs `Average Volume`, `Rel Volume`
     vs `Relative Volume`, `Previous Close` vs `Prev Close`) and `verify()` forced `view=111`
     (Overview), which ignores `c=` and drops float/short/avg-volume.
  2. `movers()` applied one list-wide `top_n` cap *while* iterating the signals, and the
     intraday top-gainers run last. The daily screens already produced **267 unique tickers**
     by `unusual_volume`, so `top_gainers_1m/5m` were skipped on **every** cycle - the
     momentum-build signal never fired even with the columns fixed.
  3. The updater preserves `config.env`, so a VPS that had been installed before this work
     still held the old column set (`…,66`) and `FINVIZ_VIEW=111`, which shadowed every
     code-level default.
- **Default adopted:** the custom view is `v=152` with
  `c=0..66,67,71,72,81,86,87,88,90..99` (84 columns), so `Volume`, `After-Hours Close/Change`,
  `Prev Close`, `Open/High/Low` and the intraday `Performance` columns are all present;
  `FINVIZ_VIEW` (default now `152`) applies to both the feed and `verify()`. Three defences
  make a stale config harmless:
  - the client **merges** `REQUIRED_COLUMNS` (Volume, after-hours, Prev Close, O/H/L,
    intraday perf) into every custom-column request (`merge_columns`), so an old `c=…,66`
    still returns Volume;
  - a custom-column request against a **fixed** view (Overview 111, Valuation 121, Ownership
    131, Performance 141, Financial 161, Technical 171 - all of which ignore `c=`) is coerced
    to `152`;
  - `top_n` now bounds **each screen** (`per_screen`), not the merged list, so the intraday
    screens always contribute.
  Mover screens add **intraday top-gainers** (`FINVIZ_INTRADAY_SIGNALS`, default
  `top_gainers_1m,top_gainers_5m`) beside the daily signals; only the
  `ta_topgainers`/`ta_toplosers` bases honour a timeframe suffix, so any other base with a
  suffix is refused (it would return the whole universe). `Average Volume` (thousands) is
  converted to raw shares; `rel_volume` is exposed so the scorer/plan can use Finviz's own
  ratio.
- **Status:** implemented; the column set, header names, volume units, the intraday screens,
  the per-screen cap and the stale-config merge were verified against a live Elite account
  (VCIG reaches the merged movers list). Covered by `tests/test_finviz.py`.
- **Where:** `src/penny/sources/finviz.py` (`SIGNALS`, `intraday_signal_ok`,
  `avg_volume_shares`, `merge_columns`, `REQUIRED_COLUMNS`, `PERF_COLUMNS`, `screener`,
  `movers`, `normalize_row`, `verify`), `src/penny/config.py` (`FINVIZ_VIEW`,
  `FINVIZ_COLUMNS`, `FINVIZ_INTRADAY_SIGNALS`, `FINVIZ_EXTENDED_HOURS`),
  `src/penny/engine.py` (`_symbols_for_cycle`, `_fetch_quotes`, `check_quote`).

## C24 — `/check` price freshness and symbols outside the universe
- **Question:** why did `/check` show the regular-session (open) price during the post
  session, and answer *"no quote available right now"* for a normal stock such as NVDA or
  AAPL?
- **Root cause:** `_run_check` read only `engine._quotes_by_symbol`, which is the previous
  cycle's cache. That cache holds just the sub-$10 universe and the mover screens, so a
  normal stock was never present (no quote), and for a stock that *was* present the row could
  be one cycle old and, in the post session, still the regular close.
- **Default adopted:** `Engine.check_quote(symbol)` fetches the symbol **on demand** with the
  current session and normalizes it (so the after-hours override from C3 applies), falling
  back to the cached quote only when the fetch fails. `_run_check` recomputes the metrics from
  that fresh quote and registers its average volume, so the price, windows and Volx all
  reflect the live print. This also makes `/check` work for any symbol, not just the sub-$10
  universe.
- **Status:** implemented; covered by `tests/test_check_quote.py`.
- **Where:** `src/penny/engine.py` (`check_quote`), `src/penny/commands.py` (`_run_check`).

- **Where:** removed with the backup subsystem.

---

## Acceptance test results

Run on the development machine (Python 3.12, Windows) with `penny selftest`,
`penny simulate` and `pytest`.

### Unit tests (`pytest -q`) — 580 passed, 1 skipped
Covers config defaults and round-trip, storage, scoring (Rise/Volx/Accel/VWAP, tiers,
phase, builds), alert and build-feed formatting, the AI gateway/runner/leaderboard, the
journal, the Telegram client, `/check` latency bounds and quote freshness, and the
Finviz feed (`tests/test_finviz.py`: column coverage, required-column merge, view coercion,
intraday-signal guard, per-screen mover cap, extended-hours price override, average-volume
units, intraday performance). Phase 2 adds `test_phase2_reality.py` (the spread/liquidity
blocker's four conditions and the `WIDE_SPREAD`/`ILLIQUID` labels, the $500 exit walk, the
Alert 1 book line), `test_phase2_tracking.py` (T1-T6, one-abort-ever, the calibration
clamp, the post-mortem clause mapping, the three new EOD sections), `test_phase2_context.py`
(analog K-NN and min_k suppression, the AI-only multi-timeframe block, the sector heatmap,
catalyst decay) and `test_phase2_commands.py` (`/model`, `/postmortem`). Spec 2.0 adds
`test_mechanisms.py` (the three tape-mechanism detectors, the `[NO-NEWS]` tag, the
`MECHANISM:` block, the provenance buckets and the rules-only DECISION block) and rewrites
`test_decision.py` for the adversarial v2 schema. `test_news_age.py` pins the news-source
order (Finviz first, the slow per-symbol feeds only as fallback), the per-ticker Finviz
news-table parse, and the timezone-correct headline age.

### Offline self-test (`penny selftest`) — ALL PASS
Configuration defaults and round-trip; owner-only permissions; state and journal schemas
open; a rising stock reaches Confirmed; score stays 0–100; price above VWAP detected; quiet
noise does not alert; a fading spike is tagged FADING; session windows; the holiday list;
CSV/HTML parsing and number formats; the AI parser (valid object, code fences, HTML
rejection, unknown enums, bad prices); aggregation majorities/medians; the prompt character
cap; Fibonacci levels, extensions and trade plans; Alert 1 shape (including the
bid/ask/spread/15m-dollar book line on line two) and Alert 2 length and
disclaimer; the model catalogue (load, persist, pool excludes aliases and over-limit
models); the composite formula and the Wilson interval; the Shariah combine rules; secret
scrubbing. Phase 2 adds its own section:
the `WIDE_SPREAD`/`ILLIQUID` labels and the honest `INSUFFICIENT_DATA` book case, the
$500 exit walk, a 5-minute close under VWAP invalidating a setup once, the confidence
clamp at hit rate + 15 points (and no prior below 30 samples), analogs suppressed below
`ANALOG_MIN_K`, the AI-only multi-timeframe block, the hot/cold sector line, the
post-mortem clause mapping, the HARD/DILUTION decay curves, and every Phase 2
configuration key existing in the defaults. Spec 2.0 adds: the rules-only
DECISION block with its `SOURCE:` line, the adversarial v2 schema (non-additive
rejection, the honest null, the server-side ENTER-on-a-blocked-gate check, facts
that never carry the gate verdict), the `[NO-NEWS]` tag and `MECHANISM:` line,
and the provenance buckets `/stats` reports.

### Offline simulation (`penny simulate`) — ALL PASS
Synthetic market: a grinder, a burst, a spike that fades and 60 noise stocks, with
synthetic index data, a fake AI and a fake Telegram. Verified results: no alerts on noise
stocks; the grinder alerts at Early build under +60% and reaches Confirmed before +100%;
the burst alerts within 8 minutes of starting and under +50%; every alert receives
enrichment and a forecast (Alert 2 sent); Fibonacci levels and a plan appear; market context
appears in the AI input; a forced price mismatch produces the DATA MISMATCH line; forecasts
are stored for all candidates and evaluated at their horizons against the simulated prices;
the leaderboard ranks the deliberately accurate fake model above the deliberately wrong one;
the momentum-build feed surfaces accumulating stocks below the alert bar and never lists an
alert-tier stock;
a model that does not beat the naive baselines is not activated; automatic selection returns
a three-model panel or keeps the current one; the cache reuses an analysis within the cache
window; the AI panel survives a failing model; parsers reject HTML and missing columns;
staged elimination runs; tracker events fire; cycle time averages ~7 ms for ~63 symbols
(well under 500 ms); and the Shariah combine rules behave as specified. Phase 2 adds: a
wide spread blocks as `WIDE_SPREAD` and a thin market as `ILLIQUID`, a feed with no book is
`INSUFFICIENT_DATA` (never a pass), a $500 exit that cannot be filled in 300s blocks as
`ILLIQUID` while a deep book exits within the limit, a 5-minute close under the alert VWAP
invalidates the setup exactly once, the multi-timeframe block is AI-only and says
`unknown` without daily history, and a hot sector gets a line while a cold one gets none.
Spec 2.0 adds: a no-news tape setup clears the gate on a named mechanism and is tagged
`[NO-NEWS]`, the alert renders `MECHANISM: silent accumulation` rather than `none found`,
and `/stats` splits closed alerts `BY SOURCE` (`agreement`/`disagreement`/`rules_only`/
`ai_rejected`).

### Not yet run (need live accounts / a VPS)
Acceptance tests 2 (token change), 3 (installer on a fresh VPS), 4 (`doctor` all PASS),
5 (mover parity), 6 (replay), 8 (failure drills), 9 (5-day soak), 12 (model catalogue
against the real provider). Run these on the VPS once the credentials are in place.

## C25. A closed market reported the frozen regular close as live

`session_for()` looked only at the clock, never the calendar, so a Saturday
afternoon returned `post`. Two consequences, both observed on a Sunday:

- The engine polled a market that was shut and reported Friday's frozen close as
  if it were live.
- `normalize_row()` applied the After-Hours Close/Change override only when
  `session == "post"`. From 20:00 ET to the next pre-market, and all weekend, the
  frozen regular close won instead.

Measured on Finviz `ta_topgainers_1m` during the Sunday session: YMAT publishes
`Price 1.30 / Change -0.77%` alongside `After-Hours Close 3.03 / +134.85%`. The
override now covers `post` **and** `closed`, and `session_for()` returns `closed`
for weekends and market holidays. Live check: regular `1.29` -> post/closed
`3.03`.

## C26. `ta_topgainers_4m` silently returns the whole universe

Finviz ignores an unsupported timeframe suffix rather than erroring. Measured
against the live Elite API:

| signal | rows |
|---|---|
| `ta_topgainers_1m` | 3 |
| `ta_topgainers_3m` | 7 |
| `ta_topgainers_5m` | 7 |
| `ta_topgainers_4m` | **2317** |

`intraday_signal_ok()` previously returned `True` for any unrecognised suffix, so
a typo would pull the entire universe into the movers list. It now validates the
base against `INTRADAY_BASES` *and* the suffix against the known timeframes, and
refuses anything else. `2m` and `3m` are supported and confirmed live; the default
is `top_gainers_1m,top_gainers_3m,top_gainers_5m` because the 15-minute screen is
where a +20% alert used to originate.

## C27. The plan was computed from a price the ticker had already left

Two separate causes, both producing a plan that looked like it came from the
wrong stock:

- `_candles()` took the **first source that returned anything**. A source that
  answers with yesterday's history beat one with a bar from the last minute purely
  by position. Sources are now scored on the age of their newest bar (`<= 90s`
  wins) and the freshest usable set is returned.
- Even a fresh candle set lags the quote by up to a full minute, so the EMA/RSI,
  the Fibonacci swing and the entry/stop were all computed from a stale close.
  `_live_tail()` now folds the live quote into a synthetic final bar, carrying the
  recent median volume so the volume-spike test is neither zeroed nor inflated.

Live after the fix (AAPL, VCIG): VCIG swing 0.4711 -> 0.5798 (23.1%) with a
`PULLBACK` plan at R:R **3.2:1**, viable. AAPL's tight 2.4% swing correctly yields
`R:R -0.0:1 | NO TRADE: R:R below minimum` instead of a confident-looking plan.

## C28. TwelveData cannot be the extended-hours source on the free plan

Measured directly, which changed the design:

- `/quote` has **no** `post_market_price`; `close` is the last trade, with
  `last_quote_at` at Friday 19:59 UTC (15:59 ET).
- `time_series?prepost=true` is rejected with **HTTP 403**, and every returned
  1-minute bar sits inside 09:30-16:00 ET (exactly 390 bars/day).

So TwelveData is the *freshness* source for single-symbol price, volume and
candles, and Finviz's After-Hours columns remain the extended-hours source. Both
free tiers are respected by construction: TwelveData is never pointed at the
~2300-symbol sweep, only at alerted names and `/check`.

## C29. The 15-minute build digest was on by default

`_maybe_build_feed()` returned early on the config gate *before* assigning
`self._builds`, so simply defaulting the notification off would have silently
broken `/builds`. The scan now always runs and populates the list; only the send
is gated, and `BUILD_FEED` defaults to `0`.

## C30. A replaced API key was saved but never used

Three caches were built with the *old* credential and survived its replacement,
so swapping a key looked like a no-op until a restart. Each needed a different
fix:

- `AIGateway._base` remembered which endpoint answered. Keyed on `AI_BASE_URL`
  alone, so replacing only `AI_KEY` re-posted to that endpoint with a credential
  the provider answers 401 for. It is now keyed on a digest of base *and* key, and
  `/set AI_KEY` calls `forget_endpoint()` so the next call re-discovers.
- `AIRunner.provider_id()` had the same gap, leaving `active_panel` holding the
  old provider's model ids and every call failing as "model not found". It now
  includes a key digest, and `reset_after_credential_change()` rebinds the panel,
  clears the analysis cache, and prefers the new provider's `/models` list.
- `ShariahService` stored an `ERROR` row for a failed lookup and then served it
  as a fresh result for the whole `SHARIAH_TTL_DAYS` (7 by default). One bad key
  poisoned the cache until it expired. Failures are no longer stored, and
  `/set HALALTERMINAL_API_KEY` clears the table outright.

The key is digested, never embedded: `identity` and `provider_id()` are logged.

## C31. Every alert was late because every gate measured a 15 or 60 minute window

`tier_for` reads `rise15`, `volx15` and `rise60`, and tier 2 additionally demands 10x
normal 15-minute volume. A 3-minute burst therefore cannot reach an alert tier until
minutes after it has already given the move back -- the alert was a description of
something that had finished. The same 15-minute window was the only volume figure
supplied to the AI panel, so the panel judged a fresh burst as "nothing behind the
move" and returned WAIT on the alert that had just fired.

Replaying a realistic burst (16 minutes flat, then +3.0% in three minutes on
600k shares per minute, 60s polls) measured the gap directly:

    FAST lane (3m/5m)     : t+18m
    tier gates (15m/60m)  : never

The tier gates never fired at all, so this was not a threshold to loosen but a
window that had to be added. `WINDOWS` gained 3 minutes, and `Engine._fast_burst()`
reads the 3- or 5-minute window -- either one, since a stock up 4% in five minutes
has not necessarily moved 2% in the last three -- with volume judged on whichever
window actually triggered.

Three things that looked like extra work turned out to be part of the same bug:

- The first draft required *both* `FAST_RISE3` and `FAST_RISE5`, and measured
  volume on both windows. That fails a 5-minute-only builder on its empty
  3-minute volume: the same error the tiers already made at 15 minutes.
- A `FAST_RISE60` guard was added to keep "a 3-minute pop on a dead tape" out. It
  rejected the exact case worth catching -- a stock flat all day that suddenly
  runs -- which is the penny mover this tool exists for. Removed, not loosened.
- `baseline_prices()` extrapolated `rise15` for the naive scoreboard, so a fast
  alert was graded against a window it never used. It now projects from the
  window the move actually happened in.

The fast lane can only *add* alerts; the tier path is untouched, so a mis-set
`FAST_*` threshold costs extra messages, never a missed one.

---

## C32. Long-only, with a strategy gate the alert actually passes through

**Finding.** The screener ranks movers. It does not know *why* one moved or
whether the setup is still buyable, so an alert was a statement about price and
nothing else. Two structural rules fix that without adding a data provider:

- **Long-only.** Exhaustion and distribution can only produce `WAIT` / `PASS` or
  an exit warning. There is no short branch anywhere in the gate, and no detector
  returns one. A short entry zone or a `SHORT` verdict from the model is
  rejected, not repaired.
- **The gate runs before the alert.** 18 named detectors, split by data cost: the
  metrics-only primaries run in the poll loop with zero extra fetches, and the
  enrichment primaries run afterwards on the detail thread where enrichment is
  already being paid for. The second, richer verdict is the one that counts.

**Blockers, in precedence order:** `DATA_CONFLICT` (quote and verified price
disagree) -> `DILUTION` (offering priced below today's open) -> `EXHAUSTED` (a
parabolic move that is spent) -> `no_strategy` -> `below_min_score`. A block is a
refusal to enter, never a short.

**Where the boundary sits.** `STRATEGY_GATE` defaults to `0` so the old tier path
keeps running for comparison; the alerts in this write-up are the gate path. The
alert fires the moment the gate passes, and the 15m/60m windows are out of the
alert header - those are what made a live burst read like a move that was already
over.

**Ceiling.** Detectors that need prior sessions (`first_green_day`, `day3_surge`,
`worg`, `float_rotation`) return `INSUFFICIENT_DATA` until `daily_close` history
has accumulated, and the gate refuses rather than guessing. They start passing on
their own from day 7; nothing to configure.

## C33. One AI decision call, strictly validated

**Finding.** The panel sent the same setup to several models, scored their
agreement, and printed a leaderboard. That cost several calls per alert to produce
a number nobody acted on differently: the verdict was `ENTER` or it was not, and
the models rarely disagreed about that in a way that changed the trade.

**What replaced it.** One call, one DECISION REPORT: verdict, rating, findings,
entry points (LONG), exit points. The methodology lives in
`prompts/methodology_v2.md` and is loaded, not inlined, so it can be edited and
versioned on its own. (Spec 2.0 replaces the v1 verdict-first prompt with the
adversarial v2 schema; see C37.)

**Validation rejects rather than repairs.** A missing required field, an `ENTER`
with an empty `rubrics_applied`, an `ENTER` on a gate that did not pass, a short
entry zone, a `SHORT` verdict, and HTML in place of JSON are all rejected. A
rejected report degrades to `PASS`: the setup may be good, but a report that
contradicts its own gate has not earned an entry. Quietly fixing it would hide a
real disagreement between the gate and the reader.

**No persona.** The prompt states "do not roleplay" and the validator would reject
a persona-styled finding that contradicted the gate. One reader, one view of the
setup, stated plainly.

## C34. Every alert graded to the close, reported once a day

**Finding.** An alert is a prediction. Without measuring it there is no way to
tell whether the gate is finding winners or just finding things that moved, and a
strategy library that is never graded only ever feels good.

**Two design decisions worth stating, because both cost something:**

- **Tracking runs off the quotes the engine already polls**, not off re-fetched
  1m candles. Finviz returns a session high and low on every row, so max gain,
  drawdown, and the stop/target tests are exact at poll resolution - and the
  TwelveData quota (8 credits/min on the free tier) is left alone. The cost is
  that a spike between two polls is invisible; at the 20s cadence that is
  acceptable.
- **One message a day, not one per alert.** Per-alert result messages train the
  reader to ignore results. A single end-of-day report makes the day's alerts as a
  set, which is how they should be judged: not "did this one work" but "did the
  gate, today, on this set".

**A missing close is `invalid`, not flat.** A symbol that stops being quoted before
the close is excluded from the day's average rather than counted as a 0% day.
`invalid` is the honest label; a fake flat day is the failure mode that would make
the grade look better than it was.

**Commands.** `/eod` (today or `/eod YYYY-MM-DD`), `/tracked`, `/stats` (the
strategies ranked by what they actually produced).

## C35. Phase 2: the nine refinements, and the two gaps the tests found

**Finding.** Grading an alert is only half the work; the other half is refusing
trades that cannot be taken, saying so when a setup dies, and keeping the AI's
confidence honest about its own sample. Nine self-contained features, all
additive:

- **F1 reality gate.** A spread over `SPREAD_MAX_PCT` (1.5%) blocks as
  `WIDE_SPREAD`; thin 15-minute dollar volume, a sub-$200 average trade size or
  fewer than `MIN_TRADES_15M` prints blocks as `ILLIQUID`. The label follows the
  first failure, because "the market is quoting wide" and "nobody is here" are
  different facts. No bid/ask in the feed is `INSUFFICIENT_DATA` - never a pass.
  Alert 1 line two now carries the book itself.
- **F4 invalidation.** Six triggers (T1-T6: VWAP, alert-time low, halt,
  persistent wide spread, negative catalyst, exhaustion near the high on
  declining volume), one abort ever, status `aborted`, and an instruction to not
  enter / exit - never a short.
- **F5 calibration.** Once a (rating, strategy) bucket has
  `CALIBRATION_MIN_SAMPLES` (30) closed alerts, the confidence shown is clamped
  to that bucket's realised T1 hit rate + `CALIBRATION_CLAMP_MARGIN` (0.15). The
  AI's own number is kept but may not sit far above what the tape delivered.
- **F6 analogs.** K-NN (K=20) over 540 days of tracked alerts, z-scored, with the
  hit rates summarised for the AI - and suppressed entirely below
  `ANALOG_MIN_K` (8), because a hit rate from three trades is a rumour.
- **F8 multi-timeframe.** Daily MA/range, 60-minute trend and weekly float
  turnover, marked "for reasoning only; not shown to the user".
- **F9 sector heatmap.** One line only when the sector is hot (median >= 2.0% or
  three names on the list), refreshed at most every 300s, median rather than
  mean so one extreme name cannot heat a sector.
- **F10 exit liquidity.** A $500 exit needing over 300s or slipping over 1.0%
  blocks as `ILLIQUID`; the $2k estimate rides along in the tracker columns.
- **F14 post-mortems.** One line per loser in the EOD (and
  `/postmortem SYM [DATE]`) naming the rubric clause that should have blocked -
  or "nothing in the rubric covered this", which is the finding.
- **F15 catalyst decay.** Exponential half-life: HARD 180m, MEDIUM 120m, SOFT
  30m; dilution (negative weight) never decays.

**Two gaps the new tests caught, both wired before shipping:**

1. `ai_rating`, `ai_verdict` and `ai_key_findings` were declared columns that
   nothing ever wrote, so the EOD `BY AI RATING` section would have grouped every
   alert under `unrated` with claimed 0.00, and every post-mortem would have said
   "the AI rated it unrated / ?". `Tracker.write_decision()` now records the one
   call's rating, verdict and findings, and the engine writes them alongside the
   calibrated confidence.
2. `build_context()` labelled a stock *below* its 20-day mean "above 20d MA"
   whenever a previous close existed (the percentage in the same line was
   negative, so the label contradicted its own number). Below the mean now reads
   below - the case that matters most for a long-only system, where "already
   extended" is the expensive error.

Also fixed: the selftest's "no 15m/60m in the header" check asserted on the whole
Alert 1 text, which Feature 1's `15m $` dollar-volume label legitimately trips;
the ban is on the header's rise line (Spec 11.1) and now reads that line.

**Commands.** `/model` (show or change the decision model, no restart) and
`/postmortem SYM [DATE]`. `/models` remains a legacy alias; the CLI's
`penny models` is unchanged.

## C36. The gate's wiring was broken four ways, and nothing could see it

**Finding.** `/check` answered every request with
`Check failed: 'Metrics' object has no attribute 'minutes_tracked'`. The
metrics-only pass in `strategies/wiring.py` read `m.minutes_tracked`, a field
`Metrics` has never had in any commit - the strategy tests build a `Bundle`
directly, so the seam was never exercised, and `_run_gate` caught the exception,
logged it and returned *no gate at all*. Three defects followed from one
untested seam, all of them invisible to the 403 tests:

1. **`/check` died.** The command calls `run_gate` directly, unguarded, so the
   AttributeError reached the reader as "Check failed: ...".
2. **A mandatory gate failed open.** With `STRATEGY_GATE=1`, the crashed gate
   read as "no gate to enforce", and `cycle()` let the alert out ungated - the
   one filter the system is built around, silently bypassed. An evaluation that
   failed is now fail-closed, counted as `gate errors` and shown in `/status`,
   because a skipped alert costs an opportunity while a bypassed gate costs the
   premise.
3. **The rejection record raised too.** `_note_gate_reject` wrote
   `[f for r in gate.strategies if r.failed_on]`, which names `f` without ever
   binding it: the first gate rejection raised NameError and took the cycle
   down. It records a flat list of failure strings now.
4. **The gate could never pass.** The poll loop builds its Bundle without
   candles or news, so `no_strategy` and `no_catalyst` are missing inputs, not
   verdicts - and enforcing them rejected 63 of 63 candidates: switching the
   gate on muted the system outright. Those two now defer to the enriched pass,
   the reality blockers (`WIDE_SPREAD`, `ILLIQUID`, `DILUTION`, `EXHAUSTED`,
   `DATA_CONFLICT`) veto on the poll, and the tier bar stays as the candidate
   filter instead of being waived - waiving it left the path with no filter at
   all, which is what STRATEGY_GATE=1 had actually been doing all along: the
   gate crashed, nothing else was asked, and every candidate alerted.

The full simulation now passes with the gate mandatory (49/49) as well as
advisory, and `tests/test_gate_wiring.py` pins each rule: the seam, the
rejection record, fail-closed, the undecidable verdicts, and the tier bar.
`/check` is covered end to end for the first time.

**Rule.** A safety filter that cannot run must fail closed, and a verdict
resting on data that was never fetched is missing data - not a decision.

## C37. The gate required a cause, so it could never alert on the moves it was built for

**Finding.** Spec 1.x treated a named catalyst as a *precondition* of the gate:
`evaluate_gate` refused any setup whose news feed named nothing, and
`GATE_REQUIRE_CATALYST=1` made that mandatory. That is exactly backwards for this
tool. A mechanical squeeze has no press release. An accumulation ran because
someone was working an order, not because a headline printed. A sympathy move is
a peer's headline, not this name's. Every one of those is the *reason* the move
is early - and the cause-first gate filtered all of them out, leaving only the
promotional names that print a press release, which are the ones a reader is
least early on.

The AI half had the mirror problem. It was handed the gate's verdict and asked
for a second opinion, so "agree with the gate" carried no information, and a
report that merely restated the tier threshold passed as a valid judgment.

Spec 2.0 decouples both:

1. **Cause is a label, not a gate** (`CAUSE_REQUIRED=0`). Three tape-mechanism
   detectors - `silent_accumulation`, `sympathy_move`, `short_squeeze_mechanics`
   - fire only when no cause was *named* (`_no_named_cause`, and `news is None`
   is unknown, never absent), so a cause-free setup clears the gate and the
   alert is tagged `[NO-NEWS]` with a `MECHANISM:` line where `none found` used
   to be. The squeeze floor drops 15% -> 8% (`SHORT_FLOAT_MIN`) so the
   mechanical detector fires before the crowd is committed.
2. **The AI is adversarial** (methodology v2). The gate verdict is removed from
   the facts entirely, so it cannot be echoed; the report leads with
   `CATALYST QUALITY` and `STRONGEST RISK`; a report that restates the gate
   (`tier`, `gate score`, `detector`, ...) is rejected as non-additive; the
   honest null (`unknown` + `none identified`) is accepted and marked, but
   `ENTER` on top of it is refused. An `ENTER` on a blocked gate is caught
   server-side, from what the caller knows, not from the payload.
3. **Provenance is explicit.** Every DECISION block ends with
   `SOURCE: ai` / `ai_rejected` / `rules`, and an unavailable or rejected AI
   degrades to a full rules-only block (`render_rules_fallback`) - never a
   placeholder, never a silent `PASS`. `/stats` buckets every closed alert
   `BY SOURCE` (`agreement` / `disagreement` / `rules_only` / `ai_rejected`) so
   the model's contribution is measured rather than assumed.

Seven acceptance criteria were added to `simulate` and `selftest`; the full
simulation passes (54/54) with the gate advisory and mandatory.

**Rule.** A gate may require evidence; it must never require a *headline*. The
absence of a named cause is itself a signal - name the mechanism or say
`unidentified`, but never treat "no press release" as "no setup".

---

## C38. Every headline read "0m ago", and the slowest feed decided the news

- **Question:** why does every headline show an age of `0m`, and can the news come
  from Finviz, which is faster than Alpaca/Finnhub/Google News?
- **Finding:** two separate faults. The age was always wrong because
  `Enricher._age_of` stamped *every* timestamp as ET. Finviz's news export and the
  wires publish UTC and Alpaca appends `Z`, so a fresh headline parsed as ET landed
  hours in the future; `age_str` clamps a negative age to `0s`, and the cause line
  renders that as `0m ago`. Separately, `_news` asked Alpaca, Finnhub and Google
  News *before* Finviz, so the slow feeds - extra round trips for the same move -
  decided the news, and the Finviz block padded unrelated market headlines onto
  every symbol (`or len(items) < 3`).
- **Default adopted:** Finviz is asked first and is the source; the per-symbol
  feeds are the fallback, skipped once Finviz named the symbol (`_news(fast=True)`
  on the alert path). The invalidation watcher passes `fast=False` so it still sees
  a dilutive PR only a wire carries - missing one is the risk that watcher exists to
  cover. Finviz rows carry their `Source` and `Url`. `_age_of` reads `Z`/`GMT` and
  RSS `pubDate` in their own zone, reads a naive value as ET, and re-reads it as UTC
  only when ET would put it in the future. Only a headline that names the symbol is
  attached to the move.
- **Where:** `src/penny/enrich.py` (`_news`, `_age_of`).
- **Why it matters:** an age that always reads "just now" makes a stale catalyst
  look live, which is the one thing the age is there to prevent; and the news source
  should be the feed that is already in hand, not the three that cost a round trip.

---

## C39. A small cap with a $201M tender offer alerted with no news at all

- **Question:** SKYA rose 43% on a GlobeNewswire tender offer, yet Alert 2 read
  `MECHANISM: unidentified`. Finviz clearly had the story on its ticker page. Why
  did the system not?
- **Finding:** the news source was `/export/news`, which is **market-wide**: it
  carries a small cap's press release only when the headline literally spells the
  ticker. "SkyAI Plans $201 Million Cash Tender Offer..." never says "SKYA", so the
  symbol filter dropped it and the alert had no cause. The symbol's own headlines
  live on its **quote page** in `id="news-table"`, keyed by `data-ticker`, which the
  system never fetched. Two smaller faults rode along: the page's dates read
  `Today 10:58AM` / `Oct-11-26 11:14PM` (ET, no year on the current-day form), and a
  bare time parsed as midnight would have made the age wrong again.
- **Default adopted:** `FinvizClient.ticker_news(symbol)` fetches the quote page and
  `parse_finviz_news_table` extracts `title`, `source`, `url` and a real ET
  `published` timestamp (`_finviz_dt` handles the `Today`/`Mon-DD-YY` forms). `_news`
  uses it first and falls back to the market-wide CSV only when the page yields
  nothing. No HTML library was added: the table is regex-parsed, the only structural
  assumption being the `id="news-table"` anchor and the `tab-link-news` links.
- **Where:** `src/penny/sources/finviz.py` (`ticker_news`, `parse_finviz_news_table`,
  `_finviz_dt`), `src/penny/enrich.py` (`_finviz_news`).
- **Why it matters:** "no news" is the most dangerous reading a small-cap alert can
  give, because it invites treating a priced, material event as an unexplained pump.
  The per-ticker page is the feed that knows, and it costs one request the quote path
  already pays for.

## C40. The 0.6 weight floor made a three-hour-old tender offer "unidentified"

- **Question:** after C39 the tender offer was finally in the feed, yet the alert
  still read `MECHANISM: unidentified`. The headline was right there with a HARD
  class. Why was it dropped?
- **Finding:** `extract_cause` skipped any item whose decayed `weight < 0.6`. That
  floor is the MEDIUM base weight, so it silently became a class-blind cut: a HARD
  catalyst decays on a 180m half-life and is 0.50 at three hours and 0.40 at the
  four-hour `CAUSE_MAX_AGE_H` edge — below the floor the whole time, so a material
  event inside its own window was treated as no cause. The old `0m ago` age bug
  (C38) had pinned every weight at 1.0 and masked it; fixing the age exposed it.
  The code comment claimed "a headline four hours old has already fallen below the
  0.6 floor for anything but a HARD catalyst", which is backwards — HARD is exactly
  what the floor was dropping.
- **Default adopted:** the 4h `CAUSE_MAX_AGE_H` window is the outer bound and the
  *class* decides inside it. HARD is nameable for the whole window regardless of
  decayed weight; MEDIUM still has to hold 0.6; SOFT never qualifies. Decay remains
  a ranking signal (`rank_catalysts` sorts on it), not a nameability test.
  *(Superseded by C43: the "MEDIUM still has to hold 0.6" clause was itself the
  bug -- MEDIUM's base weight is 0.6, so it never held. Only SOFT is dropped now.)*
- **Where:** `src/penny/strategies/news_cause.py` (`extract_cause`).
- **Why it matters:** the floor conflated "how strongly does this still pull"
  with "may we name it at all". Those are different questions, and collapsing them
  turns the one feed that knew about a $201M tender offer back into "no news".

## C41. Turning the gate on by default exposed a tracker that silently dropped the plan

- **Question:** flipping the shipped defaults to `STRATEGY_GATE=1` failed the
  end-to-end simulation with "every alert is entered in the per-alert tracker:
  2 tracked vs 3 alerts". No code on the alert path changed. Where did a row go?
- **Finding:** `Engine._fire_alert` passes `bundle.plan` -- a `TradePlan` dataclass
  -- into `Tracker.start`, which did `(plan or {}).get("stop")`. `TradePlan` has no
  `.get`, so it raised `AttributeError`, and the broad `except` in `start` logged it
  at debug and returned `None`. Every alert under the gate fired with no tracking
  row: no outcome, no EOD grading, no calibration sample. The tier path had always
  passed a plain dict, and `STRATEGY_GATE=0` had been the default, so the mismatch
  never ran in the shipped configuration.
- **Default adopted:** `Tracker.start` takes either shape. A small `_field` resolver
  reads a mapping by key and an object by attribute (`stop`/`target1`/`target2`,
  `entry_low`/`entry_high`), and `plan_entry_low`/`plan_entry_high` are filled from
  the plan when the caller does not pass them. Neither caller reshapes its input.
- **Where:** `src/penny/tracking.py` (`Tracker.start`).
- **Why it matters:** an exception swallowed inside a "best effort" tracker is the
  worst kind -- the alert looks fine, the message is sent, and the only symptom is a
  missing row that nothing reads until someone counts them. The broad except stays
  (a tracking failure must never cost the alert) but the input is now honest.

## C42. Shipped defaults tightened (gate on, stricter liquidity and burst bars)

- **Question:** the operator asked for a stricter default profile. What changes?
- **Default adopted:** `STRATEGY_GATE=1`, `ALERT_MIN_TIER=3` (CONFIRMED),
  `SPREAD_MAX_PCT=0.8`, `MIN_AVG_TRADE_SIZE=500`, `MIN_TRADES_15M=40`,
  `EXIT_LIQ_500_MAX_SEC=60`, `EXIT_LIQ_MAX_SLIP_PCT=0.5`, `FAST_RISE3=5`,
  `FAST_RISE5=7`, `FAST_MAX_FROM_HIGH=2`, `FAST_MIN_VOLUME=100000`,
  `PREMARKET_PCT=15`, `MIN_RR=2.5`. `BUILD_FEED` stays 0.
- **Note on `FAST_MAX_FROM_HIGH=2`:** the guard only engages on a *negative* value
  (`if from_high < 0 and ...`), so a positive one disables the from-high check
  entirely. Set as asked; the effect is that a spent burst is no longer rejected on
  the give-back alone.
- **Where:** `src/penny/config.py` (DEFAULTS), `config.env.example`.
- **Why it matters:** the tier and fast-lane tests encode the old bars, and the
  simulation run exercised the tier path. Those were updated to pin the mode they
  test rather than inherit the new default; the shipped behaviour is the new one.
  The tracker bug above is what turning the gate on surfaced -- the defaults were
  not the only thing that had been hidden by `STRATEGY_GATE=0`.

## C43. A real headline with no keyword matched read as noise, and every MEDIUM expired instantly

- **Question:** SKYA's tender offer now shows, but GIXI's "Gix Internet Brings
  Physical AI to Life-Saving Logistics..." did not, even though it is on the
  ticker's own Finviz news table. The fetch and parse were verified working (the
  row was parsed). So why no cause?
- **Finding:** two independent defects, both ticker-agnostic.
  1. `classify`'s catch-all returned `SOFT`. The patterns are a *vocabulary of
     known events*, so any real wire headline whose wording they did not
     anticipate -- a robotics/hospital PR -- fell through to the same class as a
     conference notice, and `extract_cause` never names `SOFT`. SKYA only worked
     because "tender offer" happens to match `_HARD`.
  2. The `weight < 0.6` floor was still class-blind for everything but HARD.
     MEDIUM's base weight *is* 0.6, so `0.6 * 2**(-t/120) < 0.6` for any `t > 0`:
     a MEDIUM was dropped the instant a single minute passed, i.e. always. C40
     exempted HARD only, which fixed SKYA and left every MEDIUM equally dead.
- **Default adopted:** an unrecognised headline is `MEDIUM`, not `SOFT` -- a real
  event the patterns did not categorise, not noise. The genuinely routine phrases
  (updates, presentations) and pure chatter (`social media`, `trending`,
  `stocktwits`, `reddit`, `meme stock`, `buy alert`, ...) are named explicitly in
  `_SOFT`, so the fallback is honest. In `extract_cause`, nameability is the
  *class*: HARD and MEDIUM are nameable for the whole `CAUSE_MAX_AGE_H` window
  (4h here; widened to 24h in C44); only `SOFT` is dropped; the redundant
  `weight < 0.6` cut is gone. Decay remains a ranking signal (`rank_catalysts`
  sorts on it), not a nameability test.
- **Where:** `src/penny/strategies/news_cause.py` (`classify`, `_SOFT`,
  `extract_cause`).
- **Why it matters:** the cause-first design is only as good as the vocabulary it
  recognises, and a keyword list can never be complete. Treating "unrecognised"
  as "noise" silently discarded the very headlines -- novel events, unusual
  companies -- most likely to be the reason a penny stock moved. The floor made
  the same mistake in time: it asked "how strong is this still" when the question
  is "may we name it at all".

## C44. The 4h naming window dropped a pre-market catalyst, and the headline had nowhere to render

- **Question:** after C43 the GIXI headline is a real event, so why did the alert
  still read `MECHANISM: unidentified — treat as speculative` with the Finviz PR
  visible on the ticker page?
- **Finding:** three faults, all on the naming path.
  1. `extract_cause` bounded the age at `CAUSE_MAX_AGE_H = 4`. GIXI's PR printed
     at 07:32 and the alert went out in the afternoon, ~7h later, so the headline
     was dropped. The 4h bound contradicted the rest of the system, which uses a
     24h horizon throughout (`CATALYST_MAX_AGE_MIN=1440`, `rank_catalysts`' own
     default of 1440, the "no news in 24h" mechanism reason, and the "none found
     in the last 24h" text). A pre-market press release *is* the cause of an
     all-day move; the 4h cut was calibrated for a same-session catalyst only.
  2. The `CAUSE_MAX_AGE_H` config key was dead. `extract_cause` read the module
     constant directly, so setting the key changed nothing.
  3. When no cause was named, `cause_block` returned only the MECHANISM line. A
     fetched headline that ranked SOFT or fell outside the window was never
     rendered anywhere -- the alert looked like the feed had returned nothing
     while the PR sat in `enr.news`.
- **Default adopted:** the naming window is 24h (`CAUSE_MAX_AGE_H`, module
  default and shipped config), copied onto the Bundle (`cause_max_age_h`) by
  `metrics_to_bundle` so the key is live. When no cause is named, `cause_block`
  appends one `NEWS (class), <age> ago - outlet: headline` line for the freshest
  fetched item -- one line, not a dump, class-labelled so the reader can see the
  system read the headline and judged it background rather than missing it. No
  headline at all still shows no line. `build_ai_facts` reads the same
  `cause_block`, so the model sees the fallback headline too.
- **Where:** `src/penny/strategies/news_cause.py` (`CAUSE_MAX_AGE_H`,
  `extract_cause`), `src/penny/strategies/base.py` (`Bundle.cause_max_age_h`),
  `src/penny/strategies/wiring.py` (`metrics_to_bundle`), `src/penny/alerts.py`
  (`cause_block`, `_news_fallback`), `src/penny/config.py` +
  `config.env.example` (`CAUSE_MAX_AGE_H=24`).
- **Why it matters:** "no news" is the most dangerous reading a small-cap alert
  can give, and the system was producing it while holding the news. A naming
  window shorter than the horizon the rest of the code reasons about, a config
  key nothing reads, and a render path with no room for a non-cause headline
  stacked into the same false negative. Any one alone would have hidden the PR.

## C45. The news was merged from other feeds, and the alert could cite a story that was not the ticker's

- **Question:** the MNDR alert named a "Mobile-health Network Solutions ...
  Acquisition of Malaysian AI Data Centers" headline. The scanner had picked up
  MNDR. Why was the cause not simply *MNDR's own* Finviz news, and why could a
  story about a different company appear?
- **Finding:** the news path merged four feeds. `_news()` asked Finviz first but
  then added Alpaca, Finnhub and Google News, and `_finviz_news` fell back to
  Finviz's **market-wide** `/export/news` CSV, which matches on the headline
  string (`sym in title.upper()`). Any headline that merely spelled the ticker --
  a sector roundup, another company's PR that mentions it -- matched and could be
  ranked as the cause. Nothing pinned the cause to the ticker the scanner had
  actually picked up, and nothing bounded it to the current session, so an
  unrelated or stale item could be named.
- **Default adopted:** Finviz is the only news source. `_finviz_news` reads the
  ticker's own quote-page `news-table` (`ticker_news`, keyed by ticker) and never
  falls back to the market-wide CSV; the wire feeds (Alpaca/Finnhub/Google) are
  no longer consulted for the cause. `_news` keeps only items stamped **today**
  (ET) via `_is_today` -- an alert answers "why is it moving now", so a
  prior-session headline is background, not a cause. The `fast` flag is retained
  for callers but no longer changes the source.
- **Also in this pass:** the `DATA MISMATCH` line compared the feed quote (which
  had gone through `normalize_row`, so after hours it carried the After-Hours
  price) against the raw `Price` column (the frozen regular close), so every
  post-market alert printed a false mismatch. `_verify` now normalizes its row
  with the same session, keeping the two on one basis. The simulation double
  learned to honor the row's `Price` so its forced mismatch still exercises the
  check.
- **Where:** `src/penny/enrich.py` (`_news`, `_finviz_news`, `_is_today`,
  `_verify`), `src/penny/simulation.py` (`normalize_row`), `tests/test_news_age.py`,
  `tests/test_finviz.py`.
- **Why it matters:** a cause that is not the ticker's own news is worse than no
  cause: it is a confident, wrong answer, and the alert is read as fact. Pinning
  the source to the ticker's Finviz page and the window to today makes "the
  cause" mean what it says.

## C46. A bare time in Finviz's news table inherited *today*, and the aftermarket price was re-derived

- **Question:** after C45 (Finviz-only, today-only news) the user still saw a
  headline that did not belong to the ticker's current session, and the
  aftermarket price looked like the regular one. Why did the "today only" filter
  not remove the old headline?
- **Finding:** the filter was right, but the parser fed it the wrong date. Finviz
  prints the date **once per day block** -- `Today 02:15AM`, `Oct-12-26 06:02PM`
  -- and a **bare time** (`04:57PM`) on the rows that continue the same day.
  `_finviz_dt` read any text without a month-and-year as *today*, so every row
  after an older dated row was stamped with today's date. A two-day-old headline
  therefore passed `_is_today`, and its age went negative and clamped to `0m`.
  Read against a live AAPL quote page: 100 rows parsed, **98 of them wrongly
  dated today**; with the fix, only the 2 rows that are genuinely today survive.
- **Also in this pass:** the after-hours quote was re-derived as
  `regular_close * (1 + After-Hours Change/100)` instead of using Finviz's own
  `After-Hours Close`. The two agree on paper but the reconstruction can drift
  from what Finviz actually displays; the reader wants the aftermarket quote, so
  `normalize_row` now uses `After-Hours Close` directly. Pre-market was checked
  against live data -- Finviz folds the pre-market print into `Price`/`Change`
  (AAPL's screener row showed the same 342.05 the quote page carried at 05:00 ET),
  so that path is unchanged.
- **Where:** `src/penny/sources/finviz.py` (`parse_finviz_news_table`,
  `_finviz_dt`, `_finviz_time`, `normalize_row`), `tests/test_news_age.py`
  (`test_a_bare_time_inherits_the_day_above_it`,
  `test_only_the_todays_rows_survive_the_filter`), `tests/test_finviz.py`.
- **Why it matters:** the today filter is only as good as the timestamps under
  it. A parser that flattens a day-block to "now" makes stale news look like the
  cause and a real age look like "0m" -- the exact two symptoms the user
  reported.

## C47. Polls faster than the dedupe window kept every symbol's history at one sample

- **Question:** a +230% mover (BKYI, Finviz `Rel Volume 148x`, 20.5M shares
  traded) printed `SETUP: NONE - below the alert bar (volx 0.0x is under 3x)` and
  never alerted. Why was the volume multiple zero on the hottest tape of the day?
- **Finding:** the engine cycles every `UNIVERSE_POLL_SECONDS` (30s), but
  `SAMPLE_SECONDS` was 55. `SymbolState.add_sample` replaces the last sample when
  an update lands *inside* the dedupe window, so at 30s every poll replaced the
  previous one and each symbol's history stayed a **single point** --
  `volume_added(w)` was 0 for every window, `volx` was always `0.0x`, and no
  tier, fast or build gate could ever fire on live data. The simulator never
  caught it because its quotes land ~60s apart, which appends and works; only
  the live cadence was broken. Reproduced in isolation: 30s polls -> `samples=1,
  volx=0.0`; 70s polls -> `samples=40, volx=15.0`.
- **Default adopted:** `Scorer` derives the dedupe window as
  `min(SAMPLE_SECONDS, UNIVERSE_POLL_SECONDS - 2)` so it can never again exceed
  the cadence it is fed at, whatever the two config keys are set to. Sub-sampled
  updates still collapse (anything closer than the window replaces the last
  sample), so the memory bound is unchanged.
- **Where:** `src/penny/scoring.py` (`Scorer.__init__`,
  `Scorer.cycle_interval_floor`), `tests/test_scoring.py`
  (`test_a_poll_faster_than_the_dedupe_window_still_builds_history`).
- **Why it matters:** every threshold in the system is written as a volume
  multiple, so a scorer that cannot build a window does not just miss a setup --
  it lies about *why* ("volx 0.0x" on a 148x tape). The fix is one line of
  policy (the window follows the cadence) rather than a new config key to keep
  in sync by hand.

## C48. Stale Finviz filings sailed through the 5-day window, and nothing vetted the ones that did not

- **Question:** the BKYI card showed FILINGS (last 5 days) containing an
  8-K dated 9/9/2020 and a 4 dated 9/5/2023. Five days ago was 2026-10-08.
  Why did a 2019 filing sit on a card that promised five days?
- **Finding:** Finviz's latest-filings export prints dates as M/D/YYYY
  (9/9/2020), but the filter compared them *lexically* against an ISO cutoff
  ('9/9/2020' < '2026-10-08' is False because '9' > '2'), so every Finviz
  filing of any age passed the filter and was printed as-is. Separately, the
  pipeline never classified what a filing *means*: a 424B offering, a reverse
  split, an active ATM shelf or a resale registration all just rendered as
  text under FILINGS, with no veto anywhere.
- **Fix (Spec 1 + 2.1/2.2, the toxicity firewall phase):**
  - strategies/toxicity.py (new): deterministic filing classification into
    ACTIVE_FINANCING / REVERSE_SPLIT_RECENT / RESALE_OVERHANG / ATM_ACTIVE /
    CLEAN_OR_PASSIVE. 14-day general window, 90-day reverse-split window.
    8-K items 3.02 (unregistered sales) veto outright; 1.01/2.03/2.04 need a
    financing keyword; 5.03/6-K need a reverse-split keyword. S-3 is passive
    unless its text names an ATM program (then ATM_ACTIVE, elevated); an S-1 is
    an offering unless it is a resale. Unparseable dates are skipped, never
    trusted, so a stale row cannot veto on an unreadable age.
  - Gate integration (fail-closed): a HARD VETO class returns
    passed=False, blocked_by=SEC_VETO_* before any scoring or AI call; an
    elevated ATM passes but tags the card [ACTIVE_ATM] and states 0.5x
    sizing. Vetoes are logged to a new screener_decisions table (ts, symbol,
    reason_code, detail, price).
  - sec.filing_text fetches (and caches, 24h) a filing's primary document so
    the keyword scan has real text; only the veto-watch forms pay for it, only
    the newest four per candidate.
  - Enrichment widens to the 14-day window, normalises both date formats to
    ISO before comparing, and attaches filing text for the watch forms.
  - FINRA daily short volume is gone from short_interest, short_text and
    the alert card (kept: Finviz short float, short ratio -- different data,
    still live). The FinraClient stays wired for the doctor only.
  - SHORT/FLOAT line now carries live float turnover (	urnover Nx) and
    Finviz's time-of-day RVOL (RVOL Nx, stored on Metrics as 
el_volume).
    An EDGAR: line renders the class on Alert 2; the alert1/2 STRATEGY block
    prints lagged: ACTIVE_ATM.
- **Where:** src/penny/strategies/toxicity.py (new),
  src/penny/strategies/gate.py (SEC veto after DILUTION, warnings on every
  return), src/penny/strategies/base.py (gate warnings),
  src/penny/sources/sec.py (iling_text), src/penny/enrich.py
  (_filings 14d + normalisation + text; _short/short_text FINRA purge),
  src/penny/scoring.py (Metrics vg_volume/
el_volume),
  src/penny/alerts.py (EDGAR line, turnover/RVOL bits, ai facts
  sec_toxicity/loat_turnover/	od_rvol),
  src/penny/engine.py (_note_gate_reject -> screener_decisions,
  _possible_split_artifact on the 90-day classifier),
  src/penny/store.py (screener_decisions table),
  	ests/test_toxicity.py (17 tests).
- **Why it matters:** a +230% mover carrying a 2019 8-K on its card was two
  failures in one: the reader was told 'recent filings' that were not recent,
  and nothing in the pipeline could stop an alert on a symbol that had just
  priced an offering or split 1-for-60. Now the window is honest and the veto
  is fail-closed, deterministic and logged with a reason code.

## C49. "Which part of the arc is this?" had no answer, and "armed" was never distinguished from "enter"

- **Question:** the card said a number went up, but never *where in the move* it
  was -- coiling, breaking out, vertical, or already rolling over -- so a
  front-side breakout and a blow-off top read identically. And there was no
  line between "this could be a long" and "the tape just confirmed it": a gate
  pass fired the alert on the same tick.
- **Finding:** the pipeline had no single label for the shape of the move.
  `Metrics.phase` (BUILDING/EXTENDED/FADING) is a score-relative tag, not a
  supernova shape, and the exhaustion blocker is the only thing that ever
  refused a spent move -- so a symbol 8% off its high with volume still
  accelerating could be presented as "back side" by the pulse line even though
  the tape was alive. Downstream, `entry_trigger` named a pattern but nothing
  required the pattern to *close* before the alert went out, which is the
  definition of chasing a sign.
- **Fix (Spec 2.3/2.4/3, the strategy-engine phase):**
  - `strategies/supernova.py` (new): `SupernovaPhaseClassifier` names exactly
    one of PRE_BREAKOUT / FRONT_SIDE / PARABOLIC / BLOW_OFF_CRASH / BACK_SIDE /
    FORMER_SUPERNOVA from price, % off the high, acceleration, 3m volume
    multiple and VWAP side. It is a pure, total function (a fallback, never an
    "unknown") so a dangerous tape can never go unlabelled.
  - Telemetry guard (2.4): a reading that is off the high *but still
    accelerating* is contradictory ("back side" cannot accelerate), so the
    classifier resolves it to PARABOLIC and sets `guard_fired`; the card's
    `PHASE:` line and `telemetry_guard()` never render a BACK_SIDE/
    BLOW_OFF_CRASH/FORMER_SUPERNOVA phase as the front of the tape.
  - `strategies/confirmation.py` (new): the two-stage ARMED -> ENTER machine.
    Stage 1 arms a passing setup (only from PRE_BREAKOUT/FRONT_SIDE) with a
    trigger just above the session high. Stage 2 confirms on a CLOSED 1m bar --
    primitive A (break & hold: close above the trigger, low held it), B
    (reclaim: wicked under, opened under, closed back above) or C (A or B with
    the bar's volume at/above the 3-bar mean). A ≤4% chase cap and a TTL bound
    it. Verdicts are ARMED / ENTER / NO_TRADE / REJECT -- there is no WAIT.
  - CONTINUATION vs REVERSAL is decided once, at arming, from the gate's
    winning mechanic, and carried on the single `trade_class`. It never flips:
    exactly one class per setup, `v_reversal` (and a liquidity-grab primary with
    no trigger) names REVERSAL, everything else CONTINUATION.
  - Wiring computes the phase once per gate evaluation (`Bundle.supernova_phase`,
    `Bundle.accel`); alerts render a `PHASE:` line on both cards and the AI
    facts carry `supernova_phase`.
  - Engine `_armed_decision()` drives the machine behind `ARMED_CONFIRM`
    (default 0, so the single-shot path is byte-for-byte unchanged); decisions
    are logged to `screener_decisions` as `PHASE_ARMED`/`PHASE_ENTER`/
    `PHASE_NO_TRADE`/`PHASE_REJECT`. `ARMED_CHASE_PCT` and `ARMED_TTL_S` tune it.
- **Where:** src/penny/strategies/supernova.py (new),
  src/penny/strategies/confirmation.py (new),
  src/penny/strategies/base.py (Bundle.accel/supernova_phase),
  src/penny/strategies/wiring.py (classify in metrics_to_bundle),
  src/penny/strategies/__init__.py (exports),
  src/penny/alerts.py (supernova_line on alert 1/2, ai facts supernova_phase),
  src/penny/engine.py (_armed/_armed_decision/_note_armed, format_alert1 bundle),
  src/penny/config.py (ARMED_CONFIRM/ARMED_CHASE_PCT/ARMED_TTL_S),
  tests/test_supernova.py (28 tests).
- **Why it matters:** the system can now say *where* in the arc a name is, and
  can refuse to call a blow-off top a setup. The two-step confirmation turns
  "wait" into a decision -- either the tape closes above the level inside the
  chase cap, or the setup is dropped -- which is the whole difference between
  reacting to a move and chasing a sign.

## C50. The phase maths was approximate, and "confirm" had no shared definition

- **Question:** the supernova phase and the ARMED->ENTER primitives had to be
  *exact*: a phase rule that rounds differently on two machines is not a rule,
  and a confirmation the alert path and the tests compute differently is not a
  gate.
- **Finding:** the phase thresholds and the confirmation primitives were stated
  in prose ("> ~35% above the EMA", "roughly double volume"), which is
  unimplementable exactly and untestable. There was also no shared place for the
  pure math the phase, the confirmation and the risk layer all need.
- **Fix (Spec 2.3/3.1):** `strategies/indicators.py` (new) holds the pure,
  I/O-free primitives -- EMA, time-of-day volume fraction, float turnover, range
  position, structural overhead levels. `supernova.py` is rewritten to the exact
  spec maths: PARABOLIC at `>35%` above the 1m EMA9 *or* `>45%` above VWAP;
  BACK_SIDE at `>15%` below HOD; FORMER_SUPERNOVA by days/weeks off the high.
  `confirmation.py` implements primitives A/B/C to the letter: A = volume
  `>=2.0x` the **5-bar** average **and** `close>open` **and** the close in the
  upper 50% of the bar's range; B = `bar[-1].low > bar[-2].low`; C =
  `bar[-1].close > trigger` within the `<=4%` chase cap.
- **Where:** src/penny/strategies/indicators.py (new),
  src/penny/strategies/supernova.py, src/penny/strategies/confirmation.py,
  tests/test_supernova.py.
- **Why it matters:** the phase and the confirmation are now the same numbers
  everywhere, which is what makes the two-stage machine trustworthy rather than
  approximately right.

## C51. `trade_class` was a label, not a decision, and a reversal had no qualifier

- **Question:** Spec 3.2 wants CONTINUATION and REVERSAL to be mutually
  exclusive, decided once, and each with its own hard-veto. Nothing measured
  whether that held.
- **Finding:** the mode was carried as a free `trade_class` string set in more
  than one place, so a later stage could disagree with the arming stage. And no
  detector qualified a *reversal* candidate at all -- a morning panic flush worth
  buying on the turn had no way to arm.
- **Fix (Spec 3.2):** the mode is decided once at the gate
  (`gate.confirmation_mode`) and read by `trade_class_for`; name->mode registries
  live in `confirmation.py`; a new `morning_panic_dip_buy` reversal qualification
  detector joins the gate. CONTINUATION and REVERSAL now cannot both be true,
  and each carries its own veto.
- **Where:** src/penny/strategies/confirmation.py, src/penny/strategies/gate.py,
  src/penny/strategies/detectors.py, tests/test_supernova.py.
- **Why it matters:** a continuation and a reversal are different trades with
  different risk; conflating them is how a machine buys a falling knife.

## C52. Every setup was sized the same, whatever the driver

- **Question:** Spec 3.3 wants position size to answer to the *kind* of catalyst:
  a name with a real, dated driver is worth full size; a pure-tape mover is not.
- **Finding:** the gate passed or blocked; it never graded the catalyst, so a
  cause-free breakout and a contract-win breakout took the same size.
- **Fix (Spec 3.3):** `strategies/routing.py` (new) computes a catalyst tier
  (1/2/3) and a size multiplier (1.0/0.75/0.5) in `metrics_to_bundle`; the plan's
  shares are scaled on a copy; Tier-3 continuations are vetoed after 10:30 ET;
  both cards render a `TIER:` line and the AI facts carry `catalyst_tier`.
- **Where:** src/penny/strategies/routing.py (new),
  src/penny/strategies/wiring.py, src/penny/alerts.py, tests/test_supernova.py.
- **Why it matters:** sizing is the only risk control this tool has, so it has to
  reflect how real the driver is, not just that a move happened.

## C53. A percentage target is a ghost, and the stop was arbitrary

- **Question:** Spec 4 deletes T1/T2 and asks for structural levels: real
  overhead supply above the entry, a stop that sits under a real bar, and a cap
  on how wide that stop may be.
- **Finding:** the plan printed fixed-percentage T1/T2 targets -- numbers that
  existed because a grid said so, not because the tape put supply there -- and
  the stop came off a fixed ratio rather than the last bar.
- **Fix (Spec 4):** `strategies/levels.py` (new) owns level suppression (no
  levels on a blocked gate), structural stops (CONTINUATION under the last bar's
  low, REVERSAL under the flush bar's low), a `STOP_TOO_WIDE` refusal at 15%/30%,
  and the early-failure + ATR-trailing exit rules. `TradePlan` now carries
  `overhead` (a real resistance list) instead of `target1`/`target2`; the plan is
  attached in `run_gate` via `apply_structural_plan`; tracking maps the first two
  overhead levels onto the existing `plan_t1`/`plan_t2` columns.
- **Where:** src/penny/strategies/levels.py (new), src/penny/fib.py,
  src/penny/strategies/wiring.py, src/penny/enrich.py, src/penny/tracking.py,
  src/penny/ai/decision.py, tests/test_supernova.py.
- **Why it matters:** a plan you can actually trade has a stop the tape defined
  and a target the tape can reach; a percentage grid is a decoration.

## C54. "WAIT" was on the menu, and a screening outage looked like a pass

- **Question:** Spec 5 bans `WAIT` (a hedge is not a verdict) and wants the
  deterministic decision to lead and the model to follow as an asynchronous red
  team. It also wants a Shariah outage to say so.
- **Finding:** the verdict vocabulary was `ENTER/WAIT/PASS`, so a report could
  decline to decide; and when the Shariah service could not be reached the label
  was `UNKNOWN`, which reads like "screened and inconclusive" when the truth is
  "could not screen".
- **Fix (Spec 5):** the vocabulary is `ENTER/ARMED/NO_TRADE/REJECT` (there is no
  `WAIT`); a pure `deterministic_verdict()` router returns the rules' decision
  with no I/O in the hot path, and the LLM only runs afterwards as a red team
  that can downgrade, never gate. Shariah `combine` returns `UNVERIFIED` on an
  all-failed set and renders `UNVERIFIED (Service Offline)`; an outage never
  suppresses an alert in `only_compliant` mode.
- **Where:** src/penny/ai/decision.py, src/penny/ai/gateway.py,
  src/penny/strategies/confirmation.py, src/penny/shariah.py,
  src/penny/tracking.py, src/penny/prompts/methodology_v2.md,
  tests/test_decision.py, tests/test_shariah.py, tests/test_supernova.py.
- **Why it matters:** a decision support tool that is allowed to say "wait" has
  not made a decision, and a network outage must never be mistakable for a
  compliance pass.

## C55. The alert printed blocks the reader had to interpret

- **Question:** Spec 6 fixes the exact Telegram layout: a fixed section order,
  no ghost blocks, and the exits stated as a structural stop and real overhead.
- **Finding:** the layout had drifted -- targets could still render as T1/T2, and
  the decision exit line named percentages rather than structure.
- **Fix (Spec 6):** the AI DECISION block renders `STOP (structural)` and
  `OVERHEAD`, never T1/T2; the section order (WHY IT MOVED -> evidence ->
  AI DECISION) is asserted; and no TECH/FIB/PLAN/target block appears anywhere.
- **Where:** src/penny/ai/decision.py, src/penny/alerts.py,
  tests/test_alerts.py, tests/test_decision.py.
- **Why it matters:** the reader decides in seconds on a phone, so the layout is
  a decision about what they have time for -- and a ghost target is noise.

## C56. The two-stage machine shipped off by default, then turned on

- **Question:** the Spec 3 two-stage ARMED->ENTER flow was implemented behind
  `ARMED_CONFIRM=0`. Once the refactor was verified green, the decision was to
  make it the live default rather than a comparison mode.
- **Finding:** every spec feature except the state machine was already the
  default code path; `ARMED_CONFIRM` was the one live-behaviour switch still off.
  The simulation and two gate-wiring tests assumed the single-shot path.
- **Fix:** `ARMED_CONFIRM=1` by default (Spec 3 is the live behaviour); the
  simulation harness pins the one-step path where it is testing the deterministic
  fire, and gains end-to-end two-stage checks (a pass arms, a closed confirming
  bar routes to ENTER, a blocked gate routes to REJECT).
- **Where:** src/penny/config.py, src/penny/engine.py, src/penny/simulation.py,
  tests/test_gate_wiring.py, tests/test_supernova.py.
- **Why it matters:** a setup is not an entry. The default now behaves the way
  the specification intends, and the comparison path is still one flag away.

