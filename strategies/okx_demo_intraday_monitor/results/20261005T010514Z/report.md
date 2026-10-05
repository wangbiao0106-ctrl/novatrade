# OKX 模拟盘日内分析与交易监控报告

- 运行时间：2026-10-05 01:05:14 UTC / 2026-10-05 09:05:14 Asia/Shanghai；完成：2026-10-05 01:06:07 UTC / 2026-10-05 09:06:07 Asia/Shanghai
- 账户：OKX 模拟盘 `--demo --profile okx-demo`，`paper`、`net_mode`；本轮 CLI 请求显式 `--demo`，未写入或输出密钥。
- 可用权益：约 106,395.35 USD；可用 USDT 约 4,995.99；TradingService `killSwitch=False`。
- UTC 当日止损计数：**0/3**（UTC 2026-10-05 00:00 至本次运行）；当日止损上限未触发。

## 市场判断

**中性 / 数据不完整，置信度约 55%；本次不交易。**

BTC 5m/15m/1h/4h 均在 20SMA 上方，24 根变化分别 0.11%/1.53%/2.18%/3.48%；ETH 5m 在 SMA 下方而 15m/1h/4h 在上方，短周期方向冲突。BTC/ETH 各周期 K 线已通过已收盘 bar 新鲜度检查，但两者均无 5m 放量突破。资金费 `fundingTime` 仍为 2026-09-24 16:00 UTC，`nextFundingTime` 已过期，无法验证资金费新鲜度。候选池 51 涨 / 25 跌（±0.05%），但 SKY +4542.86% 为异常报价，需排除。

失效/重新启用条件：资金费时间戳恢复可验证、BTC/ETH 5m/15m/1h 方向一致并出现成交量确认；若任一基准失守 1h 20SMA 或接口再次延迟/冲突，继续停开仓。

| 标的 | 5m | 15m | 1h | 4h | 资金费率 | OI(USDT) |
|---|---|---|---|---|---:|---:|
| BTC-USDT-SWAP | 上 SMA20 | 上 SMA20 | 上 SMA20 | 上 SMA20 | -0.0091%（时间戳旧） | $1,857,773,511,147 |
| ETH-USDT-SWAP | 下 SMA20 | 上 SMA20 | 上 SMA20 | 上 SMA20 | 0.0100%（时间戳旧） | $887,767,677,250 |

## UTC 当日涨幅榜前 10

口径：`last / sodUtc0 - 1`；快照时间约 2026-10-05 01:05:14–01:05:47 UTC / 09:05:14–09:05:47 Asia/Shanghai；成交额为 `volCcy24h × last`。

|#|合约|UTC涨幅|24h成交额|OI(USDT)|盘口价差|
|---:|---|---:|---:|---:|---:|
|1|SKY-USDT-SWAP|4542.86%|$6,879,450|$215,662,820,812|199.079%|
|2|MAJOR-USDT-SWAP|36.36%|$1,226,956|$343,004|0.247%|
|3|CELO-USDT-SWAP|2.55%|$635,085|$4,455,839|0.096%|
|4|ADA-USDT-SWAP|1.93%|$66,734,247|$51,490,496|0.076%|
|5|ETHFI-USDT-SWAP|1.47%|$1,536,335|$2,809,211|0.026%|
|6|SUI-USDT-SWAP|1.42%|$120,570,147|$99,806,960|0.008%|
|7|ETHW-USDT-SWAP|1.03%|$83,020|$71,703,877|0.068%|
|8|LIT-USDT-SWAP|1.02%|$4,407,083|$50,203,092|0.033%|
|9|IRYS-USDT-SWAP|0.91%|$42,607|$127,440|0.106%|
|10|ACT-USDT-SWAP|0.89%|$15,930,602|$421,424,185|0.047%|

## 热门榜前 20

口径：103 个 `live + linear + settleCcy=USDT` 永续；`heatScore = 0.6×24h成交额排名 + 0.4×当前 OI 排名`，分数越低越热；成交额/OI/涨幅快照约 2026-10-05 01:05:47 UTC / 09:05:47 Asia/Shanghai。

|#|合约|UTC涨幅|24h成交额|OI(USDT)|热度分|
|---:|---|---:|---:|---:|---:|
|1|BTC-USDT-SWAP|0.08%|$157,343,144,382|$1,857,773,511,147|1.0|
|2|ETH-USDT-SWAP|-0.01%|$59,174,418,785|$887,767,677,250|2.0|
|3|NEAR-USDT-SWAP|-0.41%|$7,156,070,142|$1,494,975,234|4.6|
|4|SHIB-USDT-SWAP|0.51%|$2,690,226,032|$2,891,546,945|5.0|
|5|SOL-USDT-SWAP|-0.32%|$1,062,162,079|$1,923,899,601|6.0|
|6|DOGE-USDT-SWAP|0.37%|$238,973,033|$709,676,726|9.8|
|7|PEPE-USDT-SWAP|0.46%|$256,430,400|$305,322,765|10.4|
|8|KAITO-USDT-SWAP|0.52%|$59,270,408|$757,570,488|11.8|
|9|BNB-USDT-SWAP|0.05%|$99,049,137|$214,233,218|12.6|
|10|1INCH-USDT-SWAP|0.10%|$23,567,358|$1,141,568,118|14.4|
|11|LTC-USDT-SWAP|-0.22%|$31,383,188|$399,679,873|14.8|
|12|SUI-USDT-SWAP|1.42%|$120,570,147|$99,806,960|14.8|
|13|ETC-USDT-SWAP|0.12%|$54,279,609|$131,047,707|16.0|
|14|AAVE-USDT-SWAP|0.75%|$3,807,055,715|$41,423,336|16.8|
|15|ACT-USDT-SWAP|0.89%|$15,930,602|$421,424,185|18.0|
|16|ADA-USDT-SWAP|1.93%|$66,734,247|$51,490,496|19.6|
|17|SKY-USDT-SWAP|4542.86%|$6,879,450|$215,662,820,812|22.2|
|18|MASK-USDT-SWAP|-0.24%|$18,085,247|$55,868,506|23.2|
|19|ICP-USDT-SWAP|0.00%|$7,086,285|$1,343,560,469|23.6|
|20|ALGO-USDT-SWAP|-0.91%|$296,515,465|$9,415,229|26.6|

## 去重候选池

共 26 个：SKY-USDT-SWAP、MAJOR-USDT-SWAP、CELO-USDT-SWAP、ADA-USDT-SWAP、ETHFI-USDT-SWAP、SUI-USDT-SWAP、ETHW-USDT-SWAP、LIT-USDT-SWAP、IRYS-USDT-SWAP、ACT-USDT-SWAP、BTC-USDT-SWAP、ETH-USDT-SWAP、NEAR-USDT-SWAP、SHIB-USDT-SWAP、SOL-USDT-SWAP、DOGE-USDT-SWAP、PEPE-USDT-SWAP、KAITO-USDT-SWAP、BNB-USDT-SWAP、1INCH-USDT-SWAP、LTC-USDT-SWAP、ETC-USDT-SWAP、AAVE-USDT-SWAP、MASK-USDT-SWAP、ICP-USDT-SWAP、ALGO-USDT-SWAP。

## 候选与交易决定

**本次不交易。** 未生成 clientOrderID，未提交订单；没有入场、止盈、止损或预期 R 点位。所有候选至少触发资金费时间不可验证、价差/深度、成交额、方向一致性或 5m 放量突破门槛。典型拒绝：SKY 的 +4542.86% 异常涨幅与缺乏突破确认；BTC/ETH 流动性足够但资金费时间戳不可验证且无放量突破；ETH 5m/15m 方向与基准不一致。

## 模拟盘订单、仓位与保护单

- 最终 SWAP 持仓：0；普通挂单：0；OCO：0；conditional：0。
- 无需撤单、平仓或保护单修复；无重复 clientOrderID。
- 已使用本地令牌鉴权检查 `okx-locald`：health 返回 `status=ok`，并收到 BTC-USDT-SWAP 1m 两个变化 K 线帧（2026-10-05 01:02 UTC）；未输出令牌。
- 扫描器已修正 K 线新鲜度判断：按“最近已收盘 bar + 缺失 bar 数”判定，避免把已收盘 bar 的正常经过时间误判为延迟。

原始审计快照：`strategies/okx_demo_intraday_monitor/results/20261005T010514Z/snapshot.json`。
