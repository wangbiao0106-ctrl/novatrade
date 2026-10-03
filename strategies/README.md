# 策略实验室

`strategies/` 是策略规则的唯一来源。每种策略必须使用一个独立子目录，目录内保存规则说明、机器配置、研究代码、测试和结果；`Sources/` 只保存依据已定稿规则实现的运行时代码，不能反向定义或覆盖策略规则。

每个策略目录遵循以下职责：

- `STRATEGY.md` 是唯一的人类可读规则真源，记录版本、信号、范围、风控和生产实现位置。
- `config/strategy.json` 是由规则真源维护的机器参数真源，必须与 `STRATEGY.md` 的版本和参数一致。
- `research/` 保存回测、扫描器和验证证据；研究结果是证据，不是规则来源。
- `README.md` 记录策略状态、入口和运行时实现位置。

修改策略时必须先更新策略实验室中的 `STRATEGY.md` 与 `config/strategy.json`，再根据新版本修改 `Sources/` 和 `Tests/`。运行时代码不得读取本目录文件。完成同步后执行：

```bash
python3 scripts/validate_strategy_sync.py
swift test
git diff --check
```

校验未通过时不得把代码称为已同步，也不得先修改 `Sources/` 再倒推实验室文档。

当前策略：

- [`sweep_reversal_short/`](sweep_reversal_short/)：山寨币二次扫顶做空，已接入策略引擎。生产范围（v1.4）是每 30 秒刷新、24h 报价成交额不低于 300 万 USDT 且排名前 100 的合规山寨币（`dynamic.sweepCandidates`）；`config/universe_recommended.json` 的 177 个标的只用于历史回测基线，不是运行时绑定名单。
- [`ema_3line_pullback/`](ema_3line_pullback/)：EMA 回踩策略族的四方向历史研究归档，不是规则真源，未接入运行时。
- [`extreme_wick_short/`](extreme_wick_short/)：山寨日内涨幅超过 100% 的 15m 动能衰竭做空研究候选；未接入运行时。
- [`double_pump_exhaustion_short/`](double_pump_exhaustion_short/)：由 `extreme_wick_short` 的 `grid_0580` 固定出的日内翻倍动能衰竭确认做空正式规则；已接入运行时，按连接账号类型路由模拟或实盘，默认停用。
- [`range_rejection_confirmation_short/`](range_rejection_confirmation_short/)：突破近期高点后出现大实体阴线、收盘回到 EMA20 附近，等待右侧确认再做空的研究候选；未接入运行时。
- [`intraday_pump_retest_short/`](intraday_pump_retest_short/)：山寨日内涨幅超过 60% 后，高点回落再突涨并缩量收高做空研究候选；未接入运行时。
- [`personal_trading_style_backtest/`](personal_trading_style_backtest/)：从 OKX 统一交易账单回放个人交易风格、验证入场前过滤条件的研究实验；未接入运行时。
- [`personal_style_strategy_variants/`](personal_style_strategy_variants/)：将账单归纳出的冲高做空、扫顶、回踩多头和均值回归偏好拆成多分支，用全量 5 分钟行情批量回顾验证；未接入运行时。
- [`liquid_crypto_trend_long/`](liquid_crypto_trend_long/)：在**下单时点**滚动 24h 报价成交额 ≥ 3000 万 USDT 的加密永续上做纯多头 1 小时趋势跟随（MA72 + 1% 滞回带 + 96 小时持仓上限 + 200% 波动率目标），以同池与全池买入持有篮子作为硬验收基准；未接入运行时。

资产类别排除清单（主流币、稳定币、股票/ETF/指数/商品）的唯一真源是 [`sweep_reversal_short/config/universe.json`](sweep_reversal_short/config/universe.json) 的 `exclude`，`Sources/TradingDomain/StrategyUniverseRules.swift`、`research/live_signal.py` 和 `ema_3line_pullback/src/backtest.py` 都只是副本，由 `scripts/validate_strategy_sync.py` 逐一对齐。

目录细则见 [`spec/DIRECTORY_STRUCTURE.md`](../spec/DIRECTORY_STRUCTURE.md)。

## 策略配置包（实验定稿、安装与卸载）

策略生成、升级和调优只在对应实验室目录完成。实验定稿后用
[`scripts/strategy_package.py`](../scripts/strategy_package.py) 生成带
`manifest.json`、版本和 SHA-256 artifact 清单的配置包；运行时导入只写入
应用数据目录 `~/Library/Application Support/NovaTrade/strategy-packages/`（也可通过
`OKX_STRATEGY_PACKAGES_DIR` 指定），不修改仓库中的 `strategies/` 或 Swift
源码。完整 schema、生命周期和 AI 指令约定见
[`spec/STRATEGY_PACKAGE.md`](../spec/STRATEGY_PACKAGE.md)。

```bash
python3 scripts/strategy_package.py pack strategies/sweep_reversal_short \
  --output /tmp/sweep-reversal-short-1.4.0.zip --version 1.4.0 --finalized
python3 scripts/strategy_package.py install /tmp/sweep-reversal-short-1.4.0.zip
python3 scripts/strategy_package.py uninstall sweep_reversal_short
```

安装会拒绝未定稿、摘要错误、降级和同版本覆盖；升级或重建同版本必须明确
使用 `--force`。卸载会删除整个策略包目录及注册表记录，交易服务应先停止
策略并清理持仓。
