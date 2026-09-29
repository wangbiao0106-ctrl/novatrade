#!/usr/bin/env python3
"""高位流动性扫顶反转策略 —— 检测 + 模拟 + 指标引擎

策略逻辑（做空）：
  1. 摆动高点：bar i 满足 h[i] > max(h[i-L:i]) 且 h[i] >= max(h[i+1:i+R+1])
  2. 扫顶：确认后首次出现 h[j] > h[i]（sweep 流动性）
  3. 回落确认：扫顶后 reject_wait 根 K 线内出现收盘价 < 摆动高点价位
  4. 入场：回落确认 bar 收盘价做空
  5. 止损：扫顶期间最高价 ext + buf_atr*ATR ；止盈：入场价 - tp_mult*风险
  6. 最多持有 max_hold 根 K 线，超时以收盘价离场
"""
import os, glob, json
import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results", "data")

# ---------------------------------------------------------------- 数据
def load_meta():
    return pd.read_csv(os.path.join(OUT_DIR, "meta.csv"))

def load_tf(tf, syms=None, min_qv=0.0):
    """返回 {sym: dict(t,o,h,l,c,v,qv)}"""
    files = glob.glob(os.path.join(OUT_DIR, tf, "*.npz"))
    out = {}
    for fp in files:
        sym = os.path.basename(fp)[:-4]
        if syms is not None and sym not in syms:
            continue
        z = np.load(fp)
        a = z["arr"]
        d = dict(t=a[:, 0].astype(np.int64), o=a[:, 1], h=a[:, 2], l=a[:, 3],
                 c=a[:, 4], v=a[:, 5], qv=a[:, 6])
        if min_qv > 0 and d["qv"].sum() < min_qv:
            continue
        out[sym] = d
    return out

# ---------------------------------------------------------------- 预计算特征
def atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    tr = np.concatenate([[h[0] - l[0]], tr])
    # 前 n 根用扩展均值（因果，无未来函数）
    return pd.Series(tr).rolling(n, min_periods=1).mean().to_numpy()

def next_higher_high(h):
    """F[i] = 首个 j>i 且 h[j] > h[i]；不存在为 -1"""
    n = len(h)
    nxt = np.full(n, -1, dtype=np.int64)
    stack = []
    for i in range(n):
        while stack and h[i] > h[stack[-1]]:
            nxt[stack.pop()] = i
        stack.append(i)
    return nxt

def rolling_max(h, w):
    s = pd.Series(h).rolling(w, min_periods=1).max().to_numpy()
    return s

def rolling_min(l, w):
    s = pd.Series(l).rolling(w, min_periods=1).min().to_numpy()
    return s

def sma(x, n):
    # 前 n 根用扩展均值（因果，无未来函数）
    return pd.Series(x).rolling(n, min_periods=1).mean().to_numpy()

LAB_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "strategy.json")

def lab_parameters(config_path=LAB_CONFIG_PATH):
    """从实验室机器参数真源派生 (detect, filter, costs)。

    研究脚本不得各自硬编码一套参数：`config/strategy.json` 是唯一机器真源，改了它
    所有对照实验必须跟着走，否则证据会和已上线规则脱钩（曾经因此出现过确认窗口、
    门控口径与参数三处不一致）。只有纯研究用的特征窗口（`mom_wins`/`sma_lens`/
    `eqh_wins`）留在代码里，它们不参与信号过滤。
    """
    with open(config_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    signal = payload["signal_parameters"]
    costs = payload.get("costs", {})
    detect = {
        "L": int(signal["L"]), "R": int(signal["R"]),
        "sweep_wait": int(signal["sweep_wait"]), "reject_wait": int(signal["reject_wait"]),
        "retest_entry": 1, "retest_wait": int(signal["resweep_wait"]), "retest_mode": "resweep",
        "major_wins": (int(signal["major_window"]),), "mom_wins": (96,),
        "sma_lens": (200,), "eqh_wins": (96,),
    }
    filters = {
        "major_win": int(signal["major_window"]), "rs_lower_ext": 1,
        "rs_deep": float(signal["resweep_deep_atr"]), "btc_down": 1,
        "rsi_s_min": float(signal["rsi_min"]), "vol_mult": float(signal["volume_multiple"]),
    }
    costs_out = {
        "atr_period": int(signal["atr_period"]), "buf_atr": float(signal["buffer_atr"]),
        "tp_mult": float(signal["take_profit_r"]), "min_atr_pct": float(signal["min_atr_pct"]),
        "max_risk_atr": float(signal["max_risk_atr"]), "max_hold_bars": int(payload.get("position_management", {}).get("time_exit_bars", 96)),
        "fee": float(costs.get("fee_rate_one_way", 0.0005)),
    }
    return detect, filters, costs_out

def btc_gate_flags(ev, btc, min_history=200):
    """BTC 门控标记的唯一实现，fail-closed，与运行时 `btcGateAllows` 一致。

    `btc_flag = 1` 表示"不通过门控"（`btc_down` 过滤器要求 flag == 0 才放行）。
    信号时刻之前不足 `min_history` 根 1h K 线、索引越界、或收盘/SMA 不可用时一律
    置 1（不发新信号）。历史实现用 `(close > sma).astype(int8)` 且 `sma` 走
    `min_periods=1`，会把数据开头（BTC 历史不足 200 根）算成"门控开"，虚增成交。
    """
    bt, bc, bs = btc
    idx = np.searchsorted(bt, ev["entry_t"], side="right") - 1
    flag = np.ones(len(ev["entry_t"]), dtype=np.int8)
    valid = (idx >= min_history - 1) & (idx < len(bc))
    safe = np.clip(idx, 0, max(len(bc) - 1, 0))
    if len(bc):
        usable = valid & np.isfinite(bc[safe]) & np.isfinite(bs[safe])
        flag[usable] = (bc[safe][usable] > bs[safe][usable]).astype(np.int8)
    ev["btc_flag"] = flag
    return ev

def rsi(c, n=14):
    diff = np.diff(c, prepend=c[0])
    up = np.clip(diff, 0, None)
    dn = np.clip(-diff, 0, None)
    # Wilder 平滑
    up_ewm = pd.Series(up).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    dn_ewm = pd.Series(dn).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    rs = up_ewm / np.maximum(dn_ewm, 1e-12)
    return 100 - 100 / (1 + rs)

def precompute(d, atr_n=14, sma_lens=(), major_wins=(), mom_wins=(), vol_n=48,
               eqh_wins=()):
    """按需预计算指标数组（一次计算，多参数组合复用）"""
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    out = {"atr": atr(h, l, c, atr_n), "rsi": rsi(c, atr_n),
           "sma20": sma(c, 20), "std20": pd.Series(c).rolling(20, min_periods=20).std().fillna(0).to_numpy()}
    # 日内运行最高价（UTC 日）
    day_id = d["t"] // 86_400_000
    n = len(h)
    daymax = np.empty(n)
    i0 = 0
    for i in range(1, n + 1):
        if i == n or day_id[i] != day_id[i0]:
            daymax[i0:i] = np.maximum.accumulate(h[i0:i])
            i0 = i
    out["daymax"] = daymax
    for n in sma_lens:
        out[f"sma{n}"] = sma(c, n)
    for w in major_wins:
        out[f"rm{w}"] = rolling_max(h, w)         # 含当前bar
        rm_shift = np.concatenate([[np.nan], rolling_max(h, w)[:-1]])
        out[f"rmb{w}"] = np.where(np.isnan(rm_shift), h[0], rm_shift)  # 不含当前bar
    for w in mom_wins:
        out[f"mn{w}"] = rolling_min(l, w)
    out["volmean"] = sma(v, vol_n)
    return out

# ---------------------------------------------------------------- 事件检测
def find_pivots(h, L, R):
    n = len(h)
    W = L + R + 1
    if n < W:
        return np.array([], dtype=np.int64)
    win = sliding_window_view(h, W)
    left_max = win[:, :L].max(axis=1) if L > 0 else np.full(win.shape[0], -np.inf)
    right_max = win[:, L + 1:].max(axis=1) if R > 0 else np.full(win.shape[0], -np.inf)
    x = h[L:n - R]
    mask = (x > left_max) & (x >= right_max)
    return np.where(mask)[0] + L

def detect_events(d, pre, F, cfg):
    """生成事件；返回 dict of np arrays"""
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    n = len(h)
    L, R = cfg["L"], cfg["R"]
    piv = find_pivots(h, L, R)
    A = pre["atr"]
    eqh_wins = cfg.get("eqh_wins", ())
    events = {"pi": [], "s": [], "k": [], "level": [], "entry": [], "ext": [],
              "atr_k": [], "atr_s": [], "atr_pi": [], "c_pi": [], "h_pi": [],
              "v_s": [], "wick_s": [], "rsi_s": [], "rsi_pi": [], "age": [],
              "k_eq_s": [], "o_k": [], "c_k1": [], "ext1": [], "h2": [],
              "v_j": [], "rsi_j": [], "o_j": [], "l_j": [], "bb_j": [],
              "mn_btw": [], "daymax_s": []}
    for w in eqh_wins:
        events[f"eqh{w}"] = []
    for pi in piv:
        level = h[pi]
        s = int(F[pi])
        if s < 0 or s - pi > cfg["sweep_wait"]:
            continue
        end = min(s + cfg["reject_wait"] + 1, n)
        cs = c[s:end]
        if not (cs < level).any():
            continue
        k = s + int(np.argmax(cs < level))
        entry = c[k]
        # 第一次扫顶特征（供过滤器使用）
        k1 = k
        c_k1 = c[k1]
        ext1 = float(h[s:k1 + 1].max())
        h2 = v_j = rsi_j = o_j = -1.0
        l_j = bb_j = -1.0
        mn_btw = 1e18
        # 回踩再入场 / 二次扫顶再入场
        if cfg.get("retest_entry", 0):
            rw = cfg.get("retest_wait", 8)
            mode = cfg.get("retest_mode", "pullback")
            j1 = min(k + rw + 1, n)
            found = -1
            if mode == "resweep":
                # 二次扫顶：再次上穿 level 后收盘又回落到 level 之下
                for j in range(k + 1, j1):
                    if h[j] > level and c[j] < level:
                        found = j
                        break
            elif mode == "resweep2":
                # 三次扫顶：连续两次"上穿 level 后收盘回落"失败后入场
                j2_first = -1
                for j in range(k + 1, j1):
                    if h[j] > level and c[j] < level:
                        j2_first = j
                        break
                if j2_first >= 0:
                    for j in range(j2_first + 1, min(j2_first + rw + 1, n)):
                        if h[j] > level and c[j] < level:
                            found = j
                            break
            else:
                rf = cfg.get("retest_frac", 0.2)
                for j in range(k + 1, j1):
                    if c[j] >= entry + rf * A[k] and h[j] <= level + 0.05 * A[j]:
                        found = j
                        break
            if found < 0:
                continue
            h2 = float(h[found]); v_j = float(v[found]); rsi_j = float(pre["rsi"][found])
            o_j = float(o[found]); l_j = float(l[found])
            bb_j = (c[found] - pre["sma20"][found]) / max(pre["std20"][found], 1e-9)
            mn_btw = float(l[k1 + 1:found + 1].min())   # 仅用到入场bar为止（无未来函数）
            k = found
            entry = c[k]
        ext = float(h[s:k + 1].max())
        if ext - entry <= 0:
            continue
        events["pi"].append(pi); events["s"].append(s); events["k"].append(k)
        events["level"].append(level); events["entry"].append(entry)
        events["ext"].append(ext)
        events["atr_k"].append(A[k]); events["atr_s"].append(A[s])
        events["atr_pi"].append(A[pi]); events["c_pi"].append(c[pi])
        events["h_pi"].append(level); events["v_s"].append(v[s])
        events["wick_s"].append(h[s] - max(o[s], c[s]))
        events["rsi_s"].append(pre["rsi"][s]); events["rsi_pi"].append(pre["rsi"][pi])
        events["age"].append(s - pi); events["k_eq_s"].append(1 if k == s else 0)
        events["o_k"].append(o[k])
        events["c_k1"].append(c_k1); events["ext1"].append(ext1)
        events["h2"].append(h2); events["v_j"].append(v_j)
        events["rsi_j"].append(rsi_j); events["o_j"].append(o_j)
        events["l_j"].append(l_j); events["bb_j"].append(bb_j)
        events["mn_btw"].append(mn_btw)
        events["daymax_s"].append(pre["daymax"][s])
        # 等高流动性：pi 前 W 根内是否存在与其价位接近的摆动高点
        if eqh_wins:
            max_w = max(eqh_wins)
            lo = int(np.searchsorted(piv, pi - max_w, side="left"))
            hi = int(np.searchsorted(piv, pi - L - R, side="left"))
            best = {w: 999.0 for w in eqh_wins}
            for j in range(lo, hi):
                p2 = piv[j]
                if p2 >= pi - L - R:
                    continue
                dist = abs(h[p2] - level) / A[pi]
                for w in eqh_wins:
                    if p2 >= pi - w and dist < best[w]:
                        best[w] = dist
            for w in eqh_wins:
                events[f"eqh{w}"].append(best[w])
    ev = {k: np.array(v) for k, v in events.items()}
    if len(ev["pi"]) == 0:
        return None
    # 依赖窗口的过滤器特征（在 precompute 中已算好数组，这里取值）
    for w in cfg.get("major_wins", ()):
        ev[f"rm{w}"] = pre[f"rm{w}"][ev["pi"]]
        ev[f"rmb{w}"] = pre[f"rmb{w}"][ev["pi"]]
    for w in cfg.get("mom_wins", ()):
        mn = pre[f"mn{w}"]
        ev[f"rise{w}"] = ev["h_pi"] - mn[ev["pi"]]
    for ln in cfg.get("sma_lens", ()):
        sm = pre[f"sma{ln}"]
        ev[f"sma{ln}"] = sm[ev["pi"]]
        ev[f"dist{ln}"] = (ev["c_pi"] - sm[ev["pi"]]) / ev["atr_pi"]
    ev["vol_ratio_s"] = ev["v_s"] / np.maximum(pre["volmean"][ev["s"]], 1e-12)
    ev["entry_t"] = d["t"][ev["k"]]
    return ev

# ---------------------------------------------------------------- 过滤器
def apply_filters(ev, f):
    """f: dict of filter params; 返回 bool mask"""
    m = np.ones(len(ev["pi"]), dtype=bool)
    if f.get("major_win", 0) > 0:
        w = f["major_win"]
        m &= ev[f"rm{w}"] == ev["h_pi"]          # 摆动点 = 最近 w 根最高价
    if f.get("new_high", 0) > 0 and f.get("major_win", 0) > 0:
        w = f["major_win"]
        m &= ev["h_pi"] > ev[f"rmb{w}"]          # 严格创新高
    if f.get("sma_len", 0) > 0 and not f.get("sma_below", 0):
        ln = f["sma_len"]
        m &= ev["c_pi"] > ev[f"sma{ln}"]         # 长期趋势向上（高位）
    if f.get("sma_below", 0) > 0 and f.get("sma_len", 0) > 0:
        ln = f["sma_len"]
        m &= ev["c_pi"] < ev[f"sma{ln}"]         # 长期趋势向下（下跌中继做空）
    if f.get("sma_dist", 0) > 0:
        ln = f["sma_dist_len"]
        m &= ev[f"dist{ln}"] >= f["sma_dist"]    # 高于均线 N 个 ATR（延伸）
    if f.get("mom_win", 0) > 0:
        w = f["mom_win"]
        m &= ev[f"rise{w}"] >= f.get("mom_min", 3.0) * ev["atr_pi"]
    if f.get("vol_mult", 0) > 0:
        m &= ev["vol_ratio_s"] >= f["vol_mult"]
    if f.get("wick_min", 0) > 0:
        m &= ev["wick_s"] >= f["wick_min"] * ev["atr_s"]
    if f.get("rj_min", 0) > 0:
        m &= (ev["level"] - ev["entry"]) >= f["rj_min"] * ev["atr_k"]
    if f.get("rsi_div", 9999) < 999:            # RSI 顶背离：扫顶时 RSI 不创新高
        m &= ev["rsi_s"] <= ev["rsi_pi"] + f["rsi_div"]
    if f.get("eqh_w", 0) > 0:                    # 等高流动性：前 W 根内存在接近价位摆动高点
        m &= ev[f"eqh{f['eqh_w']}"] <= f.get("eqh_eps", 0.5)
    if f.get("age_min", 0) > 0:
        m &= ev["age"] >= f["age_min"]           # 摆动点形成到被扫顶的时间
    if f.get("k_eq_s", 0) > 0:
        m &= ev["k_eq_s"] == 1                   # 扫顶bar本身收盘回落（纯影线扫顶）
    if f.get("rej_body", 0) > 0:                 # 入场bar为阴线且实体足够大
        m &= (ev["o_k"] - ev["entry"]) >= f["rej_body"] * ev["atr_k"]
    if f.get("rs_lower_ext", 0) > 0:             # 二次扫顶高点低于首次扫顶极端
        m &= (ev["h2"] >= 0) & (ev["h2"] < ev["ext1"])
    if f.get("rs_rsi_div", 9999) < 999:          # 二次扫顶 RSI 低于首次扫顶
        m &= (ev["h2"] >= 0) & (ev["rsi_j"] <= ev["rsi_s"] + f["rs_rsi_div"])
    if f.get("rs_vol_fade", 0) > 0:              # 二次扫顶量能萎缩
        m &= (ev["h2"] >= 0) & (ev["v_j"] < ev["v_s"])
    if f.get("rs_rej_body", 0) > 0:              # 二次扫顶收盘bar为阴线实体
        m &= (ev["h2"] >= 0) & ((ev["o_j"] - ev["entry"]) >= f["rs_rej_body"] * ev["atr_k"])
    if f.get("rj1_min", 0) > 0:                  # 首次扫顶回落力度
        m &= (ev["level"] - ev["c_k1"]) >= f["rj1_min"] * ev["atr_k"]
    if f.get("bb_k", 0) > 0:                     # 二次扫顶收盘高于布林上轨（超买延伸）
        m &= (ev["h2"] >= 0) & (ev["bb_j"] >= f["bb_k"])
    if f.get("rs_shallow", 0) > 0:               # 二次扫顶深度浅（上穿幅度小）
        m &= (ev["h2"] >= 0) & ((ev["h2"] - ev["level"]) <= f["rs_shallow"] * ev["atr_k"])
    if f.get("rs_close_low", 0) > 0:             # 二次扫顶bar收盘接近最低价（强反转K线）
        m &= (ev["h2"] >= 0) & ((ev["h2"] - ev["entry"]) >= f["rs_close_low"] * (ev["h2"] - ev["l_j"]))
    if f.get("drop_before_rs", 0) > 0:           # 二次扫顶前价格曾跌至首次回落收盘下方
        m &= (ev["h2"] >= 0) & ((ev["c_k1"] - ev["mn_btw"]) >= f["drop_before_rs"] * ev["atr_k"])
    if f.get("rs_lower_close", 0) > 0:           # 二次扫顶收盘低于首次回落收盘
        m &= (ev["h2"] >= 0) & (ev["entry"] < ev["c_k1"])
    if f.get("hod", 0) > 0:                      # 被扫价位 = 扫顶当日运行最高价（日内高位）
        m &= ev["h_pi"] >= ev["daymax_s"] - 1e-9
    if f.get("btc_up", 0) > 0:                   # BTC 处于长期均线上方（风险偏好）
        m &= ev.get("btc_flag", np.ones(len(ev["pi"]))) == 1
    if f.get("btc_down", 0) > 0:                 # BTC 处于长期均线下方（风险规避）
        m &= ev.get("btc_flag", np.zeros(len(ev["pi"]))) == 0
    if f.get("hour_ge", -1) >= 0:                # 入场小时（UTC）下限
        hour = (ev["entry_t"] // 3_600_000) % 24
        m &= hour >= f["hour_ge"]
    if f.get("hour_lt", 25) <= 24:               # 入场小时（UTC）上限（开区间）
        hour = (ev["entry_t"] // 3_600_000) % 24
        m &= hour < f["hour_lt"]
    if f.get("dow_le", 7) < 7:                   # 入场星期（周一=0）上限
        dow = (ev["entry_t"] // 86_400_000 + 4) % 7
        m &= dow <= f["dow_le"]
    if f.get("eqh_min_dist", 0) > 0 and f.get("eqh_w", 0) > 0:  # 无近距离等高聚集
        m &= ev[f"eqh{f['eqh_w']}"] >= f["eqh_min_dist"]
    if f.get("rsi_s_min", 0) > 0:                # 扫顶时 RSI 下限（超买）
        m &= ev["rsi_s"] >= f["rsi_s_min"]
    if f.get("rs_deep", 0) > 0:                  # 二次扫顶上穿深度下限
        m &= (ev["h2"] >= 0) & ((ev["h2"] - ev["level"]) >= f["rs_deep"] * ev["atr_k"])
    return m

# ---------------------------------------------------------------- 交易模拟
def simulate(ev, d, mask, bufs, tp_mults, max_hold, fee, sl_mode="ext"):
    """对给定事件模拟出场。返回 list of (buf, tp, outcomes dict)

    sl_mode: "ext" 止损在扫顶极端价之上；"level" 止损在被扫的摆动价位之上
    """
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    n = len(c)
    pi = ev["pi"][mask]; s = ev["s"][mask]; k = ev["k"][mask]
    entry = ev["entry"][mask]; ext = ev["ext"][mask]; atr_k = ev["atr_k"][mask]
    level = ev["level"][mask]; entry_t = ev["entry_t"][mask]
    h2 = ev["h2"][mask]
    m_ = len(entry)
    INF = 1 << 30
    # 每事件缓存索引窗口
    ks = k
    maxh = max_hold
    # 预取每个事件的切片（view）
    slices = []
    for i in range(m_):
        j0 = int(ks[i]) + 1
        j1 = min(j0 + maxh, n)
        if j1 <= j0:
            slices.append((j0, j0, 0))
        else:
            slices.append((j0, j1, 1))
    results = []
    for b in bufs:
        if sl_mode == "level":
            sl = np.maximum(level + b * atr_k, entry + 1e-9)
        elif sl_mode == "h2":
            # 止损在二次扫顶高点之上（仅对回踩/二次扫顶事件有效）
            sl = np.maximum(h2 + b * atr_k, entry + 1e-9)
            valid = h2 >= 0
            sl = np.where(valid, sl, entry + 1e-9)
        elif sl_mode == "atr":
            # 纯 ATR 归一化：SL = entry + b*ATR，TP = entry - tp_mult*ATR
            sl = entry + b * atr_k
        else:
            sl = np.maximum(ext + b * atr_k, entry + 1e-9)
        risk = sl - entry
        # 对每个 tp_mult 做扫描
        for tp_mult in tp_mults:
            if sl_mode == "atr":
                tp = entry - tp_mult * atr_k
            else:
                tp = entry - tp_mult * risk
            outcomes = np.zeros(m_, dtype=np.float64)   # 净 pnl（扣费）
            outcomes_g = np.zeros(m_, dtype=np.float64) # 毛 pnl（不扣费）
            kinds = np.zeros(m_, dtype=np.int8)
            for i in range(m_):
                j0, j1, ok = slices[i]
                if not ok:
                    outcomes[i] = entry[i] - c[min(k[i], n - 1)]
                    outcomes_g[i] = outcomes[i]
                    kinds[i] = 3
                    continue
                op = o[j0:j1]; hi = h[j0:j1]; lo = l[j0:j1]
                A = j0 + np.argmax(op >= sl[i]) if (op >= sl[i]).any() else INF
                B = j0 + np.argmax(op <= tp[i]) if (op <= tp[i]).any() else INF
                C = j0 + np.argmax(hi >= sl[i]) if (hi >= sl[i]).any() else INF
                D = j0 + np.argmax(lo <= tp[i]) if (lo <= tp[i]).any() else INF
                fo = min(A, B); ft = min(C, D)
                if fo == INF and ft == INF:
                    px = c[j1 - 1]; kinds[i] = 3
                elif fo <= ft:
                    if A < B:
                        px = op[A - j0]; kinds[i] = 1
                    else:
                        px = op[B - j0]; kinds[i] = 2
                else:
                    if C < D:
                        px = sl[i]; kinds[i] = 1
                    else:   # D < C 或同bar双触（保守按止损）
                        px = tp[i] if D < C else sl[i]
                        kinds[i] = 2 if D < C else 1
                outcomes[i] = entry[i] - px - fee * (entry[i] + px)
                outcomes_g[i] = entry[i] - px
            results.append(dict(buf=b, tp=tp_mult, outcomes=outcomes,
                                outcomes_g=outcomes_g, kinds=kinds,
                                risk=risk, entry=entry, entry_t=entry_t,
                                level=level, ext=ext, k=ks, pi=pi, s=s))
    return results

# ---------------------------------------------------------------- 指标汇总
def metrics(res):
    """res: dict from simulate; 计算聚合指标"""
    out = {}
    pnl = res["outcomes"]
    risk = res["risk"]
    n = len(pnl)
    win = pnl > 0
    wins = pnl[win]; losses = pnl[~win]
    out["trades"] = n
    out["win_rate"] = float(win.mean()) if n else np.nan
    out["avg_win"] = float(wins.mean()) if len(wins) else np.nan
    out["avg_loss"] = float((-losses).mean()) if len(losses) else np.nan
    out["ratio"] = (out["avg_win"] / out["avg_loss"]) if (out["avg_loss"] and out["avg_loss"] > 0) else np.nan
    out["profit_factor"] = float(wins.sum() / (-losses).sum()) if losses.sum() < 0 else np.nan
    out["expectancy_price"] = float(pnl.mean()) if n else np.nan
    out["expectancy_R"] = float((pnl / risk).mean()) if n else np.nan
    out["avg_risk"] = float(risk.mean()) if n else np.nan
    out["kind_tp"] = int((res["kinds"] == 2).sum())
    out["kind_sl"] = int((res["kinds"] == 1).sum())
    out["kind_time"] = int((res["kinds"] == 3).sum())
    return out

def metrics_by_split(res, t0, t1):
    m = (res["entry_t"] >= t0) & (res["entry_t"] < t1)
    sub = {k: (v[m] if isinstance(v, np.ndarray) else v) for k, v in res.items()}
    if len(sub["outcomes"]) == 0:
        return None
    return metrics(sub)
