#!/usr/bin/env python3
"""导出基础 resweep 事件的逐笔特征 + 输赢标签，用于特征分析"""
import sys
import numpy as np
import pandas as pd
import engine as E

SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)


def main():
    tf = sys.argv[1] if len(sys.argv) > 1 else "1h"
    out_csv = sys.argv[2] if len(sys.argv) > 2 else f"out/events_{tf}.csv"
    cfg = {"L": 5, "R": 5, "sweep_wait": 96, "reject_wait": 5,
           "retest_entry": 1, "retest_wait": 12, "retest_mode": "resweep",
           "major_wins": (288,), "mom_wins": (96,), "sma_lens": (200,),
           "eqh_wins": (96,)}
    filt = {"major_win": 288, "rs_lower_ext": 1}
    buf, tp, max_hold, fee = 0.3, 2.0, 96, 0.0005
    meta = E.load_meta()
    qv_rank = {s: r for s, r in zip(meta["sym"], meta["tot_qv"].rank(pct=True))}
    data = E.load_tf(tf)
    bd = data["BTC"]
    bs200 = E.sma(bd["c"], 200)
    bs50 = E.sma(bd["c"], 50)
    btc_t = bd["t"]
    rows = []
    for sym, d in data.items():
        pre = E.precompute(d, sma_lens=(200,), major_wins=(288,),
                           mom_wins=(96,), eqh_wins=(96,))
        F = E.next_higher_high(d["h"])
        ev = E.detect_events(d, pre, F, cfg)
        if ev is None:
            continue
        m = E.apply_filters(ev, filt)
        if m.sum() == 0:
            continue
        res = E.simulate(ev, d, m, [buf], [tp], max_hold, fee, sl_mode="ext")[0]
        o, h, l, c, v, t = d["o"], d["h"], d["l"], d["c"], d["v"], d["t"]
        for i in range(len(res["entry"])):
            k = int(res["k"][i])
            s = int(res["s"][i])
            pi = int(res["pi"][i])
            entry = res["entry"][i]
            level = res["level"][i]
            atrk = ev["atr_k"][m][i]
            win = int(res["outcomes"][i] > 0)
            bt_idx = int(np.clip(np.searchsorted(btc_t, res["entry_t"][i],
                                                 side="right") - 1,
                                 0, len(btc_t) - 1))
            rows.append(dict(
                sym=sym, qv_rank=qv_rank.get(sym, np.nan), win=win,
                entry_t=res["entry_t"][i],
                btc_b200=int(bd["c"][bt_idx] < bs200[bt_idx]),
                btc_b50=int(bd["c"][bt_idx] < bs50[bt_idx]),
                hour=(t[k] // 3_600_000) % 24,
                dow=(t[k] // 86_400_000 + 4) % 7,
                risk=res["risk"][i] / atrk,
                rj1=(level - ev["c_k1"][m][i]) / atrk,
                wick=ev["wick_s"][m][i] / ev["atr_s"][m][i],
                age=ev["age"][m][i],
                dist200=ev["dist200"][m][i],
                rsi_s=ev["rsi_s"][m][i],
                rsi_pi=ev["rsi_pi"][m][i],
                rsi_j=ev["rsi_j"][m][i],
                vol_s=ev["v_s"][m][i] / max(pre["volmean"][s], 1e-9),
                vol_j=ev["v_j"][m][i] / max(pre["volmean"][k], 1e-9),
                drop=(ev["c_k1"][m][i] - ev["mn_btw"][m][i]) / atrk,
                rs_shallow=(ev["h2"][m][i] - level) / atrk,
                rs_close=(ev["h2"][m][i] - entry) / max(
                    ev["h2"][m][i] - ev["l_j"][m][i], 1e-9),
                rej_body=(ev["o_k"][m][i] - entry) / atrk,
                bb_j=ev["bb_j"][m][i],
                rise96=(level - pre["mn96"][pi]) / ev["atr_pi"][m][i],
                eqh=ev["eqh96"][m][i],
                retest_bars=k - s,
            ))
    df = pd.DataFrame(rows)
    df.to_csv(out_csv, index=False)
    print(f"events={len(df)} win={df.win.mean():.3f} saved -> {out_csv}")
    print("split:", int((df.entry_t < SPLIT_TS).sum()), "train /",
          int((df.entry_t >= SPLIT_TS).sum()), "test")


if __name__ == "__main__":
    main()
