# SWEEP_REVERSAL_SHORT 山寨币高位二次扫顶做空策略

版本：1.0（已集成到 NovaTrade 策略引擎，`StrategyType.sweepReversalShort`）
市场：OKX USDT 线性永续合约 · 1 小时 K 线
用途：行情扫描与信号生成；信号附带 ATR 标定的止损/止盈价位，可接入模拟盘下单流程。

## 1. 核心逻辑

> 12 天最高点被连续两次假突破（扫顶）且收盘回落 + 多头衰竭确认 + 大盘 BTC 门控 = 做空信号

只做空**中低流动性山寨币**（回测验证：该策略 alpha 集中在中小市值山寨币，高流动性前 100 名期望≈0；名单见 `strategy_sweep_reversal/out/universe_recommended.json`）。BTC 只作门控参考，不交易 BTC 本身。

## 2. 信号判定（1 小时收盘时逐根判定）

1. **高位摆动高点**：`high[p]` 是最近 **288 根（12 天）最高价**，且左 10 根严格更低、右 5 根不低于（L=10, R=5）。
2. **首次扫顶**：p 之后 96 根内首次出现 `high[s] > high[p]`，且 5 根内收盘跌回该价位下方（k1）。要求扫顶时 `RSI14[s] ≥ 62` 且 `volume[s] ≥ 1.5 × 48 根均量`。
3. **二次扫顶（核心）**：k1 之后 12 根内再次 `high[j] > high[p]` 且收盘回落。要求：
   - 二次高点低于首次扫顶极端价（`high[j] < max(high[s...k1])`，多头衰竭）；
   - 上穿深度 `high[j] − high[p] ≥ 0.2 × ATR14[j]`。
4. **BTC 门控**：信号 bar 收盘时 `BTC 1H 收盘 < BTC SMA200(1H)`，否则不发出信号（数据不足同样视为门控关闭）。
5. **入场**：二次扫顶 bar 收盘价做空。

## 3. 出场与风控（随信号下发）

- 止损：`扫顶期间最高价(ext) + 0.5 × ATR14[j]`；
- 止盈：`入场价 − 2.2 × (止损 − 入场价)`；
- 时间离场：96 根（4 天）未触发则收盘平仓；
- 薄盘/极端波动保护：`ATR/价格 < 0.5%` 或 `止损距离 > 5 × ATR` 不发信号；
- 单笔风险 ≤ 账户 1%（`下单金额 = 账户 × 1% ÷ 止损距离`），冷却 96 根。

## 4. 引擎参数（StrategyConfig.parameters）

| 参数 | 默认 | 说明 |
|---|---|---|
| L / R | 10 / 5 | 摆动点左右臂 |
| majorWindow | 288 | 高位窗口（12 天）|
| sweepWait / rejectWait / resweepWait | 96 / 5 / 12 | 各阶段等待窗口 |
| rsiMin | 62 | 首次扫顶 RSI 下限 |
| volMult | 1.5 | 首次扫顶量能倍数 |
| rsDeep | 0.2 | 二次扫顶上穿深度（×ATR）|
| bufATR | 0.5 | 止损缓冲（×ATR）|
| tpMult | 2.2 | 止盈 = 2.2R |
| minATRPct | 0.5 | 最小波动过滤（%）|
| maxRiskATR | 5.0 | 最大止损距离（×ATR）|
| btcGateEnabled | 1 | BTC<SMA200 门控（0=关）|

## 5. 回测绩效（2026-03-31 ~ 2026-09-27，推荐池 177 币）

- 60 笔；成功率 **51.7%**；盈亏比 **1.69R**；期望 **+0.37R/笔**；
- 训练段 +0.26R / 测试段 +0.51R；6 个月月度全部正期望；最大回撤 7.9R；最大连亏 6 笔；
- 0.1%/边 手续费假设下期望仍 +0.34R/笔。

完整研究与逐笔记录：`strategy_sweep_reversal/`（STRATEGY_SPEC.md、out/final_trades.csv）。

## 6. 系统集成说明

- 引擎：`Sources/TradingService/StrategyEngine.swift` 的 `evaluateSweepReversal`（与 Python 回测逐条一致）；
- 门控数据：`PaperTradingStore.evaluate` 缓存 BTC 1H K 线并传入引擎；
- 信号：`StrategySignal.stopPrice / takePrice` 携带止损止盈价位；
- UI：新建策略对话框可选"高位二次扫顶做空（山寨币）"规则；
- 建议在启动后端前确保订阅了 BTC-USDT-SWAP 1H 行情（门控依赖）。
