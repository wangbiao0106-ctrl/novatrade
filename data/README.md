# 行情数据

这里仅保存虚拟币 K 线和行情采集元数据。目录按 `交易所/市场/周期` 组织，详细规则见 [`spec/DIRECTORY_STRUCTURE.md`](../spec/DIRECTORY_STRUCTURE.md)。

当前 OKX USDT 永续原始 K 线位于 `kline/okx/swap/5m/`。原始 5 分钟 gzip JSONL 保持在这里；可重建的多周期 Parquet 缓存写入仓库根目录 `.cache/kline/okx/swap/`，不会写回 `data/kline/`。使用以下命令一次读取 5m 并生成 15m、30m、1h、4h：

```bash
python3 scripts/build_kline_cache.py
```

源 manifest 没有变化时命令会直接复用现有缓存；需要重新生成时加 `--force`。Parquet 输出需要先安装 `scripts/requirements-market-cache.txt` 中的 PyArrow 依赖。回测报告、成交记录和参数搜索结果放在各策略的 `results/`，不放在这里。
