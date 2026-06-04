"""
tests/test_opportunity_scanner.py — OpportunityScannerAgent full suite.

Covers: plugin compliance, observation-mode safety, first_seen
integrity, the fatal-risk gate (record-vs-enforce), competition +
trajectory, edge normalization (no fabricated APRs), trajectory
ranking, the hypothesis log (no look-ahead; labels by separate pass),
the exploration lane (6a/6b) + gate self-audit (6c), and graceful
degradation of the dashboard reads.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from database import queries as q


# ─────────────────────────────────────────────────────────────────────────
# Isolated SQLite for every test (never touches data/cryptobot.db)
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def _temp_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import db as db_module
    from database.models import Base

    path = tmp_path / "opportunity_test.db"
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False}, echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False,
                               autocommit=False, expire_on_commit=False)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    return path


def _core_row(market_key="0xabc", *, opp_type="liquidation",
              protocol="morpho", chain="base",
              first_seen=None, risk_status="survivable",
              edge=None, trend=None, reach="unknown", **extra):
    row = {
        "opp_type": opp_type, "protocol": protocol, "chain": chain,
        "market_key": market_key, "asset_class": "defi_lending",
        "detector_id": "createmarket",
        "first_seen": first_seen or datetime.utcnow(),
        "risk_status": risk_status, "risk_flags": [],
        "edge_annualized_pct": edge, "edge_confidence": "low",
        "reachability_verdict": reach,
        "as_of": datetime.utcnow(),
    }
    if trend is not None:
        row["competitor_trend"] = trend
    row.update(extra)
    return row


def _candidate(market_key="0xabc", *, raw_detail=None, feature_vector=None,
               latency_ms=1500.0):
    from agents.opportunities.detectors.base import OpportunityCandidate
    return OpportunityCandidate(
        opp_type="liquidation", protocol="morpho", chain="base",
        market_key=market_key, detector_id="createmarket",
        first_seen=datetime.utcnow(), asset_class="defi_lending",
        raw_detail=raw_detail if raw_detail is not None else {
            "collateral_asset": "WETH", "debt_asset": "USDC",
            "lif_pct": 8.0, "lltv": 0.86, "oracle_type": "unknown",
            "audited": True, "mutable": False, "anon_team": False,
            "isolated": True, "socialized_bad_debt": False,
        },
        feature_vector=feature_vector if feature_vector is not None else {
            "lltv": 0.86, "lif_pct": 8.0,
            "supply_usd_at_detection": 50.0,
            "market_age_sec_at_detection": 120.0,
        },
        detection_latency_ms=latency_ms,
    )


# ─────────────────────────────────────────────────────────────────────────
# 1. Plugin compliance
# ─────────────────────────────────────────────────────────────────────────

class _FakeDetector:
    detector_id = "fake"
    display_name = "Fake"
    opp_type = "liquidation"
    refresh_interval = 1
    optional = True
    scanned = 0

    def is_available(self):
        return True

    async def scan(self):
        self.scanned += 1
        return [_candidate("0xfake")]


class _RaisingDetector(_FakeDetector):
    detector_id = "raising"

    async def scan(self):
        raise RuntimeError("boom")


class _UnavailableDetector(_FakeDetector):
    detector_id = "unavailable"

    def is_available(self):
        return False


def test_agent_never_imports_concrete_detectors():
    """Plugin Pattern Rule 1 — the agent imports only BaseDetector + the
    registration list, never a concrete detector by name."""
    import inspect
    import agents.opportunity_scanner_agent as mod
    src = inspect.getsource(mod)
    assert "CreateMarketDetector" not in src
    assert "REGISTERED_DETECTORS" in src


@pytest.mark.asyncio
async def test_injected_detector_picked_up_and_raising_detector_isolated(_temp_db):
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    fake, raising, unavailable = (_FakeDetector(), _RaisingDetector(),
                                  _UnavailableDetector())
    agent = OpportunityScannerAgent(detectors=[raising, fake, unavailable])
    await agent.start()
    try:
        await asyncio.sleep(1.5)   # > refresh_interval=1s
    finally:
        await agent.stop()
    # The fake detector ran and persisted despite its raising sibling…
    assert fake.scanned >= 1
    rows = q.get_ranked_opportunities(mode="standard")
    assert any(r["market_key"] == "0xfake" for r in rows)
    # …and the unavailable one was skipped cleanly (no loop spawned).
    assert unavailable.scanned == 0


def test_createmarket_parses_live_api_shape():
    """Pins _to_candidate against the exact market shape captured live
    from the Morpho GraphQL API (2026-06-04)."""
    from agents.opportunities.detectors.createmarket_detector import (
        CreateMarketDetector,
    )
    from datetime import timezone
    det = CreateMarketDetector()
    now = datetime.utcnow()
    # On-chain timestamps are UTC epochs — naive .timestamp() would
    # re-interpret utcnow() as local time and skew the latency check.
    now_epoch = now.replace(tzinfo=timezone.utc).timestamp()
    m = {
        "marketId": "0x655106914fc7605be5142315b123c51e6001086571bc6589c4687c3739403e4d",
        "creationTimestamp": now_epoch - 3600,              # 1h old
        "lltv": 860000000000000000,                          # 1e18-scaled 0.86
        "chain": {"id": 8453, "network": "Base"},
        "collateralAsset": {"symbol": "WETH"},
        "loanAsset": {"symbol": "USDC"},
        "oracle": {"address": "0x12ab", "type": "MorphoChainlinkOracleV2"},
        "state": {"supplyAssetsUsd": 13038622.97, "borrowAssetsUsd": 0},
        "badDebt": {"usd": 0},
        "realizedBadDebt": {"usd": 0},
    }
    cand = det._to_candidate(m, now, 24 * 3600.0)
    assert cand is not None
    assert cand.chain == "base"
    assert cand.market_key.startswith("0x655106")
    assert cand.raw_detail["lltv"] == pytest.approx(0.86)
    # LIF = min(1.15, 1/(0.3*0.86 + 0.7)) − 1 ≈ 4.38 %/event.
    assert cand.raw_detail["lif_pct"] == pytest.approx(4.384, abs=0.01)
    assert cand.raw_detail["oracle_type"] == "chainlink"
    assert cand.detection_latency_ms == pytest.approx(3600_000, rel=0.05)
    # No look-ahead fields in the snapshot — everything is as-of-now data.
    assert cand.feature_vector["supply_usd_at_detection"] > 0
    # Idle market (no collateral) → never a liquidation candidate.
    assert det._to_candidate({**m, "collateralAsset": None},
                             now, 24 * 3600.0) is None
    # Old market → outside the lookback, not "new".
    assert det._to_candidate(
        {**m, "creationTimestamp": now_epoch - 90 * 86400},
        now, 24 * 3600.0) is None
    # Zero-address oracle normalizes to the gate's fatal vocabulary.
    cand_bad = det._to_candidate(
        {**m, "oracle": {"address": "0x" + "0" * 40, "type": None}},
        now, 24 * 3600.0)
    assert cand_bad.raw_detail["oracle_type"] == "hardcoded"


def test_registered_detectors_is_the_single_source_of_truth():
    from agents.opportunities.detectors import REGISTERED_DETECTORS, BaseDetector
    assert REGISTERED_DETECTORS, "registry must not be empty"
    for d in REGISTERED_DETECTORS:
        assert isinstance(d, BaseDetector)
        assert d.detector_id and d.opp_type


# ─────────────────────────────────────────────────────────────────────────
# 2. Observation-mode safety
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_observation_mode_safety(_temp_db):
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    agent = OpportunityScannerAgent(detectors=[])
    assert agent.capital_allocation == 0.0
    assert agent.optional is True
    # close_all_positions is a no-op and safe to call cold.
    await agent.close_all_positions()
    stats = await agent.get_stats()
    assert stats.capital_allocated == 0.0
    assert stats.capital_deployed == 0.0
    assert stats.daily_pnl == 0.0 and stats.total_pnl == 0.0   # never fabricated


def test_zero_capital_agent_does_not_perturb_capital_sum(caplog):
    import logging
    from agents.coordinator import Coordinator
    from agents.opportunity_scanner_agent import OpportunityScannerAgent

    class _StubAgent(OpportunityScannerAgent):
        pass

    with caplog.at_level(logging.WARNING):
        Coordinator(agents=[_StubAgent()])
    assert not any("capital sum" in r.message.lower() for r in caplog.records)


def test_agent_registered_in_roster():
    import agents as agents_pkg
    ids = [a.agent_id for a in agents_pkg.REGISTERED_AGENTS]
    assert "opportunity_scanner" in ids
    a = next(x for x in agents_pkg.REGISTERED_AGENTS
             if x.agent_id == "opportunity_scanner")
    assert a.capital_allocation == 0.0


@pytest.mark.asyncio
async def test_kill_all_is_harmless(_temp_db):
    from agents.coordinator import Coordinator
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    coord = Coordinator(agents=[OpportunityScannerAgent(detectors=[])])
    result = await coord.kill_all(reason="test")
    assert result["agents_err"] == 0


def test_no_order_routing_anywhere_in_the_module():
    """$0 observation is absolute — no ccxt, no router, no order calls."""
    import inspect
    import agents.opportunity_scanner_agent as agent_mod
    import agents.opportunities.fatal_risk_gate as gate_mod
    import agents.opportunities.ranker as ranker_mod
    import agents.opportunities.exploration as expl_mod
    for mod in (agent_mod, gate_mod, ranker_mod, expl_mod):
        src = inspect.getsource(mod)
        assert "import ccxt" not in src
        assert "create_order" not in src
        assert "OrderRouter" not in src
        assert "withdraw(" not in src


# ─────────────────────────────────────────────────────────────────────────
# 3. first_seen integrity
# ─────────────────────────────────────────────────────────────────────────

def test_first_seen_immutable_under_reupsert(_temp_db):
    t0 = datetime.utcnow() - timedelta(hours=5)
    core_id = q.upsert_opportunity_core(_core_row(first_seen=t0, edge=None))
    # Re-detection with a later first_seen + new mutable fields.
    core_id2 = q.upsert_opportunity_core(_core_row(
        first_seen=datetime.utcnow(), edge=42.0, risk_status="survivable"))
    assert core_id2 == core_id            # UNIQUE key → same row
    row = q.get_opportunity_core_by_id(core_id)
    assert row["first_seen"] == t0.isoformat()       # untouched
    assert row["edge_annualized_pct"] == 42.0        # mutable updated


def test_unique_constraint_blocks_duplicate_core_rows(_temp_db):
    import sqlite3
    q.upsert_opportunity_core(_core_row())
    con = sqlite3.connect(str(_temp_db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO opportunity_core "
                "(opp_type, protocol, chain, market_key, first_seen) "
                "VALUES ('liquidation','morpho','base','0xabc','2026-01-01')"
            )
    finally:
        con.close()


def test_competition_recheck_never_touches_first_seen(_temp_db):
    t0 = datetime.utcnow() - timedelta(days=1)
    core_id = q.upsert_opportunity_core(_core_row(first_seen=t0))
    q.record_competition_check(core_id, 3, "rising_slow",
                               [{"ts": datetime.utcnow().isoformat(), "count": 3}],
                               "open")
    row = q.get_opportunity_core_by_id(core_id)
    assert row["first_seen"] == t0.isoformat()
    assert row["competitor_count"] == 3
    assert row["competitor_trend"] == "rising_slow"
    assert row["last_competition_check"] is not None
    assert row["competitor_count_history"][-1]["count"] == 3


# ─────────────────────────────────────────────────────────────────────────
# 4. Fatal-risk gate
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("detail_patch, expected_flag", [
    ({"oracle_type": "hardcoded"},            "hardcoded_oracle"),
    ({"oracle_type": "frozen"},               "hardcoded_oracle"),
    ({"oracle_type": "fixed_1to1"},           "hardcoded_oracle"),
    ({"has_exit_route": False},               "no_exit_route"),
    ({"socialized_bad_debt": True},           "socialized_bad_debt"),
    ({"permissioned_collateral": True},       "unliquidatable_collateral"),
    ({"audited": False, "mutable": True, "anon_team": True},
     "unaudited_mutable_anon"),
])
def test_each_red_flag_disqualifies(detail_patch, expected_flag):
    from agents.opportunities.fatal_risk_gate import fatal_risk_gate, DISQUALIFIED
    cand = _candidate()
    cand.raw_detail.update(detail_patch)
    status, flags = fatal_risk_gate.screen(cand)
    assert status == DISQUALIFIED
    assert any(f["flag"] == expected_flag and f["severity"] == "fatal"
               for f in flags)


def test_green_flags_survivable_and_recorded():
    from agents.opportunities.fatal_risk_gate import fatal_risk_gate, SURVIVABLE
    cand = _candidate()
    cand.raw_detail.update(oracle_type="chainlink", has_exit_route=True,
                           isolated=True, lif_pct=8.0, audited=True)
    status, flags = fatal_risk_gate.screen(cand)
    assert status == SURVIVABLE
    greens = {f["flag"] for f in flags if f["severity"] == "green"}
    assert {"sane_oracle", "liquid_exit_route", "isolated_market",
            "healthy_lif", "audited_or_immutable"} <= greens


def test_trifecta_requires_all_three():
    from agents.opportunities.fatal_risk_gate import fatal_risk_gate, SURVIVABLE
    for patch in ({"audited": False}, {"mutable": True}, {"anon_team": True},
                  {"audited": False, "mutable": True},
                  {"mutable": True, "anon_team": True}):
        cand = _candidate()
        cand.raw_detail.update(patch)
        status, _ = fatal_risk_gate.screen(cand)
        assert status == SURVIVABLE, f"partial trifecta {patch} must survive"


def test_newness_and_smallness_alone_never_disqualify():
    from agents.opportunities.fatal_risk_gate import fatal_risk_gate, SURVIVABLE
    cand = _candidate()
    # Brand new, tiny, every unknown left unknown — newness is the bet.
    cand.raw_detail.update(oracle_type="unknown", has_exit_route=None,
                           permissioned_collateral=None)
    cand.feature_vector.update(supply_usd_at_detection=12.0,
                               market_age_sec_at_detection=30.0)
    status, _ = fatal_risk_gate.screen(cand)
    assert status == SURVIVABLE


def test_record_vs_enforce_disqualified_written_but_gated(_temp_db):
    q.upsert_opportunity_core(_core_row("0xbad", risk_status="disqualified"))
    q.upsert_opportunity_core(_core_row("0xgood", risk_status="survivable"))
    default_view = q.get_ranked_opportunities(show_disqualified=False)
    assert {r["market_key"] for r in default_view} == {"0xgood"}
    full_view = q.get_ranked_opportunities(show_disqualified=True)
    assert {r["market_key"] for r in full_view} == {"0xbad", "0xgood"}


# ─────────────────────────────────────────────────────────────────────────
# 5. Competition & trajectory
# ─────────────────────────────────────────────────────────────────────────

def test_distinct_liquidator_count_from_sample_event_logs():
    from agents.opportunities.competition import count_distinct_liquidators
    events = [
        {"data": {"liquidator": "0xAA"}},
        {"data": {"liquidator": "0xaa"}},          # same sender, case-insensitive
        {"sender": "0xBB"},
        {"user": {"address": "0xCC"}},
        {"data": {}},                              # unattributable → ignored
    ]
    assert count_distinct_liquidators(events) == 3
    assert count_distinct_liquidators([]) == 0


def test_trend_transitions_follow_settings_thresholds(monkeypatch):
    from config import settings
    from agents.opportunities import competition as comp
    monkeypatch.setattr(settings, "OPPORTUNITY_SATURATED_COUNT", 5)
    monkeypatch.setattr(settings, "OPPORTUNITY_TREND_RISING_FAST_PER_DAY", 2.0)
    monkeypatch.setattr(settings, "OPPORTUNITY_TREND_RISING_SLOW_PER_DAY", 0.5)

    day_ago = (datetime.utcnow() - timedelta(days=1)).isoformat()
    assert comp.derive_trend(0, []) == comp.TREND_ZERO
    assert comp.derive_trend(5, []) == comp.TREND_SATURATED
    # 1 → 4 in a day = 3/day ≥ rising_fast threshold.
    assert comp.derive_trend(
        4, [{"ts": day_ago, "count": 1}]) == comp.TREND_RISING_FAST
    # 1 → 2 in a day = 1/day → rising_slow.
    assert comp.derive_trend(
        2, [{"ts": day_ago, "count": 1}]) == comp.TREND_RISING_SLOW


def test_window_status_derivation():
    from agents.opportunities import competition as comp
    assert comp.derive_window_status(comp.TREND_ZERO, 0) == comp.WINDOW_OPENING
    assert comp.derive_window_status(comp.TREND_RISING_SLOW, 2) == comp.WINDOW_OPEN
    assert comp.derive_window_status(comp.TREND_RISING_FAST, 4) == comp.WINDOW_CLOSING
    assert comp.derive_window_status(comp.TREND_SATURATED, 7) == comp.WINDOW_CLOSED


@pytest.mark.asyncio
async def test_measurer_uses_injected_fetcher_for_liquidation():
    from agents.opportunities.competition import CompetitionMeasurer

    async def fake_fetch(market_key, since_ts):
        return [{"data": {"liquidator": "0x1"}},
                {"data": {"liquidator": "0x2"}}]

    m = CompetitionMeasurer(liquidation_event_fetcher=fake_fetch)
    count, trend, append = await m.measure({
        "opp_type": "liquidation", "market_key": "0xabc",
        "competitor_count_history": [],
    })
    assert count == 2
    assert trend in ("rising_slow", "rising_fast", "zero", "saturated")
    assert append and append[0]["count"] == 2


# ─────────────────────────────────────────────────────────────────────────
# 6. Edge normalization
# ─────────────────────────────────────────────────────────────────────────

def test_continuous_funding_edge_annualizes_from_periodic_rate():
    from agents.opportunities.edge_normalizer import edge_normalizer
    edge, conf = edge_normalizer.annualize({
        "opp_type": "funding",
        "raw_detail": {"funding_rate_bps": 1.0, "payment_interval_h": 8.0},
    })
    # 1 bp × (8760/8 payments/yr) = 1095 bps = 10.95 %/yr.
    assert edge == pytest.approx(10.95)
    assert conf == "medium"


def test_one_shot_liquidation_refuses_to_fabricate_apr(monkeypatch):
    from config import settings
    from agents.opportunities.edge_normalizer import edge_normalizer
    monkeypatch.setattr(settings, "OPPORTUNITY_MIN_EVENTS_FOR_FREQUENCY", 5)
    # Below the event floor → (None, "low"), never an invented APR.
    edge, conf = edge_normalizer.annualize({
        "opp_type": "liquidation",
        "raw_detail": {"lif_pct": 8.0, "observed_liquidation_events": 2,
                       "observed_window_h": 72.0},
    })
    assert edge is None and conf == "low"
    # No frequency data at all → same refusal.
    edge, conf = edge_normalizer.annualize({
        "opp_type": "liquidation", "raw_detail": {"lif_pct": 8.0},
    })
    assert edge is None and conf == "low"
    # At/above the floor it annualizes from OBSERVED frequency.
    edge, conf = edge_normalizer.annualize({
        "opp_type": "liquidation",
        "raw_detail": {"lif_pct": 2.0, "observed_liquidation_events": 6,
                       "observed_window_h": 8760.0 / 10},   # 6 per 1/10 yr
    })
    assert edge == pytest.approx(2.0 * 60.0)
    assert conf == "medium"


def test_edge_never_rendered_without_trend():
    from agents.opportunities.ranker import render_row
    # Trend missing → edge withheld, marker explains.
    row = render_row({"edge_annualized_pct": 99.0, "competitor_trend": None})
    assert row["edge_annualized_pct"] is None
    assert "withheld" in row["edge_display"]
    # Trend present but edge unannualized → explicit frequency-unknown marker.
    row = render_row({"edge_annualized_pct": None, "competitor_trend": "zero",
                      "detail": {"lif_pct": 8.0}})
    assert "frequency unknown" in row["edge_display"]
    # Both present → numeric display, trend untouched beside it.
    row = render_row({"edge_annualized_pct": 12.0, "competitor_trend": "zero"})
    assert row["edge_display"] == "12.0%/yr"
    assert row["competitor_trend"] == "zero"


# ─────────────────────────────────────────────────────────────────────────
# 7. Ranking
# ─────────────────────────────────────────────────────────────────────────

def test_zero_competitor_survivor_outranks_fatter_edge_with_rising_competition():
    from agents.opportunities.ranker import trajectory_ranker
    rows = trajectory_ranker.rank([
        {"market_key": "fat",   "competitor_trend": "rising_fast",
         "edge_annualized_pct": 500.0, "reachability_verdict": "reachable"},
        {"market_key": "early", "competitor_trend": "zero",
         "edge_annualized_pct": 5.0,   "reachability_verdict": "reachable"},
    ])
    assert [r["market_key"] for r in rows] == ["early", "fat"]


def test_reachability_breaks_ties():
    from agents.opportunities.ranker import trajectory_ranker
    rows = trajectory_ranker.rank([
        {"market_key": "far",  "competitor_trend": "zero",
         "edge_annualized_pct": 10.0, "reachability_verdict": "unreachable"},
        {"market_key": "near", "competitor_trend": "zero",
         "edge_annualized_pct": 10.0, "reachability_verdict": "reachable"},
    ])
    assert [r["market_key"] for r in rows] == ["near", "far"]


def test_disqualified_rows_never_enter_default_ranking(_temp_db):
    q.upsert_opportunity_core(_core_row(
        "0xbad", risk_status="disqualified", trend="zero", edge=999.0))
    q.upsert_opportunity_core(_core_row(
        "0xok", risk_status="survivable", trend="rising_fast", edge=1.0))
    rows = q.get_ranked_opportunities(mode="standard")
    assert [r["market_key"] for r in rows] == ["0xok"]


def test_queries_ordering_matches_ranker(_temp_db):
    """The local sort in queries.get_ranked_opportunities must stay in
    sync with TrajectoryRanker (the circular-import duplication)."""
    from agents.opportunities.ranker import trajectory_ranker
    for i, (mk, trend, edge) in enumerate([
        ("0x1", "rising_fast", 500.0),
        ("0x2", "zero",        5.0),
        ("0x3", "zero",        50.0),
        ("0x4", "rising_slow", 80.0),
    ]):
        q.upsert_opportunity_core(_core_row(mk, trend=trend, edge=edge))
    db_order = [r["market_key"] for r in q.get_ranked_opportunities()]
    ranker_order = [r["market_key"] for r in trajectory_ranker.rank(
        q.get_ranked_opportunities())]
    assert db_order == ranker_order == ["0x3", "0x2", "0x4", "0x1"]


# ─────────────────────────────────────────────────────────────────────────
# 8. Hypothesis log
# ─────────────────────────────────────────────────────────────────────────

def test_feature_vector_written_once_no_lookahead_columns(_temp_db):
    core_id = q.upsert_opportunity_core(_core_row())
    fv = {"lltv": 0.86, "supply_usd_at_detection": 50.0}
    q.save_opportunity_observation({
        "core_id": core_id, "detector_id": "createmarket",
        "opp_type": "liquidation", "first_seen": datetime.utcnow(),
        "feature_vector_json": fv, "detection_latency_ms": 900.0,
    })
    # A second write (re-detection) must NOT rewrite the snapshot.
    q.save_opportunity_observation({
        "core_id": core_id, "detector_id": "createmarket",
        "opp_type": "liquidation", "first_seen": datetime.utcnow(),
        "feature_vector_json": {"lltv": 0.99, "LOOKAHEAD": True},
    })
    from database.db import get_session
    from database.models import OpportunityObservation
    with get_session() as s:
        rows = s.query(OpportunityObservation).filter_by(core_id=core_id).all()
    assert len(rows) == 1
    obs = rows[0]
    assert obs.feature_vector_json == fv          # first write wins, forever
    # Label columns null at insert — filled only by the backfill pass.
    assert obs.label_filled_at is None
    assert obs.realized_competitor_count_json is None
    assert obs.realized_edge_decay_json is None
    assert obs.window_status_at_horizon_json is None
    # Trial-count fields persist for later DSR/CPCV clustering.
    assert obs.detector_id == "createmarket"
    assert obs.opp_type == "liquidation"


def test_labels_filled_only_by_backfill_pass(_temp_db):
    core_id = q.upsert_opportunity_core(_core_row())
    q.save_opportunity_observation({
        "core_id": core_id, "detector_id": "createmarket",
        "opp_type": "liquidation", "first_seen": datetime.utcnow(),
        "feature_vector_json": {},
    })
    q.update_opportunity_labels(core_id, 24, {
        "realized_competitor_count": 2,
        "realized_edge_decay": 11.5,
        "window_status_at_horizon": "open",
    })
    q.update_opportunity_labels(core_id, 168, {
        "realized_competitor_count": 6,
        "window_status_at_horizon": "closed",
    })
    from database.db import get_session
    from database.models import OpportunityObservation
    with get_session() as s:
        obs = s.query(OpportunityObservation).filter_by(core_id=core_id).one()
    assert obs.realized_competitor_count_json == {"24": 2, "168": 6}
    assert obs.realized_edge_decay_json == {"24": 11.5}
    assert obs.window_status_at_horizon_json == {"24": "open", "168": "closed"}
    assert obs.label_filled_at is not None


@pytest.mark.asyncio
async def test_misses_and_disqualified_are_logged_too(_temp_db):
    """Non-events are data — omitting the misses is the survivorship
    bias that makes a spurious edge look real."""
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    agent = OpportunityScannerAgent(detectors=[])
    bad = _candidate("0xfatal")
    bad.raw_detail["oracle_type"] = "hardcoded"
    await agent._process_candidate(bad)
    await agent._process_candidate(_candidate("0xfine"))
    from database.db import get_session
    from database.models import OpportunityObservation
    with get_session() as s:
        n = s.query(OpportunityObservation).count()
    assert n == 2   # disqualified hypothesis logged alongside the survivor


def test_observations_needing_labels_filters_by_horizon(_temp_db):
    old_id = q.upsert_opportunity_core(_core_row(
        "0xold", first_seen=datetime.utcnow() - timedelta(hours=30)))
    new_id = q.upsert_opportunity_core(_core_row(
        "0xnew", first_seen=datetime.utcnow() - timedelta(hours=1)))
    for cid, mk in ((old_id, "0xold"), (new_id, "0xnew")):
        q.save_opportunity_observation({
            "core_id": cid, "detector_id": "createmarket",
            "opp_type": "liquidation",
            "first_seen": (datetime.utcnow() - timedelta(
                hours=30 if mk == "0xold" else 1)),
            "feature_vector_json": {},
        })
    due = q.get_opportunity_observations_needing_labels(24)
    assert [d["core_id"] for d in due] == [old_id]
    # Once labelled for that horizon it drops off the work list.
    q.update_opportunity_labels(old_id, 24, {"window_status_at_horizon": "open"})
    assert q.get_opportunity_observations_needing_labels(24) == []


# ─────────────────────────────────────────────────────────────────────────
# 9. Exploration lane (FEATURE BLOCK 6) + gate self-audit (6c)
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unconventional_survivor_gets_both_forms(_temp_db):
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    agent = OpportunityScannerAgent(detectors=[])
    # Tiny + unknown-oracle survivor → multiple exploration factors.
    cand = _candidate("0xugly")
    await agent._process_candidate(cand)
    rows = q.get_ranked_opportunities(show_disqualified=True)
    row = next(r for r in rows if r["market_key"] == "0xugly")
    assert row["unconventional_score"] is not None
    assert row["unconventional_factors"]                    # structured form
    assert isinstance(row["unconventional_rationale"], str) # free-text form
    assert row["unconventional_rationale"]
    # Mirrored on the immutable observation row, label-bearing like any other.
    from database.db import get_session
    from database.models import OpportunityObservation
    with get_session() as s:
        obs = s.query(OpportunityObservation).one()
    assert obs.unconventional_factors_json == row["unconventional_factors"]
    assert obs.unconventional_rationale == row["unconventional_rationale"]
    assert obs.label_filled_at is None      # forward labels still pending


def test_exploratory_view_same_survivors_different_order_never_merges(_temp_db):
    q.upsert_opportunity_core(_core_row(
        "0xa", trend="zero", edge=100.0,
        unconventional_score=0.4, unconventional_factors=["factor_stack"]))
    q.upsert_opportunity_core(_core_row(
        "0xb", trend="rising_fast", edge=1.0,
        unconventional_score=0.9,
        unconventional_factors=["contrarian_reachability", "factor_stack"]))
    std = q.get_ranked_opportunities(mode="standard")
    exp = q.get_ranked_opportunities(mode="exploratory")
    # Same survivor set…
    assert {r["market_key"] for r in std} == {r["market_key"] for r in exp}
    # …different order: standard leads with trajectory, exploratory with score.
    assert [r["market_key"] for r in std] == ["0xa", "0xb"]
    assert [r["market_key"] for r in exp] == ["0xb", "0xa"]


def test_below_min_score_excluded_from_exploratory_only(_temp_db, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "OPPORTUNITY_UNCONVENTIONAL_MIN_SCORE", 0.3)
    q.upsert_opportunity_core(_core_row(
        "0xquiet", trend="zero", unconventional_score=0.1))
    assert q.get_ranked_opportunities(mode="exploratory") == []
    # Still fully present in the standard view — the lane is fenced off.
    assert [r["market_key"] for r in q.get_ranked_opportunities()] == ["0xquiet"]


def test_exploration_never_changes_standard_order(_temp_db):
    q.upsert_opportunity_core(_core_row(
        "0xa", trend="zero", edge=10.0, unconventional_score=0.0))
    q.upsert_opportunity_core(_core_row(
        "0xb", trend="rising_slow", edge=90.0, unconventional_score=1.0))
    std = [r["market_key"] for r in q.get_ranked_opportunities()]
    assert std == ["0xa", "0xb"]   # trajectory wins; score is invisible here


@pytest.mark.asyncio
async def test_gate_self_audit_writes_counterfactual_but_verdict_unchanged(_temp_db):
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    agent = OpportunityScannerAgent(detectors=[])
    bad = _candidate("0xfatal")
    bad.raw_detail["oracle_type"] = "hardcoded"
    await agent._process_candidate(bad)

    core = q.get_ranked_opportunities(show_disqualified=True)[0]
    # Backfill the 24h horizon (simulating elapsed time via direct call).
    await agent._backfill_one({"core_id": core["id"]}, 24)

    from database.db import get_session
    from database.models import OpportunityObservation
    with get_session() as s:
        obs = s.query(OpportunityObservation).one()
    cf = obs.counterfactual_outcome_json
    assert cf and "24" in cf
    assert "hardcoded_oracle" in cf["24"]          # keyed by the firing flag
    # Favourable-or-not, the row is STILL absent from both ranked views
    # and the gate verdict is unchanged — the audit only logs.
    assert q.get_ranked_opportunities(mode="standard") == []
    assert q.get_ranked_opportunities(mode="exploratory") == []
    row = q.get_opportunity_core_by_id(core["id"])
    assert row["risk_status"] == "disqualified"


# ─────────────────────────────────────────────────────────────────────────
# 10. Graceful degradation
# ─────────────────────────────────────────────────────────────────────────

def test_detail_read_returns_safe_defaults_when_missing(_temp_db):
    core_id = q.upsert_opportunity_core(_core_row())
    assert q.get_opportunity_detail("liquidation", core_id) == {}
    assert q.get_opportunity_detail("nonsense_type", core_id) == {}
    q.save_opportunity_detail("liquidation", core_id,
                              {"collateral_asset": "WETH", "lif_pct": 8.0,
                               "not_a_column": "dropped"})
    d = q.get_opportunity_detail("liquidation", core_id)
    assert d["collateral_asset"] == "WETH"
    assert "not_a_column" not in d


def test_observation_summary_zeroed_on_empty_db(_temp_db):
    s = q.get_opportunity_summary()
    assert s["n_core"] == 0 and s["n_observations"] == 0
    assert s["mean_detection_latency_ms"] == 0.0


@pytest.mark.asyncio
async def test_agent_summary_survives_db_failure(monkeypatch):
    from agents.opportunity_scanner_agent import OpportunityScannerAgent
    from database import queries as dbq

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(dbq, "get_opportunity_summary", _boom)
    agent = OpportunityScannerAgent(detectors=[])
    out = agent.get_observation_summary()
    assert out["n_core"] == 0                    # zeroed, not raised
    assert "detection_latency_sla_ms" in out
