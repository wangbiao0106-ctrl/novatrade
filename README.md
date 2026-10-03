# OKX Self Trader

面向 macOS 的本地 OKX 自助交易系统，前后端分离：SwiftUI 客户端 `mac-trader` 只通过回环地址访问本地服务 `okx-locald`。当前版本以 OKX Agent Trade Kit（ATK）为连接层：Swift 应用使用 ATK 的 API Key profile，通过 `okx --json` 获取结构化数据；MCP 只作为 AI 客户端入口。

## 架构

```text
mac-trader（SwiftUI）
      |  REST + WebSocket，127.0.0.1:8787
okx-locald（Hummingbird：策略、风控、StreamHub）
      |                         |
ATKGateway（Process 适配层）   OKXCandleSocket（OKX Business WSS 实时 K 线）
      |
okx --json  <->  ~/.okx/config.toml API Key profile

AI 客户端  --->  okx-trade-mcp（stdio，可只读或按模块启用）
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
- 策略扫描池按策略类型独立解析并缓存；K 线评估和 StreamHub 订阅共用同一份解析结果，避免每根 K 线重复筛选全量合约。可用 `GET /api/v1/strategies/targets?fresh=true` 查看后台实际使用的 instrument ID、策略专属 universe 和刷新时间。
- 本地服务对 ATK CLI 调用带 TTL 缓存和单飞去重（ticker 2s、账户/持仓/挂单 5s、合约列表与历史预热 60s），多个客户端并发轮询时不再重复拉起 CLI 进程；下单成功后立即失效账户缓存。
- 状态目录（`~/Library/Application Support/NovaTrade/`）只有两份运行时文件：`paper-state.json` 原子写入策略、状态、订单、成交和风控；`runtime-log.jsonl` 保存最近 1000 条运行日志，服务启动时自动回读并压缩。
- 风控：日亏/回撤熔断按 UTC 日界滚动日初权益基线；模拟撮合支持部分平仓（保留剩余仓位）和同向加仓均价合并，已实现盈亏扣除手续费。
- 工作台通过本地服务读取合约行情和 API Key profile 状态；未配置或暂时无法连接 ATK 时显示等待状态，不注入虚构行情或策略实例。
- `ATKGateway`：使用 `Process` 调用 CLI，不经过 shell；支持 JSON 解码、退出码和测试替身。
- UI 默认不提供下单按钮；实盘 HTTP 接口需要先手动启用交易，再经过 profile、订单参数和风控检查。
- OKX 永续订单的 `quantity` 始终表示合约张数（`sz`），下单前读取 `ctVal/ctMult/lotSz/minSz/tickSz`；策略按真实报价名义价值换算并向下取整，规格缺失或不符合时拒绝下单。

## 多周期行情缓存

策略训练建议先将 5 分钟原始行情转换为可复用的 Parquet 缓存。脚本会在一次读取每个 5m 文件的过程中，同时生成 15m、30m、1h 和 4h；源 manifest 未变化时会直接跳过，无需重复解压。

```bash
python3 -m pip install -r scripts/requirements-market-cache.txt
python3 scripts/build_kline_cache.py
```

缓存位于 `.cache/kline/okx/swap/`，属于可重建产物，不提交到仓库。原始数据仍保留在 `data/kline/okx/swap/5m/`。

## 山寨币二次扫顶做空（已集成）

策略实验室中的 `sweep_reversal_short` 是当前规则的唯一来源：人类规则见 `strategies/sweep_reversal_short/STRATEGY.md`，机器参数见同目录 `config/strategy.json`。运行时策略依据该版本同步到 `Sources/`，不会读取研究目录。

这是一个 BTC 门控 + 12 天高点二次假突破的做空策略，已集成到策略引擎（`StrategyType.sweepReversalShort`）：

- 规则与集成说明：`strategies/sweep_reversal_short/STRATEGY.md`
- 参数配置：`strategies/sweep_reversal_short/config/strategy.json`
- 引擎实现：`Sources/TradingService/StrategyEngine.swift`（`evaluateWithConfirmation`，按实验室 v1.4 规则实现；信号携带 ATR 标定的止损/止盈价位）
- 执行边界：当前版本包含策略资金池 sizing、条件保护单、96 根时间离场和账户级 5% mark-to-market 熔断；熔断处置失败会写入 warning 并保持锁存。评估按已确认 K 线幂等，REST 刷新图表不会消耗冷却；状态按「策略 + 标的」隔离，1h 结构事件不会带出其它标的的信号
- 稳定策略标识：`sweepReversalShort`；UI 显示名称：`山寨币二次扫顶做空`
- 运行范围：后台每 30 秒刷新行情，排除主流币、稳定币及非加密资产，剔除 24h 报价成交额低于 300 万 USDT 的合约后，按成交额动态扫描前 100 个 USDT 线性永续山寨币（`dynamic.sweepCandidates`）；177 个推荐标的只属于历史回测基线，不是固定运行名单
- 门控数据流：`PaperTradingStore` 缓存 BTC 1H K 线，取不晚于信号时刻的最近已确认 bar；历史不足 200 根或无法对齐时门控不通过
- UI：点击“添加”先从策略卡片选择规则，再配置 USDT 资金池和杠杆；内置策略默认 2 倍杠杆，可在 1–100 倍范围调整
- 单元测试：`Tests/OKXGatewayTests/SweepReversalStrategyTests.swift`（含门控、假突破、15m 确认窗口边界、冷却幂等、跨标的信号隔离）
- 研究与回测：已上线规则口径为 177 个历史快照 54 个结构 → 41 笔、胜率 56.1%、盈亏比 1.53R、期望 +0.37R/笔（复现入口 `strategies/sweep_reversal_short/research/report_live_rule.py`）；不代表动态榜单未来绩效

策略实验室定稿后的安装、升级和卸载走配置包流程，不需要编辑 Swift 源码；只有修改内建运行时适配器时才需要同步代码。流程和 AI 指令见 [`spec/STRATEGY_PACKAGE.md`](spec/STRATEGY_PACKAGE.md)。

## 策略配置包

在实验室调优后生成并校验定稿包，安装到应用数据目录：

```bash
python3 scripts/strategy_package.py pack strategies/sweep_reversal_short --output /tmp/sweep-reversal-short-1.4.0.zip --version 1.4.0 --finalized
python3 scripts/strategy_package.py install /tmp/sweep-reversal-short-1.4.0.zip
python3 scripts/strategy_package.py list --json
python3 scripts/strategy_package.py uninstall sweep_reversal_short
```

本地服务也提供 `GET/POST/DELETE /api/v1/strategy-packages`，安装接口只接受状态目录下 `strategy-staging/` 的包目录；未知运行时处理器会安全保持暂停，不会影响已有账本。卸载前服务会检查运行实例、持仓并取消待成交挂单。

## 本机运行

环境要求：macOS 15+、Xcode 27+、Swift 6、Node.js 18+；运行 Python 回测脚本还需要 Python 3.9+。

```bash
swift test
./scripts/build_and_run.sh            # 构建 dist/NovaTrade.app 并打开
./scripts/build_and_run.sh package    # 额外生成 dist/NovaTrade.dmg
```

`build_and_run.sh` 把 `mac-trader` 和 `okx-locald` 打进同一个 `dist/NovaTrade.app`，客户端在后台以 `nohup` 静默启动同目录的 `okx-locald`，退出客户端不会中断运行中的策略。实时链路回归检查：`python3 scripts/test_local_stream.py --live`，使用独立端口和临时状态目录，只订阅 K 线，不启用策略或下单。

## 导出 AI 行情数据

后台导出脚本会读取 OKX 当前全部存续的 USDT 线性永续合约，下载最近 180 天的 5 分钟 K 线，并写入 `data/kline/okx/swap/5m/manifest.json` 和每合约一个 `.jsonl.gz` 文件。公共行情接口不需要 API Key：

```bash
python3 scripts/export_market_data.py
```

`manifest.json` 记录时间范围、字段定义、每个合约的文件名、K 线数量和失败状态；每行行情包含 UTC 时间、毫秒时间戳、OHLC、成交量、报价成交量和 `confirmed`。脚本支持断点续跑，已经完成的文件会跳过；需要重新抓取时加 `--force`。已有全量清单需要更新到当前时间时使用 `--update`，它只抓取各合约最后一根附近到现在的增量行情并替换旧文件。可用 `--output` 指定目录，`--symbols BTC-USDT-SWAP ETH-USDT-SWAP` 只导出指定合约，`--workers` 控制并发数。

安装 ATK 后，`okx-locald` 会通过系统 `PATH` 或 `/opt/homebrew/bin/okx`、`/usr/local/bin/okx` 查找 `okx`，也可使用 `OKX_CLI_PATH` 指定绝对路径。诊断时直接运行 `okx` 命令；Swift 适配层使用同一 CLI 的 `--json` 机器输出。未安装或未配置 API Key 时，界面会显示明确错误；公共行情本身不要求 API Key。

## 目录规范

行情数据、策略研究和源码导入的固定目录规则见 [`spec/DIRECTORY_STRUCTURE.md`](spec/DIRECTORY_STRUCTURE.md)。新增数据或策略文件前先按该规范选择目录。

官方参考：

- [OKX Agent Trade Kit](https://github.com/dex-original/okx-agent-trade-kit)
- [ATK 配置文档](https://github.com/dex-original/okx-agent-trade-kit/blob/master/docs/configuration.md)
- [ATK OAuth Skill](https://github.com/dex-original/okx-agent-trade-kit/blob/master/skills/okx-cex-auth/SKILL.md)
