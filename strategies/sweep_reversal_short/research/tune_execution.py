#!/usr/bin/env python3
"""Execution and timeframe experiments for sweep_reversal_short.

This script keeps the existing event detector as the reference implementation
and varies only the research questions that are easy to accidentally mix:

* pool: recommended low/mid-liquidity snapshot, all altcoins, static Top50;
* trigger: 1h event close, or the first bearish 15m close after a 1h event;
* order: market fill, or a sell limit that must be touched before expiry.

The ``scaled_15m`` experiment is a separate strategy: all bar-count windows
are multiplied by four and the event detector runs on 15m candles. It is not
presented as an execution tweak to the 1h strategy.

All fills and exits are conservative OHLC simulations. A limit order that is
not touched is a non-trade; a same-bar limit touch and stop touch is recorded
as an immediate stop. Results are research evidence, not a production order
implementation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

import engine as E


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CONFIG = os.path.join(ROOT, "config")
RESULTS = os.path.join(ROOT, "results")
SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)
HOUR_MS = 3_600_000
MINUTE15_MS = 900_000

# 检测/过滤/成本参数全部来自实验室机器真源 config/strategy.json：
# 研究对照不得各写一套参数，否则改了规则而证据不跟着变。
BASE_CFG, BASE_FILTER, _COSTS = E.lab_parameters()
BUF_ATR = _COSTS["buf_atr"]
TP_R = _COSTS["tp_mult"]
FEE = _COSTS["fee"]


@dataclass
class Trade:
    sym: str
    pool: str
    variant: str
    signal_t: int
    entry_t: int
    entry: float
    sl: float
    tp: float
    risk: float
    pnl: float
    pnl_R: float
    kind: str
    hold_bars: int
    limit_offset_atr: float = np.nan


def scaled_cfg(mult: int) -> tuple[dict, dict, int]:
    cfg = dict(BASE_CFG)
    cfg.update({
        "L": BASE_CFG["L"] * mult,
        "R": BASE_CFG["R"] * mult,
        "sweep_wait": BASE_CFG["sweep_wait"] * mult,
        "reject_wait": BASE_CFG["reject_wait"] * mult,
        "retest_wait": BASE_CFG["retest_wait"] * mult,
        "major_wins": (BASE_CFG["major_wins"][0] * mult,),
        "mom_wins": (BASE_CFG["mom_wins"][0] * mult,),
        "eqh_wins": (BASE_CFG["eqh_wins"][0] * mult,),
    })
    filt = dict(BASE_FILTER)
    filt["major_win"] = BASE_FILTER["major_win"] * mult
    return cfg, filt, 96 * mult


def btc_flags(events, btc_1h):
    """Map the BTC gate to event times using the shared fail-closed rule.

    委托给 `engine.btc_gate_flags`：信号时刻之前不足 200 根 1h K 线时必须判定为
    不通过，不能用 `min_periods=1` 的均线给出结论。
    """
    if events is None or btc_1h is None:
        return
    E.btc_gate_flags(events, btc_1h)


def exit_trade(d, entry_i: int, entry: float, sl: float, tp: float,
               max_hold: int, fee: float) -> tuple[float, float, str, int]:
    """Return pnl, pnl_R, exit kind and bars held using engine's OHLC rules."""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    stop_i = entry_i + 1
    end = min(stop_i + max_hold, len(c))
    if stop_i >= end:
        px, kind, hold = c[entry_i], "time", 0
    else:
        op, hi, lo = o[stop_i:end], h[stop_i:end], l[stop_i:end]
        inf = 1 << 30
        a = stop_i + np.argmax(op >= sl) if (op >= sl).any() else inf
        b = stop_i + np.argmax(op <= tp) if (op <= tp).any() else inf
        x = stop_i + np.argmax(hi >= sl) if (hi >= sl).any() else inf
        y = stop_i + np.argmax(lo <= tp) if (lo <= tp).any() else inf
        first_open = min(a, b)
        first_wick = min(x, y)
        if first_open == inf and first_wick == inf:
            exit_i, px, kind = end - 1, c[end - 1], "time"
        elif first_open <= first_wick:
            exit_i = first_open
            if a < b:
                px, kind = op[a - stop_i], "sl"
            else:
                px, kind = op[b - stop_i], "tp"
        else:
            exit_i = first_wick
            if x < y:
                px, kind = sl, "sl"
            elif y < x:
                px, kind = tp, "tp"
            else:
                px, kind = sl, "sl"
        hold = int(exit_i - entry_i)
    pnl = entry - px - fee * (entry + px)
    risk = max(sl - entry, 1e-12)
    return float(pnl), float(pnl / risk), kind, hold


def market_trade(sym, pool, variant, d, event, entry_i, base_entry,
                 signal_t, max_hold, slip_bps=0.0, buf_atr=BUF_ATR,
                 tp_r=TP_R, fee=FEE) -> Trade | None:
    if entry_i < 0 or entry_i >= len(d["c"]):
        return None
    # This strategy is short: adverse market slippage lowers the sell fill.
    entry = float(base_entry) * (1.0 - slip_bps / 10_000.0)
    ext = float(event["ext"])
    atr = float(event["atr_k"])
    sl = max(ext + buf_atr * atr, entry + 1e-9)
    tp = entry - tp_r * (sl - entry)
    pnl, pnl_R, kind, hold = exit_trade(d, entry_i, entry, sl, tp, max_hold, fee)
    return Trade(sym, pool, variant, int(signal_t), int(d["t"][entry_i]),
                 entry, sl, tp, sl - entry, pnl, pnl_R, kind, hold)


def limit_trade(sym, pool, variant, d, event, signal_i, base_entry,
                signal_t, max_hold, valid_bars, offset_atr) -> Trade | None:
    """Sell-limit entry above close; touch is required before expiry."""
    ext = float(event["ext"])
    atr = float(event["atr_k"])
    limit = float(base_entry) + offset_atr * atr
    sl_unfilled = ext + BUF_ATR * atr
    end = min(signal_i + 1 + valid_bars, len(d["c"]))
    fill_i = -1
    for i in range(signal_i + 1, end):
        hi = float(d["h"][i])
        if hi < limit:
            continue
        # If the same candle traverses the protective stop, OHLC cannot tell
        # whether the limit filled before the stop. We take the conservative
        # immediate-stop interpretation.
        fill_i = i
        if hi >= sl_unfilled:
            entry = limit
            sl = max(sl_unfilled, entry + 1e-9)
            pnl = entry - sl - FEE * (entry + sl)
            return Trade(sym, pool, variant, int(signal_t), int(d["t"][i]),
                         entry, sl, entry - TP_R * (sl - entry), sl - entry,
                         float(pnl), float(pnl / (sl - entry)), "sl", 0,
                         offset_atr)
        break
    if fill_i < 0:
        return None
    entry = limit
    sl = max(sl_unfilled, entry + 1e-9)
    tp = entry - TP_R * (sl - entry)
    pnl, pnl_R, kind, hold = exit_trade(d, fill_i, entry, sl, tp, max_hold, FEE)
    return Trade(sym, pool, variant, int(signal_t), int(d["t"][fill_i]),
                 entry, sl, tp, sl - entry, pnl, pnl_R, kind, hold,
                 offset_atr)


def stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"trades": 0}
    win = df.pnl > 0
    wins, losses = df[win], df[~win]
    ordered = df.sort_values("entry_t")
    eq = ordered.pnl_R.cumsum().to_numpy()
    dd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    max_loss = run = 0
    for x in ordered.pnl_R:
        run = run + 1 if x <= 0 else 0
        max_loss = max(max_loss, run)
    return {
        "trades": int(len(df)),
        "win_rate": float(win.mean()),
        "ratio_R": float(wins.pnl_R.mean() / (-losses.pnl_R.mean())) if len(losses) else None,
        "expect_R": float(df.pnl_R.mean()),
        "avg_win_R": float(wins.pnl_R.mean()) if len(wins) else None,
        "avg_loss_R": float((-losses.pnl_R).mean()) if len(losses) else None,
        "tp_pct": float((df.kind == "tp").mean()),
        "sl_pct": float((df.kind == "sl").mean()),
        "time_pct": float((df.kind == "time").mean()),
        "max_drawdown_R": dd,
        "max_consecutive_loss": int(max_loss),
        "median_hold_bars": float(df.hold_bars.median()),
    }


def pool_sets(meta):
    uni = json.load(open(os.path.join(CONFIG, "universe.json")))
    rec = json.load(open(os.path.join(CONFIG, "universe_recommended.json")))
    alts = [s for s in uni["altcoins"] if s in set(meta.sym)]
    m = meta.set_index("sym").loc[alts].copy()
    days = ((m.t1 - m.t0) / 86_400_000).clip(lower=1)
    m["qv_daily"] = m.tot_qv / days
    m = m.sort_values("qv_daily", ascending=False)
    rec_ordered = [s for s in m.index.tolist() if s in set(rec["recommended_low_mid"])]
    third = len(rec_ordered) // 3
    return {
        "lowmid177": set(rec["recommended_low_mid"]) & set(alts),
        "lowmid_high59": set(rec_ordered[:third]),
        "lowmid_mid59": set(rec_ordered[third:2 * third]),
        "lowmid_low59": set(rec_ordered[2 * third:]),
        "all295": set(alts),
        "top50": set(m.index[:50]),
        "bottom148": set(m.index[len(m) // 2:]),
    }


def prepare_events(data, symbols, cfg, filt, btc):
    rows = []
    # The tuple list keeps source arrays alive without rebuilding features for
    # every execution variant.
    for sym in sorted(symbols):
        d = data.get(sym)
        if d is None:
            continue
        wins = tuple(set(cfg.get("major_wins", ())) | {filt.get("major_win", 0)})
        moms = tuple(set(cfg.get("mom_wins", ())))
        smas = tuple(set(cfg.get("sma_lens", ())))
        eqhs = tuple(set(cfg.get("eqh_wins", ())))
        pre = E.precompute(d, sma_lens=smas, major_wins=wins,
                           mom_wins=moms, eqh_wins=eqhs)
        ev = E.detect_events(d, pre, E.next_higher_high(d["h"]), dict(cfg))
        if ev is None:
            continue
        btc_flags(ev, btc)
        mask = E.apply_filters(ev, filt)
        for i in np.flatnonzero(mask):
            rows.append((sym, d, ev, int(i)))
    return rows


def run_h1_variant(data, pool_name, symbols, btc, variant, slip_bps=0.0,
                   limit_offset=0.0, limit_valid=4):
    rows = []
    events = prepare_events(data, symbols, dict(BASE_CFG), dict(BASE_FILTER), btc)
    for sym, d, ev, i in events:
        k = int(ev["k"][i])
        if variant == "h1_market":
            t = market_trade(sym, pool_name, variant, d,
                             {key: ev[key][i] for key in ("ext", "atr_k")},
                             k, ev["entry"][i], d["t"][k], 96, slip_bps)
        else:
            t = limit_trade(sym, pool_name, variant, d,
                            {key: ev[key][i] for key in ("ext", "atr_k")},
                            k, ev["entry"][i], d["t"][k], 96,
                            limit_valid, limit_offset)
        if t:
            rows.append(t)
    return rows, len(events)


def run_h1_to_m15(data_h1, data_15, pool_name, symbols, btc, variant,
                  limit_offset=0.0, limit_valid=4):
    rows = []
    confirmed = 0
    events = prepare_events(data_h1, symbols, dict(BASE_CFG), dict(BASE_FILTER), btc)
    for sym, d1, ev, i in events:
        k = int(ev["k"][i])
        event = {key: ev[key][i] for key in ("ext", "atr_k")}
        signal_t = int(d1["t"][k] + HOUR_MS)
        d15 = data_15.get(sym)
        if d15 is None:
            continue
        p0 = int(np.searchsorted(d15["t"], signal_t, side="left"))
        # First 15m close that confirms continuation below the 1h signal
        # close. Four bars are one hour; no future beyond the confirmation
        # window is consulted.
        p = -1
        for j in range(p0, min(p0 + 4, len(d15["c"]))):
            if d15["c"][j] <= ev["entry"][i] and d15["c"][j] < d15["o"][j]:
                p = j
                break
        if p < 0:
            continue
        confirmed += 1
        if variant == "h1_structure_m15_market":
            t = market_trade(sym, pool_name, variant, d15, event, p,
                             d15["c"][p], signal_t, 384, 0.0)
        else:
            t = limit_trade(sym, pool_name, variant, d15, event, p,
                            d15["c"][p], signal_t, 384, limit_valid,
                            limit_offset)
        if t:
            rows.append(t)
    return rows, confirmed


def run_scaled_15m(data, pool_name, symbols, btc):
    cfg, filt, max_hold = scaled_cfg(4)
    rows = []
    events = prepare_events(data, symbols, cfg, filt, btc)
    for sym, d, ev, i in events:
        k = int(ev["k"][i])
        event = {key: ev[key][i] for key in ("ext", "atr_k")}
        t = market_trade(sym, pool_name, "scaled_15m_market", d, event, k,
                         ev["entry"][i], d["t"][k], max_hold, 0.0)
        if t:
            rows.append(t)
    return rows, len(events)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(RESULTS, "execution_tuning"))
    ap.add_argument("--skip-scaled-15m", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    meta = E.load_meta()
    pools = pool_sets(meta)
    data1 = E.load_tf("1h")
    data15 = E.load_tf("15m")
    bd = data1.get("BTC")
    btc = (bd["t"], bd["c"], E.sma(bd["c"], 200)) if bd is not None else None

    all_trades = []
    signal_counts = {}
    for pool_name, symbols in pools.items():
        print(f"pool={pool_name} symbols={len(symbols)}", flush=True)
        for slip in (0.0, 0.001, 0.002):
            ts, n_signals = run_h1_variant(data1, pool_name, symbols, btc, "h1_market", slip_bps=slip * 10_000)
            signal_counts[(pool_name, f"h1_close_market_{int(slip * 10000)}bps")] = n_signals
            for t in ts:
                t.variant = f"h1_close_market_{int(slip * 10000)}bps"
            all_trades.extend(ts)
        for offset in (0.0, 0.2):
            ts, n_signals = run_h1_variant(data1, pool_name, symbols, btc,
                                            "h1_limit", limit_offset=offset, limit_valid=4)
            signal_counts[(pool_name, f"h1_close_limit_{offset:.1f}atr_4bars")] = n_signals
            for t in ts:
                t.variant = f"h1_close_limit_{offset:.1f}atr_4bars"
            all_trades.extend(ts)
        ts, n_signals = run_h1_to_m15(data1, data15, pool_name, symbols, btc,
                                      "h1_structure_m15_market")
        signal_counts[(pool_name, "h1_structure_m15_market")] = n_signals
        all_trades.extend(ts)
        ts, n_signals = run_h1_to_m15(data1, data15, pool_name, symbols, btc,
                                      "h1_structure_m15_limit", limit_offset=0.0,
                                      limit_valid=4)
        signal_counts[(pool_name, "h1_structure_m15_limit")] = n_signals
        all_trades.extend(ts)

    if not args.skip_scaled_15m:
        # The scaled experiment is expensive. Use the two broad pools so the
        # comparison remains useful and avoids pretending that Top50 is a
        # separate 15m strategy selection.
        cfg, filt, _ = scaled_cfg(4)
        btc15 = btc
        for pool_name in ("lowmid177", "lowmid_high59", "lowmid_mid59",
                          "lowmid_low59", "all295"):
            ts, n_signals = run_scaled_15m(data15, pool_name, pools[pool_name], btc15)
            signal_counts[(pool_name, "scaled_15m_market")] = n_signals
            all_trades.extend(ts)

    records = [t.__dict__ for t in all_trades]
    df = pd.DataFrame(records)
    if df.empty:
        raise SystemExit("no trades")
    df["split"] = np.where(df.signal_t < SPLIT_TS, "train", "test")
    df["date"] = pd.to_datetime(df.entry_t, unit="ms", utc=True).dt.strftime("%Y-%m")
    df.to_csv(os.path.join(args.out, "trades.csv"), index=False)

    summary = []
    for (pool, variant), g in df.groupby(["pool", "variant"], sort=True):
        n_signals = signal_counts.get((pool, variant), len(g))
        row = {"pool": pool, "variant": variant, "signals": int(n_signals),
               "filled": int(len(g)),
               "fill_rate": float(len(g) / n_signals) if n_signals else 0.0,
               "full": stats(g),
               "train": stats(g[g.split == "train"]),
               "test": stats(g[g.split == "test"])}
        summary.append(row)
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump({"assumptions": {
            "fee_per_side": FEE, "stop": "ext + 0.5 ATR", "tp": "2.2R",
            "split": "2026-07-29 UTC", "limit": "touch high, 4 bars, same-bar stop conservative",
            "m15_confirmation": "first bearish 15m close <= 1h signal close within 1h",
        }, "rows": summary}, f, ensure_ascii=False, indent=2)
    srows = []
    for r in summary:
        base = {"pool": r["pool"], "variant": r["variant"],
                "signals": r["signals"], "filled": r["filled"],
                "fill_rate": r["fill_rate"]}
        for part in ("full", "train", "test"):
            base.update({f"{part}_{k}": v for k, v in r[part].items()})
        srows.append(base)
    sdf = pd.DataFrame(srows).sort_values(["pool", "variant"])
    sdf.to_csv(os.path.join(args.out, "summary.csv"), index=False)
    print(sdf[["pool", "variant", "full_trades", "full_expect_R",
               "train_trades", "train_expect_R", "test_trades", "test_expect_R",
               "full_time_pct", "full_max_drawdown_R"]].to_string(index=False))
    print(f"saved -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
