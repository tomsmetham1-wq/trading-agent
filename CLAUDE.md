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
- **Re-bases the cost of positions both sides hold** to T212's actual GBP
  fill (`walletImpact.totalCost / quantity`, Sep 2026) when it differs by
  more than `COST_REBASE_TOLERANCE` (0.1%). Shadow books a BUY at the price
  it saw at run time; T212 fills at market, often the next open. Nothing
  reconciled the two, so on 14 Sep 2026 MRVL was carried at £145.67 against
  a £154.53 fill (-5.7%), AMZN/GOOGL/DELL were 2-3% off, and every "+N% from
  entry" mechanism (trim triggers, played-out declarations, giveback peak,
  FX split) measured from the wrong line. The total was never wrong — cash
  is synced, so the error hid inside the per-position P&L. Logged per ticker
  as `SYNC_COST` (old and new cost); `compute_realized_pnl()` re-prices the
  open lots on it, and it makes a basis MORE exact, never estimated. Only the
  wallet figure is trusted; the native-price fallback needs an FX conversion.
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
- No repeat top-up of the SAME holding within 8 weeks (Sep 2026,
  `TOPUP_REPEAT_MIN_WEEKS`). Prompt rule plus an advisory alert
  (`_repeat_topup_alerts`), deliberately not a block. Why it exists: every
  dead-zone top-up is individually legal, so choosing the same destination run
  after run accumulates a large position that was never argued for — NVDA was
  topped up on 1 Sep (£246) and again on 10 Sep (£259), nine days apart, taking
  it to the biggest holding at 15.9% while sitting at -0.07%. Why it is not a
  block: a block leaves the slice undeployed with no mechanism to pick another
  destination, which is the idle-cash trap closed in Aug 2026. The Sep 2026
  deep review asked instead for price confirmation ("new local high on volume")
  — NOT implemented, and do not implement it: it is a momentum signal in a
  fundamentals-only strategy.
  Correction to that review, verified against the trade log (do not restate its
  version): it reported "four top-ups — 10 May, 13 May, 1 Sep, 10 Sep". The
  10 May order was REJECTED at T212 and removed by sync the same day, and
  13 May was the retry that opened the position; the held share count
  reconciles to 13 May + 1 Sep + 10 Sep exactly (6.196315). There have been two
  top-ups, not four, and the May pair is a rejected order plus its retry — the
  same episode that motivated the flip-flop guard, not evidence of fixation.
  What survives the correction is the real bias: the top-up criterion Claude
  keeps using is "most upside to the first trim level", which is measured from
  ENTRY and therefore always favours the holding that has gone up least. That
  is averaging down in the language of forward risk/reward, and it is why the
  slice went to the same name twice. The prompt now names it.
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
  **TRIGGERED 10 Sep 2026.** Ex-DELL the book had returned ~4.7% on ~£4,600
  against VUSA's +7.12%; DELL was ~£1,117 of ~£1,333 total profit (~85%).
  Continuing was accepted as an explicit bet with a deadline, not as a
  comfortable assumption — see the single-name dependency check below.

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
- **Undriven played-out sell** (added Aug 2026): a position listed in the
  new `played_out` JSON array with NO forward driver on record, no SET_DRIVER
  this run and no TRIM/SELL is EXITED IN FULL. The forward driver is the
  reason to hold a realized winner, so a position with none has no stated
  case. The prompt always said this ("defaulting to HOLD with no named
  forward driver is not permitted") but nothing enforced it: `thesis_played_out`
  is set BY `_apply_set_driver`, so a position became played out only because
  a driver was named, and "played out with no driver" could not be represented
  in the ledger at all -- the judgement lived in prose that no guard reads.
  `extract_played_out()` makes it machine-readable. Deliberately narrow: a
  position that already HAS a driver is untouched, because re-confirming an
  unchanged driver is legitimate and is covered by the weekly
  confirm/replace/trim loop. Runs BEFORE the bank injection so the bank sees
  the sell and doesn't also trim 33% of a position being closed. This is the
  only guard that can close a position; every other mechanical path banks a
  third at a time.
  Watch for: since every mechanism here is triggered by Claude's own
  admission, making the declaration more expensive raises the incentive to
  declare less often. The tell is declarations getting RARER, not the sells
  looking wrong.
- **Played-out bank injection** (added Aug 2026): a played-out position owing
  a bank (no trim since declaration or in 12 weeks) gets a 33% TRIM inserted
  at the FRONT of the rec list — the only guard that creates a trade rather
  than blocking one. Skipped when Claude's own recs already SELL/TRIM the
  ticker; falls back to an advisory alert when the position has no live price
  or the trim would be under £25.
- **SET_TRIMS tighten-only** (added Aug 2026): blocks any SET_TRIMS that
  raises or removes the next un-hit trim trigger.
- **Trim trigger parsing** (fixed Aug 2026): `_parse_trim_triggers()` strips
  bracketed commentary before reading "+N%" levels, and honoured levels are
  counted from the FIRST SET_TRIMS for that ticker, not from `first_bought`.
  Both bugs were live on DELL and between them made its alert unfireable: the
  regex read a superseded "+150%" out of an explanatory bracket, and four
  cap-breach trims from May–June were counted as honouring levels that were
  not set until 27 July — so the alert needed six levels hit when only three
  parsed. Earliest rather than latest SET_TRIMS so re-stating levels cannot
  reset the count and re-alert a level already acted on.
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
  idle-cash trap, see the dead-zone rule above); a BUY tops up a holding
  bought into within the last 8 weeks (`_repeat_topup_alerts`); a SET_TRIMS is
  the third or later rewrite of one position's levels
  (`TRIM_RESET_ALERT_COUNT`, `_trim_reset_alerts` — DELL's were re-set on
  27 Jul, 3 Aug, 10 Aug and 10 Sep; counting rather than blocking because once
  every level has been honoured there is no un-hit trigger for the tighten-only
  rule to compare against, and blocking would leave the residual with no
  mechanical exit at all); a theme is STILL over the 60% cap after this run's recs are
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
computed realised-vs-unrealised figures into the deep review prompt.

The replay honours what sync did to the ledger (Aug 2026 fix -- do not
regress): `SYNC_REMOVE` drops a position T212 never held so its phantom BUYs
can't blend into a later real position's basis, `SYNC_ADD` seeds a holding at
T212's own cost, `SYNC_RESET` marks a wholesale rebuild and clears the replay.
`sync_from_t212()` writes these per-ticker records itself; before it did, sync
logged only a prose note and the replay never saw the change. Consequences as
of 24 Aug 2026: the four April phantom META lots (2,900 GBP of rejected orders)
were still blending into the real 13 May position, reporting its loss as
-93.79 GBP when the shares actually bought and sold lost -48.99 GBP, and DELL's
five trims scored +90 GBP against a real ~+650 GBP. Both fed the monthly deep
review, which is where the kill-criteria decomposition is made.

Shares sold with no recorded basis now fall back to the position's current
`avg_cost_gbp` and land in `tickers_with_estimated_basis` (right order of
magnitude, not exact) rather than being skipped -- skipping them understated
realised P&L by hundreds. Names with no position left to infer from stay in
`tickers_with_incomplete_basis`, and their `unpriced_proceeds_gbp` are reported
so a missing figure doesn't read as a zero.

`sp.reconcile_trade_log()` checks the log still explains the positions held and
the cash balance; the deep review prompt carries a WARNING block when it
doesn't. `migrate_trade_log.py` is the one-off (idempotent) backfill that
converted the legacy prose sync entries and stamped a `SYNC_BASELINE` -- shares
held before the April 2026 bootstrap rebuilds were never logged, so
reconciliation counts from that baseline forward.

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

Driver failure is a thesis break (Aug 2026). A forward driver is the ENTIRE
basis for holding a played-out position, so a driver contradicted by evidence
is a break on that claim — but nothing distinguished a failed driver from a
superseded one, and "name a new driver" was always available at no cost. DELL's
driver #1 was not tested and found valid on 2026-08-10; it was simply replaced
when a better-sounding fact appeared. A SET_DRIVER that replaces an existing
driver must now carry `previous_driver_status`:
- `"failed"` — evidence contradicted it. Prompt makes SELL the default and
  requires the thesis-break checklist to keep any of the position; code sets
  `driver_failed_on` and `played_out_bank_due()` returns True immediately
  rather than in 12 weeks. `_inject_played_out_banks()` reads the failure off
  THIS RUN'S RECS, not off the position -- `_apply_set_driver` writes
  `driver_failed_on` at execution, which is after the guards, so reading the
  position would fire the bank a week late and reintroduce the delay the rule
  exists to remove. Skipped as normal when Claude's own recs already SELL/TRIM
  the ticker.
- `"superseded"` — the old driver still holds, the new one states it better.
  Leaves the 12-week clock alone.
- Omitted → recorded as `"unstated"` and treated as failed. Declining to say
  must not be cheaper than saying it.

Every driver replacement banks (Sep 2026). The failed/superseded split above
priced a replacement at 33% or at NOTHING, decided by a free-text field that
Claude fills in about its own reasoning with nothing adjudicating it. That is a
free option, and it was taken the first week it existed: on 2026-09-01 DELL's
driver #2 ("ISG margin-expansion story") was replaced after the report itself
quoted ISG operating margin FALLING 110bp -- evidence contradicting the driver,
which is the definition of `"failed"` -- and the swap was filed `"superseded"`,
banking nothing on a +110% position that is 11% of the book.

The fix is not a sharper definition of "failed"; no prompt wording survives a
free option. The label now sets the SIZE of the bank, never whether one
happens:
- `"failed"` / `"unstated"` -> `PLAYED_OUT_BANK_TRIM_PCT` (33%), as before.
- `"superseded"` -> `PLAYED_OUT_SUPERSEDE_TRIM_PCT` (15%). Rewriting the reason
  you hold a realized winner is evidence about the hold whatever the reason.
  Honest labelling still saves 18 points, so the incentive points the right
  way; it just cannot reach zero.
- A SET_DRIVER naming a position's FIRST driver, or restating the existing one
  verbatim, is not a replacement and banks nothing.

Above that the driver COUNT escalates regardless of label, because a hold
re-argued from scratch every few weeks is carried by churn, not by a claim:
`DRIVER_CHURN_BANK_COUNT` (#3) banks the full 33%, `DRIVER_CHURN_EXIT_COUNT`
(#4) exits the position outright. DELL reached driver #3 in five weeks, each
one "confirmed with fresh evidence" -- for a secular theme there always is
some, which is why the count and not the content has to be what bites. Under
this rule the 1 Sep DELL swap banks £227 on either label, and DELL now stands
at driver #3: the next replacement, whatever it is called, closes the position.

All four constants live in `shadow_portfolio.py` beside the other played-out
policy numbers (`build_thesis_review()` quotes them into the prompt, so they
cannot live in `trading_agent.py` without an import cycle); `trading_agent.py`
aliases them. The prompt states the price list, so the label is chosen with the
cost known rather than discovered afterwards.

`_driver_replacements()` reads replacements off THIS RUN'S RECS, never off the
position -- `_apply_set_driver` appends to `forward_driver_history` and writes
`driver_failed_on` at execution, which is after the guards, so reading the
position fires every consequence a week late. That bug was already fixed once
for the failed-driver bank and was still live in `_forward_driver_alerts()`,
where the churn alert counted only executed history: DELL named driver #3 on
1 Sep and the "3 different forward drivers" alert did not fire. Both now share
the one helper.

High-water giveback bank (Aug 2026). Pre-committed trim levels are gains from
ENTRY, so on a large winner they sit far above the price and only ever fire on
a rally. A played-out winner sliding back down passed no trim level, could not
break a thesis that had already played out, and was caught by nothing but the
12-week drip — which is precisely the scenario kill criterion #2 describes.
`update_played_out_peaks()` ratchets `played_out_peak_gain_pct` before the
prompt is built; handing back `sp.PLAYED_OUT_GIVEBACK_PCT` (25%) of that peak
makes the bank due early, but only once that peak is at least
`sp.PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT` (50%). The floor exists because the test
measures the GAIN, so the price move it implies shrinks with the size of the
winner: a quarter of the gain is a 16.7% fall at a +200% peak, 13.3% at +100%,
but only 4.2% at +20% -- noise, not a giveback. Below the floor the 12-week
clock is the only mechanism. 25% rather than 50% because 50% IS the kill
criterion — acting there would only ever coincide with the shutdown it exists
to prevent. Only a bank taken strictly AFTER the peak date clears the
obligation (a trim on the peak date happened at the top). A missing peak is
seeded from the best SELL/TRIM price since declaration, not from today — DELL
was trimmed at +113.6% and sits at +100.5%, so a cold start would have erased
a giveback that already happened.

Two advisory guards make the same thing visible outside the prompt (see the
alerts list above) — without them the whole mechanism lived inside Claude's
context and never reached the weekly email. The prompt also now requires that a
played-out position's next trim level be within ~15% of TODAY's price, tightened
via SET_TRIMS alongside the SET_DRIVER if it isn't.

Size never argued (Sep 2026). Every dead-zone top-up is individually legal
and individually small, so a position can become the largest in the book
without anyone deciding it should be: NVDA opened at £500 (~8%) on 13 May,
was topped up £246 on 1 Sep and £259 on 10 Sep — 11.6% to 15.9% in nine
days, the biggest holding — with each buy argued on "thesis confirmed" and
"most upside to the first trim level". Neither argued for a 15% position.
`sp.topup_composition()` replays the current lot (a SYNC_REMOVE, SYNC_RESET
or closed_position sell starts a new one; first BUY/SYNC_ADD is the opening,
later ones are top-ups) and `sp.size_never_argued()` flags a holding at
`SIZE_ARGUED_MIN_WEIGHT_PCT` (12%) or more of the book with
`SIZE_ARGUED_TOPUP_SHARE` (30%) or more of its cost from top-ups.
`build_thesis_review()` renders it as "SIZE NEVER ARGUED" demanding (a) a
SET_SIZE — the fourth ledger-only action, records `size_argument` and
`size_argued_on` on the position — or (b) a TRIM toward the size argued at
entry. A SET_SIZE clears the flag only until the next top-up of that name (an
argument for 12% does not cover a buy to 16%). `_size_argued_alerts()` puts
it in the weekly email unless the run's recs already answer it (SET_SIZE,
TRIM or SELL of the name). Advisory, not a block: the size may well be right,
but it has to be decided rather than accumulated. Positions whose opening buy
predates the April 2026 SYNC_RESETs (AMZN, GOOGL) have no replayable
composition and cannot be flagged.

Last-session moves (Sep 2026) — the "tape" the prompt never carried. Every
figure the prompt shows for a holding is measured from ENTRY, so a same-day
shock is invisible: on 14 Sep 2026 the AI industry's own CEOs called for
slowing capability development, DELL/MRVL/NVDA opened -6%/-7%/-3% (48% of the
book in one theme, its three purest names the three worst on the day), the
T212 prices in the prompt already reflected it, and the email said NVDA had
"no material adverse news" with rates as the only macro headwind. Two causes:
the prompt carried no day-change data, and the search task was per-ticker
only — "NVDA news" returns earnings and analyst notes and crowds out a story
that hit every name at once (DELL's bullet was Friday's RBC $640 initiation
while the stock was -6% on Monday).

`sp.day_moves()` gives last price vs prior close per holding in its OWN
currency (so FX can't leak in), the benchmark, and a value-weighted move per
theme; `_session_move()` is cached per run so the prompt, email and alert all
carry the same figures. `build_tape_review()` puts it in the prompt with
"MUST EXPLAIN" against every holding over `DAY_MOVE_ALERT_PCT` (3%) and every
MULTI-holding theme over `DAY_MOVE_THEME_ALERT_PCT` (2%) — a one-name theme
is judged on the holding's bar, since a theme flag means "a common driver
moved several names". The prompt forbids "no material news" for anything on
that list and the search task now requires at least one search on the THEME
itself for every theme above 25% of the book. `format_tape_for_email()` and
`_day_move_alerts()` ("ALERT: LARGE MOVE today: ...") make it visible in the
email from code, whatever the analysis says. Before the US open (the Task
Scheduler run is Monday 10:00) US figures are the PREVIOUS session's and the
block says so. Deliberately no trade rule: whether a sector-wide story breaks
a thesis is the judgement the run exists to make; the defect was that it was
never asked.

Currency decomposition (Sep 2026). Every holding is priced in GBP, so its
reported P&L blends what the business did with what sterling did, and the
prompt carried NO FX data at all -- every price in it is GBP. So "that loss is
a GBP FX artefact" could be neither supported nor refuted from anything the
agent was given, and on 2026-09-01 it was asserted for exactly the three red
positions (NVDA, AMZN, GOOGL) and for none of the six green ones. The real
numbers invert that: sterling moved from ~1.347 to ~1.353 over the whole
period, so the FX drag on AMZN was 0.44pts of a 3.59% loss and on GOOGL
0.44pts of 2.70%, while NVDA's was +0.07pts -- FX helped it slightly, so its
loss is entirely real. The two largest drags, XOM at -2.83pts and JPM at
-2.57pts, sat on positions the same report called unqualified winners.

`fx_neutral_returns()` splits each position: `local_pct` is the return in the
position's own currency, `(1 + gbp_return) * (fx_now / fx_at_entry) - 1`, and
`fx_pts` is `gbp_pct - local_pct` (negative = sterling strength cost you).
Surfaced by `build_fx_review()` in the prompt and `format_fx_for_email()` in
the weekly email. The prompt block states the rule the numbers embody: an FX
move is common to every holding in the same currency over the same window, so
it can never explain why one position is down and another is up.

`fx_at_entry` is recorded on the position. A BUY stores the live rate
(`fx_basis: "actual"`) and a top-up blends it cost-weighted exactly as
`avg_cost_gbp` does; an estimated leg keeps the whole basis estimated.
`ensure_entry_fx()` runs before the prompt is built and reconstructs a missing
rate from `fx_rate_on(pair, first_bought)`, marking it `"estimated"` -- for a
position built from several buys that is the first rate, not the blend, so it
is the right order of magnitude and not exact, in the same spirit as
`tickers_with_estimated_basis`. It is idempotent, so it costs one FX history
fetch on the first run and nothing after, and it doubles as the backfill --
no migration script. A position with no stored entry rate or no live rate is
OMITTED from the decomposition rather than guessed at; a missing rate must
never read as "no FX effect".

`ensure_entry_fx()` must be CALLED, and is, from `run_weekly()` step 3b —
after sync, before the valuation and prompt are built (Sep 2026 fix, do not
regress). It shipped written and unit-tested but never wired into the run, so
the only position carrying an entry rate was the one the BUY path had recorded:
on 2026-09-10 the decomposition covered NVDA alone and the email read "FX moved
the book between +0.00 and +0.00 pts" — a missing rate rendering as "no FX
effect", the one thing the docstring says must never happen. With it wired, all
nine positions backfill (DELL/AMZN/GOOGL 1.3466, MRVL 1.3496, ABBV 1.3336,
XOM/JPM 1.3208, V 1.3654), which is what surfaces the real FX drag on XOM and
JPM that the Sep 2026 commit was written to expose.

Note `fx_rate_on()` and `_fx_rate()` need `os_ca_bundle.ensure_os_ca_bundle()`
to have run (it does, at shadow_portfolio import). Importing yfinance directly
in a scratch script skips it and every FX call fails with "unable to get local
issuer certificate".

Single-name dependency check (Sep 2026). Kill criterion #5 triggered at the
10 Sep 2026 deep review, whose own §7(a) verdict was that six of its seven
recommendations were housekeeping: the finding is that the picks other than the
top one generate no alpha, and no rule tightening creates a second good idea.
The one recommendation that addressed the trigger was to make the number
visible every run so the dependency cannot hide. That is
`sp.ex_top_contributor_performance()` — in the weekly prompt via
`build_ex_top_review()`, in the weekly email via `format_ex_top_for_email()`,
and in the deep review in place of the old ad-hoc `top_contributor` line.

Contribution is realised + unrealised per ticker. Ranking on unrealised alone
(what the deep review prompt did) is wrong for exactly the case the criterion
exists to catch: a name whose gains have been BANKED shows a small unrealised
figure because it delivered. DELL's £804 realised was more than the entire
realised book and was invisible to that ranking.

The remainder is scored against the capital it actually had — starting capital
less `peak_cost_gbp` for the top name (the most cost basis it ever had open at
once, now returned by `compute_realized_pnl()`). Charging the whole starting
capital to the remainder understates it; ignoring the top name's capital
overstates it. Neither is exact because capital recycles, so every rendering
labels the figure approximate. On the 10 Sep book this reproduces the review's
own arithmetic independently: DELL £1,117.11, remainder £216.16 on ~£4,600 =
+4.70% against +7.12%.

The test has a deadline and two ways to pass, both from the review:
`EX_TOP_TEST_DATE` = 30 Nov 2026, and by then either the ex-top book beats the
benchmark OR one non-top position has earned `EX_TOP_SECOND_IDEA_GBP` (£150) in
its own right. As of 10 Sep the second leg is live and close — XOM at £134.32.
If neither is true by the date, the strategy is funding variance, not skill.

Capture — the pick scored apart from its management (Sep 2026). Kill
criterion #5 asks whether the OTHER picks make money; nothing scored what the
agent did with the pick it had. DELL was sold down six times May–Sep 2026,
four of them cap trims nobody decided, and the one discretionary call —
keeping the remainder after declaring the thesis played out on 27 Jul at
+98% — earned ~£175 (+18%) against +1.6% for VUSA over the window, more than
the entire ex-DELL book. "DELL vs everything else" credits the pick with all
of it. `sp.position_capture()` replays the current lot (same boundaries as
`topup_composition`) and reports per held name: buy-and-hold of every share
ever bought vs actual (proceeds + open value − cost), the capture ratio,
each sell's forgone gain against today tagged [guard]/[defect]/[claude]
(`guard_generated` now persists on the trade record), and for a played-out
position the value of the shares held at declaration vs what they became.
Rendered by `build_capture_review()` into the DEEP REVIEW ONLY (section 1 is
told to score selection, sizing and management apart); deliberately not in
the weekly email — nothing acts on it weekly. On the 21 Sep book DELL is
59% capture: £1,118 kept of a £1,910 buy-and-hold, £793 of the gap from the
four cap trims (£412 from the first, at +20%).

The declaration price comes from `record_played_out_prices()` (run_weekly
step 7c, post-trade so a same-run injected bank is already out of the share
count), stored once as `played_out_price_gbp` / `played_out_price_basis:
"actual"`. A legacy declaration is left unscored rather than seeded from
today. DELL's was backfilled by `fix_dell_played_out_price.py` from the
+97.6% in the 27 Jul report (basis "backfilled", labelled in every
rendering). The forgone figures are hindsight — they say what the sells cost,
not that they were wrong — and none of this produces a second idea, so the
30 Nov test is unchanged.

Deliberately NOT implemented from the same review: raising the cash floor to
8–10%. It contradicts the Aug 2026 dead-zone rule (an 8% floor would have made
this run's £259 slice a £69 slice, below the 3% top-up minimum — nothing
deployable, which is the trap that rule exists to close), and it contradicts
the review's own §7(c) finding that the failure is idea generation and NOT
deployment or constraints. Holding more cash does not produce a second good
idea; it just adds drag against a fully-invested benchmark.

Entry-thesis provenance (Sep 2026). `entry_thesis_provenance()` classifies each
holding's thesis as recorded / backfilled / synced / missing, and
`build_thesis_review()` flags anything but "recorded" as needing to be
re-underwritten this run. As of 10 Sep 2026 no position is thesis-less, but
AMZN and GOOGL (both bought 2026-04-26, both predating the field) carry cases
backfilled on 2026-07-03.

The distinction is not bookkeeping. A case reconstructed ten weeks after entry
was written with the price history already visible, so it was never a
prediction and re-confirming it proves nothing — it is the confirmation-seeking
the Sep 2026 deep review flagged, in the two positions the same review named as
"small losers held without a fresh thesis review". Between them they are ~18%
of the book, have returned -GBP56.81 over 137 days against a benchmark that
returned +7.12%, and have cost ~GBP144 relative to holding VUSA with the same
capital.

Deliberately NOT a mechanical sell, and do not make it one: exiting on a
record-keeping defect is a trade forced by paperwork rather than by
fundamentals, which is the AVGO process error the review called the worst
artefact in the book. The flag forces the position to be argued fresh or
recycled; which of those happens is a judgement made in the run, in front of
the single-name dependency block.

Re-underwriting is its own action: SET_THESIS (Sep 2026). The third
ledger-only rec (`LEDGER_ONLY_ACTIONS`), `_apply_set_thesis()`: replaces
`thesis` with the case written today, prefixed `[Re-underwritten <date>]`,
sets `thesis_reunderwritten`, logs a `SET_THESIS` trade carrying the text it
replaced, and touches NOTHING in the played-out machinery.
`entry_thesis_provenance()` reads the prefix as "recorded" with the date, so
the review scores the case from that day rather than flagging it again.

Why it had to be separate — the 14 Sep 2026 run: the provenance flag shipped
telling Claude to re-underwrite "as a SET_DRIVER". SET_DRIVER means "the
original thesis has PLAYED OUT": `_apply_set_driver` sets
`thesis_played_out`, and `_inject_played_out_banks()` saw "declared this run,
nothing banked" and trimmed a third of AMZN at -3.6% and a third of GOOGL at
+0.16% "to convert paper alpha into realised alpha". Two flat positions sold
down because the paperwork used the wrong verb — the trade-forced-by-paperwork
outcome the flag was explicitly documented never to cause. A re-underwrite
says the position never had a scoreable thesis; a driver says the thesis it
had has been realised. Different claims, so different actions, and the prompt
now says which is which. `fix_reunderwrite_records.py` (one-off, idempotent)
repaired the ledger: both positions un-flagged with the driver moved into
`thesis`, the two SET_DRIVER trades rewritten as SET_THESIS, and the two TRIMs
annotated `process_defect: true` with an `exit_thesis` that says so (original
kept under `exit_thesis_original`) so the recent-exits replay does not present
them as a judgement. The T212 trims stand — the flip-flop guard rightly stops
a same-week rebuy, and re-buying would be churn.

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

Exits are tracked automatically (Aug 2026): any trade on this run carrying
`closed_position: true` is added to the watchlist by `record_watchlist()`,
recorded `active: false` with `source: "exit"` and `exited_on`, and rendered as
"SOLD <date>" rather than "dropped" — a sold position is evidence about a
decision, not a live idea. Relying on the report to list its own exits did not
work: on 2026-08-24 the agent sold META saying "the watchlist is the right
place for that" and then left it off the array, so the exit was scored nowhere.
A name Claude also lists that run keeps its listed entry (active, with Claude's
thesis); the exit path never clobbers it.

Watchlist theses are replayed months later as the record of why a name was NOT
bought, so the prompt reserves the word "blocked" for trades a strategy guard
actually blocked. MSFT was logged on 2026-08-24 as "blocked on AI
infrastructure theme concentration" when the buy would have taken the theme to
55% against a 60% cap and the guard log shows no block — it was a preference
for diversification. Both records were repaired by `fix_watchlist_records.py`
(one-off, idempotent).
Deliberately absent: any rule that blocks a BUY for not being on the watchlist,
or requires a name to persist N weeks before it can be bought. Those were
considered and rejected — they forfeit real upside to buy a filter the data
doesn't yet justify. Revisit only once there are ~3 months of scores.

Test suite: `test_trading_agent.py` (327 tests, no network). Run it after any
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
