# DeepSeek AI 决策策略规则

## 规则身份

- 稳定 ID：`deepseek_ai_decision`
- 配置中心 ID：`deepseek`
- provider：DeepSeek Harness ACP
- 默认模型：`deepseek-v4-pro`
- 默认推理强度：`low`
- 规则版本：`1.0`
- 生命周期：`candidate`

本文件是人类可读的规则真源；机器字段见
[`config/strategy.json`](config/strategy.json)。运行时不得从 `strategies/`
读取配置，发布时只安装经校验的配置包。

## 决策契约

DeepSeek 仅作为严格 JSON 决策引擎。ACP profile 的工具、skill 和工作区上下文
不能成为交易权限；模型不能调用工具、读取 OKX 凭据、执行命令或直接下单。每轮
必须为固定观察池的每个合约返回一条 assessment，一次最多提交一个动作。

开仓必须通过服务端 freshness、可交易性、confidence、winRate、风险收益比、止损、
杠杆、日限额和冷却检查。平仓、撤单和持仓管理保留原有 policy/gateway 路径，
不能因为缩短模型输入而删除。

## 省 token 规则

本地指标继续用完整采集历史；只缩短模型收到的 K 线文本。profile overlay、列式
编码和 compact 窗口必须以真实 usage、延迟和回放结果验收，不能用 UTF-8 字节数
宣称 token 节省。

决策指纹包括已确认结构、价格/盘口/资金费关键分箱、可交易性、账户/风险/挂单、
每日 AI 开仓计数、配置和模型路由。指纹不变时预筛写入安全 hold 并跳过 ACP；模型
成功但被 policy 拒绝会推进指纹，模型/采集/未知数据/网关失败不会推进。持仓和
挂单存在时优先保证管理动作，不允许静默跳过。

## 分阶段开关

- `NOVATRADE_DEEPSEEK_PROFILE=stock|optimized`
- `NOVATRADE_DEEPSEEK_ENCODING=raw|compact20|compact10|compact60`
- `NOVATRADE_AI_EVENT_MODE=off|shadow|on`
- `NOVATRADE_AI_EVENT_MAX_SKIP_SECONDS` 控制 watchdog

生产默认关闭事件预筛和实验性 compact 窗口。任何 schema、policy、ACP、超时或
管理动作漏检上升都回滚到上一阶段；所有决策必须可由脱敏 snapshot 和规则版本重放。
