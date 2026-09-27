#!/usr/bin/env python3
"""高位流动性扫顶反转（做空）策略 · 实时信号扫描器 v1.0

与 STRATEGY_SPEC.md / engine.py 完全一致。对每个合约输出当前状态：
  等待首次扫顶 / 扫顶已发生·等待回落 / 等待二次扫顶 / ★入场信号 / 持仓中 / 已离场

用法:
  python live_signal.py                       # 用最新数据扫描全市场
  python live_signal.py --asof 2026-08-10T00:00Z   # 历史时点重放（验证/复盘）
  python live_signal.py --json out/live.json  # 同时输出 JSON
  python live_signal.py --min-qv 1e8          # 只扫描流动性过滤后的合约
"""
import argparse, json, os, sys
import numpy as np
import pandas as pd
import engine as E

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
PARAMS = dict(L=10, R=5, sweep_wait=96, reject_wait=5, retest_wait=12,
              major_win=288, rsi_s_min=62, vol_mult=1.5, rs_deep=0.2,
              buf_atr=0.5, tp_mult=2.2, max_hold=96)


def load_market(tf="1h", min_qv=0.0, pool="lowmid"):
    data = E.load_tf(tf, min_qv=min_qv)
    if "BTC" not in data:
        raise RuntimeError("缺少 BTC 数据（门控需要）")
    # 标的池：lowmid = 中低流动性山寨币（推荐，alpha 所在）；all = 全部295山寨币
    uni_path = os.path.join(OUT_DIR, "universe.json")
    if pool == "all":
        alt = set(json.load(open(uni_path))["altcoins"])
    else:
        rec = json.load(open(os.path.join(OUT_DIR, "universe_recommended.json")))
        alt = set(rec["recommended_low_mid"])
    data = {s: d for s, d in data.items() if s in alt or s == "BTC"}
    return data, alt


def btc_gate_at(btc_d, idx):
    c, s = btc_d["c"], E.sma(btc_d["c"], 200)
    if idx < 0 or np.isnan(s[idx]):
        return False, float(c[idx]) if idx >= 0 else np.nan, np.nan
    return c[idx] < s[idx], float(c[idx]), float(s[idx])


def symbol_state(d, btc_d, cfg):
    """返回该合约的当前状态列表（可能有多个进行中的候选）。"""
    n = len(d["c"])
    h, l, c, v, t = d["h"], d["l"], d["c"], d["v"], d["t"]
    A = E.atr(h, l, c)
    RSI = E.rsi(c)
    volmean = E.sma(v, 48)
    rm288 = E.rolling_max(h, 288)
    F = E.next_higher_high(h)
    piv = E.find_pivots(h, cfg["L"], cfg["R"])
    states = []
    # 考察最近几个（还可能在窗口内的）摆动高点
    recent = [pi for pi in piv if pi >= n - 1 - (cfg["sweep_wait"] + cfg["retest_wait"] + 5)]
    for pi in recent[-8:]:
        level = h[pi]
        if not (level == rm288[pi]):           # 必须是288根高位
            continue
        s = int(F[pi])
        if s < 0:
            states.append(dict(phase="等待首次扫顶", pi=pi, level=level,
                               note=f"高位形成于 {pd.to_datetime(t[pi], unit='ms')}"))
            continue
        if s - pi > cfg["sweep_wait"]:
            continue
        # 首次回落确认 k1
        end = min(s + cfg["reject_wait"] + 1, n)
        cs = c[s:end]
        k1 = s + int(np.argmax(cs < level)) if (cs < level).any() else None
        if k1 is None:
            if s >= n - 1 - cfg["reject_wait"]:
                states.append(dict(phase="扫顶已发生·等待收盘回落", pi=pi, level=level,
                                   s=s, note="首次扫顶后未回落确认"))
            continue
        ext1 = float(h[s:k1 + 1].max())
        # 二次扫顶 j
        j1 = min(k1 + cfg["retest_wait"] + 1, n)
        found = None
        for j in range(k1 + 1, j1):
            if h[j] > level and c[j] < level:
                found = j
                break
        if found is None:
            if n - 1 <= k1 + cfg["retest_wait"]:
                states.append(dict(phase="等待二次扫顶", pi=pi, level=level,
                                   k1=k1, ext1=ext1,
                                   note="首次扫顶已回落，等待再次上穿"))
            continue
        j = found
        # 入场过滤
        if not (h[j] < ext1):
            continue
        if not (h[j] - level >= cfg["rs_deep"] * A[j]):
            continue
        if not (RSI[s] >= cfg["rsi_s_min"]):
            continue
        if not (v[s] >= cfg["vol_mult"] * volmean[s]):
            continue
        gate, bc, bs = btc_gate_at(btc_d, np.clip(
            np.searchsorted(btc_d["t"], t[j], side="right") - 1,
            0, len(btc_d["t"]) - 1))
        if not gate:
            continue
        entry = c[j]
        ext = float(h[s:j + 1].max())
        sl = ext + cfg["buf_atr"] * A[j]
        risk = sl - entry
        tp = entry - cfg["tp_mult"] * risk
        if risk <= 0:
            continue
        # 薄盘/极端波动保护（不影响历史回测：60 笔均在此范围内）
        if A[j] / entry < cfg.get("min_atr_pct", 0.5) / 100:
            continue
        if risk / A[j] > cfg.get("max_risk_atr", 5.0):
            continue
        # 出场扫描（与 engine.simulate 完全一致）
        j0 = j + 1
        j1 = min(j0 + cfg["max_hold"], n)
        kind, px = None, None
        if j1 > j0:
            op = d["o"][j0:j1]; hi = h[j0:j1]; lo = l[j0:j1]
            INF = 1 << 30
            A_ = j0 + np.argmax(op >= sl) if (op >= sl).any() else INF
            B_ = j0 + np.argmax(op <= tp) if (op <= tp).any() else INF
            C_ = j0 + np.argmax(hi >= sl) if (hi >= sl).any() else INF
            D_ = j0 + np.argmax(lo <= tp) if (lo <= tp).any() else INF
            fo, ft = min(A_, B_), min(C_, D_)
            if fo == INF and ft == INF:
                # 仅在完整 96 根窗口结束后才视为时间离场
                if n - 1 >= j0 + cfg["max_hold"] - 1:
                    kind, px = "时间离场", c[j1 - 1]
            elif fo <= ft:
                if A_ < B_:
                    kind, px = "止损(跳空)", op[A_ - j0]
                else:
                    kind, px = "止盈(跳空)", op[B_ - j0]
            else:
                if C_ < D_:
                    kind, px = "止损", sl
                elif D_ < C_:
                    kind, px = "止盈", tp
                else:  # 同bar双触，保守按止损
                    kind, px = "止损(同bar双触)", sl
        if kind is None and j == n - 1:
            states.append(dict(
                phase="★入场信号", sym_ok=True, j=j, entry=round(entry, 6),
                sl=round(sl, 6), tp=round(tp, 6), risk=round(risk, 6),
                level=round(level, 6), ext=round(ext, 6),
                atr=round(A[j], 6), note="二次扫顶bar刚收盘，全部过滤通过"))
        elif kind is None and j < n - 1:
            states.append(dict(
                phase="持仓中", j=j, entry=round(entry, 6), sl=round(sl, 6),
                tp=round(tp, 6), risk=round(risk, 6),
                bars=int(n - 1 - j), cur=round(c[-1], 6),
                note=f"持仓 {n-1-j} 根 / {cfg['max_hold']} 根上限"))
        elif kind is not None:
            pnl = entry - px - 0.0005 * (entry + px)
            states.append(dict(
                phase="已离场", j=j, entry=round(entry, 6), exit=round(px, 6),
                kind=kind, pnl_R=round(pnl / risk, 3),
                note=f"{kind} @ {px:.6g}, {pnl/risk:+.2f}R"))
    return states


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--asof", default=None, help="历史时点 UTC，如 2026-08-10T00:00Z")
    ap.add_argument("--json", default=None)
    ap.add_argument("--min-qv", type=float, default=0.0)
    ap.add_argument("--pool", default="lowmid", choices=["lowmid", "all"],
                    help="lowmid=中低流动性山寨币177个(推荐) all=全部295个山寨币")
    ap.add_argument("--min-atr-pct", type=float, default=0.5,
                    help="最小波动过滤：ATR14/价格低于该百分比(%)的币不下单（薄盘保护）")
    ap.add_argument("--max-risk-atr", type=float, default=5.0,
                    help="最大止损距离：R 超过该 ATR 倍数的信号放弃（极端波动保护）")
    ap.add_argument("--top", type=int, default=40, help="打印信号条数上限")
    args = ap.parse_args()

    data, pool_set = load_market(args.tf, args.min_qv, args.pool)
    if args.asof:
        asof_ms = int(pd.Timestamp(args.asof, tz="UTC").value // 1e6)
        # BTC 不截断：门控 SMA200 需要完整历史；其余合约截断到 asof
        for s, d in data.items():
            if s == "BTC":
                continue
            idx = np.searchsorted(d["t"], asof_ms, side="right")
            for k in d:
                d[k] = d[k][:idx]
        data = {s: d for s, d in data.items() if len(d["t"]) > 20}
    btc_d = data["BTC"]
    run_cfg = dict(PARAMS)
    run_cfg["min_atr_pct"] = args.min_atr_pct
    run_cfg["max_risk_atr"] = args.max_risk_atr

    rows = []
    for sym, d in data.items():
        if sym == "BTC":
            continue
        try:
            sts = symbol_state(d, btc_d, run_cfg)
        except Exception as e:
            print(f"[warn] {sym}: {e}", file=sys.stderr)
            continue
        for st in sts:
            st["sym"] = sym
            rows.append(st)

    gate_on, bc, bs = btc_gate_at(btc_d, len(btc_d["c"]) - 1)
    last_ts = pd.to_datetime(btc_d["t"][-1], unit="ms")
    print(f"扫描时点: {last_ts}  (BTC {bc:.0f} vs SMA200 {bs:.0f} → "
          f"门控 {'开·可做空' if gate_on else '关·停止开新仓'})")
    print(f"标的池: {'中低流动性山寨币' if args.pool=='lowmid' else '全部山寨币'} "
          f"({len(pool_set)} 个)  状态条目: {len(rows)}")
    order = {"★入场信号": 0, "持仓中": 1, "等待二次扫顶": 2, "扫顶已发生·等待收盘回落": 3,
             "等待首次扫顶": 4, "已离场": 5}
    rows.sort(key=lambda r: order.get(r["phase"], 9))
    n_show = min(args.top, len(rows))
    if n_show:
        print("\n" + "-" * 110)
        for r in rows[:n_show]:
            p = r["phase"]
            if p == "★入场信号":
                info = (f"level={r['level']:.6g} entry={r['entry']:.6g} "
                        f"SL={r['sl']:.6g} TP={r['tp']:.6g} R={r['risk']:.6g} "
                        f"ATR={r['atr']:.6g}")
            elif p == "持仓中":
                info = (f"entry={r['entry']:.6g} SL={r['sl']:.6g} TP={r['tp']:.6g} "
                        f"现价={r['cur']:.6g} 已持{r['bars']}根")
            elif p == "已离场":
                info = f"entry={r['entry']:.6g} → {r['kind']} @ {r['exit']:.6g} ({r['pnl_R']:+.2f}R)"
            elif p == "等待二次扫顶":
                info = f"level={r['level']:.6g} ext1={r['ext1']:.6g}"
            elif p == "等待首次扫顶":
                info = f"level={r['level']:.6g}"
            else:
                info = f"level={r['level']:.6g}"
            print(f"{r['sym']:12s} {p:18s} {info}")
        print("-" * 110)
    # 汇总
    from collections import Counter
    cnt = Counter(r["phase"] for r in rows)
    print("\n状态分布:", dict(cnt))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(dict(asof=str(last_ts), gate_on=gate_on, btc=bc, sma200=bs,
                           signals=rows), f, ensure_ascii=False, indent=1,
                      default=str)
        print("JSON 已保存:", args.json)


if __name__ == "__main__":
    main()
