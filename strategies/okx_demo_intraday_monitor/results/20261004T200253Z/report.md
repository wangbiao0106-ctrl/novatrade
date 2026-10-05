# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 20:02:53 UTC / 2026-10-05 04:02:53 Asia/Shanghai；完成：2026-10-04 20:03:46 UTC / 2026-10-05 04:03:46 Asia/Shanghai。所有 CLI 请求均显式 `--demo --profile okx-demo --json --env`，未输出密钥。
- 账户：OKX demo / `模拟1` / `net_mode` / paper；可用 USDT 约 4,995.99；TradingService `killSwitch=false`。
- 最终状态：SWAP 持仓 0、普通挂单 0、OCO 0、conditional 0。
- UTC 当日已实际触发并完成止损：**1 / 3 笔**；止损上限未触发。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。**

- UTC 日内广度（±0.05%）：48 上涨 / 39 下跌 / 16 横盘。
- BTC-USDT-SWAP：5m 上/20SMA，+0.07%；15m 上，+0.21%；1h 上，+0.68%；4h 上，+2.18%；OI $1834.94B；资金费 0.0018%。
- ETH-USDT-SWAP：5m 下/20SMA，-0.01%；15m 上，+0.10%；1h 上，+0.60%；4h 上，+1.21%；OI $880.27B；资金费 0.0055%。
- BTC/ETH 5m、15m 收盘数据约 224 秒未更新；资金费 `fundingTime` 为 2026-09-24、`nextFundingTime` 已过（响应时间约 20:03 UTC），资金费新鲜度不可验证。部分合约 UTC 开盘字段与 00:00 K 线也不一致，按数据异常处理。
- 失效/恢复条件：BTC/ETH 各周期恢复可验证新鲜且方向一致、资金费时间戳有效，且候选通过流动性、突破量和净预期 R≥2 门槛。

## UTC 当日涨幅榜前 10

口径：`last / sodUtc0 - 1`；ticker 时间约 2026-10-04 20:02:xx UTC / 2026-10-05 04:02:xx Asia/Shanghai；成交额为 24h `volCcy24h × last`。

|#|合约|涨幅|24h成交额|OI|
|-:|---|---:|---:|---:|
|1|SPK-USDT-SWAP|+88.79%|$0.15M|$39.73M|
|2|AXS-USDT-SWAP|+14.87%|$8.45M|$2.74M|
|3|IOTA-USDT-SWAP|+9.52%|$7.69M|$2.79M|
|4|CHZ-USDT-SWAP|+6.80%|$3.74M|$59.25M|
|5|MAGIC-USDT-SWAP|+6.62%|$0.56M|$1.40M|
|6|GMT-USDT-SWAP|+3.88%|$0.01M|$6.50M|
|7|ADA-USDT-SWAP|+3.61%|$45.29M|$46.33M|
|8|XTZ-USDT-SWAP|+3.20%|$3.29M|$31.54M|
|9|LIT-USDT-SWAP|+3.16%|$4.33M|$50.04M|
|10|A-USDT-SWAP|+3.16%|$0.03M|$0.30M|

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；热度分 = `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热；ticker/OI 时间约 2026-10-04 20:02:xx UTC / 2026-10-05 04:02:xx Asia/Shanghai。

|#|合约|涨幅|24h成交额|OI|热度分|
|-:|---|---:|---:|---:|---:|
|1|BTC-USDT-SWAP|+0.85%|$138109.77M|$1834943.09M|1.0|
|2|ETH-USDT-SWAP|+0.50%|$78709.62M|$880268.38M|2.0|
|3|NEAR-USDT-SWAP|+2.17%|$7247.50M|$1637.91M|4.6|
|4|SHIB-USDT-SWAP|+1.40%|$2062.27M|$2849.89M|5.0|
|5|SOL-USDT-SWAP|+1.20%|$1172.24M|$1994.27M|6.0|
|6|DOGE-USDT-SWAP|+2.88%|$201.68M|$702.13M|9.2|
|7|PEPE-USDT-SWAP|+0.00%|$152.15M|$297.66M|11.0|
|8|KAITO-USDT-SWAP|-0.14%|$73.77M|$811.65M|11.8|
|9|BNB-USDT-SWAP|+0.32%|$122.60M|$216.79M|12.0|
|10|ETC-USDT-SWAP|+0.66%|$98.95M|$129.51M|14.8|
|11|LTC-USDT-SWAP|+1.32%|$30.63M|$403.65M|14.8|
|12|1INCH-USDT-SWAP|+0.57%|$24.82M|$1149.73M|15.0|
|13|AAVE-USDT-SWAP|-1.04%|$4220.28M|$48.05M|15.2|
|14|SUI-USDT-SWAP|+2.13%|$110.28M|$97.69M|15.4|
|15|THETA-USDT-SWAP|+0.88%|$20.63M|$179.67M|19.0|
|16|SKY-USDT-SWAP|-30.11%|$8.27M|$219146.60M|20.4|
|17|ADA-USDT-SWAP|+3.61%|$45.29M|$46.33M|22.2|
|18|CRO-USDT-SWAP|+2.06%|$16.06M|$65.98M|24.2|
|19|CAT-USDT-SWAP|-0.57%|$10.52M|$144.61M|24.6|
|20|ACT-USDT-SWAP|-1.40%|$7.81M|$420.18M|24.6|

## 去重候选池

SPK-USDT-SWAP, AXS-USDT-SWAP, IOTA-USDT-SWAP, CHZ-USDT-SWAP, MAGIC-USDT-SWAP, GMT-USDT-SWAP, ADA-USDT-SWAP, XTZ-USDT-SWAP, LIT-USDT-SWAP, A-USDT-SWAP, BTC-USDT-SWAP, ETH-USDT-SWAP, NEAR-USDT-SWAP, SHIB-USDT-SWAP, SOL-USDT-SWAP, DOGE-USDT-SWAP, PEPE-USDT-SWAP, KAITO-USDT-SWAP, BNB-USDT-SWAP, ETC-USDT-SWAP, LTC-USDT-SWAP, 1INCH-USDT-SWAP, AAVE-USDT-SWAP, SUI-USDT-SWAP, THETA-USDT-SWAP, SKY-USDT-SWAP, CRO-USDT-SWAP, CAT-USDT-SWAP, ACT-USDT-SWAP

## 候选与交易决定

逐一检查了 5m/15m/1h 结构、盘口价差、200 USDT 深度滑点、成交额、资金费时间戳和放量突破。所有候选至少触发资金费不可验证或 K 线延迟/方向不一致；SPK 另有异常涨幅与低成交额，CHZ/主流候选存在价差或深度门槛问题。`TEST01-USDT-SWAP` 的 UTC 开盘与 K 线不一致且盘口价差约 1.10%，不纳入交易。

**本次不交易。** 未生成 clientOrderID，未提交订单。

## 监控状态

- 当前无持仓、普通挂单或保护单，无需撤单/平仓；无保护单异常、API/认证失败。
- 已通过 okx-locald 认证 WebSocket 收到 BTC-USDT-SWAP 1m 实时变更帧（20:03:42–20:03:44 UTC），连接与订阅正常。
- 继续使用 OKX 模拟账户监控至下一次整点；在 K 线与资金费时间戳恢复前保持不开仓。
