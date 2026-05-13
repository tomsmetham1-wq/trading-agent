"""
Shadow portfolio — tracks what would have happened if every one of Claude's
recommendations had been executed. Uses T212 live prices when available
(for held positions), falls back to yfinance for new positions and the
benchmark. Stored in shadow_portfolio.json.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import yfinance as yf


LEDGER_PATH = Path(os.getenv("SHADOW_LEDGER_PATH", "shadow_portfolio.json"))
STARTING_CAPITAL_GBP = float(os.getenv("STARTING_CAPITAL_GBP", "5000"))
BENCHMARK_TICKER = os.getenv("BENCHMARK_TICKER", "VUSA.L")  # Vanguard S&P 500, GBP


# =============================================================================
# Ledger persistence
# =============================================================================

def _default_ledger() -> dict:
    """
    Return a fresh ledger dict for a brand-new portfolio with no history.

    Called by load_ledger() when shadow_portfolio.json does not yet exist.
    The benchmark start price is set to None and recorded on the first real run,
    so the benchmark comparison always starts from the same baseline as the portfolio.
    """
    return {
        "created": datetime.now().isoformat(timespec="seconds"),
        "starting_capital_gbp": STARTING_CAPITAL_GBP,
        "benchmark_ticker": BENCHMARK_TICKER,
        "benchmark_start_price_gbp": None,  # recorded on first run
        "cash_gbp": STARTING_CAPITAL_GBP,
        "positions": {},       # {ticker: {shares, avg_cost_gbp, first_bought, thesis}}
        "trades": [],          # append-only trade log
        "weekly_snapshots": [], # weekly valuation snapshots for performance tracking
    }


def load_ledger() -> dict:
    """
    Load the shadow portfolio ledger from disk.

    Returns the default ledger if the file doesn't yet exist (first run).

    Returns:
        dict: The full ledger dict with positions, trades, and snapshots.
    """
    if not LEDGER_PATH.exists():
        return _default_ledger()
    with open(LEDGER_PATH) as f:
        return json.load(f)


def save_ledger(ledger: dict) -> None:
    """
    Persist the shadow portfolio ledger to disk as JSON.

    Args:
        ledger: The ledger dict to save. Overwrites the existing file.
    """
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2, default=str)


# =============================================================================
# Shadow ↔ T212 bidirectional sync
# =============================================================================

def sync_from_t212(ledger: dict, t212_cash: dict, t212_positions: list,
                   t212_to_yf_fn, bidirectional: bool = True,
                   pending_yf_tickers: set = None) -> bool:
    """
    Reconcile the shadow ledger against T212 (source of truth when T212_DEMO_EXECUTE=true).

    Three things happen in bidirectional mode:
      1. ADD positions T212 holds that shadow is missing (e.g. after a ledger reset,
         or manual trades placed directly in the T212 app).
      2. REMOVE positions shadow holds that T212 doesn't — these are execution failures:
         T212 rejected the order (bad ticker, insufficient funds, etc.) but the old
         shadow-first design had already written the position to the ledger.
      3. SYNC cash to T212's actual available balance.

    In shadow-only mode (bidirectional=False), only step 1 runs — shadow is
    authoritative so phantom positions are never removed.

    This sync runs before Claude is called each week so recommendations are based
    on the real state, not stale shadow data.

    Args:
        ledger:             Shadow ledger dict. Mutated in-place on changes.
        t212_cash:          Raw T212 account summary dict.
        t212_positions:     Raw T212 positions list.
        t212_to_yf_fn:      Callable(t212_ticker) -> yfinance_ticker for translation.
        bidirectional:      True when T212_DEMO_EXECUTE=true (T212 is authoritative).
        pending_yf_tickers: Set of yfinance tickers with open T212 buy orders.
                            These are skipped in the remove step — the order is queued,
                            not failed, so removing the shadow position would be wrong.

    Returns:
        bool: True if any changes were made to the ledger, False if everything matched.
    """
    if not isinstance(t212_positions, list):
        return False

    # Build a map of T212 positions keyed by yfinance ticker.
    # Also capture cost data so we can use the actual purchase price (not current
    # market price) when adding missing positions to shadow.
    #
    # T212 position fields (current API format):
    #   instrument.ticker       — T212 ticker string
    #   instrument.currency     — native currency (field is "currency", not "currencyCode")
    #   quantity                — number of shares held
    #   averagePricePaid        — avg purchase price in native currency
    #   walletImpact.totalCost  — total cost in GBP (best source: no conversion needed)
    t212_by_yf = {}
    for pos in t212_positions:
        if not isinstance(pos, dict):
            continue
        t212_ticker = (
            pos["instrument"].get("ticker", "") if "instrument" in pos
            else pos.get("ticker", "")
        )
        if not t212_ticker:
            continue
        yf_ticker = t212_to_yf_fn(t212_ticker)
        if not yf_ticker:
            continue

        # Native currency: T212 uses "currency" (not "currencyCode") in the
        # instrument sub-object. Fall back to suffix inference for flat format.
        if "instrument" in pos:
            raw_cur = (pos["instrument"].get("currency")
                       or pos["instrument"].get("currencyCode")
                       or "USD")
            currency = str(raw_cur).upper()
        else:
            _sfx_to_cur = {
                "L": "GBX", "DE": "EUR", "PA": "EUR", "AS": "EUR",
                "MI": "EUR", "MC": "EUR", "SW": "CHF", "HK": "HKD",
                "T": "JPY", "TO": "CAD", "AX": "AUD",
            }
            yf_sfx = yf_ticker.rsplit(".", 1)[1].upper() if "." in yf_ticker else ""
            currency = _sfx_to_cur.get(yf_sfx, "USD")

        qty = float(pos.get("quantity", 0))
        # walletImpact.totalCost is the total cost in GBP — derive avg_cost from it.
        wallet       = pos.get("walletImpact", {}) or {}
        total_cost_gbp = float(wallet.get("totalCost") or 0)
        avg_cost_gbp_from_t212 = (total_cost_gbp / qty) if (total_cost_gbp > 0 and qty > 0) else None

        # Fallback: averagePricePaid in native currency (field name in current API)
        avg_price_native = float(pos.get("averagePricePaid") or pos.get("averagePrice") or 0)

        t212_by_yf[yf_ticker] = {
            "shares":              qty,
            "t212_ticker":         t212_ticker,
            "avg_cost_gbp":        avg_cost_gbp_from_t212,   # GBP, no conversion needed
            "avg_price_native":    avg_price_native,           # native currency fallback
            "currency":            currency,
        }

    shadow_tickers = set(ledger["positions"].keys())
    t212_tickers   = set(t212_by_yf.keys())

    missing_in_shadow = t212_tickers - shadow_tickers   # T212 holds it, shadow doesn't
    extra_in_shadow   = shadow_tickers - t212_tickers   # Shadow holds it, T212 doesn't

    # T212 account summary returns flat {"free": ..., "total": ...}
    t212_available = float(
        t212_cash.get("free", 0)
        or t212_cash.get("cash", {}).get("free", 0)
        or t212_cash.get("cash", {}).get("availableToTrade", 0)
    )
    cash_changed = bidirectional and abs(t212_available - ledger["cash_gbp"]) > 1.0

    needs_work = (
        missing_in_shadow
        or (bidirectional and extra_in_shadow)
        or cash_changed
    )
    if not needs_work:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    changed = False

    # Step 1: Add positions T212 holds that shadow is missing
    if missing_in_shadow:
        print(f"  Sync: adding to shadow (held in T212): {sorted(missing_in_shadow)}")
        for yf_ticker in missing_in_shadow:
            entry = t212_by_yf[yf_ticker]

            # Priority for avg_cost_gbp (most accurate first):
            # 1. walletImpact.totalCost / quantity  — already GBP, no conversion
            # 2. averagePricePaid in native currency — needs FX conversion
            # 3. Current market price from yfinance  — stalest, but always available
            avg_cost_gbp = entry.get("avg_cost_gbp")  # from walletImpact

            if not avg_cost_gbp or avg_cost_gbp <= 0:
                avg_native = entry.get("avg_price_native", 0)
                if avg_native and avg_native > 0:
                    avg_cost_gbp = _native_to_gbp(avg_native, entry.get("currency", "USD"))

            if not avg_cost_gbp or avg_cost_gbp <= 0:
                avg_cost_gbp = fetch_price_gbp(yf_ticker)

            if avg_cost_gbp is None:
                print(f"  ! {yf_ticker}: couldn't determine GBP price, skipping sync")
                continue

            ledger["positions"][yf_ticker] = {
                "shares":       entry["shares"],
                "avg_cost_gbp": avg_cost_gbp,
                "first_bought": today,
                "thesis":       "(synced from T212)",
            }
            changed = True

    # Step 2: Remove positions shadow holds that T212 doesn't — bidirectional only.
    # Positions with a pending T212 buy order are excluded: the order is queued
    # (placed outside market hours), not failed, so the shadow position is correct.
    if bidirectional and extra_in_shadow:
        queued    = (pending_yf_tickers or set()) & extra_in_shadow
        to_remove = extra_in_shadow - queued
        if queued:
            print(f"  Sync: keeping in shadow (T212 buy order pending): {sorted(queued)}")
        if to_remove:
            print(f"  Sync: removing from shadow (not in T212): {sorted(to_remove)}")
            for yf_ticker in to_remove:
                del ledger["positions"][yf_ticker]
            changed = True

    # Step 3: Sync cash to T212's actual balance — bidirectional only
    if bidirectional and (cash_changed or changed):
        ledger["cash_gbp"] = t212_available
        changed = True

    if changed:
        ledger["trades"].append({
            "date":   today,
            "action": "SYNC_FROM_T212",
            "ticker": "-",
            "note": (
                f"Bidirectional sync: added {sorted(missing_in_shadow)}, "
                f"removed {sorted(extra_in_shadow)}, "
                f"cash set to £{t212_available:.2f}"
            ),
        })

    return changed


# =============================================================================
# Price fetching with GBP conversion
# =============================================================================

_fx_cache: dict = {}


def _fx_rate(pair: str) -> Optional[float]:
    """
    Fetch a GBP FX rate from yfinance, with an in-process cache.

    The cache prevents redundant network calls when multiple positions share the
    same currency (e.g. five US stocks all needing GBPUSD=X). The cache is
    ephemeral — it lives for one run only, so rates are always fresh per run.

    Args:
        pair: Yahoo Finance FX pair ticker, e.g. "GBPUSD=X".

    Returns:
        float: The current rate, or None if the fetch fails.
    """
    if pair in _fx_cache:
        return _fx_cache[pair]
    try:
        rate = yf.Ticker(pair).fast_info.last_price
        _fx_cache[pair] = rate
        return rate
    except Exception:
        return None


def fetch_price_gbp(yf_ticker: str) -> Optional[float]:
    """
    Fetch the latest price for a ticker and return it in GBP.

    Handles currency conversion for USD and EUR prices using live yfinance FX rates.
    Also handles LSE stocks priced in pence (GBX) by dividing by 100.

    Args:
        yf_ticker: Yahoo Finance ticker string (e.g. "AAPL", "SHEL.L", "ASML.AS").

    Returns:
        float: Latest price in GBP, or None if the price could not be fetched.
    """
    try:
        info = yf.Ticker(yf_ticker).fast_info
        price = info.last_price
        currency = (info.currency or "").upper()
    except Exception as e:
        print(f"  ! price fetch failed for {yf_ticker}: {e}")
        return None

    if price is None or price <= 0:
        return None

    if currency == "GBP":
        return float(price)
    if currency in ("GBX", "GBP.", "GBp"):
        # LSE stocks sometimes report in pence — convert to pounds
        return float(price) / 100
    if currency == "USD":
        fx = _fx_rate("GBPUSD=X")
        return float(price) / fx if fx else None
    if currency == "EUR":
        fx = _fx_rate("GBPEUR=X")
        return float(price) / fx if fx else None
    # Unknown currency — return as-is and warn so the issue is visible in logs
    print(f"  ! unknown currency {currency} for {yf_ticker}, treating as GBP")
    return float(price)


# =============================================================================
# Applying recommendations to the shadow ledger
# =============================================================================

def _apply_buy(ledger: dict, rec: dict, ticker: str,
               price: float, run_date: str) -> str:
    """
    Apply a single BUY recommendation to the shadow ledger.

    Checks that the amount is valid and cash is sufficient, then either opens a
    new position or adds to an existing one (recalculating the average cost).
    Appends the trade to ledger["trades"] and deducts cash.

    Args:
        ledger:   Shadow portfolio ledger dict. Mutated in-place.
        rec:      Recommendation dict from Claude (must have action, ticker, amount_gbp).
        ticker:   yfinance ticker string (pre-extracted from rec).
        price:    Current GBP price (pre-fetched by apply_recommendations).
        run_date: ISO date string for the trade log entry.

    Returns:
        str: Human-readable event string, or a "SKIP ..." string if the trade failed.
    """
    amount_gbp = float(rec.get("amount_gbp") or 0)
    if amount_gbp <= 0:
        return f"SKIP BUY {ticker}: no amount specified"
    if amount_gbp > ledger["cash_gbp"] + 0.01:
        return (
            f"SKIP BUY {ticker}: insufficient cash "
            f"(need £{amount_gbp:.2f}, have £{ledger['cash_gbp']:.2f})"
        )

    shares = amount_gbp / price
    thesis = rec.get("thesis_oneline", "")
    positions = ledger["positions"]

    if ticker in positions:
        # Adding to an existing position: recalculate weighted average cost
        pos = positions[ticker]
        total_cost = pos["shares"] * pos["avg_cost_gbp"] + amount_gbp
        pos["shares"] += shares
        pos["avg_cost_gbp"] = total_cost / pos["shares"]
        # Append the new thesis alongside the original so history is preserved
        if thesis:
            existing = pos.get("thesis", "")
            pos["thesis"] = f"{existing} | [{run_date}] {thesis}".strip(" |")
    else:
        positions[ticker] = {
            "shares":       shares,
            "avg_cost_gbp": price,
            "first_bought": run_date,
            "thesis":       thesis,
        }

    ledger["cash_gbp"] -= amount_gbp
    ledger["trades"].append({
        "date":       run_date,
        "action":     "BUY",
        "ticker":     ticker,
        "shares":     round(shares, 6),
        "price_gbp":  round(price, 4),
        "amount_gbp": round(amount_gbp, 2),
        "thesis":     thesis,
    })
    return f"BOUGHT £{amount_gbp:.2f} of {ticker} @ £{price:.4f}"


def _apply_sell_or_trim(ledger: dict, rec: dict, ticker: str,
                         action: str, price: float, run_date: str) -> str:
    """
    Apply a SELL or TRIM recommendation to the shadow ledger.

    SELL liquidates the entire position. TRIM sells a percentage (trim_pct field,
    defaulting to 50% if not specified). Proceeds are added back to cash.
    The position is deleted if shares fall below a dust threshold (1e-4).

    Args:
        ledger:   Shadow portfolio ledger dict. Mutated in-place.
        rec:      Recommendation dict from Claude.
        ticker:   yfinance ticker string (pre-extracted from rec).
        action:   "SELL" or "TRIM".
        price:    Current GBP price (pre-fetched by apply_recommendations).
        run_date: ISO date string for the trade log entry.

    Returns:
        str: Human-readable event string, or a "SKIP ..." string if not held.
    """
    positions = ledger["positions"]
    if ticker not in positions:
        return f"SKIP {action} {ticker}: no position held"

    pos = positions[ticker]
    if action == "SELL":
        shares_to_sell = pos["shares"]
    else:
        pct = float(rec.get("trim_pct") or 50) / 100
        shares_to_sell = pos["shares"] * pct

    proceeds = shares_to_sell * price
    pos["shares"] -= shares_to_sell
    ledger["cash_gbp"] += proceeds

    exit_thesis = rec.get("thesis_oneline", "")
    ledger["trades"].append({
        "date":         run_date,
        "action":       action,
        "ticker":       ticker,
        "shares":       round(shares_to_sell, 6),
        "price_gbp":    round(price, 4),
        "amount_gbp":   round(proceeds, 2),
        "exit_thesis":  exit_thesis,
        "entry_thesis": pos.get("thesis", ""),
    })

    # Remove dust positions left after a partial trim
    if pos["shares"] < 1e-4:
        del positions[ticker]

    return (
        f"{action} {shares_to_sell:.4f} {ticker} @ £{price:.4f} "
        f"= £{proceeds:.2f}"
    )


def apply_recommendations(ledger: dict, recs: list, run_date: str) -> list[str]:
    """
    Apply a list of trade recommendations to the shadow portfolio ledger.

    Iterates over all recommendations, fetches the current GBP price for each ticker,
    then delegates to _apply_buy() or _apply_sell_or_trim() depending on action type.
    Returns a log of what happened to each recommendation for email reporting.

    Only BUY, SELL, and TRIM actions are processed; HOLD is ignored.

    Args:
        ledger:   Shadow portfolio ledger dict. Mutated in-place.
        recs:     List of recommendation dicts from Claude (extracted from JSON block).
        run_date: ISO date string (YYYY-MM-DD) used for trade log entries.

    Returns:
        list[str]: Human-readable event strings (one per rec), including SKIP messages.
    """
    events = []

    for rec in recs:
        action = rec.get("action", "").upper().strip()
        ticker = rec.get("yfinance_ticker") or rec.get("ticker")
        if not ticker or action not in ("BUY", "SELL", "TRIM"):
            continue

        # Fetch price once here — both _apply_buy and _apply_sell_or_trim need it
        price = fetch_price_gbp(ticker)
        if price is None:
            events.append(f"SKIP {action} {ticker}: no price available")
            continue

        if action == "BUY":
            event = _apply_buy(ledger, rec, ticker, price, run_date)
        else:
            event = _apply_sell_or_trim(ledger, rec, ticker, action, price, run_date)

        events.append(event)

    return events


# =============================================================================
# Valuation — mark-to-market against T212 live prices + yfinance benchmark
# =============================================================================

def _build_t212_price_map(t212_positions: list,
                          t212_to_yf_fn,
                          instruments: list = None) -> dict[str, float]:
    """
    Build a {yf_ticker: price_data} map from T212 live position data.

    T212 positions carry currentPrice (in native currency) and ppl (profit/loss in GBP).
    We store the native price and currency so the valuation step can convert later
    using the same FX logic as fetch_price_gbp.

    Using T212 prices instead of yfinance eliminates the 15-20 minute quote delay
    and FX mismatch that yfinance introduces for US stocks.

    Args:
        t212_positions: Raw T212 positions list from the /equity/positions endpoint.
        t212_to_yf_fn:  Callable(t212_ticker) -> yfinance_ticker for translation.
        instruments:    Optional T212 instruments list for currency lookup fallback.

    Returns:
        dict: {yf_ticker: {price_native, currency, ppl_gbp, qty}} for each position.
    """
    price_map = {}
    if not isinstance(t212_positions, list):
        return price_map

    # Build currency lookup from instruments cache: {t212_ticker: currencyCode}
    inst_currency: dict[str, str] = {}
    if instruments:
        for inst in instruments:
            t = inst.get("ticker", "")
            c = inst.get("currencyCode", "")
            if t and c:
                inst_currency[t] = c.upper()

    for pos in t212_positions:
        if not isinstance(pos, dict):
            continue
        # T212 API returns flat {"ticker": "AAPL_US_EQ", ...}
        t212_ticker = (
            pos["instrument"].get("ticker", "") if "instrument" in pos
            else pos.get("ticker", "")
        )
        if not t212_ticker:
            continue
        yf_ticker = t212_to_yf_fn(t212_ticker)
        if not yf_ticker:
            continue

        try:
            qty                  = float(pos.get("quantity", 0))
            current_price_native = float(pos.get("currentPrice", 0))
            # Currency priority: instrument sub-object → instruments cache → default USD.
            # T212 uses "currency" (not "currencyCode") in the instrument sub-object.
            if "instrument" in pos:
                raw_cur  = (pos["instrument"].get("currency")
                            or pos["instrument"].get("currencyCode")
                            or "USD")
                currency = str(raw_cur).upper()
            else:
                currency = inst_currency.get(t212_ticker, "USD")
            # P&L in GBP is in walletImpact.unrealizedProfitLoss (current API)
            # or top-level ppl (older API format)
            wallet = pos.get("walletImpact", {}) or {}
            ppl_gbp = (wallet.get("unrealizedProfitLoss")
                       or pos.get("ppl"))

            if current_price_native > 0 and qty > 0:
                price_map[yf_ticker] = {
                    "price_native": current_price_native,
                    "currency":     currency,
                    "ppl_gbp":      float(ppl_gbp) if ppl_gbp is not None else None,
                    "qty":          qty,
                }
        except (TypeError, ValueError):
            continue

    return price_map


def _native_to_gbp(price: float, currency: str) -> Optional[float]:
    """
    Convert a price in native currency to GBP using live yfinance FX rates.

    Covers all major currencies that T212 instruments are denominated in.
    Unknown currencies are returned as-is with a warning (better than crashing).

    Args:
        price:    Price in the instrument's native currency.
        currency: ISO currency code string (e.g. "USD", "EUR", "JPY").

    Returns:
        float: Price converted to GBP, or None if the FX rate is unavailable.
    """
    if currency == "GBP":
        return float(price)
    if currency in ("GBX", "GBp"):
        return float(price) / 100
    if currency == "USD":
        fx = _fx_rate("GBPUSD=X")
        return float(price) / fx if fx else None
    if currency == "EUR":
        fx = _fx_rate("GBPEUR=X")
        return float(price) / fx if fx else None
    if currency == "CAD":
        fx = _fx_rate("GBPCAD=X")
        return float(price) / fx if fx else None
    if currency == "AUD":
        fx = _fx_rate("GBPAUD=X")
        return float(price) / fx if fx else None
    if currency == "JPY":
        fx = _fx_rate("GBPJPY=X")
        return float(price) / fx if fx else None
    if currency == "HKD":
        fx = _fx_rate("GBPHKD=X")
        return float(price) / fx if fx else None
    if currency == "CHF":
        fx = _fx_rate("GBPCHF=X")
        return float(price) / fx if fx else None
    print(f"  ! unknown currency {currency}, treating as GBP")
    return float(price)


def _value_position(ticker: str, pos: dict,
                    t212_price_map: Optional[dict]) -> dict:
    """
    Mark a single position to market and return a valuation sub-dict.

    Tries T212 live price first, falls back to yfinance if the position is not
    in the T212 map (e.g. the position was added to shadow but not yet to T212,
    or T212 data was unavailable this run).

    Args:
        ticker:         yfinance ticker string.
        pos:            Position dict from the shadow ledger.
        t212_price_map: Live price map from _build_t212_price_map(), or None.

    Returns:
        dict: Valuation sub-dict with shares, avg_cost, current_price, pnl, etc.
              current_value_gbp and pnl fields are None if price is unavailable.
    """
    price_gbp = None
    price_source = "yfinance"

    if t212_price_map and ticker in t212_price_map:
        t212 = t212_price_map[ticker]
        price_gbp = _native_to_gbp(t212["price_native"], t212["currency"])
        price_source = "T212"

    if price_gbp is None:
        price_gbp = fetch_price_gbp(ticker)
        price_source = "yfinance"

    if price_gbp is None:
        return {
            "shares":            pos["shares"],
            "avg_cost_gbp":      pos["avg_cost_gbp"],
            "current_price_gbp": None,
            "current_value_gbp": None,
            "pnl_pct":           None,
            "note":              "price unavailable",
        }

    value      = pos["shares"] * price_gbp
    cost_basis = pos["shares"] * pos["avg_cost_gbp"]
    return {
        "shares":            round(pos["shares"], 6),
        "avg_cost_gbp":      round(pos["avg_cost_gbp"], 4),
        "current_price_gbp": round(price_gbp, 4),
        "current_value_gbp": round(value, 2),
        "pnl_gbp":           round(value - cost_basis, 2),
        "pnl_pct":           round(((price_gbp / pos["avg_cost_gbp"]) - 1) * 100, 2),
        "first_bought":      pos["first_bought"],
        "price_source":      price_source,
    }


def valuation(ledger: dict, t212_price_map: Optional[dict] = None) -> dict:
    """
    Mark the entire shadow portfolio to market and compute performance vs benchmark.

    For each held position, uses T212 live price if available (avoids the yfinance
    15-20 min delay and FX mismatch). Falls back to yfinance per position if T212
    data isn't available for that ticker.

    The benchmark (VUSA.L) always uses yfinance — it isn't typically a held position
    so it won't be in the T212 price map.

    Args:
        ledger:         Shadow portfolio ledger dict.
        t212_price_map: Optional live price map from _build_t212_price_map().
                        Pass None (or omit) to use yfinance for everything.

    Returns:
        dict: Full valuation snapshot with per-position data, totals, return %, and
              benchmark comparison. All monetary values are in GBP.
    """
    position_values = {}
    total_positions_gbp = 0.0

    for ticker, pos in ledger["positions"].items():
        pv = _value_position(ticker, pos, t212_price_map)
        position_values[ticker] = pv
        if pv.get("current_value_gbp") is not None:
            total_positions_gbp += pv["current_value_gbp"]

    total_value = ledger["cash_gbp"] + total_positions_gbp
    start = ledger["starting_capital_gbp"]

    # Benchmark always uses yfinance (VUSA.L not typically held)
    benchmark_price_now = fetch_price_gbp(ledger["benchmark_ticker"])
    if ledger.get("benchmark_start_price_gbp") is None and benchmark_price_now:
        ledger["benchmark_start_price_gbp"] = benchmark_price_now
    start_bm = ledger.get("benchmark_start_price_gbp")

    if start_bm and benchmark_price_now:
        benchmark_value = start * (benchmark_price_now / start_bm)
        benchmark_pct   = ((benchmark_price_now / start_bm) - 1) * 100
    else:
        benchmark_value = None
        benchmark_pct   = None

    return {
        "cash_gbp":              round(ledger["cash_gbp"], 2),
        "positions_value_gbp":   round(total_positions_gbp, 2),
        "total_value_gbp":       round(total_value, 2),
        "starting_capital_gbp":  start,
        "total_return_gbp":      round(total_value - start, 2),
        "total_return_pct":      round(((total_value / start) - 1) * 100, 2) if start else 0,
        "benchmark_ticker":      ledger["benchmark_ticker"],
        "benchmark_value_gbp":   round(benchmark_value, 2) if benchmark_value else None,
        "benchmark_return_pct":  round(benchmark_pct, 2) if benchmark_pct else None,
        "vs_benchmark_pct": (
            round(((total_value / start) - 1) * 100 - benchmark_pct, 2)
            if benchmark_pct is not None else None
        ),
        "positions": position_values,
    }


# =============================================================================
# Snapshots and reporting
# =============================================================================

def snapshot(ledger: dict, val: dict, run_date: str) -> None:
    """
    Append a weekly valuation snapshot to the ledger for long-term tracking.

    Snapshots are the raw material for the monthly deep review — Opus uses them
    to see how the portfolio has evolved over time and whether it's beating the
    benchmark consistently or just got lucky on one trade.

    Args:
        ledger:   Shadow portfolio ledger dict. Mutated in-place.
        val:      Valuation dict from valuation().
        run_date: ISO date string (YYYY-MM-DD) for the snapshot label.
    """
    ledger["weekly_snapshots"].append({
        "date":                 run_date,
        "total_value_gbp":      val["total_value_gbp"],
        "total_return_pct":     val["total_return_pct"],
        "benchmark_return_pct": val["benchmark_return_pct"],
        "vs_benchmark_pct":     val["vs_benchmark_pct"],
        "position_count":       len(val["positions"]),
    })


def build_thesis_review(ledger: dict, current_val: dict) -> str:
    """
    Build the thesis accountability section for the Claude weekly prompt.

    For each currently held position, shows the original thesis and current P&L
    so Claude must explicitly evaluate whether its own prior reasoning held up.
    Also shows the last 5 exits with entry vs exit reasoning, creating a feedback
    loop that forces the model to learn from closed positions.

    This is one of the most important prompt sections — without it, Claude tends
    to repeat the same picks regardless of how they've actually performed.

    Args:
        ledger:      Shadow portfolio ledger dict.
        current_val: Current valuation dict from valuation().

    Returns:
        str: Formatted thesis accountability section, or empty string if no history.
    """
    lines = []

    positions     = ledger.get("positions", {})
    position_vals = current_val.get("positions", {})
    if positions:
        lines.append("=== Thesis accountability — current positions ===")
        for ticker, pos in positions.items():
            thesis  = pos.get("thesis", "(no thesis recorded)")
            val     = position_vals.get(ticker, {})
            pnl_pct = val.get("pnl_pct")
            bought  = pos.get("first_bought", "?")
            pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "unknown"
            lines.append(
                f"  {ticker} (bought {bought}, P&L: {pnl_str})\n"
                f"    Entry thesis: {thesis}"
            )
        lines.append("")

    # Last 5 closed positions — shows entry reasoning vs actual exit reason
    closed = [
        t for t in ledger.get("trades", [])
        if t.get("action") in ("SELL", "TRIM") and
        (t.get("entry_thesis") or t.get("exit_thesis"))
    ][-5:]

    if closed:
        lines.append("=== Recent exits — entry vs exit reasoning ===")
        for t in closed:
            lines.append(
                f"  {t['action']} {t['ticker']} on {t['date']}\n"
                f"    Entry thesis: {t.get('entry_thesis', '(none recorded)')}\n"
                f"    Exit reason:  {t.get('exit_thesis', '(none recorded)')}"
            )
        lines.append("")

    if not lines:
        return ""

    lines.insert(0,
        "IMPORTANT: Before recommending any action, explicitly state whether\n"
        "each current position's original thesis has played out, broken down,\n"
        "or is still pending. This is your primary accountability check.\n"
    )

    return "\n".join(lines)


def format_valuation_for_email(val: dict) -> str:
    """
    Format the portfolio valuation as a plain-text summary for the email body.

    Includes starting capital, current value (split into cash + positions),
    total return in £ and %, and benchmark comparison if available.
    Each position is shown with shares, average cost, current value, and P&L %.

    Args:
        val: Valuation dict from valuation().

    Returns:
        str: Multi-line plain-text summary ready to embed in the email body.
    """
    lines = [
        "=== Shadow Portfolio Performance ===",
        f"Starting capital: £{val['starting_capital_gbp']:.2f}",
        f"Current value:    £{val['total_value_gbp']:.2f} "
        f"(cash £{val['cash_gbp']:.2f} + positions £{val['positions_value_gbp']:.2f})",
        f"Total return:     £{val['total_return_gbp']:+.2f} "
        f"({val['total_return_pct']:+.2f}%)",
    ]
    if val["benchmark_return_pct"] is not None:
        lines.append(
            f"Benchmark ({val['benchmark_ticker']}): "
            f"{val['benchmark_return_pct']:+.2f}% "
            f"(£{val['benchmark_value_gbp']:.2f})"
        )
        lines.append(f"Claude vs benchmark: {val['vs_benchmark_pct']:+.2f} pts")
    lines.append("")
    lines.append("Positions:")
    if not val["positions"]:
        lines.append("  (none)")
    for ticker, p in val["positions"].items():
        if p.get("current_value_gbp") is None:
            lines.append(f"  {ticker}: {p['shares']:.4f} shares (price unavailable)")
        else:
            lines.append(
                f"  {ticker}: {p['shares']:.4f} sh @ avg £{p['avg_cost_gbp']:.4f} "
                f"-> £{p['current_value_gbp']:.2f} ({p['pnl_pct']:+.2f}%)"
            )
    return "\n".join(lines)
