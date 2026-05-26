"""
prompts.py — Claude prompt templates and builders for the trading agent.

Edit this file to tune strategy rules, output format, or the deep review criteria.
The two constants (ANALYSIS_PROMPT, DEEP_REVIEW_PROMPT) are the most frequently
changed part of the codebase — keeping them here avoids scrolling through
orchestration code to adjust the model's instructions.
"""

import json
from datetime import datetime

import shadow_portfolio as sp


# =============================================================================
# Weekly analysis prompt (Sonnet + web search)
# =============================================================================

ANALYSIS_PROMPT = """You are a fundamentals-focused investment analyst advising a UK retail
investor who runs an experimental portfolio on Trading 212.

=== Strategy constraints (do not deviate) ===
- Fundamentals-based reasoning only. No momentum or technical chart signals.
- Holding period: weeks to months.
- Target 5–10 concentrated positions once fully invested.
- No single position should exceed 25% of total portfolio value.
- Universe: UK- or US-listed stocks/ETFs on Trading 212.
- Cash reserve: 5–15% of total portfolio value. AVOID sitting in more cash than this —
  uninvested cash is a strategic choice, not a default.
- Minimum position size: 8% of total portfolio value — don't spread too thin.

=== Deployment rules — MANDATORY ===
- Available cash is shown in the shadow portfolio state section below.
- If cash as a percentage of total portfolio value > 15%: you MUST propose enough BUYs
  to bring cash below 15% of total portfolio value.
- Each individual BUY should be 8–25% of total portfolio value. No smaller, no larger.
- Deploy as many positions as needed to get under the 15% cash threshold. On a fresh or
  newly-liquidated portfolio this will naturally be several positions at once; when there
  is only a small excess above 15% it may be just one. Do not drip-feed one small buy
  when significant cash is available, but also don't split into more buys than conviction
  supports.
- Same-run sell-and-reinvest IS allowed: sell orders are always queued before buy orders,
  so sell proceeds are available for buys placed in the same run. When calculating how
  much cash you have available for buys, include: current available cash PLUS the
  estimated proceeds from any SELLs or TRIMs you recommend this run.
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
For each: BUY / SELL / HOLD / TRIM, ticker, % of portfolio or trim %, one-sentence
fundamental thesis. Convert your % to a GBP amount using the total portfolio value
shown in the shadow portfolio state for the JSON block.

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


# =============================================================================
# Monthly deep review prompt (Opus, no web search)
# =============================================================================

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


# =============================================================================
# Prompt builders
# =============================================================================

def build_prompt(shadow_val: dict, shadow_ledger: dict,
                 t212_cash: dict, t212_positions: list) -> str:
    """
    Construct the weekly analysis prompt to send to Claude.

    Assembles the shadow portfolio state, T212 account snapshot, recent trade
    history, and thesis accountability review into the ANALYSIS_PROMPT template.
    Only the fields Claude needs are included — raw T212 responses are condensed
    to prevent unnecessary context bloat and token cost.

    Args:
        shadow_val:     Result of sp.valuation() — mark-to-market shadow portfolio.
        shadow_ledger:  Raw shadow ledger dict (used for the last 15 trades).
        t212_cash:      Raw T212 account summary dict.
        t212_positions: Raw T212 positions list.

    Returns:
        str: Fully formatted prompt string, ready to send to Claude.
    """
    _total = shadow_val["total_value_gbp"] or 1
    shadow_summary = {
        "cash_gbp":             shadow_val["cash_gbp"],
        "cash_pct":             round(shadow_val["cash_gbp"] / _total * 100, 1),
        "total_value_gbp":      shadow_val["total_value_gbp"],
        "total_return_pct":     shadow_val["total_return_pct"],
        "benchmark_return_pct": shadow_val["benchmark_return_pct"],
        "vs_benchmark_pct":     shadow_val["vs_benchmark_pct"],
        "position_count":       len(shadow_val["positions"]),
        "positions": {
            ticker: {k: v for k, v in pos.items() if k not in ("price_source", "pnl_gbp")}
            for ticker, pos in shadow_val["positions"].items()
        },
    }
    recent_trades = [
        t for t in shadow_ledger.get("trades", [])
        if t.get("action") != "SYNC_FROM_T212"
    ][-15:]

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
        "cash_gbp":        round(t212_available, 2),
        "total_value_gbp": round(t212_total_val, 2),
        "position_count":  len(t212_positions) if isinstance(t212_positions, list) else 0,
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

    Passes the full ledger (with sync entries stripped), all weekly snapshots,
    and current valuation so Opus can perform a comprehensive retrospective on
    strategy adherence and performance attribution.

    Args:
        ledger:    Full shadow portfolio ledger dict (includes all trades + snapshots).
        valuation: Current mark-to-market valuation from sp.valuation().

    Returns:
        str: Fully formatted DEEP_REVIEW_PROMPT string.
    """
    filtered_ledger = {
        **ledger,
        "trades": [
            t for t in ledger.get("trades", [])
            if t.get("action") != "SYNC_FROM_T212"
        ],
    }
    return DEEP_REVIEW_PROMPT.format(
        ledger_json=json.dumps(filtered_ledger, indent=2, default=str),
        snapshots_json=json.dumps(
            ledger.get("weekly_snapshots", []), indent=2, default=str
        ),
        valuation_json=json.dumps(valuation, indent=2, default=str),
        today=datetime.now().strftime("%A, %d %B %Y"),
    )
