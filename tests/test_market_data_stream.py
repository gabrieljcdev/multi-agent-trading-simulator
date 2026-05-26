"""Order-book streamer — one small loop per symbol, no busy-spin.

Regression for the event-loop CPU spin: watch_order_book was swept over many
symbols in a single loop, where each call returned the cached book immediately
and pinned the asyncio thread at 100%. Now there's one loop per symbol (each
awaits its own next update) with a backoff on the error path.
"""
import asyncio

import pytest

from core.market_data import MarketData


@pytest.mark.asyncio
async def test_stream_one_orderbook_processes_then_backs_off(monkeypatch):
    md = MarketData()
    md._running = True
    book = {"bids": [[100.0, 1.0]], "asks": [[100.5, 1.0]]}
    calls = {"watch": 0, "sleep": 0, "ofi": 0}

    class FakeEx:
        async def watch_order_book(self, pair, depth):
            calls["watch"] += 1
            if calls["watch"] == 1:
                return book
            if calls["watch"] == 2:
                raise RuntimeError("ws dropped")     # error path → must back off
            md._running = False                       # third call ends the loop
            return book

    async def fake_sleep(_s):
        calls["sleep"] += 1

    monkeypatch.setattr("core.market_data.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("core.market_data.ofi_scorer.update_book",
                        lambda **k: calls.__setitem__("ofi", calls["ofi"] + 1))

    await md._stream_one_orderbook("kraken", FakeEx(), "BTC/USDT")

    assert md._last_book[("kraken", "BTC/USDT")] == book   # processed a book
    assert calls["ofi"] >= 1                                # fed the OFI scorer
    assert calls["sleep"] >= 1                              # backed off on error (no spin)
    assert calls["watch"] >= 3


@pytest.mark.asyncio
async def test_stream_one_orderbook_fires_book_callbacks(monkeypatch):
    """Regression: the order-book stream must fan every tick out to
    on_book_update subscribers (the scalp agent's OFIEngine). The whole
    'zero scalp_observations' bug was that nothing was wired to the stream."""
    md = MarketData()
    md._running = True
    book = {"bids": [[100.0, 1.0]], "asks": [[100.5, 1.0]]}
    received = []

    class FakeEx:
        async def watch_order_book(self, pair, depth):
            if md._running:
                md._running = False          # one tick, then stop the loop
                return book
            return book

    md.on_book_update(lambda exchange, pair, bids, asks:
                      received.append((exchange, pair, bids, asks)))
    monkeypatch.setattr("core.market_data.ofi_scorer.update_book", lambda **k: None)

    await md._stream_one_orderbook("mexc", FakeEx(), "BTC/USDT")

    assert received == [("mexc", "BTC/USDT", book["bids"], book["asks"])]


def test_orderbook_pairs_for_scalp_venue_streams_full_universe():
    """A scalp venue (mexc) streams the whole SCALP_PAIRS universe, not just
    the volume-ranked top-N; a signal venue gets only the active top-N. Both
    are filtered to symbols the venue actually lists."""
    from config import settings

    md = MarketData()
    md._active_pairs = ["BTC/USDT", "ETH/USDT", "DEAD/USDT"]

    class FakeEx:
        # lists every scalp pair + the live actives, but NOT DEAD/USDT
        markets = {**{p: {} for p in settings.SCALP_PAIRS},
                   "BTC/USDT": {}, "ETH/USDT": {}}

    mexc_pairs = md._orderbook_pairs_for("mexc", FakeEx())
    assert set(settings.SCALP_PAIRS).issubset(set(mexc_pairs))   # full universe
    assert "DEAD/USDT" not in mexc_pairs                          # dead listing filtered

    binance_pairs = md._orderbook_pairs_for("binance", FakeEx())
    assert "BTC/USDT" in binance_pairs
    assert "JUP/USDT" not in binance_pairs   # scalp-only pair isn't streamed on a signal venue


@pytest.mark.asyncio
async def test_stream_orderbooks_shards_over_cap(monkeypatch):
    """When a venue's symbol set exceeds the per-connection cap, the streamer
    shards across dedicated ws clients so every symbol gets a subscription
    (MEXC's ~30-sub limit on the 96-pair scalp universe)."""
    md = MarketData()
    md._running = True
    pairs = [f"P{i}/USDT" for i in range(10)]

    class FakeEx:
        def __init__(self):
            self.has = {"watchOrderBook": True}
            self.markets = {p: {} for p in pairs}

        async def load_markets(self):
            return self.markets

    md._active_pairs = pairs
    monkeypatch.setattr("core.market_data.settings.ORDER_BOOK_MAX_STREAMS_PER_CONN", 4)

    made = []
    monkeypatch.setattr("core.market_data._make_exchange",
                        lambda name: (made.append(FakeEx()), made[-1])[1])

    streamed = []

    async def fake_one(exn, ex, pair):
        streamed.append(pair)

    monkeypatch.setattr(md, "_stream_one_orderbook", fake_one)

    await md._stream_orderbooks("mexc", FakeEx())

    assert sorted(streamed) == sorted(pairs)   # every symbol streamed
    assert len(md._ob_conns) == 3              # 10 pairs / 4 per conn -> 3 shards
    assert len(made) == 3                      # one dedicated client per shard


@pytest.mark.asyncio
async def test_stream_one_orderbook_skips_when_not_running():
    md = MarketData()
    md._running = False

    class FakeEx:
        async def watch_order_book(self, pair, depth):
            raise AssertionError("must not be called when not running")

    await md._stream_one_orderbook("kraken", FakeEx(), "BTC/USDT")   # returns at once


@pytest.mark.asyncio
async def test_stream_orderbooks_noop_without_watch():
    md = MarketData()
    md._running = True

    class RestEx:   # plain ccxt — no watch_order_book attribute
        pass

    await md._stream_orderbooks("binance", RestEx())   # must return, not raise


@pytest.mark.asyncio
async def test_stream_one_candle_processes_then_backs_off(monkeypatch):
    md = MarketData()
    md._running = True
    candle = [1700000000000, 100.0, 101.0, 99.0, 100.5, 10.0]   # ts,o,h,l,c,v
    calls = {"watch": 0, "sleep": 0, "proc": 0}

    class FakeEx:
        async def watch_ohlcv(self, pair, tf):
            calls["watch"] += 1
            if calls["watch"] == 1:
                return [candle]
            if calls["watch"] == 2:
                raise RuntimeError("ws dropped")     # error path → must back off
            md._running = False
            return [candle]

    async def fake_sleep(_s):
        calls["sleep"] += 1

    async def fake_process(exn, pair, tf, raw):
        calls["proc"] += 1

    monkeypatch.setattr("core.market_data.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(md, "_process_candle", fake_process)
    monkeypatch.setattr(md, "_report_health", lambda *a, **k: None)

    await md._stream_one_candle("kraken", FakeEx(), "BTC/USDT", "5m")

    assert calls["proc"] >= 1     # processed a bar
    assert calls["sleep"] >= 1    # backed off on error (no spin)
    assert calls["watch"] >= 3


@pytest.mark.asyncio
async def test_stream_candles_noop_without_watch():
    md = MarketData()
    md._running = True

    class RestEx:   # plain ccxt — no watch_ohlcv attribute
        pass

    await md._stream_candles("binance", RestEx())   # must return, not raise
