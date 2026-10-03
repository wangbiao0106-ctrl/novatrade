# Liquid Crypto Trend Long

高流动性加密永续趋势跟随（纯多头）。策略在下单时点的滚动 24 小时 USDT 报价成交额 ≥ 3000 万的加密永续里，只做多站上 1 小时 72 均线加 1% 滞回带的一侧，用波动率目标控制敞口，不做空、不设止损止盈，只靠带宽翻转和 96 小时持仓上限离场。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器配置：[config/strategy.json](config/strategy.json)
- 标的类别快照：[config/universe.json](config/universe.json)
- 研究证据：[research/README.md](research/README.md)
- 回测引擎：[src/backtest.py](src/backtest.py)
- 参数扫描：[research/scan_parameters.py](research/scan_parameters.py)
- 状态：**研究候选，未接入运行时**

## 一年半复核（2025-04-03 ~ 2026-10-03，7 bp/边）

| 序列 | 区间收益 | 最大回撤 | 夏普 |
| --- | --- | --- | --- |
| 策略 | **-45.0%** | -80.9% | **-0.28** |
| 同池等权篮子 | -72.2% | -88.5% | -0.75 |
| 全池等权篮子 | -36.2% | -73.0% | -0.07 |
| BTC 买入持有 | +1.6% | -53.8% | 0.23 |

策略只优于同池篮子的收益与夏普，未超过全池篮子；按验收条件不能定稿或上线。结果覆盖了 2025 年 4 月以来的多段回撤，说明原先 6 个月上涨窗口的正面结论不能外推。

## 与历史研究的关系

早期研究先在一份 30 个月（2024-04 ~ 2026-10，来自外部抓取的 K 线）样本上得出"双向趋势跟随"结论，再落到仓库内的真实 5 分钟数据上复核。复核过程推翻了两个中间结论，本目录只保留经仓库内数据验证后的版本：

1. **纯多头胜于双向**。同一窗口下双向版本仅 +23.6%，纯多头 +84.9%（5 bp/边、同一时期、同一池）。空头腿在 2026-04~09 的上涨行情里持续失血。
2. **时长上限必须放宽**。早期在双向版本上得到的最优值 60h 在纯多头下只有夏普 1.06；96h 以上才是平台。

## 复现

```bash
python3 strategies/liquid_crypto_trend_long/src/backtest.py
python3 strategies/liquid_crypto_trend_long/research/scan_parameters.py
python3 -m unittest discover -s strategies/liquid_crypto_trend_long/tests
```

## 边界

一年半复核仍未覆盖完整多年熊市；资金费未建模；当前回测未通过收益与夏普双重验收；未接入运行时。
