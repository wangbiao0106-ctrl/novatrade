import Foundation
import TradingDomain

public actor CandleStore {
    private var candlesByKey: [String: [Candle]] = [:]
    private let capacity: Int

    public init(capacity: Int = 500) { self.capacity = max(10, capacity) }

    public func ingest(_ candle: Candle, instrumentID: String, interval: KlineInterval) {
        let key = "\(instrumentID):\(interval.rawValue)"
        var values = candlesByKey[key, default: []]
        // A stale REST snapshot or delayed WSS frame must not turn a confirmed
        // bar back into an open one. An unconfirmed bar remains in the history
        // until it scrolls out of the evaluation window.
        if let existing = values.first(where: { $0.timestamp == candle.timestamp }), existing.confirmed, !candle.confirmed {
            return
        }
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
    private static let utcCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }()

    private struct PoolState {
        var allocationPercent: Decimal
        var initialCapital: Decimal
        var equity: Decimal
        var reservedCapital: Decimal
        var realizedPnL: Decimal
        var unrealizedPnL: Decimal
        var rolloverCount: Int
        var updatedAt: Date
        /// 当前未平仓订单的止损风险合计（|入场价 − 止损价| × 数量）。
        /// 用于执行策略实验室的"开放止损风险 ≤ 池权益比例"上限。
        var openRisk: Decimal = 0
        /// 当前未平仓订单数，用于执行策略实验室的并发持仓上限。
        var openPositions: Int = 0

        func snapshot(strategyID: UUID) -> StrategyCapitalSnapshot {
            StrategyCapitalSnapshot(
                strategyID: strategyID,
                allocationPercent: allocationPercent,
                initialCapital: initialCapital,
                equity: equity,
                reservedCapital: reservedCapital,
                availableCapital: max(0, equity - reservedCapital),
                realizedPnL: realizedPnL,
                unrealizedPnL: unrealizedPnL,
                rolloverCount: rolloverCount,
                updatedAt: updatedAt,
                openRisk: openRisk,
                openPositions: openPositions
            )
        }
    }

    public let limits: RiskLimits
    private var equity: Decimal
    private var unrealizedPnL: Decimal = 0
    private var equityPeak: Decimal
    private var dayStartEquity: Decimal
    private var hasSynchronizedEquity = false
    /// An authenticated zero-equity account is a valid, fail-closed state.
    /// Keep it separate from the initial paper-broker fallback so callers can
    /// distinguish a real empty account from an account that has not synced.
    private var authenticatedZeroEquity = false
    /// UTC calendar day that `dayStartEquity` was captured for. Advances only
    /// when a later UTC day is observed, so live trading rolls the daily
    /// baseline at 00:00 UTC and historical candle ingestion rolls it in
    /// candle-time order.
    private var dayStartBoundary: Date?
    /// A persisted kill switch may be restored after UTC midnight. Keep this bit
    /// so the next manual reset is allowed even though `refresh` has already
    /// advanced the in-memory boundary to the current day.
    private var resetEligibleAfterDayChange = false
    /// When restoring after UTC midnight, the first authenticated account read is
    /// the authoritative equity for today's baseline.
    private var needsDayStartEquitySync = false
    private var realizedPnL: Decimal = 0
    private var orderTimes: [Date] = []
    private var notionals: [String: Decimal] = [:]
    private var killSwitch = false
    private var reason: String?
    private var pools: [UUID: PoolState] = [:]
    /// Authenticated USDT balance used for strategy pool sizing.  It is kept
    /// independent from `equity`, which is the all-asset account value used
    /// by global risk limits and the daily-loss circuit breaker.
    private var strategyCapitalBase: Decimal?

    public init(limits: RiskLimits = RiskLimits(), initialEquity: Decimal = 100_000) {
        self.limits = limits
        self.equity = initialEquity
        self.equityPeak = initialEquity
        self.dayStartEquity = initialEquity
    }

    public func registerStrategy(_ strategyID: UUID, allocationPercent: Decimal = 100, now: Date = .now) -> StrategyCapitalSnapshot {
        if let existing = pools[strategyID] { return existing.snapshot(strategyID: strategyID) }
        let allocation = strategyCapitalAllocation()
        let effectiveAllocation = allocation.effectiveAllocationPercent(for: allocationPercent)
        let capital = allocation.capital(for: effectiveAllocation)
        let state = PoolState(allocationPercent: effectiveAllocation, initialCapital: capital, equity: capital, reservedCapital: 0, realizedPnL: 0, unrealizedPnL: 0, rolloverCount: 0, updatedAt: now)
        pools[strategyID] = state
        return state.snapshot(strategyID: strategyID)
    }

    /// Updates an existing strategy's configured allocation without relying on
    /// `registerStrategy`'s idempotent behavior. The requested percentage is
    /// evaluated against all other pools, just as it is for a new strategy.
    /// Existing realized results and reservations are retained, but the pool's
    /// equity is capped at the newly allocated USDT capital so a resize cannot
    /// create spendable balance beyond the account's current allocation.
    public func updateStrategyAllocation(_ strategyID: UUID, allocationPercent: Decimal, now: Date = .now) -> StrategyCapitalSnapshot {
        guard var pool = pools[strategyID] else {
            return registerStrategy(strategyID, allocationPercent: allocationPercent, now: now)
        }
        let otherPools = pools
            .filter { $0.key != strategyID }
            .map { $0.value.snapshot(strategyID: $0.key) }
        let allocation = StrategyCapitalAllocation(totalCapital: strategyCapitalBase ?? equity, strategyCapitals: otherPools)
        let effective = allocation.effectiveAllocationPercent(for: allocationPercent)
        let capital = allocation.capital(for: effective)
        pool.allocationPercent = effective
        pool.initialCapital = capital
        // The authenticated USDT balance is authoritative for an idle pool;
        // a remote credit already reflected in that balance must not compound
        // again during an allocation resize.
        if pool.reservedCapital == 0 {
            pool.equity = capital
        } else {
            pool.equity = min(max(0, pool.equity), capital)
        }
        pool.updatedAt = now
        pools[strategyID] = pool
        return pool.snapshot(strategyID: strategyID)
    }

    public func removeStrategy(_ strategyID: UUID) {
        pools.removeValue(forKey: strategyID)
    }

    public func strategyCapital(_ strategyID: UUID, allocationPercent: Decimal = 100, now: Date = .now) -> StrategyCapitalSnapshot {
        _ = registerStrategy(strategyID, allocationPercent: allocationPercent, now: now)
        return pools[strategyID]!.snapshot(strategyID: strategyID)
    }

    public func strategyCapitals() -> [StrategyCapitalSnapshot] {
        pools.map { $0.value.snapshot(strategyID: $0.key) }.sorted { $0.strategyID.uuidString < $1.strategyID.uuidString }
    }

    public func hasStrategyPool(_ strategyID: UUID) -> Bool { pools[strategyID] != nil }

    /// `riskAmount` 是该订单的止损风险（|入场价 − 止损价| × 数量）；配合
    /// `maxOpenRiskPercent` / `maxConcurrentPositions` 执行策略实验室的组合上限。
    public func authorize(instrumentID: String, notional: Decimal, margin: Decimal, reduceOnly: Bool = false, now: Date = .now, strategyID: UUID? = nil, poolAllocationPercent: Decimal = 100, riskAmount: Decimal = 0, maxOpenRiskPercent: Decimal? = nil, maxConcurrentPositions: Int? = nil) -> RiskDecision {
        refresh(now: now)
        if instrumentID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return RiskDecision(allowed: false, reason: "合约不能为空") }
        if !notional.isFinite || notional <= 0 { return RiskDecision(allowed: false, reason: "名义价值必须是有限的正数") }
        if !margin.isFinite || margin < 0 { return RiskDecision(allowed: false, reason: "保证金必须是有限的非负数") }
        if !riskAmount.isFinite || riskAmount < 0 { return RiskDecision(allowed: false, reason: "止损风险必须是有限的非负数") }
        if let cap = maxOpenRiskPercent, !cap.isFinite || cap <= 0 { return RiskDecision(allowed: false, reason: "开放风险上限无效") }
        if let maxConcurrent = maxConcurrentPositions, maxConcurrent <= 0 { return RiskDecision(allowed: false, reason: "并发持仓上限无效") }
        // Exits remain available after a kill switch, but malformed orders
        // must never bypass the input checks above.
        if reduceOnly { return RiskDecision(allowed: true) }
        if authenticatedZeroEquity || equity <= 0 {
            return RiskDecision(allowed: false, reason: "账户权益为零")
        }
        if killSwitch { return RiskDecision(allowed: false, reason: reason ?? "风险熔断") }
        if strategyID == nil {
            // Account-level notional and margin ceilings bound standalone
            // (manual / paper) orders. A strategy order is sized to its own
            // isolated pool below, and that pool is its only size limit; the
            // daily-loss circuit breaker above still covers it.
            if notionals[instrumentID, default: 0] + notional > limits.maxInstrumentNotional {
                return RiskDecision(allowed: false, reason: "单标的名义价值超过上限")
            }
            if notionals.values.reduce(0, +) + notional > limits.maxTotalNotional {
                return RiskDecision(allowed: false, reason: "总名义价值超过上限")
            }
            if equity > 0, margin / equity * 100 > limits.maxMarginPercent {
                return RiskDecision(allowed: false, reason: "单笔保证金超过权益比例")
            }
        }
        if let strategyID {
            let pool = strategyCapital(strategyID, allocationPercent: poolAllocationPercent, now: now)
            guard margin <= pool.availableCapital else {
                return RiskDecision(allowed: false, reason: "策略资金池可用余额不足")
            }
            // 开放止损风险上限：按本策略资金池权益的百分比计算，累计已开仓风险 + 本单风险。
            if let cap = maxOpenRiskPercent {
                let limit = pool.equity * cap / 100
                if pool.openRisk + riskAmount > limit {
                    return RiskDecision(allowed: false, reason: "策略开放止损风险超过上限")
                }
            }
            if let maxConcurrent = maxConcurrentPositions,
               pool.openPositions + 1 > maxConcurrent {
                return RiskDecision(allowed: false, reason: "策略并发持仓超过上限")
            }
        }
        let hourAgo = now.addingTimeInterval(-3600)
        orderTimes.removeAll { $0 < hourAgo }
        if orderTimes.count >= limits.maxOrdersPerHour { return RiskDecision(allowed: false, reason: "每小时下单次数超过上限") }
        if let last = orderTimes.last, now.timeIntervalSince(last) < Double(limits.minOrderIntervalSeconds) {
            return RiskDecision(allowed: false, reason: "下单间隔不足")
        }
        orderTimes.append(now)
        notionals[instrumentID, default: 0] += notional
        if let strategyID, var pool = pools[strategyID] {
            pool.reservedCapital += margin
            pool.openRisk += riskAmount
            pool.openPositions += 1
            pool.updatedAt = now
            pools[strategyID] = pool
        }
        return RiskDecision(allowed: true)
    }

    /// 释放一笔授权。`closedPosition` 为真时同时释放一个并发名额（平仓 / 入场单失败）。
    public func release(instrumentID: String, notional: Decimal, strategyID: UUID? = nil, margin: Decimal? = nil, riskAmount: Decimal = 0, closedPosition: Bool = false) {
        guard !instrumentID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              notional.isFinite, notional >= 0,
              riskAmount.isFinite, riskAmount >= 0,
              margin.map({ $0.isFinite && $0 >= 0 }) ?? true else { return }
        notionals[instrumentID] = max(0, notionals[instrumentID, default: 0] - notional)
        if let strategyID, var pool = pools[strategyID] {
            pool.reservedCapital = max(0, pool.reservedCapital - (margin ?? notional))
            if riskAmount > 0 { pool.openRisk = max(0, pool.openRisk - riskAmount) }
            if closedPosition { pool.openPositions = max(0, pool.openPositions - 1) }
            pool.updatedAt = .now
            pools[strategyID] = pool
        }
    }

    /// Reconciles the local risk baseline with the authenticated account before
    /// an exchange order is authorized. The default equity is only a fallback
    /// for standalone paper-broker use; live and demo orders must use the
    /// account's current equity instead.
    public func synchronizeEquity(_ value: Decimal, now: Date = .now) {
        guard value.isFinite, value >= 0 else { return }
        let dayStart = Self.utcCalendar.startOfDay(for: now)
        if value == 0 {
            equity = 0
            unrealizedPnL = 0
            authenticatedZeroEquity = true
            hasSynchronizedEquity = true
            equityPeak = 0
            dayStartEquity = 0
            dayStartBoundary = dayStart
            killSwitch = true
            reason = "账户权益为零"
            refresh(now: now)
            return
        }
        authenticatedZeroEquity = false
        if !hasSynchronizedEquity {
            // The default equity is only a standalone-paper fallback. On the
            // first authenticated read, establish all baselines from the real
            // account instead of treating the difference as a loss. A peak that
            // was already raised by locally recorded PnL must stay monotone,
            // otherwise a real drawdown would be silently erased.
            let hasLocalActivity = realizedPnL != 0 || unrealizedPnL != 0
            equity = value
            unrealizedPnL = 0
            equityPeak = hasLocalActivity ? max(equityPeak, value) : value
            dayStartEquity = value
            dayStartBoundary = dayStart
            hasSynchronizedEquity = true
            // Strategy pools use their authenticated USDT base. Until the
            // first USDT read arrives, size them from account equity;
            // synchronizeStrategyCapital then establishes the real base.
            if strategyCapitalBase == nil { rebaseUnreservedPools(to: value, now: now) }
            refresh(now: now)
            return
        }
        equity = value
        unrealizedPnL = 0
        equityPeak = max(equityPeak, value)
        if needsDayStartEquitySync, dayStartBoundary == dayStart {
            dayStartEquity = value
            needsDayStartEquitySync = false
        }
        if dayStartBoundary.map({ dayStart > $0 }) == true {
            dayStartEquity = value
            dayStartBoundary = dayStart
            if killSwitch { resetEligibleAfterDayChange = true }
        }
        refresh(now: now)
    }

    /// Reconciles the strategy allocation ledger with the account's USDT
    /// asset.  Zero is a valid balance and must not fall back to all-asset
    /// account equity.  The first call allocates every pool from that base;
    /// later calls only resize empty pools so realized PnL and open
    /// reservations remain intact.
    public func synchronizeStrategyCapital(_ value: Decimal, now: Date = .now) {
        guard value.isFinite, value >= 0 else { return }
        let isFirstSync = strategyCapitalBase == nil
        strategyCapitalBase = value
        if isFirstSync {
            allocatePools(fromFirstBase: value, now: now)
        } else {
            rebaseUnreservedStrategyPools(to: value, now: now)
        }
    }

    /// Records a realized result. `settledUnrealizedPnL` is the portion of the
    /// account's currently marked unrealized PnL that was crystallized by this
    /// fill. Callers that first reconcile open positions (as `PaperBroker`
    /// does) should leave it at zero; direct callers can provide it to avoid
    /// counting the same mark-to-market result twice.
    public func record(realizedPnL: Decimal, now: Date = .now, strategyID: UUID? = nil, settledUnrealizedPnL: Decimal = 0, countRollover: Bool = true) {
        self.realizedPnL += realizedPnL
        equity += realizedPnL - settledUnrealizedPnL
        unrealizedPnL -= settledUnrealizedPnL
        if let strategyID, var pool = pools[strategyID] {
            pool.equity = max(0, pool.equity + realizedPnL)
            pool.realizedPnL += realizedPnL
            if countRollover { pool.rolloverCount += 1 }
            pool.unrealizedPnL -= settledUnrealizedPnL
            pool.updatedAt = now
            pools[strategyID] = pool
        }
        equityPeak = max(equityPeak, equity)
        refresh(now: now)
    }

    /// Credits a realized result reported by the remote exchange to the
    /// strategy pool. Account equity is deliberately left untouched here and
    /// will be reconciled from the next authenticated account snapshot; this
    /// prevents an estimated remote fill from being added on top of the same
    /// mark-to-market value twice.
    public func recordStrategyRealized(_ realizedPnL: Decimal, strategyID: UUID, now: Date = .now) {
        guard var pool = pools[strategyID] else { return }
        pool.equity = max(0, pool.equity + realizedPnL)
        pool.realizedPnL += realizedPnL
        pool.rolloverCount += 1
        pool.updatedAt = now
        pools[strategyID] = pool
    }

    /// Updates account equity with mark-to-market unrealized PnL. Unrealized
    /// gains remain informational for strategy pools and never increase their
    /// available opening balance.
    public func markToMarket(unrealizedPnL: Decimal, now: Date = .now) {
        let realizedEquity = equity - self.unrealizedPnL
        self.unrealizedPnL = unrealizedPnL
        equity = realizedEquity + unrealizedPnL
        refresh(now: now)
    }

    public func recordStrategyUnrealized(_ values: [UUID: Decimal], now: Date = .now) {
        for strategyID in pools.keys {
            guard var pool = pools[strategyID] else { continue }
            let value = values[strategyID] ?? 0
            pool.unrealizedPnL = value
            pool.updatedAt = now
            pools[strategyID] = pool
        }
        markToMarket(unrealizedPnL: values.values.reduce(0, +), now: now)
    }

    public func snapshot(now: Date = .now) -> RiskSnapshot {
        refresh(now: now)
        let daily = dayStartEquity == 0 ? 0 : (equity - dayStartEquity) / dayStartEquity * 100
        let drawdown = equityPeak == 0 ? 0 : (equityPeak - equity) / equityPeak * 100
        let capitalSnapshots = pools.map { $0.value.snapshot(strategyID: $0.key) }
            .sorted { $0.strategyID.uuidString < $1.strategyID.uuidString }
        return RiskSnapshot(equity: equity, equityPeak: equityPeak, dayStartEquity: dayStartEquity, dayStartAt: dayStartBoundary, dailyPnLPercent: daily, drawdownPercent: drawdown, killSwitch: killSwitch, reason: reason, strategyCapitals: capitalSnapshots, globalNotionals: notionals, strategyCapitalBase: strategyCapitalBase)
    }

    public func restore(_ snapshot: RiskSnapshot, now: Date = .now) {
        // Zero is a valid authenticated account state and must survive a
        // restart as a fail-closed circuit-breaker state. Reject only corrupt
        // negative/non-finite snapshots; silently ignoring zero would restore
        // the constructor's paper-equity fallback and permit new entries.
        guard snapshot.equity.isFinite, snapshot.equity >= 0 else { return }
        let currentDay = Self.utcCalendar.startOfDay(for: now)
        equity = snapshot.equity
        equityPeak = max(snapshot.equityPeak, snapshot.equity)
        dayStartEquity = snapshot.dayStartEquity > 0 ? snapshot.dayStartEquity : snapshot.equity
        let persistedDay = snapshot.dayStartAt.map(Self.utcCalendar.startOfDay(for:))
        let restoredFromPriorDay = persistedDay.map { currentDay > $0 } ?? false
        dayStartBoundary = persistedDay ?? currentDay
        if restoredFromPriorDay {
            // The old baseline no longer belongs to today. The current
            // equity is the best available midnight baseline until the next
            // authenticated account sync supplies the exact value.
            dayStartEquity = snapshot.equity
            dayStartBoundary = currentDay
        }
        unrealizedPnL = 0
        authenticatedZeroEquity = false
        killSwitch = snapshot.killSwitch
        reason = snapshot.reason
        resetEligibleAfterDayChange = restoredFromPriorDay && snapshot.killSwitch
        needsDayStartEquitySync = restoredFromPriorDay
        pools = Dictionary(uniqueKeysWithValues: snapshot.strategyCapitals.map { capital in
            (capital.strategyID, PoolState(
                allocationPercent: capital.allocationPercent,
                initialCapital: capital.initialCapital,
                equity: capital.equity,
                // Keep reservations and open risk from the persisted ledger.
                // The authenticated reconciliation will release completed
                // exposure, while clearing it here could oversubscribe a pool
                // immediately after a restart.
                reservedCapital: max(0, capital.reservedCapital),
                realizedPnL: capital.realizedPnL,
                unrealizedPnL: 0,
                rolloverCount: capital.rolloverCount,
                updatedAt: capital.updatedAt,
                openRisk: max(0, capital.openRisk),
                openPositions: max(0, capital.openPositions)
            ))
        })
        notionals = snapshot.globalNotionals.reduce(into: [:]) { result, item in
            let key = item.key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !key.isEmpty, item.value.isFinite, item.value > 0 else { return }
            result[key] = item.value
        }
        hasSynchronizedEquity = true
        authenticatedZeroEquity = snapshot.equity == 0
        strategyCapitalBase = snapshot.strategyCapitalBase.flatMap { value in
            value.isFinite && value >= 0 ? value : nil
        }
        // A hand-written zero-equity snapshot may not carry the
        // kill-switch bit. Recompute the invariant before returning so callers
        // cannot observe an open-entry state between restore and the next
        // account refresh.
        refresh(now: now)
    }

    /// Replaces the global exposure baseline with an authenticated exchange
    /// view.  This closes the restart gap where an in-memory reservation would
    /// otherwise disappear (or remain forever) after a service relaunch.
    public func reconcileGlobalNotionals(_ exposures: [String: Decimal]) {
        notionals = exposures.reduce(into: [:]) { result, item in
            let key = item.key.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !key.isEmpty, item.value.isFinite, item.value > 0 else { return }
            result[key] = item.value
        }
    }

    public func resetKillSwitch(now: Date = .now) -> RiskSnapshot {
        let dayStart = Self.utcCalendar.startOfDay(for: now)
        guard resetEligibleAfterDayChange || dayStartBoundary.map({ dayStart > $0 }) == true else { return snapshot(now: now) }
        dayStartEquity = equity
        dayStartBoundary = dayStart
        killSwitch = false
        reason = nil
        resetEligibleAfterDayChange = false
        needsDayStartEquitySync = false
        return snapshot(now: now)
    }

    private func refresh(now: Date) {
        let dayStart = Self.utcCalendar.startOfDay(for: now)
        if let boundary = dayStartBoundary {
            if dayStart > boundary {
                let wasKillSwitch = killSwitch
                dayStartEquity = equity
                dayStartBoundary = dayStart
                if wasKillSwitch { resetEligibleAfterDayChange = true }
            }
        } else {
            dayStartBoundary = dayStart
        }
        let daily = dayStartEquity == 0 ? 0 : (equity - dayStartEquity) / dayStartEquity * 100
        let drawdown = equityPeak == 0 ? 0 : (equityPeak - equity) / equityPeak * 100
        if authenticatedZeroEquity || equity <= 0 {
            killSwitch = true
            reason = "账户权益为零"
        } else if daily <= -limits.maxDailyLossPercent {
            killSwitch = true
            reason = "单日亏损熔断"
        } else if drawdown >= limits.maxDrawdownPercent, limits.maxDrawdownPercent > 0 {
            killSwitch = true
            reason = "累计回撤熔断"
        }
    }

    private func rebaseUnreservedPools(to accountEquity: Decimal, now: Date) {
        for strategyID in pools.keys {
            guard var pool = pools[strategyID], pool.reservedCapital == 0, pool.realizedPnL == 0 else { continue }
            let capital = accountEquity * pool.allocationPercent / 100
            pool.initialCapital = capital
            pool.equity = capital
            pool.updatedAt = now
            pools[strategyID] = pool
        }
    }

    private func strategyCapitalAllocation() -> StrategyCapitalAllocation {
        StrategyCapitalAllocation(
            totalCapital: strategyCapitalBase ?? equity,
            strategyCapitals: pools.map { $0.value.snapshot(strategyID: $0.key) }
        )
    }

    private func allocatePools(fromFirstBase totalCapital: Decimal, now: Date) {
        var usedAllocation: Decimal = 0
        for strategyID in pools.keys.sorted(by: { $0.uuidString < $1.uuidString }) {
            guard var pool = pools[strategyID] else { continue }
            let requested = pool.allocationPercent.isFinite ? max(0, pool.allocationPercent) : 0
            let effective = min(requested, max(0, 100 - usedAllocation))
            let initial = totalCapital * effective / 100
            pool.allocationPercent = effective
            pool.initialCapital = initial
            // A positive historical result must not make the pool exceed its
            // newly authenticated USDT allocation after a balance decrease.
            pool.equity = min(max(0, initial + pool.realizedPnL), initial)
            pool.updatedAt = now
            pools[strategyID] = pool
            usedAllocation += effective
        }
    }

    private func rebaseUnreservedStrategyPools(to totalCapital: Decimal, now: Date) {
        for strategyID in pools.keys {
            guard var pool = pools[strategyID] else { continue }
            // Existing pools retain their configured share when the USDT
            // balance changes. Clamping against aggregate occupancy here would
            // incorrectly shrink every idle pool when their percentages sum to
            // 100%; a lower account balance is still a hard upper bound for
            // every pool, including pools with open reservations.
            let percent = pool.allocationPercent.isFinite ? min(max(0, pool.allocationPercent), 100) : 0
            let capital = totalCapital * percent / 100
            pool.allocationPercent = percent
            // The authenticated USDT balance is authoritative. A transient
            // remote realized credit can be visible between two account reads,
            // but must not be applied a second time after that balance already
            // includes the fill.
            if pool.reservedCapital == 0 {
                pool.initialCapital = capital
                pool.equity = capital
            } else if capital < pool.equity {
                pool.initialCapital = min(pool.initialCapital, capital)
                pool.equity = capital
            }
            pool.updatedAt = now
            pools[strategyID] = pool
        }
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
    private var positionStrategyIDs: [String: UUID] = [:]
    // reduceOnly only matters while an order is pending, so it stays out of
    // the persisted PaperOrder ledger record.
    private var reduceOnlyOrderIDs: Set<UUID> = []
    // Risk reservations are based on the submitted reference price. Retain
    // that amount per pending order so a normal closing/reversing order can
    // release only the portion that actually closed the existing position.
    private var reservedNotionals: [UUID: Decimal] = [:]
    // Keep the matching stop-risk reservation.  PaperPosition intentionally
    // remains a small public ledger type, so this execution-only map tracks
    // the risk budget without changing its persisted schema.
    private var reservedRiskAmounts: [UUID: Decimal] = [:]
    private var positionRiskAmounts: [String: Decimal] = [:]

    public init(risk: RiskEngine = RiskEngine(), feeRate: Decimal = 0.0005, slippageBps: Decimal = 2) {
        self.risk = risk
        self.feeRate = feeRate
        self.slippageBps = slippageBps
    }

    public func submit(strategyID: UUID, instrumentID: String, side: String, quantity: Decimal, referencePrice: Decimal, requestedAt: Date = .now, reduceOnly: Bool = false, poolAllocationPercent: Decimal = 100, riskAmount: Decimal = 0, maxOpenRiskPercent: Decimal? = nil, maxConcurrentPositions: Int? = nil) async -> Result<PaperOrder, Error> {
        let normalizedSide = side.lowercased()
        guard ["long", "short"].contains(normalizedSide) else {
            return .failure(BrokerError.invalidOrder("方向必须是 long 或 short"))
        }
        guard quantity > 0, referencePrice > 0 else {
            return .failure(BrokerError.invalidOrder("数量和参考价格必须大于 0"))
        }
        let notional = abs(quantity * referencePrice)
        // A caller that supplies strategy-level controls must get a pool even
        // if this is its first paper order. Standalone paper orders use the
        // account-only paper budget.
        let hasPool = await risk.hasStrategyPool(strategyID)
        let usesStrategyControls = riskAmount != 0 || maxOpenRiskPercent != nil || maxConcurrentPositions != nil || poolAllocationPercent != 100
        let poolStrategyID = hasPool || usesStrategyControls ? strategyID : nil
        let decision = await risk.authorize(
            instrumentID: instrumentID, notional: notional, margin: notional,
            reduceOnly: reduceOnly, now: requestedAt, strategyID: poolStrategyID,
            poolAllocationPercent: poolAllocationPercent, riskAmount: riskAmount,
            maxOpenRiskPercent: maxOpenRiskPercent,
            maxConcurrentPositions: maxConcurrentPositions
        )
        guard decision.allowed else { return .failure(BrokerError.riskRejected(decision.reason ?? "风险拒绝")) }
        let order = PaperOrder(strategyID: strategyID, instrumentID: instrumentID, side: normalizedSide, quantity: quantity, requestedAt: requestedAt)
        pending.append(order)
        orders.append(order)
        if reduceOnly { reduceOnlyOrderIDs.insert(order.id) }
        else {
            reservedNotionals[order.id] = notional
            reservedRiskAmounts[order.id] = riskAmount
        }
        return .success(order)
    }

    public func processNextOpen(instrumentID: String, candle: Candle) async {
        let due = pending.filter { $0.instrumentID == instrumentID && $0.requestedAt < candle.timestamp }
        pending.removeAll { order in due.contains(where: { $0.id == order.id }) }
        for order in due { await fill(order, at: candle.open, timestamp: candle.timestamp) }
    }

    public func mark(instrumentID: String, price: Decimal, at timestamp: Date = .now) async {
        guard let position = positions[instrumentID] else { return }
        let sign: Decimal = position.side == "short" ? -1 : 1
        let pnl = (price - position.entryPrice) * position.quantity * sign
        positions[instrumentID] = PaperPosition(id: position.id, instrumentID: position.instrumentID, side: position.side, quantity: position.quantity, entryPrice: position.entryPrice, markPrice: price, unrealizedPnL: pnl, updatedAt: timestamp)
        var byStrategy: [UUID: Decimal] = [:]
        for (id, current) in positions {
            guard let strategyID = positionStrategyIDs[id] else { continue }
            byStrategy[strategyID, default: 0] += current.unrealizedPnL
        }
        await risk.recordStrategyUnrealized(byStrategy, now: timestamp)
    }

    public func allOrders() -> [PaperOrder] { orders }
    public func allFills() -> [PaperFill] { fills }
    public func allPositions() -> [PaperPosition] { Array(positions.values) }
    public func riskSnapshot() async -> RiskSnapshot { await risk.snapshot() }

    /// Keeps the risk engine's mark-to-market view aligned with the positions
    /// after a fill changes quantity or entry price. This must run before a
    /// realized result is recorded; otherwise the same PnL can be counted once
    /// as unrealized and again as realized.
    private func synchronizeRiskUnrealized(at timestamp: Date) async {
        var byStrategy: [UUID: Decimal] = [:]
        for (instrumentID, position) in positions {
            guard let strategyID = positionStrategyIDs[instrumentID] else { continue }
            byStrategy[strategyID, default: 0] += position.unrealizedPnL
        }
        await risk.recordStrategyUnrealized(byStrategy, now: timestamp)
    }

    private func fill(_ order: PaperOrder, at open: Decimal, timestamp: Date) async {
        let reduceOnly = reduceOnlyOrderIDs.remove(order.id) != nil
        let reservedOrderNotional = reservedNotionals.removeValue(forKey: order.id)
        let reservedOrderRisk = reservedRiskAmounts.removeValue(forKey: order.id) ?? 0
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
            positionStrategyIDs[order.instrumentID] = order.strategyID
            positionRiskAmounts[order.instrumentID] = reservedOrderRisk
            // Entry fees reduce account/pool equity immediately. They are a
            // realized cost, but opening a position is not a rollover event.
            if fee > 0 {
                await risk.record(realizedPnL: -fee, now: timestamp, strategyID: order.strategyID, countRollover: false)
            }
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
            let strategyID = positionStrategyIDs[order.instrumentID] ?? order.strategyID
            let realized = (price - existing.entryPrice) * closeQuantity * direction - fee
            let remaining = existing.quantity - closeQuantity
            let existingRisk = positionRiskAmounts[order.instrumentID] ?? 0
            let closedPositionRisk = existing.quantity > 0 ? existingRisk * closeQuantity / existing.quantity : 0
            // A normal order is authorized as a new position before we know
            // whether it will close, partially close, or reverse an existing
            // position. Keep that reservation only for a reversal residual;
            // otherwise release the synthetic position slot while retaining
            // the old position slot when it still has quantity left.
            let leavesReversal = !reduceOnly && order.quantity > closeQuantity
            // A normal order reserves risk for its full requested quantity.
            // Release the fraction used to close the old position; any
            // residual reversal remains reserved for the new position.
            if !reduceOnly, let reservedOrderNotional {
                let closedReservation = reservedOrderNotional * closeQuantity / order.quantity
                let closedOrderRisk = order.quantity > 0 ? reservedOrderRisk * closeQuantity / order.quantity : 0
                await risk.release(
                    instrumentID: order.instrumentID,
                    notional: closedReservation,
                    strategyID: order.strategyID,
                    margin: closedReservation,
                    riskAmount: closedOrderRisk,
                    closedPosition: !leavesReversal
                )
            }
            if remaining > 0 {
                let remainingPnL = (price - existing.entryPrice) * remaining * direction
                positions[order.instrumentID] = PaperPosition(id: existing.id, instrumentID: order.instrumentID, side: existing.side, quantity: remaining, entryPrice: existing.entryPrice, markPrice: price, unrealizedPnL: remainingPnL, updatedAt: timestamp)
                positionRiskAmounts[order.instrumentID] = max(0, existingRisk - closedPositionRisk)
            } else if !reduceOnly, order.quantity > closeQuantity {
                // A normal order that exceeds the current position reverses
                // the residual quantity instead of silently losing it.
                positions[order.instrumentID] = PaperPosition(id: existing.id, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity - closeQuantity, entryPrice: price, markPrice: price, updatedAt: timestamp)
                positionStrategyIDs[order.instrumentID] = order.strategyID
                let residualOrderRisk = max(0, reservedOrderRisk - (order.quantity > 0 ? reservedOrderRisk * closeQuantity / order.quantity : 0))
                positionRiskAmounts[order.instrumentID] = residualOrderRisk
            } else {
                positions.removeValue(forKey: order.instrumentID)
                positionStrategyIDs.removeValue(forKey: order.instrumentID)
                positionRiskAmounts.removeValue(forKey: order.instrumentID)
            }
            // Reconcile the remaining positions before crystallizing the
            // closing result so the old mark is removed exactly once.
            await synchronizeRiskUnrealized(at: timestamp)
            await risk.record(realizedPnL: realized, now: timestamp, strategyID: strategyID)
            await risk.release(
                instrumentID: order.instrumentID,
                notional: closeQuantity * existing.entryPrice,
                strategyID: strategyID,
                margin: closeQuantity * existing.entryPrice,
                riskAmount: closedPositionRisk,
                closedPosition: remaining <= 0
            )
        } else {
            // Adding to an existing position merges the average entry price.
            let fee = abs(price * order.quantity) * feeRate
            let totalQuantity = existing.quantity + order.quantity
            guard totalQuantity != 0 else { positions.removeValue(forKey: order.instrumentID); positionStrategyIDs.removeValue(forKey: order.instrumentID); return }
            let blendedEntry = (existing.entryPrice * existing.quantity + price * order.quantity) / totalQuantity
            positions[order.instrumentID] = PaperPosition(id: existing.id, instrumentID: order.instrumentID, side: existing.side, quantity: totalQuantity, entryPrice: blendedEntry, markPrice: price, unrealizedPnL: (price - blendedEntry) * totalQuantity * sign, updatedAt: timestamp)
            positionStrategyIDs[order.instrumentID] = positionStrategyIDs[order.instrumentID] ?? order.strategyID
            positionRiskAmounts[order.instrumentID] = max(0, positionRiskAmounts[order.instrumentID, default: 0] + reservedOrderRisk)
            await synchronizeRiskUnrealized(at: timestamp)
            // Adding to a position does not create a second concurrent
            // position. The order authorization reserved margin/notional for
            // the added quantity, so only release its synthetic slot.
            await risk.release(instrumentID: order.instrumentID, notional: 0, strategyID: order.strategyID, margin: 0, closedPosition: true)
            // Every fill pays a fee, including an addition to an existing
            // position. Charge it immediately so account equity and the
            // strategy pool cannot overstate available capital.
            if fee > 0 {
                await risk.record(realizedPnL: -fee, now: timestamp, strategyID: order.strategyID, countRollover: false)
            }
        }
    }

    public func cancelPendingOrders() async {
        await cancelPendingOrders(where: { _ in true })
    }

    /// Cancels only one strategy's pending entries. Package removal uses this
    /// scoped form so another installed strategy's order is never touched.
    public func cancelPendingOrders(strategyID: UUID) async {
        await cancelPendingOrders(where: { $0.strategyID == strategyID })
    }

    private func cancelPendingOrders(where predicate: (PaperOrder) -> Bool) async {
        let cancelled = pending.filter(predicate)
        for order in cancelled {
            if let reserved = reservedNotionals.removeValue(forKey: order.id) {
                await risk.release(instrumentID: order.instrumentID, notional: reserved, strategyID: order.strategyID, margin: reserved, riskAmount: reservedRiskAmounts.removeValue(forKey: order.id) ?? 0, closedPosition: true)
            }
            reservedRiskAmounts.removeValue(forKey: order.id)
            reduceOnlyOrderIDs.remove(order.id)
            if let index = orders.firstIndex(where: { $0.id == order.id }) {
                orders[index] = PaperOrder(id: order.id, strategyID: order.strategyID, instrumentID: order.instrumentID, side: order.side, quantity: order.quantity, requestedAt: order.requestedAt, status: "cancelled", remoteOrderID: order.remoteOrderID)
            }
        }
        pending.removeAll(where: predicate)
    }

    @discardableResult
    public func flattenAll(at timestamp: Date = .now) async -> [PaperPosition] {
        let closed = Array(positions.values)
        for position in closed {
            let price = position.markPrice ?? position.entryPrice
            let direction: Decimal = position.side == "short" ? -1 : 1
            let strategyID = positionStrategyIDs[position.instrumentID]
            let realized = (price - position.entryPrice) * position.quantity * direction - abs(price * position.quantity) * feeRate
            positions.removeValue(forKey: position.instrumentID)
            positionStrategyIDs.removeValue(forKey: position.instrumentID)
            let positionRisk = positionRiskAmounts.removeValue(forKey: position.instrumentID) ?? 0
            await synchronizeRiskUnrealized(at: timestamp)
            await risk.record(realizedPnL: realized, now: timestamp, strategyID: strategyID)
            await risk.release(instrumentID: position.instrumentID, notional: position.entryPrice * position.quantity, strategyID: strategyID, margin: position.entryPrice * position.quantity, riskAmount: positionRisk, closedPosition: true)
        }
        await risk.recordStrategyUnrealized([:], now: timestamp)
        return closed
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
