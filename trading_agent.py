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
import logging
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
from prompts import build_prompt, build_deep_review_prompt

logger = logging.getLogger(__name__)


# =============================================================================
# Helpers
# =============================================================================

def _clean(s: str) -> str:
    """Strip whitespace, quotes, and non-ASCII from a string for safe HTTP header use."""
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
    """Build the Authorization header for T212 API requests (Basic auth)."""
    if T212_API_SECRET:
        token = base64.b64encode(
            f"{T212_API_KEY}:{T212_API_SECRET}".encode("ascii")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    return {"Authorization": T212_API_KEY}


def get_t212_cash() -> dict:
    """Fetch T212 account summary (cash balance fields)."""
    r = requests.get(
        f"{T212_BASE_URL}/equity/account/summary",
        headers=t212_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


def get_t212_positions() -> list:
    """Fetch all currently open positions from T212."""
    r = requests.get(
        f"{T212_BASE_URL}/equity/positions",
        headers=t212_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


def fetch_t212_account() -> tuple[dict, list]:
    """Fetch T212 account summary and open positions."""
    t212_cash = get_t212_cash()
    t212_positions = get_t212_positions()
    total = t212_cash.get("total", t212_cash.get("totalValue", t212_cash.get("free", "?")))
    currency = t212_cash.get("currency", "GBP")
    logger.info("T212: %s %s | %d positions", total, currency, len(t212_positions))
    return t212_cash, t212_positions


def extract_t212_totals(t212_cash: dict) -> tuple[float, float]:
    """Extract total account value and available cash from the T212 summary."""
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
    """Build a live T212 price map and run the bidirectional shadow sync."""
    t212_price_map = {}
    try:
        instruments = t212ex._load_instruments()

        def _t212_to_yf(t212_ticker: str):
            return t212ex.t212_to_yf_ticker(t212_ticker, instruments)

        t212_price_map = sp._build_t212_price_map(t212_positions, _t212_to_yf, instruments)

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
        logger.warning("T212 data error, falling back to yfinance: %s", e)

    return t212_price_map


# =============================================================================
# Claude API calls
# =============================================================================

def call_claude(user_prompt: str, model: str = CLAUDE_MODEL_WEEKLY,
                use_web_search: bool = True,
                system_prompt: str = None) -> str:
    """
    Send a prompt to Claude and return the combined text response.

    When system_prompt is provided it is sent with cache_control=ephemeral so
    Anthropic can cache the static strategy rules between runs, reducing cost.

    Retries on rate-limit (HTTP 429) with exponential backoff: 60s → 120s → 240s.
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    max_retries = 4
    wait = 60
    tools = [{"type": "web_search_20250305", "name": "web_search"}] if use_web_search else []

    # Build system param with cache_control when a system prompt is provided
    system_param = None
    if system_prompt:
        system_param = [{"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}}]

    for attempt in range(max_retries):
        try:
            kwargs = dict(
                model=model,
                max_tokens=8000,
                messages=[{"role": "user", "content": user_prompt}],
            )
            if system_param:
                kwargs["system"] = system_param
            if tools:
                kwargs["tools"] = tools

            response = client.messages.create(**kwargs)
            parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            return "\n\n".join(parts).strip()
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e):
                if attempt < max_retries - 1:
                    logger.warning(
                        "Rate limit hit — waiting %ds then retrying (attempt %d/%d)...",
                        wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    wait *= 2
                else:
                    raise
            else:
                raise


def extract_recommendations(text: str) -> list:
    """Extract the structured JSON recommendations block from Claude's prose response."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        match = re.search(r"```\s*(\{[^`]*?\"recommendations\".*?\})\s*```",
                          text, re.DOTALL)
    if not match:
        logger.warning("No JSON block found in Claude response")
        return []
    try:
        return json.loads(match.group(1)).get("recommendations", [])
    except json.JSONDecodeError as e:
        logger.warning("JSON parse error: %s", e)
        return []


def strip_json_block(text: str) -> str:
    """Remove the trailing ```json ... ``` block from Claude's response."""
    return re.sub(r"```json.*?```", "", text, flags=re.DOTALL).strip()


def get_claude_recommendations(pre_val: dict, ledger: dict,
                                t212_cash: dict, t212_positions: list) -> tuple[str, list]:
    """Build the weekly prompt, call Claude (with web search), and extract recommendations."""
    logger.info("Calling Claude (%s) with web search...", CLAUDE_MODEL_WEEKLY)
    system_prompt, user_prompt = build_prompt(pre_val, ledger, t212_cash, t212_positions)
    response = call_claude(user_prompt, model=CLAUDE_MODEL_WEEKLY,
                           system_prompt=system_prompt)
    recs = extract_recommendations(response)
    logger.info("Claude made %d actionable recommendation(s)", len(recs))
    return response, recs


# =============================================================================
# Trade execution
# =============================================================================

def execute_and_apply_trades(ledger: dict, recs: list, run_date: str) -> tuple[list, list]:
    """Execute recommendations on T212 first, then mirror only confirmed trades to shadow."""
    t212_events, confirmed_recs = t212ex.execute_recommendations(recs)
    for e in t212_events:
        logger.info("[T212] %s", e)

    shadow_events = (
        sp.apply_recommendations(ledger, confirmed_recs, run_date)
        if confirmed_recs else []
    )
    for e in shadow_events:
        logger.info("Shadow: %s", e)

    return t212_events, shadow_events


# =============================================================================
# Monthly deep review (Opus)
# =============================================================================

def run_deep_review(ledger: dict, valuation: dict) -> str:
    """Run the monthly strategic critique using Claude Opus (no web search)."""
    system_prompt, user_prompt = build_deep_review_prompt(ledger, valuation)
    return call_claude(user_prompt, model=CLAUDE_MODEL_DEEP,
                       use_web_search=False, system_prompt=system_prompt)


def is_first_monday_of_month(d: datetime) -> bool:
    """Return True if d falls on the first Monday of its month."""
    return d.weekday() == 0 and d.day <= 7


def fetch_deep_review_price_map() -> dict:
    """Fetch T212 live prices for deep review valuation (no sync step)."""
    try:
        t212_positions = get_t212_positions()
        instruments = t212ex._load_instruments()
        def _t212_to_yf(t212_ticker: str):
            return t212ex.t212_to_yf_ticker(t212_ticker, instruments)
        return sp._build_t212_price_map(t212_positions, _t212_to_yf, instruments)
    except Exception as e:
        logger.warning("T212 price map failed for deep review: %s", e)
        return {}


def check_deep_review_prerequisites(ledger: dict) -> bool:
    """Verify there is enough history for a meaningful deep review."""
    n_snapshots = len(ledger.get("weekly_snapshots", []))
    n_trades = len(ledger.get("trades", []))
    if n_snapshots < 2 or n_trades < 3:
        logger.warning(
            "Not enough history for deep review (snapshots=%d, trades=%d). Skipping.",
            n_snapshots, n_trades,
        )
        return False
    return True


# =============================================================================
# Email building
# =============================================================================

def build_weekly_email_body(started: datetime, post_val: dict,
                             t212_cash: dict, t212_positions: list,
                             shadow_events: list, t212_events: list,
                             prose: str, prev_snapshot: dict = None) -> str:
    """Assemble the full weekly report email body."""
    t212_total, t212_cash_available = extract_t212_totals(t212_cash)

    perf_section = sp.format_valuation_for_email(post_val)
    if prev_snapshot and prev_snapshot.get("total_value_gbp"):
        prev_val = prev_snapshot["total_value_gbp"]
        wow_gbp  = post_val["total_value_gbp"] - prev_val
        wow_pct  = ((post_val["total_value_gbp"] / prev_val) - 1) * 100
        perf_section += (
            f"\n\nWeek-on-week:     £{wow_gbp:+.2f} ({wow_pct:+.2f}%)"
            f" since {prev_snapshot['date']}"
        )

    attribution = sp.format_attribution_for_email(post_val)
    if attribution:
        perf_section += "\n\n" + attribution

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
        f"{perf_section}\n\n"
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
    """Build the weekly report email subject line."""
    return (
        f"Trading Review — {started.strftime('%d %b %Y')} | "
        f"T212: £{t212_total:.0f} | Shadow: £{post_val['total_value_gbp']:.0f} "
        f"({post_val['total_return_pct']:+.2f}%)"
    )


def build_deep_review_email_body(started: datetime, val: dict, critique: str) -> str:
    """Assemble the monthly deep review email body."""
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
    """Build the monthly deep review email subject line."""
    return (
        f"Monthly Deep Review — {started.strftime('%d %b %Y')} | "
        f"£{val['total_value_gbp']:.2f} "
        f"({val['total_return_pct']:+.2f}%)"
    )


# =============================================================================
# Email sending
# =============================================================================

def send_email(subject: str, body: str) -> None:
    """Send a plain-text email via Gmail SMTP SSL (port 465)."""
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
    """Fail fast with a clear message if any required env var is missing."""
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
      2. Load shadow ledger; idempotency check (skip if already ran today)
      3. Sync shadow ↔ T212 and build live price map
      4. Pre-trade valuation (passed to Claude as portfolio context)
      5. Call Claude (Sonnet + web search) → prose report + JSON recommendations
      6. Execute on T212 first → shadow mirrors only confirmed trades
      7. Post-trade valuation, weekly snapshot, save ledger
      8. Build and send the weekly email report
    """
    run_date = started.strftime("%Y-%m-%d")

    # Step 1: Fetch current T212 state
    t212_cash, t212_positions = fetch_t212_account()

    # Steps 2–3: Load ledger, idempotency guard, sync, build live price map
    ledger = sp.load_ledger()
    if ledger.get("last_run_date") == run_date:
        logger.warning(
            "Weekly run already completed today (%s) — skipping to avoid duplicate trades.",
            run_date,
        )
        return
    sp.init_benchmark_start_price(ledger)
    t212_price_map = sync_shadow_with_t212(ledger, t212_cash, t212_positions)

    # Step 4: Pre-trade valuation — passed into Claude prompt as portfolio context
    pre_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    logger.info(
        "Shadow: £%.2f (%+.2f%%) vs benchmark %.2f%%",
        pre_val["total_value_gbp"],
        pre_val["total_return_pct"],
        pre_val["benchmark_return_pct"] or 0,
    )

    # Step 5: Claude analysis
    response, recs = get_claude_recommendations(pre_val, ledger, t212_cash, t212_positions)

    # Step 6: T212 executes first — shadow mirrors only confirmed trades
    t212_events, shadow_events = execute_and_apply_trades(ledger, recs, run_date)

    # Step 7: Post-trade valuation, persist ledger with snapshot and last_run_date
    prev_snapshot = (ledger.get("weekly_snapshots") or [None])[-1]
    t212_total, _ = extract_t212_totals(t212_cash)
    post_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    sp.snapshot(ledger, post_val, run_date, t212_total_gbp=t212_total)
    ledger["last_run_date"] = run_date
    sp.save_ledger(ledger)

    # Step 8: Build and send the weekly email
    prose = strip_json_block(response)
    body = build_weekly_email_body(
        started, post_val, t212_cash, t212_positions, shadow_events, t212_events, prose,
        prev_snapshot=prev_snapshot,
    )
    subject = build_weekly_email_subject(started, t212_total, post_val)
    try:
        send_email(subject, body)
        logger.info("Weekly email sent to %s", EMAIL_RECIPIENT)
    except Exception as e:
        logger.error("Email failed (trades were executed and ledger saved): %s", e)


def run_monthly_deep_review(started: datetime) -> None:
    """
    Monthly strategic critique using Claude Opus.

    Loads the full ledger, values the portfolio with live T212 prices, and calls
    Opus to assess whether the strategy is working. Skipped if insufficient history.
    """
    ledger = sp.load_ledger()
    sp.init_benchmark_start_price(ledger)
    t212_price_map = fetch_deep_review_price_map()
    val = sp.valuation(ledger, t212_price_map=t212_price_map)

    if not check_deep_review_prerequisites(ledger):
        return

    logger.info("Calling Claude (%s) for deep review...", CLAUDE_MODEL_DEEP)
    critique = run_deep_review(ledger, val)

    body = build_deep_review_email_body(started, val, critique)
    subject = build_deep_review_email_subject(started, val)
    try:
        send_email(subject, body)
        logger.info("Deep review email sent to %s", EMAIL_RECIPIENT)
    except Exception as e:
        logger.error("Deep review email failed: %s", e)


def main() -> None:
    """
    CLI entry point. Parses arguments and dispatches to run_weekly() and/or
    run_monthly_deep_review() as appropriate.

    CLI flags:
      --deep-review   Force a monthly Opus critique this run (regardless of date).
      --skip-weekly   With --deep-review: run the critique only, skip weekly analysis.

    Automatic behaviour (no flags needed):
      The monthly deep review also runs automatically on the first Monday of each
      month, so Task Scheduler only needs one command: python trading_agent.py
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

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
    logger.info("Run starting (env=%s)", T212_ENV)

    if args.deep_review and args.skip_weekly:
        run_monthly_deep_review(started)
        logger.info("Done.")
        return

    run_weekly(started)

    if args.deep_review or is_first_monday_of_month(started):
        logger.info("Running monthly deep review...")
        run_monthly_deep_review(started)

    logger.info("Done.")


if __name__ == "__main__":
    main()
