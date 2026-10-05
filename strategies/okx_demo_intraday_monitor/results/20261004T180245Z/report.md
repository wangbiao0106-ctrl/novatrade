# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 18:02:45 UTC / 2026-10-05 02:02:45 Asia/Shanghai；完成：2026-10-04 18:03:37 UTC / 2026-10-05 02:03:37 Asia/Shanghai。
- 账户：OKX demo / profile `okx-demo` / 标签 `模拟1` / `net_mode` / paper；本轮 OKX CLI 请求均显式 `--demo --profile okx-demo --json --env`，未输出密钥。
- 账户可用 USDT：约 4,995.99；TradingService `killSwitch=False`；最终持仓、普通挂单、OCO、conditional：均为空。
- UTC 当日已实际触发并完成止损：**1 / 3 笔**；唯一证据为 CHZ OCO `3979314716284137482` 于 2026-10-04 09:39:55 UTC / 17:39:55 Asia/Shanghai 触发 SL，关闭单已 filled。未触发上限。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。**

- BTC 5m 上/20SMA、15m 上、1h 上、4h 上；24根变动 0.43%
- ETH 5m 下/20SMA、15m 上、1h 上、4h 上；24根变动 0.52%。
- UTC 日内广度（±0.05%）：45 上涨 / 44 下跌 / 14 横盘，市场未形成一致方向。
- 资金费当前返回 BTC -0.0204%、ETH 0.0043%，但 `fundingTime` 为旧时间且 `nextFundingTime` 已过，结算新鲜度不可验证；OI 约 BTC $1832.93B、ETH $880.55B。
- 失效/恢复条件：BTC、ETH 的 5m/15m/1h/4h 均恢复可验证新鲜且方向一致，资金费时间戳有效，同时候选通过成交额 ≥300 万 USDT、价差 ≤0.1%、200 USDT 深度滑点 ≤0.05%、放量突破和净预期 R ≥2。

## UTC 当日涨幅榜前 10

口径：`last / sodUtc0 - 1`；ticker 时间约 2026-10-04 18:02:35 UTC / 2026-10-05 02:02:35 Asia/Shanghai；成交额为 24h `volCcy24h × last`。

| # | 合约 | UTC涨幅 | 24h成交额 | OI |
|---:|---|---:|---:|---:|
| 1 | SPK-USDT-SWAP | 177.58% | $213.52K | $58.42M |
| 2 | AXS-USDT-SWAP | 13.10% | $8.19M | $2.57M |
| 3 | IOTA-USDT-SWAP | 9.32% | $7.63M | $2.82M |
| 4 | CHZ-USDT-SWAP | 6.42% | $3.73M | $59.04M |
| 5 | MAGIC-USDT-SWAP | 6.13% | $550.91K | $1.40M |
| 6 | GMT-USDT-SWAP | 3.20% | $11.40K | $6.45M |
| 7 | SUI-USDT-SWAP | 3.08% | $98.74M | $97.99M |
| 8 | ZETA-USDT-SWAP | 2.88% | $1.79M | $9.62M |
| 9 | DOGE-USDT-SWAP | 2.82% | $199.15M | $700.28M |
| 10 | NEAR-USDT-SWAP | 2.80% | $7.09B | $1.65B |

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；热度分 = `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热；数据时间约 18:02:35 UTC / 02:02:35 Asia/Shanghai。

| # | 合约 | UTC涨幅 | 24h成交额 | OI | 热度分 |
|---:|---|---:|---:|---:|---:|
| 1 | BTC-USDT-SWAP | 0.68% | $137.64B | $1832.93B | 1.0 |
| 2 | ETH-USDT-SWAP | 0.51% | $78.39B | $880.55B | 2.0 |
| 3 | NEAR-USDT-SWAP | 2.80% | $7.09B | $1.65B | 4.6 |
| 4 | SHIB-USDT-SWAP | 0.17% | $1.90B | $2.82B | 5.0 |
| 5 | SOL-USDT-SWAP | 1.56% | $1.24B | $2.01B | 6.0 |
| 6 | DOGE-USDT-SWAP | 2.82% | $199.15M | $700.28M | 9.2 |
| 7 | PEPE-USDT-SWAP | 0.23% | $151.85M | $298.39M | 11.6 |
| 8 | KAITO-USDT-SWAP | -1.21% | $73.33M | $802.98M | 11.8 |
| 9 | BNB-USDT-SWAP | 0.20% | $130.57M | $216.70M | 12.6 |
| 10 | ETC-USDT-SWAP | 0.14% | $174.34M | $128.84M | 13.0 |
| 11 | AAVE-USDT-SWAP | -1.07% | $4.23B | $53.55M | 14.4 |
| 12 | LTC-USDT-SWAP | 0.99% | $31.09M | $402.25M | 14.8 |
| 13 | 1INCH-USDT-SWAP | -0.76% | $24.48M | $1.13B | 15.0 |
| 14 | SUI-USDT-SWAP | 3.08% | $98.74M | $97.99M | 16.0 |
| 15 | THETA-USDT-SWAP | 0.44% | $20.53M | $178.89M | 18.4 |
| 16 | SKY-USDT-SWAP | -30.11% | $8.27M | $219.15B | 19.2 |
| 17 | ADA-USDT-SWAP | 1.56% | $36.94M | $48.15M | 22.6 |
| 18 | ICP-USDT-SWAP | -0.15% | $6.48M | $1.34B | 24.2 |
| 19 | CRO-USDT-SWAP | 1.87% | $16.03M | $65.86M | 24.4 |
| 20 | CAT-USDT-SWAP | -1.23% | $10.45M | $143.66M | 24.6 |

## 去重候选池

SPK-USDT-SWAP, AXS-USDT-SWAP, IOTA-USDT-SWAP, CHZ-USDT-SWAP, MAGIC-USDT-SWAP, GMT-USDT-SWAP, SUI-USDT-SWAP, ZETA-USDT-SWAP, DOGE-USDT-SWAP, NEAR-USDT-SWAP, BTC-USDT-SWAP, ETH-USDT-SWAP, SHIB-USDT-SWAP, SOL-USDT-SWAP, PEPE-USDT-SWAP, KAITO-USDT-SWAP, BNB-USDT-SWAP, ETC-USDT-SWAP, AAVE-USDT-SWAP, LTC-USDT-SWAP, 1INCH-USDT-SWAP, THETA-USDT-SWAP, SKY-USDT-SWAP, ADA-USDT-SWAP, ICP-USDT-SWAP, CRO-USDT-SWAP, CAT-USDT-SWAP

## 候选与交易决定

候选逐一检查 5m/15m/1h 结构、盘口价差、200 USDT 深度滑点、成交额、资金费时间戳和突破量。代表性硬拒绝：SPK 资金费不可验证、成交额不足且价差/深度不合格；IOTA 深度与 K 线门槛不合格；BTC/ETH/NEAR/SOL 等主流候选资金费不可验证且 K 线新鲜度门槛未通过。没有可审计的同向突破回撤与净 R≥2 信号。

**本次不交易。** 未生成 clientOrderID，未提交限价单；因此无入场、止盈、止损或预期 R。

## 监控状态

已通过 `okx-locald` 本地 WebSocket 鉴权并收到 BTC-USDT-SWAP 1m OKX WSS candle 帧（18:04:00 UTC，`confirmed=false`）；当前无仓位、挂单或保护单，无需撤单/平仓。无 API/认证异常。下一整点继续监控；资金费时间戳和候选 K 线新鲜度恢复前保持不开仓。
