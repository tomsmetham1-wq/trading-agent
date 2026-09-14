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


class TestIdleCashTrapAlert:
    """
    The gap that idled cash from late June to late Aug 2026: cash above the
    old 5-8% dead-zone band but with a deployable slice too small to fund a
    new position at the 8% minimum. No rule fired, so nothing was deployed
    for eight weeks. Reproduces the real 17 Aug numbers.
    """
    LEDGER = {"trades": [], "positions": {}}

    def _pre_val(self, cash, total=6344.43, positions=None):
        return {"total_value_gbp": total, "cash_gbp": cash,
                "positions": positions or {}}

    def test_alerts_on_the_17_aug_trap(self):
        # cash 12.7%; slice = 808.35 - 317.22 = 491.13 vs a 507.55 minimum
        _, events = ta.enforce_strategy_guards([], self.LEDGER,
                                                self._pre_val(808.35))
        assert any("dead-zone top-up" in e for e in events)

    def test_no_alert_when_a_buy_is_proposed(self):
        _, events = ta.enforce_strategy_guards(
            [{"action": "BUY", "yfinance_ticker": "NEW", "amount_gbp": 400}],
            self.LEDGER, self._pre_val(808.35))
        assert not any("dead-zone top-up" in e for e in events)

    def test_no_alert_when_slice_funds_a_new_position(self):
        # cash 14% -> slice 9% > the 8% minimum, so a new position is fundable
        _, events = ta.enforce_strategy_guards([], self.LEDGER,
                                                self._pre_val(888.0))
        assert not any("dead-zone top-up" in e for e in events)

    def test_no_alert_when_slice_is_below_the_topup_minimum(self):
        # 3 Aug 2026: slice was 2.6%, genuinely too small to use
        _, events = ta.enforce_strategy_guards([], self.LEDGER,
                                                self._pre_val(466.14, total=6094.47))
        assert not any("dead-zone top-up" in e for e in events)

    def test_sale_proceeds_count_toward_the_slice(self):
        # a SELL this run lifts cash out of the trap and into fundable range
        pre_val = self._pre_val(
            300.0, positions={"OLD": {"current_value_gbp": 600.0}})
        _, events = ta.enforce_strategy_guards(
            [{"action": "SELL", "yfinance_ticker": "OLD"}],
            {"trades": [], "positions": {"OLD": {}}}, pre_val)
        assert not any("dead-zone top-up" in e for e in events)

    def test_no_alert_when_cash_unknown(self):
        _, events = ta.enforce_strategy_guards(
            [], self.LEDGER, {"total_value_gbp": 6344.43, "positions": {}})
        assert not any("dead-zone top-up" in e for e in events)

    def test_alert_is_advisory_never_blocking(self):
        recs = [{"action": "SET_TRIMS", "yfinance_ticker": "X",
                 "pre_commit_trims": "Trim 1/3 at +40%."}]
        allowed, events = ta.enforce_strategy_guards(
            recs, self.LEDGER, self._pre_val(808.35))
        assert allowed == recs
        assert any("dead-zone top-up" in e for e in events)


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
# SET_THESIS — re-underwriting a thesis that was never a prediction
# =============================================================================

class TestSetThesis:
    """
    On 2026-09-14 the prompt asked for a backfilled thesis to be re-underwritten
    "as a SET_DRIVER". SET_DRIVER declares the thesis played out, so the bank
    injection trimmed a third of AMZN at -3.6% and of GOOGL at +0.16%. This is
    the action that should have existed: replaces the thesis, touches nothing
    in the played-out machinery.
    """
    CASE = "AWS re-accelerating to 37% with 39% margins; would buy fresh today."

    def _rec(self, ticker="TEST", text=CASE, key="thesis"):
        return {"action": "SET_THESIS", "yfinance_ticker": ticker, key: text}

    def _held(self):
        ledger = make_ledger()
        sp.apply_recommendations(ledger, [gbp_buy_rec("TEST", 200)], "2026-04-26")
        ledger["positions"]["TEST"]["thesis"] = (
            "[Backfilled 2026-07-03 - no thesis recorded at entry] Old case.")
        return ledger

    def test_replaces_thesis_without_price_fetch_or_cash_movement(self, monkeypatch):
        monkeypatch.setattr(
            sp, "fetch_price_gbp",
            lambda *a, **k: pytest.fail("SET_THESIS must not fetch a price"))
        ledger = self._held()
        events = sp.apply_recommendations(ledger, [self._rec()], "2026-09-14")
        assert events == [f"SET_THESIS TEST: {self.CASE}"]
        pos = ledger["positions"]["TEST"]
        assert pos["thesis"] == f"[Re-underwritten 2026-09-14] {self.CASE}"
        assert pos["thesis_reunderwritten"] == "2026-09-14"
        assert ledger["cash_gbp"] == pytest.approx(800.0)
        assert ledger["positions"]["TEST"]["shares"] == pytest.approx(20.0)

    def test_does_not_touch_the_played_out_machinery(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._rec()], "2026-09-14")
        pos = ledger["positions"]["TEST"]
        for key in ("thesis_played_out", "forward_driver", "forward_driver_set",
                    "forward_driver_history", "driver_failed_on"):
            assert key not in pos

    def test_trade_record_keeps_the_replaced_text(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._rec()], "2026-09-14")
        t = ledger["trades"][-1]
        assert t["action"] == "SET_THESIS"
        assert t["ticker"] == "TEST"
        assert t["replaces_thesis"].startswith("[Backfilled 2026-07-03")
        assert t["thesis"].startswith("[Re-underwritten 2026-09-14]")

    def test_falls_back_to_thesis_oneline(self):
        ledger = self._held()
        sp.apply_recommendations(
            ledger, [self._rec(text="Fallback case.", key="thesis_oneline")],
            "2026-09-14")
        assert ledger["positions"]["TEST"]["thesis"].endswith("Fallback case.")

    def test_unknown_ticker_and_empty_text_are_skipped(self):
        ledger = self._held()
        events = sp.apply_recommendations(
            ledger, [self._rec(ticker="NOPE"), self._rec(text="  ")], "2026-09-14")
        assert "SKIP SET_THESIS NOPE" in events[0]
        assert "SKIP SET_THESIS TEST" in events[1]
        assert ledger["positions"]["TEST"]["thesis"].startswith("[Backfilled")
        assert not any(t["action"] == "SET_THESIS" for t in ledger["trades"])

    def test_provenance_becomes_recorded_dated_from_the_rewrite(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._rec()], "2026-09-14")
        kind, detail = sp.entry_thesis_provenance(ledger["positions"]["TEST"])
        assert kind == "recorded"
        assert "re-underwritten 2026-09-14" in detail

    def test_provenance_reads_the_date_off_the_prefix_if_field_missing(self):
        kind, detail = sp.entry_thesis_provenance(
            {"thesis": "[Re-underwritten 2026-09-14] Fresh case."})
        assert kind == "recorded"
        assert "2026-09-14" in detail

    def test_review_no_longer_flags_it_but_notes_the_date(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._rec()], "2026-09-14")
        review = sp.build_thesis_review(
            ledger, {"positions": {"TEST": {"pnl_pct": -3.6}}})
        assert "ENTRY THESIS NOT RECORDED" not in review
        assert "re-underwritten 2026-09-14" in review

    def test_review_asks_for_set_thesis_not_set_driver(self):
        review = sp.build_thesis_review(
            self._held(), {"positions": {"TEST": {"pnl_pct": -3.6}}})
        assert "as a SET_THESIS action" in review
        assert "NOT SET_DRIVER" in review

    def test_passes_strategy_guards_untouched(self):
        rec = self._rec()
        pre_val = {"total_value_gbp": 1000.0, "cash_gbp": 100.0, "positions": {}}
        allowed, events = ta.enforce_strategy_guards([rec], make_ledger(), pre_val)
        assert allowed == [rec]
        assert not any("SET_THESIS" in e for e in events)

    def test_does_not_trigger_the_played_out_bank(self):
        # The 2026-09-14 failure mode, end to end: a re-underwrite of a
        # position with no gain must not have a 33% trim injected after it.
        ledger = self._held()
        pre_val = {"total_value_gbp": 1000.0, "cash_gbp": 800.0,
                   "positions": {"TEST": {"current_value_gbp": 193.0,
                                          "pnl_pct": -3.6}}}
        allowed, events = ta.enforce_strategy_guards([self._rec()], ledger, pre_val)
        assert [r["action"] for r in allowed] == ["SET_THESIS"]
        assert not any("FORCED TRIM" in e for e in events)

    def test_executor_confirms_without_placing_order(self, monkeypatch):
        monkeypatch.setattr(t212ex, "T212_DEMO_EXECUTE", True)
        monkeypatch.setattr(t212ex, "T212_ENV", "demo")
        monkeypatch.setattr(t212ex, "_load_instruments", lambda: INSTRUMENTS)
        monkeypatch.setattr(t212ex, "get_t212_positions_map", lambda: {})
        monkeypatch.setattr(
            t212ex, "_place_market_order",
            lambda *a, **k: pytest.fail("SET_THESIS must not place a T212 order"))
        rec = self._rec()
        events, confirmed = t212ex.execute_recommendations([rec])
        assert confirmed == [rec]
        assert any("SET_THESIS TEST" in e for e in events)

    def test_realized_pnl_and_reconciliation_ignore_it(self):
        ledger = self._held()
        sp.apply_recommendations(ledger, [self._rec()], "2026-09-14")
        assert sp.compute_realized_pnl(ledger)["total_gbp"] == pytest.approx(0.0)
        assert sp.reconcile_trade_log(ledger)["clean"]


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
        # A recent bank keeps the mandatory played-out bank satisfied, so these
        # tests exercise the advisory alerts, not the forced-trim injection
        # (which has its own test class).
        ledger["trades"].append({
            "date": date.today().isoformat(), "action": "TRIM",
            "ticker": "DELL", "shares": 1.0, "amount_gbp": 300.0,
        })
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


class TestPlayedOutBankGuard:
    """
    A played-out declaration costs 1/3 of the position, enforced in code: if
    no trim has banked gains since the declaration (or in the last 12 weeks),
    a 33% TRIM is injected into the rec list. Added Aug 2026 after DELL was
    declared played out at +97.6% and then simply held week after week on a
    re-confirmed forward driver — words satisfied the accountability loop,
    money never moved.
    """

    def _ledger(self, declared=None, banked=None):
        declared = declared or (date.today() - timedelta(weeks=1)).isoformat()
        ledger = make_ledger()
        ledger["positions"] = {
            "DELL": {
                "shares": 3.0, "avg_cost_gbp": 160.0, "first_bought": "2026-04-26",
                "thesis": "cheap at 17x",
                "pre_commit_trims": "Trim 1/3 at +110%, another 1/3 at +150%.",
                "thesis_played_out": True,
                "forward_driver": "backlog", "forward_driver_set": declared,
                "forward_driver_history": [{"date": declared, "driver": "backlog"}],
            }
        }
        if banked:
            ledger["trades"].append({
                "date": banked, "action": "TRIM",
                "ticker": "DELL", "shares": 1.0, "amount_gbp": 300.0,
            })
        return ledger

    def _pre_val(self, value=960.0):
        return {"total_value_gbp": 6000.0, "cash_gbp": 400.0,
                "positions": {"DELL": {"current_value_gbp": value,
                                       "pnl_pct": 97.6}}}

    def _forced(self, allowed):
        return [r for r in allowed if r.get("guard_generated")]

    def test_injects_forced_trim_when_no_bank_since_declaration(self):
        allowed, events = ta.enforce_strategy_guards(
            [], self._ledger(), self._pre_val())
        forced = self._forced(allowed)
        assert len(forced) == 1
        assert forced[0]["action"] == "TRIM"
        assert forced[0]["yfinance_ticker"] == "DELL"
        assert forced[0]["trim_pct"] == ta.PLAYED_OUT_BANK_TRIM_PCT
        assert any(e.startswith("FORCED TRIM DELL") for e in events)

    def test_injected_trim_comes_before_other_recs(self):
        buy = gbp_buy_rec("MSFT", 300, theme="software")
        allowed, _ = ta.enforce_strategy_guards(
            [buy], self._ledger(), self._pre_val())
        assert allowed[0].get("guard_generated")
        assert allowed[-1] == buy

    def test_no_injection_when_claude_trims_itself(self):
        recs = [{"action": "TRIM", "yfinance_ticker": "DELL", "trim_pct": 25}]
        allowed, events = ta.enforce_strategy_guards(
            recs, self._ledger(), self._pre_val())
        assert not self._forced(allowed)
        assert not any("FORCED TRIM" in e for e in events)

    def test_no_injection_when_claude_sells(self):
        recs = [{"action": "SELL", "yfinance_ticker": "DELL"}]
        allowed, _ = ta.enforce_strategy_guards(
            recs, self._ledger(), self._pre_val())
        assert not self._forced(allowed)

    def test_no_injection_when_recently_banked(self):
        ledger = self._ledger(banked=date.today().isoformat())
        allowed, events = ta.enforce_strategy_guards([], ledger, self._pre_val())
        assert not self._forced(allowed)
        # the advisory note that the hold rests on a driver still fires
        assert any("held on a forward driver" in e for e in events)

    def test_reinjects_when_last_bank_is_stale(self):
        declared = (date.today() - timedelta(weeks=20)).isoformat()
        banked = (date.today() - timedelta(weeks=13)).isoformat()
        ledger = self._ledger(declared=declared, banked=banked)
        allowed, _ = ta.enforce_strategy_guards([], ledger, self._pre_val())
        assert len(self._forced(allowed)) == 1

    def test_no_reinjection_inside_rebank_window(self):
        declared = (date.today() - timedelta(weeks=20)).isoformat()
        banked = (date.today() - timedelta(weeks=5)).isoformat()
        ledger = self._ledger(declared=declared, banked=banked)
        allowed, _ = ta.enforce_strategy_guards([], ledger, self._pre_val())
        assert not self._forced(allowed)

    def test_same_run_set_driver_without_trim_triggers_bank(self):
        ledger = self._ledger()
        del ledger["positions"]["DELL"]["thesis_played_out"]
        recs = [{"action": "SET_DRIVER", "yfinance_ticker": "DELL",
                 "forward_driver": "new driver"}]
        allowed, events = ta.enforce_strategy_guards(recs, ledger, self._pre_val())
        forced = self._forced(allowed)
        assert len(forced) == 1
        assert any("declared this run" in e for e in events)
        # the SET_DRIVER itself still goes through
        assert any(r.get("action") == "SET_DRIVER" for r in allowed)

    def test_same_run_set_driver_with_own_trim_no_injection(self):
        ledger = self._ledger()
        del ledger["positions"]["DELL"]["thesis_played_out"]
        recs = [
            {"action": "SET_DRIVER", "yfinance_ticker": "DELL",
             "forward_driver": "new driver"},
            {"action": "TRIM", "yfinance_ticker": "DELL", "trim_pct": 33},
        ]
        allowed, _ = ta.enforce_strategy_guards(recs, ledger, self._pre_val())
        assert not self._forced(allowed)

    def test_dust_position_alerts_instead_of_trimming(self):
        allowed, events = ta.enforce_strategy_guards(
            [], self._ledger(), self._pre_val(value=60.0))
        assert not self._forced(allowed)
        assert any("too small to force" in e for e in events)

    def test_missing_price_alerts_instead_of_trimming(self):
        pre_val = {"total_value_gbp": 6000.0, "cash_gbp": 400.0,
                   "positions": {"DELL": {}}}
        allowed, events = ta.enforce_strategy_guards([], self._ledger(), pre_val)
        assert not self._forced(allowed)
        assert any("no live price" in e for e in events)

    def test_non_played_out_position_untouched(self):
        ledger = self._ledger()
        del ledger["positions"]["DELL"]["thesis_played_out"]
        allowed, events = ta.enforce_strategy_guards([], ledger, self._pre_val())
        assert allowed == []
        assert not any("FORCED TRIM" in e for e in events)

    def test_thesis_review_warns_when_bank_due(self):
        review = sp.build_thesis_review(
            self._ledger(), {"positions": {"DELL": {"pnl_pct": 97.6}}})
        assert "MECHANICAL BANK DUE" in review

    def test_thesis_review_silent_when_recently_banked(self):
        review = sp.build_thesis_review(
            self._ledger(banked=date.today().isoformat()),
            {"positions": {"DELL": {"pnl_pct": 97.6}}})
        assert "MECHANICAL BANK DUE" not in review


class TestSetTrimsTightenGuard:
    """
    Trim levels may only be tightened, never loosened — the July 2026 deep
    review flagged DELL's levels being reset twice in eight days as moving
    the goalposts. A SET_TRIMS that raises (or removes) the next un-hit
    trigger is blocked and the existing levels kept.
    """

    def _ledger(self, trims="Trim 1/3 at +130%, another 1/3 at +175%."):
        ledger = make_ledger()
        ledger["positions"] = {
            "DELL": {
                "shares": 3.0, "avg_cost_gbp": 160.0, "first_bought": "2026-04-26",
                "thesis": "cheap at 17x", "pre_commit_trims": trims,
            }
        }
        return ledger

    def _pre_val(self, pnl=97.6):
        positions = {"DELL": {"current_value_gbp": 960.0}}
        if pnl is not None:
            positions["DELL"]["pnl_pct"] = pnl
        return {"total_value_gbp": 6000.0, "cash_gbp": 400.0,
                "positions": positions}

    def _set_trims(self, text):
        return {"action": "SET_TRIMS", "yfinance_ticker": "DELL",
                "pre_commit_trims": text}

    def test_blocks_raising_next_unhit_level(self):
        rec = self._set_trims("Trim 1/3 at +150%, another 1/3 at +200%.")
        allowed, events = ta.enforce_strategy_guards(
            [rec], self._ledger(), self._pre_val())
        assert rec not in allowed
        assert any("BLOCKED SET_TRIMS DELL" in e for e in events)

    def test_allows_tightening(self):
        rec = self._set_trims("Trim 1/3 at +110%, another 1/3 at +150%.")
        allowed, events = ta.enforce_strategy_guards(
            [rec], self._ledger(), self._pre_val())
        assert rec in allowed
        assert not any("BLOCKED SET_TRIMS" in e for e in events)

    def test_blocks_removing_every_unhit_level(self):
        # all proposed levels sit below current P&L — nothing left to bite
        rec = self._set_trims("Trim 1/3 at +30%.")
        allowed, events = ta.enforce_strategy_guards(
            [rec], self._ledger(), self._pre_val())
        assert rec not in allowed
        assert any("removes every un-hit level" in e for e in events)

    def test_allows_backfill_when_no_existing_levels(self):
        rec = self._set_trims("Trim 1/3 at +150%.")
        allowed, _ = ta.enforce_strategy_guards(
            [rec], self._ledger(trims=""), self._pre_val())
        assert rec in allowed

    def test_equal_level_allowed(self):
        rec = self._set_trims("Trim 1/3 at +130%, another 1/3 at +175%.")
        allowed, _ = ta.enforce_strategy_guards(
            [rec], self._ledger(), self._pre_val())
        assert rec in allowed

    def test_unknown_pnl_compares_raw_first_triggers(self):
        raise_rec = self._set_trims("Trim 1/3 at +140%.")
        allowed, events = ta.enforce_strategy_guards(
            [raise_rec], self._ledger(), self._pre_val(pnl=None))
        assert raise_rec not in allowed
        assert any("BLOCKED SET_TRIMS" in e for e in events)

        lower_rec = self._set_trims("Trim 1/3 at +120%.")
        allowed, _ = ta.enforce_strategy_guards(
            [lower_rec], self._ledger(), self._pre_val(pnl=None))
        assert lower_rec in allowed

    def test_hit_levels_are_ignored_in_comparison(self):
        # +130% already hit at +140% P&L: only +175% is pending, so a new set
        # with first pending level +160% is a tightening even though its raw
        # first trigger (+160%) is above the old raw first (+130%).
        rec = self._set_trims("Trim 1/3 at +160%.")
        allowed, _ = ta.enforce_strategy_guards(
            [rec], self._ledger(), self._pre_val(pnl=140.0))
        assert rec in allowed


class TestWatchlistRecording:
    """
    The watchlist is RECORDED and scored, never gated. Added Aug 2026: the
    agent named 3 fresh watchlist ideas every week and never revisited them
    (ZTS/STZ/ACN one run, NFLX/AZN.L/WMT the next), so there was no evidence
    about whether its non-held ideas were any good — the open question after
    DELL carried the entire book. These tests pin the recording behaviour AND
    the guarantee that nothing here blocks or creates a trade.
    """

    def _entry(self, ticker="ZTS", thesis="Cheap animal health.", theme="pharma"):
        return {"ticker": ticker, "yfinance_ticker": ticker,
                "thesis_oneline": thesis, "theme": theme}

    def _prices(self, mapping):
        return lambda t: mapping.get(t)

    def test_records_new_name_with_price_and_thesis(self):
        ledger = make_ledger()
        events = sp.record_watchlist(
            ledger, [self._entry()], "2026-08-17",
            price_fn=self._prices({"ZTS": 100.0}), benchmark_return_pct=9.0)
        rec = ledger["watchlist"]["ZTS"]
        assert rec["first_seen"] == "2026-08-17"
        assert rec["active"] is True
        assert rec["thesis"] == "Cheap animal health."
        assert rec["theme"] == "pharma"
        assert rec["observations"] == [
            {"date": "2026-08-17", "price_gbp": 100.0, "benchmark_return_pct": 9.0}
        ]
        assert any("WATCHLIST +ZTS" in e for e in events)

    def test_exited_position_is_tracked_even_if_not_listed(self):
        # META, 2026-08-24: the report said the watchlist was the right place
        # for it and then didn't list it, so the exit was scored nowhere.
        ledger = make_ledger()
        ledger["trades"].append({
            "date": "2026-08-24", "action": "SELL", "ticker": "META",
            "shares": 1.1197, "amount_gbp": 451.01,
            "exit_thesis": "Thesis broken: FCF collapsed.",
            "closed_position": True,
        })
        events = sp.record_watchlist(
            ledger, [], "2026-08-24",
            price_fn=self._prices({"META": 402.79}), benchmark_return_pct=6.82)
        rec = ledger["watchlist"]["META"]
        assert rec["source"] == "exit"
        assert rec["active"] is False          # evidence, not a live idea
        assert rec["exited_on"] == "2026-08-24"
        assert rec["thesis"] == "Thesis broken: FCF collapsed."
        assert rec["observations"][-1]["price_gbp"] == 402.79
        assert any("WATCHLIST +META" in e for e in events)

    def test_exit_does_not_clobber_a_name_claude_also_listed(self):
        ledger = make_ledger()
        ledger["trades"].append({
            "date": "2026-08-24", "action": "SELL", "ticker": "ZTS",
            "shares": 1, "amount_gbp": 100, "closed_position": True,
        })
        sp.record_watchlist(
            ledger, [self._entry("ZTS")], "2026-08-24",
            price_fn=self._prices({"ZTS": 100.0}))
        rec = ledger["watchlist"]["ZTS"]
        assert rec["active"] is True                    # Claude's entry wins
        assert rec["thesis"] == "Cheap animal health."
        assert "source" not in rec

    def test_exit_tracking_is_idempotent_across_reruns(self):
        ledger = make_ledger()
        ledger["trades"].append({
            "date": "2026-08-24", "action": "SELL", "ticker": "META",
            "shares": 1, "amount_gbp": 451, "closed_position": True,
        })
        for _ in range(2):
            sp.record_watchlist(ledger, [], "2026-08-24",
                                price_fn=self._prices({"META": 402.79}))
        assert len(ledger["watchlist"]["META"]["observations"]) == 1

    def test_trim_that_does_not_close_is_not_tracked(self):
        ledger = make_ledger()
        ledger["trades"].append({
            "date": "2026-08-24", "action": "TRIM", "ticker": "DELL",
            "shares": 1, "amount_gbp": 343.35,
        })
        sp.record_watchlist(ledger, [], "2026-08-24",
                            price_fn=self._prices({"DELL": 320.0}))
        assert "DELL" not in ledger["watchlist"]

    def test_repeat_mention_appends_observation_not_duplicate_entry(self):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 100.0}))
        sp.record_watchlist(ledger, [self._entry()], "2026-08-24",
                            price_fn=self._prices({"ZTS": 110.0}))
        rec = ledger["watchlist"]["ZTS"]
        assert rec["first_seen"] == "2026-08-17"
        assert rec["last_seen"] == "2026-08-24"
        assert [o["price_gbp"] for o in rec["observations"]] == [100.0, 110.0]

    def test_same_date_rerun_updates_in_place(self):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 100.0}))
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 105.0}))
        obs = ledger["watchlist"]["ZTS"]["observations"]
        assert len(obs) == 1
        assert obs[0]["price_gbp"] == 105.0

    def test_dropped_name_keeps_being_priced(self):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 100.0}))
        events = sp.record_watchlist(ledger, [], "2026-08-24",
                                     price_fn=self._prices({"ZTS": 130.0}))
        rec = ledger["watchlist"]["ZTS"]
        assert rec["active"] is False
        # the whole point: an abandoned idea that then ran is still visible
        assert [o["price_gbp"] for o in rec["observations"]] == [100.0, 130.0]
        assert any("dropped from the active list" in e for e in events)

    def test_readded_name_becomes_active_again(self):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 100.0}))
        sp.record_watchlist(ledger, [], "2026-08-24",
                            price_fn=self._prices({"ZTS": 100.0}))
        sp.record_watchlist(ledger, [self._entry()], "2026-08-31",
                            price_fn=self._prices({"ZTS": 100.0}))
        assert ledger["watchlist"]["ZTS"]["active"] is True

    def test_tracking_window_closes_for_long_cold_name(self):
        ledger = make_ledger()
        stale = (date.today() - timedelta(weeks=sp.WATCHLIST_TRACK_WEEKS + 1))
        ledger["watchlist"] = {
            "OLD": {"first_seen": "2026-01-01", "yfinance_ticker": "OLD",
                    "last_seen": stale.isoformat(), "active": False,
                    "observations": [{"date": "2026-01-01", "price_gbp": 10.0}]},
        }
        events = sp.record_watchlist(ledger, [], date.today().isoformat(),
                                     price_fn=self._prices({"OLD": 99.0}))
        rec = ledger["watchlist"]["OLD"]
        assert rec.get("tracking_ended")
        assert len(rec["observations"]) == 1      # no new price fetched
        assert any("tracking window closed" in e for e in events)

    def test_unpriceable_name_is_tracked_without_observation(self):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry("NOPE")], "2026-08-17",
                            price_fn=self._prices({}))
        assert ledger["watchlist"]["NOPE"]["observations"] == []

    def test_malformed_entries_ignored(self):
        ledger = make_ledger()
        sp.record_watchlist(
            ledger, ["not a dict", {}, {"ticker": "  "}, None], "2026-08-17",
            price_fn=self._prices({}))
        assert ledger["watchlist"] == {}

    # --- scoring ---

    def _two_obs_ledger(self, p0=100.0, p1=120.0, b0=0.0, b1=10.0):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": p0}),
                            benchmark_return_pct=b0)
        sp.record_watchlist(ledger, [self._entry()], "2026-08-24",
                            price_fn=self._prices({"ZTS": p1}),
                            benchmark_return_pct=b1)
        return ledger

    def test_scores_return_against_benchmark_over_its_own_window(self):
        # +20% for the name; benchmark went 0% -> 10% inception-relative,
        # which over THIS window is (110/100 - 1) = +10%, so +10 pts.
        row = sp.watchlist_performance(self._two_obs_ledger())[0]
        assert row["return_pct"] == pytest.approx(20.0)
        assert row["benchmark_pct"] == pytest.approx(10.0)
        assert row["vs_benchmark_pts"] == pytest.approx(10.0)

    def test_benchmark_window_is_not_since_inception(self):
        # A name first seen when the benchmark was already +50% must not be
        # charged that 50% — only what happened after it was first seen.
        row = sp.watchlist_performance(
            self._two_obs_ledger(b0=50.0, b1=65.0))[0]
        assert row["benchmark_pct"] == pytest.approx(10.0)

    def test_single_observation_is_tracked_but_unscored(self):
        ledger = make_ledger()
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 100.0}))
        row = sp.watchlist_performance(ledger)[0]
        assert row["return_pct"] is None
        assert row["vs_benchmark_pts"] is None

    def test_bought_name_is_flagged_with_its_buy_date(self, monkeypatch):
        ledger = self._two_obs_ledger()
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 10.0)
        sp.apply_recommendations(ledger, [gbp_buy_rec("ZTS", 200)], "2026-08-24")
        row = sp.watchlist_performance(ledger)[0]
        assert row["bought_date"] == "2026-08-24"

    def test_buy_before_first_seen_is_not_counted(self, monkeypatch):
        ledger = make_ledger()
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 10.0)
        sp.apply_recommendations(ledger, [gbp_buy_rec("ZTS", 200)], "2026-07-01")
        sp.record_watchlist(ledger, [self._entry()], "2026-08-17",
                            price_fn=self._prices({"ZTS": 100.0}))
        assert sp.watchlist_performance(ledger)[0]["bought_date"] is None

    def test_ranking_puts_best_vs_benchmark_first(self):
        ledger = make_ledger()
        for day, prices in (("2026-08-17", {"AAA": 100.0, "BBB": 100.0}),
                            ("2026-08-24", {"AAA": 90.0, "BBB": 140.0})):
            sp.record_watchlist(
                ledger, [self._entry("AAA"), self._entry("BBB")], day,
                price_fn=self._prices(prices), benchmark_return_pct=0.0)
        assert [r["ticker"] for r in sp.watchlist_performance(ledger)] == ["BBB", "AAA"]

    # --- surfaces ---

    def test_prompt_section_replays_names_and_states_it_is_not_a_gate(self):
        review = sp.build_watchlist_review(self._two_obs_ledger())
        assert "ZTS" in review
        assert "+20.0%" in review
        assert "RECORDING ONLY" in review
        assert "Cheap animal health." in review

    def test_prompt_section_empty_when_nothing_tracked(self):
        assert sp.build_watchlist_review(make_ledger()) == ""

    def test_email_section_shows_score_and_average(self):
        table = sp.format_watchlist_for_email(self._two_obs_ledger())
        assert "ZTS" in table
        assert "+20.00%" in table
        assert "average" in table

    def test_email_section_empty_when_nothing_tracked(self):
        assert sp.format_watchlist_for_email(make_ledger()) == ""

    # --- the no-gate guarantee ---

    def test_watchlist_never_blocks_or_creates_a_trade(self):
        ledger = self._two_obs_ledger()
        pre_val = {"total_value_gbp": 6000.0, "cash_gbp": 1000.0, "positions": {}}
        # a BUY of a name that was never watched must pass untouched
        recs = [gbp_buy_rec("NEVERWATCHED", 500, theme="software")]
        allowed, events = ta.enforce_strategy_guards(recs, ledger, pre_val)
        assert allowed == recs
        assert not any("watchlist" in e.lower() for e in events)
        # and a tracked name generates no recs of its own
        allowed, events = ta.enforce_strategy_guards([], ledger, pre_val)
        assert allowed == []
        assert not any("watchlist" in e.lower() for e in events)

    def test_extract_watchlist_from_recommendations_block(self):
        text = (
            'prose\n```json\n'
            '{"recommendations": [], "watchlist": '
            '[{"ticker": "ZTS", "yfinance_ticker": "ZTS"}]}\n```'
        )
        assert ta.extract_watchlist(text) == [
            {"ticker": "ZTS", "yfinance_ticker": "ZTS"}]

    def test_extract_watchlist_missing_is_empty_not_an_error(self):
        assert ta.extract_watchlist('```json\n{"recommendations": []}\n```') == []
        assert ta.extract_watchlist("no json here") == []

    def test_extract_watchlist_ignores_echoed_example_block(self):
        text = (
            '```json\n{"recommendations": [], "watchlist": '
            '[{"ticker": "EXAMPLE"}]}\n```\n'
            'real one:\n```json\n{"recommendations": [], "watchlist": '
            '[{"ticker": "REAL"}]}\n```'
        )
        assert ta.extract_watchlist(text) == [{"ticker": "REAL"}]

    def test_recommendations_still_extracted_alongside_watchlist(self):
        text = (
            '```json\n{"recommendations": [{"action": "BUY", "ticker": "X"}], '
            '"watchlist": [{"ticker": "ZTS"}]}\n```'
        )
        assert ta.extract_recommendations(text) == [{"action": "BUY", "ticker": "X"}]
        assert ta.extract_watchlist(text) == [{"ticker": "ZTS"}]


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

    def test_sync_remove_drops_phantom_lots(self):
        # The August 2026 META bug: buys T212 rejected stayed in the replay and
        # blended into the basis of the real position bought later.
        ledger = {"positions": {}, "trades": [
            {"action": "BUY",         "ticker": "M", "shares": 10, "amount_gbp": 1000},
            {"action": "SYNC_REMOVE", "ticker": "M"},
            {"action": "BUY",         "ticker": "M", "shares": 10, "amount_gbp": 500},
            {"action": "SELL",        "ticker": "M", "shares": 10, "amount_gbp": 450},
        ]}
        result = sp.compute_realized_pnl(ledger)
        # Priced against the £500 lot that really existed, not the £750 blend.
        assert result["by_ticker"]["M"] == pytest.approx(-50.0)
        assert result["tickers_with_incomplete_basis"] == []

    def test_sync_reset_clears_replay_without_flagging_later_buys(self):
        ledger = {"positions": {}, "trades": [
            {"action": "BUY",        "ticker": "X", "shares": 10, "amount_gbp": 900},
            {"action": "SYNC_RESET", "ticker": "-"},
            {"action": "BUY",        "ticker": "X", "shares": 10, "amount_gbp": 100},
            {"action": "SELL",       "ticker": "X", "shares": 10, "amount_gbp": 150},
        ]}
        result = sp.compute_realized_pnl(ledger)
        assert result["by_ticker"]["X"] == pytest.approx(50.0)
        assert result["tickers_with_incomplete_basis"] == []

    def test_sync_add_seeds_basis_from_t212_cost(self):
        ledger = {"positions": {}, "trades": [
            {"action": "SYNC_ADD", "ticker": "X", "shares": 10,
             "avg_cost_gbp": 10.0, "basis_source": "t212_wallet"},
            {"action": "SELL",     "ticker": "X", "shares": 10, "amount_gbp": 150},
        ]}
        result = sp.compute_realized_pnl(ledger)
        assert result["by_ticker"]["X"] == pytest.approx(50.0)
        assert result["tickers_with_estimated_basis"] == []

    def test_sync_add_from_market_price_is_flagged_estimated(self):
        ledger = {"positions": {}, "trades": [
            {"action": "SYNC_ADD", "ticker": "X", "shares": 10,
             "avg_cost_gbp": 10.0, "basis_source": "market_price"},
            {"action": "SELL",     "ticker": "X", "shares": 10, "amount_gbp": 150},
        ]}
        result = sp.compute_realized_pnl(ledger)
        assert result["tickers_with_estimated_basis"] == ["X"]

    def test_unmatched_sell_falls_back_to_position_avg_cost(self):
        # DELL: trims exceeded logged buys because sync seeded shares silently.
        ledger = {
            "positions": {"D": {"shares": 2, "avg_cost_gbp": 100.0}},
            "trades": [
                {"action": "TRIM", "ticker": "D", "shares": 5, "amount_gbp": 750},
            ],
        }
        result = sp.compute_realized_pnl(ledger)
        assert result["by_ticker"]["D"] == pytest.approx(250.0)
        assert result["tickers_with_estimated_basis"] == ["D"]
        assert result["tickers_with_incomplete_basis"] == []

    def test_unpriceable_sell_reports_proceeds(self):
        ledger = {"positions": {}, "trades": [
            {"action": "SELL", "ticker": "Z", "shares": 5, "amount_gbp": 60},
        ]}
        result = sp.compute_realized_pnl(ledger)
        assert result["tickers_with_incomplete_basis"] == ["Z"]
        assert result["unpriced_proceeds_gbp"]["Z"] == pytest.approx(60.0)
        assert result["total_gbp"] == 0.0


class TestTrimTriggerParsing:
    """
    DELL's alert was dead: the regex read a superseded level out of bracketed
    commentary, and honoured-level counting started at first_bought when the
    levels were set three months later.
    """

    def test_bracketed_commentary_is_not_a_trigger(self):
        text = ("Trim 1/3 at +110% from entry (~£335/share) [EXECUTED THIS RUN]; "
                "trim another 1/3 at +130% from entry (~£367/share) "
                "[tightened from prior +150%/~£399 - old level was 17.4% above]")
        assert ta._parse_trim_triggers(text) == [110.0, 130.0]

    def test_plain_levels_still_parse(self):
        assert ta._parse_trim_triggers(
            "Trim 1/3 at +40%, trim another 1/3 at +80%.") == [40.0, 80.0]

    def test_honoured_count_starts_at_first_set_trims(self):
        pos = {"first_bought": "2026-04-26",
               "pre_commit_trims": "Trim 1/3 at +110%, another 1/3 at +130%."}
        ledger = {"positions": {"DELL": pos}, "trades": [
            # Four cap-breach trims BEFORE any levels existed.
            {"action": "TRIM", "ticker": "DELL", "date": "2026-05-10"},
            {"action": "TRIM", "ticker": "DELL", "date": "2026-05-26"},
            {"action": "TRIM", "ticker": "DELL", "date": "2026-06-01"},
            {"action": "TRIM", "ticker": "DELL", "date": "2026-06-08"},
            {"action": "SET_TRIMS", "ticker": "DELL", "date": "2026-07-27"},
            {"action": "TRIM", "ticker": "DELL", "date": "2026-08-10"},
        ]}
        triggers, done = ta._trim_triggers_and_honoured(ledger, "DELL", pos)
        assert triggers == [110.0, 130.0]
        assert done == 1                      # not 5
        assert len(triggers) > done           # the alert can fire at all

    def test_restating_levels_does_not_reset_the_count(self):
        pos = {"first_bought": "2026-04-26",
               "pre_commit_trims": "Trim 1/3 at +110%, another 1/3 at +130%."}
        ledger = {"positions": {"DELL": pos}, "trades": [
            {"action": "SET_TRIMS", "ticker": "DELL", "date": "2026-07-27"},
            {"action": "TRIM",      "ticker": "DELL", "date": "2026-08-10"},
            {"action": "SET_TRIMS", "ticker": "DELL", "date": "2026-08-31"},
        ]}
        _, done = ta._trim_triggers_and_honoured(ledger, "DELL", pos)
        assert done == 1


class TestForwardDriverFailure:
    """
    A forward driver is the whole basis for holding a played-out position, so a
    driver that FAILED is a thesis break. Before Aug 2026 nothing distinguished
    a failed driver from a superseded one, and swapping in a fresh
    justification was free.
    """

    def _played_out(self):
        return {"shares": 10, "avg_cost_gbp": 100.0, "first_bought": "2026-04-26",
                "thesis_played_out": True, "forward_driver": "Old driver.",
                "forward_driver_set": "2026-07-27",
                "forward_driver_history": [
                    {"date": "2026-07-27", "driver": "Old driver."}]}

    def test_failed_driver_records_break_and_makes_bank_due(self):
        ledger = {"positions": {"X": self._played_out()}, "trades": [
            {"action": "TRIM", "ticker": "X", "date": "2026-08-10"},
        ]}
        sp._apply_set_driver(ledger, {
            "forward_driver": "New driver.",
            "previous_driver_status": "failed",
        }, "X", "2026-08-31")
        pos = ledger["positions"]["X"]
        assert pos["driver_failed_on"] == "2026-08-31"
        # Trim on 10 Aug predates the failure, so a fresh bank is owed now —
        # not in 12 weeks.
        assert sp.played_out_bank_due(ledger, "X", pos) is True

    def test_superseded_driver_leaves_the_twelve_week_clock_alone(self):
        ledger = {"positions": {"X": self._played_out()}, "trades": [
            {"action": "TRIM", "ticker": "X", "date": "2026-08-10"},
        ]}
        sp._apply_set_driver(ledger, {
            "forward_driver": "New driver.",
            "previous_driver_status": "superseded",
        }, "X", "2026-08-31")
        pos = ledger["positions"]["X"]
        assert "driver_failed_on" not in pos
        assert sp.played_out_bank_due(ledger, "X", pos) is False

    def test_unstated_status_is_treated_as_failed(self):
        ledger = {"positions": {"X": self._played_out()}, "trades": [
            {"action": "TRIM", "ticker": "X", "date": "2026-08-10"},
        ]}
        sp._apply_set_driver(ledger, {"forward_driver": "New driver."},
                             "X", "2026-08-31")
        pos = ledger["positions"]["X"]
        assert pos["forward_driver_history"][-1]["previous_driver_status"] == "unstated"
        assert sp.played_out_bank_due(ledger, "X", pos) is True

    def test_first_driver_needs_no_status(self):
        ledger = {"positions": {"X": {"shares": 10, "avg_cost_gbp": 100.0,
                                      "first_bought": "2026-04-26"}},
                  "trades": []}
        sp._apply_set_driver(ledger, {"forward_driver": "First driver."},
                             "X", "2026-07-27")
        pos = ledger["positions"]["X"]
        assert "driver_failed_on" not in pos
        assert "previous_driver_status" not in pos["forward_driver_history"][-1]


class TestUndrivenPlayedOutSell:
    """
    The forward driver IS the reason to hold a realized winner. The prompt
    always said a played-out position needs a driver or a trim/sell, but the
    guards read JSON recs and the judgement lived in prose — so "played out
    with no driver" was invisible and the position carried on as a plain HOLD.
    """

    def _ledger(self, driver=None):
        pos = {"shares": 10, "avg_cost_gbp": 100.0, "first_bought": "2026-04-26"}
        if driver:
            pos.update(thesis_played_out=True, forward_driver=driver,
                       forward_driver_set="2026-07-27")
        return {"positions": {"X": pos}, "trades": []}

    def _val(self):
        return {"total_value_gbp": 6000.0,
                "positions": {"X": {"pnl_pct": 90.0, "current_value_gbp": 1200.0}}}

    def _declared(self):
        return [{"ticker": "X", "yfinance_ticker": "X", "reason": "re-rating done"}]

    def _forced(self, recs, played_out, ledger=None):
        out, _ = ta._inject_undriven_played_out_sells(
            recs, ledger or self._ledger(), self._val(), played_out)
        return [(r["action"], r["ticker"]) for r in out if r.get("guard_generated")]

    def test_no_driver_no_trim_forces_a_full_sell(self):
        assert self._forced([], self._declared()) == [("SELL", "X")]

    def test_set_driver_this_run_prevents_the_sell(self):
        recs = [{"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
                 "forward_driver": "New case."}]
        assert self._forced(recs, self._declared()) == []

    def test_claude_trimming_it_prevents_the_sell(self):
        recs = [{"action": "TRIM", "ticker": "X", "yfinance_ticker": "X",
                 "trim_pct": 33}]
        assert self._forced(recs, self._declared()) == []

    def test_existing_driver_on_record_is_left_alone(self):
        # Re-confirming an unchanged driver is legitimate (option (a)) and is
        # covered by the weekly confirm/replace/trim loop, not by this rule.
        assert self._forced([], self._declared(),
                            self._ledger(driver="Backlog converts.")) == []

    def test_position_not_held_is_ignored(self):
        assert self._forced([], [{"ticker": "TSLA", "yfinance_ticker": "TSLA"}]) == []

    def test_nothing_declared_forces_nothing(self):
        assert self._forced([], []) == []

    def test_bare_ticker_strings_are_accepted(self):
        assert self._forced([], ["X"]) == [("SELL", "X")]

    def test_missing_price_alerts_instead_of_selling(self):
        out, events = ta._inject_undriven_played_out_sells(
            [], self._ledger(), {"positions": {}}, self._declared())
        assert [r for r in out if r.get("guard_generated")] == []
        assert any("no live price" in e for e in events)

    def test_extracted_from_the_recommendations_block(self):
        text = '''```json
{"recommendations": [], "played_out": [{"ticker": "X", "reason": "done"}]}
```'''
        assert ta.extract_played_out(text) == [{"ticker": "X", "reason": "done"}]

    def test_absent_played_out_array_is_not_an_error(self):
        assert ta.extract_played_out('```json\n{"recommendations": []}\n```') == []


class TestDriverFailureBanksSameRun:
    """
    _apply_set_driver writes driver_failed_on at EXECUTION, which is after the
    guards run — so reading the position alone would fire the bank a week late,
    which is the delay the failed-driver rule exists to remove. The guard reads
    this run's recs instead.
    """

    def _ledger(self):
        return {"positions": {"X": {
            "shares": 10, "avg_cost_gbp": 100.0, "first_bought": "2026-04-26",
            "thesis_played_out": True, "forward_driver": "Old driver.",
            "forward_driver_set": "2026-07-27",
            "played_out_peak_gain_pct": 100.0,
            "played_out_peak_date": "2026-08-10",
        }}, "trades": [{"action": "TRIM", "ticker": "X", "date": "2026-08-10"}]}

    def _val(self):
        return {"positions": {"X": {"pnl_pct": 100.0, "current_value_gbp": 600.0}}}

    def _driver(self, status=None):
        rec = {"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
               "forward_driver": "New driver."}
        if status:
            rec["previous_driver_status"] = status
        return [rec]

    def _injected(self, recs):
        out, _ = ta._inject_played_out_banks(recs, self._ledger(), self._val())
        return [r for r in out if r.get("guard_generated")]

    def test_failed_driver_banks_in_the_same_run(self):
        assert len(self._injected(self._driver("failed"))) == 1

    def test_omitted_status_banks_in_the_same_run(self):
        assert len(self._injected(self._driver())) == 1

    def test_superseded_driver_banks_at_the_reduced_rate(self):
        # "superseded" used to cost nothing, which made the label a free
        # option — taken on DELL the first week it existed. It now costs less
        # than "failed", never nothing.
        injected = self._injected(self._driver("superseded"))
        assert len(injected) == 1
        assert injected[0]["trim_pct"] == ta.PLAYED_OUT_SUPERSEDE_TRIM_PCT

    def test_failed_driver_costs_more_than_superseded(self):
        failed = self._injected(self._driver("failed"))[0]["trim_pct"]
        superseded = self._injected(self._driver("superseded"))[0]["trim_pct"]
        assert failed == ta.PLAYED_OUT_BANK_TRIM_PCT
        assert failed > superseded

    def test_claude_selling_it_itself_pre_empts_the_injection(self):
        recs = self._driver("failed") + [
            {"action": "SELL", "ticker": "X", "yfinance_ticker": "X"}]
        assert self._injected(recs) == []

    def test_first_driver_on_a_played_out_position_does_not_bank(self):
        # No previous driver to have failed — nothing to declare.
        ledger = self._ledger()
        ledger["positions"]["X"].pop("forward_driver")
        out, _ = ta._inject_played_out_banks(
            self._driver(), ledger, self._val())
        assert [r for r in out if r.get("guard_generated")] == []

    def test_unchanged_driver_text_is_not_a_failure(self):
        recs = [{"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
                 "forward_driver": "Old driver."}]
        assert self._injected(recs) == []


class TestDriverChurnEscalation:
    """
    The COUNT of drivers named for one position is itself the signal: a hold
    re-argued from scratch every few weeks is carried by churn, not by a claim.
    DELL reached driver #3 in five weeks, each "confirmed with fresh evidence".
    The count bites whatever the label says.
    """

    def _ledger(self, history_len):
        history = [{"date": "2026-07-27", "driver": f"d{i}"}
                   for i in range(history_len)]
        return {"positions": {"X": {
            "shares": 10, "avg_cost_gbp": 100.0, "first_bought": "2026-04-26",
            "thesis_played_out": True, "forward_driver": "Old driver.",
            "forward_driver_set": "2026-08-24",
            "forward_driver_history": history,
        }}, "trades": [{"action": "TRIM", "ticker": "X", "date": "2026-08-24"}]}

    def _val(self):
        return {"positions": {"X": {"pnl_pct": 110.0,
                                    "current_value_gbp": 686.84}}}

    def _injected(self, history_len, status="superseded"):
        recs = [{"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
                 "forward_driver": "New driver.",
                 "previous_driver_status": status}]
        out, events = ta._inject_played_out_banks(
            recs, self._ledger(history_len), self._val())
        return [r for r in out if r.get("guard_generated")], events

    def test_second_driver_banks_at_the_supersede_rate(self):
        injected, _ = self._injected(1)
        assert injected[0]["trim_pct"] == ta.PLAYED_OUT_SUPERSEDE_TRIM_PCT

    def test_third_driver_banks_the_full_third_despite_superseded(self):
        injected, _ = self._injected(2)
        assert injected[0]["action"] == "TRIM"
        assert injected[0]["trim_pct"] == ta.PLAYED_OUT_BANK_TRIM_PCT

    def test_fourth_driver_exits_the_position(self):
        injected, events = self._injected(3)
        assert injected[0]["action"] == "SELL"
        assert any("FORCED SELL X" in e for e in events)

    def test_legacy_driver_with_no_history_counts_as_the_first(self):
        # A position can carry a driver predating forward_driver_history; the
        # driver on record is #1, so the replacement is #2, not #1.
        ledger = self._ledger(0)
        recs = [{"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
                 "forward_driver": "New driver.",
                 "previous_driver_status": "superseded"}]
        out, _ = ta._inject_played_out_banks(recs, ledger, self._val())
        injected = [r for r in out if r.get("guard_generated")]
        assert injected[0]["trim_pct"] == ta.PLAYED_OUT_SUPERSEDE_TRIM_PCT

    def test_claude_acting_itself_still_pre_empts_the_escalation(self):
        recs = [{"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
                 "forward_driver": "New driver.",
                 "previous_driver_status": "superseded"},
                {"action": "TRIM", "ticker": "X", "yfinance_ticker": "X",
                 "trim_pct": 40}]
        out, _ = ta._inject_played_out_banks(recs, self._ledger(3), self._val())
        assert [r for r in out if r.get("guard_generated")] == []

    def test_churn_alert_counts_the_driver_named_this_run(self):
        recs = [{"action": "SET_DRIVER", "ticker": "X", "yfinance_ticker": "X",
                 "forward_driver": "New driver.",
                 "previous_driver_status": "superseded"}]
        alerts = ta._forward_driver_alerts(recs, self._ledger(2))
        assert any("3 different forward drivers" in a for a in alerts)


class TestPlayedOutGiveback:
    """
    Trim levels are gains from ENTRY, so on a big winner they sit above the
    price and only fire on a rally. A played-out winner sliding back down
    passed no level and broke no thesis — the scenario kill criterion #2
    describes, with no rule acting on it.
    """

    def _ledger(self, peak, peak_date="2026-08-10", last_trim="2026-08-03"):
        pos = {"shares": 10, "avg_cost_gbp": 100.0, "first_bought": "2026-04-26",
               "thesis_played_out": True, "forward_driver": "d",
               "forward_driver_set": "2026-07-27",
               "played_out_peak_gain_pct": peak,
               "played_out_peak_date": peak_date}
        return {"positions": {"X": pos}, "trades": [
            {"action": "TRIM", "ticker": "X", "date": last_trim},
        ]}

    def test_giveback_percentage(self):
        assert sp.played_out_giveback_pct({"played_out_peak_gain_pct": 100.0},
                                          40.0) == pytest.approx(60.0)
        assert sp.played_out_giveback_pct({"played_out_peak_gain_pct": 100.0},
                                          100.0) == pytest.approx(0.0)

    def test_bank_due_once_giveback_threshold_crossed(self):
        ledger = self._ledger(peak=100.0)
        pos = ledger["positions"]["X"]
        # 80% gain = 20% of the peak handed back — under the 25% threshold.
        assert sp.played_out_bank_due(ledger, "X", pos, 80.0) is False
        # 70% gain = 30% handed back.
        assert sp.played_out_bank_due(ledger, "X", pos, 70.0) is True

    def test_bank_taken_after_the_peak_clears_the_obligation(self):
        ledger = self._ledger(peak=100.0, peak_date="2026-08-10",
                              last_trim="2026-08-17")
        pos = ledger["positions"]["X"]
        assert sp.played_out_bank_due(ledger, "X", pos, 70.0) is False

    def test_small_winner_is_below_the_peak_floor(self):
        # Peak +20%: a quarter of that gain is a 4.2% price fall, which is
        # noise. The twelve-week clock is the only mechanism down here.
        ledger = self._ledger(peak=20.0)
        pos = ledger["positions"]["X"]
        assert sp.played_out_giveback_pct(pos, 10.0) == pytest.approx(50.0)
        assert sp.played_out_giveback_due(pos, 10.0) is False
        assert sp.played_out_bank_due(ledger, "X", pos, 10.0) is False

    def test_peak_at_the_floor_is_included(self):
        ledger = self._ledger(peak=sp.PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT)
        pos = ledger["positions"]["X"]
        floor = sp.PLAYED_OUT_GIVEBACK_MIN_PEAK_PCT
        assert sp.played_out_giveback_due(pos, floor * 0.7) is True

    def test_trim_on_the_peak_date_does_not_clear_the_obligation(self):
        # DELL's peak is seeded from its 2026-08-10 trim, so peak date and
        # last-bank date are the same day. That trim happened at the top —
        # it cannot count as banking a giveback that came later.
        ledger = self._ledger(peak=113.6, peak_date="2026-08-10",
                              last_trim="2026-08-10")
        pos = ledger["positions"]["X"]
        assert sp.played_out_bank_due(ledger, "X", pos, 80.0) is True

    def test_peak_seeded_from_trade_history(self):
        ledger = {
            "positions": {"X": {
                "avg_cost_gbp": 100.0, "thesis_played_out": True,
                "forward_driver_set": "2026-07-27",
            }},
            "trades": [
                {"action": "TRIM", "ticker": "X", "date": "2026-07-01",
                 "price_gbp": 300.0},          # before declaration, ignored
                {"action": "TRIM", "ticker": "X", "date": "2026-08-10",
                 "price_gbp": 213.6},
            ],
        }
        sp.update_played_out_peaks(
            ledger, {"positions": {"X": {"pnl_pct": 100.0}}}, "2026-08-31")
        pos = ledger["positions"]["X"]
        assert pos["played_out_peak_gain_pct"] == pytest.approx(113.6)
        assert pos["played_out_peak_date"] == "2026-08-10"

    def test_peak_ratchets_up_only(self):
        ledger = {"positions": {"X": {"thesis_played_out": True}}}
        val = {"positions": {"X": {"pnl_pct": 100.0}}}
        sp.update_played_out_peaks(ledger, val, "2026-08-24")
        assert ledger["positions"]["X"]["played_out_peak_gain_pct"] == 100.0

        val = {"positions": {"X": {"pnl_pct": 60.0}}}
        sp.update_played_out_peaks(ledger, val, "2026-08-31")
        assert ledger["positions"]["X"]["played_out_peak_gain_pct"] == 100.0
        assert ledger["positions"]["X"]["played_out_peak_date"] == "2026-08-24"

    def test_positions_not_played_out_get_no_peak(self):
        ledger = {"positions": {"X": {}}}
        sp.update_played_out_peaks(ledger, {"positions": {"X": {"pnl_pct": 50.0}}},
                                   "2026-08-24")
        assert "played_out_peak_gain_pct" not in ledger["positions"]["X"]

    def test_missing_price_does_not_trigger_a_bank(self):
        ledger = self._ledger(peak=100.0)
        pos = ledger["positions"]["X"]
        assert sp.played_out_bank_due(ledger, "X", pos, None) is False


class TestTradeLogReconciliation:
    def test_clean_log_reconciles(self):
        ledger = {
            "starting_capital_gbp": 1000,
            "cash_gbp": 900,
            "positions": {"X": {"shares": 10}},
            "trades": [
                {"action": "BUY", "ticker": "X", "shares": 10, "amount_gbp": 100},
            ],
        }
        r = sp.reconcile_trade_log(ledger)
        assert r["clean"] is True
        assert r["cash_diff_gbp"] == pytest.approx(0.0)

    def test_drift_detected(self):
        ledger = {
            "starting_capital_gbp": 1000,
            "cash_gbp": 900,
            "positions": {"X": {"shares": 4}},
            "trades": [
                {"action": "BUY", "ticker": "X", "shares": 10, "amount_gbp": 100},
            ],
        }
        r = sp.reconcile_trade_log(ledger)
        assert r["clean"] is False
        assert r["drifts"]["X"]["diff"] == pytest.approx(6.0)

    def test_baseline_seeds_shares_and_cash(self):
        ledger = {
            "starting_capital_gbp": 1000,
            "cash_gbp": 500,
            "positions": {"X": {"shares": 10}},
            "trades": [
                {"action": "BUY", "ticker": "Q", "shares": 99, "amount_gbp": 9999},
                {"action": "SYNC_BASELINE", "ticker": "-",
                 "positions": {"X": 10}, "cash_gbp": 500},
            ],
        }
        r = sp.reconcile_trade_log(ledger)
        assert r["clean"] is True
        assert r["cash_diff_gbp"] == pytest.approx(0.0)


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


class TestSyncCostRebase:
    """
    Shadow books a BUY at the price it saw; T212 fills at market, often the
    next open. By Sep 2026 MRVL was carried at 145.67 against a 154.53 fill
    (-5.7%), so every "+N% from entry" mechanism measured from the wrong line.
    Held positions are now re-based to T212's walletImpact cost on sync.
    """

    def _ledger(self, avg=145.67, thesis="MRVL case"):
        ledger = make_ledger(cash_gbp=500.0)
        ledger["positions"] = {
            "AAPL": {"shares": 2.0, "avg_cost_gbp": avg, "first_bought": "2026-05-26",
                     "thesis": thesis, "pre_commit_trims": "Trim 1/3 at +45%."},
        }
        ledger["trades"].append({
            "date": "2026-05-26", "action": "BUY", "ticker": "AAPL",
            "shares": 2.0, "price_gbp": avg, "amount_gbp": 2 * avg,
        })
        return ledger

    def test_rebases_to_t212_wallet_cost_and_logs_it(self):
        ledger = self._ledger()
        changed = sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 2.0, 309.06)],
            _t212_to_yf, bidirectional=True)
        assert changed
        pos = ledger["positions"]["AAPL"]
        assert pos["avg_cost_gbp"] == pytest.approx(154.53)
        # everything else on the position survives
        assert pos["thesis"] == "MRVL case"
        assert pos["pre_commit_trims"] == "Trim 1/3 at +45%."
        assert pos["first_bought"] == "2026-05-26"
        rec = [t for t in ledger["trades"] if t["action"] == "SYNC_COST"]
        assert len(rec) == 1
        assert rec[0]["ticker"] == "AAPL"
        assert rec[0]["avg_cost_gbp"] == pytest.approx(154.53)
        assert rec[0]["was_avg_cost_gbp"] == pytest.approx(145.67)
        summary = ledger["trades"][-1]
        assert summary["action"] == "SYNC_FROM_T212"
        assert "cost re-based ['AAPL']" in summary["note"]

    def test_within_tolerance_is_left_alone(self):
        ledger = self._ledger(avg=100.0)
        changed = sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 2.0, 200.1)],
            _t212_to_yf, bidirectional=True)
        assert not changed
        assert ledger["positions"]["AAPL"]["avg_cost_gbp"] == 100.0
        assert not any(t["action"] == "SYNC_COST" for t in ledger["trades"])

    def test_shadow_only_mode_never_rebases(self):
        ledger = self._ledger()
        changed = sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 2.0, 309.06)],
            _t212_to_yf, bidirectional=False)
        assert not changed
        assert ledger["positions"]["AAPL"]["avg_cost_gbp"] == 145.67

    def test_no_wallet_cost_means_no_rebase(self):
        # averagePricePaid alone needs an FX conversion; not trusted for this.
        ledger = self._ledger()
        t212 = {"ticker": "AAPL_US_EQ", "quantity": 2.0,
                "averagePricePaid": 200.0, "walletImpact": {}}
        changed = sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212], _t212_to_yf, bidirectional=True)
        assert not changed
        assert ledger["positions"]["AAPL"]["avg_cost_gbp"] == 145.67

    def test_realized_pnl_replay_honours_the_rebase(self):
        ledger = self._ledger()
        sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 2.0, 309.06)],
            _t212_to_yf, bidirectional=True)
        ledger["trades"].append({
            "date": "2026-10-01", "action": "TRIM", "ticker": "AAPL",
            "shares": 1.0, "price_gbp": 200.0, "amount_gbp": 200.0,
        })
        ledger["positions"]["AAPL"]["shares"] = 1.0
        out = sp.compute_realized_pnl(ledger)
        # 200 proceeds against the REAL 154.53 basis, not the 145.67 guess
        assert out["by_ticker"]["AAPL"] == pytest.approx(200.0 - 154.53, abs=0.01)
        assert "AAPL" not in out["tickers_with_estimated_basis"]
        assert sp.reconcile_trade_log(ledger)["clean"]

    def test_p_and_l_pct_follows_the_new_basis(self, monkeypatch):
        ledger = self._ledger()
        sp.sync_from_t212(
            ledger, {"free": 500.0}, [t212_pos("AAPL_US_EQ", 2.0, 309.06)],
            _t212_to_yf, bidirectional=True)
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda *a, **k: 100.0)
        val = sp.valuation(
            ledger, {"AAPL": {"price_native": 163.61, "currency": "GBP"}})
        assert val["positions"]["AAPL"]["pnl_pct"] == pytest.approx(5.88, abs=0.01)


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

    def test_weekly_prompt_includes_watchlist_review(self):
        ledger = self._themed_ledger()
        sp.record_watchlist(ledger, [{"ticker": "ZTS", "yfinance_ticker": "ZTS",
                                      "thesis_oneline": "Cheap animal health."}],
                            "2026-08-17", price_fn=lambda t: 100.0,
                            benchmark_return_pct=6.0)
        _, user = prompts.build_prompt(
            self._fake_val(ledger), ledger, {"free": 550.0, "total": 6000.0}, [])
        assert "Watchlist accountability" in user
        assert "ZTS" in user

    def test_weekly_prompt_omits_watchlist_when_empty(self):
        ledger = self._themed_ledger()
        _, user = prompts.build_prompt(
            self._fake_val(ledger), ledger, {"free": 550.0, "total": 6000.0}, [])
        assert "Watchlist accountability" not in user

    def test_deep_review_demands_kill_verdict_reconciliation(self):
        system, _ = prompts.build_deep_review_prompt(
            self._themed_ledger(), self._fake_val(self._themed_ledger()))
        assert "must not contradict" in system
        assert "IDEA GENERATION" in system
        assert "DEPLOYMENT/CONSTRAINTS" in system

    def test_deep_review_carries_watchlist_evidence(self):
        ledger = self._themed_ledger()
        sp.record_watchlist(ledger, [{"ticker": "ZTS", "yfinance_ticker": "ZTS",
                                      "thesis_oneline": "Cheap animal health."}],
                            "2026-08-17", price_fn=lambda t: 100.0,
                            benchmark_return_pct=6.0)
        _, user = prompts.build_deep_review_prompt(ledger, self._fake_val(ledger))
        assert "ideas flagged but NOT bought" in user
        assert "ZTS" in user

    def test_deep_review_handles_empty_watchlist(self):
        ledger = self._themed_ledger()
        _, user = prompts.build_deep_review_prompt(ledger, self._fake_val(ledger))
        assert "no watchlist names tracked yet" in user

    def test_deep_review_strips_raw_watchlist_observations(self):
        ledger = self._themed_ledger()
        sp.record_watchlist(ledger, [{"ticker": "ZTS", "yfinance_ticker": "ZTS"}],
                            "2026-08-17", price_fn=lambda t: 100.0)
        _, user = prompts.build_deep_review_prompt(ledger, self._fake_val(ledger))
        ledger_section = user.split("=== Weekly snapshots")[0]
        assert "observations" not in ledger_section

    def test_dead_zone_rule_keys_off_the_deployable_slice(self):
        system, _ = prompts.build_prompt(
            self._fake_val(self._themed_ledger()), self._themed_ledger(),
            {"free": 550.0}, [])
        assert "DEPLOYABLE SLICE" in system


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


class TestCurrencyDecomposition:
    """
    Every holding is priced in GBP, so its reported P&L blends what the business
    did with what sterling did. Nothing separated them, which let a loss be
    explained away as "a GBP FX artefact" with no number attached — done on
    2026-09-01 for the three red AI names while the two largest real FX drags
    sat on XOM and JPM, described in the same report as unqualified winners.
    """

    def _ledger(self, **over):
        pos = {"shares": 10, "avg_cost_gbp": 100.0, "first_bought": "2026-04-26",
               "fx_at_entry": 1.3466, "fx_basis": "estimated"}
        pos.update(over)
        return {"positions": {"AMZN": pos}, "trades": [], "cash_gbp": 1000.0}

    def test_currency_from_ticker_suffix(self):
        assert sp.position_currency("AMZN") == "USD"
        assert sp.position_currency("SHEL.L") == "GBP"
        assert sp.position_currency("ASML.AS") == "EUR"

    def test_gbp_return_splits_into_business_and_currency(self, monkeypatch):
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.3530)
        out = sp.fx_neutral_returns(
            self._ledger(), {"positions": {"AMZN": {"pnl_pct": -3.59}}})["AMZN"]
        # Sterling barely moved: 1.3466 -> 1.3530 is 0.5%, so almost the whole
        # loss is the business, not the currency.
        assert out["local_pct"] == pytest.approx(-3.15, abs=0.05)
        assert out["fx_pts"] == pytest.approx(-0.44, abs=0.05)

    def test_fx_contribution_is_gbp_minus_native(self, monkeypatch):
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.3530)
        out = sp.fx_neutral_returns(
            self._ledger(), {"positions": {"AMZN": {"pnl_pct": -3.59}}})["AMZN"]
        assert out["gbp_pct"] - out["local_pct"] == pytest.approx(out["fx_pts"])

    def test_sterling_weakness_flatters_the_gbp_return(self, monkeypatch):
        # fx_now BELOW fx_at_entry means sterling fell: a USD asset translates
        # into MORE pounds, so the GBP return overstates the business.
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.2000)
        out = sp.fx_neutral_returns(
            self._ledger(), {"positions": {"AMZN": {"pnl_pct": 10.0}}})["AMZN"]
        assert out["local_pct"] < out["gbp_pct"]
        assert out["fx_pts"] > 0

    def test_gbp_listed_position_has_no_currency_effect(self, monkeypatch):
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.3530)
        ledger = {"positions": {"SHEL.L": {
            "shares": 10, "avg_cost_gbp": 25.0, "first_bought": "2026-04-26",
            "fx_at_entry": 1.0, "fx_basis": "none"}}}
        out = sp.fx_neutral_returns(
            ledger, {"positions": {"SHEL.L": {"pnl_pct": 8.0}}})["SHEL.L"]
        assert out["fx_pts"] == pytest.approx(0.0)

    def test_position_with_no_entry_rate_is_omitted_not_guessed(self, monkeypatch):
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.3530)
        ledger = self._ledger()
        ledger["positions"]["AMZN"].pop("fx_at_entry")
        assert sp.fx_neutral_returns(
            ledger, {"positions": {"AMZN": {"pnl_pct": -3.59}}}) == {}

    def test_unavailable_live_rate_is_omitted_not_treated_as_no_effect(self, monkeypatch):
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: None)
        assert sp.fx_neutral_returns(
            self._ledger(), {"positions": {"AMZN": {"pnl_pct": -3.59}}}) == {}

    def test_backfill_is_idempotent_and_does_not_refetch(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sp, "fx_rate_on",
                            lambda pair, d: calls.append(d) or 1.3466)
        ledger = self._ledger()
        ledger["positions"]["AMZN"].pop("fx_at_entry")
        assert sp.ensure_entry_fx(ledger) != []
        assert ledger["positions"]["AMZN"]["fx_basis"] == "estimated"
        assert sp.ensure_entry_fx(ledger) == []      # already present
        assert len(calls) == 1

    def test_backfill_leaves_position_alone_when_rate_unavailable(self, monkeypatch):
        monkeypatch.setattr(sp, "fx_rate_on", lambda pair, d: None)
        ledger = self._ledger()
        ledger["positions"]["AMZN"].pop("fx_at_entry")
        sp.ensure_entry_fx(ledger)
        assert "fx_at_entry" not in ledger["positions"]["AMZN"]

    def test_new_buy_records_the_live_rate_as_actual(self, monkeypatch):
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 100.0)
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.3530)
        ledger = {"positions": {}, "trades": [], "cash_gbp": 1000.0}
        sp.apply_recommendations(ledger, [{
            "action": "BUY", "ticker": "NVDA", "yfinance_ticker": "NVDA",
            "amount_gbp": 500.0}], "2026-09-07")
        pos = ledger["positions"]["NVDA"]
        assert pos["fx_at_entry"] == pytest.approx(1.3530)
        assert pos["fx_basis"] == "actual"

    def test_top_up_blends_the_entry_rate_cost_weighted(self, monkeypatch):
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 100.0)
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.4000)
        ledger = self._ledger(fx_basis="actual")   # 10 sh @ £100 = £1000 cost
        ledger["cash_gbp"] = 1000.0
        sp.apply_recommendations(ledger, [{
            "action": "BUY", "ticker": "AMZN", "yfinance_ticker": "AMZN",
            "amount_gbp": 1000.0}], "2026-09-07")
        # Equal cost either side, so the blend sits midway.
        assert ledger["positions"]["AMZN"]["fx_at_entry"] == pytest.approx(
            (1.3466 + 1.4000) / 2)

    def test_estimated_leg_keeps_the_whole_basis_estimated(self, monkeypatch):
        monkeypatch.setattr(sp, "fetch_price_gbp", lambda t: 100.0)
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.4000)
        ledger = self._ledger()                    # fx_basis "estimated"
        ledger["cash_gbp"] = 1000.0
        sp.apply_recommendations(ledger, [{
            "action": "BUY", "ticker": "AMZN", "yfinance_ticker": "AMZN",
            "amount_gbp": 500.0}], "2026-09-07")
        assert ledger["positions"]["AMZN"]["fx_basis"] == "estimated"

    def test_prompt_review_warns_against_attributing_to_currency(self, monkeypatch):
        monkeypatch.setattr(sp, "_fx_rate", lambda pair: 1.3530)
        review = sp.build_fx_review(
            self._ledger(), {"positions": {"AMZN": {"pnl_pct": -3.59}}})
        assert "Currency decomposition" in review
        assert "can never explain why one position is down" in review

    def test_email_block_is_empty_when_nothing_can_be_decomposed(self):
        assert sp.format_fx_for_email({}) == ""


class TestExTopContributor:
    """
    Kill criterion #5 as a standing metric (Sep 2026 deep review).

    The reference numbers are the real 10 Sep 2026 book: +£1,333 total, of
    which DELL was ~£1,117 (£804 realised + £313 unrealised), leaving ~£216 on
    ~£4,600 of capital — ~4.7% against VUSA's +7.12%.
    """

    def _ledger(self):
        return {
            "starting_capital_gbp": 5000.0,
            "positions": {"DELL": {}, "XOM": {}},
            "trades": [
                {"action": "BUY",  "ticker": "DELL", "shares": 4, "amount_gbp": 400},
                {"action": "TRIM", "ticker": "DELL", "shares": 2, "amount_gbp": 1004},
                {"action": "BUY",  "ticker": "XOM",  "shares": 7, "amount_gbp": 700},
            ],
        }

    def _val(self, dell_unrealised=313.10, xom_unrealised=134.32):
        return {
            "starting_capital_gbp": 5000.0,
            "total_return_gbp": round(804.0 + dell_unrealised + xom_unrealised, 2),
            "benchmark_return_pct": 7.12,
            "positions": {
                "DELL": {"pnl_gbp": dell_unrealised, "current_value_gbp": 531.75},
                "XOM":  {"pnl_gbp": xom_unrealised,  "current_value_gbp": 834.32},
            },
        }

    def test_top_contributor_counts_realised_and_unrealised(self):
        ex = sp.ex_top_contributor_performance(self._ledger(), self._val())
        # DELL's unrealised (£313) is SMALLER than XOM's total, so an
        # unrealised-only ranking picks the wrong name — the bug this replaces.
        assert ex["top_ticker"] == "DELL"
        assert ex["top_pnl_gbp"] == pytest.approx(1117.1, abs=1.0)

    def test_remainder_is_scored_against_the_capital_it_had(self):
        ex = sp.ex_top_contributor_performance(self._ledger(), self._val())
        # DELL never had more than £400 of basis open at once.
        assert ex["capital_ex_top_gbp"] == pytest.approx(4600.0)
        assert ex["ex_top_pnl_gbp"] == pytest.approx(134.32, abs=1.0)
        assert ex["ex_top_return_pct"] == pytest.approx(2.9, abs=0.2)

    def test_failing_when_remainder_lags_and_no_second_idea(self):
        ex = sp.ex_top_contributor_performance(self._ledger(), self._val())
        assert ex["ex_top_vs_benchmark_pts"] < 0
        assert ex["beats_benchmark"] is False
        assert ex["has_second_idea"] is False        # XOM £134 < £150 bar
        assert ex["passing"] is False

    def test_a_single_second_idea_passes_the_test(self):
        ex = sp.ex_top_contributor_performance(
            self._ledger(), self._val(xom_unrealised=200.0))
        assert ex["has_second_idea"] is True
        assert ex["passing"] is True

    def test_remainder_beating_the_benchmark_passes_the_test(self):
        ex = sp.ex_top_contributor_performance(
            self._ledger(), self._val(xom_unrealised=400.0))
        assert ex["beats_benchmark"] is True
        assert ex["passing"] is True

    def test_peak_cost_is_the_most_basis_ever_open_not_what_is_left(self):
        peaks = sp.compute_realized_pnl(self._ledger())["peak_cost_gbp"]
        assert peaks["DELL"] == pytest.approx(400.0)   # not the £200 residual

    def test_email_and_prompt_blocks_carry_the_verdict_and_date(self):
        led, val = self._ledger(), self._val()
        email = sp.format_ex_top_for_email(led, val)
        prompt = sp.build_ex_top_review(led, val)
        for block in (email, prompt):
            assert "FAILING" in block
            assert sp.EX_TOP_TEST_DATE in block
            assert "DELL" in block
        assert "not evidence of stock-picking" in prompt

    def test_blocks_are_empty_when_nothing_can_be_scored(self):
        empty = {"starting_capital_gbp": 5000.0, "total_return_gbp": 0.0,
                 "positions": {}}
        assert sp.ex_top_contributor_performance({"trades": []}, empty) == {}
        assert sp.format_ex_top_for_email({"trades": []}, empty) == ""
        assert sp.build_ex_top_review({"trades": []}, empty) == ""

    def test_missing_benchmark_does_not_read_as_passing(self):
        val = self._val()
        val["benchmark_return_pct"] = None
        ex = sp.ex_top_contributor_performance(self._ledger(), val)
        assert ex["ex_top_vs_benchmark_pts"] is None
        assert ex["beats_benchmark"] is False


class TestRepeatTopUpAlert:
    """
    NVDA was added to four times and became the biggest position in the book
    while flat. Advisory, not blocking — a block would idle the deployable
    slice with no way to choose another destination.
    """

    def _ledger(self, last_buy):
        return {
            "positions": {"NVDA": {"theme": "AI infrastructure"}},
            "trades": [{"action": "BUY", "ticker": "NVDA",
                        "date": last_buy, "shares": 1, "amount_gbp": 259}],
        }

    def _pre_val(self):
        return {"total_value_gbp": 6333.0,
                "positions": {"NVDA": {"current_value_gbp": 1009.25}}}

    def test_top_up_inside_the_window_is_flagged(self):
        recs = [{"action": "BUY", "ticker": "NVDA", "amount_gbp": 259}]
        alerts = ta._repeat_topup_alerts(
            recs, self._ledger("2026-09-01"), self._pre_val())
        assert len(alerts) == 1
        assert "2026-09-01" in alerts[0] and "NVDA" in alerts[0]

    def test_top_up_outside_the_window_is_not_flagged(self):
        recs = [{"action": "BUY", "ticker": "NVDA", "amount_gbp": 259}]
        assert ta._repeat_topup_alerts(
            recs, self._ledger("2026-05-13"), self._pre_val()) == []

    def test_a_new_position_is_never_a_repeat_top_up(self):
        recs = [{"action": "BUY", "ticker": "LLY", "amount_gbp": 600}]
        assert ta._repeat_topup_alerts(
            recs, self._ledger("2026-09-01"), self._pre_val()) == []

    def test_the_alert_never_blocks_the_buy(self, monkeypatch):
        monkeypatch.setattr(ta, "_recent_full_exit_date", lambda *a: None)
        recs = [{"action": "BUY", "ticker": "NVDA", "amount_gbp": 259,
                 "theme": "AI infrastructure"}]
        led = self._ledger("2026-09-01")
        pre_val = {"total_value_gbp": 6333.0, "cash_gbp": 575.0,
                   "positions": {"NVDA": {"current_value_gbp": 1009.25}}}
        allowed, events = ta.enforce_strategy_guards(recs, led, pre_val)
        assert [r["ticker"] for r in allowed] == ["NVDA"]
        assert any("repeat" in e for e in events)


class TestTrimResetAlert:
    def _ledger(self, resets):
        return {"positions": {"DELL": {}},
                "trades": [{"action": "SET_TRIMS", "ticker": "DELL"}] * resets}

    def test_third_reset_is_flagged(self):
        recs = [{"action": "SET_TRIMS", "ticker": "DELL",
                 "pre_commit_trims": "Final 1/3 exit at +165%"}]
        alerts = ta._trim_reset_alerts(recs, self._ledger(2))
        assert len(alerts) == 1 and "#3" in alerts[0]

    def test_first_reset_is_not_flagged(self):
        recs = [{"action": "SET_TRIMS", "ticker": "DELL"}]
        assert ta._trim_reset_alerts(recs, self._ledger(0)) == []

    def test_alert_does_not_block_the_set_trims(self):
        recs = [{"action": "SET_TRIMS", "ticker": "DELL",
                 "pre_commit_trims": "Final 1/3 exit at +165% from entry"}]
        led = self._ledger(3)
        led["positions"]["DELL"] = {}     # no existing levels to compare
        allowed, events = ta.enforce_strategy_guards(
            recs, led, {"total_value_gbp": 6333.0, "positions": {}})
        assert len(allowed) == 1
        assert any("re-set #4" in e for e in events)


class TestEntryThesisProvenance:
    """
    A thesis reconstructed after entry was never a prediction, so confirming it
    proves nothing — AMZN and GOOGL were both backfilled on 2026-07-03 for
    positions bought 2026-04-26. Flagged for re-underwriting, never sold
    mechanically: a trade forced by a record-keeping defect is the AVGO process
    error, not risk management.
    """

    def test_recorded_thesis_is_not_flagged(self):
        kind, _ = sp.entry_thesis_provenance(
            {"thesis": "Cheapest stock in portfolio at 17x P/E vs 50x sector."})
        assert kind == "recorded"

    def test_backfilled_thesis_is_flagged(self):
        kind, detail = sp.entry_thesis_provenance(
            {"thesis": "[Backfilled 2026-07-03 - no thesis recorded at entry] "
                       "AWS re-acceleration to 28% YoY growth."})
        assert kind == "backfilled"
        assert "price history" in detail

    def test_sync_placeholder_is_flagged(self):
        kind, _ = sp.entry_thesis_provenance({"thesis": "(synced from T212)"})
        assert kind == "synced"

    def test_absent_thesis_is_flagged(self):
        assert sp.entry_thesis_provenance({})[0] == "missing"
        assert sp.entry_thesis_provenance({"thesis": "  "})[0] == "missing"

    def test_a_later_backfill_mention_does_not_false_positive(self):
        # The marker is a prefix; the word appearing deep in a real thesis
        # must not demote a properly recorded case.
        kind, _ = sp.entry_thesis_provenance({"thesis": "A" * 200 + " backfilled"})
        assert kind == "recorded"

    def test_review_demands_re_underwriting_not_a_sell(self):
        ledger = {"positions": {"AMZN": {
            "thesis": "[Backfilled 2026-07-03] AWS re-acceleration.",
            "first_bought": "2026-04-26", "theme": "AI infrastructure"}},
            "trades": []}
        review = sp.build_thesis_review(
            ledger, {"positions": {"AMZN": {"pnl_pct": -4.92}}})
        assert "ENTRY THESIS NOT RECORDED AT ENTRY" in review
        assert "RE-UNDERWRITE AMZN THIS RUN" in review
        assert "recycle the capital" in review

    def test_review_stays_quiet_on_properly_recorded_positions(self):
        ledger = {"positions": {"XOM": {
            "thesis": "Permian-scale FCF machine at ~13x forward P/E.",
            "first_bought": "2026-06-22"}}, "trades": []}
        review = sp.build_thesis_review(
            ledger, {"positions": {"XOM": {"pnl_pct": 19.19}}})
        assert "ENTRY THESIS NOT RECORDED" not in review


# =============================================================================
# Last-session moves — the tape the prompt never carried
# =============================================================================

class TestDayMoves:
    """
    Every figure in the prompt is measured from entry, so a same-day shock is
    invisible: on 2026-09-14 the AI names opened -3% to -7% on a sector-wide
    story, the T212 prices in the prompt already reflected it, and the report
    said "no material adverse news". This block hands over the day move per
    holding and per theme and names what must be explained.
    """
    MOVES = {
        "MRVL": -6.95, "DELL": -6.26, "NVDA": -3.09, "AMZN": -1.37,
        "GOOGL": 2.08, "ABBV": 1.94, "XOM": 0.45, "VUSA.L": -0.63,
    }

    def _ledger(self):
        return {
            "benchmark_ticker": "VUSA.L",
            "positions": {
                "MRVL":  {"theme": "AI infrastructure"},
                "DELL":  {"theme": "AI infrastructure"},
                "NVDA":  {"theme": "AI infrastructure"},
                "AMZN":  {"theme": "AI infrastructure"},
                "GOOGL": {"theme": "AI infrastructure"},
                "ABBV":  {"theme": "pharma"},
                "XOM":   {"theme": "energy"},
            },
        }

    def _val(self):
        weights = {"MRVL": 366, "DELL": 539, "NVDA": 965, "AMZN": 432,
                   "GOOGL": 369, "ABBV": 694, "XOM": 845}
        return {"total_value_gbp": 6367.0,
                "positions": {t: {"current_value_gbp": v} for t, v in weights.items()}}

    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        monkeypatch.setattr(
            sp, "_session_move",
            lambda t: ({"pct": self.MOVES[t], "last": 1, "prev_close": 1}
                       if t in self.MOVES else None))
        monkeypatch.setattr(sp, "us_session_open_now", lambda: True)

    def test_positions_sorted_worst_first_with_weights_and_themes(self):
        tape = sp.day_moves(self._ledger(), self._val())
        assert list(tape["positions"])[:3] == ["MRVL", "DELL", "NVDA"]
        nvda = tape["positions"]["NVDA"]
        assert nvda["pct"] == pytest.approx(-3.09)
        assert nvda["weight_pct"] == pytest.approx(965 / 6367 * 100)
        assert nvda["theme"] == "AI infrastructure"
        assert tape["benchmark"] == {"ticker": "VUSA.L", "pct": -0.63}

    def test_theme_move_is_value_weighted(self):
        tape = sp.day_moves(self._ledger(), self._val())
        ai = tape["themes"]["AI infrastructure"]
        w = {"MRVL": 366, "DELL": 539, "NVDA": 965, "AMZN": 432, "GOOGL": 369}
        expect = sum(self.MOVES[t] * v for t, v in w.items()) / sum(w.values())
        assert ai["pct"] == pytest.approx(expect)
        assert ai["weight_pct"] == pytest.approx(sum(w.values()) / 6367 * 100)
        assert ai["count"] == 5

    def test_flags_holdings_over_3pct_and_multi_name_themes_over_2pct(self):
        tape = sp.day_moves(self._ledger(), self._val())
        assert tape["flagged"] == ["MRVL", "DELL", "NVDA"]
        assert tape["flagged_themes"] == ["AI infrastructure"]

    def test_single_holding_theme_is_judged_on_the_holding_bar(self, monkeypatch):
        # pharma is ABBV alone at +2.5%: below the 3% holding bar, and a
        # "theme" of one name has no common driver to flag at 2%.
        moves = dict(self.MOVES, ABBV=2.5)
        monkeypatch.setattr(
            sp, "_session_move",
            lambda t: ({"pct": moves[t]} if t in moves else None))
        tape = sp.day_moves(self._ledger(), self._val())
        assert "pharma" not in tape["flagged_themes"]
        assert "ABBV" not in tape["flagged"]

    def test_unpriced_holding_is_omitted_not_shown_as_flat(self):
        val = self._val()
        val["positions"]["NVDA"]["current_value_gbp"] = None
        tape = sp.day_moves(self._ledger(), val)
        assert "NVDA" not in tape["positions"]
        ledger = self._ledger()
        ledger["positions"]["ZZZ"] = {"theme": "x"}
        val["positions"]["ZZZ"] = {"current_value_gbp": 100.0}
        tape = sp.day_moves(ledger, val)
        assert "ZZZ" not in tape["positions"]      # no session move available

    def test_review_marks_must_explain_and_states_the_rule(self):
        review = sp.build_tape_review(self._ledger(), self._val())
        assert "MRVL    -6.95%" in review
        assert review.count("<-- MUST EXPLAIN") == 4   # 3 names + 1 theme
        assert "holdings: MRVL, DELL, NVDA; themes: AI infrastructure" in review
        assert "NOT a permitted answer" in review
        assert "search the THEME itself" in review
        assert "Benchmark VUSA.L: -0.63%" in review

    def test_review_has_no_rule_when_nothing_moved(self, monkeypatch):
        monkeypatch.setattr(sp, "_session_move", lambda t: {"pct": 0.4})
        review = sp.build_tape_review(self._ledger(), self._val())
        assert "MUST EXPLAIN" not in review
        assert "RULE:" not in review
        assert "MRVL" in review

    def test_review_says_when_us_figures_are_the_prior_session(self, monkeypatch):
        monkeypatch.setattr(sp, "us_session_open_now", lambda: False)
        review = sp.build_tape_review(self._ledger(), self._val())
        assert "PREVIOUS session" in review

    def test_review_empty_without_positions(self):
        assert sp.build_tape_review({"positions": {}}, {"positions": {}}) == ""

    def test_prompt_template_carries_the_block_and_the_theme_search_task(self):
        assert "{tape_review}" in prompts.ANALYSIS_USER_TEMPLATE
        assert "search on the\n     theme itself" in prompts.ANALYSIS_USER_TEMPLATE
        assert "MUST EXPLAIN" in prompts.ANALYSIS_SYSTEM

    def test_email_block_marks_flagged_names(self):
        tape = sp.day_moves(self._ledger(), self._val())
        text = sp.format_tape_for_email(tape)
        assert "MRVL   ! -6.95%" in text
        assert "XOM      +0.45%" in text
        assert "AI infrastructure !" in text
        assert "pharma  +1.94%" in text
        assert sp.format_tape_for_email({}) == ""

    def test_alert_fires_from_the_tape_not_the_analysis(self):
        tape = sp.day_moves(self._ledger(), self._val())
        alerts = ta._day_move_alerts(tape)
        assert len(alerts) == 1
        a = alerts[0]
        assert a.startswith("ALERT: LARGE MOVE today: MRVL -7.0%; DELL -6.3%; NVDA -3.1%")
        assert "'AI infrastructure' theme -3." in a
        assert "check the analysis explains it" in a

    def test_alert_silent_when_nothing_flagged(self, monkeypatch):
        monkeypatch.setattr(sp, "_session_move", lambda t: {"pct": 0.4})
        assert ta._day_move_alerts(sp.day_moves(self._ledger(), self._val())) == []
        assert ta._day_move_alerts({}) == []

    def test_session_move_is_cached_per_run(self, monkeypatch):
        calls = []

        class _Info:
            last_price = 101.0
            previous_close = 100.0

        class _Tkr:
            def __init__(self, t):
                calls.append(t)
                self.fast_info = _Info()

        monkeypatch.undo()   # restore the real _session_move for this test
        sp._day_move_cache.clear()
        monkeypatch.setattr(sp.yf, "Ticker", _Tkr)
        a = sp._session_move("CACHE_TEST")
        b = sp._session_move("CACHE_TEST")
        assert a["pct"] == pytest.approx(1.0)
        assert a is b
        assert calls == ["CACHE_TEST"]
        sp._day_move_cache.clear()


# =============================================================================
# SIZE NEVER ARGUED — accumulated top-ups with no sizing decision
# =============================================================================

class TestSizeNeverArgued:
    """
    NVDA went from 11.6% to 15.9% of the book in nine days via two dead-zone
    top-ups, each legal and each argued on "thesis confirmed"; nobody ever
    argued for a 15% position. The flag puts the accumulated size in front of
    the agent until it is argued on the record (SET_SIZE) or trimmed.
    """

    def _ledger(self, topups=("2026-09-01", "2026-09-10"), extra_trades=()):
        ledger = make_ledger()
        ledger["positions"] = {
            "NVDA": {"shares": 6.2, "avg_cost_gbp": 162.0,
                     "first_bought": "2026-05-13", "thesis": "cheap peer"},
            "XOM": {"shares": 6.7, "avg_cost_gbp": 104.0,
                    "first_bought": "2026-06-22", "thesis": "FCF"},
        }
        ledger["trades"] = [
            {"date": "2026-05-10", "action": "BUY", "ticker": "NVDA",
             "shares": 3.2, "amount_gbp": 500.0},
            {"date": "2026-05-10", "action": "SYNC_REMOVE", "ticker": "NVDA"},
            {"date": "2026-05-13", "action": "BUY", "ticker": "NVDA",
             "shares": 3.06, "amount_gbp": 500.0},
            {"date": "2026-06-22", "action": "BUY", "ticker": "XOM",
             "shares": 6.7, "amount_gbp": 700.0},
        ] + [
            {"date": d, "action": "BUY", "ticker": "NVDA",
             "shares": 1.5, "amount_gbp": 250.0}
            for d in topups
        ] + list(extra_trades)
        return ledger

    def _val(self, nvda=965.0, total=6300.0):
        return {"total_value_gbp": total,
                "positions": {"NVDA": {"current_value_gbp": nvda, "pnl_pct": -4.0},
                              "XOM": {"current_value_gbp": 845.0, "pnl_pct": 20.0}}}

    def test_composition_starts_the_lot_after_a_rejected_order(self):
        comp = sp.topup_composition(self._ledger(), "NVDA")
        assert comp["opening_gbp"] == 500.0          # the 10 May phantom is dropped
        assert comp["topup_gbp"] == 500.0
        assert comp["topup_share"] == pytest.approx(0.5)
        assert comp["topups"] == ["2026-09-01", "2026-09-10"]

    def test_composition_restarts_after_a_full_exit(self):
        ledger = self._ledger(extra_trades=[
            {"date": "2026-09-20", "action": "SELL", "ticker": "NVDA",
             "shares": 6.2, "amount_gbp": 900.0, "closed_position": True},
            {"date": "2026-10-05", "action": "BUY", "ticker": "NVDA",
             "shares": 3.0, "amount_gbp": 480.0},
        ])
        comp = sp.topup_composition(ledger, "NVDA")
        assert comp["opening_gbp"] == 480.0
        assert comp["topups"] == []

    def test_flags_large_position_built_by_topups(self):
        ledger = self._ledger()
        flag = sp.size_never_argued(ledger, "NVDA", ledger["positions"]["NVDA"], self._val())
        assert flag is not None
        assert flag["weight_pct"] == pytest.approx(965 / 6300 * 100)
        assert flag["topup_share"] == pytest.approx(0.5)
        assert flag["argued_on"] is None

    def test_no_flag_below_weight_or_topup_thresholds(self):
        ledger = self._ledger()
        pos = ledger["positions"]["NVDA"]
        assert sp.size_never_argued(ledger, "NVDA", pos, self._val(nvda=700.0)) is None   # 11.1%
        ledger = self._ledger(topups=("2026-09-01",))   # 250 of 750 = 33% -> still flagged
        assert sp.size_never_argued(ledger, "NVDA", ledger["positions"]["NVDA"], self._val())
        ledger["trades"][-1]["amount_gbp"] = 150.0       # 150 of 650 = 23% -> not
        assert sp.size_never_argued(ledger, "NVDA", ledger["positions"]["NVDA"], self._val()) is None
        assert sp.size_never_argued(ledger, "XOM", ledger["positions"]["XOM"], self._val()) is None

    def test_set_size_clears_the_flag_until_the_next_topup(self):
        ledger = self._ledger()
        events = sp.apply_recommendations(
            ledger,
            [{"action": "SET_SIZE", "yfinance_ticker": "NVDA",
              "size_argument": "15% is deliberate: highest-conviction expression."}],
            "2026-09-21")
        assert events == ["SET_SIZE NVDA: 15% is deliberate: highest-conviction expression."]
        pos = ledger["positions"]["NVDA"]
        assert pos["size_argued_on"] == "2026-09-21"
        assert ledger["trades"][-1]["action"] == "SET_SIZE"
        assert ledger["cash_gbp"] == 1000.0
        assert sp.size_never_argued(ledger, "NVDA", pos, self._val()) is None
        # a later top-up re-opens the question
        ledger["trades"].append({"date": "2026-11-09", "action": "BUY", "ticker": "NVDA",
                                 "shares": 1.5, "amount_gbp": 250.0})
        flag = sp.size_never_argued(ledger, "NVDA", pos, self._val())
        assert flag and flag["topups_since"] == ["2026-11-09"]

    def test_set_size_skips_unknown_ticker_and_empty_text(self):
        ledger = self._ledger()
        events = sp.apply_recommendations(
            ledger,
            [{"action": "SET_SIZE", "yfinance_ticker": "NOPE", "size_argument": "x"},
             {"action": "SET_SIZE", "yfinance_ticker": "NVDA", "size_argument": " "}],
            "2026-09-21")
        assert "SKIP SET_SIZE NOPE" in events[0]
        assert "SKIP SET_SIZE NVDA" in events[1]
        assert "size_argued_on" not in ledger["positions"]["NVDA"]

    def test_review_demands_a_sizing_decision(self):
        ledger = self._ledger()
        review = sp.build_thesis_review(ledger, self._val())
        assert "SIZE NEVER ARGUED: NVDA is 15.3% of the book" in review
        assert "50% of its cost came from top-ups" in review
        assert "(a) SET_SIZE" in review
        assert "(b) TRIM toward the size that was argued for at entry" in review
        assert review.count("SIZE NEVER ARGUED") == 1   # XOM is not flagged

    def test_review_shows_the_argument_once_recorded(self):
        ledger = self._ledger()
        sp.apply_recommendations(
            ledger, [{"action": "SET_SIZE", "yfinance_ticker": "NVDA",
                      "size_argument": "Deliberate 15%."}], "2026-09-21")
        review = sp.build_thesis_review(ledger, self._val())
        assert "SIZE NEVER ARGUED" not in review
        assert "Size argued 2026-09-21: Deliberate 15%." in review

    def test_review_reflags_when_topped_up_after_the_argument(self):
        ledger = self._ledger(topups=("2026-09-01",))
        ledger["positions"]["NVDA"].update(
            {"size_argued_on": "2026-09-05", "size_argument": "Deliberate."})
        ledger["trades"].append({"date": "2026-09-10", "action": "BUY", "ticker": "NVDA",
                                 "shares": 1.5, "amount_gbp": 250.0})
        review = sp.build_thesis_review(ledger, self._val())
        assert "argued on 2026-09-05 but it has been topped up since (2026-09-10)" in review

    def test_alert_fires_unless_the_run_answers_it(self):
        ledger = self._ledger()
        alerts = ta._size_argued_alerts([], ledger, self._val())
        assert len(alerts) == 1
        assert alerts[0].startswith("ALERT: NVDA is 15.3% of the book with 50% of its cost from top-ups")
        for rec in (
            {"action": "SET_SIZE", "yfinance_ticker": "NVDA", "size_argument": "x"},
            {"action": "TRIM", "yfinance_ticker": "NVDA", "trim_pct": 25},
            {"action": "SELL", "yfinance_ticker": "NVDA"},
        ):
            assert ta._size_argued_alerts([rec], ledger, self._val()) == []

    def test_alert_reaches_guard_events_and_set_size_passes_guards(self):
        ledger = self._ledger()
        pre_val = dict(self._val(), cash_gbp=300.0)
        allowed, events = ta.enforce_strategy_guards([], ledger, pre_val)
        assert any("size was never argued for" in e for e in events)
        rec = {"action": "SET_SIZE", "yfinance_ticker": "NVDA", "size_argument": "x"}
        allowed, events = ta.enforce_strategy_guards([rec], ledger, pre_val)
        assert allowed == [rec]
        assert not any("never argued" in e for e in events)

    def test_executor_confirms_set_size_without_placing_order(self, monkeypatch):
        monkeypatch.setattr(t212ex, "T212_DEMO_EXECUTE", True)
        monkeypatch.setattr(t212ex, "T212_ENV", "demo")
        monkeypatch.setattr(t212ex, "_load_instruments", lambda: INSTRUMENTS)
        monkeypatch.setattr(t212ex, "get_t212_positions_map", lambda: {})
        monkeypatch.setattr(
            t212ex, "_place_market_order",
            lambda *a, **k: pytest.fail("SET_SIZE must not place a T212 order"))
        rec = {"action": "SET_SIZE", "yfinance_ticker": "NVDA", "size_argument": "x"}
        events, confirmed = t212ex.execute_recommendations([rec])
        assert confirmed == [rec]
        assert any("SET_SIZE NVDA" in e for e in events)

    def test_prompt_documents_set_size(self):
        assert '"action": "SET_SIZE"' in prompts.ANALYSIS_SYSTEM
        assert "SET_SIZE records the argument for a position's ACCUMULATED size" in prompts.ANALYSIS_SYSTEM
