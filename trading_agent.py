"""
UK Retail Trading Agent — Stage 1/2: Decision + Shadow Tracking + Demo Execution

This script:
  1. Pulls your current Trading 212 portfolio and cash balance.
  2. Values the SHADOW portfolio (what Claude's prior recommendations would
     be worth today) and compares to a benchmark ETF.
  3. Asks Claude to analyse it (with live web search) and output:
       - A prose report for your email
       - A structured JSON block of actionable recommendations
  4. Applies the recommendations to the shadow ledger (on paper).
  5. Optionally mirrors trades to your T212 demo account (T212_DEMO_EXECUTE=true).
  6. Emails you the report, performance summary, and events log.
  7. Once a month runs an Opus strategic critique (--deep-review flag).
"""

import json
import os
import re
import base64
import smtplib
import ssl
import time
import argparse
from datetime import datetime
from email.message import EmailMessage

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

# Load .env BEFORE importing local modules so they can read env vars at import time
load_dotenv()

import shadow_portfolio as sp
import t212_executor as t212ex


# =============================================================================
# Helpers
# =============================================================================

def _clean(s: str) -> str:
    """
    Strip whitespace, quotes, and non-ASCII characters from a string.

    HTTP headers must be latin-1 encodable. This prevents Unicode errors caused by
    copy-paste artefacts (smart quotes, invisible characters) in API keys or other
    config values read from .env files.

    Args:
        s: Raw string to clean, typically an env var value.

    Returns:
        ASCII-safe string (printable chars 32–126 only), or empty string if falsy.
    """
    if not s:
        return ""
    s = s.strip().strip('"').strip("'")
    return "".join(c for c in s if 32 <= ord(c) <= 126)


# =============================================================================
# Config — loaded from .env, cleaned for safe HTTP use
# =============================================================================

T212_API_KEY    = _clean(os.getenv("T212_API_KEY", ""))
T212_API_SECRET = _clean(os.getenv("T212_API_SECRET", ""))
T212_ENV        = _clean(os.getenv("T212_ENV", "demo")).lower()
T212_BASE_URL   = (
    "https://live.trading212.com/api/v0"
    if T212_ENV == "live"
    else "https://demo.trading212.com/api/v0"
)

ANTHROPIC_API_KEY    = _clean(os.getenv("ANTHROPIC_API_KEY", ""))
CLAUDE_MODEL_WEEKLY  = _clean(os.getenv("CLAUDE_MODEL_WEEKLY", "claude-sonnet-4-6"))
CLAUDE_MODEL_DEEP    = _clean(os.getenv("CLAUDE_MODEL_DEEP", "claude-opus-4-7"))

EMAIL_SENDER       = _clean(os.getenv("EMAIL_SENDER", ""))
EMAIL_APP_PASSWORD = _clean(os.getenv("EMAIL_APP_PASSWORD", ""))
EMAIL_RECIPIENT    = _clean(os.getenv("EMAIL_RECIPIENT", ""))


# =============================================================================
# Trading 212 API client
# =============================================================================

def t212_headers() -> dict:
    """
    Build the Authorization header for T212 API requests.

    T212 uses HTTP Basic auth with a base64-encoded "key:secret" pair.
    Falls back to a legacy single-token header if T212_API_SECRET is not set
    (some older API key formats only used a single token).

    Returns:
        dict: Headers dict ready to pass to requests.
    """
    if T212_API_SECRET:
        token = base64.b64encode(
            f"{T212_API_KEY}:{T212_API_SECRET}".encode("ascii")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": T212_API_KEY}


def get_t212_cash() -> dict:
    """
    Fetch the T212 account summary, which includes cash balance fields.

    The /equity/account/summary endpoint returns a flat dict. Key fields:
      - "free":  available cash to trade
      - "total": total account value (cash + positions)
      - "currency": account currency (always GBP for UK demo accounts)

    Returns:
        dict: Raw T212 account summary JSON.

    Raises:
        requests.HTTPError: If the API returns a non-2xx status.
    """
    r = requests.get(
        f"{T212_BASE_URL}/equity/account/summary",
        headers=t212_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


def get_t212_positions() -> list:
    """
    Fetch all currently open positions from the T212 account.

    The /equity/positions endpoint returns a list of position dicts. The current
    T212 API uses a flat structure {"ticker": "AAPL_US_EQ", "quantity": ...},
    but older API versions used nested {"instrument": {"ticker": ...}}.
    Both formats are handled throughout the codebase.

    Returns:
        list: List of position dicts from T212.

    Raises:
        requests.HTTPError: If the API returns a non-2xx status.
    """
    r = requests.get(
        f"{T212_BASE_URL}/equity/positions",
        headers=t212_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


def fetch_t212_account() -> tuple[dict, list]:
    """
    Fetch both the T212 account summary and open positions in one call.

    Convenience wrapper around get_t212_cash() and get_t212_positions() that
    also logs a summary line so progress is visible in Task Scheduler output.

    Returns:
        tuple: (t212_cash dict, t212_positions list)
    """
    t212_cash = get_t212_cash()
    t212_positions = get_t212_positions()
    total = t212_cash.get("total", t212_cash.get("totalValue", t212_cash.get("free", "?")))
    currency = t212_cash.get("currency", "GBP")
    print(f"  T212: {total} {currency} | {len(t212_positions)} positions")
    return t212_cash, t212_positions


def extract_t212_totals(t212_cash: dict) -> tuple[float, float]:
    """
    Safely extract total account value and available cash from the T212 summary.

    The T212 API has changed field names across versions, so we try several
    alternatives before defaulting to 0.0. This prevents crashes when T212
    updates its response schema.

    Args:
        t212_cash: Raw account summary dict from get_t212_cash().

    Returns:
        tuple: (total_account_value_gbp, available_cash_gbp).
               Both default to 0.0 if the fields are absent or non-numeric.
    """
    try:
        t212_total = float(
            t212_cash.get("total")
            or t212_cash.get("totalValue")
            or t212_cash.get("free", 0)
        )
        t212_cash_available = float(
            t212_cash.get("free", 0)
            or t212_cash.get("cash", {}).get("free", 0)
            or t212_cash.get("cash", {}).get("availableToTrade", 0)
        )
        return t212_total, t212_cash_available
    except Exception:
        return 0.0, 0.0


# =============================================================================
# Shadow ↔ T212 sync
# =============================================================================

def sync_shadow_with_t212(ledger: dict,
                           t212_cash: dict,
                           t212_positions: list) -> dict:
    """
    Build a live T212 price map and run the bidirectional shadow sync.

    Called once per weekly run, before Claude is consulted, so the shadow ledger
    reflects T212 reality before new recommendations are generated.

    Steps performed:
      1. Load the T212 instruments list (24h cached in t212_instruments.json).
      2. Build a live price map from T212 position data (avoids the 15-min yfinance delay).
      3. Fetch pending buy orders so queued positions aren't wrongly removed.
      4. Run sp.sync_from_t212() — bidirectional when T212_DEMO_EXECUTE=true:
           - Adds positions T212 holds that shadow is missing.
           - Removes phantom shadow positions T212 never successfully executed.
           - Syncs shadow cash to T212's actual available balance.

    On any T212 data error, degrades gracefully: returns an empty price map and
    logs a warning. The valuation step then falls back to yfinance for all tickers.

    Args:
        ledger: Shadow portfolio ledger dict. Mutated in-place if sync changes it.
        t212_cash: T212 account summary from get_t212_cash().
        t212_positions: T212 open positions list from get_t212_positions().

    Returns:
        dict: Live price map {yf_ticker: {price_native, currency, ppl_gbp, qty}},
              or empty dict if T212 data was unavailable.
    """
    t212_price_map = {}
    try:
        instruments = t212ex._load_instruments()

        def _t212_to_yf(t212_ticker: str):
            return t212ex.t212_to_yf_ticker(t212_ticker, instruments)

        t212_price_map = sp._build_t212_price_map(t212_positions, _t212_to_yf, instruments)

        # Fetch pending orders — prevents removing positions whose T212 buy was placed
        # outside market hours (queued, not failed)
        pending_yf_tickers = (
            t212ex.get_pending_buy_tickers(instruments, _t212_to_yf)
            if t212ex.T212_DEMO_EXECUTE else set()
        )

        changed = sp.sync_from_t212(
            ledger, t212_cash, t212_positions,
            _t212_to_yf,
            bidirectional=t212ex.T212_DEMO_EXECUTE,
            pending_yf_tickers=pending_yf_tickers,
        )
        if changed:
            sp.save_ledger(ledger)

    except Exception as e:
        print(f"  ! T212 data error, falling back to yfinance: {e}")

    return t212_price_map


# =============================================================================
# Claude prompts
# =============================================================================

ANALYSIS_PROMPT = """You are a fundamentals-focused investment analyst advising a UK retail
investor who runs a small experimental portfolio (~£5,000) on Trading 212.

=== Strategy constraints (do not deviate) ===
- Fundamentals-based reasoning only. No momentum or technical chart signals.
- Holding period: weeks to months.
- Target 5–10 concentrated positions once fully invested.
- No single position should exceed 25% of total portfolio value (max ~£1,250).
- Universe: UK- or US-listed stocks/ETFs on Trading 212.
- Cash reserve: 5–15% (£250–£750). AVOID sitting in more cash than this —
  uninvested cash is a strategic choice, not a default.
- Minimum position size £400 at this portfolio size — don't spread too thin.

=== Deployment rules — MANDATORY ===
- Available cash is shown in the shadow portfolio state section below.
- If cash > £750: you MUST propose enough BUYs to bring cash below £750.
- Spread new capital across 3–5 positions in a single run — do not drip-feed
  one position at a time. A fresh £5,000 portfolio should be 70–80% invested
  within the first two runs.
- Each individual BUY should be £400–£1,250. No smaller, no larger.
- If you cannot find enough conviction buys to deploy the cash, say so
  explicitly in section 5 — but this should be rare. There are always
  fundamentally sound stocks available somewhere.

=== Shadow portfolio state (what would have happened if every prior
recommendation had been executed) ===
{shadow_state}

=== Trading 212 account snapshot (user's actual account) ===
{t212_state}

=== Recent trade history (shadow, last 15) ===
{trade_history}

=== Thesis accountability ===
{thesis_review}

Today: {today}

Task — use web search to:
  1. Check for material news (last 7 days) affecting current shadow positions.
  2. Ruthlessly reassess prior theses. If a thesis has broken down, say SELL.
     Do not feel committed to prior picks.
  3. Identify 1–3 candidate new positions with a clear fundamental thesis.
  4. Keep total new BUY amounts within available cash.

=== Output format ===

Write a concise prose report in this structure:

**1. Portfolio health check**
One short paragraph.

**2. News & events affecting holdings**
Bulleted, most material first. "Nothing significant" is a valid answer.

**3. Recommended actions this week**
For each: BUY / SELL / HOLD / TRIM, ticker, £ amount or trim %, one-sentence
fundamental thesis.

**4. Watchlist**
1–3 names to research further but not yet actionable, one line each.

**5. Confidence & caveats**
What you don't know. What could invalidate the thesis. Where you're speculating.

Then, on a new line, output a JSON code block with ONLY the actionable items
(BUY, SELL, TRIM — skip HOLD). Use this exact schema:

```json
{{
  "recommendations": [
    {{
      "action": "BUY",
      "ticker": "AAPL",
      "yfinance_ticker": "AAPL",
      "amount_gbp": 500.00,
      "thesis_oneline": "Services margin expansion + buyback cadence."
    }},
    {{
      "action": "TRIM",
      "ticker": "VOD.L",
      "yfinance_ticker": "VOD.L",
      "trim_pct": 50,
      "thesis_oneline": "Thesis broken — exit half, watch Q4 results."
    }}
  ]
}}
```

yfinance_ticker rules — use the exact format Yahoo Finance uses:
  US (NYSE/NASDAQ):  bare symbol         e.g. AAPL, MSFT, NVDA
  UK (LSE):          .L suffix           e.g. SHEL.L, BARC.L, VUSA.L
  Germany (Xetra):   .DE suffix          e.g. SAP.DE, BMW.DE
  France (Paris):    .PA suffix          e.g. MC.PA, BNP.PA
  Netherlands:       .AS suffix          e.g. ASML.AS, UNA.AS
  Switzerland:       .SW suffix          e.g. NESN.SW, ROG.SW
  Spain:             .MC suffix          e.g. SAN.MC, ITX.MC
  Italy:             .MI suffix          e.g. ENI.MI, ISP.MI
  Canada (TSX):      .TO suffix          e.g. SHOP.TO, RY.TO
  Japan (Tokyo):     .T suffix           e.g. 7203.T, 6758.T
  Australia (ASX):   .AX suffix          e.g. CBA.AX, BHP.AX
  Hong Kong:         .HK suffix          e.g. 0700.HK, 0005.HK

Rules:
  - ONE dot only between symbol and suffix (never BA..L — that's malformed).
  - If you're not 100% certain a ticker exists on Yahoo Finance, don't include it.
  - Prefer UK/US listings over foreign ones for simplicity.

IMPORTANT: You MUST end your response with the JSON block, even if you need to
shorten the prose sections. The JSON block is required for trade execution.
If there are no actionable trades, output an empty recommendations list: ```json
{{"recommendations": []}}
```

Be direct. No hype. No disclaimers beyond one line.
"""


DEEP_REVIEW_PROMPT = """You are a senior portfolio strategist reviewing the past month of an AI
trading agent's decisions. Your job is NOT to pick new trades — it's to
critique the strategy itself and the quality of reasoning.

=== Full shadow portfolio ledger ===
{ledger_json}

=== Weekly snapshots (performance over time) ===
{snapshots_json}

=== Current valuation ===
{valuation_json}

Today: {today}

Review the following with intellectual honesty:

**1. Performance attribution**
Is the portfolio beating the benchmark? If yes, is it luck or skill — is one
trade carrying everything, or is performance broad-based? If no, where are
the biggest losses coming from?

**2. Strategy adherence**
Has the agent stuck to the stated rules (5–10 positions, no position >25%,
cash reserve 5–15%, fundamentals-only, weeks-to-months holds)? Call out
specific violations.

**3. Behavioural patterns**
Look for biases: sector concentration, favourite names, reluctance to cut
losers, chasing recent winners, over-trading, under-diversification.

**4. Thesis quality**
Of the theses in the trade log, which held up? Which were wrong? Were the
mistakes about facts (got the data wrong) or judgement (interpreted the data
badly)?

**5. Strategic recommendations**
What should CHANGE in the strategy or prompt for next month? Be specific.
Not "do better" but "reduce max position size to 20%" or "require two
independent catalysts before buying" — things that could be implemented.

**6. Kill criteria**
Under what evidence would you recommend shutting this experiment down?
Be honest — if the agent is underperforming the benchmark after 3+ months,
that's a real signal.

Be blunt. The user is paying for this review specifically because they need
an outside perspective harder than the weekly voice. Don't hedge.
"""


def build_prompt(shadow_val: dict, shadow_ledger: dict,
                 t212_cash: dict, t212_positions: list) -> str:
    """
    Construct the weekly analysis prompt to send to Claude.

    Assembles the shadow portfolio state, T212 account snapshot, recent trade
    history, and thesis accountability review into the ANALYSIS_PROMPT template.
    Only the fields Claude needs are included — raw T212 responses are condensed
    to prevent unnecessary context bloat and token cost.

    Args:
        shadow_val:    Result of sp.valuation() — mark-to-market shadow portfolio.
        shadow_ledger: Raw shadow ledger dict (used for the last 15 trades).
        t212_cash:     Raw T212 account summary dict.
        t212_positions: Raw T212 positions list.

    Returns:
        str: Fully formatted prompt string, ready to send to Claude.
    """
    shadow_summary = {
        "cash_gbp":             shadow_val["cash_gbp"],
        "total_value_gbp":      shadow_val["total_value_gbp"],
        "total_return_pct":     shadow_val["total_return_pct"],
        "benchmark_return_pct": shadow_val["benchmark_return_pct"],
        "vs_benchmark_pct":     shadow_val["vs_benchmark_pct"],
        "positions":            shadow_val["positions"],
    }
    recent_trades = shadow_ledger.get("trades", [])[-15:]

    # Compact T212 summary — only send what Claude needs, not the full raw response.
    # T212 account summary returns flat {"free": ..., "total": ...}
    t212_available = float(
        t212_cash.get("free", 0)
        or t212_cash.get("cash", {}).get("free", 0)
        or t212_cash.get("cash", {}).get("availableToTrade", 0)
    )
    t212_total_val = float(
        t212_cash.get("total", 0)
        or t212_cash.get("totalValue", t212_available)
    )
    t212_summary = {
        "cash_gbp":       round(t212_available, 2),
        "total_value_gbp": round(t212_total_val, 2),
        "position_count": len(t212_positions) if isinstance(t212_positions, list) else 0,
        "positions": [
            {
                # T212 positions: flat {"ticker": ...} or nested {"instrument": {"ticker": ...}}
                "ticker": (
                    p["instrument"].get("ticker", "") if "instrument" in p
                    else p.get("ticker", "")
                ),
                "quantity":     p.get("quantity"),
                "currentPrice": p.get("currentPrice"),
                "ppl":          p.get("ppl"),
            }
            for p in (t212_positions if isinstance(t212_positions, list) else [])
        ],
    }
    return ANALYSIS_PROMPT.format(
        shadow_state=json.dumps(shadow_summary, indent=2, default=str),
        t212_state=json.dumps(t212_summary, indent=2, default=str),
        trade_history=json.dumps(recent_trades, indent=2, default=str) or "(none yet)",
        thesis_review=sp.build_thesis_review(shadow_ledger, shadow_val),
        today=datetime.now().strftime("%A, %d %B %Y"),
    )


def build_deep_review_prompt(ledger: dict, valuation: dict) -> str:
    """
    Construct the monthly deep review prompt for Claude Opus.

    Passes the full ledger, all weekly snapshots, and current valuation so Opus
    can perform a comprehensive retrospective on strategy adherence and
    performance attribution.

    Args:
        ledger:    Full shadow portfolio ledger dict (includes all trades + snapshots).
        valuation: Current mark-to-market valuation from sp.valuation().

    Returns:
        str: Fully formatted DEEP_REVIEW_PROMPT string.
    """
    return DEEP_REVIEW_PROMPT.format(
        ledger_json=json.dumps(ledger, indent=2, default=str),
        snapshots_json=json.dumps(
            ledger.get("weekly_snapshots", []), indent=2, default=str
        ),
        valuation_json=json.dumps(valuation, indent=2, default=str),
        today=datetime.now().strftime("%A, %d %B %Y"),
    )


# =============================================================================
# Claude API calls
# =============================================================================

def call_claude(prompt: str, model: str = CLAUDE_MODEL_WEEKLY) -> str:
    """
    Send a prompt to Claude and return the combined text response.

    Enables the web_search tool so Claude can look up live market data and news.
    Automatically retries on rate-limit errors (HTTP 429) with exponential backoff:
    60s → 120s → 240s (up to 4 attempts total). Other errors are re-raised immediately.

    Args:
        prompt: Full text prompt to send to the model.
        model:  Claude model ID. Defaults to CLAUDE_MODEL_WEEKLY (Sonnet).

    Returns:
        str: All text content blocks from Claude's response joined with double newlines.

    Raises:
        Exception: On non-rate-limit errors, or after exhausting all retries.
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    max_retries = 4
    wait = 60  # seconds — doubles on each retry (exponential backoff)
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}],
            )
            parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            return "\n\n".join(parts).strip()
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                if attempt < max_retries - 1:
                    print(f"  Rate limit hit — waiting {wait}s then retrying "
                          f"(attempt {attempt + 1}/{max_retries})...")
                    time.sleep(wait)
                    wait *= 2  # backoff: 60s, 120s, 240s
                else:
                    raise
            else:
                raise


def extract_recommendations(text: str) -> list:
    """
    Extract the structured JSON recommendations block from Claude's prose response.

    Claude is instructed to end its response with a ```json ... ``` block containing
    a "recommendations" array. This function finds that block via regex, parses it,
    and returns the list. Returns [] on any parse failure rather than crashing the run.

    Args:
        text: Full text returned by call_claude().

    Returns:
        list: Recommendation dicts (action, ticker, amount_gbp, etc.),
              or empty list if no valid JSON block was found.
    """
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"```\s*(\{[^`]*?\"recommendations\".*?\})\s*```",
                          text, re.DOTALL)
    if not match:
        print("  ! No JSON block found in Claude response")
        return []
    try:
        return json.loads(match.group(1)).get("recommendations", [])
    except json.JSONDecodeError as e:
        print(f"  ! JSON parse error: {e}")
        return []


def strip_json_block(text: str) -> str:
    """
    Remove the trailing ```json ... ``` block from Claude's response.

    The JSON recommendations block is embedded in the response for machine
    consumption but should not appear in the email body that Tom reads.

    Args:
        text: Claude's full response text.

    Returns:
        str: Prose portion only, with the JSON code block removed.
    """
    return re.sub(r"```json.*?```", "", text, flags=re.DOTALL).strip()


def get_claude_recommendations(pre_val: dict, ledger: dict,
                                t212_cash: dict, t212_positions: list) -> tuple[str, list]:
    """
    Build the weekly prompt, call Claude (with web search), and extract recommendations.

    Args:
        pre_val:        Pre-trade shadow portfolio valuation (for context in the prompt).
        ledger:         Shadow portfolio ledger dict.
        t212_cash:      T212 account summary dict.
        t212_positions: T212 open positions list.

    Returns:
        tuple: (full_response_text, list_of_recommendation_dicts)
    """
    print(f"  Calling Claude ({CLAUDE_MODEL_WEEKLY}) with web search...")
    prompt = build_prompt(pre_val, ledger, t212_cash, t212_positions)
    response = call_claude(prompt, model=CLAUDE_MODEL_WEEKLY)
    recs = extract_recommendations(response)
    print(f"  Claude made {len(recs)} actionable recommendations")
    return response, recs


# =============================================================================
# Trade execution
# =============================================================================

def execute_and_apply_trades(ledger: dict, recs: list, run_date: str) -> tuple[list, list]:
    """
    Execute recommendations on T212 first, then mirror only confirmed trades to shadow.

    The T212-first order is critical to preventing ledger drift. If T212 fails a trade
    (insufficient funds, bad ticker, market closed), we do NOT write it to shadow.
    In the old design, shadow applied all recs then T212 tried to execute — if T212
    failed, the position appeared in shadow permanently until next week's sync removed it.

    Args:
        ledger:   Shadow portfolio ledger dict. Mutated in-place by apply_recommendations.
        recs:     Recommendation dicts from extract_recommendations().
        run_date: ISO date string (YYYY-MM-DD) for trade log entries.

    Returns:
        tuple: (t212_events list, shadow_events list).
               Both are human-readable strings describing what happened to each trade.
    """
    t212_events, confirmed_recs = t212ex.execute_recommendations(recs)
    for e in t212_events:
        print(f"    [T212] {e}")

    shadow_events = (
        sp.apply_recommendations(ledger, confirmed_recs, run_date)
        if confirmed_recs else []
    )
    for e in shadow_events:
        print(f"    - {e}")

    return t212_events, shadow_events


# =============================================================================
# Monthly deep review (Opus)
# =============================================================================

def run_deep_review(ledger: dict, valuation: dict) -> str:
    """
    Run the monthly strategic critique using Claude Opus.

    Opus reviews the full trade history and snapshot data to assess whether the
    strategy is working — not to pick new trades, but to critique the agent itself:
    performance attribution, rule adherence, behavioural biases, thesis quality.

    Args:
        ledger:    Full shadow portfolio ledger dict.
        valuation: Current valuation from sp.valuation().

    Returns:
        str: Opus's full prose critique.
    """
    prompt = build_deep_review_prompt(ledger, valuation)
    return call_claude(prompt, model=CLAUDE_MODEL_DEEP)


def is_first_sunday_of_month(d: datetime) -> bool:
    """
    Return True if d falls on the first Sunday of its month.

    Used to automatically trigger the monthly deep review without needing the
    --deep-review flag — Task Scheduler only needs to run one command.
    """
    return d.weekday() == 6 and d.day <= 7


def fetch_deep_review_price_map() -> dict:
    """
    Fetch T212 live prices for use in the deep review valuation.

    Mirrors the price-map logic in sync_shadow_with_t212 but without the sync
    step — we only need current prices for an accurate valuation snapshot to
    pass into the Opus prompt.

    Returns:
        dict: Live price map {yf_ticker: price_data}, or empty dict on any error.
              An empty dict causes sp.valuation() to fall back to yfinance prices.
    """
    try:
        t212_positions = get_t212_positions()
        instruments = t212ex._load_instruments()
        def _t212_to_yf(t212_ticker: str):
            return t212ex.t212_to_yf_ticker(t212_ticker, instruments)
        return sp._build_t212_price_map(t212_positions, _t212_to_yf, instruments)
    except Exception as e:
        print(f"  ! T212 price map failed for deep review: {e}")
        return {}


def check_deep_review_prerequisites(ledger: dict) -> bool:
    """
    Verify there is enough history for a meaningful deep review.

    The Opus review is only worth running after at least 2 weekly snapshots and
    3 trades — with less data it would speculate without a real performance signal.

    Args:
        ledger: Shadow portfolio ledger dict.

    Returns:
        bool: True if the review should proceed, False if it should be skipped.
    """
    n_snapshots = len(ledger.get("weekly_snapshots", []))
    n_trades = len(ledger.get("trades", []))
    if n_snapshots < 2 or n_trades < 3:
        print(f"  ! Not enough history for deep review "
              f"(snapshots={n_snapshots}, trades={n_trades}). Skipping.")
        return False
    return True


# =============================================================================
# Email building
# =============================================================================

def build_weekly_email_body(started: datetime, post_val: dict,
                             t212_cash: dict, t212_positions: list,
                             shadow_events: list, t212_events: list,
                             prose: str) -> str:
    """
    Assemble the full weekly report email body.

    Combines: run metadata header, shadow performance summary, T212 account snapshot,
    shadow trade events log, T212 order events, and Claude's prose analysis.

    Args:
        started:        Datetime the run started (for the header timestamp).
        post_val:       Post-trade shadow portfolio valuation.
        t212_cash:      T212 account summary dict (for account value + cash line).
        t212_positions: T212 open positions list (for position count).
        shadow_events:  Human-readable strings from sp.apply_recommendations().
        t212_events:    Human-readable strings from t212ex.execute_recommendations().
        prose:          Claude's analysis text with the JSON block already stripped.

    Returns:
        str: Complete plain-text email body.
    """
    t212_total, t212_cash_available = extract_t212_totals(t212_cash)

    t212_section = (
        f"=== T212 Demo Account ===\n"
        f"  Account value:  £{t212_total:.2f}\n"
        f"  Positions:      {len(t212_positions)}\n"
        f"  Cash available: £{t212_cash_available:.2f}\n"
    ) if t212_total else ""

    if t212_events:
        demo_section = (
            "\n=== T212 demo orders placed ===\n"
            + "\n".join(f"  {e}" for e in t212_events)
            + "\n  (Orders queue for next market open if placed out of hours.)\n"
        )
    elif os.getenv("T212_DEMO_EXECUTE", "false").lower() == "true":
        demo_section = "\n=== T212 demo orders ===\n  (none placed this run)\n"
    else:
        demo_section = ""

    return (
        f"Weekly Portfolio Review\n"
        f"Environment: {T212_ENV.upper()}\n"
        f"Model: {CLAUDE_MODEL_WEEKLY}\n"
        f"Generated: {started.strftime('%A, %d %B %Y at %H:%M')}\n\n"
        f"{sp.format_valuation_for_email(post_val)}\n\n"
        f"{t212_section}\n"
        f"=== This week's applied trades (shadow) ===\n"
        + ("\n".join(f"  {e}" for e in shadow_events) if shadow_events else "  (none)")
        + demo_section
        + "\n\n"
        f"=== Claude's full analysis ===\n\n{prose}\n\n"
        f"---\n"
        f"Shadow portfolio uses T212 live prices for held positions, yfinance\n"
        f"for the benchmark. No real money has been moved (demo account).\n"
    )


def build_weekly_email_subject(started: datetime, t212_total: float, post_val: dict) -> str:
    """
    Build the weekly report email subject line.

    The key numbers (T212 value, shadow return) are visible in the inbox without
    opening the email, which is useful for a quick weekly sanity check.

    Args:
        started:     Run start datetime.
        t212_total:  T212 total account value in GBP.
        post_val:    Post-trade shadow portfolio valuation.

    Returns:
        str: Email subject string.
    """
    return (
        f"Trading Review — {started.strftime('%d %b %Y')} | "
        f"T212: £{t212_total:.0f} | Shadow: £{post_val['total_value_gbp']:.0f} "
        f"({post_val['total_return_pct']:+.2f}%)"
    )


def build_deep_review_email_body(started: datetime, val: dict, critique: str) -> str:
    """
    Assemble the monthly deep review email body.

    Args:
        started:  Datetime the run started.
        val:      Current shadow portfolio valuation.
        critique: Opus's full strategic critique prose.

    Returns:
        str: Complete plain-text email body.
    """
    return (
        f"Monthly Deep Review (Strategic Critique)\n"
        f"Model: {CLAUDE_MODEL_DEEP}\n"
        f"Generated: {started.strftime('%A, %d %B %Y at %H:%M')}\n\n"
        f"{sp.format_valuation_for_email(val)}\n\n"
        f"=== Critique ===\n\n{critique}\n\n"
        f"---\n"
        f"Meta-review of strategy only. No trades applied.\n"
    )


def build_deep_review_email_subject(started: datetime, val: dict) -> str:
    """
    Build the monthly deep review email subject line.

    Args:
        started: Run start datetime.
        val:     Current shadow portfolio valuation.

    Returns:
        str: Email subject string.
    """
    return (
        f"Monthly Deep Review — {started.strftime('%d %b %Y')} | "
        f"£{val['total_value_gbp']:.2f} "
        f"({val['total_return_pct']:+.2f}%)"
    )


# =============================================================================
# Email sending
# =============================================================================

def send_email(subject: str, body: str) -> None:
    """
    Send a plain-text email via Gmail's SMTP SSL service (port 465).

    Uses EMAIL_SENDER with a Gmail App Password — a 16-character code generated
    in Google Account → Security → App Passwords. App Passwords bypass 2FA and
    work with third-party SMTP clients; they are NOT the account password.

    Args:
        subject: Email subject line.
        body:    Plain-text email body.

    Raises:
        smtplib.SMTPException: On authentication failure or network error.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
        s.send_message(msg)


# =============================================================================
# Orchestration
# =============================================================================

def validate_config() -> None:
    """
    Verify all required environment variables are set before the run begins.

    Fails fast with a clear message rather than crashing mid-run with a cryptic
    auth error after Claude has already been called and API credits consumed.

    Raises:
        RuntimeError: If any required env var is missing or empty.
    """
    missing = [n for n, v in [
        ("T212_API_KEY",        T212_API_KEY),
        ("ANTHROPIC_API_KEY",   ANTHROPIC_API_KEY),
        ("EMAIL_SENDER",        EMAIL_SENDER),
        ("EMAIL_APP_PASSWORD",  EMAIL_APP_PASSWORD),
        ("EMAIL_RECIPIENT",     EMAIL_RECIPIENT),
    ] if not v]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )


def run_weekly(started: datetime) -> None:
    """
    Main weekly orchestration: fetch → sync → value → analyse → trade → report.

    Execution order (do not change — each step feeds the next):
      1. Fetch T212 account state (cash + positions)
      2. Load shadow ledger
      3. Sync shadow ↔ T212 and build live price map
      4. Pre-trade valuation (passed to Claude as portfolio context)
      5. Call Claude (Sonnet + web search) → prose report + JSON recommendations
      6. Execute on T212 first → shadow mirrors only confirmed trades
      7. Post-trade valuation, weekly snapshot, save ledger
      8. Build and send the weekly email report

    Args:
        started: Datetime the run started. Used for run_date labelling and
                 email timestamps.
    """
    run_date = started.strftime("%Y-%m-%d")

    # Step 1: Fetch current T212 state
    t212_cash, t212_positions = fetch_t212_account()

    # Steps 2–3: Load ledger, sync shadow with T212, build live price map
    ledger = sp.load_ledger()
    t212_price_map = sync_shadow_with_t212(ledger, t212_cash, t212_positions)

    # Step 4: Pre-trade valuation — passed into Claude prompt as portfolio context
    pre_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    print(f"  Shadow: £{pre_val['total_value_gbp']:.2f} "
          f"({pre_val['total_return_pct']:+.2f}%) "
          f"vs benchmark {pre_val['benchmark_return_pct']}%")

    # Step 5: Claude analysis
    response, recs = get_claude_recommendations(pre_val, ledger, t212_cash, t212_positions)

    # Step 6: T212 executes first — shadow mirrors only confirmed trades
    t212_events, shadow_events = execute_and_apply_trades(ledger, recs, run_date)

    # Step 7: Post-trade valuation, persist ledger with snapshot
    post_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    sp.snapshot(ledger, post_val, run_date)
    sp.save_ledger(ledger)

    # Step 8: Build and send the weekly email
    prose = strip_json_block(response)
    t212_total, _ = extract_t212_totals(t212_cash)
    body = build_weekly_email_body(
        started, post_val, t212_cash, t212_positions, shadow_events, t212_events, prose
    )
    subject = build_weekly_email_subject(started, t212_total, post_val)
    send_email(subject, body)
    print(f"  Weekly email sent to {EMAIL_RECIPIENT}")


def run_monthly_deep_review(started: datetime) -> None:
    """
    Monthly strategic critique using Claude Opus.

    Loads the full ledger, values the portfolio with live T212 prices, and calls
    Opus to assess whether the strategy is working — not to pick trades, but to
    critique the agent's reasoning, rule adherence, and performance attribution.

    Automatically skipped if there is insufficient history (fewer than 2 weekly
    snapshots or fewer than 3 trades) — Opus needs real data, not speculation.

    Args:
        started: Datetime the run started (for email timestamps).
    """
    ledger = sp.load_ledger()
    t212_price_map = fetch_deep_review_price_map()
    val = sp.valuation(ledger, t212_price_map=t212_price_map)

    if not check_deep_review_prerequisites(ledger):
        return

    print(f"  Calling Claude ({CLAUDE_MODEL_DEEP}) for deep review...")
    critique = run_deep_review(ledger, val)

    body = build_deep_review_email_body(started, val, critique)
    subject = build_deep_review_email_subject(started, val)
    send_email(subject, body)
    print(f"  Deep review email sent to {EMAIL_RECIPIENT}")


def main() -> None:
    """
    CLI entry point. Parses arguments and dispatches to run_weekly() and/or
    run_monthly_deep_review() as appropriate.

    CLI flags:
      --deep-review   Force a monthly Opus critique this run (regardless of date).
      --skip-weekly   With --deep-review: run the critique only, skip weekly analysis.

    Automatic behaviour (no flags needed):
      The monthly deep review also runs automatically on the first Sunday of each
      month, so Task Scheduler only needs one command: python trading_agent.py
    """
    parser = argparse.ArgumentParser(
        description="UK retail trading agent — weekly review with shadow tracking."
    )
    parser.add_argument("--deep-review", action="store_true",
                        help="Run monthly Opus strategic critique.")
    parser.add_argument("--skip-weekly", action="store_true",
                        help="With --deep-review, run critique only.")
    args = parser.parse_args()

    validate_config()
    started = datetime.now()
    print(f"[{started.isoformat(timespec='seconds')}] Run starting "
          f"(env={T212_ENV})")

    if args.deep_review and args.skip_weekly:
        run_monthly_deep_review(started)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] Done.")
        return

    run_weekly(started)

    if args.deep_review or is_first_sunday_of_month(started):
        print("  Running monthly deep review...")
        run_monthly_deep_review(started)

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Done.")


if __name__ == "__main__":
    main()
