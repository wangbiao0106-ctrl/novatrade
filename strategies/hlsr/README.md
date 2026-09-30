# HLSR

高位扫顶反转策略，已接入本地策略引擎的 OKX 模拟盘和信号生成流程；默认禁用，不自动提交真实订单。

- 稳定标识：`hlsr`
- 英文名：`High-Level Liquidity Sweep Reversal`
- 界面显示：`高位扫顶反转做空`
- 运行实现：`Sources/TradingDomain/`、`Sources/TradingService/`、`Sources/TradingService/HLSRPositionManager.swift`、`Sources/OKXLocalD/`、`Sources/MacTraderApp/`

- 规则：[`STRATEGY.md`](STRATEGY.md)（**唯一人类规则真源**）
- 设计：[`DESIGN.md`](DESIGN.md)（架构与流程；不重复定义规则）
- 配置：[`config/strategy.json`](config/strategy.json)（**唯一机器参数真源**：`signal_parameters`、`hard_filters`、`position_management`、`costs` 四个块都被源码读取）
- 回测与信号源码：`src/`
- Python 测试：`tests/`（包含 K 线完整性与信号序列化回归：`python3 -m unittest discover -s strategies/hlsr/tests -p 'test_*.py'`）
- 报告、参数网格和标的缓存：`results/`

K 线输入是 `data/kline/okx/swap/5m` 的 5 分钟导出：`src/hlsr_market_export.py` 与 `src/hlsr_signal_generator.py` 直接读它并在内存中聚合为 15 分钟。历史版本里 `src/high_short_strategy.py` 与 `src/altcoin_backtest.py` 的默认数据目录写的是 `data/kline/okx/swap/15m`，该目录在本仓库中并不存在，必须用 `--data-dir` 指向 15 分钟缓存或改用 5 分钟入口。运行时不会读取本目录；正式实现只使用确认的 15m + 已完成 4H 数据和 `volCcyQuote` 报价成交额。

`altcoin_backtest.py` 的候选池默认只使用从窗口起点开始、至少覆盖最初 60 天且 15 分钟 K 线连续完整的本地训练片段，并按这 60 天成交额排序；训练片段之后的数据不足会在报告的 `data_quality` 中标记，不会改变选币。历史训练缓存不足时默认保留已有历史候选，不再把当前成交额带入历史选择。若只是交互式探索，可显式传入 `--allow-live-selection-fallback`，此时报告会记录 `selection_method`，并标明当前 live 合约成交额造成的选择前视。

`high_short_strategy.py` 的本地 15 分钟缓存排名采用同样的窗口起点、时间戳连续性、OHLCV 有限值和成交额校验；缓存文件仍放在 `results/` 或指定的 `--data-dir` 下，不复制到 `data/kline/` 之外的目录。

研究接受标准目前为 **FAIL**：样本外只有 8 笔、胜率 37.5%、平均净 R +0.455，不能把 HLSR 描述为已证明盈利的策略。
