# OKX 模拟盘日内监控报告

- 运行时间：2026-10-04 14:01:18 UTC / 2026-10-04 22:01:18 Asia/Shanghai（完成：2026-10-04 14:02:12 UTC / 2026-10-04 22:02:12 Asia/Shanghai）
- 账户：OKX demo / profile `okx-demo` / 模拟1 / `net_mode` / paper；本轮所有 ATK 请求均显式 `--demo --profile okx-demo --json --env`。
- UTC 当日止损：1/3；当日止损上限未触发。止损证据：CHZ-USDT-SWAP 2026-10-04 09:39:55 UTC / 2026-10-04 17:39:55 Asia/Shanghai。

## 市场判断
- 结论：**中性 / 数据不完整，置信度约 55%，本次不交易**。
- BTC：5m/15m/1h/4h 最新确认 K 线分别约 130 秒、130 秒、130 秒、7330 秒未更新，5m/15m 与 1h 方向不一致；4h 虽在 20SMA 上方但已明显陈旧。
- ETH：5m/15m/1h/4h 同样存在约 130 秒、130 秒、130 秒、7330 秒延迟；5m/15m 与 1h 方向不一致。
- 当前 BTC/ETH OI 约 1,831.26B / 879.45B USDT；资金费率约 -0.1173% / -0.0119%，但 `fundingTime` 为 2026-09-24 16:00 UTC，且下一结算时间为空/不可验证。
- 失效/重新启用条件：5m/15m/1h/4h K 线恢复新鲜、资金费结算时间可验证，且 BTC/ETH 结构一致；否则只监控。

## UTC 涨幅榜前 10（按 `last / sodUtc0 - 1`，ticker 时间约 14:01:24 UTC / 22:01:24 Asia/Shanghai）

|#|合约|涨幅|24h USDT成交额|盘口价差|
|---:|---|---:|---:|---:|
|1|SPK-USDT-SWAP|+42.62%|0.09M|13.943%|
|2|AXS-USDT-SWAP|+14.28%|6.88M|0.022%|
|3|IOTA-USDT-SWAP|+12.35%|5.09M|0.196%|
|4|MAGIC-USDT-SWAP|+11.50%|0.44M|0.063%|
|5|SAND-USDT-SWAP|+7.51%|29.12M|0.125%|
|6|CHZ-USDT-SWAP|+7.12%|3.24M|0.059%|
|7|GMT-USDT-SWAP|+6.05%|0.01M|0.217%|
|8|ATOM-USDT-SWAP|+3.85%|5.85M|0.056%|
|9|YGG-USDT-SWAP|+3.27%|1.30M|0.351%|
|10|CRO-USDT-SWAP|+2.91%|16.19M|0.029%|

## 热门榜前 20
- 口径：对实时 `24h USDT 成交额 = volCcy24h × last` 与实时 OI 分别排名，热度分 `0.6 × 成交额排名 + 0.4 × OI 排名`，分数越低越热；ticker/OI 采样约 14:01:24 UTC / 22:01:24 Asia/Shanghai。

|#|合约|热度分|24h成交额(USDT)|OI(USDT)|UTC涨幅|
|---:|---|---:|---:|---:|---:|
|1|BTC-USDT-SWAP|1.0|143232.76M|1831.26B|+0.57%|
|2|ETH-USDT-SWAP|2.0|79827.44M|879.45B|+0.40%|
|3|NEAR-USDT-SWAP|4.6|6242.34M|1.61B|+0.54%|
|4|SHIB-USDT-SWAP|5.0|1802.04M|2.79B|-0.70%|
|5|SOL-USDT-SWAP|6.0|1176.10M|1.98B|+1.78%|
|6|DOGE-USDT-SWAP|9.8|246.28M|0.69B|+0.88%|
|7|BNB-USDT-SWAP|10.8|250.20M|0.22B|+0.33%|
|8|KAITO-USDT-SWAP|11.2|73.95M|0.80B|-1.55%|
|9|PEPE-USDT-SWAP|11.6|196.10M|0.29B|-0.93%|
|10|ETC-USDT-SWAP|14.2|156.51M|0.14B|-0.09%|
|11|LTC-USDT-SWAP|16.0|27.93M|0.40B|+1.10%|
|12|SUI-USDT-SWAP|16.6|47.96M|0.09B|+0.55%|
|13|1INCH-USDT-SWAP|16.8|19.48M|1.14B|-0.09%|
|14|AAVE-USDT-SWAP|17.6|4704.88M|0.04B|-1.38%|
|15|THETA-USDT-SWAP|18.4|20.32M|0.18B|-0.35%|
|16|SKY-USDT-SWAP|21.0|6.58M|248.51B|-20.74%|
|17|ADA-USDT-SWAP|22.4|35.24M|0.05B|+0.33%|
|18|CRO-USDT-SWAP|23.8|16.19M|0.07B|+2.91%|
|19|CAT-USDT-SWAP|24.0|10.52M|0.14B|-0.66%|
|20|TRX-USDT-SWAP|24.6|45.82M|0.04B|+0.09%|

## 去重候选池
SPK-USDT-SWAP, AXS-USDT-SWAP, IOTA-USDT-SWAP, MAGIC-USDT-SWAP, SAND-USDT-SWAP, CHZ-USDT-SWAP, GMT-USDT-SWAP, ATOM-USDT-SWAP, YGG-USDT-SWAP, CRO-USDT-SWAP, BTC-USDT-SWAP, ETH-USDT-SWAP, NEAR-USDT-SWAP, SHIB-USDT-SWAP, SOL-USDT-SWAP, DOGE-USDT-SWAP, BNB-USDT-SWAP, KAITO-USDT-SWAP, PEPE-USDT-SWAP, ETC-USDT-SWAP, LTC-USDT-SWAP, SUI-USDT-SWAP, 1INCH-USDT-SWAP, AAVE-USDT-SWAP, THETA-USDT-SWAP, SKY-USDT-SWAP, ADA-USDT-SWAP, CAT-USDT-SWAP, TRX-USDT-SWAP

## 候选与交易
- 合格候选：无。
- 所有候选至少触发资金费时间不可验证或 K 线延迟；部分标的另有成交额不足、价差 >0.1%、200 USDT 深度滑点不足等问题。没有可验证的突破/回撤阈值和风险收益比，**本次不交易**。

## 模拟盘状态与监控
- 当前仓位：无。
- 普通挂单：无。
- OCO/条件保护单：无。
- 本地 TradingService：`killSwitch=false`，策略无远端仓位，USDT 可用余额约 4,995.99。
- 本轮未生成 clientOrderID，未发生订单变更；继续监控至下一次整点。

原始审计快照：`strategies/okx_demo_intraday_monitor/results/20261004T140118Z/snapshot.json`。
