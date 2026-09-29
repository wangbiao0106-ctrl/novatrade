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

- [`hlsr/`](hlsr/)：高位流动性扫顶反转。
- [`sweep_reversal_short/`](sweep_reversal_short/)：山寨币二次扫顶做空（1h），已接入策略引擎。生产范围是每 30 秒刷新、按 24h 报价成交额排序的动态热门榜前 20 个合规山寨币；`config/universe_recommended.json` 的 177 个标的只用于历史回测基线，不是运行时绑定名单。
- [`ema_altcoin_long/`](ema_altcoin_long/)：双均线交易山寨币多（EMA20/60/120、1h），规则已冻结并接入运行时（`StrategyType.emaAltcoinLong`），仅纸面/模拟盘运行；样本外胜率低于其 40% 门槛，禁止实盘自动下单。
- [`ema_3line_pullback/`](ema_3line_pullback/)：EMA 回踩策略族的四方向历史研究归档，不是规则真源，未接入运行时。

`hlsr/` 仍是研究中的临时策略，不属于当前可选的运行时规则。资产类别排除清单（主流币、稳定币、股票/ETF/指数/商品）的唯一真源是 [`sweep_reversal_short/config/universe.json`](sweep_reversal_short/config/universe.json) 的 `exclude`，`Sources/TradingDomain/StrategyUniverseRules.swift`、`research/live_signal.py` 和两个 EMA 研究脚本都只是副本，由 `scripts/validate_strategy_sync.py` 逐一对齐。

目录细则见 [`spec/DIRECTORY_STRUCTURE.md`](../spec/DIRECTORY_STRUCTURE.md)。
