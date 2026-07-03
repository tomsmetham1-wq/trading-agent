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
from pathlib import Path

import requests
import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

# Load .env BEFORE importing local modules so they can read env vars at import time
load_dotenv()

# Use the OS (Windows) certificate store for TLS verification, not just the
# bundled certifi store. Required when the network has a TLS-intercepting proxy
# or antivirus HTTPS scanning: it presents a certificate signed by a private
# root CA that Windows trusts but certifi doesn't, which otherwise fails every
# Anthropic SDK call with "CERTIFICATE_VERIFY_FAILED: unable to get local issuer
# certificate". This does NOT disable verification — it widens the trust store
# to match what the OS already trusts. No-op (and harmless) on networks with no
# interceptor. Guarded so a missing truststore package degrades to certifi.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

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
CLAUDE_MODEL_DEEP    = _clean(os.getenv("CLAUDE_MODEL_DEEP", "claude-opus-4-8"))

EMAIL_SENDER       = _clean(os.getenv("EMAIL_SENDER", ""))
EMAIL_APP_PASSWORD = _clean(os.getenv("EMAIL_APP_PASSWORD", ""))
EMAIL_RECIPIENT    = _clean(os.getenv("EMAIL_RECIPIENT", ""))

# Crash-recovery journal: written just before T212 execution starts, cleared
# after the ledger is saved. If a run dies in between, the journal survives
# and blocks a same-day re-run from re-executing the same trades.
RUN_JOURNAL_PATH = Path(os.getenv("RUN_JOURNAL_PATH", "run_journal.json"))


# =============================================================================
# Run journal — closes the crash window between T212 execution and ledger save
# =============================================================================

def journal_blocks_run(run_date: str) -> bool:
    """
    Return True if a previous run started executing trades today and never
    finished (crash between order placement and ledger save). In that case
    re-running would place the same orders twice — refuse, and tell the user
    how to recover.

    A journal from a previous day doesn't block: the ledger idempotency check
    and the bidirectional sync handle stale state across days.
    """
    if not RUN_JOURNAL_PATH.exists():
        return False
    try:
        with open(RUN_JOURNAL_PATH, encoding="utf-8") as f:
            journal = json.load(f)
    except Exception as e:
        logger.error(
            "Run journal %s is unreadable (%s). A previous run may have "
            "crashed mid-execution. Verify T212 orders manually, then delete "
            "the file to re-enable runs.", RUN_JOURNAL_PATH, e,
        )
        return True

    if journal.get("date") == run_date and journal.get("status") == "executing":
        logger.error(
            "Run journal shows trade execution started today (%s) but never "
            "completed — a previous run crashed after orders may have reached "
            "T212. NOT re-running. Check the T212 order history, then delete "
            "%s to re-enable runs (next week's sync will reconcile shadow).",
            run_date, RUN_JOURNAL_PATH,
        )
        return True

    # Stale journal from a previous day — safe to discard
    clear_run_journal()
    return False


def write_run_journal(run_date: str, recs: list) -> None:
    """Record that trade execution is about to start (crash marker)."""
    with open(RUN_JOURNAL_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "date":    run_date,
            "status":  "executing",
            "written": datetime.now().isoformat(timespec="seconds"),
            "recs":    recs,
        }, f, indent=2, default=str)


def clear_run_journal() -> None:
    """Remove the crash marker after the ledger has been saved successfully."""
    try:
        RUN_JOURNAL_PATH.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Couldn't remove run journal: %s", e)


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
        # (x or {}) guards against the API returning "cash": null
        t212_cash_available = float(
            t212_cash.get("free", 0)
            or (t212_cash.get("cash") or {}).get("free", 0)
            or (t212_cash.get("cash") or {}).get("availableToTrade", 0)
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

def _create_with_retry(client: Anthropic, **kwargs):
    """
    Run a streamed messages request and return the final assembled Message,
    with retries on rate-limit (429), overload (529), server errors (5xx), and
    transient connection drops, using exponential backoff: 30s → 60s → 120s.
    Client errors (4xx other than 429) are raised immediately — retrying them
    would just repeat the same failure.

    Streaming (not messages.create) is REQUIRED here: the weekly call runs a
    long server-side web-search + adaptive-thinking loop that can take several
    minutes. A non-streaming request holds one HTTP connection idle for that
    whole time and the connection gets dropped (APIConnectionError ~3 min in);
    streaming keeps data flowing so read timeouts never fire. get_final_message
    assembles the complete Message (text + stop_reason) just like create would.
    """
    max_retries = 4
    wait = 30
    last_exc = None
    for attempt in range(max_retries):
        try:
            with client.messages.stream(**kwargs) as stream:
                return stream.get_final_message()
        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.APITimeoutError) as e:
            last_exc = e
        except anthropic.APIStatusError as e:
            if e.status_code < 500:
                raise
            last_exc = e
        if attempt < max_retries - 1:
            logger.warning(
                "API error (%s) — waiting %ds then retrying (attempt %d/%d)...",
                type(last_exc).__name__, wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            wait *= 2
    raise last_exc


def call_claude(user_prompt: str, model: str = CLAUDE_MODEL_WEEKLY,
                use_web_search: bool = True,
                system_prompt: str = None) -> str:
    """
    Send a prompt to Claude and return the combined text response.

    - system_prompt is sent with cache_control=ephemeral so retries within a
      run can reuse the cached prefix.
    - Adaptive thinking is enabled — the model decides how much to reason,
      which materially helps the quality of the weekly analysis.
    - Web search uses the dynamic-filtering tool version, capped at 8 searches
      per call to bound cost.
    - Handles stop_reason="pause_turn": the server-side web search loop can
      pause mid-turn; we resend the paused content so the model resumes and
      finishes (critically, so the trailing JSON block isn't lost).
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    tools = (
        [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        if use_web_search else []
    )

    system_param = None
    if system_prompt:
        system_param = [{"type": "text", "text": system_prompt,
                         "cache_control": {"type": "ephemeral"}}]

    messages = [{"role": "user", "content": user_prompt}]
    text_parts: list[str] = []
    max_continuations = 5

    for continuation in range(max_continuations + 1):
        kwargs = dict(
            model=model,
            # Streaming (in _create_with_retry) means a high ceiling is safe and
            # gives adaptive thinking + web-search summaries + prose + the JSON
            # block room to complete without a max_tokens truncation.
            max_tokens=32000,
            thinking={"type": "adaptive"},
            messages=messages,
        )
        if system_param:
            kwargs["system"] = system_param
        if tools:
            kwargs["tools"] = tools

        response = _create_with_retry(client, **kwargs)
        block_types = [getattr(b, "type", "?") for b in response.content]
        text_parts.extend(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        )
        logger.info(
            "Claude iter=%d stop_reason=%s blocks=%s",
            continuation, response.stop_reason, block_types,
        )

        if response.stop_reason == "pause_turn" and continuation < max_continuations:
            # Server tool loop paused — append the paused assistant content and
            # resend; the API resumes where it left off.
            messages = messages + [{"role": "assistant", "content": response.content}]
            continue
        if response.stop_reason == "max_tokens":
            logger.warning(
                "Claude response hit max_tokens — output may be truncated "
                "(the JSON recommendations block may be missing this run)."
            )
        break

    return "\n\n".join(text_parts).strip()


def extract_recommendations(text: str) -> list:
    """
    Extract the structured JSON recommendations block from Claude's prose response.

    Scans ALL fenced JSON blocks and uses the LAST one that carries a
    "recommendations" key. The model is instructed to END with the block, so
    the last one is authoritative — taking the first would execute an echoed
    example block (e.g. if the model restates the schema in its prose).
    """
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error in candidate block: %s", e)
            continue
        if isinstance(data, dict) and "recommendations" in data:
            recs = data["recommendations"]
            return recs if isinstance(recs, list) else []
    logger.warning("No JSON recommendations block found in Claude response")
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
# Strategy guards — mechanical enforcement of rules Claude sometimes misses
# =============================================================================

FLIP_FLOP_CALENDAR_DAYS = 7   # ≈ 5 trading days
POSITION_HARD_CAP = 0.20      # max 20% of total portfolio value per position
THEME_HARD_CAP = 0.60         # max 60% of total portfolio value per macro theme
MIN_ORDER_GBP = 25.0          # a cap-reduced buy below this is blocked, not placed
CASH_FLOOR_ALERT = 0.04       # warn when planned buys leave cash below ~5% reserve

# Matches "+40%" style triggers inside pre-committed trim text,
# e.g. "Trim 1/3 at +40%, trim another 1/3 at +80%."
_TRIM_TRIGGER_RE = re.compile(r"\+\s*(\d+(?:\.\d+)?)\s*%")


def _rec_ticker(rec: dict) -> str:
    return (rec.get("yfinance_ticker") or rec.get("ticker") or "").strip()


def _recent_full_exit_date(ledger: dict, ticker: str, today) -> str | None:
    """
    Return the date string of the most recent full exit (SELL, or TRIM that
    closed the position) of ticker if it falls within the flip-flop window,
    else None.
    """
    for t in reversed(ledger.get("trades", [])):
        if t.get("ticker") != ticker:
            continue
        is_full_exit = (
            t.get("action") == "SELL"
            or (t.get("action") == "TRIM" and t.get("closed_position"))
        )
        if not is_full_exit:
            continue
        try:
            sell_date = datetime.strptime(t.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            return None
        if (today - sell_date).days < FLIP_FLOP_CALENDAR_DAYS:
            return t["date"]
        return None
    return None


def _pre_commit_trim_alerts(recs: list, ledger: dict, pre_val: dict) -> list[str]:
    """
    Advisory alerts (never blocking) for pre-committed trim levels that have
    been hit but not acted on this run.

    Trim levels are stored as free text at BUY time ("Trim 1/3 at +40%, trim
    another 1/3 at +80%."). The prompt calls them binding, but nothing checked
    compliance in code — this parses the "+N%" triggers, compares against the
    position's current P&L, and flags any level the model ignored. Levels
    already honoured are skipped by counting TRIM trades since the position
    was opened.
    """
    alerts: list[str] = []
    acted = {
        _rec_ticker(r) for r in recs
        if (r.get("action") or "").upper().strip() in ("SELL", "TRIM")
    }
    positions_val = pre_val.get("positions", {})

    for ticker, pos in ledger.get("positions", {}).items():
        trims_text = (pos.get("pre_commit_trims") or "").strip()
        if not trims_text or ticker in acted:
            continue
        pnl_pct = (positions_val.get(ticker, {}) or {}).get("pnl_pct")
        if pnl_pct is None:
            continue
        triggers = sorted(float(m) for m in _TRIM_TRIGGER_RE.findall(trims_text))
        hit = [lvl for lvl in triggers if pnl_pct >= lvl]
        if not hit:
            continue
        trims_done = sum(
            1 for t in ledger.get("trades", [])
            if t.get("action") == "TRIM" and t.get("ticker") == ticker
            and t.get("date", "") >= pos.get("first_bought", "")
        )
        if len(hit) > trims_done:
            alerts.append(
                f"ALERT {ticker}: pre-committed trim level +{hit[trims_done]:.0f}% "
                f"hit (P&L {pnl_pct:+.1f}%) but no TRIM recommended this run"
            )
    return alerts


def enforce_strategy_guards(recs: list, ledger: dict, pre_val: dict) -> tuple[list, list]:
    """
    Filter Claude's recommendations against the hard strategy rules in code.

    The rules already exist in the prompt, but prompt-only rules get violated
    occasionally (e.g. the NVDA buy-rebuy in May 2026, the 81% AI-theme
    concentration in June 2026). Enforced mechanically before any order
    reaches T212:

      1. Flip-flop rule: a BUY is BLOCKED if the same ticker was fully SOLD
         within the last ~5 trading days (7 calendar days) — including a full
         exit recommended IN THIS SAME RUN, which the trade-history check
         can't see because the sell hasn't hit the ledger yet.
      2. Position cap: a BUY that would push a position above 20% of total
         portfolio value is REDUCED to land exactly at the cap (or blocked if
         the position is already over it, or the reduced order would be dust).
         Multiple BUYs of the same ticker in one run are counted cumulatively.
      3. Theme cap: a BUY whose theme label would push that macro theme above
         60% of total portfolio value is REDUCED or BLOCKED. Exposure freed by
         SELL/TRIM recs of the same theme in this run is credited first, so a
         legitimate rebalance-within-theme isn't wrongly blocked.

    Advisory alerts (returned in guard_events but never blocking):
      - a pre-committed trim level was hit but no TRIM was recommended;
      - the planned buys would leave cash below the 5% reserve floor.

    Returns:
        tuple: (filtered_recs, guard_events) — guard_events are human-readable
               strings for the email report.
    """
    guard_events: list[str] = []
    allowed: list[dict] = []
    total = pre_val.get("total_value_gbp") or 0
    positions_val = pre_val.get("positions", {})
    ledger_positions = ledger.get("positions", {})
    today = datetime.now().date()

    def _pos_value(ticker: str) -> float:
        return (positions_val.get(ticker, {}) or {}).get("current_value_gbp") or 0.0

    # Tickers fully exited in THIS run's rec list — a BUY of the same ticker in
    # the same run is a flip-flop the ledger history can't catch.
    same_run_exits: set[str] = set()
    for rec in recs:
        action = (rec.get("action") or "").upper().strip()
        ticker = _rec_ticker(rec)
        if not ticker:
            continue
        if action == "SELL" or (
            action == "TRIM" and float(rec.get("trim_pct") or 50) >= 99.9
        ):
            same_run_exits.add(ticker)

    # Theme exposure from ledger labels, credited with what this run's sells
    # free up (a SELL releases the full position value, a TRIM its trim_pct).
    theme_exposure: dict[str, float] = {}
    for ticker, pos in ledger_positions.items():
        theme = (pos.get("theme") or "").strip().lower()
        if theme:
            theme_exposure[theme] = theme_exposure.get(theme, 0.0) + _pos_value(ticker)
    for rec in recs:
        action = (rec.get("action") or "").upper().strip()
        if action not in ("SELL", "TRIM"):
            continue
        ticker = _rec_ticker(rec)
        theme = ((ledger_positions.get(ticker) or {}).get("theme") or "").strip().lower()
        if not theme:
            continue
        freed = _pos_value(ticker)
        if action == "TRIM":
            freed *= min(float(rec.get("trim_pct") or 50), 100) / 100
        theme_exposure[theme] = theme_exposure.get(theme, 0.0) - freed

    bought_so_far: dict[str, float] = {}  # per-ticker GBP committed this run

    for rec in recs:
        action = (rec.get("action") or "").upper().strip()
        ticker = _rec_ticker(rec)

        if action == "BUY" and ticker:
            # Rule 1: flip-flop — against trade history, then this run's sells.
            # ASCII only — these strings hit the cp1252 Windows console.
            exit_date = _recent_full_exit_date(ledger, ticker, today)
            if exit_date:
                guard_events.append(
                    f"BLOCKED BUY {ticker}: flip-flop rule - "
                    f"sold on {exit_date}, within 5 trading days"
                )
                continue
            if ticker in same_run_exits:
                guard_events.append(
                    f"BLOCKED BUY {ticker}: flip-flop rule - "
                    f"this same run also fully exits {ticker}"
                )
                continue

            amount = float(rec.get("amount_gbp") or 0)

            # Rule 2: position cap — reduce or block buys that breach 20%,
            # counting earlier buys of the same ticker in this run.
            if total > 0:
                current = _pos_value(ticker) + bought_so_far.get(ticker, 0.0)
                max_amount = POSITION_HARD_CAP * total - current
                if max_amount < MIN_ORDER_GBP:
                    guard_events.append(
                        f"BLOCKED BUY {ticker}: position already at/above the "
                        f"20% cap ({current / total * 100:.1f}% of portfolio)"
                    )
                    continue
                if amount > max_amount + 0.01:
                    rec = {**rec, "amount_gbp": round(max_amount, 2)}
                    guard_events.append(
                        f"REDUCED BUY {ticker}: £{amount:.2f} -> £{max_amount:.2f} "
                        f"to stay within the 20% position cap"
                    )
                    amount = round(max_amount, 2)

            # Rule 3: theme cap — reduce or block buys that breach 60% per theme
            theme_label = (rec.get("theme") or "").strip()
            theme = theme_label.lower()
            if total > 0 and theme and amount > 0:
                headroom = THEME_HARD_CAP * total - theme_exposure.get(theme, 0.0)
                if headroom < MIN_ORDER_GBP:
                    guard_events.append(
                        f"BLOCKED BUY {ticker}: '{theme_label}' theme already "
                        f"at/above the 60% cap"
                    )
                    continue
                if amount > headroom + 0.01:
                    rec = {**rec, "amount_gbp": round(headroom, 2)}
                    guard_events.append(
                        f"REDUCED BUY {ticker}: £{amount:.2f} -> £{headroom:.2f} "
                        f"to stay within the 60% '{theme_label}' theme cap"
                    )
                    amount = round(headroom, 2)
            if theme:
                theme_exposure[theme] = theme_exposure.get(theme, 0.0) + amount
            bought_so_far[ticker] = bought_so_far.get(ticker, 0.0) + amount

        allowed.append(rec)

    # Advisory alerts — surfaced in the email, never blocking
    guard_events.extend(_pre_commit_trim_alerts(recs, ledger, pre_val))

    cash = pre_val.get("cash_gbp")
    if cash is not None and total > 0:
        planned_buys = sum(
            float(r.get("amount_gbp") or 0) for r in allowed
            if (r.get("action") or "").upper().strip() == "BUY"
        )
        est_proceeds = 0.0
        for r in allowed:
            a = (r.get("action") or "").upper().strip()
            if a not in ("SELL", "TRIM"):
                continue
            v = _pos_value(_rec_ticker(r))
            est_proceeds += v if a == "SELL" else v * min(float(r.get("trim_pct") or 50), 100) / 100
        cash_after = cash + est_proceeds - planned_buys
        if cash_after < CASH_FLOOR_ALERT * total:
            guard_events.append(
                f"ALERT: planned buys would leave estimated cash at £{cash_after:.2f} "
                f"({cash_after / total * 100:.1f}% of portfolio) - below the 5% reserve floor"
            )

    return allowed, guard_events


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
                             prose: str, prev_snapshot: dict = None,
                             guard_events: list = None) -> str:
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
        if prev_snapshot.get("pricing_incomplete") or post_val.get("pricing_incomplete"):
            perf_section += (
                "\n  (a position had no price in this comparison - "
                "treat week-on-week as indicative only)"
            )
        elif wow_pct <= -15:
            # Kill criterion: single-week drawdown >15% needs a thesis-level
            # explanation — flag it the week it happens, not at month end.
            perf_section += (
                "\n*** KILL-CRITERION WARNING: single-week drawdown over 15%. "
                "Check Claude's analysis for a thesis-level explanation - "
                "if there isn't one, this is a risk-management failure. ***"
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

    guard_section = (
        "\n=== Strategy guard actions (rules enforced in code) ===\n"
        + "\n".join(f"  {e}" for e in guard_events)
        + "\n"
    ) if guard_events else ""

    return (
        f"Weekly Portfolio Review\n"
        f"Environment: {T212_ENV.upper()}\n"
        f"Model: {CLAUDE_MODEL_WEEKLY}\n"
        f"Generated: {started.strftime('%A, %d %B %Y at %H:%M')}\n\n"
        f"{perf_section}\n\n"
        f"{t212_section}\n"
        f"=== This week's applied trades (shadow) ===\n"
        + ("\n".join(f"  {e}" for e in shadow_events) if shadow_events else "  (none)")
        + guard_section
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
    """
    Send a plain-text email via Gmail SMTP using STARTTLS on port 587.

    Port 587 + STARTTLS is used rather than implicit SSL on 465 because Avast
    Mail Shield (and similar AV mail scanners) intercept implicit-SSL 465 with
    a cert whose root isn't in the Windows trust store — the handshake then
    fails "root certificate not trusted by the trust provider". On 587 the AV
    passes Gmail's real certificate through, so truststore verification (Windows
    store) succeeds. No verification is disabled.
    """
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT
    msg.set_content(body)
    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.ehlo()
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
    if journal_blocks_run(run_date):
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

    # Step 5b: mechanical strategy guards (flip-flop rule, 20% position cap)
    recs, guard_events = enforce_strategy_guards(recs, ledger, pre_val)
    for e in guard_events:
        logger.warning("[GUARD] %s", e)

    # Step 6: T212 executes first — shadow mirrors only confirmed trades.
    # The journal closes the crash window: if the process dies after orders
    # reach T212 but before the ledger saves, a same-day re-run is blocked
    # instead of placing the same orders twice.
    if recs:
        write_run_journal(run_date, recs)
    t212_events, shadow_events = execute_and_apply_trades(ledger, recs, run_date)

    # Step 7: Post-trade valuation, persist ledger with snapshot and last_run_date
    prev_snapshot = (ledger.get("weekly_snapshots") or [None])[-1]
    t212_total, _ = extract_t212_totals(t212_cash)
    post_val = sp.valuation(ledger, t212_price_map=t212_price_map)
    sp.snapshot(ledger, post_val, run_date, t212_total_gbp=t212_total)
    ledger["last_run_date"] = run_date
    sp.save_ledger(ledger)
    clear_run_journal()

    # Step 8: Build and send the weekly email
    prose = strip_json_block(response)
    body = build_weekly_email_body(
        started, post_val, t212_cash, t212_positions, shadow_events, t212_events, prose,
        prev_snapshot=prev_snapshot, guard_events=guard_events,
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
