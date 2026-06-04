"""
agents/opportunities/fatal_risk_gate.py

Stage 2 — the fatal-risk gate. Screens FATAL, un-survivable flaws ONLY
(PROTOCOL_OPPORTUNITIES §2 Stage 2). It does NOT screen "risk" in
general: risk and competition are inversely coupled (§1.1) — the quiet
markets with edge are the new/small ones that also carry contract /
oracle risk, so general risk screening would gate out the entire
opportunity set. Sane oracle is the gate; newness is the bet.

Record-vs-enforce: the gate's verdict is a VIEW filter, not a write
filter — every screened candidate is persisted with its risk_status +
risk_flags (red AND green), and disqualified rows simply never appear
in the default ranked view. The gate never ranks.

Unknown inputs (None) are never fatal — a detector that can't yet read
a field must not disqualify a market for being new. The gate self-audit
(FEATURE BLOCK 6c) later labels whether each fatal call was right; that
audit LOGS ONLY and never changes a verdict here.
"""

from __future__ import annotations

import logging

from agents.opportunities.detectors.base import OpportunityCandidate

logger = logging.getLogger(__name__)

SURVIVABLE   = "survivable"
DISQUALIFIED = "disqualified"

# Oracle designs that ARE the fatal flaw (the Stream xUSD failure:
# oracle stuck at $1.26 while market traded $0.23 — $650k bad debt).
_FATAL_ORACLE_TYPES = {"hardcoded", "frozen", "fixed_1to1"}
_SANE_ORACLE_TYPES  = {"chainlink", "pyth"}

# LLTV band producing a healthy 5-15% LIF — a green flag, never a gate.
_GREEN_LIF_MIN_PCT = 5.0
_GREEN_LIF_MAX_PCT = 15.0


class FatalRiskGate:
    """screen() returns (risk_status, risk_flags) — flags recorded
    either way so months of data can say which red flags actually
    predicted blowups vs which were false alarms."""

    def screen(self, candidate: OpportunityCandidate) -> tuple[str, list[dict]]:
        d = candidate.raw_detail or {}
        flags: list[dict] = []

        # ── Red flags → disqualified (fatal only) ───────────────────────
        oracle_type = (d.get("oracle_type") or "").lower()
        if oracle_type in _FATAL_ORACLE_TYPES:
            flags.append({
                "flag": "hardcoded_oracle", "severity": "fatal",
                "detail": f"oracle_type={oracle_type} — cannot reprice; "
                          "the Stream xUSD failure mode",
            })
        if d.get("has_exit_route") is False:    # None = unknown, NOT fatal
            flags.append({
                "flag": "no_exit_route", "severity": "fatal",
                "detail": "illiquid collateral with no DEX exit route",
            })
        if d.get("socialized_bad_debt") is True:
            flags.append({
                "flag": "socialized_bad_debt", "severity": "fatal",
                "detail": "socialized-bad-debt vault design",
            })
        if d.get("permissioned_collateral") is True:
            flags.append({
                "flag": "unliquidatable_collateral", "severity": "fatal",
                "detail": "permissioned / RWA collateral that can't be liquidated",
            })
        # The trifecta is fatal only when ALL THREE hold together.
        if (d.get("audited") is False and d.get("mutable") is True
                and d.get("anon_team") is True):
            flags.append({
                "flag": "unaudited_mutable_anon", "severity": "fatal",
                "detail": "unaudited AND mutable AND anon-team",
            })

        # ── Green flags → recorded, sized small later ───────────────────
        if oracle_type in _SANE_ORACLE_TYPES:
            flags.append({
                "flag": "sane_oracle", "severity": "green",
                "detail": f"{oracle_type} oracle with a sane heartbeat",
            })
        if d.get("has_exit_route") is True:
            flags.append({
                "flag": "liquid_exit_route", "severity": "green",
                "detail": "liquid collateral with a real swap route",
            })
        if d.get("isolated") is True:
            flags.append({
                "flag": "isolated_market", "severity": "green",
                "detail": "isolated (non-socialized) market",
            })
        lif = d.get("lif_pct")
        if lif is not None and _GREEN_LIF_MIN_PCT <= lif <= _GREEN_LIF_MAX_PCT:
            flags.append({
                "flag": "healthy_lif", "severity": "green",
                "detail": f"LLTV produces a {lif:.1f}% LIF (5-15% band)",
            })
        if d.get("audited") is True or d.get("mutable") is False:
            flags.append({
                "flag": "audited_or_immutable", "severity": "green",
                "detail": "audited or immutable contract",
            })

        status = DISQUALIFIED if any(
            f["severity"] == "fatal" for f in flags) else SURVIVABLE
        return status, flags


# Module-level singleton — cross-cutting state is shared by import,
# not re-instantiation (project convention).
fatal_risk_gate = FatalRiskGate()

__all__ = ["FatalRiskGate", "fatal_risk_gate", "SURVIVABLE", "DISQUALIFIED"]
