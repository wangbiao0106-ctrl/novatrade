# Intraday Pump Retest Short

新增独立研究策略，保留 `strategies/extreme_wick_short/` 的 >100% 动能衰竭策略不变。

规则：UTC 日内涨幅严格超过 60% 的标的，在已经形成 >60% 的日内高点后震荡下跌；随后 15m K 线重新上涨至少 15% 并触及此前日内高点，下一根 K 线缩量且收在自身高位附近；突涨阈值分别比较 >20% 与 >30%，确认后下一根开盘做空。默认 2 倍杠杆。

另有独立的严格 `>100%` 观察锚点变体，使用 [`research/anchor_100.json`](research/anchor_100.json) 和 [`results/anchor_100/`](results/anchor_100/)，不覆盖默认 `>60%` 配置和结果。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器参数：[config/strategy.json](config/strategy.json)
- 回测和敏感性扫描：[src/backtest.py](src/backtest.py)
- 研究记录：[research/README.md](research/README.md)
- 结果：[results/](results/)

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
