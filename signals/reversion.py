"""
signals/reversion.py
Mean reversion scanner — Track C.
"""
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
from signals.base import Signal
from core.regime_detector import regime_detector
from signals.ofi import ofi_scorer
from config import settings

logger = logging.getLogger(__name__)

class ReversionScanner:
    def __init__(self, market_data):
        self._market_data = market_data
        self._last_signals: dict[str, datetime] = {}

    async def scan(self, sentiment_scores: dict) -> list:
        signals = []
        for pair in self._market_data.active_pairs():
            for exchange in settings.ENABLED_EXCHANGES:
                s = await self._evaluate_pair(pair, exchange, sentiment_scores)
                if s:
                    signals.append(s)
                    break
        return signals

    async def _evaluate_pair(self, pair, exchange, sentiment_scores):
        dedup_key = f"rev_{pair}"
        if dedup_key in self._last_signals:
            if datetime.utcnow() - self._last_signals[dedup_key] < timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES):
                return None
        df_fast = self._market_data.get_candles(exchange, pair, settings.FAST_TIMEFRAME)
        df_mid  = self._market_data.get_candles(exchange, pair, settings.MID_TIMEFRAME)
        df_slow = self._market_data.get_candles(exchange, pair, settings.SLOW_TIMEFRAME)
        if df_fast is None or len(df_fast) < 30:
            return None
        latest = df_fast.iloc[-1]
        close  = float(latest["close"])
        regime = regime_detector.get_primary(pair)
        if regime and not regime.reversion_ok:
            return None
        bb_upper = latest.get("bb_upper")
        bb_lower = latest.get("bb_lower")
        bb_mid   = latest.get("bb_mid")
        if any(v is None or pd.isna(v) for v in [bb_upper, bb_lower, bb_mid]):
            return None
        bb_upper = float(bb_upper)
        bb_lower = float(bb_lower)
        bb_mid   = float(bb_mid)
        bb_range = bb_upper - bb_lower
        if bb_range == 0:
            return None
        bb_position = (close - bb_lower) / bb_range
        if close <= bb_lower + (bb_range * 0.1):
            direction = "long"
        elif close >= bb_upper - (bb_range * 0.1):
            direction = "short"
        else:
            return None
        rsi = latest.get("rsi")
        if rsi is None or pd.isna(rsi):
            return None
        rsi = float(rsi)
        if direction == "long" and rsi > settings.RSI_OVERSOLD + 10:
            return None
        if direction == "short" and rsi < settings.RSI_OVERBOUGHT - 10:
            return None
        if settings.REVERSION_REQUIRE_DIVERGENCE:
            if not self._detect_divergence(df_fast, direction):
                return None
        vwap = latest.get("vwap")
        vwap_stretch = 0.0
        if vwap and not pd.isna(vwap) and float(vwap) > 0:
            vwap_stretch = abs(close - float(vwap)) / float(vwap) * 100
            if settings.REVERSION_VWAP_CONFIRM and vwap_stretch < settings.VWAP_STRETCH_PCT:
                return None
        tf_5m  = self._confirms(df_fast, direction)
        tf_15m = self._confirms(df_mid,  direction) if df_mid  is not None else False
        tf_1h  = self._confirms(df_slow, direction) if df_slow is not None else False
        if sum([tf_5m, tf_15m, tf_1h]) < settings.MIN_TF_CONFIRMATIONS:
            return None
        ofi = ofi_scorer.get_best(pair)
        ofi_mod = ofi.signal_modifier(direction) if ofi else 0.0
        raw_score = 45.0
        if direction == "long":
            raw_score += min(15.0, (bb_lower - close) / bb_range * 100 * 3)
            raw_score += min(10.0, max(0, settings.RSI_OVERSOLD + 5 - rsi) * 0.8)
        else:
            raw_score += min(15.0, (close - bb_upper) / bb_range * 100 * 3)
            raw_score += min(10.0, max(0, rsi - settings.RSI_OVERBOUGHT + 5) * 0.8)
        raw_score += min(8.0, vwap_stretch * 2)
        raw_score += sum([tf_5m, tf_15m, tf_1h]) * 4
        raw_score = min(92.0, raw_score)
        coin = pair.split("/")[0]
        sentiment = sentiment_scores.get(coin, sentiment_scores.get("MARKET", {}))
        composite = sentiment.get("composite", 50)
        velocity  = sentiment.get("velocity", 0)
        sent_mod = 0.0
        if direction == "long" and composite <= settings.SENTIMENT_BLOCK_THRESHOLD:
            sent_mod += 8
        elif composite >= settings.SENTIMENT_BOOST_THRESHOLD:
            sent_mod += settings.SENTIMENT_BOOST_AMOUNT * 0.5
        signal = Signal(
            pair=pair, signal_type="reversion", direction=direction,
            exchange=exchange, raw_score=raw_score,
            sentiment_mod=sent_mod + ofi_mod,
            rsi=rsi, bb_position=bb_position,
            sentiment_score=composite, sentiment_velocity=velocity,
            tf_5m=tf_5m, tf_15m=tf_15m, tf_1h=tf_1h,
            indicators={"close": close, "bb_upper": bb_upper,
                       "bb_lower": bb_lower, "bb_mid": bb_mid,
                       "bb_position": bb_position,
                       "vwap_stretch": vwap_stretch,
                       "ofi": ofi.ema_ofi if ofi else None},
        )
        signal.expires_at = datetime.utcnow() + timedelta(minutes=settings.SIGNAL_EXPIRY_MINUTES)
        atr = latest.get("atr")
        if atr and not pd.isna(atr):
            atr = float(atr)
            signal.suggested_entry = close
            signal.suggested_sl = close - atr*1.2 if direction=="long" else close + atr*1.2
            signal.suggested_tp = bb_mid
            risk   = abs(close - signal.suggested_sl)
            reward = abs(close - signal.suggested_tp)
            signal.risk_reward = reward/risk if risk > 0 else None
        self._last_signals[dedup_key] = datetime.utcnow()
        logger.info(f"REVERSION: {pair} {direction} score={raw_score:.0f} rsi={rsi:.0f}")
        return signal

    def _detect_divergence(self, df, direction):
        if len(df) < 10:
            return False
        recent = df.iloc[-10:]
        closes = recent["close"].values
        rsis   = recent["rsi"].dropna().values
        if len(rsis) < 5:
            return False
        if direction == "long":
            return min(closes[-3:]) < min(closes[-8:-3]) and min(rsis[-3:]) > min(rsis[:-3])
        else:
            return max(closes[-3:]) > max(closes[-8:-3]) and max(rsis[-3:]) < max(rsis[:-3])

    def _confirms(self, df, direction):
        if df is None or len(df) < 2:
            return False
        rsi = df.iloc[-1].get("rsi")
        if rsi is None or pd.isna(rsi):
            return False
        return float(rsi) < 40 if direction=="long" else float(rsi) > 60
