# SPHA 研究计划

本目录记录研究口径和复现入口；当前结果仍属于价格层敏感性分析，不是运行时实现或实盘收益证明。研究代码和输出都留在 `strategies/spot_perp_hedged_accumulation/` 内。

## 数据

回测入口读取 `data/kline/okx/swap/5m/` 下的已确认永续 K 线，先按连续完整的 12 根聚合成 1 小时。现货与永续在当前脚本中用同一价格序列代理；永续还需要标记价、指数价、资金费结算值、合约乘数、最小张数、手续费和可成交盘口。缺失这些数据的实验只能标记为价格层敏感性分析，不能作为策略晋级证据。

当前代理让现货和永续使用同一个价格，但两腿的仓位权益会因现货市值与永续未实现盈亏的变化而产生差异。回测以分配权益为基础计算 `E_spot`、`E_short`；`equity_deviation` 和 `notional_deviation` 仅作为风险报告，不触发普通调仓。核心对冲由 `core_spot_qty == core_short_base_qty` 保证，战术 Delta 由资金池买卖的战术现货产生并受上限约束。真实现货/永续价格、基差、标记价和资金费仍然缺失，因此报告不能替代真实双市场回放。

```bash
python3 strategies/spot_perp_hedged_accumulation/src/hedged_backtest.py \
  --data-file data/kline/okx/swap/5m/BTC_USDT_SWAP_5m_<start>_<end>.jsonl.gz \
  --config strategies/spot_perp_hedged_accumulation/config/strategy.json \
  --output-dir strategies/spot_perp_hedged_accumulation/results/fixed_short_grid/btc/2x
```

主流币快照和 3×3 网格敏感性（3/4/8% 价格阈值 × 10/25/50% 可用资金池使用比例）：

```bash
python3 strategies/spot_perp_hedged_accumulation/research/run_mainstream.py
```

报告写入 `results/mainstream/report.json` 和 `results/mainstream/by_symbol.csv`。仓库当前缺少 TON 快照时会记录为 unavailable，不会用其他标的替代。

固定核心网格杠杆扫描：

```bash
python3 strategies/spot_perp_hedged_accumulation/research/run_fixed_short_grid.py
```

报告写入 `results/fixed_short_grid/report.json`、`results/fixed_short_grid/summary.csv` 和各标的的分杠杆子目录。

BTC、ETH、OKB 的固定核心网格和 2x/3x/5x/10x 杠杆扫描写入 `results/fixed_short_grid/report.json` 和 `results/fixed_short_grid/summary.csv`；OKB 不属于当前实例白名单，只用于研究比较。

## 最小实验集

1. 先固定 1h 基线，再在独立实验中比较 4h；按时间切分训练、验证、测试，不能按币种或结果随机打散。
2. 扫描阈值 3/4/8%、可用资金池使用比例 10/25/50%，同时检查战术 Delta 上限、资金池最低余额、EMA20/60、背离间隔和 ATR 容差的邻域，不以单个最优点作为规则。
3. 对比只持有现货、静态 1:1 现货空头和无信号固定网格；逐笔分解现货数量增量、空头数量、资金池、资金费、手续费、滑点和再平衡贡献。
4. 报告相对固定 1:1 对冲的组合误差、核心对冲偏离、战术最大/平均 Delta、最大/平均仓位权益偏离、最大/平均名义偏离、资金费净额（当前缺失）、资金池最低余额、总手续费、再平衡次数、全仓保证金利用率、双腿不同步成交、强平缓冲和最长连亏。

## 研究判定

验证和测试段都要能解释现货数量增量、战术 Delta、资金池最低余额、资金池触底退出次数、全仓保证金压力和相对静态 1:1 对冲基线的差异；报告仓位权益偏离何时超过 20%（仅作风险报告）、名义偏离和核心对冲异常。如果结果只由 BTC/ETH、某一个周期或某一个参数点贡献，继续标记 `candidate`；只有在明确审阅并确认规则后，才允许进入运行时同步流程。
