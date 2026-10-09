# Codex AI 测试夹具

策略契约测试位于本目录的 `test_contract.py`，事件和执行契约测试位于
`scripts/test_ai_trigger.py` 与 `scripts/test_ai_gateway.py`。这里不保存真实账户、
API 凭据或原始行情；新增夹具必须脱敏，并验证完整 assessment、实盘闸门、未知数据
fail-closed 和共享订单网关幂等性。

v1.1 的脱敏回放使用 `test_scan_schedule.py` 验证 10 分钟开始间隔、跳过超时轮次、
配置迁移和停止行为；`test_four_hour_scan.py` 验证 4H 多空主周期、完整趋势历史、
数据不足时拒绝开仓以及持仓管理仍可用。

```bash
python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
```
