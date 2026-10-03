#!/usr/bin/env python3
"""盈利分布条件预测与典型交易 K 线图（纯 Python SVG）。

先运行 report_full_history.py，随后运行本文件。输入为同目录的全历史报告、
生产范围逐笔 CSV、单仓 CSV 及预处理的 1h/15m 数组；不修改正式策略参数。
结果写入 results/full_history/profit_projection.json 和 charts/ 下六张 SVG。
预测是固定历史经验分布上的 IID 重抽情景，不能当作未来收益承诺。

用法：
  python3 strategies/sweep_reversal_short/research/profit_projection.py
  python3 strategies/sweep_reversal_short/research/profit_projection.py --help
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine as E  # noqa: E402

ROOT = HERE.parent
FULL = ROOT / "results" / "full_history"
CHARTS = FULL / "charts"
H_MS, M15_MS, DAY_MS = 3_600_000, 900_000, 86_400_000
FONT = "PingFang SC, Hiragino Sans GB, Helvetica Neue, Arial, sans-serif"
SCENARIOS = {"pessimistic_bull_gate_mostly_closed": 24, "neutral_18m_average": 36,
             "optimistic_bear_gate_mostly_open": 48}
PERCENTILES = (5, 10, 25, 50, 75, 90, 95)
CAVEATS = [
    "仅包含当前仍上市的合约，存在幸存者偏差；较晚上市合约历史较短。",
    "基线仅含单边 5bp 手续费，不含滑点、资金费率、盘口冲击、部分成交和合约张数取整。",
    "收益路径只在平仓后计入盈亏；持仓中浮动回撤和账户级固定 5% 日内 MTM 熔断未模拟，实际路径可能不同。",
    "bootstrap 假定未来每笔独立同分布（IID），且沿用固定历史经验分布；未覆盖参数选择偏差、分布变化或估计不确定性。",
    "同币事件及连续市场阶段可能相关，IID 重抽会低估聚集亏损；历史单仓样本量较小。",
    "年交易数 24/36/48 是门控开闭程度的假设情景，不是信号频率的置信区间；行情变化可能使频率落在此范围之外。",
    "滚动一年窗口高度重叠，不能当作相互独立的样本外检验；正历史收益不能保证未来盈利。",
    "成本压力仅对同一成交序列扣除额外费用，未重新模拟成交、保护单触发和熔断。",
    "OHLC 只能确定止盈/止损所在 K 线，无法确定精确触发时刻；本报告在该 K 线收盘边界确认已实现盈亏。",
    "回测按确认收盘价计算保护价、入场后持有最多 96 小时；运行时保护价不按实际成交价重算，时间离场从确认 K 线开盘计时（提前 15 分钟），两者并非完整实盘复现。",
]


def iso(ms: int, full: bool = False) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M" if full else "%m-%d %H:%M")


def equity_path(returns: np.ndarray) -> tuple[np.ndarray, float]:
    """Includes the initial equity so an initial losing trade contributes to drawdown."""
    returns = np.asarray(returns, dtype=float)
    if not np.all(np.isfinite(returns)) or np.any(returns <= -1):
        raise ValueError("池收益必须为有限数且大于 -100%")
    equity = np.concatenate(([1.0], np.cumprod(1 + returns)))
    peak = np.maximum.accumulate(equity)
    return equity, float(np.max(1 - equity / peak))


def longest_losses(returns: np.ndarray) -> int:
    run = best = 0
    for value in returns:
        run = run + 1 if value < 0 else 0
        best = max(best, run)
    return best


def recover_exits(frame: pd.DataFrame, data15: dict, fee: float) -> pd.DataFrame:
    """Recover execution prices using tune_execution.exit_trade's OHLC semantics.

    entry_ts is the confirmation candle's OPEN. hold_bars_15m is exit_index -
    entry_index. Entry executes at confirmation close; time exits at exit close.
    Stop/target executes intrabar (or at a gap open); its close boundary is an
    observation upper bound, not an invented exact execution timestamp.
    """
    rows = []
    for _, row in frame.iterrows():
        d = data15[row.symbol]
        entry_i = int(np.searchsorted(d["t"], int(row.entry_ts)))
        if entry_i >= len(d["t"]) or int(d["t"][entry_i]) != int(row.entry_ts):
            raise ValueError(f"找不到入场确认 K 线：{row.symbol} {row.entry_ts}")
        exit_i = entry_i + int(row.hold_bars_15m)
        if exit_i >= len(d["t"]):
            raise ValueError(f"退出索引越界：{row.symbol}")
        op = float(d["o"][exit_i])
        if row.kind == "sl":
            px = max(op, float(row.sl))
        elif row.kind == "tp":
            px = min(op, float(row.tp))
        elif row.kind == "time":
            px = float(d["c"][exit_i])
        else:
            raise ValueError(f"未知离场方式：{row.kind}")
        net_pnl = float(row.entry) - px - fee * (float(row.entry) + px)
        if not np.isclose(net_pnl, float(row.pnl), atol=1e-10, rtol=1e-8):
            raise ValueError(f"成交复核不一致：{row.symbol} pnl={row.pnl} replay={net_pnl}")
        item = row.to_dict()
        item.update(entry_execution_ts=int(row.entry_ts) + M15_MS,
                    exit_bar_open_ts=int(d["t"][exit_i]),
                    exit_recognition_ts=int(d["t"][exit_i]) + M15_MS,
                    exit_price=px,
                    exit_time_semantics="bar_close" if row.kind == "time" else "intrabar_unknown_recognized_at_bar_close")
        rows.append(item)
    return pd.DataFrame(rows).sort_values(["exit_recognition_ts", "symbol"]).reset_index(drop=True)


def complete_rolling_windows(slot: pd.DataFrame, coverage_start: int, coverage_end: int) -> list[dict]:
    """Monthly anchors plus dataset edges; every window covers a complete 365 days."""
    span = 365 * DAY_MS
    latest_start = coverage_end - span
    if latest_start < coverage_start:
        return []
    start = pd.Timestamp(coverage_start, unit="ms", tz="UTC")
    end = pd.Timestamp(latest_start, unit="ms", tz="UTC")
    month = start.normalize().replace(day=1) + pd.offsets.MonthBegin(1)
    anchors = {coverage_start, latest_start}
    anchors.update(int(t.value // 1_000_000) for t in pd.date_range(month, end, freq="MS"))
    rows = []
    for lo in sorted(anchors):
        hi = lo + span
        # Include exits at the coverage boundary; exclude those at the starting boundary.
        chosen = slot[(slot.exit_recognition_ts > lo) & (slot.exit_recognition_ts <= hi)]
        ret = (chosen.pnl / chosen.entry).to_numpy()
        rows.append({"start_ts": int(lo), "end_ts": int(hi), "trades": int(len(chosen)),
                     "return": float(np.prod(1 + ret) - 1),
                     "max_realized_drawdown_pct": equity_path(ret)[1] * 100})
    return rows


def monthly_returns(slot: pd.DataFrame, coverage_start: int, coverage_end: int) -> list[dict]:
    first = pd.Timestamp(coverage_start, unit="ms", tz="UTC").tz_localize(None).to_period("M")
    last = pd.Timestamp(coverage_end - 1, unit="ms", tz="UTC").tz_localize(None).to_period("M")
    exit_month = pd.to_datetime(slot.exit_recognition_ts, unit="ms", utc=True).dt.strftime("%Y-%m")
    equity = 1.0
    rows = []
    for period in pd.period_range(first, last, freq="M"):
        chosen = slot[exit_month == str(period)]
        ret = (chosen.pnl / chosen.entry).to_numpy()
        before = equity
        equity *= float(np.prod(1 + ret))
        rows.append({"month_utc": str(period), "trades": int(len(chosen)), "wins": int((ret > 0).sum()),
                     "losses": int((ret < 0).sum()), "return": float(equity / before - 1),
                     "complete_calendar_month": (int(period.start_time.tz_localize("UTC").value // 1_000_000) >= coverage_start
                                                 and int((period + 1).start_time.tz_localize("UTC").value // 1_000_000) <= coverage_end),
                     "equity_start": before, "equity_end": equity,
                     "max_realized_drawdown_pct": equity_path(ret)[1] * 100})
    return rows


def r_buckets(values: np.ndarray) -> dict[str, int]:
    edges = (-np.inf, -1, -0.5, 0, 0.5, 1, 2, np.inf)
    labels = ("< -1R", "[-1,-0.5)R", "[-0.5,0)R", "[0,0.5)R", "[0.5,1)R", "[1,2)R", ">= 2R")
    result = {label: int(((values >= lo) & (values < hi)).sum())
              for label, lo, hi in zip(labels, edges[:-1], edges[1:])}
    if sum(result.values()) != len(values):
        raise ValueError("R 分布存在未覆盖样本")
    return result


def projection(slot: pd.DataFrame, live: pd.DataFrame, report: dict, *, seed: int = 7,
               draws: int = 40_000, dd_draws: int = 20_000) -> dict:
    slot = slot.sort_values("exit_recognition_ts").reset_index(drop=True)
    coverage_start = int(report["data"]["start"])
    # report.data.end is the last complete 1h candle's OPEN timestamp.
    coverage_end = int(report["data"]["end"]) + H_MS
    span_days = (coverage_end - coverage_start) / DAY_MS
    ret = (slot.pnl / slot.entry).to_numpy()
    stop_distance = (slot.risk / slot.entry).to_numpy()
    equity, max_dd = equity_path(ret)
    losses = ret[ret < 0]
    rolling = complete_rolling_windows(slot, coverage_start, coverage_end)
    rolling_ret = np.array([row["return"] for row in rolling])
    rng = np.random.default_rng(seed)
    scenarios = {}
    for name, n in SCENARIOS.items():
        sampled = rng.choice(ret, (draws, n), replace=True)
        yearly = np.prod(1 + sampled, axis=1) - 1
        scenarios[name] = {"trades_per_year": n,
                           "percentiles": {f"p{q}": float(np.percentile(yearly, q)) for q in PERCENTILES},
                           "mean": float(yearly.mean()),
                           "geometric_mean": float(np.expm1(np.log1p(yearly).mean())),
                           "p_loss": float((yearly < 0).mean()),
                           "p_loss_over_30pct": float((yearly < -0.3).mean()),
                           "p_loss_over_50pct": float((yearly < -0.5).mean()),
                           "p_double": float((yearly > 1).mean())}
    sampled = rng.choice(ret, (dd_draws, SCENARIOS["neutral_18m_average"]), replace=True)
    paths = np.concatenate((np.ones((dd_draws, 1)), np.cumprod(1 + sampled, axis=1)), axis=1)
    year_dd = (1 - paths / np.maximum.accumulate(paths, axis=1)).max(axis=1)
    sensitivity = []
    base_fee = float(report["costs"]["fee_per_side"])
    for extra_bps in (0, 2, 5, 10):
        extra = extra_bps / 10_000
        stressed = ret - extra * (1 + slot.exit_price.to_numpy() / slot.entry.to_numpy())
        stressed_eq, stressed_dd = equity_path(stressed)
        sensitivity.append({"additional_cost_bps_per_side": extra_bps,
                            "total_fee_equivalent_bps_per_side": base_fee * 10_000 + extra_bps,
                            "final_multiple": float(stressed_eq[-1]),
                            "total_return": float(stressed_eq[-1] - 1),
                            "annualized": float(stressed_eq[-1] ** (365 / span_days) - 1),
                            "max_drawdown_pct": stressed_dd * 100,
                            "mean_trade_pool_return": float(stressed.mean()),
                            "win_rate": float((stressed > 0).mean())})
    r_live = live.pnl_R.to_numpy()
    curve = [{"timestamp": coverage_start, "equity": 1.0, "event": "coverage_start"}]
    curve.extend({"timestamp": int(t), "equity": float(e), "event": "realized_exit"}
                 for t, e in zip(slot.exit_recognition_ts, equity[1:]))
    curve.append({"timestamp": coverage_end, "equity": float(equity[-1]), "event": "coverage_end"})
    return {
        "basis": "生产范围单仓；名义仓位 = 1 × 资金池可用余额；单笔池净收益 = pnl / entry；平仓后复投",
        "trades": int(len(slot)), "span_days": span_days,
        "coverage": {"start_ts": coverage_start, "end_exclusive_ts": coverage_end,
                     "start_utc": iso(coverage_start, True), "end_utc": iso(coverage_end, True),
                     "end_semantics": "report.data.end + 1h；最后完整 1h K 线的收盘边界"},
        "trades_per_year_observed": len(slot) / span_days * 365,
        "per_trade_pool_return": {"mean": float(ret.mean()), "median": float(np.median(ret)),
                                  "std": float(ret.std(ddof=1)), "geometric_mean": float(np.expm1(np.log1p(ret).mean())),
                                  "win_rate": float((ret > 0).mean()), "worst": float(ret.min()), "best": float(ret.max()),
                                  "median_loss": float(-np.median(losses)) if len(losses) else 0.0,
                                  "max_consecutive_losses": longest_losses(ret)},
        "stop_distance_pct": {"median": float(np.median(stop_distance) * 100),
                              "p90": float(np.percentile(stop_distance, 90) * 100), "max": float(stop_distance.max() * 100)},
        "r_distribution_live_pool": {"basis": "所有生产范围候选成交，允许跨币重叠；不等同单仓资金池路径",
                                     "trades": int(len(live)), "mean_R": float(r_live.mean()), "buckets": r_buckets(r_live),
                                     "exit_mix": live.kind.value_counts(normalize=True).to_dict()},
        "full_period": {"final_multiple": float(equity[-1]), "total_return": float(equity[-1] - 1),
                        "annualized": float(equity[-1] ** (365 / span_days) - 1), "max_drawdown_pct": max_dd * 100},
        "monthly_realized_returns": monthly_returns(slot, coverage_start, coverage_end),
        "rolling_12m_windows": {"method": "完整 365 天，按 UTC 月初、覆盖起点及最后可完整覆盖的起点取窗；按离场确认时间计收益；窗口高度重叠",
                                "count": len(rolling), "windows": rolling,
                                "percentiles": {f"p{q}": float(np.percentile(rolling_ret, q)) for q in (10, 25, 50, 75, 90)} if len(rolling) else {},
                                "min": float(rolling_ret.min()) if len(rolling) else None,
                                "max": float(rolling_ret.max()) if len(rolling) else None,
                                "p_positive": float((rolling_ret > 0).mean()) if len(rolling) else None},
        "bootstrap_one_year": scenarios,
        "bootstrap_one_year_max_drawdown_neutral": {f"p{q}": float(np.percentile(year_dd, q) * 100) for q in (50, 75, 90, 95)},
        "cost_sensitivity": {"method": "冻结成交序列；额外每边成本 × (入场价 + 回测出场价) / 入场价；可作为滑点/费用的等价压力，不是重撮合",
                             "scenarios": sensitivity},
        "method": {"rng": "numpy.random.default_rng", "seed": seed, "bootstrap_draws": draws,
                   "drawdown_draws": dd_draws, "year_days": 365, "sampling": "with replacement, IID empirical per-trade pool returns",
                   "annualization": "final_multiple ** (365 / coverage_days) - 1",
                   "frequency": "single_slot_trade_count / coverage_days * 365",
                   "r_bucket_boundaries": "左闭右开；首尾覆盖正负无穷；等于 -1R 归入 [-1,-0.5)R",
                   "equity_timestamp": "exit candle close boundary; initial equity 1.0 at coverage start",
                   "numpy_version": np.__version__, "pandas_version": pd.__version__},
        "caveats": CAVEATS, "_equity_curve": curve,
    }


class SVG:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="{FONT}">',
                      f'<rect width="{width}" height="{height}" fill="#0f141b"/>']

    def line(self, x1, y1, x2, y2, color="#8899aa", width=1, dash=None, opacity=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{width}" opacity="{opacity}"{d}/>')

    def rect(self, x, y, w, h, fill, stroke=None, opacity=1.0, rx=0):
        s = f' stroke="{stroke}"' if stroke else ""
        self.parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{max(h, 0):.1f}" fill="{fill}" opacity="{opacity}" rx="{rx}"{s}/>')

    def text(self, x, y, s, size=12, color="#e6edf3", anchor="start", weight="normal", opacity=1.0):
        self.parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}" opacity="{opacity}">{escape(str(s))}</text>')

    def circle(self, x, y, r, fill, stroke="#0f141b"):
        self.parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')

    def polyline(self, points, color, width=2, fill="none", opacity=1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        self.parts.append(f'<polyline points="{pts}" fill="{fill}" stroke="{color}" stroke-width="{width}" opacity="{opacity}"/>')

    def save(self, path: Path):
        path.write_text("\n".join(self.parts + ["</svg>"]), encoding="utf-8")


def candles(svg, t, o, h, l, c, box, y_lo, y_hi, highlight=None):
    x0, y0, x1, y1 = box
    interval = int(np.median(np.diff(t))) if len(t) > 1 else M15_MS
    step = (x1 - x0) / len(t)
    body = max(1.2, step * 0.62)
    def xf(ts): return x0 + (ts - int(t[0])) / interval * step
    def yf(p): return y1 - (p - y_lo) / (y_hi - y_lo) * (y1 - y0)
    for i in range(len(t)):
        x = x0 + (i + 0.5) * step
        color = "#3fb950" if c[i] >= o[i] else "#f85149"
        if highlight and i in highlight:
            svg.rect(x - step / 2, y0, step, y1 - y0, highlight[i], opacity=0.16)
        svg.line(x, yf(h[i]), x, yf(l[i]), color)
        top, bottom = yf(max(o[i], c[i])), yf(min(o[i], c[i]))
        svg.rect(x - body / 2, top, body, max(bottom - top, 1), color)
    return xf, yf


def axes(svg, box, t, y_lo, y_hi, n_price=5, n_time=6):
    x0, y0, x1, y1 = box
    svg.rect(x0, y0, x1 - x0, y1 - y0, "none", stroke="#30363d")
    for k in range(n_price + 1):
        p = y_lo + (y_hi - y_lo) * k / n_price
        y = y1 - (y1 - y0) * k / n_price
        svg.line(x0, y, x1, y, "#30363d", dash="2,4")
        svg.text(x0 - 8, y + 4, f"{p:.5g}", 11, "#8b949e", "end")
    for k in range(n_time + 1):
        i = min(len(t) - 1, int(round(k * (len(t) - 1) / n_time)))
        x = x0 + (i + 0.5) * (x1 - x0) / len(t)
        svg.text(x, y1 + 18, iso(int(t[i])), 10, "#8b949e", "middle")


def pin(svg, x, y, number, color, dy=-23):
    svg.line(x, y, x, y + dy, color, 1.2)
    svg.circle(x, y + dy, 11, color)
    svg.text(x, y + dy + 4, number, 11, "#0f141b", "middle", "bold")


def structure_for(symbol, signal_ts, data1, btc):
    detect, filters, _ = E.lab_parameters()
    d = data1[symbol]
    pre = E.precompute(d, sma_lens=(200,), major_wins=(288,), mom_wins=(96,), eqh_wins=(96,))
    ev = E.detect_events(d, pre, E.next_higher_high(d["h"]), dict(detect))
    if ev is None:
        return None
    E.btc_gate_flags(ev, btc)
    for i in np.flatnonzero(E.apply_filters(ev, filters)):
        j = int(ev["k"][i])
        if int(d["t"][j]) == signal_ts:
            pi, s = int(ev["pi"][i]), int(ev["s"][i])
            level = float(ev["level"][i])
            k1 = next(idx for idx in range(s, j) if d["c"][idx] < level)
            return {"pi": pi, "s": s, "k1": k1, "j": j, "level": level, "ext": float(ev["ext"][i]),
                    "structure_close": float(ev["entry"][i]), "atr": float(ev["atr_k"][i])}
    return None


def example_chart(trade, structure, d1, d15, path):
    kind_zh = {"tp": "止盈出场", "sl": "止损出场", "time": "96 小时时间离场"}[trade.kind]
    svg = SVG(1280, 1010)
    svg.text(30, 34, f"{trade.symbol}-USDT-SWAP · 山寨币二次扫顶 · {kind_zh}", 21, weight="bold")
    svg.text(30, 61, f"结构收盘 {iso(int(trade.signal_ts) + H_MS, True)} UTC  →  确认收盘 / 回测入场 {iso(int(trade.entry_execution_ts), True)} UTC", 12, "#8b949e")
    svg.text(30, 83, f"回测入场 {trade.entry:.6g}  |  初始止损 {trade.sl:.6g}（距离 {trade.risk / trade.entry:.2%}）  |  固定止盈 {trade.tp:.6g}（2.2R）  |  净收益 {trade.pnl_R:+.2f}R（含手续费）", 12, "#e6edf3")
    pi, s, k1, j = (structure[k] for k in ("pi", "s", "k1", "j"))
    a = max(0, pi - 12)
    exit_1h = int(np.searchsorted(d1["t"], int(trade.exit_bar_open_ts), side="right")) - 1
    b = min(len(d1["t"]) - 1, max(exit_1h + 4, j + 10))
    t, o, h, l, c = (d1[key][a:b + 1] for key in ("t", "o", "h", "l", "c"))
    spread = max(float(h.max()), trade.sl) - min(float(l.min()), trade.tp)
    lo, hi = min(float(l.min()), trade.tp) - spread * 0.14, max(float(h.max()), trade.sl) + spread * 0.17
    box = (82, 124, 1100, 480)
    xf, yf = candles(svg, t, o, h, l, c, box, lo, hi,
                     {pi - a: "#d29922", s - a: "#f0883e", k1 - a: "#58a6ff", j - a: "#f85149"})
    axes(svg, box, t, lo, hi)
    svg.text(82, 113, "A · 1 小时结构与完整持仓区间", 13, weight="bold")
    svg.line(box[0], yf(structure["level"]), box[2], yf(structure["level"]), "#d29922", dash="6,4")
    svg.text(1108, yf(structure["level"]) - 7, "12 天前高", 11, "#d29922")
    for idx, num, col, dy in ((pi, "1", "#d29922", -28), (s, "2", "#f0883e", -36),
                              (k1, "3", "#58a6ff", 28), (j, "4", "#f85149", -28)):
        px = d1["l"][idx] if num == "3" else d1["h"][idx]
        pin(svg, xf(int(d1["t"][idx]) + H_MS / 2), yf(px), num, col, dy)
    exit_marker_ts = (int(trade.exit_recognition_ts) if trade.kind == "time"
                      else int(trade.exit_bar_open_ts) + M15_MS / 2)
    x_entry, x_exit = xf(int(trade.entry_execution_ts)), xf(exit_marker_ts)
    for px, col, name in ((trade.sl, "#f85149", "止损"), (trade.entry, "#ffffff", "入场"), (trade.tp, "#3fb950", "止盈")):
        svg.line(x_entry, yf(px), x_exit, yf(px), col, 1.6, dash="5,3" if name != "入场" else None)
        svg.text(1108, yf(px) + 9, f"{name} {px:.6g}", 11, col)
    pin(svg, x_entry, yf(trade.entry), "5", "#ffffff", 38)
    exit_col = {"tp": "#3fb950", "sl": "#f85149", "time": "#a371f7"}[trade.kind]
    pin(svg, x_exit, yf(trade.exit_price), "6", exit_col, -26 if trade.kind == "sl" else 28)
    legend = [("1  12 天最高的摆动高点", "#d29922"), ("2  首次扫顶：RSI ≥ 62、放量", "#f0883e"),
              ("3  收盘跌回前高下方", "#58a6ff"), ("4  二次扫顶：高点低于首次极值", "#f85149"),
              ("5  后续 15m 阴线确认收盘后下单", "#ffffff"), (f"6  {kind_zh}，回测价 {trade.exit_price:.6g}", exit_col)]
    for k, (label, col) in enumerate(legend):
        svg.text(82 + (k % 3) * 383, 528 + (k // 3) * 26, label, 12, col)
    svg.text(82, 584, f"持仓 {int(trade.hold_bars_15m)} 根 15m；出场 K 线 {iso(int(trade.exit_bar_open_ts), True)} UTC。止盈/止损点标在该 K 线上，触发分钟无法由 OHLC 确定。", 11, "#8b949e")
    struct_ts = int(trade.signal_ts)
    i0 = int(np.searchsorted(d15["t"], struct_ts - 2 * H_MS))
    i1 = int(np.searchsorted(d15["t"], int(trade.entry_execution_ts) + 2 * H_MS))
    t15, o15, h15, l15, c15 = (d15[key][i0:i1] for key in ("t", "o", "h", "l", "c"))
    spread2 = max(float(h15.max()), trade.sl) - min(float(l15.min()), trade.entry)
    lo2, hi2 = min(float(l15.min()), trade.entry) - spread2 * 0.18, max(float(h15.max()), trade.sl) + spread2 * 0.16
    box2 = (82, 638, 1100, 885)
    highlight = {idx: "#f85149" if struct_ts <= ts < struct_ts + H_MS else "#58a6ff"
                 for idx, ts in enumerate(t15) if struct_ts <= ts < struct_ts + 2 * H_MS}
    xf2, yf2 = candles(svg, t15, o15, h15, l15, c15, box2, lo2, hi2, highlight)
    axes(svg, box2, t15, lo2, hi2)
    svg.text(82, 622, "B · 15 分钟确认与下单点放大", 13, weight="bold")
    for px, col, name in ((structure["structure_close"], "#d29922", "确认阈值"), (trade.sl, "#f85149", "保护止损")):
        svg.line(box2[0], yf2(px), box2[2], yf2(px), col, dash="5,4")
        svg.text(1108, yf2(px) + 4, f"{name} {px:.6g}", 11, col)
    pin(svg, xf2(int(trade.entry_execution_ts)), yf2(trade.entry), "5", "#ffffff", 32)
    svg.text(82, 933, "红底：结构 1h 内的 15m K 线，结构确认前已经收盘，不能作入场确认。", 12, "#f85149")
    svg.text(82, 957, "蓝底：结构收盘后的 4 根 15m 确认窗口；首根 close ≤ 确认阈值且 close < open 的收盘边界为下单点。", 12, "#58a6ff")
    svg.text(82, 985, "真实历史 K 线，点位为回测参考；时间统一为 UTC，横轴为开盘时间。上图包含止盈线。回测出场价不含费用，净 R 含费用。", 11, "#8b949e")
    svg.save(path)


def r_distribution_chart(live, path, slot_trades):
    svg = SVG(1120, 500)
    r = live.pnl_R.to_numpy()
    svg.text(28, 35, f"逐笔净 R 分布 · 生产范围候选成交 {len(r)} 笔", 20, weight="bold")
    svg.text(28, 60, f"允许跨币重叠，独立统计信号质量；年收益预测使用 {slot_trades} 笔单仓序列。均值 {r.mean():+.2f}R，胜率 {(r > 0).mean():.1%}。", 12, "#8b949e")
    step_r = .25
    edges = np.arange(math.floor(r.min() / step_r) * step_r, math.ceil(r.max() / step_r) * step_r + step_r / 2, step_r)
    counts, _ = np.histogram(r, edges)
    box = (75, 103, 1085, 386)
    step = (box[2] - box[0]) / len(counts)
    ymax = max(counts.max(), 1) * 1.18
    for k in range(5):
        n = ymax * k / 4
        y = box[3] - n / ymax * (box[3] - box[1])
        svg.line(box[0], y, box[2], y, "#30363d", dash="2,4")
        svg.text(box[0] - 9, y + 4, f"{n:.0f}", 11, "#8b949e", "end")
    for k, n in enumerate(counts):
        x = box[0] + k * step
        height = n / ymax * (box[3] - box[1])
        if n:
            svg.rect(x + 3, box[3] - height, step - 6, height, "#3fb950" if edges[k] >= 0 else "#f85149")
            svg.text(x + step / 2, box[3] - height - 7, str(n), 11, anchor="middle")
        svg.text(x, box[3] + 20, f"{edges[k]:+.2f}", 10, "#8b949e", "middle")
    svg.text(box[2], box[3] + 20, f"{edges[-1]:+.2f}", 10, "#8b949e", "middle")
    zero = box[0] + (0 - edges[0]) / (edges[-1] - edges[0]) * (box[2] - box[0])
    svg.line(zero, box[1], zero, box[3], "#e6edf3", dash="5,4")
    svg.text(75, 444, "−1R 附近为止损，+2.2R 附近为止盈；中间主要是 96 小时时间离场。手续费使止损净 R 略低于 −1。", 12, "#8b949e")
    svg.text(75, 470, "R = 净价格盈亏 / 初始止损距离；基线成本为单边 5bp，未含滑点与资金费率。每个柱体左闭右开，最后一柱包含右边界。", 11, "#8b949e")
    svg.save(path)


def equity_chart(proj, path):
    svg = SVG(1120, 560)
    curve = proj["_equity_curve"]
    ts = np.array([r["timestamp"] for r in curve]); eq = np.array([r["equity"] for r in curve])
    fp = proj["full_period"]
    svg.text(28, 35, f"资金池已实现净值 · {proj['trades']} 笔单仓，名义 = 1 × 池，平仓后复投", 20, weight="bold")
    svg.text(28, 61, f"终值 {fp['final_multiple']:.2f}x  |  覆盖期年化 {fp['annualized']:+.1%}  |  已实现最大回撤 {fp['max_drawdown_pct']:.1f}%  |  约 {proj['trades_per_year_observed']:.1f} 笔/年", 12, "#8b949e")
    box = (75, 106, 1068, 427)
    lo, hi = min(eq.min(), 1.0) * .8, max(eq.max(), 1.0) * 1.1
    def xf(v): return box[0] + (v - ts[0]) / (ts[-1] - ts[0]) * (box[2] - box[0])
    def yf(v): return box[3] - (v - lo) / (hi - lo) * (box[3] - box[1])
    peak = np.maximum.accumulate(eq)
    for k in range(6):
        v = lo + (hi - lo) * k / 5
        svg.line(box[0], yf(v), box[2], yf(v), "#30363d", dash="2,4")
        svg.text(box[0] - 8, yf(v) + 4, f"{v:.2f}x", 11, "#8b949e", "end")
    svg.line(box[0], yf(1), box[2], yf(1), "#8b949e", dash="6,4")
    points = [(xf(ts[0]), yf(eq[0]))]
    for i in range(1, len(eq)):
        points.extend(((xf(ts[i]), yf(eq[i - 1])), (xf(ts[i]), yf(eq[i]))))
    svg.polyline(points, "#3fb950", 2.4)
    svg.polyline([(xf(t), yf(p)) for t, p in zip(ts, peak)], "#8b949e", 1, opacity=.65)
    for t, e in zip(ts[1:-1], eq[1:-1]): svg.circle(xf(t), yf(e), 2.5, "#3fb950")
    dd_i = int(np.argmax(1 - eq / peak))
    svg.line(xf(ts[dd_i]), yf(eq[dd_i]), xf(ts[dd_i]), yf(peak[dd_i]), "#f85149", 1.6, dash="3,3")
    svg.text(min(xf(ts[dd_i]) + 10, 820), (yf(eq[dd_i]) + yf(peak[dd_i])) / 2, f"已实现回撤 {fp['max_drawdown_pct']:.1f}%", 12, "#f85149")
    for k in range(7):
        t = ts[0] + (ts[-1] - ts[0]) * k / 6
        svg.text(xf(t), box[3] + 22, datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m"), 11, "#8b949e", "middle")
    svg.text(75, 486, "阶梯在离场 K 线收盘边界更新；覆盖期起点包含 1.0x 初始权益。灰线为历史峰值，持仓浮盈不提前复投。", 12, "#8b949e")
    svg.text(75, 513, "该曲线未展示持仓内浮动盈亏，也未模拟固定 5% 日内 MTM 熔断；实际账户路径和最大回撤可能不同。", 12, "#f0883e")
    svg.text(75, 539, f"历史最长连续亏损 {proj['per_trade_pool_return']['max_consecutive_losses']} 笔；手续费单边 5bp，未含滑点、资金费率和成交冲击。", 11, "#8b949e")
    svg.save(path)


def yearly_chart(proj, path):
    svg = SVG(1240, 640)
    svg.text(28, 35, f"未来一年复投收益的条件分布 · IID bootstrap {proj['method']['bootstrap_draws']:,} 次", 20, weight="bold")
    svg.text(28, 61, f"从 {proj['trades']} 笔单仓池净收益重抽；柱体为 P25–P75，细线为 P5–P95，白点为中位数。全部分位点在刻度内。", 12, "#8b949e")
    names = ("低频情景 · 24 笔/年", "中频情景 · 36 笔/年", "高频情景 · 48 笔/年")
    scen = list(proj["bootstrap_one_year"].values())
    lo = math.floor(min(s["percentiles"]["p5"] for s in scen) * 2) / 2
    hi = math.ceil(max(s["percentiles"]["p95"] for s in scen))
    box = (90, 146, 1178, 468)
    def yf(v): return box[3] - (v - lo) / (hi - lo) * (box[3] - box[1])
    for v in np.linspace(lo, hi, 8):
        svg.line(box[0], yf(v), box[2], yf(v), "#30363d", dash="2,4")
        svg.text(box[0] - 9, yf(v) + 4, f"{v:+.0%}", 11, "#8b949e", "end")
    svg.line(box[0], yf(0), box[2], yf(0), "#e6edf3", 1.2)
    step = (box[2] - box[0]) / 3
    for k, s in enumerate(scen):
        x = box[0] + (k + .5) * step
        p = s["percentiles"]
        svg.text(x, 96, names[k], 14, anchor="middle", weight="bold")
        svg.text(x, 120, f"P5 {p['p5']:+.0%}  /  中位 {p['p50']:+.0%}  /  P95 {p['p95']:+.0%}", 11, "#8b949e", "middle")
        svg.line(x, yf(p["p5"]), x, yf(p["p95"]), "#8b949e", 2)
        svg.line(x - 15, yf(p["p5"]), x + 15, yf(p["p5"]), "#8b949e", 2)
        svg.line(x - 15, yf(p["p95"]), x + 15, yf(p["p95"]), "#8b949e", 2)
        svg.rect(x - 43, yf(p["p75"]), 86, yf(p["p25"]) - yf(p["p75"]), "#1f6feb", rx=3)
        svg.circle(x, yf(p["p50"]), 5, "#ffffff")
        svg.text(x + 55, yf(p["p50"]) + 4, f"{p['p50']:+.0%}", 13)
        svg.text(x, 503, f"亏损 {s['p_loss']:.1%}  |  亏超 30% {s['p_loss_over_30pct']:.1%}", 12, "#f0883e", "middle")
    dd = proj["bootstrap_one_year_max_drawdown_neutral"]
    svg.text(90, 547, f"中频情景的一年已实现最大回撤：中位 {dd['p50']:.1f}% / P90 {dd['p90']:.1f}% / P95 {dd['p95']:.1f}%。", 12, "#f0883e")
    svg.text(90, 575, "24/36/48 笔是交易频率假设，不是概率区间。分位数仅对“经验分布固定、每笔独立”成立。", 12, "#8b949e")
    svg.text(90, 603, "未覆盖市场分布变化、事件相关性和参数估计误差；未计资金费率、滑点及日内 MTM 熔断，不能视作未来盈利保证。", 11, "#8b949e")
    svg.save(path)


def source_record(path: Path) -> dict:
    return {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=7, help="bootstrap 随机种子，默认 7")
    parser.add_argument("--draws", type=int, default=40_000, help="每档年收益重抽次数，默认 40000")
    parser.add_argument("--drawdown-draws", type=int, default=20_000, help="中频情景回撤重抽次数，默认 20000")
    args = parser.parse_args(argv)
    if args.draws < 100 or args.drawdown_draws < 100:
        parser.error("重抽次数至少 100")
    CHARTS.mkdir(parents=True, exist_ok=True)
    report_path = FULL / "report_fee5bps.json"
    live_path, slot_path = FULL / "live_pool_fee5bps.csv", FULL / "live_single_slot_fee5bps.csv"
    report = json.loads(report_path.read_text())
    fee = float(report["costs"]["fee_per_side"])
    live = pd.read_csv(live_path)
    slot = pd.read_csv(slot_path)
    E.OUT_DIR = str(FULL / "data")
    data15 = E.load_tf("15m", syms=set(live.symbol))
    live = recover_exits(live, data15, fee)
    slot = recover_exits(slot, data15, fee)
    proj = projection(slot, live, report, seed=args.seed, draws=args.draws, dd_draws=args.drawdown_draws)
    window = slot[slot.period == "B_v1_4_window"]
    examples = {kind: window[window.kind == kind].sort_values("entry_execution_ts").iloc[-1]
                for kind in ("tp", "sl", "time")}
    symbols = {row.symbol for row in examples.values()} | {"BTC"}
    data1 = E.load_tf("1h", syms=symbols)
    bd = data1["BTC"]
    btc = (bd["t"], bd["c"], E.sma(bd["c"], 200))
    proj["example_selection"] = "v1.4 研究窗口内，每种离场方式按入场时间取最后一笔，确定性选择；不挑选最盈利样本"
    proj["example_trades"] = {}
    for kind, row in examples.items():
        structure = structure_for(row.symbol, int(row.signal_ts), data1, btc)
        if structure is None:
            raise ValueError(f"找不到原始结构：{row.symbol} {kind}")
        name = f"example_{kind}_{row.symbol}.svg"
        example_chart(row, structure, data1[row.symbol], data15[row.symbol], CHARTS / name)
        proj["example_trades"][kind] = {
            "symbol": row.symbol, "signal_ts": int(row.signal_ts), "entry_ts": int(row.entry_ts),
            "entry_execution_ts": int(row.entry_execution_ts), "exit_bar_open_ts": int(row.exit_bar_open_ts),
            "exit_recognition_ts": int(row.exit_recognition_ts), "exit_time_semantics": row.exit_time_semantics,
            "entry": float(row.entry), "stop": float(row.sl), "take": float(row.tp), "backtest_exit_price": float(row.exit_price),
            "pnl_R": float(row.pnl_R), "net_pool_return": float(row.pnl / row.entry),
            "hold_bars_15m": int(row.hold_bars_15m), "stop_distance_pct": float(row.risk / row.entry * 100),
            "structure": structure, "chart": f"charts/{name}"}
    r_distribution_chart(live, CHARTS / "r_distribution.svg", len(slot))
    equity_chart(proj, CHARTS / "pool_equity_curve.svg")
    yearly_chart(proj, CHARTS / "yearly_return_distribution.svg")
    proj["equity_curve"] = proj.pop("_equity_curve")
    input_paths = [report_path, live_path, slot_path, ROOT / "config" / "strategy.json"]
    input_paths.extend(FULL / "data" / "15m" / f"{symbol}.npz" for symbol in sorted(set(live.symbol)))
    input_paths.extend(FULL / "data" / "1h" / f"{symbol}.npz" for symbol in sorted(symbols))
    proj["method"]["source_files"] = [source_record(path) for path in input_paths]
    proj["method"]["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    proj["method"]["reproduce"] = f"python3 strategies/sweep_reversal_short/research/profit_projection.py --seed {args.seed} --draws {args.draws} --drawdown-draws {args.drawdown_draws}"
    proj["method"]["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    out = FULL / "profit_projection.json"
    out.write_text(json.dumps(proj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {out}，六张 SVG；{proj['trades']} 笔单仓，覆盖 {proj['span_days']:.2f} 天，约 {proj['trades_per_year_observed']:.2f} 笔/年")
    print(f"终值 {proj['full_period']['final_multiple']:.2f}x，覆盖期年化 {proj['full_period']['annualized']:+.1%}，完整滚动 365 天窗口 {proj['rolling_12m_windows']['count']} 个")
    for name, scenario in proj["bootstrap_one_year"].items():
        p = scenario["percentiles"]
        print(f"{name}: P5 {p['p5']:+.1%} / 中位 {p['p50']:+.1%} / P95 {p['p95']:+.1%}；亏损概率 {scenario['p_loss']:.1%}")


if __name__ == "__main__":
    main()
