# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 19:01:22 UTC / 2026-10-05 03:01:22 Asia/Shanghai；完成：2026-10-04 19:02:13 UTC / 2026-10-05 03:02:13 Asia/Shanghai。所有 OKX CLI 请求均显式 `--demo --profile okx-demo --json --env`，未输出密钥。
- 账户：OKX demo / profile `okx-demo` / 标签 `模拟1` / `net_mode` / paper；可用 USDT 约 4995.989685443095；TradingService `killSwitch=False`。
- 最终状态：SWAP 持仓 0、普通挂单 0、OCO 0、conditional 0。
- UTC 当日已实际触发并完成止损：**1 / 3 笔**；止损上限未触发。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。**

- UTC 日内广度（±0.05%）：49 上涨 / 38 下跌 / 16 横盘；涨跌没有形成足够一致的方向。
- BTC-USDT-SWAP: 5m 下/20SMA，-0.05%, 15m 上/20SMA，+0.24%, 1h 上/20SMA，+0.47%, 4h 上/20SMA，+1.33%；OI $1832.97B；资金费 -0.0051%。
- ETH-USDT-SWAP: 5m 下/20SMA，-0.07%, 15m 上/20SMA，+0.22%, 1h 上/20SMA，+0.60%, 4h 上/20SMA，+0.57%；OI $880.13B；资金费 +0.0048%。
- BTC/ETH 的 5m、15m K 线约 131 秒未更新，4h K 线约 3.0 小时未更新；资金费 `fundingTime` 为旧时间且 `nextFundingTime` 已过，资金费新鲜度不可验证。
- 失效/恢复条件：BTC、ETH 的 5m/15m/1h/4h 恢复可验证新鲜且方向一致，资金费时间戳有效，同时候选通过成交额、价差、深度、放量突破和净预期 R≥2 门槛。

## UTC 当日涨幅榜前 10

口径：`last / sodUtc0 - 1`；行情时间约 2026-10-04 19:01:28 UTC / 2026-10-05 03:01:28 Asia/Shanghai；成交额为 24h `volCcy24h × last`。

| # | 合约 | UTC涨幅 | 24h成交额 | OI |
|---:|---|---:|---:|---:|
| 1 | SPK-USDT-SWAP | +99.89% | $163.69K | $42.07M |
| 2 | AXS-USDT-SWAP | +13.41% | $8.22M | $2.58M |
| 3 | IOTA-USDT-SWAP | +9.57% | $7.67M | $2.80M |
| 4 | CHZ-USDT-SWAP | +6.93% | $3.75M | $59.32M |
| 5 | MAGIC-USDT-SWAP | +6.27% | $558.93K | $1.40M |
| 6 | GMT-USDT-SWAP | +4.11% | $11.63K | $6.51M |
| 7 | NEAR-USDT-SWAP | +3.53% | $7.24B | $1.66B |
| 8 | SUI-USDT-SWAP | +3.29% | $102.38M | $98.53M |
| 9 | ATOM-USDT-SWAP | +3.09% | $5.79M | $1.83M |
| 10 | ZETA-USDT-SWAP | +2.88% | $1.68M | $9.62M |

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；热度分 = `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热；ticker/OI 时间约 2026-10-04 19:01:28 UTC / 2026-10-05 03:01:28 Asia/Shanghai。

| # | 合约 | UTC涨幅 | 24h成交额 | OI | 热度分 |
|---:|---|---:|---:|---:|---:|
| 1 | BTC-USDT-SWAP | +0.68% | $137.55B | $1832.97B | 1.0 |
| 2 | ETH-USDT-SWAP | +0.49% | $78.35B | $880.13B | 2.0 |
| 3 | NEAR-USDT-SWAP | +3.53% | $7.24B | $1.66B | 4.6 |
| 4 | SHIB-USDT-SWAP | +0.70% | $1.90B | $2.83B | 5.0 |
| 5 | SOL-USDT-SWAP | +1.59% | $1.18B | $2.00B | 6.0 |
| 6 | DOGE-USDT-SWAP | +2.46% | $201.48M | $699.19M | 9.2 |
| 7 | PEPE-USDT-SWAP | +0.23% | $152.41M | $298.36M | 11.0 |
| 8 | KAITO-USDT-SWAP | +0.17% | $74.18M | $814.20M | 11.8 |
| 9 | BNB-USDT-SWAP | +0.33% | $129.51M | $216.57M | 12.0 |
| 10 | ETC-USDT-SWAP | +0.57% | $129.07M | $129.38M | 14.2 |
| 11 | LTC-USDT-SWAP | +1.36% | $30.96M | $403.82M | 14.8 |
| 12 | 1INCH-USDT-SWAP | -0.09% | $24.65M | $1.14B | 15.0 |
| 13 | SUI-USDT-SWAP | +3.29% | $102.38M | $98.53M | 16.0 |
| 14 | AAVE-USDT-SWAP | -0.82% | $4.24B | $41.30M | 18.0 |
| 15 | THETA-USDT-SWAP | +1.19% | $20.69M | $180.23M | 18.4 |
| 16 | SKY-USDT-SWAP | -30.11% | $8.27M | $219.15B | 19.2 |
| 17 | ADA-USDT-SWAP | +2.55% | $40.20M | $45.85M | 21.8 |
| 18 | ACT-USDT-SWAP | -1.02% | $7.84M | $421.77M | 24.0 |
| 19 | CAT-USDT-SWAP | -1.23% | $10.45M | $143.66M | 24.6 |
| 20 | ALGO-USDT-SWAP | +0.46% | $268.37M | $15.81M | 24.6 |

## 去重候选池

SPK-USDT-SWAP, AXS-USDT-SWAP, IOTA-USDT-SWAP, CHZ-USDT-SWAP, MAGIC-USDT-SWAP, GMT-USDT-SWAP, NEAR-USDT-SWAP, SUI-USDT-SWAP, ATOM-USDT-SWAP, ZETA-USDT-SWAP, BTC-USDT-SWAP, ETH-USDT-SWAP, SHIB-USDT-SWAP, SOL-USDT-SWAP, DOGE-USDT-SWAP, PEPE-USDT-SWAP, KAITO-USDT-SWAP, BNB-USDT-SWAP, ETC-USDT-SWAP, LTC-USDT-SWAP, 1INCH-USDT-SWAP, AAVE-USDT-SWAP, THETA-USDT-SWAP, SKY-USDT-SWAP, ADA-USDT-SWAP, ACT-USDT-SWAP, CAT-USDT-SWAP, ALGO-USDT-SWAP

## 候选与交易决定

逐一检查 5m/15m/1h 结构、盘口价差、200 USDT 深度滑点、成交额、资金费时间戳与突破量。所有候选至少触发资金费不可验证或 K 线缺失/延迟；代表性硬拒绝如下：
- SPK-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- AXS-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟
- IOTA-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟
- CHZ-USDT-SWAP：盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- MAGIC-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；K线缺失/延迟
- GMT-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- NEAR-USDT-SWAP：资金费结算时间不可验证；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- SUI-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟
- ATOM-USDT-SWAP：资金费结算时间不可验证；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- ZETA-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；K线缺失/延迟

没有可审计的同向突破/回撤确认、明确限价入场、止盈止损和净预期 R≥2 信号。**本次不交易。** 未生成 clientOrderID，未提交限价单。

## 监控状态

- 当前无持仓、普通挂单或保护单，无需撤单/平仓；无保护单异常、API/认证失败或数据接口报错。
- 继续使用 OKX 模拟账户监控至下一次整点；在 K 线与资金费时间戳恢复前保持不开仓。
