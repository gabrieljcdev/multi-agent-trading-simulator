"""
signals/arbitrage.py
Cross-exchange arbitrage scanner — Track A.
"""
import logging
from datetime import datetime, timedelta
from itertools import combinations
from typing import Optional
from signals.base import Signal
from config import settings

logger = logging.getLogger(__name__)

class ArbScanner:
    def __init__(self, market_data):
        self._market_data = market_data
        self._last_signals: dict[str, datetime] = {}

    async def scan(self, sentiment_scores: dict) -> list:
        signals = []
        pairs = self._market_data.active_pairs()
        for pair in pairs:
            prices = self._market_data.get_all_prices(pair)
            if len(prices) < 2:
                continue
            for ex_a, ex_b in combinations(prices.keys(), 2):
                s = self._evaluate_gap(pair, ex_a, ex_b, prices, sentiment_scores)
                if s: signals.append(s)
                s = self._evaluate_gap(pair, ex_b, ex_a, prices, sentiment_scores)
                if s: signals.append(s)
        return signals

    def _evaluate_gap(self, pair, ex_buy, ex_sell, prices, sentiment_scores):
        price_buy  = prices.get(ex_buy)
        price_sell = prices.get(ex_sell)
        if not price_buy or not price_sell or price_buy <= 0:
            return None
        gap_pct = (price_sell - price_buy) / price_buy * 100
        net_gap = gap_pct - settings.ARB_FEE_ESTIMATE_PCT
        # Signal-track arb uses the fallback (0.35%) threshold; the bitget
        # special-case ARB_MIN_GAP_PCT (0.03%) is reserved for the dedicated
        # execution/arb_engine.py.
        if net_gap < settings.ARB_MIN_GAP_PCT_FALLBACK:
            return None
        dedup_key = f"{pair}_{ex_buy}_{ex_sell}"
        if dedup_key in self._last_signals:
            if datetime.utcnow() - self._last_signals[dedup_key] < timedelta(minutes=3):
                return None
        raw_score = min(95.0, 50.0 + (net_gap / settings.ARB_MIN_GAP_PCT_FALLBACK) * 25)
        coin = pair.split("/")[0]
        sentiment = sentiment_scores.get(coin, sentiment_scores.get("MARKET", {}))
        composite = sentiment.get("composite", 50)
        velocity  = sentiment.get("velocity", 0)
        sent_mod = 0.0
        if composite >= settings.SENTIMENT_BOOST_THRESHOLD:
            sent_mod = settings.SENTIMENT_BOOST_AMOUNT * 0.5
        elif composite <= settings.SENTIMENT_BLOCK_THRESHOLD:
            sent_mod = -settings.SENTIMENT_SUPPRESS_AMOUNT * 0.5
        signal = Signal(
            pair=pair, signal_type="arb", direction="arb",
            exchange=ex_buy, exchange_b=ex_sell,
            raw_score=raw_score, sentiment_mod=sent_mod,
            arb_gap_pct=net_gap, sentiment_score=composite,
            sentiment_velocity=velocity,
            tf_5m=True, tf_15m=True, tf_1h=True,
            indicators={"price_buy": price_buy, "price_sell": price_sell,
                       "gap_raw": gap_pct, "gap_net": net_gap},
        )
        signal.expires_at = datetime.utcnow() + timedelta(minutes=3)
        self._last_signals[dedup_key] = datetime.utcnow()
        logger.info(f"ARB: {pair} {ex_buy}->{ex_sell} gap={net_gap:.3f}% score={raw_score:.0f}")
        return signal
