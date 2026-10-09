# 下单前复核回放

规则与参数由 [`../STRATEGY.md`](../STRATEGY.md) 和
[`../config/strategy.json`](../config/strategy.json) 的 `entry_preflight` 定义。
使用固定时间、脱敏快照和伪造盘口的确定性回放，验证多空方向、行情偏移、价差、
标记价格触及止损、盘口深度、费用后盈亏比及纸面/交易所的提交边界。

验收入口：

```bash
NOVATRADE_TRADING_MODE=exchange python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_entry_preflight.py'
```

拒绝场景应验证没有订单 POST、没有新增纸面订单、reservation 和日开仓额度未被
占用，worker 保留逐币评估并记录安全 hold。通过场景允许原计划提交，限价不会因
偏移复核被修改为追价。此回放只证明执行门禁，不提供交易收益或避免止损的证据。

2026-10-09 验收结果：新增 19 个下单前复核测试通过；Codex 策略、AI 网关、执行、
行情采集、事件预筛、行情事实、FastAPI 网关及纸面交易的相关 Python 回归共 308 个
通过，`swift test --disable-automatic-resolution` 的 191 个 Swift 测试通过。
`python3 scripts/validate_strategy_sync.py` 与 `git diff --check` 均通过。

完整 Python 回归可复现为：

```bash
NOVATRADE_TRADING_MODE=exchange python3 -m unittest \
  scripts.test_ai_gateway scripts.test_ai_execution scripts.test_ai_snapshot \
  scripts.test_ai_trigger scripts.test_ai_market_facts scripts.test_fastapi_gateway \
  scripts.test_paper_mode scripts.test_paper_trading \
  strategies.codex_ai_decision.tests.test_contract \
  strategies.codex_ai_decision.tests.test_four_hour_scan \
  strategies.codex_ai_decision.tests.test_scan_schedule \
  strategies.codex_ai_decision.tests.test_entry_preflight
```
