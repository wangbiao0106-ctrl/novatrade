# DeepSeek AI 研究

研究只使用脱敏快照、结构摘要和 ACP usage 审计。原始 K 线继续放在
`data/kline/`，本目录不存放复制数据。每次省 token 实验必须同时记录 input、output、
reasoning token、p95 延迟、ACP 错误率、assessment 覆盖率和管理动作漏检率；UTF-8
字节数只能作为独立的 payload 指标。

当前验收入口：

```bash
python3 -m unittest discover -s strategies/deepseek_ai_decision/tests -p 'test_*.py'
python3 scripts/test_ai_deepseek_profile.py
python3 scripts/test_ai_trigger.py
```

事件预筛、compact 编码和 profile overlay 都保持可独立回滚，未通过脱敏回放前不改为
生产默认值。
