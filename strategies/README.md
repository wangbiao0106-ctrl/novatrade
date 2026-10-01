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

- [`hlsr/`](hlsr/)：高位扫顶反转做空，运行时只扫描 24h 涨幅 >40% 且报价成交额 >3,000 万 USDT 的候选山寨币，已接入本地策略引擎的 OKX 模拟盘；默认禁用，不自动提交真实订单。
- [`sweep_reversal_short/`](sweep_reversal_short/)：山寨币二次扫顶做空，已接入策略引擎。生产范围是每 30 秒刷新、按 24h 报价成交额排序的动态热门榜前 20 个合规山寨币；`config/universe_recommended.json` 的 177 个标的只用于历史回测基线，不是运行时绑定名单。
- [`ema_altcoin_long/`](ema_altcoin_long/)：双均线交易山寨币做多，运行时扫描合规山寨币成交额前 50，已接入运行时策略引擎；研究目录同时保留回测和参数验证。
- [`ema_3line_pullback/`](ema_3line_pullback/)：EMA 回踩策略族的四方向历史研究归档，不是规则真源，未接入运行时。
- [`extreme_wick_short/`](extreme_wick_short/)：山寨日内涨幅超过 100% 的 15m 动能衰竭做空研究候选；未接入运行时。
- [`double_pump_exhaustion_short/`](double_pump_exhaustion_short/)：由 `extreme_wick_short` 的 `grid_0580` 固定出的日内翻倍动能衰竭确认做空正式规则；已接入纸面/模拟运行时，默认停用。
- [`intraday_pump_retest_short/`](intraday_pump_retest_short/)：山寨日内涨幅超过 60% 后，高点回落再突涨并缩量收高做空研究候选；未接入运行时。

`hlsr/` 的规则和参数仍以本目录为真源，运行时只移植定稿规则，不读取研究目录。HLSR 的稳定代码标识是 `hlsr`，界面显示“高位扫顶反转做空”，执行范围是确认的 15m + 已完成 4H 候选山寨币：24h 涨幅严格大于 40%、报价成交额严格大于 3,000 万 USDT；它不复用通用热门榜前 20。研究结果目前为 FAIL，只能作为 OKX 模拟盘候选。资产类别排除清单（主流币、稳定币、股票/ETF/指数/商品）的唯一真源是 [`sweep_reversal_short/config/universe.json`](sweep_reversal_short/config/universe.json) 的 `exclude`，`Sources/TradingDomain/StrategyUniverseRules.swift`、`research/live_signal.py` 和两个 EMA 研究脚本都只是副本，由 `scripts/validate_strategy_sync.py` 逐一对齐。

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
python3 scripts/strategy_package.py pack strategies/hlsr \
  --output /tmp/hlsr-1.1.0.zip --version 1.1.0 --finalized
python3 scripts/strategy_package.py install /tmp/hlsr-1.1.0.zip
python3 scripts/strategy_package.py uninstall hlsr
```

安装会拒绝未定稿、摘要错误、降级和同版本覆盖；升级或重建同版本必须明确
使用 `--force`。卸载会删除整个策略包目录及注册表记录，交易服务应先停止
策略并清理持仓。
