"""Scalp-v2 MarketData / OFIEngine accessors (commit 2).

These back the ConfluenceChecker + ATRStopCalculator. All None-safe; indicator
values derive from the cached candle DataFrames.
"""
import time
from collections import deque

import pandas as pd
import pytest

from core.market_data import MarketData, OrderBook, OBLevel
from agents.scalping_agent import OFIEngine


def _candle_df(n=40):
    rows = []
    for i in range(n):
        c = 50000.0 + i
        rows.append({"open": c, "high": c + 5, "low": c - 5, "close": c,
                     "volume": 100.0 + i, "vwap": c - 1})
    return pd.DataFrame(rows)


def _md():
    md = MarketData()
    md._candles[("mexc", "BTC/USDT", "5m")] = _candle_df()
    md._last_book[("mexc", "BTC/USDT")] = {
        "bids": [[50099.0, 2.0], [50098.0, 1.0]],
        "asks": [[50101.0, 2.0], [50102.0, 1.0]],
    }
    md._last_price[("mexc", "BTC/USDT")] = 50100.0
    return md


def test_get_mid_price_from_book():
    assert _md().get_mid_price("BTC/USDT", "mexc") == pytest.approx(50100.0)


def test_get_mid_price_falls_back_to_last_price():
    md = MarketData()
    md._last_price[("mexc", "BTC/USDT")] = 123.0
    assert md.get_mid_price("BTC/USDT", "mexc") == 123.0
    assert md.get_mid_price("ETH/USDT", "mexc") is None


def test_get_order_book_shape():
    ob = _md().get_order_book("BTC/USDT", "mexc", levels=5)
    assert isinstance(ob, OrderBook)
    assert isinstance(ob.bids[0], OBLevel)
    assert ob.bids[0].price == 50099.0 and ob.bids[0].size == 2.0
    assert _md().get_order_book("NOPE/USDT", "mexc") is None


def test_get_ema_and_atr_from_candles():
    md = _md()
    assert (md.get_ema("BTC/USDT", "mexc", "5m", 8) or 0) > 0
    assert (md.get_atr("BTC/USDT", "mexc", 20, "5m") or 0) > 0
    # 1m isn't streamed → None (ATR stop calc falls back to its base SL).
    assert md.get_atr("BTC/USDT", "mexc", 20, "1m") is None


def test_get_vwap_and_volume():
    md = _md()
    assert md.get_session_vwap("BTC/USDT", "mexc") is not None
    assert md.get_current_minute_volume("BTC/USDT", "mexc") is not None  # 5m fallback
    med = md.get_rolling_median_volume("BTC/USDT", "mexc", "1m", 20)
    assert med is not None and med > 0


def test_get_mid_price_at_offset():
    md = MarketData()
    now = time.time()
    md._price_history[("mexc", "BTC/USDT")] = deque(
        [(now - 1.0, 50000.0), (now - 0.05, 50010.0)]
    )
    assert md.get_mid_price_at_offset("BTC/USDT", "mexc", 100) == 50000.0
    assert md.get_mid_price_at_offset("ETH/USDT", "mexc", 100) is None


def test_ofi_get_z_score_none_until_bucket_closes():
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    assert eng.get_z_score("BTC/USDT", "mexc") is None
    key = "BTC/USDT:mexc"
    eng._last_bucket_close[key] = time.time()
    eng._last_z[key] = 2.3
    assert eng.get_z_score("BTC/USDT", "mexc") == pytest.approx(2.3)


def test_ofi_get_exchanges_for_symbol():
    eng = OFIEngine(levels=5, window_sec=20, zscore_window=80)
    eng._last_book["BTC/USDT:mexc"] = object()
    eng._last_book["BTC/USDT:bitget"] = object()
    eng._last_book["ETH/USDT:mexc"] = object()
    assert set(eng.get_exchanges_for_symbol("BTC/USDT")) == {"mexc", "bitget"}
