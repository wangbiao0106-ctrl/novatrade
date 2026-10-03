# Extreme Wick Momentum Exhaustion Short

`extreme_wick_short` 是山寨币日内翻倍后的 15m 动能衰竭做空研究包。当前版本要求滚动 24 小时涨幅严格大于 100%，用收阴、上影线、收盘位置、RSI 回落、成交量和前高突破共同确认衰竭，下一根 15m 开盘做空，默认 2 倍杠杆。

- 规则真源：[`STRATEGY.md`](STRATEGY.md)
- 机器参数：[`config/strategy.json`](config/strategy.json)
- 当前回测入口：[`src/momentum_exhaustion.py`](src/momentum_exhaustion.py)
- >100% 样本扩充入口：[`src/sample_expansion.py`](src/sample_expansion.py)，配置和结果见 [`research/README.md`](research/README.md)
- 历史双挂单对照：[`src/limit_order_analysis.py`](src/limit_order_analysis.py)
- 研究说明和结果：[`research/README.md`](research/README.md)、[`results/`](results/)
- 扩充实验结论：[`results/sample_expansion/README.md`](results/sample_expansion/README.md)，46 笔研究成交、44 笔全局单仓成交；仍待新数据前推验证
- 总净收益目标实验：[`results/return_maximization/README.md`](results/return_maximization/README.md)，网格内候选为 48 笔、68.8% 胜率、PF 2.12、17.17R
- 取消上影线对照：[`results/no_wick_return_maximization/README.md`](results/no_wick_return_maximization/README.md)，无组合通过验证段质量门槛
- 延长下跌捕获实验：[`results/trend_capture/README.md`](results/trend_capture/README.md)，补充趋势入场与移动止损，未接入运行时
- 入场挂单方式实验：[`results/entry_orders/README.md`](results/entry_orders/README.md)，实时信号收盘成交优于前几根 K 线高点限价

最近一年半默认规则复核（`2025-04-03T00:00:00Z` 至 `2026-10-03T07:45:00Z` 的完整 15m K 线）已更新到 `results/momentum_exhaustion_report.json`：279 个合约、2,095 个观察事件、24 笔交易，胜率 41.67%，总净收益 -1.1344R，Profit Factor 0.9171。样本量和收益质量都不足以支持上线。

运行分析：

```bash
python3 strategies/extreme_wick_short/src/momentum_exhaustion.py \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/extreme_wick_short/config/strategy.json \
  --output-dir strategies/extreme_wick_short/results
```

当前结果仅用于研究。是否继续优化以成交样本量、样本外稳定性、净 R、盈亏因子和回撤共同判断，不会自动接入运行时。
