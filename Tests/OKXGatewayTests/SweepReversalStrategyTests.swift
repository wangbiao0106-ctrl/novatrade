import Foundation
import Testing
import TradingDomain
@testable import TradingService

/// 高位二次扫顶做空策略引擎测试：与 Python 回测（strategy_sweep_reversal/engine.py）逐条对应。
/// 构造序列：缓涨 → 288 根高位摆动点 p → 首次扫顶 s（放量、收盘回落）→ 二次扫顶 j（更低高点、收盘回落，最后一根）。

private func makeSweepCandles(resweep: Bool = true) -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    var candles: [Candle] = []
    var price = 90.0
    for index in 0..<240 {
        var high: Double
        var low: Double
        var close: Double
        var volume: Double = 100
        switch index {
        case 0..<230:                    // 缓涨 + 右臂前段
            price += 0.043
            close = price
            high = close + 0.3
            low = close - 0.3
        case 230:                        // 摆动高点 p：288 根最高
            price += 0.043
            close = 102.0
            high = 103.0
            low = 101.5
        case 231...235:                  // 右臂（不高于 p）
            price = 101.6
            close = price
            high = 102.4
            low = 101.2
        case 236:                        // 首次扫顶 s：放量、收盘回落
            close = 102.5
            high = 104.0
            low = 101.9
            volume = 200
        case 237...238:                  // 中间整理
            close = 101.5
            high = 102.0
            low = 101.1
        case 239:                        // 二次扫顶 j（最后一根）
            if resweep {
                close = 102.4
                high = 103.5
                low = 101.9
            } else {
                close = 101.5
                high = 102.2
                low = 101.1
            }
        default:
            close = price
            high = close + 0.3
            low = close - 0.3
        }
        candles.append(Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close - 0.05), high: Decimal(high), low: Decimal(low), close: Decimal(close), volume: Decimal(volume), confirmed: true))
    }
    return candles
}

/// BTC 1H：前高后低 → 收盘低于 SMA200（门控开）
private func makeBTCBearishCandles() -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    return (0..<240).map { index in
        let close = 200.0 - Double(index) * 0.42   // 200 → 100
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1), close: Decimal(close), volume: 100, confirmed: true)
    }
}

/// BTC 1H：前低后高 → 收盘高于 SMA200（门控关）
private func makeBTCBullishCandles() -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    return (0..<240).map { index in
        let close = 100.0 + Double(index) * 0.42   // 100 → 200
        return Candle(timestamp: start.addingTimeInterval(Double(index) * 3600), open: Decimal(close), high: Decimal(close + 1), low: Decimal(close - 1), close: Decimal(close), volume: 100, confirmed: true)
    }
}

private func makeSweepConfig() -> StrategyConfig {
    StrategyConfig(
        name: "山寨币高位二次扫顶做空", instrumentID: "SATS-USDT-SWAP", interval: .oneHour, type: .sweepReversalShort,
        parameters: ["L": 10, "R": 5, "majorWindow": 288, "sweepWait": 96, "rejectWait": 5,
                     "resweepWait": 12, "rsiMin": 62, "volMult": 1.5, "rsDeep": 0.2,
                     "bufATR": 0.5, "tpMult": 2.2, "minATRPct": 0.5, "maxRiskATR": 5.0, "btcGateEnabled": 1],
        enabled: true, cooldownBars: 96
    )
}

@Test
func sweepReversalEmitsShortSignalWithATRScaledStopAndTake() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles()
    let btc = makeBTCBearishCandles()
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    let signal = status.lastSignal
    #expect(signal != nil)
    #expect(signal?.type == "entry_short")
    #expect(status.direction == "short")
    // 入场价 = 二次扫顶 bar 收盘价
    let entry = NSDecimalNumber(decimal: candles[239].close).doubleValue
    #expect(abs(NSDecimalNumber(decimal: signal!.price).doubleValue - entry) < 1e-9)
    // 止损 = 扫顶期间最高价(104) + 0.5 × ATR14；止盈 = 入场 − 2.2 × 风险
    let atr = IndicatorCalculator.atrSMA(candles)[239]
    let ext = 104.0
    let expectedStop = ext + 0.5 * atr
    let expectedTake = entry - 2.2 * (expectedStop - entry)
    #expect(abs(NSDecimalNumber(decimal: signal!.stopPrice ?? 0).doubleValue - expectedStop) < 1e-9)
    #expect(abs(NSDecimalNumber(decimal: signal!.takePrice ?? 0).doubleValue - expectedTake) < 1e-9)
    // 冷却 = 96
    #expect(status.cooldown == 96)
    // 同一根 bar 重复评估不重复发信号
    let again = engine.evaluate(config: makeSweepConfig(), candles: candles, previous: status, btcCandles: btc)
    #expect(again.lastSignal?.id == signal?.id)
}

@Test
func sweepReversalBlockedWhenBTCAboveSMA200() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles()
    let btc = makeBTCBullishCandles()   // BTC 在 SMA200 上方 → 门控关
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    #expect(status.lastSignal == nil)
}

@Test
func sweepReversalNoSignalWithoutSecondSweep() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles(resweep: false)   // 只有一次扫顶
    let btc = makeBTCBearishCandles()
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles, btcCandles: btc)
    #expect(status.lastSignal == nil)
}

@Test
func sweepReversalNoSignalWithoutBTCCandles() {
    let engine = StrategyEngine()
    let candles = makeSweepCandles()
    // 未提供 BTC K 线 → 门控无法判定 → 不发信号（fail-safe）
    let status = engine.evaluate(config: makeSweepConfig(), candles: candles)
    #expect(status.lastSignal == nil)
}
