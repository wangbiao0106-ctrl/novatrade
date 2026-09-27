#!/usr/bin/env python3
"""最终交付：推荐配置（门控 2.2R）完整回测 + 交易记录导出"""
import json, os
import numpy as np
import pandas as pd
import engine as E

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)

# 推荐配置：BTC 熊市门控 + 二次扫顶 + 2.2R
CFG = {"L": 10, "R": 5, "sweep_wait": 96, "reject_wait": 5,
       "retest_entry": 1, "retest_wait": 12, "retest_mode": "resweep",
       "major_wins": (288,), "mom_wins": (96,), "sma_lens": (200,),
       "eqh_wins": (96,)}
FILT = {"major_win": 288, "rs_lower_ext": 1, "rs_deep": 0.2,
        "btc_down": 1, "rsi_s_min": 62, "vol_mult": 1.5}
BUF, TP, MAX_HOLD, FEE = 0.5, 2.2, 96, 0.0005


def main(pool="lowmid"):
    os.makedirs(OUT, exist_ok=True)
    meta = E.load_meta()
    data = E.load_tf("1h")
    # 标的池：lowmid = 中低流动性山寨币177个（推荐池，alpha所在）；all = 全部295山寨币
    uni = json.load(open(os.path.join(CONFIG, "universe.json")))
    if pool == "all":
        altcoins = set(uni["altcoins"])
    else:
        rec = json.load(open(os.path.join(CONFIG, "universe_recommended.json")))
        altcoins = set(rec["recommended_low_mid"])
    data = {s: d for s, d in data.items() if s in altcoins}
    bd = E.load_tf("1h", syms={"BTC"})["BTC"]
    bs = E.sma(bd["c"], 200)
    qv = meta.set_index("sym")["tot_qv"]
    trades = []
    for sym, d in data.items():
        pre = E.precompute(d, sma_lens=(200,), major_wins=(288,),
                           mom_wins=(96,), eqh_wins=(96,))
        F = E.next_higher_high(d["h"])
        ev = E.detect_events(d, pre, F, dict(CFG))
        if ev is None:
            continue
        idx = np.clip(np.searchsorted(bd["t"], ev["entry_t"], side="right") - 1,
                      0, len(bd["t"]) - 1)
        ev["btc_flag"] = (bd["c"][idx] > bs[idx]).astype(np.int8)
        m = E.apply_filters(ev, FILT)
        if m.sum() == 0:
            continue
        res = E.simulate(ev, d, m, [BUF], [TP], MAX_HOLD, FEE,
                         sl_mode="ext")[0]
        for i in range(len(res["entry"])):
            trades.append(dict(
                sym=sym, entry_t=res["entry_t"][i],
                entry=res["entry"][i], sl=res["entry"][i] + res["risk"][i],
                tp=res["entry"][i] - TP * res["risk"][i],
                risk=res["risk"][i], pnl=res["outcomes"][i],
                pnl_R=res["outcomes"][i] / res["risk"][i],
                kind=int(res["kinds"][i])))
    df = pd.DataFrame(trades)
    df["split"] = np.where(df.entry_t < SPLIT_TS, "训练段", "测试段")
    df["date"] = pd.to_datetime(df.entry_t, unit="ms").dt.strftime("%Y-%m")
    df["win"] = (df.pnl > 0).astype(int)
    df.to_csv(os.path.join(OUT, "final_trades.csv"), index=False)

    def stats(sub):
        if len(sub) == 0:
            return dict(n=0)
        w = sub[sub.win == 1]
        l = sub[sub.win == 0]
        return dict(
            n=len(sub), win_rate=float(sub.win.mean()),
            avg_win_R=float((w.pnl_R).mean()),
            avg_loss_R=float((-l.pnl_R).mean()),
            ratio_R=float(w.pnl_R.mean() / (-l.pnl_R.mean())) if len(l) else None,
            ratio_price=float(w.pnl.mean() / (-l.pnl.mean())) if len(l) else None,
            expect_R=float(sub.pnl_R.mean()),
            tp_hit=float((sub.kind == 2).mean()),
            sl_hit=float((sub.kind == 1).mean()),
            time=float((sub.kind == 3).mean()))

    report = dict(params=dict(tf="1h", detect=CFG, filters=FILT, buf_atr=BUF,
                              tp_mult=TP, max_hold=MAX_HOLD, fee_per_side=FEE,
                              universe=("中低流动性山寨币177个(推荐池)" if pool == "lowmid"
                                        else "全部山寨币295个")))
    report["full"] = stats(df)
    report["train"] = stats(df[df.split == "训练段"])
    report["test"] = stats(df[df.split == "测试段"])
    report["by_month"] = {mo: stats(g) for mo, g in df.groupby("date")}
    with open(os.path.join(OUT, "final_report.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1, default=str)
    df2 = df.sort_values("entry_t").reset_index(drop=True)
    pd.DataFrame({"entry_t": df2.entry_t.values,
                  "eq_R": np.cumsum(df2.pnl_R.values)}).to_csv(
        os.path.join(OUT, "final_equity.csv"), index=False)
    print("推荐配置回测完成：")
    for tag in ("full", "train", "test"):
        s = report[tag]
        print(f"  {tag}: n={s['n']} 成功率={s['win_rate']:.1%} "
              f"盈亏比R={s['ratio_R']:.2f} 平均盈={s['avg_win_R']:.2f}R "
              f"平均亏={s['avg_loss_R']:.2f}R 期望={s['expect_R']:+.2f}R "
              f"TP%={s['tp_hit']:.0%} SL%={s['sl_hit']:.0%} 时间%={s['time']:.0%}")
    print("文件: results/final_trades.csv / final_equity.csv / final_report.json")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="lowmid", choices=["lowmid", "all"])
    args = ap.parse_args()
    main(args.pool)
