#!/usr/bin/env python3
"""Compare market and prior-candle limit entries for the recommended overlay."""
from __future__ import annotations

import argparse
import itertools
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import sample_expansion as engine
import trend_capture as tc

LAB = Path(__file__).resolve().parents[1]
MS = engine.MS


def contiguous_backwards(bars, index, count):
    start = max(0, index-count)
    if index-start < count:
        return None
    for i in range(start+1, index+1):
        if bars[i].ts != bars[i-1].ts+MS:
            return None
    return start


def prior_limit_price(bars, event_index, kind, lookback):
    start = contiguous_backwards(bars, event_index, lookback)
    if start is None:
        return None
    previous = bars[start:event_index]
    if kind == 'body_high':
        return max(max(b.open, b.close) for b in previous)
    if kind == 'close_high':
        return max(b.close for b in previous)
    if kind == 'open_high':
        return max(b.open for b in previous)
    raise ValueError(f'unknown limit kind: {kind}')


def find_fill(event, bars, order):
    first = event.index+1
    if first >= len(bars) or bars[first].ts != event.timestamp+MS:
        return None, 'entry_gap', None
    if order['mode'] == 'market_next_open':
        return first, None, bars[first].open
    if order['mode'] == 'market_signal_close':
        # The signal is evaluated only after the candle has closed. Holding
        # starts on the next candle, while the fill price is the confirmed
        # signal close plus the modeled sell slippage.
        return first, None, bars[event.index].close
    target = prior_limit_price(bars, event.index, order['kind'], order['lookback'])
    if target is None or target <= 0:
        return None, 'limit_reference_gap', target
    last = min(len(bars), first+order['wait_bars'])
    for index in range(first, last):
        if index > first and bars[index].ts != bars[index-1].ts+MS:
            return None, 'entry_gap', target
        # A sell limit filled above the opening print executes at the opening
        # price; otherwise it fills when the candle trades up to the limit.
        if bars[index].open >= target:
            return index, None, bars[index].open
        if bars[index].high >= target:
            return index, None, target
    return None, 'limit_unfilled', target


def quality(summary, selection):
    for phase in ('train', 'validation'):
        m = summary[phase]['portfolio']
        if (m['trades'] < selection[f'minimum_{phase}_trades']
                or m['win_rate'] < selection[f'minimum_{phase}_win_rate']
                or m['profit_factor'] is None
                or m['profit_factor'] < selection[f'minimum_{phase}_profit_factor']
                or m['total_r'] <= 0):
            return False
    return True


def load_recommended(data_dir, config):
    series, sources = engine.load_once(data_dir, config)
    values = {s: tc.indicators(bars, config) for s, bars in series.items()
              if any(bars[i].high > 2*bars[i-96].close for i in range(96, len(bars)))}
    wick = []
    for symbol, bars in series.items():
        wick.extend(e for e in engine.features(symbol, bars, config)
                    if engine.matches(e, bars, config['signal_parameters']))
    trend_params = {'mode': 'ema_retest', 'pump_memory_bars': 192,
                    'swing_lookback_bars': 3, 'rsi_max': 70.0}
    trend = []
    for symbol in sorted(values):
        trend.extend(tc.trend_events(symbol, series[symbol], values[symbol], trend_params))
    by_identity = {}
    # The original wick entry has priority when both entry families signal on
    # the same confirmed candle.
    for event in wick:
        by_identity[(event.symbol, event.index)] = ('wick', event,
            {'mode': 'fixed', 'target_r': 1.0, 'max_hold_bars': 24})
    for event in trend:
        by_identity.setdefault((event.symbol, event.index), ('ema_retest', event,
            {'mode': 'runner', 'tp1_fraction': 0.5, 'trail_mode': 'atr',
             'trail_distance': 3.5, 'max_hold_bars': 192}))
    return series, values, list(by_identity.values()), sources


def evaluate(signals, series, values, config, order, split, cache, phase='full'):
    raw, fills, rejected = [], [], Counter()
    signals_seen, orders_filled = 0, 0
    for source, event, exit_spec in signals:
        if phase == 'train' and event.timestamp+MS >= split:
            continue
        if phase == 'validation' and event.timestamp+MS < split:
            continue
        signals_seen += 1
        key = (source, event.symbol, event.index, json.dumps(order, sort_keys=True),
               'train' if phase == 'train' else 'full')
        if key not in cache:
            fill_index, reason, fill_price = find_fill(event, series[event.symbol], order)
            if fill_index is None:
                cache[key] = (None, reason, fill_price, None)
            else:
                boundary = split if phase == 'train' else None
                trade, sim_reason = tc.simulate(event, series[event.symbol],
                    values[event.symbol]['atr'], config, exit_spec, source, boundary,
                    fill_index, fill_price)
                cache[key] = (trade, sim_reason, fill_price, fill_index)
        trade, reason, fill_price, filled_index = cache[key]
        if filled_index is not None:
            orders_filled += 1
        if trade is None:
            rejected[reason] += 1
            continue
        fills.append({'symbol': event.symbol, 'signal_index': event.index,
                      'signal_utc': engine.iso(event.timestamp), 'entry_mode': source,
                      'order_mode': order['mode'], 'limit_kind': order.get('kind'),
                      'lookback_bars': order.get('lookback'), 'wait_bars': order.get('wait_bars'),
                      'order_price': fill_price, 'fill_index': filled_index,
                      'entry_utc': engine.iso(trade.entry_ts)})
        raw.append(trade)
    admitted, rows, account = engine.portfolio(raw, config)
    m = engine.metrics(admitted)
    return {'signals': signals_seen, 'orders_filled': orders_filled,
            'fill_rate': orders_filled/signals_seen if signals_seen else None,
            'trades_after_risk_checks': len(raw),
            'rejected': dict(rejected), 'portfolio': m, 'account': account,
            'entry_modes': dict(Counter(t.entry_mode for t in admitted)),
            'exit_reasons': dict(Counter(t.reason for t in admitted))}, rows, fills


def run(data_dir, experiment_path, output):
    experiment = json.loads(experiment_path.read_text())
    ref = engine.ROOT/experiment['reference_config']
    config = json.loads(ref.read_text())
    split = int(datetime.fromisoformat(experiment['split_utc']).timestamp()*1000)
    started = time.monotonic()
    print('Loading all market candles once; reusing them for every order model.', flush=True)
    series, values, signals, sources = load_recommended(data_dir, config)
    print(f'Loaded {len(series)} symbols and {sum(len(b) for b in series.values())} 15m bars; '
          f'{len(signals)} recommended signals.', flush=True)
    default_orders = [
        {'name': 'market_signal_close', 'mode': 'market_signal_close'},
        {'name': 'market_next_open', 'mode': 'market_next_open'},
    ]
    for kind, lookback in itertools.product(('body_high', 'close_high', 'open_high'), (3, 6)):
        for wait in (2, 4, 8):
            default_orders.append({'name': f'limit_{kind}_{lookback}bars_wait{wait}', 'mode': 'limit',
                                   'kind': kind, 'lookback': lookback, 'wait_bars': wait})
    orders = experiment.get('orders', default_orders)
    output.mkdir(parents=True, exist_ok=True)
    cache, grid, details = {}, [], {}
    for order in orders:
        phases = {}
        phase_fills = {}
        for phase in ('full', 'train', 'validation'):
            summary, rows, fills = evaluate(signals, series, values, config, order, split, cache, phase)
            phases[phase] = summary
            phase_fills[phase] = fills
            if phase == 'full':
                full_rows = rows
        name = order['name']
        details[name] = {'order': order, 'phases': phases}
        row = {'name': name, 'mode': order.get('mode'), 'kind': order.get('kind'),
               'lookback': order.get('lookback'), 'wait_bars': order.get('wait_bars')}
        for phase in phases:
            row.update({phase+'_'+k: v for k, v in phases[phase]['portfolio'].items()})
            row[phase+'_signals'] = phases[phase]['signals']
            row[phase+'_orders_filled'] = phases[phase]['orders_filled']
            row[phase+'_trades_after_risk_checks'] = phases[phase]['trades_after_risk_checks']
            row[phase+'_fill_rate'] = phases[phase]['fill_rate']
            row[phase+'_account_return_pct'] = phases[phase]['account']['account_return_pct']
        row['retrospective_quality_pass'] = quality(phases, experiment['selection'])
        grid.append(row)
        engine.base.write_csv(output/(name+'_trades.csv'), full_rows)
        engine.base.write_csv(output/(name+'_fills.csv'), phase_fills['full'])
    full_rank = lambda row: (row['full_total_r'], row['full_trades'], row['name'])
    best_full = max(grid, key=full_rank)
    eligible = [row for row in grid if row['retrospective_quality_pass']]
    best_quality = max(eligible, key=full_rank) if eligible else None
    output.mkdir(parents=True, exist_ok=True)
    engine.base.write_csv(output/'grid.csv', grid)
    report = {'experiment': experiment, 'data': {'symbols': len(series),
              'bars': sum(len(b) for b in series.values()), 'market_data_read_passes': 1,
              'sources': sources}, 'signals': len(signals), 'order_variants': len(orders),
              'best_full_total_r': best_full['name'],
              'best_full_total_r_with_retrospective_quality': best_quality['name'] if best_quality else None,
              'variants': details, 'limitations': [
                  'Historical data was previously explored; validation is retrospective.',
                  'Limit fills use only prior confirmed candles for the order price and future OHLC for fill simulation.',
                  'A pending order is evaluated independently per signal; global single-position admission is applied after fills.',
                  'No funding, liquidation or queue priority model; a limit touch is treated as a fill.',
                  'The recommended combined overlay gives wick entries priority on the same candle.'],
              'runtime_seconds': time.monotonic()-started}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print('Best full:', best_full['name'], best_full['full_trades'], best_full['full_win_rate'],
          best_full['full_profit_factor'], best_full['full_total_r'], flush=True)
    print('Best quality:', best_quality['name'] if best_quality else None, flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=engine.base.DEFAULT_DATA)
    parser.add_argument('--experiment', type=Path, default=LAB/'research/entry_orders.json')
    parser.add_argument('--output-dir', type=Path, default=LAB/'results/entry_orders')
    args = parser.parse_args()
    run(args.data_dir, args.experiment, args.output_dir)


if __name__ == '__main__':
    main()
