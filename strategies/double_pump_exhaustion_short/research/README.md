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

正式规则包已接入 Swift 运行时并默认停用；运行时按连接账号类型路由模拟或实盘。新的流动性门槛、实际成交偏离和样本外数据仍需在运营流程中验收，但不作为策略配置的交易模式限制。

## 全历史复核（18 个月，2025-04-03 ~ 2026-10-03）

[`report_full_history.py`](report_full_history.py) 复用 `extreme_wick_short` 的观察池、信号匹配和撮合代码，把 `grid_0580` 固定参数放到全部可用历史上复核，并逐项改变入场口径与流动性门槛（成本单边 6bp 手续费 + 2bp 滑点，281 个合规山寨币）：

| 口径 | 全部笔数 | 全部净 R | 新样本外笔数 | 新样本外净 R | 原窗口净 R |
|---|---:|---:|---:|---:|---:|
| 下一根开盘（原研究） | 108 | +13.94R | 60 | **−3.23R** | +17.17R |
| 信号收盘市价（正式规则入场） | 107 | +13.51R | 60 | −3.23R | +16.75R |
| 信号收盘 + 1000 万 USDT 流动性门槛 | 107 | +13.51R | 60 | −3.23R | +16.75R |
| 正式规则（再加止损距离 ≤15%） | 105 | +8.25R | 59 | **−5.48R** | +13.73R |

- 原窗口 48 笔 / 68.8% / PF 2.12 被逐笔复现，说明那组数字确实只属于 2026-03-31 至 2026-09-30；窗口之前的 12 个月三种口径全部为负（PF 0.83~0.90）。
- 1,000 万 USDT 流动性门槛一笔都没有拦住：翻倍后的标的 24h 报价成交额最低 6,600 万、中位 4.4 亿（`results/full_history/report.json` 的 `rejected` 只有 `risk_distance` 和 `stop_distance`）。
- 按全池仓位折算，正式规则在 18 个月上终值 1.12x、最大回撤 70.6%，新样本外段为 0.42x。
- 结论：`grid_0580` 参数应视为失效，而不是“待验证”。若要保留这个思路，需要用 2025 年的数据重新训练、用之后的区间验证。

产物：`results/full_history/report.json` 与各口径逐笔 CSV。
