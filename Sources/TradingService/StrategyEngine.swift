import Foundation
import TradingDomain

public enum IndicatorCalculator {
    /// Wilder RSI used by the sweep-reversal strategy.
    public static func rsiWilder(_ values: [Double], period: Int = 14) -> [Double] {
        guard values.count > 1, period > 0 else { return values.map { _ in 50 } }
        var averageGain = 0.0
        var averageLoss = 0.0
        var result = Array(repeating: 50.0, count: values.count)
        let alpha = 1.0 / Double(period)
        for index in values.indices {
            if index > 0 {
                let delta = values[index] - values[index - 1]
                averageGain = averageGain * (1 - alpha) + max(delta, 0) * alpha
                averageLoss = averageLoss * (1 - alpha) + max(-delta, 0) * alpha
            }
            if averageLoss == 0 {
                result[index] = averageGain > 0 ? 100 : 50
            } else {
                result[index] = 100 - 100 / (1 + averageGain / averageLoss)
            }
        }
        return result
    }

    /// 简单移动平均（前 period 根用已有数据的均值，因果、与 Python 回测一致）
    public static func sma(_ values: [Double], period: Int) -> [Double] {
        guard !values.isEmpty, period > 0 else { return [] }
        var result = [Double](); result.reserveCapacity(values.count)
        var sum = 0.0
        for (index, value) in values.enumerated() {
            sum += value
            if index >= period { sum -= values[index - period] }
            result.append(sum / Double(min(index + 1, period)))
        }
        return result
    }

    /// Causal moving average of the bars *before* each index.  A signal bar
    /// must not dilute its own volume/volatility baseline (for example, a
    /// sweep volume spike should be compared with the preceding 48 bars).
    /// The first element has no history and is returned as zero.
    public static func priorSMA(_ values: [Double], period: Int) -> [Double] {
        guard !values.isEmpty, period > 0 else { return [] }
        var result = Array(repeating: 0.0, count: values.count)
        var sum = 0.0
        for index in values.indices {
            let count = min(index, period)
            if count > 0 { result[index] = sum / Double(count) }
            sum += values[index]
            if index >= period { sum -= values[index - period] }
        }
        return result
    }

    /// Causal exponential moving average used by the EMA altcoin strategy.
    public static func ema(_ values: [Double], period: Int) -> [Double] {
        guard !values.isEmpty, period > 0 else { return [] }
        let alpha = 2.0 / Double(period + 1)
        var result: [Double] = []
        result.reserveCapacity(values.count)
        for value in values {
            result.append(result.last.map { alpha * value + (1 - alpha) * $0 } ?? value)
        }
        return result
    }

    /// 滚动窗口最大值（前 period 根用已有数据的最大值）
    public static func rollingMax(_ values: [Double], period: Int) -> [Double] {
        guard !values.isEmpty, period > 0 else { return [] }
        let safePeriod = max(1, period)
        return values.indices.map { index in values[max(0, index - safePeriod + 1)...index].max() ?? values[index] }
    }

    /// SMA 版 ATR14（与 Python 回测 atr() 一致）
    public static func atrSMA(_ candles: [Candle], period: Int = 14) -> [Double] {
        guard !candles.isEmpty else { return [] }
        var trueRanges = [Double](); trueRanges.reserveCapacity(candles.count)
        for (index, candle) in candles.enumerated() {
            let high = NSDecimalNumber(decimal: candle.high).doubleValue
            let low = NSDecimalNumber(decimal: candle.low).doubleValue
            let previousClose = index > 0 ? NSDecimalNumber(decimal: candles[index - 1].close).doubleValue : NSDecimalNumber(decimal: candle.close).doubleValue
            trueRanges.append(max(high - low, max(abs(high - previousClose), abs(low - previousClose))))
        }
        return sma(trueRanges, period: period)
    }

    /// ATR with the EMA smoothing used by the formal EMA strategy.
    public static func atrEMA(_ candles: [Candle], period: Int = 14) -> [Double] {
        guard !candles.isEmpty else { return [] }
        var trueRanges = [Double](); trueRanges.reserveCapacity(candles.count)
        for (index, candle) in candles.enumerated() {
            let high = NSDecimalNumber(decimal: candle.high).doubleValue
            let low = NSDecimalNumber(decimal: candle.low).doubleValue
            let previousClose = index > 0 ? NSDecimalNumber(decimal: candles[index - 1].close).doubleValue : NSDecimalNumber(decimal: candle.close).doubleValue
            trueRanges.append(max(high - low, max(abs(high - previousClose), abs(low - previousClose))))
        }
        return ema(trueRanges, period: period)
    }
}

public struct StrategyEngine: Sendable {
    public init() {}

    // Strategy lab source: sweep_reversal_short v1.4.
    // v1.4 只把生产范围改为 `dynamic.sweepCandidates`（StrategyUniverseRules
    // 成交额前 100 且 ≥300 万 USDT），信号与出场规则和 v1.3 相同。
    // 同步记录 2026-09：v1.3 规则原文是"二次扫顶 1h bar 收盘后的后续 4 根
    // 15m"。此前实现把 K 线时间戳当作收盘时间，确认窗口错位到结构 bar 自身
    // 那一小时并漏掉 +75/+90/+105。本次只修实现的时间基，规则与参数未变
    // （见实验室 research/STRATEGY_SPEC.md §9.1 同步记录）。

    /// Indicator series stored in the status are only a bounded tail: the full
    /// window would be re-persisted and re-broadcast on every bar close.
    private static let indicatorTailLength = 120

    private static func trimmed(_ values: [Double]) -> [Double] {
        values.count > indicatorTailLength ? Array(values.suffix(indicatorTailLength)) : values
    }

    private static func period(_ value: Double?, fallback: Int) -> Int {
        guard let value, value.isFinite else { return fallback }
        if value < 1 { return 1 }
        // Keep malformed but finite values from trapping during Int conversion
        // or making a user supplied strategy unexpectedly expensive.
        return value > 10_000 ? 10_000 : Int(value)
    }

    public func evaluate(config: StrategyConfig, candles: [Candle], previous: StrategyStatus? = nil, btcCandles: [Candle]? = nil) -> StrategyStatus {
        let confirmed = candles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
        let status = previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
        guard config.type.hasRuntimeHandler else {
            return StrategyStatus(id: status.id, state: .paused, direction: status.direction,
                                  cooldown: status.cooldown, pnl: status.pnl,
                                  lastSignal: status.lastSignal, indicators: status.indicators,
                                  lastEvaluatedBar: status.lastEvaluatedBar)
        }
        // A stale status must not keep advertising a running strategy after
        // the user pauses it, including when the input bar did not change.
        if !config.enabled {
            return StrategyStatus(id: status.id, state: .paused, direction: status.direction,
                                  cooldown: status.cooldown, pnl: status.pnl,
                                  lastSignal: status.lastSignal, indicators: status.indicators,
                                  lastEvaluatedBar: status.lastEvaluatedBar)
        }
        guard confirmed.count >= 3 else { return previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused) }
        let closes = confirmed.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let atrPeriod = Self.period(config.parameters["atrPeriod"], fallback: 14)
        let rsi = IndicatorCalculator.rsiWilder(closes, period: atrPeriod)
        let atr = IndicatorCalculator.atrSMA(confirmed, period: atrPeriod)
        // 按 bar 幂等：冷却与指标只在一根**新的**已确认 K 线上推进。REST 图表刷新
        // 会用同一根 K 线重复调用本函数，若每次都递减冷却，冷却会被读操作提前烧完。
        guard let latestBar = confirmed.last?.timestamp else { return status }
        if let lastEvaluated = status.lastEvaluatedBar, latestBar <= lastEvaluated {
            return status.evaluated(at: lastEvaluated)
        }
        if let lastSignal = status.lastSignal, lastSignal.timestamp == latestBar {
            return StrategyStatus(id: status.id, state: config.enabled ? .running : .paused, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: lastSignal, indicators: status.indicators, lastEvaluatedBar: latestBar)
        }
        if status.cooldown > 0 {
            return StrategyStatus(id: status.id, state: config.enabled ? .running : .paused, direction: status.direction, cooldown: status.cooldown - 1, pnl: status.pnl, lastSignal: status.lastSignal, indicators: status.indicators, lastEvaluatedBar: latestBar)
        }
        var signal: StrategySignal?
        var direction = status.direction
        if config.type == .sweepReversalShort {
            signal = Self.evaluateSweepReversal(config: config, confirmed: confirmed, closes: closes, btcCandles: btcCandles)
            if signal != nil { direction = "short" }
        }
        let indicators: [String: [Double]] = ["rsi": Self.trimmed(rsi), "atr": Self.trimmed(atr)]
        return StrategyStatus(id: config.id, state: .running, direction: direction, cooldown: signal == nil ? max(0, status.cooldown - 1) : config.cooldownBars, pnl: status.pnl, lastSignal: signal ?? status.lastSignal, indicators: indicators, lastEvaluatedBar: latestBar)
    }

    /// Production execution path: the 1h candles establish the sweep/resweep
    /// structure, then the first qualifying confirmed 15m candle inside the
    /// configured confirmation window supplies the market entry price.
    ///
    /// Candle timestamps are bar OPEN times (OKX `ts`), so the structure bar's
    /// close is one structure period after its timestamp. The confirmation
    /// window is therefore the half-open interval beginning at that close,
    /// which is exactly the four 15m bars at +60/+75/+90/+105 minutes. Bars
    /// inside the structure bar's own hour closed before the structure was
    /// known and must never confirm it.
    public func evaluateWithConfirmation(config: StrategyConfig, structureCandles: [Candle], confirmationCandles: [Candle], previous: StrategyStatus? = nil, btcCandles: [Candle]? = nil) -> StrategyStatus {
        let structure = structureCandles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
        let confirmations = confirmationCandles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
        let status = previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
        guard config.type.hasRuntimeHandler else {
            return StrategyStatus(id: status.id, state: .paused, direction: status.direction,
                                  cooldown: status.cooldown, pnl: status.pnl,
                                  lastSignal: status.lastSignal, indicators: status.indicators,
                                  lastEvaluatedBar: status.lastEvaluatedBar)
        }
        if !config.enabled {
            return StrategyStatus(id: status.id, state: .paused, direction: status.direction,
                                  cooldown: status.cooldown, pnl: status.pnl,
                                  lastSignal: status.lastSignal, indicators: status.indicators,
                                  lastEvaluatedBar: status.lastEvaluatedBar)
        }
        guard !structure.isEmpty else { return status }
        let closes = structure.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let atrPeriod = Self.period(config.parameters["atrPeriod"], fallback: 14)
        let rsi = IndicatorCalculator.rsiWilder(closes, period: atrPeriod)
        let atr = IndicatorCalculator.atrSMA(structure, period: atrPeriod)
        let indicators = ["rsi": Self.trimmed(rsi), "atr": Self.trimmed(atr)]
        // 按 bar 幂等：冷却与指标只在一根**新的**已确认 15m K 线上推进。前端刷新
        // 图表会走 REST 用同一根 K 线重复调用本函数，若每次都递减冷却，冷却会被
        // 读操作提前烧完（冷却被定义在 15m 执行路径上，共 cooldownBars × 4 格）。
        guard let latestBar = confirmations.last?.timestamp else { return status }
        if let lastEvaluated = status.lastEvaluatedBar, latestBar <= lastEvaluated {
            return status.evaluated(at: lastEvaluated)
        }
        let evaluated = { (value: StrategyStatus) in value.evaluated(at: latestBar) }
        guard config.type == .sweepReversalShort else {
            return evaluated(StrategyStatus(id: status.id, state: config.enabled ? .running : .paused, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        if status.cooldown > 0 {
            return evaluated(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown - 1, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        guard let setup = Self.findSweepSetup(config: config, confirmed: structure, closes: closes, btcCandles: btcCandles) else {
            return evaluated(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        // 实验室 config/strategy.json：candle_timestamp = bar_open_time，
        // confirmation_window_origin = structure_bar_close。窗口是半开区间
        // [结构 bar 收盘, 结构 bar 收盘 + confirmationWindowMinutes)，
        // 在 15m 数据上恰好等于 +60/+75/+90/+105 分钟四根 K 线。
        // Configs normally pass through PaperTradingStore validation, but the
        // engine is also a public value type and can be called directly by a
        // package/test. Bound time arithmetic here so malformed doubles cannot
        // produce infinite Date offsets.
        let structureMinutes = Self.boundedFinite(config.parameters["entryTimeframeMinutes"], fallback: 60, lower: 1, upper: 1_440)
        let windowMinutes = Self.boundedFinite(config.parameters["confirmationWindowMinutes"], fallback: 60, lower: 1, upper: 1_440)
        let windowStart = setup.timestamp.addingTimeInterval(structureMinutes * 60)
        let deadline = windowStart.addingTimeInterval(windowMinutes * 60)
        guard let confirmation = confirmations.first(where: {
            $0.timestamp >= windowStart && $0.timestamp < deadline &&
                $0.close <= Decimal(setup.structureEntry) && $0.close < $0.open
        }) else {
            return evaluated(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        if status.lastSignal?.timestamp == confirmation.timestamp {
            return evaluated(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        let entry = NSDecimalNumber(decimal: confirmation.close).doubleValue
        guard let signal = Self.makeSweepSignal(config: config, setup: setup, entry: entry, timestamp: confirmation.timestamp, reason: "1h二次扫顶成立，首根15m阴线收盘确认（市价做空）") else {
            return evaluated(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        // cooldownBars is defined in 1h bars; four confirmed 15m evaluations
        // represent one hour in the execution path.
        let safeCooldownBars = min(max(0, config.cooldownBars), StrategyConfig.maximumCooldownBars)
        let cooldown = safeCooldownBars * 4
        return evaluated(StrategyStatus(id: status.id, state: .running, direction: "short", cooldown: cooldown, pnl: status.pnl, lastSignal: signal, indicators: indicators))
    }

    /// 高位流动性二次扫顶反转（做空）——与 Python 回测 `engine.py` 逐条一致。
    /// 信号仅在“二次扫顶 bar 收盘”（即最新已确认 bar）时发出，并附带 ATR 标定的止损/止盈价位。
    private struct SweepSetup {
        let timestamp: Date
        let structureEntry: Double
        let stop: Double
        let atr: Double
    }

    private static func makeSweepSignal(config: StrategyConfig, setup: SweepSetup, entry: Double, timestamp: Date, reason: String) -> StrategySignal? {
        let minATRPct = Self.boundedFinite(config.parameters["minATRPct"], fallback: 0.5, lower: 0, upper: 100) / 100
        let maxRiskATR = Self.boundedFinite(config.parameters["maxRiskATR"], fallback: 5.0, lower: 0, upper: 100)
        let tpMult = Self.boundedFinite(config.parameters["tpMult"], fallback: 2.2, lower: 0, upper: 100)
        guard entry > 0, setup.atr > 0, setup.atr / entry >= minATRPct else { return nil }
        let risk = setup.stop - entry
        guard risk > 0, risk / setup.atr <= maxRiskATR else { return nil }
        let take = entry - tpMult * risk
        guard take.isFinite, take > 0 else { return nil }
        return StrategySignal(strategyID: config.id, type: "entry_short", price: Decimal(entry), reason: reason, timestamp: timestamp, stopPrice: Decimal(setup.stop), takePrice: Decimal(take))
    }

    private static func boundedFinite(_ value: Double?, fallback: Double, lower: Double, upper: Double) -> Double {
        guard let value, value.isFinite else { return fallback }
        return min(max(value, lower), upper)
    }

    private static func btcGateAllows(at timestamp: Date, btcCandles: [Candle]?, enabled: Bool) -> Bool {
        guard enabled else { return true }
        guard let btcCandles else { return false }
        let confirmedBTC = btcCandles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
        guard confirmedBTC.count >= 200,
              let index = confirmedBTC.lastIndex(where: { $0.timestamp <= timestamp }),
              index >= 199 else { return false }
        guard timestamp.timeIntervalSince(confirmedBTC[index].timestamp) <= 3600 else { return false }
        let closes = confirmedBTC.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let sma = IndicatorCalculator.sma(closes, period: 200)
        return index < closes.count && index < sma.count && closes[index] < sma[index]
    }

    private static func findSweepSetup(config: StrategyConfig, confirmed: [Candle], closes: [Double], btcCandles: [Candle]?) -> SweepSetup? {
        let n = confirmed.count
        let leftArm = period(config.parameters["L"], fallback: 10)
        let rightArm = period(config.parameters["R"], fallback: 5)
        let majorWindow = period(config.parameters["majorWindow"], fallback: 288)
        let sweepWait = period(config.parameters["sweepWait"], fallback: 96)
        let rejectWait = period(config.parameters["rejectWait"], fallback: 5)
        let resweepWait = period(config.parameters["resweepWait"], fallback: 12)
        let rsiMin = config.parameters["rsiMin"] ?? 62
        let volMult = config.parameters["volMult"] ?? 1.5
        let rsDeep = config.parameters["rsDeep"] ?? 0.2
        let atrPeriod = period(config.parameters["atrPeriod"], fallback: 14)
        let bufATR = config.parameters["bufATR"] ?? 0.5
        let btcGateEnabled = (config.parameters["btcGateEnabled"] ?? 1) >= 1
        guard n >= majorWindow, n > leftArm + rightArm + 1 else { return nil }

        let highs = confirmed.map { NSDecimalNumber(decimal: $0.high).doubleValue }
        let volumes = confirmed.map { NSDecimalNumber(decimal: $0.volume).doubleValue }
        let atrSeries = IndicatorCalculator.atrSMA(confirmed, period: atrPeriod)
        let rsiSeries = IndicatorCalculator.rsiWilder(closes, period: atrPeriod)
        let volMean = IndicatorCalculator.priorSMA(volumes, period: 48)
        let majorMax = IndicatorCalculator.rollingMax(highs, period: majorWindow)

        let signalBar = n - 1
        // 只关心“最新 bar 恰好完成二次扫顶”的摆动高点
        let firstPivot = max(leftArm, majorWindow - 1)
        let lastPivot = n - rightArm - 1
        guard lastPivot >= firstPivot else { return nil }
        for p in stride(from: lastPivot, through: firstPivot, by: -1) {
            let level = highs[p]
            guard abs(level - majorMax[p]) < 1e-9 else { continue }            // 288 根高位
            guard level > (highs[max(0, p - leftArm)..<p].max() ?? level) else { continue }  // 左臂严格更高
            guard level >= (highs[(p + 1)...min(p + rightArm, n - 1)].max() ?? level) else { continue }  // 右臂不低于
            // 首次扫顶
            var s = -1
            for idx in (p + 1)...min(p + sweepWait, n - 1) where highs[idx] > level { s = idx; break }
            guard s >= 0 else { continue }
            // 首次回落确认
            var k1 = -1
            for idx in s...min(s + rejectWait, n - 1) where closes[idx] < level { k1 = idx; break }
            guard k1 >= 0 else { continue }
            let ext1 = highs[s...k1].max() ?? level
            // 二次扫顶
            var j = -1
            let resweepEnd = min(k1 + resweepWait, n - 1)
            if k1 + 1 <= resweepEnd {
                for idx in (k1 + 1)...resweepEnd where highs[idx] > level && closes[idx] < level { j = idx; break }
            }
            guard j >= 0 else { continue }
            guard j == signalBar else { continue }                              // 信号只在二次扫顶 bar 收盘时发
            guard highs[j] < ext1 else { continue }                             // 二次高点低于首次极端
            guard highs[j] - level >= rsDeep * atrSeries[j] else { continue }   // 上穿深度
            guard rsiSeries[s] >= rsiMin else { continue }                      // 首扫超买
            guard volumes[s] >= volMult * volMean[s] else { continue }          // 首扫放量
            let ext = highs[s...j].max() ?? ext1
            let atrJ = atrSeries[j]
            let stop = ext + bufATR * atrJ
            guard Self.btcGateAllows(at: confirmed[j].timestamp, btcCandles: btcCandles, enabled: btcGateEnabled) else { continue }
            return SweepSetup(timestamp: confirmed[j].timestamp, structureEntry: closes[j], stop: stop, atr: atrJ)
        }
        return nil
    }

    private static func evaluateSweepReversal(config: StrategyConfig, confirmed: [Candle], closes: [Double], btcCandles: [Candle]?) -> StrategySignal? {
        guard let setup = findSweepSetup(config: config, confirmed: confirmed, closes: closes, btcCandles: btcCandles) else { return nil }
        return makeSweepSignal(config: config, setup: setup, entry: setup.structureEntry, timestamp: setup.timestamp, reason: "高位二次扫顶反转（12天高点被两次假突破，收盘回落）")
    }

}
