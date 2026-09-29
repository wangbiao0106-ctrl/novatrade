#!/usr/bin/env python3
"""网格搜索驱动：检测参数 × 过滤器 × 缓冲 × 止盈倍数，输出汇总 CSV

用法: python run_grid.py --tf 1h --config cfg_base.json [--min-qv 0] [--out res.csv] [--max-syms N]
"""
import argparse, json, os, time
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
        a[p + "pnl_R"] = 0.0
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
        a[p + "pnl_R"] += np.divide(p_, np.maximum(r_, 1e-12)).sum()
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
    row[p + "expect_R_mean"] = a[p + "pnl_R"] / a[p + "n"]
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
            E.btc_gate_flags(ev, btc)
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
    ap.add_argument("--out", default="strategies/sweep_reversal_short/results/grid_result.csv")
    ap.add_argument("--min-qv", type=float, default=0.0)
    ap.add_argument("--max-syms", type=int, default=0)
    ap.add_argument("--pool", default="all",
                    choices=["all", "lowmid", "lowmid_high", "lowmid_mid", "lowmid_low",
                             "top50", "bottom148"],
                    help="历史研究池：全部山寨币、推荐中低流动性三层、静态Top50或后148")
    ap.add_argument("--max-hold", type=int, default=96,
                    help="最大持仓根数；上线规则是 96 根 1h，默认值必须与规则一致")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--sl-mode", default="ext", choices=["ext", "level", "h2", "atr"])
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    cfg = json.load(open(args.config))
    detect_cfgs = cfg["detect"]      # list of dict(L,R,sweep_wait,reject_wait)
    filter_cfgs = cfg["filters"]     # list of filter dicts
    bufs = cfg["bufs"]
    tp_mults = cfg["tp_mults"]

    meta = E.load_meta()
    syms = set(meta["sym"])
    data = E.load_tf(args.tf, syms=syms, min_qv=args.min_qv)
    btc_data = data.get("BTC")
    universe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "universe.json")
    uni = json.load(open(universe_path))
    alts = set(uni["altcoins"])
    m = meta[meta["sym"].isin(alts)].copy()
    days = ((m["t1"] - m["t0"]) / 86_400_000).clip(lower=1)
    ordered = m.assign(qv_daily=m["tot_qv"] / days).sort_values("qv_daily", ascending=False)["sym"].tolist()
    if args.pool == "all":
        data = {s: d for s, d in data.items() if s in alts}
    if args.pool != "all":
        if args.pool.startswith("lowmid"):
            rec = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "universe_recommended.json")))
            rec_ordered = [s for s in ordered if s in set(rec["recommended_low_mid"])]
            if args.pool == "lowmid":
                allowed = set(rec_ordered)
            else:
                third = len(rec_ordered) // 3
                chunks = {
                    "lowmid_high": rec_ordered[:third],
                    "lowmid_mid": rec_ordered[third:2 * third],
                    "lowmid_low": rec_ordered[2 * third:],
                }
                allowed = set(chunks[args.pool])
        else:
            allowed = set(ordered[:50] if args.pool == "top50" else ordered[len(ordered) // 2:])
        data = {s: d for s, d in data.items() if s in allowed}
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
        bd = btc_data
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
