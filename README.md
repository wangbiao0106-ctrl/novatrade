# OKX Self Trader

面向苹果生态的本地 OKX 自助交易系统。当前版本以 OKX Agent Trade Kit（ATK）为连接层：Swift 应用使用 ATK 的 API Key profile，通过 `okx --json` 获取结构化数据；MCP 只作为 AI 客户端入口。

## 架构

```text
macOS SwiftUI
      |
本地交易服务（策略、风控、审计）
      |
ATKGateway（Swift Process 适配层）
      |
okx --json  <->  ~/.okx/config.toml API Key profile
      |
OKX REST API

iOS Monitor  --->  Mac 本地服务（只读，不持有凭据）
AI 客户端   --->  okx-trade-mcp（stdio，可只读或按模块启用）
```

Swift 代码不直接打开 `~/.okx/config.toml`，也不保存或展示 API Key、Secret、Passphrase 或 OAuth Token。`config show --json` 的原始输出可能包含密钥，适配层只在内存中提取 profile/site/是否存在 API Key，随后丢弃原始输出，绝不写入日志、界面或审计。

## ATK API Key 配置

当前项目固定使用 ATK API Key，不调用 OAuth：

```bash
npm install -g @okx_ai/okx-trade-cli@1.4.8 @okx_ai/okx-trade-mcp@1.4.8
okx config init
okx config show --json
```

配置向导会设置站点、实盘/模拟盘和 API Key、Secret Key、Passphrase。API Key 只需 `Read + Trade` 权限，禁止 `Withdraw`。

Swift 应用不会直接打开配置文件，也不会持久化或展示密钥。`config show --json` 的原始输出可能包含密钥，适配层只在内存中提取 profile/site/是否存在 API Key，随后丢弃原始输出。

如果配置中没有 API Key profile，交易路径会明确报错并要求先运行 `okx config init`。OAuth 相关 ATK 方法保留在底层适配器中，但当前应用不会调用。

## 当前版本

- macOS SwiftUI 交易工作台：支持永续合约搜索、自选收藏、合约切换、周期切换、行情指标和策略配置/启停。
- K 线使用 SwiftUI 原生 Canvas：红涨绿跌、连续拖拽、悬停十字线、日期/价格轴、成交量和 EMA。历史数据首次通过 API 预热；实时 K 柱仅使用 OKX V5 Business WSS 的 candle 频道，没有 K 线轮询或 ticker 拼接。WSS 不提供历史查询，因此首次历史加载仍需 API。
- `okx-locald` 使用 Hummingbird 提供 REST 和本地 WebSocket；上游订阅、断线、重连状态会传到图表底栏。使用 OKX 文本 ping/pong 保活，断线后指数退避并重新订阅。多个本地客户端订阅同一合约/周期时共享同一个上游 OKX 连接和辅助轮询（StreamHub 扇出），实时 K 柱只在服务端摄入一次。
- 本地服务对 ATK CLI 调用带 TTL 缓存和单飞去重（ticker 2s、账户/持仓/挂单 5s、合约列表与历史预热 60s），多个客户端并发轮询时不再重复拉起 CLI 进程；下单成功后立即失效账户缓存。盘口和逐笔成交通过 OKX REST 提供。
- 运行日志按 JSONL 持久化到状态目录的 `runtime-log.jsonl`，服务重启后自动回读；策略状态、订单账本和状态审计仍分别写入 `paper-state.json`、`paper-ledger.json` 和 `audit.jsonl`。
- 风控：日亏/回撤熔断按日历日正确滚动日初权益基线；模拟撮合支持部分平仓（保留剩余仓位）和同向加仓均价合并，已实现盈亏扣除手续费。
- 工作台通过 ATK CLI 读取实时 ticker 和 API Key profile 状态；未配置或暂时无法连接 ATK 时显示等待状态，不注入虚构行情或策略实例。
- iOS SwiftUI 入口：只读占位，不运行 CLI，不保存凭据。
- `ATKGateway`：使用 `Process` 调用 CLI，不经过 shell；支持 JSON 解码、退出码和测试替身。
- UI 默认不提供下单按钮；实盘 HTTP 接口需要先手动启用交易，再经过 profile、订单参数和风控检查。
- OKX 永续订单的 `quantity` 始终表示合约张数（`sz`），下单前读取 `ctVal/ctMult/lotSz/minSz/tickSz`；策略按真实报价名义价值换算并向下取整，规格缺失或不符合时拒绝下单。

## 山寨币高位做空回测

HLSR 回测只保留当前配置和可重建的输入，输出统一写入对应策略的 `results/` 目录。

```bash
python3 strategies/hlsr/src/altcoin_backtest.py --symbols 50 --min-trades 20 --end 2026-09-26T19:00:00+00:00
```


### 15分钟二次见顶版本

`strategies/hlsr/src/altcoin_backtest.py` 现在默认执行180天15分钟回测：滚动96根K线硬性筛选24小时涨幅至少40%、24小时报价成交额至少3,000万 USDT 的 USDT 线性永续，比较前高收盘转弱、上影线反抽和跌破EMA后反抽失败三类入场时机，使用2倍杠杆、1R/2R/最终目标分批止盈和高点跟踪止损。候选池默认只接受从窗口起点开始、至少覆盖最初60天且连续无缺口的本地15分钟训练片段，并按这60天成交额排序；训练片段之后的缺口只在报告 `data_quality` 中标记，不影响选币，避免用测试期末的当前成交量选币。交互式探索若要允许当前 live 合约补齐，必须显式加 `--allow-live-selection-fallback`，报告会记录该选择前视。参数通过60天训练、30天验证、30天测试的三个滚动窗口选择，并输出Beta胜率区间和按标的/日期聚类Bootstrap的正收益概率。

```bash
python3 strategies/hlsr/src/altcoin_backtest.py --symbols 50 --min-trades 20 --end 2026-09-26T19:00:00+00:00
python3 -m unittest strategies/hlsr/tests/test_altcoin_backtest.py
```

结果写入 `strategies/hlsr/results/`。当复合条件导致训练样本少于 `--min-trades` 时，程序仍输出报告并将对应折标记为未满足最小样本；这些结果可随时重建，不作为运行时数据。

### HLSR — High-Level Liquidity Sweep Reversal（已接入 OKX 模拟盘）

高位扫顶反转策略的稳定标识为 `hlsr`，界面显示“高位扫顶反转做空”。规则和流程图见 [`strategies/hlsr/STRATEGY.md`](strategies/hlsr/STRATEGY.md) 与 [`strategies/hlsr/DESIGN.md`](strategies/hlsr/DESIGN.md)，机器参数见 [`strategies/hlsr/config/strategy.json`](strategies/hlsr/config/strategy.json)。运行时位于 `Sources/TradingDomain/`、`Sources/TradingService/`、`Sources/TradingService/HLSRPositionManager.swift`、`Sources/OKXLocalD/` 与 `Sources/MacTraderApp/`，使用确认的 15m 入场和已完成的 4H 状态；默认禁用，可人工启用 OKX 模拟盘，不自动提交真实订单。

```bash
python3 strategies/hlsr/src/high_short_strategy.py --symbols 50 --days 180 --end 2026-09-26T19:00:00+00:00
python3 -m unittest strategies/hlsr/tests/test_high_short_strategy.py
```

结果写入 `strategies/hlsr/results/`。默认只从经过完整性校验的本地历史缓存选标的；历史缓存不足时命令会停止。交互式探索若要用当前 live 成交额补齐，必须显式加入 `--allow-live-selection-fallback`，报告会记录选择前视。该策略的回测结果必须通过样本外交易数、胜率和平均净R门槛才会标记为 `PASS`。

HLSR 的标准策略说明、参数配置和只读信号生成器分别位于：

- `strategies/hlsr/STRATEGY.md`
- `strategies/hlsr/config/strategy.json`
- `strategies/hlsr/src/hlsr_signal_generator.py`

运行时会验证 4H 至少 55 根连续历史、15m 与 4H K 线完整性和 `volCcyQuote` 报价成交额；确认后在下一根 15m 开盘估算市价入场，并管理 30%/30%/40% 三段目标、TP1 后保本、TP2 后跟踪止损、收盘失效和 16 根 15m 冷却。研究样本外结果为 **FAIL**（8 笔、37.5% 胜率、平均净 R +0.455），不能据此宣称策略已经盈利。

信号生成器读取已确认的 5 分钟或 15 分钟 OHLCV 文件，自动聚合到 15 分钟，输出最近或全部历史信号；它不会连接交易所，也不会提交订单：

```bash
python3 strategies/hlsr/src/hlsr_signal_generator.py \
  --input data/kline/okx/swap/5m/BEAT_USDT_SWAP_5m_20260331T065300Z_20260927T065300Z.jsonl.gz \
  --symbol BEAT-USDT-SWAP \
  --config strategies/hlsr/config/strategy.json
```

回测的 `--days` 至少为 180 天，以覆盖三个 60/30/30 天 walk-forward 折；`--end` 支持带时区的 ISO-8601 时间，也支持 `Z` 结尾。

## 双均线交易山寨币做多（已集成）

`ema_altcoin_long` 已接入 `StrategyType.emaAltcoinLong`，运行时在动态合规山寨币池中按 EMA20/60/120 突破回踩规则生成多头信号，使用 BTC EMA 门控、ATR 止损/止盈、0.5% 开放风险和单币种并发限制。该规则只允许 OKX 模拟盘策略下单；实验室规则与参数见 `strategies/ema_altcoin_long/`，运行时映射位于 `Sources/TradingService/StrategyEngine.swift`。

## 山寨币二次扫顶做空（已集成）

策略实验室中的 `sweep_reversal_short` 是当前规则的唯一来源：人类规则见 `strategies/sweep_reversal_short/STRATEGY.md`，机器参数见同目录 `config/strategy.json`。运行时策略依据该版本同步到 `Sources/`，不会读取研究目录。

这是一个 BTC 门控 + 12 天高点二次假突破的做空策略，已集成到策略引擎（`StrategyType.sweepReversalShort`）：

- 规则与集成说明：`strategies/sweep_reversal_short/STRATEGY.md`
- 参数配置：`strategies/sweep_reversal_short/config/strategy.json`
- 引擎实现：`Sources/TradingService/StrategyEngine.swift`（`evaluateWithConfirmation`，按实验室 v1.3 规则实现；信号携带 ATR 标定的止损/止盈价位）
- 执行边界：当前版本包含策略资金池 sizing、条件保护单、96 根时间离场和账户级 5% mark-to-market 熔断；熔断处置失败会写入 warning 并保持锁存。评估按已确认 K 线幂等，REST 刷新图表不会消耗冷却；状态按「策略 + 标的」隔离，1h 结构事件不会带出其它标的的信号
- 稳定策略标识：`sweepReversalShort`；UI 显示名称：`山寨币二次扫顶做空`
- 运行范围：后台每 30 秒刷新行情，排除主流币、稳定币及非加密资产后，按 24h 报价成交额动态扫描前 20 个 USDT 线性永续山寨币；177 个推荐标的只属于历史回测基线，不是固定运行名单
- 门控数据流：`PaperTradingStore` 缓存 BTC 1H K 线，取不晚于信号时刻的最近已确认 bar；历史不足 200 根或无法对齐时门控不通过
- UI：新建策略对话框可选"山寨币二次扫顶做空"规则
- 单元测试：`Tests/OKXGatewayTests/SweepReversalStrategyTests.swift`（含门控、假突破、15m 确认窗口边界、冷却幂等、跨标的信号隔离）
- 研究与回测：已上线规则口径为 177 个历史快照 54 个结构 → 41 笔、胜率 56.1%、盈亏比 1.53R、期望 +0.37R/笔（复现入口 `strategies/sweep_reversal_short/research/report_live_rule.py`）；不代表动态榜单未来绩效

策略实验室定稿后的安装、升级和卸载走配置包流程，不需要编辑 Swift 源码；只有修改内建运行时适配器时才需要同步代码。流程和 AI 指令见 [`spec/STRATEGY_PACKAGE.md`](spec/STRATEGY_PACKAGE.md)。

## 策略配置包

在实验室调优后生成并校验定稿包，安装到应用数据目录：

```bash
python3 scripts/strategy_package.py pack strategies/hlsr --output /tmp/hlsr-1.1.0.zip --version 1.1.0 --finalized
python3 scripts/strategy_package.py install /tmp/hlsr-1.1.0.zip
python3 scripts/strategy_package.py list --json
python3 scripts/strategy_package.py uninstall hlsr
```

本地服务也提供 `GET/POST/DELETE /api/v1/strategy-packages`，安装接口只接受状态目录下 `strategy-staging/` 的包目录；未知运行时处理器会安全保持暂停，不会影响已有账本。卸载前服务会检查运行实例、持仓并取消待成交挂单。

## 本机运行

环境要求：macOS 15+、Xcode 27+、Swift 6、Node.js 18+；运行 Python 回测脚本还需要 Python 3.9+。

```bash
swift test
./scripts/launch-app.sh
swift run okx-atk-cli BTC-USDT-SWAP
```

`launch-app.sh` 构建并打开 `.build/NovaTrade.app`，客户端在后台静默启动 `okx-locald`。实时链路回归检查：`python3 scripts/test_local_stream.py --live`，使用独立端口和临时状态目录，只订阅 K 线，不启用策略或下单。

## 导出 AI 行情数据

后台导出脚本会读取 OKX 当前全部存续的 USDT 线性永续合约，下载最近 180 天的 5 分钟 K 线，并写入 `data/kline/okx/swap/5m/manifest.json` 和每合约一个 `.jsonl.gz` 文件。公共行情接口不需要 API Key：

```bash
python3 scripts/export_market_data.py
```

`manifest.json` 记录时间范围、字段定义、每个合约的文件名、K 线数量和失败状态；每行行情包含 UTC 时间、毫秒时间戳、OHLC、成交量、报价成交量和 `confirmed`。脚本支持断点续跑，已经完成的文件会跳过；需要重新抓取时加 `--force`。已有全量清单需要更新到当前时间时使用 `--update`，它只抓取各合约最后一根附近到现在的增量行情并替换旧文件。可用 `--output` 指定目录，`--symbols BTC-USDT-SWAP ETH-USDT-SWAP` 只导出指定合约，`--workers` 控制并发数。

安装 ATK 后，`okx-atk-cli` 会通过系统 `PATH` 或 `/opt/homebrew/bin/okx`、`/usr/local/bin/okx` 查找 `okx`，也可使用 `OKX_CLI_PATH` 指定绝对路径。它是本项目的诊断入口；Swift 适配层使用同一 CLI 的 `--json` 机器输出。未安装或未配置 API Key 时，界面会显示明确错误；公共行情本身不要求 API Key。

## 后续迭代

1. 增加本地 `okx-locald` actor 服务，统一 ATK 调用、订单状态机和 append-only 审计。
2. 增加纸面交易、订单预览、风险拦截和人工确认；Swift 端禁止任意写命令，只允许显式白名单。
3. 增加 ATK CLI 的账户、持仓和订单 JSON 适配，处理超时、重连、部分失败和幂等。
4. 增加 iOS 与 Mac 的本地配对，只同步脱敏状态和告警。
5. 最后才开放实盘写操作，并要求独立小额子账户、无提币权限、日损熔断和保护单核验。

## 目录规范

行情数据、策略研究和源码导入的固定目录规则见 [`spec/DIRECTORY_STRUCTURE.md`](spec/DIRECTORY_STRUCTURE.md)。新增数据或策略文件前先按该规范选择目录。

官方参考：

- [OKX Agent Trade Kit](https://github.com/dex-original/okx-agent-trade-kit)
- [ATK 配置文档](https://github.com/dex-original/okx-agent-trade-kit/blob/master/docs/configuration.md)
- [ATK OAuth Skill](https://github.com/dex-original/okx-agent-trade-kit/blob/master/skills/okx-cex-auth/SKILL.md)
