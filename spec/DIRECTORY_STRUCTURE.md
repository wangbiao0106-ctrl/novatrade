# 项目目录规范

本文件是行情数据、策略实验室和策略研究目录的唯一约束来源。新增文件、迁移文件和编写脚本时都必须遵循这里的边界。

## 总体布局

```text
data/
└── kline/                         只存虚拟币 K 线和采集清单
    └── <exchange>/<market>/<tf>/  例如 okx/swap/5m、15m、1h

strategies/
├── README.md
└── <strategy_name>/                每种临时策略一个独立目录
    ├── README.md                   策略状态、入口和源码导入位置
    ├── STRATEGY.md                 可复现的规则和参数
    ├── config/                     策略配置、标的池和参数快照
    ├── src/                        临时实现、信号生成器和回测器
    ├── tests/                      只验证该策略的测试
    ├── research/                   研究笔记、实验配置和对比分析
    └── results/                    报告、成交记录、参数网格和缓存

Sources/                            正式运行时 Swift 源码
Tests/                              正式源码的跨模块测试
scripts/                            项目级启动、数据采集、账户迁移和服务诊断脚本
spec/                               稳定的工程规范和接口约定
```

## `data/kline/` 规则

- 只放 OKX 等交易所的虚拟币 K 线原始数据，以及描述这些数据的 `manifest.json` 等采集元数据。
- 原始目录按 `交易所/市场/周期` 分层；当前 OKX USDT 永续原始数据使用 `data/kline/okx/swap/5m/`。15m、30m、1h、4h 等派生 Parquet 缓存由 `scripts/build_kline_cache.py` 写入仓库根目录 `.cache/kline/okx/swap/`，不属于 `data/kline/`。
- 原始文件名必须包含标的和周期，使用 `SYMBOL_USDT_SWAP_5m_<start>_<end>.jsonl.gz`；可重建缓存的文件名和 manifest 由缓存脚本管理。
- 报告、CSV 成交记录、参数网格、标的名单、NPZ 派生数组和策略缓存不属于行情数据，必须写入对应策略的 `results/`。
- 需要新的周期或市场时，只能在相同层级新增目录，不能恢复 `data/backtest`、`data/market_export` 等按用途混放的目录。

## `strategies/` 规则

- 每个策略使用一个稳定的 ASCII `snake_case` 目录名。策略名称变更时同步更新目录 README、命令和引用。
- 策略研究阶段的全部文件必须留在自己的目录中，不得把策略脚本放回项目级 `scripts/`，也不得把研究结果放进 `data/`。
- `results/` 可以保存可复现的轻量结果；大体积行情导出或可重建缓存应加入 `.gitignore`，但不能改变数据目录职责。
- 对普通策略，`STRATEGY.md` 是策略实验室中唯一的人类规则真源；`config/strategy.json` 是与其版本对应的机器参数真源。Codex AI 决策包的公共门禁另见 [`spec/AI_DECISION_POLICY.md`](AI_DECISION_POLICY.md)，其 `STRATEGY.md` 维护模型调用与分组分析边界。`research/` 中的回测、扫描器和结果只能提供证据，不能反向修改规则定义。
- 策略完成并准备接入交易服务时，先在实验室更新 `STRATEGY.md` 和 `config/strategy.json`，再把该版本规则移植到 `Sources/`，同步更新 `Tests/` 和目录 README。运行时源码不能通过相对路径读取 `strategies/`。
- 删除或替换实验产物时，保留规则说明和能够重现结果的配置；同步修复所有文档、测试和命令路径。

## 策略实验室同步流程

策略修改必须按以下顺序进行：

1. 在 `strategies/<strategy_name>/STRATEGY.md` 修改规则、范围、风控或版本，并同步维护 `config/strategy.json` 的机器字段。
2. 在 `research/` 更新回测、信号扫描器和验证结果，确认新规则有可复现的证据；研究结果不能成为新的规则来源。
3. 根据实验室版本修改 `Sources/` 和 `Tests/`，在代码或同步记录中保留实验室版本与参数映射。运行时实现不得自行发明未写入实验室的条件。
4. 执行 `python3 scripts/validate_strategy_sync.py`、相关研究入口的 `--help`/测试、`swift test` 和 `git diff --check`。校验失败时不能称为已同步，也不能先改代码再回填实验室文档。

当前 `sweep_reversal_short` 的运行范围是 `dynamic.sweepCandidates`：后台每 30 秒刷新行情，排除主流币、稳定币及非加密资产，剔除 24h 报价成交额低于 300 万 USDT 的合约后按成交额取前 100 个 USDT 线性永续山寨币。177 个推荐标的是历史回测快照，只用于复现研究结果。BTC 门控取不晚于信号时刻的最近已确认 1 小时 K 线；历史不足 200 根或无法对齐时关闭门控。

## 当前目录映射

| 目录 | 用途 | 状态 |
| --- | --- | --- |
| `strategies/sweep_reversal_short/` | 山寨币二次扫顶 | 已集成 `Sources/TradingService/StrategyEngine.swift`（v1.4）|
| `strategies/codex_ai_decision/` | Codex / GPT AI 决策策略实验室包 | 候选规则；由 backend 配置中心 `codex` 管理 |
| `strategies/ema_3line_pullback/` | 三线突破回踩（EMA 20/60/120，四方向历史研究） | 研究归档，不是规则真源，未接入运行时 |
| `strategies/intraday_pump_retest_short/` | 缩量二次拉升（山寨日内涨幅超过 60% 后回落再突涨） | 研究候选，未接入运行时 |
| `strategies/personal_trading_style_backtest/` | 交易风格回放（OKX 统一账单回放、风格验证和候选筛选） | 研究候选，未接入运行时 |
| `strategies/personal_style_strategy_variants/` | 交易偏好分支（个人偏好拆分与全量行情回顾验证） | 研究候选，未接入运行时 |
| `strategies/range_rejection_confirmation_short/` | 冲高阴线确认（前高突破失败、确认跌破 EMA20） | 研究候选，未接入运行时 |
| `strategies/liquid_crypto_trend_long/` | 高流动性趋势（逐时点 24h 报价成交额 ≥ 3000 万 USDT 的 1h 信号） | 研究候选，未接入运行时 |
| `strategies/five_minute_surge_waterfall_short/` | 5m 大涨与前 4 小时高点状态组合、开盘/72 根实体顶部限价做空 | 研究候选，未接入运行时 |
| `strategies/extreme_negative_funding_110_short/` | 独立验证 5m 日内涨幅、+110% 限价做空、极端负资金费率过滤与分段止盈 | 研究候选，资金费历史缺失，未接入运行时 |
| `strategies/spot_perp_hedged_accumulation/` | 单主流币 1:1 现货多头 + USDT 永续空头的 1h 背离/均线回踩激活与有界再平衡 | 研究候选，名义偏离硬限 20%，资金费/基差缺失，未接入运行时 |
| `strategies/spot_adaptive_martingale/` | 纯现货 ATR 自适应网格与最多四层递增买入，现金底线和应急退出 | 研究候选，BTC/ETH/OKB 价格层结果，未接入运行时 |

## 迁移检查

目录调整完成后，至少执行以下检查：

1. `rg` 搜索旧路径，确认没有残留引用。
2. 运行对应策略测试和 `swift test`。
3. 对 Python 入口执行 `--help`，确认默认输入和输出路径分别落在 `data/kline/` 与策略 `results/`。
4. 运行 `python3 scripts/validate_strategy_sync.py`，确认实验室规则、参数映射和运行时范围一致。
5. 用 `git status` 确认迁移没有生成未预期的大型数据文件。

## 策略配置包

实验定稿后的策略可以打成带 `manifest.json`、版本和 artifact 摘要的配置包。
包导入/卸载由 [`scripts/strategy_package.py`](../scripts/strategy_package.py)
完成，只写应用数据目录下的 `NovaTrade/strategy-packages/` 和其中的
`registry.json`，不修改实验室源目录或 `Sources/`。包格式、生命周期和校验规则见
[`spec/STRATEGY_PACKAGE.md`](STRATEGY_PACKAGE.md)。

## 运行账户状态与迁移

纸面账户和交易历史属于运行数据，保存在应用支持目录 `~/Library/Application Support/NovaTrade/`，不放进 `data/kline/` 或策略研究目录。`paper-account.json` 保存本地撮合账本；`paper-state.json` 保存策略配置、运行状态和风控快照。账户迁移命令 [`scripts/reset_paper_account.py`](../scripts/reset_paper_account.py) 只能在后台停止后执行；它将原运行文件备份到状态目录的 `backups/paper-reset-<UTC时间>/`，保留策略和 AI 参数但全部停用，再建立新的纸面账户。备份不属于仓库产物，目录权限为 `700`，文件权限为 `600`。
