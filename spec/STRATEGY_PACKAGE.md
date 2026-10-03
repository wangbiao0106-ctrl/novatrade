# 策略实验室配置包

策略的生成、升级和调优都在 `strategies/<strategy_id>/` 完成。实验定稿后，使用 `scripts/strategy_package.py` 把实验室目录打成一个可校验的配置包。安装包只会复制到运行时的应用数据目录（macOS 默认是 `~/Library/Application Support/NovaTrade/strategy-packages/`，也可用 `OKX_STRATEGY_PACKAGES_DIR` 覆盖），不会修改 `Sources/` 或交易服务代码；卸载会删除对应运行时包和 CLI 注册表记录，实验室源目录保持不变。

## 包布局

包可以是目录，也可以是 zip 文件。根目录必须包含：

```text
manifest.json
STRATEGY.md
config/strategy.json
README.md                 # 推荐
src/、research/、tests/  # 可选，随实验定稿一起留档
```

`manifest.json` 使用 schema 版本 `1`：

```json
{
  "schema_version": 1,
  "package_id": "sweep_reversal_short",
  "strategy_id": "sweep_reversal_short",
  "version": "1.4.0",
  "display_name": "山寨币二次扫顶",
  "lifecycle": "finalized",
  "source_of_truth": "strategies/sweep_reversal_short/STRATEGY.md",
  "artifacts": [
    {"path": "STRATEGY.md", "sha256": "<64 位十六进制摘要>"},
    {"path": "config/strategy.json", "sha256": "<64 位十六进制摘要>"}
  ]
}
```

`lifecycle` 有 `draft`、`candidate`、`finalized` 和 `retired` 四种值。只有 `finalized` 包可以安装或升级。每个 artifact 都必须是普通文件，路径不能越出包目录，摘要不匹配时安装会停止。策略 ID 必须是稳定的 ASCII `snake_case`，版本支持 `1`、`1.2`、`1.2.3` 及预发布后缀。

运行时还会拒绝符号链接和特殊文件，并限制单个文件不超过 64 MiB、包内文件总量不超过 512 MiB；`STRATEGY.md` 与 `config/strategy.json` 必须同时出现在 `artifacts` 摘要列表中。

## 实验室到运行时

先在实验室完成规则和参数验证，再打包并安装：

```bash
# 调优阶段：包可分享和校验，但不能导入运行时目录
python3 scripts/strategy_package.py pack strategies/sweep_reversal_short \
  --output /tmp/sweep-reversal-short-1.4.0.zip --version 1.4.0 --lifecycle candidate
python3 scripts/strategy_package.py validate /tmp/sweep-reversal-short-1.4.0.zip --allow-unfinalized

# 实验定稿：明确标记后才能安装/升级
python3 scripts/strategy_package.py pack strategies/sweep_reversal_short \
  --output /tmp/sweep-reversal-short-1.4.0.zip --version 1.4.0 --finalized
python3 scripts/strategy_package.py validate /tmp/sweep-reversal-short-1.4.0.zip
python3 scripts/strategy_package.py install /tmp/sweep-reversal-short-1.4.0.zip
```

安装同一策略的新版本会执行版本比较，拒绝降级和相同版本覆盖；确实要重建同版本包时显式加 `--force`。安装过程先写临时目录、重新校验，再原子替换旧目录，失败会恢复旧版本。
升级只替换策略包和新建实例使用的默认参数；已经存在的实例配置保持原值，调优后的参数需要在策略实验室确认后通过实例更新接口显式应用。

不再需要某策略时：

```bash
python3 scripts/strategy_package.py list --json
python3 scripts/strategy_package.py uninstall sweep_reversal_short
```

卸载只触碰运行时包目录和注册表，不会删除实验室源目录或交易服务源码；运行时服务直接扫描应用数据目录发现可用策略。正在运行的交易实例必须先由交易服务停止并清理持仓，然后再卸载包。

## AI 指令约定

可以直接向 AI 下达以下三类指令：

1. “把 `<策略目录>` 打成 `finalized` 的 `<版本>` 包并校验。”
2. “安装/升级 `<包路径>`；如果是降级或摘要不匹配就停止并报告。”
3. “卸载 `<strategy_id>`，同时确认注册表和策略目录都已删除。”

这些操作不需要手工修改代码。定稿包的 `STRATEGY.md`、`config/strategy.json` 和 manifest 摘要保留了可复现的规则版本；研究结果仍然只作为证据，不能绕过定稿步骤直接安装。
