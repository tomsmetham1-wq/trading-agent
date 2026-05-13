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
  1. Manual aliases (for known quirks, e.g. BRK.B which has a dot in the symbol).
  2. T212 shortName field match (T212's own symbol — most direct and reliable).
  3. Root symbol + expected currency (inferred from yfinance exchange suffix).
  4. Root symbol + T212 exchange marker heuristic (_US_EQ vs non-US).
  5. Root symbol only — last resort, logged as a warning.

Currency is the most reliable signal because every T212 instrument carries a
currencyCode and every yfinance suffix maps unambiguously to one or two currencies.
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


# =============================================================================
# Config
# =============================================================================

T212_API_KEY    = os.getenv("T212_API_KEY", "").strip()
T212_API_SECRET = os.getenv("T212_API_SECRET", "").strip()
T212_ENV        = os.getenv("T212_ENV", "demo").lower().strip()
T212_BASE_URL   = (
    "https://live.trading212.com/api/v0"
    if T212_ENV == "live"
    else "https://demo.trading212.com/api/v0"
)
T212_DEMO_EXECUTE = os.getenv("T212_DEMO_EXECUTE", "false").lower().strip() == "true"

INSTRUMENTS_CACHE_PATH = Path(
    os.getenv("T212_INSTRUMENTS_CACHE", "t212_instruments.json")
)


# =============================================================================
# Exchange reference — the heart of robust ticker translation
# =============================================================================

# Maps yfinance exchange suffix → set of currencies T212 uses for that exchange.
# Most exchanges are single-currency; LSE has GBP and GBX (pence) variants.
#
# When Claude suggests "SHEL.L", we strip ".L", look up "L" here, get {"GBP", "GBX"},
# then filter T212 instruments with root "SHEL" whose currencyCode is in that set.
YF_SUFFIX_TO_CURRENCIES: dict[str, set[str]] = {
    "":    {"USD"},                    # US (NYSE/NASDAQ)
    "L":   {"GBP", "GBX", "GBp"},     # London Stock Exchange
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


# =============================================================================
# Manual ticker aliases
# =============================================================================

# Maps yfinance ticker → exact T212 ticker. Only add entries here when you've
# CONFIRMED the T212 ticker exists. Wrong aliases cause 404s and bypass the
# automatic search entirely.
TICKER_ALIASES: dict[str, str] = {
    # Berkshire — T212 uses BRK_B_US_EQ (underscore separator, not dot).
    # BRK.B and BRK.A also resolve correctly via shortName matching, but are
    # kept here to be explicit. BRK-B (hyphen) REQUIRES the alias — the hyphen
    # doesn't appear in any shortName or ticker root so auto-translate fails.
    "BRK.B":  "BRK_B_US_EQ",   # shortName="BRK.B" (Berkshire Class B)
    "BRK.A":  "BRK/A_US_EQ",   # shortName="BRK.A" (Berkshire Class A, T212 uses slash)
    "BRK-B":  "BRK_B_US_EQ",   # Hyphen variant — needs explicit alias

    # Meta Platforms — T212 still uses the old Facebook ticker FB_US_EQ.
    # Without this alias, the shortName search finds three "META" matches
    # (FB_US_EQ + two WisdomTree ETFs), and could return the wrong one if the
    # list order ever changes. Confirmed: FB_US_EQ is Meta Platforms (ISIN US30303M1027).
    "META":   "FB_US_EQ",

    # UK stocks — explicit mappings to lock in the correct GBX-listed instrument.
    # Auto-translation via shortName+currency also works for these, but aliases
    # prevent any future ambiguity if T212 adds new instruments with the same shortName.
    "BA.L":   "BAl_EQ",    # BAE Systems (shortName=BA, GBX — distinct from BA_US_EQ=Richtech)
    "RR.L":   "RRl_EQ",    # Rolls-Royce (shortName=RR, GBX — distinct from RR_US_EQ=Richtech)
    "LSEG.L": "LSEl_EQ",   # London Stock Exchange Group (shortName=LSEG, GBX)
}


# =============================================================================
# Auth
# =============================================================================

def _headers() -> dict:
    """
    Build Authorization + Content-Type headers for T212 API requests.

    Uses Basic auth with a base64-encoded "key:secret" pair (current API format).
    Falls back to a legacy single-token header if T212_API_SECRET is not set.

    Returns:
        dict: Headers dict ready to pass to requests.
    """
    if T212_API_SECRET:
        token = base64.b64encode(
            f"{T212_API_KEY}:{T212_API_SECRET}".encode("ascii")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
    return {"Authorization": T212_API_KEY, "Content-Type": "application/json"}


# =============================================================================
# Instrument lookup and caching
# =============================================================================

def _load_instruments() -> list[dict]:
    """
    Fetch the complete list of tradable instruments from T212, with a 24h local cache.

    T212 has ~17,000 instruments. Fetching them on every run is slow and unnecessary —
    the list changes infrequently. The cache is stored in t212_instruments.json and
    refreshed automatically when it's older than 24 hours.

    Returns:
        list[dict]: Full instrument list. Each dict has at minimum "ticker",
                    "shortName", and "currencyCode" fields.

    Raises:
        requests.HTTPError: If the instruments endpoint returns a non-2xx status.
    """
    if INSTRUMENTS_CACHE_PATH.exists():
        age = time.time() - INSTRUMENTS_CACHE_PATH.stat().st_mtime
        if age < 86400:  # 24 hours
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


# =============================================================================
# Ticker parsing helpers
# =============================================================================

def _parse_yf_ticker(yf_ticker: str) -> tuple[str, str]:
    """
    Split a yfinance ticker into (root_symbol, exchange_suffix).

    Examples:
        "SHEL.L"  → ("SHEL", "L")
        "AAPL"    → ("AAPL", "")
        "BRK.B"   → ("BRK.B", "")   # .B is not a known exchange suffix
        "7203.T"  → ("7203", "T")

    The split happens on the LAST dot only when the suffix is a recognised
    exchange code in YF_SUFFIX_TO_CURRENCIES. This prevents treating "BRK.B"
    as root="BRK", suffix="B" — B is not an exchange code.

    Args:
        yf_ticker: Yahoo Finance ticker string (case-insensitive).

    Returns:
        tuple: (root_symbol, exchange_suffix). Suffix is "" for US tickers.
    """
    upper = yf_ticker.upper().strip()
    if "." in upper:
        base, suffix = upper.rsplit(".", 1)
        if suffix in YF_SUFFIX_TO_CURRENCIES:
            return base, suffix
        # Dot is part of the symbol (e.g. BRK.B) — treat whole thing as root
        return upper, ""
    return upper, ""


def _instrument_currency(inst: dict) -> str:
    """
    Extract the currency code from a T212 instrument record.

    Tolerates slight field name variations (currencyCode vs currency) that have
    appeared across different T212 API versions.

    Args:
        inst: T212 instrument dict from the instruments list.

    Returns:
        str: Upper-cased currency code, e.g. "USD", "GBP". Empty string if absent.
    """
    return str(
        inst.get("currencyCode")
        or inst.get("currency")
        or ""
    ).upper()


def _instrument_root(inst: dict) -> str:
    """
    Extract the root symbol from a T212 instrument's ticker string.

    T212 tickers come in several formats:
      Standard:     AAPL_US_EQ    → root "AAPL"
      Dot-format:   META.US_EQ   → root "META"  (some US stocks)
      Symbol-dot:   BRK.B_US_EQ  → root "BRK.B" (symbol itself contains a dot)
      Exchange-sfx: SHELl_EQ     → root "SHEL"  (T212 appends a lowercase letter
                    SAPd_EQ      → root "SAP"    as an exchange/listing disambiguator)
                    VUSAl_EQ     → root "VUSA"

    Key insight: T212 appends a SINGLE LOWERCASE LETTER to disambiguate different
    exchange listings (e.g. SHELl for London Shell vs SHEL_US_EQ for NYSE Shell).
    This letter must be stripped BEFORE uppercasing — otherwise "SHELl".upper()
    becomes "SHELL", mangling the symbol.

    Args:
        inst: T212 instrument dict.

    Returns:
        str: Root symbol in uppercase (e.g. "SHEL", "VUSA", "BRK.B").
    """
    ticker = inst.get("ticker", "")
    # Split on first underscore to get the pre-underscore part
    raw = ticker.split("_", 1)[0]
    # Strip T212's lowercase exchange-disambiguation suffix BEFORE uppercasing.
    # e.g. "SHELl" → "SHEL", "SAPd" → "SAP", "VUSAl" → "VUSA"
    # Single uppercase/digit/dot suffixes (like "BRK.B") are never stripped.
    while raw and raw[-1].islower():
        raw = raw[:-1]
    root = raw.upper()
    # Handle dot-exchange format: "META.US" → "META" (strip 2-3 char alpha suffix)
    if "." in root:
        base, suffix = root.rsplit(".", 1)
        if suffix.isalpha() and 2 <= len(suffix) <= 3:
            return base
    return root


# =============================================================================
# Ticker translation: yfinance → T212
# =============================================================================

def yf_to_t212_ticker(yf_ticker: str, instruments: list[dict]) -> Optional[str]:
    """
    Translate a yfinance-style ticker to the correct T212 instrument ticker.

    Translation pipeline (stops at first match):
      Stage 1 — Manual alias: check TICKER_ALIASES for an exact override.
      Stage 2 — shortName match: search instruments by T212's own "shortName" field.
                This is the most direct — if T212 names it "AAPL", that matches "AAPL".
                Disambiguates by currency when multiple exchanges list the same name.
      Stage 3 — Ticker root match: find instruments whose root symbol equals the base.
                Adds a prefix-match fallback (ticker starts with "SYMBOL_").
      Stage 4 — Currency filter: when multiple root matches exist, keep only those
                whose currencyCode matches the expected currency for the yfinance suffix.
      Stage 5 — Exchange heuristic: US tickers prefer "_US_EQ" variants; non-US
                prefer anything without "_US_" in the T212 ticker.

    Args:
        yf_ticker:   Yahoo Finance ticker (e.g. "AAPL", "SHEL.L", "SAP.DE").
        instruments: T212 instruments list from _load_instruments().

    Returns:
        str: T212 ticker string (e.g. "AAPL_US_EQ"), or None if not found.
    """
    upper = yf_ticker.upper().strip()

    # Stage 1: Manual alias overrides
    if upper in TICKER_ALIASES:
        return TICKER_ALIASES[upper]

    base_symbol, yf_suffix = _parse_yf_ticker(upper)
    expected_currencies = YF_SUFFIX_TO_CURRENCIES.get(yf_suffix, set())

    # Stage 2: shortName match (T212's own symbol label)
    short_matches = [
        inst for inst in instruments
        if (inst.get("shortName") or "").upper() == base_symbol
    ]
    if short_matches:
        if len(short_matches) == 1:
            return short_matches[0]["ticker"]
        if expected_currencies:
            currency_short = [
                m for m in short_matches
                if _instrument_currency(m) in expected_currencies
            ]
            if currency_short:
                return currency_short[0]["ticker"]
        return short_matches[0]["ticker"]

    # Stage 3: Ticker root match
    matches = [inst for inst in instruments if _instrument_root(inst) == base_symbol]
    if not matches:
        matches = [inst for inst in instruments
                   if inst.get("ticker", "").upper().startswith(base_symbol + "_")]

    if not matches:
        print(f"  ! No T212 instrument found for '{yf_ticker}' "
              f"(tried shortName='{base_symbol}' and ticker root)")
        return None

    if len(matches) == 1:
        return matches[0]["ticker"]

    # Stage 4: Currency filter
    if expected_currencies:
        currency_matches = [
            m for m in matches if _instrument_currency(m) in expected_currencies
        ]
        if currency_matches:
            matches = currency_matches

    # Stage 5: Exchange heuristic (US vs non-US)
    is_us = (yf_suffix == "")
    for inst in matches:
        t = inst.get("ticker", "")
        if is_us and "_US_EQ" in t:
            return t
        if not is_us and "_US_" not in t:
            return t

    print(f"  ! Ambiguous match for '{yf_ticker}' — using {matches[0]['ticker']}")
    return matches[0]["ticker"]


def t212_to_yf_ticker(t212_ticker: str, instruments: list[dict]) -> Optional[str]:
    """
    Reverse translation: T212 ticker → yfinance ticker.

    Used by the sync logic to convert T212 position tickers back to yfinance format
    for price lookups and shadow ledger keying.

    Strategy: find the instrument by exact T212 ticker match, then use shortName
    (T212's current market symbol) as the yfinance root — not the ticker root.
    This correctly handles:
      - Renamed stocks: FB_US_EQ → "META" (shortName), not "FB" (ticker root)
      - NatWest: RBSl_EQ → "NWG" (shortName), not "RBSL" (mangled ticker root)
      - BHP: BLTl_EQ → "BHP" (shortName), not "BLTL" (mangled ticker root)

    Currency is used to pick the yfinance exchange suffix:
      USD → bare symbol (e.g. "META")
      GBP/GBX → ".L" suffix (e.g. "SHEL.L")
      EUR → ".DE" suffix (first EUR entry; other Eurozone exchanges are a known
            limitation — not an issue for the current UK/US-focused strategy)

    Args:
        t212_ticker:  T212 ticker string (e.g. "AAPL_US_EQ", "SHELl_EQ", "FB_US_EQ").
        instruments:  T212 instruments list from _load_instruments().

    Returns:
        str: yfinance ticker (e.g. "AAPL", "SHEL.L", "META"), or None if not found.
    """
    for inst in instruments:
        if inst.get("ticker", "").upper() == t212_ticker.upper():
            # Prefer shortName (current market symbol) over ticker root.
            # shortName is the live, correct symbol; the ticker root can be stale
            # for renamed companies (e.g. FB→META, RBS→NWG, BLT→BHP).
            short_name = (inst.get("shortName") or "").upper().strip()
            root       = short_name if short_name else _instrument_root(inst)
            currency   = _instrument_currency(inst)
            for suffix, currencies in YF_SUFFIX_TO_CURRENCIES.items():
                if currency in currencies:
                    return f"{root}.{suffix}" if suffix else root
            return root  # Unknown currency — return symbol without suffix
    return None


# =============================================================================
# Account state queries
# =============================================================================

def get_pending_buy_tickers(instruments: list[dict], t212_to_yf_fn) -> set[str]:
    """
    Return yfinance tickers that have an open (unfilled) buy order in T212.

    Used by sync_from_t212 to avoid removing positions whose T212 buy order was
    placed outside market hours. The order is queued, not failed — the shadow
    position is correct and should be kept.

    Terminal statuses (FILLED, CANCELLED, CANCELLING, REJECTED, REPLACED) are
    excluded — only truly open orders count as "pending".

    On any API error, returns an empty set so the caller degrades gracefully
    (the sync will be conservative rather than crashing the run).

    Args:
        instruments:    T212 instruments list for reverse ticker translation.
        t212_to_yf_fn:  Callable(t212_ticker) -> yfinance_ticker.

    Returns:
        set[str]: yfinance tickers with pending buy orders. Empty set on error.
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
        side   = (order.get("side") or "").upper()
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
    """
    Fetch all open T212 positions and return them keyed by T212 ticker.

    Used during order execution to look up current quantities for SELL/TRIM
    orders — we need to know how many shares are actually held before placing
    a sell order.

    Returns:
        dict: {t212_ticker: position_data_dict} for all open positions.

    Raises:
        requests.HTTPError: If the /equity/positions endpoint returns non-2xx.
    """
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
        ticker = (
            p["instrument"]["ticker"] if "instrument" in p
            else p.get("ticker", "")
        )
        if ticker:
            result[ticker] = p
    return result


# =============================================================================
# Order placement
# =============================================================================

def _place_market_order(t212_ticker: str, quantity: float) -> dict:
    """
    Place a market order on T212.

    Positive quantity = BUY, negative quantity = SELL. The extendedHours flag
    allows orders to queue for the next market open when placed outside trading
    hours — without it, out-of-hours orders would be rejected.

    Args:
        t212_ticker: T212 instrument ticker string (e.g. "AAPL_US_EQ").
        quantity:    Number of shares. Positive for buy, negative for sell.

    Returns:
        dict: T212 order response JSON (includes "id" and "status" fields).

    Raises:
        requests.HTTPError: If T212 rejects the order (e.g. 404 bad ticker,
                            400 insufficient funds, 429 rate limit).
    """
    qty = round(abs(quantity), 4)
    if quantity < 0:
        qty = -qty
    payload = {
        "ticker":        t212_ticker,
        "quantity":      qty,
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


# =============================================================================
# Input validation
# =============================================================================

def _is_valid_ticker(yf_ticker: str) -> bool:
    """
    Reject obviously malformed ticker strings before attempting translation.

    Guards against Claude occasionally outputting garbled tickers like "BA..L"
    (double dots), empty strings, or excessively long strings that would never
    match anything in T212's catalogue.

    Args:
        yf_ticker: Ticker string to validate (from Claude's JSON output).

    Returns:
        bool: True if the ticker looks plausibly valid, False to skip it.
    """
    if not yf_ticker or len(yf_ticker) > 15:
        return False
    t = yf_ticker.upper().strip()
    if ".." in t:
        return False
    for c in t:
        if not (c.isalnum() or c in ".-"):
            return False
    return True


# =============================================================================
# Order execution helpers
# =============================================================================

def _get_available_cash() -> float:
    """
    Fetch the current available-to-trade cash balance from T212.

    Called at the start of execute_recommendations to seed the expected-cash
    budget tracker. Returns 0.0 on any API error so callers degrade gracefully.

    Returns:
        float: Available cash in GBP, or 0.0 on error.
    """
    try:
        r = requests.get(
            f"{T212_BASE_URL}/equity/account/summary",
            headers=_headers(), timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        return float(
            data.get("cash", {}).get("availableToTrade")
            or data.get("free")
            or 0
        )
    except Exception as e:
        print(f"  ! Couldn't fetch available cash for budget tracking: {e}")
        return 0.0


def _estimate_sell_proceeds_gbp(rec: dict, t212_ticker: str,
                                 positions_map: dict) -> float:
    """
    Estimate the GBP proceeds from a SELL or TRIM order before it is placed.

    Used to update the expected-cash budget so subsequent BUY orders can be
    validated against post-sell funds, not just the current pre-sell balance.

    Uses walletImpact.currentValue (already in GBP) from the T212 position
    data — no FX conversion needed. Returns 0.0 if the position isn't found
    or data is incomplete, so budget tracking degrades conservatively.

    Args:
        rec:           Recommendation dict (needs "action" and optionally "trim_pct").
        t212_ticker:   T212 ticker string for the position to look up.
        positions_map: {t212_ticker: position_dict} from get_t212_positions_map().

    Returns:
        float: Estimated GBP proceeds, or 0.0 if unknown.
    """
    pos = positions_map.get(t212_ticker)
    if not pos:
        return 0.0
    held = float(pos.get("quantity") or 0)
    if held <= 0:
        return 0.0

    action = rec.get("action", "").upper().strip()
    qty_to_sell = (
        held if action == "SELL"
        else held * (float(rec.get("trim_pct") or 50) / 100)
    )

    # walletImpact.currentValue is the full position's current value in GBP
    wallet = pos.get("walletImpact", {}) or {}
    current_value_gbp = float(wallet.get("currentValue") or 0)
    if current_value_gbp > 0 and held > 0:
        return current_value_gbp * (qty_to_sell / held)

    return 0.0


def _search_translate(yf_ticker: str, instruments: list[dict]) -> Optional[str]:
    """
    Translate using the search logic only, bypassing the TICKER_ALIASES map.

    Used as a fallback when an alias ticker returns 404 from T212 — maybe the
    alias is stale. We try the instrument search directly instead.

    Args:
        yf_ticker:   yfinance ticker string.
        instruments: T212 instruments list.

    Returns:
        str: T212 ticker, or None if no match found.
    """
    upper = yf_ticker.upper().strip()
    base_symbol, yf_suffix = _parse_yf_ticker(upper)
    expected_currencies = YF_SUFFIX_TO_CURRENCIES.get(yf_suffix, set())
    matches = [inst for inst in instruments if _instrument_root(inst) == base_symbol]
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


def _execute_buy(rec: dict, yf_ticker: str, t212_ticker: str,
                 instruments: list, events: list, confirmed_recs: list) -> None:
    """
    Execute a single BUY recommendation as a T212 market order.

    Calculates the share quantity from the GBP amount and current price, places
    the order, and on success appends to both events and confirmed_recs.
    Includes a 404 retry: if the T212 ticker came from an alias and returned 404,
    falls back to instrument search to find the correct ticker.

    A 1.2s sleep between orders prevents T212 rate-limiting on bulk runs.

    Args:
        rec:           Recommendation dict from Claude.
        yf_ticker:     yfinance ticker (for price lookup and error messages).
        t212_ticker:   T212 ticker (from yf_to_t212_ticker).
        instruments:   T212 instruments list (for alias-404 fallback search).
        events:        List to append human-readable result strings to.
        confirmed_recs: List to append successful recs to (for shadow mirroring).
    """
    amount_gbp = float(rec.get("amount_gbp") or 0)
    if amount_gbp <= 0:
        events.append(f"SKIP BUY {yf_ticker}: no amount specified")
        return

    price_gbp = fetch_price_gbp(yf_ticker)
    if not price_gbp or price_gbp <= 0:
        events.append(f"SKIP BUY {yf_ticker}: couldn't fetch price")
        return

    shares = amount_gbp / price_gbp
    effective_t212_ticker = t212_ticker

    try:
        result = _place_market_order(t212_ticker, shares)
    except requests.HTTPError as e:
        # If the alias returned 404, try searching the instrument list directly
        if e.response.status_code == 404 and yf_ticker.upper() in TICKER_ALIASES:
            print(f"  ! Alias {t212_ticker} returned 404, retrying with instrument search...")
            fallback = _search_translate(yf_ticker, instruments)
            if fallback:
                effective_t212_ticker = fallback
                result = _place_market_order(fallback, shares)
            else:
                events.append(f"SKIP BUY {yf_ticker}: alias 404 and search found nothing")
                return
        else:
            raise  # Re-raise for the outer try/except in execute_recommendations

    events.append(
        f"T212 BUY {shares:.4f} {effective_t212_ticker} "
        f"(order {result.get('id', '?')})"
    )
    confirmed_recs.append(rec)
    time.sleep(1.2)  # Rate-limit guard between orders


def _execute_sell_or_trim(rec: dict, yf_ticker: str, t212_ticker: str,
                           action: str, positions_map: dict,
                           events: list, confirmed_recs: list) -> None:
    """
    Execute a single SELL or TRIM recommendation as a T212 market order.

    Looks up the current quantity held in T212, computes the shares to sell
    (100% for SELL, trim_pct% for TRIM), places a negative-quantity market order,
    and on success appends to events and confirmed_recs.

    Args:
        rec:           Recommendation dict from Claude.
        yf_ticker:     yfinance ticker (for error messages).
        t212_ticker:   T212 ticker (for order placement + positions_map lookup).
        action:        "SELL" or "TRIM".
        positions_map: {t212_ticker: position_dict} from get_t212_positions_map().
        events:        List to append human-readable result strings to.
        confirmed_recs: List to append successful recs to (for shadow mirroring).
    """
    pos = positions_map.get(t212_ticker)
    if not pos:
        events.append(
            f"SKIP {action} {yf_ticker}: "
            f"no T212 position found for {t212_ticker}"
        )
        return

    held = float(pos.get("quantity", 0))
    if held <= 0:
        events.append(f"SKIP {action} {yf_ticker}: zero quantity held")
        return

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
    time.sleep(1.2)  # Rate-limit guard between orders


# =============================================================================
# Main entry point
# =============================================================================

def execute_recommendations(recs: list) -> tuple[list[str], list[dict]]:
    """
    Mirror Claude's recommendations as real T212 demo market orders.

    Returns (events, confirmed_recs). confirmed_recs contains ONLY the
    recommendations T212 successfully accepted — shadow must apply only these,
    not the full rec list. This prevents ledger drift when T212 rejects an order
    (e.g. insufficient funds, bad ticker, market closed).

    When T212_DEMO_EXECUTE=false: returns ([], all_recs) so shadow applies
    everything regardless, running in paper-only mode.

    Safety guard: refuses to execute if T212_ENV=live and T212_DEMO_EXECUTE=true.

    Execution order:
      All SELL/TRIM orders are placed before any BUY orders, regardless of the
      order Claude listed them. This is critical for out-of-hours runs where T212
      queues all orders for the next market open: sells must be queued first so
      their proceeds are available before buys execute at market open.

      A running "expected_cash" budget is maintained:
        - Starts at the current T212 available balance.
        - Each confirmed SELL/TRIM adds estimated GBP proceeds.
        - Each BUY pre-validates against expected_cash before being attempted,
          and deducts on success.
      This prevents pointless 400 "insufficient funds" errors on buys that would
      only fail because sell proceeds haven't settled yet at placement time.

    Args:
        recs: List of recommendation dicts from extract_recommendations().

    Returns:
        tuple: (events list of str, confirmed_recs list of dict)
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
    instruments   = _load_instruments()
    positions_map = get_t212_positions_map()

    # Sort: all SELL/TRIM before BUY, preserving relative order within each group.
    # This guarantees sell orders are queued first so they execute at market open
    # before any buys, making their proceeds available for the buy orders.
    ordered_recs = sorted(
        recs,
        key=lambda r: 0 if r.get("action", "").upper().strip() in ("SELL", "TRIM") else 1
    )

    # Seed the expected-cash budget from the current T212 available balance.
    # This is updated as each sell is confirmed (+proceeds) and each buy succeeds (-amount).
    expected_cash = _get_available_cash()
    print(f"  T212 available cash for order budget: £{expected_cash:.2f}")

    for rec in ordered_recs:
        action    = rec.get("action", "").upper().strip()
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
            if action in ("SELL", "TRIM"):
                # Estimate proceeds before placing so budget is updated if the order succeeds
                proceeds_estimate = _estimate_sell_proceeds_gbp(rec, t212_ticker, positions_map)
                prev_confirmed = len(confirmed_recs)
                _execute_sell_or_trim(rec, yf_ticker, t212_ticker, action,
                                      positions_map, events, confirmed_recs)
                if len(confirmed_recs) > prev_confirmed:
                    expected_cash += proceeds_estimate
                    print(f"  Budget after {action} {yf_ticker}: "
                          f"£{expected_cash:.2f} (+£{proceeds_estimate:.2f} estimated)")

            else:  # BUY
                amount_gbp = float(rec.get("amount_gbp") or 0)
                # Pre-validate against expected post-sell cash before hitting the T212 API.
                # Avoids a guaranteed 400 when the buy clearly exceeds available funds.
                if amount_gbp > 0 and expected_cash < amount_gbp - 0.01:
                    events.append(
                        f"SKIP BUY {yf_ticker}: insufficient funds after sells "
                        f"(need £{amount_gbp:.2f}, expected £{expected_cash:.2f})"
                    )
                    continue
                prev_confirmed = len(confirmed_recs)
                _execute_buy(rec, yf_ticker, t212_ticker, instruments,
                             events, confirmed_recs)
                if len(confirmed_recs) > prev_confirmed:
                    expected_cash -= amount_gbp
                    print(f"  Budget after BUY {yf_ticker}: "
                          f"£{expected_cash:.2f} (-£{amount_gbp:.2f})")

        except requests.HTTPError as e:
            events.append(
                f"T212 ORDER FAILED {action} {t212_ticker}: "
                f"{e.response.status_code} {e.response.text[:200]}"
            )
        except Exception as e:
            events.append(f"T212 ORDER ERROR {action} {t212_ticker}: {e}")

    return events, confirmed_recs
