# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 16:00:55 UTC / 2026-10-05 00:00:55 Asia/Shanghai；完成：2026-10-04 16:01:50 UTC / 2026-10-05 00:01:50 Asia/Shanghai。
- 账户：OKX demo / profile `okx-demo` / 标签 `模拟1` / `net_mode` / paper；本轮每个 ATK/OKX CLI 请求均显式 `--demo --profile okx-demo --json --env`，未写入或输出密钥。
- 账户与服务：可用 USDT 约 4,995.99；TradingService `killSwitch=False`；最终无 SWAP 持仓、普通挂单、OCO 或 conditional 保护单。
- UTC 当日已实际触发并完成的止损：**1 笔 / 3 笔**（CHZ，2026-10-04 09:39:55 UTC / 17:39:55 Asia/Shanghai）；ZRO 为止盈。未触发“当日止损上限”。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。** BTC 1h/4h 仍在 20SMA 上方（24 根约 0.43%/1.33%），ETH 1h/4h 也在上方（约 0.65%/0.57%），但 5m/15m K 线相对运行时约 108 秒延迟，且 BTC/ETH 短周期方向不一致；资金费 `fundingTime` 仍为过期时点、`nextFundingTime` 不可用，时序无法验证。BTC/ETH 当前 OI 约 $1830.67B/$879.68B。候选还未通过 K 线新鲜度、突破放量、价差/深度或资金费门槛。

重新允许开仓的条件：BTC 与 ETH 的 5m/15m/1h/4h 数据恢复可验证新鲜且方向一致，资金费时间戳可验证，同时候选满足成交额 ≥300 万 USDT、价差 ≤0.1%、200 USDT 深度滑点 ≤0.05%、5m 放量突破（≥1.5 倍）和净预期 R ≥2；否则继续只报告。

## UTC 当日涨幅榜前 10

涨幅按 `last / sodUtc0 - 1` 计算；ticker 时间约 2026-10-04 16:01:02 UTC / 2026-10-05 00:01:02 Asia/Shanghai；成交额为 24h USDT 名义成交额 `volCcy24h × last`。

| # | 合约 | UTC涨幅 | 24h成交额 | OI |
|---:|---|---:|---:|---:|
| 1 | SPK-USDT-SWAP | 207.33% | $212.49K | $64.68M |
| 2 | AXS-USDT-SWAP | 12.28% | $8.13M | $2.54M |
| 3 | IOTA-USDT-SWAP | 10.64% | $7.64M | $2.93M |
| 4 | MAGIC-USDT-SWAP | 10.08% | $544.70K | $1.45M |
| 5 | CHZ-USDT-SWAP | 6.30% | $3.72M | $58.97M |
| 6 | SUI-USDT-SWAP | 5.87% | $76.13M | $107.43M |
| 7 | ZETA-USDT-SWAP | 3.57% | $1.87M | $9.68M |
| 8 | ETHFI-USDT-SWAP | 3.30% | $1.05M | $2.99M |
| 9 | SAND-USDT-SWAP | 3.22% | $28.71M | $9.92M |
| 10 | ATOM-USDT-SWAP | 3.15% | $5.75M | $1.83M |

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；按 `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热。ticker/OI 时间约 16:01:02/16:01:03 UTC（00:01:02/00:01:03 Asia/Shanghai）。

| # | 合约 | UTC涨幅 | 24h成交额 | OI | 热度分 |
|---:|---|---:|---:|---:|---:|
| 1 | BTC-USDT-SWAP | 0.60% | $137.56B | $1830.67B | 1.0 |
| 2 | ETH-USDT-SWAP | 0.44% | $78.02B | $879.68B | 2.0 |
| 3 | NEAR-USDT-SWAP | -0.54% | $6.23B | $1.60B | 4.6 |
| 4 | SHIB-USDT-SWAP | 0.52% | $1.88B | $2.84B | 5.0 |
| 5 | SOL-USDT-SWAP | 1.71% | $1.23B | $2.01B | 6.0 |
| 6 | BNB-USDT-SWAP | 0.08% | $243.19M | $216.30M | 10.8 |
| 7 | DOGE-USDT-SWAP | 1.58% | $169.99M | $691.82M | 11.0 |
| 8 | PEPE-USDT-SWAP | 0.23% | $198.25M | $298.48M | 11.0 |
| 9 | KAITO-USDT-SWAP | -1.35% | $73.75M | $801.82M | 11.8 |
| 10 | ETC-USDT-SWAP | 0.09% | $174.36M | $128.78M | 13.6 |
| 11 | LTC-USDT-SWAP | 2.14% | $30.45M | $406.82M | 14.8 |
| 12 | SUI-USDT-SWAP | 5.87% | $76.13M | $107.43M | 15.2 |
| 13 | 1INCH-USDT-SWAP | -0.85% | $24.46M | $1.13B | 15.6 |
| 14 | AAVE-USDT-SWAP | -1.45% | $4.62B | $38.83M | 18.0 |
| 15 | THETA-USDT-SWAP | 0.44% | $20.50M | $178.89M | 19.0 |
| 16 | SKY-USDT-SWAP | -30.11% | $8.27M | $219.15B | 19.2 |
| 17 | ADA-USDT-SWAP | 1.81% | $36.80M | $48.23M | 21.8 |
| 18 | ALGO-USDT-SWAP | 1.30% | $295.87M | $18.98M | 23.8 |
| 19 | ICP-USDT-SWAP | 0.48% | $6.43M | $1.35B | 24.2 |
| 20 | CRO-USDT-SWAP | 2.92% | $16.18M | $66.55M | 24.4 |

## 去重候选池

SPK-USDT-SWAP, AXS-USDT-SWAP, IOTA-USDT-SWAP, MAGIC-USDT-SWAP, CHZ-USDT-SWAP, SUI-USDT-SWAP, ZETA-USDT-SWAP, ETHFI-USDT-SWAP, SAND-USDT-SWAP, ATOM-USDT-SWAP, BTC-USDT-SWAP, ETH-USDT-SWAP, NEAR-USDT-SWAP, SHIB-USDT-SWAP, SOL-USDT-SWAP, BNB-USDT-SWAP, DOGE-USDT-SWAP, PEPE-USDT-SWAP, KAITO-USDT-SWAP, ETC-USDT-SWAP, LTC-USDT-SWAP, 1INCH-USDT-SWAP, AAVE-USDT-SWAP, THETA-USDT-SWAP, SKY-USDT-SWAP, ADA-USDT-SWAP, ALGO-USDT-SWAP, ICP-USDT-SWAP, CRO-USDT-SWAP。

## 候选与交易决定

本轮最多 1 个开仓名额，但没有合格标的；未生成 clientOrderID，未提交订单。29 个候选全部至少触发一项硬拒绝：BTC/ETH 等核心合约短周期 K 线新鲜度不足；多数标的资金费时间戳不可验证；SPK 价差约 4.97% 且异常上涨，CHZ 价差约 0.118% 且 200 USDT 深度滑点约 0.10%/0.14%，IOTA/ATOM/1INCH/ICP 等深度滑点超过 0.05%，其余还存在成交额、方向一致性或放量突破不足。

结论：**本次不交易**。没有限价入场区间、触发条件、止盈/止损或预期 R，因为数据不完整且没有通过全部硬门槛。

## 监控状态

当前无本任务仓位、挂单或保护单，无需撤单/平仓；下一整点继续复核。已确认 CHZ OCO 止损记录仍为 1 笔；本轮 API/认证无错误，保护单无异常。
