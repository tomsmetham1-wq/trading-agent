# -*- coding: utf-8 -*-
"""
Regression tests for the trading agent. No network access required — anything
that would hit yfinance or T212 is stubbed.

Run with:  venv\\Scripts\\python.exe -m pytest test_trading_agent.py -q
"""

import json
from datetime import date, datetime, timedelta

import anthropic
import httpx
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

    def test_reverse_us_dot_class_uses_dash(self):
        # yfinance prices US share classes with a dash (BRK-B), not the dot
        # T212's shortName uses — the dot form is unpriceable on yfinance
        assert t212ex.t212_to_yf_ticker("BRK_B_US_EQ", INSTRUMENTS) == "BRK-B"

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

    def test_same_run_sell_then_rebuy_blocked(self):
        recs = [
            {"action": "SELL", "yfinance_ticker": "DELL"},
            {"action": "BUY", "yfinance_ticker": "DELL", "amount_gbp": 300},
        ]
        out, events = ta.enforce_strategy_guards(
            recs, {"trades": [], "positions": {}}, self._pre_val())
        assert [r["action"] for r in out] == ["SELL"]
        assert any("flip-flop" in e and "same run" in e for e in events)

    def test_same_run_partial_trim_allows_buy(self):
        recs = [
            {"action": "TRIM", "yfinance_ticker": "DELL", "trim_pct": 30},
            {"action": "BUY", "yfinance_ticker": "DELL", "amount_gbp": 50},
        ]
        out, _ = ta.enforce_strategy_guards(
            recs, {"trades": [], "positions": {}}, self._pre_val())
        assert [r["action"] for r in out] == ["TRIM", "BUY"]

    def test_multiple_buys_same_ticker_capped_cumulatively(self):
        # DELL at £1,100 of £6,000 → £100 headroom under the 20% cap.
        # First £80 buy fits; second £80 buy must be blocked (only £20 left,
        # below the £25 minimum order).
        recs = [
            {"action": "BUY", "yfinance_ticker": "DELL", "amount_gbp": 80},
            {"action": "BUY", "yfinance_ticker": "DELL", "amount_gbp": 80},
        ]
        out, events = ta.enforce_strategy_guards(
            recs, {"trades": [], "positions": {}}, self._pre_val())
        assert len(out) == 1
        assert any("BLOCKED" in e for e in events)


class TestThemeCapGuard:
    def _ledger(self):
        return {"trades": [], "positions": {
            "AVGO": {"theme": "AI infrastructure"},
            "NVDA": {"theme": "AI infrastructure"},
            "ABBV": {"theme": "pharma"},
        }}

    def _pre_val(self, avgo=350.0, nvda=300.0):
        return {
            "total_value_gbp": 1000.0,
            "cash_gbp": 400.0,
            "positions": {
                "AVGO": {"current_value_gbp": avgo},
                "NVDA": {"current_value_gbp": nvda},
                "ABBV": {"current_value_gbp": 100.0},
            },
        }

    def test_buy_blocked_when_theme_at_cap(self):
        # AI theme at 65% — any further AI buy is blocked
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "AMD", "amount_gbp": 100,
              "theme": "AI infrastructure"}],
            self._ledger(), self._pre_val())
        assert out == []
        assert any("theme" in e and "BLOCKED" in e for e in events)

    def test_buy_reduced_to_theme_headroom(self):
        # AI theme at 50% (500/1000) — £100 headroom under the 60% cap
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "AMD", "amount_gbp": 200,
              "theme": "AI infrastructure"}],
            self._ledger(), self._pre_val(avgo=250.0, nvda=250.0))
        assert out[0]["amount_gbp"] == pytest.approx(100.0)
        assert any("theme cap" in e for e in events)

    def test_same_run_sell_frees_theme_headroom(self):
        # AI theme at 65%, but this run sells AVGO (350) → 30% after — a
        # rebalance-within-theme buy must not be wrongly blocked
        out, events = ta.enforce_strategy_guards(
            [{"action": "SELL", "yfinance_ticker": "AVGO"},
             {"action": "BUY", "yfinance_ticker": "AMD", "amount_gbp": 200,
              "theme": "AI infrastructure"}],
            self._ledger(), self._pre_val())
        buys = [r for r in out if r["action"] == "BUY"]
        assert len(buys) == 1
        assert buys[0]["amount_gbp"] == 200

    def test_other_theme_unaffected(self):
        out, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "XOM", "amount_gbp": 150,
              "theme": "energy"}],
            self._ledger(), self._pre_val())
        assert out[0]["amount_gbp"] == 150


class TestPreCommitTrimAlerts:
    def _ledger(self, trades=None):
        return {
            "trades": trades or [],
            "positions": {
                "XOM": {
                    "theme": "energy", "first_bought": "2026-06-22",
                    "pre_commit_trims": "Trim 1/3 at +25%, trim another 1/3 at +50%.",
                },
            },
        }

    def _pre_val(self, pnl_pct):
        return {
            "total_value_gbp": 6000.0, "cash_gbp": 500.0,
            "positions": {"XOM": {"current_value_gbp": 900.0, "pnl_pct": pnl_pct}},
        }

    def test_alert_when_level_hit_and_ignored(self):
        _, events = ta.enforce_strategy_guards([], self._ledger(), self._pre_val(30.0))
        assert any("pre-committed trim" in e and "+25%" in e for e in events)

    def test_no_alert_below_level(self):
        _, events = ta.enforce_strategy_guards([], self._ledger(), self._pre_val(10.0))
        assert not any("pre-committed" in e for e in events)

    def test_no_alert_when_trim_recommended(self):
        recs = [{"action": "TRIM", "yfinance_ticker": "XOM", "trim_pct": 33}]
        _, events = ta.enforce_strategy_guards(recs, self._ledger(), self._pre_val(30.0))
        assert not any("pre-committed" in e for e in events)

    def test_no_alert_when_level_already_honoured(self):
        trades = [{"action": "TRIM", "ticker": "XOM", "date": "2026-06-29"}]
        _, events = ta.enforce_strategy_guards([], self._ledger(trades), self._pre_val(30.0))
        assert not any("pre-committed" in e for e in events)

    def test_second_level_alerts_after_first_honoured(self):
        trades = [{"action": "TRIM", "ticker": "XOM", "date": "2026-06-29"}]
        _, events = ta.enforce_strategy_guards([], self._ledger(trades), self._pre_val(55.0))
        assert any("pre-committed trim" in e and "+50%" in e for e in events)


class TestCashFloorAlert:
    def test_alert_when_buys_drain_cash(self):
        pre_val = {"total_value_gbp": 6000.0, "cash_gbp": 500.0, "positions": {}}
        _, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NEW", "amount_gbp": 480}],
            {"trades": [], "positions": {}}, pre_val)
        assert any("5% reserve floor" in e for e in events)

    def test_no_alert_when_sells_fund_buys(self):
        pre_val = {
            "total_value_gbp": 6000.0, "cash_gbp": 500.0,
            "positions": {"OLD": {"current_value_gbp": 700.0}},
        }
        _, events = ta.enforce_strategy_guards(
            [{"action": "SELL", "yfinance_ticker": "OLD"},
             {"action": "BUY", "yfinance_ticker": "NEW", "amount_gbp": 700}],
            {"trades": [], "positions": {"OLD": {}}}, pre_val)
        assert not any("reserve floor" in e for e in events)

    def test_no_alert_when_cash_unknown(self):
        pre_val = {"total_value_gbp": 6000.0, "positions": {}}
        _, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NEW", "amount_gbp": 480}],
            {"trades": [], "positions": {}}, pre_val)
        assert not any("reserve floor" in e for e in events)


class TestExistingThemeOverCapAlert:
    def _ledger(self):
        return {"trades": [], "positions": {
            "AVGO": {"theme": "AI infrastructure"},
            "NVDA": {"theme": "AI infrastructure"},
            "ABBV": {"theme": "pharma"},
        }}

    def _pre_val(self, avgo=400.0, nvda=350.0, abbv=250.0):
        return {
            "total_value_gbp": 1000.0, "cash_gbp": 0.0,
            "positions": {
                "AVGO": {"current_value_gbp": avgo},
                "NVDA": {"current_value_gbp": nvda},
                "ABBV": {"current_value_gbp": abbv},
            },
        }

    def test_alert_fires_with_no_recs_at_all(self):
        # AI infra at 75% of 1000 total — nothing recommended this run
        _, events = ta.enforce_strategy_guards([], self._ledger(), self._pre_val())
        assert any(
            "AI infrastructure" in e and "no rebalancing recommended" in e
            for e in events
        )

    def test_no_alert_when_theme_under_cap(self):
        # AI infra at 40% — well under the 60% cap
        _, events = ta.enforce_strategy_guards(
            [], self._ledger(), self._pre_val(avgo=250.0, nvda=150.0))
        assert not any("AI infrastructure" in e for e in events)

    def test_alert_wording_differs_when_partially_rebalanced(self):
        # A TRIM in the theme happened, but it's still over cap afterwards
        recs = [{"action": "TRIM", "yfinance_ticker": "AVGO", "trim_pct": 10}]
        _, events = ta.enforce_strategy_guards(recs, self._ledger(), self._pre_val())
        assert any(
            "AI infrastructure" in e and "after this run's rebalancing" in e
            for e in events
        )

    def test_no_alert_once_sell_brings_theme_under_cap(self):
        # Selling AVGO entirely drops AI infra from 75% to 35% — fixed
        recs = [{"action": "SELL", "yfinance_ticker": "AVGO"}]
        _, events = ta.enforce_strategy_guards(recs, self._ledger(), self._pre_val())
        assert not any("AI infrastructure" in e for e in events)

    def test_other_theme_unaffected(self):
        _, events = ta.enforce_strategy_guards([], self._ledger(), self._pre_val())
        assert not any("pharma" in e for e in events)


# =============================================================================
# SET_TRIMS — ledger-only backfill of pre-committed trim levels
# =============================================================================

class TestSetTrims:
    def _set_trims_rec(self, ticker="TEST",
                       trims="Trim 1/3 at +35%, trim another 1/3 at +70%."):
        return {"action": "SET_TRIMS", "yfinance_ticker": ticker,
                "pre_commit_trims": trims}

    def test_updates_existing_position_without_price_fetch(self, monkeypatch):
        monkeypatch.setattr(
            sp, "fetch_price_gbp",
            lambda *a, **k: pytest.fail("SET_TRIMS must not fetch a price"))
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-07-20")
        events = sp.apply_recommendations(ledger, [self._set_trims_rec()], "2026-07-20")
        assert events == ["SET_TRIMS TEST: Trim 1/3 at +35%, trim another 1/3 at +70%."]
        pos = ledger["positions"]["TEST"]
        assert pos["pre_commit_trims"] == "Trim 1/3 at +35%, trim another 1/3 at +70%."
        assert ledger["trades"][-1]["action"] == "SET_TRIMS"
        assert ledger["cash_gbp"] == pytest.approx(800.0)   # no money moved

    def test_unknown_ticker_skipped(self):
        ledger = make_ledger()
        events = sp.apply_recommendations(
            ledger, [self._set_trims_rec(ticker="NOPE")], "2026-07-20")
        assert "SKIP SET_TRIMS NOPE" in events[0]
        assert not ledger["trades"]

    def test_empty_trims_text_skipped(self):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-07-20")
        events = sp.apply_recommendations(
            ledger, [self._set_trims_rec(trims="  ")], "2026-07-20")
        assert "SKIP SET_TRIMS TEST" in events[0]
        assert "pre_commit_trims" not in ledger["positions"]["TEST"]

    def test_passes_strategy_guards_untouched(self):
        rec = self._set_trims_rec()
        pre_val = {"total_value_gbp": 1000.0, "cash_gbp": 100.0, "positions": {}}
        allowed, events = ta.enforce_strategy_guards([rec], make_ledger(), pre_val)
        assert allowed == [rec]
        assert not any("SET_TRIMS" in e for e in events)

    def test_executor_confirms_without_placing_order(self, monkeypatch):
        monkeypatch.setattr(t212ex, "T212_DEMO_EXECUTE", True)
        monkeypatch.setattr(t212ex, "T212_ENV", "demo")
        monkeypatch.setattr(t212ex, "_load_instruments", lambda: INSTRUMENTS)
        monkeypatch.setattr(t212ex, "get_t212_positions_map", lambda: {})
        monkeypatch.setattr(
            t212ex, "_place_market_order",
            lambda *a, **k: pytest.fail("SET_TRIMS must not place a T212 order"))
        rec = self._set_trims_rec()
        events, confirmed = t212ex.execute_recommendations([rec])
        assert confirmed == [rec]
        assert any("SET_TRIMS TEST" in e for e in events)

    def test_realized_pnl_ignores_set_trims_trades(self):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-07-20")
        sp.apply_recommendations(ledger, [self._set_trims_rec()], "2026-07-20")
        pnl = sp.compute_realized_pnl(ledger)
        assert pnl["total_gbp"] == pytest.approx(0.0)

    def test_thesis_review_flags_missing_trim_levels(self):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-07-20")
        val = {"positions": {"TEST": {"pnl_pct": 5.0}}}
        review = sp.build_thesis_review(ledger, val)
        assert "NONE SET" in review
        sp.apply_recommendations(ledger, [self._set_trims_rec()], "2026-07-20")
        review = sp.build_thesis_review(ledger, val)
        assert "NONE SET" not in review
        assert "Trim 1/3 at +35%" in review


# =============================================================================
# SET_DRIVER — the forward driver carrying a played-out position
# =============================================================================

class TestSetDriver:
    DRIVER = "Backlog of $51bn underwrites 12-18 months of revenue at rising margins."

    def _set_driver_rec(self, ticker="TEST", driver=DRIVER):
        return {"action": "SET_DRIVER", "yfinance_ticker": ticker,
                "forward_driver": driver}

    def _held(self, ledger=None):
        ledger = ledger or make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-07-20")
        return ledger

    def test_records_driver_without_price_fetch_or_cash_movement(self, monkeypatch):
        monkeypatch.setattr(
            sp, "fetch_price_gbp",
            lambda *a, **k: pytest.fail("SET_DRIVER must not fetch a price"))
        ledger = self._held()
        events = sp.apply_recommendations(
            ledger, [self._set_driver_rec()], "2026-07-27")
        assert events == [f"SET_DRIVER TEST: {self.DRIVER}"]
        pos = ledger["positions"]["TEST"]
        assert pos["thesis_played_out"] is True
        assert pos["forward_driver"] == self.DRIVER
        assert pos["forward_driver_set"] == "2026-07-27"
        assert pos["forward_driver_history"] == [
            {"date": "2026-07-27", "driver": self.DRIVER}
        ]
        assert ledger["trades"][-1]["action"] == "SET_DRIVER"
        assert ledger["cash_gbp"] == pytest.approx(800.0)

    def test_falls_back_to_thesis_oneline(self):
        ledger = self._held()
        sp.apply_recommendations(
            ledger,
            [{"action": "SET_DRIVER", "yfinance_ticker": "TEST",
              "thesis_oneline": "Fallback driver."}],
            "2026-07-27")
        assert ledger["positions"]["TEST"]["forward_driver"] == "Fallback driver."

    def test_replacement_is_recorded_in_history_and_trade(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._set_driver_rec()], "2026-07-27")
        events = sp.apply_recommendations(
            ledger, [self._set_driver_rec(driver="A different driver.")],
            "2026-08-03")
        assert "replaces" in events[0]
        pos = ledger["positions"]["TEST"]
        assert pos["forward_driver"] == "A different driver."
        assert pos["forward_driver_set"] == "2026-08-03"
        assert [h["driver"] for h in pos["forward_driver_history"]] == [
            self.DRIVER, "A different driver."
        ]
        assert ledger["trades"][-1]["replaces_driver"] == self.DRIVER

    def test_unknown_ticker_skipped(self):
        ledger = make_ledger()
        events = sp.apply_recommendations(
            ledger, [self._set_driver_rec(ticker="NOPE")], "2026-07-27")
        assert "SKIP SET_DRIVER NOPE" in events[0]
        assert not ledger["trades"]

    def test_empty_driver_text_skipped(self):
        ledger = self._held()
        events = sp.apply_recommendations(
            ledger, [self._set_driver_rec(driver="  ")], "2026-07-27")
        assert "SKIP SET_DRIVER TEST" in events[0]
        assert "thesis_played_out" not in ledger["positions"]["TEST"]

    def test_passes_strategy_guards_untouched(self):
        rec = self._set_driver_rec()
        pre_val = {"total_value_gbp": 1000.0, "cash_gbp": 100.0, "positions": {}}
        allowed, events = ta.enforce_strategy_guards([rec], make_ledger(), pre_val)
        assert allowed == [rec]
        assert not any("SET_DRIVER" in e for e in events)

    def test_executor_confirms_without_placing_order(self, monkeypatch):
        monkeypatch.setattr(t212ex, "T212_DEMO_EXECUTE", True)
        monkeypatch.setattr(t212ex, "T212_ENV", "demo")
        monkeypatch.setattr(t212ex, "_load_instruments", lambda: INSTRUMENTS)
        monkeypatch.setattr(t212ex, "get_t212_positions_map", lambda: {})
        monkeypatch.setattr(
            t212ex, "_place_market_order",
            lambda *a, **k: pytest.fail("SET_DRIVER must not place a T212 order"))
        rec = self._set_driver_rec()
        events, confirmed = t212ex.execute_recommendations([rec])
        assert confirmed == [rec]
        assert any("SET_DRIVER TEST" in e for e in events)

    def test_realized_pnl_ignores_set_driver_trades(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._set_driver_rec()], "2026-07-27")
        assert sp.compute_realized_pnl(ledger)["total_gbp"] == pytest.approx(0.0)

    def test_thesis_review_replays_driver_and_demands_a_decision(self):
        ledger = self._held()
        val = {"positions": {"TEST": {"pnl_pct": 95.0}}}
        assert "ORIGINAL THESIS ALREADY PLAYED OUT" not in sp.build_thesis_review(
            ledger, val)

        sp.apply_recommendations(ledger, [self._set_driver_rec()], "2026-07-27")
        review = sp.build_thesis_review(ledger, val)
        assert "ORIGINAL THESIS ALREADY PLAYED OUT" in review
        assert self.DRIVER in review
        assert "REQUIRED THIS RUN" in review

    def test_thesis_review_shows_driver_age(self):
        ledger = self._held()
        old = (date.today() - timedelta(weeks=6)).isoformat()
        sp.apply_recommendations(ledger, [self._set_driver_rec()], old)
        review = sp.build_thesis_review(ledger, {"positions": {}})
        assert "named 6 weeks ago" in review

    def test_thesis_review_lists_superseded_drivers(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._set_driver_rec()], "2026-07-27")
        sp.apply_recommendations(
            ledger, [self._set_driver_rec(driver="Second driver.")], "2026-08-03")
        review = sp.build_thesis_review(ledger, {"positions": {}})
        assert "driver #2" in review
        assert self.DRIVER in review          # the superseded one is still shown
        assert "Second driver." in review

    def test_selling_the_position_drops_the_played_out_flag(self, monkeypatch):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._set_driver_rec()], "2026-07-27")
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 12.0)
        sp.apply_recommendations(
            ledger, [{"action": "SELL", "yfinance_ticker": "TEST"}], "2026-08-03")
        assert "TEST" not in ledger["positions"]

    # --- advisory alerts surfaced in the weekly email ---

    def _played_out_ledger(self, trims="Trim 1/3 at +130%, another 1/3 at +175%.",
                           drivers=1, set_date="2026-07-27"):
        ledger = make_ledger()
        ledger["positions"] = {
            "DELL": {
                "shares": 3.0, "avg_cost_gbp": 160.0, "first_bought": "2026-04-26",
                "thesis": "cheap at 17x", "pre_commit_trims": trims,
                "thesis_played_out": True,
                "forward_driver": "backlog", "forward_driver_set": set_date,
                "forward_driver_history": [
                    {"date": set_date, "driver": f"driver {i}"}
                    for i in range(drivers)
                ],
            }
        }
        return ledger

    def _pre_val(self, pnl=97.6):
        return {"total_value_gbp": 6000.0, "cash_gbp": 400.0,
                "positions": {"DELL": {"current_value_gbp": 960.0,
                                       "pnl_pct": pnl}}}

    def test_alerts_that_position_is_held_on_a_driver(self):
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(), self._pre_val())
        assert any("held on a forward driver" in e for e in events)

    def test_no_driver_alert_when_position_is_being_sold(self):
        _, events = ta.enforce_strategy_guards(
            [{"action": "SELL", "yfinance_ticker": "DELL"}],
            self._played_out_ledger(), self._pre_val())
        assert not any("held on a forward driver" in e for e in events)

    def test_driver_churn_flagged_from_third_driver(self):
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(drivers=2), self._pre_val())
        assert not any("different forward drivers" in e for e in events)
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(drivers=3), self._pre_val())
        assert any("3 different forward drivers" in e for e in events)

    def test_unreachable_trim_level_alerts(self):
        # +97.6% now, next level +130% from entry = a further 16.4% rally
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(), self._pre_val())
        assert any("no near-term mechanical exit" in e for e in events)

    def test_reachable_trim_level_does_not_alert(self):
        # +115% from entry on a +97.6% position is only ~8.8% above today
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(trims="Trim 1/3 at +115%."),
            self._pre_val())
        assert not any("no near-term mechanical exit" in e for e in events)

    def test_no_trim_alert_when_set_trims_lands_this_run(self):
        _, events = ta.enforce_strategy_guards(
            [{"action": "SET_TRIMS", "yfinance_ticker": "DELL",
              "pre_commit_trims": "Trim 1/3 at +110%."}],
            self._played_out_ledger(), self._pre_val())
        assert not any("no near-term mechanical exit" in e for e in events)

    def test_alerts_when_no_trim_level_remains_above_current_pnl(self):
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(trims="Trim 1/3 at +30%."),
            self._pre_val())
        assert any("no trim level remains" in e for e in events)

    def test_healthy_position_raises_no_played_out_alerts(self):
        ledger = self._played_out_ledger()
        del ledger["positions"]["DELL"]["thesis_played_out"]
        _, events = ta.enforce_strategy_guards([], ledger, self._pre_val())
        assert not any("played out" in e or "forward driver" in e for e in events)

    def test_alerts_are_advisory_never_blocking(self):
        recs = [gbp_buy_rec("MSFT", 300, theme="software")]
        allowed, events = ta.enforce_strategy_guards(
            recs, self._played_out_ledger(), self._pre_val())
        assert allowed == recs
        assert any("played out" in e for e in events)

    def test_missing_price_does_not_crash_the_alert(self):
        pre_val = {"total_value_gbp": 6000.0, "cash_gbp": 400.0,
                   "positions": {"DELL": {"current_value_gbp": 960.0}}}
        _, events = ta.enforce_strategy_guards(
            [], self._played_out_ledger(), pre_val)
        assert not any("mechanical exit" in e for e in events)

    def test_weeks_since_handles_bad_input(self):
        assert sp._weeks_since(None) is None
        assert sp._weeks_since("not-a-date") is None
        assert sp._weeks_since(date.today().isoformat()) == 0


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
# Sell settlement detection (position-delta based)
# =============================================================================

class TestSellSettlement:
    def _orders(self):
        # (order_id, rec, t212_ticker, qty_sold)
        return [
            ("o1", {"action": "SELL"}, "AVGO_US_EQ", 3.0),
            ("o2", {"action": "TRIM"}, "GOOGL_US_EQ", 1.5),
        ]

    def test_full_sell_settles_when_position_gone(self):
        pre = {"AVGO_US_EQ": {"quantity": 3.0}, "GOOGL_US_EQ": {"quantity": 3.6}}
        cur = {"GOOGL_US_EQ": {"quantity": 3.6}}   # AVGO gone
        out = t212ex._classify_sell_settlement(
            [self._orders()[0]], pre, cur, {})
        assert out["o1"] == "SETTLED"

    def test_trim_settles_when_quantity_drops(self):
        pre = {"GOOGL_US_EQ": {"quantity": 3.6}}
        cur = {"GOOGL_US_EQ": {"quantity": 2.1}}   # dropped 1.5
        out = t212ex._classify_sell_settlement(
            [self._orders()[1]], pre, cur, {})
        assert out["o2"] == "SETTLED"

    def test_pending_when_position_unchanged(self):
        pre = {"AVGO_US_EQ": {"quantity": 3.0}}
        cur = {"AVGO_US_EQ": {"quantity": 3.0}}     # queued, not executed
        out = t212ex._classify_sell_settlement(
            [self._orders()[0]], pre, cur, {})
        assert out["o1"] == "PENDING"

    def test_partial_drop_less_than_sold_is_pending(self):
        pre = {"GOOGL_US_EQ": {"quantity": 3.6}}
        cur = {"GOOGL_US_EQ": {"quantity": 3.0}}    # dropped 0.6 < 1.5
        out = t212ex._classify_sell_settlement(
            [self._orders()[1]], pre, cur, {})
        assert out["o2"] == "PENDING"

    def test_rejected_status_overrides_position(self):
        pre = {"AVGO_US_EQ": {"quantity": 3.0}}
        cur = {"AVGO_US_EQ": {"quantity": 3.0}}
        out = t212ex._classify_sell_settlement(
            [self._orders()[0]], pre, cur, {"o1": "REJECTED"})
        assert out["o1"] == "REJECTED"

    def test_mixed_batch(self):
        pre = {"AVGO_US_EQ": {"quantity": 3.0}, "GOOGL_US_EQ": {"quantity": 3.6}}
        cur = {"GOOGL_US_EQ": {"quantity": 3.6}}    # AVGO sold, GOOGL trim not yet
        out = t212ex._classify_sell_settlement(
            self._orders(), pre, cur, {})
        assert out["o1"] == "SETTLED"
        assert out["o2"] == "PENDING"


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

    def test_picks_last_recommendations_block(self):
        # If the model echoes an example block in its prose, the FINAL block
        # is the actionable one
        text = (
            '```json\n{"recommendations": [{"action": "BUY", "ticker": "ECHO"}]}\n```\n'
            'more prose\n'
            '```json\n{"recommendations": [{"action": "BUY", "ticker": "REAL"}]}\n```'
        )
        recs = ta.extract_recommendations(text)
        assert len(recs) == 1
        assert recs[0]["ticker"] == "REAL"

    def test_skips_non_recommendation_json(self):
        text = (
            '```json\n{"recommendations": [{"action": "SELL", "ticker": "X"}]}\n```\n'
            '```json\n{"some_other_data": 1}\n```'
        )
        recs = ta.extract_recommendations(text)
        assert recs[0]["ticker"] == "X"


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


# =============================================================================
# Claude API retry behaviour
# =============================================================================

class _FakeStream:
    """Mimics the context manager returned by client.messages.stream()."""
    def __init__(self, outcome):
        self._outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeClient:
    """Yields one scripted outcome (exception or message) per stream() call."""
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.messages = self  # so client.messages.stream(...) resolves here

    def stream(self, **kwargs):
        self.calls += 1
        return _FakeStream(self._outcomes.pop(0))


class TestCreateWithRetry:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        monkeypatch.setattr(ta.time, "sleep", lambda s: None)

    def test_mid_stream_disconnect_is_retried(self):
        # A connection dropped WHILE streaming (e.g. AV HTTPS interception)
        # raises a raw httpx error, not an anthropic.APIConnectionError —
        # this crashed the 2026-07-13 weekly run before the fix.
        exc = httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body")
        client = _FakeClient([exc, "final-message"])
        assert ta._create_with_retry(client) == "final-message"
        assert client.calls == 2

    def test_persistent_disconnect_raises_after_all_retries(self):
        exc = httpx.RemoteProtocolError("incomplete chunked read")
        client = _FakeClient([exc] * 4)
        with pytest.raises(httpx.RemoteProtocolError):
            ta._create_with_retry(client)
        assert client.calls == 4

    def test_connection_error_is_retried(self):
        exc = anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com"))
        client = _FakeClient([exc, "final-message"])
        assert ta._create_with_retry(client) == "final-message"
        assert client.calls == 2

    def test_client_4xx_not_retried(self):
        resp = httpx.Response(
            400, request=httpx.Request("POST", "https://api.anthropic.com"))
        exc = anthropic.APIStatusError("bad request", response=resp, body=None)
        client = _FakeClient([exc])
        with pytest.raises(anthropic.APIStatusError):
            ta._create_with_retry(client)
        assert client.calls == 1

    def test_server_5xx_retried(self):
        resp = httpx.Response(
            500, request=httpx.Request("POST", "https://api.anthropic.com"))
        exc = anthropic.APIStatusError("server error", response=resp, body=None)
        client = _FakeClient([exc, "final-message"])
        assert ta._create_with_retry(client) == "final-message"
        assert client.calls == 2
