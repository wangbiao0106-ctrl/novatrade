import Foundation
import Testing
import TradingDomain
@testable import TradingService

private let hlsrSpec = SwapInstrumentSpec(
    instrumentID: "ALT-USDT-SWAP", ctVal: 1, lotSize: 1, minSize: 1, tickSize: 0.1,
    state: "live", ctType: "linear", settleCurrency: "USDT"
)

private func hlsrSignal(
    strategyID: UUID = UUID(),
    stop: Decimal = 110,
    targets: [Decimal] = [95, 90, 85],
    fractions: [Decimal] = [0.3, 0.3, 0.4],
    invalidation: Decimal? = 105
) -> StrategySignal {
    StrategySignal(
        strategyID: strategyID, type: "entry_short", price: 100,
        reason: "test", stopPrice: stop, takePrice: targets.first,
        takePrices: targets, targetFractions: fractions,
        moveStopToEntryAfterTP1: true, trailBars: 2,
        invalidationPrice: invalidation
    )
}

private func candle(_ timestamp: TimeInterval, open: Decimal, high: Decimal, low: Decimal, close: Decimal, confirmed: Bool = true) -> Candle {
    Candle(timestamp: Date(timeIntervalSince1970: timestamp), open: open, high: high, low: low, close: close, volume: 1, quoteVolume: 1, confirmed: confirmed)
}

private func position(_ quantity: Decimal, entry: Decimal = 100) -> PositionSnapshot {
    PositionSnapshot(instrumentID: "ALT-USDT-SWAP", side: "short", quantity: quantity, entryPrice: entry, markPrice: entry)
}

@Test("HLSR manager rounds actual fills and persists its pending claim")
func hlsrManagerRoundsAndPersistsPendingLeg() throws {
    let strategyID = UUID()
    var manager = try HLSRPositionManager(
        strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(strategyID: strategyID),
        spec: hlsrSpec, requestedQuantity: 10.9, actualFilledQuantity: 9.8, actualFillPrice: 100
    )
    #expect(manager.initialQuantity == 9)
    #expect(manager.remainingQuantity == 9)

    let intent = manager.evaluate(candle: candle(0, open: 99, high: 100, low: 94, close: 96), position: position(9))
    #expect(intent?.reason == .target1)
    #expect(intent?.quantity == 2) // 9 × 30% rounds down to whole contracts
    let leg = try #require(intent.flatMap { manager.claim($0) })
    #expect(manager.evaluate(candle: candle(900, open: 99, high: 100, low: 94, close: 96), position: position(9)) == nil)
    let restored = try JSONDecoder().decode(HLSRPositionManager.self, from: JSONEncoder().encode(manager))
    #expect(restored.pendingLeg?.id == leg.id)
    #expect(restored.remainingQuantity == 9)
}

@Test("HLSR stop has priority and waits for actual flat confirmation")
func hlsrStopPriorityAndConfirmation() throws {
    let strategyID = UUID()
    var manager = try HLSRPositionManager(strategyID: strategyID, instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(strategyID: strategyID), spec: hlsrSpec, requestedQuantity: 10)
    let maybeIntent = manager.evaluate(candle: candle(0, open: 111, high: 115, low: 80, close: 90), position: position(10))
    let intent = try #require(maybeIntent)
    #expect(intent.reason == .stopLoss)
    #expect(intent.quantity == 10)
    let maybeLeg = manager.claim(intent)
    let leg = try #require(maybeLeg)
    let unchanged = manager.confirm(leg.id, position: position(10))
    #expect(unchanged == false) // submit acknowledgement is not a fill
    let filled = manager.confirm(leg.id, position: position(0))
    #expect(filled == true)
    #expect(manager.isFlat)
    #expect(manager.evaluate(price: 80, position: position(0)) == nil)
}

@Test("HLSR keeps a claimed leg pending across partial fills")
func hlsrPartialFillConfirmation() throws {
    var manager = try HLSRPositionManager(strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(), spec: hlsrSpec, requestedQuantity: 10)
    let maybeIntent = manager.evaluate(candle: candle(0, open: 99, high: 100, low: 94, close: 96), position: position(10))
    let intent = try #require(maybeIntent)
    let maybeLeg = manager.claim(intent)
    let leg = try #require(maybeLeg)
    let partial = manager.confirm(leg.id, position: position(8))
    #expect(partial == false)
    #expect(manager.pendingLeg?.id == leg.id)
    #expect(manager.remainingQuantity == 8)
    let complete = manager.confirm(leg.id, position: position(7))
    #expect(complete)
    #expect(manager.pendingLeg == nil)
    #expect(manager.nextTargetIndex == 1)
}

@Test("HLSR retries only the residual quantity after a partial leg fails")
func hlsrPartialFailedRetryUsesResidual() throws {
    var manager = try HLSRPositionManager(strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(), spec: hlsrSpec, requestedQuantity: 10)
    let firstMaybe = manager.evaluate(candle: candle(0, open: 99, high: 100, low: 94, close: 96), position: position(10))
    let first = try #require(firstMaybe)
    let legMaybe = manager.claim(first)
    let leg = try #require(legMaybe)
    #expect(manager.confirm(leg.id, position: position(8)) == false)
    let failed = manager.fail(leg.id)
    #expect(failed)
    let staleClaim = manager.claim(first)
    #expect(staleClaim == nil)
    let retryMaybe = manager.evaluate(candle: candle(900, open: 99, high: 100, low: 94, close: 96), position: position(8))
    let retry = try #require(retryMaybe)
    #expect(retry.reason == .target1)
    #expect(retry.quantity == 1)
}

@Test("HLSR TP1 moves stop to entry and TP2 enables two-bar trailing")
func hlsrTargetsBreakevenAndTrailing() throws {
    var manager = try HLSRPositionManager(strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(), spec: hlsrSpec, requestedQuantity: 10)
    let firstMaybe = manager.evaluate(candle: candle(0, open: 99, high: 100, low: 94, close: 96), position: position(10))
    let first = try #require(firstMaybe)
    #expect(first.reason == .target1)
    let leg1Maybe = manager.claim(first)
    let leg1 = try #require(leg1Maybe)
    let tp1Filled = manager.confirm(leg1.id, position: position(7))
    #expect(tp1Filled)
    #expect(manager.nextTargetIndex == 1)
    #expect(manager.activeStop == 100)

    let secondMaybe = manager.evaluate(candle: candle(900, open: 89, high: 92, low: 88, close: 90), position: position(7))
    let second = try #require(secondMaybe)
    #expect(second.reason == .target2)
    let leg2Maybe = manager.claim(second)
    let leg2 = try #require(leg2Maybe)
    let tp2Filled = manager.confirm(leg2.id, position: position(4))
    #expect(tp2Filled)
    #expect(manager.nextTargetIndex == 2)

    _ = manager.evaluate(candle: candle(1800, open: 94, high: 97, low: 92, close: 94), position: position(4))
    #expect(manager.activeStop == 97)
    _ = manager.evaluate(candle: candle(2700, open: 94, high: 96, low: 93, close: 94), position: position(4))
    #expect(manager.activeStop == 97)
    let trailingMaybe = manager.evaluate(candle: candle(3600, open: 98, high: 99, low: 90, close: 92), position: position(4))
    let trailingStop = try #require(trailingMaybe)
    #expect(trailingStop.reason == .stopLoss)
    #expect(trailingStop.quantity == 4)
}

@Test("HLSR consumes a confirmed candle once and never self-triggers a new trailing stop")
func hlsrTrailingCandleIsIdempotent() throws {
    var manager = try HLSRPositionManager(
        strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(),
        spec: hlsrSpec, requestedQuantity: 10
    )

    let firstMaybe = manager.evaluate(
        candle: candle(0, open: 99, high: 100, low: 94, close: 96),
        position: position(10)
    )
    let first = try #require(firstMaybe)
    let leg1Maybe = manager.claim(first)
    let leg1 = try #require(leg1Maybe)
    let leg1Confirmed = manager.confirm(leg1.id, position: position(7))
    #expect(leg1Confirmed)

    let secondMaybe = manager.evaluate(
        candle: candle(900, open: 89, high: 92, low: 88, close: 90),
        position: position(7)
    )
    let second = try #require(secondMaybe)
    let leg2Maybe = manager.claim(second)
    let leg2 = try #require(leg2Maybe)
    let leg2Confirmed = manager.confirm(leg2.id, position: position(4))
    #expect(leg2Confirmed)

    // TP2's bar was inspected before its remote fill was confirmed. Replaying
    // it after confirmation must not seed the trailing window retroactively.
    #expect(manager.evaluate(candle: candle(900, open: 89, high: 92, low: 88, close: 90), position: position(4)) == nil)
    #expect(manager.trailingHighs.isEmpty)

    // The bar's high tightens the stop from 100 to 97 only after its old stop
    // was checked. Replaying that same bar must not turn the new 97 stop into
    // a same-bar stop fill, nor append the high a second time.
    let trailingBar = candle(1_800, open: 94, high: 97, low: 92, close: 94)
    #expect(manager.evaluate(candle: trailingBar, position: position(4)) == nil)
    #expect(manager.activeStop == 97)
    #expect(manager.trailingHighs == [97])
    #expect(manager.evaluate(candle: trailingBar, position: position(4)) == nil)
    #expect(manager.trailingHighs == [97])
    #expect(manager.lastProcessedConfirmedCandleTimestamp == trailingBar.timestamp)
}

@Test("HLSR does not reuse pre-TP1 high when a confirmed candle is replayed")
func hlsrBreakevenReplayDoesNotUseOldHigh() throws {
    var manager = try HLSRPositionManager(
        strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(),
        spec: hlsrSpec, requestedQuantity: 10
    )
    let firstMaybe = manager.evaluate(
        candle: candle(0, open: 99, high: 108, low: 94, close: 96),
        position: position(10)
    )
    let first = try #require(firstMaybe)
    let legMaybe = manager.claim(first)
    let leg = try #require(legMaybe)
    let legConfirmed = manager.confirm(leg.id, position: position(7))
    #expect(legConfirmed)
    #expect(manager.activeStop == 100)

    // TP1 was confirmed after the bar was inspected. Its old high of 108 must
    // not be treated as a breakeven stop hit at 100.
    #expect(manager.evaluate(candle: candle(0, open: 99, high: 108, low: 94, close: 96), position: position(7)) == nil)
}

@Test("HLSR intrabarRange false uses the live mark instead of candle OHLC")
func hlsrConfirmedRangeCanBeDisabled() throws {
    var manager = try HLSRPositionManager(
        strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(),
        spec: hlsrSpec, requestedQuantity: 10
    )
    let bar = candle(0, open: 99, high: 100, low: 94, close: 96)
    #expect(manager.evaluate(candle: bar, price: 98, position: position(10), intrabarRange: false) == nil)
    let intentMaybe = manager.evaluate(candle: bar, price: 94, position: position(10), intrabarRange: false)
    let intent = try #require(intentMaybe)
    #expect(intent.reason == .target1)
}

@Test("HLSR invalidation requires a confirmed close and failed legs can retry")
func hlsrInvalidationAndRetry() throws {
    var manager = try HLSRPositionManager(strategyID: UUID(), instrumentID: "ALT-USDT-SWAP", signal: hlsrSignal(), spec: hlsrSpec, requestedQuantity: 10)
    #expect(manager.evaluate(candle: candle(0, open: 100, high: 106, low: 99, close: 106, confirmed: false), position: position(10)) == nil)
    let intentMaybe = manager.evaluate(candle: candle(900, open: 100, high: 104, low: 99, close: 106), position: position(10))
    let intent = try #require(intentMaybe)
    #expect(intent.reason == .invalidation)
    let legMaybe = manager.claim(intent)
    let leg = try #require(legMaybe)
    let failed = manager.fail(leg.id)
    #expect(failed)
    #expect(manager.pendingLeg == nil)
    let retry = manager.claim(intent)
    #expect(retry != nil)
}
