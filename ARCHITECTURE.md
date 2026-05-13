# Trading Agent — Architecture & Function Reference

A fundamentals-based autonomous trading agent for Trading 212. Uses Claude Sonnet
weekly for analysis and Claude Opus monthly for strategic critique. ~£5,000 demo
capital; no real money at risk.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Weekly Run — Step by Step](#weekly-run--step-by-step)
3. [Monthly Deep Review](#monthly-deep-review)
4. [File Reference](#file-reference)
   - [trading_agent.py](#trading_agentpy)
   - [shadow_portfolio.py](#shadow_portfoliopy)
   - [t212_executor.py](#t212_executorpy)
5. [Key Design Decisions](#key-design-decisions)
6. [Data Flow Diagram](#data-flow-diagram)

---

## System Overview

```
trading_agent.py      — main orchestrator (run this weekly)
shadow_portfolio.py   — ledger engine: valuation, sync, apply trades
t212_executor.py      — T212 demo order execution + ticker translation
shadow_portfolio.json — live ledger state (auto-updated each run)
t212_instruments.json — cached T212 instrument list (24h cache)
.env                  — secrets (never commit)
```

The agent has two portfolios running in parallel:

| Portfolio | What it represents |
|-----------|-------------------|
| **Shadow** | Paper ledger — every Claude recommendation applied, regardless of execution. Tracks "what would have happened if everything worked". |
| **T212 demo** | What actually executed. T212 is the source of truth when `T212_DEMO_EXECUTE=true`. |

The bidirectional sync (run at the start of each weekly run) reconciles these two,
using T212 as the authority. This prevents phantom positions building up in shadow
when T212 rejects an order.

---

## Weekly Run — Step by Step

```
python trading_agent.py
```

| Step | Function | What happens |
|------|----------|--------------|
| 1 | `validate_config()` | Check all env vars are set — fail fast before any API calls |
| 2 | `fetch_t212_account()` | GET /equity/account/summary + /equity/positions |
| 3 | `sp.load_ledger()` | Read shadow_portfolio.json from disk |
| 4 | `sync_shadow_with_t212()` | Reconcile shadow ↔ T212; build live price map |
| 5 | `sp.valuation()` | Mark shadow to market (pre-trade) |
| 6 | `get_claude_recommendations()` | Build prompt → call Claude Sonnet + web search → extract JSON |
| 7 | `execute_and_apply_trades()` | T212 executes first → shadow mirrors confirmed trades only |
| 8 | `sp.valuation()` | Mark shadow to market (post-trade) |
| 9 | `sp.snapshot()` + `sp.save_ledger()` | Append weekly snapshot; persist ledger |
| 10 | `build_weekly_email_body/subject()` | Assemble the report email |
| 11 | `send_email()` | Send via Gmail SMTP SSL |

---

## Monthly Deep Review

Runs automatically on the first Sunday of each month, or manually with `--deep-review`.

```
python trading_agent.py --deep-review          # runs both weekly + deep review
python trading_agent.py --deep-review --skip-weekly  # deep review only
```

| Step | Function | What happens |
|------|----------|--------------|
| 1 | `fetch_deep_review_price_map()` | Get T212 live prices for current valuation |
| 2 | `sp.valuation()` | Mark portfolio to market |
| 3 | `check_deep_review_prerequisites()` | Skip if < 2 snapshots or < 3 trades |
| 4 | `run_deep_review()` | Build Opus prompt → call Claude Opus |
| 5 | `build_deep_review_email_*()` | Assemble critique email |
| 6 | `send_email()` | Send |

---

## File Reference

---

### trading_agent.py

The top-level orchestrator. Imports `shadow_portfolio` as `sp` and `t212_executor` as `t212ex`.

#### Config

| Name | Source | Purpose |
|------|--------|---------|
| `T212_API_KEY` | `.env` | T212 API key |
| `T212_API_SECRET` | `.env` | T212 API secret (used for Basic auth) |
| `T212_ENV` | `.env` | `"demo"` or `"live"` |
| `T212_BASE_URL` | derived | Base URL for T212 API (set from T212_ENV) |
| `ANTHROPIC_API_KEY` | `.env` | Anthropic API key |
| `CLAUDE_MODEL_WEEKLY` | `.env` | Model for weekly runs (default: `claude-sonnet-4-6`) |
| `CLAUDE_MODEL_DEEP` | `.env` | Model for monthly review (default: `claude-opus-4-7`) |
| `EMAIL_SENDER` | `.env` | Gmail address to send from |
| `EMAIL_APP_PASSWORD` | `.env` | Gmail App Password (16 chars) |
| `EMAIL_RECIPIENT` | `.env` | Address to send reports to |

#### Helpers

---

**`_clean(s)`**

Strips whitespace, quotes, and non-ASCII characters from a string.
HTTP headers must be latin-1 encodable — this prevents Unicode errors from
copy-paste artefacts (smart quotes, invisible chars) in API keys read from `.env`.

---

#### T212 API Client

---

**`t212_headers()`** → `dict`

Builds the `Authorization` header for T212 API requests.
T212 uses Basic auth with a base64-encoded `key:secret` pair.
Falls back to a legacy single-token header if `T212_API_SECRET` is not set.

---

**`get_t212_cash()`** → `dict`

`GET /equity/account/summary` — returns cash balances.
Key fields: `free` (available to trade), `total` (total account value), `currency`.

---

**`get_t212_positions()`** → `list`

`GET /equity/positions` — returns all open positions.
The current T212 API uses a flat structure (`{"ticker": "AAPL_US_EQ", ...}`).
Older versions used nested `{"instrument": {"ticker": ...}}`. Both are handled.

---

**`fetch_t212_account()`** → `(dict, list)`

Convenience wrapper: calls `get_t212_cash()` + `get_t212_positions()` and logs
a summary line. Returns `(t212_cash, t212_positions)`.

---

**`extract_t212_totals(t212_cash)`** → `(float, float)`

Safely extracts `(total_account_value_gbp, available_cash_gbp)` from the T212
summary dict. Tries multiple field names to handle API version changes.
Defaults to `(0.0, 0.0)` if fields are absent or non-numeric.

---

#### Shadow ↔ T212 Sync

---

**`sync_shadow_with_t212(ledger, t212_cash, t212_positions)`** → `dict`

Orchestrates the bidirectional sync between shadow and T212.

1. Loads the T212 instrument list (24h cached).
2. Builds a live price map from T212 position data.
3. Fetches pending buy orders (so queued positions aren't wrongly removed).
4. Calls `sp.sync_from_t212()` to reconcile the ledger.
5. Saves the ledger if anything changed.

Returns the live price map `{yf_ticker: price_data}`, or `{}` on error
(valuation then falls back to yfinance for all tickers).

---

#### Claude Prompts

---

**`ANALYSIS_PROMPT`** (module-level constant)

The weekly analysis prompt template. Contains:
- Strategy constraints (fundamentals only, position limits, cash rules)
- Mandatory deployment rules (forces cash deployment if > £750 idle)
- Placeholder sections filled by `build_prompt()`: shadow state, T212 snapshot,
  trade history, thesis accountability, today's date
- Output format instructions (prose report + JSON block)
- yfinance ticker rules for each major exchange

---

**`DEEP_REVIEW_PROMPT`** (module-level constant)

The monthly Opus critique prompt template. Contains:
- Six structured review sections (performance, adherence, biases, thesis quality,
  strategic recs, kill criteria)
- The full ledger JSON and weekly snapshots as context

---

**`build_prompt(shadow_val, shadow_ledger, t212_cash, t212_positions)`** → `str`

Constructs the weekly analysis prompt by filling in `ANALYSIS_PROMPT` with:
- Condensed shadow portfolio state (cash, returns, positions)
- Condensed T212 account snapshot (avoids bloating context with raw API responses)
- Last 15 shadow trades
- Thesis accountability section from `sp.build_thesis_review()`

---

**`build_deep_review_prompt(ledger, valuation)`** → `str`

Constructs the monthly Opus critique prompt by filling in `DEEP_REVIEW_PROMPT` with
the full ledger JSON, all weekly snapshots, and current valuation.

---

#### Claude API Calls

---

**`call_claude(prompt, model)`** → `str`

Sends a prompt to Claude with the `web_search` tool enabled. Joins all text
content blocks in the response and returns them as a single string.

Automatically retries on HTTP 429 (rate limit) with exponential backoff:
60s → 120s → 240s (up to 4 attempts). Other errors are re-raised immediately.

---

**`extract_recommendations(text)`** → `list`

Parses the `\`\`\`json ... \`\`\`` block from Claude's response using regex.
Returns the `recommendations` array, or `[]` if no valid block is found.
Never crashes the run — parse failures are logged and return empty list.

---

**`strip_json_block(text)`** → `str`

Removes the `\`\`\`json ... \`\`\`` block from Claude's response.
The JSON is for machine consumption; the email body shows only the prose.

---

**`get_claude_recommendations(pre_val, ledger, t212_cash, t212_positions)`** → `(str, list)`

Composes the prompt, calls Claude, extracts recommendations.
Returns `(full_response_text, recommendations_list)`.

---

#### Trade Execution

---

**`execute_and_apply_trades(ledger, recs, run_date)`** → `(list, list)`

Implements the T212-first execution order:
1. `t212ex.execute_recommendations(recs)` → places real T212 orders
2. `sp.apply_recommendations(confirmed_recs)` → mirrors ONLY confirmed trades to shadow

Returns `(t212_events, shadow_events)` — both are lists of human-readable strings.

**Why T212 first?** In the old design, shadow applied all recs then T212 executed.
If T212 failed (bad ticker, insufficient funds), the position appeared in shadow
permanently until next week's sync cleaned it out. T212-first prevents this drift.

---

#### Monthly Deep Review

---

**`run_deep_review(ledger, valuation)`** → `str`

Calls `build_deep_review_prompt()` then `call_claude()` with `CLAUDE_MODEL_DEEP`.
Returns Opus's full prose critique.

---

**`is_first_sunday_of_month(d)`** → `bool`

Returns `True` if `d` is the first Sunday of its month (weekday=6, day≤7).
Used to auto-trigger the monthly deep review without needing the `--deep-review` flag.

---

**`fetch_deep_review_price_map()`** → `dict`

Fetches T212 live prices for the deep review valuation (no sync step).
Returns `{}` on any error — `sp.valuation()` then uses yfinance as fallback.

---

**`check_deep_review_prerequisites(ledger)`** → `bool`

Returns `False` (and prints a skip message) if the ledger has fewer than
2 weekly snapshots or fewer than 3 trades. Opus needs real data to be useful.

---

#### Email Building

---

**`build_weekly_email_body(started, post_val, t212_cash, t212_positions, shadow_events, t212_events, prose)`** → `str`

Assembles the full weekly report email: metadata header, shadow performance
summary, T212 account snapshot, trade events log, T212 order events, Claude's prose.

---

**`build_weekly_email_subject(started, t212_total, post_val)`** → `str`

Builds the subject line with T212 value and shadow return so key numbers
are visible in the inbox without opening the email.
Example: `Trading Review — 11 May 2026 | T212: £5517 | Shadow: £5119 (+2.38%)`

---

**`build_deep_review_email_body(started, val, critique)`** → `str`

Assembles the monthly critique email: metadata header, valuation summary, Opus critique.

---

**`build_deep_review_email_subject(started, val)`** → `str`

Builds the subject line for the monthly critique email.

---

#### Email Sending

---

**`send_email(subject, body)`**

Sends plain-text email via Gmail SMTP SSL (port 465).
Requires a Gmail App Password — a 16-character code from Google Account →
Security → App Passwords. This is NOT the account password.

---

#### Orchestration

---

**`validate_config()`**

Checks all required env vars are non-empty before any API call.
Raises `RuntimeError` with a clear message listing what's missing.

---

**`run_weekly(started)`**

Main weekly orchestration. Calls all the step functions in order (see table above).
The `started` datetime is used for `run_date` trade labels and email timestamps.

---

**`run_monthly_deep_review(started)`**

Monthly critique orchestration. Loads ledger, values portfolio, checks prerequisites,
calls Opus, sends email.

---

**`main()`**

CLI entry point. Parses `--deep-review` and `--skip-weekly` flags, calls
`validate_config()`, dispatches to `run_weekly()` and/or `run_monthly_deep_review()`.

---

### shadow_portfolio.py

The ledger engine. Stores portfolio state in `shadow_portfolio.json` and handles
valuation, bidirectional sync, and applying trade recommendations.

All monetary values are in GBP. Positions are keyed by yfinance ticker.

#### Ledger structure

```json
{
  "created": "2026-04-22T18:00:00",
  "starting_capital_gbp": 5000.0,
  "benchmark_ticker": "VUSA.L",
  "benchmark_start_price_gbp": 85.43,
  "cash_gbp": 623.50,
  "positions": {
    "AAPL": {
      "shares": 2.847,
      "avg_cost_gbp": 175.23,
      "first_bought": "2026-04-22",
      "thesis": "Services margin expansion + buyback cadence."
    }
  },
  "trades": [ ... ],
  "weekly_snapshots": [ ... ]
}
```

#### Persistence

---

**`_default_ledger()`** → `dict`

Returns a fresh ledger dict. Called when `shadow_portfolio.json` doesn't exist yet.
`benchmark_start_price_gbp` is `None` — recorded on the first real run so the
benchmark comparison starts from the same date as the portfolio.

---

**`load_ledger()`** → `dict`

Loads `shadow_portfolio.json` from disk, or returns `_default_ledger()` if absent.

---

**`save_ledger(ledger)`**

Writes the ledger dict to `shadow_portfolio.json` as indented JSON.

---

#### Bidirectional Sync

---

**`sync_from_t212(ledger, t212_cash, t212_positions, t212_to_yf_fn, bidirectional, pending_yf_tickers)`** → `bool`

Reconciles shadow ↔ T212. Three operations:

| Operation | When | Why |
|-----------|------|-----|
| **ADD** missing positions | Always | T212 holds a stock shadow doesn't know about (manual trade, or ledger reset) |
| **REMOVE** extra positions | `bidirectional=True` only | Shadow has a stock T212 doesn't — the T212 order failed but shadow wrote it anyway |
| **SYNC** cash | `bidirectional=True` only | Keep shadow cash in sync with T212's actual available balance |

`pending_yf_tickers` are excluded from removal — a pending buy order means the
position is queued for the next market open, not failed.

Returns `True` if any changes were made (so the caller knows to save).

---

#### Price Fetching

---

**`_fx_rate(pair)`** → `float | None`

Fetches a GBP FX rate from yfinance (e.g. `"GBPUSD=X"`) with an in-process cache.
The cache lasts for one run only — rates are always fresh per run, but one USD
price fetch doesn't trigger five separate GBPUSD=X network calls.

---

**`fetch_price_gbp(yf_ticker)`** → `float | None`

Fetches the latest price for a ticker and converts to GBP.
Handles: GBP (direct), GBX/pence (÷100), USD (÷GBPUSD), EUR (÷GBPEUR).
Unknown currencies are returned as-is with a warning.

---

#### Applying Recommendations

---

**`_apply_buy(ledger, rec, ticker, price, run_date)`** → `str`

Applies a single BUY to the shadow ledger.
- Validates amount > 0 and sufficient cash
- Opens a new position or adds to existing (recalculates weighted average cost)
- Appends thesis history for add-to-existing: `"original | [date] new thesis"`
- Deducts cash and logs the trade
- Returns event string or `"SKIP BUY ..."` message

---

**`_apply_sell_or_trim(ledger, rec, ticker, action, price, run_date)`** → `str`

Applies a single SELL or TRIM to the shadow ledger.
- SELL: liquidates the full position
- TRIM: sells `trim_pct`% (defaults to 50% if not specified)
- Adds proceeds to cash, logs entry and exit thesis
- Deletes position if shares fall below dust threshold (1e-4)
- Returns event string or `"SKIP ..."` message

---

**`apply_recommendations(ledger, recs, run_date)`** → `list[str]`

Iterates all recommendations, fetches the current GBP price for each ticker once,
then delegates to `_apply_buy()` or `_apply_sell_or_trim()`.
Returns a list of human-readable event strings for email reporting.

---

#### Valuation

---

**`_build_t212_price_map(t212_positions, t212_to_yf_fn, instruments)`** → `dict`

Builds `{yf_ticker: {price_native, currency, ppl_gbp, qty}}` from T212 position data.
Using T212 prices avoids the 15-20 minute yfinance quote delay and FX mismatch.

---

**`_native_to_gbp(price, currency)`** → `float | None`

Converts a native currency price to GBP using live FX rates.
Covers: GBP, GBX (pence), USD, EUR, CAD, AUD, JPY, HKD, CHF.
Unknown currencies are returned as-is with a logged warning.

---

**`_value_position(ticker, pos, t212_price_map)`** → `dict`

Marks a single position to market. Tries T212 live price first, falls back to
yfinance. Returns a sub-dict with shares, avg cost, current price, P&L £ and %.
Returns `None` for price fields if the price is completely unavailable.

---

**`valuation(ledger, t212_price_map)`** → `dict`

Marks the entire shadow portfolio to market. Calls `_value_position()` for each
holding, sums position values, computes total return vs starting capital, and
compares against the benchmark (VUSA.L via yfinance).

Returns a comprehensive snapshot dict used by both the email formatter and as
context in the Claude prompt.

---

#### Snapshots and Reporting

---

**`snapshot(ledger, val, run_date)`**

Appends a weekly snapshot entry to `ledger["weekly_snapshots"]`.
Snapshots are the historical record used by the monthly Opus deep review.

---

**`build_thesis_review(ledger, current_val)`** → `str`

Builds the thesis accountability section for the Claude weekly prompt.
For each held position: shows original thesis and current P&L.
For last 5 exits: shows entry thesis vs exit reason.
This forces Claude to explicitly evaluate whether its prior reasoning held up
before making new recommendations.

---

**`format_valuation_for_email(val)`** → `str`

Formats the portfolio valuation as plain-text for the email body.
Includes starting capital, current value breakdown, total return, benchmark
comparison, and per-position detail.

---

### t212_executor.py

The T212 bridge. Translates yfinance tickers to T212 format, places market orders,
and manages the instrument cache. Enabled via `T212_DEMO_EXECUTE=true`.

#### Ticker Translation

The translation from yfinance format to T212 format is the most complex part of the
system. T212 uses `AAPL_US_EQ`, `SHEL_EQ`, `SAP_DE_EQ` — yfinance uses `AAPL`,
`SHEL.L`, `SAP.DE`. The translation pipeline has 5 stages:

```
Stage 1: Manual alias       → exact override (e.g. BRK.B → BRK.B_US_EQ)
Stage 2: shortName match    → T212's own symbol label (most reliable)
Stage 3: Root symbol match  → ticker.split("_")[0].upper()
Stage 4: Currency filter    → keep only instruments with expected currencyCode
Stage 5: Exchange heuristic → US tickers prefer _US_EQ; others prefer non-US
```

---

**`YF_SUFFIX_TO_CURRENCIES`** (module-level dict)

Maps every recognised yfinance exchange suffix to the currency or currencies T212
uses for that exchange. E.g. `"L" → {"GBP", "GBX", "GBp"}` for London Stock Exchange.
This is the core data structure for the currency-matching stages.

---

**`TICKER_ALIASES`** (module-level dict)

Manual overrides for tickers the automatic translation can't handle. Only add entries
here when you've confirmed the exact T212 ticker exists. Wrong aliases cause 404s.

---

#### Auth

---

**`_headers()`** → `dict`

Builds `Authorization` + `Content-Type` headers for T212 API requests.
Same Basic auth logic as `t212_headers()` in `trading_agent.py`.

---

#### Instrument Lookup

---

**`_load_instruments()`** → `list[dict]`

Fetches all tradable instruments from T212 (`/equity/metadata/instruments`) with a
24h local cache in `t212_instruments.json`. T212 has ~17,000 instruments — fetching
on every run is slow and unnecessary; the list changes infrequently.

---

#### Ticker Parsing Helpers

---

**`_parse_yf_ticker(yf_ticker)`** → `(str, str)`

Splits a yfinance ticker into `(root_symbol, exchange_suffix)`.
Splits on the last dot only when the suffix is a recognised exchange code in
`YF_SUFFIX_TO_CURRENCIES`. This prevents treating `BRK.B` as root=`BRK`, suffix=`B`.

---

**`_instrument_currency(inst)`** → `str`

Extracts the currency code from a T212 instrument dict.
Tolerates field name variations (`currencyCode` vs `currency`).

---

**`_instrument_root(inst)`** → `str`

Extracts the root symbol from a T212 instrument's ticker string.
Handles `AAPL_US_EQ → AAPL`, `META.US_EQ → META`, `BRK.B_US_EQ → BRK.B`.

---

#### Translation

---

**`yf_to_t212_ticker(yf_ticker, instruments)`** → `str | None`

Full 5-stage translation pipeline from yfinance ticker to T212 ticker.
Returns `None` if no match found after all stages.

---

**`t212_to_yf_ticker(t212_ticker, instruments)`** → `str | None`

Reverse translation: T212 ticker → yfinance ticker.
Used by the sync logic to convert T212 position tickers to yfinance format
for price lookups and shadow ledger keying.

---

#### Account State

---

**`get_pending_buy_tickers(instruments, t212_to_yf_fn)`** → `set[str]`

Returns yfinance tickers with open (unfilled) buy orders in T212.
Used by `sync_from_t212()` to avoid removing positions whose order is merely
queued (placed outside market hours), not failed.
Returns `set()` on any API error — callers degrade gracefully.

---

**`get_t212_positions_map()`** → `dict`

Returns `{t212_ticker: position_dict}` for all open positions.
Used during order execution to look up current quantities for SELL/TRIM orders.

---

#### Order Placement

---

**`_place_market_order(t212_ticker, quantity)`** → `dict`

Places a market order on T212. Positive quantity = BUY, negative = SELL.
`extendedHours: True` allows orders to queue for the next market open when placed
outside trading hours.

---

**`_is_valid_ticker(yf_ticker)`** → `bool`

Rejects malformed tickers (double dots like `BA..L`, empty strings, length > 15,
non-alphanumeric characters). Guards against garbled Claude output.

---

#### Execution Helpers

---

**`_search_translate(yf_ticker, instruments)`** → `str | None`

Translation using instrument search only, bypassing `TICKER_ALIASES`.
Used as fallback when an alias returns 404 — the alias may be stale.

---

**`_execute_buy(rec, yf_ticker, t212_ticker, instruments, events, confirmed_recs)`**

Executes a single BUY on T212:
1. Validates amount > 0
2. Fetches current GBP price
3. Calculates shares = amount / price
4. Places market order (with alias-404 retry via `_search_translate()`)
5. On success: appends event string and adds rec to `confirmed_recs`
6. 1.2s sleep between orders to avoid rate limiting

---

**`_execute_sell_or_trim(rec, yf_ticker, t212_ticker, action, positions_map, events, confirmed_recs)`**

Executes a single SELL or TRIM on T212:
1. Looks up current quantity in `positions_map`
2. Calculates `qty_to_sell` (100% for SELL, `trim_pct`% for TRIM)
3. Places negative-quantity market order
4. On success: appends event string and adds rec to `confirmed_recs`

---

**`execute_recommendations(recs)`** → `(list[str], list[dict])`

Main entry point. Iterates all recommendations and for each:
- Validates the ticker format
- Translates yfinance → T212 ticker
- Delegates to `_execute_buy()` or `_execute_sell_or_trim()`
- Catches per-rec errors so one failure doesn't abort the run

Returns `(events, confirmed_recs)`. Only `confirmed_recs` get applied to shadow.

When `T212_DEMO_EXECUTE=false`: returns `([], all_recs)` — shadow-only mode.
Safety guard: refuses to run when `T212_ENV=live`.

---

## Key Design Decisions

### 1. T212-first execution

```
T212 executes FIRST → shadow mirrors only confirmed trades
```

The previous design applied all recommendations to shadow, then T212 executed.
When T212 failed (bad ticker, insufficient funds), the position appeared in shadow
permanently. The bidirectional sync eventually cleaned it up but only on the next
weekly run — one week of phantom position data in the email. T212-first prevents this.

### 2. Bidirectional sync

Shadow positions T212 doesn't hold are **removed** (when `T212_DEMO_EXECUTE=true`).
This is the counterpart to T212-first execution: it catches any drift that slips
through (e.g. if the script crashed between T212 execution and shadow apply).

Positions with pending T212 buy orders are explicitly preserved — a queued order
is not a failure, so removing the shadow position would be wrong.

### 3. T212 live prices for valuation

Shadow uses T212's `currentPrice` for held positions rather than yfinance.
- Eliminates the 15-20 minute quote delay
- Eliminates FX mismatch (T212 knows the exact execution currency)
- yfinance is still used for the benchmark (VUSA.L) and any position not in T212

### 4. Two-model design

| Run | Model | Purpose |
|-----|-------|---------|
| Weekly | Claude Sonnet | Live fundamentals analysis + web search + trade recommendations |
| Monthly | Claude Opus | Strategic critique of the agent itself — not picking trades, assessing whether the strategy works |

Opus is never used for trade recommendations — it's too expensive and overkill for
a simple BUY/SELL decision. It's reserved for the meta-level strategic review.

### 5. Thesis accountability

Every position's original thesis and current P&L is included in the Claude prompt.
Every exit shows entry reasoning vs exit reason. This creates a feedback loop that
forces the model to confront whether its prior reasoning held up before making new
recommendations — without it, Claude tends to repeat picks regardless of performance.

---

## Data Flow Diagram

```
                    ┌─────────────┐
                    │  .env file  │
                    └──────┬──────┘
                           │ config
                           ▼
                   ┌───────────────┐
          ┌────────│ trading_agent │────────┐
          │        └───────────────┘        │
          │                │                │
          ▼                ▼                ▼
   ┌────────────┐  ┌──────────────┐  ┌──────────────┐
   │ T212 API   │  │ Anthropic    │  │ Gmail SMTP   │
   │ /positions │  │ Claude API   │  │ (email out)  │
   │ /summary   │  │ + web search │  └──────────────┘
   │ /orders    │  └──────┬───────┘
   └─────┬──────┘         │ prose + JSON
         │                │
         │   ┌────────────┘
         │   │
         ▼   ▼
   ┌──────────────────┐
   │ shadow_portfolio │◄──── shadow_portfolio.json
   │ (ledger engine)  │────► shadow_portfolio.json
   └──────────────────┘
         │
         ▼
   ┌──────────────────┐
   │  t212_executor   │◄──── t212_instruments.json (24h cache)
   │  (T212 bridge)   │
   └──────────────────┘
         │
         ▼
   ┌────────────┐
   │ T212 API   │
   │ /orders    │
   │ (market    │
   │  orders)   │
   └────────────┘
```
