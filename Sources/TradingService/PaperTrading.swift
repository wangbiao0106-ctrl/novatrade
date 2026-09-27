import Foundation
import TradingDomain

public actor CandleStore {
    private var candlesByKey: [String: [Candle]] = [:]
    private let capacity: Int

    public init(capacity: Int = 500) { self.capacity = max(10, capacity) }

    public func ingest(_ candle: Candle, instrumentID: String, interval: KlineInterval) {
        let key = "\(instrumentID):\(interval.rawValue)"
        var values = candlesByKey[key, default: []]
        values.upsert(candle)
        if values.count > capacity { values.removeFirst(values.count - capacity) }
        candlesByKey[key] = values
    }

    public func ingest(_ candles: [Candle], instrumentID: String, interval: KlineInterval) {
        for candle in candles { ingest(candle, instrumentID: instrumentID, interval: interval) }
    }

    public func values(instrumentID: String, interval: KlineInterval) -> [Candle] {
        candlesByKey["\(instrumentID):\(interval.rawValue)"] ?? []
    }

    public func removeAll() { candlesByKey.removeAll() }
}

public actor RiskEngine {
    public let limits: RiskLimits
    private var equity: Decimal
    private var equityPeak: Decimal
    private var dayStartEquity: Decimal
    private var hasSynchronizedEquity = false
    /// Calendar day that `dayStartEquity` was captured for. Advances only when
    /// a later calendar day is observed, so live trading rolls the daily
    /// baseline at midnight and historical candle ingestion rolls it in
    /// candle-time order.
    private var dayStartBoundary: Date?
    private var realizedPnL: Decimal = 0
    private var orderTimes: [Date] = []
    private var notionals: [String: Decimal] = [:]
    private var killSwitch = false
    private var reason: String?

    public init(limits: RiskLimits = RiskLimits(), initialEquity: Decimal = 100_000) {
        self.limits = limits
        self.equity = initialEquity
        self.equityPeak = initialEquity
        self.dayStartEquity = initialEquity
    }

    public func authorize(instrumentID: String, notional: Decimal, margin: Decimal, reduceOnly: Bool = false, now: Date = .now) -> RiskDecision {
        refresh(now: now)
        if notional <= 0 { return RiskDecision(allowed: false, reason: "名义价值必须大于 0") }
        if margin < 0 { return RiskDecision(allowed: false, reason: "保证金不能为负") }
        // Exits remain available after a kill switch, but malformed orders
        // must never bypass the input checks above.
        if reduceOnly { return RiskDecision(allowed: true) }
        if killSwitch { return RiskDecision(allowed: false, reason: reason ?? "风险熔断") }
        if notionals[instrumentID, default: 0] + notional > limits.maxInstrumentNotional {
            return RiskDecision(allowed: false, reason: "单标的名义价值超过上限")
        }
        if notionals.values.reduce(0, +) + notional > limits.maxTotalNotional {
            return RiskDecision(allowed: false, reason: "总名义价值超过上限")
        }
        if equity > 0, margin / equity * 100 > limits.maxMarginPercent {
            return RiskDecision(allowed: false, reason: "单笔保证金超过权益比例")
        }
        let hourAgo = now.addingTimeInterval(-3600)
        orderTimes.removeAll { $0 < hourAgo }
        if orderTimes.count >= limits.maxOrdersPerHour { return RiskDecision(allowed: false, reason: "每小时下单次数超过上限") }
        if let last = orderTimes.last, now.timeIntervalSince(last) < Double(limits.minOrderIntervalSeconds) {
            return RiskDecision(allowed: false, reason: "下单间隔不足")
        }
        orderTimes.append(now)
        notionals[instrumentID, default: 0] += notional
        return RiskDecision(allowed: true)
    }

    public func release(instrumentID: String, notional: Decimal) {
        notionals[instrumentID] = max(0, notionals[instrumentID, default: 0] - notional)
    }

    /// Reconciles the local risk baseline with the authenticated account before
    /// an exchange order is authorized. The default equity is only a fallback
    /// for standalone paper-broker use; live and demo orders must use the
    /// account's current equity instead.
    public func synchronizeEquity(_ value: Decimal, now: Date = .now) {
        guard value > 0 else { return }
        let calendar = Calendar(identifier: .gregorian)
        let dayStart = calendar.startOfDay(for: now)
        if !hasSynchronizedEquity {
            // The default equity is only a standalone-paper fallback. On the
            // first authenticated read, establish all baselines from the real
            // account instead of treating the difference as a loss.
            equity = value
            equityPeak = value
            dayStartEquity = value
            dayStartBoundary = dayStart
            hasSynchronizedEquity = true
            refresh(now: now)
            return
        }
        equity = value
        equityPeak = max(equityPeak, value)
        if dayStartBoundary.map({ dayStart > $0 }) == true {
            dayStartEquity = value
            dayStartBoundary = dayStart
        }
        refresh(now: now)
    }

    public func record(realizedPnL: Decimal, now: Date = .now) {
        self.realizedPnL += realizedPnL
        equity += realizedPnL
        equityPeak = max(equityPeak, equity)
        refresh(now: now)
    }

    public func snapshot(now: Date = .now) -> RiskSnapshot {
        refresh(now: now)
        let daily = dayStartEquity == 0 ? 0 : (equity - dayStartEquity) / dayStartEquity * 100
        let drawdown = equityPeak == 0 ? 0 : (equityPeak - equity) / equityPeak * 100
        return RiskSnapshot(equity: equity, equityPeak: equityPeak, dayStartEquity: dayStartEquity, dailyPnLPercent: daily, drawdownPercent: drawdown, killSwitch: killSwitch, reason: reason)
    }

    private func refresh(now: Date) {
        let calendar = Calendar(identifier: .gregorian)
        let dayStart = calendar.startOfDay(for: now)
        if let boundary = dayStartBoundary {
            if dayStart > boundary {
                dayStartEquity = equity
                dayStartBoundary = dayStart
            }
        } else {
            dayStartBoundary = dayStart
        }
        let daily = dayStartEquity == 0 ? 0 : (equity - dayStartEquity) / dayStartEquity * 100
        let drawdown = equityPeak == 0 ? 0 : (equityPeak - equity) / equityPeak * 100
        if daily <= -limits.maxDailyLossPercent { killSwitch = true; reason = "单日亏损熔断" }
        if drawdown >= limits.maxDrawdownPercent { killSwitch = true; reason = "最大回撤熔断" }
    }
}

public actor PaperBroker {
    public let feeRate: Decimal
    public let slippageBps: Decimal
    private let risk: RiskEngine
    private var pending: [PaperOrder] = []
    private var orders: [PaperOrder] = []
    private var fills: [PaperFill] = []
    private var positions: [String: PaperPosition] = [:]
    // PaperOrder predates reduceOnly and is persisted without that field. Keep
    // the execution-only flag separately so the public ledger stays compatible.
    private var reduceOnlyOrderIDs: Set<UUID> = []
    // Risk reservations are based on the submitted reference price. Retain
    // that amount per pending order so a normal closing/reversing order can
    // release only the portion that actually closed the existing position.
    private var reservedNotionals: [UUID: Decimal] = [:]

    public init(risk: RiskEngine = RiskEngine(), feeRate: Decimal = 0.0005, slippageBps: Decimal = 2) {
        self.risk = risk
        self.feeRate = feeRate
        self.slippageBps = slippageBps
    }

    public func submit(strategyID: UUID, instrumentID: String, side: String, quantity: Decimal, referencePrice: Decimal, requestedAt: Date = .now, reduceOnly: Bool = false) async -> Result<PaperOrder, Error> {
        let normalizedSide = side.lowercased()
        guard ["long", "short"].contains(normalizedSide) else {
            return .failure(BrokerError.invalidOrder("方向必须是 long 或 short"))
        }
        guard quantity > 0, referencePrice > 0 else {
            return .failure(BrokerError.invalidOrder("数量和参考价格必须大于 0"))
        }
        let notional = abs(quantity * referencePrice)
        let decision = await risk.authorize(instrumentID: instrumentID, notional: notional, margin: notional, reduceOnly: reduceOnly, now: requestedAt)
        guard decision.allowed else { return .failure(BrokerError.riskRejected(decision.reason ?? "风险拒绝")) }
        let order = PaperOrder(strategyID: strategyID, instrumentID: instrumentID, side: normalizedSide, quantity: quantity, requestedAt: requestedAt)
        pending.append(order)
        orders.append(order)
        if reduceOnly { reduceOnlyOrderIDs.insert(order.id) }
        else { reservedNotionals[order.id] = notional }
        return .success(order)
    }

    public func processNextOpen(instrumentID: String, candle: Candle) async {
        let due = pending.filter { $0.instrumentID == instrumentID && $0.requestedAt < candle.timestamp }
        pending.removeAll { order in due.contains(where: { $0.id == order.id }) }
        for order in due { await fill(order, at: candle.open, timestamp: candle.timestamp) }
    }

    public func mark(instrumentID: String, price: Decimal, at timestamp: Date = .now) {
        guard let position = positions[instrumentID] else { return }
        let sign: Decimal = position.side == "short" ? -1 : 1
        let pnl = (price - position.entryPrice) * position.quantity * sign
        positions[instrumentID] = PaperPosition(id: position.id, instrumentID: position.instrumentID, side: position.side, quantity: position.quantity, entryPrice: position.entryPrice, markPrice: price, unrealizedPnL: pnl, updatedAt: timestamp)
    }

    public func allOrders() -> [PaperOrder] { orders }
    public func allFills() -> [PaperFill] { fills }
    public func allPositions() -> [PaperPosition] { Array(positions.values) }
    public func riskSnapshot() async -> RiskSnapshot { await risk.snapshot() }

    private func fill(_ order: PaperOrder, at open: Decimal, timestamp: Date) async {
        let reduceOnly = reduceOnlyOrderIDs.remove(order.id) != nil
        let reservedOrderNotional = reservedNotionals.removeValue(forKey: order.id)
        let sign: Decimal = order.side == "short" ? -1 : 1
        let slip = open * slippageBps / 10_000 * sign
        let price = open + slip
        guard let existing = positions[order.instrumentID] else {
            if reduceOnly {
                let rejected = PaperOrder(id: order.id, strategyID: order.strategyID, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity, requestedAt: order.requestedAt, status: "rejected", remoteOrderID: order.remoteOrderID)
                if let index = orders.firstIndex(where: { $0.id == order.id }) { orders[index] = rejected }
                return
            }
            let fee = abs(price * order.quantity) * feeRate
            let filled = PaperOrder(id: order.id, strategyID: order.strategyID, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity, requestedAt: order.requestedAt, fillPrice: price, status: "filled", remoteOrderID: order.remoteOrderID)
            if let index = orders.firstIndex(where: { $0.id == order.id }) { orders[index] = filled }
            fills.append(PaperFill(orderID: order.id, price: price, quantity: order.quantity, fee: fee, timestamp: timestamp))
            positions[order.instrumentID] = PaperPosition(instrumentID: order.instrumentID, side: order.side, quantity: order.quantity, entryPrice: price, markPrice: price, updatedAt: timestamp)
            return
        }
        // A reduce-only order can only close the opposite side; it must never
        // create a new position when the requested side already matches.
        if reduceOnly, existing.side == order.side {
            let rejected = PaperOrder(id: order.id, strategyID: order.strategyID, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity, requestedAt: order.requestedAt, status: "rejected", remoteOrderID: order.remoteOrderID)
            if let index = orders.firstIndex(where: { $0.id == order.id }) { orders[index] = rejected }
            return
        }
        if existing.side != order.side {
            // Closing trade. A partial close reduces the position instead of
            // wiping it out, and released notional returns to the risk pool.
            let closeQuantity = min(existing.quantity, order.quantity)
            let executedQuantity = reduceOnly ? closeQuantity : order.quantity
            let fee = abs(price * executedQuantity) * feeRate
            let filled = PaperOrder(id: order.id, strategyID: order.strategyID, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity, requestedAt: order.requestedAt, fillPrice: price, status: "filled", remoteOrderID: order.remoteOrderID)
            if let index = orders.firstIndex(where: { $0.id == order.id }) { orders[index] = filled }
            fills.append(PaperFill(orderID: order.id, price: price, quantity: executedQuantity, fee: fee, timestamp: timestamp))
            let direction: Decimal = existing.side == "short" ? -1 : 1
            await risk.record(realizedPnL: (price - existing.entryPrice) * closeQuantity * direction - fee, now: timestamp)
            await risk.release(instrumentID: order.instrumentID, notional: closeQuantity * existing.entryPrice)
            let remaining = existing.quantity - closeQuantity
            // A normal order reserves risk for its full requested quantity.
            // Release the fraction used to close the old position; any
            // residual reversal remains reserved for the new position.
            if !reduceOnly, let reservedOrderNotional {
                let closedReservation = reservedOrderNotional * closeQuantity / order.quantity
                await risk.release(instrumentID: order.instrumentID, notional: closedReservation)
            }
            if remaining > 0 {
                positions[order.instrumentID] = PaperPosition(id: existing.id, instrumentID: order.instrumentID, side: existing.side, quantity: remaining, entryPrice: existing.entryPrice, markPrice: price, updatedAt: timestamp)
            } else if !reduceOnly, order.quantity > closeQuantity {
                // A normal order that exceeds the current position reverses
                // the residual quantity instead of silently losing it.
                positions[order.instrumentID] = PaperPosition(id: existing.id, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity - closeQuantity, entryPrice: price, markPrice: price, updatedAt: timestamp)
            } else {
                positions.removeValue(forKey: order.instrumentID)
            }
        } else {
            // Adding to an existing position merges the average entry price.
            let totalQuantity = existing.quantity + order.quantity
            guard totalQuantity != 0 else { positions.removeValue(forKey: order.instrumentID); return }
            let blendedEntry = (existing.entryPrice * existing.quantity + price * order.quantity) / totalQuantity
            positions[order.instrumentID] = PaperPosition(id: existing.id, instrumentID: order.instrumentID, side: existing.side, quantity: totalQuantity, entryPrice: blendedEntry, markPrice: price, updatedAt: timestamp)
        }
    }

    public enum BrokerError: LocalizedError, Sendable {
        case riskRejected(String)
        case invalidOrder(String)
        public var errorDescription: String? {
            switch self {
            case let .riskRejected(message), let .invalidOrder(message): return message
            }
        }
    }
}
