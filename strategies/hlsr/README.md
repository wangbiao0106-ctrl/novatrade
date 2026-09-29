# HLSR

高位流动性扫顶反转策略，当前处于研究和信号生成阶段。

- 规则：[`STRATEGY.md`](STRATEGY.md)（**唯一人类规则真源**）
- 设计：[`DESIGN.md`](DESIGN.md)（架构与流程；不重复定义规则）
- 配置：[`config/strategy.json`](config/strategy.json)（**唯一机器参数真源**：`signal_parameters`、`hard_filters`、`position_management`、`costs` 四个块都被源码读取）
- 回测与信号源码：`src/`
- Python 测试：`tests/`
- 报告、参数网格和标的缓存：`results/`

K 线输入是 `data/kline/okx/swap/5m` 的 5 分钟导出：`src/hlsr_market_export.py` 与 `src/hlsr_signal_generator.py` 直接读它并在内存中聚合为 15 分钟。历史版本里 `src/high_short_strategy.py` 与 `src/altcoin_backtest.py` 的默认数据目录写的是 `data/kline/okx/swap/15m`，该目录在本仓库中并不存在，必须用 `--data-dir` 指向 15 分钟缓存或改用 5 分钟入口。策略源码可以在规则定稿后移植到 `Sources/TradingService/`，运行时不依赖本目录。
