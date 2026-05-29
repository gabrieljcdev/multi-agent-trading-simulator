"""
tests/test_funding_arb.py — FundingEngine + FundingArbAgent (Phase 1).

20 tests covering APR annualisation, scan/filter, depth gate, concurrency
primitives, sim slippage, exit-reason priority, observation-mode
zero-routing guarantee, circuit breakers, stats / availability, kill
switch, DB round-trip, and coordinator wiring.

Style mirrors tests/test_arb_engine.py / test_scalping_agent.py: small
helpers, AsyncMock for any network, patched DB so no SQLite writes
unless a specific test exercises the round trip.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import settings
from execution.funding_engine import (
    FundingEngine,
    FundingOpportunity,
    FundingPosition,
    funding_engine as global_funding_engine,
)
from agents.funding_arb_agent import FundingArbAgent


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────

def _binance_stub(rate_8h: float = 0.0001, oi_usd: float | None = None) -> MagicMock:
    """Build a CCXT-shaped binance stub returning a fixed funding rate.

    rate_8h is the fractional 8h funding (e.g. 0.0001 = 0.01%). Passing
    oi_usd=None makes fetch_open_interest absent so the engine falls
    through to depth_ok=True per spec.
    """
    ex = MagicMock()
    ex.fetch_funding_rate = AsyncMock(return_value={"fundingRate": rate_8h})
    if oi_usd is not None:
        ex.fetch_open_interest = AsyncMock(
            return_value={"openInterestAmount": float(oi_usd)},
        )
    else:
        # The engine checks `getattr(ex, "fetch_open_interest", None)` and
        # skips if absent — but MagicMock auto-creates attrs. Pop the
        # auto-created mock so the engine sees None.
        if hasattr(ex, "fetch_open_interest"):
            del ex.fetch_open_interest
    ex.close = AsyncMock()
    return ex


def _engine_with(rate_8h: float = 0.0001, oi_usd: float | None = None) -> FundingEngine:
    """Fresh FundingEngine wired to a binance stub. Concurrency primitives
    (locks, semaphore) are independent of the module singleton."""
    stub = _binance_stub(rate_8h=rate_8h, oi_usd=oi_usd)
    eng = FundingEngine(ccxt_factory=lambda: stub)
    return eng


def _opp(
    symbol: str = "BTC/USDT",
    funding_apr: float = 0.15,
    oi_usd: float = 1_000_000.0,
    depth_ok: bool = True,
) -> FundingOpportunity:
    return FundingOpportunity(
        symbol=symbol, variant="delta_neutral",
        venue_long="binance", venue_short="binance",
        funding_apr=funding_apr, spread_apr=0.0,
        oi_usd=oi_usd, depth_ok=depth_ok,
    )


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Default: never touch SQLite via the engine's order-routing path.

    save_trade is patched out so the sim-slippage test can capture the
    arg dict without hitting SQLite. The funding-observation helpers
    are NOT patched here because doing so would shadow them on the
    database.queries module (the agent's `db_queries` alias IS that
    module), which the DB round-trip test in this file relies on.
    Individual tests that need the no-op stub patch it themselves.
    """
    monkeypatch.setattr(
        "execution.funding_engine.db_queries.save_trade",
        lambda *a, **k: 1,
    )


@pytest.fixture
def _force_observation(monkeypatch):
    """Phase 1 hard gate: observation mode True, SIM_MODE True."""
    monkeypatch.setattr(settings, "FUNDING_OBSERVATION_MODE", True)
    monkeypatch.setattr(settings, "SIM_MODE", True)


# ─────────────────────────────────────────────────────────────────────────
# 1. funding_apr annualisation (rate_8h × 1095)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funding_apr_annualisation_is_1095(monkeypatch):
    """funding_apr = rate_8h * 1095 — three 8h fundings/day * 365 days."""
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.0)
    eng = _engine_with(rate_8h=0.0001)
    opps = await eng.scan()
    assert len(opps) == 1
    # 0.0001 * 1095 = 0.1095
    assert opps[0].funding_apr == pytest.approx(0.0001 * 1095.0)


# ─────────────────────────────────────────────────────────────────────────
# 2. scan builds a delta_neutral opportunity per symbol
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_builds_delta_neutral_per_symbol(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT", "ETH/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.0)
    eng = _engine_with(rate_8h=0.0002)
    opps = await eng.scan()
    assert sorted(o.symbol for o in opps) == ["BTC/USDT", "ETH/USDT"]
    for o in opps:
        assert o.variant == "delta_neutral"
        assert o.venue_long == "binance"
        assert o.venue_short == "binance"


# ─────────────────────────────────────────────────────────────────────────
# 3. _filter drops sub-FUNDING_MIN_APR opportunities
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filter_drops_below_min_apr(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.5)
    # 0.0001 * 1095 = 0.1095 — far below 0.5.
    eng = _engine_with(rate_8h=0.0001)
    opps = await eng.scan()
    assert opps == []


# ─────────────────────────────────────────────────────────────────────────
# 3b. Negative funding → reverse_carry variant; symmetric |APR| filter
# ─────────────────────────────────────────────────────────────────────────
# Live Binance pull (2026-05-29) showed OP -25% APR, INJ -20%, TIA -5%
# — rich short-funding inversions the original one-sided filter ignored.
# These tests pin the new symmetric behaviour: emit reverse_carry for
# negative funding, accept it through _filter, and keep open() blocked
# on that variant as defence-in-depth.

@pytest.mark.asyncio
async def test_negative_funding_builds_reverse_carry_variant(monkeypatch):
    """A negative funding rate flips the carry direction — the agent should
    see variant='reverse_carry' so observation logging + downstream
    consumers can distinguish the two legs without re-deriving the sign."""
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["OP/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.0)
    # -0.0001 * 1095 = -0.1095 → negative funding → reverse_carry
    eng = _engine_with(rate_8h=-0.0001)
    opps = await eng.scan()
    assert len(opps) == 1
    assert opps[0].variant == "reverse_carry"
    assert opps[0].funding_apr == pytest.approx(-0.1095)


@pytest.mark.asyncio
async def test_filter_uses_abs_funding_apr(monkeypatch):
    """A -10% APR opportunity must pass the 6% floor — `|funding_apr| >= floor`."""
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["OP/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.06)
    # -0.0001 * 1095 = -0.1095 = -10.95% APR → |x| = 0.1095 ≥ 0.06 ✓
    eng = _engine_with(rate_8h=-0.0001)
    opps = await eng.scan()
    assert len(opps) == 1
    assert opps[0].variant == "reverse_carry"


@pytest.mark.asyncio
async def test_filter_drops_subthreshold_negative(monkeypatch):
    """A -3% APR opportunity must NOT pass a 6% floor (|−3| < 6)."""
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.06)
    # -0.00003 * 1095 = -0.03285 = -3.3% APR → |x| < 0.06
    eng = _engine_with(rate_8h=-0.00003)
    opps = await eng.scan()
    assert opps == []


def test_engine_binance_client_uses_future_market_type():
    """Regression (2026-05-29): the engine was instantiating
    ccxt.binance() with no defaultType, so it defaulted to spot — and
    binance.fetch_funding_rate raises NotSupported on spot ("supports
    linear and inverse contracts only"). Every scan tick silently
    dropped every symbol via the engine's DEBUG-level try/except,
    leaving funding_arb_observations empty. The constructor must pin
    defaultType to a perp/futures market so funding endpoints route to
    fapi.binance.com."""
    # Skip if ccxt unavailable in this venv — the engine itself tolerates
    # it (returns None from _get_exchange), but the test is meaningful
    # only when a real client can be built.
    pytest.importorskip("ccxt.async_support")
    eng = FundingEngine()
    client = eng._get_exchange()
    assert client is not None, "engine couldn't build a binance client"
    assert client.options.get("defaultType") == "future", (
        f"expected defaultType=future, got {client.options.get('defaultType')!r} "
        "— spot doesn't expose fetch_funding_rate, scans will all return None"
    )


@pytest.mark.asyncio
async def test_open_refuses_reverse_carry_variant(monkeypatch):
    """Defence in depth: open() must refuse reverse_carry even if the agent
    somehow reaches it (e.g. FUNDING_OBSERVATION_MODE flipped off without
    the Phase-2 leg map being wired). The guard logs + returns rather than
    routing a wrong-direction order pair."""
    monkeypatch.setattr(settings, "SIM_MODE", True, raising=False)
    monkeypatch.setattr(settings, "FUNDING_OBSERVATION_MODE", True, raising=False)
    eng = _engine_with(rate_8h=0.0001)
    # Build the inverse-variant opp manually so we don't depend on a
    # negative rate to trigger the guard.
    opp = FundingOpportunity(
        symbol="OP/USDT", variant="reverse_carry",
        venue_long="binance", venue_short="binance",
        funding_apr=-0.20, spread_apr=0.0, oi_usd=1e7, depth_ok=True,
    )
    # If the guard fails, _place would be reached and the patched
    # save_trade fixture would record the call. Spy on _place to be sure.
    with patch.object(eng, "_place", new=AsyncMock()) as place:
        await eng.open(opp)
        place.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────
# 4. depth gate — depth_ok rejects when oi_usd < min × notional
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_depth_gate_rejects_thin_oi(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.0)
    monkeypatch.setattr(settings, "FUNDING_MAX_NOTIONAL_USD", 250.0)
    monkeypatch.setattr(settings, "FUNDING_MIN_OI_MULT", 10.0)
    # Required OI = 250 * 10 = 2500. 500 < 2500 → depth_ok False.
    eng = _engine_with(rate_8h=0.0001, oi_usd=500.0)
    opps = await eng.scan()
    assert len(opps) == 1
    assert opps[0].depth_ok is False


# ─────────────────────────────────────────────────────────────────────────
# 5. open() fires both legs concurrently (asyncio.gather, not sequential)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_fires_legs_concurrently(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_OBSERVATION_MODE", True)
    monkeypatch.setattr(settings, "SIM_MODE", True)
    eng = _engine_with()

    call_times: list[float] = []

    async def _slow_place(pos, leg):
        # 50ms each — if the engine ran them sequentially, total > 100ms.
        call_times.append(time.perf_counter())
        await asyncio.sleep(0.05)
        return None

    monkeypatch.setattr(eng, "_place", _slow_place)
    t0 = time.perf_counter()
    await eng.open(_opp())
    elapsed = time.perf_counter() - t0
    assert len(call_times) == 2
    # Both calls must have entered roughly together (< 20ms apart) and the
    # whole gather completed in well under 100ms.
    assert abs(call_times[0] - call_times[1]) < 0.02
    assert elapsed < 0.1


# ─────────────────────────────────────────────────────────────────────────
# 6. per-symbol lock prevents double-open
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_symbol_lock_prevents_double_open(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_OBSERVATION_MODE", True)
    monkeypatch.setattr(settings, "SIM_MODE", True)
    eng = _engine_with()
    eng._symbol_locks["BTC/USDT"] = asyncio.Lock()

    calls = 0

    async def _track(pos, leg):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.03)

    monkeypatch.setattr(eng, "_place", _track)

    # Hold the lock while a second open() runs — it must early-return.
    async with eng._symbol_locks["BTC/USDT"]:
        # The second call sees locked()==True and returns immediately.
        await eng.open(_opp())

    assert calls == 0  # _place never reached while the lock was held


# ─────────────────────────────────────────────────────────────────────────
# 7. semaphore caps concurrent opens at FUNDING_MAX_CONCURRENT
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_opens(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_OBSERVATION_MODE", True)
    monkeypatch.setattr(settings, "SIM_MODE", True)
    monkeypatch.setattr(settings, "FUNDING_MAX_CONCURRENT", 1)
    # Re-instantiate so the semaphore picks up the new cap (it's read at
    # construction time, same as ArbEngine).
    stub = _binance_stub()
    eng = FundingEngine(ccxt_factory=lambda: stub)

    active = 0
    peak = 0

    async def _track(pos, leg):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1

    monkeypatch.setattr(eng, "_place", _track)
    await asyncio.gather(
        eng.open(_opp(symbol="BTC/USDT")),
        eng.open(_opp(symbol="ETH/USDT")),
    )
    # Each open() runs two _place legs concurrently — that's fine (the
    # cap is on whole open() calls). What it must prevent is the second
    # open() entering before the first completes; with cap=1 we expect
    # exactly 2 legs in flight at once, never 4.
    assert peak <= 2


# ─────────────────────────────────────────────────────────────────────────
# 8. sim fill applies ±FUNDING_SIM_SLIPPAGE_PCT
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sim_fill_applies_slippage(monkeypatch):
    monkeypatch.setattr(settings, "SIM_MODE", True)
    monkeypatch.setattr(settings, "FUNDING_OBSERVATION_MODE", True)
    monkeypatch.setattr(settings, "FUNDING_SIM_SLIPPAGE_PCT", 0.0002)

    captured: list[dict] = []

    def _capture(trade_data):
        captured.append(trade_data)
        return 42

    monkeypatch.setattr(
        "execution.funding_engine.db_queries.save_trade", _capture,
    )

    eng = _engine_with()
    pos = FundingPosition(
        opp=_opp(), notional_usd=250.0,
        margin_used=125.0, basis_at_entry=0.0,
    )
    await eng._place(pos, leg="long")
    await eng._place(pos, leg="short")
    assert len(captured) == 2
    long_fill  = next(c for c in captured if c["side"] == "long")["entry_price"]
    short_fill = next(c for c in captured if c["side"] == "short")["entry_price"]
    # Long pays slip; short receives. Around the unit-mid of 1.0.
    assert long_fill == pytest.approx(1.0 * (1.0 + 0.0002))
    assert short_fill == pytest.approx(1.0 * (1.0 - 0.0002))


# ─────────────────────────────────────────────────────────────────────────
# 9-13. exit_reason priority order — each reason, in order
# ─────────────────────────────────────────────────────────────────────────

def _pos_with(funding_apr: float = 0.15, age_sec: float = 0.0,
              healthy: bool = True) -> tuple[FundingEngine, FundingPosition]:
    eng = _engine_with()
    eng._venue_healthy = healthy
    pos = FundingPosition(
        opp=_opp(funding_apr=funding_apr),
        notional_usd=250.0, margin_used=125.0, basis_at_entry=0.0,
        opened_at=time.time() - age_sec,
    )
    return eng, pos


def test_exit_reason_funding_decay_first(monkeypatch):
    """Reason 1: funding_apr < FUNDING_FLIP_EXIT_APR — highest priority."""
    monkeypatch.setattr(settings, "FUNDING_FLIP_EXIT_APR", 0.0)
    eng, pos = _pos_with(funding_apr=-0.01)
    assert eng.exit_reason(pos) == "funding_decay"


def test_exit_reason_basis_blowout(monkeypatch):
    """Reason 2: _basis_blowout fires. Patch the helper so we reach it
    without setting up real basis state."""
    monkeypatch.setattr(settings, "FUNDING_FLIP_EXIT_APR", 0.0)
    eng, pos = _pos_with(funding_apr=0.15)
    monkeypatch.setattr(eng, "_basis_blowout", lambda p: True)
    assert eng.exit_reason(pos) == "basis_blowout"


def test_exit_reason_margin_breach(monkeypatch):
    """Reason 3: margin_breach beats venue_health and max_hold."""
    monkeypatch.setattr(settings, "FUNDING_FLIP_EXIT_APR", 0.0)
    eng, pos = _pos_with(funding_apr=0.15)
    monkeypatch.setattr(eng, "_margin_breach", lambda p: True)
    assert eng.exit_reason(pos) == "margin_breach"


def test_exit_reason_venue_health(monkeypatch):
    """Reason 4: venue_health when funding feed has been failing."""
    monkeypatch.setattr(settings, "FUNDING_FLIP_EXIT_APR", 0.0)
    eng, pos = _pos_with(funding_apr=0.15, healthy=False)
    assert eng.exit_reason(pos) == "venue_health"


def test_exit_reason_max_hold(monkeypatch):
    """Reason 5: age > FUNDING_MAX_HOLD_SEC — lowest priority, only fires
    when none of 1-4 match."""
    monkeypatch.setattr(settings, "FUNDING_FLIP_EXIT_APR", 0.0)
    monkeypatch.setattr(settings, "FUNDING_MAX_HOLD_SEC", 1.0)
    eng, pos = _pos_with(funding_apr=0.15, age_sec=10.0)
    assert eng.exit_reason(pos) == "max_hold"


def test_exit_reason_priority_order(monkeypatch):
    """Funding decay outranks basis blowout outranks margin breach."""
    monkeypatch.setattr(settings, "FUNDING_FLIP_EXIT_APR", 0.0)
    eng, pos = _pos_with(funding_apr=-0.01)
    monkeypatch.setattr(eng, "_basis_blowout", lambda p: True)
    monkeypatch.setattr(eng, "_margin_breach", lambda p: True)
    # All three could fire — funding_decay wins because it's listed first.
    assert eng.exit_reason(pos) == "funding_decay"


# ─────────────────────────────────────────────────────────────────────────
# 14. OBSERVATION MODE — would_enter rows written, ZERO orders routed
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_observation_mode_zero_routing(monkeypatch, _force_observation):
    """The hard gate. While observation mode is on, _place is never reached.

    We mock save_trade — the agent's loop should never call it because the
    loop bypasses engine.open() entirely when observation_mode is True.
    """
    monkeypatch.setattr(settings, "FUNDING_SCAN_INTERVAL_SEC", 1)
    monkeypatch.setattr(settings, "FUNDING_SYMBOLS", ["BTC/USDT"])
    monkeypatch.setattr(settings, "FUNDING_MIN_APR", 0.0)

    save_trade = MagicMock()
    monkeypatch.setattr(
        "execution.funding_engine.db_queries.save_trade", save_trade,
    )

    captured_rows: list = []

    def _capture_obs(rows):
        captured_rows.extend(rows)

    monkeypatch.setattr(
        "agents.funding_arb_agent.db_queries.save_funding_observations",
        _capture_obs,
    )

    agent = FundingArbAgent(engine=_engine_with(rate_8h=0.0001))
    # Tap a single scan tick directly — no loop, no sleep.
    opps = await agent._engine.scan()
    for opp in opps:
        await agent._log_observation(opp)

    # ZERO routing.
    assert save_trade.call_count == 0
    # Would-enter rows written.
    assert captured_rows, "observation should produce at least one row"
    assert all(r["would_enter"] for r in captured_rows)
    assert all(r["observation_only"] for r in captured_rows)


# ─────────────────────────────────────────────────────────────────────────
# 15. daily-loss circuit breaker halts the loop
# ─────────────────────────────────────────────────────────────────────────

def test_daily_loss_circuit_breaker_halts(monkeypatch):
    # %-based: halt fires when daily_loss reaches (PCT/100) * allocation.
    # Pick concrete alloc + PCT so the threshold is deterministic.
    monkeypatch.setattr(settings, "FUNDING_DAILY_LOSS_HALT_PCT", 2.0)
    agent = FundingArbAgent(engine=_engine_with())
    agent.capital_allocation = 250.0   # 2% of $250 → $5 halt
    agent._daily_loss = 6.0
    agent._check_circuit_breakers()
    assert agent._halted is True
    assert agent._halt_reason == "daily_loss"


def test_daily_loss_circuit_breaker_zero_alloc_noop(monkeypatch):
    """0-allocation observation-mode agent never halts on the daily-loss
    rule — never divides by zero, never silently stops a zero-capital
    agent from observing."""
    monkeypatch.setattr(settings, "FUNDING_DAILY_LOSS_HALT_PCT", 2.0)
    agent = FundingArbAgent(engine=_engine_with())
    agent.capital_allocation = 0.0
    agent._daily_loss = 1_000_000.0
    agent._check_circuit_breakers()
    assert agent._halted is False


def test_daily_loss_halt_scales_with_allocation(monkeypatch):
    """Doubling allocation doubles the USD loss tolerated — that's the
    whole point of the %-based rule (it scales with the fund)."""
    monkeypatch.setattr(settings, "FUNDING_DAILY_LOSS_HALT_PCT", 2.0)
    # Small fund, $5 halt
    a1 = FundingArbAgent(engine=_engine_with())
    a1.capital_allocation = 250.0
    a1._daily_loss = 4.99
    a1._check_circuit_breakers()
    assert a1._halted is False
    a1._daily_loss = 5.01
    a1._check_circuit_breakers()
    assert a1._halted is True

    # Double the fund → halt should ride up to $10
    a2 = FundingArbAgent(engine=_engine_with())
    a2.capital_allocation = 500.0
    a2._daily_loss = 9.99
    a2._check_circuit_breakers()
    assert a2._halted is False
    a2._daily_loss = 10.01
    a2._check_circuit_breakers()
    assert a2._halted is True


# ─────────────────────────────────────────────────────────────────────────
# 16. consecutive-loss circuit breaker halts the loop
# ─────────────────────────────────────────────────────────────────────────

def test_consecutive_loss_circuit_breaker_halts(monkeypatch):
    monkeypatch.setattr(settings, "FUNDING_CONSECUTIVE_LOSS_HALT", 3)
    agent = FundingArbAgent(engine=_engine_with())
    agent._consec_losses = 3
    agent._check_circuit_breakers()
    assert agent._halted is True
    assert agent._halt_reason == "consecutive_loss"


# ─────────────────────────────────────────────────────────────────────────
# 17. get_stats() shape + is_available() with empty env
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_stats_and_availability(monkeypatch):
    # Wipe exchange-key env vars so we're testing the "empty keys.env" path.
    for k in ("BINANCE_API_KEY", "BINANCE_SECRET", "COINGLASS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    agent = FundingArbAgent(engine=_engine_with())
    # Available without any key — Phase 1 reads funding via public CCXT.
    assert agent.is_available() is True
    stats = await agent.get_stats()
    # AgentStats shape — every documented field present.
    for field in ("agent_id", "status", "capital_allocated",
                  "capital_deployed", "daily_pnl", "daily_pnl_pct",
                  "total_pnl", "trades_today", "win_rate_today",
                  "win_rate_alltime", "consecutive_losses",
                  "last_trade_time", "error"):
        assert hasattr(stats, field)
    assert stats.agent_id == "funding_arb"


# ─────────────────────────────────────────────────────────────────────────
# 18. close_all_positions closes every open position via gather
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_close_all_positions_drains_via_gather(monkeypatch, _force_observation):
    """Kill-switch parity: every open position is closed concurrently."""
    agent = FundingArbAgent(engine=_engine_with())

    pos_a = FundingPosition(opp=_opp(symbol="BTC/USDT"),
                            notional_usd=100.0, margin_used=50.0,
                            basis_at_entry=0.0)
    pos_b = FundingPosition(opp=_opp(symbol="ETH/USDT"),
                            notional_usd=100.0, margin_used=50.0,
                            basis_at_entry=0.0)
    agent._positions = {"BTC/USDT": pos_a, "ETH/USDT": pos_b}

    closed_calls: list[str] = []
    in_flight = {"n": 0, "peak": 0}

    async def _slow_close(symbol, pos, reason):
        in_flight["n"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
        await asyncio.sleep(0.05)
        in_flight["n"] -= 1
        closed_calls.append(symbol)
        agent._positions.pop(symbol, None)

    monkeypatch.setattr(agent, "_close", _slow_close)
    t0 = time.perf_counter()
    await agent.close_all_positions()
    elapsed = time.perf_counter() - t0

    assert sorted(closed_calls) == ["BTC/USDT", "ETH/USDT"]
    # Concurrent: both should be in flight at the same time, total < 100ms.
    assert in_flight["peak"] == 2
    assert elapsed < 0.1


# ─────────────────────────────────────────────────────────────────────────
# 19. save_funding_observations + get_funding_summary round-trip (temp DB)
# ─────────────────────────────────────────────────────────────────────────

def test_db_roundtrip_observations_and_summary(tmp_path, monkeypatch):
    """End-to-end DB round trip against an isolated SQLite file.

    Avoids interfering with the project's data/cryptobot.db: a fresh
    engine is bound to a tmp_path file, every table is created, and
    save_funding_observations / get_funding_summary are exercised
    through the same query helpers production calls.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database import queries as q
    from database.models import Base

    test_db_path = tmp_path / "funding_test.db"
    test_engine = create_engine(
        f"sqlite:///{test_db_path}",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False,
                               autocommit=False, expire_on_commit=False)

    # Redirect get_session for the duration of the test. get_session
    # reads SessionLocal from database.db's module globals at call time,
    # so patching it here reroutes every query helper.
    monkeypatch.setattr(db_module, "engine", test_engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    now = time.time()
    rows = [
        # Two would-enter rows, one closed at +$1 net, one still open.
        {"timestamp": now - 7200, "symbol": "BTC/USDT", "variant": "delta_neutral",
         "venue_long": "binance", "venue_short": "binance",
         "funding_apr": 0.15, "spread_apr": 0.0, "oi_usd": 1e7, "depth_ok": True,
         "notional_usd": 250.0, "margin_used": 125.0, "basis_at_entry": 0.0,
         "projected_funding_per_interval": 0.1, "projected_fees": 0.1,
         "projected_net_apr": 0.149,
         "would_enter": True, "skip_reason": "", "observation_only": True},
        {"timestamp": now - 3600, "symbol": "ETH/USDT", "variant": "delta_neutral",
         "venue_long": "binance", "venue_short": "binance",
         "funding_apr": 0.18, "spread_apr": 0.0, "oi_usd": 5e6, "depth_ok": True,
         "notional_usd": 250.0, "margin_used": 125.0, "basis_at_entry": 0.0,
         "projected_funding_per_interval": 0.1, "projected_fees": 0.1,
         "projected_net_apr": 0.179,
         "would_enter": True, "skip_reason": "", "observation_only": True},
    ]
    q.save_funding_observations(rows)

    # Close the BTC row — this exercises the upsert path.
    close_row = dict(rows[0])
    close_row["exit_time"]    = now - 1800
    close_row["exit_reason"]  = "funding_decay"
    close_row["hold_sec"]     = 1800.0
    close_row["funding_collected"] = 2.0
    close_row["fees_paid"]    = 0.1
    close_row["pnl_usd"]      = 1.9
    q.save_funding_observations([close_row])

    summary = q.get_funding_summary(days=7)
    assert summary["total"]       == 2
    assert summary["would_enter"] == 2
    assert summary["closed"]      == 1
    assert summary["exit_reason"].get("funding_decay") == 1
    assert summary["mean_hold_hours"] == pytest.approx(0.5, rel=0.01)

    rows_back = q.get_funding_observations(limit=10)
    assert len(rows_back) == 2
    btc = next(r for r in rows_back if r["symbol"] == "BTC/USDT")
    assert btc["pnl_usd"] == pytest.approx(1.9)
    assert btc["exit_reason"] == "funding_decay"


# ─────────────────────────────────────────────────────────────────────────
# 20. coordinator picks up FundingArbAgent with no coordinator edits
# ─────────────────────────────────────────────────────────────────────────

def test_coordinator_picks_up_via_registry():
    """The whole point of the plugin pattern: REGISTERED_AGENTS contains
    an instance, and that instance is a BaseAgent. Coordinator code does
    not need to know about FundingArbAgent specifically."""
    from agents import REGISTERED_AGENTS
    from agents.base import BaseAgent

    funding = [a for a in REGISTERED_AGENTS
               if getattr(a, "agent_id", "") == "funding_arb"]
    assert len(funding) == 1, "exactly one FundingArbAgent instance registered"
    assert isinstance(funding[0], BaseAgent)
    # The instance must conform to the BaseAgent contract — required
    # methods all callable.
    for m in ("start", "stop", "get_stats", "close_all_positions"):
        assert callable(getattr(funding[0], m))


# Reset the module-level singleton's venue-health flag between tests so
# venue_unhealthy state from one test doesn't leak into another.
@pytest.fixture(autouse=True)
def _reset_global_engine_health():
    yield
    global_funding_engine._venue_healthy = True
