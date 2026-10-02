# Sweep Reversal Short

山寨币二次扫顶做空，策略实验室规则版本 1.4，已同步到 `StrategyType.sweepReversalShort` 和 `Sources/TradingService/StrategyEngine.swift`。

- 稳定策略标识：`sweepReversalShort`（`StrategyType.identifier`，程序匹配和路由使用）
- UI 显示名称：`山寨币二次扫顶做空`（`StrategyConfig.displayName`，修改文案不影响程序标识）

- 集成规则：[`STRATEGY.md`](STRATEGY.md)
- **上线规则绩效证据**：[`research/report_live_rule.py`](research/report_live_rule.py)（177 币基线 41 笔 / 56.1% / +0.37R，逐笔与 `research/live_signal.py` 对齐；`--pool live` 复现生产选币范围：55 笔 / 47.3% / +0.21R，单仓 26 笔 +11.58R，见 [`STRATEGY.md`](STRATEGY.md) §5）
- 研究材料和实验配置：[`research/`](research/)
- 执行与触发时机调优记录：[`research/EXECUTION_TUNING.md`](research/EXECUTION_TUNING.md)
- 中低流动性币专项调优：[`research/LOWMID_TUNING.md`](research/LOWMID_TUNING.md)
- 中低流动性币滚动验证：[`research/LOWMID_FOLLOWUP.md`](research/LOWMID_FOLLOWUP.md)
- 调优脚本：`research/tune_execution.py`、`research/run_grid.py`、`research/lowmid_followup.py`
- 研究层因果性回归测试：[`tests/test_causal_research.py`](tests/test_causal_research.py)；运行 `python3 -m unittest discover -s strategies/sweep_reversal_short/tests -p 'test_*.py'`（需要研究引擎的 pandas 依赖才能执行指标测试）
- 集成配置：[`config/strategy.json`](config/strategy.json)
- 回测输出和标的池：`results/`

研究脚本从 `data/kline/okx/swap/5m` 读取 K 线，派生数组和结果写入本目录的 `results/`。生产执行只使用 Swift 源码，不读取研究目录。

生产扫描范围固定为 `dynamic.sweepCandidates`（v1.4）：每 30 秒刷新行情，排除主流币、稳定币及非加密资产，再剔除 24h 报价成交额低于 300 万 USDT 的合约后，按成交额取前 100 个；报价成交额按 OKX ticker 的 `volCcy24h × last` 计算，不使用以张计的 `vol24h`。策略不保存固定币种名单。策略运行时扫描多个币种，但同一实例只允许一个币种下单和持仓。`config/universe_recommended.json` 的 177 个标的是历史回测快照，只用于复现研究结果。

当前同步范围包含 1h 结构、15m 收盘确认、市价入场、账户权益 sizing、条件止损/止盈、96 根 1h（384 根 15m）时间离场和账户级 5% mark-to-market 熔断；策略入场不再使用固定数量。

单笔风险预算按每个入场订单计算，固定使用授权时账户权益的 1%，硬上限为 1%，仓位由入场价与保护止损的相对距离反向缩放；可用资金不足、手续费、滑点或跳空会使实际仓位和损失与目标值不同。策略自带 `扫顶期间最高价 + 0.5 × ATR14` 的动态止损，界面只展示该规则，不另设固定止损百分比。已实现盈亏才回写本池滚仓，未实现浮盈不释放可用余额。

修改策略时先更新 `STRATEGY.md` 与 `config/strategy.json`，再同步 `Sources/`；运行 `python3 scripts/validate_strategy_sync.py` 验证同步状态。
