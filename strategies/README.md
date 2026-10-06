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

| 已定稿规则 | 全历史生产单仓 | 盈利分布与 K 线说明书 |
| --- | --- | --- |
| 山寨币二次扫顶（v1.4） | 54 笔，资金池终值 2.720x；已实现回撤 43.49% | [`sweep_reversal_short/STRATEGY.md`](sweep_reversal_short/STRATEGY.md) |

AI 决策策略由 backend 配置中心单独管理，实验室包只保存规则、参数和验收口径：

- [`codex_ai_decision/`](codex_ai_decision/)：Codex / `gpt-6-luna` 决策策略，配置中心 ID 为 `codex`，当前为候选规则。
- [`deepseek_ai_decision/`](deepseek_ai_decision/)：DeepSeek Harness / `deepseek-v4-pro` 决策策略，配置中心 ID 为 `deepseek`，当前为候选规则。

全历史结果仅说明规则已同步。年度分布属于条件预测，不能把历史期望当作收益保证；其他目录保持研究候选或归档状态。

- [`sweep_reversal_short/`](sweep_reversal_short/)：山寨币二次扫顶，已接入策略引擎。生产范围（v1.4）是每 30 秒刷新、24h 报价成交额不低于 300 万 USDT 且排名前 100 的合规山寨币（`dynamic.sweepCandidates`）；`config/universe_recommended.json` 的 177 个标的只用于历史回测基线，不是运行时绑定名单。
- [`codex_ai_decision/`](codex_ai_decision/) 与 [`deepseek_ai_decision/`](deepseek_ai_decision/)：AI 决策候选包。两者共享服务端 policy、订单网关和 live trading 闸门；DeepSeek 的 profile/编码与事件预筛必须先通过 usage 和回放验收，不能把实验室配置当作运行时文件读取。
- [`ema_3line_pullback/`](ema_3line_pullback/)：三线突破回踩，EMA 20/60/120 四方向历史研究归档，不是规则真源，未接入运行时。
- [`range_rejection_confirmation_short/`](range_rejection_confirmation_short/)：冲高阴线确认，突破近期高点后出现大实体阴线、收盘回到 EMA20 附近，等待右侧确认再做空的研究候选；未接入运行时。
- [`intraday_pump_retest_short/`](intraday_pump_retest_short/)：缩量二次拉升，山寨日内涨幅超过 60% 后，高点回落再突涨并缩量收高的研究候选；未接入运行时。
- [`personal_trading_style_backtest/`](personal_trading_style_backtest/)：交易风格回放，从 OKX 统一交易账单回放个人交易风格、验证入场前过滤条件的研究实验；未接入运行时。
- [`personal_style_strategy_variants/`](personal_style_strategy_variants/)：交易偏好分支，将账单归纳出的冲高做空、扫顶、回踩多头和均值回归偏好拆成多分支，用全量 5 分钟行情批量回顾验证；未接入运行时。
- [`liquid_crypto_trend_long/`](liquid_crypto_trend_long/)：高流动性趋势，在**下单时点**滚动 24h 报价成交额 ≥ 3000 万 USDT 的加密永续上做纯多头 1 小时趋势跟随（MA72 + 1% 滞回带 + 96 小时持仓上限 + 200% 波动率目标），以同池与全池买入持有篮子作为硬验收基准；未接入运行时。
- [`five_minute_surge_waterfall_short/`](five_minute_surge_waterfall_short/)：5m 大涨（严格 >20% / >30%）与前 4 小时高点突破状态组合，比较下一根开盘和最近 72 根实体顶部限价做空；研究候选，未接入运行时。
- [`extreme_negative_funding_110_short/`](extreme_negative_funding_110_short/)：独立验证 5m 日内涨幅 >80%、价格触及日内开盘价 +110% 且资金费率达到币种负值上限后挂限价做空，1 倍杠杆、20% 止损、分段止盈；当前行情目录没有历史资金费率，未接入运行时。
- [`sixty_day_support_retest_failed_breakout_short/`](sixty_day_support_retest_failed_breakout_short/)：最近 60 日暴涨超过 300%、当日冲高后支撑回踩、二次突破失败的 15m 市价做空研究候选；未接入运行时。
- [`spot_perp_hedged_accumulation/`](spot_perp_hedged_accumulation/)：单一主流币的 1:1 现货多头 + USDT 永续空头对冲研究；1h 背离/EMA20 回踩激活，价格每跨 4% 尝试转移 25% 基础币单位，但名义偏离硬限 20%，资金费和基差尚未建模，研究候选，未接入运行时。
- [`spot_adaptive_martingale/`](spot_adaptive_martingale/)：纯现货自适应有限马丁；1h ATR 网格在小波动中分批止盈，下跌中最多四层递增买入，带现金底线、库存上限和 35% 应急退出，BTC/ETH/OKB 价格层研究，未接入运行时。

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
