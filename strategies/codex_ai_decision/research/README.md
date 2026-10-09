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

一次扫描的真实输入用量可用 [`measure_scan_input.py`](measure_scan_input.py) 核验。
它通过已运行纸面后台的 GET 接口读取账户/风控，复用运行时公共行情采集器和
单次整池 `compact20` 输入，执行一次 Codex 纯分析并读取 `turn.completed.usage`。
不实例化交易 worker 或订单网关、不写纸面账本、不保存原始 K 线及账户明细。
输入 token 包括 CLI 附加上下文/schema，缓存 token 是输入的子集。

```bash
python3 strategies/codex_ai_decision/research/measure_scan_input.py \
  --output strategies/codex_ai_decision/results/scan_input_usage_2026-10-10.json
```

2026-10-10 00:22:25（北京时间）采集的一轮纯分析实测：10 个标的，
`gpt-6-luna / medium`，单次整池调用，全部 10 个 assessment 返回成功。

| 项目 | 实测值 |
| --- | ---: |
| provider 报告的输入 token | 119,535 |
| 输入中的缓存 token | 0 |
| 输出 token（含其中 644 个推理 token） | 2,505 |
| 完整提示正文 UTF-8 字节 | 239,870（234.25 KiB） |
| 15m K 线行数 | 600 |
| 5m / 1H / 4H K 线行数 | 各 210 |
| 模型收到的 K 线总行数 | 1,230 |
| 快照中 K 线部分的字节 | 121,555 |

用量来自 CLI 的 `turn.completed.usage`，包括 CLI 附加上下文和输出 schema，
并非按字符数估算；提示正文的字节数只统计传入的交易提示。缓存是输入的子集，
不能再次加到总输入上。4H 为可选参考，15m 原始数据完整保留；辅助周期为最近
20 根已确认行加当前未收盘行。数据收集用时 7.777 秒。该次核验没有调用订单执行，
没有改动运行中 worker 的配置或状态。结构化统计位于
[`results/scan_input_usage_2026-10-10.json`](../results/scan_input_usage_2026-10-10.json)。
