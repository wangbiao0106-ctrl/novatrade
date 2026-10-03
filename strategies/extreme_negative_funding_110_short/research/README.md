# 研究说明

`src/backtest.py --scan` 逐文件读取已确认 OKX 5m K 线，并按 `config/strategy.json` 复用统一山寨币标的池，排除非加密和主流币；要求 UTC 日界开盘和连续时间。候选触发只在首次运行涨幅达到 +110%（高点 `2.10 × UTC 日开盘`）时产生；限价单从下一根 K 线激活，价格同为 `2.10 × UTC 日开盘`，订单在 UTC 日末撤销。资金费率不是行情推导字段，必须通过 `--funding` 传入 JSON 快照；未提供时报告状态为 `unavailable`，不会把缺失值当成通过。

快照示例：

```json
{
  "snapshots": [
    {"symbol": "ALT", "timestamp_ms": 1760000700000,
     "funding_rate": -0.02, "symbol_max_negative_rate": -0.02}
  ]
}
```

运行：

```bash
python3 strategies/extreme_negative_funding_110_short/src/backtest.py --scan
python3 strategies/extreme_negative_funding_110_short/src/backtest.py --scan --funding funding_snapshots.json
python3 strategies/extreme_negative_funding_110_short/src/backtest.py --scan --ignore-funding
```

输出 `results/report.json`、`results/signals.csv` 和 `results/trades.csv`。报告会区分价格候选、资金费率拒绝和最终成交，包含数据缺口、不完整 UTC 日及资金费率不可用原因。`--ignore-funding` 会输出价格条件敏感性结果，资金费率字段为空，不能解释为原策略的完整验证。该实验没有接入现有 `five_minute_surge_waterfall_short`，也没有样本外收益结论。

已采纳的小网格验证使用独立入口：

```bash
python3 strategies/extreme_negative_funding_110_short/research/grid_backtest.py --scan
```

它只遍历触发涨幅 `110%/120%/130%`、信号后等待 `1/2/3` 根 5m K 线和初始止损 `10%/15%/20%`，其余规则固定。输出位于 `results/grid/`；报告按 `(symbol, UTC day)` 的首次触发定义事件，并要求验证区至少 30 个事件，未达门槛的组合不能被选为最终规则。

若只验证一个固定变体，可运行：

```bash
python3 strategies/extreme_negative_funding_110_short/research/run_100_gain_130_order.py --scan
```

该变体使用严格日内涨幅 `>100%`、`2.30 × UTC 日开盘` 卖出限价、下一根连续 5m K 线激活、1x、入场上方 20% 止损，并沿用 20% 平半及累计 40% 平余仓的分段退出。结果位于 `results/gain_100_order_130/`。

同一变体的 30% 初始止损可通过 `--stop-pct 0.30` 写入独立的 `results/gain_100_order_130_stop30/` 目录。

日线配对规则使用完整 UTC 日：前一日收盘相对前一日开盘严格涨幅 `>100%`，后一日收盘相对后一日开盘在 `0%`–`10%`（含边界），并在后一日收盘价 1x 做空。它没有指定止损，分批止盈为盈利 20% 平半、累计盈利 40% 平余仓：

```bash
python3 strategies/extreme_negative_funding_110_short/research/run_prev_day_close_reversal.py --scan
```
