# DeepSeek AI 决策策略

这是 DeepSeek Harness provider 的 AI 决策策略实验室包。DeepSeek 专属规则入口是
[`STRATEGY.md`](STRATEGY.md)，DeepSeek 与 Codex 共用的决策规则见
[`spec/AI_DECISION_POLICY.md`](../../spec/AI_DECISION_POLICY.md)，机器配置是
[`config/strategy.json`](config/strategy.json)。

当前状态：`candidate`。运行时由 backend 的 DeepSeek Harness worker 和配置中心
API 管理，不从本目录读取文件。

- 配置中心 ID：`deepseek`
- 实验室包 ID：`deepseek_ai_decision`
- provider：`deepseek-harness`，默认 `deepseek-v4-pro / low`
- 默认 profile：`optimized`；默认编码：`compact60`
- 开关：`NOVATRADE_AI_EVENT_MODE`、`NOVATRADE_DEEPSEEK_ENCODING`
- 开仓、平仓、撤单、持仓保护和分批止盈都必须经过服务端 policy 和共享订单网关

验证 DeepSeek profile、worker 和事件预筛契约：

```bash
python3 scripts/test_ai_deepseek_profile.py
python3 scripts/test_ai_trigger.py
python3 -m unittest discover -s strategies/deepseek_ai_decision/tests -p 'test_*.py'
```
