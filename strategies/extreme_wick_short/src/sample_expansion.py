#!/usr/bin/env python3
"""Expand >100% 15m exhaustion samples, loading all candles once before scans."""
from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import momentum_exhaustion as base
from extreme_wick_short import aggregate_15m, choose_paths, excluded_symbols, load_bars

LAB = Path(__file__).resolve().parents[1]
ROOT = LAB.parents[1]
MS = base.FIFTEEN_MINUTES
DAY = 96 * MS


@dataclass(frozen=True)
class Outcome:
    symbol: str
    signal_index: int
    signal_ts: int
    entry_ts: int
    exit_ts: int
    exit_index: int
    entry: float
    exit: float
    stop: float
    target: float
    net_r: float
    notional_return: float
    margin_return_2x_pct: float
    adverse_return: float
    reason: str


def iso(ts):
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


def report_path(path: Path) -> str:
    """Path as written into committed reports: relative to the repository
    root, never an absolute path that leaks the local home directory. Data
    outside the repository (e.g. a symlinked checkout) keeps only its name."""
    for candidate in (path.absolute(), path.resolve()):
        try:
            return candidate.relative_to(ROOT).as_posix()
        except ValueError:
            continue
    return path.name


def load_once(data_dir: Path, config: dict):
    universe_path = ROOT / 'strategies/sweep_reversal_short/config/universe.json'
    universe = json.loads(universe_path.read_text())
    allowed = set(universe['altcoins'])
    excluded = excluded_symbols(base.DEFAULT_CONFIG, config)
    series, sources = {}, []
    for symbol, path in choose_paths(data_dir, excluded, allowed):
        bars = aggregate_15m(load_bars(path))
        if len(bars) > 96:
            series[symbol] = bars
        sources.append({'symbol': symbol, 'path': report_path(path), 'bytes': path.stat().st_size,
                        'modified_ns': path.stat().st_mtime_ns, 'complete_15m_bars': len(bars)})
    return series, sources


def features(symbol, bars, config, minimum_history=96):
    params = config['signal_parameters']
    rsi = [None] * len(bars)
    segment_start = 0
    # Reset smoothing after gaps. Once 96 confirmed bars exist the entire
    # rolling-24h denominator and indicator windows are contiguous again.
    for i in range(1, len(bars) + 1):
        if i == len(bars) or bars[i].ts != bars[i - 1].ts + MS:
            rsi[segment_start:i] = base.wilder_rsi([b.close for b in bars[segment_start:i]], params['rsi_period'])
            segment_start = i
    atr = base.atr_values(bars, params['atr_period'])
    volume = base.simple_average([b.quote_volume for b in bars], params['volume_period'])
    result = []
    segment_start = 0
    for i, b in enumerate(bars[:-1]):
        if i and b.ts != bars[i - 1].ts + MS:
            segment_start = i
        if i - segment_start < minimum_history:
            continue
        gain = b.high / bars[i - 96].close - 1
        if gain <= 1.0:
            continue
        if rsi[i] is None or rsi[i - 1] is None or atr[i] <= 0:
            continue
        span = b.high - b.low
        if span <= 0:
            continue
        prior_high = max(x.high for x in bars[i - params['breakout_lookback_bars']:i])
        result.append(base.Event(symbol, i, b.ts, gain, 0.0, rsi[i], rsi[i] - rsi[i - 1],
                                 (b.high - max(b.open, b.close)) / span, (b.close - b.low) / span,
                                 b.quote_volume / volume[i] if volume[i] > 0 else 0.0,
                                 b.high / prior_high - 1, atr[i] / b.close, b.high, atr[i]))
    return result


def matches(event, bars, params):
    return (bars[event.index].close < bars[event.index].open
            and event.upper_wick_ratio >= params['upper_wick_min']
            and event.close_position <= params['close_position_max']
            and event.rsi >= params['rsi_min']
            and (not params['rsi_must_fall'] or event.rsi_delta < 0)
            and event.volume_ratio >= params['volume_multiple']
            and (params['breakout_min_pct'] is None or event.breakout_pct >= params['breakout_min_pct']))


def simulate(event, bars, config, params, boundary=None):
    risk_config, costs = config['risk'], config['costs']
    start = event.index + 1
    if start >= len(bars) or bars[start].ts != bars[event.index].ts + MS:
        return None, 'entry_gap'
    if boundary is not None and bars[start].ts >= boundary:
        return None, 'outside_window'
    entry = bars[start].open * (1 - costs['slippage_one_way'])
    stop = event.anchor_high + params['stop_atr'] * event.atr
    distance = stop - entry
    if not risk_config['min_risk_atr'] <= distance / event.atr <= risk_config['max_risk_atr']:
        return None, 'risk_distance'
    target = entry - params['target_r'] * distance
    if target <= 0:
        return None, 'invalid_target'
    last = min(len(bars) - 1, start + risk_config['max_hold_bars'] - 1)
    reason = 'data_end' if last == len(bars) - 1 else 'timeout'
    if boundary is not None and bars[last].ts >= boundary:
        # Never use a validation candle to settle a training position.
        last = next(j for j in range(start, last + 1) if bars[j].ts >= boundary) - 1
        reason = 'split_end'
    exit_price, exit_index, exit_ts = bars[last].close, last, bars[last].ts + MS
    worst_price = entry
    for j in range(start, last + 1):
        b = bars[j]
        if j > start and b.ts != bars[j - 1].ts + MS:
            exit_price, exit_index, exit_ts, reason = bars[j - 1].close, j - 1, bars[j - 1].ts + MS, 'data_gap'
            break
        if b.open >= stop:
            worst_price = max(worst_price, b.open)
            exit_price, exit_index, exit_ts, reason = b.open, j, b.ts, 'stop_gap'
            break
        worst_price = max(worst_price, min(b.high, stop))
        if b.high >= stop:
            exit_price, exit_index, exit_ts, reason = stop, j, b.ts + MS, 'stop'
            break
        if b.low <= target:
            exit_price, exit_index, exit_ts, reason = target, j, b.ts + MS, 'target'
            break
    exit_price *= 1 + costs['slippage_one_way']
    net = entry - exit_price - costs['fee_rate_one_way'] * (entry + exit_price) - entry * costs['funding_rate_round_trip']
    adverse_exit = worst_price * (1 + costs['slippage_one_way'])
    adverse = (entry - adverse_exit - costs['fee_rate_one_way'] * (entry + adverse_exit)) / entry
    return Outcome(event.symbol, event.index, event.timestamp + MS, bars[start].ts, exit_ts, exit_index,
                   entry, exit_price, stop, target, net / distance, net / entry,
                   net / entry * config['position_management']['leverage'] * 100, adverse, reason), None


def metrics(trades):
    ordered = sorted(trades, key=lambda x: (x.exit_ts, x.symbol, x.entry_ts))
    values = [x.net_r for x in ordered]
    gain, loss = sum(v for v in values if v > 0), -sum(v for v in values if v < 0)
    equity = peak = dd = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    episodes = 0
    last_event = {}
    for t in sorted(trades, key=lambda x: (x.signal_ts, x.symbol)):
        if t.signal_ts - last_event.get(t.symbol, -10**18) >= DAY:
            episodes += 1
        last_event[t.symbol] = t.signal_ts
    return {'trades': len(values), 'wins': sum(v > 0 for v in values),
            'win_rate': sum(v > 0 for v in values) / len(values) if values else None,
            'total_r': sum(values), 'avg_net_r': statistics.fmean(values) if values else None,
            'profit_factor': gain / loss if loss else None, 'max_drawdown_r': dd,
            'symbols': len({t.symbol for t in trades}), 'pump_episodes_24h': episodes,
            'avg_margin_return_2x_pct': statistics.fmean(t.margin_return_2x_pct for t in trades) if trades else None}


def per_symbol_filter(trades, cooldown):
    result, next_allowed = [], {}
    for t in sorted(trades, key=lambda x: (x.entry_ts, x.symbol)):
        if t.signal_index <= next_allowed.get(t.symbol, -1):
            continue
        result.append(t)
        next_allowed[t.symbol] = t.exit_index + cooldown
    return result


def portfolio(trades, config):
    pm, risk_config = config['position_management'], config['risk']
    equity = peak = 10000.0
    dd = adverse_dd = 0.0
    active_until = -1
    next_allowed = {}
    admitted, rows = [], []
    counts = Counter()
    current_day, daily_equity = None, equity
    for t in sorted(trades, key=lambda x: (x.entry_ts, x.symbol)):
        if t.entry_ts < active_until:
            counts['global_concurrency'] += 1
            continue
        if t.signal_index <= next_allowed.get(t.symbol, -1):
            counts['symbol_cooldown'] += 1
            continue
        day = t.entry_ts // DAY
        if day != current_day:
            current_day, daily_equity = day, equity
        if equity <= daily_equity * (1 - pm['account_daily_loss_limit_pct'] / 100):
            counts['realized_daily_loss_gate'] += 1
            continue
        notional = min(equity * pm['leverage'], equity * pm['risk_per_trade_pct'] / 100 * t.entry / (t.stop - t.entry))
        pnl = notional * t.notional_return
        adverse_dd = max(adverse_dd, 1 - (equity + notional * t.adverse_return) / peak)
        rows.append(dict(asdict(t), notional_usdt=notional, margin_usdt=notional / pm['leverage'],
                         equity_before=equity, pnl_usdt=pnl, equity_after=equity + pnl))
        admitted.append(t)
        equity += pnl
        peak = max(peak, equity)
        dd = max(dd, 1 - equity / peak)
        active_until = t.exit_ts
        next_allowed[t.symbol] = t.exit_index + risk_config['cooldown_bars']
    return admitted, rows, {'initial_equity_usdt': 10000.0, 'final_equity_usdt': equity,
                            'account_return_pct': (equity / 10000 - 1) * 100,
                            'max_realized_drawdown_pct': dd * 100,
                            'conservative_adverse_drawdown_pct': max(dd, adverse_dd) * 100,
                            'leverage': pm['leverage'], 'admission_rejected': dict(counts)}


def evaluate(series, events, params, config, split, cache, phase='full'):
    signals = 0
    raw = []
    rejected = Counter()
    for symbol, ee in events.items():
        bars = series[symbol]
        for e in ee:
            if phase == 'train' and e.timestamp + MS >= split:
                continue
            if phase == 'validation' and e.timestamp + MS < split:
                continue
            if not matches(e, bars, params):
                continue
            signals += 1
            key = (symbol, e.index, params['stop_atr'], params['target_r'], 'train' if phase == 'train' else 'full')
            if key not in cache:
                cache[key] = simulate(e, bars, config, params, split if phase == 'train' else None)
            t, reason = cache[key]
            if t is None:
                rejected[reason] += 1
            else:
                raw.append(t)
    single = per_symbol_filter(raw, config['risk']['cooldown_bars'])
    admitted, rows, account = portfolio(raw, config)
    return {'signals': signals, 'risk_rejected': dict(rejected), 'per_symbol': metrics(single),
            'portfolio': metrics(admitted), 'account': account}, rows, admitted, single


def flatten(name, params, summary):
    return dict(name=name, **params, signals=summary['signals'], **summary['portfolio'],
                account_return_pct=summary['account']['account_return_pct'],
                account_drawdown_pct=summary['account']['max_realized_drawdown_pct'])


def run(data_dir, config_path, experiment_path, output):
    config, experiment = json.loads(config_path.read_text()), json.loads(experiment_path.read_text())
    if config['observation_filter']['intraday_gain_gt'] != 1.0 or config['position_management']['leverage'] != 2.0:
        raise ValueError('This experiment requires the original strict >100% gate and 2x leverage.')
    output.mkdir(parents=True, exist_ok=True)
    split = int(datetime.fromisoformat(experiment['split_utc']).timestamp() * 1000)
    started = time.monotonic()
    print('Loading all market files once, then reusing candles/features/outcomes in memory.', flush=True)
    series, sources = load_once(data_dir, config)
    print(f'Loaded {len(series)} symbols, {sum(len(b) for b in series.values())} 15m bars in {time.monotonic()-started:.1f}s.', flush=True)
    events = {s: features(s, bars, config) for s, bars in series.items()}
    print(f'Prepared {sum(len(e) for e in events.values())} strictly >100% observations; scanning in memory.', flush=True)
    original = dict(config['signal_parameters'], stop_atr=config['risk']['stop_atr'], target_r=config['risk']['target_r'])
    cache = {}
    # Reproduce the previous script exactly, without reading any price file again.
    legacy_events = {s: base.build_events(s, bars, config) for s, bars in series.items() if len(bars) >= 2940}
    legacy_trades = []
    for s, ee in legacy_events.items():
        bars, next_allowed = series[s], -1
        for e in ee:
            if e.index <= next_allowed or not base.qualifies(e, bars, config):
                continue
            distance = e.anchor_high + original['stop_atr'] * e.atr - bars[e.index + 1].open * (1-config['costs']['slippage_one_way'])
            if not config['risk']['min_risk_atr'] <= distance / e.atr <= config['risk']['max_risk_atr']:
                continue
            last = min(len(bars)-1, e.index+config['risk']['max_hold_bars'])
            if any(bars[i].ts != bars[i-1].ts+MS for i in range(e.index+1, last+1)):
                continue
            t = base.simulate(e, bars, config)
            legacy_trades.append(t)
            next_allowed = e.index + t.bars_held + config['risk']['cooldown_bars']
    legacy_stats = base.metrics(legacy_trades, 2.0)
    comparisons = []
    cases = [('same_rules_shorter_warmup', original)]
    for key, value in [('breakout_min_pct', None), ('volume_multiple', 0.0), ('rsi_min', 55.0),
                       ('upper_wick_min', 0.2), ('close_position_max', 0.7)]:
        cases.append(('only_relax_' + key, dict(original, **{key: value})))
    for name, params in cases:
        summary, _, _, _ = evaluate(series, events, params, config, split, cache)
        comparisons.append(dict(name=name, parameters=params, **summary))
    grid_rows, eligible, grid_specs = [], [], {}
    grid = experiment['grid']
    keys = list(grid)
    select = experiment['selection']
    for index, values in enumerate(itertools.product(*(grid[k] for k in keys))):
        params = dict(original, **dict(zip(keys, values)))
        name = f'grid_{index:04d}'
        grid_specs[name] = params
        train, _, _, _ = evaluate(series, events, params, config, split, cache, 'train')
        validation, _, _, _ = evaluate(series, events, params, config, split, cache, 'validation')
        full, _, _, _ = evaluate(series, events, params, config, split, cache)
        row = flatten(name, params, full)
        for phase, summary in [('train', train), ('validation', validation)]:
            row.update({phase + '_' + k: v for k, v in summary['portfolio'].items()})
            row[phase + '_account_return_pct'] = summary['account']['account_return_pct']
        grid_rows.append(row)
        m = train['portfolio']
        qualifies = (m['trades'] >= select['minimum_train_trades'] and m['win_rate'] >= select['minimum_train_win_rate']
                     and m['profit_factor'] is not None and m['profit_factor'] >= select['minimum_train_profit_factor']
                     and m['total_r'] > 0)
        if qualifies:
            eligible.append((m['trades'], m['avg_net_r'], -index, name))
    selected_name = max(eligible)[3] if eligible else None
    # Also publish the widest sample variant to show the cost of more trades.
    widest = max(grid_rows, key=lambda row: (row['trades'], row['total_r'], row['name']))['name']
    robust_select = select.get('robust_validation', {})
    robust_eligible = []
    if robust_select:
        for row in grid_rows:
            train_ok = (row['train_trades'] >= robust_select['minimum_train_trades']
                        and row['train_win_rate'] >= robust_select['minimum_train_win_rate']
                        and row['train_profit_factor'] is not None
                        and row['train_profit_factor'] >= robust_select['minimum_train_profit_factor'])
            validation_ok = (row['validation_trades'] >= robust_select['minimum_validation_trades']
                             and row['validation_win_rate'] >= robust_select['minimum_validation_win_rate']
                             and row['validation_profit_factor'] is not None
                             and row['validation_profit_factor'] >= robust_select['minimum_validation_profit_factor']
                             and row['validation_total_r'] > robust_select['minimum_validation_total_r'])
            if train_ok and validation_ok:
                robust_eligible.append((row['total_r'], row['trades'], row['validation_total_r'], row['name']))
    robust_name = max(robust_eligible)[3] if robust_eligible else None
    full_period_max = max(grid_rows, key=lambda row: (row['total_r'], row['trades'], row['name']))['name']
    candidate_names = list(dict.fromkeys(([selected_name] if selected_name else [])
                                         + ([robust_name] if robust_name else []) + [full_period_max, widest]))
    candidates = {}
    for name in candidate_names:
        params = grid_specs[name]
        full, rows, admitted, single = evaluate(series, events, params, config, split, cache)
        train, _, _, _ = evaluate(series, events, params, config, split, cache, 'train')
        validation, _, _, _ = evaluate(series, events, params, config, split, cache, 'validation')
        by_symbol = {s: metrics([t for t in admitted if t.symbol == s]) for s in sorted({t.symbol for t in admitted})}
        by_month = {month: metrics([t for t in admitted if iso(t.entry_ts)[:7] == month])
                    for month in sorted({iso(t.entry_ts)[:7] for t in admitted})}
        without_symbol = {s: metrics([t for t in admitted if t.symbol != s]) for s in by_symbol}
        candidates[name] = dict(parameters=params, full=full, train=train, validation=validation,
                                by_symbol=by_symbol, by_month_utc=by_month, leave_one_symbol_out=without_symbol)
        base.write_csv(output / (name + '_trades.csv'), rows)
        base.write_csv(output / (name + '_per_symbol_trades.csv'), [asdict(t) for t in single])
        candidate_config = copy.deepcopy(config)
        candidate_config['signal_parameters'] = {k: v for k, v in params.items() if k not in ('stop_atr', 'target_r')}
        candidate_config['risk'].update(stop_atr=params['stop_atr'], target_r=params['target_r'])
        candidate_config.update(status='research_experiment', experiment_id=name,
                                research_preparation={'minimum_contiguous_history_bars': 96})
        (output / (name + '_config.json')).write_text(json.dumps(candidate_config, ensure_ascii=False, indent=2)+'\n')
    base.write_csv(output / 'grid.csv', grid_rows)
    base.write_csv(output / 'observations.csv', [asdict(e) for ee in events.values() for e in ee])
    report = {
        'experiment': experiment['experiment'], 'data': {'symbols': len(series), 'bars': sum(len(b) for b in series.values()),
        'start_utc': iso(min(b[0].ts for b in series.values())), 'end_utc': iso(max(b[-1].ts+MS for b in series.values())),
        'observations': sum(len(e) for e in events.values()), 'market_data_read_passes': 1, 'sources': sources},
        'baseline_config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
        'experiment_config': experiment, 'legacy_reproduction': legacy_stats, 'comparisons': comparisons,
        'grid_variants': len(grid_rows), 'eligible_train_variants': len(eligible), 'selected_by_train_only': selected_name,
        'robust_validation_variants': len(robust_eligible),
        'robust_validation_descriptive_only': robust_name,
        'selected_by_full_period_total_r_with_validation_constraints': robust_name,
        'full_period_max_total_r_descriptive_only': full_period_max,
        'widest_sample_descriptive_only': widest, 'candidates': candidates,
        'validation': {'chronological_split_utc': experiment['split_utc'], 'fresh_out_of_sample': False,
                       'reason': 'Previously explored history; retrospective validation only.', 'passed': False},
        'limitations': ['survivorship bias in current universe', 'funding zero, liquidation not modeled',
                       'conservative adverse drawdown uses OHLC bounds; no intrabar ordering',
                       'independent 24h episodes may be much fewer than trades',
                       'legacy summed 2x percentages are not account returns; use risk-sized account_return_pct'],
    }
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({k: report[k] for k in ['legacy_reproduction', 'comparisons', 'grid_variants', 'eligible_train_variants',
                                          'selected_by_train_only', 'widest_sample_descriptive_only', 'candidates']}, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, default=base.DEFAULT_DATA)
    p.add_argument('--config', type=Path, default=base.DEFAULT_CONFIG)
    p.add_argument('--experiment', type=Path, default=LAB/'research/sample_expansion.json')
    p.add_argument('--output-dir', type=Path, default=LAB/'results/sample_expansion')
    args = p.parse_args()
    run(args.data_dir, args.config, args.experiment, args.output_dir)


if __name__ == '__main__':
    main()
