# 研究依据

正式参数固定自 [`../../extreme_wick_short/results/return_maximization/grid_0580_config.json`](../../extreme_wick_short/results/return_maximization/grid_0580_config.json)。原策略的扫描代码、逐笔结果和入场方式实验继续保存在原目录，避免正式规则包覆盖研究证据。

核心候选的历史结果为 48 笔全局单仓成交、68.8% 胜率、PF 2.12、17.17R；训练段 39 笔，回顾验证段 9 笔。该验证段曾被用于候选筛选，不能视为独立样本外证明。

入场实验比较了确认后实时成交、下一根开盘成交及前 3/6 根 K 线高点限价。实时成交在包含趋势补充模块的组合中达到 22.27R，但 PF 只有 1.37；正式核心因此保留 `grid_0580` 的高质量固定止盈规则，并将实时成交定义为执行方式，等待独立的核心策略执行复测。

复核命令：

```bash
python3 strategies/extreme_wick_short/src/sample_expansion.py \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/extreme_wick_short/config/strategy.json \
  --experiment strategies/extreme_wick_short/research/return_maximization.json \
  --output-dir strategies/extreme_wick_short/results/return_maximization
```

正式规则包已接入 Swift 纸面/模拟运行时并默认停用；申请真实交易资格前仍必须使用新的流动性门槛、实际成交偏离和新样本外数据重新验收。
