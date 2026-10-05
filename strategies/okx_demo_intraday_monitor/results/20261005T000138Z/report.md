# OKX 模拟盘日内分析与交易监控报告

- 运行时间：2026-10-05 00:01:38 UTC / 2026-10-05 08:01:38 Asia/Shanghai；完成时间：2026-10-05 00:02:31 UTC / 2026-10-05 08:02:31 Asia/Shanghai
- 账户：OKX 模拟盘 `--demo --profile okx-demo`，`paper`、`net_mode`；本轮所有 CLI 请求均显式 `--demo`，未写入或输出密钥。
- 可用 USDT：约 4,995.99；TradingService `killSwitch=false`；账户已认证。
- UTC 当日止损计数：**0/3**（统计 UTC 2026-10-05 00:00 至本次运行；上一 UTC 日 CHZ 止损不计入本日）。当日止损上限未触发。

## 市场判断

**中性 / 数据不完整，置信度约 55%；本次不交易。**

BTC 与 ETH 的 1h、4h 收盘均在 20SMA 上方，但 5m/15m 收盘延迟约 149 秒且被 freshness 门槛判无效；BTC 5m/15m 仍在 SMA 上方，ETH 5m 在下、15m 在上，短周期方向不一致。BTC 1h/4h 24 根涨幅约 2.05%/3.48%，ETH 约 1.49%/1.54%；两者均无放量突破。资金费率响应时间戳为 2026-09-24 16:00 UTC，`nextFundingTime` 已于 2026-09-25 00:00 UTC 过期，资金费不可验证；当前 OI 快照时间约 00:01:43 UTC。

失效/重新启用条件：5m、15m、1h K 线均新鲜且 BTC/ETH 方向一致，资金费 `fundingTime/nextFundingTime` 可验证；若任一基准失守 1h 20SMA 或数据延迟/冲突持续，继续停开仓。

### BTC/ETH 结构

|标的|5m|15m|1h|4h|资金费率|OI(USDT)|
|---|---|---|---|---|---:|---:|
|BTC-USDT-SWAP|上方 SMA20，延迟 149s|上方 SMA20，延迟 149s|上方 SMA20，24根 +2.05%|上方 SMA20，24根 +3.48%|-0.0028%（时间戳旧）|$1,854,347,627,050|
|ETH-USDT-SWAP|下方 SMA20，延迟 149s|上方 SMA20，延迟 149s|上方 SMA20，24根 +1.49%|上方 SMA20，24根 +1.54%|+0.0100%（时间戳旧）|$887,362,973,222|

## UTC 当日涨幅榜前 10

口径：`last / sodUtc0 - 1`；时间戳约 2026-10-05 00:01:43 UTC / 08:01:43 Asia/Shanghai；成交额为 `volCcy24h × last`。

|#|合约|UTC涨幅|24h成交额|OI(USDT)|价差|
|---:|---|---:|---:|---:|---:|
|1|NEO-USDT-SWAP|0.43%|$18,140,898|$32,793,055|0.35%|
|2|IRYS-USDT-SWAP|0.27%|$34,888|$129,313|0.01%|
|3|CATI-USDT-SWAP|0.15%|$4,522,753|$721,400|0.15%|
|4|ZETA-USDT-SWAP|0.08%|$1,618,192|$9,674,150|0.08%|
|5|ZIL-USDT-SWAP|0.03%|$8,342,447|$52,755,733|0.11%|
|6|SUSHI-USDT-SWAP|0.00%|$51,075|$6,126,295|0.08%|
|7|THETA-USDT-SWAP|0.00%|$750,709|$181,935,261|0.09%|
|8|SPK-USDT-SWAP|0.00%|$19,742|$4,694,941|56.23%|
|9|A-USDT-SWAP|0.00%|$16,467|$299,917|0.21%|
|10|PIGGY-USDT-SWAP|0.00%|$0|$61|—|

## 热门榜前 20

口径：103 个 `live + linear + settleCcy=USDT` 永续；`heatScore = 0.6×24h成交额排名 + 0.4×当前 OI 排名`，分数越低越热。成交额、OI、涨幅同一快照（约 00:01:43 UTC / 08:01:43 Asia/Shanghai）。

|#|合约|UTC涨幅|24h成交额|OI(USDT)|热度分|
|---:|---|---:|---:|---:|---:|
|1|BTC-USDT-SWAP|-0.16%|$145,426,830,313|$1,854,347,627,050|1.0|
|2|ETH-USDT-SWAP|-0.11%|$83,777,765,360|$887,362,973,222|2.0|
|3|NEAR-USDT-SWAP|-0.39%|$7,108,278,011|$1,495,482,362|4.6|
|4|SHIB-USDT-SWAP|-0.17%|$2,577,797,266|$2,874,289,631|5.0|
|5|SOL-USDT-SWAP|-0.12%|$1,135,642,326|$1,926,051,291|6.0|
|6|DOGE-USDT-SWAP|-0.20%|$234,178,618|$704,875,512|9.2|
|7|PEPE-USDT-SWAP|-0.23%|$196,282,482|$303,239,997|11.0|
|8|KAITO-USDT-SWAP|-0.06%|$73,871,816|$753,170,454|11.2|
|9|BNB-USDT-SWAP|-0.03%|$115,335,189|$214,462,451|12.6|
|10|1INCH-USDT-SWAP|0.00%|$31,290,748|$1,140,483,144|13.8|
|11|LTC-USDT-SWAP|-0.18%|$31,820,392|$399,606,010|14.8|
|12|SUI-USDT-SWAP|-0.13%|$115,912,239|$97,548,561|14.8|
|13|ETC-USDT-SWAP|-0.22%|$53,897,451|$130,713,605|16.0|
|14|AAVE-USDT-SWAP|-0.01%|$3,688,736,034|$41,146,230|16.8|
|15|ADA-USDT-SWAP|0.00%|$54,136,857|$50,647,478|20.2|
|16|CRO-USDT-SWAP|-0.07%|$19,560,378|$70,317,662|21.0|
|17|CAT-USDT-SWAP|0.00%|$15,597,566|$144,231,323|21.0|
|18|SKY-USDT-SWAP|0.00%|$6,800,806|$216,989,976,633|22.8|
|19|ICP-USDT-SWAP|-0.09%|$7,319,717|$1,342,367,512|24.2|
|20|MASK-USDT-SWAP|-0.12%|$18,080,212|$55,924,705|24.4|

## 去重候选池

共 30 个：NEO-USDT-SWAP、IRYS-USDT-SWAP、CATI-USDT-SWAP、ZETA-USDT-SWAP、ZIL-USDT-SWAP、SUSHI-USDT-SWAP、THETA-USDT-SWAP、SPK-USDT-SWAP、A-USDT-SWAP、PIGGY-USDT-SWAP、BTC-USDT-SWAP、ETH-USDT-SWAP、NEAR-USDT-SWAP、SHIB-USDT-SWAP、SOL-USDT-SWAP、DOGE-USDT-SWAP、PEPE-USDT-SWAP、KAITO-USDT-SWAP、BNB-USDT-SWAP、1INCH-USDT-SWAP、LTC-USDT-SWAP、SUI-USDT-SWAP、ETC-USDT-SWAP、AAVE-USDT-SWAP、ADA-USDT-SWAP、CRO-USDT-SWAP、CAT-USDT-SWAP、SKY-USDT-SWAP、ICP-USDT-SWAP、MASK-USDT-SWAP。

## 候选与交易决定

**本次不交易。** 无合格标的，未生成 clientOrderID，未提交订单；因此无入场、止盈、止损或预期 R 点位。所有候选均因至少一项硬门槛失败：资金费时间不可验证、5m/15m/1h K 线延迟，或成交额/价差/200 USDT 深度滑点不达标。典型拒绝：NEO 价差约 0.31% 且深度不足；SPK 价差约 56.23% 且成交额不足；BTC/ETH 虽流动性好但 K 线与资金费新鲜度不合格。

## 模拟盘订单、仓位与保护单

- 最终 SWAP 持仓：0；普通挂单：0；OCO：0；conditional：0。
- 无需撤单、平仓或保护单修复；无重复 clientOrderID。
- 已尝试独立 `scripts/test_local_stream.py --live`，临时 `okx-locald /health` 返回 HTTP 403，未完成该独立 WSS 回归检查；本轮账户 REST 认证、行情抓取和状态核对均成功。下一轮继续复核本地流权限与 K 线/资金费新鲜度。

原始审计快照：`strategies/okx_demo_intraday_monitor/results/20261005T000138Z/snapshot.json`。
