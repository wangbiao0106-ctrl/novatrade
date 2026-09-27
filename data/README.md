# 行情数据

这里仅保存虚拟币 K 线和行情采集元数据。目录按 `交易所/市场/周期` 组织，详细规则见 [`spec/DIRECTORY_STRUCTURE.md`](../spec/DIRECTORY_STRUCTURE.md)。

当前 OKX USDT 永续 K 线位于 `kline/okx/swap/`：5 分钟导出、15 分钟回测缓存和 1 小时回测缓存分别进入对应周期目录。回测报告、成交记录和参数搜索结果放在各策略的 `results/`，不放在这里。
