# 缩量二次拉升

新增独立研究策略，使用本目录维护的 >100% 观察锚点。

规则：UTC 日内涨幅严格超过 60% 的标的，在已经形成 >60% 的日内高点后震荡下跌；随后 15m K 线重新上涨至少 15% 并触及此前日内高点，下一根 K 线缩量且收在自身高位附近；突涨阈值分别比较 >20% 与 >30%，确认后下一根开盘做空。默认 2 倍杠杆。

另有独立的严格 `>100%` 观察锚点变体，使用 [`research/anchor_100.json`](research/anchor_100.json) 和 [`results/anchor_100/`](results/anchor_100/)，不覆盖默认 `>60%` 配置和结果。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器参数：[config/strategy.json](config/strategy.json)
- 回测和敏感性扫描：[src/backtest.py](src/backtest.py)
- 研究记录：[research/README.md](research/README.md)
- 结果：[results/](results/)

## 最近一年半全量复核

默认规则已使用 `2025-04-03T00:00:00Z` 至 `2026-10-03T08:00:00Z` 的最新行情重跑，报告写入 `results/report.json`。本次纳入 295 个合约、11,896,703 根完整 15m K 线；36 个候选突涨中只有 1 个通过形态条件，但被 ATR 止损距离风控拒绝，最终 0 笔交易、区间收益 0%、最大回撤 0%。样本量未达到 30 笔验收线，策略仍不能推进运行时。

运行：

```bash
python3 strategies/intraday_pump_retest_short/src/backtest.py --scan
```

运行 `>100%` 锚点变体：

```bash
python3 strategies/intraday_pump_retest_short/src/backtest.py \
  --config strategies/intraday_pump_retest_short/research/anchor_100.json \
  --output-dir strategies/intraday_pump_retest_short/results/anchor_100 \
  --scan
```

入口会先把全部合规标的 5m 行情聚合到内存，默认回测与 72 组敏感性扫描复用同一份数据。策略仍是研究候选，尚未接入运行时。
