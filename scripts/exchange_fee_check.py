"""Read-only diagnostic: what fees does an exchange ACTUALLY charge?

Generalises scripts/mexc_fee_check.py (the MEXC-specific original) to any
ccxt exchange the bot routes through. For each sample pair it reports up to
three independent sources and compares them against the bot's configured
fees (SCALP_FEE_OVERRIDES + ARB_FEE_MAP):

  1. fetch_trading_fee(symbol)  — the PRIVATE per-account fee endpoint.
                                  Authoritative for *this* account's tier.
                                  Skipped when no API keys are configured.
  2. load_markets() maker/taker — the exchange's PUBLIC standard rate.
                                  (Several venues — bitget, mexc — publish
                                  per-symbol makerFeeRate/takerFeeRate in the
                                  public symbols endpoint; ccxt maps it here.)
  3. raw market info            — the venue's own makerFeeRate/takerFeeRate
                                  fields where present, shown for cross-check.

It then runs the scalper's economics (FeeManager math) at the observed worst
taker and at the maker rate, so the viability impact is explicit.

Usage:
    python scripts/exchange_fee_check.py                # default: mexc
    python scripts/exchange_fee_check.py bitget
    python scripts/exchange_fee_check.py bybit --pairs BTC/USDT,ETH/USDT

SAFETY: read-only. No order is created, amended, or cancelled. API secrets
are never printed (only a masked key id).
"""
from __future__ import annotations

import argparse
import os

import ccxt
from dotenv import load_dotenv

from config import settings
from agents.scalping_agent import FeeManager

load_dotenv("config/keys.env")

DEFAULT_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "PEPE/USDT"]


def _mask(api_key: str) -> str:
    if not api_key:
        return "<none>"
    return f"{api_key[:6]}…{api_key[-3:]}" if len(api_key) > 9 else "<short>"


def _bps(frac) -> float | None:
    return None if frac is None else float(frac) * 10_000.0


def _key_sets(exchange_id: str) -> list[tuple[str, str, str]]:
    """(label, api_key, secret) candidates for this exchange. MEXC's
    pair-restricted multi-key layout plus the generic {EX}_API_KEY pattern."""
    out = []
    up = exchange_id.upper()
    if os.getenv(f"{up}_API_KEY") and os.getenv(f"{up}_SECRET"):
        out.append((f"{up}_API_KEY", os.getenv(f"{up}_API_KEY"), os.getenv(f"{up}_SECRET")))
    for idx in (1, 2, 3, 4):
        api = os.getenv(f"{up}_KEY_{idx}_API_KEY")
        sec = os.getenv(f"{up}_KEY_{idx}_SECRET")
        if api and sec:
            out.append((f"{up}_KEY_{idx}", api, sec))
    return out


def _fee_via_trading_fee(client, symbol: str) -> dict:
    try:
        f = client.fetch_trading_fee(symbol)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "maker_bps": _bps(f.get("maker")), "taker_bps": _bps(f.get("taker"))}


def _public_fees(client, symbol: str) -> dict:
    mkt = (client.markets or {}).get(symbol) or {}
    info = mkt.get("info") or {}
    return {
        "maker_bps": _bps(mkt.get("maker")),
        "taker_bps": _bps(mkt.get("taker")),
        "raw_maker": info.get("makerFeeRate"),
        "raw_taker": info.get("takerFeeRate"),
    }


def _scalper_economics(exchange_id: str, taker_bps: float, maker_bps: float) -> None:
    sym = DEFAULT_PAIRS[0]
    fm = FeeManager({exchange_id: {"maker": maker_bps, "taker": taker_bps}})

    def snap(rt):
        tp = rt + float(settings.SCALP_NET_PROFIT_TARGET_BPS)
        rr = float(settings.SCALP_RR_RATIO)
        sl = tp / rr if rr > 0 else tp
        be = (rt + sl) / (tp + sl) if (tp + sl) > 0 else 1.0
        return rt, tp, sl, be

    limit = float(settings.SCALP_MAX_BREAKEVEN_WIN_RATE)
    for label, rt in (("taker basis", taker_bps * 2.0), ("maker basis", maker_bps * 2.0)):
        rt, tp, sl, be = snap(rt)
        verdict = "viable" if be <= limit else "BLOCKED"
        print(f"  scalp {label:<11}: round_trip={rt:.1f}bps tp={tp:.1f} sl={sl:.1f} "
              f"breakeven_WR={be:.1%} -> {verdict} (cap {limit:.0%})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("exchange", nargs="?", default="mexc")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    args = ap.parse_args()
    exchange_id = args.exchange.lower()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    scalp_ov = settings.SCALP_FEE_OVERRIDES.get(exchange_id)
    arb_fee = settings.ARB_FEE_MAP.get(exchange_id)
    print("=" * 78)
    print(f"{exchange_id.upper()} REAL-FEE CHECK  (read-only)")
    print("=" * 78)
    print(f"Configured: SCALP_FEE_OVERRIDES[{exchange_id!r}] = {scalp_ov}")
    print(f"            ARB_FEE_MAP[{exchange_id!r}]         = {arb_fee}"
          f"{'' if arb_fee is None else f'  ({arb_fee*10000:.1f} bps)'}")
    print(f"Sample pairs: {', '.join(pairs)}")
    print()

    klass = getattr(ccxt, exchange_id, None)
    if klass is None:
        print(f"ccxt has no exchange {exchange_id!r}")
        return

    makers: list[float] = []
    takers: list[float] = []

    # ── Public standard rates (no keys needed) ───────────────────────────
    pub = klass({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    try:
        pub.load_markets()
    except Exception as e:
        print(f"public load_markets failed: {type(e).__name__}: {e}")
        return
    print("PUBLIC standard rates (exchange symbols endpoint via ccxt):")
    for sym in pairs:
        if sym not in (pub.markets or {}):
            print(f"  {sym:<10} not listed")
            continue
        f = _public_fees(pub, sym)
        print(f"  {sym:<10} maker={f['maker_bps']}bps taker={f['taker_bps']}bps"
              f"   (raw makerFeeRate={f['raw_maker']} takerFeeRate={f['raw_taker']})")
        if f["maker_bps"] is not None:
            makers.append(f["maker_bps"])
        if f["taker_bps"] is not None:
            takers.append(f["taker_bps"])

    # ── Private per-account fee (authoritative; needs keys) ──────────────
    key_sets = _key_sets(exchange_id)
    if not key_sets:
        print(f"\nPRIVATE account fee: skipped — no {exchange_id.upper()}_API_KEY / "
              f"{exchange_id.upper()}_KEY_N_* in keys.env (public standard rate is "
              "the best available evidence)")
    for label, api, sec in key_sets:
        print(f"\nPRIVATE account fee via {label} ({_mask(api)}):")
        client = klass({"apiKey": api, "secret": sec,
                        "enableRateLimit": True, "options": {"defaultType": "spot"}})
        try:
            client.load_markets()
        except Exception as e:
            print(f"  load_markets failed: {type(e).__name__}: {e}")
            continue
        for sym in pairs:
            if sym not in (client.markets or {}):
                continue
            tf = _fee_via_trading_fee(client, sym)
            if tf.get("ok"):
                print(f"  {sym:<10} maker={tf['maker_bps']}bps taker={tf['taker_bps']}bps")
                if tf["maker_bps"] is not None:
                    makers.append(tf["maker_bps"])
                if tf["taker_bps"] is not None:
                    takers.append(tf["taker_bps"])
            else:
                print(f"  {sym:<10} ERROR {tf.get('error')}")

    # ── Verdict ──────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    if not takers:
        print("VERDICT: no fee data readable.")
        print("=" * 78)
        return
    worst_taker = max(takers)
    worst_maker = max(makers) if makers else 0.0
    print(f"Observed: maker up to {worst_maker:.1f}bps, taker up to {worst_taker:.1f}bps "
          f"(min taker {min(takers):.1f}bps across {len(takers)} samples)")
    if scalp_ov is not None:
        ov_t = float(scalp_ov.get("taker", 0.0))
        ov_m = float(scalp_ov.get("maker", 0.0))
        if abs(worst_taker - ov_t) < 0.5 and abs(worst_maker - ov_m) < 0.5:
            print(f"SCALP override matches observed rates.")
        else:
            print(f"SCALP MISMATCH: override says {ov_m}/{ov_t}bps but observed "
                  f"{worst_maker:.1f}/{worst_taker:.1f}bps (maker/taker).")
    if arb_fee is not None:
        arb_bps = float(arb_fee) * 10000.0
        if abs(worst_taker - arb_bps) < 0.5:
            print(f"ARB_FEE_MAP matches observed taker.")
        else:
            print(f"ARB MISMATCH: ARB_FEE_MAP says {arb_bps:.1f}bps/leg but observed "
                  f"taker is {worst_taker:.1f}bps — arb net-gap math is off by "
                  f"{(worst_taker - arb_bps):.1f}bps per leg on this venue.")
    print()
    _scalper_economics(exchange_id, worst_taker, worst_maker)
    print("=" * 78)


if __name__ == "__main__":
    main()
