# HLSR 高位扫顶反转策略

版本：1.0  
英文名：HLSR - High-Level Liquidity Sweep Reversal  
市场：OKX USDT 线性永续合约  
显示名：高位扫顶反转做空
用途：已接入本地策略引擎的 OKX 模拟盘和信号生成；默认禁用，不自动启用策略或提交真实订单。

适合标的：高波动且流动性充足的 OKX USDT 线性永续山寨币。运行时筛选 24h 涨幅严格大于 40%、报价成交额严格大于 3,000 万 USDT 的候选池，排除 BTC、ETH 等主流币、稳定币、股票/ETF/指数/商品和其他非加密合约；缺少连续 15m/4H 数据、报价成交额或盘口深度不足的合约关闭信号。

## 1. 核心逻辑

策略只做空已经出现大幅上涨的山寨币。完整条件为：

> 24 小时涨幅大于 40% + 24 小时成交额大于 3,000 万 USDT + 扫过前高但收回 + 拒绝形态 + 局部结构破坏 = 做空信号

策略使用 15 分钟入场周期、4 小时判断市场状态（此前文档写的 1 小时从未参与任何判定，代码里的 1H 序列只用于一个恒真 guard，已删除）。所有判断只能使用当前信号收盘时已经确认的 K 线；入场默认放在确认 K 线之后的下一根 K 线开盘。

## 2. 硬性筛选

在当前 15 分钟 K 线 `i` 上计算：

```text
gain_24h = close[i] / close[i - 96] - 1
quote_volume_24h = sum(volCcyQuote[i - 95:i + 1])
```

只有以下条件同时满足时才继续检查信号：

```text
gain_24h > 0.40
quote_volume_24h > 30,000,000 USDT
```

主流币和非 USDT 线性永续合约不在策略标的池中。当前离线优化从满足历史复合事件次数的合约中选择事件最多的 50 个标的。

## 3. 市场状态和高位区

4 小时周期计算 EMA20、EMA50、最近 20 根 K 线高低点和 ATR14：

- `bearish`：收盘价低于 EMA20，且 EMA20 低于 EMA50；
- `range`：最近区间宽度不大于 `max(8%, ATR / 中点 * 8)`；
- `bullish` 和 `transition` 默认不做空。

阻力参考值为：

```text
resistance = recent_high + 0.25 * ATR14(4H)
```

扫顶位置按距离阻力的 ATR 倍数标记为 `normal_extension`、`primary_sweep` 或 `extreme_sweep`。

## 4. 扫顶和拒绝

前高使用 15 分钟周期前 `swing_lookback` 根 K 线的最高价：

```text
high[i] > previous_swing_high
close[i] < previous_swing_high
```

拒绝评分由以下四项组成，每项满足计 1 分：

1. 上影线占本根振幅达到 `wick_ratio`；
2. 收阴；
3. 成交额超过前 20 根均值乘以 `volume_multiple`；
4. 深度失败突破：`close[i] < previous_swing_high − reject_depth_atr × ATR14[i]`（默认 `reject_depth_atr = 0.1`）。

第 4 项必须比"扫顶"本身更严格。扫顶定义里已经要求 `close[i] < previous_swing_high`，
若第 4 项只重复该条件则恒为真，评分会整体虚高一分、`minimum_rejection_score` 实际
降一档。`reject_depth_atr` 必须大于 0。

默认至少需要 2 分。

## 5. 右侧确认和入场

扫顶后最多等待 `confirmation_window` 根 K 线，满足以下任意一项：

- 收盘跌破扫顶后的局部低点；
- 反抽接近前高（容差约 0.5%），同时收阴并收盘低于前高。

确认发生后，在下一根 15 分钟 K 线开盘做空。信号生成器在下一根 K 线尚未出现时会标记为 `PENDING_NEXT_OPEN`，不会把确认收盘价假装成已成交价。

## 6. 止损、止盈和仓位

默认 2 倍杠杆；新建策略实例可在 1–100 倍范围调整。杠杆只影响保证金占用，单笔风险仍按价格止损距离计算。

```text
stop = sweep_high + stop_atr * ATR14(15M)
risk = stop - entry
```

止盈采用结构位和 R 倍数中较保守的组合：

- TP1：确认前后最近支撑，平仓 30%；
- TP2：4 小时区间中点，或至少 2R，平仓 30%；
- TP3：前一主要低点，或至少 3R，平仓 40%。

TP1 成交后止损移到开仓价；TP2 或更远目标成交后，使用最近 2 根 15 分钟 K 线高点收紧止损。若同一根 K 线同时触发止盈和止损，按止损优先。价格跳空越过止损时按下一根开盘价估算退出。

## 7. 失效和重新入场

出现以下任意情况，空头逻辑失效：

- K 线收盘重新站上扫顶高点；
- 价格触及初始止损或动态止损；
- 信号确认窗口结束但没有确认。

成交口径（与回测一致）：

- 触及止损价 → 按该止损价离场；若该 K 线**开盘已越过止损价**，按开盘价离场（更差）；
- 仅"收盘重新站上扫顶高点"而该 K 线并未触及止损 → 按**该 K 线收盘价**离场，不能按从未成交的止损价记账；
- 持仓到数据窗口末端仍未离场 → 按最后一根收盘价标记离场，`exit_reason` 记为 `window_end`，与正常止损/止盈/失效区分统计；
- TP2 之后的跟踪止损窗口长度取自 `trail_bars`（含当根），不再硬编码。

单笔交易退出后进入冷却期，不允许立即加仓或摊平。必须重新出现新的阻力、扫顶、拒绝和结构确认，才允许重新生成信号。

## 8. 离线优化基线

当前标准参数来自 180 天市场导出数据的滚动训练/验证/测试：

```json
{
  "swing_lookback": 6,
  "wick_ratio": 0.6,
  "volume_multiple": 1.0,
  "minimum_rejection_score": 2,
  "reject_depth_atr": 0.1,
  "confirmation_window": 4,
  "stop_atr": 0.25,
  "trail_bars": 2,
  "allow_range": true,
  "zone_required": "any"
}
```

2026-09 修正后重跑（标的池只用数据起点起 60 天训练期的硬筛选事件数挑选；拒绝评分第 4 项改为深度失败突破；`symbol_from_path` 修正；接受标准统一为五项；失效离场按收盘价而非止损价记账；跟踪窗口改用 `trail_bars`；不完整 15m 桶丢弃；参数搜索空间与回测统一为 384 组，此前市场导出只有 128 组），报告见 `results/hlsr_market_export_report.json`：

- 搜索 384 组、样本外 8 笔、胜率 **37.5%**、平均净 R **+0.455**、实际平均收益/风险 **2.84**；
- 接受标准判定为 **FAIL**：`win_rate_gt_50pct=false`、`sample_sufficient_30_trades=false`（8 笔低于 30 笔门槛），其余三项通过；
- 因此**不能**作为已通过的策略证据。

修正过程作废的几版数字，都不得再引用：

| 版本 | 样本外 | 问题 |
|---|---|---|
| 最早报告 | 9 笔 / 55.56% / 收益风险 3.04 / `passed=true` | 选币用含测试期的整段数据（前视）、拒绝评分第 4 项恒真、`passed` 未含样本量门槛 |
| 中间版 | 10 笔 / 50.0% / +0.978R / 2.90 | 失效离场与跟踪窗口修正前；`passed` 只看 3 项 |
| 128 组网格版 | 10 笔 / 50.0% / +1.015R / 2.97 | 搜索空间与 `DESIGN.md` 声明的 384 组不符 |

风险收益表述按“风险:收益不高于 1:2”解释，即收益/风险至少为 2.0。

## 9. 机器参数与接受标准

- 机器参数真源是 [`config/strategy.json`](config/strategy.json)：`signal_parameters`、`hard_filters`、`position_management`（杠杆/分批/冷却）、`costs`（手续费/滑点/资金费）四个块都由 `src/` 读取，代码里不得再出现判定用的字面量。
- 接受标准（`passed`）的唯一实现在 `src/high_short_strategy.py` 的 `acceptance_criteria()`，两个报告脚本共用；定义见 [`DESIGN.md`](DESIGN.md) 的「回测」一节。
- 参数搜索空间唯一实现在 `parameter_grid()`（384 组），回测与市场导出共用。

## 10. 使用信号生成器

读取单个 5 分钟 JSONL 或 gzip JSONL 文件，自动聚合为 15 分钟：

```bash
python3 strategies/hlsr/src/hlsr_signal_generator.py \
  --input data/kline/okx/swap/5m/BEAT_USDT_SWAP_5m_20260331T065300Z_20260928T125358Z.jsonl.gz \
  --symbol BEAT-USDT-SWAP \
  --config strategies/hlsr/config/strategy.json
```

## 11. 运行时集成边界

HLSR 的稳定代码标识为 `hlsr`，领域层类型为 `StrategyType.hlsr`，界面显示为“高位扫顶反转做空”。运行时实现位于 `Sources/TradingDomain/`、`Sources/TradingService/`、`Sources/TradingService/HLSRPositionManager.swift`、`Sources/OKXLocalD/` 和 `Sources/MacTraderApp/`，不会读取本目录文件；规则和默认机器参数分别由本文件与 [`config/strategy.json`](config/strategy.json) 维护。

- 运行范围是每 30 秒刷新、筛选 24 小时涨幅严格大于 40% 且报价成交额严格大于 3,000 万 USDT 的动态候选山寨币池；它不是通用热门榜前 20。每个策略实例最多一个活动币种，默认每笔风险为账户权益的 1%，开放止损风险上限为 1%。
- 15 分钟确认 K 线和已完成的 4 小时 K 线都必须连续、已确认且不重复；4 小时状态至少需要 55 根历史 K 线。成交额硬筛选只接受 OKX `volCcyQuote`，缺字段或历史不足时关闭信号，不以合约张数成交量替代。
- 确认后只在下一根 15 分钟 K 线开盘估算市价入场；止损、TP1/TP2/TP3、TP1 后保本、TP2 后两根 K 线高点跟踪、收盘失效和 16 根 K 线冷却均由持仓管理状态机执行。止损优先，分批数量按合约 lot 规格向下取整，重启前持仓腿、目标和冷却状态必须持久化。
- OKX 模拟盘可由人工启用，真实交易仍需额外手动开关；研究样本外结果目前为 **FAIL**（8 笔、37.5% 胜率、平均净 R +0.455），不能把该结果描述为已证明盈利。

默认只输出最近一个信号。需要导出全部历史信号时添加 `--all`，需要写入文件时添加 `--output path/to/signals.json`。该命令只输出信号、入场参考价、止损、止盈和风险字段，不会连接交易所或提交订单。
