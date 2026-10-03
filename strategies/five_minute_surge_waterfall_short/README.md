# 5分钟大涨回调/瀑布做空

这是一个独立的研究候选策略，用已确认的 OKX USDT 线性永续 5m K 线统计两类大涨阈值（严格超过 20% 和 30%），并分别拆分为突破/未突破前 4 小时高点。每个信号再比较下一根 5m 开盘做空和最近 72 根 K 线实体顶部价卖出限价单两种执行方式；实体顶部按阳线收盘、阴线开盘计算。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器参数：[config/strategy.json](config/strategy.json)
- 回测入口：[src/backtest.py](src/backtest.py)
- 单元测试：[tests/test_backtest.py](tests/test_backtest.py)
- 研究结果：[results/](results/)

运行全量实验：

```bash
python3 strategies/five_minute_surge_waterfall_short/src/backtest.py
```

默认输入为 `data/kline/okx/swap/5m/`，所有报告和成交记录都写入本目录 `results/`。当前只做研究，不接入 `Sources/` 运行时，也不会自动选取全历史收益最高的组合。

本次全量复核覆盖 304 个排除非加密资产后的合约、35,697,069 根 5m K 线（2025-04-03 至 2026-10-03）。信号事件为：20% 突破前 4 小时高点 218 根、未突破 251 根；30% 突破 77 根、未突破 114 根。突破与未突破是成对对照组。固定 ATR 风控下，8 个组合的全局单仓净 R 均为负：20% 突破约 -19.07R（下一根开盘）/-21.08R（实体顶部限价），20% 未突破约 -14.42R/-4.39R，30% 突破约 -4.87R/-5.84R，30% 未突破约 -4.15R/-0.16R；30% 未突破限价只有 7 笔组合成交，样本不足。结果不支持接入运行时。
