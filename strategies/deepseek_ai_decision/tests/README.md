# DeepSeek AI 测试夹具

DeepSeek ACP 边界测试位于 `scripts/test_ai_deepseek_profile.py`，事件与 policy 契约
测试位于 `scripts/test_ai_trigger.py` 和 `scripts/test_ai_gateway.py`。夹具只能保存
脱敏输入和白名单 usage 字段，必须覆盖工具/凭据隔离、session 持久化关闭、总 prompt
与输出边界、profile fallback、未知账户数据 fail-closed 以及挂单/持仓管理动作。
