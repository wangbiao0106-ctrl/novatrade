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
- 默认轮询：30 秒；事件预筛默认关闭，开启前需完成回放验收
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

Swift 客户端通过 API 对齐配置与审计契约：参数设置可保存快照时效和三个止损闸门，
决策及逐币评估保留每档止盈的 `price` / `quantityPercent`，日志和明细表展示分批计划。
完整配置缺失快照窗口时默认 90 秒，历史记录缺失或为 null 的分批计划仍可读取；
退役配置键只兼容读取，不再写出。客户端契约验证位于
`Tests/OKXGatewayTests/AIRiskContractTests.swift` 和
`Tests/OKXGatewayTests/RiskDataQualityTests.swift`，不在运行时读取本目录。

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
