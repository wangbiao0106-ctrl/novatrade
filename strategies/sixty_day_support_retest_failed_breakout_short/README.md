# 60 日暴涨、支撑回踩后二次突破失败做空

独立研究候选，未接入 `Sources/`。脚本从确认的 5 分钟 K 线聚合 15 分钟 K 线，扫描最近 60 日涨幅超过 300%、当日涨幅超过 40%、支撑回踩后二次突破失败的形态。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器参数：[config/strategy.json](config/strategy.json)
- 回测入口：[research/backtest.py](research/backtest.py)
- 结果：[results/](results/)

运行：

```bash
python3 strategies/sixty_day_support_retest_failed_breakout_short/research/backtest.py --scan
```

资金费率不在本规则中，回测不计资金费损益。止盈形式化为原始仓位 50% 在盈利 5%、20% 在盈利 10%、剩余 30% 在盈利 15%；没有达到目标时最长持有 96 根 15m K 线（24 小时）。
