# Codex AI 决策策略规则

## 规则身份

- 稳定 ID：`codex_ai_decision`
- 配置中心 ID：`codex`
- provider：Codex CLI
- 默认模型：`gpt-6-luna`
- 默认推理强度：`medium`
- 规则版本：`1.0`
- 生命周期：`candidate`

本文件是人类可读的规则真源；机器字段见
[`config/strategy.json`](config/strategy.json)。运行时不得从 `strategies/`
读取配置，发布时只安装经校验的配置包。

## 决策范围

每轮快照包含固定观察池中每个合约的完整 assessment。模型只能返回严格 JSON
决策意图，不能调用工具、读取凭据、执行交易命令或直接下单。一次决策最多选择
一个动作：`open`、`close`、`cancel` 或 `hold`。

开仓必须同时满足服务端的观察池、账户可交易性、快照时效、confidence、winRate、
风险收益比、止损、杠杆、日限额和冷却检查。平仓和撤单不被开仓质量阈值阻断，
但仍须核验当前持仓或挂单的 instrument、方向、数量和订单 ID。

## 省 token 事件预筛

行情采集仍按配置周期运行；模型调用只在以下状态变化时发生：

1. 已确认 K 线结构或 forming K 线关键分箱变化；
2. ticker、盘口、资金费率、可交易性或风险状态变化；
3. 持仓、待处理挂单、账户余额、每日 AI 开仓计数或配置/模型路由变化；
4. 首次运行或 watchdog 达到最长跳过时间。

指纹不变时由服务端写入 `decisionSource=prescreen` 的安全 hold，不伪装成模型
评估。模型成功给出但被 policy 拒绝的决策会推进指纹，避免静态拒绝重复消耗；
模型、采集、未知账户状态或网关失败不会推进指纹，下一轮继续重试。

事件预筛默认关闭。启用前需要用脱敏快照回放证明 assessment 覆盖率为 100%，并且
开仓、平仓、撤单和账户数据缺失场景没有漏检。

## 安全和回滚

默认 `shadow`/关闭开仓。进入 `demo-active` 或 `live-armed` 仍须人工启用，实盘
还须单独打开 live trading 开关。任何 schema、policy、网关、超时或 provider 错误
都按 hold 处理；连续失败达到上限后 worker halted。回滚只需关闭事件预筛或恢复
`NOVATRADE_DEEPSEEK_ENCODING=compact60` 等独立开关，不修改审计记录。
