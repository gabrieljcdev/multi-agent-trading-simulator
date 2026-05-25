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
