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

全量网关及 4H 回归中的旧分组/协调用例仍要求多次分组调用，与当前单次整池流程
不一致；尝试全量执行时出现对应断言失败，旧超时/取消用例阻塞后被中断。本次未
修改这部分用例，也未将全量套件计为通过。

## v1.5 综合分析与 2.2 门槛验收

2026-10-10（北京时间）：已移除 4H 趋势和 20 根历史硬门槛，优先发送 15m
原始 OHLCV；5m/1H/4H 是可选背景。`tests/test_holistic_scan.py` 取代旧
4 小时回放文件，覆盖缺失/相反 4H、不足指标窗口、两个方向与 2.2 的开仓边界。
`tests/test_market_context.py` 覆盖 OKX v5 持仓量、币种级 5m 多空账户比和主动
买卖量，情绪/官方美联储消息的来源时间、缓存、失败和正文摘录，以及 RSI、
滚动典型价格加权均价、波动率、数据传递、快照哈希及事件指纹。

模型质量及真实限价复核的净盈亏比下限均为 2.2；分批目标按最近档检查。
多空净盈亏比边界回放证明，账面 2.2 但扣除费用/滑点后不足的方案不能下单。
可选数据的限时与 Rubik 分接口限频不会移除观察合约；失败不补零。

本版本验收：策略目录 72 项、核心行情/执行/撮合/FastAPI 回归 146 项、网关
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

### 打包与重启核验

随后按用户要求执行 `./scripts/build_and_run.sh package` 和
`./scripts/build_and_run.sh --verify`，已生成 `dist/NovaTrade.dmg` 并重启
该工作区的 NovaTrade 与后台。旧应用/后台 PID 已替换，新后台 `/health` 返回
`ok`，账户环境为 `paper`；策略 API 返回 `market-facts-v2`。
打包后的八个相关 backend 源文件与当前源码 SHA-256 一致，包内优先周期为
15m、最少有效收盘数据为 1 根，模型及净盈亏比下限均为 2.2；应用 bundle 的
`codesign --verify --deep --strict` 通过。
