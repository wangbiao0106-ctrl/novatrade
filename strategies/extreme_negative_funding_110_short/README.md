# 极限负资金费率 110% 逼空做空

独立研究候选：确认的 5m K 线在 UTC 日内涨幅先超过 80%，运行涨幅达到 +110%（高点为 `2.10 × 日开盘`）时，在下一根 K 线起挂 `2.10 × 日开盘` 卖出限价单；资金费率必须由外部快照明确证明达到该币种最大负值。1x 杠杆，入场上方 20% 止损，跌至入场价的 80% 平半并把剩余止损移到保本，再跌至入场价的 60% 平余仓。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器参数：[config/strategy.json](config/strategy.json)
- 回测与扫描：[src/backtest.py](src/backtest.py)
- 研究说明：[research/README.md](research/README.md)
- 小网格验证：[research/grid/README.md](research/grid/README.md)
- 单元测试：[tests/test_backtest.py](tests/test_backtest.py)
- 报告输出：[results/](results/)

默认行情目录为 `data/kline/okx/swap/5m/`。不提供 `--funding` 时，扫描会继续检查行情和价格触发，但所有候选都因资金费率不可用而拒绝，并在 `results/report.json` 明确记录这一点。需要只验证价格条件时使用 `--ignore-funding`；该模式会在报告中标记资金费率门控为 `ignored`，不代表原策略的完整回测结果。

小网格验证固定资金费率门控为忽略，遍历触发涨幅 `110%/120%/130%`、等待 `1/2/3` 根 5m K 线和初始止损 `10%/15%/20%`：

```bash
python3 strategies/extreme_negative_funding_110_short/research/grid_backtest.py --scan
```

结果写入 `results/grid/grid.csv`、`results/grid/trades.csv` 和 `results/grid/report.json`；验证区独立事件少于 30 的组合不得作为最终规则。

单独验证“日内涨幅严格高于 100%、2.30 × 日开盘挂空、1x、20% 止损、累计 40% 止盈”的命令：

```bash
python3 strategies/extreme_negative_funding_110_short/research/run_100_gain_130_order.py --scan
```

结果写入 `results/gain_100_order_130/report.json`、`signals.csv` 和 `trades.csv`。

将初始止损改为 30% 的变体：

```bash
python3 strategies/extreme_negative_funding_110_short/research/run_100_gain_130_order.py \
  --scan --stop-pct 0.30 \
  --output-dir strategies/extreme_negative_funding_110_short/results/gain_100_order_130_stop30
```

前一日收盘涨幅严格高于 100%、后一日收盘涨幅在 0%–10%，后一日收盘做空的日线配对规则：

```bash
python3 strategies/extreme_negative_funding_110_short/research/run_prev_day_close_reversal.py --scan
```

该规则未指定止损，回测持仓直到分批止盈、数据缺口或数据结束；结果写入 `results/prev_day_close_reversal/`。
