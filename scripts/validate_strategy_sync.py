#!/usr/bin/env python3
"""Validate the checked-in runtime implementation against the strategy lab.

The strategy lab is deliberately represented by ordinary JSON/Markdown files;
this validator is the small, reviewable bridge to the Swift implementation.
It does not load lab files at runtime.  Run it after changing a strategy spec
and before changing or committing its ``Sources/`` implementation.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "strategies" / "sweep_reversal_short"
CONFIG_PATH = LAB / "config" / "strategy.json"
DOMAIN_PATH = ROOT / "Sources" / "TradingDomain" / "Domain.swift"
ENGINE_PATH = ROOT / "Sources" / "TradingService" / "StrategyEngine.swift"
STREAM_PATH = ROOT / "Sources" / "OKXLocalD" / "main.swift"
UNIVERSE_RULES_PATH = ROOT / "Sources" / "TradingDomain" / "StrategyUniverseRules.swift"
SIGNAL_PATH = LAB / "research" / "live_signal.py"


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def main() -> int:
    errors: list[str] = []
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except Exception as exc:  # pragma: no cover - command-line diagnostic
        print(f"无法读取策略配置: {exc}", file=sys.stderr)
        return 2

    runtime = config.get("runtime_universe", {})
    signal = config.get("signal_parameters", {})
    integration = config.get("runtime_integration", {})
    domain = DOMAIN_PATH.read_text()
    engine = ENGINE_PATH.read_text()
    stream = STREAM_PATH.read_text()
    universe_rules = UNIVERSE_RULES_PATH.read_text()
    live_signal = SIGNAL_PATH.read_text()
    strategy_doc = (LAB / "STRATEGY.md").read_text()
    spec_doc = (LAB / "research" / "STRATEGY_SPEC.md").read_text()

    if config.get("version") != "1.3":
        fail(errors, "config/strategy.json 不是规则版本 1.3")
    if not re.search(r"版本：1\.3", strategy_doc):
        fail(errors, "STRATEGY.md 未标记规则版本 1.3")
    if "规则说明书 v1.3" not in spec_doc:
        fail(errors, "STRATEGY_SPEC.md 未标记规则版本 1.3")
    if "实时信号扫描器 v1.3" not in live_signal:
        fail(errors, "研究实时信号扫描器未标记规则版本 1.3")
    if config.get("source_of_truth") != "strategies/sweep_reversal_short/STRATEGY.md":
        fail(errors, "config 未声明 STRATEGY.md 为规则真源")
    if runtime.get("mode") != "dynamic" or runtime.get("category") != "hotAltcoins":
        fail(errors, "运行范围必须是 dynamic.hotAltcoins")
    if runtime.get("limit") != 20 or runtime.get("refresh_seconds") != 30:
        fail(errors, "运行范围必须是每 30 秒刷新、上限 20")
    if integration.get("signal_generation") != "implemented":
        fail(errors, "实验室未声明信号生成已同步")
    if config.get("entry_timeframe_minutes") != 60 or config.get("confirmation_timeframe_minutes") != 15:
        fail(errors, "生产执行周期必须是 1h 结构 + 15m 确认")
    if config.get("confirmation_window_minutes") != 60:
        fail(errors, "15m 确认窗口必须为 60 分钟")
    if config.get("entry_submission") != "market" or integration.get("entry_submission") != "okx_demo_market_order":
        fail(errors, "策略入场必须声明为市价提交")
    if "first confirmed 15m close" not in config.get("confirmation_rule", ""):
        fail(errors, "策略配置未声明首根符合条件的 15m 收盘确认")
    if integration.get("protective_exit_orders") != "implemented":
        fail(errors, "策略实验室未声明条件保护单已接入")
    if integration.get("risk_distance_position_sizing") != "implemented":
        fail(errors, "策略实验室未声明按止损距离 sizing 已接入")
    if integration.get("strategy_time_exit") != "implemented":
        fail(errors, "策略实验室未声明时间离场已接入")
    if integration.get("account_daily_loss_circuit_breaker") != "implemented":
        fail(errors, "策略实验室未声明账户级日损熔断已接入")
    position_management = config.get("position_management", {})
    if position_management.get("leverage") != 2.0:
        fail(errors, "扫顶策略实验室杠杆必须为 2 倍")
    if position_management.get("risk_per_trade_pct") != 1.0:
        fail(errors, "策略实验室单笔风险默认值必须为 1%")
    if position_management.get("risk_per_trade_max_pct") != 5.0:
        fail(errors, "策略实验室单笔风险硬上限必须为 5%")
    if position_management.get("max_concurrent_positions") != 1:
        fail(errors, "策略实验室必须限制策略实例同时只持有一个币种")
    if position_management.get("max_open_risk_percent") != 5.0:
        fail(errors, "策略实验室单币种开放止损风险上限必须为 5%")
    if position_management.get("risk_per_trade_scope") != "each_entry_order":
        fail(errors, "策略实验室必须把单笔风险定义为每个入场订单")
    if position_management.get("risk_per_trade_basis") != "strategy_pool_equity_at_authorization":
        fail(errors, "策略实验室单笔风险基准必须是授权时策略资金池权益")
    expected_sizing = "min(available_capital, pool_equity * risk_per_trade_pct / 100 * entry_price / abs(stop_price - entry_price))"
    if position_management.get("sizing_formula") != expected_sizing:
        fail(errors, "策略实验室 sizing 公式未明确按相对止损距离和资金池可用余额封顶")
    if position_management.get("risk_budget_costs_included") is not False:
        fail(errors, "策略实验室必须明确单笔止损预算不包含成交成本，账户熔断负责 mark-to-market 成本")
    risk_policy = config.get("risk_policy", {})
    required_actions = {
        "stop_all_strategies",
        "cancel_entry_orders",
        "close_all_positions_reduce_only",
        "latch_until_manual_reset",
    }
    if (
        risk_policy.get("account_daily_loss_percent") != 5.0
        or risk_policy.get("baseline") != "calendar_day_start_equity"
        or risk_policy.get("measurement") != "mark_to_market"
        or not required_actions.issubset(set(risk_policy.get("actions", [])))
    ):
        fail(errors, "账户级日损熔断必须是 mark-to-market 的 5%")
    capital_pool = config.get("capital_pool", {})
    if (
        capital_pool.get("mode") != "per_strategy_instance"
        or capital_pool.get("cross_pool_borrowing") is not False
        or capital_pool.get("rollover") != "compound_realized_pnl"
        or capital_pool.get("unrealized_pnl_releases_available") is not False
    ):
        fail(errors, "策略资金池必须按实例隔离、只用已实现盈亏滚仓且禁止浮盈释放或跨池借用")

    if not re.search(r"availableCases\s*:.*\.sweepReversalShort.*\.emaAltcoinLong", domain, re.DOTALL):
        fail(errors, "StrategyType.availableCases 未包含 EMA 多头策略")
    if ".prefix(20)" not in domain and "prefix(limit)" not in domain:
        fail(errors, "StrategyScope.hotAltcoins 未按动态成交额前 20 实现")
    if "StrategyUniverseRules.isEligibleHotAltcoin" not in domain:
        fail(errors, "StrategyScope.hotAltcoins 未使用统一资产类别过滤")
    excluded = config.get("backtest_universe", {}).get("files", [])
    universe_config_path = LAB / "config" / "universe.json"
    try:
        universe_config = json.loads(universe_config_path.read_text())
    except Exception as exc:
        fail(errors, f"无法读取 universe.json：{exc}")
        universe_config = {}
    for symbol in universe_config.get("exclude", []):
        if f'"{symbol}"' not in universe_rules:
            fail(errors, f"Swift 资产排除清单缺少 {symbol}")

    # 资产类别排除清单在多个文件里各有一份副本。任何一份漂移都会让"山寨币"
    # 结果混入股票/ETF/指数/商品合约，所以这里把副本逐一对齐到
    # strategies/sweep_reversal_short/config/universe.json 的 exclude。
    def python_literal_set(path: Path, name: str) -> set[str] | None:
        try:
            source = path.read_text()
        except Exception as exc:  # pragma: no cover - diagnostic path
            fail(errors, f"无法读取 {path.relative_to(ROOT)}：{exc}")
            return None
        match = re.search(name + r"\s*=\s*\{(.*?)\}", source, re.DOTALL)
        if match is None:
            fail(errors, f"{path.relative_to(ROOT)} 缺少集合 {name}")
            return None
        return {item.upper() for item in re.findall(r'"([^"]+)"', match.group(1))}

    canonical_exclude = {item.upper() for item in universe_config.get("exclude", [])}
    swift_mainstream = set(re.findall(r'"([^"]+)"', re.search(r"mainstreamSymbols:\s*Set<String>\s*=\s*\[(.*?)\]", universe_rules, re.DOTALL).group(1)))
    swift_excluded = set(re.findall(r'"([^"]+)"', re.search(r"excludedNonCryptoSymbols:\s*Set<String>\s*=\s*\[(.*?)\]", universe_rules, re.DOTALL).group(1)))
    if canonical_exclude and swift_excluded != canonical_exclude:
        fail(errors, "Swift excludedNonCryptoSymbols 与 universe.json 的 exclude 不一致")

    live_mainstream = python_literal_set(SIGNAL_PATH, "MAINSTREAM")
    live_stable = python_literal_set(SIGNAL_PATH, "STABLECOINS")
    if live_mainstream is not None and live_mainstream != swift_mainstream:
        fail(errors, "live_signal.py 的 MAINSTREAM 与 Swift mainstreamSymbols 不一致")
    if live_stable is not None and not live_stable <= canonical_exclude:
        fail(errors, f"live_signal.py 的 STABLECOINS 未全部包含在 universe.json 的 exclude 中：{sorted(live_stable - canonical_exclude)}")

    for strategy in ("ema_altcoin_long", "ema_3line_pullback"):
        source_path = ROOT / "strategies" / strategy / "src" / "backtest.py"
        major = python_literal_set(source_path, "MAJOR")
        stable = python_literal_set(source_path, "STABLE")
        non_crypto = python_literal_set(source_path, "NON_CRYPTO")
        if major is None or stable is None or non_crypto is None:
            continue
        if major != swift_mainstream:
            fail(errors, f"{strategy} 的 MAJOR 与 Swift mainstreamSymbols 不一致")
        if not stable <= canonical_exclude:
            fail(errors, f"{strategy} 的 STABLE 未全部包含在 universe.json 的 exclude 中")
        expected_non_crypto = canonical_exclude - major - stable
        if non_crypto != expected_non_crypto:
            missing = sorted(expected_non_crypto - non_crypto)
            extra = sorted(non_crypto - expected_non_crypto)
            fail(errors, f"{strategy} 的 NON_CRYPTO 与规范排除清单不一致：缺少 {missing}，多出 {extra}")
    main_source = (ROOT / "Sources" / "MacTraderApp" / "main.swift").read_text()
    service_source = (ROOT / "Sources" / "TradingService" / "TradingService.swift").read_text()
    runtime_sources = domain + engine + service_source + main_source + stream
    if "emaAltcoinLong" not in runtime_sources or "evaluateEmaAltcoinLong" not in runtime_sources:
        fail(errors, "运行时代码未注册 EMA 山寨币多头策略")
    if re.search(r"case\s+trendFollowing|case\s+rsiReversal|\.trendFollowing|\.rsiReversal", domain + engine + service_source + main_source):
        fail(errors, "运行时代码仍包含已清理的演示策略入口")
    if "scope: .dynamic(.hotAltcoins)" not in main_source:
        fail(errors, "新建策略表单未固定为动态热门山寨币 scope")
    if "ForEach(StrategyType.availableCases" not in main_source:
        fail(errors, "新建策略表单未从可用策略规则列表选择规则")
    if "StrategyUniverseRules.isEligibleHotAltcoin" not in main_source:
        fail(errors, "前台热门山寨币筛选未使用统一资产类别过滤")
    if "config.scope == .dynamic(.hotAltcoins)" not in service_source:
        fail(errors, "后端未校验动态热门山寨币范围")
    if "pool.openPositions < maxConcurrent" not in service_source or "已有其他币种的挂单或持仓" not in service_source:
        fail(errors, "后端未限制策略实例同时只允许一个活动币种")
    if "case .emaAltcoinLong" not in service_source or "evaluateEmaAltcoinLong" not in service_source:
        fail(errors, "后端未接入 EMA 多头策略")
    if "双均线交易山寨币做多" not in main_source:
        fail(errors, "新建策略表单未显示 EMA 多头策略名称")
    if "isEligibleHotAltcoin" not in service_source:
        fail(errors, "后台热门榜未使用统一资产类别过滤")
    if "config.type.maxRiskPercent" not in service_source:
        fail(errors, "后端未按策略类型收敛单笔风险上限")
    if "maxRiskPercent" not in domain or "5.0" not in domain[domain.index("public var maxRiskPercent"):domain.index("public var defaultRiskPercent")]:
        fail(errors, "领域层策略风险上限未同步为 5%")
    if "capitalPoolPercent" not in service_source or "strategyCapital" not in service_source:
        fail(errors, "后端未接入策略资金池")
    if "let riskBudget = pool.equity * Decimal(config.riskPercent) / 100" not in service_source:
        fail(errors, "后端未以授权时策略池权益计算单笔风险预算")
    if "targetNotional = min(pool.availableCapital, riskBudget * entry / riskDistance)" not in service_source:
        fail(errors, "后端未按止损距离 sizing 并受资金池可用余额封顶")
    if "enforceGlobalRiskIfNeeded" not in service_source or "closeDemoPosition" not in service_source:
        fail(errors, "后端未接入账户级熔断处置")

    paper_source = (ROOT / "Sources" / "TradingService" / "PaperTrading.swift").read_text()
    if "同一标的只允许一笔" not in service_source:
        fail(errors, "后端未实现每标的单仓约束")
    if "timedOut" not in service_source or "96 * 3600" not in service_source:
        fail(errors, "后端未实现 96 小时时间离场")
    if "selectedRule.defaultParameters" not in main_source:
        fail(errors, "新建策略表单未使用领域层的规则默认参数")
    if "stopLossDescription" not in domain or "stopLossDescription" not in main_source:
        fail(errors, "策略自带止损规则未在领域层和前台展示")
    if '"maxConcurrentPositions": 1' not in domain or '"maxOpenRiskPercent": 5.0' not in domain:
        fail(errors, "运行时默认参数未同步策略级单币种并发和开放风险上限")

    ema_config_path = ROOT / "strategies" / "ema_altcoin_long" / "config" / "strategy.json"
    try:
        ema_config = json.loads(ema_config_path.read_text())
        ema_runtime = ema_config.get("runtime", {})
        if ema_runtime.get("scope") != "dynamic.hotAltcoins":
            fail(errors, "EMA 策略实验室运行范围必须是 dynamic.hotAltcoins")
        if ema_runtime.get("max_concurrent_positions") != 1 or ema_runtime.get("max_open_risk_pct") != 0.5:
            fail(errors, "EMA 策略实验室必须限制为单币种和 0.5% 开放风险")
        if ema_config.get("portfolio", {}).get("leverage") != 2.0:
            fail(errors, "EMA 策略实验室杠杆必须为 2 倍")
        if ema_config.get("universe", {}).get("one_active_symbol_per_strategy") is not True:
            fail(errors, "EMA 策略实验室未声明策略实例只允许一个活动币种")
    except Exception as exc:
        fail(errors, f"无法读取 EMA 策略配置：{exc}")

    # 运行时默认参数必须与实验室 config 的 signal_parameters 逐项一致。
    def swift_default_parameters(case_label: str) -> dict[str, float]:
        # 先定位 defaultParameters 属性，再在它内部找对应的 case 分支，
        # 否则会匹配到 labDirectory/displayName 里的同名 case。
        body = domain[domain.index("public var defaultParameters"):]
        start = body.index(case_label)
        tail = body[start:]
        end = tail.find("\n        case ")
        block = tail[: end if end != -1 else len(tail)]
        return {key: float(value.replace("_", "")) for key, value in re.findall(r'"([A-Za-z0-9]+)":\s*(-?[0-9_.]+)', block)}

    def compare_parameters(label: str, lab: dict, swift: dict, mapping: dict) -> None:
        for lab_key, (swift_key, scale) in mapping.items():
            if lab_key not in lab:
                continue
            want = float(lab[lab_key]) * scale
            got = swift.get(swift_key)
            if got is None:
                fail(errors, f"{label} 运行时默认参数缺少 {swift_key}")
            elif abs(got - want) > 1e-9:
                fail(errors, f"{label} 参数 {swift_key}={got} 与实验室 {lab_key}={lab[lab_key]} 不一致")

    parameter_checks = {
        "L": r'parameters\["L"\].*fallback:\s*10',
        "R": r'parameters\["R"\].*fallback:\s*5',
        "major_window": r'parameters\["majorWindow"\].*fallback:\s*288',
        "sweep_wait": r'parameters\["sweepWait"\].*fallback:\s*96',
        "reject_wait": r'parameters\["rejectWait"\].*fallback:\s*5',
        "resweep_wait": r'parameters\["resweepWait"\].*fallback:\s*12',
        "rsi_min": r'parameters\["rsiMin"\].*\?\?\s*62',
        "volume_multiple": r'parameters\["volMult"\].*\?\?\s*1\.5',
        "resweep_deep_atr": r'parameters\["rsDeep"\].*\?\?\s*0\.2',
        "atr_period": r'parameters\["atrPeriod"\].*fallback:\s*14',
        "buffer_atr": r'parameters\["bufATR"\].*\?\?\s*0\.5',
        "take_profit_r": r'parameters\["tpMult"\].*\?\?\s*2\.2',
        "min_atr_pct": r'parameters\["minATRPct"\].*\?\?\s*0\.5',
        "max_risk_atr": r'parameters\["maxRiskATR"\].*\?\?\s*5\.0',
    }
    for name, pattern in parameter_checks.items():
        if name in signal and not re.search(pattern, engine):
            fail(errors, f"参数 {name} 未在 StrategyEngine 中保持映射")

    if "rsiWilder" not in engine:
        fail(errors, "扫顶策略未使用实验室定义的 Wilder RSI")
    if "sweep_reversal_short v1.3" not in engine:
        fail(errors, "StrategyEngine 未声明对应的策略实验室版本 1.3")
    if "evaluateWithConfirmation" not in engine or "confirmationWindowMinutes" not in engine:
        fail(errors, "StrategyEngine 未接入 15m 收盘确认 API")
    # 确认窗口的时间基：K 线时间戳是开盘时间，窗口必须从结构 bar 收盘时刻起算。
    if config.get("candle_timestamp") != "bar_open_time":
        fail(errors, "config 未声明 candle_timestamp = bar_open_time")
    if config.get("confirmation_window_origin") != "structure_bar_close":
        fail(errors, "config 未声明 confirmation_window_origin = structure_bar_close")
    if config.get("confirmation_window_bars") != [60, 75, 90, 105]:
        fail(errors, "config 未声明结构收盘后的 15m 确认窗口为 +60/+75/+90/+105 分钟")
    if not re.search(r'entryTimeframeMinutes"\s*\]\s*\?\?\s*60', engine):
        fail(errors, "StrategyEngine 未读取 entryTimeframeMinutes 作为结构周期")
    if "let windowStart = setup.timestamp.addingTimeInterval(structureMinutes * 60)" not in engine:
        fail(errors, "确认窗口起点不是结构 bar 收盘时刻（时间戳 + 结构周期）")
    if not re.search(r"\$0\.timestamp\s*>=\s*windowStart\s*&&\s*\$0\.timestamp\s*<\s*deadline", engine):
        fail(errors, "确认窗口不是以结构收盘为起点的半开区间 [windowStart, deadline)")
    if not any("entryTimeframeMinutes" in source for source in (stream, service_source, domain)):
        fail(errors, "运行时未把 entryTimeframeMinutes 写入策略参数")
    for document, label in ((strategy_doc, "STRATEGY.md"), (spec_doc, "STRATEGY_SPEC.md")):
        if "开盘时间" not in document:
            fail(errors, f"{label} 未声明 K 线时间戳为开盘时间")
        if "+60" not in document:
            fail(errors, f"{label} 未声明确认窗口为结构收盘后的 +60/+75/+90/+105 分钟")
    if not re.search(r"lastIndex\(where:\s*\{\s*\$0\.timestamp\s*<=", engine):
        fail(errors, "BTC 门控未按信号时刻选择最近一根历史 bar")
    if "confirmedBTC.count >= 200" not in engine:
        fail(errors, "BTC 门控未执行 200 根历史暖机")
    if "index >= 199" not in engine and "idx >= 199" not in engine:
        fail(errors, "BTC 门控未拒绝信号时刻之前不足 200 根历史 bar")
    if config.get("regime_gate", {}).get("minimum_history_bars") != 200:
        fail(errors, "策略配置未声明 BTC 门控需要 200 根历史 K 线")
    if not re.search(r"len\(c\) < 200 or idx < 199", live_signal):
        fail(errors, "实时研究扫描器未在 BTC 信号时刻不足 200 根时关闭门控")
    if not re.search(r"btc_idx\s*=\s*int\(np\.searchsorted\(btc_d\[\"t\"\].*side=\"right\"\)\s*-\s*1\)", live_signal):
        fail(errors, "实时研究扫描器未按最近一根不晚于信号的 BTC K 线对齐")
    if "if k1 + 1 <= resweepEnd" not in engine:
        fail(errors, "二次扫顶窗口缺少空范围保护")
    # 评估幂等与标的隔离：冷却必须按已确认 K 线推进，状态必须按 (策略, 标的) 隔离。
    if "lastEvaluatedBar" not in engine or "latestBar <= lastEvaluated" not in engine:
        fail(errors, "StrategyEngine 未按已确认 K 线做幂等评估（冷却会被 REST 刷新消耗）")
    if "lastEvaluatedBar" not in domain or "func evaluated(at" not in domain:
        fail(errors, "StrategyStatus 未记录 lastEvaluatedBar / 缺少 evaluated(at:) 标记方法")
    if not re.search(r"statusesByInstrument\[config\.id\]\?\[snapshot\.instrumentID\]", service_source):
        fail(errors, "1h 结构事件未返回该标的自己的状态（可能跨标的取用信号）")
    if re.search(r"if let current = statuses\[config\.id\] \{ evaluatedStatuses\.append\(current\) \}", service_source):
        fail(errors, "1h 结构事件仍在返回全局汇总状态")
    if "status(for: config.id, instrumentID: instrumentID)?.lastSignal?.id == signal.id" not in service_source:
        fail(errors, "提交前未校验信号归属（跨标的信号可能被下单）")
    if "Task.sleep(for: .seconds(30))" not in stream:
        fail(errors, "策略动态范围刷新周期未保持 30 秒")
    if "interval: .fifteenMinutes" not in stream:
        fail(errors, "后台未为扫顶策略订阅 15m 确认 K 线")
    if not re.search(r"--pool.*default=\"hot20\"", live_signal):
        fail(errors, "实验室实时扫描器默认池不是 hot20")
    # HLSR 的机器参数必须来自 config/strategy.json：此前 generator/backtest 各自
    # 硬编码 40%/3000万/冷却16/杠杆2.0/分批30-30-40，改配置没有任何效果。
    hlsr_lab = ROOT / "strategies" / "hlsr"
    if not (hlsr_lab / "config" / "strategy.json").is_file():
        fail(errors, "HLSR 缺少 config/strategy.json 机器参数真源")
    for name in ("high_short_strategy.py", "hlsr_signal_generator.py", "hlsr_market_export.py"):
        source = (hlsr_lab / "src" / name).read_text()
        if "load_lab_config" not in source:
            fail(errors, f"hlsr/src/{name} 未读取 config/strategy.json")
        # 只针对"判定/记账处"的硬编码：兜底默认值集中在 high_short_strategy 里，
        # 不参与任何判定。
        for literal in ("<= 30_000_000", "> 30_000_000", "<= 0.40", "* 1.40",
                        "cooldown = confirmation_index + 16", "leverage=2.0"):
            if literal in source:
                fail(errors, f"hlsr/src/{name} 仍在判定处硬编码 {literal}（应取自配置）")
    if "def acceptance_criteria" not in (hlsr_lab / "src" / "high_short_strategy.py").read_text():
        fail(errors, "HLSR 缺少唯一的接受标准实现 acceptance_criteria")
    market = (hlsr_lab / "src" / "hlsr_market_export.py").read_text()
    if "parameter_grid(" not in market or "export_grid" in market:
        fail(errors, "HLSR 的市场导出与回测未共用同一套参数网格")
    # 分层证据必须能由已上线规则复现：静态分层 + 因果（按入场时点排名）两个入口，
    # 规范里要指向产物，否则旧的 1h 基线数字会被当成现行结论。
    report_rule = (LAB / "research" / "report_live_rule.py").read_text()
    for flag in ("--by-tier", "--rolling-rank"):
        if flag not in report_rule:
            fail(errors, f"report_live_rule.py 缺少 {flag} 分层入口")
    spec_text = (LAB / "research" / "STRATEGY_SPEC.md").read_text()
    for artifact in ("live_rule_report_tiers.json", "live_rule_report_rolling_rank.json"):
        if artifact not in spec_text:
            fail(errors, f"STRATEGY_SPEC 未指向分层产物 {artifact}")
    if "universe.json" not in live_signal or "EXCLUDED_BASES" not in live_signal:
        fail(errors, "实验室实时扫描器未复用 universe.json 的排除清单")
    if "def _load_signal_parameters" not in live_signal or "strategy.json" not in live_signal:
        fail(errors, "实验室实时扫描器未从 config/strategy.json 读取机器参数")
    # 研究脚本不得各自硬编码一套参数：必须从 config/strategy.json 派生，否则改了
    # 规则而对照实验不跟着变，证据会与规则脱钩。
    for script in ("engine.py", "tune_execution.py", "report_live_rule.py", "final_report.py",
                   "liquidity_tiers.py", "fusion.py"):
        source = (LAB / "research" / script).read_text()
        if script == "engine.py":
            if "def lab_parameters" not in source:
                fail(errors, "engine.py 缺少 lab_parameters 参数真源入口")
        elif "E.lab_parameters()" not in source:
            fail(errors, f"research/{script} 未从 config/strategy.json 派生参数")
    # 上线规则的绩效证据必须存在，门控必须 fail-closed（唯一实现在 engine.py 里），
    # 且文档口径要与报告一致。
    evidence_path = LAB / "research" / "report_live_rule.py"
    engine_lab_path = LAB / "research" / "engine.py"
    if not evidence_path.exists():
        fail(errors, "缺少上线规则绩效证据脚本 research/report_live_rule.py")
    else:
        evidence = evidence_path.read_text()
        if "btc_gate_flags" not in evidence:
            fail(errors, "绩效证据脚本未使用共享的 fail-closed BTC 门控 engine.btc_gate_flags")
    engine_lab = engine_lab_path.read_text() if engine_lab_path.exists() else ""
    if "def btc_gate_flags" not in engine_lab:
        fail(errors, "engine.py 缺少 fail-closed 的 btc_gate_flags 实现")
    elif "idx >= min_history - 1" not in engine_lab:
        fail(errors, "btc_gate_flags 不是 fail-closed（BTC 历史不足时未判定为不通过）")
    report_path = LAB / "results" / "live_rule_report.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
            full = report.get("full", {})
            declared = config.get("backtest", {})
            comparisons = (
                ("trades", "trades"),
                ("win_rate", "win_rate"),
                ("expectancy_R_per_trade", "expect_R"),
                ("max_drawdown_R", "max_drawdown_R"),
            )
            for declared_key, report_key in comparisons:
                want = declared.get(declared_key)
                got = full.get(report_key)
                if want is None or got is None:
                    fail(errors, f"策略配置/报告缺少 {declared_key}")
                    continue
                if abs(float(want) - float(got)) > 0.01:
                    fail(errors, f"config.backtest.{declared_key}={want} 与 results/live_rule_report.json 的 {got} 不一致")
        except Exception as exc:  # pragma: no cover - diagnostic path
            fail(errors, f"无法读取 live_rule_report.json：{exc}")
    for document, label in ((strategy_doc, "STRATEGY.md"), (spec_doc, "STRATEGY_SPEC.md")):
        if "fail-closed" not in document and "fail_closed" not in document:
            fail(errors, f"{label} 未声明 BTC 门控的 fail-closed 语义")
        if "report_live_rule.py" not in document:
            fail(errors, f"{label} 未指向唯一绩效证据脚本 report_live_rule.py")

    for document, label in ((strategy_doc, "STRATEGY.md"), (spec_doc, "STRATEGY_SPEC.md")):
        if "dynamic.hotAltcoins" not in document or "前 20" not in document:
            fail(errors, f"{label} 未明确动态热门榜前 20 运行范围")
        if "177" not in document or "历史" not in document:
            fail(errors, f"{label} 未把 177 币标记为历史回测基线")
        if "历史不足 200 根" not in document or not ("关闭门控" in document or "门控关闭" in document):
            fail(errors, f"{label} 未明确 BTC 历史不足时关闭门控")
        if "15m" not in document or "市价" not in document:
            fail(errors, f"{label} 未明确 15m 收盘确认和市价入场")
        if "每个入场订单" not in document or "授权时" not in document or "策略资金池权益" not in document:
            fail(errors, f"{label} 未明确单笔风险的订单范围和资金池基准")
        if "未实现浮盈" not in document or "已实现盈亏" not in document or "滚仓" not in document:
            fail(errors, f"{label} 未明确资金池滚仓和浮盈不可释放规则")
    if not ("执行边界" in strategy_doc or "执行状态" in strategy_doc) or "保护单" not in strategy_doc:
        fail(errors, "STRATEGY.md 未明确当前执行层边界")

    # HLSR（高位扫顶反转）是独立的 15m + 4H 策略。实验室规则仍然是
    # 唯一真源，但接入后必须由领域层、信号引擎、服务和前台共同注册；
    # 这里检查集成契约，避免只改了名称或只加了一个 UI 选项。
    hlsr_lab = ROOT / "strategies" / "hlsr"
    hlsr_config_path = hlsr_lab / "config" / "strategy.json"
    try:
        hlsr_config = json.loads(hlsr_config_path.read_text())
    except Exception as exc:
        fail(errors, f"无法读取 HLSR config/strategy.json：{exc}")
        hlsr_config = {}
    if hlsr_config:
        expected_metadata = {
            "strategy": "HLSR",
            "version": "1.0",
            "name_zh": "高位扫顶反转",
            "display_name": "高位扫顶反转做空",
            "name_en": "High-Level Liquidity Sweep Reversal",
            "source_of_truth": "strategies/hlsr/STRATEGY.md",
            "status": "implemented_paper_demo",
            "runtime_integration": "implemented_paper_demo",
        }
        for key, want in expected_metadata.items():
            if hlsr_config.get(key) != want:
                fail(errors, f"HLSR config.{key} 必须是 {want!r}")
        runtime = hlsr_config.get("runtime", {})
        runtime_expectations = {
            "strategy_type": "hlsr",
            "scope": "dynamic.hotAltcoins",
            "universe_refresh_seconds": 30,
            "evaluation_interval": "confirmed_15m_close",
            "higher_timeframe": "4H",
            "higher_timeframe_minimum_history_bars": 55,
            "entry_order": "next_15m_open_market",
            "max_concurrent_positions": 1,
            "one_position_per_symbol": True,
            "one_active_symbol_per_strategy": True,
            "live_order_mode": "okx_demo_only",
            "auto_submit_live_orders": False,
            "quote_volume_required": True,
            "quote_volume_field": "volCcyQuote",
        }
        for key, want in runtime_expectations.items():
            if runtime.get(key) != want:
                fail(errors, f"HLSR runtime.{key} 必须是 {want!r}")
        if hlsr_config.get("entry_timeframe_minutes") != 15 or hlsr_config.get("confirmation_timeframe_minutes") != 15:
            fail(errors, "HLSR 必须使用 15m 入场和确认周期")
        if hlsr_config.get("higher_timeframe_minutes") != 240:
            fail(errors, "HLSR 高周期必须是 4H（240 分钟）")
        if hlsr_config.get("candle_timestamp") != "bar_open_time" or hlsr_config.get("confirmation_window_origin") != "sweep_bar_close":
            fail(errors, "HLSR 未声明 K 线开盘时间戳和扫顶 bar 收盘后的确认窗口")
        hlsr_position = hlsr_config.get("position_management", {})
        for key, want in {
            "leverage": 2.0,
            "partial_targets": [0.3, 0.3, 0.4],
            "move_stop_to_entry_after_tp1": True,
            "cooldown_bars": 16,
            "risk_per_trade_pct": 0.5,
            "risk_per_trade_max_pct": 5.0,
            "max_concurrent_positions": 1,
            "max_open_risk_percent": 5.0,
        }.items():
            if hlsr_position.get(key) != want:
                fail(errors, f"HLSR position_management.{key} 未同步规则真源")
        # Numeric defaults are copied into StrategyType.defaultParameters so
        # new instances use the same machine source without reading the lab at
        # runtime. Compare every scalar that participates in signal or exit
        # evaluation; the three fractions are checked above as a list.
        try:
            hlsr_defaults = swift_default_parameters("case .hlsr:")
            hlsr_lab_values = {
                "entry_timeframe_minutes": hlsr_config.get("entry_timeframe_minutes"),
                "higher_timeframe_minutes": hlsr_config.get("higher_timeframe_minutes"),
                **hlsr_config.get("hard_filters", {}),
                **hlsr_config.get("signal_parameters", {}),
                "leverage": hlsr_position.get("leverage"),
                "move_stop_to_entry_after_tp1": hlsr_position.get("move_stop_to_entry_after_tp1"),
                "cooldown_bars": hlsr_position.get("cooldown_bars"),
                "fee_rate_one_way": hlsr_config.get("costs", {}).get("fee_rate_one_way"),
                "slippage": hlsr_config.get("costs", {}).get("slippage"),
                "funding_rate": hlsr_config.get("costs", {}).get("funding_rate"),
                "max_open_risk_percent": hlsr_position.get("max_open_risk_percent"),
            }
            hlsr_mapping = {
                "entry_timeframe_minutes": ("entryTimeframeMinutes", 1),
                "higher_timeframe_minutes": ("structureTimeframeMinutes", 1),
                "gain_24h_gt": ("gain24hGt", 1),
                "quote_volume_24h_gt": ("quoteVolume24hGt", 1),
                "swing_lookback": ("swingLookback", 1),
                "wick_ratio": ("wickRatio", 1),
                "volume_multiple": ("volumeMultiple", 1),
                "minimum_rejection_score": ("minimumRejectionScore", 1),
                "reject_depth_atr": ("rejectDepthATR", 1),
                "confirmation_window": ("confirmationWindow", 1),
                "stop_atr": ("stopATR", 1),
                "trail_bars": ("trailBars", 1),
                "allow_range": ("allowRange", 1),
                "leverage": ("leverage", 1),
                "move_stop_to_entry_after_tp1": ("moveStopToEntryAfterTP1", 1),
                "cooldown_bars": ("cooldownBars", 1),
                "fee_rate_one_way": ("feeRateOneWay", 1),
                "slippage": ("slippage", 1),
                "funding_rate": ("fundingRate", 1),
                "max_open_risk_percent": ("maxOpenRiskPercent", 1),
            }
            compare_parameters("HLSR", hlsr_lab_values, hlsr_defaults, hlsr_mapping)
        except (ValueError, IndexError) as exc:
            fail(errors, f"无法读取 StrategyType.hlsr 默认参数：{exc}")
        if hlsr_config.get("validation", {}).get("research_status") != "FAIL":
            fail(errors, "HLSR 必须保留样本外研究 FAIL 局限")

        hlsr_manager_path = ROOT / "Sources" / "TradingService" / "HLSRPositionManager.swift"
        if not hlsr_manager_path.is_file():
            fail(errors, "HLSR 缺少 Sources/TradingService/HLSRPositionManager.swift 退出状态机")
            hlsr_manager = ""
        else:
            hlsr_manager = hlsr_manager_path.read_text()
        hlsr_sources = (domain, engine, service_source, main_source, stream, hlsr_manager)
        hlsr_runtime_sources = "\n".join(hlsr_sources)
        if not re.search(r"case\s+\.hlsr|\.hlsr\b", hlsr_runtime_sources):
            fail(errors, "StrategyType.availableCases 未注册 HLSR（hlsr）")
        if not re.search(r"evaluate(?:HLSR|Hlsr|HighShort)", engine):
            fail(errors, "StrategyEngine 未接入 HLSR 信号评估入口")
        if "高位扫顶反转做空" not in main_source or "高位扫顶反转做空" not in domain:
            fail(errors, "HLSR 中文显示名未同步到领域层和前台")
        if "High-Level Liquidity Sweep Reversal" not in domain + main_source:
            fail(errors, "HLSR 英文稳定名称未保留在运行时元数据")
        if "fifteenMinutes" not in hlsr_runtime_sources or "fourHours" not in hlsr_runtime_sources:
            fail(errors, "HLSR 运行时未同时订阅 15m 和 4H 数据")
        if "quoteVolume" not in hlsr_runtime_sources and "volCcyQuote" not in hlsr_runtime_sources:
            fail(errors, "HLSR 运行时未接入报价成交额字段（quoteVolume/volCcyQuote）")
        if "config.type == .hlsr ? nil" in service_source and "config.type == .hlsr ||" not in service_source:
            fail(errors, "HLSR 三段目标由监控管理时，入场下单仍被单一 takePrice guard 错误拒绝")
        if not re.search(r"55|minimum.*History.*4H|higher.*History", hlsr_runtime_sources, re.IGNORECASE):
            fail(errors, "HLSR 运行时未执行 4H 至少 55 根历史暖机")
        if "partial" not in hlsr_runtime_sources.lower() or not re.search(r"break.?even|stop.?to.?entry|保本", hlsr_runtime_sources, re.IGNORECASE):
            fail(errors, "HLSR 运行时未接入分批目标和 TP1 后保本")
        if not re.search(r"trail|跟踪", hlsr_runtime_sources, re.IGNORECASE):
            fail(errors, "HLSR 运行时未接入 TP2 后跟踪止损")
        if "cooldown" not in hlsr_runtime_sources.lower() or "16" not in hlsr_runtime_sources:
            fail(errors, "HLSR 运行时未接入 16 根 15m 冷却")
        if "struct HLSRPositionManager" not in hlsr_manager or "PendingReduceOnlyLeg" not in hlsr_manager:
            fail(errors, "HLSR 退出状态机未持久化 reduce-only leg 状态")
        for name in ("high_short_strategy.py", "hlsr_signal_generator.py", "hlsr_market_export.py"):
            source = (hlsr_lab / "src" / name).read_text()
            if "load_lab_config" not in source and "LabConfig.load" not in source:
                fail(errors, f"hlsr/src/{name} 未读取 config/strategy.json")

        hlsr_doc = (hlsr_lab / "STRATEGY.md").read_text()
        hlsr_readme = (hlsr_lab / "README.md").read_text()
        for document, label in ((hlsr_doc, "hlsr/STRATEGY.md"), (hlsr_readme, "hlsr/README.md")):
            if "高位扫顶反转" not in document or "高位扫顶反转做空" not in document:
                fail(errors, f"{label} 未统一 HLSR 中文名称")
            if "paper" not in document and "模拟盘" not in document:
                fail(errors, f"{label} 未说明 HLSR 纸面/模拟盘执行边界")
            if "FAIL" not in document:
                fail(errors, f"{label} 未保留研究 FAIL 局限")

    if errors:
        print("策略实验室同步检查失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print("策略实验室与运行时代码同步检查通过（sweep_reversal_short v1.3 + HLSR 1.0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
