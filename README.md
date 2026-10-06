# OKX Self Trader

面向 macOS 的本地 OKX 自助交易系统，前后端分离：SwiftUI 客户端 `mac-trader` 只通过回环地址访问 FastAPI 本地服务。行情请求由 Python 直接调用 OKX REST/WebSocket；MCP 只作为 AI 客户端入口。

## 架构

```text
mac-trader（SwiftUI）
      |  REST + WebSocket，127.0.0.1:8787
FastAPI backend（鉴权、行情、状态、WebSocket）

AI 客户端  --->  okx-trade-mcp（stdio，可只读或按模块启用）
```

FastAPI 只读取 `OKX_REST_URL`、`OKX_WS_URL` 等非敏感连接配置；API Key、Secret 和 Passphrase 不会写入应用状态目录。

## 私有接口配置

账户、持仓、挂单和下单接口使用 OKX v5 私有 REST 签名。先复制配置模板并填入凭据：

```bash
python3 -m pip install -r backend/requirements.txt
mkdir -p "$HOME/Library/Application Support/NovaTrade"
cp backend/.env.example "$HOME/Library/Application Support/NovaTrade/backend.env"
# 编辑 backend.env 后启动应用；FastAPI 会自动读取这个文件
```

`OKX_DEMO=1` 使用模拟盘并发送 `x-simulated-trading: 1`；实盘必须设为 `0`，并且在客户端中先启用实盘交易。API Key 只需要 `Read + Trade` 权限，禁止 `Withdraw`。

私有凭据只从进程环境读取，不会写入日志、JSON 状态或返回给客户端。未配置凭据时，FastAPI 返回未认证账户状态，订单接口返回明确错误。

## 当前版本

- macOS SwiftUI 交易工作台：支持永续合约搜索、自选收藏、合约切换、周期切换、行情指标和策略配置/启停。
- K 线使用 SwiftUI 原生 Canvas：绿涨红跌、连续拖拽、悬停十字线、日期/价格轴、成交量和 EMA。历史数据首次通过 API 预热；实时 K 柱仅使用 OKX V5 Business WSS 的 candle 频道，没有 K 线轮询或 ticker 拼接。WSS 不提供历史查询，因此首次历史加载仍需 API。
- FastAPI backend 负责本地 REST/WebSocket 请求的 token、Host/Origin 校验，并直接请求 OKX 公共 REST/WSS。历史 K 线和实时 K 线都由 Python 处理，客户端不再启动 Swift 本地服务。
- 策略和风控状态保存在应用支持目录的 JSON 文件中；可用 `GET /api/v1/strategies/targets?fresh=true` 读取当前缓存。
- 状态目录（`~/Library/Application Support/NovaTrade/`）的基础运行文件包括 `paper-state.json`（原子写入策略、状态、订单、成交和风控）与 `runtime-log.jsonl`（最近 1000 条运行日志）；启用 AI 后还会增加独立的 AI 配置、决策审计和订单 reservation 文件。
- 风控：日亏/回撤熔断按 UTC 日界滚动日初权益基线；模拟撮合支持部分平仓（保留剩余仓位）和同向加仓均价合并，已实现盈亏扣除手续费。
- 工作台通过 FastAPI 读取合约行情和账户状态；未配置私有凭据时显示未认证状态，不注入虚构余额或策略实例。
- UI 默认不提供下单按钮；实盘 HTTP 接口需要先手动启用交易，再经过 profile、订单参数和风控检查。
- OKX 永续订单的 `quantity` 始终表示合约张数（`sz`），下单前读取 `ctVal/ctMult/lotSz/minSz/tickSz`；策略按真实报价名义价值换算并向下取整，规格缺失或不符合时拒绝下单。
- AI 自主交易由 FastAPI 内的 `AI Decision Worker` 管理：Codex CLI 只读取脱敏行情快照并返回严格 JSON 交易意图，不能读取 OKX 凭据、执行 `okx` CLI 或直接下单。服务端订单网关统一负责风险、合约规格、幂等和未知提交恢复。
- AI 策略固定使用 `gpt-6-luna / medium`，候选信号或多周期冲突均使用此模型；旧模型路由配置会自动迁移，并保留观察池与风控设置。
- AI 观察标的始终来自固定合约白名单，不再按热门榜、涨幅榜或其他动态分类自动选币。用户可在参数设置中从全部 USDT 线性永续合约多选。
- AI 决策日志保留每轮独立的决策结果、执行状态和逐币明细；单币 `hold` 必须属于本次观察集合。`confidence` 是模型对当前动作的自评信心，99% 的 `hold` 表示继续观望，不会下单。每次决策的观察名单保存为状态的 `lastDecisionInstruments` 和审计行的 `instruments`；历史记录缺少名单时不会用当前池代替。
- AI 策略实例卡片只显示模式、运行状态和最近动作，保留启停与平仓操作，并通过运行日志图标打开决策日志。日志按决策时间倒序显示，每轮一条，时间位于左侧；不再提供聊天区或观察池、时效、技术记录等附加面板。每条记录的“查看明细表”固定显示该轮方向、胜率、盈亏比和点位，后续评估不会替换已打开的历史明细；参数设置仍可从日志窗口顶部打开。
- 每轮 AI 评估按固定观察池逐币显示完整合约 ID、多空方向、预计胜率、盈亏比、建议挂单价和不满足的开仓条件，`hold` 也保留整池明细。没有明确方向或可靠估计时显示观望及空值，并说明原因。生产响应要求本次每个观察合约恰好一条评估，遗漏、重复或池外合约会被拒绝；一次决策仍最多提交一个交易意图。建议挂单价是模型提出的入场点位，不表示订单已经提交。服务端拒绝开仓后，安全 `hold` 仍保留逐币评估，审计的 `rawDecision` 保存模型原始意图，便于区分模型建议和服务端执行结果；旧记录没有这些字段时仍可读取。
- AI 同时读取服务端计算的逐币行情摘要与完整原始快照。摘要只计算已确认 K 线的价格、涨跌幅、均价、近期高低点、真实波幅均值及盘口事实，缺数保持未知；方向、条件入场方案和预计胜率仍由 AI 判断。尚未达到开仓门槛的币也应保留可解释的方向、价位和质量估计，而不是用整个观察池的 `hold` 清空分析。
- Prompt 中字段一致的 K 线使用「列名 + 每行数值」无损编码，减少重复字段名；全部合约、周期、K 线和原始字段均保留，字段不一致的行仍用原对象表示。此编码只影响传输，不修改行情快照或交易规则。
- 观察池超过 4 个合约时，每批 4 币、最多 4 批并行分析，再由同一固定模型汇总一次总决策。逐币评估必须完整合并，分批建议不会直接下单；分析和汇总分别使用原有 CLI 超时时限，失败时取消其余进程。汇总失败会保留已经完成的整池评估，并显示系统观望及错误。开仓仍受原有快照、决策有效期与提交前时效检查约束。
- 快照时效由服务器 UTC 计算，`capturedAt` 是开始采集的时间，采集和 AI 推理均计入有效期。Prompt 明确提供当前时间与真实年龄，服务器记录时效核验结果；设置杠杆和发送开仓订单前还会重新检查快照与决策期限，过期指令不发出订单。最新未收盘 K 线保留实时行情用途，已收盘历史用于确认信号。
- AI 采集全部观察合约的四个周期 K 线（每周期最近 60 根）、ticker、资金费和五档盘口，限制并发和 K 线请求速率。限流与临时网络故障只做有限采集重试；缺数保留具体错误，未知账户数据不补零。审计记录采集完整性和 Prompt 大小；超过 1,000,000 字节的全池 Prompt 在启动 CLI 前明确拒绝。
- AI 开仓决策必须同时提供模型估计胜率和盈亏比：`winRate >= 0.45` 且 `riskRewardRatio >= 2.0` 才能进入下单链路；平仓和撤单不受这两个入场质量门槛阻断。两者是模型估计值，不等同于历史胜率或收益保证。
- AI 交易限制默认是每日最多开仓 20 单、每日最多 5 个亏损单、每笔保证金上限 500 USDT、最大杠杆 5x。每次开仓由 AI 在 `1x` 到配置上限之间选择实际杠杆，名义金额按「单笔保证金 × AI 杠杆」计算，并按实际限价向下取整，因此单笔实际保证金不会超过配置值。OKX `cross` 模式的杠杆按合约和保证金模式生效；同一合约已有持仓或挂单时，服务端禁止切换杠杆，避免交易所重算既有暴露的保证金。达到任一日限额后只拒绝新的 `open`，平仓和撤单仍可执行。每日统计按 UTC 日计算，未知的下单结果会先占用开仓额度，直到订单状态明确。
- 可交易性与下单规格来自认证账户的 `/account/instruments`，模拟盘只支持部分公开市场合约。每轮快照保留完整固定观察池，同时提供逐币 `tradingAvailability`：不可交易或无法核验的币仍有技术评估，但不能成为开仓对象；服务端和执行前再次检查，不切换交易环境。设置杠杆失败时尚未发送订单 POST，立即释放本次新预留；订单提交后结果未知的预留继续保留。
- AI 默认关闭，运行模式为 `shadow`、`demo-active` 或人工授权后的 `live-armed`；实盘开关不能由 Codex 修改。状态、决策审计和订单 reservation 位于应用支持目录的 `ai-config.json`、`ai-state.json`、`ai-decisions.jsonl` 和 `order-ledger.json`。
- AI 配置中心同时提供 `codex` 与 `deepseek` 两个独立策略；对应实验室规则分别见 [`strategies/codex_ai_decision/STRATEGY.md`](strategies/codex_ai_decision/STRATEGY.md) 和 [`strategies/deepseek_ai_decision/STRATEGY.md`](strategies/deepseek_ai_decision/STRATEGY.md)。目录文件只用于研究、版本和验收，运行时通过 `/api/v1/ai/strategies` 的 package/source/runtime/liveGate 元数据管理。

## 多周期行情缓存

策略训练建议先将 5 分钟原始行情转换为可复用的 Parquet 缓存。脚本会在一次读取每个 5m 文件的过程中，同时生成 15m、30m、1h 和 4h；源 manifest 未变化时会直接跳过，无需重复解压。

```bash
python3 -m pip install -r scripts/requirements-market-cache.txt
python3 scripts/build_kline_cache.py
```

缓存位于 `.cache/kline/okx/swap/`，属于可重建产物，不提交到仓库。原始数据仍保留在 `data/kline/okx/swap/5m/`。

## 山寨币二次扫顶（已集成）

策略实验室中的 `sweep_reversal_short` 是当前规则的唯一来源：人类规则见 `strategies/sweep_reversal_short/STRATEGY.md`，机器参数见同目录 `config/strategy.json`。运行时策略依据该版本同步到 `Sources/`，不会读取研究目录。

这是一个 BTC 门控 + 12 天高点二次假突破的做空策略，已集成到策略引擎（`StrategyType.sweepReversalShort`）：

- 规则与集成说明：`strategies/sweep_reversal_short/STRATEGY.md`
- 参数配置：`strategies/sweep_reversal_short/config/strategy.json`
- 引擎实现：`Sources/TradingService/StrategyEngine.swift`（`evaluateWithConfirmation`，按实验室 v1.4 规则实现；信号携带 ATR 标定的止损/止盈价位）
- 执行边界：当前版本包含全池名义仓位（资金池可用余额 × 1，止损距离 > 15% 不下单）、条件保护单、96 根时间离场和账户级固定 5% mark-to-market 日内熔断；新建或启动策略时，资金池占比必须满足「资金池占比 × 15% < 5%」，扫顶策略上限约为 33.33%。熔断处置失败会写入 warning 并保持锁存。评估按已确认 K 线幂等，REST 刷新图表不会消耗冷却；状态按「策略 + 标的」隔离，1h 结构事件不会带出其它标的的信号
- 稳定策略标识：`sweepReversalShort`；UI 显示名称：`山寨币二次扫顶`
- 运行范围：后台每 30 秒刷新行情，排除主流币、稳定币及非加密资产，剔除 24h 报价成交额低于 300 万 USDT 的合约后，按成交额动态扫描前 100 个 USDT 线性永续山寨币（`dynamic.sweepCandidates`）；177 个推荐标的只属于历史回测基线，不是固定运行名单
- 门控数据流：`PaperTradingStore` 缓存 BTC 1H K 线，取不晚于信号时刻的最近已确认 bar；历史不足 200 根或无法对齐时门控不通过
- UI：点击“添加”先从策略卡片选择规则，再配置 USDT 资金池和杠杆；内置策略默认 2 倍杠杆，可在 1–100 倍范围调整
- 单元测试：`Tests/OKXGatewayTests/SweepReversalStrategyTests.swift`（含门控、假突破、15m 确认窗口边界、冷却幂等、跨标的信号隔离）
- 研究与回测：已上线规则口径为 177 个历史快照 54 个结构 → 39 笔、胜率 56.4%、盈亏比 1.59R、期望 +0.41R/笔（含 15% 止损距离上限）；全历史生产范围 108 笔、单仓 54 笔，单仓池终值 2.720x、已实现最大回撤 43.49%。完整证据、盈利分布预测和真实 K 线点位见 [策略说明书](strategies/sweep_reversal_short/STRATEGY.md)，不代表未来绩效。

## 正式策略说明书

| 中文名称 | 全历史单仓复投 / 已实现最大回撤 | 说明书 |
| --- | --- | --- |
| 山寨币二次扫顶 | +172.0% / 43.5% | [信号、选币、盈利分布预测、三类真实 K 线和执行检查](strategies/sweep_reversal_short/STRATEGY.md) |

上述正收益为截至 2026-10-03 的历史证据。预测以策略资金池为分母，使用单仓成交序列；账户级每日 MTM 熔断和持仓内浮亏未在这些已实现资金池路径中完整模拟。策略名称只体现标的或信号，稳定 ID 和交易方向规则保持原值。

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

`build_and_run.sh` 把 `mac-trader` 和 FastAPI backend 打进同一个 `dist/NovaTrade.app`。客户端启动 Python 服务；退出客户端不会删除状态目录。依赖可用 `python3 -m pip install -r backend/requirements.txt` 安装。服务回归检查：`python3 scripts/test_fastapi_gateway.py`。

AI worker 的契约和本地测试：

```bash
python3 scripts/test_ai_gateway.py
python3 scripts/test_ai_snapshot.py
python3 scripts/test_ai_execution.py
python3 scripts/test_ai_market_facts.py
python3 scripts/test_ai_deepseek_profile.py
python3 -m unittest discover -s strategies/codex_ai_decision/tests -p 'test_*.py'
python3 -m unittest discover -s strategies/deepseek_ai_decision/tests -p 'test_*.py'
```

首次接入建议保持 `shadow`，确认 `GET /api/v1/ai/decisions` 的快照、决策和拒绝原因，再启用 OKX 模拟盘。Codex CLI 路径可通过 `NOVATRADE_CODEX_BIN` 配置；每次调用使用临时目录、`--sandbox read-only`、`--ephemeral` 和 `--output-schema`，超时或非法输出按 `hold` 处理。

DeepSeek 策略默认使用已安装的 `acp` profile，并通过 `backend/deepseek_profile.py` 的 overlay（`--profile acp --patch <overlay>`）去掉决策不会使用的工具、工作区上下文和会话持久化。厂商 `acp` profile 会随每次请求下发 24 个工具 schema、工作区指令、skill 目录和运行时样板，合计约 19 KB / 请求，而决策提示词明确禁止调用工具；overlay 不改动观察池、开仓闸门、提示词、schema、校验与下单路径。overlay 会以内容寻址文件名写入 `$DSH_HOME/novatrade-deepseek/`；优化 overlay 无法应用时自动使用只关闭工具与持久化的 safety overlay，只有显式设置 `NOVATRADE_DEEPSEEK_PROFILE=0` 才允许使用原厂 profile。`NOVATRADE_DEEPSEEK_ENCODING=raw|compact20|compact10` 可实验性调整模型收到的 K 线窗口，默认 `compact60`，本地指标仍使用完整采集数据；`ai-decisions.jsonl` 中的 `analysis-workflow.profile` 字段记录本次实际生效的 profile。

事件预筛默认关闭。设置 `NOVATRADE_AI_EVENT_MODE=shadow` 可只记录潜在跳过、不改变模型调用；设置 `NOVATRADE_AI_EVENT_DRIVEN=1` 或 `NOVATRADE_AI_EVENT_MODE=on` 后，首次运行、确认 K 线或关键行情分箱变化、资金/账户/风险/可交易性/路由变化，以及持仓或挂单管理状态都会调用模型；指纹不变时生成服务端安全 hold 并跳过模型，最长连续跳过时间由 `NOVATRADE_AI_EVENT_MAX_SKIP_SECONDS` 控制（默认 900 秒）。模型失败、网关失败、账户或挂单数据未知时不会推进成功指纹。该开关只减少 DeepSeek/Codex 请求和模型 token，当前 REST 行情采集仍按原轮询周期执行。

## 导出 AI 行情数据

后台导出脚本会读取 OKX 当前全部存续的 USDT 线性永续合约，下载最近 180 天的 5 分钟 K 线，并写入 `data/kline/okx/swap/5m/manifest.json` 和每合约一个 `.jsonl.gz` 文件。公共行情接口不需要 API Key：

```bash
python3 scripts/export_market_data.py
```

`manifest.json` 记录时间范围、字段定义、每个合约的文件名、K 线数量和失败状态；每行行情包含 UTC 时间、毫秒时间戳、OHLC、成交量、报价成交量和 `confirmed`。脚本支持断点续跑，已经完成的文件会跳过；需要重新抓取时加 `--force`。已有全量清单需要更新到当前时间时使用 `--update`，它只抓取各合约最后一根附近到现在的增量行情并替换旧文件。可用 `--output` 指定目录，`--symbols BTC-USDT-SWAP ETH-USDT-SWAP` 只导出指定合约，`--workers` 控制并发数。

需要补齐超过 REST 分页效率可接受的历史区间时，使用官方月度 1 分钟 ZIP 并在本地聚合为 5 分钟，再由 REST 补齐当前月份。例如更新最近约一年半：

```bash
python3 scripts/import_okx_bulk_data.py \
  --start 2025-04-03T00:00:00Z \
  --end 2026-10-03T08:01:01Z \
  --output data/kline/okx/swap/5m \
  --workers 8
```

批量导入器会生成相同的 `manifest.json` 和 JSONL 字段；较晚上线的合约只包含上市以来的数据。导入行情后使用 `python3 scripts/build_kline_cache.py --force` 重建多周期缓存。

公共行情接口不需要 API Key。账户和下单接口需要在 Python backend 中补充 OKX 私有 REST 凭据后才会启用；未配置时服务返回未认证账户状态，不生成虚构余额或订单。

## 目录规范

行情数据、策略研究和源码导入的固定目录规则见 [`spec/DIRECTORY_STRUCTURE.md`](spec/DIRECTORY_STRUCTURE.md)。新增数据或策略文件前先按该规范选择目录。

官方参考：

- [OKX Agent Trade Kit](https://github.com/dex-original/okx-agent-trade-kit)
- [ATK 配置文档](https://github.com/dex-original/okx-agent-trade-kit/blob/master/docs/configuration.md)
- [ATK OAuth Skill](https://github.com/dex-original/okx-agent-trade-kit/blob/master/skills/okx-cex-auth/SKILL.md)
