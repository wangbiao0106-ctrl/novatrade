# Double Pump Exhaustion Short

正式规则：`double_pump_exhaustion_short`（日内翻倍动能衰竭确认做空）。策略针对高波动、流动性足够的 USDT 线性永续山寨币，只做确认后的动能衰竭空单，默认 2 倍杠杆和每笔账户权益 1% 风险预算。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器配置：[config/strategy.json](config/strategy.json)
- 研究证据：[research/README.md](research/README.md)
- 状态：正式规则，已接入 Swift 纸面/模拟运行时，默认停用，未获自动实盘资格

核心候选来自 `strategies/extreme_wick_short` 的 `grid_0580`。原研究报告和参数没有被覆盖；本目录只固定正式规则，便于后续单独做样本外验证和仿真盘验收；运行时只使用编译后的 Swift 默认参数。
