"""Integration: the v2 selectivity layer wired into ScalpingAgent._evaluate_entry
(commit 4). Gates 1-13 are driven to pass, then ConfluenceChecker /
ATRStopCalculator are stubbed to exercise the agent's branch logic."""
import time
from unittest.mock import AsyncMock, patch

import pytest

from config import settings
from agents.scalping_agent import ScalpingAgent
from agents.scalping_confluence import CombinedConfluenceResult, ConfluenceResult
from agents.scalping_atr_sl import TpSlV2


def _agent():
    """ScalpingAgent in observation mode with gates 1-13 stubbed to pass."""
    with patch.object(settings, "SCALP_CAPITAL", 0.0):
        a = ScalpingAgent()
        a._capital = 0.0
        a.capital_allocation = 0.0
    a._get_mid_price = AsyncMock(return_value=50_000.0)
    a._get_spread_bps = AsyncMock(return_value=1.0)
    a._get_regime = AsyncMock(return_value="TRENDING")
    a._get_btc_1m_change = AsyncMock(return_value=0.0)
    eng = a._ofi_engine
    key = "BTC/USDT:mexc"
    eng._last_z[key] = settings.SCALP_OFI_Z_ENTRY + 0.5
    eng._last_tfi[key] = 1.0
    eng._last_bucket_close[key] = time.time()
    eng._persist[key] = settings.SCALP_OFI_PERSIST_TICKS
    return a


def _combined(passed, blocking_reason="", strength="MODERATE"):
    return CombinedConfluenceResult(
        passed=passed, blocking_reason=blocking_reason,
        confluence_score=2, strength_label=strength,
        cross_exchange_agrees=True, btc_compatible=True,
        adverse_selection_ok=True, depth_ok=True,
        individual_results=[
            ConfluenceResult(True,  "ok",  score=1.0, gate_name="VWAP"),
            ConfluenceResult(True,  "ok",  score=1.0, gate_name="HTF"),
            ConfluenceResult(False, "low", score=0.0, gate_name="VOLUME"),
        ],
    )


@pytest.mark.asyncio
async def test_v2_block_logs_prefixed_skip_with_entry_price(monkeypatch):
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", True)
    a = _agent()
    a.confluence.run_all_gates = lambda **kw: _combined(False, "adverse: mid dropped")

    await a._evaluate_entry("BTC/USDT", "mexc")

    obs = a._observations[-1]
    assert obs.would_entry is False
    assert obs.skip_reason == "V2:adverse: mid dropped"
    assert obs.entry_price == 50_000.0           # mid logged for skipped obs too
    assert obs.strength_label == "MODERATE"
    assert obs.vwap_aligned is True and obs.volume_adequate is False
    # No position opened on a skip.
    assert "BTC/USDT:mexc" not in a._positions


@pytest.mark.asyncio
async def test_v2_pass_uses_atr_tp_sl_and_labels(monkeypatch):
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", True)
    a = _agent()
    a.confluence.run_all_gates = lambda **kw: _combined(True, "", "VERY_STRONG")
    a.atr_calc.compute_tp_sl_v2 = lambda **kw: TpSlV2(
        tp_bps=3.0, sl_bps=2.5, rr_actual=1.2, base_sl_bps=1.875,
        atr_bps=8.3, atr_adjusted=True, sl_clamped="",
    )

    await a._evaluate_entry("BTC/USDT", "mexc")

    obs = a._observations[-1]
    assert obs.would_entry is True
    assert obs.tp_bps == 3.0 and obs.sl_bps == 2.5    # from the ATR calc, not FeeManager
    assert obs.atr_bps == 8.3 and obs.atr_adjusted is True
    assert obs.rr_actual == 1.2
    assert obs.strength_label == "VERY_STRONG"


@pytest.mark.asyncio
async def test_confluence_disabled_falls_back_to_v1_entry(monkeypatch):
    monkeypatch.setattr(settings, "SCALP_SESSION_START_UTC", 0)
    monkeypatch.setattr(settings, "SCALP_SESSION_END_UTC", 24)
    monkeypatch.setattr(settings, "SCALP_USE_CONFLUENCE", False)
    a = _agent()
    # confluence must NOT be consulted when the flag is off
    a.confluence.run_all_gates = lambda **kw: (_ for _ in ()).throw(AssertionError("called"))

    await a._evaluate_entry("BTC/USDT", "mexc")

    obs = a._observations[-1]
    assert obs.would_entry is True
    assert obs.strength_label is None            # v2 not run → diagnostics stay null
