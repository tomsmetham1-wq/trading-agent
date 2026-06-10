# -*- coding: utf-8 -*-
"""
Regression tests for the trading agent. No network access required — anything
that would hit yfinance or T212 is stubbed.

Run with:  venv\\Scripts\\python.exe -m pytest test_trading_agent.py -q
"""

import json
from datetime import datetime, timedelta

import pytest

import shadow_portfolio as sp
import t212_executor as t212ex
import trading_agent as ta
import prompts


# =============================================================================
# Fixtures
# =============================================================================

INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "currencyCode": "USD"},
    {"ticker": "SHELl_EQ",   "shortName": "SHEL", "currencyCode": "GBX"},
    {"ticker": "SHEL_US_EQ", "shortName": "SHEL", "currencyCode": "USD"},
    {"ticker": "FB_US_EQ",   "shortName": "META", "currencyCode": "USD"},
    {"ticker": "BRK_B_US_EQ", "shortName": "BRK.B", "currencyCode": "USD"},
    {"ticker": "SAPd_EQ",    "shortName": "SAP",  "currencyCode": "EUR"},
    {"ticker": "VUSAl_EQ",   "shortName": "VUSA", "currencyCode": "GBX"},
]


def make_ledger(**overrides):
    ledger = sp._default_ledger()
    ledger["cash_gbp"] = 1000.0
    ledger.update(overrides)
    return ledger


def gbp_buy_rec(ticker, amount, **extra):
    """BUY rec with an injected GBP fill price so no network call is needed."""
    rec = {
        "action": "BUY", "yfinance_ticker": ticker, "amount_gbp": amount,
        "thesis_oneline": "test thesis",
        "_fill_price_native": 10.0, "_fill_price_currency": "GBP",
    }
    rec.update(extra)
    return rec


# =============================================================================
# Ticker parsing and translation
# =============================================================================

class TestTickerParsing:
    def test_us_ticker(self):
        assert t212ex._parse_yf_ticker("AAPL") == ("AAPL", "")

    def test_lse_suffix(self):
        assert t212ex._parse_yf_ticker("SHEL.L") == ("SHEL", "L")

    def test_symbol_dot_not_exchange(self):
        # .B is not an exchange code — the dot belongs to the symbol
        assert t212ex._parse_yf_ticker("BRK.B") == ("BRK.B", "")

    def test_instrument_root_strips_lowercase_suffix(self):
        assert t212ex._instrument_root({"ticker": "SHELl_EQ"}) == "SHEL"
        assert t212ex._instrument_root({"ticker": "SAPd_EQ"}) == "SAP"
        assert t212ex._instrument_root({"ticker": "AAPL_US_EQ"}) == "AAPL"


class TestTranslation:
    def test_us_stock(self):
        assert t212ex.yf_to_t212_ticker("AAPL", INSTRUMENTS) == "AAPL_US_EQ"

    def test_lse_listing_disambiguated_by_currency(self):
        assert t212ex.yf_to_t212_ticker("SHEL.L", INSTRUMENTS) == "SHELl_EQ"

    def test_us_listing_of_dual_listed(self):
        assert t212ex.yf_to_t212_ticker("SHEL", INSTRUMENTS) == "SHEL_US_EQ"

    def test_meta_alias(self):
        assert t212ex.yf_to_t212_ticker("META", INSTRUMENTS) == "FB_US_EQ"

    def test_brk_hyphen_alias(self):
        assert t212ex.yf_to_t212_ticker("BRK-B", INSTRUMENTS) == "BRK_B_US_EQ"

    def test_reverse_uses_short_name_for_renamed(self):
        assert t212ex.t212_to_yf_ticker("FB_US_EQ", INSTRUMENTS) == "META"

    def test_reverse_lse_gets_l_suffix(self):
        assert t212ex.t212_to_yf_ticker("SHELl_EQ", INSTRUMENTS) == "SHEL.L"

    def test_unknown_returns_none(self):
        assert t212ex.yf_to_t212_ticker("ZZZZZ", INSTRUMENTS) is None


class TestTickerValidation:
    def test_valid(self):
        assert t212ex._is_valid_ticker("AAPL")
        assert t212ex._is_valid_ticker("SHEL.L")
        assert t212ex._is_valid_ticker("BRK-B")

    def test_invalid(self):
        assert not t212ex._is_valid_ticker("")
        assert not t212ex._is_valid_ticker("BA..L")       # double dot
        assert not t212ex._is_valid_ticker("A" * 16)      # too long
        assert not t212ex._is_valid_ticker("AB C")        # space


# =============================================================================
# Currency conversion — the GBp/GBX pence ordering bug
# =============================================================================

class TestNativeToGbp:
    def setup_method(self):
        sp._fx_cache["GBPUSD=X"] = 1.25

    def test_pence_variants_divide_by_100(self):
        assert sp._native_to_gbp(250.0, "GBX") == 2.5
        assert sp._native_to_gbp(250.0, "GBp") == 2.5
        assert sp._native_to_gbp(250.0, "GBP.") == 2.5

    def test_pounds_unchanged(self):
        assert sp._native_to_gbp(2.5, "GBP") == 2.5

    def test_usd_converted(self):
        assert abs(sp._native_to_gbp(125.0, "USD") - 100.0) < 1e-9


# =============================================================================
# Strategy guards
# =============================================================================

class TestStrategyGuards:
    def _pre_val(self):
        return {
            "total_value_gbp": 6000.0,
            "positions": {
                "DELL": {"current_value_gbp": 1100.0},  # 18.3% — £100 headroom
                "AVGO": {"current_value_gbp": 1300.0},  # 21.7% — over cap
            },
        }

    def _ledger_with_exit(self, action, days_ago, closed=False):
        date = (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        trade = {"date": date, "action": action, "ticker": "NVDA"}
        if closed:
            trade["closed_position"] = True
        return {"trades": [trade], "positions": {}}

    def test_recent_sell_blocks_buy(self):
        ledger = self._ledger_with_exit("SELL", days_ago=3)
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NVDA", "amount_gbp": 500}],
            ledger, self._pre_val())
        assert out == []
        assert any("flip-flop" in e for e in events)

    def test_old_sell_allows_buy(self):
        ledger = self._ledger_with_exit("SELL", days_ago=30)
        out, _ = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NVDA", "amount_gbp": 500}],
            ledger, self._pre_val())
        assert len(out) == 1

    def test_trim_to_zero_blocks_buy(self):
        ledger = self._ledger_with_exit("TRIM", days_ago=3, closed=True)
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NVDA", "amount_gbp": 500}],
            ledger, self._pre_val())
        assert out == []
        assert any("flip-flop" in e for e in events)

    def test_partial_trim_does_not_block_buy(self):
        ledger = self._ledger_with_exit("TRIM", days_ago=3, closed=False)
        out, _ = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NVDA", "amount_gbp": 500}],
            ledger, self._pre_val())
        assert len(out) == 1

    def test_buy_reduced_to_position_cap(self):
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "DELL", "amount_gbp": 500}],
            {"trades": [], "positions": {}}, self._pre_val())
        assert out[0]["amount_gbp"] == 100.0
        assert any("REDUCED" in e for e in events)

    def test_buy_blocked_when_over_cap(self):
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "AVGO", "amount_gbp": 500}],
            {"trades": [], "positions": {}}, self._pre_val())
        assert out == []
        assert any("BLOCKED" in e for e in events)

    def test_sells_and_trims_pass_through(self):
        recs = [{"action": "TRIM", "yfinance_ticker": "DELL", "trim_pct": 30}]
        out, events = ta.enforce_strategy_guards(
            recs, {"trades": [], "positions": {}}, self._pre_val())
        assert out == recs
        assert events == []


# =============================================================================
# Shadow ledger — buys, sells, trims
# =============================================================================

class TestApplyRecommendations:
    def test_buy_opens_position_with_theme_and_trims(self):
        ledger = make_ledger()
        rec = gbp_buy_rec("TEST", 200, theme="energy",
                          pre_commit_trims="Trim 1/3 at +40%")
        events = sp.apply_recommendations(ledger, [rec], "2026-06-10")
        assert "BOUGHT" in events[0]
        pos = ledger["positions"]["TEST"]
        assert pos["shares"] == pytest.approx(20.0)
        assert pos["theme"] == "energy"
        assert pos["pre_commit_trims"] == "Trim 1/3 at +40%"
        assert ledger["cash_gbp"] == pytest.approx(800.0)
        assert ledger["trades"][-1]["theme"] == "energy"

    def test_buy_adds_to_position_recomputes_avg_cost(self):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-06-10")
        rec2 = gbp_buy_rec("TEST", 200)
        rec2["_fill_price_native"] = 20.0   # second buy at double the price
        sp.apply_recommendations(ledger, [rec2], "2026-06-11")
        pos = ledger["positions"]["TEST"]
        assert pos["shares"] == pytest.approx(30.0)                 # 20 + 10
        assert pos["avg_cost_gbp"] == pytest.approx(400.0 / 30.0)

    def test_buy_insufficient_cash_skipped(self):
        ledger = make_ledger(cash_gbp=50.0)
        events = sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-06-10")
        assert "SKIP" in events[0]
        assert "TEST" not in ledger["positions"]

    def test_sell_closes_position_and_flags_it(self, monkeypatch):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-06-10")
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 12.0)
        events = sp.apply_recommendations(
            ledger, [{"action": "SELL", "yfinance_ticker": "TEST"}], "2026-06-12")
        assert "SELL" in events[0]
        assert "TEST" not in ledger["positions"]
        assert ledger["trades"][-1]["closed_position"] is True
        assert ledger["cash_gbp"] == pytest.approx(800.0 + 20 * 12.0)

    def test_partial_trim_keeps_position_no_flag(self, monkeypatch):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-06-10")
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 12.0)
        sp.apply_recommendations(
            ledger, [{"action": "TRIM", "yfinance_ticker": "TEST", "trim_pct": 50}],
            "2026-06-12")
        assert ledger["positions"]["TEST"]["shares"] == pytest.approx(10.0)
        assert "closed_position" not in ledger["trades"][-1]

    def test_trim_to_100pct_flags_closed(self, monkeypatch):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-06-10")
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 12.0)
        sp.apply_recommendations(
            ledger, [{"action": "TRIM", "yfinance_ticker": "TEST", "trim_pct": 100}],
            "2026-06-12")
        assert "TEST" not in ledger["positions"]
        assert ledger["trades"][-1]["closed_position"] is True

    def test_sell_unheld_position_skipped(self, monkeypatch):
        ledger = make_ledger()
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 12.0)
        events = sp.apply_recommendations(
            ledger, [{"action": "SELL", "yfinance_ticker": "GHOST"}], "2026-06-10")
        assert "SKIP" in events[0]


# =============================================================================
# Realised P&L replay
# =============================================================================

class TestRealizedPnl:
    def test_buy_trim_sell_sequence(self):
        ledger = {"trades": [
            {"action": "BUY",  "ticker": "X", "shares": 10, "amount_gbp": 100},
            {"action": "TRIM", "ticker": "X", "shares": 5,  "amount_gbp": 75},
            {"action": "SELL", "ticker": "X", "shares": 5,  "amount_gbp": 60},
        ]}
        result = sp.compute_realized_pnl(ledger)
        # TRIM: 75 - 5*10 = +25 ; SELL: 60 - 5*10 = +10
        assert result["by_ticker"]["X"] == pytest.approx(35.0)
        assert result["total_gbp"] == pytest.approx(35.0)
        assert result["tickers_with_incomplete_basis"] == []

    def test_sell_without_buy_marked_incomplete(self):
        ledger = {"trades": [
            {"action": "SELL", "ticker": "Y", "shares": 5, "amount_gbp": 60},
        ]}
        result = sp.compute_realized_pnl(ledger)
        assert "Y" in result["tickers_with_incomplete_basis"]
        assert result["total_gbp"] == 0.0

    def test_sync_entries_ignored(self):
        ledger = {"trades": [
            {"action": "SYNC_FROM_T212", "ticker": "-", "note": "x"},
            {"action": "BUY", "ticker": "X", "shares": 10, "amount_gbp": 100},
        ]}
        result = sp.compute_realized_pnl(ledger)
        assert result["total_gbp"] == 0.0
        assert result["tickers_with_incomplete_basis"] == []


# =============================================================================
# Bidirectional sync
# =============================================================================

def _t212_to_yf(t212_ticker):
    return t212ex.t212_to_yf_ticker(t212_ticker, INSTRUMENTS)


def t212_pos(ticker, qty, total_cost_gbp):
    return {
        "ticker": ticker, "quantity": qty,
        "averagePricePaid": 0,
        "walletImpact": {"totalCost": total_cost_gbp},
    }


class TestSync:
    def test_adds_missing_position_with_t212_cost_basis(self):
        ledger = make_ledger(cash_gbp=500.0)
        changed = sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 2, 300.0)],
            _t212_to_yf, bidirectional=True)
        assert changed
        assert ledger["positions"]["AAPL"]["shares"] == 2
        assert ledger["positions"]["AAPL"]["avg_cost_gbp"] == pytest.approx(150.0)

    def test_removes_extra_position(self):
        ledger = make_ledger(cash_gbp=500.0)
        ledger["positions"] = {
            "AAPL": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
            "META": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
        }
        sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 1, 100.0)],
            _t212_to_yf, bidirectional=True)
        assert "META" not in ledger["positions"]
        assert "AAPL" in ledger["positions"]

    def test_pending_buy_not_removed(self):
        ledger = make_ledger(cash_gbp=500.0)
        ledger["positions"] = {
            "AAPL": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
            "META": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
        }
        sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 1, 100.0)],
            _t212_to_yf, bidirectional=True, pending_yf_tickers={"META"})
        assert "META" in ledger["positions"]

    def test_wipe_guard_refuses_full_removal(self):
        ledger = make_ledger(cash_gbp=1000.0)
        ledger["positions"] = {
            "AAPL": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
            "META": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
        }
        changed = sp.sync_from_t212(
            ledger, {"free": 1000.0}, [],   # T212 says: no positions at all
            _t212_to_yf, bidirectional=True)
        assert len(ledger["positions"]) == 2   # nothing wiped
        assert not changed

    def test_shadow_only_mode_never_removes(self):
        ledger = make_ledger(cash_gbp=500.0)
        ledger["positions"] = {
            "META": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
        }
        sp.sync_from_t212(
            ledger, {"free": 999.0}, [], _t212_to_yf, bidirectional=False)
        assert "META" in ledger["positions"]
        assert ledger["cash_gbp"] == 500.0   # cash untouched in shadow-only mode

    def test_cash_synced_to_t212(self):
        ledger = make_ledger(cash_gbp=500.0)
        ledger["positions"] = {
            "AAPL": {"shares": 1, "avg_cost_gbp": 100, "first_bought": "x", "thesis": ""},
        }
        changed = sp.sync_from_t212(
            ledger, {"free": 750.0}, [t212_pos("AAPL_US_EQ", 1, 100.0)],
            _t212_to_yf, bidirectional=True)
        assert changed
        assert ledger["cash_gbp"] == 750.0


# =============================================================================
# Recommendation extraction
# =============================================================================

class TestExtractRecommendations:
    def test_nested_json_survives(self):
        text = """prose
```json
{"recommendations": [
  {"action": "TRIM", "ticker": "X", "trim_pct": 50,
   "thesis_break_checklist": {"datum_changed": "a", "knowable_at_entry": "no", "would_rebuy": "no"}}
]}
```"""
        recs = ta.extract_recommendations(text)
        assert len(recs) == 1
        assert recs[0]["thesis_break_checklist"]["would_rebuy"] == "no"

    def test_empty_recommendations(self):
        assert ta.extract_recommendations('```json\n{"recommendations": []}\n```') == []

    def test_no_json_block(self):
        assert ta.extract_recommendations("no block here") == []

    def test_malformed_json(self):
        assert ta.extract_recommendations('```json\n{"recommendations": [}\n```') == []

    def test_strip_json_block(self):
        text = 'before\n```json\n{"recommendations": []}\n```\nafter'
        assert ta.strip_json_block(text) == "before\n\nafter"


# =============================================================================
# Valuation and snapshots
# =============================================================================

class TestValuation:
    def test_zero_benchmark_return_is_not_none(self, monkeypatch):
        ledger = make_ledger(cash_gbp=100.0)
        ledger["benchmark_start_price_gbp"] = 100.0
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 100.0)
        val = sp.valuation(ledger)
        assert val["benchmark_return_pct"] == 0.0      # was None via falsy check
        assert val["vs_benchmark_pct"] is not None

    def test_pricing_incomplete_flag(self, monkeypatch):
        ledger = make_ledger(cash_gbp=100.0)
        ledger["benchmark_start_price_gbp"] = 100.0
        ledger["positions"] = {
            "X": {"shares": 1, "avg_cost_gbp": 10, "first_bought": "d", "thesis": ""},
        }
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 100.0 if t == "VUSA.L" else None)
        val = sp.valuation(ledger)
        assert val["pricing_incomplete"] is True
        snap_ledger = {"weekly_snapshots": []}
        sp.snapshot(snap_ledger, val, "2026-06-10")
        assert snap_ledger["weekly_snapshots"][0]["pricing_incomplete"] is True


# =============================================================================
# Run journal — crash window between T212 execution and ledger save
# =============================================================================

class TestRunJournal:
    @pytest.fixture(autouse=True)
    def _tmp_journal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ta, "RUN_JOURNAL_PATH", tmp_path / "run_journal.json")

    def test_no_journal_does_not_block(self):
        assert ta.journal_blocks_run("2026-06-10") is False

    def test_same_day_executing_journal_blocks(self):
        ta.write_run_journal("2026-06-10", [{"action": "BUY", "ticker": "X"}])
        assert ta.journal_blocks_run("2026-06-10") is True

    def test_stale_journal_from_previous_day_cleared(self):
        ta.write_run_journal("2026-06-03", [])
        assert ta.journal_blocks_run("2026-06-10") is False
        assert not ta.RUN_JOURNAL_PATH.exists()

    def test_corrupt_journal_blocks(self):
        ta.RUN_JOURNAL_PATH.write_text("{not json", encoding="utf-8")
        assert ta.journal_blocks_run("2026-06-10") is True

    def test_clear_removes_file(self):
        ta.write_run_journal("2026-06-10", [])
        ta.clear_run_journal()
        assert not ta.RUN_JOURNAL_PATH.exists()
        ta.clear_run_journal()   # idempotent on missing file


# =============================================================================
# Prompt builders
# =============================================================================

class TestPrompts:
    def _fake_val(self, ledger):
        return {
            "total_value_gbp": 6000.0, "cash_gbp": 550.0,
            "positions_value_gbp": 5450.0, "starting_capital_gbp": 5000.0,
            "total_return_gbp": 1000.0, "total_return_pct": 20.0,
            "benchmark_ticker": "VUSA.L", "benchmark_value_gbp": 5300.0,
            "benchmark_return_pct": 6.0, "vs_benchmark_pct": 14.0,
            "pricing_incomplete": False,
            "positions": {
                t: {"current_value_gbp": 680.0, "pnl_gbp": 50.0, "pnl_pct": 8.0,
                    "shares": 1.0, "avg_cost_gbp": 1.0, "current_price_gbp": 1.0,
                    "first_bought": "x", "price_source": "T212"}
                for t in ledger["positions"]
            },
        }

    def _themed_ledger(self):
        ledger = make_ledger()
        ledger["positions"] = {
            "AVGO": {"shares": 1, "avg_cost_gbp": 1, "first_bought": "x",
                     "thesis": "", "theme": "AI infrastructure"},
            "NVDA": {"shares": 1, "avg_cost_gbp": 1, "first_bought": "x",
                     "thesis": "", "theme": "AI infrastructure"},
            "ABBV": {"shares": 1, "avg_cost_gbp": 1, "first_bought": "x",
                     "thesis": "", "theme": "pharma"},
        }
        return ledger

    def test_weekly_prompt_includes_theme_exposure(self):
        ledger = self._themed_ledger()
        _, user = prompts.build_prompt(
            self._fake_val(ledger), ledger, {"free": 550.0, "total": 6000.0}, [])
        assert "Theme exposure" in user
        assert "AI infrastructure" in user

    def test_theme_over_cap_flagged(self):
        ledger = self._themed_ledger()
        # 2 of 3 equal positions = 22.7% of £6000 total... use bigger values
        val = self._fake_val(ledger)
        val["positions"]["AVGO"]["current_value_gbp"] = 2500.0
        val["positions"]["NVDA"]["current_value_gbp"] = 1500.0
        _, user = prompts.build_prompt(val, ledger, {"free": 550.0}, [])
        assert "OVER 60% CAP" in user

    def test_deep_review_includes_realized_pnl(self):
        ledger = self._themed_ledger()
        ledger["trades"] = [
            {"action": "BUY",  "ticker": "X", "shares": 10, "amount_gbp": 100},
            {"action": "SELL", "ticker": "X", "shares": 10, "amount_gbp": 150},
        ]
        _, user = prompts.build_deep_review_prompt(ledger, self._fake_val(ledger))
        assert "realized_total_gbp" in user
        assert "50.0" in user

    def test_deep_review_ledger_not_duplicating_snapshots(self):
        ledger = self._themed_ledger()
        ledger["weekly_snapshots"] = [{"date": "2026-06-08", "total_value_gbp": 6000}]
        _, user = prompts.build_deep_review_prompt(ledger, self._fake_val(ledger))
        ledger_section = user.split("=== Weekly snapshots")[0]
        assert "weekly_snapshots" not in ledger_section
