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

import logging

# Point curl_cffi (yfinance's HTTPS client) at the OS trust store BEFORE
# yfinance is imported, so it trusts any local TLS-interceptor root (e.g. Avast
# HTTPS scanning) that Windows already trusts. Without this, every yfinance
# price/FX call fails with "unable to get local issuer certificate" behind such
# an interceptor. No-op on non-Windows / non-intercepted networks.
from os_ca_bundle import ensure_os_ca_bundle
ensure_os_ca_bundle()

import yfinance as yf

logger = logging.getLogger(__name__)

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
        "watchlist": {},       # {ticker: {first_seen, thesis, observations}} — recorded, never gated
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
    # Explicit UTF-8: Windows defaults to cp1252, which corrupts any non-ASCII
    # characters (em-dashes in theses were previously mangled this way).
    with open(LEDGER_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_ledger(ledger: dict) -> None:
    """
    Persist the shadow portfolio ledger to disk as JSON.

    Args:
        ledger: The ledger dict to save. Overwrites the existing file.
    """
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, default=str)


# =============================================================================
# Shadow ↔ T212 bidirectional sync
# =============================================================================

# A held position whose shadow cost differs from T212's actual GBP cost by
# more than this fraction is re-based to T212 on sync. 0.1% is well above
# float rounding and well below the smallest real discrepancy seen (XOM at
# 0.13%, a fill-slippage-plus-FX-fee gap that is itself worth correcting).
COST_REBASE_TOLERANCE = 0.001


def sync_from_t212(ledger: dict, t212_cash: dict, t212_positions: list,
                   t212_to_yf_fn, bidirectional: bool = True,
                   pending_yf_tickers: set = None) -> bool:
    """
    Reconcile the shadow ledger against T212 (source of truth when T212_DEMO_EXECUTE=true).

    Four things happen in bidirectional mode:
      1. ADD positions T212 holds that shadow is missing (e.g. after a ledger reset,
         or manual trades placed directly in the T212 app).
      2. REMOVE positions shadow holds that T212 doesn't — these are execution failures:
         T212 rejected the order (bad ticker, insufficient funds, etc.) but the old
         shadow-first design had already written the position to the ledger.
      3. RE-BASE the cost of positions both sides hold to T212's actual GBP cost
         (walletImpact.totalCost / quantity). Shadow books a BUY at the price it
         saw at run time; T212 fills at market, often the next open, hours later.
         Nothing reconciled the two afterwards, so by Sep 2026 MRVL was carried
         at £145.67 against a real fill of £154.53 (-5.7%) and every "+N% from
         entry" mechanism — pre-committed trim triggers, played-out declarations,
         the giveback peak, the FX split — was measuring from the wrong line.
         Only the wallet figure is trusted for this; the native-price fallback
         needs an FX conversion and would re-base on noise.
      4. SYNC cash to T212's actual available balance.

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

    # T212 account summary returns flat {"free": ..., "total": ...}.
    # (x or {}) guards against the API returning "cash": null.
    t212_available = float(
        t212_cash.get("free", 0)
        or (t212_cash.get("cash") or {}).get("free", 0)
        or (t212_cash.get("cash") or {}).get("availableToTrade", 0)
    )
    cash_changed = bidirectional and abs(t212_available - ledger["cash_gbp"]) > 1.0

    # Held on both sides but carried at a different cost. Tolerance is
    # relative so a £400 lot and a £1,000 lot are judged alike; T212's
    # totalCost is stable between trades, so once re-based a position stays
    # silent until the next fill moves it.
    rebase: dict[str, tuple[float, float]] = {}   # ticker -> (old, new)
    if bidirectional:
        for yf_ticker in shadow_tickers & t212_tickers:
            new_cost = t212_by_yf[yf_ticker].get("avg_cost_gbp")
            old_cost = ledger["positions"][yf_ticker].get("avg_cost_gbp")
            try:
                old_cost = float(old_cost)
            except (TypeError, ValueError):
                old_cost = 0.0
            if not new_cost or new_cost <= 0:
                continue
            if old_cost <= 0 or abs(new_cost / old_cost - 1) > COST_REBASE_TOLERANCE:
                rebase[yf_ticker] = (old_cost, new_cost)

    needs_work = (
        missing_in_shadow
        or (bidirectional and extra_in_shadow)
        or cash_changed
        or rebase
    )
    if not needs_work:
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    changed = False
    added: list[str] = []    # what actually got added/removed — the candidate
    removed: list[str] = []  # sets can shrink (skips, queued orders, wipe guard)
    sync_records: list[dict] = []   # per-ticker SYNC_ADD / SYNC_REMOVE entries

    # Step 1: Add positions T212 holds that shadow is missing
    if missing_in_shadow:
        logger.info("Sync: adding to shadow (held in T212): %s", sorted(missing_in_shadow))
        for yf_ticker in missing_in_shadow:
            entry = t212_by_yf[yf_ticker]

            # Priority for avg_cost_gbp (most accurate first):
            # 1. walletImpact.totalCost / quantity  — already GBP, no conversion
            # 2. averagePricePaid in native currency — needs FX conversion
            # 3. Current market price from yfinance  — stalest, but always available
            avg_cost_gbp = entry.get("avg_cost_gbp")  # from walletImpact
            basis_source = "t212_wallet"

            if not avg_cost_gbp or avg_cost_gbp <= 0:
                avg_native = entry.get("avg_price_native", 0)
                if avg_native and avg_native > 0:
                    avg_cost_gbp = _native_to_gbp(avg_native, entry.get("currency", "USD"))
                    basis_source = "t212_native_fx"

            if not avg_cost_gbp or avg_cost_gbp <= 0:
                avg_cost_gbp = fetch_price_gbp(yf_ticker)
                basis_source = "market_price"

            if avg_cost_gbp is None:
                logger.warning("%s: couldn't determine GBP price, skipping sync", yf_ticker)
                continue

            ledger["positions"][yf_ticker] = {
                "shares":       entry["shares"],
                "avg_cost_gbp": avg_cost_gbp,
                "first_bought": today,
                "thesis":       "(synced from T212)",
            }
            added.append(yf_ticker)
            # Per-ticker record so compute_realized_pnl() can price later sells
            # of these shares. The summary entry below is prose only.
            sync_records.append({
                "date":         today,
                "action":       "SYNC_ADD",
                "ticker":       yf_ticker,
                "shares":       entry["shares"],
                "avg_cost_gbp": avg_cost_gbp,
                "basis_source": basis_source,
            })
            changed = True

    # Step 2: Remove positions shadow holds that T212 doesn't — bidirectional only.
    # Positions with a pending T212 buy order are excluded: the order is queued
    # (placed outside market hours), not failed, so the shadow position is correct.
    #
    # Wipe guard: if T212 reports ZERO positions while shadow holds several, that
    # is far more likely a T212 API glitch (empty 200 response) than a real
    # liquidation — skip the removal step rather than wiping the whole ledger.
    if bidirectional and extra_in_shadow and not t212_tickers and len(shadow_tickers) >= 2:
        logger.warning(
            "Sync: T212 returned 0 positions but shadow holds %d — "
            "skipping removals this run (possible API glitch). "
            "If the account really is empty, this will need manual review.",
            len(shadow_tickers),
        )
        extra_in_shadow = set()

    if bidirectional and extra_in_shadow:
        queued    = (pending_yf_tickers or set()) & extra_in_shadow
        to_remove = extra_in_shadow - queued
        if queued:
            logger.info("Sync: keeping in shadow (T212 buy order pending): %s", sorted(queued))
        if to_remove:
            logger.info("Sync: removing from shadow (not in T212): %s", sorted(to_remove))
            for yf_ticker in to_remove:
                del ledger["positions"][yf_ticker]
                # T212 never held these shares, so any BUY sitting in the log
                # for them is phantom. Record the removal per-ticker or the
                # realised-P&L replay keeps the phantom lots forever and
                # blends them into the basis of a later real position.
                sync_records.append({
                    "date":   today,
                    "action": "SYNC_REMOVE",
                    "ticker": yf_ticker,
                    "note":   "not held at T212 — order rejected or never executed",
                })
            removed = sorted(to_remove)
            changed = True

    # Step 3: Re-base cost of positions held on both sides to T212's fill.
    # Per-ticker SYNC_COST records so compute_realized_pnl() re-prices the
    # open lots too — otherwise the replay keeps scoring later sells against
    # the price shadow guessed, not the one that was paid.
    rebased: list[str] = []
    for yf_ticker, (old_cost, new_cost) in sorted(rebase.items()):
        pos = ledger["positions"][yf_ticker]
        pos["avg_cost_gbp"] = new_cost
        sync_records.append({
            "date":         today,
            "action":       "SYNC_COST",
            "ticker":       yf_ticker,
            "shares":       pos.get("shares"),
            "avg_cost_gbp": new_cost,
            "was_avg_cost_gbp": old_cost,
            "note": (f"cost re-based to T212 fill: £{old_cost:.4f} -> "
                     f"£{new_cost:.4f}/share"),
        })
        logger.info("Sync: %s cost re-based GBP %.4f -> %.4f",
                    yf_ticker, old_cost, new_cost)
        rebased.append(yf_ticker)
        changed = True

    # Step 4: Sync cash to T212's actual balance — bidirectional only
    if bidirectional and (cash_changed or changed):
        ledger["cash_gbp"] = t212_available
        changed = True

    if changed:
        ledger["trades"].extend(sync_records)
        ledger["trades"].append({
            "date":   today,
            "action": "SYNC_FROM_T212",
            "ticker": "-",
            "note": (
                f"Bidirectional sync: added {sorted(added)}, "
                f"removed {removed}, "
                f"cost re-based {rebased}, "
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


# Currency a listing trades in, by yfinance suffix. Everything unsuffixed on
# yfinance is US-listed and therefore USD.
_SUFFIX_CURRENCY = {"L": "GBP", "AS": "EUR", "DE": "EUR", "PA": "EUR",
                    "MI": "EUR", "MC": "EUR", "BR": "EUR", "IR": "EUR"}
FX_PAIR_BY_CURRENCY = {"USD": "GBPUSD=X", "EUR": "GBPEUR=X"}


def position_currency(yf_ticker: str) -> str:
    """Native trading currency for a yfinance ticker, from its suffix."""
    if "." in yf_ticker:
        return _SUFFIX_CURRENCY.get(yf_ticker.rsplit(".", 1)[1].upper(), "USD")
    return "USD"


_fx_history_cache: dict = {}


def fx_rate_on(pair: str, date_iso: str) -> Optional[float]:
    """
    The FX rate on a past date — the last close at or before it.

    Used to reconstruct the rate a position was entered at, so a GBP return can
    be split into the part the business earned and the part the currency moved.
    Falls back to the earliest available close when the date predates the
    series, and returns None if the fetch fails; callers must treat a missing
    rate as "unknown", never as "no FX effect".
    """
    if pair not in _fx_history_cache:
        try:
            hist = yf.Ticker(pair).history(period="2y")["Close"]
            _fx_history_cache[pair] = {
                d.strftime("%Y-%m-%d"): float(v) for d, v in hist.items()
            }
        except Exception as e:
            logger.warning("FX history fetch failed for %s: %s", pair, e)
            _fx_history_cache[pair] = {}
    series = _fx_history_cache[pair]
    if not series:
        return None
    on_or_before = [d for d in series if d <= date_iso]
    if on_or_before:
        return series[max(on_or_before)]
    return series[min(series)]        # position predates the series


def ensure_entry_fx(ledger: dict) -> list[str]:
    """
    Backfill fx_at_entry on any position missing it, from the rate on
    first_bought. Idempotent: a position that already carries the field is left
    alone, so this costs one FX history fetch on the first run and nothing after.

    A rate reconstructed from first_bought is marked fx_basis "estimated" — for
    a position built from several buys at different rates it is the first one,
    not the cost-weighted blend that a position bought under this code records.
    Right order of magnitude, not exact, in the same spirit as
    tickers_with_estimated_basis in the realised-P&L replay.

    Returns a list of human-readable notes for the run log.
    """
    notes: list[str] = []
    for ticker, pos in (ledger.get("positions") or {}).items():
        if pos.get("fx_at_entry") or not pos.get("first_bought"):
            continue
        currency = position_currency(ticker)
        if currency == "GBP":
            pos["fx_at_entry"] = 1.0
            pos["fx_basis"] = "none"        # no currency risk to decompose
            continue
        pair = FX_PAIR_BY_CURRENCY.get(currency)
        rate = fx_rate_on(pair, pos["first_bought"]) if pair else None
        if rate is None:
            continue                        # leave absent; retry next run
        pos["fx_at_entry"] = rate
        pos["fx_basis"] = "estimated"
        notes.append(f"{ticker}: entry FX {rate:.4f} ({pos['first_bought']})")
    return notes


def fx_neutral_returns(ledger: dict, valuation_result: dict) -> dict:
    """
    Split each position's GBP return into the business return and the currency.

    Every holding is priced in GBP, so a USD position's reported P&L blends what
    the company did with what sterling did, and nothing in the ledger separated
    them. That let a losing position be explained away as "a GBP FX artefact"
    with no number attached — and on 2026-09-01 it was, for the three red AI
    names, while the largest actual FX drags sat on XOM and JPM, which the same
    report described as unqualified winners.

    local_pct is the return in the position's own currency:
        (1 + gbp_return) * (fx_now / fx_at_entry) - 1
    fx_pts is gbp_pct - local_pct: negative means sterling strength cost you,
    positive means it helped.

    Returns {ticker: {gbp_pct, local_pct, fx_pts, currency, estimated}} for the
    positions it can decompose. A position with no stored entry rate, no live
    rate, or no P&L is omitted rather than guessed at.
    """
    out: dict = {}
    positions = ledger.get("positions") or {}
    for ticker, pv in (valuation_result.get("positions") or {}).items():
        pos = positions.get(ticker) or {}
        gbp_pct = pv.get("pnl_pct")
        fx0 = pos.get("fx_at_entry")
        if gbp_pct is None or not fx0:
            continue
        currency = position_currency(ticker)
        if currency == "GBP":
            fx_now = 1.0
        else:
            pair = FX_PAIR_BY_CURRENCY.get(currency)
            fx_now = _fx_rate(pair) if pair else None
        if not fx_now:
            continue
        local_pct = ((1 + gbp_pct / 100) * (fx_now / fx0) - 1) * 100
        out[ticker] = {
            "gbp_pct":   gbp_pct,
            "local_pct": local_pct,
            "fx_pts":    gbp_pct - local_pct,
            "currency":  currency,
            "estimated": pos.get("fx_basis") == "estimated",
        }
    return out


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
        raw_currency = info.currency or ""
        currency = raw_currency.upper()
    except Exception as e:
        logger.warning("price fetch failed for %s: %s", yf_ticker, e)
        return None

    if price is None or price <= 0:
        return None

    # Pence check MUST come before the GBP check: yfinance reports LSE prices
    # with currency "GBp" (pence), which uppercases to "GBP" and would otherwise
    # be treated as pounds — a silent 100x overvaluation.
    if raw_currency == "GBp" or currency in ("GBX", "GBP."):
        return float(price) / 100
    if currency == "GBP":
        return float(price)
    if currency == "USD":
        fx = _fx_rate("GBPUSD=X")
        return float(price) / fx if fx else None
    if currency == "EUR":
        fx = _fx_rate("GBPEUR=X")
        return float(price) / fx if fx else None
    # Unknown currency — return as-is and warn so the issue is visible in logs
    logger.warning("unknown currency %s for %s, treating as GBP", currency, yf_ticker)
    return float(price)


# =============================================================================
# Applying recommendations to the shadow ledger
# =============================================================================

def _find_entry_thesis(ledger: dict, ticker: str) -> str:
    """Search trade history in reverse for the last BUY thesis recorded for ticker."""
    for t in reversed(ledger.get("trades", [])):
        if t.get("action") == "BUY" and t.get("ticker") == ticker and t.get("thesis"):
            return t["thesis"]
    return ""


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
    theme  = (rec.get("theme") or "").strip()
    pre_commit_trims = (rec.get("pre_commit_trims") or "").strip()
    positions = ledger["positions"]

    # Rate this buy actually went on at, so the position's GBP return can later
    # be split from the currency's contribution. A top-up blends cost-weighted,
    # exactly as avg_cost_gbp does; the blend is only as good as its weakest
    # part, so an estimated leg keeps the whole basis estimated.
    currency = position_currency(ticker)
    if currency == "GBP":
        buy_fx, buy_basis = 1.0, "none"
    else:
        pair = FX_PAIR_BY_CURRENCY.get(currency)
        buy_fx = _fx_rate(pair) if pair else None
        buy_basis = "actual" if buy_fx else None

    if ticker in positions:
        # Adding to an existing position: recalculate weighted average cost
        pos = positions[ticker]
        prior_cost = pos["shares"] * pos["avg_cost_gbp"]
        total_cost = prior_cost + amount_gbp
        prior_fx = pos.get("fx_at_entry")
        if buy_fx and prior_fx and total_cost > 0:
            pos["fx_at_entry"] = (
                (prior_cost * prior_fx + amount_gbp * buy_fx) / total_cost)
            if pos.get("fx_basis") != "estimated":
                pos["fx_basis"] = buy_basis
        elif buy_fx and not prior_fx:
            pos["fx_at_entry"] = buy_fx
            pos["fx_basis"] = "estimated"   # earlier legs went on at unknown rates
        pos["shares"] += shares
        pos["avg_cost_gbp"] = total_cost / pos["shares"]
        # Append the new thesis alongside the original so history is preserved
        if thesis:
            existing = pos.get("thesis", "")
            pos["thesis"] = f"{existing} | [{run_date}] {thesis}".strip(" |")
        if theme:
            pos["theme"] = theme
        if pre_commit_trims:
            pos["pre_commit_trims"] = pre_commit_trims
    else:
        positions[ticker] = {
            "shares":       shares,
            "avg_cost_gbp": price,
            "first_bought": run_date,
            "thesis":       thesis,
        }
        if buy_fx:
            positions[ticker]["fx_at_entry"] = buy_fx
            positions[ticker]["fx_basis"] = buy_basis
        if theme:
            positions[ticker]["theme"] = theme
        if pre_commit_trims:
            positions[ticker]["pre_commit_trims"] = pre_commit_trims

    ledger["cash_gbp"] -= amount_gbp
    trade = {
        "date":       run_date,
        "action":     "BUY",
        "ticker":     ticker,
        "shares":     round(shares, 6),
        "price_gbp":  round(price, 4),
        "amount_gbp": round(amount_gbp, 2),
        "thesis":     thesis,
    }
    if theme:
        trade["theme"] = theme
    if pre_commit_trims:
        trade["pre_commit_trims"] = pre_commit_trims
    ledger["trades"].append(trade)
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

    # A TRIM that leaves only dust is a full exit in all but name — flag it so
    # the flip-flop guard treats it the same as a SELL.
    closes_position = pos["shares"] < 1e-4

    exit_thesis = rec.get("thesis_oneline", "")
    pos_thesis = pos.get("thesis", "")
    if not pos_thesis or pos_thesis == "(synced from T212)":
        pos_thesis = _find_entry_thesis(ledger, ticker)
    trade = {
        "date":         run_date,
        "action":       action,
        "ticker":       ticker,
        "shares":       round(shares_to_sell, 6),
        "price_gbp":    round(price, 4),
        "amount_gbp":   round(proceeds, 2),
        "exit_thesis":  exit_thesis,
        "entry_thesis": pos_thesis,
    }
    if closes_position:
        trade["closed_position"] = True
    ledger["trades"].append(trade)

    # Remove dust positions left after a partial trim
    if closes_position:
        del positions[ticker]

    return (
        f"{action} {shares_to_sell:.4f} {ticker} @ £{price:.4f} "
        f"= £{proceeds:.2f}"
    )


def _apply_set_trims(ledger: dict, rec: dict, ticker: str, run_date: str) -> str:
    """
    Backfill pre-committed trim levels on an existing position (ledger-only).

    SET_TRIMS recs move no money and place no orders: they exist so positions
    bought before the pre_commit_trims field was introduced can be brought
    under the same mechanical trim discipline as newer buys. Logged as a trade
    so the change is auditable and shows up in the prompt's recent-trade
    history.
    """
    trims = (rec.get("pre_commit_trims") or "").strip()
    pos = ledger.get("positions", {}).get(ticker)
    if pos is None:
        return f"SKIP SET_TRIMS {ticker}: no such position in shadow ledger"
    if not trims:
        return f"SKIP SET_TRIMS {ticker}: no pre_commit_trims text provided"
    pos["pre_commit_trims"] = trims
    ledger.setdefault("trades", []).append({
        "date": run_date,
        "action": "SET_TRIMS",
        "ticker": ticker,
        "pre_commit_trims": trims,
    })
    return f"SET_TRIMS {ticker}: {trims}"


def _apply_set_driver(ledger: dict, rec: dict, ticker: str, run_date: str) -> str:
    """
    Record the forward driver that is carrying a played-out position (ledger-only).

    When a position's ORIGINAL thesis has substantially played out, the strategy
    allows continuing to hold only if a NEW, independent, forward-looking driver
    is named — one that would be underwritten as a fresh BUY at today's price.
    Before this action existed that claim was made in prose and then forgotten:
    the next run saw a plain HOLD, the driver was never re-tested, and a
    realized winner could coast indefinitely on an assertion made weeks earlier
    (DELL, July 2026). Persisting it flips the default — build_thesis_review()
    replays the driver and its age into every subsequent prompt, so it must be
    defended with fresh evidence, replaced, or the position trimmed.

    Once set, thesis_played_out is never cleared: a thesis that has been
    realized does not become un-realized. Selling the position removes it.
    Moves no money and places no order.
    """
    driver = (rec.get("forward_driver") or rec.get("thesis_oneline") or "").strip()
    pos = ledger.get("positions", {}).get(ticker)
    if pos is None:
        return f"SKIP SET_DRIVER {ticker}: no such position in shadow ledger"
    if not driver:
        return f"SKIP SET_DRIVER {ticker}: no forward_driver text provided"

    previous = (pos.get("forward_driver") or "").strip()
    replacing = bool(previous and previous != driver)

    # Why the old driver is going matters more than what replaces it. A driver
    # that FAILED is a thesis break on the claim the position is being held by,
    # and a break has always meant sell. A driver merely SUPERSEDED by a better
    # framing is not. Nothing distinguished the two before, so a falsified
    # driver could be swapped for a fresh one indefinitely at no cost — DELL's
    # driver #1 was not tested and found valid on 2026-08-10, it was simply
    # replaced when a better-sounding fact turned up.
    status = (rec.get("previous_driver_status") or "").strip().lower()
    if replacing and status not in ("failed", "superseded"):
        status = "unstated"      # surfaced as a guard alert, never silent
    elif not replacing:
        status = ""

    pos["thesis_played_out"]  = True
    pos["forward_driver"]     = driver
    pos["forward_driver_set"] = run_date
    # History includes the current driver as its last element, so repeatedly
    # swapping in a fresh justification each week is itself visible.
    entry = {"date": run_date, "driver": driver}
    if status:
        entry["previous_driver_status"] = status
    pos.setdefault("forward_driver_history", []).append(entry)

    if status in ("failed", "unstated"):
        # Drives the mechanical bank: a failed driver owes a bank now, not in
        # twelve weeks. "unstated" is treated the same way — declining to say
        # whether the old driver failed cannot be cheaper than saying it did.
        pos["driver_failed_on"] = run_date
    else:
        pos.pop("driver_failed_on", None)

    trade = {
        "date":           run_date,
        "action":         "SET_DRIVER",
        "ticker":         ticker,
        "forward_driver": driver,
    }
    if replacing:
        trade["replaces_driver"] = previous
        trade["previous_driver_status"] = status
    ledger.setdefault("trades", []).append(trade)

    if replacing:
        return (f"SET_DRIVER {ticker} [previous driver {status}]: {driver} "
                f"(replaces: {previous})")
    return f"SET_DRIVER {ticker}: {driver}"


def _apply_set_thesis(ledger: dict, rec: dict, ticker: str, run_date: str) -> str:
    """
    Re-underwrite a position's entry thesis in place (ledger-only).

    This is the action for a thesis that was never a prediction — one that
    entry_thesis_provenance() reports as backfilled, synced or missing — and
    for nothing else. It replaces `thesis` with a case written today, prefixed
    "[Re-underwritten <date>]" so every later replay (the accountability
    review, the entry_thesis copied onto exits) says which day the prediction
    was actually made, and keeps the text it replaced on the trade record.

    It does NOT touch thesis_played_out, forward_driver or anything else in
    the played-out machinery. That is the whole reason it exists: on
    2026-09-14 the prompt told Claude to re-underwrite AMZN and GOOGL "as a
    SET_DRIVER", and SET_DRIVER means "the original thesis has played out" —
    so both were marked played out and the bank injection trimmed a third of
    each, AMZN at -3.6% and GOOGL at +0.16%, "to convert paper alpha into
    realised alpha" on positions with none. A trade forced by paperwork, which
    is precisely what the provenance flag was documented never to cause.
    A re-underwrite says the position never had a scoreable thesis; a
    forward driver says the thesis it had has been realised. Different claims,
    different actions.
    """
    text = (rec.get("thesis") or rec.get("thesis_oneline") or "").strip()
    pos = ledger.get("positions", {}).get(ticker)
    if pos is None:
        return f"SKIP SET_THESIS {ticker}: no such position in shadow ledger"
    if not text:
        return f"SKIP SET_THESIS {ticker}: no thesis text provided"

    previous = (pos.get("thesis") or "").strip()
    stamped = f"[Re-underwritten {run_date}] {text}"
    pos["thesis"] = stamped
    pos["thesis_reunderwritten"] = run_date

    ledger.setdefault("trades", []).append({
        "date":            run_date,
        "action":          "SET_THESIS",
        "ticker":          ticker,
        "thesis":          stamped,
        "replaces_thesis": previous,
    })
    return f"SET_THESIS {ticker}: {text}"


def apply_recommendations(ledger: dict, recs: list, run_date: str) -> list[str]:
    """
    Apply a list of trade recommendations to the shadow portfolio ledger.

    Iterates over all recommendations, fetches the current GBP price for each ticker,
    then delegates to _apply_buy() or _apply_sell_or_trim() depending on action type.
    Returns a log of what happened to each recommendation for email reporting.

    Only BUY, SELL, TRIM, SET_TRIMS, SET_DRIVER and SET_THESIS actions are
    processed; HOLD is ignored. SET_TRIMS (pre-committed trim levels),
    SET_DRIVER (the forward driver carrying a played-out position) and
    SET_THESIS (re-underwriting a thesis that was never recorded at entry)
    are ledger-only metadata — no price fetch, no cash movement.

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
        if not ticker or action not in ("BUY", "SELL", "TRIM", "SET_TRIMS",
                                        "SET_DRIVER", "SET_THESIS"):
            continue

        # Metadata-only actions never need a price — handle them before the
        # price fetch so they can't be skipped for a pricing failure.
        if action == "SET_TRIMS":
            events.append(_apply_set_trims(ledger, rec, ticker, run_date))
            continue
        if action == "SET_DRIVER":
            events.append(_apply_set_driver(ledger, rec, ticker, run_date))
            continue
        if action == "SET_THESIS":
            events.append(_apply_set_thesis(ledger, rec, ticker, run_date))
            continue

        # For BUY: use T212 actual fill price if available (most accurate cost basis).
        # _fill_price_native/_fill_price_currency are injected by t212_executor when
        # the order response contains a fill price. Falls back to yfinance if the fill
        # price is absent or if FX conversion fails (e.g. rate temporarily unavailable).
        price = None
        if action == "BUY" and rec.get("_fill_price_native") and rec.get("_fill_price_currency"):
            price = _native_to_gbp(rec["_fill_price_native"], rec["_fill_price_currency"])
        if price is None:
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
    # Pence check first — "GBp" (pence notation) must be caught before the GBP
    # check because an upstream .upper() would make it indistinguishable.
    if currency in ("GBX", "GBp", "GBP."):
        return float(price) / 100
    if currency == "GBP":
        return float(price)
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
    logger.warning("unknown currency %s, treating as GBP", currency)
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
        "pnl_pct":           round(((price_gbp / pos["avg_cost_gbp"]) - 1) * 100, 2) if pos.get("avg_cost_gbp") else 0.0,
        "first_bought":      pos["first_bought"],
        "price_source":      price_source,
    }


def init_benchmark_start_price(ledger: dict) -> None:
    """
    Record the benchmark start price the first time the agent runs.

    Idempotent — does nothing if already set. Called once per run before
    valuation() so that valuation() itself is a pure read-only function.

    Args:
        ledger: Shadow portfolio ledger dict. Mutated in-place on first call only.
    """
    if ledger.get("benchmark_start_price_gbp") is None:
        price = fetch_price_gbp(ledger["benchmark_ticker"])
        if price:
            ledger["benchmark_start_price_gbp"] = price


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
    start_bm = ledger.get("benchmark_start_price_gbp")

    if start_bm and benchmark_price_now:
        benchmark_value = start * (benchmark_price_now / start_bm)
        benchmark_pct   = ((benchmark_price_now / start_bm) - 1) * 100
    else:
        benchmark_value = None
        benchmark_pct   = None

    # Flag valuations where any position couldn't be priced — its value counts
    # as £0, so the total understates reality. Downstream consumers (snapshots,
    # the deep review) must not treat such a run as a real drawdown.
    pricing_incomplete = any(
        pv.get("current_value_gbp") is None for pv in position_values.values()
    )

    return {
        "cash_gbp":              round(ledger["cash_gbp"], 2),
        "positions_value_gbp":   round(total_positions_gbp, 2),
        "total_value_gbp":       round(total_value, 2),
        "starting_capital_gbp":  start,
        "total_return_gbp":      round(total_value - start, 2),
        "total_return_pct":      round(((total_value / start) - 1) * 100, 2) if start else 0,
        "benchmark_ticker":      ledger["benchmark_ticker"],
        "benchmark_value_gbp":   round(benchmark_value, 2) if benchmark_value is not None else None,
        "benchmark_return_pct":  round(benchmark_pct, 2) if benchmark_pct is not None else None,
        "vs_benchmark_pct": (
            round(((total_value / start) - 1) * 100 - benchmark_pct, 2)
            if benchmark_pct is not None else None
        ),
        "pricing_incomplete":    pricing_incomplete,
        "positions": position_values,
    }


def compute_realized_pnl(ledger: dict) -> dict:
    """
    Compute realised P&L per ticker by replaying the trade log chronologically.

    BUYs accumulate shares and cost; SELLs/TRIMs realise (proceeds - average
    cost of the shares sold). This gives the deep review hard numbers instead
    of asking the model to derive them from the raw trade log.

    The replay must honour what sync did to the ledger, or it silently prices
    sells against lots that never existed. Three sync actions are replayed:

      SYNC_REMOVE  a position T212 never held (rejected order) — its BUYs are
                   phantom, so drop the holding. Without this, the four April
                   2026 phantom META lots (£2,900 of orders T212 rejected)
                   stayed in the replay and blended into the cost basis of the
                   real 13 May position, reporting its loss as -£93.79 when the
                   shares actually bought and sold lost -£48.99.
      SYNC_ADD     a position T212 holds that shadow was missing — seed the
                   holding at T212's own cost. Reliable when it came from
                   walletImpact.totalCost, an estimate otherwise.
      SYNC_RESET   the ledger was rebuilt wholesale from T212 (the April 2026
                   bootstrap runs) — every lot before it was replaced, so the
                   replay starts over and pre-reset basis is not recoverable.
      SYNC_COST    the open lots were re-based to T212's actual fill cost —
                   the shares stay, their total cost becomes shares × that
                   figure. It comes from T212's wallet, so it makes a basis
                   MORE exact, never estimated.

    Shares sold with no recorded basis fall back to the position's current
    avg_cost_gbp and the ticker is reported under "tickers_with_estimated_basis".
    Skipping them instead understates realised P&L badly — DELL's five trims
    banked ~£650 against shares mostly seeded by a rebuild, and reporting that
    as +£90 misleads exactly the kill-criteria decomposition the deep review
    exists to make. Tickers with no basis available at all stay under
    "tickers_with_incomplete_basis".

    Args:
        ledger: Shadow portfolio ledger dict.

    Returns:
        dict: {
            "total_gbp": float,                      # sum of realised P&L
            "by_ticker": {ticker: realised_gbp},     # sorted best-first
            "peak_cost_gbp": {ticker: gbp},          # most basis ever open at
                                                     # once, the capital the
                                                     # name actually tied up
            "tickers_with_incomplete_basis": [str],  # no cost basis at all
            "tickers_with_estimated_basis": [str],   # basis inferred, not recorded
            "unpriced_proceeds_gbp": {ticker: gbp},  # sold, but P&L unknowable
        }
    """
    holdings: dict[str, list[float]] = {}   # ticker -> [shares_held, total_cost_gbp]
    realized: dict[str, float] = {}
    peak_cost: dict[str, float] = {}
    incomplete: set[str] = set()
    estimated: set[str] = set()
    unpriced: dict[str, float] = {}   # proceeds we cannot price at all
    positions = ledger.get("positions", {}) or {}

    def _bump_peak(tk: str) -> None:
        """Ratchet the most cost basis this ticker has ever had open at once."""
        held = holdings.get(tk)
        if held:
            peak_cost[tk] = max(peak_cost.get(tk, 0.0), held[1])

    def _fallback_basis(tk: str):
        """Per-share cost for shares with no recorded BUY, or None."""
        cost = (positions.get(tk) or {}).get("avg_cost_gbp")
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            return None
        return cost if cost > 0 else None

    for t in ledger.get("trades", []):
        action = t.get("action")
        ticker = t.get("ticker")

        if action == "SYNC_RESET":
            # Wholesale rebuild: anything still open was replaced by T212 state
            # whose cost basis never reached the trade log. Clear the replay but
            # don't flag the tickers here — a name re-bought after the reset has
            # a perfectly good basis, and flagging it forever would wrongly mark
            # META (cleanly bought 13 May, sold 24 Aug) as unpriceable. Sells
            # that really have no basis are caught where they happen, below.
            holdings.clear()
            continue

        if not ticker or ticker == "-":
            continue
        shares = float(t.get("shares") or 0)

        if action == "SYNC_REMOVE":
            holdings.pop(ticker, None)

        elif action == "SYNC_COST":
            cost = float(t.get("avg_cost_gbp") or 0)
            h = holdings.get(ticker)
            if h and h[0] > 0 and cost > 0:
                h[1] = h[0] * cost
                _bump_peak(ticker)

        elif action == "SYNC_ADD":
            cost = float(t.get("avg_cost_gbp") or 0)
            if shares > 0 and cost > 0:
                h = holdings.setdefault(ticker, [0.0, 0.0])
                h[0] += shares
                h[1] += shares * cost
                _bump_peak(ticker)
                if t.get("basis_source") != "t212_wallet":
                    estimated.add(ticker)
            else:
                incomplete.add(ticker)

        elif action == "BUY":
            amount = float(t.get("amount_gbp") or 0)
            h = holdings.setdefault(ticker, [0.0, 0.0])
            h[0] += shares
            h[1] += amount
            _bump_peak(ticker)

        elif action in ("SELL", "TRIM"):
            proceeds = float(t.get("amount_gbp") or 0)
            if shares <= 0:
                incomplete.add(ticker)
                continue
            h = holdings.setdefault(ticker, [0.0, 0.0])

            matched = min(shares, h[0]) if h[0] > 0 else 0.0
            if matched > 0:
                avg_cost = h[1] / h[0]
                realized[ticker] = (
                    realized.get(ticker, 0.0)
                    + proceeds * (matched / shares)
                    - matched * avg_cost
                )
                h[0] -= matched
                h[1] -= matched * avg_cost

            unmatched = shares - matched
            if unmatched > 1e-9:
                basis = _fallback_basis(ticker)
                if basis is None:
                    # Nothing to price these against. Report the proceeds so the
                    # deep review sees unaccounted realised activity instead of
                    # a silent zero.
                    incomplete.add(ticker)
                    unpriced[ticker] = (
                        unpriced.get(ticker, 0.0) + proceeds * (unmatched / shares)
                    )
                else:
                    realized[ticker] = (
                        realized.get(ticker, 0.0)
                        + proceeds * (unmatched / shares)
                        - unmatched * basis
                    )
                    estimated.add(ticker)

    return {
        "total_gbp": round(sum(realized.values()), 2),
        "by_ticker": {
            k: round(v, 2)
            for k, v in sorted(realized.items(), key=lambda x: -x[1])
        },
        "peak_cost_gbp": {k: round(v, 2) for k, v in sorted(peak_cost.items())},
        "tickers_with_incomplete_basis": sorted(incomplete),
        "tickers_with_estimated_basis": sorted(estimated - incomplete),
        "unpriced_proceeds_gbp": {
            k: round(v, 2) for k, v in sorted(unpriced.items())
        },
    }


# Kill criterion #5 ("remove the top contributor and the rest still
# underperforms VUSA") TRIGGERED at the 10 Sep 2026 Opus deep review, which
# also pointed out that every other recommendation it made was housekeeping:
# the finding is that the picks other than the top one generate no alpha, and
# no rule tightening creates a second good idea. What it asked for instead was
# a number, checked every run rather than argued about monthly, plus a date by
# which the number has to come good. These two constants are that test.
#
# On 10 Sep 2026 the book was +26.56% against VUSA's +7.12%, of which DELL was
# ~£1,117 of ~£1,328 - roughly 85% of every pound of profit.
EX_TOP_TEST_DATE = "2026-11-30"

# ...OR one non-top position must have earned this much in its own right, so
# the test can also be passed by finding a second good idea rather than only by
# the remainder out-running the index in aggregate.
EX_TOP_SECOND_IDEA_GBP = 150.0


def ex_top_contributor_performance(ledger: dict, valuation_result: dict) -> dict:
    """
    Strip the single best contributor out of the book and score what is left.

    The headline return is the number that decides whether this strategy is
    worth running, and a book carried by one name reports the same headline as
    a book that picks well nine times. Kill criterion #5 exists for exactly
    that, but it was only ever evaluated inside the monthly deep review - a
    prose argument, made against whichever figures the model derived for
    itself, four weeks apart. This computes it in code so it can be shown every
    run.

    Contribution is realised + unrealised per ticker, so a name whose gains
    were banked counts them: DELL's realised ~£804 is most of its case and is
    invisible to an unrealised-only ranking (which is what the deep review
    prompt used to pick its "top contributor").

    The remainder is scored against the capital it actually had to work with -
    starting capital less the most cost basis the top name ever had open at
    once (peak_cost_gbp from the replay). Charging the whole starting capital to
    the remainder would understate it; ignoring the top name's capital entirely
    would overstate it. Neither is exact, because capital recycles through the
    book, and the figure is labelled approximate wherever it is shown.

    Returns {} when nothing can be scored yet. Otherwise the dict documented in
    the keys below; passing is beats_benchmark OR has_second_idea.
    """
    positions_val = valuation_result.get("positions") or {}
    realized = compute_realized_pnl(ledger)

    contributions: dict[str, float] = dict(realized.get("by_ticker") or {})
    for ticker, pv in positions_val.items():
        pnl = pv.get("pnl_gbp")
        if pnl is None:
            continue
        contributions[ticker] = contributions.get(ticker, 0.0) + pnl
    if not contributions:
        return {}

    top_ticker = max(contributions, key=lambda k: contributions[k])
    top_pnl = contributions[top_ticker]

    start = (valuation_result.get("starting_capital_gbp")
             or ledger.get("starting_capital_gbp") or 0)
    total_pnl = valuation_result.get("total_return_gbp")
    if not start or total_pnl is None:
        return {}
    ex_top_pnl = total_pnl - top_pnl

    peak_cost = (realized.get("peak_cost_gbp") or {}).get(top_ticker, 0.0)
    capital_ex_top = start - peak_cost
    ex_top_pct = (ex_top_pnl / capital_ex_top * 100) if capital_ex_top > 0 else None

    bench = valuation_result.get("benchmark_return_pct")
    vs_bench = (round(ex_top_pct - bench, 2)
                if ex_top_pct is not None and bench is not None else None)

    others = {k: v for k, v in contributions.items() if k != top_ticker}
    second_ticker = max(others, key=lambda k: others[k]) if others else None
    second_pnl = others[second_ticker] if second_ticker else 0.0

    beats = bool(vs_bench is not None and vs_bench > 0)
    has_second = bool(second_pnl >= EX_TOP_SECOND_IDEA_GBP)

    return {
        "top_ticker":                top_ticker,
        "top_pnl_gbp":               round(top_pnl, 2),
        "total_pnl_gbp":             round(total_pnl, 2),
        "ex_top_pnl_gbp":            round(ex_top_pnl, 2),
        "capital_ex_top_gbp":        round(capital_ex_top, 2),
        "ex_top_return_pct":         round(ex_top_pct, 2) if ex_top_pct is not None else None,
        "benchmark_return_pct":      bench,
        "ex_top_vs_benchmark_pts":   vs_bench,
        "second_ticker":             second_ticker,
        "second_pnl_gbp":            round(second_pnl, 2),
        "second_idea_threshold_gbp": EX_TOP_SECOND_IDEA_GBP,
        "test_date":                 EX_TOP_TEST_DATE,
        "beats_benchmark":           beats,
        "has_second_idea":           has_second,
        "passing":                   beats or has_second,
        "contributions": {
            k: round(v, 2)
            for k, v in sorted(contributions.items(), key=lambda x: -x[1])
        },
        "basis_estimated": realized.get("tickers_with_estimated_basis") or [],
    }


def _ex_top_lines(ex: dict) -> list[str]:
    """Shared body of the single-name dependency block (prompt and email)."""
    pct = ("n/a" if ex["ex_top_return_pct"] is None
           else f"{ex['ex_top_return_pct']:+.2f}%")
    bench = ("n/a" if ex["benchmark_return_pct"] is None
             else f"{ex['benchmark_return_pct']:+.2f}%")
    gap = ("n/a" if ex["ex_top_vs_benchmark_pts"] is None
           else f"{ex['ex_top_vs_benchmark_pts']:+.2f} pts")
    verdict = "PASSING" if ex["passing"] else "FAILING"
    lines = [
        f"  Top contributor:   {ex['top_ticker']} "
        f"£{ex['top_pnl_gbp']:+.2f} (realised + unrealised)",
        f"  Whole book:        £{ex['total_pnl_gbp']:+.2f}",
        f"  Everything else:   £{ex['ex_top_pnl_gbp']:+.2f} on "
        f"~£{ex['capital_ex_top_gbp']:.2f} of capital = {pct} (approximate)",
        f"  Benchmark:         {bench}   ex-{ex['top_ticker']} vs benchmark: {gap}",
        f"  Best of the rest:  {ex['second_ticker'] or 'n/a'} "
        f"£{ex['second_pnl_gbp']:+.2f} "
        f"(second-idea bar: £{ex['second_idea_threshold_gbp']:.0f})",
        f"  {ex['test_date']} TEST: {verdict} - the remainder must be beating "
        f"the benchmark, OR one non-{ex['top_ticker']} position must have "
        f"earned £{ex['second_idea_threshold_gbp']:.0f} in its own right.",
    ]
    if ex["basis_estimated"]:
        lines.append(
            f"  (cost basis estimated for {', '.join(ex['basis_estimated'])} - "
            f"right order of magnitude, not exact)"
        )
    return lines


def build_ex_top_review(ledger: dict, valuation_result: dict) -> str:
    """
    Single-name dependency block for the weekly prompt.

    Kill criterion #5 triggered on 10 Sep 2026, and the deep review's own
    verdict was that the ONLY one of its seven recommendations that touched the
    trigger was making this number visible every run instead of once a month,
    so the dependency "can't hide". Carrying it in the weekly prompt means each
    week's picking is argued in front of the evidence about the last five
    months of picking.
    """
    ex = ex_top_contributor_performance(ledger, valuation_result)
    if not ex:
        return ""
    lines = ["=== Single-name dependency (kill criterion #5) ==="]
    lines.extend(_ex_top_lines(ex))
    lines.append(
        "  READ THIS BEFORE CLAIMING THE STRATEGY IS WORKING: the headline\n"
        "  return is not evidence of stock-picking while this test is failing\n"
        f"  - it is evidence about {ex['top_ticker']}. A HOLD on a position\n"
        "  that has earned nothing is a decision being made again this week,\n"
        "  not a decision already made. For each such holding, say what would\n"
        "  have to happen for it to become the second good idea; if you cannot\n"
        "  name it, recycle the capital into something that can."
    )
    return "\n".join(lines) + "\n"


def format_ex_top_for_email(ledger: dict, valuation_result: dict) -> str:
    """Format the single-name dependency check for the weekly email."""
    ex = ex_top_contributor_performance(ledger, valuation_result)
    if not ex:
        return ""
    return "\n".join(["Single-name dependency (kill criterion #5):"]
                     + _ex_top_lines(ex))


def reconcile_trade_log(ledger: dict) -> dict:
    """
    Check the trade log still explains the positions the ledger holds.

    The log and the position state can drift apart silently — sync used to
    rewrite positions without recording per-ticker entries, so by August 2026
    the log implied -3.79 DELL shares against 2.04 actually held, and a cash
    balance of -£1,172.60 against £559.36. Nothing noticed, and the realised
    P&L fed to the monthly deep review was computed from it regardless.

    Args:
        ledger: Shadow portfolio ledger dict.

    Returns:
        dict: {
            "drifts": {ticker: {"log": float, "ledger": float, "diff": float}},
            "cash_log_gbp": float,    # cash the log implies
            "cash_ledger_gbp": float,
            "cash_diff_gbp": float,
            "clean": bool,
        }
    """
    shares: dict[str, float] = {}
    cash = float(ledger.get("starting_capital_gbp") or 0)

    for t in ledger.get("trades", []):
        action = t.get("action")
        ticker = t.get("ticker")

        if action == "SYNC_BASELINE":
            # Known-good starting point stamped by migrate_trade_log.py. The
            # April 2026 bootstrap rebuilt the ledger from T212 without ever
            # recording what it held, so shares before the baseline are
            # unrecoverable and reconciling from £5,000 can only ever fail.
            # Anything that drifts after it is real and worth an alert.
            shares.clear()
            shares.update({
                k: float(v) for k, v in (t.get("positions") or {}).items()
            })
            cash = float(t.get("cash_gbp") or 0)
            continue
        if action == "SYNC_RESET":
            shares.clear()
            continue
        if not ticker or ticker == "-":
            continue

        qty    = float(t.get("shares") or 0)
        amount = float(t.get("amount_gbp") or 0)

        if action == "BUY":
            shares[ticker] = shares.get(ticker, 0.0) + qty
            cash -= amount
        elif action in ("SELL", "TRIM"):
            shares[ticker] = shares.get(ticker, 0.0) - qty
            cash += amount
        elif action == "SYNC_ADD":
            shares[ticker] = shares.get(ticker, 0.0) + qty
        elif action == "SYNC_REMOVE":
            shares.pop(ticker, None)

    ledger_shares = {
        tk: float(p.get("shares") or 0)
        for tk, p in (ledger.get("positions") or {}).items()
    }

    drifts = {}
    for tk in sorted(set(shares) | set(ledger_shares)):
        log_qty = round(shares.get(tk, 0.0), 6)
        led_qty = round(ledger_shares.get(tk, 0.0), 6)
        if abs(log_qty - led_qty) > 1e-4:
            drifts[tk] = {
                "log":    log_qty,
                "ledger": led_qty,
                "diff":   round(log_qty - led_qty, 6),
            }

    cash_ledger = float(ledger.get("cash_gbp") or 0)
    return {
        "drifts":           drifts,
        "cash_log_gbp":     round(cash, 2),
        "cash_ledger_gbp":  round(cash_ledger, 2),
        "cash_diff_gbp":    round(cash - cash_ledger, 2),
        "clean":            not drifts,
    }


# =============================================================================
# Snapshots and reporting
# =============================================================================

def snapshot(ledger: dict, val: dict, run_date: str,
             t212_total_gbp: float = None) -> None:
    """
    Append a weekly valuation snapshot to the ledger for long-term tracking.

    Snapshots are the raw material for the monthly deep review — Opus uses them
    to see how the portfolio has evolved over time and whether it's beating the
    benchmark consistently or just got lucky on one trade.

    Args:
        ledger:          Shadow portfolio ledger dict. Mutated in-place.
        val:             Valuation dict from valuation().
        run_date:        ISO date string (YYYY-MM-DD) for the snapshot label.
        t212_total_gbp:  T212 account total value in GBP, for shadow vs T212 tracking.
    """
    if ledger["weekly_snapshots"] and ledger["weekly_snapshots"][-1]["date"] == run_date:
        return
    entry = {
        "date":                 run_date,
        "total_value_gbp":      val["total_value_gbp"],
        "total_return_pct":     val["total_return_pct"],
        "benchmark_return_pct": val["benchmark_return_pct"],
        "vs_benchmark_pct":     val["vs_benchmark_pct"],
        "position_count":       len(val["positions"]),
        "positions":            sorted(val["positions"].keys()),
    }
    if t212_total_gbp is not None:
        entry["t212_total_gbp"] = round(t212_total_gbp, 2)
    if val.get("pricing_incomplete"):
        # One or more positions had no price this run — the total is understated.
        # Recorded so the deep review doesn't read this as a genuine drawdown.
        entry["pricing_incomplete"] = True
    ledger["weekly_snapshots"].append(entry)


def _weeks_since(iso_date: str | None) -> Optional[int]:
    """Whole weeks between an ISO date string and today, or None if unparseable."""
    if not iso_date:
        return None
    try:
        then = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return max((date.today() - then).days, 0) // 7


# A played-out position must bank a trim at declaration and then at least
# once every this-many weeks — a forward driver carries the un-banked
# remainder of a realized winner, never the whole position indefinitely.
PLAYED_OUT_REBANK_WEEKS = 12

# How much of a played-out position's PEAK gain may be handed back before the
# mechanical bank fires early. Kill criterion #2 shuts the agent down when the
# top contributor gives back >50% of its gains, so acting at 50% would only
# ever coincide with the shutdown it exists to prevent — this fires while there
# is still something left to bank.
PLAYED_OUT_GIVEBACK_PCT = 25.0

# Peak gain a played-out position must have reached before the giveback test
# applies at all. The test is measured on the GAIN, so the price move it
# implies shrinks with the size of the winner: at a +200% peak it takes a 16.7%
# fall to hand back a quarter, at +100% it takes 13.3%, but at +20% it takes
# only 4.2% — noise, not a giveback. Below this floor the twelve-week clock is
# the only mechanism, because there isn't enough profit at stake to protect.
PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT = 50.0

# Declaring a thesis played out costs a third of the position: the forward
# driver may carry the remainder, never the whole win. If Claude doesn't
# include the trim itself, this is the one injected mechanically.
PLAYED_OUT_BANK_TRIM_PCT = 33.0

# Replacing a live forward driver costs something even when the old one is
# declared "superseded" rather than "failed" (Sep 2026).
#
# The failed/superseded split was meant to separate a driver contradicted by
# evidence from one merely restated better. It priced them at 33% and NOTHING,
# which made the label a free-text field with a third of a position attached
# and no adjudication — so the label drifts towards the cheap word whatever the
# evidence says. It did exactly that on its first live run: on 2026-09-01 DELL's
# driver #2 ("ISG margin-expansion story") was replaced after Claude itself
# quoted ISG operating margin FALLING 110bp, and the swap was filed as
# "superseded", banking nothing on a +110% position.
#
# The fix is not a better definition of "failed" — no prompt wording survives a
# free option. Rewriting the reason you hold a realized winner is evidence about
# the hold whatever the reason, so every replacement banks. The label now sets
# the SIZE, not whether money moves: an honest "failed" still costs more, but
# "superseded" can no longer cost nothing.
PLAYED_OUT_SUPERSEDE_TRIM_PCT = 15.0

# Churn escalation: the count of drivers named for one position is itself the
# signal. Naming a third distinct driver banks the full third whatever the
# label; a fourth exits the position. DELL reached driver #3 in five weeks,
# each one "confirmed with fresh evidence" — for a secular theme there always
# is some, which is why the count, not the content, has to be what bites.
DRIVER_CHURN_BANK_COUNT = 3
DRIVER_CHURN_EXIT_COUNT = 4


def played_out_declared_date(pos: dict) -> Optional[str]:
    """
    ISO date a position's thesis was declared played out, or None.

    The declaration is the FIRST forward-driver entry — forward_driver_set
    moves every time the driver is replaced, so it can't anchor "since
    declaration" checks.
    """
    if not pos.get("thesis_played_out"):
        return None
    history = pos.get("forward_driver_history") or []
    if history and history[0].get("date"):
        return history[0]["date"]
    return pos.get("forward_driver_set")


def last_bank_since(ledger: dict, ticker: str, since: str) -> Optional[str]:
    """
    ISO date of the most recent TRIM or SELL of ticker on/after `since`,
    or None if no gain has been banked in that window.
    """
    latest = None
    for t in ledger.get("trades", []):
        if (t.get("ticker") == ticker
                and t.get("action") in ("SELL", "TRIM")
                and (t.get("date") or "") >= since):
            if latest is None or t["date"] > latest:
                latest = t["date"]
    return latest


def played_out_giveback_pct(pos: dict, current_gain_pct: Optional[float]) -> Optional[float]:
    """
    How much of a played-out position's PEAK gain has been handed back, as a
    percentage of that peak. None when there is no peak on record or no price.

    Peak gain 100%, now 40% -> 60.0 (sixty percent of the gain surrendered).
    """
    peak = pos.get("played_out_peak_gain_pct")
    if peak is None or current_gain_pct is None or peak <= 0:
        return None
    return max(0.0, (peak - current_gain_pct) / peak * 100.0)


def played_out_giveback_due(pos: dict, current_gain_pct: Optional[float]) -> bool:
    """
    True when a played-out position has handed back enough of a big enough
    peak gain to owe an early bank.

    Both conditions matter: PLAYED_OUT_GIVEBACK_PCT of the gain surrendered,
    on a peak of at least PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT. The floor keeps
    the rule off small winners, where a quarter of the gain is a few percent
    of price and would fire on ordinary movement.
    """
    peak = pos.get("played_out_peak_gain_pct")
    if peak is None or peak < PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT:
        return False
    giveback = played_out_giveback_pct(pos, current_gain_pct)
    return giveback is not None and giveback >= PLAYED_OUT_GIVEBACK_PCT


def played_out_bank_due(ledger: dict, ticker: str, pos: dict,
                        current_gain_pct: Optional[float] = None) -> bool:
    """
    True when a played-out position owes a mechanical bank.

    Three ways to owe one:
      1. No TRIM/SELL since the played-out declaration.
      2. The most recent one is at least PLAYED_OUT_REBANK_WEEKS old.
      3. The forward driver FAILED (or its replacement declined to say), and
         nothing has been banked since — a broken driver owes a bank now, not
         in twelve weeks.
      4. The position has handed back PLAYED_OUT_GIVEBACK_PCT of its peak gain
         since being declared played out, with nothing banked since that peak
         — and that peak was at least PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT, so the
         rule stays off small winners where it would fire on noise.

    (4) closes the hole that mattered most: pre-committed trim levels are gains
    from ENTRY, so on a large winner they sit far above the current price and
    only ever trigger on a rally. A played-out winner sliding back down passed
    no level, could not break a thesis that had already played out, and was
    caught by nothing except the twelve-week drip — which is the scenario kill
    criterion #2 describes ("top contributor gives back >50% of gains").
    """
    declared = played_out_declared_date(pos)
    if not declared:
        return False

    last_bank = last_bank_since(ledger, ticker, declared)
    if last_bank is None:
        return True

    failed_on = pos.get("driver_failed_on")
    if failed_on and last_bank < failed_on:
        return True

    if played_out_giveback_due(pos, current_gain_pct):
        peak_date = pos.get("played_out_peak_date") or ""
        # Only a bank taken strictly AFTER the peak clears the obligation. A
        # trim on the peak date happened at the top, before any of the gain was
        # handed back — and when the peak is seeded from that very trim the two
        # dates are identical, which is exactly DELL's case.
        if last_bank <= peak_date:
            return True

    weeks = _weeks_since(last_bank)
    return weeks is not None and weeks >= PLAYED_OUT_REBANK_WEEKS


def _peak_from_trades(ledger: dict, ticker: str, pos: dict):
    """
    Best gain evidenced by a SELL/TRIM price since the played-out declaration,
    as (gain_pct, date), or (None, None). Used only to seed a missing peak.
    """
    declared = played_out_declared_date(pos)
    cost = pos.get("avg_cost_gbp")
    if not declared or not cost:
        return None, None

    best, best_date = None, None
    for t in ledger.get("trades", []):
        if (t.get("ticker") != ticker
                or t.get("action") not in ("SELL", "TRIM")
                or (t.get("date") or "") < declared):
            continue
        price = t.get("price_gbp")
        if not price:
            continue
        gain = (float(price) / float(cost) - 1) * 100.0
        if best is None or gain > best:
            best, best_date = gain, t.get("date")
    return best, best_date


def update_played_out_peaks(ledger: dict, valuation: dict, run_date: str) -> list[str]:
    """
    Record the high-water gain of every played-out position.

    Runs before the guards each week so the giveback test in
    played_out_bank_due() has a peak to measure against. The peak only ever
    ratchets up; the date moves with it so a bank taken after a peak clears the
    obligation until a new peak is set.
    """
    events: list[str] = []
    positions_val = (valuation or {}).get("positions", {}) or {}

    for ticker, pos in (ledger.get("positions") or {}).items():
        if not pos.get("thesis_played_out"):
            continue
        gain = (positions_val.get(ticker) or {}).get("pnl_pct")
        if gain is None:
            continue
        peak = pos.get("played_out_peak_gain_pct")
        if peak is None:
            # First time this position is measured. Starting the high-water
            # mark at today's gain would forget everything it already reached:
            # DELL was trimmed at +113.6% on 2026-08-10 and sits at +100.5%,
            # so a cold start would erase a giveback that has already happened.
            # Sells since the declaration are the only priced history there is.
            seed, seed_date = _peak_from_trades(ledger, ticker, pos)
            if seed is not None and seed > gain:
                pos["played_out_peak_gain_pct"] = round(seed, 4)
                pos["played_out_peak_date"] = seed_date
                events.append(
                    f"{ticker}: high-water gain seeded at {seed:+.1f}% from the "
                    f"{seed_date} trim (now {gain:+.1f}%)"
                )
                continue
        if peak is None or gain > peak:
            pos["played_out_peak_gain_pct"] = round(gain, 4)
            pos["played_out_peak_date"] = run_date
            if peak is not None:
                events.append(
                    f"{ticker}: new high-water gain {gain:+.1f}% "
                    f"(was {peak:+.1f}%)"
                )
    return events


def _format_forward_driver(pos: dict, bank_due: bool = False) -> str:
    """
    Render the played-out / forward-driver block for one position in the thesis
    accountability section.

    A position marked thesis_played_out is being held on a claim, not on its
    entry thesis. Replaying that claim verbatim — with its age, and any earlier
    drivers it replaced — forces it to be re-argued against this week's facts
    instead of silently carrying the hold forever.
    """
    if not pos.get("thesis_played_out"):
        return ""

    driver   = pos.get("forward_driver") or "(none recorded)"
    set_date = pos.get("forward_driver_set") or "?"
    weeks    = _weeks_since(pos.get("forward_driver_set"))
    if weeks is None:
        age = ""
    elif weeks == 0:
        age = " (named this week)"
    else:
        age = f" (named {weeks} week{'s' if weeks != 1 else ''} ago)"

    block = (
        f"\n    ** ORIGINAL THESIS ALREADY PLAYED OUT — declared {set_date} **"
        f"\n    This hold rests solely on the forward driver below{age}, NOT on the"
        f"\n    entry thesis above:"
        f"\n      \"{driver}\""
    )

    history = pos.get("forward_driver_history") or []
    if len(history) > 1:
        block += (
            f"\n    This is driver #{len(history)} for this position. Previously named:"
        )
        for h in history[:-1]:
            block += f"\n      [{h.get('date', '?')}] {h.get('driver', '')}"
        block += (
            "\n      (Repeatedly swapping in a fresh justification to keep a"
            "\n       realized winner is itself evidence the hold is not earning"
            "\n       its place — weigh that.)"
        )

    peak = pos.get("played_out_peak_gain_pct")
    if peak is not None:
        block += (
            f"\n    Peak gain since declaration: {peak:+.1f}%"
            f" (set {pos.get('played_out_peak_date', '?')})."
        )
        if peak >= PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT:
            trigger_gain = peak * (1 - PLAYED_OUT_GIVEBACK_PCT / 100)
            block += (
                f" Handing back {PLAYED_OUT_GIVEBACK_PCT:.0f}% of that"
                f"\n    peak — a fall to {trigger_gain:+.1f}% from entry —"
                f" forces the mechanical bank early. Trim"
                f"\n    levels are entry-relative and only trigger on a rally,"
                f" so nothing else"
                f"\n    catches a played-out winner sliding back down."
            )
        else:
            block += (
                f" Below the {PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT:.0f}% peak"
                f"\n    floor, so the giveback bank does not apply to this"
                f" position."
            )

    block += (
        "\n    REQUIRED THIS RUN — pick one, do not default to HOLD:"
        "\n      (a) Confirm the driver is STILL live, citing evidence from the past"
        "\n          week or the most recent results. Restating it in different words"
        "\n          with no new evidence is NOT a defence."
        "\n      (b) Replace it with a different forward driver you would underwrite"
        "\n          as a fresh BUY at today's price and weight — issue a new"
        "\n          SET_DRIVER action so the replacement is on record, and set"
        "\n          \"previous_driver_status\" to \"failed\" or \"superseded\"."
        "\n          FAILED means the old driver was contradicted by evidence:"
        "\n          that is a thesis break on the claim this position is held by,"
        "\n          so SELL is the default and keeping any of it needs the"
        "\n          thesis-break checklist answered. SUPERSEDED means the old"
        "\n          driver is still true but a better statement of the same case"
        "\n          exists — it is NOT a licence to swap in a fresh justification"
        "\n          because the old one grew stale."
        f"\n          EITHER LABEL BANKS. Replacing a live driver"
        f" costs {PLAYED_OUT_SUPERSEDE_TRIM_PCT:.0f}% of the position if"
        "\n          superseded and"
        f" {PLAYED_OUT_BANK_TRIM_PCT:.0f}% if failed, injected"
        "\n          automatically unless your own recs trim or sell"
        "\n          the ticker. The label sets the SIZE, never"
        "\n          whether the bank happens, so choose it on the"
        "\n          evidence and not on the cost."
        f"\n          Driver #{DRIVER_CHURN_BANK_COUNT} for one"
        f" position banks the full {PLAYED_OUT_BANK_TRIM_PCT:.0f}% whatever"
        f"\n          the label; driver #{DRIVER_CHURN_EXIT_COUNT}"
        " exits it outright."
        "\n      (c) TRIM or SELL and state where the freed capital goes."
    )
    if bank_due:
        block += (
            "\n    MECHANICAL BANK DUE: no gain has been banked on this position"
            "\n    since declaration (or in the last 12 weeks). Options (a) and (b)"
            "\n    keep the hold but do NOT waive the bank — unless your"
            "\n    recommendations include a TRIM or SELL for this ticker, the"
            f"\n    system will automatically add a"
        f" {PLAYED_OUT_BANK_TRIM_PCT:.0f}% TRIM this run. Recommend"
            "\n    your own trim (with sizing and destination for the proceeds)"
            "\n    rather than letting the mechanical default decide."
        )
    return block


# =============================================================================
# Watchlist recording — observational only, never gates a trade
# =============================================================================

# How long a name dropped from the active watchlist keeps being priced. Long
# enough to score an idea over the strategy's weeks-to-months horizon, short
# enough that the weekly price fetch doesn't grow without bound.
WATCHLIST_TRACK_WEEKS = 26


def _watchlist_bought_date(ledger: dict, ticker: str, since: str) -> Optional[str]:
    """ISO date of the first BUY of ticker on/after `since`, or None."""
    for t in ledger.get("trades", []):
        if (t.get("ticker") == ticker and t.get("action") == "BUY"
                and (t.get("date") or "") >= (since or "")):
            return t["date"]
    return None


def record_watchlist(ledger: dict, entries: list, run_date: str,
                     price_fn=None,
                     benchmark_return_pct: Optional[float] = None) -> list[str]:
    """
    Record this run's watchlist names and re-price every name already tracked.

    Purely observational — nothing here blocks, gates or creates a trade. It
    exists to answer a question the agent has no evidence for: are its
    non-held ideas any good? Watchlist names used to live only in the prose
    report and evaporate the next run (ZTS/STZ/ACN one week, NFLX/AZN.L/WMT
    the next), so there was never a record to score them against.

    Names dropped from the active list keep being priced for
    WATCHLIST_TRACK_WEEKS — an idea that was abandoned and then ran is
    precisely the data this is here to capture.

    benchmark_return_pct is the portfolio-inception-relative benchmark return
    at this run, stored per observation so a name's performance can later be
    measured over its OWN window rather than since inception.

    Returns human-readable event strings for the run log.
    """
    price_fn = price_fn or fetch_price_gbp
    watchlist = ledger.setdefault("watchlist", {})
    events: list[str] = []

    seen_now: set[str] = set()
    for entry in (entries or []):
        if not isinstance(entry, dict):
            continue
        ticker = (entry.get("yfinance_ticker") or entry.get("ticker") or "").strip()
        if not ticker:
            continue
        seen_now.add(ticker)
        rec = watchlist.get(ticker)
        if rec is None:
            rec = watchlist[ticker] = {
                "first_seen":      run_date,
                "yfinance_ticker": ticker,
                "observations":    [],
            }
            events.append(f"WATCHLIST +{ticker}: now tracked")
        rec["last_seen"] = run_date
        rec["active"] = True
        rec.pop("tracking_ended", None)
        thesis = (entry.get("thesis_oneline") or entry.get("thesis") or "").strip()
        if thesis:
            rec["thesis"] = thesis
        theme = (entry.get("theme") or "").strip()
        if theme:
            rec["theme"] = theme

    # Positions exited this run get tracked whether or not Claude listed them.
    # A name the agent sold and then watched run is the most expensive blind
    # spot it has, and relying on the report to remember is not enough: on
    # 2026-08-24 it sold META saying "the watchlist is the right place for
    # that" and then left it off the list, so the exit was scored nowhere.
    # Recorded inactive — a sold position is evidence, not a live idea.
    for t in ledger.get("trades", []):
        if t.get("date") != run_date or not t.get("closed_position"):
            continue
        ticker = (t.get("ticker") or "").strip()
        if not ticker or ticker in seen_now:
            continue
        rec = watchlist.get(ticker)
        if rec is None:
            rec = watchlist[ticker] = {
                "first_seen":      run_date,
                "yfinance_ticker": t.get("yfinance_ticker") or ticker,
                "observations":    [],
                "active":          False,
                "source":          "exit",
                "thesis":          (t.get("exit_thesis") or "").strip(),
            }
            events.append(
                f"WATCHLIST +{ticker}: exited this run, tracked for "
                f"{WATCHLIST_TRACK_WEEKS}w to score the exit"
            )
        rec["exited_on"] = run_date
        rec["last_seen"] = run_date
        rec.pop("tracking_ended", None)

    for ticker, rec in watchlist.items():
        if ticker not in seen_now and rec.get("active"):
            rec["active"] = False
            events.append(
                f"WATCHLIST -{ticker}: dropped from the active list, "
                f"still priced for {WATCHLIST_TRACK_WEEKS}w"
            )
        if rec.get("tracking_ended"):
            continue
        if not rec.get("active"):
            weeks_cold = _weeks_since(rec.get("last_seen"))
            if weeks_cold is not None and weeks_cold >= WATCHLIST_TRACK_WEEKS:
                rec["tracking_ended"] = run_date
                events.append(f"WATCHLIST {ticker}: tracking window closed")
                continue

        price = price_fn(rec.get("yfinance_ticker") or ticker)
        if price is None:
            continue
        observation = {
            "date":      run_date,
            "price_gbp": round(price, 4),
        }
        if benchmark_return_pct is not None:
            observation["benchmark_return_pct"] = round(benchmark_return_pct, 4)
        observations = rec.setdefault("observations", [])
        # A re-run on the same date updates in place rather than double-counting.
        if observations and observations[-1].get("date") == run_date:
            observations[-1] = observation
        else:
            observations.append(observation)

    return events


def watchlist_performance(ledger: dict) -> list[dict]:
    """
    Score every tracked watchlist name over its own observation window.

    Each name is measured from its first priced observation to its latest, and
    against the benchmark over that SAME window (not since portfolio
    inception), so a name first seen last week isn't compared against six
    months of benchmark return.

    Returns a list of dicts sorted by vs-benchmark, best first. Names with
    fewer than two priced observations are included with None returns — they
    are being tracked but can't be scored yet.
    """
    scored: list[dict] = []
    for ticker, rec in (ledger.get("watchlist") or {}).items():
        priced = [
            o for o in (rec.get("observations") or [])
            if o.get("price_gbp")
        ]
        row = {
            "ticker":          ticker,
            "thesis":          rec.get("thesis", ""),
            "theme":           rec.get("theme"),
            "first_seen":      rec.get("first_seen"),
            "active":          bool(rec.get("active")),
            "exited_on":       rec.get("exited_on"),
            "tracking_ended":  rec.get("tracking_ended"),
            "weeks_tracked":   _weeks_since(rec.get("first_seen")),
            "bought_date":     _watchlist_bought_date(
                ledger, ticker, rec.get("first_seen")),
            "return_pct":       None,
            "benchmark_pct":    None,
            "vs_benchmark_pts": None,
        }
        if len(priced) >= 2:
            first, last = priced[0], priced[-1]
            row["return_pct"] = round(
                (last["price_gbp"] / first["price_gbp"] - 1) * 100, 2)
            b0 = first.get("benchmark_return_pct")
            b1 = last.get("benchmark_return_pct")
            if b0 is not None and b1 is not None and (100 + b0) != 0:
                # Both are inception-relative, so the benchmark's return over
                # THIS name's window is the ratio of the two, not their
                # difference.
                bench = ((100 + b1) / (100 + b0) - 1) * 100
                row["benchmark_pct"] = round(bench, 2)
                row["vs_benchmark_pts"] = round(row["return_pct"] - bench, 2)
        scored.append(row)

    return sorted(
        scored,
        key=lambda r: (r["vs_benchmark_pts"] is None, -(r["vs_benchmark_pts"] or 0)),
    )


def build_watchlist_review(ledger: dict) -> str:
    """
    Build the watchlist section for the Claude weekly prompt.

    Replays every tracked name with its age, its recorded thesis and how it has
    actually performed since first mention. This is recording only: there is no
    obligation to buy a watchlist name, no gate on buying one that isn't
    listed, and dropping a name costs nothing but a stated reason.
    """
    rows = watchlist_performance(ledger)
    if not rows:
        return ""

    lines = [
        "=== Watchlist accountability (RECORDING ONLY — no gate on any trade) ===",
        "Names you have flagged previously, with what they have actually done",
        "since. Carry forward the ones you still believe in (repeat them in this",
        "run's watchlist array), and DROP the ones you don't by simply omitting",
        "them — but say in one line why any name you drop is no longer of",
        "interest. Dropped names keep being priced, so an idea abandoned just",
        "before it ran will show up. None of this restricts what you may buy.",
        "",
    ]
    for row in rows:
        weeks = row["weeks_tracked"]
        age = f"{weeks}w" if weeks is not None else "?"
        # An exited holding isn't a dropped idea — it's a position that was
        # sold, and the point of tracking it is to score that decision.
        if row["active"]:
            state = "active"
        elif row["exited_on"]:
            state = f"SOLD {row['exited_on']}"
        else:
            state = "dropped"
        if row["tracking_ended"]:
            state = "tracking ended"
        if row["bought_date"]:
            state += f", BOUGHT {row['bought_date']}"
        if row["return_pct"] is None:
            perf = "no scoreable price history yet"
        else:
            perf = f"{row['return_pct']:+.1f}% since first seen"
            if row["vs_benchmark_pts"] is not None:
                perf += (
                    f" (benchmark {row['benchmark_pct']:+.1f}% over the same"
                    f" window, {row['vs_benchmark_pts']:+.1f} pts)"
                )
        lines.append(
            f"  {row['ticker']} (first seen {row['first_seen']}, {age} ago, "
            f"{state})\n    {perf}"
        )
        if row["thesis"]:
            lines.append(f"    Recorded thesis: {row['thesis']}")
    lines.append("")
    return "\n".join(lines)


def format_watchlist_for_email(ledger: dict) -> str:
    """
    Format tracked watchlist names and their performance for the weekly email.

    Purely a scoreboard for ideas the agent flagged but did not buy — the
    counterfactual that decides whether its idea generation is worth anything.
    """
    rows = watchlist_performance(ledger)
    if not rows:
        return ""

    lines = ["=== Watchlist tracking (recorded, not acted on) ==="]
    for row in rows:
        weeks = row["weeks_tracked"]
        age = f"{weeks}w" if weeks is not None else "?"
        if row["active"]:
            flags = []
        elif row["exited_on"]:
            flags = [f"sold {row['exited_on']}"]
        else:
            flags = ["dropped"]
        if row["tracking_ended"]:
            flags = ["tracking ended"]
        if row["bought_date"]:
            flags.append(f"bought {row['bought_date']}")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        if row["return_pct"] is None:
            lines.append(f"  {row['ticker']:<8} {age:>4} tracked  (no score yet){suffix}")
        else:
            vs = (
                f"  vs bench: {row['vs_benchmark_pts']:>+6.1f} pts"
                if row["vs_benchmark_pts"] is not None else ""
            )
            lines.append(
                f"  {row['ticker']:<8} {age:>4} tracked  "
                f"{row['return_pct']:>+7.2f}%{vs}{suffix}"
            )

    scoreable = [r for r in rows if r["vs_benchmark_pts"] is not None]
    if scoreable:
        avg = sum(r["vs_benchmark_pts"] for r in scoreable) / len(scoreable)
        lines.append(
            f"  ---\n  {len(scoreable)} scoreable name(s), average "
            f"{avg:+.1f} pts vs benchmark over their own windows."
        )
        lines.append(
            "  (Ideas flagged but not bought. If this stays positive while the\n"
            "   held book lags, the agent's problem is deployment, not picking.)"
        )
    return "\n".join(lines)


def entry_thesis_provenance(pos: dict) -> tuple[str, str]:
    """
    Say whether a position's entry thesis was actually written at entry.

    Three of the nine holdings predate the thesis field, and the difference
    matters more than bookkeeping: a case reconstructed on 2026-07-03 for a
    position bought on 2026-04-26 was written with ten weeks of price history
    already visible. That is a justification produced with the outcome known,
    which is the confirmation-seeking the Sep 2026 deep review flagged — and
    it is exactly the reasoning the thesis-accountability loop exists to test.
    A recorded entry thesis can be wrong; a reconstructed one cannot be
    scored at all, because it was never a prediction.

    Not grounds for a mechanical sell. Selling on a record-keeping defect
    would be a trade forced by paperwork rather than by fundamentals — the
    AVGO process error the same review called the worst artefact in the book.
    It is grounds for having to re-underwrite the position.

    Returns (kind, detail) where kind is one of "recorded", "backfilled",
    "synced" or "missing". A thesis replaced via SET_THESIS is "recorded" —
    it is a live prediction from the day it was written — with a detail
    naming that day so the review scores it from there, not from entry.
    """
    thesis = (pos.get("thesis") or "").strip()
    if not thesis or thesis == "(no thesis recorded)":
        return "missing", "no thesis on record at all"
    if thesis.startswith("(synced from T212)"):
        return "synced", "placeholder written by T212 sync, never a stated case"
    low = thesis[:80].lower()
    if low.startswith("[re-underwritten"):
        when = pos.get("thesis_reunderwritten") or thesis[16:27].strip(" ]")
        return "recorded", (f"re-underwritten {when}; a prediction from "
                            f"that date, not from entry")
    if "backfilled" in low:
        return "backfilled", "reconstructed after entry, with price history visible"
    return "recorded", ""


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
            theme   = pos.get("theme")
            entry = (
                f"  {ticker} (bought {bought}, P&L: {pnl_str}"
                + (f", theme: {theme}" if theme else "")
                + f")\n    Entry thesis: {thesis}"
            )
            kind, detail = entry_thesis_provenance(pos)
            if kind != "recorded":
                entry += (
                    f"\n    *** ENTRY THESIS NOT RECORDED AT ENTRY ({kind}: "
                    f"{detail}). This case was never a prediction, so its\n"
                    f"        track record cannot be scored and re-confirming "
                    f"it proves nothing. RE-UNDERWRITE {ticker} THIS RUN: state "
                    f"the case you\n        would buy it on fresh today at this "
                    f"weight, as a SET_THESIS action (NOT SET_DRIVER - that "
                    f"declares the\n        thesis played out and forces a "
                    f"33% bank), or recycle the capital. If it has also "
                    f"earned\n        nothing since entry, the single-name "
                    f"dependency block applies to it directly. ***"
                )
            elif detail:
                entry += f"\n    (Thesis {detail}.)"
            entry += _format_forward_driver(
                pos, bank_due=played_out_bank_due(ledger, ticker, pos))
            trims = pos.get("pre_commit_trims")
            if trims:
                entry += (
                    f"\n    Pre-committed trim levels (BINDING — check against "
                    f"current P&L): {trims}"
                )
            else:
                entry += (
                    "\n    Pre-committed trim levels: NONE SET (legacy position)"
                    " — set binding levels THIS run via a SET_TRIMS action."
                )
            lines.append(entry)
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
        "If a thesis has PLAYED OUT, holding is not automatic: either name one\n"
        "new, independent, forward-looking driver that would justify buying it\n"
        "fresh at today's price and weight — and record it with a SET_DRIVER\n"
        "action so it can be re-tested next week — or TRIM/exit and recycle the\n"
        "capital into better forward risk/reward. Any position already carrying\n"
        "a recorded forward driver is marked below and MUST be re-argued.\n"
    )

    return "\n".join(lines)


def format_attribution_for_email(val: dict) -> str:
    """
    Format a per-position P&L attribution table for the weekly email.

    Shows each position sorted by total P&L contribution (highest first), with
    portfolio weight, absolute P&L, P&L %, and contribution in percentage points
    relative to starting capital. Contribution points sum to the total return pts
    from equities (cash drag excluded).

    Args:
        val: Valuation dict from valuation().

    Returns:
        str: Multi-line attribution table, or empty string if no priced positions.
    """
    positions = {
        t: p for t, p in val["positions"].items()
        if p.get("pnl_gbp") is not None and p.get("current_value_gbp") is not None
    }
    if not positions:
        return ""

    start = val.get("starting_capital_gbp") or 1
    total = val.get("total_value_gbp") or 1

    sorted_pos = sorted(positions.items(), key=lambda x: x[1]["pnl_gbp"], reverse=True)

    lines = ["Position attribution (vs entry cost):"]
    for ticker, p in sorted_pos:
        weight  = p["current_value_gbp"] / total * 100
        contrib = p["pnl_gbp"] / start * 100
        lines.append(
            f"  {ticker:<6}  £{p['current_value_gbp']:>8.2f} ({weight:>4.1f}%)  "
            f"P&L: £{p['pnl_gbp']:>+8.2f} ({p['pnl_pct']:>+6.2f}%)  "
            f"contrib: {contrib:>+5.2f}pts"
        )
    return "\n".join(lines)


def build_fx_review(ledger: dict, valuation_result: dict) -> str:
    """
    Currency decomposition section for the weekly prompt.

    The prompt carried no FX data at all — every price in it is GBP — so a
    claim like "the loss is a GBP FX artefact" could be neither supported nor
    refuted from anything the agent was given, and on 2026-09-01 it was made
    for exactly the three red positions while the two largest real FX drags
    sat on positions described as unqualified winners. Putting the split in
    front of Claude makes the currency claim answerable with a number.
    """
    fx = fx_neutral_returns(ledger, valuation_result)
    if not fx:
        return ""
    lines = ["=== Currency decomposition (GBP return vs native return) ==="]
    for ticker, d in sorted(fx.items(), key=lambda x: x[1]["fx_pts"]):
        est = " (entry rate estimated)" if d["estimated"] else ""
        lines.append(
            f"  {ticker:<6} GBP {d['gbp_pct']:>+7.2f}%  "
            f"{d['currency']} {d['local_pct']:>+7.2f}%  "
            f"FX contribution {d['fx_pts']:>+5.2f}pts{est}"
        )
    drags = [d["fx_pts"] for d in fx.values()]
    lines.append(
        f"  Range across the book: {min(drags):+.2f} to {max(drags):+.2f} pts."
    )
    lines.append(
        "  READ THIS BEFORE ATTRIBUTING ANYTHING TO CURRENCY: the FX move is\n"
        "  common to every holding in the same currency over the same window,\n"
        "  so it can never explain why one position is down and another is up.\n"
        "  The native-currency column is what the business did. Do not describe\n"
        "  a loss as an FX artefact unless the FX contribution column above\n"
        "  actually accounts for it, and quote the number when you do."
    )
    return "\n".join(lines) + "\n"


def format_fx_for_email(fx: dict) -> str:
    """
    Format the currency decomposition for the weekly email.

    Every holding is priced in GBP, so the reported P&L blends the business with
    sterling. Showing both side by side is the only thing that makes a claim
    like "that loss is a GBP FX artefact" checkable rather than rhetorical.
    """
    if not fx:
        return ""
    lines = ["Currency decomposition (GBP return vs native return):"]
    for ticker, d in sorted(fx.items(), key=lambda x: x[1]["fx_pts"]):
        est = " ~" if d["estimated"] else "  "
        lines.append(
            f"  {ticker:<6}{est}GBP {d['gbp_pct']:>+7.2f}%   "
            f"{d['currency']} {d['local_pct']:>+7.2f}%   "
            f"FX {d['fx_pts']:>+5.2f}pts"
        )
    drags = [d["fx_pts"] for d in fx.values()]
    lines.append(
        f"  ---\n  FX moved the book between {min(drags):+.2f} and "
        f"{max(drags):+.2f} pts. It applies to every USD holding at once,\n"
        f"  so it cannot explain why one position is down and another is up."
    )
    if any(d["estimated"] for d in fx.values()):
        lines.append("  (~ entry rate reconstructed from first_bought, not exact.)")
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
