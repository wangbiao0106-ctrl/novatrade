# Codex AI 测试夹具

策略契约测试位于本目录的 `test_contract.py`，事件和执行契约测试位于
`scripts/test_ai_trigger.py` 与 `scripts/test_ai_gateway.py`。这里不保存真实账户、
API 凭据或原始行情；新增夹具必须脱敏，并验证完整 assessment、实盘闸门、未知数据
fail-closed 和共享订单网关幂等性。

v1.1 的脱敏回放使用 `test_scan_schedule.py` 验证 10 分钟开始间隔、跳过超时轮次、
配置迁移和停止行为；`test_holistic_scan.py` 验证 15m 优先、4H 限制移除、
盈亏比 2.2 边界和缺少基础数据时持仓管理仍可用。v1.6 追加先定行情价格再筛选的
提示约束，覆盖多空、单档/分批低盈亏比方案保留原值、错误标记可入场被拒绝，
以及自然出现 2.25 的方案仍可保留。此回放使用 mock 模型，不能证明真实模型
每次均遵循价格依据要求。`test_market_context.py` 验证
OKX v5 衍生品源、情绪/美联储消息、指标、来源时效和事件指纹。

```bash
python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
```
