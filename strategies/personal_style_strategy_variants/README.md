# 个人做单偏好多分支回顾实验

研究用途的策略包，用全量已下载 5 分钟行情回顾验证从个人账单归纳出的多空偏好。它不会下单，也没有接入 `Sources/`。

- 规则真源：[STRATEGY.md](STRATEGY.md)
- 机器参数：[config/strategy.json](config/strategy.json)
- 批量回测入口：[src/backtest.py](src/backtest.py)
- 研究说明：[research/README.md](research/README.md)
- 结果目录：[results/](results/)

运行基准和压力成本回测：

```bash
python3 strategies/personal_style_strategy_variants/src/backtest.py \
  --data-dir data/kline/okx/swap/5m \
  --manifest data/kline/okx/swap/5m/manifest.json \
  --universe-config strategies/sweep_reversal_short/config/universe.json \
  --config strategies/personal_style_strategy_variants/config/strategy.json \
  --output-dir strategies/personal_style_strategy_variants/results
```

脚本每个合约只读取 manifest 选出的最新文件，逐个释放行情数组；因此是一次批量读取全量可用合约，但不会把 2GB 原始压缩文件重复加载。报告包含每个分支的全样本、训练、验证、测试、月度、品种集中度、MAE/MFE 和成本压力结果。
