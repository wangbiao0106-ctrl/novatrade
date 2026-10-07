# Codex AI 决策策略规则

## 规则身份

- 稳定 ID：`codex_ai_decision`
- 配置中心 ID：`codex`
- provider：Codex CLI
- 默认模型：`gpt-6-luna`
- 默认推理强度：`medium`
- 规则版本：`1.0`
- 生命周期：`candidate`

本文件是 Codex provider 差异的规则真源；公共决策、开仓、持仓管理、保护单和故障安全规则见
[`spec/AI_DECISION_POLICY.md`](../../spec/AI_DECISION_POLICY.md)。本文件是 Codex
provider 的策略包入口和差异补充；机器字段见
[`config/strategy.json`](config/strategy.json)。运行时不得从 `strategies/` 读取文件，
发布时只安装经校验的配置包。

## Codex 专属边界

- Codex CLI 只接收脱敏快照并返回严格 JSON 决策意图，不能调用工具、读取凭据、
  执行命令或直接下单。服务端 schema、policy 和共享订单网关始终是最终授权边界。
- 自动轮询固定使用 `gpt-6-luna / medium`。旧路由字段只用于兼容迁移，不能改变
  自动决策模型或推理强度。
- 观察池超过 4 个合约时，使用分组分析后由同一模型协调；每组只提供建议，最终
  决策仍须覆盖完整观察池并经过公共 policy。

## 事件预筛与回滚

事件预筛默认关闭。启用时遵循公共规则中的 fingerprint、managed-state 和
`decisionSource=prescreen` 语义；首次运行、行情/账户/风险/可交易性/路由变化、
持仓或挂单管理状态变化必须触发模型。模型、采集、未知账户状态或网关失败不会
推进成功指纹。

启用前需用脱敏快照回放证明 assessment 覆盖率为 100%，且开仓、平仓、撤单和数据
缺失场景没有漏检。发生 schema、policy、provider、超时或管理动作漏检时，关闭事件
预筛即可回到完整轮询；该回滚不修改审计记录。
