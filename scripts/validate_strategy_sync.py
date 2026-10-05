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
UNIVERSE_RULES_PATH = ROOT / "Sources" / "TradingDomain" / "StrategyUniverseRules.swift"
SIGNAL_PATH = LAB / "research" / "live_signal.py"
RUNTIME_STRATEGY_DIRS = {"sweep_reversal_short"}
# 运行时策略共用的仓位契约（与 StrategyType.notionalPoolMultiple /
# maxStopDistancePercent 和 RiskLimits 默认日损熔断对应）。
MAX_NOTIONAL_POOL_MULTIPLE = 1.0
MAX_STOP_DISTANCE_PCT = 15.0
ACCOUNT_DAILY_LOSS_PCT = 5.0
MAX_CAPITAL_POOL_PCT = 33.33


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
    universe_rules = UNIVERSE_RULES_PATH.read_text()
    live_signal = SIGNAL_PATH.read_text()
    strategy_doc = (LAB / "STRATEGY.md").read_text()
    spec_doc = (LAB / "research" / "STRATEGY_SPEC.md").read_text()

    if config.get("version") != "1.4":
        fail(errors, "config/strategy.json 不是规则版本 1.4")
    if not re.search(r"版本：1\.4", strategy_doc):
        fail(errors, "STRATEGY.md 未标记规则版本 1.4")
    if "规则说明书 v1.4" not in spec_doc:
        fail(errors, "STRATEGY_SPEC.md 未标记规则版本 1.4")
    if "实时信号扫描器 v1.4" not in live_signal:
        fail(errors, "研究实时信号扫描器未标记规则版本 1.4")
    if config.get("source_of_truth") != "strategies/sweep_reversal_short/STRATEGY.md":
        fail(errors, "config 未声明 STRATEGY.md 为规则真源")
    if runtime.get("mode") != "dynamic" or runtime.get("category") != "sweepCandidates":
        fail(errors, "运行范围必须是 dynamic.sweepCandidates")
    if runtime.get("limit") != 100 or runtime.get("refresh_seconds") != 30:
        fail(errors, "运行范围必须是每 30 秒刷新、上限 100")
    if runtime.get("min_quote_volume_24h_usdt") != 3_000_000:
        fail(errors, "运行范围的 24h 报价成交额下限必须是 300 万 USDT")
    if config.get("universe", {}).get("production_scope") != "dynamic.sweepCandidates":
        fail(errors, "config.universe.production_scope 必须是 dynamic.sweepCandidates")
    if integration.get("signal_generation") != "implemented":
        fail(errors, "实验室未声明信号生成已同步")
    if config.get("entry_timeframe_minutes") != 60 or config.get("confirmation_timeframe_minutes") != 15:
        fail(errors, "生产执行周期必须是 1h 结构 + 15m 确认")
    if config.get("confirmation_window_minutes") != 60:
        fail(errors, "15m 确认窗口必须为 60 分钟")
    if config.get("entry_submission") != "market" or integration.get("entry_submission") != "okx_account_mode_market_order":
        fail(errors, "策略入场必须声明为按账户类型路由的市价提交")
    if integration.get("order_routing") != "account_mode":
        fail(errors, "策略入场路由必须由账户类型决定")
    if "first confirmed 15m close" not in config.get("confirmation_rule", ""):
        fail(errors, "策略配置未声明首根符合条件的 15m 收盘确认")
    if integration.get("protective_exit_orders") != "implemented":
        fail(errors, "策略实验室未声明条件保护单已接入")
    if integration.get("full_pool_position_sizing") != "implemented":
        fail(errors, "策略实验室未声明全池名义仓位 sizing 已接入")
    if integration.get("stop_distance_cap") != "implemented":
        fail(errors, "策略实验室未声明止损距离上限已接入")
    if integration.get("circuit_breaker_headroom_check") != "implemented":
        fail(errors, "策略实验室未声明熔断阈值余量校验已接入")
    if integration.get("strategy_time_exit") != "implemented":
        fail(errors, "策略实验室未声明时间离场已接入")
    if integration.get("account_daily_loss_circuit_breaker") != "implemented":
        fail(errors, "策略实验室未声明账户级日损熔断已接入")
    position_management = config.get("position_management", {})
    if position_management.get("leverage") != 2.0:
        fail(errors, "扫顶策略实验室杠杆必须为 2 倍")
    if position_management.get("max_concurrent_positions") != 1:
        fail(errors, "策略实验室必须限制策略实例同时只持有一个币种")
    # 仓位契约：每笔用满资金池可用余额（名义倍数 1），止损距离 > 15% 不下单；
    # 没有 risk_per_trade_pct / 开放风险上限这类按止损距离反推的预算。
    if position_management.get("sizing") != "full_pool_available_capital":
        fail(errors, "策略实验室 sizing 必须是 full_pool_available_capital")
    if position_management.get("notional_pool_multiple") != MAX_NOTIONAL_POOL_MULTIPLE:
        fail(errors, f"策略实验室名义倍数必须为 {MAX_NOTIONAL_POOL_MULTIPLE}")
    if position_management.get("sizing_formula") != "pool_available_capital * notional_pool_multiple":
        fail(errors, "策略实验室 sizing 公式必须是 pool_available_capital * notional_pool_multiple")
    if position_management.get("max_stop_distance_pct") != MAX_STOP_DISTANCE_PCT:
        fail(errors, f"策略实验室止损距离上限必须为 {MAX_STOP_DISTANCE_PCT}%")
    if position_management.get("max_loss_per_trade_pct_of_pool") != MAX_STOP_DISTANCE_PCT * MAX_NOTIONAL_POOL_MULTIPLE:
        fail(errors, "策略实验室单笔最坏亏损必须等于止损距离上限 × 名义倍数")
    if position_management.get("leverage_effect") != "margin_only":
        fail(errors, "策略实验室必须声明杠杆只影响保证金占用")
    for legacy in ("risk_per_trade_pct", "risk_per_trade_max_pct", "max_open_risk_percent", "risk_per_trade_basis", "risk_budget_costs_included"):
        if legacy in position_management:
            fail(errors, f"策略实验室仍保留已废弃的按止损距离 sizing 字段 {legacy}")
    risk_policy = config.get("risk_policy", {})
    required_actions = {
        "stop_all_strategies",
        "cancel_entry_orders",
        "close_all_positions_reduce_only",
        "latch_until_manual_reset",
    }
    if (
        risk_policy.get("account_daily_loss_percent") != ACCOUNT_DAILY_LOSS_PCT
        or risk_policy.get("baseline") != "calendar_day_start_equity"
        or risk_policy.get("measurement") != "mark_to_market"
        or not required_actions.issubset(set(risk_policy.get("actions", [])))
    ):
        fail(errors, f"账户级日损熔断必须是 mark-to-market 的 {ACCOUNT_DAILY_LOSS_PCT:g}%")
    if risk_policy.get("max_capital_pool_percent") != MAX_CAPITAL_POOL_PCT:
        fail(errors, f"策略资金池上限必须同步为 {MAX_CAPITAL_POOL_PCT:g}%")
    derived_pool_cap = (ACCOUNT_DAILY_LOSS_PCT / (MAX_STOP_DISTANCE_PCT * MAX_NOTIONAL_POOL_MULTIPLE)) * 100
    if derived_pool_cap <= MAX_CAPITAL_POOL_PCT:
        # The persisted two-decimal cap must leave a strict margin below the
        # fixed account breaker after rounding.
        fail(errors, "资金池上限必须严格低于账户日损熔断 ÷ 单笔最坏亏损")
    capital_pool = config.get("capital_pool", {})
    if (
        capital_pool.get("mode") != "per_strategy_instance"
        or capital_pool.get("cross_pool_borrowing") is not False
        or capital_pool.get("rollover") != "compound_realized_pnl"
        or capital_pool.get("unrealized_pnl_releases_available") is not False
    ):
        fail(errors, "策略资金池必须按实例隔离、只用已实现盈亏滚仓且禁止浮盈释放或跨池借用")
    if capital_pool.get("allocation_percent_default") != MAX_CAPITAL_POOL_PCT:
        fail(errors, f"策略资金池默认比例必须同步为 {MAX_CAPITAL_POOL_PCT:g}%")

    if not re.search(r"availableCases\s*:.*\.sweepReversalShort", domain, re.DOTALL):
        fail(errors, "StrategyType.availableCases 未包含全部已集成策略")
    if re.search(r"emaAltcoin|双均线交易山寨币做多", "".join(
            path.read_text() for path in sorted((ROOT / "Sources").rglob("*.swift")))):
        fail(errors, "运行时代码仍包含已下线的 EMA 山寨币做多策略")
    if ".prefix(20)" not in domain and "prefix(limit)" not in domain:
        fail(errors, "StrategyScope.hotAltcoins 未按动态成交额前 20 实现")
    if "StrategyUniverseRules.isEligibleHotAltcoin" not in domain:
        fail(errors, "StrategyScope.hotAltcoins 未使用统一资产类别过滤")
    if not re.search(r"case \.sweepReversalShort: return \.sweepCandidates", domain):
        fail(errors, "sweepReversalShort 的默认范围不是 sweepCandidates")
    if "prefix(StrategyUniverseRules.sweepCandidateLimit)" not in domain or "StrategyUniverseRules.isEligibleSweep" not in domain:
        fail(errors, "StrategyScope.sweepCandidates 未按统一上限与成交额下限实现")
    limit_match = re.search(r"sweepCandidateLimit\s*=\s*([0-9_]+)", universe_rules)
    floor_match = re.search(r"sweepMinimumQuoteVolume24h\s*:\s*Decimal\s*=\s*([0-9_]+)", universe_rules)
    if not limit_match or int(limit_match.group(1).replace("_", "")) != runtime.get("limit"):
        fail(errors, "StrategyUniverseRules.sweepCandidateLimit 与 config.runtime_universe.limit 不一致")
    if not floor_match or int(floor_match.group(1).replace("_", "")) != runtime.get("min_quote_volume_24h_usdt"):
        fail(errors, "StrategyUniverseRules.sweepMinimumQuoteVolume24h 与 config 的成交额下限不一致")
    if not re.search(r"func isEligibleSweep\(_ contract: ContractMarket\) -> Bool \{\s*isEligibleHotAltcoin\(contract\)\s*&& contract\.volume24h >= sweepMinimumQuoteVolume24h", universe_rules):
        fail(errors, "isEligibleSweep 未复用统一资产类别过滤并施加成交额下限")
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

    for strategy in ("ema_3line_pullback",):
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
    main_source = "".join(path.read_text() for path in sorted((ROOT / "Sources" / "MacTraderApp").glob("*.swift")))
    service_source = (ROOT / "Sources" / "TradingService" / "TradingService.swift").read_text()
    if re.search(r"case\s+trendFollowing|case\s+rsiReversal|\.trendFollowing|\.rsiReversal", domain + engine + service_source + main_source):
        fail(errors, "运行时代码仍包含已清理的演示策略入口")
    if "scope: selectedRule.defaultScope" not in main_source:
        fail(errors, "新建策略表单未使用策略类型对应的动态标的范围")
    if "ForEach(StrategyType.availableCases" not in main_source:
        fail(errors, "新建策略表单未从可用策略规则列表选择规则")
    if "config.scope.mode == .dynamicCategory" not in service_source or "config.type.defaultScope" not in service_source:
        fail(errors, "后端未校验并 canonicalize 策略类型对应的动态标的范围")
    if "pool.openPositions < maxConcurrent" not in service_source or "已有其他币种的挂单或持仓" not in service_source:
        fail(errors, "后端未限制策略实例同时只允许一个活动币种")
    if "selectedRule.displayName" not in main_source:
        fail(errors, "新建策略表单未显示领域层的策略名称")
    if "isEligibleHotAltcoin" not in service_source:
        fail(errors, "后台热门榜未使用统一资产类别过滤")
    if "capitalPoolPercent" not in service_source or "strategyCapital" not in service_source:
        fail(errors, "后端未接入策略资金池")
    # `maxOpenRiskPercent` remains as a read-only migration key in the
    # validator/runtime compatibility path. Rejecting its name anywhere in
    # Domain.swift incorrectly flags that migration code as an active sizing
    # contract; the actual contract is checked below from the constants and
    # target-notional implementation.
    stop_cap = re.search(r"maxStopDistancePercent:\s*Double\s*=\s*([0-9.]+)", domain)
    multiple = re.search(r"notionalPoolMultiple:\s*Double\s*=\s*([0-9.]+)", domain)
    if not stop_cap or float(stop_cap.group(1)) != MAX_STOP_DISTANCE_PCT:
        fail(errors, f"StrategyType.maxStopDistancePercent 与实验室止损距离上限 {MAX_STOP_DISTANCE_PCT}% 不一致")
    if not multiple or float(multiple.group(1)) != MAX_NOTIONAL_POOL_MULTIPLE:
        fail(errors, f"StrategyType.notionalPoolMultiple 与实验室名义倍数 {MAX_NOTIONAL_POOL_MULTIPLE} 不一致")
    if "let targetNotional = pool.availableCapital * Decimal(StrategyType.notionalPoolMultiple)" not in service_source:
        fail(errors, "后端未按资金池可用余额 × 名义倍数 sizing")
    if "stopDistancePercent <= Decimal(StrategyType.maxStopDistancePercent)" not in service_source:
        fail(errors, "后端未执行止损距离上限")
    if "func requireCircuitBreakerHeadroom" not in service_source or "StrategyType.maxLossPerTradePercent" not in service_source:
        fail(errors, "后端未在启用策略时校验资金池与固定熔断阈值")
    if "maxCapitalPoolPercent" not in domain or "maxCapitalPoolPercent" not in service_source:
        fail(errors, "领域层和后端未同步策略资金池安全上限")
    limits_default = re.search(r"maxDailyLossPercent:\s*Decimal\s*=\s*([0-9.]+),\s*maxDrawdownPercent:\s*Decimal\s*=\s*([0-9.]+)\)", domain)
    if not limits_default or float(limits_default.group(1)) != ACCOUNT_DAILY_LOSS_PCT:
        fail(errors, f"RiskLimits 默认日损熔断与实验室 {ACCOUNT_DAILY_LOSS_PCT:g}% 不一致")
    if limits_default and float(limits_default.group(2)) != 0:
        fail(errors, "RiskLimits 默认累计回撤熔断必须关闭（无法复位）")
    if "if strategyID == nil {" not in (ROOT / "Sources" / "TradingService" / "PaperTrading.swift").read_text():
        fail(errors, "风控引擎未把账户级名义/保证金上限限定为非策略订单")
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
    if '"maxConcurrentPositions": 1' not in domain:
        fail(errors, "运行时默认参数未同步策略级单币种并发上限")

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
        "take_profit_r": r'boundedFinite\(config\.parameters\["tpMult"\].*fallback:\s*2\.2',
        "min_atr_pct": r'boundedFinite\(config\.parameters\["minATRPct"\].*fallback:\s*0\.5',
        "max_risk_atr": r'boundedFinite\(config\.parameters\["maxRiskATR"\].*fallback:\s*5\.0',
    }
    for name, pattern in parameter_checks.items():
        if name in signal and not re.search(pattern, engine):
            fail(errors, f"参数 {name} 未在 StrategyEngine 中保持映射")

    if "rsiWilder" not in engine:
        fail(errors, "扫顶策略未使用实验室定义的 Wilder RSI")
    if "sweep_reversal_short v1.4" not in engine:
        fail(errors, "StrategyEngine 未声明对应的策略实验室版本 1.4")
    if "evaluateWithConfirmation" not in engine or "confirmationWindowMinutes" not in engine:
        fail(errors, "StrategyEngine 未接入 15m 收盘确认 API")
    # 确认窗口的时间基：K 线时间戳是开盘时间，窗口必须从结构 bar 收盘时刻起算。
    if config.get("candle_timestamp") != "bar_open_time":
        fail(errors, "config 未声明 candle_timestamp = bar_open_time")
    if config.get("confirmation_window_origin") != "structure_bar_close":
        fail(errors, "config 未声明 confirmation_window_origin = structure_bar_close")
    if config.get("confirmation_window_bars") != [60, 75, 90, 105]:
        fail(errors, "config 未声明结构收盘后的 15m 确认窗口为 +60/+75/+90/+105 分钟")
    if not re.search(r'boundedFinite\(config\.parameters\["entryTimeframeMinutes"\].*fallback:\s*60', engine):
        fail(errors, "StrategyEngine 未读取 entryTimeframeMinutes 作为结构周期")
    if "let windowStart = setup.timestamp.addingTimeInterval(structureMinutes * 60)" not in engine:
        fail(errors, "确认窗口起点不是结构 bar 收盘时刻（时间戳 + 结构周期）")
    if not re.search(r"\$0\.timestamp\s*>=\s*windowStart\s*&&\s*\$0\.timestamp\s*<\s*deadline", engine):
        fail(errors, "确认窗口不是以结构收盘为起点的半开区间 [windowStart, deadline)")
    if not any("entryTimeframeMinutes" in source for source in (service_source, domain)):
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
    # The former Swift local stream loop was removed; the FastAPI backend owns
    # market streaming now. The Swift service still validates the
    # strategy's timeframe and candle handling above.
    if not re.search(r"--pool.*default=\"live\"", live_signal):
        fail(errors, "实验室实时扫描器默认池不是 live（生产选币范围）")
    if 'json.load(handle)["runtime_universe"]' not in live_signal:
        fail(errors, "实验室实时扫描器未从 config.runtime_universe 读取生产选币范围")
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
    # 全历史复核脚本必须存在，且必须通过 prep.py 的独立数据集复现（不覆盖 177 币基线）。
    full_history = LAB / "research" / "report_full_history.py"
    if not full_history.exists() or "prep.main" not in full_history.read_text():
        fail(errors, "缺少从 prep.py 独立数据集复现的全历史复核脚本 research/report_full_history.py")
    if "report_full_history.py" not in spec_text:
        fail(errors, "STRATEGY_SPEC 未指向全历史复核脚本 report_full_history.py")
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
        if "dynamic.sweepCandidates" not in document or "前 100" not in document or "300 万" not in document:
            fail(errors, f"{label} 未明确 24h 成交额前 100、≥300 万 USDT 的运行范围")
        if "177" not in document or "历史" not in document:
            fail(errors, f"{label} 未把 177 币标记为历史回测基线")
        if "历史不足 200 根" not in document or not ("关闭门控" in document or "门控关闭" in document):
            fail(errors, f"{label} 未明确 BTC 历史不足时关闭门控")
        if "15m" not in document or "市价" not in document:
            fail(errors, f"{label} 未明确 15m 收盘确认和市价入场")
        if "资金池可用余额" not in document or "15%" not in document or "止损距离" not in document:
            fail(errors, f"{label} 未明确全池名义仓位和 15% 止损距离上限")
        if f"{ACCOUNT_DAILY_LOSS_PCT:g}%" not in document or "熔断" not in document:
            fail(errors, f"{label} 未明确 {ACCOUNT_DAILY_LOSS_PCT:g}% 账户日内熔断及其余量要求")
        if "未实现浮盈" not in document or "已实现盈亏" not in document or "滚仓" not in document:
            fail(errors, f"{label} 未明确资金池滚仓和浮盈不可释放规则")
    if not ("执行边界" in strategy_doc or "执行状态" in strategy_doc) or "保护单" not in strategy_doc:
        fail(errors, "STRATEGY.md 未明确当前执行层边界")

    # All strategy packages share the current account-level risk contract and
    # must describe their eligible instruments next to the human rule source.
    # Historical result JSON is deliberately excluded: it records the risk
    # convention used when that report was produced and is not a live config.
    strategy_dirs = [
        ROOT / "strategies" / name for name in (
            "sweep_reversal_short", "intraday_pump_retest_short",
            "ema_3line_pullback", "liquid_crypto_trend_long",
        )
    ]
    for strategy_dir in strategy_dirs:
        config_path = strategy_dir / "config" / "strategy.json"
        if not config_path.is_file() and strategy_dir.name == "ema_3line_pullback":
            # This is a historical four-variant archive whose machine source
            # predates the single-strategy config schema.
            config_path = strategy_dir / "config" / "variants.json"
        doc_path = strategy_dir / "STRATEGY.md"
        if not config_path.is_file() or not doc_path.is_file():
            fail(errors, f"{strategy_dir.name} 缺少策略真源或 config/strategy.json")
            continue
        try:
            strategy_config = json.loads(config_path.read_text())
        except Exception as exc:
            fail(errors, f"无法读取 {config_path.relative_to(ROOT)}：{exc}")
            continue
        # 运行时接入的策略共用一份仓位契约：每笔用满资金池可用余额（名义倍数 1），
        # 止损距离 > 15% 不下单。纯研究目录保留各自回测时声明的口径。
        if strategy_dir.name in RUNTIME_STRATEGY_DIRS:
            block = strategy_config.get("position_management", {})
            if block.get("sizing") != "full_pool_available_capital" or block.get("notional_pool_multiple") != MAX_NOTIONAL_POOL_MULTIPLE:
                fail(errors, f"{strategy_dir.name} 必须声明全池名义仓位 sizing（倍数 {MAX_NOTIONAL_POOL_MULTIPLE}）")
            if block.get("max_stop_distance_pct") != MAX_STOP_DISTANCE_PCT:
                fail(errors, f"{strategy_dir.name} 止损距离上限必须为 {MAX_STOP_DISTANCE_PCT}%")
            for key in ("risk_per_trade_pct", "risk_per_trade_max_pct", "max_open_risk_pct", "max_open_risk_percent", "risk_per_trade_basis"):
                if key in block:
                    fail(errors, f"{strategy_dir.name} 仍保留已废弃的按止损距离 sizing 字段 {key}")
        if strategy_dir.name != "ema_3line_pullback" and not any(key in strategy_config for key in ("universe", "runtime_universe", "market")):
            fail(errors, f"{strategy_dir.name} 未声明适合标的范围")
        document = doc_path.read_text()
        if "USDT" not in document or ("山寨币" not in document and "适合标的" not in document):
            fail(errors, f"{strategy_dir.name}/STRATEGY.md 未明确适合标的")

    if errors:
        print("策略实验室同步检查失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"策略实验室与运行时代码同步检查通过（运行时策略全池仓位契约：名义 1× 资金池、止损距离 ≤15%、日损熔断 {ACCOUNT_DAILY_LOSS_PCT:g}% + sweep v1.4）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
