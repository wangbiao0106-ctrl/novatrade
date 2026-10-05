# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 21:02:44 UTC / 2026-10-05 05:02:44 Asia/Shanghai；完成：2026-10-04 21:03:37 UTC / 2026-10-05 05:03:37 Asia/Shanghai。
- 账户：OKX demo / profile `okx-demo` / 标签 `模拟1` / `net_mode` / paper；本轮所有 ATK/OKX CLI 请求显式 `--demo --profile okx-demo --json --env`，未写入或输出密钥。
- 可用 USDT：约 4,995.99；最终 SWAP 持仓 0、普通挂单 0、OCO 0、conditional 0；TradingService `killSwitch=false`。
- UTC 当日已实际触发并完成止损：**1 / 3 笔**；止损上限未触发。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。** BTC/ETH 日内广度（±0.05%）为 42 上涨 / 43 下跌 / 18 横盘；1h 仍在 20SMA 上方，但 5m/15m 约 214 秒未更新、4h 约 3814 秒未更新，短周期与高周期无法按新鲜数据验证一致性。资金费 `fundingTime` 为旧时间且 `nextFundingTime` 已过，结算新鲜度不可验证。

BTC-USDT-SWAP：5m延迟上20SMA/+0.62%，15m延迟上20SMA/+0.68%，1h新鲜上20SMA/+1.28%，4h延迟上20SMA/+2.18%；资金费 +0.0043%（fundingTime/nextFundingTime 已过）；OI $1844.65B。
ETH-USDT-SWAP：5m延迟上20SMA/+0.18%，15m延迟上20SMA/+0.31%，1h新鲜上20SMA/+0.71%，4h延迟上20SMA/+1.21%；资金费 +0.0059%（fundingTime/nextFundingTime 已过）；OI $881.80B。

失效/恢复条件：BTC 与 ETH 的 5m/15m/1h/4h 恢复可验证新鲜且方向一致，资金费时间戳有效，且候选通过成交额、价差、深度、放量突破和净预期 R≥2 门槛；否则继续只报告。

## UTC 当日涨幅榜前 10

涨幅口径：`last / sodUtc0 - 1`；成交额为 24h USDT 名义成交额 `volCcy24h × last`。

|#|合约|UTC涨幅|24h成交额|OI|
|-:|---|---:|---:|---:|
|1|AXS-USDT-SWAP|+13.95%|$8.39M|$2.71M|
|2|IOTA-USDT-SWAP|+8.76%|$7.65M|$2.75M|
|3|MAGIC-USDT-SWAP|+6.88%|$573.64K|$1.40M|
|4|CHZ-USDT-SWAP|+6.36%|$3.73M|$59.00M|
|5|GMT-USDT-SWAP|+3.88%|$12.22K|$6.50M|
|6|DOGE-USDT-SWAP|+3.39%|$204.86M|$705.88M|
|7|A-USDT-SWAP|+3.16%|$24.46K|$299.92K|
|8|ADA-USDT-SWAP|+3.12%|$45.79M|$46.15M|
|9|ZETA-USDT-SWAP|+3.06%|$1.61M|$9.63M|
|10|XTZ-USDT-SWAP|+2.39%|$3.26M|$31.30M|

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；热度分 = `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热；ticker/OI 时间见快照，均约 2026-10-04 21:02:50 UTC / 2026-10-05 05:02:50 Asia/Shanghai。

|#|合约|UTC涨幅|24h成交额|OI|热度分|
|-:|---|---:|---:|---:|---:|
|1|BTC-USDT-SWAP|+1.33%|$139.68B|$1844.65B|1.0|
|2|ETH-USDT-SWAP|+0.72%|$78.84B|$881.80B|2.0|
|3|NEAR-USDT-SWAP|+1.98%|$7.16B|$1.63B|4.6|
|4|SHIB-USDT-SWAP|+1.57%|$2.18B|$2.85B|5.0|
|5|SOL-USDT-SWAP|+1.40%|$1.18B|$2.00B|6.0|
|6|DOGE-USDT-SWAP|+3.39%|$204.86M|$705.88M|9.2|
|7|PEPE-USDT-SWAP|-0.23%|$148.10M|$296.96M|11.0|
|8|KAITO-USDT-SWAP|-1.06%|$72.97M|$804.14M|11.8|
|9|BNB-USDT-SWAP|+0.65%|$124.29M|$216.76M|12.0|
|10|1INCH-USDT-SWAP|-0.38%|$26.37M|$1.14B|13.8|
|11|ETC-USDT-SWAP|+0.71%|$98.99M|$129.59M|14.8|
|12|LTC-USDT-SWAP|+0.70%|$30.67M|$400.92M|14.8|
|13|SUI-USDT-SWAP|+1.42%|$111.59M|$96.86M|15.4|
|14|AAVE-USDT-SWAP|-1.00%|$4.10B|$36.89M|18.0|
|15|THETA-USDT-SWAP|+1.19%|$20.72M|$180.21M|19.0|
|16|ADA-USDT-SWAP|+3.12%|$45.79M|$46.15M|21.8|
|17|ALGO-USDT-SWAP|+0.38%|$289.11M|$20.08M|23.4|
|18|CRO-USDT-SWAP|+2.09%|$16.06M|$66.00M|23.8|
|19|ACT-USDT-SWAP|-1.54%|$7.79M|$419.59M|24.0|
|20|SKY-USDT-SWAP|-42.59%|$6.44M|$180.00B|24.0|

## 去重候选池

AXS-USDT-SWAP、IOTA-USDT-SWAP、MAGIC-USDT-SWAP、CHZ-USDT-SWAP、GMT-USDT-SWAP、DOGE-USDT-SWAP、A-USDT-SWAP、ADA-USDT-SWAP、ZETA-USDT-SWAP、XTZ-USDT-SWAP、BTC-USDT-SWAP、ETH-USDT-SWAP、NEAR-USDT-SWAP、SHIB-USDT-SWAP、SOL-USDT-SWAP、PEPE-USDT-SWAP、KAITO-USDT-SWAP、BNB-USDT-SWAP、1INCH-USDT-SWAP、ETC-USDT-SWAP、LTC-USDT-SWAP、SUI-USDT-SWAP、AAVE-USDT-SWAP、THETA-USDT-SWAP、ALGO-USDT-SWAP、CRO-USDT-SWAP、ACT-USDT-SWAP、SKY-USDT-SWAP

## 候选与交易决定

本轮最多 1 个开仓名额，但没有合格标的。AXS/IOTA/DOGE 等因资金费结算时间不可验证或 K 线延迟被拒；CHZ 深度滑点约 0.068%/0.119%；GMT/A 等成交额、价差或深度不合格；主流 BTC/ETH/NEAR/SOL 等也未通过资金费与新鲜度门槛。没有可审计的同向突破/回撤确认、明确限价入场、止盈止损和净预期 R≥2 信号。

**本次不交易。** 未生成 clientOrderID，未提交订单；因此无入场、止盈、止损或预期 R。

## 监控状态

当前无本任务持仓、挂单或保护单，无需撤单/平仓；本轮 API/认证无错误，保护单无异常。继续使用 OKX 模拟账户监控至下一次整点；在 K 线与资金费时间戳恢复前保持不开仓。

可复核快照：`/Users/bill/Desktop/Codex/交易软件/strategies/okx_demo_intraday_monitor/results/20261004T210244Z/snapshot.json`
