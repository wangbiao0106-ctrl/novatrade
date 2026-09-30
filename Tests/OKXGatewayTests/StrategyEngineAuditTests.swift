import Foundation
import Testing
import TradingDomain
@testable import TradingService

@Test
func priorSMAExcludesTheCurrentBar() {
    let values = [10.0, 10.0, 10.0, 100.0]
    let mean = IndicatorCalculator.priorSMA(values, period: 3)
    #expect(mean[0] == 0)
    #expect(mean[1] == 10)
    #expect(mean[2] == 10)
    // Including the current spike would produce 40 instead of the causal 10.
    #expect(mean[3] == 10)
}

@Test
func disabledStrategyCannotKeepRunningStatus() {
    let id = UUID()
    let engine = StrategyEngine()
    let previous = StrategyStatus(id: id, state: .running, direction: "short", cooldown: 4)
    let config = StrategyConfig(
        id: id, name: "paused", instrumentID: "ALT-USDT-SWAP", interval: .oneHour,
        type: .sweepReversalShort, enabled: false
    )
    let candle = Candle(timestamp: Date(timeIntervalSince1970: 1_700_000_000), open: 10, high: 11, low: 9, close: 10)
    let status = engine.evaluate(config: config, candles: [candle], previous: previous)
    #expect(status.state == .paused)
    #expect(status.cooldown == 4)
    #expect(status.direction == "short")
}

private func sweepAuditCandles() -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    var candles: [Candle] = []
    var price = 90.0
    for index in 0..<319 {
        let high: Double
        let low: Double
        let close: Double
        var volume = 100.0
        switch index {
        case 0..<300:
            price += 0.02
            close = price; high = close + 0.3; low = close - 0.3
        case 300:
            close = 102; high = 103; low = 101.5
        case 301...305:
            price = 101.6; close = price; high = 102.4; low = 101.2
        case 306:
            close = 102.5; high = 104; low = 101.9; volume = 200
        case 307...317:
            close = 101.5; high = 102; low = 101.1
        default:
            close = 102.4; high = 103.5; low = 101.9
        }
        candles.append(Candle(timestamp: start.addingTimeInterval(Double(index) * 3600),
                              open: Decimal(close - 0.05), high: Decimal(high), low: Decimal(low),
                              close: Decimal(close), volume: Decimal(volume)))
    }
    return candles
}

@Test
func sweepGateFailsClosedWhenBTCDataIsOlderThanOneHour() {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    let signalTime = start.addingTimeInterval(318 * 3600)
    // Exactly 200 bars exist, but the nearest prior BTC bar is two hours old.
    let btcStart = signalTime.addingTimeInterval(-201 * 3600)
    let btc = (0..<200).map { index in
        let close = 200.0 - Double(index) * 0.42
        return Candle(timestamp: btcStart.addingTimeInterval(Double(index) * 3600),
                      open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1),
                      close: Decimal(close), volume: 100)
    }
    let config = StrategyConfig(
        name: "扫顶", instrumentID: "ALT-USDT-SWAP", interval: .oneHour,
        type: .sweepReversalShort,
        parameters: ["L": 10, "R": 5, "majorWindow": 288, "sweepWait": 96,
                     "rejectWait": 5, "resweepWait": 12, "rsiMin": 62,
                     "volMult": 1.5, "rsDeep": 0.2, "atrPeriod": 14,
                     "bufATR": 0.5, "tpMult": 2.2, "minATRPct": 0.5,
                     "maxRiskATR": 5, "btcGateEnabled": 1], enabled: true,
        cooldownBars: 96
    )
    let status = StrategyEngine().evaluate(config: config, candles: sweepAuditCandles(), btcCandles: btc)
    #expect(status.lastSignal == nil)
}
