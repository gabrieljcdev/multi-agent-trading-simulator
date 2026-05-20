"""
core/market_data.py
Exchange connections, WebSocket streaming, and candle management.
"""
import asyncio
import logging
import os
import time
import pandas as pd
import ta
import ccxt.async_support as ccxt
from dotenv import load_dotenv
from config import settings
from core.regime_detector import regime_detector
from signals.ofi import ofi_scorer
from database.queries import save_candle

load_dotenv(dotenv_path="config/keys.env")
logger = logging.getLogger(__name__)

def _make_exchange(name):
    configs = {
        "binance": {"apiKey": os.getenv("BINANCE_API_KEY"), "secret": os.getenv("BINANCE_SECRET"), "options": {"defaultType": "spot"}},
        "kraken":  {"apiKey": os.getenv("KRAKEN_API_KEY"),  "secret": os.getenv("KRAKEN_SECRET")},
        "bybit":   {"apiKey": os.getenv("BYBIT_API_KEY"),   "secret": os.getenv("BYBIT_SECRET"),  "options": {"defaultType": "spot"}},
        "okx":     {"apiKey": os.getenv("OKX_API_KEY"),     "secret": os.getenv("OKX_SECRET"),    "password": os.getenv("OKX_PASSPHRASE"), "options": {"defaultType": "spot"}},
    }
    cls  = getattr(ccxt, name)
    conf = configs.get(name, {})
    return cls({**conf, "enableRateLimit": True})

def _compute_indicators(df):
    if len(df) < 30:
        return df
    try:
        df["rsi"]        = ta.momentum.RSIIndicator(df["close"], window=settings.RSI_PERIOD).rsi()
        macd             = ta.trend.MACD(df["close"], window_slow=settings.MACD_SLOW, window_fast=settings.MACD_FAST, window_sign=settings.MACD_SIGNAL)
        df["macd"]       = macd.macd()
        df["macd_signal"]= macd.macd_signal()
        df["macd_hist"]  = macd.macd_diff()
        bb               = ta.volatility.BollingerBands(df["close"], window=settings.BB_PERIOD, window_dev=settings.BB_STDDEV)
        df["bb_upper"]   = bb.bollinger_hband()
        df["bb_mid"]     = bb.bollinger_mavg()
        df["bb_lower"]   = bb.bollinger_lband()
        df["ema_fast"]   = ta.trend.EMAIndicator(df["close"], window=settings.EMA_FAST).ema_indicator()
        df["ema_slow"]   = ta.trend.EMAIndicator(df["close"], window=settings.EMA_SLOW).ema_indicator()
        df["ema_trend"]  = ta.trend.EMAIndicator(df["close"], window=settings.EMA_TREND).ema_indicator()
        df["atr"]        = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
        df["adx"]        = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
        df["volume_sma"] = ta.trend.SMAIndicator(df["volume"], window=20).sma_indicator()
        try:
            df["vwap"]   = ta.volume.VolumeWeightedAveragePrice(df["high"], df["low"], df["close"], df["volume"]).volume_weighted_average_price()
        except:
            df["vwap"]   = df["close"].rolling(20).mean()
    except Exception as e:
        logger.debug(f"Indicator error: {e}")
    return df

class MarketData:
    def __init__(self, dashboard=None):
        self._exchanges:   dict = {}
        self._candles:     dict = {}
        self._last_price:  dict = {}
        self._callbacks:   list = []
        self._active_pairs: list = []
        self._running = False
        # Optional Dashboard reference; main.py wires this via set_dashboard
        # AFTER bot construction (Dashboard needs the bot, bot owns market_data).
        self._dashboard = dashboard

    def set_dashboard(self, dashboard) -> None:
        """Late-bind the dashboard so health updates flow once it exists."""
        self._dashboard = dashboard

    def _report_health(self, exchange: str, latency_ms: float, connected: bool) -> None:
        """Safely push a health update — a broken dashboard must never break streaming."""
        dash = self._dashboard
        if dash is None:
            return
        try:
            dash.update_exchange_health(exchange, latency_ms, connected)
        except Exception as e:
            logger.debug(f"dashboard health update failed: {e}")

    def on_candle_close(self, fn):
        self._callbacks.append(fn)

    def get_candles(self, exchange, pair, timeframe):
        return self._candles.get((exchange, pair, timeframe))

    def get_latest_candle(self, exchange, pair, timeframe):
        df = self._candles.get((exchange, pair, timeframe))
        if df is None or df.empty:
            return None
        return df.iloc[-1]

    def get_price(self, exchange, pair):
        return self._last_price.get((exchange, pair))

    def get_all_prices(self, pair):
        return {ex: price for (ex, p), price in self._last_price.items() if p == pair}

    def active_pairs(self):
        return self._active_pairs

    async def start(self):
        self._running = True
        logger.info("Initialising market data...")
        for name in settings.ENABLED_EXCHANGES:
            try:
                ex = _make_exchange(name)
                await ex.load_markets()
                self._exchanges[name] = ex
                logger.info(f"  {name}: connected")
            except Exception as e:
                logger.error(f"  {name}: failed — {e}")
        if not self._exchanges:
            raise RuntimeError("No exchanges connected")
        self._active_pairs = await self._resolve_pairs()
        logger.info(f"Monitoring {len(self._active_pairs)} pairs")
        await self._load_history()
        tasks = []
        for name, ex in self._exchanges.items():
            tasks.append(self._stream_candles(name, ex))
            tasks.append(self._stream_orderbooks(name, ex))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        self._running = False
        for ex in self._exchanges.values():
            try:
                await ex.close()
            except:
                pass

    async def _resolve_pairs(self):
        if settings.PAIR_UNIVERSE == "manual":
            return settings.FALLBACK_PAIRS
        try:
            primary = list(self._exchanges.values())[0]
            tickers = await primary.fetch_tickers()
            usdt = {s: t for s, t in tickers.items() if s.endswith("/USDT") and t.get("quoteVolume")}
            sorted_pairs = sorted(usdt.items(), key=lambda x: x[1].get("quoteVolume", 0), reverse=True)
            pairs = [s for s, _ in sorted_pairs[:settings.PAIR_UNIVERSE_TOP_N]]
            return pairs if pairs else settings.FALLBACK_PAIRS
        except Exception as e:
            logger.warning(f"Pair resolution failed: {e}")
            return settings.FALLBACK_PAIRS

    async def _load_history(self):
        logger.info("Loading candle history...")
        tasks = []
        for name, ex in self._exchanges.items():
            for pair in self._active_pairs[:20]:
                for tf in settings.TIMEFRAMES:
                    tasks.append(self._fetch_candles(name, ex, pair, tf))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        loaded  = sum(1 for r in results if not isinstance(r, Exception))
        logger.info(f"Loaded {loaded}/{len(tasks)} candle series")

    async def _fetch_candles(self, exchange_name, ex, pair, tf):
        limit = settings.CANDLE_LOOKBACK.get(tf, 200)
        try:
            ohlcv = await ex.fetch_ohlcv(pair, tf, limit=limit)
            if not ohlcv:
                return
            df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df = df.set_index("timestamp")
            df = _compute_indicators(df)
            self._candles[(exchange_name, pair, tf)] = df
            if len(df) > 0:
                self._last_price[(exchange_name, pair)] = float(df.iloc[-1]["close"])
            for _, row in df.iterrows():
                self._update_regime(pair, tf, row)
        except Exception as e:
            logger.debug(f"Candle fetch {exchange_name} {pair} {tf}: {e}")

    def _update_regime(self, pair, tf, row):
        try:
            regime_detector.update(
                pair=pair, timeframe=tf,
                close=float(row["close"]), high=float(row["high"]), low=float(row["low"]),
                adx=float(row["adx"])       if "adx"      in row.index and pd.notna(row["adx"])      else None,
                atr=float(row["atr"])       if "atr"      in row.index and pd.notna(row["atr"])      else None,
                bb_upper=float(row["bb_upper"]) if "bb_upper" in row.index and pd.notna(row["bb_upper"]) else None,
                bb_lower=float(row["bb_lower"]) if "bb_lower" in row.index and pd.notna(row["bb_lower"]) else None,
                bb_mid=float(row["bb_mid"])     if "bb_mid"   in row.index and pd.notna(row["bb_mid"])   else None,
            )
        except Exception as e:
            logger.debug(f"Regime update error: {e}")

    async def _stream_candles(self, exchange_name, ex):
        if not hasattr(ex, "watch_ohlcv"):
            return
        while self._running:
            try:
                for pair in self._active_pairs[:20]:
                    for tf in settings.TIMEFRAMES:
                        t0 = time.perf_counter()
                        try:
                            ohlcv = await asyncio.wait_for(
                                ex.watch_ohlcv(pair, tf), timeout=10.0)
                            latency_ms = (time.perf_counter() - t0) * 1000.0
                            if ohlcv:
                                await self._process_candle(exchange_name, pair, tf, ohlcv[-1])
                            # Successful tick (or empty payload) — exchange is reachable
                            self._report_health(exchange_name, latency_ms, connected=True)
                        except asyncio.TimeoutError:
                            self._report_health(exchange_name,
                                                (time.perf_counter() - t0) * 1000.0,
                                                connected=False)
                        except Exception as e:
                            logger.debug(f"Stream {exchange_name} {pair} {tf}: {e}")
                            self._report_health(exchange_name,
                                                (time.perf_counter() - t0) * 1000.0,
                                                connected=False)
            except Exception as e:
                logger.warning(f"Stream error {exchange_name}: {e}")
                self._report_health(exchange_name, 0.0, connected=False)
                await asyncio.sleep(5)

    async def _process_candle(self, exchange_name, pair, tf, raw):
        ts, open_, high, low, close, volume = raw
        key = (exchange_name, pair, tf)
        self._last_price[(exchange_name, pair)] = close
        new_row = pd.DataFrame(
            [[open_, high, low, close, volume]],
            columns=["open","high","low","close","volume"],
            index=[pd.Timestamp(ts, unit="ms")]
        )
        if key not in self._candles or self._candles[key].empty:
            self._candles[key] = new_row
        else:
            df = self._candles[key]
            if new_row.index[0] in df.index:
                df.loc[new_row.index[0]] = new_row.iloc[0]
            else:
                df = pd.concat([df, new_row])
                max_len = settings.CANDLE_LOOKBACK.get(tf, 200) + 50
                if len(df) > max_len:
                    df = df.iloc[-max_len:]
                self._candles[key] = df
        df = self._candles[key]
        if len(df) >= 30:
            df = _compute_indicators(df)
            self._candles[key] = df
            self._update_regime(pair, tf, df.iloc[-1])
            for cb in self._callbacks:
                try:
                    await cb(exchange_name, pair, tf, df)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

    async def _stream_orderbooks(self, exchange_name, ex):
        if not hasattr(ex, "watch_order_book"):
            return
        while self._running:
            try:
                for pair in self._active_pairs[:20]:
                    try:
                        ob = await asyncio.wait_for(ex.watch_order_book(pair, settings.ORDER_BOOK_DEPTH), timeout=5.0)
                        ofi_scorer.update_book(pair=pair, exchange=exchange_name, bids=ob.get("bids",[]), asks=ob.get("asks",[]))
                    except asyncio.TimeoutError:
                        pass
                    except Exception as e:
                        logger.debug(f"OB {exchange_name} {pair}: {e}")
            except Exception as e:
                logger.warning(f"OB error {exchange_name}: {e}")
                await asyncio.sleep(5)
