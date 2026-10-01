#!/usr/bin/env python3
"""Reproducible paper-trading backtest for the EMA altcoin long strategy.

The script consumes confirmed OKX 5-minute candles, keeps only complete hours,
and writes all derived output below this strategy directory.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/kline/okx/swap/5m"
LAB = ROOT / "strategies/ema_altcoin_long"
OUT = LAB / "results"
UTC = timezone.utc
HOUR_MS = 3_600_000
FIVE_MIN_MS = 300_000

MAJOR = {
    "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI",
}
STABLE = {"USDC", "USDT", "DAI", "BUSD", "FDUSD", "TUSD", "USDE", "USD1", "PYUSD", "GUSD", "EURT", "EURS"}
# OKX's export contains synthetic stock, index, commodity and other non-crypto swaps.
NON_CRYPTO = {
    "AAOI", "AAPL", "ADBE", "AEHR", "ALAB", "AMAT", "AMC", "AMD", "AMZN", "ANTHROPIC", "APLD",
    "APP", "ARM", "ASML", "ASTS", "AVGO", "AXTI", "BB", "BE", "BRKB", "BX", "BZ", "CGNX",
    "CIEN", "CL", "COHR", "COIN", "COST", "CRCL", "CRDO", "CRM", "CRWD", "CRWV", "CSCO",
    "CSOPSAMSUNG2L", "CSOPSKHYNIX2L", "CXMT", "DDOG", "DELL", "DKNG", "EWJ", "EWT", "EWY",
    "EWZ", "FLNC", "FLY", "GEV", "GLW", "GME", "GOOGL", "GTLB", "HANMI", "HIMS", "HOOD", "HPE",
    "HUT", "HYUNDAI", "IBM", "INTC", "INTW", "IONQ", "IREN", "ISRG", "IWM", "JNJ", "JP225",
    "KIOXIA", "KLAC", "KO", "KORU", "KR200", "KSTR", "LGELECTRONICS", "LITE", "LLY", "LRCX",
    "LUNR", "LYTE", "MARA", "META", "MINIMAX", "MOONSHOT", "MRK", "MRNA", "MRVL", "MSFT",
    "MSTR", "MSTU", "MU", "MUU", "MVLL", "NAVER", "NBIS", "NET", "NFLX", "NG", "NOK", "NOW",
    "NVDA", "NVDL", "OKLO", "OKTA", "ON", "ONDS", "OPENAI", "ORCL", "OSCR", "OURA", "OUST",
    "PLTR", "POET", "POPMART", "PYPL", "QCOM", "QQQ", "RDDT", "RDW", "RIOT", "RIVN", "RKLB",
    "ROK", "SAMSUNG", "SHAZ", "SHEIN", "SHLD", "SHOP", "SIMO", "SKDD", "SKHY", "SKHYNIX",
    "SKUU", "SMCI", "SMH", "SNDK", "SNOW", "SOFTBANK", "SONY", "SOXL", "SOXS", "SPCH", "SPCX",
    "SPY", "SQQQ", "STABLE", "SUSD", "TEAM", "TEM", "TER", "TMF", "TQQQ", "TSEM", "TSLA",
    "TSLL", "TSM", "TTMI", "TTWO", "TWLO", "UNH", "UNITREE", "URNM", "US100", "US500", "USDF",
    "USDP", "USDY", "USO", "UVXY", "VRT", "WDC", "WEN", "WMT", "XAG", "XAU", "XBI", "XCU",
    "XIAOMI", "XLE", "XOM", "XPD", "XPT", "ZHIPU", "ZHONGJI", "ZM",
}


@dataclass
class Bar:
    ts: int
    o: float
    h: float
    l: float
    c: float
    q: float


@dataclass
class Trade:
    symbol: str
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    target: float
    risk: float
    r: float
    reason: str


def ema(xs: list[float], n: int) -> list[float]:
    alpha = 2 / (n + 1)
    out: list[float] = []
    for value in xs:
        out.append(value if not out else alpha * value + (1 - alpha) * out[-1])
    return out


def atr(bs: list[Bar], n: int = 14) -> list[float]:
    tr: list[float] = []
    for i, bar in enumerate(bs):
        previous = bs[i - 1].c if i else bar.c
        tr.append(max(bar.h - bar.l, abs(bar.h - previous), abs(bar.l - previous)))
    return ema(tr, n)


def resample(rows: list[Bar]) -> list[Bar]:
    """Build only complete UTC hours with exactly twelve consecutive 5m bars."""
    groups: dict[int, list[Bar]] = {}
    for bar in rows:
        hour = (bar.ts // HOUR_MS) * HOUR_MS
        groups.setdefault(hour, []).append(bar)
    out: list[Bar] = []
    for hour, group in sorted(groups.items()):
        group.sort(key=lambda item: item.ts)
        timestamps = [item.ts for item in group]
        expected = [hour + i * FIVE_MIN_MS for i in range(12)]
        if timestamps != expected:
            continue
        out.append(Bar(hour, group[0].o, max(x.h for x in group), min(x.l for x in group), group[-1].c, sum(x.q for x in group)))
    return out


def load(path: Path) -> list[Bar]:
    rows: list[Bar] = []
    with gzip.open(path, "rt") as handle:
        for line in handle:
            item = json.loads(line)
            if not item.get("confirmed", True):
                continue
            try:
                rows.append(Bar(int(item["timestamp_ms"]), float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]), float(item.get("quote_volume", 0))))
            except (KeyError, TypeError, ValueError):
                continue
    return resample(rows)


def base(path: Path) -> str:
    return path.name.split("_USDT_")[0]


def is_altcoin(symbol: str) -> bool:
    return symbol not in MAJOR and symbol not in STABLE and symbol not in NON_CRYPTO


def execution_price(raw: float, side: str, slip: float) -> float:
    return raw * (1 + slip if side == "buy" else 1 - slip)


def exit_price(raw: float, side: str, slip: float) -> float:
    return raw * (1 - slip if side == "buy" else 1 + slip)


def simulate(symbol: str, bs: list[Bar], p: dict, start: int, end: int, btc_state: dict[int, tuple[float, float, float]]):
    if len(bs) < p["ema_trend"] + 40 or end - start < 2:
        return []
    closes = [bar.c for bar in bs]
    # 周期全部来自 config/strategy.json 的 signal_parameters（机器参数真源），
    # 不得在代码里再写一套默认值。
    fast_n, slow_n, trend_n, atr_n = p["ema_fast"], p["ema_slow"], p["ema_trend"], p["atr_period"]
    fast, slow, trend, aa = ema(closes, fast_n), ema(closes, slow_n), ema(closes, trend_n), atr(bs, atr_n)
    trades: list[Trade] = []
    pending: Optional[int] = None
    pos: Optional[dict] = None
    i = max(trend_n - 1, start)
    limit = min(end, len(bs))
    while i < limit:
        bar = bs[i]
        state = btc_state.get(bar.ts)
        # BTC 门控只关闭该时刻的新信号（STRATEGY.md §2.5-§2.6）。已持仓头寸的
        # 止损/止盈/超时必须每个小时照常判定，否则保护单会被门控整段暂停，
        # 并把止损推迟到门控重开后才按从未成交的止损价记账。
        gated = state is None or state[0] <= state[1] or state[1] <= state[2]
        if gated:
            pending = None
        if pos is not None:
            hit_stop = bar.l <= pos["stop"]
            hit_target = bar.h >= pos["target"]
            if hit_stop or hit_target:
                if hit_stop:
                    # 跳空穿越止损时按开盘价成交（更差），不按未成交的止损价记账。
                    raw_exit = min(pos["stop"], bar.o)
                else:
                    raw_exit = pos["target"]
                px = exit_price(raw_exit, "buy", p["slippage"])
                gross = px - pos["entry"]
                net = gross - (pos["entry"] + px) * p["fee_rate"]
                trades.append(Trade(symbol, pos["signal_ts"], pos["entry_ts"], bar.ts, pos["entry"], px, pos["stop"], pos["target"], pos["risk"], net / pos["risk"], "stop" if hit_stop else "target"))
                pos = None
            elif i - pos["index"] >= p["max_hold_bars"]:
                px = exit_price(bar.c, "buy", p["slippage"])
                net = px - pos["entry"] - (pos["entry"] + px) * p["fee_rate"]
                trades.append(Trade(symbol, pos["signal_ts"], pos["entry_ts"], bar.ts, pos["entry"], px, pos["stop"], pos["target"], pos["risk"], net / pos["risk"], "timeout"))
                pos = None
            i += 1
            continue

        if gated:
            i += 1
            continue

        aligned = closes[i] > fast[i] > slow[i] > trend[i]
        atr_ok = aa[i] / max(closes[i], 1e-12) >= p["min_atr_pct"]
        if pending is not None:
            if i - pending > p["pullback_bars"] or not aligned:
                pending = None
            else:
                touched = bar.l <= fast[i] + p["pullback_atr"] * aa[i]
                if touched and i + 1 < limit:
                    # A first touch that closes below the fast EMA is a failed
                    # pullback; it must not be retried inside the same setup.
                    if bar.c > fast[i] and atr_ok:
                        raw_entry = bs[i + 1].o
                        entry = execution_price(raw_entry, "buy", p["slippage"])
                        risk = p["stop_atr"] * aa[i]
                        if risk > 0:
                            pos = {"signal_ts": bar.ts, "entry_ts": bs[i + 1].ts, "entry": entry, "risk": risk, "stop": entry - risk, "target": entry + p["target_r"] * risk, "index": i + 1}
                    pending = None
                    i += 1
                    continue

        lookback = bs[max(0, i - p["breakout_bars"]):i]
        prior_high = max(x.h for x in lookback)
        previous_spread = (max(fast[i - 1], slow[i - 1], trend[i - 1]) - min(fast[i - 1], slow[i - 1], trend[i - 1])) / max(aa[i - 1], 1e-12)
        breakout_strength = (bar.c - prior_high) / max(aa[i], 1e-12)
        spread_now = (max(fast[i], slow[i], trend[i]) - min(fast[i], slow[i], trend[i])) / max(aa[i], 1e-12)
        if previous_spread <= p["cluster_atr"] and bar.c > prior_high and aligned and atr_ok and breakout_strength >= p["min_breakout_atr"] and spread_now >= p["min_spread_atr"]:
            pending = i
        i += 1
    if pos is not None:
        # 窗口末端仍持仓：按本窗口最后一根收盘价标记离场（与 HLSR 的 window_end 口径
        # 一致）。此前直接丢弃，未结束的交易会从统计里消失；训练段用的是本窗口最后
        # 一根，不会用到测试段价格。
        last = bs[limit - 1]
        px = exit_price(last.c, "buy", p["slippage"])
        net = px - pos["entry"] - (pos["entry"] + px) * p["fee_rate"]
        trades.append(Trade(symbol, pos["signal_ts"], pos["entry_ts"], last.ts, pos["entry"], px, pos["stop"], pos["target"], pos["risk"], net / pos["risk"], "window_end"))
    return trades


def portfolio_filter(trades: list[Trade], max_concurrent: int, max_open_r: float, symbol_rank: Optional[dict[str, int]] = None) -> list[Trade]:
    """按"并发笔数 + 开放风险预算"确定性准入。

    仓位按固定风险比例缩放，每个入场订单的止损风险都等于同一份预算，因此一笔在
    持仓中的交易恰好占用 1R 的开放风险预算；`max_open_r` 就是
    `max_open_risk_pct / risk_per_trade_pct`（默认 3.0 / 0.5 = 6R），与
    `max_concurrent` 数值相同是设计结果，不是巧合。两者都保留：前者限制笔数，
    后者在风险比例被调小时自动收紧。
    """
    accepted: list[Trade] = []
    active: list[Trade] = []
    rank = symbol_rank or {}
    for trade in sorted(trades, key=lambda item: (item.entry_ts, rank.get(item.symbol, 10**9), item.symbol, item.exit_ts)):
        active = [item for item in active if item.exit_ts > trade.entry_ts]
        if len(active) >= max_concurrent or len(active) + 1 > max_open_r:
            continue
        active.append(trade)
        accepted.append(trade)
    return accepted


def stats(trades: list[Trade]) -> dict:
    ordered = sorted(trades, key=lambda item: (item.exit_ts, item.symbol))
    rs = [item.r for item in ordered]
    wins = [value for value in rs if value > 0]
    losses = [value for value in rs if value <= 0]
    equity = peak = drawdown = 0.0
    losing_streak = max_losing_streak = 0
    for value in rs:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        losing_streak = losing_streak + 1 if value <= 0 else 0
        max_losing_streak = max(max_losing_streak, losing_streak)
    hold_hours = [(item.exit_ts - item.entry_ts) / HOUR_MS for item in ordered]
    return {
        "trades": len(rs),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(rs), 4) if rs else 0,
        "total_r": round(sum(rs), 4),
        "avg_r": round(sum(rs) / len(rs), 4) if rs else 0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses and sum(losses) else None,
        "max_drawdown_r": round(drawdown, 4),
        "max_consecutive_losses": max_losing_streak,
        "avg_hold_hours": round(sum(hold_hours) / len(hold_hours), 2) if hold_hours else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the formal EMA altcoin long paper strategy")
    parser.add_argument("--max-symbols", type=int, default=0, help="training-period quote-volume cap (default: config value)")
    args = parser.parse_args()
    config = json.loads((LAB / "config/strategy.json").read_text())
    p = config["signal_parameters"] | {
        "fee_rate": config["costs"]["fee_rate"],
        "slippage": config["costs"]["slippage"],
        "leverage": config.get("portfolio", {}).get("leverage", 2.0),
    }
    files = [path for path in DATA.glob("*_USDT_SWAP_5m_*.jsonl.gz") if is_altcoin(base(path)) or base(path) == "BTC"]
    series = {base(path): load(path) for path in files}
    min_bars = p["ema_trend"] + 40
    series = {name: bars for name, bars in series.items() if len(bars) >= min_bars}
    if "BTC" not in series:
        raise SystemExit("BTC complete-hour series is required for the regime gate")
    btc = series["BTC"]
    first = btc[0].ts
    split = first + config["validation"]["train_days"] * 24 * HOUR_MS
    gate = config["regime_gate"]
    btc_slow = ema([bar.c for bar in btc], p["ema_slow"])
    slope_bars = int(gate["slope_bars"])
    min_history = int(gate["minimum_history_bars"])
    # 门控需要 BTC 自己的暖机：不足 minimum_history_bars 根时状态置 None（不通过），
    # 不能用"更早的 EMA 值等于当前值"这种假值让门控通过。
    warmup = max(slope_bars, min_history - 1)
    btc_state = {
        bar.ts: ((bar.c, btc_slow[i], btc_slow[i - slope_bars]) if i >= warmup else None)
        for i, bar in enumerate(btc)
    }
    max_symbols = args.max_symbols or config["universe"]["backtest_top_n"]
    symbols = sorted((name for name in series if is_altcoin(name)), key=lambda name: sum(bar.q for bar in series[name] if bar.ts < split), reverse=True)[:max_symbols]
    symbol_rank = {symbol: rank for rank, symbol in enumerate(symbols)}
    results: list[dict] = []
    detail: list[dict] = []
    for label, lower, upper in (("train", 0, split), ("test", split, None)):
        candidates: list[Trade] = []
        for symbol in symbols:
            bars = series[symbol]
            start = next((i for i, bar in enumerate(bars) if bar.ts >= lower), len(bars))
            end = next((i for i, bar in enumerate(bars) if upper is not None and bar.ts >= upper), len(bars)) if upper is not None else len(bars)
            candidates.extend(simulate(symbol, bars, p, start, end, btc_state))
        accepted = portfolio_filter(candidates, config["portfolio"]["max_concurrent_positions"], config["portfolio"]["max_open_risk_pct"] / config["portfolio"]["risk_per_trade_pct"], symbol_rank)
        summary = stats(accepted)
        summary.update({"split": label, "symbols": len(symbols), "raw_signals": len(candidates), "rejected_by_portfolio": len(candidates) - len(accepted)})
        results.append(summary)
        by_symbol: dict[str, list[Trade]] = {}
        for trade in accepted:
            by_symbol.setdefault(trade.symbol, []).append(trade)
        for symbol, trades in sorted(by_symbol.items()):
            symbol_stats = stats(trades)
            detail.append({"split": label, "symbol": symbol, **{key: symbol_stats[key] for key in ("trades", "wins", "win_rate", "total_r", "avg_r")}})
    generated = datetime.now(UTC).isoformat()
    payload = {"generated_at": generated, "data_start": datetime.fromtimestamp(first / 1000, UTC).isoformat(), "data_end": datetime.fromtimestamp(btc[-1].ts / 1000, UTC).isoformat(), "strategy_version": config["version"], "parameters": p, "universe": symbols, "summary": results}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    with (OUT / "summary.csv").open("w", newline="") as handle:
        fields = ["split", "symbols", "raw_signals", "rejected_by_portfolio", "trades", "wins", "win_rate", "total_r", "avg_r", "profit_factor", "max_drawdown_r", "max_consecutive_losses", "avg_hold_hours"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in results)
    with (OUT / "by_symbol.csv").open("w", newline="") as handle:
        fields = ["split", "symbol", "trades", "wins", "win_rate", "total_r", "avg_r"]
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(detail)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
