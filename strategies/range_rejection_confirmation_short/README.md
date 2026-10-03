# Range Rejection Confirmation Short

`range_rejection_confirmation_short`（冲高失败大阴回落确认做空）是从 SAND/USDT 截图抽象出的 15m 研究候选。它识别“突破近期高点后出现大实体阴线、收盘回到 EMA20 附近”的 setup，等待最多 4 根 K 线确认跌破 EMA20/信号低点，再在下一根开盘做空。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器配置：[config/strategy.json](config/strategy.json)
- 回测入口：[src/backtest.py](src/backtest.py)
- 研究记录：[research/README.md](research/README.md)
- 测试：[tests/test_backtest.py](tests/test_backtest.py)
- 状态：研究候选，未接入 Swift 运行时，默认不下单

运行历史回测：

```bash
python3 strategies/range_rejection_confirmation_short/src/backtest.py \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/range_rejection_confirmation_short/config/strategy.json \
  --output-dir strategies/range_rejection_confirmation_short/results \
  --scan
```

截图中的 K 线只构成 setup，若后续没有确认收盘则没有交易信号；不要在已经下跌约 5.9% 的 setup 收盘处直接追空。

