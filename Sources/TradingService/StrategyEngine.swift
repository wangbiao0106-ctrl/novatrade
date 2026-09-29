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
        let confirmed = candles.filter(\.confirmed)
        guard confirmed.count >= 3 else { return previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused) }
        let closes = confirmed.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let atrPeriod = Self.period(config.parameters["atrPeriod"], fallback: 14)
        let rsi = IndicatorCalculator.rsiWilder(closes, period: atrPeriod)
        let atr = IndicatorCalculator.atrSMA(confirmed, period: atrPeriod)
        let status = previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
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
        guard config.enabled else { return status.evaluated(at: latestBar) }
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
        let structure = structureCandles.filter(\.confirmed)
        let confirmations = confirmationCandles.filter(\.confirmed)
        let status = previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
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
        guard config.enabled, config.type == .sweepReversalShort else {
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
        let confirmed = candles.filter(\.confirmed)
        let status = previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
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
        guard config.enabled, config.type == .emaAltcoinLong else { return marked(idle) }

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
        guard Self.hasEmaPullbackSetup(config: config, closes: closes, highs: highs, lows: lows, fast: fast, slow: slow, trend: trend, atr: atr, minATRPct: minATRPct, atrOK: atrOK, aligned: aligned, index: index) else {
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
        let confirmedBTC = btcCandles.filter(\.confirmed)
        guard confirmedBTC.count >= minimumHistoryBars,
              let index = confirmedBTC.lastIndex(where: { $0.timestamp <= timestamp }),
              index >= minimumHistoryBars - 1, index >= slopeBars else { return false }
        let closes = confirmedBTC.map { NSDecimalNumber(decimal: $0.close).doubleValue }
        let slow = IndicatorCalculator.ema(closes, period: slowPeriod)
        guard index < closes.count, index < slow.count else { return false }
        return closes[index] > slow[index] && slow[index] > slow[index - slopeBars]
    }

    /// 当前 bar 是否构成"窗口内首个有效回踩确认"：窗口内最近一次突破 b，
    /// (b, i) 之间排列未失效且没有触及过 EMA20 容差区，而当前 bar 首次触及并收在其上方。
    private static func hasEmaPullbackSetup(config: StrategyConfig, closes: [Double], highs: [Double], lows: [Double], fast: [Double], slow: [Double], trend: [Double], atr: [Double], minATRPct: Double, atrOK: Bool, aligned: Bool, index i: Int) -> Bool {
        guard aligned, atrOK, i >= 1 else { return false }
        let breakoutBars = period(config.parameters["breakoutBars"], fallback: 4)
        let pullbackBars = period(config.parameters["pullbackBars"], fallback: 6)
        let clusterATR = config.parameters["clusterATR"] ?? 0.75
        let pullbackATR = config.parameters["pullbackATR"] ?? 0.35
        let minBreakoutATR = config.parameters["minBreakoutATR"] ?? 0.3
        let minSpreadATR = config.parameters["minSpreadATR"] ?? 0.5

        func span(_ k: Int) -> Double { max(fast[k], slow[k], trend[k]) - min(fast[k], slow[k], trend[k]) }
        func isAligned(_ k: Int) -> Bool { closes[k] > fast[k] && fast[k] > slow[k] && slow[k] > trend[k] }
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
        for k in (b + 1)..<i {
            // 排列失效或提前触及即取消该 setup，不在同一突破后重试。
            if !isAligned(k) { return false }
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
        let confirmedBTC = btcCandles.filter(\.confirmed)
        guard confirmedBTC.count >= 200,
              let index = confirmedBTC.lastIndex(where: { $0.timestamp <= timestamp }),
              index >= 199 else { return false }
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
        let volMean = IndicatorCalculator.sma(volumes, period: 48)
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
