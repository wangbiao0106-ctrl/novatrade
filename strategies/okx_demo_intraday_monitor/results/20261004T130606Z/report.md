# OKX 模拟盘日内分析与交易监控

运行时间：2026-10-04 13:06:06 UTC / 2026-10-04 21:06:06 Asia/Shanghai（完成 2026-10-04 13:07:00 UTC / 2026-10-04 21:07:00 Asia/Shanghai）。所有 ATK 请求均显式 `--demo --profile okx-demo --json --env`，未输出密钥。原始审计快照：`snapshot.json`。

## 账户与止损

- 账户模式：OKX demo / paper，net_mode；可用 USDT 4995.99；本任务当前持仓、普通挂单、OCO/conditional 保护单均为空。
- UTC 当日实际止损：**1/3**。唯一止损为 CHZ 09:39:55 UTC；ZRO 为止盈。未触发当日止损上限。
- 本地 TradingService 风控 killSwitch=false；独立策略仍启用但当前无远端仓位。

## 市场扫描

- 103 个 live USDT linear SWAP；涨幅口径为 `last/sodUtc0-1`，行情约 13:06 UTC / 21:06 Asia/Shanghai。
- UTC 涨幅榜前 10（涨幅 / 24h USDT 成交额）：SPK +929.41%/$0.55M, AXS +14.27%/$6.72M, IOTA +9.57%/$4.23M, CHZ +6.61%/$3.23M, MAGIC +6.34%/$0.33M, GMT +5.48%/$0.01M, YGG +4.00%/$1.31M, CRO +3.24%/$16.24M, ATOM +2.97%/$5.87M, CATI +2.84%/$4.01M
- 热门榜前 20 口径：`0.6*24h USDT turnover rank + 0.4*current OI rank`，成交额=`volCcy24h*last`，OI 为 `market open-interest`，时间约 13:06 UTC。
- 热门榜：BTC($143440.9M,OI$1829146.7M), ETH($79906.4M,OI$878640.2M), NEAR($6217.4M,OI$1611.2M), SHIB($1810.3M,OI$2798.0M), SOL($1172.3M,OI$1971.5M), DOGE($247.0M,OI$685.9M), BNB($251.3M,OI$216.0M), KAITO($74.4M,OI$804.5M), PEPE($198.5M,OI$295.0M), ETC($156.5M,OI$137.2M), LTC($28.5M,OI$404.6M), 1INCH($19.6M,OI$1148.6M), SUI($47.0M,OI$93.3M), AAVE($4747.2M,OI$43.1M), THETA($20.3M,OI$177.5M), SKY($5.8M,OI$248511.1M), ADA($35.2M,OI$47.6M), CRO($16.2M,OI$66.7M), CAT($10.5M,OI$144.5M), TRX($38.6M,OI$34.8M)
- 去重候选池（29）：SPK, AXS, IOTA, CHZ, MAGIC, GMT, YGG, CRO, ATOM, CATI, BTC, ETH, NEAR, SHIB, SOL, DOGE, BNB, KAITO, PEPE, ETC, LTC, 1INCH, SUI, AAVE, THETA, SKY, ADA, CAT, TRX

## 方向与交易决定

BTC/ETH 的 5m、15m、1h、4h K 线响应均未达到本次扫描的新鲜度阈值（最近确认柱分别约滞后 2–67 分钟）；资金费率返回的 fundingTime 为 2026-09-24，nextFundingTime 为空，时间不可验证。BTC 5m/15m 低于 20SMA、1h/4h 高于；ETH 同样短周期偏弱、1h/4h 高于，结构冲突。综合判断：**中性，置信度 55%**。失效/恢复条件：BTC 与 ETH 同时恢复新鲜且 5m/15m/1h 同向，资金费时间戳可验证，并且涨跌广度重新明显偏向一侧。

候选审计（前 12 个）：
- SPK-USDT-SWAP: 资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足
- AXS-USDT-SWAP: 资金费结算时间不可验证；K线缺失/延迟
- IOTA-USDT-SWAP: 资金费结算时间不可验证；盘口价差>0.1%或缺失；K线缺失/延迟
- CHZ-USDT-SWAP: 盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- MAGIC-USDT-SWAP: 资金费结算时间不可验证；24h成交额<300万；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- GMT-USDT-SWAP: 资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足
- YGG-USDT-SWAP: 资金费结算时间不可验证；24h成交额<300万；盘口价差>0.1%或缺失；K线缺失/延迟
- CRO-USDT-SWAP: 资金费结算时间不可验证；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- ATOM-USDT-SWAP: 资金费结算时间不可验证；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- CATI-USDT-SWAP: 资金费结算时间不可验证；盘口价差>0.1%或缺失；200USDT深度滑点>0.05%或不足；K线缺失/延迟
- BTC-USDT-SWAP: 资金费结算时间不可验证；K线缺失/延迟
- ETH-USDT-SWAP: 资金费结算时间不可验证；K线缺失/延迟

数据延迟、资金费时间冲突和盘口/滑点门槛导致没有可验证阈值信号；按安全规则本次不交易。未生成 clientOrderID，未提交任何订单。

## 监控状态

当前无仓位、无挂单、无保护单，无需撤单或平仓；继续等待下一整点扫描。
