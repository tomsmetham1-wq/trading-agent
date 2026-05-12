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


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _clean(s: str) -> str:
    """Strip whitespace, quotes, and non-ASCII characters.
    HTTP headers must be latin-1 encodable — this prevents Unicode errors
    from copy-paste artefacts in API keys."""
    if not s:
        return ""
    s = s.strip().strip('"').strip("'")
    return "".join(c for c in s if 32 <= ord(c) <= 126)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Trading 212 client
# -----------------------------------------------------------------------------
def t212_headers() -> dict:
    """Basic auth with key:secret pair (current T212 API).
    Falls back to legacy single-token header if no secret is set."""
    if T212_API_SECRET:
        token = base64.b64encode(
            f"{T212_API_KEY}:{T212_API_SECRET}".encode("ascii")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": T212_API_KEY}


def get_t212_cash() -> dict:
    r = requests.get(
        f"{T212_BASE_URL}/equity/account/summary",
        headers=t212_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


def get_t212_positions() -> list:
    r = requests.get(
        f"{T212_BASE_URL}/equity/positions",
        headers=t212_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


# -----------------------------------------------------------------------------
# Prompt & Claude call
# -----------------------------------------------------------------------------
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


def build_prompt(shadow_val: dict, shadow_ledger: dict,
                 t212_cash: dict, t212_positions: list) -> str:
    shadow_summary = {
        "cash_gbp": shadow_val["cash_gbp"],
        "total_value_gbp": shadow_val["total_value_gbp"],
        "total_return_pct": shadow_val["total_return_pct"],
        "benchmark_return_pct": shadow_val["benchmark_return_pct"],
        "vs_benchmark_pct": shadow_val["vs_benchmark_pct"],
        "positions": shadow_val["positions"],
    }
    recent_trades = shadow_ledger.get("trades", [])[-15:]

    # Compact T212 summary — only send what Claude needs, not the full raw response
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
        "cash_gbp": round(t212_available, 2),
        "total_value_gbp": round(t212_total_val, 2),
        "position_count": len(t212_positions) if isinstance(t212_positions, list) else 0,
        "positions": [
            {
                # T212 positions: flat {"ticker": ...} or nested {"instrument": {"ticker": ...}}
                "ticker": (
                    p["instrument"].get("ticker", "") if "instrument" in p
                    else p.get("ticker", "")
                ),
                "quantity": p.get("quantity"),
                "currentPrice": p.get("currentPrice"),
                "ppl": p.get("ppl"),
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


def call_claude(prompt: str, model: str = CLAUDE_MODEL_WEEKLY) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    import time
    max_retries = 4
    wait = 60  # seconds between retries
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
                    wait *= 2  # Back off: 60s, 120s, 240s
                else:
                    raise
            else:
                raise


def extract_recommendations(text: str) -> list:
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
    return re.sub(r"```json.*?```", "", text, flags=re.DOTALL).strip()


# -----------------------------------------------------------------------------
# Monthly deep review (Opus)
# -----------------------------------------------------------------------------
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


def run_deep_review(ledger: dict, valuation: dict) -> str:
    prompt = DEEP_REVIEW_PROMPT.format(
        ledger_json=json.dumps(ledger, indent=2, default=str),
        snapshots_json=json.dumps(ledger.get("weekly_snapshots", []),
                                  indent=2, default=str),
        valuation_json=json.dumps(valuation, indent=2, default=str),
        today=datetime.now().strftime("%A, %d %B %Y"),
    )
    return call_claude(prompt, model=CLAUDE_MODEL_DEEP)


def is_first_sunday_of_month(d: datetime) -> bool:
    return d.weekday() == 6 and d.day <= 7


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------
def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(EMAIL_SENDER, EMAIL_APP_PASSWORD)
        s.send_message(msg)


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------
def validate_config() -> None:
    missing = [n for n, v in [
        ("T212_API_KEY", T212_API_KEY),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("EMAIL_SENDER", EMAIL_SENDER),
        ("EMAIL_APP_PASSWORD", EMAIL_APP_PASSWORD),
        ("EMAIL_RECIPIENT", EMAIL_RECIPIENT),
    ] if not v]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )


def run_weekly(started: datetime) -> None:
    run_date = started.strftime("%Y-%m-%d")

    t212_cash = get_t212_cash()
    t212_positions = get_t212_positions()
    total = t212_cash.get("total", t212_cash.get("totalValue", t212_cash.get("free", "?")))
    currency = t212_cash.get("currency", "GBP")
    print(f"  T212: {total} {currency} | {len(t212_positions)} positions")

    ledger = sp.load_ledger()

    # Load instruments once, use for both price map and T212 sync
    t212_price_map = {}
    try:
        instruments = t212ex._load_instruments()

        def _t212_to_yf(t212_ticker: str):
            return t212ex.t212_to_yf_ticker(t212_ticker, instruments)

        # Build live price map from T212 position data (replaces yfinance for held positions)
        t212_price_map = sp._build_t212_price_map(t212_positions, _t212_to_yf,
                                                   instruments)

        # Check for pending T212 buy orders so sync doesn't remove positions
        # that are merely queued (placed outside market hours), not failed.
        pending_yf_tickers = (
            t212ex.get_pending_buy_tickers(instruments, _t212_to_yf)
            if t212ex.T212_DEMO_EXECUTE else set()
        )

        # Sync shadow ↔ T212. Runs every week regardless of T212_DEMO_EXECUTE:
        #   bidirectional=True  → T212 is authoritative (removes phantom positions, syncs cash)
        #   bidirectional=False → add-only (catches manual T212 trades; shadow stays authoritative)
        changed = sp.sync_from_t212(ledger, t212_cash, t212_positions,
                                    _t212_to_yf,
                                    bidirectional=t212ex.T212_DEMO_EXECUTE,
                                    pending_yf_tickers=pending_yf_tickers)
        if changed:
            sp.save_ledger(ledger)

    except Exception as e:
        print(f"  ! T212 data error, falling back to yfinance: {e}")

    pre_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    print(f"  Shadow: £{pre_val['total_value_gbp']:.2f} "
          f"({pre_val['total_return_pct']:+.2f}%) "
          f"vs benchmark {pre_val['benchmark_return_pct']}%")

    print(f"  Calling Claude ({CLAUDE_MODEL_WEEKLY}) with web search...")
    prompt = build_prompt(pre_val, ledger, t212_cash, t212_positions)
    response = call_claude(prompt, model=CLAUDE_MODEL_WEEKLY)

    recs = extract_recommendations(response)
    print(f"  Claude made {len(recs)} actionable recommendations")

    # T212 executes first — shadow only mirrors confirmed trades.
    # This prevents drift when T212 fails (insufficient funds, bad ticker, etc).
    t212_events, confirmed_recs = t212ex.execute_recommendations(recs)
    for e in t212_events:
        print(f"    [T212] {e}")

    events = sp.apply_recommendations(ledger, confirmed_recs, run_date) if confirmed_recs else []
    for e in events:
        print(f"    - {e}")

    post_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    sp.snapshot(ledger, post_val, run_date)
    sp.save_ledger(ledger)

    prose = strip_json_block(response)

    # Build T212 live account summary for email
    t212_total = 0.0
    t212_cash_available = 0.0
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
        t212_section = (
            f"=== T212 Demo Account ===\n"
            f"  Account value:  £{t212_total:.2f}\n"
            f"  Positions:      {len(t212_positions)}\n"
            f"  Cash available: £{t212_cash_available:.2f}\n"
        )
    except Exception:
        t212_section = ""

    demo_section = ""
    if t212_events:
        demo_section = (
            "\n=== T212 demo orders placed ===\n"
            + "\n".join(f"  {e}" for e in t212_events)
            + "\n  (Orders queue for next market open if placed out of hours.)\n"
        )
    elif os.getenv("T212_DEMO_EXECUTE", "false").lower() == "true":
        demo_section = "\n=== T212 demo orders ===\n  (none placed this run)\n"

    body = (
        f"Weekly Portfolio Review\n"
        f"Environment: {T212_ENV.upper()}\n"
        f"Model: {CLAUDE_MODEL_WEEKLY}\n"
        f"Generated: {started.strftime('%A, %d %B %Y at %H:%M')}\n\n"
        f"{sp.format_valuation_for_email(post_val)}\n\n"
        f"{t212_section}\n"
        f"=== This week's applied trades (shadow) ===\n"
        + ("\n".join(f"  {e}" for e in events) if events else "  (none)")
        + demo_section
        + "\n\n"
        f"=== Claude's full analysis ===\n\n{prose}\n\n"
        f"---\n"
        f"Shadow portfolio uses T212 live prices for held positions, yfinance\n"
        f"for the benchmark. No real money has been moved (demo account).\n"
    )
    subject = (
        f"Trading Review — {started.strftime('%d %b %Y')} | "
        f"T212: £{t212_total:.0f} | Shadow: £{post_val['total_value_gbp']:.0f} "
        f"({post_val['total_return_pct']:+.2f}%)"
    )

    send_email(subject, body)
    print(f"  Weekly email sent to {EMAIL_RECIPIENT}")


def run_monthly_deep_review(started: datetime) -> None:
    ledger = sp.load_ledger()

    # Use T212 live prices for valuation, same as weekly run
    t212_price_map = {}
    try:
        t212_positions = get_t212_positions()
        instruments = t212ex._load_instruments()
        def _t212_to_yf(t212_ticker: str):
            return t212ex.t212_to_yf_ticker(t212_ticker, instruments)
        t212_price_map = sp._build_t212_price_map(t212_positions, _t212_to_yf,
                                                   instruments)
    except Exception as e:
        print(f"  ! T212 price map failed for deep review: {e}")

    val = sp.valuation(ledger, t212_price_map=t212_price_map)

    n_snapshots = len(ledger.get("weekly_snapshots", []))
    n_trades = len(ledger.get("trades", []))
    if n_snapshots < 2 or n_trades < 3:
        print(f"  ! Not enough history for deep review "
              f"(snapshots={n_snapshots}, trades={n_trades}). Skipping.")
        return

    print(f"  Calling Claude ({CLAUDE_MODEL_DEEP}) for deep review...")
    critique = run_deep_review(ledger, val)

    body = (
        f"Monthly Deep Review (Strategic Critique)\n"
        f"Model: {CLAUDE_MODEL_DEEP}\n"
        f"Generated: {started.strftime('%A, %d %B %Y at %H:%M')}\n\n"
        f"{sp.format_valuation_for_email(val)}\n\n"
        f"=== Critique ===\n\n{critique}\n\n"
        f"---\n"
        f"Meta-review of strategy only. No trades applied.\n"
    )
    subject = (
        f"Monthly Deep Review — {started.strftime('%d %b %Y')} | "
        f"£{val['total_value_gbp']:.2f} "
        f"({val['total_return_pct']:+.2f}%)"
    )
    send_email(subject, body)
    print(f"  Deep review email sent to {EMAIL_RECIPIENT}")


def main() -> None:
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
