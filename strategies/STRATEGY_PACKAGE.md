# 策略配置包协议

策略实验室的定稿目录可以作为一个可安装策略包。包根目录至少要包含
`config/strategy.json`，也可以附带 `STRATEGY.md`、研究脚本、回测结果和参数记录。
运行时会把包复制到应用数据目录 `Application Support/NovaTrade/strategy-packages/<identifier>/`，不会读取仓库中的 `strategies/` 目录。

配置文件需要提供以下字段：

```json
{
  "identifier": "my_strategy",
  "version": "1.0.0",
  "display_name": "我的策略",
  "name_en": "My Strategy",
  "runtime": {
    "strategy_type": "my_strategy",
    "scope": "dynamic.hotAltcoins",
    "live_order_mode": "paper_only",
    "auto_submit_live_orders": false,
    "enabled_by_default": false
  },
  "entry_timeframe_minutes": 15,
  "signal_parameters": {},
  "position_management": {
    "risk_per_trade_pct": 1.0,
    "risk_per_trade_max_pct": 1.0,
    "cooldown_bars": 16
  }
}
```

`runtime.strategy_type` 是执行适配器的稳定标识。配置包可以在适配器尚未随应用发布时先安装，但会保持休眠状态：只有当前二进制能够识别该适配器时，策略才可以创建实例。`enabled_by_default` 和 `auto_submit_live_orders` 默认均为 `false`，安装过程不会启动策略或提交订单。

AI/策略实验室完成定稿后，把策略目录复制到后台的 `strategy-staging/` 目录，再调用
`POST /api/v1/strategy-packages`（`{"path":"<相对路径>","replacing":false}`）；升级时将
`replacing` 设为 `true`。停止所有该策略实例后，调用 `DELETE /api/v1/strategy-packages/<identifier>`
删除整个已安装包。安装与卸载只改应用数据目录和持久化策略实例，不需要修改 Swift 路由代码。
