import Foundation
import Testing
import TradingDomain
@testable import TradingService

private func hlsrConfig(enabled: Bool = true) -> StrategyConfig {
    StrategyConfig(name: "高位扫顶反转做空", scope: .dynamic(.hlsrCandidates), interval: .fifteenMinutes,
                   type: .hlsr, enabled: enabled, cooldownBars: 16)
}

private func hlsrFixtures() -> (lower: [Candle], fourHour: [Candle]) {
    let start = Date(timeIntervalSince1970: 0)
    var lower: [Candle] = []
    for index in 0..<103 {
        let timestamp = start.addingTimeInterval(Double(index) * 15 * 60)
        let value = 65.0 + Double(index) * 0.37
        switch index {
        case 100:
            // Six-bar swing high is swept, rejected and closes back below the
            // prior high.  The quote-volume spike contributes one rejection
            // point while the wick and bearish close supply two more.
            lower.append(Candle(timestamp: timestamp, open: 100, high: 108, low: 98, close: 99,
                                volume: 1, quoteVolume: 500_000))
        case 101:
            // Failed retest of the swept high.  Entry must wait for index 102.
            lower.append(Candle(timestamp: timestamp, open: 100, high: 102, low: 96, close: 98,
                                volume: 1, quoteVolume: 400_000))
        case 102:
            // The signal price is this next bar's open, never the confirmation
            // close at 98.
            lower.append(Candle(timestamp: timestamp, open: 97, high: 98, low: 95, close: 97,
                                volume: 1, quoteVolume: 100_000, confirmed: false))
        default:
            lower.append(Candle(timestamp: timestamp, open: Decimal(value - 0.1),
                                high: Decimal(value + 0.4), low: Decimal(value - 0.4),
                                close: Decimal(value), volume: 1, quoteVolume: 400_000))
        }
    }

    // The latest 4H state is bearish (close < EMA20 < EMA50) and has the
    // required 55 completed bars.  The latest completed 4H bucket closes at
    // t=24h, so it is fresh for the lower-timeframe sweep at t=25h.
    var fourHour: [Candle] = []
    for index in 0..<62 {
        let timestamp = start.addingTimeInterval(Double(index - 55) * 4 * 3600)
        let close = 200.0 - Double(index) * 1.8
        fourHour.append(Candle(timestamp: timestamp, open: Decimal(close + 1), high: Decimal(close + 3),
                                low: Decimal(close - 3), close: Decimal(close), volume: 1,
                                quoteVolume: 1_000_000))
    }
    return (lower, fourHour)
}

@Test
func hlsrUsesQuoteVolumeAndNextOpenWithFullExitPlan() {
    let fixtures = hlsrFixtures()
    let engine = StrategyEngine()
    let status = engine.evaluateHLSR(config: hlsrConfig(), candles15m: fixtures.lower,
                                     fourHourCandles: fixtures.fourHour)
    #expect(status.direction == "short")
    #expect(status.lastSignal?.type == "entry_short")
    #expect(status.lastSignal?.price == Decimal(97))
    #expect(status.lastSignal?.takePrices?.count == 3)
    #expect(status.lastSignal?.targetFractions == [Decimal(0.3), Decimal(0.3), Decimal(0.4)])
    #expect(status.lastSignal?.moveStopToEntryAfterTP1 == true)
    #expect(status.lastSignal?.trailBars == 2)
    #expect(status.lastSignal?.invalidationPrice == Decimal(108))
    #expect(status.lastSignal?.stopPrice ?? 0 > Decimal(108))
    #expect(abs(NSDecimalNumber(decimal: status.lastSignal?.stopPrice ?? 0).doubleValue - 108.50666666666667) < 1e-9)
    if let targets = status.lastSignal?.takePrices {
        #expect(zip(targets, targets.dropFirst()).allSatisfy { $0 > $1 })
    }
    let repeated = engine.evaluateHLSR(config: hlsrConfig(), candles15m: fixtures.lower,
                                       fourHourCandles: fixtures.fourHour, previous: status)
    #expect(repeated.lastSignal?.id == status.lastSignal?.id)
    #expect(repeated.cooldown == status.cooldown)
}

@Test
func hlsrFailsClosedWhenQuoteVolumeIsMissing() {
    var fixtures = hlsrFixtures()
    fixtures.lower[100] = Candle(timestamp: fixtures.lower[100].timestamp,
                                 open: 100, high: 108, low: 98, close: 99,
                                 volume: 10_000_000, quoteVolume: nil)
    let status = StrategyEngine().evaluateHLSR(config: hlsrConfig(), candles15m: fixtures.lower,
                                               fourHourCandles: fixtures.fourHour)
    #expect(status.lastSignal == nil)
}

@Test
func hlsrDoesNotUseContractVolumeForHardLiquidityFilter() {
    let fixtures = hlsrFixtures()
    let lowQuote = fixtures.lower.map { candle in
        Candle(timestamp: candle.timestamp, open: candle.open, high: candle.high, low: candle.low,
               close: candle.close, volume: 50_000_000, quoteVolume: 1, confirmed: candle.confirmed)
    }
    let status = StrategyEngine().evaluateHLSR(config: hlsrConfig(), candles15m: lowQuote,
                                               fourHourCandles: fixtures.fourHour)
    #expect(status.lastSignal == nil)
}

@Test
func hlsrRejectsMultipleOrDelayedUnconfirmedBars() {
    let fixtures = hlsrFixtures()
    var multiple = fixtures.lower
    let prior = multiple[101]
    multiple[101] = Candle(timestamp: prior.timestamp, open: prior.open, high: prior.high,
                            low: prior.low, close: prior.close, volume: prior.volume,
                            quoteVolume: prior.quoteVolume, confirmed: false)
    let engine = StrategyEngine()
    let multipleStatus = engine.evaluateHLSR(config: hlsrConfig(), candles15m: multiple,
                                             fourHourCandles: fixtures.fourHour)
    #expect(multipleStatus.lastSignal == nil)

    var delayed = fixtures.lower
    let opening = delayed.removeLast()
    delayed.append(Candle(timestamp: opening.timestamp.addingTimeInterval(30 * 60),
                          open: opening.open, high: opening.high, low: opening.low,
                          close: opening.close, volume: opening.volume,
                          quoteVolume: opening.quoteVolume, confirmed: false))
    let delayedStatus = engine.evaluateHLSR(config: hlsrConfig(), candles15m: delayed,
                                            fourHourCandles: fixtures.fourHour)
    #expect(delayedStatus.lastSignal == nil)
}

@Test
func hlsrRejectsRestSnapshotWithConfirmedLatestBar() {
    let fixtures = hlsrFixtures()
    let latest = fixtures.lower.last!
    var confirmed = fixtures.lower
    confirmed[confirmed.count - 1] = Candle(timestamp: latest.timestamp, open: latest.open,
                                            high: latest.high, low: latest.low, close: latest.close,
                                            volume: latest.volume, quoteVolume: latest.quoteVolume,
                                            confirmed: true)
    let status = StrategyEngine().evaluateHLSR(config: hlsrConfig(), candles15m: confirmed,
                                               fourHourCandles: fixtures.fourHour)
    #expect(status.lastSignal == nil)
}

@Test
func hlsrDoesNotUseAnIncompleteFourHourRegime() {
    let fixtures = hlsrFixtures()
    let incomplete = fixtures.fourHour.map { candle in
        Candle(timestamp: candle.timestamp, open: candle.open, high: candle.high, low: candle.low,
               close: candle.close, volume: candle.volume, quoteVolume: candle.quoteVolume, confirmed: false)
    }
    let status = StrategyEngine().evaluateHLSR(config: hlsrConfig(), candles15m: fixtures.lower,
                                               fourHourCandles: incomplete)
    #expect(status.lastSignal == nil)
}
