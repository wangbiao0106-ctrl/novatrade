# 双均线交易山寨币做多

这是 EMA20/60/120 趋势突破回踩的山寨币多头正式规则目录。规则已按 [`STRATEGY.md`](STRATEGY.md) §8「运行时映射契约」定稿并**集成到应用运行时**：`StrategyType.emaAltcoinLong`，引擎实现见 `Sources/TradingService/StrategyEngine.swift` 的 `evaluateEmaAltcoinLong`，**仅允许纸面/模拟盘下单**，不提供实盘自动下单入口。名称中的“双均线”指 EMA20 与 EMA60 的主趋势和回踩关系；EMA120 是长期趋势过滤，因此实现使用三条 EMA，不能把它简化成只有两条均线。

规则真源是 [`STRATEGY.md`](STRATEGY.md)，机器参数真源是 [`config/strategy.json`](config/strategy.json)。回测入口为 [`src/backtest.py`](src/backtest.py)，输入是 `data/kline/okx/swap/5m/`，结果只写入本目录 `results/`。

运行：

```bash
python3 strategies/ema_altcoin_long/src/backtest.py
```

最近一次正式版回测（2026-03-31 至 2026-09-28，训练 120 天、测试 60 天；已修正“BTC 门控不再暂停持仓出场”的缺陷，并把非加密资产排除清单对齐到规范真源后重跑；按当前 `config/strategy.json` 的单仓、1% 风险组合约束统计，见 [`results/summary.csv`](results/summary.csv)）：

| 区间 | 原始信号 | 并发拒绝 | 交易数 | 胜率 | 总净 R | 盈亏因子 | 最大回撤 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 训练 | 73 | 29 | 44 | 29.55% | -1.6226R | 0.9512 | 9.8370R |
| 样本外测试 | 36 | 10 | 26 | 26.92% | -3.0952R | 0.8465 | 8.9114R |

**当前配置在训练和样本外都为负收益**，样本外交易数 26 笔也低于最低观察门槛 30 笔，胜率远低于本策略自身的 40% 目标，因此本参数只能作为观察候选，不具备推进实盘的证据。此前文档引用的训练 73 笔 +9.2756R、样本外 35 笔 +8.2944R 是不设并发上限时全部原始信号的统计，与当前单仓配置不一致，不得作为当前配置的证据引用。

回测用训练期报价成交额前 50 个合规山寨币选定标的，测试期冻结该名单；实盘观察应改用当前滚动 24 小时成交额榜，并逐根确认 1 小时 K 线完整性。回测不含资金费率、盘口冲击、退市存活偏差和真实组合保证金占用，因此不得直接切换为实盘下单。

研究结果和限制见 [`results/summary.csv`](results/summary.csv) 以及 [`STRATEGY.md`](STRATEGY.md)。运行时实现位于 `Sources/TradingService/StrategyEngine.swift`（`evaluateEmaAltcoinLong`）、`Sources/TradingService/TradingService.swift`（动态多币种扫描、市价单、条件止损/止盈、96 小时时间离场、策略级单币种并发限制）与 `RiskEngine`（资金池按 `openRisk` / `openPositions` 执行 1% 开放风险与 1 笔并发上限），界面入口在 `Sources/MacTraderApp/main.swift`；参数默认值与实验室映射由 `python3 scripts/validate_strategy_sync.py` 逐项校验。
