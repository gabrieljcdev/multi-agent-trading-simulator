"""
agents/opportunities/detectors/createmarket_detector.py

CreateMarketDetector — watches new ISOLATED LENDING MARKETS (the
cleanest signal per PROTOCOL_OPPORTUNITIES §2; build-first per the
research doc's liquidation-agent recommendation).

This pass reads Morpho Blue market activity via Morpho's public GraphQL
API (settings.OPPORTUNITY_MORPHO_API_URL — free, no key, same aiohttp
transport every data source uses; no parallel HTTP stack). The API has
no creation-time ordering on `markets`, so detection rides the
`marketTransactions` activity feed: any Supply / SupplyCollateral /
Borrow inside the lookback window joins its market's creationTimestamp,
and a market created inside the window is a candidate. That is the
right detection signal anyway — a market's first deposit is the moment
it becomes liquidatable at all. (Query shape verified live against the
API on 2026-06-04; graceful degradation covers future schema drift.)

Euler v2 / Silo deployment watching plugs in later behind the same
emit shape:
  # TODO: Euler v2 deployments (CreateMarket event logs via web3 layer)
  # TODO: Silo deployments

Emits one OpportunityCandidate per market created inside
OPPORTUNITY_CREATEMARKET_LOOKBACK_H, stamped first_seen=utcnow() the
instant it appears here. detection_latency_ms = first_seen − on-chain
creation timestamp — the metric the detection layer is graded on (§1.3).

Graceful degradation (PLUGIN_PATTERN Rule 4): every network/parse
failure logs and returns [] — never raises to the agent.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from config import settings
from agents.opportunities.detectors.base import BaseDetector, OpportunityCandidate

logger = logging.getLogger(__name__)

# Morpho Blue protocol constants (pinned by the deployed immutable
# contract, NOT tunables — Liquidation Incentive Factor formula:
# LIF = min(M, 1 / (beta * LLTV + (1 - beta))), beta=0.3, M=1.15).
_MORPHO_LIF_BETA = 0.3
_MORPHO_LIF_CAP  = 1.15

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

_ACTIVITY_QUERY = """
query NewMarketActivity($since: Int!, $first: Int!) {
  marketTransactions(
    first: $first, orderBy: Timestamp, orderDirection: Desc,
    where: { timestamp_gte: $since,
             type_in: [SupplyCollateral, Supply, Borrow] }
  ) {
    items {
      timestamp
      type
      market {
        marketId
        creationTimestamp
        lltv
        chain { id network }
        collateralAsset { symbol }
        loanAsset { symbol }
        oracle { address type }
        state { supplyAssetsUsd borrowAssetsUsd }
        badDebt { usd }
        realizedBadDebt { usd }
      }
    }
  }
}
"""


class CreateMarketDetector(BaseDetector):
    detector_id      = "createmarket"
    display_name     = "New Lending Markets"
    opp_type         = "liquidation"
    refresh_interval = settings.OPPORTUNITY_CREATEMARKET_REFRESH_SEC
    optional         = True

    def is_available(self) -> bool:
        # Public API, no key — available whenever a URL is configured.
        return bool(getattr(settings, "OPPORTUNITY_MORPHO_API_URL", ""))

    # ── Detection ────────────────────────────────────────────────────────

    async def scan(self) -> list[OpportunityCandidate]:
        """Emit a candidate per market younger than the lookback window
        (deduped per scan). Re-emitting a market on a later scan is by
        design — downstream UPSERTs mutable fields only; first_seen and
        the hypothesis-log snapshot are written once and never rewritten.
        """
        try:
            items = await self._fetch_activity()
        except Exception as e:
            logger.debug("[createmarket] fetch failed: %s", e)
            return []

        now = datetime.utcnow()
        lookback_sec = float(settings.OPPORTUNITY_CREATEMARKET_LOOKBACK_H) * 3600.0
        out: list[OpportunityCandidate] = []
        seen: set[str] = set()
        for item in items:
            try:
                cand = self._to_candidate(
                    (item or {}).get("market") or {}, now, lookback_sec)
            except Exception as e:
                logger.debug("[createmarket] parse failed: %s", e)
                continue
            if cand is None or cand.market_key in seen:
                continue
            seen.add(cand.market_key)
            out.append(cand)
        return out

    async def _fetch_activity(self) -> list[dict]:
        now_ts = int(datetime.utcnow().replace(tzinfo=timezone.utc).timestamp())
        since = now_ts - int(
            float(settings.OPPORTUNITY_CREATEMARKET_LOOKBACK_H) * 3600.0)
        timeout = aiohttp.ClientTimeout(
            total=settings.DATA_SOURCES_HTTP_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    settings.OPPORTUNITY_MORPHO_API_URL,
                    json={"query": _ACTIVITY_QUERY,
                          "variables": {
                              "since": since,
                              "first": int(settings.OPPORTUNITY_CREATEMARKET_PAGE_SIZE),
                          }},
                ) as r:
                    payload = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.debug("[createmarket] graphql transport: %s", e)
            return []
        if not isinstance(payload, dict) or payload.get("errors"):
            logger.debug("[createmarket] graphql errors: %s",
                         (payload or {}).get("errors"))
            return []
        return (((payload.get("data") or {}).get("marketTransactions") or {})
                .get("items") or [])

    # ── Candidate construction ──────────────────────────────────────────

    def _to_candidate(self, m: dict, now: datetime,
                      lookback_sec: float) -> OpportunityCandidate | None:
        created_ts = self._float(m.get("creationTimestamp"))
        if created_ts is None:
            return None
        age_sec = (now.replace(tzinfo=timezone.utc).timestamp() - created_ts)
        if age_sec < 0 or age_sec > lookback_sec:
            return None    # not a NEW market — just an active old one
        market_key = str(m.get("marketId") or "")
        if not market_key:
            return None
        collateral = m.get("collateralAsset") or {}
        if not collateral:
            # Idle market (no collateral asset) — nothing can ever be
            # liquidated in it; not an opportunity for this opp_type.
            return None

        chain = self._chain_name(m.get("chain") or {})
        loan = m.get("loanAsset") or {}
        lltv = self._parse_lltv(m.get("lltv"))
        lif_pct = self._lif_pct(lltv)
        oracle_type = self._oracle_type(m.get("oracle"))
        state = m.get("state") or {}
        supply_usd = self._float(state.get("supplyAssetsUsd")) or 0.0
        borrow_usd = self._float(state.get("borrowAssetsUsd")) or 0.0
        bad_debt_usd = self._float((m.get("badDebt") or {}).get("usd"))
        realized_bad_debt_usd = self._float(
            (m.get("realizedBadDebt") or {}).get("usd"))
        bad_debt_history = None
        if bad_debt_usd or realized_bad_debt_usd:
            bad_debt_history = {"bad_debt_usd": bad_debt_usd,
                                "realized_bad_debt_usd": realized_bad_debt_usd}

        raw_detail = {
            "collateral_asset": collateral.get("symbol"),
            "debt_asset":       loan.get("symbol"),
            "lif_pct":          lif_pct,
            "lltv":             lltv,
            "oracle_type":      oracle_type,
            "bad_debt_history": bad_debt_history,
            "dune_market_id":   None,
            # Gate inputs — Morpho Blue is an immutable, audited,
            # isolated-market design; bad debt is per-market, never
            # socialized. Exit-route liquidity is unknown at creation
            # (None = unknown, NOT a red flag — newness is the bet).
            "audited":                True,
            "mutable":                False,
            "anon_team":              False,
            "isolated":               True,
            "socialized_bad_debt":    False,
            "permissioned_collateral": None,
            "has_exit_route":         None,
        }
        # Decision-time snapshot for the hypothesis log — ONLY data
        # available at/before first_seen (no look-ahead, ever).
        feature_vector = {
            "protocol":          "morpho",
            "chain":             chain,
            "lltv":              lltv,
            "lif_pct":           lif_pct,
            "oracle_type":       oracle_type,
            "supply_usd_at_detection": supply_usd,
            "borrow_usd_at_detection": borrow_usd,
            "market_age_sec_at_detection": age_sec,
            "collateral_asset":  collateral.get("symbol"),
            "debt_asset":        loan.get("symbol"),
        }
        return OpportunityCandidate(
            opp_type=self.opp_type,
            protocol="morpho",
            chain=chain,
            market_key=market_key,
            detector_id=self.detector_id,
            first_seen=now,
            asset_class="defi_lending",
            raw_detail=raw_detail,
            feature_vector=feature_vector,
            detection_latency_ms=age_sec * 1000.0,
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _chain_name(chain: dict) -> str:
        network = str(chain.get("network") or "").strip()
        if network:
            return network.lower().replace(" ", "_")
        cid = chain.get("id")
        return f"chain_{cid}" if cid is not None else "unknown"

    @staticmethod
    def _oracle_type(oracle: dict | None) -> str:
        """Normalize the API's oracle record into the gate's vocabulary.
        Zero-address oracle → 'hardcoded' (the frozen-oracle tell the
        gate fires on); chainlink/pyth families pass through as the
        green-flag values; everything else (incl. an un-indexed oracle)
        stays 'unknown' — unknown is never fatal."""
        if not oracle:
            return "unknown"
        addr = str(oracle.get("address") or "")
        if addr and addr.lower() == _ZERO_ADDRESS:
            return "hardcoded"
        typ = str(oracle.get("type") or "").lower()
        if "chainlink" in typ:
            return "chainlink"
        if "pyth" in typ:
            return "pyth"
        return "unknown"

    @staticmethod
    def _float(v) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_lltv(v) -> float | None:
        """Morpho returns LLTV 1e18-scaled (e.g. 860000000000000000 =
        0.86). Tolerate an already-fractional value defensively."""
        f = CreateMarketDetector._float(v)
        if f is None:
            return None
        return f / 1e18 if f > 1.5 else f

    @staticmethod
    def _lif_pct(lltv: float | None) -> float | None:
        """Per-event liquidation incentive %, from the Morpho Blue LIF
        formula. This is a PER-EVENT return — the edge normalizer
        refuses to annualize it until event frequency is observed."""
        if lltv is None or lltv <= 0:
            return None
        lif = min(_MORPHO_LIF_CAP,
                  1.0 / (_MORPHO_LIF_BETA * lltv + (1.0 - _MORPHO_LIF_BETA)))
        return (lif - 1.0) * 100.0
