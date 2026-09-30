import Foundation
import TradingDomain

/// A small, persistence-friendly state machine for one HLSR short position.
///
/// The manager only decides *what* reduce-only leg should be submitted.  It
/// never calls an exchange client and it never treats a submitted order as
/// filled until a later position snapshot confirms that the actual position
/// quantity decreased.  This makes retries and service restarts safe.
public struct HLSRPositionManager: Codable, Equatable, Sendable {
    public enum ExitReason: String, Codable, Sendable {
        case stopLoss
        case target1
        case target2
        case target3
        case invalidation

        public var targetIndex: Int? {
            switch self {
            case .target1: return 0
            case .target2: return 1
            case .target3: return 2
            case .stopLoss, .invalidation: return nil
            }
        }
    }

    /// A pure instruction returned by ``evaluate``.  It is deliberately
    /// separate from ``PendingReduceOnlyLeg`` so callers must claim an
    /// instruction before submitting it remotely.
    public struct ExitIntent: Codable, Equatable, Sendable {
        public let reason: ExitReason
        public let quantity: Decimal
        public let price: Decimal
        public let targetIndex: Int?

        public init(reason: ExitReason, quantity: Decimal, price: Decimal, targetIndex: Int? = nil) {
            self.reason = reason
            self.quantity = quantity
            self.price = price
            self.targetIndex = targetIndex
        }
    }

    /// A claimed reduce-only leg.  The ID is stable across retries and is
    /// persisted with the manager so a restart cannot create a duplicate leg.
    public struct PendingReduceOnlyLeg: Codable, Equatable, Sendable, Identifiable {
        public let id: UUID
        public let intent: ExitIntent
        public let submittedAt: Date
        /// Position quantity expected after this leg is fully filled.  A
        /// remote order can be partially filled across snapshots; keeping the
        /// expected quantity lets ``confirm`` remain pending until the leg is
        /// actually complete instead of skipping a target early.
        public let expectedRemainingQuantity: Decimal

        public var reason: ExitReason { intent.reason }
        public var quantity: Decimal { intent.quantity }
        public var price: Decimal { intent.price }

        fileprivate init(intent: ExitIntent, expectedRemainingQuantity: Decimal, submittedAt: Date) {
            self.id = UUID()
            self.intent = intent
            self.submittedAt = submittedAt
            self.expectedRemainingQuantity = expectedRemainingQuantity
        }
    }

    public enum Error: Swift.Error, Equatable, Sendable {
        case invalidQuantity
        case invalidFillPrice
        case invalidStop
        case invalidTargets
        case invalidFractions
        case unsupportedInstrument
    }

    public let strategyID: UUID
    public let instrumentID: String
    public let signal: StrategySignal
    public let spec: SwapInstrumentSpec
    public let requestedQuantity: Decimal
    public let initialQuantity: Decimal
    public let entryPrice: Decimal
    public let initialStop: Decimal
    public let targets: [Decimal]
    public let targetFractions: [Decimal]
    public let invalidationPrice: Decimal?
    public let trailBars: Int

    public private(set) var remainingQuantity: Decimal
    public private(set) var nextTargetIndex: Int
    /// Quantity already filled for each target. This survives a canceled
    /// partially filled leg so a retry submits only the residual quantity.
    public private(set) var targetFilledQuantities: [Decimal]
    public private(set) var activeStop: Decimal
    public private(set) var trailingHighs: [Decimal]
    /// The last confirmed candle timestamp whose trailing window was consumed.
    /// Confirmed candles may be delivered repeatedly by websocket and REST
    /// reconciliation; this prevents duplicate highs from changing the window.
    public private(set) var lastProcessedConfirmedCandleTimestamp: Date?
    /// State observed when the last confirmed candle was consumed.  It lets a
    /// replay after a newly confirmed TP1/TP2 avoid applying that candle's
    /// already-seen high to the newly changed protection level.
    private var lastProcessedConfirmedNextTargetIndex: Int?
    private var lastProcessedConfirmedStop: Decimal?
    public private(set) var pendingLeg: PendingReduceOnlyLeg?
    public private(set) var completed: Bool
    public let openedAt: Date

    private static let tolerance = Decimal(string: "0.0000000001")!

    public init(strategyID: UUID,
                instrumentID: String,
                signal: StrategySignal,
                spec: SwapInstrumentSpec,
                requestedQuantity: Decimal,
                actualFilledQuantity: Decimal? = nil,
                actualFillPrice: Decimal? = nil,
                openedAt: Date = .now) throws {
        guard spec.isLiveUSDTLinearSwap else { throw Error.unsupportedInstrument }
        guard requestedQuantity.isFinite, requestedQuantity > 0 else { throw Error.invalidQuantity }
        let rawQuantity = actualFilledQuantity ?? requestedQuantity
        let quantity = Self.roundDown(rawQuantity, lot: spec.lotSize)
        guard quantity > 0, spec.accepts(contractQuantity: quantity) else { throw Error.invalidQuantity }
        let fillPrice = actualFillPrice ?? signal.price
        guard fillPrice.isFinite, fillPrice > 0 else { throw Error.invalidFillPrice }
        guard let stop = signal.stopPrice, stop.isFinite, stop > fillPrice else { throw Error.invalidStop }

        let rawTargets = signal.takePrices ?? signal.takePrice.map { [$0] } ?? []
        let cleanTargets = rawTargets.filter { $0.isFinite && $0 > 0 && $0 < fillPrice }
        guard !cleanTargets.isEmpty else { throw Error.invalidTargets }
        let fractions = signal.targetFractions ?? []
        let normalizedFractions: [Decimal]
        if fractions.count == cleanTargets.count,
           fractions.allSatisfy({ $0.isFinite && $0 > 0 }),
           fractions.reduce(0, +) <= 1.000000001 {
            normalizedFractions = fractions
        } else if cleanTargets.count == 3 {
            normalizedFractions = [Decimal(string: "0.3")!, Decimal(string: "0.3")!, Decimal(string: "0.4")!]
        } else {
            normalizedFractions = Array(repeating: Decimal(1) / Decimal(cleanTargets.count), count: cleanTargets.count)
        }
        guard normalizedFractions.count == cleanTargets.count else { throw Error.invalidFractions }

        self.strategyID = strategyID
        self.instrumentID = instrumentID
        self.signal = signal
        self.spec = spec
        self.requestedQuantity = requestedQuantity
        self.initialQuantity = quantity
        self.entryPrice = fillPrice
        self.initialStop = stop
        self.targets = cleanTargets
        self.targetFractions = normalizedFractions
        self.invalidationPrice = signal.invalidationPrice
        self.trailBars = max(1, signal.trailBars ?? 2)
        self.remainingQuantity = quantity
        self.nextTargetIndex = 0
        self.targetFilledQuantities = Array(repeating: .zero, count: cleanTargets.count)
        self.activeStop = stop
        self.trailingHighs = []
        self.lastProcessedConfirmedCandleTimestamp = nil
        self.lastProcessedConfirmedNextTargetIndex = nil
        self.lastProcessedConfirmedStop = nil
        self.pendingLeg = nil
        self.completed = false
        self.openedAt = openedAt
    }

    private enum CodingKeys: String, CodingKey {
        case strategyID, instrumentID, signal, spec, requestedQuantity
        case initialQuantity, entryPrice, initialStop, targets, targetFractions
        case invalidationPrice, trailBars, remainingQuantity, nextTargetIndex
        case targetFilledQuantities, activeStop, trailingHighs, pendingLeg
        case lastProcessedConfirmedCandleTimestamp, lastProcessedConfirmedNextTargetIndex
        case lastProcessedConfirmedStop
        case completed, openedAt
    }

    /// Decode old manager snapshots written before residual target progress
    /// was persisted. Missing progress starts at zero and remains safe.
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        strategyID = try values.decode(UUID.self, forKey: .strategyID)
        instrumentID = try values.decode(String.self, forKey: .instrumentID)
        signal = try values.decode(StrategySignal.self, forKey: .signal)
        spec = try values.decode(SwapInstrumentSpec.self, forKey: .spec)
        requestedQuantity = try values.decode(Decimal.self, forKey: .requestedQuantity)
        initialQuantity = try values.decode(Decimal.self, forKey: .initialQuantity)
        entryPrice = try values.decode(Decimal.self, forKey: .entryPrice)
        initialStop = try values.decode(Decimal.self, forKey: .initialStop)
        targets = try values.decode([Decimal].self, forKey: .targets)
        targetFractions = try values.decode([Decimal].self, forKey: .targetFractions)
        invalidationPrice = try values.decodeIfPresent(Decimal.self, forKey: .invalidationPrice)
        trailBars = try values.decode(Int.self, forKey: .trailBars)
        remainingQuantity = try values.decode(Decimal.self, forKey: .remainingQuantity)
        nextTargetIndex = try values.decode(Int.self, forKey: .nextTargetIndex)
        targetFilledQuantities = try values.decodeIfPresent([Decimal].self, forKey: .targetFilledQuantities)
            ?? Array(repeating: .zero, count: targets.count)
        if targetFilledQuantities.count != targets.count {
            targetFilledQuantities = Array(targetFilledQuantities.prefix(targets.count))
            if targetFilledQuantities.count < targets.count {
                targetFilledQuantities.append(contentsOf: Array(repeating: .zero, count: targets.count - targetFilledQuantities.count))
            }
        }
        activeStop = try values.decode(Decimal.self, forKey: .activeStop)
        trailingHighs = try values.decodeIfPresent([Decimal].self, forKey: .trailingHighs) ?? []
        lastProcessedConfirmedCandleTimestamp = try values.decodeIfPresent(Date.self, forKey: .lastProcessedConfirmedCandleTimestamp)
        lastProcessedConfirmedNextTargetIndex = try values.decodeIfPresent(Int.self, forKey: .lastProcessedConfirmedNextTargetIndex)
        lastProcessedConfirmedStop = try values.decodeIfPresent(Decimal.self, forKey: .lastProcessedConfirmedStop)
        pendingLeg = try values.decodeIfPresent(PendingReduceOnlyLeg.self, forKey: .pendingLeg)
        completed = try values.decode(Bool.self, forKey: .completed)
        openedAt = try values.decode(Date.self, forKey: .openedAt)
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(strategyID, forKey: .strategyID)
        try values.encode(instrumentID, forKey: .instrumentID)
        try values.encode(signal, forKey: .signal)
        try values.encode(spec, forKey: .spec)
        try values.encode(requestedQuantity, forKey: .requestedQuantity)
        try values.encode(initialQuantity, forKey: .initialQuantity)
        try values.encode(entryPrice, forKey: .entryPrice)
        try values.encode(initialStop, forKey: .initialStop)
        try values.encode(targets, forKey: .targets)
        try values.encode(targetFractions, forKey: .targetFractions)
        try values.encodeIfPresent(invalidationPrice, forKey: .invalidationPrice)
        try values.encode(trailBars, forKey: .trailBars)
        try values.encode(remainingQuantity, forKey: .remainingQuantity)
        try values.encode(nextTargetIndex, forKey: .nextTargetIndex)
        try values.encode(targetFilledQuantities, forKey: .targetFilledQuantities)
        try values.encode(activeStop, forKey: .activeStop)
        try values.encode(trailingHighs, forKey: .trailingHighs)
        try values.encodeIfPresent(lastProcessedConfirmedCandleTimestamp, forKey: .lastProcessedConfirmedCandleTimestamp)
        try values.encodeIfPresent(lastProcessedConfirmedNextTargetIndex, forKey: .lastProcessedConfirmedNextTargetIndex)
        try values.encodeIfPresent(lastProcessedConfirmedStop, forKey: .lastProcessedConfirmedStop)
        try values.encodeIfPresent(pendingLeg, forKey: .pendingLeg)
        try values.encode(completed, forKey: .completed)
        try values.encode(openedAt, forKey: .openedAt)
    }

    public var isFlat: Bool { completed || remainingQuantity <= Self.tolerance }

    /// Evaluates one mark/candle and returns at most one intent. Stop loss has
    /// priority over targets and invalidation. Confirmed closes are required
    /// for invalidation; protective stop and target checks can use an opening
    /// update so a gap through a level is handled at the worse open price.
    public mutating func evaluate(candle: Candle? = nil,
                                  price: Decimal? = nil,
                                  position: PositionSnapshot,
                                  now: Date = .now,
                                  intrabarRange: Bool = true) -> ExitIntent? {
        guard !isFlat else { return nil }
        guard pendingLeg == nil else { return nil }
        guard position.instrumentID == instrumentID else { return nil }
        let positionSide = position.side.lowercased()
        guard positionSide == "short" || (positionSide == "net" && position.quantity < 0) else { return nil }
        let actualQuantity = abs(position.quantity)
        guard actualQuantity.isFinite, actualQuantity > Self.tolerance else {
            completed = true
            remainingQuantity = 0
            return nil
        }
        // Never increase local exposure based on a remote snapshot. A smaller
        // quantity may be an exchange fill that arrived before its leg was
        // acknowledged; fold it into local state conservatively.
        if actualQuantity + Self.tolerance < remainingQuantity {
            remainingQuantity = Self.roundDown(actualQuantity, lot: spec.lotSize)
            if remainingQuantity <= Self.tolerance {
                completed = true
                return nil
            }
        }

        let confirmedReplay = candle?.confirmed == true &&
            lastProcessedConfirmedCandleTimestamp == candle?.timestamp
        // If TP1/TP2 was confirmed after this candle was first inspected, the
        // candle's high predates the new stop.  Replaying its OHLC range would
        // manufacture a same-bar breakeven/trailing stop hit.  A live mark is
        // still safe to inspect on the replay.
        let changedAfterCandle = confirmedReplay &&
            ((lastProcessedConfirmedNextTargetIndex.map { nextTargetIndex != $0 } ?? false) ||
             (lastProcessedConfirmedStop.map { activeStop != $0 } ?? false))
        // A confirmed candle is never replayed as an intrabar stop range. This
        // is what prevents a high that just tightened the stop from stopping
        // the same bar. Target retries remain range-aware when the target
        // index/protection did not advance (for example after a rejected
        // submission), while a newly confirmed TP cannot use the old bar.
        let useCandleStopRange = intrabarRange && !confirmedReplay
        let useCandleTargetRange = intrabarRange && !changedAfterCandle

        let stopTouched: Bool
        let stopExecution: Decimal
        if useCandleStopRange, let candle {
            stopTouched = candle.high >= activeStop
            stopExecution = candle.open >= activeStop ? candle.open : activeStop
        } else if let price, price.isFinite {
            stopTouched = price >= activeStop
            stopExecution = price >= activeStop ? price : activeStop
        } else {
            stopTouched = false
            stopExecution = activeStop
        }
        if stopTouched {
            return Self.intent(reason: .stopLoss, quantity: remainingQuantity, price: stopExecution)
        }

        if let candle, candle.confirmed,
           let invalidationPrice, invalidationPrice.isFinite,
           candle.close > invalidationPrice {
            return Self.intent(reason: .invalidation, quantity: remainingQuantity, price: candle.close)
        }

        guard nextTargetIndex < targets.count else {
            advanceConfirmedCandle(candle)
            return nil
        }
        let target = targets[nextTargetIndex]
        let targetTouched: Bool
        let targetExecution: Decimal
        if useCandleTargetRange, let candle {
            targetTouched = candle.low <= target
            targetExecution = candle.open <= target ? candle.open : target
        } else if let price, price.isFinite {
            targetTouched = price <= target
            targetExecution = target
        } else {
            targetTouched = false
            targetExecution = target
        }
        guard targetTouched else {
            advanceConfirmedCandle(candle)
            return nil
        }

        let isFinal = nextTargetIndex == targets.count - 1
        let targetSize = initialQuantity * targetFractions[nextTargetIndex]
        let alreadyFilled = targetFilledQuantities[nextTargetIndex]
        let requested = isFinal ? remainingQuantity : max(0, targetSize - alreadyFilled)
        let quantity = Self.roundDown(min(requested, remainingQuantity), lot: spec.lotSize)
        guard quantity > 0, spec.accepts(contractQuantity: quantity) else {
            advanceConfirmedCandle(candle)
            return nil
        }
        let reason: ExitReason = nextTargetIndex == 0 ? .target1 : nextTargetIndex == 1 ? .target2 : .target3
        // A TP2 candle is the bar that triggers the exit, and its high was
        // observed before the TP2 fill was confirmed. Do not let that bar
        // become the first trailing bar; the next confirmed bar starts the
        // post-TP2 trailing window. Other target levels can safely consume
        // the candle for idempotency.
        advanceConfirmedCandle(candle, includeTrailing: nextTargetIndex != 1)
        return Self.intent(reason: reason, quantity: quantity, price: targetExecution, targetIndex: nextTargetIndex)
    }

    /// Consumes a newly confirmed candle exactly once.  The update happens
    /// after stop/target checks, so a high that tightens a trailing stop cannot
    /// also trigger that newly tightened stop on the same bar.
    private mutating func advanceConfirmedCandle(_ candle: Candle?, includeTrailing: Bool = true) {
        guard let candle, candle.confirmed else { return }
        if let last = lastProcessedConfirmedCandleTimestamp, candle.timestamp <= last { return }
        if includeTrailing, nextTargetIndex >= 1 {
            trailingHighs.append(candle.high)
            if trailingHighs.count > trailBars {
                trailingHighs.removeFirst(trailingHighs.count - trailBars)
            }
            if nextTargetIndex >= 2,
               let highest = trailingHighs.max(), highest.isFinite {
                // A short trailing stop can only tighten downwards.  This
                // takes effect on the next candle because stop checks happen
                // before this method is called.
                activeStop = min(activeStop, highest)
            }
        }
        lastProcessedConfirmedCandleTimestamp = candle.timestamp
        lastProcessedConfirmedNextTargetIndex = nextTargetIndex
        lastProcessedConfirmedStop = activeStop
    }

    /// Claims an intent for remote submission. A second claim while a leg is
    /// pending returns nil, preventing duplicate reduce-only orders.
    public mutating func claim(_ intent: ExitIntent, now: Date = .now) -> PendingReduceOnlyLeg? {
        guard !isFlat, pendingLeg == nil,
              intent.quantity > 0, intent.quantity <= remainingQuantity + Self.tolerance,
              spec.accepts(contractQuantity: intent.quantity) else { return nil }
        if let index = intent.targetIndex {
            // Do not accept a stale pre-fill intent after a partial retry. Its
            // original 30% quantity could otherwise over-close the target and
            // consume contracts intended for the next level.
            guard index == nextTargetIndex, index < targetFilledQuantities.count else { return nil }
            let isFinal = index == targets.count - 1
            let targetSize = initialQuantity * targetFractions[index]
            let residual = isFinal
                ? remainingQuantity
                : max(0, targetSize - targetFilledQuantities[index])
            guard intent.quantity <= residual + Self.tolerance else { return nil }
        }
        let expectedRemaining = Self.roundDown(max(0, remainingQuantity - intent.quantity), lot: spec.lotSize)
        let leg = PendingReduceOnlyLeg(intent: intent, expectedRemainingQuantity: expectedRemaining, submittedAt: now)
        pendingLeg = leg
        return leg
    }

    /// Confirms a claimed leg only after the remote position quantity has
    /// actually decreased. Partial fills are accepted and leave the manager
    /// ready to claim another leg. No quantity is ever added to local state.
    @discardableResult
    public mutating func confirm(_ legID: UUID, position: PositionSnapshot) -> Bool {
        guard let pending = pendingLeg, pending.id == legID else { return false }
        guard position.instrumentID == instrumentID else { return false }
        let positionSide = position.side.lowercased()
        guard positionSide == "short" || (positionSide == "net" && position.quantity <= 0) else { return false }
        let actual = abs(position.quantity)
        guard actual.isFinite, actual >= 0,
              actual <= remainingQuantity + Self.tolerance else { return false }
        let expected = pending.expectedRemainingQuantity
        let previousRemaining = remainingQuantity
        // Keep the claim until the intended reduce-only quantity is fully
        // observed. The caller can safely poll and call confirm again. Fold a
        // partial fill into the remaining quantity while retaining the leg.
        let reduced = max(0, previousRemaining - actual)
        if let index = pending.reason.targetIndex, index < targetFilledQuantities.count, reduced > 0 {
            // A remote snapshot can include an unrelated reduction. Credit at
            // most the residual of this target so a retry cannot be starved or
            // advance the next target after more than one leg was filled.
            let targetSize = initialQuantity * targetFractions[index]
            let residual = max(0, targetSize - targetFilledQuantities[index])
            targetFilledQuantities[index] += min(reduced, residual)
        }
        if actual > expected + Self.tolerance {
            remainingQuantity = Self.roundDown(actual, lot: spec.lotSize)
            return false
        }
        remainingQuantity = Self.roundDown(actual, lot: spec.lotSize)
        pendingLeg = nil
        if let index = pending.reason.targetIndex {
            nextTargetIndex = max(nextTargetIndex, index + 1)
            if index == 0, signal.moveStopToEntryAfterTP1 { activeStop = min(activeStop, entryPrice) }
        }
        if remainingQuantity <= Self.tolerance {
            remainingQuantity = 0
            completed = true
        }
        return true
    }

    /// Clears a failed/rejected submission so the same level can be retried.
    @discardableResult
    public mutating func fail(_ legID: UUID) -> Bool {
        guard pendingLeg?.id == legID else { return false }
        pendingLeg = nil
        return true
    }

    /// Explicitly clear a fully reconciled position. This is idempotent and
    /// useful when an exchange reports a zero position after a final fill.
    public mutating func clearIfFlat(position: PositionSnapshot) -> Bool {
        guard position.instrumentID == instrumentID,
              abs(position.quantity) <= Self.tolerance else { return false }
        remainingQuantity = 0
        pendingLeg = nil
        completed = true
        return true
    }

    private static func intent(reason: ExitReason, quantity: Decimal, price: Decimal, targetIndex: Int? = nil) -> ExitIntent {
        ExitIntent(reason: reason, quantity: quantity, price: price, targetIndex: targetIndex)
    }

    private static func roundDown(_ value: Decimal, lot: Decimal) -> Decimal {
        guard value.isFinite, value > 0, lot.isFinite, lot > 0 else { return 0 }
        var units = value / lot
        var whole = Decimal.zero
        NSDecimalRound(&whole, &units, 0, .down)
        return whole * lot
    }
}
