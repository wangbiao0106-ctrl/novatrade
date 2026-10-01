#!/usr/bin/env python3
"""Backtest configured UTC-day pump/retest shorts; preload candles once for all scans."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[3]
LAB = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / 'data/kline/okx/swap/5m'
BAR_MS = 900_000
CHILD_MS = 300_000
DAY_MS = 86_400_000


class Bar(NamedTuple):
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


class Pump(NamedTuple):
    index: int
    day_start: int
    qualified_index: int
    prior_day_high: float
    day_open: float
    atr: float


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


def read_bars(path: Path) -> tuple[list[Bar], dict]:
    """Read each selected gzip once and aggregate only 3 confirmed children."""
    raw = {}
    quality = Counter()
    with gzip.open(path, 'rt', encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get('confirmed') is not True:
                    quality['unconfirmed_rows'] += 1
                    continue
                ts = int(row['timestamp_ms'])
                o, h, lo, c, volume, qv = (float(row[k]) for k in
                                          ('open', 'high', 'low', 'close', 'volume', 'quote_volume'))
                if not all(math.isfinite(v) for v in (o, h, lo, c, volume, qv)):
                    raise ValueError('non-finite value')
                if ts % CHILD_MS or min(o, h, lo, c) <= 0 or volume < 0 or qv < 0 or h < max(o, c) or lo > min(o, c):
                    raise ValueError('invalid OHLCV')
                if ts in raw:
                    quality['duplicate_rows'] += 1
                raw[ts] = Bar(ts, o, h, lo, c, volume, qv)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                quality['invalid_or_missing_volume_rows'] += 1
    bars = []
    for bucket in sorted({ts // BAR_MS * BAR_MS for ts in raw}):
        timestamps = [bucket + step * CHILD_MS for step in range(3)]
        if not all(ts in raw for ts in timestamps):
            quality['incomplete_15m_buckets'] += 1
            continue
        children = [raw[ts] for ts in timestamps]
        bars.append(Bar(bucket, children[0].open, max(b.high for b in children),
                        min(b.low for b in children), children[-1].close,
                        sum(b.volume for b in children), sum(b.quote_volume for b in children)))
    quality['confirmed_5m_rows'] = len(raw)
    quality['complete_15m_bars'] = len(bars)
    return bars, dict(quality)


def load_all_series(data_dir: Path) -> tuple[dict[str, list[Bar]], list[dict]]:
    universe = json.loads((ROOT / 'strategies/sweep_reversal_short/config/universe.json').read_text())
    allowed, excluded = set(universe['altcoins']), set(universe['exclude'])
    grouped = defaultdict(list)
    for path in data_dir.glob('*_USDT_SWAP_5m_*.jsonl.gz'):
        symbol = path.name.split('_USDT_SWAP_5m_', 1)[0]
        if symbol in allowed and symbol not in excluded:
            grouped[symbol].append(path)
    series, sources = {}, []
    for symbol, paths in sorted(grouped.items()):
        path = sorted(paths, key=lambda p: p.name)[-1]
        bars, quality = read_bars(path)
        if bars:
            series[symbol] = bars
        sources.append({'symbol': symbol, 'file': str(path.resolve()), 'bytes': path.stat().st_size,
                        'quality': quality})
    return series, sources


def prepare(bars: list[Bar], config: dict) -> tuple[list[Pump], dict]:
    """Cache rare >=15% pumps and prior-day state once for every grid variant."""
    atr_n = config['risk']['atr_period']
    tr_window = []
    tr_sum = 0.0
    pumps = []
    counts = Counter()
    day_start = qualified = last_gap = 0
    day_open = prior_high = 0.0
    day_valid = False
    last_day = None
    atrs = [0.0] * len(bars)
    for i, b in enumerate(bars):
        pc = bars[i - 1].close if i else b.open
        tr = max(b.high - b.low, abs(b.high - pc), abs(b.low - pc))
        tr_window.append(tr)
        tr_sum += tr
        if i >= atr_n:
            tr_sum -= tr_window[i - atr_n]
        if i >= atr_n - 1:
            atrs[i] = tr_sum / atr_n
    for i, b in enumerate(bars):
        day = b.ts // DAY_MS
        gap = i > 0 and b.ts - bars[i - 1].ts != BAR_MS
        if gap:
            last_gap = i
        if day != last_day:
            day_start, qualified = i, -1
            day_open, prior_high = b.open, 0.0
            day_valid = b.ts % DAY_MS == 0
            last_day = day
        elif gap:
            day_valid = False
        if day_valid:
            counts['complete_day_bars'] += 1
            if max(prior_high, b.high) / day_open - 1 > config['observation_filter']['intraday_gain_gt']:
                counts['observation_bars_above_anchor'] += 1
                # Keep the legacy key so existing >60% reports remain easy to compare.
                counts['observation_bars_gt60'] += 1
            # Record before consuming this pump. The >60% test belongs to the
            # prior day-high anchor, not to the first bar of the decline.
            if (qualified >= 0 and i > 0 and b.close > b.open
                    and b.close / bars[i - 1].close - 1 >= config['signal_parameters']['pump_close_gain_min']):
                counts['pump_bars_after_anchor'] += 1
                counts['pump_bars_after_gt60'] += 1
                q = i + 1
                if (q < len(bars) and bars[q].ts == b.ts + BAR_MS
                        and bars[q].ts // DAY_MS == day and q >= atr_n
                        and last_gap <= q - atr_n):
                    pumps.append(Pump(i, day_start, qualified, prior_high, day_open, atrs[q]))
                else:
                    counts['confirmation_history_rejected'] += 1
            prior_high = max(prior_high, b.high)
            if qualified < 0 and prior_high / day_open - 1 > config['observation_filter']['intraday_gain_gt']:
                qualified = i
    counts['cached_pumps'] = len(pumps)
    return pumps, dict(counts)


def pattern_features(symbol: str, bars: list[Bar], pump: Pump, params: dict,
                     observation_gain_gt: float = 0.6) -> dict:
    i, n = pump.index, params['down_bars']
    p, q = bars[i], bars[i + 1]
    start = i - n
    # The anchor is evaluated from all bars before the decline. We do not
    # require the decline to begin on the first bar that crossed +60%.
    pre_qualified = start > pump.day_start
    prior = bars[max(0, start):i]
    net = prior[-1].close / prior[0].open - 1 if len(prior) == n else 0.0
    negative = sum(b.close < b.open for b in prior) / n
    body = max((abs(b.close / b.open - 1) for b in prior), default=0.0)
    spread = max(b.high for b in prior) / min(b.low for b in prior) - 1 if prior else 0.0
    down_ok = (pre_qualified and params['down_net_min'] <= -net <= params['down_net_max']
               and negative >= params['down_negative_fraction_min']
               and body <= params['down_bar_body_max'] and spread <= params['down_range_max'])
    prior_window = bars[pump.day_start:start] if start > pump.day_start else []
    anchor_high = max((b.high for b in prior_window), default=pump.prior_day_high)
    anchor_gain = anchor_high / pump.day_open - 1 if pump.day_open > 0 else 0.0
    high = max(anchor_high, p.high, q.high)
    vr = q.volume / p.volume if p.volume > 0 else None
    # "Near the high" refers to Q's own close location, not its absolute
    # distance from the day's high. P is the bar that must retest the day high.
    distance = 1 - q.close / q.high if q.high > 0 else 1.0
    touch_ok = anchor_gain > 0.6 and p.high >= anchor_high * (1 - params['day_high_touch_tolerance'])
    volume_ok = vr is not None and vr <= params['confirmation_volume_ratio_max']
    close_ok = distance <= params['confirmation_close_to_own_high_max']
    pump_gain = p.close / bars[i - 1].close - 1
    pump_ok = pump_gain > params['pump_close_gain_min']
    return {
        'symbol': symbol, 'pump_index': i, 'pump_ts': p.ts, 'confirmation_ts': q.ts,
        'qualified_ts': bars[pump.qualified_index].ts,
        'gain_before_sequence': anchor_gain,
        'prequalified': pre_qualified and anchor_gain > observation_gain_gt,
        'anchor_high_before_sequence': anchor_high,
        'down_net': net, 'down_negative_fraction': negative,
        'down_max_body': body, 'down_range': spread, 'down_ok': down_ok,
        'pump_gain': pump_gain, 'pump_ok': pump_ok, 'prior_day_high': pump.prior_day_high,
        'pump_high': p.high, 'confirmation_high': q.high, 'anchor_high': high,
        'touch_ok': touch_ok, 'pump_volume': p.volume, 'confirmation_volume': q.volume,
        'volume_ratio': vr, 'volume_ok': volume_ok, 'close_distance': distance,
        'close_ok': close_ok, 'atr': pump.atr,
        'signal': down_ok and pump_ok and touch_ok and volume_ok and close_ok,
    }


def simulate(sample: dict, bars: list[Bar], config: dict) -> tuple[dict | None, str | None]:
    r, costs = config['risk'], config['costs']
    slip, fee = costs['slippage_one_way'], costs['fee_rate_one_way']
    index = sample['pump_index'] + 2
    if index >= len(bars) or bars[index].ts != sample['confirmation_ts'] + BAR_MS:
        return None, 'no_next_contiguous_entry'
    entry = bars[index].open * (1 - slip)
    stop = sample['anchor_high'] + r['stop_atr'] * sample['atr']
    risk = stop - entry
    if sample['atr'] <= 0 or not r['min_risk_atr'] <= risk / sample['atr'] <= r['max_risk_atr']:
        return None, 'stop_distance_rejected'
    target = entry - r['target_r'] * risk
    if target <= 0:
        return None, 'invalid_target'
    last = min(len(bars) - 1, index + r['max_hold_bars'] - 1)
    reason = 'window_end' if last == len(bars) - 1 else 'timeout'
    ex, exit_index, exit_ts = bars[last].close, last, bars[last].ts + BAR_MS
    adverse = entry
    for j in range(index, last + 1):
        b = bars[j]
        if j > index and b.ts != bars[j - 1].ts + BAR_MS:
            ex, exit_index, exit_ts, reason = bars[j - 1].close, j - 1, bars[j - 1].ts + BAR_MS, 'data_gap'
            break
        if b.open >= stop:
            adverse = max(adverse, b.open)
            ex, exit_index, exit_ts, reason = b.open, j, b.ts, 'stop_gap'
            break
        # Conservative adverse intrabar excursion before the exit. A target
        # fill's candle high is an upper bound because OHLC order is unknown.
        adverse = max(adverse, min(b.high, stop))
        if b.high >= stop:
            ex, exit_index, exit_ts, reason = stop, j, b.ts + BAR_MS, 'stop'
            break
        if b.low <= target:
            ex, exit_index, exit_ts, reason = target, j, b.ts + BAR_MS, 'target'
            break
    effective_exit = ex * (1 + slip)
    net_per_unit = entry - effective_exit - fee * (entry + effective_exit) - costs['funding_rate_round_trip'] * entry
    notional_return = net_per_unit / entry
    adverse_return = (entry - adverse * (1 + slip) - fee * (entry + adverse * (1 + slip))) / entry
    return {
        'symbol': sample['symbol'], 'pump_ts': sample['pump_ts'], 'signal_ts': sample['confirmation_ts'] + BAR_MS,
        'entry_ts': bars[index].ts, 'exit_ts': exit_ts, 'entry': entry, 'exit': effective_exit,
        'stop': stop, 'target': target, 'risk': risk, 'net_r': net_per_unit / risk,
        'notional_return': notional_return, 'margin_return_2x_pct': notional_return * config['position_management']['leverage'] * 100,
        'adverse_notional_return': adverse_return, 'exit_reason': reason, 'bars_held': exit_index - index + 1,
    }, None


def trade_metrics(trades: list[dict]) -> dict:
    values = [t['net_r'] for t in trades]
    gains, losses = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    equity = peak = drawdown = 0.0
    for t in sorted(trades, key=lambda t: (t['exit_ts'], t['symbol'])):
        equity += t['net_r']
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {'trades': len(trades), 'wins': sum(v > 0 for v in values),
            'win_rate': sum(v > 0 for v in values) / len(values) if values else 0,
            'total_r': sum(values), 'avg_net_r': statistics.fmean(values) if values else 0,
            'profit_factor': gains / losses if losses else None,
            'max_drawdown_r': drawdown, 'symbols': len({t['symbol'] for t in trades}),
            'avg_margin_return_2x_pct': statistics.fmean(t['margin_return_2x_pct'] for t in trades) if trades else 0,
            'exit_reasons': dict(Counter(t['exit_reason'] for t in trades))}


def portfolio_filter(trades: list[dict], config: dict) -> tuple[list[dict], dict]:
    """A single position, risk sizing, 2x notional cap and realized daily stop."""
    pm, r = config['position_management'], config['risk']
    equity = initial = pm['initial_equity']
    peak = adverse_peak = equity
    closed_dd = adverse_dd = 0.0
    active_until = -1
    symbol_cooldown = {}
    day = None
    day_start_equity = equity
    accepted, skipped = [], Counter()
    for t in sorted(trades, key=lambda t: (t['entry_ts'], t['symbol'])):
        if t['entry_ts'] < active_until:
            skipped['portfolio_overlap'] += 1
            continue
        if t['entry_ts'] <= symbol_cooldown.get(t['symbol'], -1):
            skipped['symbol_cooldown'] += 1
            continue
        current_day = t['entry_ts'] // DAY_MS
        if current_day != day:
            day, day_start_equity = current_day, equity
        if equity <= 0 or equity <= day_start_equity * (1 - pm['daily_realized_loss_limit_pct'] / 100):
            skipped['daily_loss_or_insolvent'] += 1
            continue
        distance = t['risk'] / t['entry']
        notional = min(equity * pm['leverage'], equity * pm['risk_per_trade_pct'] / 100 / distance)
        pnl = notional * t['notional_return']
        worst_equity = equity + notional * t['adverse_notional_return']
        adverse_dd = max(adverse_dd, 1 - worst_equity / adverse_peak)
        accepted.append(dict(t, equity_before=equity, notional=notional, margin=notional / pm['leverage'],
                             pnl_usdt=pnl, equity_after=equity + pnl))
        equity += pnl
        peak = max(peak, equity)
        adverse_peak = max(adverse_peak, equity)
        closed_dd = max(closed_dd, 1 - equity / peak)
        active_until = t['exit_ts']
        symbol_cooldown[t['symbol']] = (t['exit_ts'] - 1) // BAR_MS * BAR_MS + r['cooldown_bars'] * BAR_MS
    return accepted, {'initial_equity': initial, 'final_equity': equity,
                      'return_pct': (equity / initial - 1) * 100,
                      'max_realized_drawdown_pct': closed_dd * 100,
                      'conservative_adverse_drawdown_pct': max(adverse_dd, closed_dd) * 100,
                      'leverage': pm['leverage'], 'risk_per_trade_pct': pm['risk_per_trade_pct'],
                      'skipped': dict(skipped)}


def evaluate(series: dict[str, list[Bar]], prepared: dict[str, list[Pump]], config: dict):
    samples, candidates = [], []
    stages, rejected = Counter(), Counter()
    for symbol, bars in series.items():
        for pump in prepared[symbol]:
            s = pattern_features(symbol, bars, pump, config['signal_parameters'],
                                 config['observation_filter']['intraday_gain_gt'])
            samples.append(s)
            stages['cached_pumps'] += 1
            if not s['prequalified']:
                continue
            stages['qualified_before_decline'] += 1
            if not s['down_ok']:
                continue
            stages['decline_then_pump'] += 1
            if not s['pump_ok']:
                continue
            stages['pump_gain_threshold'] += 1
            if not s['touch_ok']:
                continue
            stages['touch_day_high'] += 1
            if not s['volume_ok']:
                continue
            stages['low_volume_confirmation'] += 1
            if not s['close_ok']:
                continue
            stages['full_signals'] += 1
            t, reason = simulate(s, bars, config)
            if t is None:
                rejected[reason] += 1
            else:
                candidates.append(t)
    admitted, portfolio = portfolio_filter(candidates, config)
    monthly = {}
    for month in sorted({iso(t['entry_ts'])[:7] for t in admitted}):
        subset = [t for t in admitted if iso(t['entry_ts'])[:7] == month]
        monthly[month] = dict(trade_metrics(subset), pnl_usdt=sum(t['pnl_usdt'] for t in subset))
    return samples, admitted, {'stages': dict(stages), 'execution_rejected': dict(rejected),
                               'unconstrained_candidates': trade_metrics(candidates),
                               'portfolio_trades': trade_metrics(admitted), 'portfolio': portfolio,
                               'by_month_utc': monthly}


def write_csv(path: Path, rows: list[dict]):
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def run(data_dir: Path, config_path: Path, output: Path, scan: bool = False):
    config = json.loads(config_path.read_text())
    started = time.monotonic()
    print('Loading the entire eligible candle universe into memory once...', flush=True)
    series, sources = load_all_series(data_dir)
    loaded = time.monotonic()
    print(f'Loaded {len(series)} symbols / {sum(len(b) for b in series.values())} 15m bars; no more market-data file reads.', flush=True)
    prepared, quality = {}, Counter()
    for symbol, bars in series.items():
        prepared[symbol], counts = prepare(bars, config)
        quality.update(counts)
    samples, trades, summary = evaluate(series, prepared, config)
    sensitivity = []
    threshold_comparison = []
    if scan:
        grid = config['research']['grid']
        keys = list(grid)
        for values in itertools.product(*(grid[k] for k in keys)):
            variant = dict(config, signal_parameters=dict(config['signal_parameters'], **dict(zip(keys, values))))
            _, _, stats = evaluate(series, prepared, variant)
            sensitivity.append(dict(zip(keys, values), **stats['portfolio_trades'],
                                    account_return_pct=stats['portfolio']['return_pct'],
                                    full_signals=stats['stages'].get('full_signals', 0)))
        threshold_comparison = [row for row in sensitivity
                                if row['down_bars'] == config['signal_parameters']['down_bars']
                                and row['confirmation_volume_ratio_max'] == config['signal_parameters']['confirmation_volume_ratio_max']
                                and row['confirmation_close_to_own_high_max'] == config['signal_parameters']['confirmation_close_to_own_high_max']]
    output.mkdir(parents=True, exist_ok=True)
    report = {
        'strategy': config['strategy'], 'version': config['version'], 'status': 'research_only',
        'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
        'parameters': config,
        'data': {'symbols': len(series), 'complete_15m_bars': sum(len(b) for b in series.values()),
                 'start_utc': iso(min(b[0].ts for b in series.values())) if series else None,
                 'end_utc': iso(max(b[-1].ts + BAR_MS for b in series.values())) if series else None,
                 'load_seconds': loaded - started, 'market_data_read_passes': 1,
                 'quality': dict(quality), 'source_files': sources},
        **summary,
        'sensitivity': {'variants': len(sensitivity), 'uses_same_in_memory_data': True,
                        'default_parameters_preserved': True, 'selection': 'descriptive_only',
                        'threshold_comparison': threshold_comparison},
        'validation': {'minimum_trades': config['research']['minimum_validation_trades'],
                       'sample_size_met': len(trades) >= config['research']['minimum_validation_trades'],
                       'independent_out_of_sample_completed': False, 'passed': False},
        'limitations': ['current eligible universe has survivorship bias', 'funding assumed zero; liquidation not modeled',
                       'only completed candles; OHLC intrabar order unknown, stop first',
                       '2x margin return is per-position, not account return',
                       'no independent out-of-sample evidence; sensitivity is descriptive'],
    }
    write_csv(output / 'samples.csv', samples)
    write_csv(output / 'trades.csv', trades)
    if scan:
        write_csv(output / 'sensitivity.csv', sensitivity)
        write_csv(output / 'threshold_comparison.csv', threshold_comparison)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k: report[k] for k in ['stages', 'execution_rejected', 'portfolio_trades', 'portfolio', 'validation']}, ensure_ascii=False, indent=2))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    p.add_argument('--config', type=Path, default=LAB / 'config/strategy.json')
    p.add_argument('--output-dir', type=Path, default=LAB / 'results')
    p.add_argument('--scan', action='store_true', help='Run all 72 predefined sensitivity variants on the same in-memory candles.')
    args = p.parse_args()
    run(args.data_dir, args.config, args.output_dir, args.scan)


if __name__ == '__main__':
    main()
