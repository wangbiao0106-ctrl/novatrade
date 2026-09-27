#!/usr/bin/env python3
"""网格搜索驱动：检测参数 × 过滤器 × 缓冲 × 止盈倍数，输出汇总 CSV

用法: python run_grid.py --tf 1h --config cfg_base.json [--min-qv 0] [--out res.csv] [--max-syms N]
"""
import argparse, json, os, sys, time
import numpy as np
import pandas as pd
import engine as E

SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)  # 训练/测试切分

def agg_init():
    a = {}
    for tag in ("", "tr", "te"):
        p = tag + "_" if tag else ""
        a[p + "n"] = 0; a[p + "win_n"] = 0.0; a[p + "loss_n"] = 0.0
        a[p + "pos"] = 0.0; a[p + "neg"] = 0.0; a[p + "pnl"] = 0.0
        a[p + "risk"] = 0.0
        a[p + "g_pos"] = 0.0; a[p + "g_neg"] = 0.0
        a[p + "k_tp"] = 0; a[p + "k_sl"] = 0; a[p + "k_time"] = 0
    return a

def agg_add(a, pnl, risk, kinds, entry_t, pnl_g=None):
    if pnl_g is None:
        pnl_g = pnl
    mtr = entry_t < SPLIT_TS
    for tag, m in (("", np.ones(len(pnl), dtype=bool)), ("tr", mtr), ("te", ~mtr)):
        p = tag + "_" if tag else ""
        p_, r_, k_, g_ = pnl[m], risk[m], kinds[m], pnl_g[m]
        w = p_ > 0
        wg = g_ > 0
        a[p + "n"] += len(p_); a[p + "win_n"] += float(w.sum())
        a[p + "loss_n"] += float((~w).sum())
        a[p + "pos"] += p_[w].sum(); a[p + "neg"] += (-p_[~w]).sum()
        a[p + "pnl"] += p_.sum(); a[p + "risk"] += r_.sum()
        a[p + "g_pos"] += g_[wg].sum(); a[p + "g_neg"] += (-g_[~wg]).sum()
        a[p + "k_tp"] += int((k_ == 2).sum()); a[p + "k_sl"] += int((k_ == 1).sum())
        a[p + "k_time"] += int((k_ == 3).sum())

def agg_row(a, prefix=""):
    p = prefix + "_" if prefix else ""
    row = {}
    if a[p + "n"] == 0:
        return row
    row[p + "trades"] = a[p + "n"]
    row[p + "win_rate"] = a[p + "win_n"] / a[p + "n"]
    avg_win = a[p + "pos"] / a[p + "win_n"] if a[p + "win_n"] > 0 else np.nan
    avg_loss = a[p + "neg"] / a[p + "loss_n"] if a[p + "loss_n"] > 0 else np.nan
    row[p + "avg_win"] = avg_win
    row[p + "avg_loss"] = avg_loss
    row[p + "ratio"] = avg_win / avg_loss if (avg_loss and avg_loss > 0) else np.nan
    # 毛利口径（不含手续费）的盈亏比
    gw = a[p + "g_pos"] / a[p + "win_n"] if a[p + "win_n"] > 0 else np.nan
    gl = a[p + "g_neg"] / a[p + "loss_n"] if a[p + "loss_n"] > 0 else np.nan
    row[p + "ratio_gross"] = gw / gl if (gl and gl > 0) else np.nan
    row[p + "pf"] = a[p + "pos"] / a[p + "neg"] if a[p + "neg"] > 0 else np.nan
    row[p + "expect"] = a[p + "pnl"] / a[p + "n"]
    row[p + "expect_R"] = a[p + "pnl"] / a[p + "risk"] if a[p + "risk"] > 0 else np.nan
    row[p + "tp_pct"] = a[p + "k_tp"] / a[p + "n"]
    row[p + "sl_pct"] = a[p + "k_sl"] / a[p + "n"]
    row[p + "time_pct"] = a[p + "k_time"] / a[p + "n"]
    return row

def run_symbol(d, pre, F, detect_cfgs, filter_cfgs, bufs, tp_mults, max_hold,
               fee, sl_mode, accum, btc=None):
    """对一个合约跑全部组合，累加进 accum"""
    for di, cfg in enumerate(detect_cfgs):
        ev = E.detect_events(d, pre, F, cfg)
        if ev is None:
            continue
        if btc is not None:
            bt, bc, bs = btc
            idx = np.searchsorted(bt, ev["entry_t"], side="right") - 1
            idx = np.clip(idx, 0, len(bt) - 1)
            ev["btc_flag"] = (bc[idx] > bs[idx]).astype(np.int8)
        for fi, f in enumerate(filter_cfgs):
            mask = E.apply_filters(ev, f)
            if mask.sum() == 0:
                continue
            key = (di, fi)
            res_list = E.simulate(ev, d, mask, bufs, tp_mults, max_hold, fee,
                                  sl_mode=sl_mode)
            for res in res_list:
                bk = key + (res["buf"], res["tp"])
                agg_add(accum[bk], res["outcomes"], res["risk"], res["kinds"],
                        res["entry_t"], res["outcomes_g"])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", default="grid_result.csv")
    ap.add_argument("--min-qv", type=float, default=0.0)
    ap.add_argument("--max-syms", type=int, default=0)
    ap.add_argument("--max-hold", type=int, default=48)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--sl-mode", default="ext", choices=["ext", "level", "h2", "atr"])
    args = ap.parse_args()

    cfg = json.load(open(args.config))
    detect_cfgs = cfg["detect"]      # list of dict(L,R,sweep_wait,reject_wait)
    filter_cfgs = cfg["filters"]     # list of filter dicts
    bufs = cfg["bufs"]
    tp_mults = cfg["tp_mults"]

    meta = E.load_meta()
    syms = set(meta["sym"])
    data = E.load_tf(args.tf, syms=syms, min_qv=args.min_qv)
    syms = sorted(data.keys())
    if args.max_syms:
        syms = syms[: args.max_syms]
    print(f"tf={args.tf} symbols={len(syms)} detect={len(detect_cfgs)} "
          f"filters={len(filter_cfgs)} bufs={bufs} tps={tp_mults}", flush=True)

    # 特征窗口集合（全局固定，precompute 一次）
    major_wins = sorted(({f.get("major_win", 0) for f in filter_cfgs} |
                         {c.get("major_win", 0) for c in detect_cfgs}) - {0})
    mom_wins = sorted(({f.get("mom_win", 0) for f in filter_cfgs}) - {0})
    sma_lens = sorted(({f.get("sma_len", 0) for f in filter_cfgs} |
                       {f.get("sma_dist_len", 0) for f in filter_cfgs}) - {0})
    eqh_wins = sorted(({f.get("eqh_w", 0) for f in filter_cfgs}) - {0})
    for c in detect_cfgs:
        c["major_wins"] = tuple(major_wins); c["mom_wins"] = tuple(mom_wins)
        c["sma_lens"] = tuple(sma_lens); c["eqh_wins"] = tuple(eqh_wins)

    accum = {}
    for di in range(len(detect_cfgs)):
        for fi in range(len(filter_cfgs)):
            for b in bufs:
                for t in tp_mults:
                    accum[(di, fi, b, t)] = agg_init()

    t0 = time.time()
    # BTC 大盘 regime（可选）
    btc = None
    if any(f.get("btc_up", 0) or f.get("btc_down", 0) for f in filter_cfgs):
        bd = data.get("BTC")
        if bd is not None:
            bs = E.sma(bd["c"], 200)
            btc = (bd["t"], bd["c"], bs)
    for i, sym in enumerate(syms):
        d = data[sym]
        pre = E.precompute(d, sma_lens=sma_lens, major_wins=major_wins,
                           mom_wins=mom_wins)
        F = E.next_higher_high(d["h"])
        run_symbol(d, pre, F, detect_cfgs, filter_cfgs, bufs, tp_mults,
                   args.max_hold, args.fee, args.sl_mode, accum, btc)
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(syms)} syms, {time.time()-t0:.0f}s", flush=True)

    # 汇总输出
    rows = []
    for di, c in enumerate(detect_cfgs):
        for fi, f in enumerate(filter_cfgs):
            for b in bufs:
                for t in tp_mults:
                    a = accum[(di, fi, b, t)]
                    row = dict(di=di, fi=fi, buf=b, tp=t, **c, **f)
                    for kk in ("major_wins", "mom_wins", "sma_lens", "eqh_wins"):
                        row.pop(kk, None)
                    row.update(agg_row(a, ""))
                    row.update(agg_row(a, "tr"))
                    row.update(agg_row(a, "te"))
                    rows.append(row)
    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False)
    print(f"rows={len(df)} saved -> {args.out}", flush=True)

    # 打印训练集最优 top10（按 win_rate≥0.5 与 ratio≥2 联合目标）
    cols = [c for c in df.columns if not c.startswith(("tr_", "te_"))]
    sub = df.dropna(subset=["tr_trades"]).copy()
    sub["ok_tr"] = (sub["tr_win_rate"] >= 0.50) & (sub["tr_ratio"] >= 2.0)
    sub["score"] = (sub["tr_win_rate"] - 0.5) * 100 + (sub["tr_ratio"] - 2.0)
    print("\n== 训练集达标组合 (win>=50%, ratio>=2), 按 tr win_rate 排序 top10 ==")
    ok = sub[sub["ok_tr"]].sort_values(["tr_win_rate", "tr_trades"],
                                       ascending=False)
    show = [c for c in ["buf", "tp", "L", "R", "reject_wait", "major_win",
                        "new_high", "sma_len", "sma_dist", "mom_win", "vol_mult",
                        "wick_min", "rj_min"] if c in ok.columns]
    show += [c for c in ["tr_trades", "tr_win_rate", "tr_ratio", "tr_expect_R",
                         "te_trades", "te_win_rate", "te_ratio", "te_expect_R"]
             if c in ok.columns]
    print(ok[show].head(10).to_string(index=False))
    print(f"\n达标组合数: {int(sub['ok_tr'].sum())} / {len(sub)}", flush=True)

if __name__ == "__main__":
    main()
