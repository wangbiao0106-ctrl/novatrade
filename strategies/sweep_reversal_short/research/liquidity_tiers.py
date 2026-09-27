#!/usr/bin/env python3
"""流动性分层稳健性验证：
1) 静态分层：按全期总成交额把山寨币池分为 Top50/Top100/Top200/全部 + 后50%
2) 因果口径：每笔交易入场时点的"过去30天日均成交额"全市场排名 → 验证 Top100 规则
"""
import json, os
import numpy as np
import pandas as pd
import engine as E

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)
CFG = {"L": 10, "R": 5, "sweep_wait": 96, "reject_wait": 5,
       "retest_entry": 1, "retest_wait": 12, "retest_mode": "resweep",
       "major_wins": (288,), "mom_wins": (96,), "sma_lens": (200,),
       "eqh_wins": (96,)}
FILT = {"major_win": 288, "rs_lower_ext": 1, "rs_deep": 0.2,
        "btc_down": 1, "rsi_s_min": 62, "vol_mult": 1.5}
BUF, TP, MAX_HOLD = 0.5, 2.2, 96


def run_backtest(syms, fee):
    data = E.load_tf("1h")
    uni = json.load(open(os.path.join(CONFIG, "universe.json")))
    alts = set(uni["altcoins"]) & set(syms)
    bd = data["BTC"]
    bs = E.sma(bd["c"], 200)
    trades = []
    for sym in sorted(alts):
        d = data[sym]
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
        res = E.simulate(ev, d, m, [BUF], [TP], MAX_HOLD, fee,
                         sl_mode="ext")[0]
        for i in range(len(res["entry"])):
            trades.append(dict(sym=sym, entry_t=res["entry_t"][i],
                               entry=res["entry"][i], risk=res["risk"][i],
                               pnl=res["outcomes"][i],
                               pnl_R=res["outcomes"][i] / res["risk"][i],
                               kind=int(res["kinds"][i])))
    return pd.DataFrame(trades)


def stats(df):
    if len(df) == 0:
        return dict(n=0, win_rate=np.nan, ratio=np.nan, expect=np.nan)
    w = df[df.pnl > 0]
    l = df[df.pnl <= 0]
    return dict(
        n=len(df), win_rate=float((df.pnl > 0).mean()),
        ratio=float(w.pnl_R.mean() / (-l.pnl_R.mean())) if len(l) else np.nan,
        expect=float(df.pnl_R.mean()),
        avg_win=float(w.pnl_R.mean()) if len(w) else np.nan,
        avg_loss=float(-l.pnl_R.mean()) if len(l) else np.nan)


def main():
    os.makedirs(OUT, exist_ok=True)
    meta = E.load_meta().set_index("sym")
    uni = json.load(open(os.path.join(CONFIG, "universe.json")))
    alts = sorted(uni["altcoins"])
    # 全期日均成交额（USDT）
    days = (meta.t1 - meta.t0) / 86_400_000
    meta["qv_daily"] = meta.tot_qv / days.clip(lower=1)
    m = meta.loc[alts].sort_values("qv_daily", ascending=False)
    tiers = {
        "Top50": m.index[:50],
        "Top100": m.index[:100],
        "Top200": m.index[:200],
        "后50%": m.index[len(m) // 2:],
        "全部295": m.index,
    }
    print("== 静态分层（按全期日均成交额）==")
    print(f"{'层级':<10} {'合约数':>6} {'笔数':>6} {'胜率':>7} {'盈亏比R':>8} "
          f"{'期望R':>8} | {'训练胜率':>8} {'测试胜率':>8} {'测试期望R':>9}")
    rows = []
    for name, syms in tiers.items():
        df = run_backtest(syms, 0.0005)
        if len(df) == 0:
            print(f"{name:<10} {len(syms):6d} 无交易")
            continue
        s = stats(df)
        tr = stats(df[df.entry_t < SPLIT_TS])
        te = stats(df[df.entry_t >= SPLIT_TS])
        print(f"{name:<10} {len(syms):6d} {s['n']:6d} {s['win_rate']*100:6.1f}% "
              f"{s['ratio']:8.2f} {s['expect']:+8.2f} | {tr['win_rate']*100:7.1f}% "
              f"{te['win_rate']*100:7.1f}% {te['expect']:+9.2f}")
        rows.append(dict(tier=name, n_syms=len(syms), **s,
                         tr_win=tr["win_rate"], te_win=te["win_rate"],
                         te_expect=te["expect"]))
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "tier_static.csv"), index=False)

    # 因果口径：入场时点过去30天日均成交额的实时排名
    print("\n== 因果口径：入场时点·过去30天日均成交额排名 ==")
    data = E.load_tf("1h")
    qv_series = {}
    for sym in alts:
        d = data[sym]
        t = pd.to_datetime(d["t"], unit="ms")
        s = pd.Series(d["qv"], index=t)
        daily = s.resample("1D").sum()
        qv_series[sym] = daily
    df_all = run_backtest(alts, 0.0005)
    ranks = []
    for _, row in df_all.iterrows():
        et = pd.to_datetime(row.entry_t, unit="ms")
        vals = {}
        for sym, daily in qv_series.items():
            past = daily[daily.index <= et].tail(30)
            vals[sym] = past.median() if len(past) else 0.0
        # 全市场排名（含未交易合约）
        ranked = sorted(vals.values(), reverse=True)
        v = vals.get(row.sym, 0.0)
        rank = int(np.searchsorted(-np.array(ranked), -v)) + 1  # 1-based
        ranks.append(rank)
    df_all["rank_at_entry"] = ranks
    df_all.to_csv(os.path.join(OUT, "final_trades_ranked.csv"), index=False)
    for cutoff, label in [(50, "Top50"), (100, "Top100"), (200, "Top200")]:
        g = df_all[df_all.rank_at_entry <= cutoff]
        s = stats(g)
        tr = stats(g[g.entry_t < SPLIT_TS])
        te = stats(g[g.entry_t >= SPLIT_TS])
        print(f"{label}@入场: 笔数={s['n']:4d} 胜率={s['win_rate']*100:5.1f}% "
              f"盈亏比R={s['ratio']:4.2f} 期望={s['expect']:+.2f}R | "
              f"训练 {tr['win_rate']*100:4.1f}% / 测试 {te['win_rate']*100:4.1f}% "
              f"期望 {te['expect']:+.2f}R")
    # 费用压力：Top100@入场 × 0.0008
    print("\n== 费用压力测试（Top100@入场）==")
    for fee in (0.0005, 0.0006, 0.0008):
        g = df_all[df_all.rank_at_entry <= 100].copy()
        if fee != 0.0005:
            # 重新模拟太贵，改用近似：手续费增量 = fee*2*(entry) 对 pnl 的修正
            df2 = run_backtest(alts, fee)
            df2["rank_at_entry"] = df_all["rank_at_entry"]
            g = df2[df2.rank_at_entry <= 100]
        s = stats(g)
        print(f"fee={fee}: 笔数={s['n']} 胜率={s['win_rate']*100:.1f}% "
              f"盈亏比R={s['ratio']:.2f} 期望={s['expect']:+.2f}R")


if __name__ == "__main__":
    main()
