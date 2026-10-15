# Codex AI 测试夹具

策略契约测试位于本目录的 `test_contract.py`，事件和执行契约测试位于
`scripts/test_ai_trigger.py` 与 `scripts/test_ai_gateway.py`。这里不保存真实账户、
API 凭据或原始行情；新增夹具必须脱敏，并验证完整 assessment、实盘闸门、未知数据
fail-closed 和共享订单网关幂等性。

v1.9 的脱敏回放使用 `test_scan_schedule.py` 验证 5 分钟开始间隔、跳过超时轮次、
配置迁移和停止行为；`test_holistic_scan.py` 验证 4H 主入场周期、至少 20 根有效已确认
K 线、与 4H 趋势一致的多空门禁、15m 入场时机、1H/4H 保护数据和盈亏比 2.2 边界。
缺少高周期数据时，新开仓必须被阻断而持仓管理仍可用。v1.6 追加先定行情价格再
筛选的提示约束，覆盖多空、单档/分批低盈亏比方案保留原值、错误标记可入场被拒绝，
以及自然出现 2.25 的方案仍可保留。此回放使用 mock 模型，不能证明真实模型每次均
遵循价格依据要求。v1.9 还验证默认 grouped workflow 的逐组完整覆盖与 coordinator
汇总；`analysisWorkflow="single"` 仅作为显式运维覆盖。`test_market_context.py`
验证 OKX v5 衍生品源、情绪/美联储消息、指标、来源时效和事件指纹。

```bash
python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
```
