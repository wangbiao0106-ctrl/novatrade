#!/usr/bin/env python3
"""Walk-forward follow-up for the recommended low/mid-liquidity execution.

The experiment fixes the execution recommendation to:

    1h sweep structure -> first bearish 15m close confirmation -> market short

It then compares a small, pre-declared parameter set across three liquidity
tiers and three chronological windows. This is deliberately smaller than a
new global grid: the goal is to test whether the recommendation transfers,
not to select another configuration from the same test segment.
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

import engine as E
import tune_execution as T


RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
SPLIT_WINDOWS = {
    "early": (int(pd.Timestamp("2026-04-01", tz="UTC").value // 1e6),
               int(pd.Timestamp("2026-06-01", tz="UTC").value // 1e6)),
    "middle": (int(pd.Timestamp("2026-06-01", tz="UTC").value // 1e6),
                int(pd.Timestamp("2026-08-01", tz="UTC").value // 1e6)),
    "late": (int(pd.Timestamp("2026-08-01", tz="UTC").value // 1e6),
              int(pd.Timestamp("2026-10-01", tz="UTC").value // 1e6)),
}

# These are the only profiles tested in this follow-up. The per-tier profiles
# are research diagnostics, not a request to hard-code static symbol lists.
PROFILES = {
    "baseline": {"retest_wait": 12, "rsi": 62, "vol": 1.5, "tp": 2.2, "buf": 0.5},
    "fast_same_filters": {"retest_wait": 6, "rsi": 62, "vol": 1.5, "tp": 2.2, "buf": 0.5},
    "uniform_candidate": {"retest_wait": 6, "rsi": 62, "vol": 2.0, "tp": 3.0, "buf": 0.5},
    "mid_profile": {"retest_wait": 6, "rsi": 62, "vol": 1.2, "tp": 1.5, "buf": 0.5},
    "low_profile": {"retest_wait": 6, "rsi": 65, "vol": 2.0, "tp": 3.0, "buf": 0.5},
}


def profile_cfg(profile):
    cfg = dict(T.BASE_CFG)
    cfg["retest_wait"] = profile["retest_wait"]
    filt = dict(T.BASE_FILTER)
    filt["rsi_s_min"] = profile["rsi"]
    filt["vol_mult"] = profile["vol"]
    return cfg, filt


def run_profile(data1, data15, pool_name, symbols, btc, profile_name,
                profile, slip_bps=0.0):
    cfg, filt = profile_cfg(profile)
    events = T.prepare_events(data1, symbols, cfg, filt, btc)
    rows = []
    confirmations = 0
    for sym, d1, ev, i in events:
        k = int(ev["k"][i])
        signal_t = int(d1["t"][k] + T.HOUR_MS)
        d15 = data15.get(sym)
        if d15 is None:
            continue
        p0 = int(np.searchsorted(d15["t"], signal_t, side="left"))
        p = -1
        for j in range(p0, min(p0 + 4, len(d15["c"]))):
            if d15["c"][j] <= ev["entry"][i] and d15["c"][j] < d15["o"][j]:
                p = j
                break
        if p < 0:
            continue
        confirmations += 1
        event = {key: ev[key][i] for key in ("ext", "atr_k")}
        trade = T.market_trade(
            sym, pool_name, f"{profile_name}_{int(slip_bps)}bps", d15,
            event, p, d15["c"][p], signal_t, 384, slip_bps=slip_bps,
            buf_atr=profile["buf"], tp_r=profile["tp"], fee=T.FEE,
        )
        if trade is not None:
            rows.append(trade.__dict__)
    return rows, len(events), confirmations


def period_stats(df, start=None, end=None):
    if start is not None:
        df = df[df.signal_t >= start]
    if end is not None:
        df = df[df.signal_t < end]
    return T.stats(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(RESULTS, "lowmid_followup"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    meta = E.load_meta()
    pools = T.pool_sets(meta)
    data1 = E.load_tf("1h")
    data15 = E.load_tf("15m")
    bd = data1.get("BTC")
    btc = (bd["t"], bd["c"], E.sma(bd["c"], 200)) if bd is not None else None

    all_rows = []
    signal_counts = []
    for pool_name in ("lowmid177", "lowmid_high59", "lowmid_mid59", "lowmid_low59"):
        symbols = pools[pool_name]
        for profile_name, profile in PROFILES.items():
            for slip_bps in (0.0, 10.0):
                rows, events, confirmations = run_profile(
                    data1, data15, pool_name, symbols, btc,
                    profile_name, profile, slip_bps=slip_bps,
                )
                all_rows.extend(rows)
                signal_counts.append({
                    "pool": pool_name, "profile": profile_name,
                    "slip_bps": slip_bps, "events": events,
                    "confirmations": confirmations, "filled": len(rows),
                    "confirmation_rate": confirmations / events if events else 0.0,
                })

    trades = pd.DataFrame(all_rows)
    if trades.empty:
        raise SystemExit("no trades")
    trades["period"] = "outside"
    for name, (start, end) in SPLIT_WINDOWS.items():
        m = (trades.signal_t >= start) & (trades.signal_t < end)
        trades.loc[m, "period"] = name
    trades["date"] = pd.to_datetime(trades.entry_t, unit="ms", utc=True).dt.strftime("%Y-%m")
    trades.to_csv(os.path.join(args.out, "trades.csv"), index=False)

    counts = {(x["pool"], x["profile"], x["slip_bps"]): x for x in signal_counts}
    rows = []
    for (pool, variant), group in trades.groupby(["pool", "variant"]):
        # variant includes the slippage suffix; recover the profile name for
        # joining the signal/confirmation denominator.
        profile_name = str(variant).rsplit("_", 1)[0]
        slip = float(str(variant).rsplit("_", 1)[1].replace("bps", ""))
        base = counts[(pool, profile_name, slip)]
        for period in ("full", "early", "middle", "late"):
            if period == "full":
                s = period_stats(group)
            else:
                start, end = SPLIT_WINDOWS[period]
                s = period_stats(group, start, end)
            row = {"pool": pool, "profile": profile_name, "slip_bps": slip,
                   "events": base["events"], "confirmations": base["confirmations"],
                   "filled": base["filled"],
                   "confirmation_rate": base["confirmation_rate"],
                   "period": period}
            row.update(s)
            rows.append(row)
    summary = pd.DataFrame(rows)
    summary.to_csv(os.path.join(args.out, "summary.csv"), index=False)
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump({"windows": SPLIT_WINDOWS, "profiles": PROFILES,
                   "rows": rows}, f, ensure_ascii=False, indent=2)

    show = summary[(summary.period.isin(["early", "middle", "late"])) &
                   (summary.slip_bps == 0)].copy()
    print(show[["pool", "profile", "period", "trades", "expect_R",
                "win_rate", "max_drawdown_R", "confirmation_rate"]]
          .sort_values(["pool", "profile", "period"])
          .to_string(index=False))
    print(f"saved -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
