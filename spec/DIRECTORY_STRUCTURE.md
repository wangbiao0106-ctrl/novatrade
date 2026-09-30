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
scripts/                            项目级启动、数据采集和服务诊断脚本
spec/                               稳定的工程规范和接口约定
```

## `data/kline/` 规则

- 只放 OKX 等交易所的虚拟币 K 线原始数据，以及描述这些数据的 `manifest.json` 等采集元数据。
- 目录按 `交易所/市场/周期` 分层；当前 OKX USDT 永续使用 `data/kline/okx/swap/{5m,15m,1h}`。
- 文件名必须包含标的和周期。原始导出使用 `SYMBOL_USDT_SWAP_5m_<start>_<end>.jsonl.gz`；回测缓存使用现有的 `SYMBOL_USDT_SWAP_<tf>_<start>_<end>.json` 格式。
- 报告、CSV 成交记录、参数网格、标的名单、NPZ 派生数组和策略缓存不属于行情数据，必须写入对应策略的 `results/`。
- 需要新的周期或市场时，只能在相同层级新增目录，不能恢复 `data/backtest`、`data/market_export` 等按用途混放的目录。

## `strategies/` 规则

- 每个策略使用一个稳定的 ASCII `snake_case` 目录名。策略名称变更时同步更新目录 README、命令和引用。
- 策略研究阶段的全部文件必须留在自己的目录中，不得把策略脚本放回项目级 `scripts/`，也不得把研究结果放进 `data/`。
- `results/` 可以保存可复现的轻量结果；大体积行情导出或可重建缓存应加入 `.gitignore`，但不能改变数据目录职责。
- `STRATEGY.md` 是策略实验室中唯一的人类规则真源；`config/strategy.json` 是与其版本对应的机器参数真源。`research/` 中的回测、扫描器和结果只能提供证据，不能反向修改规则定义。
- 策略完成并准备接入交易服务时，先在实验室更新 `STRATEGY.md` 和 `config/strategy.json`，再把该版本规则移植到 `Sources/`，同步更新 `Tests/` 和目录 README。运行时源码不能通过相对路径读取 `strategies/`。
- 删除或替换实验产物时，保留规则说明和能够重现结果的配置；同步修复所有文档、测试和命令路径。

## 策略实验室同步流程

策略修改必须按以下顺序进行：

1. 在 `strategies/<strategy_name>/STRATEGY.md` 修改规则、范围、风控或版本，并同步维护 `config/strategy.json` 的机器字段。
2. 在 `research/` 更新回测、信号扫描器和验证结果，确认新规则有可复现的证据；研究结果不能成为新的规则来源。
3. 根据实验室版本修改 `Sources/` 和 `Tests/`，在代码或同步记录中保留实验室版本与参数映射。运行时实现不得自行发明未写入实验室的条件。
4. 执行 `python3 scripts/validate_strategy_sync.py`、相关研究入口的 `--help`/测试、`swift test` 和 `git diff --check`。校验失败时不能称为已同步，也不能先改代码再回填实验室文档。

当前 `sweep_reversal_short` 的运行范围是 `dynamic.hotAltcoins`：后台每 30 秒刷新行情，排除主流币、稳定币及非加密资产后按 24h 报价成交额取前 20 个 USDT 线性永续山寨币。177 个推荐标的是历史回测快照，只用于复现研究结果。BTC 门控取不晚于信号时刻的最近已确认 1 小时 K 线；历史不足 200 根或无法对齐时关闭门控。

## 当前目录映射

| 目录 | 用途 | 状态 |
| --- | --- | --- |
| `strategies/hlsr/` | 高位扫顶反转及其 15 分钟回测 | 已接入 OKX 模拟盘执行层，默认禁用；`STRATEGY.md` 是人类规则真源、`config/strategy.json` 是机器参数真源（四个参数块都被源码读取）；三段退出状态机位于 `Sources/TradingService/HLSRPositionManager.swift` |
| `strategies/sweep_reversal_short/` | 山寨币二次扫顶做空 | 已集成 `Sources/TradingService/StrategyEngine.swift`（v1.3）|
| `strategies/ema_altcoin_long/` | 双均线交易山寨币做多 | 已集成 `Sources/TradingService/StrategyEngine.swift`；目录同时保存规则真源、回测和参数验证 |
| `strategies/ema_3line_pullback/` | EMA 20/60/120 回踩策略族历史研究（四方向） | 研究归档，不是规则真源，未接入运行时 |

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
