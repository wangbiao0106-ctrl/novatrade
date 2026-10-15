# 下单前复核回放

规则与参数由 [`../STRATEGY.md`](../STRATEGY.md) 和
[`../config/strategy.json`](../config/strategy.json) 的 `entry_preflight` 定义。
使用固定时间、脱敏快照和伪造盘口的确定性回放，验证多空方向、限价挂单位置、价差、
标记价格触及止损、费用后盈亏比及纸面/交易所的提交边界。

验收入口：

```bash
NOVATRADE_TRADING_MODE=exchange python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_entry_preflight.py'
```

拒绝场景应验证没有订单 POST、没有新增纸面订单、reservation 和日开仓额度未被
占用，worker 保留逐币评估并记录安全 hold。通过场景允许原计划提交，限价不会被
修改为追价。此回放只证明执行门禁，不提供交易收益或避免止损的证据。

## 历史 v1.4 下单前复核记录（已由当前 v1.9 契约 supersede）

2026-10-09 v1.4 验收结果：23 个下单前复核测试通过，覆盖大幅快照偏移后的多空
限价保留、拒绝等价/穿价挂单、滞后的成交价与盘口冲突、交易所 `post_only` 路由、
纸面挂单等待后续行情成交，以及手动市价/AI 平仓可用性。
相关执行、行情、撮合、触发器、FastAPI 和策略契约回归合计 186 项通过；另有
AI 网关 policy/评估保留回归 30 项通过。Swift 191 项通过，同步与差异检查均通过。

本次 Python 回归主入口（186 项）：

```bash
NOVATRADE_TRADING_MODE=exchange python3 -m unittest \
  scripts.test_ai_execution scripts.test_ai_snapshot \
  scripts.test_ai_trigger scripts.test_ai_market_facts scripts.test_fastapi_gateway \
  scripts.test_paper_mode scripts.test_paper_trading \
  strategies.codex_ai_decision.tests.test_contract \
  strategies.codex_ai_decision.tests.test_scan_schedule \
  strategies.codex_ai_decision.tests.test_entry_preflight
```

网关 policy/评估保留回归（30 项）：

```bash
NOVATRADE_TRADING_MODE=exchange python3 - <<'PY'
import unittest
from scripts.test_ai_gateway import AIGatewayTests
prefixes = ('test_policy_', 'test_current_', 'test_selected_', 'test_open_',
            'test_rejected_', 'test_gateway_failure_', 'test_repeated_gateway_',
            'test_eligible_', 'test_proposed_', 'test_valid_open_', 'test_named_hold_',
            'test_entry_freshness_', 'test_server_freshness_')
names = [name for name in unittest.defaultTestLoader.getTestCaseNames(AIGatewayTests)
         if name.startswith(prefixes)]
result = unittest.TextTestRunner().run(unittest.TestSuite(AIGatewayTests(name) for name in names))
raise SystemExit(not result.wasSuccessful())
PY
```

当前全量网关和 4H 回归按默认 grouped workflow 验证逐组分析与 coordinator 汇总；
`analysisWorkflow="single"` 的单次整池路径仍由独立回归覆盖，作为运维覆盖保留。

## 历史 v1.5 综合分析记录（已由 v1.9 supersede）

2026-10-10（北京时间）的旧版本曾移除 4H 趋势和 20 根历史硬门槛，优先发送 15m
原始 OHLCV；这段记录只保留历史背景。v1.9 已恢复 4H 主入场周期、至少 20 根有效
已确认 K 线和多空趋势一致性门禁。`tests/test_holistic_scan.py` 现覆盖这些门禁、
两个方向与 2.2 的开仓边界。
`tests/test_market_context.py` 覆盖 OKX v5 持仓量、币种级 5m 多空账户比和主动
买卖量，情绪/官方美联储消息的来源时间、缓存、失败和正文摘录，以及 RSI、
滚动典型价格加权均价、波动率、数据传递、快照哈希及事件指纹。

模型质量及真实限价复核的净盈亏比下限均为 2.2；分批目标按最近档检查。
多空净盈亏比边界回放证明，账面 2.2 但扣除费用/滑点后不足的方案不能下单。
可选数据的限时与 Rubik 分接口限频不会移除观察合约；失败不补零。

该历史版本验收：策略目录 72 项、核心行情/执行/撮合/FastAPI 回归 146 项、网关
policy/评估保留回归 30 项，共 248 项 Python 测试通过；Swift 191 项通过。
联网只读核验 BTC 与 STRK 的三个 OKX 衍生品接口、BTC/ETH 报价、情绪指数、
美联储 RSS 和最新 FOMC 声明正文成功。行情全部优先 OKX v5；全局情绪指数和
美联储消息分别来自 Alternative.me 和美联储官网，不代表逐币情绪或未来日程。
旧全量网关分组/协调用例仍不是本次验收入口；没有将其计为通过。

```bash
NOVATRADE_TRADING_MODE=exchange python3 -m unittest discover \
  -s strategies/codex_ai_decision/tests -p 'test_*.py'
NOVATRADE_TRADING_MODE=exchange python3 -m unittest \
  scripts.test_ai_execution scripts.test_ai_snapshot scripts.test_paper_mode \
  scripts.test_paper_trading scripts.test_ai_trigger scripts.test_ai_market_facts \
  scripts.test_fastapi_gateway
python3 scripts/validate_strategy_sync.py
swift test --disable-automatic-resolution
git diff --check
```

以上是源码与只读数据验证；未重新打包或重启运行中的应用/后台，没有提交真实订单。

### 历史 v1.5 打包与重启核验

该历史版本随后按用户要求执行 `./scripts/build_and_run.sh package` 和
`./scripts/build_and_run.sh --verify`，已生成 `dist/NovaTrade.dmg` 并重启
该工作区的 NovaTrade 与后台。旧应用/后台 PID 已替换，新后台 `/health` 返回
`ok`，账户环境为 `paper`；策略 API 返回 `market-facts-v2`。
打包后的八个相关 backend 源文件与当前源码 SHA-256 一致，包内优先周期为
15m、最少有效收盘数据为 1 根，模型及净盈亏比下限均为 2.2；应用 bundle 的
`codesign --verify --deep --strict` 通过。

## 历史 v1.6 行情价格先于盈亏比筛选记录（已由当前 v1.9 契约 supersede）

2026-10-10（北京时间）：逐币评估曾全部返回 2.25；运行时和界面没有该固定值，
示例 ZK 的目标 0.0144375 恰好等于 0.01365 + (0.01365 - 0.01330) × 2.25。
旧提示要求按方案价格计算比值，但没有明确禁止按门槛反推价格，且要求模型预算
净盈亏比，可能诱导模型为筛选门槛设计目标。该现象不足以证明所有目标均无行情依据。

新提示明确先按每币行情确定入场、失效止损和合理目标，再计算最近目标的盈亏比，
最后应用下单门槛；三个价格都需解释行情依据。禁止反推倍数或为门槛调整价格。
净盈亏比不足时拒绝入场并保留原价，不制造新的止盈距离。

`tests/test_holistic_scan.py` 通过 mock provider 回放多空、单档和分批方案：
1.4、1.95、2.25、3.6 的比值及对应价格通过 runner 与 policy 后保持原值；
低于门槛但错误声称可入场的方案被拒绝，原评估仍保留。既有开仓边界和净费用
复核测试保持通过；自然同为 2.25 的方案不会仅因倍数相同被拒绝。

本次策略目录 73 项、行情/执行/撮合/FastAPI 回归 146 项和 Swift 191 项测试通过；
`python3 scripts/validate_strategy_sync.py` 与 `git diff --check` 通过。执行命令沿用
上方验收入口。这是提示契约和执行边界的确定性验证，没有调用真实模型验证
生成目标的行情合理性，也不代表收益改善。

补充运行 `scripts.test_ai_gateway.AIGatewayTests` 的六项相关回归：
`test_prompt_exposes_quality_gates_and_independent_per_contract_setups`、
`test_hold_and_account_blocks_preserve_conditional_analysis_in_prompt`、
`test_open_ratio_cannot_exceed_its_proposed_price_setup`、
`test_eligible_assessment_must_meet_existing_quality_gates`、
`test_rejected_open_preserves_id_assessments_and_raw_audit`、
`test_policy_requires_open_win_rate_and_risk_reward_ratio`，全部通过。

已执行 `./scripts/build_and_run.sh package` 和 `./scripts/build_and_run.sh --verify`。
应用和后台 PID 已更换，认证 `/health` 返回 `ok / paper`；AI 配置仍为
`enabled=true / paper-active`。包内 `ai_worker.py` 与源码 SHA-256 一致，
包含禁止反推止盈的提示，应用 `codesign --verify --deep --strict` 通过。
旧评估保留历史原值；后续扫描才使用新提示。

## 历史 v1.8 小时级保护与五分钟扫描记录（已由 v1.9 supersede）

2026-10-11（北京时间）：规则改为每 300 秒开始一次扫描，原 30/600/900 秒默认配置
迁移至 300 秒；其他自定义值及停止/故障闩锁保留。扫描周期不扩大默认 90 秒
快照有效期，不改变撮合器或交易所连续执行已有保护的机制。

旧版本的模型输入完整保留采集的 15m、1H、4H 原始行（每周期最多 60），只有 5m 可
压缩窗口。提示要求新旧方案的止盈止损和分批目标结合 1H、4H 已确认结构与波动，
解释两个周期的价格依据；低周期只细化入场，不凭短周期噪声反复调整保护或退出。
缺少高周期依据时模型应标记新方案不可入场，保留已有有效保护，不能强造目标。
当时尚未新增固定 4H 方向门槛；v1.9 已明确 4H/20 根门禁，并将 15m 限定为入场
时机细化。当前默认 workflow 也已改为 grouped，再由 coordinator 汇总。

策略目录 75 项、行情/执行/撮合/FastAPI 回归 146 项、九项网关提示/价格/数据回归
和 Swift 191 项通过；同步校验及 `git diff --check` 通过。命令沿用上方入口，
网关追加 `test_prompt_candle_compaction_is_lossless_for_complete_16_contract_pool`、
`test_experimental_prompt_encoding_keeps_facts_local_and_limits_rows` 与
`test_codex_prompt_retains_full_candle_history`。虚拟时钟验证 300 秒起始间隔、
采集/推理耗时计入周期、跨过周期跳过补跑、配置修改与中断等待；模型 mock 验证
完整高周期历史、forming 标记、冒号键和缺失数据保留，以及提示中的小时级分析职责。

此验收证明输入、提示和调度契约，不证明真实模型每次都使用合理的高周期目标，
也不证明交易次数或亏损一定减少。没有放宽实际发单前的盈亏比、报价与风控门禁。

已通过 `./scripts/build_and_run.sh package` 和 `./scripts/build_and_run.sh --verify`
更新应用并重启。认证健康检查为 `ok / paper`，运行配置为
`enabled=true / paper-active / decisionIntervalSeconds=300`，状态为
`running / consecutiveFailures=0`。包内 `ai_schema.py`、`ai_worker.py`、
`ai_market_facts.py` 与源码摘要一致，应用签名校验通过。既有界面格式调整也保留。
