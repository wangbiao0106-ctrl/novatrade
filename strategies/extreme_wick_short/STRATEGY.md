# 山寨冲高观察池双挂单做空

版本：0.2（研究候选，未接入运行时）
稳定标识：`extreme_wick_short`
市场：OKX USDT 线性永续合约
统一时间周期：5m 原始数据只用于聚合，所有观察、形态、挂单和出场都按 **15m K 线**判断

本文是规则真源；机器参数在 [`config/strategy.json`](config/strategy.json)。研究结果不能反向修改规则。

## 1. 观察池

只使用已经确认的完整 15m K 线。对当前 K 线 `i`，以前 24 小时收盘价 `C[i-96]` 为基准：

```text
gain24_intraday = max(H[i], C[i]) / C[i-96] - 1
low30 = min(L[i-2880:i])
price_to_low30 = max(H[i], C[i]) / low30
```

观察池条件为 `gain24_intraday > 60%` 且 `price_to_low30 >= 3.0`。30 日低点窗口排除当前 K 线，避免当前插针自己制造分母。涨幅没有上限，+100% 及以上保留。时间统一按 UTC 日界；缺 K 线的窗口关闭信号。观察池只是候选范围，必须继续满足下面任一挂单形态。

## 2. 策略一：15m 小幅上行 → +20% 突涨 → 小跌回挂

1. 突涨 K 线 `S` 前 4 根 15m 是小幅震荡上行：净变化 0～12%，至少 50% 收阳，每根实体变化不超过 5%，总高低区间不超过 20%。
2. `S.high / C[S-1] - 1 >= 20%`。这里“20 个点”固定解释为 **20%**；整根 K 线收盘后才确认其最高点，不能在未收盘时提前触发。
3. 紧随 `S` 的 K 线 `D` 必须收阴，跌幅 `0% < (D.open-D.close)/D.open <= 8%`，且 `D.low / S.close >= 0.90`，表示大涨后的轻微回落。
4. `D` 收盘后，在下一根 15m 起挂卖出限价，挂单价为 `D.open`。有效 4 根 15m；下一根开盘高于挂单价时按开盘成交，否则后续最高价触价按挂单价成交；未成交则取消。不能把 `D.open` 当成已经成交。

## 3. 策略二：日内已确认高点 → 6 根 15m 小幅下行 → 高点收盘价回挂

1. 先在 UTC 当日内滚动维护已收盘 15m K 线最高点。日内高点 K 线 `H` 必须在当前小幅下行序列之前已经确认；使用 `H.close` 作为挂单价，不能使用当天结束后才知道的最终最高点。
2. `H` 之后的 6 根 15m K 线（包含当前确认 K 线）小幅震荡下行：净收盘跌幅 0.5%～8%，至少 60% 收阴，每根实体变化不超过 3%，且没有重新突破 `H.high` 的 1% 容差。
3. 这 6 根 K 线收盘后，从下一根 15m 起挂卖出限价，挂单价为 `H.close`。有效 4 根 15m，成交和取消规则与策略一相同。

## 4. 共同出场和成本

成交后止损为锚点高点（策略一为 `S.high`，策略二为 `H.high`）+ `0.30 × ATR14`；止盈为 `1.5R`，最多持有 32 根 15m。开盘越过止损按开盘价；同一根同时触发止损和止盈时止损优先。回测扣除单边手续费 6bp 和单边滑点 2bp，并按止损距离检查 0.5～3 ATR 的风险范围。当前研究脚本在每种形态内部按币种冷却 16 根；组合统计只是两种形态成交的合并，尚未施加跨币种和跨形态的全局并发过滤；`position_management.max_concurrent_positions` 与 `one_position_per_symbol` 是后续运行时约束。

## 5. 研究结论与限制

入口：

```bash
python3 strategies/extreme_wick_short/src/limit_order_analysis.py \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/extreme_wick_short/config/strategy.json \
  --output-dir strategies/extreme_wick_short/results
```

报告写入 `results/limit_report.json`，成交写入 `results/limit_trades.csv`，观察池事件写入 `results/observations.csv`。报告按两种挂单策略分别给出观察事件、挂单数、成交率、未成交取消、净 R、盈亏比和最大回撤。当前仍是研究候选，未接入 `Sources/`，没有自动下单资格。

`src/extreme_wick_short.py` 是早期的常规冲高反转扫描器，仅保留作历史对照；本版本的规则、分析入口和结果以 `limit_order_analysis.py` 为准。
