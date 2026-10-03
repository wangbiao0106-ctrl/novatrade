# 交易风格回放

这是一个只读研究策略，不接入 `Sources/`，也不会下单。它先把 OKX 统一交易账单聚合成关联订单组和近似风格 episode，再用仓库中的已确认 5 分钟 K 线复盘事件后的可执行价格，比较固定风险规则和入场前过滤条件。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器配置：[config/strategy.json](config/strategy.json)
- 回测入口：[src/backtest.py](src/backtest.py)
- 价格层回放：[src/price_backtest.py](src/price_backtest.py)
- 研究口径：[research/README.md](research/README.md)
- 测试：[tests/test_backtest.py](tests/test_backtest.py)
- 状态：研究候选，未接入运行时

运行账单实验：

```bash
python3 strategies/personal_trading_style_backtest/src/backtest.py \
  --ledger /Users/bill/Downloads/NTM5NTczNjI=_欧易统一交易账单_2026-07-22~2026-08-22~UTC+8~6530099a043ba19e63533fd8723d7833.zip \
  --config strategies/personal_trading_style_backtest/config/strategy.json \
  --output-dir strategies/personal_trading_style_backtest/results
```

运行价格层行为回放：

```bash
python3 strategies/personal_trading_style_backtest/src/price_backtest.py \
  --ledger /Users/bill/Downloads/NTM5NTczNjI=_欧易统一交易账单_2026-07-22~2026-08-22~UTC+8~6530099a043ba19e63533fd8723d7833.zip \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/personal_trading_style_backtest/config/strategy.json \
  --output-dir strategies/personal_trading_style_backtest/results
```

运行策略测试：

```bash
python3 -m unittest discover \
  -s strategies/personal_trading_style_backtest/tests \
  -p 'test_*.py'
```

输入账单只从命令行路径读取，不复制到 `data/kline/`；K 线只读取仓库原始行情，不把派生数据写回行情目录。所有结果都写入本策略的 `results/`，其中记录输入 ZIP SHA-256、配置摘要和 K 线文件名，方便复现和核对数据版本。账单元数据中的用户 ID 会脱敏。
