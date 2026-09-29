#!/usr/bin/env python3
"""融合版回测：我的 resweep 信号 + HLSR 出场结构（三段止盈/分批/保本/移动止损）

对比基线：固定 2.2R + 96根时间离场（60笔/51.7%/1.69R/+0.37R）
"""
import json, os, sys
import numpy as np
import pandas as pd
import engine as E

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)
# 参数来自实验室机器真源 config/strategy.json。
CFG, FILT, _COSTS = E.lab_parameters()


def collect_events():
    """推荐池 + 门控过滤后的全部事件（与 final_report 一致）"""
    rec = json.load(open(os.path.join(CONFIG, "universe_recommended.json")))
    pool = set(rec["recommended_low_mid"])
    data = E.load_tf("1h")
    bd = data["BTC"]
    bs = E.sma(bd["c"], 200)
    events = []
    for sym in sorted(pool):
        d = data[sym]
        pre = E.precompute(d, sma_lens=(200,), major_wins=(288,),
                           mom_wins=(96,), eqh_wins=(96,))
        F = E.next_higher_high(d["h"])
        ev = E.detect_events(d, pre, F, dict(CFG))
        if ev is None:
            continue
        E.btc_gate_flags(ev, (bd["t"], bd["c"], bs))
        m = E.apply_filters(ev, FILT)
        if m.sum() == 0:
            continue
        events.append((sym, d, ev, m))
    return events


def fusion_exit(d, k, entry, ext, stop, risk, spec, max_hold, trail_bars, cost_ratio):
    """HLSR 风格出场管理。spec: (mode, targets_mult, fracs, be_after_tp1, trail_after_tp2)
    返回 (net_r, kind)"""
    h, l, c, o = d["h"], d["l"], d["c"], d["o"]
    n = len(c)
    mode, tmult, fracs, be1, trail2 = spec
    if mode == "structural":
        support = float(np.min(l[max(0, k - 8):k + 1]))
        recent_high = float(np.max(h[max(0, k - 80):k + 1]))
        major_low = float(np.min(l[max(0, k - 80):k + 1]))
        midpoint = (recent_high + major_low) / 2
        t1 = support if support < entry else entry - risk
        t2 = midpoint if midpoint < t1 else entry - 2 * risk
        t3 = major_low if major_low < t2 else entry - 3 * risk
    else:  # fixedR: targets = entry - risk*tmult[i]
        t1 = entry - risk * tmult[0]
        t2 = entry - risk * tmult[1] if len(tmult) > 1 else None
        t3 = entry - risk * tmult[2] if len(tmult) > 2 else None
    raw = [t for t in (t1, t2, t3) if t is not None and t < entry]
    targets = sorted({round(t, 12) for t in raw}, reverse=True)
    while len(targets) < len(fracs):
        targets.append(entry - risk * (len(targets) + 1))
    targets = targets[:len(fracs)]
    remaining, net_r = 1.0, 0.0
    hit = set()
    current_stop = stop
    kind = "time"
    j1 = min(k + max_hold, n)
    for j in range(k + 1, j1):
        if o[j] >= current_stop:
            net_r += (entry - o[j]) / risk * remaining
            kind = "sl"
            remaining = 0.0
            break
        if c[j] > ext or h[j] >= current_stop:
            net_r += (entry - current_stop) / risk * remaining
            kind = "inval"
            remaining = 0.0
            break
        for ti, (price, frac) in enumerate(zip(targets, fracs), 1):
            if ti not in hit and remaining + 1e-9 >= frac and l[j] <= price:
                net_r += (entry - price) / risk * frac
                remaining = max(0.0, remaining - frac)
                hit.add(ti)
                if ti == 1 and be1:
                    current_stop = min(current_stop, entry)
                if ti >= 2 and trail2:
                    recent = max(h[max(k + 1, j - trail_bars + 1):j + 1])
                    current_stop = min(current_stop, recent)
                if remaining <= 1e-9:
                    kind = "tp"
                    break
        if remaining <= 1e-9:
            break
    if remaining > 0:
        net_r += (entry - c[j1 - 1]) / risk * remaining
    net_r -= cost_ratio * entry / risk
    return net_r, kind


def run(spec, max_hold, trail_bars, buf_atr, cost_ratio, out_csv=None):
    recs = []
    for sym, d, ev, m in collect_events():
        k = ev["k"][m]
        entry = ev["entry"][m]
        ext = ev["ext"][m]
        atr_k = ev["atr_k"][m]
        et = ev["entry_t"][m]
        for i in range(len(k)):
            stop = ext[i] + buf_atr * atr_k[i]
            risk = stop - entry[i]
            if risk <= 0:
                continue
            net_r, kind = fusion_exit(d, int(k[i]), entry[i], ext[i], stop, risk,
                                      spec, max_hold, trail_bars, cost_ratio)
            recs.append(dict(sym=sym, entry_t=et[i], entry=entry[i], risk=risk,
                             net_r=net_r, kind=kind))
    df = pd.DataFrame(recs)
    if out_csv:
        df.to_csv(out_csv, index=False)
    return df


def stats(df):
    if len(df) == 0:
        return dict(n=0)
    w = df[df.net_r > 0]
    l = df[df.net_r <= 0]
    return dict(
        n=len(df), win_rate=float((df.net_r > 0).mean()),
        avg_win_R=float(w.net_r.mean()) if len(w) else np.nan,
        avg_loss_R=float(-l.net_r.mean()) if len(l) else np.nan,
        ratio_R=float(w.net_r.mean() / (-l.net_r.mean())) if len(l) else np.nan,
        expect_R=float(df.net_r.mean()))


def main():
    os.makedirs(OUT, exist_ok=True)
    print(f"{'spec':<28} {'持仓':>4} {'trail':>5} {'成本':>6} | "
          f"{'n':>4} {'胜率':>6} {'盈亏比':>6} {'期望':>7} | {'训练':>10} {'测试':>10}")
    rows = []
    specs = [
        ("struct 30/30/40 BE+trail", "structural", (1, 2, 3), (0.30, 0.30, 0.40), True, True),
        ("1/2/3R 30/30/40 BE+trail", "fixedR", (1, 2, 3), (0.30, 0.30, 0.40), True, True),
        ("1/2/3R 20/30/50 BE+trail", "fixedR", (1, 2, 3), (0.20, 0.30, 0.50), True, True),
        ("1/2/3R 20/20/60 BE+trail", "fixedR", (1, 2, 3), (0.20, 0.20, 0.60), True, True),
        ("1/3R 25/75 BE+trail", "fixedR", (1, 3), (0.25, 0.75), True, True),
        ("1/3R 15/85 BE+trail", "fixedR", (1, 3), (0.15, 0.85), True, True),
        ("1/2.5/4R 30/30/40 BE+trail", "fixedR", (1, 2.5, 4), (0.30, 0.30, 0.40), True, True),
        ("1/3/5R 20/20/60 BE+trail", "fixedR", (1, 3, 5), (0.20, 0.20, 0.60), True, True),
        ("1/2/3R 30/30/40 noBE+trail", "fixedR", (1, 2, 3), (0.30, 0.30, 0.40), False, True),
        ("1/3R 25/75 noBE+trail", "fixedR", (1, 3), (0.25, 0.75), False, True),
        ("1/3R 25/75 BE+notrail", "fixedR", (1, 3), (0.25, 0.75), True, False),
    ]
    for name, mode, tmult, fracs, be1, trail2 in specs:
        spec = (mode, tmult, fracs, be1, trail2)
        for mh in (96,):
            for tb in (2,):
                for cost in (0.0016,):
                    df = run(spec, mh, tb, 0.5, cost)
                    s = stats(df)
                    tr = stats(df[df.entry_t < SPLIT_TS])
                    te = stats(df[df.entry_t >= SPLIT_TS])
                    print(f"{name:<28} {mh:4d} {tb:5d} {cost:6.4f} | "
                          f"{s['n']:4d} {s['win_rate']*100:5.1f}% {s['ratio_R']:6.2f} "
                          f"{s['expect_R']:+7.2f} | {tr['n']:3d}笔{tr['expect_R']:+5.2f}R "
                          f"{te['n']:3d}笔{te['expect_R']:+5.2f}R", flush=True)
                    rows.append(dict(spec=name, mh=mh, trail=tb, cost=cost, **s,
                                     tr_n=tr["n"], tr_expect=tr["expect_R"],
                                     te_n=te["n"], te_expect=te["expect_R"]))
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "fusion_grid2.csv"), index=False)
    print("\n结果已保存 results/fusion_grid2.csv")


if __name__ == "__main__":
    main()
