# Extreme Wick Altcoin Short

`extreme_wick_short` 是山寨冲高观察池的两种 15m 挂单做空研究包。观察池要求日内涨幅严格超过 60%，并且当前价格相对前 30 日低点至少 3 倍；涨幅没有上限，+100% 及以上保留。

- 规则真源：[`STRATEGY.md`](STRATEGY.md)
- 机器参数：[`config/strategy.json`](config/strategy.json)
- 两种挂单分析入口：[`src/limit_order_analysis.py`](src/limit_order_analysis.py)
- 研究说明：[`research/README.md`](research/README.md)
- 结果目录：`results/`

`src/extreme_wick_short.py` 是早期规则的历史对照扫描器；本版本只以 `src/limit_order_analysis.py`、`STRATEGY.md` 和 `config/strategy.json` 为准。

两种形态都只看 15m K 线：

1. 小幅震荡上行后单根涨幅至少 20%，下一根小跌，在小跌 K 线开盘价挂卖出限价。
2. 日内已确认 15m 高点之后连续 6 根 15m 小幅震荡下行，在第 6 根收盘后以该日内高点 K 线收盘价挂卖出限价。

“20 个点”在配置中固定为 `pump_gain_min=0.20`，计算为突涨 K 线最高价相对前一根 15m 收盘价的涨幅；整根 K 线收盘后才确认。挂单必须等形态 K 线收盘后才生效，按下一根开始的 4 根 15m K 线判断成交，未成交取消。

运行分析：

```bash
python3 strategies/extreme_wick_short/src/limit_order_analysis.py \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/extreme_wick_short/config/strategy.json \
  --output-dir strategies/extreme_wick_short/results
```

分析会写入 `limit_report.json`、`limit_trades.csv` 和 `observations.csv`。当前结果仅用于研究，是否有效以成交样本量、成交率、净 R 和分月稳定性共同判断，不会自动接入运行时。
