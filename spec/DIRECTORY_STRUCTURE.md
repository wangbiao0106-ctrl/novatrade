# 项目目录规范

本文件是行情数据和策略研究目录的唯一约束来源。新增文件、迁移文件和编写脚本时都必须遵循这里的边界。

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
- 策略完成并准备接入交易服务时，先在 `STRATEGY.md` 写明状态和生产实现位置，再把规则移植到 `Sources/`。运行时源码不能通过相对路径读取 `strategies/`。
- 删除或替换实验产物时，保留规则说明和能够重现结果的配置；同步修复所有文档、测试和命令路径。

## 当前目录映射

| 目录 | 用途 | 状态 |
| --- | --- | --- |
| `strategies/hlsr/` | 高位流动性扫顶反转及其 15 分钟回测 | 研究中，可生成信号 |
| `strategies/sweep_reversal_short/` | 山寨币二次扫顶做空（1h） | 已集成 `Sources/TradingService/StrategyEngine.swift` |

## 迁移检查

目录调整完成后，至少执行以下检查：

1. `rg` 搜索旧路径，确认没有残留引用。
2. 运行对应策略测试和 `swift test`。
3. 对 Python 入口执行 `--help`，确认默认输入和输出路径分别落在 `data/kline/` 与策略 `results/`。
4. 用 `git status` 确认迁移没有生成未预期的大型数据文件。
