# Sweep Reversal Short

山寨币二次扫顶做空（1h），策略实验室规则版本 1.3，已同步到 `StrategyType.sweepReversalShort` 和 `Sources/TradingService/StrategyEngine.swift`。

- 稳定策略标识：`sweepReversalShort`（`StrategyType.identifier`，程序匹配和路由使用）
- UI 显示名称：`山寨币二次扫顶做空（1h）`（`StrategyConfig.displayName`，修改文案不影响程序标识）

- 集成规则：[`STRATEGY.md`](STRATEGY.md)
- **上线规则绩效证据**：[`research/report_live_rule.py`](research/report_live_rule.py)（177 币基线 41 笔 / 56.1% / +0.37R，逐笔与 `research/live_signal.py` 对齐）
- 研究材料和实验配置：[`research/`](research/)
- 执行与触发时机调优记录：[`research/EXECUTION_TUNING.md`](research/EXECUTION_TUNING.md)
- 中低流动性币专项调优：[`research/LOWMID_TUNING.md`](research/LOWMID_TUNING.md)
- 中低流动性币滚动验证：[`research/LOWMID_FOLLOWUP.md`](research/LOWMID_FOLLOWUP.md)
- 调优脚本：`research/tune_execution.py`、`research/run_grid.py`、`research/lowmid_followup.py`
- 集成配置：[`config/strategy.json`](config/strategy.json)
- 回测输出和标的池：`results/`

研究脚本从 `data/kline/okx/swap/5m` 读取 K 线，派生数组和结果写入本目录的 `results/`。生产执行只使用 Swift 源码，不读取研究目录。

生产标的范围固定为 `dynamic.hotAltcoins`：每 30 秒刷新行情，排除主流币、稳定币及非加密资产后，按 24h 报价成交额取前 20 个；策略不保存固定币种名单。`config/universe_recommended.json` 的 177 个标的是历史回测快照，只用于复现研究结果。

当前同步范围包含 1h 结构、15m 收盘确认、市价入场、资金池 sizing、条件止损/止盈、96 根 1h（384 根 15m）时间离场和账户级 5% mark-to-market 熔断；策略入场不再使用固定数量。

单笔风险按每个入场订单计算，默认上限为授权时策略资金池权益的 1%，仓位由入场价与保护止损的相对距离反向缩放；可用资金不足、手续费、滑点或跳空会使实际仓位和损失与目标值不同。已实现盈亏才回写本池滚仓，未实现浮盈不释放可用余额。

修改策略时先更新 `STRATEGY.md` 与 `config/strategy.json`，再同步 `Sources/`；运行 `python3 scripts/validate_strategy_sync.py` 验证同步状态。
