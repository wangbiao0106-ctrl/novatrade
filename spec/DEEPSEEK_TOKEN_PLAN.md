# NovaTrade DeepSeek AI 策略省 Token 方案 v2

> 状态：P0、P1 的安全基础和 P3 事件预筛已落地；P2 单调用仲裁仍保持关闭，P4 的时间冷却预筛已废弃。当前重复开仓只由实时持仓和活动挂单门禁控制。
>
> 本方案不把 UTF-8 字节数当作 token，也不把传输压缩当作模型费用节省。所有收益数字必须来自 DeepSeek usage、端到端延迟和决策回放。

## 0. 目标和边界

目标是降低每轮 DeepSeek 决策的实际输入 token、输出 token 和无效调用次数，同时保持现有的服务端校验、下单闸门和故障安全行为。

本方案不改变以下约束：

- 服务端仍是唯一的决策校验和下单入口；模型不能直接调用交易工具。
- 快照、决策、拒绝原因和模型路由继续写入审计记录。
- 开仓仍须通过 freshness、可交易性、信心、胜率、风险收益比、止损、杠杆、日限额和实时敞口检查；同合约固定时间冷却不再是门槛。
- 平仓、撤单和持仓管理不能因为优化输入而被静默删除。
- 研究配置放在 `strategies/<strategy_name>/`；运行时不能从 `strategies/` 读取文件。

当前生产日志中的“10 个标的、4 个周期、每周期 60 根”只是一个观测样本，不是代码保证的固定上限。现有配置允许不同数量的 `allowedInstruments`。

## 1. 先固定测量口径

### 1.1 每次 ACP 请求记录的字段

在 `backend/deepseek_harness.py` 增加一个白名单 usage 解析器，保留厂商返回的原始字段映射，并统一写入以下字段。字段拿不到时写 `null`，不能用字节数补算：

- `inputTokens`、`outputTokens`、`reasoningTokens`；
- `cacheReadTokens`、`cacheWriteTokens`（厂商提供时）；
- `initializeSeconds`、`sessionCreateSeconds`、`configSeconds`、`promptSeconds`、`closeSeconds`、`totalSeconds`；
- `basePromptBytes`、`strictContractBytes`、`schemaBytes`、`modelPayloadBytes`；
- `profile`、`model`、`reasoningEffort`、`sessionId`、`requestId`、`outcome`。

`modelPayloadBytes` 只表示发给模型的文本大小；ACP 初始化、profile 注入、网络传输和模型 input tokens 分开统计。`snapshotQuality.promptBytes` 不能继续被当作完整 DeepSeek 输入大小。

### 1.2 基线数据集

建立三类可重复输入：

1. 10 个标的、4 个周期、60 根 K 线的生产形状快照；
2. 2、4、10、20、40 个标的的规模梯度快照；
3. 包含开仓候选、全部 hold、持仓需平仓、挂单需撤单、账户数据缺失和过期快照的回放夹具。

每个夹具保存脱敏的完整 snapshot、配置 hash、模型路由和预期的服务端 policy 结果。研究数据只放在 `strategies/deepseek_ai_decision/research/` 或 `results/`，不写入 `data/kline/`。

### 1.3 基线报告

先跑现有“分组分析 + 协调器”路径，至少取得 100 个夹具回放和 24 小时 shadow 日志，报告：

- 每轮的实际 input/output/reasoning/cache token；
- 每轮从开始到开始的间隔，而不是把 `decisionIntervalSeconds` 当作完整周期；
- p50/p95 总延迟和各阶段延迟；
- schema 错误、超时、provider 错误、policy 拒绝、网关拒绝分别计数；
- 每个标的 assessment 完整率、`entryEligible`、价格几何和最终 policy 结果。

在 usage 不可观测前，不写“每小时消耗多少 token”的确定数字。

## 2. P0：先修正 profile，再测真实收益

现有 `backend/deepseek_profile.py` 的 prompt-only profile 只移除决策不会使用的工具、skill 和工作区上下文，不改变决策 prompt、schema 和 policy。但当前实现假设 `--profile novatrade-decision --patch` 可以临时创建 profile；真实 dsh 可能要求先注册该 profile。必须先解决这个启动契约。

### 实施

- 首选使用已存在的 `acp` profile 加 patch，除非能在安装阶段可靠注册 `novatrade-decision` profile。
- 启动阶段执行真实 dsh smoke test：`initialize`、`session/new`、设置 model/effort、发送最小合法 JSON 请求，并校验返回对象。
- profile 文件不可写或 patch 被 dsh 拒绝时，自动重试一次 stock `acp`；回退原因写入审计。不能只在文件写入失败时回退。
- 在真实 DeepSeek 环境各跑一组 stock profile 和 optimized profile，比较 usage，而不是比较 patch 文件大小。

### 验收

- 两种 profile 都能完成合法 JSON round-trip；
- profile 失败不会阻塞 worker，也不会绕过现有 fail-closed 逻辑；
- `inputTokens`、端到端延迟和失败率有真实差异数据；
- 未获得真实 usage 前，P0 只能标记为“启动参数已验证”，不能宣称已节省固定 token。

## 3. P1：无损紧凑编码

### 3.1 本地计算与模型输入分离

行情采集和本地事实计算继续保留足够历史。当前 `market_facts` 需要 SMA50、21 根成交量和 ATR14，不能把采集源直接截成 10 根。

模型 prompt 可以实验性减少原始 K 线窗口，但本地计算仍使用完整可用历史：

- A：60 根确认 K 线；
- B：最近 20 根确认 K 线；
- C：最近 10 根确认 K 线加最新 forming K 线；
- 所有方案都发送 ticker 当前价，并保留 `confirmed` 语义。

### 3.2 文本编码

第一版只使用模型能直接读取的无损文本，不使用 gzip 或 base64：

当前代码已提供 `raw`、默认 `compact60` 列式编码，以及实验性的 `compact20`/`compact10` 窗口开关。后两者只减少模型收到的历史行数，不改变本地指标计算，也不应在回放验收完成前作为生产默认值。

- 每个周期只发送一次升序时间戳起点和 delta；
- OHLCV 使用固定字段顺序数组；
- 价格按该合约 `tickSize` 转为整数 tick，无法取得 tickSize 时保留十进制字符串；
- confirmed 使用位图或布尔数组；
- 本地保留原始 JSON snapshot，模型 prompt 只使用紧凑副本。

每个编码器都必须提供反解函数。验收时对反解后的 canonical candle 做 hash，要求字段、顺序、数值和确认状态完全一致。

### 3.3 schema 处理

先保留现有严格 schema，单独测量 `strictContractBytes` 和 `schemaBytes`。只有在以下条件都满足时才允许改成更短的输出契约：

- 服务端 `AIDecision.from_dict` 和 policy 测试全部通过；
- 缺字段、额外字段、错误类型和 assessment 覆盖率的拒绝行为不变；
- 100 个以上回放夹具的方向、价格几何、开仓资格和 policy 结果无系统性变化。

## 4. P2：单调用试验和安全仲裁

不直接删除现有协调器。先实现 shadow-only 的单调用路径，并与当前路径使用同一个 snapshot、配置和模型路由。

### 4.1 单调用输出

单调用必须仍然返回每个 observed contract 恰好一份 assessment。观察池数量超过明确上限时继续分组；不能把 10 个标的的预算外推到任意数量。建议先把自动轮询上限设为 10，并对配置写入做服务端校验。

### 4.2 开仓仲裁

只有在没有持仓、没有待处理挂单、账户/风险/可交易性数据完整时，才允许用确定性仲裁替代“开仓协调”模型调用。服务端按以下顺序过滤：

1. `entryEligible=true` 且无 `unmetConditions`；
2. 当前配置的 confidence、winRate、riskRewardRatio、止损和价格几何门槛；
3. freshness、交易可用性、日限额、账户风险以及当前持仓和活动挂单状态；
4. 按 `confidence`、`riskRewardRatio`、`winRate`、配置顺序排序，最后用 instrument ID 做稳定 tie-break；
5. 最多选择一单，没有候选则 hold。

这些阈值全部从当前 `AIConfig` 和 policy 常量读取，不能硬编码生产配置中的某个值。

### 4.3 持仓和挂单管理

当前协调器要求真实 positions 和 pending order IDs。快照采集必须补充待处理挂单；数据缺失时不得猜测为零。

在持仓或挂单存在时，先保留现有协调器，或新增只包含管理对象的 management call。close/cancel/early-close 的语义、ID 校验和 `allowClose`/`allowCancel` 必须继续走现有 policy 和 gateway。

### 4.4 验收

- 同一 snapshot 的单调用和分组路径都通过 schema、policy 和 gateway 测试；
- 对开仓候选、无候选、多个候选、平仓、撤单和账户缺失夹具逐一比对；
- 记录 input/output/reasoning token 和 p95 延迟；
- 单调用失败只产生安全 hold，不得提交未验证动作；
- shadow 通过后再在 demo-active 启用，保留一键切回分组路径的配置开关。

## 5. P3：无变化跳过

不能使用 `snapshotId` 判断“无变化”，因为它包含每轮采集时间。新增独立的 `decisionFingerprint`，只包含会改变决策的字段：

- 最新确认 K 线时间、结构摘要 hash 和 forming K 线状态；
- ticker 价格相对关键位/ATR 的区间变化；
- order book、资金费率和可交易性变化；
- positions、pending orders、账户余额、风险闸门和配置 hash；
- profile、model、reasoning effort 和结构规则版本。

只有在无持仓、无挂单、无账户/风险变化、无开仓候选变化且 fingerprint 不变时，才可以跳过模型。跳过必须写入一条 `decisionSource=prescreen` 的审计记录，并由服务端生成安全 hold；不能伪装成模型已经分析过完整 assessment。

预筛先以 shadow 运行 24 小时，统计应调用但被跳过的漏检率、跳过后下一次真实调用是否出现新的候选或管理动作，以及实际减少的模型调用和 token。

## 6. P4：时间冷却已废弃

旧版本曾计划用同合约时间冷却减少重复开仓，但当前规则已经移除该门槛。重复开仓
只由服务端实时检查当前持仓、活动挂单和未知提交 reservation 控制；撤单或平仓后，
只有新的快照再次通过完整开仓门禁才允许开仓。持仓、挂单和 reduce-only 管理动作不能
被任何历史冷却字段屏蔽。

`cooldownSeconds` 和 `NOVATRADE_DEEPSEEK_COOLDOWN_PREFILTER` 只保留用于读取旧配置
的兼容信息，不得作为新策略参数、提示词门禁或事件指纹依据。验收应覆盖成交后、
部分成交、状态未知、重启和 Codex/DeepSeek 共享网关的实时敞口场景。

## 7. 回放、版本和目录

研究夹具和 A/B 结果使用：

```text
strategies/deepseek_ai_decision/
├── README.md
├── STRATEGY.md
├── config/strategy.json
├── research/
├── tests/
└── results/
```

规则版本、结构摘要 schema 和 fixture manifest 在该目录维护；如果现有 `validate_strategy_sync.py` 不覆盖该策略，则新增专用校验或扩展其映射。运行时的 `ai_structure.py` 不读取这个目录；发布时把已验证的规则编译进 backend 或导入应用数据目录的 strategy package，并在审计记录 `structureVersion` 和 `structureRulesHash`。

精确回放需要保存完整的脱敏 snapshot、结构摘要、配置 hash、模型路由和原始决策输入。历史行情使用 `data/kline/` 的原始 5m 数据，通过 `.cache/kline/okx/swap/` 重建多周期缓存；不能假设仓库中存在原始 15m/1h/4h 文件。

## 8. 分阶段开关和回滚

每项优化独立配置：

- `NOVATRADE_DEEPSEEK_PROFILE`：stock / optimized；
- `NOVATRADE_DEEPSEEK_ENCODING`：raw / compact20 / compact10；
- `NOVATRADE_DEEPSEEK_WORKFLOW`：grouped / single-shadow / single；
- `NOVATRADE_AI_EVENT_DRIVEN`：0 / 1（当前实现的事件预筛开关，默认关闭）；
- `NOVATRADE_AI_EVENT_MODE`：off / shadow / on（`shadow` 只审计潜在跳过，不减少模型调用）；
- `NOVATRADE_AI_EVENT_MAX_SKIP_SECONDS`：连续无变化时的最长模型跳过时间，默认 900 秒；

任何 schema 错误、policy 拒绝率、超时率、管理动作漏检或 provider 错误上升，都自动退回上一阶段，并保留失败夹具和 usage 记录。

## 9. 最终验收指标

只有真实 usage 和回放结果满足以下条件，才允许把优化设为默认：

- 实际 input token 相对基线下降至少 35%；stretch 目标为 50%，不预先承诺 70%；
- output/reasoning token、p95 总延迟和 provider 错误率没有系统性上升；
- assessment 覆盖率 100%，schema/policy/gateway 测试全部通过；
- 开仓、平仓、撤单、实时敞口和账户缺失夹具没有漏放动作；
- 预筛漏检率为 0，或明确保持关闭；
- 所有线上决策都能通过保存的 snapshot 和规则版本重放。

最终成本报告使用真实 usage 字段计算：

```text
每小时实际 token = 该小时所有请求的 input + output + reasoning token 之和
实际轮间隔 = 相邻决策开始时间之差
```

UTF-8 字节数、gzip 大小和模型 token 作为三个独立指标展示，不能互相替代。
