# 山寨币二次扫顶研究证据

本目录用于验证 [`../STRATEGY.md`](../STRATEGY.md) 与 [`../config/strategy.json`](../config/strategy.json) 的正式 v1.4 规则。研究报告、图表和参数扫描均不能替代规则真源；Swift 运行时不读取本目录。

## 当前说明书证据

| 入口 | 输入与职责 | 输出 |
| --- | --- | --- |
| [`report_full_history.py`](report_full_history.py) | 从全量 5m 原始数据独立聚合 1h/15m，因果重现动态选币、15m 确认、15% 距离上限及正式单仓 | `../results/full_history/report_fee5bps.json`、三份逐笔 CSV，派生缓存 `../results/full_history/data/` |
| [`profit_projection.py`](profit_projection.py) | 读取上述报告、全部信号/单仓 CSV 与已聚合数据；生成历史分布、条件 bootstrap、成本敏感性、真实入场和退出点位 | `../results/full_history/profit_projection.json`、`../results/full_history/charts/` 六张 SVG |
| [`report_live_rule.py`](report_live_rule.py) | 较短历史快照窗口的上线规则逐笔复核，`--pool live` 为生产范围，默认使用 177 币快照 | `../results/live_rule_report*.json`、逐笔 CSV |
| [`live_signal.py`](live_signal.py) | 研究实时扫描器，确认时间基和 Swift 信号的对齐证据 | 按命令写入 `../results/` |

从仓库根目录运行：

```bash
python3 strategies/sweep_reversal_short/research/report_full_history.py --help
python3 strategies/sweep_reversal_short/research/report_full_history.py
python3 strategies/sweep_reversal_short/research/profit_projection.py --help
python3 strategies/sweep_reversal_short/research/profit_projection.py
python3 -m unittest discover -s strategies/sweep_reversal_short/tests -p 'test_*.py'
python3 scripts/validate_strategy_sync.py
swift test
git diff --check
```

Python 研究入口需要 NumPy 和 pandas；本项目 `.venv` 已提供相关依赖。首次全历史预处理需要数分钟；已有缓存时可直接运行预测脚本。原始数据放在 `data/kline/okx/swap/5m/`；所有派生数据与报告都留在本策略 `results/`。轻量全历史证据与图表应随文档保留，`results/full_history/data/` 可重建、不提交。

## 口径和阅读顺序

1. 优先阅读 [`../STRATEGY.md`](../STRATEGY.md) §8–11，那里给出完整解释和图示。机器预测详情在 [`../results/full_history/profit_projection.json`](../results/full_history/profit_projection.json)。
2. 全历史当前可用合规山寨币 279 个，生产范围成交 108 笔，正式单仓序列 54 笔。全部信号的 R 分布允许时间重叠；资金池复投与年度预测只使用单仓序列。
3. 分段名称 `A_fresh_before_window` 表示早于 2026-03-31 的此前未参与调参区间；其时间早于已查看的调参窗口，不是定稿后前瞻样本。`B_v1_4_window` 是参数已被查看的历史窗口；`C_after_window` 仅有很少新增成交。
4. 收益基线仅含单边 5bp 手续费。bootstrap 的分位数以样本分布保持不变为条件，未模拟相关连亏、持仓中浮亏、全账户每日 MTM 熔断和人工复位；成本敏感性是在同一成交序列上追加成本。K 线图用真实价格，净 R 单独包含费用。
5. CSV 时间戳是 bar 开盘时间。下单发生在确认 bar 收盘后；报告中的已实现净值只在平仓时更新。所有研究时间均为 UTC。

## 历史研究材料

统计和成交口径的回归测试见 [`../tests/test_profit_projection.py`](../tests/test_profit_projection.py)，因果性测试见 [`../tests/test_causal_research.py`](../tests/test_causal_research.py)。这些测试防止把费用画成成交价、把不足一年的尾段当成年收益或忽略初始亏损回撤。

[`STRATEGY_SPEC.md`](STRATEGY_SPEC.md) 是由规则真源派生的研究说明，含旧实验对照；其中 1h 收盘入场的 60 笔基线不代表正式 15m 确认执行规则。旧窗口、旧选币范围与新全历史不能混在同一个绩效结论里。

- 执行时机与挂单实验：[`EXECUTION_TUNING.md`](EXECUTION_TUNING.md)、`tune_execution.py`、`run_grid.py`。
- 中低流动性专项：[`LOWMID_TUNING.md`](LOWMID_TUNING.md)、[`LOWMID_FOLLOWUP.md`](LOWMID_FOLLOWUP.md)、`lowmid_followup.py`。
- 旧基线和网格：`final_report.py`、`run_grid.py`、`fusion.py`、`dump_events.py` 及 `tuning_*.json`。它们保留作对照，不能绕过 `STRATEGY.md` 改动正式规则。
