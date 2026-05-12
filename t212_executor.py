"""
t212_executor.py — Stage 2: Execute shadow recommendations as real demo trades.

Bridges the shadow portfolio and the T212 demo account so live positions
appear in the T212 app.

Enable by setting T212_DEMO_EXECUTE=true in your .env file.

---

Ticker translation — how it works globally:

yfinance uses exchange suffixes like AAPL, SHEL.L, SAP.DE, 7203.T.
T212 uses its own format like AAPL_US_EQ, SHEL_EQ, SAP_DE_EQ.

We translate by matching in this priority order:
  1. Manual aliases (for known quirks, e.g. META vs FB history).
  2. Root symbol + expected currency (inferred from yfinance suffix).
  3. Root symbol + T212 exchange marker heuristic.
  4. Root symbol only (last resort, logged as a warning).

Currency is the most reliable signal because every T212 instrument carries
it and every yfinance suffix maps unambiguously to one or two currencies.
This is why matching by currency works for all major global exchanges.
"""

from __future__ import annotations

import json
import time
import os
import base64
from pathlib import Path
from typing import Optional

import requests
from shadow_portfolio import fetch_price_gbp


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
T212_API_KEY = os.getenv("T212_API_KEY", "").strip()
T212_API_SECRET = os.getenv("T212_API_SECRET", "").strip()
T212_ENV = os.getenv("T212_ENV", "demo").lower().strip()
T212_BASE_URL = (
    "https://live.trading212.com/api/v0"
    if T212_ENV == "live"
    else "https://demo.trading212.com/api/v0"
)
T212_DEMO_EXECUTE = os.getenv("T212_DEMO_EXECUTE", "false").lower().strip() == "true"

INSTRUMENTS_CACHE_PATH = Path(
    os.getenv("T212_INSTRUMENTS_CACHE", "t212_instruments.json")
)


# -----------------------------------------------------------------------------
# Global exchange reference — the heart of robust ticker translation
# -----------------------------------------------------------------------------
# Maps yfinance exchange suffix → the set of currencies we'd expect T212 to
# report for that exchange. Most are single-currency; some have alternatives
# (e.g. LSE reports GBP or GBX for pence).
#
# When Claude suggests a ticker like "SHEL.L", we strip the .L, look up
# "L" here, and get {"GBP", "GBX"}. We then match T212 instruments with root
# SHEL and currency in that set.
YF_SUFFIX_TO_CURRENCIES: dict[str, set[str]] = {
    "":    {"USD"},                    # US (NYSE/NASDAQ)
    "L":   {"GBP", "GBX", "GBp"},      # London Stock Exchange
    "DE":  {"EUR"},                    # Xetra (Germany)
    "F":   {"EUR"},                    # Frankfurt
    "MU":  {"EUR"},                    # Munich
    "BE":  {"EUR"},                    # Berlin
    "PA":  {"EUR"},                    # Euronext Paris
    "AS":  {"EUR"},                    # Euronext Amsterdam
    "BR":  {"EUR"},                    # Euronext Brussels
    "LS":  {"EUR"},                    # Euronext Lisbon
    "MI":  {"EUR"},                    # Borsa Italiana (Milan)
    "MC":  {"EUR"},                    # Madrid
    "IR":  {"EUR"},                    # Ireland
    "VI":  {"EUR"},                    # Vienna
    "HE":  {"EUR"},                    # Helsinki
    "SW":  {"CHF"},                    # SIX Swiss Exchange
    "VX":  {"CHF"},                    # SIX Swiss alt
    "ST":  {"SEK"},                    # Stockholm
    "CO":  {"DKK"},                    # Copenhagen
    "OL":  {"NOK"},                    # Oslo
    "IC":  {"ISK"},                    # Iceland
    "WA":  {"PLN"},                    # Warsaw
    "PR":  {"CZK"},                    # Prague
    "BD":  {"HUF"},                    # Budapest
    "IS":  {"TRY"},                    # Istanbul
    "TO":  {"CAD"},                    # Toronto (TSX)
    "V":   {"CAD"},                    # TSX Venture
    "CN":  {"CAD"},                    # CSE
    "HK":  {"HKD"},                    # Hong Kong
    "T":   {"JPY"},                    # Tokyo
    "TWO": {"TWD"},                    # Taiwan OTC
    "TW":  {"TWD"},                    # Taiwan
    "KS":  {"KRW"},                    # Korea KOSPI
    "KQ":  {"KRW"},                    # Korea KOSDAQ
    "SS":  {"CNY"},                    # Shanghai
    "SZ":  {"CNY"},                    # Shenzhen
    "SI":  {"SGD"},                    # Singapore
    "AX":  {"AUD"},                    # ASX (Australia)
    "NZ":  {"NZD"},                    # New Zealand
    "JO":  {"ZAR"},                    # Johannesburg
    "MX":  {"MXN"},                    # Mexico
    "SA":  {"BRL"},                    # Sao Paulo (B3)
    "BA":  {"ARS"},                    # Buenos Aires
    "SN":  {"CLP"},                    # Santiago (Chile)
    "TA":  {"ILS"},                    # Tel Aviv
}


# -----------------------------------------------------------------------------
# Manual aliases — yfinance ticker → exact T212 ticker
# Used when the automatic translation fails or we know the exact answer.
# Expand this list as you encounter edge cases.
# -----------------------------------------------------------------------------
TICKER_ALIASES: dict[str, str] = {
    # Only add entries here when you've CONFIRMED the exact T212 ticker.
    # Wrong aliases cause 404s and bypass the search.

    # Berkshire — dot in symbol requires explicit mapping
    "BRK.B": "BRK.B_US_EQ",
    "BRK.A": "BRK.A_US_EQ",
    "BRK-B": "BRK.B_US_EQ",

    # UK stocks where root symbol is ambiguous
    "BA.L":   "BAES_EQ",      # BAE Systems
    "RR.L":   "RR_EQ",        # Rolls-Royce
    "LSEG.L": "LSEG_EQ",      # London Stock Exchange Group
}


# -----------------------------------------------------------------------------
# Auth (shared with main script)
# -----------------------------------------------------------------------------
def _headers() -> dict:
    if T212_API_SECRET:
        token = base64.b64encode(
            f"{T212_API_KEY}:{T212_API_SECRET}".encode("ascii")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}",
                "Content-Type": "application/json"}
    return {"Authorization": T212_API_KEY,
            "Content-Type": "application/json"}


# -----------------------------------------------------------------------------
# Instrument lookup
# -----------------------------------------------------------------------------
def _load_instruments() -> list[dict]:
    """Fetch all tradable instruments from T212, with 24h local cache."""
    if INSTRUMENTS_CACHE_PATH.exists():
        age = time.time() - INSTRUMENTS_CACHE_PATH.stat().st_mtime
        if age < 86400:
            with open(INSTRUMENTS_CACHE_PATH) as f:
                return json.load(f)

    print("  Fetching T212 instrument list (cached for 24h)...")
    r = requests.get(
        f"{T212_BASE_URL}/equity/metadata/instruments",
        headers=_headers(), timeout=30,
    )
    r.raise_for_status()
    instruments = r.json()
    with open(INSTRUMENTS_CACHE_PATH, "w") as f:
        json.dump(instruments, f)
    print(f"  Fetched {len(instruments)} instruments.")
    return instruments


def _parse_yf_ticker(yf_ticker: str) -> tuple[str, str]:
    """Split "SHEL.L" -> ("SHEL", "L"); "AAPL" -> ("AAPL", "")."""
    upper = yf_ticker.upper().strip()
    if "." in upper:
        # Handle BRK.B — only split on LAST dot if suffix is a known exchange
        base, suffix = upper.rsplit(".", 1)
        if suffix in YF_SUFFIX_TO_CURRENCIES:
            return base, suffix
        # Otherwise treat the dot as part of the symbol (e.g. BRK.B)
        return upper, ""
    return upper, ""


def _instrument_currency(inst: dict) -> str:
    """Extract currency from an instrument record, tolerating field variations."""
    return str(
        inst.get("currencyCode")
        or inst.get("currency")
        or ""
    ).upper()


def _instrument_root(inst: dict) -> str:
    """Get the root symbol from a T212 ticker.
    Handles both formats:
      AAPL_US_EQ   -> AAPL   (standard)
      META.US_EQ   -> META   (dot-format, some US stocks)
      BRK.B_US_EQ  -> BRK.B (symbol contains dot)
    Strategy: split on first underscore, then strip any exchange suffix
    that follows a dot (e.g. .US in META.US)."""
    ticker = inst.get("ticker", "")
    # Get everything before the first underscore
    root = ticker.split("_", 1)[0].upper()
    # If root contains a dot followed by an exchange code (e.g. META.US),
    # and it's NOT a known symbol-dot like BRK.B, strip the .EXCHANGE part
    if "." in root:
        base, suffix = root.rsplit(".", 1)
        # Exchange suffixes are 2-3 alpha chars (US, DE, HK etc.)
        # Symbol dots have 1-char suffixes (BRK.B) or digits (0700.HK handled above)
        if suffix.isalpha() and 2 <= len(suffix) <= 3:
            return base
    return root


def yf_to_t212_ticker(yf_ticker: str,
                      instruments: list[dict]) -> Optional[str]:
    """
    Translate a yfinance-style ticker to the correct T212 ticker.

    Priority:
      1. Manual alias map
      2. Match on shortName field (most reliable — T212's own symbol name)
      3. Currency-based root symbol matching as fallback
    """
    upper = yf_ticker.upper().strip()

    # Stage 1 — manual alias
    if upper in TICKER_ALIASES:
        return TICKER_ALIASES[upper]

    # Stage 2 — parse the yfinance ticker
    base_symbol, yf_suffix = _parse_yf_ticker(upper)
    expected_currencies = YF_SUFFIX_TO_CURRENCIES.get(yf_suffix, set())

    # Stage 3 — match on shortName (T212's own symbol, most direct)
    short_matches = [
        inst for inst in instruments
        if (inst.get("shortName") or "").upper() == base_symbol
    ]
    if short_matches:
        if len(short_matches) == 1:
            return short_matches[0]["ticker"]
        # Multiple shortName matches — filter by currency
        if expected_currencies:
            currency_short = [
                m for m in short_matches
                if _instrument_currency(m) in expected_currencies
            ]
            if currency_short:
                return currency_short[0]["ticker"]
        return short_matches[0]["ticker"]

    # Stage 4 — fall back to ticker root matching
    matches = [inst for inst in instruments
               if _instrument_root(inst) == base_symbol]

    if not matches:
        matches = [inst for inst in instruments
                   if inst.get("ticker", "").upper().startswith(base_symbol + "_")]

    if not matches:
        print(f"  ! No T212 instrument found for '{yf_ticker}' "
              f"(tried shortName='{base_symbol}' and ticker root)")
        return None

    if len(matches) == 1:
        return matches[0]["ticker"]

    # Stage 5 — currency filter
    if expected_currencies:
        currency_matches = [
            m for m in matches
            if _instrument_currency(m) in expected_currencies
        ]
        if currency_matches:
            matches = currency_matches

    # Stage 6 — exchange marker heuristic
    is_us = (yf_suffix == "")
    for inst in matches:
        t = inst.get("ticker", "")
        if is_us and "_US_EQ" in t:
            return t
        if not is_us and "_US_" not in t:
            return t

    print(f"  ! Ambiguous match for '{yf_ticker}' — using {matches[0]['ticker']}")
    return matches[0]["ticker"]


def t212_to_yf_ticker(t212_ticker: str,
                      instruments: list[dict]) -> Optional[str]:
    """
    Reverse translation: T212 ticker → yfinance ticker.
    Useful for price lookup on existing positions.
    """
    for inst in instruments:
        if inst.get("ticker", "").upper() == t212_ticker.upper():
            root = _instrument_root(inst)
            currency = _instrument_currency(inst)
            # Find the yfinance suffix that matches this currency
            for suffix, currencies in YF_SUFFIX_TO_CURRENCIES.items():
                if currency in currencies:
                    return f"{root}.{suffix}" if suffix else root
            return root  # Unknown currency — just return the root
    return None


# -----------------------------------------------------------------------------
# Account state
# -----------------------------------------------------------------------------
def get_pending_buy_tickers(instruments: list[dict], t212_to_yf_fn) -> set[str]:
    """
    Return yfinance tickers that have an open (unfilled) buy order in T212.

    Used by sync_from_t212 to avoid removing positions whose T212 order was
    placed outside market hours and is merely queued, not failed.
    Returns empty set on any API error so callers degrade gracefully.
    """
    try:
        r = requests.get(
            f"{T212_BASE_URL}/equity/orders",
            headers=_headers(), timeout=20,
        )
        r.raise_for_status()
        orders = r.json()
    except Exception as e:
        print(f"  ! Couldn't fetch pending orders (sync will be conservative): {e}")
        return set()

    if not isinstance(orders, list):
        return set()

    pending: set[str] = set()
    terminal = {"FILLED", "CANCELLED", "CANCELLING", "REJECTED", "REPLACED"}
    for order in orders:
        if not isinstance(order, dict):
            continue
        side = (order.get("side") or "").upper()
        status = (order.get("status") or "").upper()
        if "BUY" not in side or status in terminal:
            continue
        t212_ticker = order.get("ticker", "")
        if not t212_ticker:
            continue
        yf_ticker = t212_to_yf_fn(t212_ticker)
        if yf_ticker:
            pending.add(yf_ticker)

    if pending:
        print(f"  Pending T212 buy orders: {sorted(pending)}")
    return pending


def get_t212_positions_map() -> dict[str, dict]:
    """Returns {t212_ticker: position_data} for all open positions."""
    r = requests.get(
        f"{T212_BASE_URL}/equity/positions",
        headers=_headers(), timeout=20,
    )
    r.raise_for_status()
    positions = r.json()
    result = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        # T212 API returns flat {"ticker": ...} or nested {"instrument": {"ticker": ...}}
        ticker = (
            p["instrument"]["ticker"] if "instrument" in p
            else p.get("ticker", "")
        )
        if ticker:
            result[ticker] = p
    return result


# -----------------------------------------------------------------------------
# Orders
# -----------------------------------------------------------------------------
def _search_translate(yf_ticker: str, instruments: list[dict]) -> Optional[str]:
    """Translation using only the search logic, bypassing aliases.
    Used as fallback when an alias returns 404."""
    upper = yf_ticker.upper().strip()
    base_symbol, yf_suffix = _parse_yf_ticker(upper)
    expected_currencies = YF_SUFFIX_TO_CURRENCIES.get(yf_suffix, set())
    matches = [inst for inst in instruments
               if _instrument_root(inst) == base_symbol]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]["ticker"]
    if expected_currencies:
        currency_matches = [m for m in matches
                            if _instrument_currency(m) in expected_currencies]
        if currency_matches:
            matches = currency_matches
    is_us = (yf_suffix == "")
    for inst in matches:
        t = inst.get("ticker", "")
        if is_us and "_US_EQ" in t:
            return t
        if not is_us and "_US_" not in t:
            return t
    return matches[0]["ticker"]


def _place_market_order(t212_ticker: str, quantity: float) -> dict:
    """Market order. Positive quantity = BUY, negative = SELL."""
    qty = round(abs(quantity), 4)
    if quantity < 0:
        qty = -qty
    payload = {
        "ticker": t212_ticker,
        "quantity": qty,
        "extendedHours": True,
    }
    r = requests.post(
        f"{T212_BASE_URL}/equity/orders/market",
        headers=_headers(),
        json=payload,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# -----------------------------------------------------------------------------
# Input validation
# -----------------------------------------------------------------------------
def _is_valid_ticker(yf_ticker: str) -> bool:
    """Reject obviously malformed tickers (double dots, weird chars, etc.)."""
    if not yf_ticker or len(yf_ticker) > 15:
        return False
    t = yf_ticker.upper().strip()
    # Reject double dots (e.g. "BA..L")
    if ".." in t:
        return False
    # Must be alphanumeric plus dots/hyphens only
    for c in t:
        if not (c.isalnum() or c in ".-"):
            return False
    return True


# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------
def execute_recommendations(recs: list) -> tuple[list[str], list[dict]]:
    """
    Mirror recommendations as real T212 demo orders.

    Returns (events, confirmed_recs). confirmed_recs contains only the recs
    T212 successfully accepted — shadow must apply these only, to prevent drift
    when T212 fails (e.g. insufficient funds, ticker not found).

    When T212_DEMO_EXECUTE=false, returns ([], all_recs) so shadow applies everything.
    """
    if not T212_DEMO_EXECUTE:
        return [], list(recs)
    if T212_ENV == "live":
        print("  ! T212_DEMO_EXECUTE=true but T212_ENV=live — refusing to execute.")
        return ["SKIPPED: T212_DEMO_EXECUTE refused on live account."], []
    if not recs:
        return [], []

    events: list[str] = []
    confirmed_recs: list[dict] = []
    instruments = _load_instruments()
    positions_map = get_t212_positions_map()

    for rec in recs:
        action = rec.get("action", "").upper().strip()
        yf_ticker = (rec.get("yfinance_ticker") or rec.get("ticker") or "").strip()
        if not yf_ticker or action not in ("BUY", "SELL", "TRIM"):
            continue

        if not _is_valid_ticker(yf_ticker):
            events.append(f"SKIP {action} '{yf_ticker}': invalid ticker format")
            continue

        t212_ticker = yf_to_t212_ticker(yf_ticker, instruments)
        if not t212_ticker:
            events.append(f"SKIP {action} {yf_ticker}: no T212 ticker found")
            continue

        try:
            if action == "BUY":
                amount_gbp = float(rec.get("amount_gbp") or 0)
                if amount_gbp <= 0:
                    events.append(f"SKIP BUY {yf_ticker}: no amount specified")
                    continue
                price_gbp = fetch_price_gbp(yf_ticker)
                if not price_gbp or price_gbp <= 0:
                    events.append(f"SKIP BUY {yf_ticker}: couldn't fetch price")
                    continue
                shares = amount_gbp / price_gbp
                order_placed = False
                try:
                    result = _place_market_order(t212_ticker, shares)
                    order_placed = True
                except requests.HTTPError as e:
                    if e.response.status_code == 404 and yf_ticker.upper() in TICKER_ALIASES:
                        print(f"  ! Alias {t212_ticker} returned 404, "
                              f"retrying with instrument search...")
                        t212_ticker = _search_translate(yf_ticker, instruments)
                        if t212_ticker:
                            result = _place_market_order(t212_ticker, shares)
                            order_placed = True
                        else:
                            events.append(f"SKIP BUY {yf_ticker}: "
                                          f"alias 404 and search found nothing")
                            continue
                    else:
                        raise
                if order_placed:
                    events.append(
                        f"T212 BUY {shares:.4f} {t212_ticker} "
                        f"(order {result.get('id', '?')})"
                    )
                    confirmed_recs.append(rec)
                    time.sleep(1.2)

            elif action in ("SELL", "TRIM"):
                pos = positions_map.get(t212_ticker)
                if not pos:
                    events.append(
                        f"SKIP {action} {yf_ticker}: "
                        f"no T212 position found for {t212_ticker}"
                    )
                    continue
                held = float(pos.get("quantity", 0))
                if held <= 0:
                    events.append(f"SKIP {action} {yf_ticker}: zero quantity held")
                    continue
                qty_to_sell = (
                    held if action == "SELL"
                    else held * (float(rec.get("trim_pct") or 50) / 100)
                )
                result = _place_market_order(t212_ticker, -qty_to_sell)
                events.append(
                    f"T212 {action} {qty_to_sell:.4f} {t212_ticker} "
                    f"(order {result.get('id', '?')})"
                )
                confirmed_recs.append(rec)
                time.sleep(1.2)

        except requests.HTTPError as e:
            events.append(
                f"T212 ORDER FAILED {action} {t212_ticker}: "
                f"{e.response.status_code} {e.response.text[:200]}"
            )
        except Exception as e:
            events.append(f"T212 ORDER ERROR {action} {t212_ticker}: {e}")

    return events, confirmed_recs
