import Foundation
import TradingDomain

public enum IndicatorCalculator {
    public static func ema(_ values: [Double], period: Int) -> [Double] {
        guard !values.isEmpty, period > 0 else { return [] }
        let alpha = 2.0 / (Double(period) + 1)
        var result = [values[0]]
        for value in values.dropFirst() { result.append(value * alpha + result[result.count - 1] * (1 - alpha)) }
        return result
    }

    public static func rsi(_ values: [Double], period: Int = 14) -> [Double] {
        guard values.count > 1, period > 0 else { return values.map { _ in 50 } }
        var gains = Array(repeating: 0.0, count: values.count)
        var losses = Array(repeating: 0.0, count: values.count)
        for index in 1..<values.count {
            let delta = values[index] - values[index - 1]
            gains[index] = max(delta, 0)
            losses[index] = max(-delta, 0)
        }
        let averageGain = ema(gains, period: period)
        let averageLoss = ema(losses, period: period)
        return values.indices.map { index in
            guard averageLoss[index] > 0 else { return averageGain[index] > 0 ? 100 : 50 }
            return 100 - 100 / (1 + averageGain[index] / averageLoss[index])
        }
    }

    public static func atr(_ candles: [Candle], period: Int = 14) -> [Double] {
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

    public static func donchian(_ candles: [Candle], period: Int = 20) -> (high: [Double], low: [Double]) {
        let highs = candles.map { NSDecimalNumber(decimal: $0.high).doubleValue }
        let lows = candles.map { NSDecimalNumber(decimal: $0.low).doubleValue }
        let safePeriod = max(1, period)
        return (highs.indices.map { index in highs[max(0, index - safePeriod + 1)...index].max() ?? highs[index] }, lows.indices.map { index in lows[max(0, index - safePeriod + 1)...index].min() ?? lows[index] })
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
}

public struct StrategyEngine: Sendable {
    public init() {}

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
        let fastPeriod = Self.period(config.parameters["fastEMA"], fallback: 20)
        let slowPeriod = Self.period(config.parameters["slowEMA"], fallback: 60)
        let rsiPeriod = Self.period(config.parameters["period"], fallback: 14)
        let fast = IndicatorCalculator.ema(closes, period: fastPeriod)
        let slow = IndicatorCalculator.ema(closes, period: slowPeriod)
        let rsi = IndicatorCalculator.rsi(closes, period: rsiPeriod)
        let atr = IndicatorCalculator.atr(confirmed, period: Self.period(config.parameters["atrPeriod"], fallback: 14))
        let status = previous ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
        if let lastSignal = status.lastSignal, lastSignal.timestamp == confirmed.last?.timestamp {
            return StrategyStatus(id: status.id, state: config.enabled ? .running : .paused, direction: status.direction, cooldown: status.cooldown, pnl: status.pnl, lastSignal: lastSignal, indicators: status.indicators)
        }
        if status.cooldown > 0 {
            return StrategyStatus(id: status.id, state: config.enabled ? .running : .paused, direction: status.direction, cooldown: status.cooldown - 1, pnl: status.pnl, lastSignal: status.lastSignal, indicators: status.indicators)
        }
        guard config.enabled else { return status }
        let last = closes.count - 1
        let signalTimestamp = confirmed[last].timestamp
        var signal: StrategySignal?
        var direction = status.direction
        if config.type == .trendFollowing, last > 0 {
            let donchian = IndicatorCalculator.donchian(confirmed, period: Self.period(config.parameters["donchianPeriod"], fallback: 20))
            if fast[last] > slow[last], fast[last - 1] <= slow[last - 1], closes[last] >= donchian.high[last] {
                direction = "long"; signal = StrategySignal(strategyID: config.id, type: "entry_long", price: Decimal(closes[last]), reason: "EMA 金叉", timestamp: signalTimestamp)
            } else if fast[last] < slow[last], fast[last - 1] >= slow[last - 1], closes[last] <= donchian.low[last] {
                direction = "short"; signal = StrategySignal(strategyID: config.id, type: "entry_short", price: Decimal(closes[last]), reason: "EMA 死叉", timestamp: signalTimestamp)
            }
        } else if config.type == .rsiReversal {
            let oversold = config.parameters["oversold"] ?? 30
            let overbought = config.parameters["overbought"] ?? 70
            if rsi[last] > oversold, rsi[last - 1] <= oversold { direction = "long"; signal = StrategySignal(strategyID: config.id, type: "entry_long", price: Decimal(closes[last]), reason: "RSI 回穿超卖", timestamp: signalTimestamp) }
            if rsi[last] < overbought, rsi[last - 1] >= overbought { direction = "short"; signal = StrategySignal(strategyID: config.id, type: "entry_short", price: Decimal(closes[last]), reason: "RSI 回穿超买", timestamp: signalTimestamp) }
        } else if config.type == .sweepReversalShort {
            signal = Self.evaluateSweepReversal(config: config, confirmed: confirmed, closes: closes, btcCandles: btcCandles)
            if signal != nil { direction = "short" }
        }
        return StrategyStatus(id: config.id, state: .running, direction: direction, cooldown: signal == nil ? status.cooldown : config.cooldownBars, pnl: status.pnl, lastSignal: signal ?? status.lastSignal, indicators: ["emaFast": Self.trimmed(fast), "emaSlow": Self.trimmed(slow), "rsi": Self.trimmed(rsi), "atr": Self.trimmed(atr)])
    }

    /// 高位流动性二次扫顶反转（做空）——与 Python 回测 `engine.py` 逐条一致。
    /// 信号仅在“二次扫顶 bar 收盘”（即最新已确认 bar）时发出，并附带 ATR 标定的止损/止盈价位。
    private static func evaluateSweepReversal(config: StrategyConfig, confirmed: [Candle], closes: [Double], btcCandles: [Candle]?) -> StrategySignal? {
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
        let bufATR = config.parameters["bufATR"] ?? 0.5
        let tpMult = config.parameters["tpMult"] ?? 2.2
        let minATRPct = (config.parameters["minATRPct"] ?? 0.5) / 100
        let maxRiskATR = config.parameters["maxRiskATR"] ?? 5.0
        let btcGateEnabled = (config.parameters["btcGateEnabled"] ?? 1) >= 1
        guard n > leftArm + rightArm + 1 else { return nil }

        let highs = confirmed.map { NSDecimalNumber(decimal: $0.high).doubleValue }
        let lows = confirmed.map { NSDecimalNumber(decimal: $0.low).doubleValue }
        let volumes = confirmed.map { NSDecimalNumber(decimal: $0.volume).doubleValue }
        let atrSeries = IndicatorCalculator.atrSMA(confirmed)
        let rsiSeries = IndicatorCalculator.rsi(closes)
        let volMean = IndicatorCalculator.sma(volumes, period: 48)
        let majorMax = IndicatorCalculator.rollingMax(highs, period: majorWindow)

        // BTC 门控：1h 收盘 < BTC SMA200（与回测口径一致；数据不足视为门控关闭）
        var btcCloses: [Double]?
        var btcSMA: [Double]?
        if btcGateEnabled, let btc = btcCandles, btc.count > 200 {
            let values = btc.map { NSDecimalNumber(decimal: $0.close).doubleValue }
            btcCloses = values
            btcSMA = IndicatorCalculator.sma(values, period: 200)
        } else if btcGateEnabled {
            return nil
        }

        let signalBar = n - 1
        // 只关心“最新 bar 恰好完成二次扫顶”的摆动高点
        for p in stride(from: n - rightArm - 1, through: leftArm, by: -1) {
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
            for idx in (k1 + 1)...min(k1 + resweepWait, n - 1) where highs[idx] > level && closes[idx] < level { j = idx; break }
            guard j >= 0 else { continue }
            guard j == signalBar else { continue }                              // 信号只在二次扫顶 bar 收盘时发
            guard highs[j] < ext1 else { continue }                             // 二次高点低于首次极端
            guard highs[j] - level >= rsDeep * atrSeries[j] else { continue }   // 上穿深度
            guard rsiSeries[s] >= rsiMin else { continue }                      // 首扫超买
            guard volumes[s] >= volMult * volMean[s] else { continue }          // 首扫放量
            let entry = closes[j]
            let ext = highs[s...j].max() ?? ext1
            let atrJ = atrSeries[j]
            guard atrJ / entry >= minATRPct else { continue }                   // 薄盘保护
            let stop = ext + bufATR * atrJ
            let risk = stop - entry
            guard risk > 0, risk / atrJ <= maxRiskATR else { continue }         // 极端波动保护
            // BTC 门控
            if btcGateEnabled, let btcCloses, let btcSMA,
               let idx = btcCandles?.firstIndex(where: { $0.timestamp == confirmed[j].timestamp }) {
                guard btcCloses[idx] < btcSMA[idx] else { return nil }
            }
            let take = entry - tpMult * risk
            return StrategySignal(
                strategyID: config.id,
                type: "entry_short",
                price: Decimal(entry),
                reason: "高位二次扫顶反转（12天高点被两次假突破，收盘回落）",
                timestamp: confirmed[j].timestamp,
                stopPrice: Decimal(stop),
                takePrice: Decimal(take)
            )
        }
        return nil
    }
}
