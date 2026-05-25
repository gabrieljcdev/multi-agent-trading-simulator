"""
Unit tests for scalping agent v2 components.

Runs with:  pytest test_scalping_v2.py -v

All tests use mocked market_data and ofi_engine — no exchange connectivity needed.
"""

import pytest
from dataclasses import dataclass
from typing import List, Optional, Dict
from unittest.mock import MagicMock

from scalping_confluence import ConfluenceChecker, ConfluenceResult, CombinedConfluenceResult
from scalping_atr_sl import ATRStopCalculator, TpSlV2
from scalping_agent_v2_integration import is_ready_for_live_v2
import settings_scalp_v2 as v2


# =============================================================================
# Test fixtures / mocks
# =============================================================================

@dataclass
class FakeLevel:
    price: float
    size: float

@dataclass
class FakeBook:
    bids: List[FakeLevel]
    asks: List[FakeLevel]


class MockMarketData:
    """Configurable mock for market_data dependency."""

    def __init__(self):
        self.vwap = 50000.0
        self.mid = 50100.0
        self.mid_then = 50090.0
        self.ema_fast = 50050.0
        self.ema_slow = 49950.0
        self.current_vol = 1500.0
        self.median_vol = 1000.0
        self.atr = 50.0
        self.book = FakeBook(
            bids=[FakeLevel(50099, 1.0), FakeLevel(50098, 1.0), FakeLevel(50097, 1.0),
                  FakeLevel(50096, 1.0), FakeLevel(50095, 1.0)],
            asks=[FakeLevel(50101, 1.0), FakeLevel(50102, 1.0), FakeLevel(50103, 1.0),
                  FakeLevel(50104, 1.0), FakeLevel(50105, 1.0)],
        )

    def get_mid_price(self, symbol, exchange):
        return self.mid

    def get_mid_price_at_offset(self, symbol, exchange, offset_ms):
        return self.mid_then

    def get_session_vwap(self, symbol, exchange):
        return self.vwap

    def get_ema(self, symbol, exchange, timeframe, period):
        if period == 8:
            return self.ema_fast
        return self.ema_slow

    def get_current_minute_volume(self, symbol, exchange):
        return self.current_vol

    def get_rolling_median_volume(self, symbol, exchange, timeframe, lookback):
        return self.median_vol

    def get_order_book(self, symbol, exchange, levels=5):
        return self.book

    def get_atr(self, symbol, exchange, period, timeframe):
        return self.atr


class MockOFIEngine:
    def __init__(self):
        self.z_scores: Dict[tuple, float] = {}
        self.exchanges_by_symbol: Dict[str, List[str]] = {}

    def get_z_score(self, symbol, exchange):
        return self.z_scores.get((symbol, exchange))

    def get_exchanges_for_symbol(self, symbol):
        return self.exchanges_by_symbol.get(symbol, [])


@pytest.fixture
def md():
    return MockMarketData()

@pytest.fixture
def ofi():
    return MockOFIEngine()

@pytest.fixture
def settings():
    return v2.default_v2_settings()

@pytest.fixture
def checker(md, ofi, settings):
    return ConfluenceChecker(md, ofi, settings)

@pytest.fixture
def atr_calc(md, settings):
    return ATRStopCalculator(md, settings)


# =============================================================================
# VWAP gate
# =============================================================================

class TestVWAP:
    def test_long_passes_when_mid_above_vwap(self, checker, md):
        md.mid, md.vwap = 50100.0, 50000.0
        r = checker.check_vwap_alignment("BTC/USDT", "MEXC", "LONG")
        assert r.passed
        assert r.score == 1.0

    def test_long_fails_when_mid_below_vwap(self, checker, md):
        md.mid, md.vwap = 49900.0, 50000.0
        r = checker.check_vwap_alignment("BTC/USDT", "MEXC", "LONG")
        assert not r.passed

    def test_short_passes_when_mid_below_vwap(self, checker, md):
        md.mid, md.vwap = 49900.0, 50000.0
        r = checker.check_vwap_alignment("BTC/USDT", "MEXC", "SHORT")
        assert r.passed

    def test_short_fails_when_mid_above_vwap(self, checker, md):
        md.mid, md.vwap = 50100.0, 50000.0
        r = checker.check_vwap_alignment("BTC/USDT", "MEXC", "SHORT")
        assert not r.passed

    def test_passes_when_data_unavailable(self, checker, md):
        md.vwap = None
        r = checker.check_vwap_alignment("BTC/USDT", "MEXC", "LONG")
        assert r.passed  # fail-open on missing data


# =============================================================================
# HTF trend gate
# =============================================================================

class TestHTF:
    def test_long_passes_when_htf_uptrend(self, checker, md):
        md.ema_fast, md.ema_slow = 50050.0, 49950.0  # uptrend
        r = checker.check_htf_trend("BTC/USDT", "MEXC", "LONG")
        assert r.passed

    def test_long_fails_when_htf_downtrend(self, checker, md):
        md.ema_fast, md.ema_slow = 49950.0, 50050.0  # downtrend
        r = checker.check_htf_trend("BTC/USDT", "MEXC", "LONG")
        assert not r.passed

    def test_short_passes_when_htf_downtrend(self, checker, md):
        md.ema_fast, md.ema_slow = 49950.0, 50050.0
        r = checker.check_htf_trend("BTC/USDT", "MEXC", "SHORT")
        assert r.passed


# =============================================================================
# Volume gate
# =============================================================================

class TestVolume:
    def test_passes_when_volume_above_median(self, checker, md):
        md.current_vol, md.median_vol = 1500.0, 1000.0
        r = checker.check_volume("BTC/USDT", "MEXC")
        assert r.passed

    def test_fails_when_volume_below_threshold(self, checker, md):
        md.current_vol, md.median_vol = 500.0, 1000.0
        r = checker.check_volume("BTC/USDT", "MEXC")
        assert not r.passed

    def test_handles_zero_median(self, checker, md):
        md.median_vol = 0.0
        r = checker.check_volume("BTC/USDT", "MEXC")
        assert r.passed  # fail-open


# =============================================================================
# Cross-exchange OFI gate
# =============================================================================

class TestCrossExchange:
    def test_blocks_when_other_venue_strongly_opposes(self, checker, ofi):
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC", "BITGET"]
        ofi.z_scores[("BTC/USDT", "BITGET")] = -1.5  # bearish, signal was LONG
        r = checker.check_cross_exchange_ofi("BTC/USDT", "MEXC", "LONG", primary_z=2.0)
        assert not r.passed
        assert "opposing" in r.reason

    def test_confirms_when_other_venue_agrees(self, checker, ofi):
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC", "BITGET"]
        ofi.z_scores[("BTC/USDT", "BITGET")] = 1.2  # bullish
        r = checker.check_cross_exchange_ofi("BTC/USDT", "MEXC", "LONG", primary_z=2.0)
        assert r.passed
        assert r.score == 1.0
        assert "confirming" in r.reason

    def test_neutral_when_other_venue_in_band(self, checker, ofi):
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC", "BITGET"]
        ofi.z_scores[("BTC/USDT", "BITGET")] = 0.1  # within neutral band
        r = checker.check_cross_exchange_ofi("BTC/USDT", "MEXC", "LONG", primary_z=2.0)
        assert r.passed
        assert r.score == 0.5

    def test_handles_no_other_venues(self, checker, ofi):
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC"]
        r = checker.check_cross_exchange_ofi("BTC/USDT", "MEXC", "LONG", primary_z=2.0)
        assert r.passed
        assert r.metadata["others_count"] == 0


# =============================================================================
# BTC directional gate
# =============================================================================

class TestBTCDirectional:
    def test_blocks_alt_long_when_btc_bearish(self, checker, ofi):
        ofi.z_scores[("BTC/USDT", "MEXC")] = -1.5
        r = checker.check_btc_directional("SOL/USDT", "MEXC", "LONG")
        assert not r.passed

    def test_blocks_alt_short_when_btc_bullish(self, checker, ofi):
        ofi.z_scores[("BTC/USDT", "MEXC")] = 1.5
        r = checker.check_btc_directional("SOL/USDT", "MEXC", "SHORT")
        assert not r.passed

    def test_allows_alt_long_when_btc_in_band(self, checker, ofi):
        ofi.z_scores[("BTC/USDT", "MEXC")] = 0.2  # within neutral band
        r = checker.check_btc_directional("SOL/USDT", "MEXC", "LONG")
        assert r.passed

    def test_allows_alt_long_when_btc_aligned(self, checker, ofi):
        ofi.z_scores[("BTC/USDT", "MEXC")] = 1.5  # bullish, matches LONG
        r = checker.check_btc_directional("SOL/USDT", "MEXC", "LONG")
        assert r.passed

    def test_btc_symbol_skips_gate(self, checker):
        r = checker.check_btc_directional("BTC/USDT", "MEXC", "LONG")
        assert r.passed
        assert "BTC" in r.reason


# =============================================================================
# Adverse selection gate
# =============================================================================

class TestAdverseSelection:
    def test_blocks_long_when_mid_dropped(self, checker, md, settings):
        md.mid = 49995.0
        md.mid_then = 50000.0  # drop = 1 bp, just at threshold
        # Below threshold means within tolerance — still allowed
        r = checker.check_adverse_selection("BTC/USDT", "MEXC", "LONG")
        assert r.passed

    def test_blocks_long_when_drop_exceeds_threshold(self, checker, md, settings):
        md.mid = 49985.0
        md.mid_then = 50000.0  # 3 bps drop
        r = checker.check_adverse_selection("BTC/USDT", "MEXC", "LONG")
        assert not r.passed
        assert "dropped" in r.reason

    def test_blocks_short_when_mid_rose(self, checker, md):
        md.mid = 50015.0
        md.mid_then = 50000.0  # 3 bps rise
        r = checker.check_adverse_selection("BTC/USDT", "MEXC", "SHORT")
        assert not r.passed

    def test_allows_when_mid_stable(self, checker, md):
        md.mid = 50000.5
        md.mid_then = 50000.0  # 0.1 bp rise — well within tolerance
        r = checker.check_adverse_selection("BTC/USDT", "MEXC", "LONG")
        assert r.passed


# =============================================================================
# Depth gate
# =============================================================================

class TestDepth:
    def test_passes_when_book_deep(self, checker, md):
        # default book has 1 BTC at each level × 5 levels ≈ $250K
        r = checker.check_depth("BTC/USDT", "MEXC", position_size_usd=100.0)
        assert r.passed

    def test_blocks_when_top5_inadequate(self, checker, md):
        # Position size of $100K with each level only 1 BTC ($50K) = $250K top5
        # Required is 5x = $500K — this should fail
        r = checker.check_depth("BTC/USDT", "MEXC", position_size_usd=100_000.0)
        assert not r.passed
        assert "top5" in r.reason

    def test_blocks_when_consume_pct_too_high(self, checker, md):
        # Each level has 1 BTC at $50K. Position of $20K = 40% of top1.
        # Threshold is 20% — should fail.
        r = checker.check_depth("BTC/USDT", "MEXC", position_size_usd=20_000.0)
        assert not r.passed
        assert "consume" in r.reason


# =============================================================================
# Combined runner
# =============================================================================

class TestCombined:
    def test_all_pass_with_clean_setup(self, checker, ofi):
        # default mocks all align for a LONG
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC", "BITGET"]
        ofi.z_scores[("BTC/USDT", "BITGET")] = 1.2  # bullish confirmation
        result = checker.run_all_gates("BTC/USDT", "MEXC", "LONG",
                                       primary_z=2.0, position_size_usd=100.0)
        assert result.passed
        assert result.confluence_score == 3  # all 3 soft gates passed
        assert result.strength_label == "VERY_STRONG"
        assert result.cross_exchange_agrees

    def test_fails_fast_on_adverse_selection(self, checker, md):
        md.mid = 49980.0
        md.mid_then = 50000.0  # 4 bps drop = adverse for LONG
        result = checker.run_all_gates("BTC/USDT", "MEXC", "LONG",
                                       primary_z=2.0, position_size_usd=100.0)
        assert not result.passed
        assert not result.adverse_selection_ok
        # short-circuit should mean depth wasn't even checked
        assert not any(r.gate_name == "DEPTH" for r in result.individual_results)

    def test_two_of_three_soft_gates_passes(self, checker, md, ofi):
        # Knock out volume gate
        md.current_vol, md.median_vol = 500.0, 1000.0
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC"]
        result = checker.run_all_gates("BTC/USDT", "MEXC", "LONG",
                                       primary_z=2.0, position_size_usd=100.0)
        # 2/3 soft gates pass (VWAP, HTF) — should still go through
        assert result.passed
        assert result.confluence_score == 2
        assert result.strength_label == "MODERATE"

    def test_one_of_three_soft_gates_fails(self, checker, md, ofi):
        # Knock out two soft gates
        md.current_vol, md.median_vol = 500.0, 1000.0
        md.ema_fast, md.ema_slow = 49950.0, 50050.0  # HTF opposes LONG
        ofi.exchanges_by_symbol["BTC/USDT"] = ["MEXC"]
        result = checker.run_all_gates("BTC/USDT", "MEXC", "LONG",
                                       primary_z=2.0, position_size_usd=100.0)
        assert not result.passed
        assert result.confluence_score == 1
        assert "confluence" in result.blocking_reason.lower()


# =============================================================================
# ATR-aware stop loss
# =============================================================================

class TestATRStopLoss:
    def test_atr_widens_sl_on_volatile_pair(self, atr_calc, md):
        # ATR of 50 on 50100 mid = ~9.98 bps; × 0.3 = ~3 bps
        # FeeManager base SL at 0 round-trip = 3.0 / 1.6 = 1.875 bps
        # ATR-derived 3 bps should win
        result = atr_calc.compute_tp_sl_v2("BTC/USDT", "MEXC", round_trip_bps=0.0)
        assert result.tp_bps == pytest.approx(3.0)
        assert result.sl_bps > result.base_sl_bps
        assert result.atr_adjusted

    def test_atr_floor_applied_on_calm_pair(self, atr_calc, md):
        md.atr = 1.0  # tiny ATR = ~0.2 bps × 0.3 = 0.06 bps
        result = atr_calc.compute_tp_sl_v2("BTC/USDT", "MEXC", round_trip_bps=0.0)
        # floor is 1.5 bps; base is 1.875 bps; max wins → 1.875
        assert result.sl_bps == pytest.approx(1.875)

    def test_atr_ceiling_caps_extreme_volatility(self, atr_calc, md):
        md.atr = 1000.0  # huge ATR → ~199 bps × 0.3 = ~60 bps, ceiling = 8
        result = atr_calc.compute_tp_sl_v2("BTC/USDT", "MEXC", round_trip_bps=0.0)
        assert result.sl_clamped == "CEILING"
        assert result.sl_bps == pytest.approx(8.0)

    def test_fee_aware_tp_increases_with_fees(self, atr_calc):
        result_mexc = atr_calc.compute_tp_sl_v2("BTC/USDT", "MEXC", round_trip_bps=0.0)
        result_bitget = atr_calc.compute_tp_sl_v2("BTC/USDT", "BITGET", round_trip_bps=2.0)
        assert result_bitget.tp_bps == pytest.approx(5.0)
        assert result_bitget.tp_bps > result_mexc.tp_bps

    def test_rr_actual_recomputed(self, atr_calc):
        result = atr_calc.compute_tp_sl_v2("BTC/USDT", "MEXC", round_trip_bps=0.0)
        assert result.rr_actual == pytest.approx(result.tp_bps / result.sl_bps, rel=1e-6)

    def test_disabled_atr_falls_back_to_base(self, md):
        s = v2.default_v2_settings()
        s.SCALP_USE_ATR_AWARE_SL = False
        calc = ATRStopCalculator(md, s)
        result = calc.compute_tp_sl_v2("BTC/USDT", "MEXC", round_trip_bps=0.0)
        assert result.sl_bps == pytest.approx(1.875)
        assert not result.atr_adjusted


# =============================================================================
# Activation readiness v2
# =============================================================================

class TestActivationReadiness:
    def test_all_criteria_met(self, settings):
        stats = {
            "n_closed": 350,
            "win_rate": 0.58,
            "avg_net_bps": 0.7,
            "max_hold_pct": 0.20,
            "directional_accuracy_1m": 0.60,
        }
        r = is_ready_for_live_v2(stats, settings)
        assert r["ready"]
        assert r["reasons_failing"] == []

    def test_insufficient_observations(self, settings):
        stats = {"n_closed": 100, "win_rate": 0.60, "avg_net_bps": 1.0,
                 "max_hold_pct": 0.20, "directional_accuracy_1m": 0.60}
        r = is_ready_for_live_v2(stats, settings)
        assert not r["ready"]
        assert any("n_closed" in x for x in r["reasons_failing"])

    def test_win_rate_below_threshold(self, settings):
        stats = {"n_closed": 350, "win_rate": 0.51, "avg_net_bps": 0.7,
                 "max_hold_pct": 0.20, "directional_accuracy_1m": 0.60}
        r = is_ready_for_live_v2(stats, settings)
        assert not r["ready"]

    def test_max_hold_too_high(self, settings):
        stats = {"n_closed": 350, "win_rate": 0.60, "avg_net_bps": 0.7,
                 "max_hold_pct": 0.40, "directional_accuracy_1m": 0.60}
        r = is_ready_for_live_v2(stats, settings)
        assert not r["ready"]
        assert any("max_hold_pct" in x for x in r["reasons_failing"])
