#!/usr/bin/env python3
"""Compare causal runner exits and post-pump trend entries in one memory load."""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

import sample_expansion as engine

LAB = Path(__file__).resolve().parents[1]
MS = engine.MS


@dataclass(frozen=True)
class TrendOutcome(engine.Outcome):
    entry_mode: str
    initial_stop: float
    tp1_ts: int | None
    tp1_fraction: float
    bars_held: int


def indicators(bars, config):
    period = config['signal_parameters']['atr_period']
    atr = engine.base.atr_values(bars, period)
    volume = engine.base.simple_average([b.quote_volume for b in bars], 20)
    ema, rsi = [0.0] * len(bars), [None] * len(bars)
    start = 0
    for i, b in enumerate(bars):
        new_segment = i == 0 or b.ts != bars[i-1].ts + MS
        ema[i] = b.close if new_segment else ema[i-1] + 2 / 21 * (b.close - ema[i-1])
        if i and new_segment:
            rsi[start:i] = engine.base.wilder_rsi([x.close for x in bars[start:i]], 14)
            start = i
    rsi[start:] = engine.base.wilder_rsi([x.close for x in bars[start:]], 14)
    return {'atr': atr, 'volume': volume, 'ema': ema, 'rsi': rsi}


def trend_events(symbol, bars, values, params):
    events = []
    segment_start, last_pump = 0, None
    n = params['swing_lookback_bars']
    for i, b in enumerate(bars[:-1]):
        if i and b.ts != bars[i-1].ts + MS:
            segment_start, last_pump = i, None
        if i-segment_start < 96:
            continue
        gain = b.high / bars[i-96].close - 1
        if gain > 1.0:
            last_pump = i
        if last_pump is None or i-last_pump > params['pump_memory_bars']:
            continue
        rsi, previous = values['rsi'][i], values['rsi'][i-1]
        if rsi is None or previous is None or values['atr'][i] <= 0:
            continue
        ema = values['ema'][i]
        if not (b.close < b.open and b.close < ema < values['ema'][i-1]
                and rsi < previous and rsi <= params['rsi_max']
                and b.quote_volume >= 0.5 * values['volume'][i]):
            continue
        if params['mode'] == 'breakdown':
            accepted = b.close < min(x.low for x in bars[i-n:i])
        else:
            accepted = (bars[i-1].close < values['ema'][i-1]
                        and b.high >= ema and b.close < bars[i-1].close)
        if not accepted:
            continue
        span = b.high-b.low
        anchor = max(x.high for x in bars[i-n+1:i+1])
        events.append(engine.base.Event(symbol, i, b.ts, gain, 0.0, rsi, rsi-previous,
                      (b.high-max(b.open, b.close))/span if span else 0.0,
                      (b.close-b.low)/span if span else 0.0,
                      b.quote_volume/values['volume'][i] if values['volume'][i] else 0.0,
                      0.0, values['atr'][i]/b.close, anchor, values['atr'][i]))
    return events


def simulate(event, bars, atr, config, spec, entry_mode='wick', boundary=None,
             entry_index=None, entry_price=None):
    costs, risk = config['costs'], config['risk']
    start = event.index+1 if entry_index is None else entry_index
    if start >= len(bars) or bars[start].ts != event.timestamp+MS:
        if entry_index is None or start >= len(bars) or bars[start].ts < event.timestamp+MS:
            return None, 'entry_gap'
    if boundary is not None and bars[start].ts >= boundary:
        return None, 'outside_window'
    raw_entry = bars[start].open if entry_price is None else entry_price
    entry = raw_entry * (1-costs['slippage_one_way'])
    initial_stop = event.anchor_high + risk['stop_atr'] * event.atr
    distance = initial_stop-entry
    if not risk['min_risk_atr'] <= distance/event.atr <= risk['max_risk_atr']:
        return None, 'risk_distance'
    target_r = spec.get('target_r', 1.0)
    target = entry-target_r*distance
    if target <= 0:
        return None, 'invalid_target'
    last = min(len(bars)-1, start+spec['max_hold_bars']-1)
    reason = 'data_end' if last == len(bars)-1 else 'timeout'
    if boundary is not None:
        while last >= start and bars[last].ts >= boundary:
            last -= 1
            reason = 'split_end'
    stop, remaining, partial_exit = initial_stop, 1.0, 0.0
    tp1_ts, used_fraction, activated = None, 0.0, False
    lowest_close = entry
    adverse, exit_index, exit_ts = 0.0, last, bars[last].ts+MS
    weighted_exit = None
    for j in range(start, last+1):
        b = bars[j]
        if j > start and b.ts != bars[j-1].ts+MS:
            weighted_exit = partial_exit+remaining*bars[j-1].close
            exit_index, exit_ts, reason = j-1, bars[j-1].ts+MS, 'data_gap'
            break
        hit = b.open if b.open >= stop else stop
        worst = b.open if b.open >= stop else min(b.high, stop)
        worst_exit = (partial_exit+remaining*worst)*(1+costs['slippage_one_way'])
        adverse = min(adverse, (entry-worst_exit-costs['fee_rate_one_way']*(entry+worst_exit))/entry)
        # Stops were fixed before this candle opened. Intrabar stop/TP ties
        # use the stop first; close-derived changes only affect the next bar.
        if b.open >= stop or b.high >= stop:
            weighted_exit = partial_exit+remaining*hit
            exit_index = j
            exit_ts = b.ts if b.open >= stop else b.ts+MS
            reason = 'stop_gap' if b.open >= stop else ('trailing_stop' if activated else 'stop')
            break
        if spec['mode'] == 'fixed' and b.low <= target:
            weighted_exit = target
            exit_index, exit_ts, reason = j, b.ts+MS, 'target'
            break
        if spec['mode'] == 'runner':
            if not activated and b.low <= target:
                activated, tp1_ts = True, b.ts+MS
                used_fraction = spec['tp1_fraction']
                partial_exit = used_fraction*target
                remaining = 1.0-used_fraction
                stop = min(stop, entry)
            lowest_close = min(lowest_close, b.close)
            if activated:
                if spec['trail_mode'] == 'atr':
                    proposed = lowest_close+spec['trail_distance']*atr[j]
                else:
                    n = int(spec['trail_distance'])
                    proposed = max(x.high for x in bars[max(start, j-n+1):j+1])+0.1*atr[j]
                # Do not retroactively execute a close-derived stop. If a
                # candidate is already through the close, use that close as
                # the stop for the next candle (with gap execution).
                stop = min(stop, max(b.close, proposed))
        if j == last:
            weighted_exit = partial_exit+remaining*b.close
    exit_price = weighted_exit*(1+costs['slippage_one_way'])
    net = entry-exit_price-costs['fee_rate_one_way']*(entry+exit_price)-entry*costs['funding_rate_round_trip']
    return TrendOutcome(event.symbol, event.index, event.timestamp+MS, bars[start].ts,
                        exit_ts, exit_index, entry, exit_price, initial_stop, target,
                        net/distance, net/entry, net/entry*2*100, adverse, reason,
                        entry_mode, initial_stop, tp1_ts, used_fraction, exit_index-start+1), None


def evaluate(inputs, series, values, config, split, cache, phase='full'):
    trades, rejected = [], Counter()
    signals = 0
    seen = set()
    for entry_mode, events, exit_spec in inputs:
        for event in events:
            if phase == 'train' and event.timestamp+MS >= split:
                continue
            if phase == 'validation' and event.timestamp+MS < split:
                continue
            # Wick entry wins a same-bar tie when both modules agree.
            identity = (event.symbol, event.index)
            if identity in seen:
                continue
            key = (event.symbol, event.index, entry_mode, event.anchor_high,
                   tuple(sorted(exit_spec.items())), boundary_key(phase, split))
            if key not in cache:
                cache[key] = simulate(event, series[event.symbol], values[event.symbol]['atr'],
                                      config, exit_spec, entry_mode, split if phase == 'train' else None)
            signals += 1
            trade, reason = cache[key]
            if trade is None:
                rejected[reason] += 1
            else:
                seen.add(identity)
                trades.append(trade)
    admitted, rows, account = engine.portfolio(trades, config)
    return {'signals': signals, 'rejected': dict(rejected), 'portfolio': engine.metrics(admitted),
            'account': account, 'entry_modes': dict(Counter(t.entry_mode for t in admitted)),
            'exit_reasons': dict(Counter(t.reason for t in admitted)),
            'tp1_activated': sum(t.tp1_ts is not None for t in admitted)}, rows, admitted


def boundary_key(phase, split):
    return split if phase == 'train' else None


def meets_quality(train, validation, selection):
    for phase, data in [('train', train), ('validation', validation)]:
        m = data['portfolio']
        if (m['trades'] < selection['minimum_'+phase+'_trades']
            or m['win_rate'] < selection['minimum_'+phase+'_win_rate']
            or m['profit_factor'] is None
            or m['profit_factor'] < selection['minimum_'+phase+'_profit_factor']
            or m['total_r'] <= 0):
            return False
    return True


def after_exit_diagnostics(trades, series):
    result = []
    for trade in trades:
        if trade.reason != 'target':
            continue
        bars = series[trade.symbol]
        row = {'symbol': trade.symbol, 'entry_utc': engine.iso(trade.entry_ts),
               'exit_utc': engine.iso(trade.exit_ts), 'net_r': trade.net_r}
        for window in (96, 192):
            future = []
            for j in range(trade.exit_index+1, min(len(bars), trade.exit_index+window+1)):
                if bars[j].ts != bars[j-1].ts+MS:
                    break
                future.append(bars[j])
            row[f'complete_next_{window//4}h'] = len(future) == window
            low = min((b.low for b in future), default=trade.exit)
            row[f'additional_downside_{window//4}h_pct'] = max(0.0, 1-low/(trade.exit/1.0002))*100
            row[f'entry_to_future_low_{window//4}h_r_hindsight_only'] = (trade.entry-low)/(trade.stop-trade.entry)
        result.append(row)
    return sorted(result, key=lambda x: x['additional_downside_24h_pct'], reverse=True)


def run(data_dir, experiment_path, output):
    experiment = json.loads(experiment_path.read_text())
    reference_path = engine.ROOT/experiment['reference_config']
    config = json.loads(reference_path.read_text())
    assert config['observation_filter']['intraday_gain_gt'] == 1.0
    assert config['position_management']['leverage'] == 2.0
    split = int(datetime.fromisoformat(experiment['split_utc']).timestamp()*1000)
    started = time.monotonic()
    print('Loading all market candles once; subsequent experiments reuse memory.', flush=True)
    series, sources = engine.load_once(data_dir, config)
    print(f'Loaded {len(series)} symbols, {sum(len(b) for b in series.values())} 15m bars.', flush=True)
    wick = []
    for symbol, bars in series.items():
        wick.extend(e for e in engine.features(symbol, bars, config)
                    if engine.matches(e, bars, config['signal_parameters']))
    pump_symbols = set()
    # Only post-pump symbols need extra indicators; candles for the entire
    # eligible universe are nevertheless already resident in memory.
    for symbol, bars in series.items():
        if any(bars[i].high > 2*bars[i-96].close for i in range(96, len(bars))):
            pump_symbols.add(symbol)
    values = {s: indicators(series[s], config) for s in pump_symbols}
    print(f'Prepared {len(wick)} wick signals; {len(values)} post-pump symbols.', flush=True)
    baseline_exit = {'mode': 'fixed', 'target_r': 1.0, 'max_hold_bars': 24}
    specs = [('reference_1r_6h', [('wick', wick, baseline_exit)], {'family': 'reference', 'exit': baseline_exit})]
    wg = experiment['wick_runner_grid']
    for index, (fraction, trail, hold) in enumerate(itertools.product(wg['tp1_fraction'], wg['trail'], wg['max_hold_bars'])):
        exit_spec = {'mode': 'runner', 'tp1_fraction': fraction, 'trail_mode': trail['mode'],
                     'trail_distance': trail['distance'], 'max_hold_bars': hold}
        specs.append((f'wick_runner_{index:03d}', [('wick', wick, exit_spec)],
                      {'family': 'wick_runner', 'exit': exit_spec}))
    for target in (2.0, 3.0):
        exit_spec = {'mode': 'fixed', 'target_r': target, 'max_hold_bars': 96}
        specs.append((f'wick_fixed_{int(target)}r_24h', [('wick', wick, exit_spec)],
                      {'family': 'wick_fixed', 'exit': exit_spec}))
    eg = experiment['entry_grid']
    for index, combination in enumerate(itertools.product(*(eg[k] for k in eg))):
        params = dict(zip(eg, combination))
        ee = []
        for symbol in sorted(pump_symbols):
            ee.extend(trend_events(symbol, series[symbol], values[symbol], params))
        for exit_index, exit_spec in enumerate(experiment['trend_exits']):
            name = f'trend_{index:03d}_exit_{exit_index}'
            inputs = [(params['mode'], ee, exit_spec)]
            details = {'family': 'trend', 'entry': params, 'exit': exit_spec}
            specs.append((name, inputs, details))
            specs.append(('combined_'+name, [('wick', wick, baseline_exit)]+inputs,
                          dict(details, family='combined')))
    print(f'Evaluating {len(specs)} combinations in memory.', flush=True)
    cache, results, grid = {}, {}, []
    for name, inputs, details in specs:
        phases = {}
        for phase in ('full', 'train', 'validation'):
            summary, rows, admitted = evaluate(inputs, series, values, config, split, cache, phase)
            phases[phase] = summary
            if phase == 'full':
                full_rows, full_admitted = rows, admitted
        results[name] = {'parameters': details, **phases, '_rows': full_rows, '_trades': full_admitted}
        row = {'name': name, 'family': details['family'], 'parameters': json.dumps(details, sort_keys=True)}
        for phase in phases:
            row.update({phase+'_'+k: v for k, v in phases[phase]['portfolio'].items()})
            row[phase+'_account_return_pct'] = phases[phase]['account']['account_return_pct']
        row['retrospective_quality_pass'] = meets_quality(phases['train'], phases['validation'], experiment['selection'])
        grid.append(row)
    def rank(name):
        m = results[name]['full']['portfolio']
        return m['total_r'], m['trades'], name
    eligible = [r['name'] for r in grid if r['retrospective_quality_pass']]
    best_quality = max(eligible, key=rank) if eligible else None
    best_full = max(results, key=rank)
    chosen = {'reference_1r_6h', best_full}
    if best_quality:
        chosen.add(best_quality)
    family_bests = {}
    for family in ('wick_runner', 'wick_fixed', 'trend', 'combined'):
        members = [name for name in results if results[name]['parameters']['family'] == family]
        family_bests[family] = max(members, key=rank)
        chosen.add(family_bests[family])
    output.mkdir(parents=True, exist_ok=True)
    diagnostics = after_exit_diagnostics(results['reference_1r_6h']['_trades'], series)
    engine.base.write_csv(output/'after_exit_diagnostics.csv', diagnostics)
    engine.base.write_csv(output/'grid.csv', grid)
    candidates = {}
    for name in sorted(chosen):
        item = results[name]
        trades = item['_trades']
        candidates[name] = {k: v for k, v in item.items() if not k.startswith('_')}
        candidates[name]['by_month_utc'] = {
            month: engine.metrics([t for t in trades if engine.iso(t.entry_ts)[:7] == month])
            for month in sorted({engine.iso(t.entry_ts)[:7] for t in trades})}
        candidates[name]['leave_one_symbol_out_total_r'] = {
            symbol: sum(t.net_r for t in trades if t.symbol != symbol)
            for symbol in sorted({t.symbol for t in trades})}
        engine.base.write_csv(output/(name+'_trades.csv'), item['_rows'])
        (output/(name+'_parameters.json')).write_text(json.dumps(item['parameters'], ensure_ascii=False, indent=2)+'\n')
    report = {'experiment': experiment, 'data': {'symbols': len(series), 'bars': sum(len(b) for b in series.values()),
              'start_utc': engine.iso(min(b[0].ts for b in series.values())),
              'end_utc': engine.iso(max(b[-1].ts+MS for b in series.values())),
              'market_data_read_passes': 1, 'sources': sources},
              'grid_variants': len(grid), 'retrospective_quality_variants': len(eligible),
              'best_full_total_r_descriptive_only': best_full,
              'best_full_total_r_with_retrospective_quality': best_quality,
              'family_bests_full_total_r': family_bests, 'candidates': candidates,
              'after_exit_diagnostics': {'tp1_exits': len(diagnostics),
                   'next_24h_additional_downside_gt_10pct': sum(d['complete_next_24h'] and d['additional_downside_24h_pct'] > 10 for d in diagnostics),
                   'hindsight_only': True},
              'limitations': ['Previously explored history; retrospective selection uses validation/full history.',
                              'Current-universe survivorship bias; funding zero; no liquidation model.',
                              '15m OHLC stop-first execution; close-derived stops begin next bar.',
                              'Risk-sized account settles weighted partial returns at final exit for admission; single global position.',
                              'After-exit minimum prices diagnose missed moves only; they are not executable returns.',
                              'No specific user-referenced symbol/time supplied; this covers the entire eligible universe.'],
              'runtime_seconds': time.monotonic()-started}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    for name in sorted(chosen):
        item = candidates[name]
        print(name, json.dumps({p: item[p]['portfolio'] for p in ('full', 'train', 'validation')}, ensure_ascii=False), flush=True)
    print('Best retrospective quality:', best_quality, 'runtime:', report['runtime_seconds'], flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=engine.base.DEFAULT_DATA)
    parser.add_argument('--experiment', type=Path, default=LAB/'research/trend_capture.json')
    parser.add_argument('--output-dir', type=Path, default=LAB/'results/trend_capture')
    args = parser.parse_args()
    run(args.data_dir, args.experiment, args.output_dir)


if __name__ == '__main__':
    main()
