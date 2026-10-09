# Codex AI 决策策略

这是 Codex 的 AI 决策策略实验室包。模型调用规则入口是
[`STRATEGY.md`](STRATEGY.md)，决策规则见
[`spec/AI_DECISION_POLICY.md`](../../spec/AI_DECISION_POLICY.md)，机器配置是
[`config/strategy.json`](config/strategy.json)。

当前状态：`candidate`。该目录记录策略边界、默认参数和省 token 的事件预筛方案；
运行时由 backend 的 Codex worker 和配置中心 API 管理，不从本目录读取文件。
本目录没有 `manifest.json`，因此不会被打包安装；`config/strategy.json` 只是
`AIConfig` 默认值的可读副本，其一致性由 `tests/test_contract.py` 逐字段锁定。

- 配置中心 ID：`codex`
- 实验室包 ID：`codex_ai_decision`
- provider：`codex`，固定 `gpt-6-luna / medium`
- 规则版本：1.5；默认每 10 分钟开始一次扫描，采集和推理耗时计入周期；事件预筛
  默认关闭，开启前需完成回放验收
- 15m 原始 OHLCV 优先；取消 4H 趋势和 20 根历史硬门槛，由 AI 综合多空判断。
  行情、资金费、持仓量、多空账户比和主动买卖量优先使用 OKX v5 API；补充盘口、
  RSI、波动率、量比、滚动加权均价及 BTC/ETH 背景。情绪指数和美联储官方消息
  是注明范围和时间的可选外部证据；不足窗口/来源失败明确标注，不虚构。
- 默认账户环境为本地纸面交易，启用后使用 `paper-active`；真实公共行情与本地撮合
  的执行边界见公共规则中的“账户模式与执行边界”，初始资金为 5000 USDT
- AI 入场默认使用止损后的 4 小时同品种冷却，并在 60 分钟内累计 2 次止损后暂停
  全部 AI 新开仓；状态持久化在纸面账本中，手动平仓和保护管理仍可执行
- 账户级日损熔断固定为 UTC 日初权益的 5%（`ACCOUNT_DAILY_LOSS_PERCENT`），
  达到即锁存并只拒绝新开仓
- 连续 3 次模型/流程失败进入持久化 `halted`：重启或调用启用接口都不会恢复，
  必须人工保存一次配置更新；`requireStopLoss`、止损闸门参数、`enabled`、`mode`
  与快照时效窗口都不在对话可建议字段内
- 开仓、平仓、撤单、持仓保护和分批止盈都必须经过服务端 policy 和共享订单网关
- v1.3 将新开仓预估胜率下限从 45% 提高到 50%，用于纸面观察；固定质量门槛
  发布于 `config/strategy.json` 的 `entry_quality`，由 `tests/test_contract.py` 对齐
  服务端常量。置信度按当前配置筛选，净盈亏比下限为 2.2
  50% 边界、低胜率观望和退出动作回归位于 `tests/test_win_rate_gate.py`

Swift 客户端通过 API 对齐配置与审计契约：参数设置可保存快照时效和三个止损闸门，
决策及逐币评估保留每档止盈的 `price` / `quantityPercent`，日志和明细表展示分批计划。
完整配置缺失快照窗口时默认 90 秒，历史记录缺失或为 null 的分批计划仍可读取；
退役配置键只兼容读取，不再写出。客户端契约验证位于
`Tests/OKXGatewayTests/AIRiskContractTests.swift` 和
`Tests/OKXGatewayTests/RiskDataQualityTests.swift`，不在运行时读取本目录。
扫描节奏与综合分析的回归回放分别位于 `tests/test_scan_schedule.py` 和
`tests/test_holistic_scan.py`；默认值及 15m 优先分析参数由 `tests/test_contract.py`
与服务端常量逐项对齐。快照有效期独立保持默认 90 秒。
数据源、缺失数据、外部消息、指标和事件指纹回归位于 `tests/test_market_context.py`。
单次扫描输入字节数和真实 provider token 用量可用
[`research/measure_scan_input.py`](research/measure_scan_input.py) 核验；使用现有纸面
账户做一次纯分析，不执行交易。重现命令见 [`research/README.md`](research/README.md)。
2026-10-10 的 10 标的核验输入为 119,535 tokens、提示正文 239,870 字节；
元数据见 [`results/scan_input_usage_2026-10-10.json`](results/scan_input_usage_2026-10-10.json)，
统计范围、逐周期数据量和来源说明见研究 README。

规则 v1.4 将 AI 新开仓统一改为限价挂单，不再按分析快照价格偏移拒绝挂单。
`config/strategy.json` 的 `entry_execution` 声明限价和交易所 `post_only` 路由，
`entry_preflight` 声明当前行情及净盈亏比门禁；运行时实现位于
`backend/ai_entry_preflight.py` 和 `backend/main.py`。
回放说明见 [`research/ENTRY_PREFLIGHT.md`](research/ENTRY_PREFLIGHT.md)，参数一致性和
拒绝/通过场景由 `tests/test_entry_preflight.py` 验证；复核拒绝保留评估，不计入失败熔断。

验证 AI worker 契约：

```bash
swift test
(
  export NOVATRADE_TRADING_MODE=exchange
  python3 scripts/test_ai_gateway.py
  python3 scripts/test_ai_trigger.py
  python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
)
```
