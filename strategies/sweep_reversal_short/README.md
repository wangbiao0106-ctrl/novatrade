# Sweep Reversal Short

山寨币二次扫顶做空（1h），已集成到 `StrategyType.sweepReversalShort` 和 `Sources/TradingService/StrategyEngine.swift`。

- 稳定策略标识：`sweepReversalShort`（`StrategyType.identifier`，程序匹配和路由使用）
- UI 显示名称：`山寨币二次扫顶做空（1h）`（`StrategyConfig.displayName`，修改文案不影响程序标识）

- 集成规则：[`STRATEGY.md`](STRATEGY.md)
- 研究材料和实验配置：[`research/`](research/)
- 集成配置：[`config/strategy.json`](config/strategy.json)
- 回测输出和标的池：`results/`

研究脚本从 `data/kline/okx/swap/5m` 读取 K 线，派生数组和结果写入本目录的 `results/`。生产执行只使用 Swift 源码，不读取研究目录。
