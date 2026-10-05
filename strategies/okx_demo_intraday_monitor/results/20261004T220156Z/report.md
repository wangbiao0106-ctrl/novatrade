# OKX 模拟盘日内分析与交易监控

- 运行时间：2026-10-04 22:01:56 UTC / 2026-10-05 06:01:56 Asia/Shanghai；完成：2026-10-04 22:02:46 UTC / 2026-10-05 06:02:46 Asia/Shanghai。所有 ATK/OKX CLI 请求显式 `--demo --profile okx-demo --json --env`，未写入或输出密钥。
- 账户：OKX demo / profile `okx-demo` / 标签 `模拟1` / `net_mode`；可用 USDT 约 4995.989685443095；TradingService `killSwitch=false`。
- 最终状态：SWAP 持仓 0、普通挂单 0、OCO 0、conditional 0。
- UTC 当日已实际触发并完成止损：**1 / 3 笔**；止损上限未触发。

## 市场判断

**中性 / 数据不完整，置信度约 55%，本次不交易。**

- UTC 日内广度（±0.05%）：42 上涨 / 46 下跌 / 15 横盘。
- BTC-USDT-SWAP：5m上/20SMA，+0.52%（延迟）；15m上/20SMA，+0.72%（延迟）；1h上/20SMA，+1.36%；4h上/20SMA，+2.18%（延迟）；OI $1847.71B；资金费 +0.0005%。
- ETH-USDT-SWAP：5m下/20SMA，+0.03%（延迟）；15m下/20SMA，+0.15%（延迟）；1h上/20SMA，+0.66%；4h上/20SMA，+1.21%（延迟）；OI $881.96B；资金费 +0.0067%。
- BTC/ETH 的 5m、15m 收盘约 164 秒未更新，4h 收盘约 2.05 小时未更新；资金费 `fundingTime` 为旧时间且 `nextFundingTime` 已过，资金费新鲜度不可验证。短周期方向与高周期无法按新鲜数据确认一致。
- 失效/恢复条件：BTC/ETH 各周期恢复可验证新鲜且方向一致、资金费时间戳有效，且候选通过成交额、价差、深度、放量突破和净预期 R≥2 门槛。

## UTC 当日涨幅榜前 10

口径：`last / sodUtc0 - 1`；ticker 时间约 2026-10-04 22:02:01 UTC / 2026-10-05 06:02:01 Asia/Shanghai；成交额为 24h `volCcy24h × last`。

|#|合约|UTC涨幅|24h成交额|OI|
|-:|---|---:|---:|---:|
|1|AXS-USDT-SWAP|+13.10%|$8.35M|$2.70M|
|2|IOTA-USDT-SWAP|+9.41%|$7.74M|$2.74M|
|3|MAGIC-USDT-SWAP|+6.69%|$0.58M|$1.39M|
|4|CHZ-USDT-SWAP|+6.55%|$3.39M|$59.10M|
|5|ADA-USDT-SWAP|+4.52%|$46.80M|$46.73M|
|6|SHIB-USDT-SWAP|+4.36%|$2.40B|$2.96B|
|7|DOGE-USDT-SWAP|+4.17%|$221.46M|$711.84M|
|8|GMT-USDT-SWAP|+3.88%|$0.01M|$6.50M|
|9|A-USDT-SWAP|+3.16%|$0.02M|$0.30M|
|10|CRO-USDT-SWAP|+2.98%|$16.22M|$66.56M|

## 热门榜前 20

口径：103 个 live、linear、USDT SWAP；热度分 = `0.6 × 24h成交额排名 + 0.4 × 当前OI排名`，分数越低越热；ticker/OI 时间见快照。

|#|合约|UTC涨幅|24h成交额|OI|热度分|
|-:|---|---:|---:|---:|---:|
|1|BTC-USDT-SWAP|+1.49%|$140.59B|$1847.71B|1.0|
|2|ETH-USDT-SWAP|+0.76%|$79.33B|$881.96B|2.0|
|3|NEAR-USDT-SWAP|+2.21%|$7.14B|$1.64B|4.6|
|4|SHIB-USDT-SWAP|+4.36%|$2.40B|$2.96B|5.0|
|5|SOL-USDT-SWAP|+1.67%|$1.19B|$2.00B|6.0|
|6|DOGE-USDT-SWAP|+4.17%|$221.46M|$711.84M|9.2|
|7|PEPE-USDT-SWAP|+1.16%|$185.98M|$301.15M|11.0|
|8|KAITO-USDT-SWAP|-1.04%|$72.95M|$804.39M|11.8|
|9|BNB-USDT-SWAP|+0.85%|$129.89M|$218.57M|12.0|
|10|1INCH-USDT-SWAP|-0.66%|$26.30M|$1.14B|13.8|
|11|ETC-USDT-SWAP|+0.93%|$99.22M|$129.88M|14.8|
|12|LTC-USDT-SWAP|+0.29%|$30.76M|$398.99M|14.8|
|13|SUI-USDT-SWAP|+1.37%|$113.61M|$96.86M|15.4|
|14|AAVE-USDT-SWAP|-0.88%|$3.94B|$36.92M|18.0|
|15|THETA-USDT-SWAP|+1.06%|$20.69M|$179.97M|19.0|
|16|CAT-USDT-SWAP|-0.74%|$15.60M|$144.37M|21.6|
|17|ADA-USDT-SWAP|+4.52%|$46.80M|$46.73M|21.8|
|18|CRO-USDT-SWAP|+2.98%|$16.22M|$66.56M|23.8|
|19|ACT-USDT-SWAP|-1.30%|$7.81M|$420.58M|24.0|
|20|TRX-USDT-SWAP|+0.03%|$54.30M|$34.62M|24.4|

## 去重候选池

AXS-USDT-SWAP、IOTA-USDT-SWAP、MAGIC-USDT-SWAP、CHZ-USDT-SWAP、ADA-USDT-SWAP、SHIB-USDT-SWAP、DOGE-USDT-SWAP、GMT-USDT-SWAP、A-USDT-SWAP、CRO-USDT-SWAP、BTC-USDT-SWAP、ETH-USDT-SWAP、NEAR-USDT-SWAP、SOL-USDT-SWAP、PEPE-USDT-SWAP、KAITO-USDT-SWAP、BNB-USDT-SWAP、1INCH-USDT-SWAP、ETC-USDT-SWAP、LTC-USDT-SWAP、SUI-USDT-SWAP、AAVE-USDT-SWAP、THETA-USDT-SWAP、CAT-USDT-SWAP、ACT-USDT-SWAP、TRX-USDT-SWAP

## 候选与交易决定

逐一检查了 5m/15m/1h 结构、盘口价差、200 USDT 深度滑点、成交额、资金费时间戳和放量突破。所有候选至少触发资金费不可验证或 K 线延迟；代表性拒绝如下：
- AXS-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟。
- IOTA-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟。
- MAGIC-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；200USDT深度滑点>0.05%或不足；K线缺失/延迟。
- CHZ-USDT-SWAP：200USDT深度滑点>0.05%或不足；K线缺失/延迟。
- ADA-USDT-SWAP：资金费结算时间不可验证；200USDT深度滑点>0.05%或不足；K线缺失/延迟。
- SHIB-USDT-SWAP：资金费结算时间不可验证；盘口价差>0.1%或缺失；K线缺失/延迟。
- DOGE-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟。
- GMT-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟。
- A-USDT-SWAP：资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟。
- CRO-USDT-SWAP：资金费结算时间不可验证；K线缺失/延迟。

没有可审计的同向突破/回撤确认、明确限价入场、止盈止损和净预期 R≥2 信号。**本次不交易。** 未生成 clientOrderID，未提交限价单。

## 监控状态

- 当前无本任务持仓、普通挂单或保护单，无需撤单/平仓；扫描 API/认证请求无错误，保护单无异常。
- 本轮独立 `scripts/test_local_stream.py --live` 未通过：临时 okx-locald 的 `/health` 返回 HTTP 403，未能完成 WSS 实时帧验证；需后续人工检查本地服务健康路由/权限。
- 继续使用 OKX 模拟账户监控至下一次整点；在 K 线与资金费时间戳恢复前保持不开仓。

可复核快照：`/Users/bill/Desktop/Codex/交易软件/strategies/okx_demo_intraday_monitor/results/20261004T220156Z/snapshot.json`
