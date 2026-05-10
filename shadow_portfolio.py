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


# -----------------------------------------------------------------------------
# Ledger persistence
# -----------------------------------------------------------------------------
def _default_ledger() -> dict:
    return {
        "created": datetime.now().isoformat(timespec="seconds"),
        "starting_capital_gbp": STARTING_CAPITAL_GBP,
        "benchmark_ticker": BENCHMARK_TICKER,
        "benchmark_start_price_gbp": None,  # set on first run
        "cash_gbp": STARTING_CAPITAL_GBP,
        "positions": {},  # ticker -> {shares, avg_cost_gbp, first_bought}
        "trades": [],     # append-only
        "weekly_snapshots": [],
    }


def load_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return _default_ledger()
    with open(LEDGER_PATH) as f:
        return json.load(f)


def save_ledger(ledger: dict) -> None:
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2, default=str)


def sync_from_t212(ledger: dict, t212_cash: dict, t212_positions: list,
                   t212_to_yf_fn) -> bool:
    """
    Bidirectional reconciliation of shadow ledger against T212 (source of truth).

    When T212_DEMO_EXECUTE=true, T212 is what actually executed. This sync:
      - ADDS positions T212 holds that shadow is missing (e.g. after ledger reset)
      - REMOVES positions shadow holds that T212 doesn't (execution failures)
      - SYNCS cash to T212's actual available balance

    This runs before Claude is called each week, so shadow always reflects
    reality before new recommendations are made.

    t212_to_yf_fn: a function (t212_ticker) -> yfinance_ticker for translation.
    """
    if not isinstance(t212_positions, list):
        return False

    # Build a map of T212 positions by yfinance ticker
    t212_by_yf = {}
    for pos in t212_positions:
        if not isinstance(pos, dict) or "instrument" not in pos:
            continue
        t212_ticker = pos["instrument"].get("ticker", "")
        yf_ticker = t212_to_yf_fn(t212_ticker)
        if not yf_ticker:
            continue
        t212_by_yf[yf_ticker] = {
            "shares": float(pos.get("quantity", 0)),
            "t212_ticker": t212_ticker,
        }

    shadow_tickers = set(ledger["positions"].keys())
    t212_tickers = set(t212_by_yf.keys())

    missing_in_shadow = t212_tickers - shadow_tickers   # T212 holds it, shadow doesn't
    extra_in_shadow   = shadow_tickers - t212_tickers   # Shadow holds it, T212 doesn't

    t212_available = float(t212_cash.get("cash", {}).get("availableToTrade", 0))
    cash_changed = abs(t212_available - ledger["cash_gbp"]) > 1.0

    if not missing_in_shadow and not extra_in_shadow and not cash_changed:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    changed = False

    # Add positions T212 holds that shadow is missing
    if missing_in_shadow:
        print(f"  Sync: adding to shadow (held in T212): {sorted(missing_in_shadow)}")
        for yf_ticker in missing_in_shadow:
            price_gbp = fetch_price_gbp(yf_ticker)
            if price_gbp is None:
                print(f"  ! {yf_ticker}: couldn't fetch GBP price, skipping sync")
                continue
            ledger["positions"][yf_ticker] = {
                "shares": t212_by_yf[yf_ticker]["shares"],
                "avg_cost_gbp": price_gbp,
                "first_bought": today,
                "thesis": "(synced from T212)",
            }
            changed = True

    # Remove positions shadow holds that T212 never executed
    if extra_in_shadow:
        print(f"  Sync: removing from shadow (never executed in T212): {sorted(extra_in_shadow)}")
        for yf_ticker in extra_in_shadow:
            del ledger["positions"][yf_ticker]
        changed = True

    # Sync cash to T212's actual balance (always, when anything changed)
    if cash_changed or changed:
        ledger["cash_gbp"] = t212_available
        changed = True

    if changed:
        ledger["trades"].append({
            "date": today,
            "action": "SYNC_FROM_T212",
            "ticker": "-",
            "note": (
                f"Bidirectional sync: added {sorted(missing_in_shadow)}, "
                f"removed {sorted(extra_in_shadow)}, "
                f"cash set to £{t212_available:.2f}"
            ),
        })

    return changed


# -----------------------------------------------------------------------------
# Price fetching (with currency conversion to GBP)
# -----------------------------------------------------------------------------
_fx_cache: dict = {}


def _fx_rate(pair: str) -> Optional[float]:
    if pair in _fx_cache:
        return _fx_cache[pair]
    try:
        rate = yf.Ticker(pair).fast_info.last_price
        _fx_cache[pair] = rate
        return rate
    except Exception:
        return None


def fetch_price_gbp(yf_ticker: str) -> Optional[float]:
    """Return the latest price in GBP, converting from USD/EUR if needed."""
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
    # Unknown currency — return as-is and warn
    print(f"  ! unknown currency {currency} for {yf_ticker}, "
          f"treating as GBP")
    return float(price)


# -----------------------------------------------------------------------------
# Applying recommendations
# -----------------------------------------------------------------------------
def apply_recommendations(ledger: dict, recs: list, run_date: str) -> list[str]:
    """Execute recommendations against the shadow portfolio. Returns events log."""
    events = []
    positions = ledger["positions"]

    for rec in recs:
        action = rec.get("action", "").upper().strip()
        ticker = rec.get("yfinance_ticker") or rec.get("ticker")
        if not ticker or action not in ("BUY", "SELL", "TRIM"):
            continue

        price = fetch_price_gbp(ticker)
        if price is None:
            events.append(f"SKIP {action} {ticker}: no price available")
            continue

        if action == "BUY":
            amount_gbp = float(rec.get("amount_gbp") or 0)
            if amount_gbp <= 0:
                events.append(f"SKIP BUY {ticker}: no amount specified")
                continue
            if amount_gbp > ledger["cash_gbp"] + 0.01:
                events.append(
                    f"SKIP BUY {ticker}: insufficient cash "
                    f"(need £{amount_gbp:.2f}, have £{ledger['cash_gbp']:.2f})"
                )
                continue
            shares = amount_gbp / price
            thesis = rec.get("thesis_oneline", "")
            if ticker in positions:
                pos = positions[ticker]
                total_cost = pos["shares"] * pos["avg_cost_gbp"] + amount_gbp
                pos["shares"] += shares
                pos["avg_cost_gbp"] = total_cost / pos["shares"]
                # Append thesis if adding to existing position
                if thesis:
                    existing = pos.get("thesis", "")
                    pos["thesis"] = f"{existing} | [{run_date}] {thesis}".strip(" |")
            else:
                positions[ticker] = {
                    "shares": shares,
                    "avg_cost_gbp": price,
                    "first_bought": run_date,
                    "thesis": thesis,
                }
            ledger["cash_gbp"] -= amount_gbp
            ledger["trades"].append({
                "date": run_date, "action": "BUY", "ticker": ticker,
                "shares": round(shares, 6), "price_gbp": round(price, 4),
                "amount_gbp": round(amount_gbp, 2),
                "thesis": thesis,
            })
            events.append(f"BOUGHT £{amount_gbp:.2f} of {ticker} @ £{price:.4f}")

        elif action in ("SELL", "TRIM"):
            if ticker not in positions:
                events.append(f"SKIP {action} {ticker}: no position held")
                continue
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
                "date": run_date, "action": action, "ticker": ticker,
                "shares": round(shares_to_sell, 6), "price_gbp": round(price, 4),
                "amount_gbp": round(proceeds, 2),
                "exit_thesis": exit_thesis,
                "entry_thesis": pos.get("thesis", ""),
            })
            events.append(
                f"{action} {shares_to_sell:.4f} {ticker} @ £{price:.4f} "
                f"= £{proceeds:.2f}"
            )
            if pos["shares"] < 1e-4:
                del positions[ticker]

    return events


# -----------------------------------------------------------------------------
# Valuation & benchmark
# -----------------------------------------------------------------------------
def _build_t212_price_map(t212_positions: list,
                          t212_to_yf_fn) -> dict[str, float]:
    """
    Build a {yf_ticker: price_gbp} map from T212 live position data.

    T212 positions carry currentPrice (native currency) and ppl (GBP).
    We back-calculate the GBP price as:
        price_gbp = (avg_cost_gbp * shares + ppl) / shares
    which equals current value in GBP / shares.

    Falls back to avg_cost if ppl is missing.
    """
    price_map = {}
    if not isinstance(t212_positions, list):
        return price_map

    for pos in t212_positions:
        if not isinstance(pos, dict) or "instrument" not in pos:
            continue
        t212_ticker = pos["instrument"].get("ticker", "")
        yf_ticker = t212_to_yf_fn(t212_ticker)
        if not yf_ticker:
            continue

        try:
            qty = float(pos.get("quantity", 0))
            current_price_native = float(pos.get("currentPrice", 0))
            currency = pos.get("instrument", {}).get("currencyCode", "USD").upper()
            ppl = pos.get("ppl")  # profit/loss in account currency (GBP)

            if current_price_native > 0 and qty > 0:
                price_map[yf_ticker] = {
                    "price_native": current_price_native,
                    "currency": currency,
                    "ppl_gbp": float(ppl) if ppl is not None else None,
                    "qty": qty,
                }
        except (TypeError, ValueError):
            continue

    return price_map


def _native_to_gbp(price: float, currency: str) -> Optional[float]:
    """Convert a native currency price to GBP using yfinance FX rates.
    This is only called for FX conversion, not for price discovery."""
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
    # Unknown — return as-is and warn
    print(f"  ! unknown currency {currency}, treating as GBP")
    return float(price)


def valuation(ledger: dict,
              t212_price_map: Optional[dict] = None) -> dict:
    """Mark-to-market the shadow portfolio and compare to benchmark.

    t212_price_map: optional {yf_ticker: {price_native, currency, ppl_gbp, qty}}
    from T212 live data. When provided, uses T212 prices for held positions
    instead of yfinance — eliminates the 15-20 min delay and FX mismatch.
    yfinance is still used for the benchmark ticker and any position not
    found in the T212 map.
    """
    position_values = {}
    total_positions_gbp = 0.0

    for ticker, pos in ledger["positions"].items():
        price_gbp = None

        # Try T212 live price first
        if t212_price_map and ticker in t212_price_map:
            t212 = t212_price_map[ticker]
            price_gbp = _native_to_gbp(t212["price_native"], t212["currency"])

        # Fall back to yfinance if T212 data unavailable
        if price_gbp is None:
            price_gbp = fetch_price_gbp(ticker)

        if price_gbp is None:
            position_values[ticker] = {
                "shares": pos["shares"],
                "avg_cost_gbp": pos["avg_cost_gbp"],
                "current_price_gbp": None,
                "current_value_gbp": None,
                "pnl_pct": None,
                "note": "price unavailable",
            }
            continue

        value = pos["shares"] * price_gbp
        cost_basis = pos["shares"] * pos["avg_cost_gbp"]
        total_positions_gbp += value
        price_source = "T212" if (t212_price_map and ticker in t212_price_map) else "yfinance"
        position_values[ticker] = {
            "shares": round(pos["shares"], 6),
            "avg_cost_gbp": round(pos["avg_cost_gbp"], 4),
            "current_price_gbp": round(price_gbp, 4),
            "current_value_gbp": round(value, 2),
            "pnl_gbp": round(value - cost_basis, 2),
            "pnl_pct": round(((price_gbp / pos["avg_cost_gbp"]) - 1) * 100, 2),
            "first_bought": pos["first_bought"],
            "price_source": price_source,
        }

    total_value = ledger["cash_gbp"] + total_positions_gbp
    start = ledger["starting_capital_gbp"]

    # Benchmark always uses yfinance (VUSA.L not typically held)
    benchmark_price_now = fetch_price_gbp(ledger["benchmark_ticker"])
    if ledger.get("benchmark_start_price_gbp") is None and benchmark_price_now:
        ledger["benchmark_start_price_gbp"] = benchmark_price_now
    start_bm = ledger.get("benchmark_start_price_gbp")

    if start_bm and benchmark_price_now:
        benchmark_value = start * (benchmark_price_now / start_bm)
        benchmark_pct = ((benchmark_price_now / start_bm) - 1) * 100
    else:
        benchmark_value = None
        benchmark_pct = None

    return {
        "cash_gbp": round(ledger["cash_gbp"], 2),
        "positions_value_gbp": round(total_positions_gbp, 2),
        "total_value_gbp": round(total_value, 2),
        "starting_capital_gbp": start,
        "total_return_gbp": round(total_value - start, 2),
        "total_return_pct": round(((total_value / start) - 1) * 100, 2) if start else 0,
        "benchmark_ticker": ledger["benchmark_ticker"],
        "benchmark_value_gbp": round(benchmark_value, 2) if benchmark_value else None,
        "benchmark_return_pct": round(benchmark_pct, 2) if benchmark_pct else None,
        "vs_benchmark_pct": (
            round(((total_value / start) - 1) * 100 - benchmark_pct, 2)
            if benchmark_pct is not None else None
        ),
        "positions": position_values,
    }


def snapshot(ledger: dict, val: dict, run_date: str) -> None:
    """Append a weekly valuation snapshot for long-term tracking."""
    ledger["weekly_snapshots"].append({
        "date": run_date,
        "total_value_gbp": val["total_value_gbp"],
        "total_return_pct": val["total_return_pct"],
        "benchmark_return_pct": val["benchmark_return_pct"],
        "vs_benchmark_pct": val["vs_benchmark_pct"],
        "position_count": len(val["positions"]),
    })


def build_thesis_review(ledger: dict, current_val: dict) -> str:
    """
    Build a thesis accountability section for the Claude prompt.
    For each current position, shows the original thesis and current P&L
    so Claude can explicitly evaluate whether its reasoning held up.
    Also shows recent exits with entry vs exit reasoning.
    """
    lines = []

    # Current positions — thesis vs performance
    positions = ledger.get("positions", {})
    position_vals = current_val.get("positions", {})
    if positions:
        lines.append("=== Thesis accountability — current positions ===")
        for ticker, pos in positions.items():
            thesis = pos.get("thesis", "(no thesis recorded)")
            val = position_vals.get(ticker, {})
            pnl_pct = val.get("pnl_pct")
            bought = pos.get("first_bought", "?")
            pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "unknown"
            lines.append(
                f"  {ticker} (bought {bought}, P&L: {pnl_str})\n"
                f"    Entry thesis: {thesis}"
            )
        lines.append("")

    # Recent closed trades — entry vs exit reasoning
    closed = [
        t for t in ledger.get("trades", [])
        if t.get("action") in ("SELL", "TRIM") and
        (t.get("entry_thesis") or t.get("exit_thesis"))
    ][-5:]  # Last 5 exits

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