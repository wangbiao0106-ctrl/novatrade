# Double Pump Exhaustion Short

正式规则：`double_pump_exhaustion_short`（日内翻倍动能衰竭确认做空）。策略针对高波动、流动性足够的 USDT 线性永续山寨币，只做确认后的动能衰竭空单，默认 2 倍杠杆；每笔名义仓位 = 本策略资金池可用余额 × 1，止损距离超过 15% 不下单。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器配置：[config/strategy.json](config/strategy.json)
- 研究证据：[research/README.md](research/README.md)
- 状态：正式规则，已接入 Swift 运行时，默认停用；启动后按连接账号类型路由模拟或实盘
- 全历史复核：[`research/README.md`](research/README.md)（18 个月复核显示该参数在新样本外为负，应视为失效）

核心候选来自 `strategies/extreme_wick_short` 的 `grid_0580`。原研究报告和参数没有被覆盖；本目录只固定正式规则，便于后续单独做样本外验证和仿真盘验收；运行时只使用编译后的 Swift 默认参数。
