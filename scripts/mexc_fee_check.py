"""Read-only diagnostic: what fees does MEXC ACTUALLY charge?

The scalper's FeeManager applies settings.SCALP_FEE_OVERRIDES["mexc"] =
{"maker": 0.0, "taker": 0.0} *over* CCXT data, so the web UI and the
entry gate both believe MEXC is 0%/0%. That override is an assumption
("0% confirmed standard rate"), never verified against the live account.
This script verifies it, per key, against three independent sources:

  1. fetch_trading_fee(symbol)      — MEXC's PRIVATE per-symbol fee endpoint
                                      (spotPrivateGetTradeFee). Authoritative.
  2. spotPrivateGetAccount()        — account-level maker/taker commission.
  3. load_markets() market maker/taker — CCXT's static default (what the
                                      FeeManager would read if the override
                                      were removed).

It then translates the REAL taker fee into the scalper's own economics
(round-trip, dynamic TP/SL, breakeven win rate, is_viable) using the
actual FeeManager math, so the impact of a non-zero fee on trade
viability is explicit.

SAFETY: read-only. No order is created, amended, or cancelled — the only
calls are public market loads and private *read* endpoints. API secrets
are NEVER printed (only a masked key id).
"""
from __future__ import annotations

import os

import ccxt
from dotenv import load_dotenv

from config import settings
from agents.scalping_agent import FeeManager

load_dotenv("config/keys.env")

# Pairs to sample per key. MEXC commission is usually account-level, but
# fetch_trading_fee is per-symbol, so we sample a few (BTC always + a
# spread of others) rather than hammering all ~80 SCALP_PAIRS.
SAMPLE_PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "PEPE/USDT"]


def _mask(api_key: str) -> str:
    """Show only enough of the key id to tell keys apart — never the secret."""
    if not api_key:
        return "<none>"
    return f"{api_key[:6]}…{api_key[-3:]}" if len(api_key) > 9 else "<short>"


def _bps(frac) -> float | None:
    """CCXT fee fraction (0.0005) -> bps (5.0). None passes through."""
    return None if frac is None else float(frac) * 10_000.0


def _fee_via_trading_fee(client, symbol: str) -> dict:
    """Authoritative per-symbol fee from MEXC's private trade-fee endpoint."""
    try:
        f = client.fetch_trading_fee(symbol)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "maker_bps": _bps(f.get("maker")),
        "taker_bps": _bps(f.get("taker")),
        "raw": {k: f.get(k) for k in ("maker", "taker", "percentage", "tierBased")},
    }


def _fee_via_account(client) -> dict:
    """Account-level commission from spotPrivateGetAccount.

    MEXC mirrors Binance's shape: integer makerCommission/takerCommission.
    Empirically these are whole basis points (10 => 10bps => 0.10%), so we
    report the raw integer AND its bps interpretation. We do NOT assume —
    both are shown so the operator can sanity-check the unit.
    """
    try:
        acct = client.spotPrivateGetAccount()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    mk = acct.get("makerCommission")
    tk = acct.get("takerCommission")
    out = {"ok": True, "maker_raw": mk, "taker_raw": tk}
    try:
        out["maker_bps_if_int_is_bps"] = float(mk) if mk is not None else None
        out["taker_bps_if_int_is_bps"] = float(tk) if tk is not None else None
    except (TypeError, ValueError):
        out["maker_bps_if_int_is_bps"] = None
        out["taker_bps_if_int_is_bps"] = None
    return out


def _fee_via_market(client, symbol: str) -> dict:
    """CCXT's static market maker/taker (the FeeManager 'ccxt' source)."""
    mkt = (client.markets or {}).get(symbol) or {}
    return {"maker_bps": _bps(mkt.get("maker")), "taker_bps": _bps(mkt.get("taker"))}


def _scalper_economics(real_taker_bps: float, real_maker_bps: float) -> dict:
    """Run the scalper's OWN FeeManager math at the real fee, for one
    representative pair, and compare to the current 0/0 override."""
    sym = SAMPLE_PAIRS[0]
    real = FeeManager({"mexc": {"maker": real_maker_bps, "taker": real_taker_bps}})
    cur  = FeeManager(settings.SCALP_FEE_OVERRIDES)

    def snap(fm):
        rt = fm.round_trip_bps("mexc", sym)
        tp, sl = fm.compute_tp_sl("mexc", sym)
        be = fm.breakeven_win_rate("mexc", sym, tp, sl)
        viable, reason = fm.is_viable("mexc", sym)
        return {"round_trip_bps": rt, "tp_bps": tp, "sl_bps": sl,
                "breakeven_win_rate": be, "viable": viable, "reason": reason}

    return {"symbol": sym, "at_real_fee": snap(real),
            "at_current_override_0_0": snap(cur),
            "net_target_bps": float(settings.SCALP_NET_PROFIT_TARGET_BPS),
            "max_breakeven_win_rate": float(settings.SCALP_MAX_BREAKEVEN_WIN_RATE)}


def main() -> None:
    ov = settings.SCALP_FEE_OVERRIDES.get("mexc", {})
    print("=" * 78)
    print("MEXC REAL-FEE CHECK  (read-only; verifies the scalper's 0%/0% override)")
    print("=" * 78)
    print(f"Bot override in use : SCALP_FEE_OVERRIDES['mexc'] = "
          f"maker={ov.get('maker')}bps taker={ov.get('taker')}bps")
    print(f"Sample pairs        : {', '.join(SAMPLE_PAIRS)}")
    print()

    real_taker_seen: list[float] = []
    real_maker_seen: list[float] = []

    for idx in (1, 2, 3, 4):
        api = os.getenv(f"MEXC_KEY_{idx}_API_KEY")
        sec = os.getenv(f"MEXC_KEY_{idx}_SECRET")
        print("-" * 78)
        if not api or not sec:
            print(f"KEY {idx}: no env (MEXC_KEY_{idx}_*) — skipped")
            continue
        print(f"KEY {idx}: {_mask(api)}")
        client = ccxt.mexc({
            "apiKey": api, "secret": sec,
            "enableRateLimit": True, "options": {"defaultType": "spot"},
        })
        try:
            client.load_markets()
        except Exception as e:
            print(f"  load_markets failed: {type(e).__name__}: {e}")
            continue

        # 2. Account-level commission (one call).
        acct = _fee_via_account(client)
        if acct.get("ok"):
            print(f"  account commission  : maker_raw={acct['maker_raw']} "
                  f"taker_raw={acct['taker_raw']}  "
                  f"(if integer==bps → maker={acct['maker_bps_if_int_is_bps']}bps "
                  f"taker={acct['taker_bps_if_int_is_bps']}bps)")
        else:
            print(f"  account commission  : ERROR {acct.get('error')}")

        # 1 + 3. Per-symbol authoritative fee + CCXT static default.
        for sym in SAMPLE_PAIRS:
            if sym not in (client.markets or {}):
                print(f"  {sym:<10} not listed for this key/account")
                continue
            tf  = _fee_via_trading_fee(client, sym)
            mkt = _fee_via_market(client, sym)
            if tf.get("ok"):
                mk, tk = tf["maker_bps"], tf["taker_bps"]
                print(f"  {sym:<10} API trade-fee : maker={mk}bps taker={tk}bps"
                      f"   | ccxt-static: maker={mkt['maker_bps']}bps "
                      f"taker={mkt['taker_bps']}bps")
                if tk is not None:
                    real_taker_seen.append(tk)
                if mk is not None:
                    real_maker_seen.append(mk)
            else:
                print(f"  {sym:<10} API trade-fee : ERROR {tf.get('error')}"
                      f"   | ccxt-static: maker={mkt['maker_bps']}bps "
                      f"taker={mkt['taker_bps']}bps")
                if mkt["taker_bps"] is not None:
                    real_taker_seen.append(mkt["taker_bps"])
                if mkt["maker_bps"] is not None:
                    real_maker_seen.append(mkt["maker_bps"])

    # ── Verdict ──────────────────────────────────────────────────────────
    print("=" * 78)
    if not real_taker_seen:
        print("VERDICT: could not read any real fee (no keys / all calls failed).")
        print("=" * 78)
        return

    worst_taker = max(real_taker_seen)
    worst_maker = max(real_maker_seen) if real_maker_seen else 0.0
    ov_taker = float(ov.get("taker", 0.0))
    print(f"Real taker observed : min={min(real_taker_seen):.2f}bps "
          f"max={worst_taker:.2f}bps  across {len(real_taker_seen)} samples")
    print(f"Real maker observed : "
          f"{'n/a' if not real_maker_seen else f'max={worst_maker:.2f}bps'}")
    if abs(worst_taker - ov_taker) < 1e-9:
        print(f"MATCH: real taker == override ({ov_taker}bps). 0% override is accurate.")
    else:
        print(f"MISMATCH: override says taker={ov_taker}bps but MEXC charges up to "
              f"{worst_taker:.2f}bps. The web UI / FeeManager are UNDERSTATING fees.")

    econ = _scalper_economics(worst_taker, worst_maker)
    a, c = econ["at_real_fee"], econ["at_current_override_0_0"]
    print()
    print(f"Scalper economics on {econ['symbol']} "
          f"(net target {econ['net_target_bps']}bps, "
          f"max breakeven WR {econ['max_breakeven_win_rate']:.0%}):")
    print(f"  at OVERRIDE 0/0 : round_trip={c['round_trip_bps']:.1f}bps "
          f"tp={c['tp_bps']:.1f} sl={c['sl_bps']:.1f} "
          f"breakeven_WR={c['breakeven_win_rate']:.1%} viable={c['viable']}")
    print(f"  at REAL fee     : round_trip={a['round_trip_bps']:.1f}bps "
          f"tp={a['tp_bps']:.1f} sl={a['sl_bps']:.1f} "
          f"breakeven_WR={a['breakeven_win_rate']:.1%} viable={a['viable']}")
    if not a["viable"]:
        print(f"  ⚠ at the REAL fee the scalper would REFUSE this trade: {a['reason']}")
    elif a["breakeven_win_rate"] > c["breakeven_win_rate"] + 1e-9:
        print(f"  ⚠ real fee raises the breakeven win rate "
              f"{c['breakeven_win_rate']:.1%} → {a['breakeven_win_rate']:.1%} "
              f"— every logged scalp edge is overstated by the 0/0 override.")
    print("=" * 78)


if __name__ == "__main__":
    main()
