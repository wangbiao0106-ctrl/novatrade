#!/usr/bin/env python3
"""高位流动性扫顶反转（做空）策略 · 实时信号扫描器 v1.3

与 STRATEGY.md / STRATEGY_SPEC.md 及运行时 `StrategyEngine.evaluateWithConfirmation`
一致：1h 只建立二次扫顶结构，入场必须等结构收盘后 1 小时窗口内（时间戳
+60/+75/+90/+105 分钟）首根 `close <= 结构收盘价` 且收阴的 15m K 线确认，随后按
确认 K 线收盘价做空，出场在 15m 上按 384 根上限模拟。

对每个合约输出当前状态：
  等待首次扫顶 / 扫顶已发生·等待收盘回落 / 等待二次扫顶 / 等待15m确认 /
  ★入场信号 / 持仓中 / 确认失败·已取消 / 已离场

用法:
  python live_signal.py                       # 用最新数据扫描动态热门榜前20
  python live_signal.py --asof 2026-08-10T00:00Z   # 历史时点重放（验证/复盘）
  python live_signal.py --json results/live.json  # 同时输出 JSON
  python live_signal.py --min-qv 1e8          # 只扫描流动性过滤后的合约
"""
import argparse, json, os, sys
import numpy as np
import pandas as pd
import engine as E

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
PARAMS = dict(L=10, R=5, sweep_wait=96, reject_wait=5, retest_wait=12,
              major_win=288, atr_period=14, rsi_s_min=62, vol_mult=1.5, rs_deep=0.2,
              buf_atr=0.5, tp_mult=2.2, max_hold=96, max_hold_15m=384,
              confirmation_window_minutes=60)


MAINSTREAM = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI",
}
STABLECOINS = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDE", "USD1", "PYUSD",
    "GUSD", "USDP", "EURT", "EURS", "USDY", "SUSD",
}
with open(os.path.join(CONFIG_DIR, "universe.json"), encoding="utf-8") as handle:
    EXCLUDED_BASES = frozenset(item.upper() for item in json.load(handle).get("exclude", []))


def _base_symbol(symbol):
    return symbol.split("-")[0].upper()


def _eligible_hot_alt(symbol):
    normalized = symbol.upper()
    # Research files use short base symbols; a full instrument id must still
    # satisfy the same USDT linear-swap boundary as the Swift runtime.
    if "-" in normalized and not normalized.endswith("-USDT-SWAP"):
        return False
    base = _base_symbol(normalized)
    return (not base.endswith("USD") and base not in MAINSTREAM
            and base not in STABLECOINS and base not in EXCLUDED_BASES)


def load_market(tf="1h", min_qv=0.0, pool="hot20", asof_ms=None):
    data = E.load_tf(tf, min_qv=min_qv)
    if "BTC" not in data or len(data["BTC"].get("c", ())) == 0:
        raise RuntimeError("缺少 BTC 数据（门控需要）")
    # hot20 是生产规则：按扫描时点最近 24 根 1h 报价成交量动态选取。
    # lowmid/all 只保留给历史回测基线和结果复核。
    uni_path = os.path.join(CONFIG_DIR, "universe.json")
    if pool == "hot20":
        candidates = [s for s in data if s != "BTC" and _eligible_hot_alt(s)]
        scores = []
        for symbol in candidates:
            values = data[symbol]
            end = len(values["t"])
            if asof_ms is not None:
                end = int(np.searchsorted(values["t"], asof_ms, side="right"))
            start = max(0, end - 24)
            scores.append((float(values["qv"][start:end].sum()), symbol))
        alt = {symbol for _, symbol in sorted(scores, reverse=True)[:20]}
    elif pool == "all":
        alt = set(json.load(open(uni_path))["altcoins"])
    else:
        rec = json.load(open(os.path.join(CONFIG_DIR, "universe_recommended.json")))
        alt = set(rec["recommended_low_mid"])
    data = {s: d for s, d in data.items() if s in alt or s == "BTC"}
    return data, alt


def btc_gate_at(btc_d, idx):
    c = btc_d["c"]
    if len(c) < 200 or idx < 199 or idx >= len(c):
        price = float(c[idx]) if 0 <= idx < len(c) else np.nan
        return False, price, np.nan
    s = E.sma(c, 200)
    if not np.isfinite(s[idx]):
        return False, float(c[idx]), np.nan
    return c[idx] < s[idx], float(c[idx]), float(s[idx])


def symbol_state(d, btc_d, cfg, d15=None):
    """返回该合约的当前状态列表（可能有多个进行中的候选）。"""
    n = len(d["c"])
    h, l, c, v, t = d["h"], d["l"], d["c"], d["v"], d["t"]
    A = E.atr(h, l, c, cfg.get("atr_period", 14))
    RSI = E.rsi(c, cfg.get("atr_period", 14))
    volmean = E.sma(v, 48)
    rm288 = E.rolling_max(h, 288)
    F = E.next_higher_high(h)
    piv = E.find_pivots(h, cfg["L"], cfg["R"])
    states = []
    # 结构窗口最长 = sweep_wait + reject_wait + resweep_wait，窗口内的**所有**摆动高点
    # 都要考察。此前只取最近的 8 个，会整段漏掉更早的高位结构（与引擎/回测不一致）。
    recent = [pi for pi in piv if pi >= n - 1 - (cfg["sweep_wait"] + cfg["reject_wait"] + cfg["retest_wait"])]
    for pi in recent:
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
        btc_idx = int(np.searchsorted(btc_d["t"], t[j], side="right") - 1)
        gate, bc, bs = btc_gate_at(btc_d, btc_idx)
        if not gate:
            continue
        ext = float(h[s:j + 1].max())
        # 已上线规则：1h 只建立结构，入场必须等窗口内的 15m 收盘确认。
        # 窗口是半开区间 [结构 bar 收盘, 结构 bar 收盘 + 60 分钟)，K 线时间戳为开盘时间。
        if d15 is None:
            states.append(dict(phase="缺少15m数据·无法确认", j=j, level=round(level, 6),
                               ext=round(ext, 6), note="该标的没有 15m K 线，不能判定上线入场条件"))
            continue
        t15, o15, c15 = d15["t"], d15["o"], d15["c"]
        window_start = int(t[j]) + 3_600_000
        window_end = window_start + cfg.get("confirmation_window_minutes", 60) * 60_000
        p0 = int(np.searchsorted(t15, window_start, side="left"))
        p1 = int(np.searchsorted(t15, window_end, side="left"))
        confirm = -1
        for p in range(p0, min(p1, len(c15))):
            if c15[p] <= c[j] and c15[p] < o15[p]:
                confirm = p
                break
        if confirm < 0:
            expired = len(t15) > 0 and int(t15[-1]) >= window_end
            states.append(dict(
                phase="确认失败·已取消" if expired else "等待15m确认",
                j=j, level=round(level, 6), ext=round(ext, 6),
                note=("结构收盘后 1 小时内没有符合条件的 15m 阴线，结构作废"
                      if expired else "二次扫顶刚收盘，等待窗口内的首根 15m 阴线确认")))
            continue
        entry = float(c15[confirm])
        sl = ext + cfg["buf_atr"] * A[j]
        risk = sl - entry
        tp = entry - cfg["tp_mult"] * risk
        if risk <= 0:
            continue
        # 薄盘/极端波动保护（生产口径：门控 ATR 相对价格的比例与止损距离）
        if A[j] / entry < cfg.get("min_atr_pct", 0.5) / 100:
            continue
        if risk / A[j] > cfg.get("max_risk_atr", 5.0):
            continue
        # 出场扫描：从确认 K 线的下一根 15m 起，最多 max_hold_15m 根（=96 根 1h）。
        o15a, h15a, l15a = d15["o"], d15["h"], d15["l"]
        n15 = len(c15)
        hold = int(cfg.get("max_hold_15m", 384))
        k0 = confirm + 1
        k1_ = min(k0 + hold, n15)
        kind, px = None, None
        if k1_ > k0:
            op = o15a[k0:k1_]; hi = h15a[k0:k1_]; lo = l15a[k0:k1_]
            INF = 1 << 30
            A_ = k0 + int(np.argmax(op >= sl)) if (op >= sl).any() else INF
            B_ = k0 + int(np.argmax(op <= tp)) if (op <= tp).any() else INF
            C_ = k0 + int(np.argmax(hi >= sl)) if (hi >= sl).any() else INF
            D_ = k0 + int(np.argmax(lo <= tp)) if (lo <= tp).any() else INF
            fo, ft = min(A_, B_), min(C_, D_)
            if fo == INF and ft == INF:
                if n15 - 1 >= k0 + hold - 1:
                    kind, px = "时间离场", float(c15[k1_ - 1])
            elif fo <= ft:
                kind, px = ("止损(跳空)", float(op[A_ - k0])) if A_ < B_ else ("止盈(跳空)", float(op[B_ - k0]))
            else:
                if C_ < D_:
                    kind, px = "止损", sl
                elif D_ < C_:
                    kind, px = "止盈", tp
                else:
                    kind, px = "止损(同bar双触)", sl
        if kind is None and confirm == n15 - 1:
            states.append(dict(
                phase="★入场信号", sym_ok=True, j=j, confirm=confirm,
                entry=round(entry, 6), sl=round(sl, 6), tp=round(tp, 6), risk=round(risk, 6),
                level=round(level, 6), ext=round(ext, 6), atr=round(A[j], 6),
                note="首根 15m 阴线收盘确认，按上线规则市价做空"))
        elif kind is None and confirm < n15 - 1:
            states.append(dict(
                phase="持仓中", j=j, confirm=confirm, entry=round(entry, 6), sl=round(sl, 6),
                tp=round(tp, 6), risk=round(risk, 6),
                bars=int(n15 - 1 - confirm), cur=round(float(c15[-1]), 6),
                note=f"持仓 {n15 - 1 - confirm} 根 15m / {hold} 根上限"))
        elif kind is not None:
            pnl = entry - px - 0.0005 * (entry + px)
            states.append(dict(
                phase="已离场", j=j, confirm=confirm, entry=round(entry, 6), exit=round(px, 6),
                kind=kind, pnl_R=round(pnl / risk, 3),
                note=f"{kind} @ {px:.6g}, {pnl/risk:+.2f}R"))
    return states


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--asof", default=None, help="历史时点 UTC，如 2026-08-10T00:00Z")
    ap.add_argument("--json", default=None)
    ap.add_argument("--min-qv", type=float, default=0.0)
    ap.add_argument("--pool", default="hot20", choices=["hot20", "lowmid", "all"],
                    help="hot20=动态热门榜前20(生产规则) lowmid/all=历史回测基线")
    ap.add_argument("--min-atr-pct", type=float, default=0.5,
                    help="最小波动过滤：ATR14/价格低于该百分比(%%)的币不下单（薄盘保护）")
    ap.add_argument("--max-risk-atr", type=float, default=5.0,
                    help="最大止损距离：R 超过该 ATR 倍数的信号放弃（极端波动保护）")
    ap.add_argument("--top", type=int, default=40, help="打印信号条数上限")
    args = ap.parse_args()

    asof_ms = None
    if args.asof:
        asof_ms = int(pd.Timestamp(args.asof, tz="UTC").value // 1e6)
    data, pool_set = load_market(args.tf, args.min_qv, args.pool, asof_ms=asof_ms)
    if args.asof:
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

    # 已上线规则要等 15m 收盘确认，所以必须把 15m 序列一起载入并对齐到同一时点。
    data15 = E.load_tf("15m")
    if args.asof:
        for s, d in data15.items():
            if s == "BTC":
                continue
            idx = np.searchsorted(d["t"], asof_ms, side="right")
            for k in d:
                d[k] = d[k][:idx]

    rows = []
    for sym, d in data.items():
        if sym == "BTC":
            continue
        try:
            sts = symbol_state(d, btc_d, run_cfg, data15.get(sym))
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
    pool_label = {"hot20": "动态热门榜前20个山寨币", "lowmid": "中低流动性山寨币历史基线", "all": "全部山寨币历史基线"}[args.pool]
    print(f"标的池: {pool_label} "
          f"({len(pool_set)} 个)  状态条目: {len(rows)}")
    order = {"★入场信号": 0, "持仓中": 1, "等待15m确认": 2, "等待二次扫顶": 3,
             "扫顶已发生·等待收盘回落": 4, "等待首次扫顶": 5, "确认失败·已取消": 6,
             "缺少15m数据·无法确认": 7, "已离场": 8}
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
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(dict(asof=str(last_ts), gate_on=gate_on, btc=bc, sma200=bs,
                           signals=rows), f, ensure_ascii=False, indent=1,
                      default=str)
        print("JSON 已保存:", args.json)


if __name__ == "__main__":
    main()
