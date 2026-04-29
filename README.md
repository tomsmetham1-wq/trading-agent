# Trading Agent — Stage 1 (Decision-Only + Shadow Tracking)

Weekly fundamentals review for a Trading 212 portfolio, powered by Claude,
with a **shadow portfolio** that tracks every recommendation so you can
honestly judge whether Claude is any good.

The script **does not place real trades**. It:
1. Reads your T212 account (for reference).
2. Values a simulated portfolio from every past recommendation.
3. Emails you the report + performance vs benchmark.
4. Once a month, runs a deeper strategic critique using Opus.

---

## How it works

### Weekly run (Sonnet 4.6)
Every time Claude says BUY £20 of AAPL, the script:
- Fetches the current AAPL price (converted to GBP)
- Deducts £20 of imaginary cash
- Adds the equivalent shares to `shadow_portfolio.json`

Each week, before asking Claude for new ideas, the script:
- Marks every position to market
- Computes total return vs a benchmark (default: Vanguard S&P 500, `VUSA.L`)
- Feeds Claude its own track record so it can reassess honestly

### Monthly deep review (Opus 4.7)
On the first Sunday of each month (auto-triggered), or on demand with
`--deep-review`, a second Claude call runs a strategic critique. It doesn't
pick trades — it critiques the strategy itself, flags behavioural biases,
and pushes back on the weekly voice. Sent as a separate email.

---

## One-time setup (~20 min)

### 1. Trading 212 API key
1. Open the Trading 212 **mobile app**.
2. Tap **Settings → API (beta)**.
3. **Switch to Practice** first, then generate a key — this is a DEMO key
   for paper trading. Use this while you're building confidence.
4. Copy the key somewhere safe; T212 only shows it once.

### 2. Anthropic API key
1. Sign up at https://console.anthropic.com (separate from Claude.ai).
2. Add ~£5 of credit. Each weekly run costs pennies; monthly Opus runs
   are a bit more but still very cheap.
3. Create an API key.

### 3. Gmail App Password
1. Enable 2FA on your Google account.
2. Visit https://myaccount.google.com/apppasswords
3. Create an app password named "Trading Agent" and copy the 16-char code.

### 4. Install and configure
```bash
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# or: venv\Scripts\activate       # Windows

pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in the values
```

### 5. First run
```bash
python trading_agent.py
```
Expected output:
```
[2026-04-19T10:00:00] Run starting (env=demo)
  T212: 0 GBP | 0 positions
  Shadow: £100.00 (+0.00%) vs benchmark None%
  Calling Claude (claude-sonnet-4-6) with web search...
  Claude made 4 actionable recommendations
    - BOUGHT £20.00 of AAPL @ £150.1234
    - BOUGHT £18.00 of VUSA.L @ £85.5000
    ...
  Weekly email sent to you@gmail.com
```

A weekly email arrives within a minute with:
- Current shadow portfolio value and % return
- Return vs benchmark
- This week's applied trades
- Claude's full prose analysis

---

## Models

The script uses **two different Claude models** for different jobs:

| Job | Model | Why |
|---|---|---|
| Weekly review | `claude-sonnet-4-6` | Well-scoped, runs 52x/year, cost-efficient |
| Monthly deep review | `claude-opus-4-7` | Deeper critique, runs ~12x/year |

You can override either in `.env` via `CLAUDE_MODEL_WEEKLY` and
`CLAUDE_MODEL_DEEP`. Don't upgrade the weekly one unless you've got a
specific reason — it gets the job done and the cost compounds.

---

## Command-line flags

```bash
# Normal: weekly run. Also auto-runs deep review on 1st Sunday of month.
python trading_agent.py

# Force a deep review in addition to the weekly run
python trading_agent.py --deep-review

# Run ONLY the deep review, skip the weekly (no new trades applied)
python trading_agent.py --deep-review --skip-weekly
```

---

## Weekly automation

- **Manual**: run `python trading_agent.py` each Sunday evening.
- **Cron (macOS/Linux)**: `crontab -e` then:
  ```
  0 18 * * 0 cd /path/to/project && /path/to/venv/bin/python trading_agent.py
  ```
- **Windows Task Scheduler**: create a weekly task pointing at
  `C:\Users\tagsr\trading_agent\venv\Scripts\python.exe` with the argument
  `C:\Users\tagsr\trading_agent\trading_agent.py` and "Start in"
  `C:\Users\tagsr\trading_agent`.
- **GitHub Actions**: free, runs in the cloud — ask me to set up the workflow.

The deep review auto-triggers on the first Sunday of each month, so if
you've got weekly automation set up, you don't need a second schedule.

---

## Judging whether Claude is any good

After 4–8 weekly runs, look at the snapshots in `shadow_portfolio.json`:
- Is the shadow portfolio beating `VUSA.L`?
- Are the winners and losers balanced, or is one big bet carrying it?
- Does Claude cut losers, or let them ride?

The monthly Opus review will explicitly answer these for you — that's
literally what it's designed to critique.

**Don't go live until you've honestly answered these.** A few lucky weeks
isn't a signal. Look for consistency over at least a month, ideally three.

---

## Going live (only when ready)

1. Run 4+ weeks on DEMO with the shadow tracker.
2. Read at least one monthly deep review.
3. Review the ledger dispassionately. If not convincing, don't go live.
4. When ready: set `T212_ENV=live` in `.env`, swap in a live API key.
5. Fund T212 Invest with £50–100.
6. You can mirror the shadow trades manually, or keep shadow-only as a
   permanent sanity check.

Note: in Stage 1 the script never places real trades regardless of
`T212_ENV`. "Going live" here just means the script is reading your real
T212 account rather than your demo one. Trade automation is a later stage
and deliberately requires code changes, not just a config flip.

---

## Files

- `trading_agent.py` — main script (orchestration, T212, Claude, email)
- `shadow_portfolio.py` — ledger, valuation, benchmark
- `shadow_portfolio.json` — auto-created on first run; persists state
- `.env` — your secrets (keep out of git!)
- `.env.example` — template with all env vars documented

---

## Safety

- Script never writes to T212. Only reads portfolio and sends email.
- Add `.env` and `shadow_portfolio.json` to `.gitignore` if you use git.
- If your API key leaks, revoke in T212 settings immediately.
- This is a small experiment. Your LISA, ISA, and pension do the real
  wealth-building.