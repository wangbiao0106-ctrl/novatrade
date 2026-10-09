# Codex AI 研究

研究只使用脱敏的 `AISnapshot`、结构摘要和决策审计，原始 K 线仍归档在
`data/kline/`，不复制到本目录。回放需要比较固定轮询与事件预筛的调用数、真实
provider usage、p95 延迟、assessment 覆盖率和 policy/gateway 拒绝率。

当前验收入口：

```bash
python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
python3 scripts/test_ai_trigger.py
python3 scripts/test_ai_gateway.py
```

事件预筛仍保持关闭，直到脱敏回放证明没有遗漏开仓、平仓或撤单触发。

下单前行情复核的确定性回放及验收入口见 [`ENTRY_PREFLIGHT.md`](ENTRY_PREFLIGHT.md)。
