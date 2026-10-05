# 现货自适应有限马丁

研究候选（`spot_adaptive_martingale`，v0.1.0），未接入运行时。策略在小波动中用有限网格分批买卖，在下跌或大波动中按 ATR 拉宽间距并使用最多四层递增买单；现金底线、库存上限和应急退出共同限制马丁暴露。

规则真源是 [`STRATEGY.md`](STRATEGY.md)，机器参数是 [`config/strategy.json`](config/strategy.json)。回测入口为 [`src/backtest.py`](src/backtest.py)，默认对 BTC、ETH、OKB 运行，结果写入本目录 `results/`。

```bash
python3 strategies/spot_adaptive_martingale/src/backtest.py
python3 -m unittest discover -s strategies/spot_adaptive_martingale/tests
```

当前数据目录只有 OKX USDT 永续 OHLCV，没有现货逐笔成交或现货 K 线，因此本研究用同标的永续价格做代理。结果已计入单边手续费和滑点，但不能证明现货可成交性，也没有建模基差、资金费、盘口冲击、跳空成交和交易所限额。

最近数据快照（完整可用区间，BTC/ETH 为 2025-04-08 至 2026-10-03 UTC，OKB 从 2025-09-09 起）结果如下：

| 标的 | 策略收益 | 买入并持有 | 策略最大回撤 | 买入并持有最大回撤 | 交易数 | 应急退出 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BTC | -7.24% | +7.14% | 14.67% | 53.76% | 55 | 1 |
| ETH | +15.16% | +72.66% | 12.27% | 69.17% | 96 | 1 |
| OKB | -10.61% | -35.39% | 15.26% | 71.26% | 23 | 1 |

这组结果显示它更像“降低回撤、用震荡实现部分现金收益”的防守策略，牛市中会明显落后买入并持有；三条标的都触发过一次应急退出，不能把它当成无风险的自动补仓方案。
