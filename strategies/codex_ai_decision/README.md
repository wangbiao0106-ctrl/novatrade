# Codex AI 决策策略

这是 Codex provider 的 AI 决策策略实验室包。Codex 专属规则入口是
[`STRATEGY.md`](STRATEGY.md)，Codex 与 DeepSeek 共用的决策规则见
[`spec/AI_DECISION_POLICY.md`](../../spec/AI_DECISION_POLICY.md)，机器配置是
[`config/strategy.json`](config/strategy.json)。

当前状态：`candidate`。该目录记录策略边界、默认参数和省 token 的事件预筛方案；
运行时由 backend 的 Codex worker 和配置中心 API 管理，不从本目录读取文件。

- 配置中心 ID：`codex`
- 实验室包 ID：`codex_ai_decision`
- provider：`codex`，固定 `gpt-6-luna / medium`
- 默认轮询：30 秒；事件预筛默认关闭，开启前需完成回放验收
- 开仓、平仓、撤单、持仓保护和分批止盈都必须经过服务端 policy 和共享订单网关

验证 AI worker 契约：

```bash
python3 scripts/test_ai_gateway.py
python3 scripts/test_ai_trigger.py
python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
```
