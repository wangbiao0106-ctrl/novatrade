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

    // Strategy lab source: sweep_reversal_short v1.3.
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
        if config.type == .doublePumpExhaustionShort {
            signal = Self.evaluateDoublePumpExhaustionShort(config: config, confirmed: confirmed, closes: closes, rsi: rsi, atr: atr)
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
        let structureMinutes = max(1, config.parameters["entryTimeframeMinutes"] ?? 60)
        let windowMinutes = max(1, config.parameters["confirmationWindowMinutes"] ?? 60)
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
        let cooldown = max(0, config.cooldownBars) * 4
        return evaluated(StrategyStatus(id: status.id, state: .running, direction: "short", cooldown: cooldown, pnl: status.pnl, lastSignal: signal, indicators: indicators))
    }

    // MARK: - HLSR high-level liquidity sweep reversal

    /// Evaluates the production HLSR contract.  The lower timeframe is always
    /// 15 minutes and the market regime is read from *completed* 4-hour bars.
    /// This method deliberately takes the two series separately: deriving 4H
    /// state from a short cache of 15m bars would silently make the strategy
    /// trade without its required 55-bar warm-up.
    ///
    /// A signal is emitted only on the bar immediately following a qualifying
    /// confirmation bar.  Its price is that bar's open, which keeps the
    /// implementation causal (the confirmation close is never used as a
    /// pretend fill).  The caller may evaluate an unconfirmed opening update
    /// for this one bar; all structural inputs remain confirmed bars.
    public func evaluateHLSR(config: StrategyConfig,
                             candles15m: [Candle],
                             fourHourCandles: [Candle],
                             previous: StrategyStatus? = nil) -> StrategyStatus {
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
        let lower = candles15m.sorted { $0.timestamp < $1.timestamp }
        // The last candle can be an opening update used for next-open entry.
        // Structural calculations below filter it out whenever it is not
        // confirmed, while `latestBar` still advances idempotency.
        guard let latestBar = lower.last?.timestamp else { return status }
        if let last = status.lastEvaluatedBar, latestBar <= last { return status.evaluated(at: last) }

        let confirmed = lower.filter(\.confirmed)
        let htf = fourHourCandles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
        let atrPeriod = Self.period(config.parameters["atrPeriod"], fallback: 14)
        let lowerATR = IndicatorCalculator.atrEMA(confirmed, period: atrPeriod)
        let indicators = ["atr": Self.trimmed(lowerATR)]
        let marked: (StrategyStatus) -> StrategyStatus = { value in value.evaluated(at: latestBar) }
        // A realtime opening update is allowed only as the final element. An
        // incomplete bar in the historical prefix would make `filter` close
        // the gap and shift confirmation indices, creating a false causal
        // setup (especially after a delayed websocket reconnect).
        guard lower.enumerated().allSatisfy({ index, candle in
            candle.confirmed || index == lower.count - 1
        }) else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        guard config.type.identifier == "hlsr" else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        if status.cooldown > 0 {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown - 1, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }

        // At least one current opening bar plus a prior confirmed bar are
        // needed.  HLSR has no valid way to infer quote volume or a causal
        // confirmation from an incomplete history.
        guard lower.count >= 3, confirmed.count >= 2, htf.count >= 55 else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        let entryIndex = lower.count - 1
        guard let entryBar = lower.last,
              !entryBar.confirmed,
              entryBar.open.isFinite, entryBar.open > 0 else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        // The latest update must be the opening update after the confirmation;
        // all bars through the confirmation bar must be confirmed and the
        // entry is index + 1. A confirmed latest bar has already missed this
        // causal execution point and is rejected below.
        let confirmationIndex = confirmed.firstIndex { $0.timestamp == entryBar.timestamp } == nil
            ? (confirmed.count - 1)
            : (confirmed.count - 2)
        guard confirmationIndex >= 1,
              confirmed[confirmationIndex].timestamp < entryBar.timestamp,
              entryBar.timestamp.timeIntervalSince(confirmed[confirmationIndex].timestamp) == 15 * 60 else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        // A current 15m close is required for a confirmation; the opening bar
        // itself is never allowed to serve as a sweep or confirmation bar.
        let sweepBars = confirmed
        let closes = sweepBars.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let highs = sweepBars.map { NSDecimalNumber(decimal: $0.high).doubleValue }
        let lows = sweepBars.map { NSDecimalNumber(decimal: $0.low).doubleValue }
        let opens = sweepBars.map { NSDecimalNumber(decimal: $0.open).doubleValue }
        let quoteVolumes: [Double] = sweepBars.map { candle in
            guard let value = candle.quoteVolume else { return .nan }
            return NSDecimalNumber(decimal: value).doubleValue
        }
        guard quoteVolumes.allSatisfy({ $0.isFinite && $0 >= 0 }) else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        // Hard universe filters are evaluated at each candidate sweep using
        // confirmed 15m bars only. Quote currency volume is deliberately
        // separate from contract volume; a missing field fails closed.
        let minimumGain = config.parameters["gain24hGt"] ?? 0.4
        let minimumQuoteVolume = config.parameters["quoteVolume24hGt"] ?? 30_000_000
        let lowerAtr = IndicatorCalculator.atrEMA(sweepBars, period: atrPeriod)
        let lookback = Self.period(config.parameters["swingLookback"], fallback: 6)
        let wickRatio = config.parameters["wickRatio"] ?? 0.6
        let volumeMultiple = config.parameters["volumeMultiple"] ?? 1.0
        let minimumScore = Self.period(config.parameters["minimumRejectionScore"], fallback: 2)
        let rejectDepthATR = config.parameters["rejectDepthATR"] ?? 0.1
        let confirmationWindow = Self.period(config.parameters["confirmationWindow"], fallback: 4)
        let stopATR = config.parameters["stopATR"] ?? 0.25
        let trailBars = Self.period(config.parameters["trailBars"], fallback: 2)
        let allowRange = (config.parameters["allowRange"] ?? 1) >= 1
        guard confirmationIndex < sweepBars.count,
              confirmationIndex + 1 <= entryIndex else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }

        // Map a 15m timestamp to the newest *completed* 4H candle.  A 4H
        // candle whose open + four hours is after the sweep is still forming
        // and therefore cannot authorize the trade.
        func regime(at timestamp: Date) -> (state: String, resistance: Double, midpoint: Double, majorLow: Double, atr: Double)? {
            guard let index = htf.lastIndex(where: { $0.timestamp.addingTimeInterval(4 * 3600) <= timestamp }), index >= 0 else { return nil }
            guard timestamp.timeIntervalSince(htf[index].timestamp.addingTimeInterval(4 * 3600)) < 4 * 3600 else { return nil }
            let history = Array(htf.prefix(index + 1))
            guard history.count >= 55 else { return nil }
            guard zip(history, history.dropFirst()).allSatisfy({
                $1.timestamp.timeIntervalSince($0.timestamp) == 4 * 3600
            }) else { return nil }
            let htfCloses = history.map { NSDecimalNumber(decimal: $0.close).doubleValue }
            let htfHighs = history.map { NSDecimalNumber(decimal: $0.high).doubleValue }
            let htfLows = history.map { NSDecimalNumber(decimal: $0.low).doubleValue }
            let fast = IndicatorCalculator.ema(htfCloses, period: 20).last ?? 0
            let slow = IndicatorCalculator.ema(htfCloses, period: 50).last ?? 0
            let htfATR = IndicatorCalculator.atrEMA(history, period: 14).last ?? 0
            guard let recentHigh = htfHighs.suffix(20).max(), let majorLow = htfLows.suffix(20).min(), htfATR.isFinite else { return nil }
            let midpoint = (recentHigh + majorLow) / 2
            guard midpoint > 0 else { return nil }
            let width = (recentHigh - majorLow) / midpoint
            let state: String
            if htfCloses.last! < fast && fast < slow { state = "bearish" }
            else if htfCloses.last! > fast && fast > slow { state = "bullish" }
            else if width <= max(0.08, htfATR / midpoint * 8) { state = "range" }
            else { state = "transition" }
            guard state == "bearish" || (allowRange && state == "range") else { return nil }
            return (state, recentHigh + htfATR * 0.25, midpoint, majorLow, htfATR)
        }

        let firstSweep = max(lookback, confirmationIndex - confirmationWindow)
        guard firstSweep <= confirmationIndex - 1 else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        var selected: (index: Int, previousHigh: Double, regime: (state: String, resistance: Double, midpoint: Double, majorLow: Double, atr: Double), rejection: [String])?
        // The research state machine scans sweeps chronologically and consumes
        // the first confirmation. Preserve that ordering when two candidate
        // sweeps overlap the same four-bar confirmation window.
        for sweepIndex in firstSweep...confirmationIndex - 1 {
            guard sweepIndex >= lookback,
                  let regime = regime(at: sweepBars[sweepIndex].timestamp) else { continue }
            guard sweepIndex >= 96, sweepIndex >= 20 else { continue }
            let window = Array(lower[(sweepIndex - 96)...entryIndex])
            guard zip(window, window.dropFirst()).allSatisfy({
                $1.timestamp.timeIntervalSince($0.timestamp) == 15 * 60
            }) else { continue }
            let gain24h = closes[sweepIndex] / closes[sweepIndex - 96] - 1
            let quoteVolume24h = quoteVolumes[(sweepIndex - 95)...sweepIndex].reduce(0, +)
            // These are hard universe filters from the lab config.  Equality
            // at either boundary is deliberately rejected (`>` in Python).
            guard gain24h > minimumGain, quoteVolume24h > minimumQuoteVolume else { continue }
            let previousHigh = highs[(sweepIndex - lookback)..<sweepIndex].max() ?? highs[sweepIndex]
            guard highs[sweepIndex] > previousHigh, closes[sweepIndex] < previousHigh else { continue }
            guard sweepIndex < lowerAtr.count, lowerAtr[sweepIndex].isFinite, lowerAtr[sweepIndex] > 0 else { continue }
            let candleRange = max(highs[sweepIndex] - lows[sweepIndex], 1e-12)
            let upperWick = (highs[sweepIndex] - max(opens[sweepIndex], closes[sweepIndex])) / candleRange
            let meanVolume = quoteVolumes[(sweepIndex - 20)..<sweepIndex].reduce(0, +) / 20
            var rejection: [String] = []
            if upperWick >= wickRatio { rejection.append("large_upper_wick") }
            if closes[sweepIndex] < opens[sweepIndex] { rejection.append("bearish_close") }
            if quoteVolumes[sweepIndex] > meanVolume * volumeMultiple { rejection.append("volume_expansion") }
            if closes[sweepIndex] < previousHigh - rejectDepthATR * lowerAtr[sweepIndex] { rejection.append("failed_breakout") }
            guard rejection.count >= minimumScore else { continue }
            // Python's state machine chooses the first confirmation.  Reject a
            // later one if an earlier bar in this window had already confirmed.
            func confirmation(_ index: Int) -> Bool {
                guard index > sweepIndex else { return false }
                let breakLow = index > sweepIndex + 1 && closes[index] < (lows[(sweepIndex + 1)..<index].min() ?? lows[index])
                let failedRetest = highs[index] >= previousHigh * 0.995 && closes[index] < opens[index] && closes[index] < previousHigh
                return breakLow || failedRetest
            }
            guard confirmation(confirmationIndex),
                  !(sweepIndex + 1..<confirmationIndex).contains(where: confirmation) else { continue }
            selected = (sweepIndex, previousHigh, regime, rejection)
            break
        }
        guard let setup = selected else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        let entry = NSDecimalNumber(decimal: entryBar.open).doubleValue
        let sweepATR = lowerAtr[setup.index]
        let stop = highs[setup.index] + stopATR * sweepATR
        let risk = stop - entry
        guard entry > 0, risk > entry * 0.002, risk <= entry * 0.15 else {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        let zone: String
        if highs[setup.index] <= setup.regime.resistance + setup.regime.atr { zone = "normal_extension" }
        else if highs[setup.index] <= setup.regime.resistance + 2 * setup.regime.atr { zone = "primary_sweep" }
        else { zone = "extreme_sweep" }
        // zoneRequired is represented as a stable string in StrategyConfig in
        // the final domain model.  This fallback keeps old persisted numeric
        // parameter maps harmless until migration fills the string metadata.
        if let required = config.parameters["zoneRequiredCode"], required > 0 {
            let expected = required == 1 ? "normal_extension" : required == 2 ? "primary_sweep" : "extreme_sweep"
            guard zone == expected else { return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators)) }
        }
        let support = lows[max(0, confirmationIndex - 32)...confirmationIndex].min() ?? entry
        var targets = [support < entry ? support : entry - risk,
                       setup.regime.midpoint < entry ? setup.regime.midpoint : entry - 2 * risk,
                       setup.regime.majorLow < entry ? setup.regime.majorLow : entry - 3 * risk]
            .filter { $0 < entry }
        targets.sort { $0 > $1 }
        var targetValues: [Double] = []
        for value in targets where !targetValues.contains(where: { abs($0 - value) < 1e-9 }) {
            targetValues.append(value)
        }
        while targetValues.count < 3 {
            let nextByR = entry - risk * Double(targetValues.count + 1)
            let nextBelowPrevious = (targetValues.last ?? entry) - risk
            let next = min(nextByR, nextBelowPrevious)
            targetValues.append(next < entry ? next : entry - risk * Double(targetValues.count + 1))
        }
        targetValues = Array(targetValues.prefix(3))
        let configuredFractions = [
            config.parameters["partialTarget1"],
            config.parameters["partialTarget2"],
            config.parameters["partialTarget3"]
        ].compactMap { $0 }.map { Decimal($0) }
        let targetFractions = configuredFractions.count == 3 && configuredFractions.allSatisfy { $0 > 0 }
            ? configuredFractions
            : [Decimal(string: "0.3")!, Decimal(string: "0.3")!, Decimal(string: "0.4")!]
        let moveStopToEntry = (config.parameters["moveStopToEntryAfterTP1"] ?? 1) >= 1
        let signal = StrategySignal(strategyID: config.id, type: "entry_short", price: Decimal(entry),
                                     reason: "高位扫顶反转：\(setup.regime.state)，\(setup.rejection.count)项拒绝，确认后下一根15m开盘入场（\(zone)）",
                                     timestamp: entryBar.timestamp, stopPrice: Decimal(stop),
                                     takePrice: Decimal(targetValues[0]), takePrices: targetValues.map { Decimal($0) },
                                     targetFractions: targetFractions,
                                     moveStopToEntryAfterTP1: moveStopToEntry, trailBars: trailBars,
                                     invalidationPrice: Decimal(highs[setup.index]))
        if status.lastSignal?.timestamp == signal.timestamp {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction,
                                         cooldown: status.cooldown, pnl: status.pnl,
                                         lastSignal: status.lastSignal, indicators: indicators))
        }
        return marked(StrategyStatus(id: status.id, state: .running, direction: "short", cooldown: status.cooldown,
                                     pnl: status.pnl, lastSignal: signal, indicators: indicators))
    }

    // MARK: - EMA altcoin long

    // Strategy lab source: ema_altcoin_long v1.0.0.
    // 同步记录 2026-09：按实验室 STRATEGY.md §8「运行时映射契约」实现。
    // 与 Python 回测 `src/backtest.py` 的差异只有两处，均为运行时约定：
    // 1) 回测入场价取"确认 K 线的下一根开盘价 + 滑点"，运行时在确认收盘后立即
    //    市价成交，时点等价，差异体现在滑点；
    // 2) 回测按分钟级逐格撮合保护价，运行时用交易所条件单 + 心跳监控离场。

    /// 均线密集 → 突破 → 首次回踩 EMA20 确认的多头入场。
    /// 只在一根**新的**已确认 1h K 线上推进；门控只关闭新开仓。
    public func evaluateEmaAltcoinLong(config: StrategyConfig, candles: [Candle], previous: StrategyStatus? = nil, btcCandles: [Candle]? = nil) -> StrategyStatus {
        let confirmed = candles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
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
        guard confirmed.count >= 3 else { return status }
        let closes = confirmed.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let highs = confirmed.map { NSDecimalNumber(decimal: $0.high).doubleValue }
        let lows = confirmed.map { NSDecimalNumber(decimal: $0.low).doubleValue }
        let fastPeriod = Self.period(config.parameters["emaFast"], fallback: 20)
        let slowPeriod = Self.period(config.parameters["emaSlow"], fallback: 60)
        let trendPeriod = Self.period(config.parameters["emaTrend"], fallback: 120)
        let atrPeriod = Self.period(config.parameters["atrPeriod"], fallback: 14)
        let fast = IndicatorCalculator.ema(closes, period: fastPeriod)
        let slow = IndicatorCalculator.ema(closes, period: slowPeriod)
        let trend = IndicatorCalculator.ema(closes, period: trendPeriod)
        let atr = IndicatorCalculator.atrEMA(confirmed, period: atrPeriod)
        let indicators = ["emaFast": Self.trimmed(fast), "emaSlow": Self.trimmed(slow), "emaTrend": Self.trimmed(trend), "atr": Self.trimmed(atr)]

        guard let latestBar = confirmed.last?.timestamp else { return status }
        if let lastEvaluated = status.lastEvaluatedBar, latestBar <= lastEvaluated {
            return status.evaluated(at: lastEvaluated)
        }
        let idle = StrategyStatus(id: status.id, state: config.enabled ? .running : .paused, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators)
        let marked = { (value: StrategyStatus) in value.evaluated(at: latestBar) }
        guard config.type == .emaAltcoinLong else { return marked(idle) }

        // 完整小时：最后一根已确认 K 线必须与前一根正好相隔 1 小时，且累计达到
        // minimumHistoryBars（对应实验室的"每小时必须完整、不足 120 根不发信号"）。
        let minimumBars = Self.period(config.parameters["minimumHistoryBars"], fallback: 120)
        guard confirmed.count >= max(minimumBars, trendPeriod + 1), confirmed.count >= 2,
              latestBar.timeIntervalSince(confirmed[confirmed.count - 2].timestamp) == 3600 else {
            return marked(idle)
        }
        // 冷却只作为重复提交的兜底：入场后 maxHoldBars 根内不再产生新信号，
        // 与"同一标的只允许一笔持仓"一致（真正的判定在下单前检查远端持仓）。
        if status.cooldown > 0 {
            return marked(StrategyStatus(id: status.id, state: .running, direction: status.direction, cooldown: status.cooldown - 1, pnl: status.pnl, lastSignal: status.lastSignal, indicators: indicators))
        }
        let index = confirmed.count - 1
        guard Self.btcEmaGateAllows(at: latestBar, btcCandles: btcCandles, slowPeriod: slowPeriod,
                                    slopeBars: Self.period(config.parameters["gateSlopeBars"], fallback: 6),
                                    minimumHistoryBars: minimumBars) else {
            return marked(idle)
        }
        let minATRPct = (config.parameters["minATRPct"] ?? 0.4) / 100
        let atrOK = closes[index] > 0 && atr[index] / closes[index] >= minATRPct
        let aligned = closes[index] > fast[index] && fast[index] > slow[index] && slow[index] > trend[index]
        guard Self.hasEmaPullbackSetup(config: config, closes: closes, highs: highs, lows: lows, fast: fast, slow: slow, trend: trend, atr: atr, minATRPct: minATRPct, atrOK: atrOK, aligned: aligned,
                                       timestamps: confirmed.map(\.timestamp), btcCandles: btcCandles,
                                       slowPeriod: slowPeriod,
                                       slopeBars: Self.period(config.parameters["gateSlopeBars"], fallback: 6),
                                       minimumHistoryBars: minimumBars, index: index) else {
            return marked(idle)
        }
        let entry = closes[index]
        let risk = (config.parameters["stopATR"] ?? 1.25) * atr[index]
        guard entry > 0, risk > 0 else { return marked(idle) }
        let signal = StrategySignal(
            strategyID: config.id,
            type: "entry_long",
            price: Decimal(entry),
            reason: "均线密集后突破，首次回踩 EMA20 收盘确认（市价做多）",
            timestamp: latestBar,
            stopPrice: Decimal(entry - risk),
            takePrice: Decimal(entry + (config.parameters["targetR"] ?? 2.5) * risk)
        )
        return marked(StrategyStatus(id: status.id, state: .running, direction: "long",
                                     cooldown: max(0, Self.period(config.parameters["maxHoldBars"], fallback: 96)),
                                     pnl: status.pnl, lastSignal: signal, indicators: indicators))
    }

    /// BTC 门控：`BTC close > BTC EMA(slow)` 且 `EMA(slow)` 高于 `slopeBars` 根之前。
    /// fail-closed：BTC 缺失、历史不足或无法按时刻对齐时不通过（只关闭新开仓）。
    private static func btcEmaGateAllows(at timestamp: Date, btcCandles: [Candle]?, slowPeriod: Int, slopeBars: Int, minimumHistoryBars: Int) -> Bool {
        guard let btcCandles else { return false }
        let confirmedBTC = btcCandles.filter(\.confirmed).sorted { $0.timestamp < $1.timestamp }
        guard confirmedBTC.count >= minimumHistoryBars,
              let index = confirmedBTC.lastIndex(where: { $0.timestamp <= timestamp }),
              index >= minimumHistoryBars - 1, index >= slopeBars else { return false }
        // A historical BTC candle cannot authorize a current altcoin entry.
        // Permit one complete hourly bar of timestamp skew for feeds that do
        // not publish both instruments at exactly the same instant.
        guard timestamp.timeIntervalSince(confirmedBTC[index].timestamp) <= 3600 else { return false }
        let closes = confirmedBTC.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let slow = IndicatorCalculator.ema(closes, period: slowPeriod)
        guard index < closes.count, index < slow.count else { return false }
        return closes[index] > slow[index] && slow[index] > slow[index - slopeBars]
    }

    /// 当前 bar 是否构成"窗口内首个有效回踩确认"：窗口内最近一次突破 b，
    /// (b, i) 之间排列未失效且没有触及过 EMA20 容差区，而当前 bar 首次触及并收在其上方。
    private static func hasEmaPullbackSetup(config: StrategyConfig, closes: [Double], highs: [Double], lows: [Double], fast: [Double], slow: [Double], trend: [Double], atr: [Double], minATRPct: Double, atrOK: Bool, aligned: Bool, timestamps: [Date], btcCandles: [Candle]?, slowPeriod: Int, slopeBars: Int, minimumHistoryBars: Int, index i: Int) -> Bool {
        guard aligned, atrOK, i >= 1 else { return false }
        let breakoutBars = period(config.parameters["breakoutBars"], fallback: 4)
        let pullbackBars = period(config.parameters["pullbackBars"], fallback: 6)
        let clusterATR = config.parameters["clusterATR"] ?? 0.75
        let pullbackATR = config.parameters["pullbackATR"] ?? 0.35
        let minBreakoutATR = config.parameters["minBreakoutATR"] ?? 0.3
        let minSpreadATR = config.parameters["minSpreadATR"] ?? 0.5

        func span(_ k: Int) -> Double { max(fast[k], slow[k], trend[k]) - min(fast[k], slow[k], trend[k]) }
        func isAligned(_ k: Int) -> Bool { closes[k] > fast[k] && fast[k] > slow[k] && slow[k] > trend[k] }
        func gateAllows(_ k: Int) -> Bool {
            btcEmaGateAllows(at: timestamps[k], btcCandles: btcCandles, slowPeriod: slowPeriod,
                             slopeBars: slopeBars, minimumHistoryBars: minimumHistoryBars)
        }
        func isBreakout(_ b: Int) -> Bool {
            guard b >= breakoutBars, b - 1 >= 0, atr[b] > 0, atr[b - 1] > 0, closes[b] > 0 else { return false }
            let priorHigh = highs[(b - breakoutBars)..<b].max() ?? highs[b]
            let strength = (closes[b] - priorHigh) / atr[b]
            let atrAtB = atr[b] / closes[b] >= minATRPct
            return span(b - 1) <= clusterATR * atr[b - 1]
                && closes[b] > priorHigh && isAligned(b) && atrAtB
                && strength >= minBreakoutATR && span(b) >= minSpreadATR * atr[b]
        }

        let lowest = max(breakoutBars, i - pullbackBars)
        guard lowest <= i - 1 else { return false }
        guard let b = stride(from: i - 1, through: lowest, by: -1).first(where: isBreakout) else { return false }
        // The research state machine cancels a pending pullback as soon as
        // the BTC regime gate closes. Check the whole pending interval so a
        // gate outage cannot be bypassed by waiting for it to reopen.
        guard gateAllows(b) else { return false }
        for k in (b + 1)..<i {
            // 排列失效或提前触及即取消该 setup，不在同一突破后重试。
            if !isAligned(k) || !gateAllows(k) { return false }
            if lows[k] <= fast[k] + pullbackATR * atr[k] { return false }
        }
        return lows[i] <= fast[i] + pullbackATR * atr[i] && closes[i] > fast[i]
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
        let minATRPct = (config.parameters["minATRPct"] ?? 0.5) / 100
        let maxRiskATR = config.parameters["maxRiskATR"] ?? 5.0
        let tpMult = config.parameters["tpMult"] ?? 2.2
        guard entry > 0, setup.atr > 0, setup.atr / entry >= minATRPct else { return nil }
        let risk = setup.stop - entry
        guard risk > 0, risk / setup.atr <= maxRiskATR else { return nil }
        let take = entry - tpMult * risk
        return StrategySignal(strategyID: config.id, type: "entry_short", price: Decimal(entry), reason: reason, timestamp: timestamp, stopPrice: Decimal(setup.stop), takePrice: Decimal(take))
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

    /// 日内翻倍动能衰竭确认做空（DME Short）。所有条件只使用已确认的
    /// 15m K 线及其左侧历史；报价成交额缺失时 fail-closed。该实现与
    /// strategies/double_pump_exhaustion_short/config/strategy.json 的正式
    /// 规则保持一一对应，运行时不读取策略目录。
    private static func evaluateDoublePumpExhaustionShort(config: StrategyConfig,
                                                           confirmed: [Candle],
                                                           closes: [Double],
                                                           rsi: [Double],
                                                           atr: [Double]) -> StrategySignal? {
        let minimumHistory = period(config.parameters["minimumHistoryBars"], fallback: 97)
        guard confirmed.count >= minimumHistory,
              closes.count == confirmed.count,
              rsi.count == confirmed.count,
              atr.count == confirmed.count else { return nil }
        let index = confirmed.count - 1
        guard index >= 96, closes[index] > 0, closes[index - 96] > 0 else { return nil }
        // The 24h window must be contiguous. A missing 15m bar cannot be
        // silently treated as a stale close from a different session.
        let start = index - 96
        for cursor in (start + 1)...index {
            guard confirmed[cursor].timestamp.timeIntervalSince(confirmed[cursor - 1].timestamp) == 15 * 60 else { return nil }
        }
        let gain24 = (NSDecimalNumber(decimal: confirmed[index].high).doubleValue / closes[start]) - 1
        let gainThreshold = config.parameters["gain24Gt"] ?? 1.0
        guard gain24 > gainThreshold else { return nil }

        let open = NSDecimalNumber(decimal: confirmed[index].open).doubleValue
        let high = NSDecimalNumber(decimal: confirmed[index].high).doubleValue
        let low = NSDecimalNumber(decimal: confirmed[index].low).doubleValue
        let close = closes[index]
        let range = high - low
        guard range > 0, close < open else { return nil }
        let upperWick = high - max(open, close)
        let closePosition = (close - low) / range
        guard upperWick / range >= (config.parameters["upperWickMin"] ?? 0.4),
              closePosition <= (config.parameters["closePositionMax"] ?? 0.5),
              rsi[index] >= (config.parameters["rsiMin"] ?? 50.0),
              index > 0, rsi[index] < rsi[index - 1] else { return nil }

        let volumePeriod = period(config.parameters["volumePeriod"], fallback: 20)
        guard index >= volumePeriod else { return nil }
        var priorVolumes: [Double] = []
        priorVolumes.reserveCapacity(volumePeriod)
        for cursor in (index - volumePeriod)..<index {
            guard let quote = confirmed[cursor].quoteVolume else { return nil }
            let value = NSDecimalNumber(decimal: quote).doubleValue
            guard value.isFinite, value >= 0 else { return nil }
            priorVolumes.append(value)
        }
        guard let currentQuote = confirmed[index].quoteVolume else { return nil }
        let currentVolume = NSDecimalNumber(decimal: currentQuote).doubleValue
        let priorMean = priorVolumes.reduce(0, +) / Double(volumePeriod)
        guard currentVolume.isFinite, currentVolume >= 0,
              priorMean.isFinite,
              currentVolume >= priorMean * (config.parameters["volumeMultiple"] ?? 0.5) else { return nil }

        let atrValue = atr[index]
        guard atrValue.isFinite, atrValue > 0 else { return nil }
        let stop = high + (config.parameters["stopATR"] ?? 0.45) * atrValue
        let risk = stop - close
        let minRisk = config.parameters["minRiskATR"] ?? 0.5
        let maxRisk = config.parameters["maxRiskATR"] ?? 3.0
        guard risk > 0, risk / atrValue >= minRisk, risk / atrValue <= maxRisk else { return nil }
        let targetR = config.parameters["targetR"] ?? 1.0
        let take = close - targetR * risk
        guard take > 0 else { return nil }
        return StrategySignal(strategyID: config.id,
                              type: "entry_short",
                              price: Decimal(close),
                              reason: "滚动24小时涨幅超过100%，15分钟上影线动能衰竭确认",
                              timestamp: confirmed[index].timestamp,
                              stopPrice: Decimal(stop),
                              takePrice: Decimal(take))
    }

}
