# 现货永续对冲囤币（研究候选）

状态：研究候选，未接入运行时  
稳定标识：`spot_perp_hedged_accumulation`  
规则版本：`0.3.0`

本策略以同一主流币的现货多头和 USDT 线性永续空头组成两条腿。初始资金 20% 保留在策略资金池，80% 分配给两腿；核心现货数量与核心空头数量始终保持 1:1，普通调仓只用资金池买卖有限的战术现货，不调整空头。价格向下时分批买入战术现货，价格向上时只卖出战术现货并把资金收回资金池。核心价格 Delta 理论上为零，但战术现货会产生有上限的临时净多敞口；永续订单使用全仓模式。策略只有在一个实例内绑定一个标的，不自动轮换标的。

当前只完成研究规则和价格层回测。运行时实现、下单接口、策略实例 UI 和真实账户接入都必须等规则获得明确确认后再进行。

研究中的仓位权益定义为“分配给该仓位的初始/追加权益 + 未实现盈亏 - 该腿费用 - 资金费”。永续的实际所需保证金只是名义价值除以杠杆，剩余部分作为全仓可用保证金缓冲；杠杆会改变保证金占用、追加能力和强平距离，但不改变核心两腿的初始分配权益。仓位权益偏离只用于风险报告和退出判断，不驱动普通单边调空头。资金池触底且仍有战术净多现货时，策略会平掉合约、卖出现货并停止该实例。

## 文件

- [`STRATEGY.md`](STRATEGY.md)：唯一的人类可读规则真源。
- [`config/strategy.json`](config/strategy.json)：与规则版本对应的机器参数。
- [`research/README.md`](research/README.md)：数据要求、回测协议、参数扫描和晋级门槛。
- [`research/run_fixed_short_grid.py`](research/run_fixed_short_grid.py)：BTC、ETH、OKB 的固定核心网格和杠杆扫描入口。
- [`src/hedged_backtest.py`](src/hedged_backtest.py)：单标的 5m -> 1h 聚合、因果信号和两腿再平衡回测。
- [`results/fixed_short_grid/report.json`](results/fixed_short_grid/report.json)：固定核心对冲、战术现货网格的最新研究结果。

## 研究入口

研究代码和结果只允许留在本目录的 `src/`、`tests/`、`research/` 和 `results/`，不得写入 `data/kline/` 或项目级 `scripts/`。单币回测示例：

```bash
python3 strategies/spot_perp_hedged_accumulation/src/hedged_backtest.py \
  --data-file data/kline/okx/swap/5m/BTC_USDT_SWAP_5m_<start>_<end>.jsonl.gz \
  --config strategies/spot_perp_hedged_accumulation/config/strategy.json \
  --output-dir strategies/spot_perp_hedged_accumulation/results/btc
```

脚本只使用完整连续的 5 分钟 K 线聚合 1 小时；结果是价格层敏感性研究，不包含历史资金费、基差、盘口或合约张数撮合。

## 当前研究快照

基于仓库截至 2026-10-03 的 5 分钟快照，20 个白名单中有 19 个币种可用（缺少 TON），报告以现货数量增量、战术 Delta、仓位权益偏离、名义偏离、资金池最低余额、组合相对固定 1:1 对冲基线的偏差、手续费、资金费为重点；3/4/8% 阈值与 10/25/50% 资金池使用比例的敏感性表见 [`results/mainstream/report.json`](results/mainstream/report.json)。

在真实资金费、基差、标记价、全仓维持保证金和双腿成交数据补齐前，不能宣称“无风险”或“最多亏损手续费和资金费”。当前版本只保证核心现货与核心空头的数量对冲；战术现货、资金池、资金费、基差、强平和双腿不同步仍需单列压力测试。策略保持 `candidate`，不进入运行时。

固定核心网格版本使用同一回测窗口，并将 2x/3x/5x/10x 作为保证金和清算压力敏感性参数。OKB 当前不在实例可选白名单中，仅作为单独研究标的回测。
