# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 17:02:27 UTC / 2026-10-05 01:02:27 Asia/Shanghai；完成：2026-10-04 17:03:23 UTC / 2026-10-05 01:03:23 Asia/Shanghai。
- 账户：OKX demo / profile `okx-demo` / 标签 `模拟1` / `net_mode` / paper；本轮所有 ATK/OKX CLI 请求显式 `--demo --profile okx-demo --json --env`，未写入或输出密钥。
- 账户与服务：可用 USDT 约 4,995.99；TradingService `killSwitch=False`；最终无 SWAP 持仓、普通挂单、OCO 或 conditional 保护单。
- UTC 当日已实际触发并完成的止损：**1 笔 / 3 笔**（CHZ，2026-10-04 09:39:55 UTC / 2026-10-04 17:39:55 Asia/Shanghai）；ZRO 为止盈。未触发“当日止损上限”。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。** BTC 与 ETH 的 1h 仍在 20SMA 上方（BTC 24 根约 0.61%，ETH 0.82%），但短周期及 4h K 线延迟（BTC 5m 200s、BTC 15m 200s、BTC 4h 3800s、ETH 5m 200s、ETH 15m 200s、ETH 4h 3800s），且当前没有可验证的新鲜资金费结算时间（BTC/ETH 资金费约 BTC -0.0451% / ETH 0.0011%，`fundingTime` 过期、`nextFundingTime` 不可用）。BTC/ETH OI 约 $1834.09B / $880.73B。
失效/重新允许开仓条件：BTC 与 ETH 的 5m/15m/1h/4h 数据恢复可验证新鲜且方向一致，资金费时间戳可验证，同时候选满足成交额 ≥300 万 USDT、价差 ≤0.1%、200 USDT 深度滑点 ≤0.05%、放量突破和净预期 R ≥2。

## UTC 当日涨幅榜前 10

涨幅按 `last / sodUtc0 - 1`；ticker 时间约 2026-10-04 17:02:35 UTC / 2026-10-05 01:02:35 Asia/Shanghai；成交额为 24h USDT 名义成交额 `volCcy24h × last`。

| # | 合约 | UTC涨幅 | 24h成交额 | OI |
|---:|---|---:|---:|---:|
| 1 | SPK-USDT-SWAP | 205.99% | $234.72K | $64.40M |
| 2 | AXS-USDT-SWAP | 12.58% | $8.16M | $2.56M |
| 3 | IOTA-USDT-SWAP | 11.03% | $7.72M | $2.88M |
| 4 | MAGIC-USDT-SWAP | 7.28% | $547.55K | $1.41M |
| 5 | CHZ-USDT-SWAP | 6.55% | $3.73M | $59.11M |
| 6 | SUI-USDT-SWAP | 3.36% | $95.87M | $98.37M |
| 7 | GMT-USDT-SWAP | 3.20% | $11.38K | $6.45M |
| 8 | ATOM-USDT-SWAP | 3.09% | $5.63M | $1.83M |
| 9 | ETHFI-USDT-SWAP | 2.99% | $1.12M | $2.98M |
| 10 | NEAR-USDT-SWAP | 2.78% | $6.72B | $1.65B |

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；按 `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热。时间约 2026-10-04 17:02:35 UTC / 2026-10-05 01:02:35 Asia/Shanghai（ticker）及各 OI `oiTs`。

| # | 合约 | UTC涨幅 | 24h成交额 | OI | 热度分 |
|---:|---|---:|---:|---:|---:|
| 1 | BTC-USDT-SWAP | 0.73% | $137.85B | $1834.09B | 1.0 |
| 2 | ETH-USDT-SWAP | 0.55% | $78.41B | $880.73B | 2.0 |
| 3 | NEAR-USDT-SWAP | 2.78% | $6.72B | $1.65B | 4.6 |
| 4 | SHIB-USDT-SWAP | 0.00% | $1.95B | $2.83B | 5.0 |
| 5 | SOL-USDT-SWAP | 1.80% | $1.25B | $2.01B | 6.0 |
| 6 | DOGE-USDT-SWAP | 1.93% | $171.85M | $694.42M | 10.4 |
| 7 | BNB-USDT-SWAP | 0.25% | $241.00M | $216.68M | 10.8 |
| 8 | KAITO-USDT-SWAP | -1.64% | $73.45M | $799.45M | 11.8 |
| 9 | PEPE-USDT-SWAP | 0.00% | $151.17M | $297.78M | 12.2 |
| 10 | ETC-USDT-SWAP | 0.27% | $174.64M | $129.02M | 13.0 |
| 11 | LTC-USDT-SWAP | 1.23% | $30.20M | $403.24M | 14.8 |
| 12 | 1INCH-USDT-SWAP | -0.66% | $24.50M | $1.14B | 15.0 |
| 13 | SUI-USDT-SWAP | 3.36% | $95.87M | $98.37M | 16.0 |
| 14 | AAVE-USDT-SWAP | -1.05% | $4.41B | $37.77M | 18.0 |
| 15 | THETA-USDT-SWAP | 0.62% | $20.57M | $179.20M | 18.4 |
| 16 | SKY-USDT-SWAP | -30.11% | $8.27M | $219.15B | 19.2 |
| 17 | ADA-USDT-SWAP | 1.72% | $36.85M | $48.22M | 22.2 |
| 18 | ICP-USDT-SWAP | 0.09% | $6.45M | $1.34B | 24.2 |
| 19 | CRO-USDT-SWAP | 2.46% | $16.10M | $66.24M | 24.4 |
| 20 | TRX-USDT-SWAP | -0.03% | $46.29M | $35.23M | 24.4 |

## 去重候选池

SPK-USDT-SWAP、AXS-USDT-SWAP、IOTA-USDT-SWAP、MAGIC-USDT-SWAP、CHZ-USDT-SWAP、SUI-USDT-SWAP、GMT-USDT-SWAP、ATOM-USDT-SWAP、ETHFI-USDT-SWAP、NEAR-USDT-SWAP、BTC-USDT-SWAP、ETH-USDT-SWAP、SHIB-USDT-SWAP、SOL-USDT-SWAP、DOGE-USDT-SWAP、BNB-USDT-SWAP、KAITO-USDT-SWAP、PEPE-USDT-SWAP、ETC-USDT-SWAP、LTC-USDT-SWAP、1INCH-USDT-SWAP、AAVE-USDT-SWAP、THETA-USDT-SWAP、SKY-USDT-SWAP、ADA-USDT-SWAP、ICP-USDT-SWAP、CRO-USDT-SWAP、TRX-USDT-SWAP。

## 候选与交易决定

本轮最多 1 个开仓名额，但 28 个候选均至少触发一项硬拒绝；代表性原因：SPK-USDT-SWAP（资金费结算时间不可验证、24h成交额<300万、盘口价差>0.1%或缺失）；AXS-USDT-SWAP（资金费结算时间不可验证、K线缺失/延迟）；IOTA-USDT-SWAP（资金费结算时间不可验证、200USDT深度滑点>0.05%或不足、K线缺失/延迟）；MAGIC-USDT-SWAP（资金费结算时间不可验证、24h成交额<300万、200USDT深度滑点>0.05%或不足）；CHZ-USDT-SWAP（200USDT深度滑点>0.05%或不足、K线缺失/延迟）；SUI-USDT-SWAP（资金费结算时间不可验证、K线缺失/延迟）。

**本次不交易。** 未生成 clientOrderID，未提交限价单；因此无入场区间、触发条件、止盈/止损或预期 R。

## 监控状态

当前无本任务仓位、挂单或保护单，无需撤单/平仓；下一整点继续复核。保护单无异常，本轮 API/认证无错误；数据新鲜度与资金费时间戳异常需在下轮恢复后才可重新评估开仓。
