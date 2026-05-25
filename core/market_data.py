"""
core/market_data.py
Exchange connections, WebSocket streaming, and candle management.
"""
import asyncio
import logging
import os
import time
from collections import deque, namedtuple
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

# Lightweight order-book shapes for the scalp v2 confluence depth gate —
# get_order_book() returns these so callers can use .price / .size and slice
# .bids / .asks (the ConfluenceChecker's expected interface).
OBLevel   = namedtuple("OBLevel", ["price", "size"])
OrderBook = namedtuple("OrderBook", ["bids", "asks"])

def _make_exchange(name):
    configs = {
        # Binance is a PUBLIC market-data feed here (OHLCV/orderbook) — the bot
        # never signs to trade on it. fetchCurrencies=False skips the SIGNED
        # sapi currencies call ccxt otherwise makes inside load_markets, which
        # was failing with -1021 ("timestamp outside recvWindow") under WSL2
        # clock drift + busy-startup latency. adjustForTimeDifference +
        # recvWindow give any future signed call slack as well.
        "binance": {"apiKey": os.getenv("BINANCE_API_KEY"), "secret": os.getenv("BINANCE_SECRET"), "options": {"defaultType": "spot", "adjustForTimeDifference": True, "recvWindow": 10000, "fetchCurrencies": False}},
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
    # Per-(exchange, pair) ring buffer of (timestamp, mid_price) samples,
    # fed by every order-book update from _stream_orderbooks. Powers the
    # short-window get_change_pct accessor — the scalping agent's BTC
    # correlation guard wants a ~60s lookback, finer than 5m candles
    # allow. 5 minutes of buffer holds enough headroom for windows up
    # to ~300s without rewriting older points on every poll.
    PRICE_HISTORY_WINDOW_SEC = 300

    def __init__(self, dashboard=None):
        self._exchanges:   dict = {}
        self._candles:     dict = {}
        self._last_price:  dict = {}
        # Most recent order book per (exchange, pair) — same shape as
        # _last_price, populated from _stream_orderbooks. Consumed by
        # get_spread_bps; the scalping agent reads it for its spread
        # gate. Empty dict on fresh boot.
        self._last_book:   dict = {}
        # Short-window (timestamp, mid) history per (exchange, pair).
        # Append-only, capped by trim_history() at PRICE_HISTORY_WINDOW_SEC.
        self._price_history: dict = {}
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

    def get_book(self, exchange, pair):
        """Most recent order book snapshot for (exchange, pair), or None
        before any tick has arrived. Shape matches ccxt:
            {"bids": [(price, size), ...], "asks": [(price, size), ...]}"""
        return self._last_book.get((exchange, pair))

    def get_spread_bps(self, exchange, pair):
        """Top-of-book spread in basis points, or None when no book is
        cached for the pair on this exchange. Scalping agent's gate 9
        reads this; conservatively returns None on any malformed book."""
        ob = self._last_book.get((exchange, pair))
        if not ob:
            return None
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return None
        try:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
        except (IndexError, TypeError, ValueError):
            return None
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid * 10000.0

    def get_change_pct(self, exchange, pair, window_sec):
        """% change between the latest mid sample and the one nearest
        `window_sec` seconds ago, or None when insufficient history.

        Powers the scalping agent's BTC 1m correlation guard. The
        candles cadence (5m) is too coarse for a sub-minute window —
        this taps the orderbook-stream sample buffer instead.
        Returns None (not 0.0) so the caller can distinguish "no
        data yet" from "no movement".
        """
        history = self._price_history.get((exchange, pair))
        if not history or len(history) < 2:
            return None
        now_ts, now_price = history[-1]
        if now_price <= 0 or window_sec <= 0:
            return None
        target_ts = now_ts - float(window_sec)
        # Walk backwards from newest to oldest, take the first sample
        # at-or-before the target — closest match without scanning the
        # whole deque twice.
        baseline_price = None
        for ts, price in reversed(history):
            if ts <= target_ts and price > 0:
                baseline_price = price
                break
        if baseline_price is None:
            # Window pre-dates our oldest sample — not enough history.
            return None
        return (now_price - baseline_price) / baseline_price * 100.0

    def _record_price_sample(self, exchange, pair, mid_price):
        """Append a (now, mid) sample to the per-pair history buffer.

        Called from _stream_orderbooks on every book update so the
        sampling rate matches the WebSocket tick rate. Trims samples
        older than PRICE_HISTORY_WINDOW_SEC each call — bounded memory
        per pair regardless of how long the bot runs.
        """
        if mid_price is None or mid_price <= 0:
            return
        key = (exchange, pair)
        buf = self._price_history.get(key)
        if buf is None:
            buf = deque()
            self._price_history[key] = buf
        now = time.time()
        buf.append((now, float(mid_price)))
        cutoff = now - self.PRICE_HISTORY_WINDOW_SEC
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    # ── Scalp v2 selectivity accessors ──────────────────────────────────
    # The ConfluenceChecker / ATRStopCalculator (scalping_v2) read these.
    # All are None-safe — the gates fail open on None. Indicator values come
    # from the cached candle DataFrames (which already carry atr/ema/vwap/
    # volume); timeframes the bot doesn't stream (e.g. 1m) return None and the
    # consuming gate degrades gracefully.

    def get_mid_price(self, symbol, exchange):
        """Top-of-book mid for (symbol, exchange); falls back to last price."""
        ob = self._last_book.get((exchange, symbol))
        if ob:
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if bids and asks:
                try:
                    return (float(bids[0][0]) + float(asks[0][0])) / 2.0
                except (IndexError, TypeError, ValueError):
                    pass
        p = self._last_price.get((exchange, symbol))
        return float(p) if p is not None else None

    def get_mid_price_at_offset(self, symbol, exchange, offset_ms):
        """Mid price ~offset_ms ago from the short-window sample buffer, or
        None if there's no sample that old. Powers the adverse-selection gate."""
        hist = self._price_history.get((exchange, symbol))
        if not hist:
            return None
        target = time.time() - float(offset_ms) / 1000.0
        for ts, price in reversed(hist):
            if ts <= target and price > 0:
                return float(price)
        return None

    def _candle_tf(self, exchange, symbol, preferred):
        """Return `preferred` tf if we have candles for it, else the fastest
        streamed timeframe we do have. None if no candles at all."""
        if self.get_candles(exchange, symbol, preferred) is not None:
            return preferred
        for tf in settings.TIMEFRAMES:
            if self.get_candles(exchange, symbol, tf) is not None:
                return tf
        return None

    @staticmethod
    def _last_finite(series):
        try:
            val = series.iloc[-1]
            return float(val) if pd.notna(val) else None
        except Exception:
            return None

    def get_session_vwap(self, symbol, exchange):
        """Latest VWAP from the fastest available candle frame."""
        tf = self._candle_tf(exchange, symbol, settings.FAST_TIMEFRAME)
        if tf is None:
            return None
        df = self.get_candles(exchange, symbol, tf)
        if df is None or df.empty or "vwap" not in df.columns:
            return None
        return self._last_finite(df["vwap"])

    def get_ema(self, symbol, exchange, timeframe, period):
        """EMA(period) of close on `timeframe`. None if that frame is absent
        or too short (HTF gate uses 5m, which the bot streams)."""
        df = self.get_candles(exchange, symbol, timeframe)
        if df is None or df.empty or len(df) < int(period):
            return None
        try:
            ema = ta.trend.EMAIndicator(df["close"], window=int(period)).ema_indicator()
            return self._last_finite(ema)
        except Exception as e:
            logger.debug(f"get_ema {symbol} {exchange} {timeframe}/{period}: {e}")
            return None

    def get_atr(self, symbol, exchange, period, timeframe):
        """ATR(period) on `timeframe` in price units. None if that frame is
        absent — the ATR stop calculator then falls back to its base SL."""
        df = self.get_candles(exchange, symbol, timeframe)
        if df is None or df.empty or len(df) < int(period):
            return None
        try:
            atr = ta.volatility.AverageTrueRange(
                df["high"], df["low"], df["close"], window=int(period),
            ).average_true_range()
            return self._last_finite(atr)
        except Exception as e:
            logger.debug(f"get_atr {symbol} {exchange} {timeframe}/{period}: {e}")
            return None

    def get_current_minute_volume(self, symbol, exchange):
        """Latest candle volume from the fastest available frame (the volume
        gate's ratio is scale-invariant, so a 5m frame works when 1m isn't
        streamed)."""
        tf = self._candle_tf(exchange, symbol, "1m")
        if tf is None:
            return None
        df = self.get_candles(exchange, symbol, tf)
        if df is None or df.empty or "volume" not in df.columns:
            return None
        return self._last_finite(df["volume"])

    def get_rolling_median_volume(self, symbol, exchange, timeframe, lookback):
        """Median volume over the last `lookback` candles on the requested
        frame (falling back to the fastest available)."""
        tf = self._candle_tf(exchange, symbol, timeframe)
        if tf is None:
            return None
        df = self.get_candles(exchange, symbol, tf)
        if df is None or df.empty or "volume" not in df.columns:
            return None
        try:
            tail = df["volume"].tail(int(lookback)).dropna()
            if tail.empty:
                return None
            return float(tail.median())
        except Exception:
            return None

    def get_order_book(self, symbol, exchange, levels=5):
        """Cached order book as OrderBook(bids=[OBLevel(price,size)], asks=[…]),
        top `levels` per side. None when no book is cached."""
        ob = self._last_book.get((exchange, symbol))
        if not ob:
            return None
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not bids or not asks:
            return None
        try:
            b = [OBLevel(float(p), float(s)) for p, s in bids[:levels]]
            a = [OBLevel(float(p), float(s)) for p, s in asks[:levels]]
        except (TypeError, ValueError):
            return None
        return OrderBook(b, a)

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
        """Stream order books for the top-N active pairs — one small loop per
        symbol, run concurrently. ccxt.pro's watch_order_book(symbol) blocks
        until *that* symbol next ticks, so a single-symbol loop paces itself to
        the real update rate. (The previous design swept many symbols in one
        loop, where each call returned the cached book immediately and spun the
        event loop at 100% CPU.)"""
        if not hasattr(ex, "watch_order_book"):
            return
        pairs = self._active_pairs[:settings.ORDER_BOOK_STREAM_PAIRS]
        await asyncio.gather(
            *(self._stream_one_orderbook(exchange_name, ex, pair) for pair in pairs),
            return_exceptions=True,
        )

    async def _stream_one_orderbook(self, exchange_name, ex, pair):
        """One symbol's book-stream loop. Awaits the next update each iteration
        (no busy-spin); on error it backs off so a failing symbol can't spin."""
        while self._running:
            try:
                ob = await asyncio.wait_for(
                    ex.watch_order_book(pair, settings.ORDER_BOOK_DEPTH),
                    timeout=settings.ORDER_BOOK_WATCH_TIMEOUT_S,
                )
                # Persist the latest book so consumers (scalping agent's spread
                # gate, dashboard) can read it without subscribing to ofi_scorer.
                self._last_book[(exchange_name, pair)] = ob
                # Sample mid into the short-window history so the BTC
                # correlation guard (get_change_pct) has the sub-minute
                # resolution candles can't provide.
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                if bids and asks:
                    try:
                        mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
                        self._record_price_sample(exchange_name, pair, mid)
                    except (IndexError, TypeError, ValueError):
                        pass
                ofi_scorer.update_book(pair=pair, exchange=exchange_name,
                                       bids=ob.get("bids", []), asks=ob.get("asks", []))
            except asyncio.TimeoutError:
                continue   # no update within the window — just re-await
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"OB {exchange_name} {pair}: {e}")
                await asyncio.sleep(settings.ORDER_BOOK_ERROR_BACKOFF_S)
