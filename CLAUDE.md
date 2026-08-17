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

# Weekly run (what Task Scheduler does every Monday at 10:00)
python trading_agent.py

# Force a monthly deep review alongside the weekly run
python trading_agent.py --deep-review

# Run only the deep review, skip weekly (no new trades)
python trading_agent.py --deep-review --skip-weekly

# Run the regression test suite (no network needed) — run after ANY code change
python -m pytest test_trading_agent.py -q
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
CLAUDE_MODEL_DEEP=claude-opus-4-8       # Monthly deep review model
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

All sizing rules are percentage-based so they scale as the portfolio grows.

- Fundamentals only — no technical/momentum signals
- 5–10 concentrated positions
- Hard position cap: 20% of total portfolio value. Soft cap: 18%.
  When 20% is hit, trim to 15% — not to 19.9%.
- Cash reserve 5–15% of total portfolio value — uninvested cash is a deliberate choice
- Deploy trigger: cash > 15% → must buy
- Each buy: 8–20% of total portfolio value. Dead-zone exception (July 2026,
  widened Aug 2026): the trigger is the DEPLOYABLE SLICE (cash minus the 5%
  floor), not headline cash %. Whenever that slice is too small to fund a new
  position at the 8% minimum, the agent may deploy it as a 3–8% top-up of ONE
  existing holding (position/theme caps still apply, a live forward driver
  must be stated, and new positions keep the 8% minimum).
  Why it was widened: the original "cash between 5% and 8%" wording left a
  trap. On 17 Aug 2026 cash was 12.7% (£808) — above the dead-zone band so no
  top-up was permitted, below 15% so no forced deploy, and the slice (£491)
  couldn't fund the £507 minimum new position. Nothing fired; the agent
  deployed nothing for eight weeks (last new position 22 June). Keying off the
  slice strictly generalises the old rule and closes the gap.
- Do NOT exit a position solely because it shrank below 8% — only exit if thesis broken
- Thesis realized ≠ thesis intact (added July 2026): when a position's ORIGINAL
  thesis has substantially played out (mispricing closed, gain captured), HOLD is
  not the default. Continuing to hold requires naming a NEW, independent,
  forward-looking driver you'd underwrite as a fresh BUY at today's price/weight;
  "still growing / business is fine" doesn't qualify (already priced in). Else
  TRIM/exit and recycle into better forward risk/reward. Prompt-only, not a code
  guard — it's a judgement enforced via the thesis-accountability check, so
  "played out" now forces a decision instead of defaulting to hold. Thesis-
  realized trims are exempt from the thesis-break checklist's "knowable at
  entry → override the sell" rule (reaching fair value was the plan, not a
  panic) — without the exemption the checklist would veto every recycling trim.
  The named driver is no longer prose-only: it must be recorded with a
  SET_DRIVER rec (July 2026, see below) so it is replayed and re-tested every
  following week rather than silently carrying the hold forever.
- Played-out positions must BANK, not just argue (added Aug 2026): declaring a
  thesis played out costs 1/3 of the position — a forward driver carries the
  remainder, never the whole win. Code-enforced in
  `_inject_played_out_banks()`: if a played-out position has had no TRIM/SELL
  since its declaration, or none in the last 12 weeks
  (`sp.PLAYED_OUT_REBANK_WEEKS`), a 33% TRIM (`PLAYED_OUT_BANK_TRIM_PCT`) is
  INJECTED into the rec list and executes like any Claude trade. Claude is
  told in the prompt and thesis review ("MECHANICAL BANK DUE") so it can
  pre-empt with its own better-sized trim. Rationale: the accountability loop
  demanded words, not money — DELL was declared played out 2026-07-27 at
  +97.6% and was then held for weeks on a driver "confirmed with fresh
  evidence" every run (for a secular theme there always is some), while the
  Aug 2026 Opus deep review said "bank a third of DELL now".
- Trim levels only tighten (added Aug 2026, Opus deep review): a SET_TRIMS
  that raises or removes the next un-hit trigger is BLOCKED in code and the
  existing levels kept — an unreachable level means the position is extended,
  which argues for trimming, not for moving the line. Compared on the first
  trigger above current P&L (falls back to raw first triggers when the price
  is unknown), so re-tightening after a level has been hit still works.
- Theme concentration cap: max 60% in any single macro theme; must hold ≥1 non-dominant-theme position
- Flip-flop rule: no BUY within 5 trading days of a SELL/TRIM of the same ticker
- Pre-commit trim levels at BUY entry — mechanical, not reactive. Legacy
  positions bought before this field existed are backfilled via SET_TRIMS
  (July 2026): a ledger-only rec action — no T212 order, no cash movement —
  that persists `pre_commit_trims` on an existing position and logs a
  SET_TRIMS trade. The executor confirms it straight through, guards pass it
  untouched, and `build_thesis_review()` flags any holding with "NONE SET"
  until the whole book is covered.
- Holding period: weeks to months
- Universe: UK/US listed stocks and ETFs on Trading 212
- Benchmark: VUSA.L (Vanguard S&P 500 GBP)

## Kill criteria (June 2026 deep review — evaluate monthly)

- **3 consecutive months cumulative underperformance vs VUSA** → shut down
- **Top contributor gives back >50% of gains AND rest of book hasn't compensated** → shut down
- **Any single-week drawdown >15% with no thesis explanation** → risk-management failure
- **Buy-sell-rebuy flip-flop on same ticker >2× in a month** → agent is reacting to price, not fundamentals
- **End of August 2026: remove top contributor, rest still underperforms VUSA** → lottery-ticket buyer, not stock-picker → shut down

## Code-level strategy guards (added June 2026 code review)

`enforce_strategy_guards()` in trading_agent.py runs after Claude's recs and
before execution — prompt rules that were being violated are now mechanical:

- **Flip-flop rule**: BUY blocked if the same ticker was fully exited within
  7 calendar days (~5 trading days). Counts both SELLs and TRIMs that closed
  the position (trades carry a `closed_position: true` flag). Also blocks a
  BUY when the SAME run's rec list fully exits that ticker (the history check
  can't see sells that haven't hit the ledger yet).
- **20% position cap**: BUY amounts are reduced to land at the cap, or blocked
  if the position is already over it (or the reduced order would be under
  £25). Multiple BUYs of one ticker in a run count cumulatively.
- **Played-out bank injection** (added Aug 2026): a played-out position owing
  a bank (no trim since declaration or in 12 weeks) gets a 33% TRIM inserted
  at the FRONT of the rec list — the only guard that creates a trade rather
  than blocking one. Skipped when Claude's own recs already SELL/TRIM the
  ticker; falls back to an advisory alert when the position has no live price
  or the trim would be under £25.
- **SET_TRIMS tighten-only** (added Aug 2026): blocks any SET_TRIMS that
  raises or removes the next un-hit trim trigger.
- **60% theme cap** (added July 2026 after AI exposure hit 81% in June): BUYs
  whose `theme` label would push that theme above 60% are reduced or blocked.
  Exposure freed by same-run SELL/TRIM recs of the same theme is credited
  first, so rebalancing within a theme isn't wrongly blocked. This only stops
  a theme getting WORSE — it has nothing to act on when a theme is already
  overweight and no new buy is proposed in it (see the alert below for that
  case).
- **Advisory alerts** (in guard_events, never blocking): pre-committed trim
  level hit but no TRIM recommended (parses "+N%" triggers from the stored
  `pre_commit_trims` text, skipping levels already honoured by counting TRIM
  trades since first_bought); a position is held on a recorded forward driver
  after its thesis played out, with the driver's age (plus a churn flag once 3+
  different drivers have been named for the same position); a played-out
  position's next trim level needs more than a 15% rally from TODAY to trigger
  (`PLAYED_OUT_TRIM_MAX_UPSIDE` — trim levels are entry-relative, so on a big
  winner they drift out of reach: DELL was played out at +97.6% with its first
  trim at +130% from entry, ~16% away); planned buys would leave cash below the
  5% reserve floor; the deployable slice can't fund a new position at the 8%
  minimum but is big enough for a dead-zone top-up and no BUY was proposed
  (`CASH_RESERVE_FLOOR` / `MIN_NEW_POSITION_PCT` / `MIN_TOPUP_PCT` — the
  idle-cash trap, see the dead-zone rule above); a theme is STILL over the 60% cap after this run's recs are
  applied (added after the July 2026 Opus deep review flagged that AI infra
  was still ~58-63% weeks after the BUY-side cap existed, because nothing
  forces a correction when Claude doesn't propose a new buy in that theme —
  fires every run until the overweight is actually addressed).
- Guard actions appear in the weekly email under "Strategy guard actions".
- The weekly email also flags a >15% week-on-week drawdown (kill criterion)
  the week it happens, and marks the week-on-week figure as indicative when
  either snapshot had missing prices.
- `extract_recommendations()` uses the LAST ```json block containing a
  "recommendations" key, not the first — an echoed example block in the prose
  must never be executed.

Crash-recovery journal: `run_journal.json` (gitignored) is written just before
T212 execution and deleted after the ledger saves. If a run crashes in between,
same-day re-runs are blocked (orders may already be at T212) — check the T212
order history, then delete the file to re-enable runs.

Realised P&L: `sp.compute_realized_pnl()` replays the trade log and feeds
computed realised-vs-unrealised figures into the deep review prompt (tickers
whose cost basis came from T212 sync are flagged as incomplete).

Forward-driver accountability (SET_DRIVER, July 2026): the second ledger-only
rec action (alongside SET_TRIMS) — no T212 order, no cash movement, confirmed
straight through by the executor (`LEDGER_ONLY_ACTIONS` in t212_executor.py)
and passed untouched by the guards. It persists on the position:
`thesis_played_out: true`, `forward_driver`, `forward_driver_set` (date), and
`forward_driver_history` (every driver ever named, current one last).

Why it exists: the thesis-realized rule let Claude keep a played-out winner by
naming a new forward driver, but that claim lived only in that week's prose.
Next run saw a plain HOLD, never re-tested it, and the position coasted (DELL,
declared "definitively played out" at +97.6% on 2026-07-27 and held anyway).
`build_thesis_review()` now replays the recorded driver, its age in weeks, and
any drivers it superseded, then demands one of: (a) confirm it still live with
NEW evidence, (b) SET_DRIVER a replacement, (c) TRIM/SELL. Repeated
replacements are surfaced as "driver #N" — churning justifications to keep a
winner is itself a signal. `thesis_played_out` is never cleared (a realized
thesis doesn't un-realize); selling the position removes it with the position.
DELL's driver was backfilled from the 2026-07-27 report.

Two advisory guards make the same thing visible outside the prompt (see the
alerts list above) — without them the whole mechanism lived inside Claude's
context and never reached the weekly email. The prompt also now requires that a
played-out position's next trim level be within ~15% of TODAY's price, tightened
via SET_TRIMS alongside the SET_DRIVER if it isn't.

Watchlist recording (Aug 2026) — RECORDING ONLY, deliberately not a gate:
`ledger["watchlist"]` tracks every name Claude flags in section 4, with the
price at first mention and a weekly observation thereafter. Claude emits an
optional `"watchlist"` array alongside `"recommendations"` in the same JSON
block; `sp.record_watchlist()` runs AFTER execution (so a name bought this run
is scoreable as "bought") and is wrapped in try/except so idea-tracking can
never break a run that already placed orders.

Why it exists: the agent named 3 fresh watchlist ideas every week and never
revisited them (ZTS/STZ/ACN on 10 Aug, NFLX/AZN.L/WMT on 17 Aug), so there was
no evidence about whether its non-held ideas were any good — the open question
after DELL was found to carry the entire book. `watchlist_performance()` scores
each name from its first priced observation against the benchmark **over that
name's own window** (observations store the inception-relative benchmark
return; the window return is the ratio of the two, not their difference — do
not "simplify" this to a subtraction). Surfaced in the prompt via
`build_watchlist_review()` and in the weekly email via
`format_watchlist_for_email()`.

Names dropped from the active list keep being priced for
`sp.WATCHLIST_TRACK_WEEKS` (26) — an idea abandoned just before it ran is the
single most important thing this captures, so do not "clean up" dropped names.
Deliberately absent: any rule that blocks a BUY for not being on the watchlist,
or requires a name to persist N weeks before it can be bought. Those were
considered and rejected — they forfeit real upside to buy a filter the data
doesn't yet justify. Revisit only once there are ~3 months of scores.

Test suite: `test_trading_agent.py` (164 tests, no network). Run it after any
change to translation, sync, guards, or ledger logic.

Theme tracking: every BUY rec now carries a `theme` label, persisted on the
position and trade. `build_prompt()` computes per-theme exposure and flags any
theme over the 60% cap in the position-size alert section. `pre_commit_trims`
is also persisted and surfaced in the thesis review as binding.

Robustness fixes from the same review (do not regress):
- All JSON file IO uses `encoding="utf-8"` (Windows cp1252 was corrupting em-dashes).
- `fetch_price_gbp` checks pence ("GBp"/"GBX") BEFORE "GBP" — yfinance reports
  LSE prices in pence with currency "GBp", which uppercases to "GBP" (100x bug).
- `_get_available_cash()` returns None (not 0.0) on API error; the buy budget
  check is skipped when cash is unknown instead of blocking all buys.
- Bidirectional sync refuses to wipe the ledger if T212 returns 0 positions
  while shadow holds ≥2 (API-glitch guard).
- Sell orders that end REJECTED/CANCELLED are removed from confirmed_recs so
  shadow never mirrors a sell that didn't execute.
- `call_claude` handles `stop_reason="pause_turn"` (server web-search loop can
  pause mid-turn; without resuming, the trailing JSON block is lost), warns on
  `max_tokens` truncation, uses adaptive thinking, and retries with typed
  exceptions (429/5xx/529) plus raw `httpx.TransportError` — the SDK does NOT
  wrap a connection dropped mid-stream ("peer closed connection without
  sending complete message body", e.g. Avast killing a long-lived stream) in
  `anthropic.APIConnectionError`, and that crashed the 2026-07-13 weekly run
  before it was caught.
- Weekly snapshots carry `pricing_incomplete: true` when any position had no
  price — don't read those as real drawdowns.

## Two-model design

- **Weekly (Sonnet)**: fundamentals analysis with live web search, outputs
  prose report + JSON recommendations block
- **Monthly (Opus)**: strategic critique of the agent itself — not picking new
  trades, but reviewing whether the strategy/reasoning is sound. Runs on first
  Monday of each month, or with `--deep-review` flag.

Deep review section 7 (added Aug 2026): sections 5 (recommendations) and 6
(kill criteria) were independent, and 5 comes first — so the Aug 2026 review
produced seven improvements and then a "shut it down" verdict with nothing
reconciling them. Section 7 now fires only when a criterion has triggered and
forces the review to say which recommendations would actually address the
finding (or admit none would), what must be true by a named date for
continuing to have been right, and — critically — whether the failure is one
of IDEA GENERATION or of DEPLOYMENT/CONSTRAINTS. The watchlist scores are
passed into the deep review as the evidence for that last call; the raw
observation series is stripped from the ledger copy to save tokens.

## Current portfolio state (as of 2026-06-01)

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
