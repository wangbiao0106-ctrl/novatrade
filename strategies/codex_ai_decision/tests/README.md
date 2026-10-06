# Codex AI 测试夹具

策略契约测试位于本目录的 `test_contract.py`，事件和执行契约测试位于
`scripts/test_ai_trigger.py` 与 `scripts/test_ai_gateway.py`。这里不保存真实账户、
API 凭据或原始行情；新增夹具必须脱敏，并验证完整 assessment、实盘闸门、未知数据
fail-closed 和共享订单网关幂等性。
