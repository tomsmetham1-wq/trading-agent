# Trading Agent — Claude Code Context

This is a fundamentals-based autonomous trading agent for Trading 212, built for
a UK retail investor (Tom). ~£5,000 demo capital. No real money at risk yet.

---

## Project structure

```
trading_agent.py       — main orchestrator (run this weekly)
shadow_portfolio.py    — ledger engine: valuation, sync, apply trades
t212_executor.py       — T212 demo order execution + ticker translation
shadow_portfolio.json  — live ledger state (auto-updated each run)
t212_instruments.json  — cached T212 instrument list (24h cache)
sync_watch.py          — file watcher for the old zip-based sync workflow (ignore)
.env                   — secrets (never commit)
```

## How to run

```bash
# Activate venv first
venv\Scripts\activate

# Weekly run (what Task Scheduler does every Sunday at 18:00)
python trading_agent.py

# Force a monthly deep review alongside the weekly run
python trading_agent.py --deep-review

# Run only the deep review, skip weekly (no new trades)
python trading_agent.py --deep-review --skip-weekly
```

## Architecture

### trading_agent.py (orchestrator)
1. Fetches T212 account state (cash + positions)
2. Loads shadow ledger, builds T212 price map for valuation
3. Bidirectional sync: shadow ↔ T212 (T212 is source of truth)
4. Calls Claude Sonnet with web search → gets prose report + JSON recommendations
5. **T212-first execution**: T212 executes first, shadow only mirrors confirmed trades
6. Saves ledger, snapshots, sends email

### shadow_portfolio.py (ledger engine)
- `load_ledger()` / `save_ledger()` — JSON persistence
- `sync_from_t212()` — **bidirectional**: adds missing positions AND removes shadow
  positions not in T212. T212 is the source of truth when T212_DEMO_EXECUTE=true.
- `valuation()` — mark-to-market using T212 live prices (held positions) +
  yfinance fallback (benchmark + new positions)
- `apply_recommendations()` — applies confirmed recs to shadow ledger
- `build_thesis_review()` — builds thesis accountability section for Claude prompt

### t212_executor.py (T212 bridge)
- `execute_recommendations(recs)` — returns `(events, confirmed_recs)` tuple.
  Only confirmed_recs get applied to shadow. This prevents drift on T212 failures.
- `yf_to_t212_ticker()` — translates yfinance tickers to T212 format
- `t212_to_yf_ticker()` — reverse translation (used by sync)
- Ticker translation priority: manual aliases → currency match → exchange heuristic

## Key env vars (.env)

```
T212_API_KEY=...           # T212 API key
T212_API_SECRET=...        # T212 API secret (Basic auth: key:secret base64 encoded)
T212_ENV=demo              # "demo" or "live" — NEVER change to live without careful thought
T212_DEMO_EXECUTE=true     # Set true to mirror shadow trades to T212 demo account
ANTHROPIC_API_KEY=...      # Anthropic API key (same account as Claude.ai)
CLAUDE_MODEL_WEEKLY=claude-sonnet-4-6   # Weekly analysis model
CLAUDE_MODEL_DEEP=claude-opus-4-7       # Monthly deep review model
EMAIL_SENDER=...
EMAIL_APP_PASSWORD=...     # Gmail app password (16 chars, not account password)
EMAIL_RECIPIENT=...
STARTING_CAPITAL_GBP=5000
BENCHMARK_TICKER=VUSA.L   # Vanguard S&P 500 GBP ETF
```

## T212 API quirks

- **Auth**: Basic auth with base64-encoded `key:secret`, NOT bearer token
- **Endpoints**:
  - `/equity/account/summary` — cash + account info
  - `/equity/positions` — current positions
  - `/equity/orders/market` — place market orders
- **Ticker format**: T212 uses `AAPL_US_EQ`, `SHEL_EQ`, `ORCL_US_EQ` etc.
  yfinance uses `AAPL`, `SHEL.L`, `ORCL`. Translation is in `t212_executor.py`.
- **Instrument list**: fetched once and cached in `t212_instruments.json` for 24h.
  16,985 instruments. If translation breaks, check this file first.

## Known ticker issues

- **META**: T212 lists Meta Platforms under the old Facebook ticker `FB_US_EQ`
  (shortName="META", ISIN US30303M1027). This is confirmed tradable on demo.
  The alias `"META": "FB_US_EQ"` is now hardcoded in `TICKER_ALIASES` so
  translation is explicit and reliable. Previous failures were due to an older
  version of the translation code before the alias and shortName matching were added.

- **NWG.L / BHP.L**: T212 uses old tickers `RBSl_EQ` (NatWest) and `BLTl_EQ`
  (BHP). The `shortName` field correctly shows "NWG" / "BHP" so forward
  translation works. If these are ever recommended, add them to `TICKER_ALIASES`
  to make it explicit.

## Shadow vs T212 sync — the core design

When `T212_DEMO_EXECUTE=true`:
- T212 is source of truth for what actually executed
- Sync runs at start of each weekly run (before Claude is called)
- Adds positions T212 holds that shadow is missing
- **Removes positions shadow holds that T212 doesn't** — this was added to
  handle execution failures (e.g. insufficient funds, ticker not found)
- Cash is always set to T212's `availableToTrade` balance

When `T212_DEMO_EXECUTE=false`:
- Shadow-only mode: all Claude recs applied to shadow, nothing touches T212
- Sync still runs but only adds (never removes) — T212 isn't authoritative

## Execution order (critical — do not revert)

```
T212 executes FIRST → shadow mirrors only confirmed trades
```

NOT the other way around. Previously shadow applied all recs then T212 tried
to execute — this caused drift every time T212 failed (e.g. NVDA failed due to
insufficient funds after DELL trim hadn't settled; NVDA appeared in shadow
permanently until next week's sync cleaned it out).

## Strategy constraints (baked into Claude prompt)

- Fundamentals only — no technical/momentum signals
- 5–10 concentrated positions
- Max 25% per position (~£1,250 at £5k scale)
- Cash reserve 5–15% (£250–£750) — uninvested cash is a deliberate choice
- Minimum position size ~£400
- Holding period: weeks to months
- Universe: UK/US listed stocks and ETFs on Trading 212
- Benchmark: VUSA.L (Vanguard S&P 500 GBP)

## Two-model design

- **Weekly (Sonnet)**: fundamentals analysis with live web search, outputs
  prose report + JSON recommendations block
- **Monthly (Opus)**: strategic critique of the agent itself — not picking new
  trades, but reviewing whether the strategy/reasoning is sound. Runs on first
  Sunday of each month, or with `--deep-review` flag.

## Current portfolio state (as of 2026-05-10)

- Starting capital: £5,000 (22 Apr 2026)
- Shadow: ~£5,119 (+2.4%) after META removal
- T212 demo: £5,517 (6 positions)
- Benchmark VUSA.L: +3.14% over same period
- Positions: AVGO, DELL (trimmed), AMZN, MSFT, GOOGL, ORCL
- Note: early snapshots show inflated returns (+19%) from META phantom position
  which was never executable. Bidirectional sync now prevents this.

## Performance philosophy

- 1 year of outperformance = statistically meaningless
- Need 2+ years across multiple market regimes for a real signal
- Benchmark is VUSA.L — if Claude can't beat a passive S&P 500 ETF over 2+
  years, there's no case for running this strategy
- Real money (beyond demo) should never come from remortgaging or pensions

## What NOT to do

- Do not change `T212_ENV=live` without explicit instruction from Tom
- Do not revert the T212-first execution order
- Do not make shadow append-only again (bidirectional sync was added deliberately)
- Do not remove the `confirmed_recs` pattern from `t212_executor.py`
- Do not add META to manual ticker aliases without first verifying it exists
  in `t212_instruments.json`
