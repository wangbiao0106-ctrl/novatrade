# 研究记录

当前实验使用 [`../src/momentum_exhaustion.py`](../src/momentum_exhaustion.py)：从 `data/kline/okx/swap/5m/` 读取已确认 5m 数据，聚合完整 15m K 线，并按 `STRATEGY.md` 的 `gain24 > 100%` 和动能衰竭组合生成下一根开盘空单。

验证入口会先把整个研究标的池聚合后一次性载入内存，后续指标、信号和参数验证复用同一批 K 线，避免重复读取 gzip 行情文件。

本轮参数筛选组合了 RSI、上影线比例、收盘位置、成交量均值倍数、前高突破幅度、ATR 止损和目标 R。选择参数固定写入 `config/strategy.json`，扫描证据写入 `results/`；研究结果不能反向成为规则真源。

结果文件：

- `../results/momentum_exhaustion_report.json`：总体、按币种和按 UTC 月份统计，含 2 倍杠杆收益和回撤。
- `../results/momentum_exhaustion_trades.csv`：逐笔入场、止损、止盈、出场和净 R。
- `../results/momentum_exhaustion_observations.csv`：所有 >100% 观察事件及其指标值。

样本只有 8 笔成交，尚未达到配置中的 30 笔样本外验收线；因此仍是研究候选。

## >100% 样本扩充

扩充实验入口会先把所有合资格的 5m 行情一次性聚合到内存，后续 972 组参数扫描复用同一份 K 线、指标和成交结果，不重复读取 gzip 文件：

```bash
python3 strategies/extreme_wick_short/src/sample_expansion.py \
  --data-dir data/kline/okx/swap/5m \
  --config strategies/extreme_wick_short/config/strategy.json \
  --experiment strategies/extreme_wick_short/research/sample_expansion.json \
  --output-dir strategies/extreme_wick_short/results/sample_expansion
```

`results/sample_expansion/report.json` 保留 v0.3 的 8 笔基准复现、单项放宽对照、972 组网格和按时间切分的回顾性验证；`grid.csv` 是完整参数表，`observations.csv` 是严格 >100% 的观察事件。`grid_0662_config.json` 是同时通过训练段和回顾验证段质量门槛的 44 笔研究候选，配套逐笔成交和按币种成交文件也以同一编号保存。该候选在查看完整网格后筛选，使用了验证段表现，属于回顾性筛选，不能视为独立样本外检验；复现使用上述扩充入口及基准配置，候选参数文件用于记录实验，不作为 v0.3 回测入口的配置。它不覆盖 `config/strategy.json`，也不接入运行时。

若目标改为“胜率和盈亏因子达到下限后，总净收益最大”，使用 [`return_maximization.json`](return_maximization.json) 和独立输出目录 `../results/return_maximization/`。本轮在训练段至少 50% 胜率、PF 1.3，回顾验证段至少 50% 胜率、PF 1.5 的约束下，网格内优先候选为 `grid_0580`：完整区间 48 笔全局单仓成交，胜率 68.8%、PF 2.12、总净收益 17.17R、风险预算账户收益 8.90%。这仍是回顾性筛选，未覆盖 v0.3。

取消上影线过滤的对照使用 [`no_wick_return_maximization.json`](no_wick_return_maximization.json)，结果在 `../results/no_wick_return_maximization/`。无上影线组合没有任何一组同时通过验证段门槛；完整区间净收益最高的 `grid_0028` 为 13.49R、62.5% 胜率、PF 1.64，但验证段为 45.5% 胜率、PF 0.81，因此不推荐直接裸空替换 `grid_0580`。

若重点是吃到止盈后的长跌段，使用 [`trend_capture.json`](trend_capture.json) 和 [`src/trend_capture.py`](../src/trend_capture.py)。脚本先加载全部行情到内存，再同时测试移动止损和翻倍后 24/48 小时内的破位、EMA20 反抽补空。结果写入 `../results/trend_capture/`；当前最好的组合将全局单仓总净收益提高到 20.94R、账户收益 10.72%，但最大回撤升至 6.81%，验证段 PF 只有 1.37，仍是补充模块候选。

```bash
python3 strategies/extreme_wick_short/src/trend_capture.py \
  --data-dir data/kline/okx/swap/5m \
  --experiment strategies/extreme_wick_short/research/trend_capture.json \
  --output-dir strategies/extreme_wick_short/results/trend_capture
```

在推荐组合上比较实时成交和限价方式，使用 [`entry_orders.json`](entry_orders.json) 与 [`src/entry_orders.py`](../src/entry_orders.py)：

```bash
python3 strategies/extreme_wick_short/src/entry_orders.py \
  --data-dir data/kline/okx/swap/5m \
  --experiment strategies/extreme_wick_short/research/entry_orders.json \
  --output-dir strategies/extreme_wick_short/results/entry_orders
```

结果写入 `../results/entry_orders/`。信号收盘价实时成交的全局单仓总净收益为 22.27R，下一根开盘为 20.94R；前几根开盘/收盘高点限价均低于实时成交方式。
