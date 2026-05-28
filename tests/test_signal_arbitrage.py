"""tests/test_signal_arbitrage.py — signals/arbitrage.py (signal-track ArbScanner).

Distinct from execution/arb_engine.py (the dedicated engine). This is the
quality-gate scanner that turns cross-exchange price gaps into Signal objects.
"""
from unittest.mock import MagicMock

import pytest

from signals.arbitrage import ArbScanner


def _make_market_data(prices_per_pair: dict[str, dict[str, object]]):
    """Build a stub market_data whose get_all_prices(pair) returns
    prices_per_pair[pair] and active_pairs() returns its keys."""
    md = MagicMock()
    md.active_pairs.return_value = list(prices_per_pair.keys())
    md.get_all_prices.side_effect = lambda pair: prices_per_pair.get(pair, {})
    return md


@pytest.mark.asyncio
async def test_evaluate_gap_coerces_string_prices_without_raising():
    """Regression: ccxt sometimes hands prices back as strings. The 30-min
    smoke test under the $5000 reset surfaced ~48 ERROR lines of
    `unsupported operand type(s) for -: 'str' and 'float'` from this scanner
    firing every ARB_SCAN_INTERVAL_MS. Defensive float() coercion must
    accept str-typed prices and still produce a Signal when the gap is real."""
    # ~1.5% gap — well above ARB_MIN_GAP_PCT_FALLBACK (0.35%) net of the
    # 0.20% fee estimate, so a signal must be produced.
    md = _make_market_data({"BTC/USDT": {"binance": "100.0", "kraken": 101.5}})
    scanner = ArbScanner(md)
    signals = await scanner.scan(sentiment_scores={})
    assert len(signals) >= 1
    assert all(s.signal_type == "arb" for s in signals)


@pytest.mark.asyncio
async def test_evaluate_gap_skips_unparseable_prices_silently():
    """Bad price types (None, garbage string) skip the pair instead of raising."""
    md = _make_market_data({"BTC/USDT": {"binance": None, "kraken": "not-a-number"}})
    scanner = ArbScanner(md)
    signals = await scanner.scan(sentiment_scores={})
    assert signals == []


@pytest.mark.asyncio
async def test_evaluate_gap_emits_nothing_below_threshold():
    """A 0.1% raw gap is below the 0.35% fallback threshold even before fees,
    so no signal should be produced."""
    md = _make_market_data({"BTC/USDT": {"binance": 100.0, "kraken": 100.1}})
    scanner = ArbScanner(md)
    signals = await scanner.scan(sentiment_scores={})
    assert signals == []
