# DeepSeek AI 决策策略规则

## 规则身份

- 稳定 ID：`deepseek_ai_decision`
- 配置中心 ID：`deepseek`
- provider：DeepSeek Harness ACP
- 默认模型：`deepseek-v4-pro`
- 默认推理强度：`low`
- 规则版本：`1.0`
- 生命周期：`candidate`

本文件是 DeepSeek provider 差异的规则真源；公共决策、开仓、持仓管理、保护单和故障安全规则见
[`spec/AI_DECISION_POLICY.md`](../../spec/AI_DECISION_POLICY.md)。本文件是 DeepSeek
provider 的策略包入口和差异补充；机器字段见
[`config/strategy.json`](config/strategy.json)。运行时不得从 `strategies/` 读取文件，
发布时只安装经校验的配置包。

## DeepSeek 专属边界

- DeepSeek 通过本地 ACP Harness 返回严格 JSON。ACP profile、工具、skill 和工作区
  上下文不能成为交易权限；模型不能读取 OKX 凭据、执行命令或直接下单。
- 默认路由为 `deepseek-v4-pro / low`，默认 profile 为 `optimized`，默认输入编码为
  `compact60`。profile overlay 和紧凑编码只能减少无关输入，不能改变快照、schema、
  policy 或订单网关语义。
- `NOVATRADE_DEEPSEEK_PROFILE=stock|optimized` 和
  `NOVATRADE_DEEPSEEK_ENCODING=raw|compact20|compact10|compact60` 只用于已记录的
  实验阶段。缩短 K 线文本时，本地行情事实仍使用完整采集历史，并保留
  `confirmed` 语义。

## 事件预筛与验收

事件预筛默认关闭。`NOVATRADE_AI_EVENT_MODE=off|shadow|on`、
`NOVATRADE_AI_EVENT_DRIVEN=1` 和 `NOVATRADE_AI_EVENT_MAX_SKIP_SECONDS` 遵循公共规则
中的 fingerprint、managed-state 和安全 hold 语义；有持仓、挂单或数据质量异常时
不得静默跳过模型。

profile、编码和事件预筛必须用真实 usage、延迟和脱敏快照回放验收。任何 schema、
policy、ACP、超时或管理动作漏检上升时，回滚到上一个阶段或恢复
`NOVATRADE_DEEPSEEK_PROFILE=stock`、`NOVATRADE_DEEPSEEK_ENCODING=compact60`，并保留
审计和失败夹具。
