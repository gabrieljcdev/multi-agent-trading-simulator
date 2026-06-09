"""
follow/meme_scorer.py

The Meme-Coin Cluster-Pattern Rug-Rate Scorer — a capital-FREE OBSERVER source
(sibling of WalletFlowWatcher / CopyTradeObserver). It watches new Solana
launches, clusters early-buyer wallets by COMMON FUNDING SOURCE (one hop,
funder-only, terminal stop-list), tracks each funder's POINT-IN-TIME rug rate,
and emits AVOID when a launch contains a funder-cluster with a strong,
well-evidenced rug history. Otherwise NO-SIGNAL.

DECISION SPACE is BINARY + ABSTENTION-HEAVY: AVOID | NO-SIGNAL. The only failure
mode is silence (a missed rug you then avoid anyway) — never false reassurance,
never a false follow. NO-SIGNAL means "no LAZY manipulation detected", NOT
"safe" (NO_SIGNAL_LABEL says exactly that, in code and UI). It never says
BUY/SAFE, never follows, has NO submit/execute path.

OBSERVER, hard requirement: there is no submit / execute / place_order / trade /
route_order method anywhere on this class.

CRITICAL — the point-in-time spine (binary correctness; one leak = every number
lies). TWO CLOCKS per launch, never collapsed: detected_at (knowable at sample
time) and resolved_at (outcome FINAL; NULL until resolved). AS-OF RULE: to score
launch L at its detected_at = T, a funder-cluster's rug-rate uses ONLY that
cluster's prior launches with resolved_at STRICTLY < T. There is exactly ONE
shared as-of implementation — rug_rate_as_of — and the live scorer, the replay
harness, AND the web as-of inspector all route through it. No second "what did
we know at T" anywhere (asserted by test). This mirrors how
skill_scorer.resolved_actions_as_of is the single source for the wallet watcher.

SCOPE (locked — deliberate misses, NOT gaps): LOW-HANGING FRUIT ONLY — single-hop
"star" funding bundles, funder-only merge, terminal stop-list. No bridge-tracing,
no behavioural clustering, no multi-hop graphs. v1 deliberately MISSES
sophisticated operators AND (via sampling) the fastest rug-and-die launches.
Solana only. The best-effort/gap flag is mandatory and surfaced.

NOTE: DESIGN_meme_cluster_scorer.md (the stated source of truth) is ABSENT from
the repo; this module follows prompts/build_meme_scorer.md, as that prompt
instructs when the doc is missing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from config import settings
from database import queries as q
from follow import labels, sol_parse
from follow.base import BaseStreamingDataSource

logger = logging.getLogger(__name__)

# Decision vocabulary — binary + abstention-heavy.
DECISION_AVOID     = "avoid"
DECISION_NO_SIGNAL = "no_signal"
# NO-SIGNAL is NOT "safe" — it means we found no LAZY manipulation, nothing more.
NO_SIGNAL_LABEL = "no lazy manipulation detected"

# Label confidence — HARD (irreversible on-chain event) vs SOFT (gameable floors).
CONF_HARD = "hard"
CONF_SOFT = "soft"


# ── Launch visibility seam (Option 1: sampling) ──────────────────────────────

@dataclass
class Launch:
    """One observed launch. detected_at is when the mint became knowable at
    sample time; creator/signature ride for audit. best_effort flags that the
    sampling source may have MISSED faster launches between polls."""
    mint:        str
    detected_at: float                     # epoch seconds — sample time
    creator:     Optional[str] = None
    source:      str = "sampling"
    meta:        dict = field(default_factory=dict)


class LaunchSource(ABC):
    """How new launches become knowable. The seam exists so a future free
    launch-FEED backend is a swap, not a re-plumb — but v1 has ONE impl
    (SamplingLaunchSource). There is deliberately NO submit/execute here."""

    source_id:  str  = "base_launch_source"
    best_effort: bool = True               # honesty: never imply completeness

    @abstractmethod
    async def get_new_launches(self) -> list[Launch]:
        """New launches observed since the last poll. MUST NOT raise — degrade
        to [] (a missed poll is a documented sampling gap, not a crash)."""
        ...


class SamplingLaunchSource(LaunchSource):
    """v1 launch visibility: SAMPLE the launchpad program on the existing
    rate-limited RPC client, catch the launches you can, ACCEPT GAPS.

    KNOWN BIAS (documented + surfaced): public RPC cannot drink the pump.fun
    firehose, so sampling under-catches the FASTEST rug-and-die launches (they
    live between polls). Live rug-rates therefore run LIGHT on fast rugs and the
    maturation window calibrates toward slower deaths. This source NEVER pretends
    completeness — best_effort=True and a gap counter are exposed.

    This is the ONLY LaunchSource impl in v1; the feed backend is explicitly NOT
    built now."""

    source_id  = "sampling"
    best_effort = True

    def __init__(self, rpc=None, *, launchpads: Optional[list[str]] = None):
        self._rpc = rpc
        self._launchpads = launchpads
        self._seen_sigs: set[str] = set()
        self._seen_mints: set[str] = set()
        self.gaps = 0                       # polls that failed / returned nothing usable

    def _launchpad_program_ids(self) -> list[str]:
        names = (self._launchpads if self._launchpads is not None
                 else list(getattr(settings, "WALLETFLOW_PARSE_LAUNCHPADS", ["pumpfun"])))
        return [sol_parse.LAUNCHPAD_PROGRAM_IDS[n]
                for n in names if n in sol_parse.LAUNCHPAD_PROGRAM_IDS]

    async def get_new_launches(self) -> list[Launch]:
        """Poll the launchpad program's recent signatures, decode the ones we
        can into Launch records. Best-effort throughout — any RPC failure records
        a gap and returns what we have. Never raises."""
        if self._rpc is None:
            return []
        out: list[Launch] = []
        detected_at = time.time()
        for program_id in self._launchpad_program_ids():
            try:
                sigs = await self._rpc.get_signatures_for_address(program_id, limit=100)
            except Exception as e:
                self.gaps += 1
                logger.debug("meme sampling: signature poll failed: %s", e)
                continue
            for s in sigs or []:
                sig = s.get("signature")
                if not sig or sig in self._seen_sigs:
                    continue
                self._seen_sigs.add(sig)
                try:
                    tx = await self._rpc.get_transaction(sig)
                except Exception as e:
                    self.gaps += 1
                    logger.debug("meme sampling: getTransaction %s failed: %s", sig, e)
                    continue
                for ev in sol_parse.launch_events(tx or {}, launchpads=self._launchpads):
                    mint = ev.get("mint")
                    if not mint or mint in self._seen_mints:
                        continue
                    self._seen_mints.add(mint)
                    out.append(Launch(
                        mint=mint, detected_at=detected_at,
                        creator=ev.get("creator"), source="sampling",
                        meta={"signature": ev.get("signature"),
                              "sampling_gap_note": "fastest rug-and-die launches "
                              "may be missed between polls"}))
        return out


# ── ONE shared point-in-time rug-rate (the binary-correctness spine) ─────────

def rug_rate_as_of(funder: Optional[str], as_of: datetime) -> dict:
    """THE single point-in-time rug-rate query for a funding cluster. Returns a
    dict; the rate uses ONLY the funder's launches that RESOLVED STRICTLY before
    `as_of` (resolved_at < as_of). Launches detected but not yet resolved (or
    resolved at/after `as_of`) are CENSORED — excluded from the denominator,
    NEVER counted as a non-rug.

    Reused VERBATIM by the live scorer (decide), the replay harness (run_replay),
    and the web as-of inspector. Do NOT add a second as-of filter anywhere — this
    is the one place "what did we know at T" lives, exactly as
    skill_scorer.resolved_actions_as_of is for the wallet watcher.

    The HARD vs SOFT mix is carried alongside the rate: floors are gameable, so a
    soft label is never trusted alone — the decision sees both."""
    base = {
        "funder":          funder,
        "as_of":           as_of.isoformat(),
        "resolved_sample": 0,
        "rugs":            0,
        "rate":            0.0,
        "censored":        0,
        "hard_rugs":       0,
        "soft_rugs":       0,
        "confidence":      "none",
    }
    if not funder:
        return base
    try:
        rows = q.get_funder_launches(funder)        # raw, resolved AND unresolved
    except Exception as e:
        logger.debug("rug_rate_as_of(%s) read failed: %s", funder, e)
        return base

    resolved = 0
    rugs = 0
    censored = 0
    hard_rugs = 0
    soft_rugs = 0
    for r in rows:
        ra = r.get("resolved_at")
        if ra is None or not (ra < as_of):          # STRICTLY before — the no-leak boundary
            censored += 1                            # pending / future == censored, not non-rug
            continue
        resolved += 1
        if r.get("outcome") == "rug":
            rugs += 1
            if r.get("label_confidence") == CONF_SOFT:
                soft_rugs += 1
            else:
                hard_rugs += 1

    rate = (rugs / resolved) if resolved else 0.0
    if rugs == 0:
        confidence = "none"
    elif hard_rugs and soft_rugs:
        confidence = "mixed"
    elif hard_rugs:
        confidence = CONF_HARD
    else:
        confidence = CONF_SOFT                       # rate rests on gameable floors alone
    return {
        **base,
        "resolved_sample": resolved,
        "rugs":            rugs,
        "rate":            round(rate, 4),
        "censored":        censored,
        "hard_rugs":       hard_rugs,
        "soft_rugs":       soft_rugs,
        "confidence":      confidence,
    }


def funder_rug_rate(funder: Optional[str]) -> Optional[float]:
    """The bait-resistance hook discovery.py consults (one place to wire). Returns
    the funder's point-in-time rug rate as-of NOW, or None on cold-start /
    insufficient resolved sample — None maps to the "unknown" warning, so a funder
    with no evidence is NEVER assumed clean and never auto-flagged."""
    rr = rug_rate_as_of(funder, datetime.utcnow())
    if rr["resolved_sample"] < int(settings.MEME_AVOID_MIN_RESOLVED):
        return None
    return rr["rate"]


# ── Cluster graph (one hop, funder-only merge, terminal stop-list) ───────────

async def cluster_funder(wallet: str, rpc, *,
                         is_terminal: Optional[Callable[[str], bool]] = None
                         ) -> Optional[str]:
    """The persistent rug-rate-bearing identity for an early-buyer wallet: its
    first funder (one hop, via follow.funding). STOP-LIST: if the trace reaches a
    terminal exchange/router/bridge we STOP and return None — "funded by Binance"
    is not a bundle, so a terminal-funded wallet is NOT clusterable. Never raises."""
    is_terminal = is_terminal or labels.is_terminal_address
    try:
        from follow import funding
        f = await funding.first_funder(wallet, rpc, is_terminal=is_terminal)
    except Exception as e:
        logger.debug("cluster_funder(%s) failed: %s", wallet, e)
        return None
    if not f or is_terminal(f):
        return None                                  # stop at terminal — do not cluster through it
    return f


def cluster_buyers(buyer_funders: dict[str, Optional[str]]) -> dict[str, list[str]]:
    """Funder-only, HARD-links-only merge: two wallets share a cluster IFF they
    share a (non-terminal) funder. Behavioural similarity (same token, similar
    timing) is NEVER used — that is circular and leaks. A None funder (unknown or
    terminal) is not clusterable and is dropped. Returns funder -> [wallets]."""
    clusters: dict[str, list[str]] = {}
    for wallet, funder in (buyer_funders or {}).items():
        if not funder:
            continue
        clusters.setdefault(funder, []).append(wallet)
    return clusters


# ── Maturation / labelling (define "rugged" without wet labels) ──────────────

def label_from_rug_events(events: list[dict]) -> Optional[tuple[str, str]]:
    """IRREVERSIBLE rug events (LP removal, mint-authority abuse, dev dump) ->
    label instantly, HARD confidence. Returns ('rug', 'hard') if any irreversible
    event is present, else None. These are the ground-truth set."""
    if events:
        return ("rug", CONF_HARD)
    return None


def label_slowdeath(metric_series: list[dict], *,
                    liq_floor: Optional[float] = None,
                    vol_floor: Optional[float] = None,
                    window_h: Optional[float] = None) -> Optional[tuple[str, str, float]]:
    """SLOW-DEATH -> SOFT label, fired at WINDOW CLOSE. metric_series is an
    ascending list of {ts(epoch), liq_usd, vol_usd} samples. A launch labels
    'rug'/'soft' ONLY if liquidity < floor AND volume < floor held CONTINUOUSLY
    for window_h (the sustained requirement is what prevents wetness — a momentary
    dip that recovers does NOT label). Returns ('rug','soft', close_ts) or None.

    Floors are gameable, so this label is SOFT and never trusted alone (the
    scorer carries the hard/soft mix). Pure — operates on the series handed in."""
    liq_floor = float(settings.MEME_LIQ_FLOOR_USD if liq_floor is None else liq_floor)
    vol_floor = float(settings.MEME_VOL_FLOOR_USD if vol_floor is None else vol_floor)
    window_s = float(settings.MEME_SLOWDEATH_WINDOW_H if window_h is None else window_h) * 3600.0
    if not metric_series:
        return None
    run_start: Optional[float] = None
    for sample in metric_series:
        below = (float(sample.get("liq_usd", 0.0)) < liq_floor
                 and float(sample.get("vol_usd", 0.0)) < vol_floor)
        ts = float(sample.get("ts", 0.0))
        if below:
            if run_start is None:
                run_start = ts
            elif (ts - run_start) >= window_s:
                return ("rug", CONF_SOFT, ts)        # sustained -> label at window close
        else:
            run_start = None                          # recovery resets the run (no wet label)
    return None


def survival_resolution(detected_at: float, now: float, *,
                        horizon_h: Optional[float] = None
                        ) -> Optional[tuple[str, str, float]]:
    """SURVIVAL: a launch that reaches the maturation horizon with no rug is a
    NON-RUG. Before the horizon it is CENSORED (pending) — returns None, NOT a
    non-rug. Returns ('survived','hard', horizon_ts) once matured."""
    horizon_s = float(settings.MEME_MATURATION_HORIZON_H if horizon_h is None
                      else horizon_h) * 3600.0
    if (now - detected_at) >= horizon_s:
        return ("survived", CONF_HARD, detected_at + horizon_s)
    return None                                       # censored: excluded from the denominator


def calibrate_window(resolved_launches: Optional[list[dict]] = None,
                     *, percentile: float = 0.9) -> dict:
    """Derive the maturation/slow-death window from the DISTRIBUTION of
    detected_at -> rug-event times over IRREVERSIBLE (hard) rugs — choosing the
    window from data BEFORE checking profitability avoids overfitting it. Until
    enough hard rugs exist, falls back to the settings default and MARKS the
    result low-confidence (so any rate derived under it is flagged). Pure read."""
    if resolved_launches is None:
        try:
            resolved_launches = q.get_resolved_meme_launches()
        except Exception:
            resolved_launches = []
    deltas = []
    for r in resolved_launches:
        if r.get("outcome") != "rug" or r.get("label_confidence") != CONF_HARD:
            continue
        det, res = r.get("detected_at"), r.get("resolved_at")
        if det is None or res is None:
            continue
        try:
            deltas.append((res - det).total_seconds() / 3600.0)
        except Exception:
            continue
    default_h = float(settings.MEME_MATURATION_HORIZON_H)
    if len(deltas) < int(settings.MEME_AVOID_MIN_RESOLVED):
        return {"window_h": default_h, "n_hard_rugs": len(deltas),
                "low_confidence": True, "source": "settings_default"}
    deltas.sort()
    idx = min(len(deltas) - 1, int(percentile * len(deltas)))
    return {"window_h": round(deltas[idx], 2), "n_hard_rugs": len(deltas),
            "low_confidence": False, "source": "calibrated"}


# ── Decision rule (abstention-first) ─────────────────────────────────────────

def decide(present_funders, as_of: Optional[datetime] = None) -> dict:
    """AVOID iff a present funder-cluster has BOTH a high point-in-time rug-rate
    (>= MEME_AVOID_MIN_RUGRATE) AND a sufficient RESOLVED sample
    (>= MEME_AVOID_MIN_RESOLVED). One funder with one prior rug -> abstain.
    Cold-start (unknown funder, zero resolved) -> NO-SIGNAL, NOT auto-suspect
    (flagging on no evidence is alarm fatigue; the base rate is a validation
    prior, not a live flag). The decision is handed BOTH the rate and the
    resolved-sample-size AND the hard/soft mix. Routes through the ONE shared
    rug_rate_as_of. Never raises."""
    as_of = as_of or datetime.utcnow()
    min_resolved = int(settings.MEME_AVOID_MIN_RESOLVED)
    min_rate = float(settings.MEME_AVOID_MIN_RUGRATE)
    worst: Optional[dict] = None
    for funder in set(present_funders or []):
        rr = rug_rate_as_of(funder, as_of)
        if rr["resolved_sample"] >= min_resolved and rr["rate"] >= min_rate:
            if worst is None or rr["rate"] > worst["rate"]:
                worst = rr
    if worst is not None:
        return {
            "decision":        DECISION_AVOID,
            "funder":          worst["funder"],
            "rate_as_of":      worst["rate"],
            "resolved_sample": worst["resolved_sample"],
            "confidence":      worst["confidence"],
            "reason":          (f"funder-cluster rug-rate {worst['rate']:.2f} over "
                                f"{worst['resolved_sample']} resolved launches "
                                f"(confidence={worst['confidence']})"),
        }
    return {
        "decision":        DECISION_NO_SIGNAL,
        "funder":          None,
        "rate_as_of":      None,
        "resolved_sample": 0,
        "confidence":      "none",
        # NO-SIGNAL is NOT "safe" — only "no lazy manipulation detected".
        "reason":          NO_SIGNAL_LABEL,
    }


# ── Replay / validation harness (shares the ONE as-of fn) ────────────────────

def run_replay(launches: Optional[list[dict]] = None, *,
               test_fraction: Optional[float] = None) -> dict:
    """Run the assembled scorer over historical launches using ONLY each launch's
    POINT-IN-TIME state (the SAME rug_rate_as_of the live scorer uses — never a
    copy), producing TRUE-POSITIVE / FALSE-POSITIVE / COVERAGE. A held-out TEST
    SET (the tail `test_fraction`) is reported separately and the threshold tuning
    never touches it. Coverage = fraction of rugs even ELIGIBLE to catch (a
    present funder-cluster carried ANY prior resolved history at detection) —
    exposing how much "lazy fruit only" + sampling actually covers. Never raises.

    Each launch dict must carry: mint, detected_at, outcome, and
    present_funders (the cluster funders known at detection)."""
    if launches is None:
        launches = _replay_universe()
    test_fraction = (float(settings.MEME_REPLAY_TEST_FRACTION)
                     if test_fraction is None else float(test_fraction))
    n = len(launches)
    split = int(n * (1.0 - test_fraction))
    train, test = launches[:split], launches[split:]

    def _eval(rows: list[dict]) -> dict:
        rugs = [r for r in rows if r.get("outcome") == "rug"]
        survived = [r for r in rows if r.get("outcome") == "survived"]
        tp = fp = eligible = 0
        for r in rows:
            det = r.get("detected_at")
            as_of = det if isinstance(det, datetime) else datetime.utcnow()
            funders = r.get("present_funders") or []
            # Eligibility: did ANY present funder have prior resolved history at T?
            has_prior = any(rug_rate_as_of(f, as_of)["resolved_sample"] > 0
                            for f in funders)
            if r.get("outcome") == "rug" and has_prior:
                eligible += 1
            d = decide(funders, as_of)
            flagged = (d["decision"] == DECISION_AVOID)
            if flagged and r.get("outcome") == "rug":
                tp += 1
            elif flagged and r.get("outcome") == "survived":
                fp += 1
        return {
            "n":             len(rows),
            "n_rugs":        len(rugs),
            "n_survived":    len(survived),
            "true_positive_rate": (tp / len(rugs)) if rugs else 0.0,
            "false_positive_rate": (fp / len(survived)) if survived else 0.0,
            "coverage":      (eligible / len(rugs)) if rugs else 0.0,
        }

    return {
        "uses_shared_as_of_fn": True,        # run_replay calls rug_rate_as_of directly
        "test_fraction":        test_fraction,
        "train":                _eval(train),
        "test":                 _eval(test),
    }


def _replay_universe() -> list[dict]:
    """Assemble the replay universe from resolved launches + their cluster
    funders. Best-effort read; empty on any failure."""
    out: list[dict] = []
    try:
        for r in q.get_resolved_meme_launches():
            buyers = q.get_meme_launch_buyers(r["mint"])
            funders = sorted({b["funder"] for b in buyers if b.get("funder")})
            out.append({"mint": r["mint"], "detected_at": r.get("detected_at"),
                        "outcome": r.get("outcome"), "present_funders": funders})
    except Exception as e:
        logger.debug("replay universe read failed: %s", e)
    return out


# ── The observer ─────────────────────────────────────────────────────────────

class MemeScorer(BaseStreamingDataSource):
    """Capital-free OBSERVER that watches sampled Solana launches, clusters early
    buyers by funder, scores each present funder's point-in-time rug rate, and
    logs an AVOID | NO-SIGNAL decision per launch. No execution path, no follow,
    no BUY/SAFE — the systematic "spot the rig, don't be exit liquidity" signal."""

    source_id    = "meme_scorer"
    display_name = "Meme-Coin Cluster Rug-Rate Scorer"
    optional     = True

    def __init__(self, launch_source: Optional[LaunchSource] = None, rpc=None):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._rpc = rpc
        self._launch_source = launch_source
        # In-memory stats (the DB is the durable record).
        self._launches_seen = 0
        self._avoid_count = 0
        self._no_signal_count = 0
        self._last_scan_ts: Optional[float] = None

    # ── Availability ────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Runnable when enabled AND the shared label set is loaded (the terminal
        stop-list is required to cluster honestly). Free public RPC needs no key."""
        if not bool(getattr(settings, "MEME_ENABLED", False)):
            return False
        return labels.is_available()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin the sampling-poll loop. Degrades gracefully: if unavailable the
        observer stays idle rather than raising to the host agent."""
        try:
            labels.load_seed_labels()
        except Exception as e:
            logger.debug("meme: seed labels failed: %s", e)
        if not self.is_available():
            logger.info("meme: not available (enabled=%s, labels=%s) — observer idle",
                        getattr(settings, "MEME_ENABLED", False), labels.is_available())
            self._running = False
            return
        if self._rpc is None:
            from follow.rpc import SolanaRpc
            self._rpc = SolanaRpc()
        if self._launch_source is None:
            self._launch_source = SamplingLaunchSource(self._rpc)
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("meme: observing launches via %s launch source (best-effort)",
                    self._launch_source.source_id)

    async def stop(self) -> None:
        """Clean teardown. Idempotent; never raises."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        self._task = None
        if self._rpc is not None:
            try:
                await self._rpc.close()
            except Exception:
                pass
        logger.info("meme: stopped")

    async def _scan_loop(self) -> None:
        """Poll the launch source on a cadence + run a maturation pass. One
        failing pass records nothing and never crashes the host agent."""
        interval = max(15.0, float(getattr(settings, "MEME_LAUNCH_POLL_S", 30)))
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("meme: scan pass failed: %s", e)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _scan_once(self) -> None:
        """One observation pass: pull new launches, cluster + score each. Every
        step is defensive — a failing launch is logged and skipped, not propagated."""
        if self._launch_source is None:
            return
        try:
            launches = await self._launch_source.get_new_launches()
        except Exception as e:
            logger.debug("meme: get_new_launches failed: %s", e)
            launches = []
        for launch in launches or []:
            try:
                await self.process_launch(launch)
            except Exception as e:
                logger.debug("meme: process_launch(%s) failed: %s", launch.mint, e)
        self._last_scan_ts = time.time()

    async def process_launch(self, launch: Launch,
                             buyer_funders: Optional[dict[str, Optional[str]]] = None
                             ) -> dict:
        """Persist a launch, cluster its early buyers by funder, score each
        present funder POINT-IN-TIME (as-of the launch's detected_at), and log the
        AVOID | NO-SIGNAL decision. buyer_funders may be injected (tests / a future
        feed); otherwise the early-buyer set is derived best-effort. Returns the
        decision dict. Never raises out."""
        detected_dt = datetime.utcfromtimestamp(launch.detected_at or time.time())
        try:
            q.upsert_meme_launch({
                "mint": launch.mint, "detected_at": detected_dt,
                "source": launch.source, "meta": launch.meta})
            self._launches_seen += 1
        except Exception as e:
            logger.debug("meme: persist launch %s failed: %s", launch.mint, e)

        if buyer_funders is None:
            buyer_funders = await self._derive_buyer_funders(launch)
        # Persist buyers + funders (a launch contributes to each present funder).
        for wallet, funder in (buyer_funders or {}).items():
            try:
                q.insert_meme_launch_buyer({
                    "launch_mint": launch.mint, "wallet": wallet,
                    "funder": funder, "buy_at": detected_dt})
                if funder:
                    q.upsert_meme_funder(funder)
            except Exception as e:
                logger.debug("meme: persist buyer %s failed: %s", wallet, e)

        clusters = cluster_buyers(buyer_funders or {})
        decision = decide(set(clusters.keys()), as_of=detected_dt)
        try:
            q.insert_meme_decision({
                "launch_mint": launch.mint, "decided_at": detected_dt,
                "decision": decision["decision"], "funder": decision["funder"],
                "rate_as_of": decision["rate_as_of"],
                "resolved_sample": decision["resolved_sample"],
                "confidence": decision["confidence"], "reason": decision["reason"]})
        except Exception as e:
            logger.debug("meme: persist decision %s failed: %s", launch.mint, e)
        if decision["decision"] == DECISION_AVOID:
            self._avoid_count += 1
            logger.info("meme AVOID: %s funder=%s rate=%.2f sample=%d",
                        launch.mint, decision["funder"], decision["rate_as_of"] or 0.0,
                        decision["resolved_sample"])
        else:
            self._no_signal_count += 1
        return decision

    async def _derive_buyer_funders(self, launch: Launch
                                    ) -> dict[str, Optional[str]]:
        """Best-effort early-buyer -> funder map for a launch, crawled live off
        the rate-limited public RPC. Two hops, both throttled by the shared
        limiter (a slow background crawl is acceptable for a $0 observer):

          1. the mint's newest signatures (for a freshly-sampled launch these ARE
             the early activity) → keep buys inside MEME_EARLY_BUYER_WINDOW_S,
             excluding the create itself and the creator;
          2. each unique buyer's first funder (the rug-rate-bearing identity),
             stop-listed via cluster_funder so terminal-funded wallets drop out.

        Bounded by MEME_BUYER_MAX_SIGNATURES / MEME_BUYER_MAX_PER_LAUNCH. Returns
        {} (a documented gap, never 'no cluster = clean') on any failure — the
        public RPC under-serves per-mint crawling, so this catches SOME early
        buyers, never guaranteed all. Never raises out."""
        rpc = self._rpc
        if rpc is None or not launch.mint:
            return {}
        window_s = float(getattr(settings, "MEME_EARLY_BUYER_WINDOW_S", 300) or 300)
        max_sigs = int(getattr(settings, "MEME_BUYER_MAX_SIGNATURES", 60) or 60)
        max_buyers = int(getattr(settings, "MEME_BUYER_MAX_PER_LAUNCH", 12) or 12)
        detected = float(launch.detected_at or time.time())
        try:
            sigs = await rpc.get_signatures_for_address(launch.mint, limit=max_sigs)
        except Exception as e:
            logger.debug("meme: get_signatures(%s) failed: %s", launch.mint, e)
            return {}
        # Oldest-first within the fetched window so we take the EARLIEST buyers.
        sigs = sorted((s for s in (sigs or []) if isinstance(s, dict)),
                      key=lambda s: s.get("blockTime") or 0)
        buyers: dict[str, Optional[str]] = {}
        for s in sigs:
            if len(buyers) >= max_buyers:
                break
            if s.get("err"):                          # failed tx — not a real buy
                continue
            bt = s.get("blockTime")
            if bt is not None and (float(bt) - detected) > window_s:
                continue                              # past the early window
            sig = s.get("signature")
            if not sig:
                continue
            try:
                tx = await rpc.get_transaction(sig)
            except Exception as e:
                logger.debug("meme: get_transaction(%s) failed: %s", sig, e)
                continue
            buyer = sol_parse.early_buyer(tx, launch.mint)
            if not buyer or buyer == launch.creator or buyer in buyers:
                continue
            buyers[buyer] = None                      # funder resolved below
        # Hop 2 — each unique buyer's first funder (cached + stop-listed).
        for wallet in list(buyers.keys()):
            try:
                buyers[wallet] = await cluster_funder(wallet, rpc)
            except Exception as e:
                logger.debug("meme: cluster_funder(%s) failed: %s", wallet, e)
                buyers[wallet] = None
        return buyers

    # ── Stats (for the snapshot) ────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Never raises — zeroed/empty defaults on any failure. best_effort is
        always True: sampling under-catches the fastest rugs, so no surface may
        imply the launch history is complete."""
        try:
            stale = labels.staleness()
        except Exception:
            stale = {"is_stale": True, "count": 0}
        ls = self._launch_source
        return {
            "source_id":       self.source_id,
            "running":         bool(self._running),
            "available":       self._safe_available(),
            "launches_seen":   self._launches_seen,
            "avoid":           self._avoid_count,
            "no_signal":       self._no_signal_count,
            "last_scan_ts":    self._last_scan_ts,
            "label_staleness": stale,
            # Honesty about completeness — sampling, not the firehose.
            "best_effort":     True,
            "launch_source":   (ls.source_id if ls is not None else "sampling"),
            "sampling_gaps":   int(getattr(ls, "gaps", 0)) if ls is not None else 0,
            "scope_note":      ("LOW-HANGING FRUIT only (single-hop funder bundles); "
                                "sampling MISSES fast rugs; v1 misses sophisticated "
                                "operators by design. NO-SIGNAL != safe."),
        }

    def _safe_available(self) -> bool:
        try:
            return self.is_available()
        except Exception:
            return False


__all__ = [
    "MemeScorer",
    "LaunchSource", "SamplingLaunchSource", "Launch",
    "rug_rate_as_of", "funder_rug_rate",
    "cluster_funder", "cluster_buyers",
    "label_from_rug_events", "label_slowdeath", "survival_resolution",
    "calibrate_window", "decide", "run_replay",
    "DECISION_AVOID", "DECISION_NO_SIGNAL", "NO_SIGNAL_LABEL",
    "CONF_HARD", "CONF_SOFT",
]
