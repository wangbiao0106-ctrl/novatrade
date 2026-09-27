# HLSR

高位流动性扫顶反转策略，当前处于研究和信号生成阶段。

- 规则：[`STRATEGY.md`](STRATEGY.md)
- 设计：[`DESIGN.md`](DESIGN.md)
- 配置：[`config/signal.json`](config/signal.json)
- 回测与信号源码：`src/`
- Python 测试：`tests/`
- 报告、参数网格和标的缓存：`results/`

默认 K 线输入为 `data/kline/okx/swap/15m`；5 分钟导出用于 HLSR 的独立回测时读取 `data/kline/okx/swap/5m`。策略源码可以在规则定稿后移植到 `Sources/TradingService/`，运行时不依赖本目录。
