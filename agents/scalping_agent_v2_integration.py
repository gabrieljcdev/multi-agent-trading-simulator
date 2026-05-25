"""
Scalping agent v2 — integration with the existing 13-gate flow.

This module shows the new _evaluate_signal_v2() method that replaces the
existing _evaluate_signal() in agents/scalping_agent.py.

The change is additive — gates 1-13 run unchanged, then v2 selectivity gates
run, then the FeeManager call is replaced with the ATR-aware version.

To deploy:
  1. Append settings_scalp_v2 constants to config/settings.py
  2. Add scalping_confluence.py + scalping_atr_sl.py to agents/
  3. Replace _evaluate_signal in ScalpingAgent with the body below
  4. Add the new database columns (see DB_COLUMNS_V2)
  5. Re-run observation mode

Database columns to add to scalp_observations:
  - confluence_score INTEGER  (0-3, count of soft gates passed)
  - strength_label TEXT       (WEAK/MODERATE/STRONG/VERY_STRONG)
  - cross_exchange_agrees BOOLEAN
  - btc_compatible BOOLEAN
  - adverse_selection_ok BOOLEAN
  - depth_ok BOOLEAN
  - vwap_aligned BOOLEAN
  - htf_aligned BOOLEAN
  - volume_adequate BOOLEAN
  - atr_bps FLOAT
  - atr_adjusted BOOLEAN
  - sl_clamped TEXT           (FLOOR/CEILING/'')
  - rr_actual FLOAT

These are nullable so existing observations remain valid.
"""

import logging
import time
from dataclasses import asdict
from typing import Optional, Dict, Any

logger = logging.getLogger("scalping_v2.integration")


DB_COLUMNS_V2 = """
-- Append to database/migrations or apply via SQLAlchemy add_column
ALTER TABLE scalp_observations ADD COLUMN confluence_score INTEGER;
ALTER TABLE scalp_observations ADD COLUMN strength_label TEXT;
ALTER TABLE scalp_observations ADD COLUMN cross_exchange_agrees BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN btc_compatible BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN adverse_selection_ok BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN depth_ok BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN vwap_aligned BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN htf_aligned BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN volume_adequate BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN atr_bps REAL;
ALTER TABLE scalp_observations ADD COLUMN atr_adjusted BOOLEAN;
ALTER TABLE scalp_observations ADD COLUMN sl_clamped TEXT;
ALTER TABLE scalp_observations ADD COLUMN rr_actual REAL;
"""


def evaluate_signal_v2(agent, signal, confluence_checker, atr_calc) -> Optional[Dict[str, Any]]:
    """The replacement for ScalpingAgent._evaluate_signal().

    Parameters
    ----------
    agent
        The ScalpingAgent instance — used for self.fee_manager, self.market_data, etc.
    signal
        The OFI signal proposed by OFIEngine (must have .symbol, .exchange,
        .direction, .ofi_z, .timestamp).
    confluence_checker
        ConfluenceChecker instance (constructed once in agent __init__).
    atr_calc
        ATRStopCalculator instance (constructed once in agent __init__).

    Returns
    -------
    dict
        The observation record to insert into scalp_observations. If the signal
        is skipped, observation_only=True and skip_reason is set.
    """
    obs = _initial_observation(signal)

    # === Existing gates 1-13 run first ===
    # In real integration these are the original gate methods on the agent.
    # We call a unified passthrough here for clarity.
    legacy_result = _run_legacy_gates(agent, signal, obs)
    if legacy_result is not None:
        # legacy_result is a populated skip observation
        return legacy_result

    # === V2 selectivity gates ===
    fee = agent.fee_manager.get_round_trip_bps(signal.exchange)
    position_size_usd = _compute_position_size_usd(agent, signal)

    conf = confluence_checker.run_all_gates(
        symbol=signal.symbol,
        exchange=signal.exchange,
        direction=signal.direction,
        primary_z=signal.ofi_z,
        position_size_usd=position_size_usd,
    )

    # Annotate observation with all v2 diagnostics regardless of outcome
    obs.update({
        "confluence_score": conf.confluence_score,
        "strength_label": conf.strength_label,
        "cross_exchange_agrees": conf.cross_exchange_agrees,
        "btc_compatible": conf.btc_compatible,
        "adverse_selection_ok": conf.adverse_selection_ok,
        "depth_ok": conf.depth_ok,
    })
    # Track which soft gates passed individually
    for r in conf.individual_results:
        if r.gate_name == "VWAP":
            obs["vwap_aligned"] = r.passed and r.score >= 0.99
        elif r.gate_name == "HTF":
            obs["htf_aligned"] = r.passed and r.score >= 0.99
        elif r.gate_name == "VOLUME":
            obs["volume_adequate"] = r.passed and r.score >= 0.99

    if not conf.passed:
        obs["would_entry"] = False
        obs["skip_reason"] = f"V2:{conf.blocking_reason}"
        return obs

    # === ATR-aware TP/SL ===
    tpsl = atr_calc.compute_tp_sl_v2(
        symbol=signal.symbol,
        exchange=signal.exchange,
        round_trip_bps=fee,
    )
    obs.update({
        "tp_bps": tpsl.tp_bps,
        "sl_bps": tpsl.sl_bps,
        "round_trip_cost_bps": fee,
        "atr_bps": tpsl.atr_bps,
        "atr_adjusted": tpsl.atr_adjusted,
        "sl_clamped": tpsl.sl_clamped,
        "rr_actual": tpsl.rr_actual,
        "strength": conf.strength_label,
        "would_entry": True,
        "skip_reason": "",
    })

    # In live mode (SCALP_CAPITAL > 0) this is where _place_order() fires.
    # In observation mode the position is simulated by the existing
    # position-tracker loop in the agent.
    return obs


def _initial_observation(signal) -> Dict[str, Any]:
    """Fresh observation record with the signal-level fields populated."""
    return {
        "symbol": signal.symbol,
        "exchange": signal.exchange,
        "timestamp": signal.timestamp,
        "ofi_z": signal.ofi_z,
        "direction": signal.direction,
        "would_entry": False,
        "skip_reason": "",
        "observation_only": True,
        # v2 fields default to None so old queries still work
        "confluence_score": None,
        "strength_label": None,
        "cross_exchange_agrees": None,
        "btc_compatible": None,
        "adverse_selection_ok": None,
        "depth_ok": None,
        "vwap_aligned": None,
        "htf_aligned": None,
        "volume_adequate": None,
        "atr_bps": None,
        "atr_adjusted": None,
        "sl_clamped": None,
        "rr_actual": None,
    }


def _run_legacy_gates(agent, signal, obs) -> Optional[Dict[str, Any]]:
    """Run the existing 13 gates on the agent and return a populated skip
    observation if any fail; otherwise return None to continue.

    Real integration: replace this with the actual gate sequence already
    implemented in ScalpingAgent. This stub keeps the demo runnable without
    the full agent.
    """
    if not hasattr(agent, "run_legacy_gates"):
        return None  # treat as pass-through
    return agent.run_legacy_gates(signal, obs)


def _compute_position_size_usd(agent, signal) -> float:
    """Derive USD notional for the position from existing agent capital config."""
    capital = getattr(agent, "scalp_capital", 100.0)
    fund_capital = getattr(agent, "scalp_fund_capital", 100.0)
    # Use whichever is set (live capital takes precedence in live mode)
    base = capital if capital > 0 else fund_capital
    pct = getattr(agent.s, "SCALP_POSITION_PCT", 0.5) if hasattr(agent, "s") else 0.5
    return base * pct


# === Activation-readiness check for v2 ===

def is_ready_for_live_v2(stats: Dict[str, Any], settings) -> Dict[str, Any]:
    """Check whether the v2 activation criteria are met given current stats.

    Parameters
    ----------
    stats
        Output of get_scalp_summary() — must include keys: n_closed, win_rate,
        avg_net_bps, max_hold_pct, directional_accuracy_1m.
    settings
        Settings module/object with SCALP_*_FOR_LIVE_V2 constants.

    Returns
    -------
    dict
        {ready: bool, reasons_failing: [...], stats: ...}
    """
    checks = []
    n = stats.get("n_closed", 0)
    if n < getattr(settings, "SCALP_MIN_OBSERVATIONS_FOR_LIVE_V2", 300):
        checks.append(f"n_closed {n} < required 300")

    wr = stats.get("win_rate", 0.0)
    if wr < getattr(settings, "SCALP_MIN_WIN_RATE_FOR_LIVE_V2", 0.55):
        checks.append(f"win_rate {wr:.3f} < required 0.55")

    nbps = stats.get("avg_net_bps", -999.0)
    if nbps < getattr(settings, "SCALP_MIN_AVG_NET_BPS_FOR_LIVE_V2", 0.5):
        checks.append(f"avg_net_bps {nbps:.2f} < required 0.5")

    mh = stats.get("max_hold_pct", 1.0)
    if mh > getattr(settings, "SCALP_MAX_HOLD_EXIT_PCT_V2", 0.25):
        checks.append(f"max_hold_pct {mh:.3f} > limit 0.25")

    da = stats.get("directional_accuracy_1m", 0.0)
    if da < getattr(settings, "SCALP_MIN_DIRECTIONAL_ACC_1M_V2", 0.57):
        checks.append(f"directional_acc_1m {da:.3f} < required 0.57")

    return {
        "ready": not checks,
        "reasons_failing": checks,
        "stats": stats,
    }
