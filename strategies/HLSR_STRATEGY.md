# HLSR 高位流动性扫顶反转策略

版本：1.0  
英文名：HLSR - High-Level Liquidity Sweep Reversal  
市场：OKX USDT 线性永续合约  
用途：只用于历史回测、行情扫描和信号生成，不包含真实下单。

## 1. 核心逻辑

策略只做空已经出现大幅上涨的山寨币。完整条件为：

> 24 小时涨幅大于 40% + 24 小时成交额大于 3,000 万 USDT + 扫过前高但收回 + 拒绝形态 + 局部结构破坏 = 做空信号

策略使用 15 分钟入场周期，1 小时和 4 小时判断市场状态。所有判断只能使用当前信号收盘时已经确认的 K 线；入场默认放在确认 K 线之后的下一根 K 线开盘。

## 2. 硬性筛选

在当前 15 分钟 K 线 `i` 上计算：

```text
gain_24h = close[i] / close[i - 96] - 1
quote_volume_24h = sum(volCcyQuote[i - 95:i + 1])
```

只有以下条件同时满足时才继续检查信号：

```text
gain_24h > 0.40
quote_volume_24h > 30,000,000 USDT
```

主流币和非 USDT 线性永续合约不在策略标的池中。当前离线优化从满足历史复合事件次数的合约中选择事件最多的 50 个标的。

## 3. 市场状态和高位区

4 小时周期计算 EMA20、EMA50、最近 20 根 K 线高低点和 ATR14：

- `bearish`：收盘价低于 EMA20，且 EMA20 低于 EMA50；
- `range`：最近区间宽度不大于 `max(8%, ATR / 中点 * 8)`；
- `bullish` 和 `transition` 默认不做空。

阻力参考值为：

```text
resistance = recent_high + 0.25 * ATR14(4H)
```

扫顶位置按距离阻力的 ATR 倍数标记为 `normal_extension`、`primary_sweep` 或 `extreme_sweep`。

## 4. 扫顶和拒绝

前高使用 15 分钟周期前 `swing_lookback` 根 K 线的最高价：

```text
high[i] > previous_swing_high
close[i] < previous_swing_high
```

拒绝评分由以下四项组成，每项满足计 1 分：

1. 上影线占本根振幅达到 `wick_ratio`；
2. 收阴；
3. 成交额超过前 20 根均值乘以 `volume_multiple`；
4. 收盘重新跌破前高。

默认至少需要 2 分。

## 5. 右侧确认和入场

扫顶后最多等待 `confirmation_window` 根 K 线，满足以下任意一项：

- 收盘跌破扫顶后的局部低点；
- 反抽接近前高（容差约 0.5%），同时收阴并收盘低于前高。

确认发生后，在下一根 15 分钟 K 线开盘做空。信号生成器在下一根 K 线尚未出现时会标记为 `PENDING_NEXT_OPEN`，不会把确认收盘价假装成已成交价。

## 6. 止损、止盈和仓位

默认固定 2 倍杠杆。杠杆只影响保证金占用，单笔风险仍按价格止损距离计算。

```text
stop = sweep_high + stop_atr * ATR14(15M)
risk = stop - entry
```

止盈采用结构位和 R 倍数中较保守的组合：

- TP1：确认前后最近支撑，平仓 30%；
- TP2：4 小时区间中点，或至少 2R，平仓 30%；
- TP3：前一主要低点，或至少 3R，平仓 40%。

TP1 成交后止损移到开仓价；TP2 或更远目标成交后，使用最近 2 根 15 分钟 K 线高点收紧止损。若同一根 K 线同时触发止盈和止损，按止损优先。价格跳空越过止损时按下一根开盘价估算退出。

## 7. 失效和重新入场

出现以下任意情况，空头逻辑失效：

- K 线收盘重新站上扫顶高点；
- 价格触及初始止损或动态止损；
- 信号确认窗口结束但没有确认。

单笔交易退出后进入冷却期，不允许立即加仓或摊平。必须重新出现新的阻力、扫顶、拒绝和结构确认，才允许重新生成信号。

## 8. 离线优化基线

当前标准参数来自 180 天市场导出数据的滚动训练/验证/测试：

```json
{
  "swing_lookback": 6,
  "wick_ratio": 0.6,
  "volume_multiple": 1.0,
  "minimum_rejection_score": 2,
  "confirmation_window": 4,
  "stop_atr": 0.25,
  "trail_bars": 2,
  "allow_range": true,
  "zone_required": "any"
}
```

回测报告中的样本外点估计为 55.56% 胜率、实际平均收益/风险 3.04，但只有 9 笔样本，不能视为未来收益保证。风险收益表述按“风险:收益不高于 1:2”解释，即收益/风险至少为 2.0。

## 9. 使用信号生成器

读取单个 5 分钟 JSONL 或 gzip JSONL 文件，自动聚合为 15 分钟：

```bash
python3 scripts/hlsr_signal_generator.py \
  --input data/market_export/BEAT_USDT_SWAP_5m_20260331T065300Z_20260927T065300Z.jsonl.gz \
  --symbol BEAT-USDT-SWAP \
  --config strategies/hlsr_signal_config.json
```

默认只输出最近一个信号。需要导出全部历史信号时添加 `--all`，需要写入文件时添加 `--output path/to/signals.json`。该命令只输出信号、入场参考价、止损、止盈和风险字段，不会连接交易所或提交订单。
