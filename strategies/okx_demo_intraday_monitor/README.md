# OKX 模拟盘日内监控

自动化 `okx` 的只读扫描与审计记录目录，未接入 Swift 策略引擎。

- `src/scan.py`：通过 ATK CLI 显式 `--demo --profile okx-demo --json --env` 获取账户、保护单、UTC 当日行情、候选结构和盘口；只读，不提交订单、不生成 clientOrderID。
- `results/<UTC运行时间>/snapshot.json`：可复核的模拟盘响应和计算指标（账户配置仅保留白名单字段），不保存密钥。
- `results/<UTC运行时间>/report.md`：本次市场判断、两榜、候选池、拒绝原因和订单监控状态。

运行：`python3 strategies/okx_demo_intraday_monitor/src/scan.py`。

扫描门槛：数据与时间戳一致（按最近已收盘 K 线和缺失已收盘 bar 数判断，避免把当前 bar 内的正常经过时间误判为延迟）；成交额至少 300 万 USDT、双边价差 ≤0.1%、200 USDT 深度预期滑点 ≤0.05%；已确认 5m/15m/1h 方向一致；5m 突破前 20 根高点或低点、成交量 ≥前 20 根均值 1.5 倍；止损由结构与 ATR 约束、净预期 R ≥2。资金费结算时间异常、缺失数据、现有本任务仓位或 UTC 当日已完成止损 ≥3 笔时关闭开仓资格。

此扫描器只给审计证据。真正下单须经 TradingService 风控与生命周期校验、合约规格向下取整、保证金 ≤100 USDT、2 倍杠杆、限价同请求附带 mark TP/SL；无法满足即不下单。
