import Foundation
import Hummingbird
import HummingbirdFoundation
import HummingbirdWebSocket
import ATKGateway
import OKXGateway
import TradingDomain

/// Local path is resolved below the backend's strategy-staging directory;
/// callers never hand the registry an arbitrary filesystem destination.
public struct StrategyPackageInstallRequest: Codable, Equatable, Sendable {
    public let path: String
    public let replacing: Bool

    public init(path: String, replacing: Bool = false) {
        self.path = path
        self.replacing = replacing
    }
}

public struct StrategyPackageInstanceRequest: Codable, Equatable, Sendable {
    public let name: String?
    public let scope: StrategyScope?

    public init(name: String? = nil, scope: StrategyScope? = nil) {
        self.name = name
        self.scope = scope
    }
}

public actor PaperTradingStore {
    public private(set) var strategies: [StrategyConfig]
    public private(set) var statuses: [UUID: StrategyStatus]
    public private(set) var orders: [PaperOrder]
    public private(set) var fills: [PaperFill]
    public private(set) var risk = RiskSnapshot()

    private let directory: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private var saveTask: Task<Void, Never>?
    /// Runtime-only state split by instrument so one multi-instrument strategy
    /// does not let a bar or cooldown from one market suppress another.
    private var statusesByInstrument: [UUID: [String: StrategyStatus]] = [:]
    /// BTC 1 小时 K 线缓存：供"扫顶反转"策略的 BTC<SMA200 门控使用
    private var btcHourlyCandles: [Candle] = []
    /// Cross-timeframe execution caches. A 1h close only arms the structure;
    /// a confirmed 15m close is the only event allowed to create an entry.
    private var structureCandlesByInstrument: [String: [Candle]] = [:]
    private var confirmationCandlesByInstrument: [String: [Candle]] = [:]
    /// Resolved once per contract-universe refresh and reused by every candle
    /// evaluation. Re-filtering and sorting the full OKX contract list for
    /// every incoming bar was the previous hot path for dynamic strategies.
    private var resolvedTargetsByStrategy: [UUID: Set<String>] = [:]
    private var resolvedUniverseSnapshots: [UUID: StrategyUniverseSnapshot] = [:]
    private var resolvedContractsByID: [String: ContractMarket] = [:]
    private var universeResolved = false

    public nonisolated var stateDirectory: URL { directory }

    private static func canonicalized(_ config: StrategyConfig) -> StrategyConfig {
        var normalized = config
        // 参数默认值只有一份（StrategyType.defaultParameters），App 与后端共用；
        // 调用方显式传入的键优先。
        for (key, value) in config.type.defaultParameters where normalized.parameters[key] == nil {
            normalized.parameters[key] = value
        }
        // Leverage is a user-editable strategy parameter. A missing, NaN or
        // out-of-range value converges to the strategy's built-in default
        // before being written back to disk.
        if config.type.hasRuntimeHandler {
            let fallback = config.type.defaultParameters["leverage"] ?? 2.0
            let value = normalized.parameters["leverage"] ?? fallback
            normalized.parameters["leverage"] = value.isFinite &&
                config.type.leverageRange.contains(value)
                ? value : fallback
        }
        switch config.type {
        case .sweepReversalShort:
            normalized.name = config.type.displayName
            normalized.interval = .oneHour
            // Strategy instances scan their own live universe; stale generic
            // scopes converge to the strategy's canonical scope.
            normalized.scope = config.type.defaultScope
            normalized.riskPercent = min(max(normalized.riskPercent, 0.1), config.type.maxRiskPercent)
        case .doublePumpExhaustionShort:
            normalized.name = config.type.displayName
            normalized.interval = config.type.entryInterval
            normalized.scope = config.type.defaultScope
            normalized.riskPercent = min(max(normalized.riskPercent, 0.1), config.type.maxRiskPercent)
            normalized.cooldownBars = config.type.defaultCooldownBars
        case .external:
            // Keep an orphaned package instance visible so its ledger and
            // protective history survive an uninstall. It can never be
            // started until the package's runtime handler is installed.
            normalized.enabled = false
        }
        // A strategy may scan many symbols, but its instance-level execution
        // policy allows only one active symbol at a time. The open-risk cap is
        // a percentage of the strategy's own pool equity, like the per-trade
        // budget, so a strategy cannot be sized off the whole account.
        normalized.parameters["maxConcurrentPositions"] = 1
        normalized.parameters["maxOpenRiskPercent"] = config.type.maxOpenRiskPercent
        normalized.capitalPoolPercent = min(max(normalized.capitalPoolPercent, 0.1), 100.0)
        return normalized
    }

    /// Built-in strategies scan a dynamic universe on their own entry bar.
    /// The exact category is canonicalized from the strategy type, so any
    /// dynamic category is accepted here; fixed single/multiple scopes are not.
    private static func supports(_ config: StrategyConfig) -> Bool {
        if case .external = config.type { return true }
        return config.interval == config.type.entryInterval && config.scope.mode == .dynamicCategory
    }

    public init(directory: URL = PaperTradingStore.defaultDirectory()) {
        self.directory = directory
        self.encoder = JSONEncoder()
        self.decoder = JSONDecoder()
        self.strategies = []
        self.statuses = [:]
        self.orders = []
        self.fills = []
        if let file = Self.loadFile(directory: directory, decoder: decoder) {
            let loaded = file.strategies
                .filter(Self.supports(_:))
                .map(Self.canonicalized(_:))
            var unique: [StrategyConfig] = []
            for config in loaded where !unique.contains(where: { $0.type == config.type }) {
                unique.append(config)
            }
            self.strategies = unique
            self.statuses = Dictionary(uniqueKeysWithValues: file.statuses.compactMap { entry in
                guard let uuid = UUID(uuidString: entry.key) else { return nil }
                return (uuid, entry.value)
            })
            self.orders = file.orders
            self.fills = file.fills
            self.risk = file.risk
            let strategyIDs = Set(self.strategies.map(\.id))
            self.orders.removeAll { $0.status == "pending" && !strategyIDs.contains($0.strategyID) }
        }
        // A service restart is an explicit safety boundary: strategies must be
        // started manually after the backend becomes available. A new
        // installation starts with no strategy instances.
        self.strategies = self.strategies.map { config in
            var paused = config
            paused.enabled = false
            return paused
        }
        self.statuses = Dictionary(uniqueKeysWithValues: self.strategies.map { ($0.id, StrategyStatus(id: $0.id, state: .paused)) })
        Self.persist(StoreFile(strategies: self.strategies, statuses: self.statuses, orders: self.orders, fills: self.fills, risk: self.risk), directory: directory, encoder: encoder)
    }

    public static func defaultDirectory() -> URL {
        if let directory = ProcessInfo.processInfo.environment["OKX_LOCALD_STATE_DIR"], !directory.isEmpty {
            return URL(fileURLWithPath: directory, isDirectory: true)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("NovaTrade", isDirectory: true)
    }

    public func allStrategies() -> [StrategyConfig] { strategies }
    public func allOrders() -> [PaperOrder] { orders }
    public func allFills() -> [PaperFill] { fills }
    /// Dashboard view of every strategy. `statuses[id]` only mirrors the most
    /// recently evaluated instrument, so on a multi-symbol scan it flips back
    /// to "no signal, no cooldown" as soon as any other symbol ticks. The
    /// merged status keeps the newest signal across symbols and the longest
    /// remaining cooldown among the symbols still being scanned; per-symbol
    /// records are reset on every pause, start, update and delete, so nothing
    /// stale survives a restart of the strategy.
    public func allStatuses() -> [StrategyStatus] { strategies.compactMap { dashboardStatus(for: $0) } }

    private func dashboardStatus(for config: StrategyConfig) -> StrategyStatus? {
        guard let base = statuses[config.id] else { return nil }
        let perInstrument = statusesByInstrument[config.id] ?? [:]
        guard !perInstrument.isEmpty else { return base }
        let scanned = resolvedTargetsByStrategy[config.id] ?? []
        let newestSignal = (perInstrument.values.compactMap(\.lastSignal) + [base.lastSignal].compactMap { $0 })
            .max { $0.timestamp < $1.timestamp }
        let cooldown = perInstrument
            .filter { scanned.isEmpty || scanned.contains($0.key) }
            .map(\.value.cooldown)
            .reduce(base.cooldown, max)
        return StrategyStatus(id: base.id, state: base.state, direction: base.direction,
                              cooldown: cooldown, pnl: base.pnl, lastSignal: newestSignal,
                              indicators: base.indicators, lastEvaluatedBar: base.lastEvaluatedBar)
    }

    /// Rebuilds every strategy's concrete scan pool from one contract snapshot.
    /// StreamHub, REST diagnostics, and candle evaluation all consume this same
    /// cache, so displayed targets and subscribed targets cannot drift apart.
    public func refreshStrategyUniverse(_ contracts: [ContractMarket], now: Date = .now) {
        var targets: [UUID: Set<String>] = [:]
        var snapshots: [UUID: StrategyUniverseSnapshot] = [:]
        for config in strategies {
            let ids = config.scope.resolvedInstrumentIDs(from: contracts)
            targets[config.id] = Set(ids)
            snapshots[config.id] = StrategyUniverseSnapshot(
                strategyID: config.id,
                strategyName: config.name,
                strategyType: config.type,
                enabled: config.enabled,
                universeCategory: config.scope.category,
                instrumentIDs: ids,
                refreshedAt: now
            )
        }
        resolvedTargetsByStrategy = targets
        resolvedUniverseSnapshots = snapshots
        resolvedContractsByID = Dictionary(uniqueKeysWithValues: contracts.map { ($0.id, $0) })
        universeResolved = true
    }

    public func strategyUniverseTargets() -> [StrategyUniverseSnapshot] {
        strategies.compactMap { resolvedUniverseSnapshots[$0.id] }
            .sorted { $0.strategyName.localizedStandardCompare($1.strategyName) == .orderedAscending }
    }

    private func invalidateStrategyUniverse() {
        resolvedTargetsByStrategy.removeAll(keepingCapacity: true)
        resolvedUniverseSnapshots.removeAll(keepingCapacity: true)
        resolvedContractsByID.removeAll(keepingCapacity: true)
        universeResolved = false
    }
    public func status(for id: UUID) -> StrategyStatus? { statuses[id] }
    /// Returns the status for one instrument when a strategy runs over a
    /// dynamic universe. The aggregate status is retained for the dashboard,
    /// but exits must use the signal that belongs to the held instrument.
    public func status(for id: UUID, instrumentID: String) -> StrategyStatus? {
        if let perInstrument = statusesByInstrument[id]?[instrumentID] { return perInstrument }
        // 没有该标的自己的记录时返回中性状态，绝不回退到汇总状态：汇总状态可能
        // 属于另一个标的，会让止损/止盈取到别人的价位。
        guard let aggregate = statuses[id] else { return nil }
        return StrategyStatus(id: aggregate.id, state: aggregate.state)
    }

    /// Applies a completed-exit cooldown to one instrument's isolated state.
    /// The aggregate dashboard status follows the most recently updated
    /// instrument, while other symbols retain their own independent cooldown.
    public func setCooldown(strategyID: UUID, instrumentID: String, bars: Int) {
        let current = statusesByInstrument[strategyID]?[instrumentID]
            ?? statuses[strategyID]
            ?? StrategyStatus(id: strategyID, state: .running)
        let updated = StrategyStatus(id: current.id, state: current.state,
                                     direction: current.direction,
                                     cooldown: max(0, bars), pnl: current.pnl,
                                     lastSignal: current.lastSignal,
                                     indicators: current.indicators,
                                     lastEvaluatedBar: current.lastEvaluatedBar)
        statusesByInstrument[strategyID, default: [:]][instrumentID] = updated
        statuses[strategyID] = updated
        save()
    }

    public func prewarm(_ snapshot: MarketSnapshot) {
        if snapshot.instrumentID == "BTC-USDT-SWAP", snapshot.interval == .oneHour {
            btcHourlyCandles = snapshot.candles
        }
        switch snapshot.interval {
        case .oneHour:
            structureCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        case .fifteenMinutes:
            confirmationCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        default:
            break
        }
    }
    public func riskSnapshot() -> RiskSnapshot { risk }

    public func create(_ config: StrategyConfig) throws -> StrategyConfig {
        guard config.type.hasRuntimeHandler,
              Self.supports(config),
              config.riskPercent > 0,
              config.riskPercent <= config.type.maxRiskPercent,
              config.capitalPoolPercent > 0,
              config.capitalPoolPercent <= 100,
              Self.validLeverage(config) else { throw StoreError.unsupported }
        let normalized = Self.canonicalized(config)
        guard !strategies.contains(where: { $0.id == normalized.id }) else { throw StoreError.conflict }
        guard !strategies.contains(where: { $0.type == normalized.type }) else { throw StoreError.conflict }
        strategies.append(normalized)
        statuses[normalized.id] = StrategyStatus(id: normalized.id, state: normalized.enabled ? .running : .paused)
        invalidateStrategyUniverse()
        save()
        return normalized
    }

    public func update(_ config: StrategyConfig) throws -> StrategyConfig {
        guard config.type.hasRuntimeHandler,
              Self.supports(config),
              config.riskPercent > 0,
              config.riskPercent <= config.type.maxRiskPercent,
              config.capitalPoolPercent > 0,
              config.capitalPoolPercent <= 100,
              Self.validLeverage(config) else { throw StoreError.unsupported }
        let normalized = Self.canonicalized(config)
        guard let index = strategies.firstIndex(where: { $0.id == normalized.id }) else { throw StoreError.notFound }
        // A strategy's runtime type determines its signal, sizing, and exit
        // handler. Changing it in place would reinterpret existing orders and
        // positions under a different lifecycle, so require a new instance.
        guard strategies[index].type == normalized.type else { throw StoreError.unsupported }
        guard !strategies.contains(where: { $0.id != normalized.id && $0.type == normalized.type }) else { throw StoreError.conflict }
        let wasEnabled = strategies[index].enabled
        strategies[index] = normalized
        statusesByInstrument[normalized.id] = [:]
        invalidateStrategyUniverse()
        if let previous = statuses[normalized.id] {
            let lastSignal = !wasEnabled && normalized.enabled ? nil : previous.lastSignal
            statuses[normalized.id] = StrategyStatus(id: previous.id, state: normalized.enabled ? .running : .paused, direction: previous.direction, cooldown: previous.cooldown, pnl: previous.pnl, lastSignal: lastSignal, indicators: previous.indicators)
        } else {
            statuses[normalized.id] = StrategyStatus(id: normalized.id, state: normalized.enabled ? .running : .paused)
        }
        save()
        return normalized
    }

    /// Validates the raw persisted/user supplied value before canonicalization
    /// can replace it with a fallback, so create/update stay strict. The range is deliberately broader than the editor's usual defaults,
    /// because OKX's per-instrument maximum can vary while values outside
    /// this account-wide range are always unsafe.
    private static func validLeverage(_ config: StrategyConfig) -> Bool {
        let fallback = config.type.defaultParameters["leverage"] ?? 2.0
        let value = config.parameters["leverage"] ?? fallback
        return value.isFinite && config.type.leverageRange.contains(value)
    }

    public func delete(_ id: UUID) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { throw StoreError.notFound }
        guard !strategies[index].enabled else { throw StoreError.running }
        let removed = strategies.remove(at: index)
        statuses.removeValue(forKey: id)
        statusesByInstrument.removeValue(forKey: id)
        invalidateStrategyUniverse()
        // Deleting an instance must not leave a live pending entry that can
        // fill after its strategy/package has been removed. Keep the order in
        // the audit ledger, but make it ineligible for PaperBroker execution.
        orders = orders.map { order in
            guard order.strategyID == id, order.status == "pending" else { return order }
            return PaperOrder(id: order.id, strategyID: order.strategyID,
                              instrumentID: order.instrumentID, side: order.side,
                              quantity: order.quantity, requestedAt: order.requestedAt,
                              fillPrice: order.fillPrice, status: "cancelled",
                              remoteOrderID: order.remoteOrderID,
                              clientOrderID: order.clientOrderID, signal: order.signal)
        }
        save()
        return removed
    }

    public func setState(_ id: UUID, running: Bool) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { throw StoreError.notFound }
        guard strategies[index].type.hasRuntimeHandler else { throw StoreError.unsupported }
        if running, strategies[index].scope != strategies[index].type.defaultScope {
            throw StoreError.unsupported
        }
        strategies[index].enabled = running
        statusesByInstrument[id] = [:]
        statuses[id] = StrategyStatus(id: id, state: running ? .running : .paused)
        save()
        return strategies[index]
    }

    @discardableResult
    public func stopAllStrategies() -> [StrategyConfig] {
        var stopped: [StrategyConfig] = []
        for index in strategies.indices where strategies[index].enabled {
            strategies[index].enabled = false
            let config = strategies[index]
            stopped.append(config)
            statuses[config.id] = StrategyStatus(id: config.id, state: .paused)
            statusesByInstrument[config.id] = [:]
        }
        if !stopped.isEmpty { save() }
        return stopped
    }

    public func record(_ order: PaperOrder, fill: PaperFill? = nil) {
        orders.append(order)
        if let fill { fills.append(fill) }
        save()
    }

    /// Records a remote order's completed lifecycle in the durable ledger.
    /// A released reservation must not be recreated from its old submitted
    /// entry on the next authenticated reconciliation or after a restart.
    public func markRemoteOrderTerminal(_ remoteOrderID: String, status: String = "closed") {
        guard OrderLifecycle.isTerminal(status) else { return }
        var changed = false
        orders = orders.map { order in
            guard order.remoteOrderID == remoteOrderID,
                  order.status.lowercased() != status.lowercased() else { return order }
            changed = true
            return PaperOrder(id: order.id, strategyID: order.strategyID,
                              instrumentID: order.instrumentID, side: order.side,
                              quantity: order.quantity, requestedAt: order.requestedAt,
                              fillPrice: order.fillPrice, status: status.lowercased(),
                              remoteOrderID: order.remoteOrderID,
                              clientOrderID: order.clientOrderID, signal: order.signal)
        }
        if changed { save() }
    }

    /// Gives an entry recorded during an unknown submit outcome the exchange
    /// order id its clOrdId lookup later returned.
    public func attachRemoteOrderID(_ remoteOrderID: String, toLocalOrder localOrderID: UUID) {
        guard !orders.contains(where: { $0.remoteOrderID == remoteOrderID }),
              let index = orders.firstIndex(where: { $0.id == localOrderID && $0.remoteOrderID == nil }) else { return }
        let order = orders[index]
        orders[index] = PaperOrder(id: order.id, strategyID: order.strategyID,
                                   instrumentID: order.instrumentID, side: order.side,
                                   quantity: order.quantity, requestedAt: order.requestedAt,
                                   fillPrice: order.fillPrice, status: order.status,
                                   remoteOrderID: remoteOrderID,
                                   clientOrderID: order.clientOrderID, signal: order.signal)
        save()
    }

    /// Closes an entry that never received an exchange order id, so it no
    /// longer claims ownership of later positions on its instrument.
    public func markLocalOrderTerminal(_ localOrderID: UUID, status: String) {
        guard OrderLifecycle.isTerminal(status),
              let index = orders.firstIndex(where: { $0.id == localOrderID }),
              orders[index].status.lowercased() != status.lowercased() else { return }
        let order = orders[index]
        orders[index] = PaperOrder(id: order.id, strategyID: order.strategyID,
                                   instrumentID: order.instrumentID, side: order.side,
                                   quantity: order.quantity, requestedAt: order.requestedAt,
                                   fillPrice: order.fillPrice, status: status.lowercased(),
                                   remoteOrderID: order.remoteOrderID,
                                   clientOrderID: order.clientOrderID, signal: order.signal)
        save()
    }

    /// Coalesces the recompute path: a burst of bar closes (or several REST
    /// chart loads) triggers at most one disk write after the burst settles.
    private func scheduleSave() {
        saveTask?.cancel()
        saveTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            await self?.save()
        }
    }

    @discardableResult
    public func evaluate(_ snapshot: MarketSnapshot, contracts: [ContractMarket] = []) -> [StrategyStatus] {
        let engine = StrategyEngine()
        // Direct callers (tests and one-off REST evaluations) may not have
        // gone through TradingBackend.contracts(). Warm the cache lazily once;
        // the realtime path refreshes it explicitly when the contract list
        // changes, so this branch is never reached for every candle.
        if !universeResolved, !contracts.isEmpty {
            refreshStrategyUniverse(contracts)
        }
        // 缓存 BTC 1 小时 K 线，供扫顶反转策略的 BTC<SMA200 门控使用
        if snapshot.instrumentID == "BTC-USDT-SWAP", snapshot.interval == .oneHour {
            btcHourlyCandles = snapshot.candles
        }
        switch snapshot.interval {
        case .oneHour:
            structureCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        case .fifteenMinutes:
            confirmationCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        default:
            break
        }
        var changed = false
        var evaluatedStatuses: [StrategyStatus] = []
        for config in strategies where resolvedTargetsByStrategy[config.id]?.contains(snapshot.instrumentID) == true {
            if config.type == .doublePumpExhaustionShort {
                // The formal DME rule requires at least 10m USDT rolling
                // quote volume. Fail closed when the contract snapshot is
                // missing or below the guardrail; this keeps low-liquidity
                // instruments out even if a stale resolved target list still
                // contains them.
                guard let contract = resolvedContractsByID[snapshot.instrumentID],
                      StrategyUniverseRules.isEligibleDoublePump(contract) else { continue }
            }
            if !config.type.hasRuntimeHandler {
                // An installed package can outlive its compiled adapter. Keep
                // the orphan visible and paused, while leaving its orders,
                // fills, and risk records untouched for reconciliation.
                let orphan = StrategyStatus(id: config.id, state: .paused)
                if statuses[config.id] != orphan {
                    statuses[config.id] = orphan
                    changed = true
                }
                evaluatedStatuses.append(orphan)
                continue
            }
            if config.type == .sweepReversalShort {
                // The structure close is an arm-only event. It never submits
                // an order and cannot be mistaken for a 1h market entry.
                //
                // 必须返回**该标的自己**的状态。此前返回全局汇总状态
                // (`statuses[config.id]`，即最后被评估标的的状态)，会让 A 标的
                // 未提交的信号在时间戳相同的 B 标的 1h 收盘时被当成 B 的信号下单。
                guard snapshot.interval == .fifteenMinutes else {
                    let current = statusesByInstrument[config.id]?[snapshot.instrumentID]
                        ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                    statusesByInstrument[config.id, default: [:]][snapshot.instrumentID] = current
                    evaluatedStatuses.append(current)
                    continue
                }
                guard let structure = structureCandlesByInstrument[snapshot.instrumentID] else {
                    let current = statusesByInstrument[config.id]?[snapshot.instrumentID]
                        ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                    statusesByInstrument[config.id, default: [:]][snapshot.instrumentID] = current
                    evaluatedStatuses.append(current)
                    continue
                }
                let previous = statusesByInstrument[config.id]?[snapshot.instrumentID]
                    ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                let next = engine.evaluateWithConfirmation(config: config, structureCandles: structure, confirmationCandles: snapshot.candles, previous: previous, btcCandles: btcHourlyCandles)
                statusesByInstrument[config.id, default: [:]][snapshot.instrumentID] = next
                evaluatedStatuses.append(next)
                if statuses[config.id] != next {
                    statuses[config.id] = next
                    changed = true
                }
                continue
            }
            if config.type == .doublePumpExhaustionShort {
                guard snapshot.interval == .fifteenMinutes else {
                    let current = statusesByInstrument[config.id]?[snapshot.instrumentID]
                        ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                    evaluatedStatuses.append(current)
                    continue
                }
                let previous = statusesByInstrument[config.id]?[snapshot.instrumentID]
                    ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                let next = engine.evaluate(config: config, candles: snapshot.candles, previous: previous)
                statusesByInstrument[config.id, default: [:]][snapshot.instrumentID] = next
                evaluatedStatuses.append(next)
                if statuses[config.id] != next {
                    statuses[config.id] = next
                    changed = true
                }
                continue
            }
            guard config.interval == snapshot.interval else { continue }
            let previous = statusesByInstrument[config.id]?[snapshot.instrumentID]
                ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
            let next = engine.evaluate(config: config, candles: snapshot.candles, previous: previous, btcCandles: config.type == .sweepReversalShort ? btcHourlyCandles : nil)
            statusesByInstrument[config.id, default: [:]][snapshot.instrumentID] = next
            evaluatedStatuses.append(next)
            if statuses[config.id] != next {
                statuses[config.id] = next
                changed = true
            }
        }
        // The recompute path fires once per confirmed bar and per chart load;
        // coalesce the resulting writes instead of hitting disk every time.
        if changed { scheduleSave() }
        return evaluatedStatuses
    }

    public func setRisk(_ value: RiskSnapshot) {
        guard risk != value else { return }
        risk = value
        save()
    }

    /// `paper-state.json` is the single persisted record of strategies,
    /// statuses, the paper ledger and account risk.
    private struct StoreFile: Codable {
        var schemaVersion = 1
        let strategies: [StrategyConfig]
        let statuses: [String: StrategyStatus]
        let orders: [PaperOrder]
        let fills: [PaperFill]
        let risk: RiskSnapshot

        init(strategies: [StrategyConfig], statuses: [UUID: StrategyStatus], orders: [PaperOrder], fills: [PaperFill], risk: RiskSnapshot) {
            self.strategies = strategies
            self.statuses = Dictionary(uniqueKeysWithValues: statuses.map { ($0.key.uuidString, $0.value) })
            self.orders = orders
            self.fills = fills
            self.risk = risk
        }
    }

    private static func loadFile(directory: URL, decoder: JSONDecoder) -> StoreFile? {
        let url = directory.appendingPathComponent("paper-state.json")
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? decoder.decode(StoreFile.self, from: data)
    }

    private func save() {
        Self.persist(StoreFile(strategies: strategies, statuses: statuses, orders: orders, fills: fills, risk: risk), directory: directory, encoder: encoder)
    }

    private static func persist(_ file: StoreFile, directory: URL, encoder: JSONEncoder) {
        guard let data = try? encoder.encode(file) else { return }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? data.write(to: directory.appendingPathComponent("paper-state.json"), options: .atomic)
    }

    public enum StoreError: LocalizedError {
        case conflict, notFound, running, unsupported

        public var errorDescription: String? {
            switch self {
            case .conflict: return "每种策略规则只允许创建一个实例"
            case .notFound: return "策略实例不存在"
            case .running: return "策略运行中，请先停止策略后再删除"
            case .unsupported: return "当前支持确认的 1 小时或 15 分钟山寨币策略；运行时扫描动态合规币种池，每次只允许一个币种下单或持仓，单笔风险固定为策略资金池权益的 10%"
        }
        }
    }
}

/// Cache TTLs for `MarketDataService`. Every uncached read spawns an ATK CLI
/// process, so short windows collapse bursts from several stream clients and
/// the auxiliary poller into one call per window.
public struct MarketCacheTTL: Sendable {
    public var ticker: TimeInterval
    public var snapshot: TimeInterval
    public var contracts: TimeInterval
    public var account: TimeInterval
    public var positions: TimeInterval
    public var orders: TimeInterval

    public init(ticker: TimeInterval = 2, snapshot: TimeInterval = 60, contracts: TimeInterval = 60, account: TimeInterval = 5, positions: TimeInterval = 5, orders: TimeInterval = 5) {
        self.ticker = ticker; self.snapshot = snapshot; self.contracts = contracts; self.account = account
        self.positions = positions; self.orders = orders
    }
}

public actor MarketDataService {
    private struct Fresh<Value> {
        let value: Value
        let fetchedAt: Date
        func isValid(ttl: TimeInterval) -> Bool { fetchedAt.addingTimeInterval(ttl) > Date() }
    }

    private let client: ATKClient
    /// The OKX business socket is the sole source for live candle updates.
    /// ATK remains available for the authenticated account surface and the
    /// one-time historical snapshot used to paint the chart on subscription.
    private let marketSocket: OKXCandleSocket
    private let ttl: MarketCacheTTL

    private var cache: [String: MarketSnapshot] = [:]
    private var cacheFetchedAt: [String: Date] = [:]
    private var cacheTasks: [String: Task<MarketSnapshot, Error>] = [:]

    private var tickerCache: [String: Fresh<MarketTicker>] = [:]
    private var tickerTasks: [String: Task<MarketTicker, Error>] = [:]

    private var instrumentSpecCache: [String: Fresh<SwapInstrumentSpec>] = [:]
    private var instrumentSpecTasks: [String: Task<SwapInstrumentSpec, Error>] = [:]

    private var contractsCache: Fresh<[ContractMarket]>?
    private var contractsTask: Task<[ContractMarket], Error>?

    private var accountCache: Fresh<AccountOverview>?
    private var accountTask: Task<AccountOverview, Error>?
    private var accountGeneration = 0

    private var positionsCache: Fresh<[PositionSnapshot]>?
    private var positionsTask: Task<[PositionSnapshot], Error>?
    private var positionsGeneration = 0

    private var ordersCache: Fresh<[OrderSnapshot]>?
    private var ordersTask: Task<[OrderSnapshot], Error>?
    private var ordersGeneration = 0

    public init(client: ATKClient = ATKClient(), marketSocket: OKXCandleSocket = OKXCandleSocket(), ttl: MarketCacheTTL = MarketCacheTTL()) {
        self.client = client
        self.marketSocket = marketSocket
        self.ttl = ttl
    }

    public func snapshot(instrumentID: String, interval: KlineInterval, forceRefresh: Bool = false) async throws -> MarketSnapshot {
        let cacheKey = key(instrumentID, interval)
        // Historical candles are a prewarm only. Once a subscription has
        // loaded them, the WSS stream owns all subsequent candle updates; the
        // TTL re-arms a REST reload only to heal gaps after a stream outage.
        if !forceRefresh, let cached = cache[cacheKey], let fetchedAt = cacheFetchedAt[cacheKey], fetchedAt.addingTimeInterval(ttl.snapshot) > Date() {
            return cached
        }
        if let task = cacheTasks[cacheKey] { return try await task.value }
        let task = Task { [client] in
            MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: try await client.marketCandles(instrumentID: instrumentID, interval: interval))
        }
        cacheTasks[cacheKey] = task
        do {
            let value = try await task.value
            var mergedCandles = value.candles
            // REST is a historical backfill. Merge bars received from WSS
            // while the request was in flight so a refresh cannot roll the
            // realtime window backwards.
            if let previous = cache[cacheKey] {
                for candle in previous.candles {
                    if let index = mergedCandles.firstIndex(where: { $0.timestamp == candle.timestamp }) {
                        if !(mergedCandles[index].confirmed && !candle.confirmed) {
                            mergedCandles[index] = candle
                        }
                    } else {
                        mergedCandles.upsert(candle)
                    }
                }
            }
            if mergedCandles.count > 500 { mergedCandles.removeFirst(mergedCandles.count - 500) }
            let merged = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: mergedCandles)
            cache[cacheKey] = merged
            cacheFetchedAt[cacheKey] = Date()
            cacheTasks[cacheKey] = nil
            return merged
        } catch {
            cacheTasks[cacheKey] = nil
            throw error
        }
    }

    public func ticker(instrumentID: String) async throws -> MarketTicker {
        if let cached = tickerCache[instrumentID], cached.isValid(ttl: ttl.ticker) { return cached.value }
        if let task = tickerTasks[instrumentID] { return try await task.value }
        let task = Task { [client] in try await client.marketTicker(instrumentID: instrumentID) }
        tickerTasks[instrumentID] = task
        do {
            let value = try await task.value
            tickerCache[instrumentID] = Fresh(value: value, fetchedAt: Date())
            tickerTasks[instrumentID] = nil
            return value
        } catch {
            tickerTasks[instrumentID] = nil
            throw error
        }
    }

    /// Returns the authenticated exchange specification for a swap.  Specs
    /// are cached separately from ticker data because a stale or missing
    /// contract multiplier changes the meaning of every order quantity.
    public func instrumentSpec(instrumentID: String) async throws -> SwapInstrumentSpec {
        if let cached = instrumentSpecCache[instrumentID], cached.isValid(ttl: ttl.contracts) {
            return cached.value
        }
        if let task = instrumentSpecTasks[instrumentID] { return try await task.value }
        let task = Task { [client] in try await client.marketInstrumentSpec(instrumentID: instrumentID) }
        instrumentSpecTasks[instrumentID] = task
        do {
            let value = try await task.value
            instrumentSpecCache[instrumentID] = Fresh(value: value, fetchedAt: Date())
            instrumentSpecTasks[instrumentID] = nil
            return value
        } catch {
            instrumentSpecTasks[instrumentID] = nil
            throw error
        }
    }

    public func contracts(forceRefresh: Bool = false) async throws -> [ContractMarket] {
        if !forceRefresh, let cached = contractsCache, cached.isValid(ttl: ttl.contracts) { return cached.value }
        if let task = contractsTask { return try await task.value }
        let task = Task { [client] in try await client.marketContracts() }
        contractsTask = task
        do {
            let value = try await task.value
            contractsCache = Fresh(value: value, fetchedAt: Date())
            contractsTask = nil
            return value
        } catch {
            contractsTask = nil
            throw error
        }
    }

    public func account() async throws -> AccountOverview {
        if let cached = accountCache, cached.isValid(ttl: ttl.account) { return cached.value }
        if let task = accountTask { return try await task.value }
        let generation = accountGeneration
        let task = Task { [client] in try await client.accountOverview() }
        accountTask = task
        do {
            let value = try await task.value
            if generation == accountGeneration {
                accountCache = Fresh(value: value, fetchedAt: Date())
                accountTask = nil
            }
            return value
        } catch {
            if generation == accountGeneration { accountTask = nil }
            throw error
        }
    }

    /// Drops the authenticated-account caches after a successful order so the
    /// next read reflects the post-trade state instead of waiting for TTL.
    public func invalidateAccountState() {
        accountGeneration &+= 1
        positionsGeneration &+= 1
        ordersGeneration &+= 1
        accountTask?.cancel()
        positionsTask?.cancel()
        ordersTask?.cancel()
        accountTask = nil
        positionsTask = nil
        ordersTask = nil
        accountCache = nil
        positionsCache = nil
        ordersCache = nil
    }

    public func placeLiveOrder(_ request: LiveOrderRequest) async throws -> LiveOrderCommandResult { try await client.placeSwapOrder(request) }
    public func placeDemoOrder(_ request: LiveOrderRequest) async throws -> LiveOrderCommandResult { try await client.placeDemoSwapOrder(request) }
    public func privateOrder(instrumentID: String, clientOrderID: String, demo: Bool) async throws -> OrderSnapshot? {
        try await client.swapOrder(instrumentID: instrumentID, clientOrderID: clientOrderID, demo: demo)
    }
    public func cancelLiveOrder(instrumentID: String, orderID: String) async throws { try await client.cancelLiveSwapOrder(instrumentID: instrumentID, orderID: orderID); invalidateAccountState() }
    public func cancelDemoOrder(instrumentID: String, orderID: String) async throws { try await client.cancelDemoSwapOrder(instrumentID: instrumentID, orderID: orderID); invalidateAccountState() }
    public func closeLivePosition(instrumentID: String, positionSide: String?, marginMode: String? = nil) async throws { try await client.closeLiveSwapPosition(instrumentID: instrumentID, positionSide: positionSide, marginMode: marginMode); invalidateAccountState() }
    public func closeDemoPosition(instrumentID: String, positionSide: String?, marginMode: String? = nil) async throws { try await client.closeDemoSwapPosition(instrumentID: instrumentID, positionSide: positionSide, marginMode: marginMode); invalidateAccountState() }

    public func privatePositions() async throws -> [PositionSnapshot] {
        if let cached = positionsCache, cached.isValid(ttl: ttl.positions) { return cached.value }
        if let task = positionsTask { return try await task.value }
        let generation = positionsGeneration
        let task = Task { [client] in try await client.swapPositions() }
        positionsTask = task
        do {
            let value = try await task.value
            if generation == positionsGeneration {
                positionsCache = Fresh(value: value, fetchedAt: Date())
                positionsTask = nil
            }
            return value
        } catch {
            if generation == positionsGeneration { positionsTask = nil }
            throw error
        }
    }

    public func privateOrders() async throws -> [OrderSnapshot] {
        if let cached = ordersCache, cached.isValid(ttl: ttl.orders) { return cached.value }
        if let task = ordersTask { return try await task.value }
        let generation = ordersGeneration
        let task = Task { [client] in try await client.swapOrders() }
        ordersTask = task
        do {
            let value = try await task.value
            if generation == ordersGeneration {
                ordersCache = Fresh(value: value, fetchedAt: Date())
                ordersTask = nil
            }
            return value
        } catch {
            if generation == ordersGeneration { ordersTask = nil }
            throw error
        }
    }

    public func closedSwapPositions(instrumentID: String) async throws -> [ClosedSwapPositionSnapshot] {
        try await client.closedSwapPositions(instrumentID: instrumentID)
    }

    /// `OKXCandleSocket` owns the socket lifecycle and reconnects with
    /// exponential backoff. No timer or REST fallback runs in the real-time
    /// candle path.
    public func candleEvents(instrumentID: String, interval: KlineInterval) -> AsyncStream<OKXCandleEvent> {
        marketSocket.candleEvents(instrumentID: instrumentID, interval: interval)
    }

    public func cacheRealtimeCandle(_ candle: Candle, instrumentID: String, interval: KlineInterval) {
        let cacheKey = key(instrumentID, interval)
        guard let previous = cache[cacheKey] else { return }
        var values = previous.candles
        // A delayed unconfirmed frame must not undo a confirmed candle.
        if let index = values.firstIndex(where: { $0.timestamp == candle.timestamp }), values[index].confirmed, !candle.confirmed {
            return
        }
        values.upsert(candle)
        if values.count > 500 { values.removeFirst(values.count - 500) }
        cache[cacheKey] = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: values, updatedAt: .now)
    }
    public func cached(instrumentID: String, interval: KlineInterval) -> MarketSnapshot? { cache[key(instrumentID, interval)] }

    private func key(_ instrumentID: String, _ interval: KlineInterval) -> String { "\(instrumentID):\(interval.rawValue)" }
}

public actor TradingBackend {
    public let market: MarketDataService
    public let paper: PaperTradingStore
    /// Installed strategy packages are application data, independent from
    /// the laboratory checkout.  Package install/uninstall APIs below are the
    /// only runtime mutation path for this catalog.
    public let strategyPackages: StrategyPackageRegistry
    public let candles = CandleStore()
    public let riskEngine: RiskEngine
    public let broker: PaperBroker
    private var submittedSignals: Set<UUID> = []
    /// Insertion order of `submittedSignals`, used to evict the oldest entries
    /// so the dedupe set cannot grow without bound over long sessions.
    private var submittedSignalOrder: [UUID] = []
    private var strategyEntrySubmissionsInFlight: Set<UUID> = []
    /// Prevents a resolver that started before pause/delete from re-keying a
    /// resting entry after the final cancellation sweep has already run.
    private var strategyEntryClosures: Set<UUID> = []
    /// Monotonic generations invalidate resolver results captured before a
    /// pause/delete operation, including tasks that resume after its closing
    /// set has been cleared.
    private var strategyEntryClosureEpochs: [UUID: UInt64] = [:]
    private var strategyEntryInFlightInstruments: Set<String> = []
    private var runtimeLogs: [RuntimeLog] = []
    private var runtimeLogFileLines = 0
    private let runtimeLogURL: URL
    private var loggedSignalIDs: Set<UUID> = []
    private var loggedFillIDs: Set<UUID> = []
    /// Manual live orders retain an explicit operator enablement gate. Strategy
    /// orders do not read this flag; their mode is selected from the account
    /// profile in `submitStrategyOrder`.
    private var liveTradingEnabled = false
    private var contractUniverse: [ContractMarket] = []
    private var globalRiskTripHandled = false
    private var globalRiskRemoteCleanupComplete = false
    private var globalRiskRemoteCleanupInFlight = false
    private var submittedExitPositionIDs: Set<String> = []
    private var strategyExitMonitorInFlight = false
    /// Reduce-only exits remain persisted until a later account snapshot
    /// confirms that the originating position is flat.
    private struct PendingRemoteExit: Codable {
        let strategyID: UUID
        let entryOrderID: String?
        let positionID: String
        let instrumentID: String
        let quantity: Decimal
        let entryPrice: Decimal
        let exitPrice: Decimal
        let side: String
        let contractValue: Decimal
        let reservedNotional: Decimal
        let exitRisk: Decimal
        var remoteOrderID: String?
        var realizedPnL: Decimal? = nil
    }
    private var pendingRemoteExits: [String: PendingRemoteExit] = [:]
    private let pendingRemoteExitURL: URL

    private static func strategyClientOrderID(_ id: UUID) -> String {
        id.uuidString.replacingOccurrences(of: "-", with: "")
    }
    /// Reservations created by the manual live/demo order endpoints. Strategy
    /// orders have their own position lifecycle; manual orders need a small
    /// reconciliation ledger so a successful submit cannot permanently consume
    /// the global notional limit, while a still-open remote position remains
    /// protected by the reservation.
    private struct RemoteReservation: Codable {
        let instrumentID: String
        let notional: Decimal
        let createdAt: Date
        /// A reservation is tagged with the strategy that authorized it so a
        /// rejected entry (or a native exchange stop/take-profit) can return
        /// both global exposure and the strategy pool reservation exactly
        /// once.  Manual orders leave this nil.
        let strategyID: UUID?
        let margin: Decimal
        let riskAmount: Decimal
        let closedPosition: Bool
        /// Stable OKX position identity observed after entry fill.  A filled
        /// entry with no current position is only safe to settle when this ID
        /// appears in closed-position history.
        var positionID: String?
        /// Set while the remote command is suspended.  Reconciliation must
        /// carry this amount forward even when the exchange has not published
        /// the newly submitted order yet.  Mutable because a restart ends
        /// every in-flight command: load clears the flag so the entry is
        /// resolved by its client order id instead.
        var inFlight: Bool
        /// Client order id of a submission whose outcome OKX could not
        /// confirm. Only these reservations are keyed `unresolved-<clOrdId>`;
        /// a later lookup either re-keys them to the order id or releases
        /// them.
        let clientOrderID: String?
        /// Whether the unresolved order was sent with `--demo`.
        let demo: Bool?
        /// Ledger entry that carries strategy ownership until the exchange
        /// order id of an unresolved submission is known.
        let localOrderID: UUID?
        /// When a cancel was successfully sent for an entry that was found
        /// resting. A later "order does not exist" must not be read as "never
        /// placed": the cancel may be racing a fill, so the claim is held
        /// until either the order, a position or history settles it.
        var cancelRequestedAt: Date?

        init(instrumentID: String, notional: Decimal, createdAt: Date = .now,
             strategyID: UUID? = nil, margin: Decimal? = nil,
             riskAmount: Decimal = 0, closedPosition: Bool = false,
             inFlight: Bool = false, positionID: String? = nil,
             clientOrderID: String? = nil, demo: Bool? = nil,
             localOrderID: UUID? = nil, cancelRequestedAt: Date? = nil) {
            self.instrumentID = instrumentID
            self.notional = notional
            self.createdAt = createdAt
            self.strategyID = strategyID
            self.margin = margin ?? notional
            self.riskAmount = riskAmount
            self.closedPosition = closedPosition
            self.inFlight = inFlight
            self.positionID = positionID
            self.clientOrderID = clientOrderID
            self.demo = demo
            self.localOrderID = localOrderID
            self.cancelRequestedAt = cancelRequestedAt
        }
    }
    private static let unresolvedReservationPrefix = "unresolved-"
    private static func isUnresolvedReservation(_ key: String) -> Bool {
        key.hasPrefix(unresolvedReservationPrefix)
    }
    private var remoteReservations: [String: RemoteReservation] = [:]
    private let remoteReservationURL: URL
    private var terminalRemoteOrderObservations: [String: Int] = [:]
    private var riskRestored = false
    /// Serializes the short ledger mutation section. Network reads stay
    /// outside this gate; RiskEngine calls and reservation-map changes stay
    /// inside it so a reconciliation release cannot subtract a newer order.
    private var riskReservationMutationInFlight = false
    /// Monotonically tags authenticated reservation snapshots. A slower,
    /// older read must not overwrite a newer reconciliation result.
    private var reconciliationGeneration: UInt64 = 0

    private func acquireRiskReservationMutation() async {
        // This gate is held only across short RiskEngine/map mutations.
        // Cooperative polling avoids a continuation that could strand a
        // cancelled waiter and deadlock future orders.
        while riskReservationMutationInFlight {
            await Task.yield()
        }
        riskReservationMutationInFlight = true
    }

    private func releaseRiskReservationMutation() {
        riskReservationMutationInFlight = false
    }

    private func withRiskReservationMutation<T>(_ operation: () async throws -> T) async rethrows -> T {
        await acquireRiskReservationMutation()
        defer { releaseRiskReservationMutation() }
        return try await operation()
    }

    private func authorizeRemoteSubmission(instrumentID: String, notional: Decimal,
                                            reduceOnly: Bool = false,
                                            strategyID: UUID? = nil,
                                            poolAllocationPercent: Decimal = 100,
                                            riskAmount: Decimal = 0,
                                            maxOpenRiskPercent: Decimal? = nil,
                                            maxConcurrentPositions: Int? = nil,
                                            clientOrderID: String,
                                            demo: Bool,
                                            localOrderID: UUID? = nil) async -> (RiskDecision, String?) {
        return await withRiskReservationMutation {
            // The claim is keyed by the client order id; a reused id would
            // overwrite a live claim and lose its exposure.
            guard reduceOnly || remoteReservations[Self.unresolvedReservationPrefix + clientOrderID] == nil else {
                return (RiskDecision(allowed: false, reason: "客户端订单号正在使用中：\(clientOrderID)"), nil)
            }
            let decision = await riskEngine.authorize(
                instrumentID: instrumentID, notional: notional, margin: notional,
                reduceOnly: reduceOnly, strategyID: strategyID,
                poolAllocationPercent: poolAllocationPercent, riskAmount: riskAmount,
                maxOpenRiskPercent: maxOpenRiskPercent,
                maxConcurrentPositions: maxConcurrentPositions
            )
            guard decision.allowed, !reduceOnly else { return (decision, nil) }
            let token = beginRemoteSubmission(
                instrumentID: instrumentID, notional: notional,
                clientOrderID: clientOrderID, demo: demo,
                strategyID: strategyID, margin: notional,
                riskAmount: riskAmount, closedPosition: strategyID != nil,
                localOrderID: localOrderID
            )
            // Persist both before the exchange command, risk snapshot first.
            // A crash between the two writes then leaves a pool reservation
            // without a claim (conservatively over-reserved; the order was
            // never sent), never a claim whose later release would subtract
            // from a pool that never held it and under-reserve another
            // position. Every reservation save runs under this gate, so a
            // reentrant task cannot write the claim ahead of the snapshot.
            await paper.setRisk(await riskEngine.snapshot())
            saveRemoteReservations()
            return (decision, token)
        }
    }

    private func failRemoteSubmission(_ token: String?) async {
        guard let token else { return } // Reduce-only orders reserve nothing.
        await withRiskReservationMutation {
            guard let reservation = remoteReservations.removeValue(forKey: token) else { return }
            await riskEngine.release(
                instrumentID: reservation.instrumentID, notional: reservation.notional,
                strategyID: reservation.strategyID, margin: reservation.margin,
                riskAmount: reservation.riskAmount,
                closedPosition: reservation.closedPosition
            )
            saveRemoteReservations()
        }
    }

    /// Registers exposure before awaiting a remote order command.  Actors may
    /// re-enter at that await, so account reconciliation must see this token
    /// even if the exchange has not returned an order ID yet.
    /// Registers the entry as a durable claim before the exchange command is
    /// sent. The key is the client order id, which is known up front and is
    /// stable across a crash, so a restarted process can resolve the claim by
    /// looking that id up. `inFlight` marks the command as still awaiting its
    /// response in this process.
    private func beginRemoteSubmission(instrumentID: String, notional: Decimal,
                                       clientOrderID: String, demo: Bool,
                                       strategyID: UUID? = nil, margin: Decimal? = nil,
                                       riskAmount: Decimal = 0,
                                       closedPosition: Bool = false,
                                       localOrderID: UUID? = nil) -> String {
        let token = Self.unresolvedReservationPrefix + clientOrderID
        remoteReservations[token] = RemoteReservation(
            instrumentID: instrumentID,
            notional: notional,
            strategyID: strategyID,
            margin: margin,
            riskAmount: riskAmount,
            closedPosition: closedPosition,
            inFlight: true,
            clientOrderID: clientOrderID,
            demo: demo,
            localOrderID: localOrderID
        )
        return token
    }

    /// Converts a local submission token into the authenticated order ID.
    /// Keeping the same reservation across the await closes the stale remote
    /// snapshot window immediately after a successful placement.
    private func confirmRemoteSubmission(_ token: String?, orderID: String) async {
        guard let token else { return }
        await withRiskReservationMutation {
            guard let reservation = remoteReservations.removeValue(forKey: token) else { return }
            remoteReservations[orderID] = RemoteReservation(
                instrumentID: reservation.instrumentID,
                notional: reservation.notional,
                createdAt: reservation.createdAt,
                strategyID: reservation.strategyID,
                margin: reservation.margin,
                riskAmount: reservation.riskAmount,
                closedPosition: reservation.closedPosition,
                inFlight: false,
                positionID: reservation.positionID,
                clientOrderID: reservation.clientOrderID,
                demo: reservation.demo,
                localOrderID: reservation.localOrderID,
                cancelRequestedAt: reservation.cancelRequestedAt
            )
            saveRemoteReservations()
        }
    }

    /// Both the submit response and the clOrdId lookup failed, so OKX may
    /// hold the order. Releasing the reservation here would let the global
    /// caps and the strategy pool admit new exposure beside a position that
    /// nothing tracks. Keep it under a durable key instead; reconciliation
    /// settles it once a lookup answers.
    /// The submit command and the clOrdId lookup both failed, so the claim
    /// stays exactly where the pre-submit registration put it: keyed by the
    /// client order id, out of flight, and settled by the next
    /// authenticated reconciliation.
    private func markSubmissionUnresolved(_ token: String?) async {
        guard let token else { return }
        await withRiskReservationMutation {
            guard var reservation = remoteReservations[token] else { return }
            reservation.inFlight = false
            remoteReservations[token] = reservation
            saveRemoteReservations()
        }
    }

    /// Settles deferred submissions by their client order id. An accepted
    /// order takes over the reservation (and the strategy ledger entry gets
    /// its order id, so native SL/TP settlement can find it); an order OKX
    /// confirms absent releases it; another failure keeps it reserved.
    private func resolveUnconfirmedSubmissions() async {
        let unresolved = remoteReservations.compactMap { key, reservation -> (String, RemoteReservation, String, UInt64)? in
            // A submission still awaiting its own response is settled by its
            // caller; a lookup now could miss an order OKX is about to accept.
            guard Self.isUnresolvedReservation(key), !reservation.inFlight,
                  let clientOrderID = reservation.clientOrderID else { return nil }
            let epoch = reservation.strategyID.flatMap { strategyEntryClosureEpochs[$0] } ?? 0
            return (key, reservation, clientOrderID, epoch)
        }
        guard !unresolved.isEmpty else { return }
        // A pause or delete can run its cancel pass while an entry is still
        // unresolved. Once that entry turns out to be resting, it must be
        // cancelled before it is settled, or it fills later with no running
        // strategy behind it. A running strategy's market entry is merely in
        // transit and manual orders belong to the user, so both are left
        // alone. Network calls stay outside the mutation gate.
        let running = Set((await paper.allStrategies()).filter(\.enabled).map(\.id))
        var outcomes: [(key: String, clientOrderID: String, outcome: FailedSubmissionOutcome, cancelRequested: Bool, closureEpoch: UInt64)] = []
        for (key, reservation, clientOrderID, closureEpoch) in unresolved {
            let demo = reservation.demo ?? true
            var outcome = await resolveFailedSubmission(instrumentID: reservation.instrumentID,
                                                        clientOrderID: clientOrderID, demo: demo)
            // A previous pass may already have cancelled this entry.
            var cancelRequested = reservation.cancelRequestedAt != nil
            let strategyIsClosing = reservation.strategyID.map { strategyEntryClosures.contains($0) } ?? false
            if case let .accepted(orderID, status) = outcome,
               OrderLifecycle.isResting(status),
               let strategyID = reservation.strategyID,
               strategyIsClosing || !running.contains(strategyID) {
                if cancelRequested == false {
                    // Persist the cancellation intent before the network call.
                    // A process can exit after the exchange accepts the cancel
                    // but before the outcome mutation below runs.
                    cancelRequested = true
                    await withRiskReservationMutation {
                        guard var current = remoteReservations[key], current.cancelRequestedAt == nil else { return }
                        current.cancelRequestedAt = .now
                        remoteReservations[key] = current
                        saveRemoteReservations()
                    }
                }
                if let cancelError = await cancelRestingOrder(orderID: orderID, instrumentID: reservation.instrumentID) {
                    appendLog("已停止策略的挂单补撤失败（\(reservation.instrumentID) 订单 \(orderID)），下次核对重试：\(cancelError.localizedDescription)", level: "warning")
                    // Stay unresolved so the next pass retries the cancel.
                    outcome = .unknown(cancelError)
                } else {
                    cancelRequested = true
                    appendLog("已停止策略的挂单已补撤：\(reservation.instrumentID) 订单 \(orderID)", level: "warning")
                    // Settle on the post-cancel state: a cancel can race a
                    // partial fill, and only the exchange knows which won.
                    outcome = await resolveFailedSubmission(instrumentID: reservation.instrumentID,
                                                            clientOrderID: clientOrderID, demo: demo)
                    if case let .accepted(_, after) = outcome, OrderLifecycle.isResting(after) {
                        outcome = .unknown(ATKError.unavailable("撤单尚未生效"))
                    }
                }
            }
            // A cancelled entry whose order then disappears from lookups is
            // ambiguous: the cancel may be racing a fill. Release it only
            // once the exchange shows no position for the instrument and the
            // grace period has passed. Network reads stay outside the gate.
            if case .notPlaced = outcome, cancelRequested {
                let positions: [PositionSnapshot]
                do {
                    positions = try await market.privatePositions()
                } catch {
                    // An unavailable position snapshot cannot prove that the
                    // cancelled order left no exposure behind.
                    outcome = .unknown(error)
                    outcomes.append((key, clientOrderID, outcome, cancelRequested, closureEpoch))
                    continue
                }
                let hasPosition = positions.contains {
                    $0.instrumentID == reservation.instrumentID && abs($0.quantity) > 0
                }
                if hasPosition || Date().timeIntervalSince(reservation.cancelRequestedAt ?? .now) < Self.cancelSettlementGrace {
                    outcome = .unknown(ATKError.unavailable("撤单后订单暂时查询不到"))
                }
            }
            outcomes.append((key, clientOrderID, outcome, cancelRequested, closureEpoch))
        }
        await withRiskReservationMutation {
            var releases: [RemoteReservation] = []
            for (key, clientOrderID, outcome, cancelRequested, closureEpoch) in outcomes {
                // A strategy exit may have settled the entry meanwhile.
                guard var reservation = remoteReservations[key], !reservation.inFlight else { continue }
                if cancelRequested, reservation.cancelRequestedAt == nil {
                    // Persist the cancel mark before the outcome switch. A
                    // later pass sees the order missing rather than resting,
                    // so the local flag would be false again; the durable
                    // mark is what keeps that absence from releasing a claim
                    // whose cancel may have raced a fill.
                    reservation.cancelRequestedAt = .now
                    remoteReservations[key] = reservation
                    saveRemoteReservations()
                }
                switch outcome {
                case let .accepted(orderID, _):
                    if let strategyID = reservation.strategyID,
                       strategyEntryClosureEpochs[strategyID, default: 0] != closureEpoch {
                        // A resolver may have captured a running strategy
                        // before pause/delete started. Leave every accepted
                        // result under its unresolved key so the closing
                        // operation's own resolver can settle it with a fresh
                        // lifecycle snapshot. This is deliberately
                        // conservative for filled or unrecognized states too.
                        continue
                    }
                    remoteReservations.removeValue(forKey: key)
                    if remoteReservations[orderID] == nil {
                        remoteReservations[orderID] = RemoteReservation(
                            instrumentID: reservation.instrumentID,
                            notional: reservation.notional,
                            createdAt: reservation.createdAt,
                            strategyID: reservation.strategyID,
                            margin: reservation.margin,
                            riskAmount: reservation.riskAmount,
                            closedPosition: reservation.closedPosition,
                            positionID: reservation.positionID,
                            clientOrderID: reservation.clientOrderID,
                            demo: reservation.demo,
                            localOrderID: reservation.localOrderID,
                            cancelRequestedAt: reservation.cancelRequestedAt
                        )
                    } else {
                        // The order id is already reserved; keep one claim.
                        releases.append(reservation)
                    }
                    if let localOrderID = reservation.localOrderID {
                        await paper.attachRemoteOrderID(orderID, toLocalOrder: localOrderID)
                    }
                    appendLog("下单结果已核实：OKX 已接受 \(reservation.instrumentID) 订单 \(orderID)（clOrdId \(clientOrderID)）", level: "warning")
                case let .terminal(_, status):
                    remoteReservations.removeValue(forKey: key)
                    if let localOrderID = reservation.localOrderID {
                        await paper.markLocalOrderTerminal(localOrderID, status: status)
                    }
                    releases.append(reservation)
                    appendLog("下单结果已核实：OKX 订单已是终态（\(status)），已释放 \(reservation.instrumentID) 的风控占用", level: "warning")
                case .notPlaced:
                    remoteReservations.removeValue(forKey: key)
                    if let localOrderID = reservation.localOrderID {
                        await paper.markLocalOrderTerminal(localOrderID, status: "rejected")
                    }
                    releases.append(reservation)
                    appendLog("下单结果已核实：OKX 没有 clOrdId \(clientOrderID) 的订单，已释放 \(reservation.instrumentID) 的风控占用", level: "warning")
                case .unknown:
                    continue
                }
                saveRemoteReservations()
            }
            for reservation in releases {
                await riskEngine.release(
                    instrumentID: reservation.instrumentID, notional: reservation.notional,
                    strategyID: reservation.strategyID, margin: reservation.margin,
                    riskAmount: reservation.riskAmount,
                    closedPosition: reservation.closedPosition
                )
            }
        }
    }

    /// How long a cancelled entry whose order then disappears from OKX
    /// lookups keeps its claim before the absence of any position is taken as
    /// proof that nothing was executed. A cancel can race a fill, so a bare
    /// "order does not exist" is never enough on its own.
    static let cancelSettlementGrace: TimeInterval = 60

    private static let submittedSignalLimit = 500
    private static let runtimeLogMemoryLimit = 1_000

    public init(market: MarketDataService = MarketDataService(), paper: PaperTradingStore = PaperTradingStore(), riskEngine: RiskEngine = RiskEngine(), logDirectory: URL? = nil, strategyPackages: StrategyPackageRegistry? = nil) {
        self.market = market
        self.paper = paper
        self.strategyPackages = strategyPackages ?? StrategyPackageRegistry()
        self.riskEngine = riskEngine
        self.broker = PaperBroker(risk: riskEngine)
        let directory = logDirectory ?? paper.stateDirectory
        self.runtimeLogURL = directory.appendingPathComponent("runtime-log.jsonl")
        let (logs, fileLines) = Self.loadRuntimeLogs(from: self.runtimeLogURL)
        // Drop the lines that fell out of the retained window at startup.
        if fileLines > logs.count { Self.writeRuntimeLogs(logs, to: self.runtimeLogURL) }
        self.runtimeLogs = logs
        self.runtimeLogFileLines = logs.count
        self.pendingRemoteExitURL = directory.appendingPathComponent("pending-remote-exits.json")
        self.pendingRemoteExits = Self.loadPendingRemoteExits(from: self.pendingRemoteExitURL)
        // The durable settlement ledger is also the retry latch for ordinary
        // strategy exits. Rebuilding only the record but not this set would
        // submit the same pending reduce-only order again after a restart.
        self.submittedExitPositionIDs = Set(self.pendingRemoteExits.keys)
        self.remoteReservationURL = directory.appendingPathComponent("remote-reservations.json")
        self.remoteReservations = Self.loadRemoteReservations(from: self.remoteReservationURL)
    }

    private static func loadPendingRemoteExits(from url: URL) -> [String: PendingRemoteExit] {
        guard let data = try? Data(contentsOf: url),
              let values = try? JSONDecoder().decode([String: PendingRemoteExit].self, from: data) else { return [:] }
        return values
    }

    private func savePendingRemoteExits() {
        guard let data = try? JSONEncoder().encode(pendingRemoteExits) else { return }
        do {
            try FileManager.default.createDirectory(at: pendingRemoteExitURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: pendingRemoteExitURL, options: .atomic)
        } catch { }
    }

    private static func loadRemoteReservations(from url: URL) -> [String: RemoteReservation] {
        guard let data = try? Data(contentsOf: url),
              let values = try? JSONDecoder().decode([String: RemoteReservation].self, from: data) else { return [:] }
        return values.mapValues { reservation in
            var carried = reservation
            carried.inFlight = false
            return carried
        }
    }

    /// Every entry is durable, including one whose submit command has not
    /// returned yet: a process that dies mid-submission must still leave the
    /// exposure and the strategy ownership behind.
    private func saveRemoteReservations() {
        guard let data = try? JSONEncoder().encode(remoteReservations) else { return }
        do {
            try FileManager.default.createDirectory(at: remoteReservationURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: remoteReservationURL, options: .atomic)
        } catch { }
    }

    private func recordSubmittedSignal(_ id: UUID) {
        guard !submittedSignals.contains(id) else { return }
        submittedSignals.insert(id)
        submittedSignalOrder.append(id)
        if submittedSignalOrder.count > Self.submittedSignalLimit {
            submittedSignals.remove(submittedSignalOrder.removeFirst())
        }
    }

    private func restoreRiskIfNeeded() async {
        guard !riskRestored else { return }
        let persisted = await paper.riskSnapshot()
        // A newly initialized store uses the empty snapshot as an absence
        // marker. Authenticated zero equity carries a day boundary and must
        // still be restored as a circuit breaker after a restart.
        if persisted != RiskSnapshot() { await riskEngine.restore(persisted) }
        // Recreate pools for persisted strategy instances before the first
        // capital/status request. Pool state itself is runtime-owned, while
        // the strategy allocation is persisted with the instance.
        for config in await paper.allStrategies() {
            _ = await riskEngine.registerStrategy(config.id, allocationPercent: Decimal(config.capitalPoolPercent))
        }
        riskRestored = true
    }

    public func health() -> ServiceHealth { ServiceHealth() }

    /// AI and the strategy laboratory copy finalized packages into this
    /// directory before calling the install endpoint.  Keeping the staging
    /// root below the paper state directory prevents an HTTP caller from
    /// importing arbitrary paths on the host.
    public nonisolated var strategyPackageStagingDirectory: URL {
        paper.stateDirectory.appendingPathComponent("strategy-staging", isDirectory: true)
    }

    public func strategyPackageManifests() async -> [StrategyPackageManifest] {
        (await strategyPackages.installed()).map(\.manifest)
    }

    private func stagedPackageURL(_ path: String) throws -> URL {
        let root = strategyPackageStagingDirectory.resolvingSymlinksInPath().standardizedFileURL
        let candidate: URL
        if path.hasPrefix("/") {
            candidate = URL(fileURLWithPath: path).resolvingSymlinksInPath().standardizedFileURL
        } else {
            candidate = root.appendingPathComponent(path).resolvingSymlinksInPath().standardizedFileURL
        }
        let prefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
        guard candidate.path.hasPrefix(prefix), candidate.path != root.path else {
            throw StrategyPackageError.invalidPackage("策略包路径必须位于 strategy-staging 目录内")
        }
        return candidate
    }

    public func installStrategyPackage(_ request: StrategyPackageInstallRequest) async throws -> StrategyPackageManifest {
        try FileManager.default.createDirectory(at: strategyPackageStagingDirectory, withIntermediateDirectories: true)
        let source = try stagedPackageURL(request.path)
        let installed = try await strategyPackages.install(package: source, replacing: request.replacing)
        appendLog("策略包已安装：\(installed.manifest.identifier) v\(installed.manifest.version)", level: "strategy")
        return installed.manifest
    }

    public func createStrategyFromPackage(identifier: String,
                                          request: StrategyPackageInstanceRequest = StrategyPackageInstanceRequest()) async throws -> StrategyConfig {
        guard let package = await strategyPackages.package(identifier: identifier) else {
            throw StrategyPackageError.notInstalled(StrategyPackageManifest.normalizePackageIdentifier(identifier))
        }
        let config = try package.manifest.makeDefaultConfiguration(name: request.name, scope: request.scope)
        return try await createStrategy(config)
    }

    public func uninstallStrategyPackage(identifier: String) async throws -> StrategyPackageManifest {
        guard let package = await strategyPackages.package(identifier: identifier) else {
            throw StrategyPackageError.notInstalled(StrategyPackageManifest.normalizePackageIdentifier(identifier))
        }
        let normalized = package.manifest.identifier
        let strategyType = package.manifest.executableStrategyType
        let configs = await paper.allStrategies()
        let matching = configs.filter { config in
            if let strategyType { return config.type == strategyType }
            return config.type.identifier == normalized
        }
        guard matching.allSatisfy({ !$0.enabled }) else {
            throw StrategyPackageError.activePackage(normalized)
        }
        let matchingIDs = Set(matching.map(\.id))
        let positions = await broker.allPositions()
        if !positions.isEmpty {
            let orders = await paper.allOrders()
            let hasOwnedPosition = positions.contains { position in
                guard abs(position.quantity) > 0 else { return false }
                return orders.contains { order in
                    matchingIDs.contains(order.strategyID) &&
                        order.instrumentID == position.instrumentID &&
                        order.side.lowercased() == position.side.lowercased()
                }
            }
            guard !hasOwnedPosition else { throw StrategyPackageError.activePackage(normalized) }
        }
        for id in matchingIDs {
            // A paused strategy may still have an entry waiting for the next
            // candle. Cancel it before deleting the package, otherwise the
            // orphaned order could open a new position after uninstall.
            await broker.cancelPendingOrders(strategyID: id)
        }
        let removed = try await strategyPackages.uninstall(identifier: normalized)
        // Paused instances have no live exposure at this point. Remove their
        // persisted definitions with the package so a later restart cannot
        // retain an orphan rule.
        for id in matchingIDs { _ = try? await deleteStrategy(id) }
        appendLog("策略包已卸载：\(normalized)", level: "strategy")
        return removed.manifest
    }

    private func requireStrategyMutationsAllowed() async throws {
        let snapshot = await riskEngine.snapshot()
        guard !snapshot.killSwitch else {
            throw ATKError.unavailable("账户风控已熔断，请在新日复位后再修改策略")
        }
    }

    /// Returns remote order ids which this service can attribute to a
    /// strategy. The exchange does not carry strategy metadata, so the
    /// persisted order ledger and the in-flight reservation ledger are the
    /// authoritative association points.
    private func strategyRemoteOrderIDs(_ strategyID: UUID) async -> Set<String> {
        var ids = Set((await paper.allOrders())
            .filter { $0.strategyID == strategyID }
            .compactMap(\.remoteOrderID))
        ids.formUnion(remoteReservations.compactMap { key, reservation in
            reservation.strategyID == strategyID && !key.hasPrefix("pending-") &&
                !Self.isUnresolvedReservation(key) ? key : nil
        })
        return ids
    }

    /// Cancels only known strategy entry orders. Reduce-only exits are never
    /// touched here; pausing a strategy must preserve its ability to protect
    /// an existing position while preventing a new entry from filling.
    private func cancelRemoteEntryOrders(for strategyID: UUID) async throws {
        // Pause/delete waits for commands that already crossed the submission
        // boundary, then cancels their authenticated order IDs. Releasing the
        // actor here allows those network commands to finish. Sleep instead
        // of yielding so a slow CLI call does not spin a CPU core.
        while strategyEntrySubmissionsInFlight.contains(strategyID) {
            try await Task.sleep(for: .milliseconds(50))
        }
        await broker.cancelPendingOrders(strategyID: strategyID)
        let orderIDs = await strategyRemoteOrderIDs(strategyID)
        guard !orderIDs.isEmpty else { return }
        await market.invalidateAccountState()
        let remoteOrders = try await market.privateOrders()
        let pending = remoteOrders.filter { orderIDs.contains($0.id) && !$0.isTerminal }
        guard !pending.isEmpty else { return }
        let account = try await market.account()
        guard account.mode != .readOnly else {
            throw ATKError.unavailable("当前账户为只读模式，无法撤销策略挂单")
        }
        let cancelRequestedAt = Date()
        await withRiskReservationMutation {
            var changed = false
            for order in pending {
                guard var reservation = remoteReservations[order.id], reservation.cancelRequestedAt == nil else { continue }
                reservation.cancelRequestedAt = cancelRequestedAt
                remoteReservations[order.id] = reservation
                changed = true
            }
            if changed { saveRemoteReservations() }
        }
        for order in pending {
            if account.mode == .paper {
                try await market.cancelDemoOrder(instrumentID: order.instrumentID, orderID: order.id)
            } else {
                try await market.cancelLiveOrder(instrumentID: order.instrumentID, orderID: order.id)
            }
            // Cancellation can race a partial fill. Keep the reservation
            // until authenticated order and position reads confirm release.
        }
        await market.invalidateAccountState()
        try await reconcileRemoteReservations()
        // A reservation may have been reconstructed from the paper ledger
        // during reconciliation (for example after a restart). Carry the
        // cancel intent onto that recovered record before the next snapshot.
        await withRiskReservationMutation {
            var changed = false
            for order in pending {
                guard var reservation = remoteReservations[order.id], reservation.cancelRequestedAt == nil else { continue }
                reservation.cancelRequestedAt = cancelRequestedAt
                remoteReservations[order.id] = reservation
                changed = true
            }
            if changed { saveRemoteReservations() }
        }
        await paper.setRisk(await riskEngine.snapshot())
    }

    /// Finds positions that can be attributed to a strategy from persisted
    /// entry orders. A position is only considered owned when both its
    /// instrument and direction match a non-terminal strategy order.
    private func strategyRemotePositions(_ strategyID: UUID) async throws -> [PositionSnapshot] {
        let orders = await paper.allOrders().filter {
            $0.strategyID == strategyID && !OrderLifecycle.isTerminal($0.status)
        }
        let reservationOrders = remoteReservations.values.filter { $0.strategyID == strategyID }
        let instruments = Set(orders.map(\.instrumentID) + reservationOrders.map(\.instrumentID))
        guard !instruments.isEmpty else { return [] }
        let positions = try await market.privatePositions()
        let directionsByInstrument = Dictionary(grouping: orders, by: \.instrumentID).mapValues { values in
            Set(values.map { $0.side.lowercased() })
        }
        return positions.filter { position in
            guard instruments.contains(position.instrumentID), abs(position.quantity) > 0 else { return false }
            let normalized = position.side.lowercased()
            let isShort = normalized == "short" || (normalized == "net" && position.quantity < 0)
            let expected = isShort ? "short" : "long"
            let directions = directionsByInstrument[position.instrumentID] ?? []
            return directions.isEmpty || directions.contains(expected)
        }
    }

    public func createStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        // Saving a paused strategy does not create an order or change the
        // account's exposure.  Keep configuration work available while the
        // daily-loss circuit is latched, but never let an enabled request
        // bypass that circuit.
        if config.enabled {
            try await requireStrategyMutationsAllowed()
        }
        let normalized = try await normalizedCapitalPoolConfig(config)
        let created = try await paper.create(normalized)
        _ = await riskEngine.registerStrategy(created.id, allocationPercent: Decimal(created.capitalPoolPercent))
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("策略已创建：\(created.name)，规则 \(created.type.displayName)")
        return created
    }

    public func updateStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        // Pausing or editing a paused strategy is a non-trading operation and
        // remains available during a kill switch. Enabling a strategy still
        // requires an unfaulted risk state.
        if config.enabled {
            try await requireStrategyMutationsAllowed()
        }
        let normalized = try await normalizedCapitalPoolConfig(config, excluding: config.id)
        let updated = try await paper.update(normalized)
        _ = await riskEngine.updateStrategyAllocation(updated.id, allocationPercent: Decimal(updated.capitalPoolPercent))
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("策略已更新：\(updated.name)")
        return updated
    }

    /// Clamp a requested strategy pool to the currently available USDT
    /// allocation before persisting it. This keeps the saved configuration in
    /// sync with the runtime pool when other strategies already occupy part of
    /// the USDT base and rejects a known zero-balance account explicitly.
    private func normalizedCapitalPoolConfig(_ config: StrategyConfig, excluding strategyID: UUID? = nil) async throws -> StrategyConfig {
        guard config.capitalPoolPercent.isFinite,
              config.capitalPoolPercent > 0,
              config.capitalPoolPercent <= 100 else {
            // Preserve the store's existing validation contract for malformed
            // user input; allocation clamping only applies to valid requests.
            throw PaperTradingStore.StoreError.unsupported
        }
        var snapshot = await riskEngine.snapshot()
        if snapshot.strategyCapitalBase == nil {
            // Strategy creation is a capital allocation decision. Establish an
            // authenticated USDT base before persisting it instead of using
            // the RiskEngine's standalone paper fallback equity.
            _ = try await account()
            snapshot = await riskEngine.snapshot()
        }
        guard let totalCapital = snapshot.strategyCapitalBase else {
            throw ATKError.unavailable("账户 USDT 资产尚未同步，请先刷新账户")
        }
        guard totalCapital.isFinite, totalCapital > 0 else {
            throw ATKError.unavailable("USDT 资产为 0，无法分配策略资金池")
        }
        let existing = snapshot.strategyCapitals.filter { $0.strategyID != strategyID }
        let allocation = StrategyCapitalAllocation(totalCapital: totalCapital, strategyCapitals: existing)
        let effective = allocation.effectiveAllocationPercent(for: Decimal(config.capitalPoolPercent))
        // The store canonicalizes a positive allocation to at least 0.1%.
        // Refuse a smaller remainder here instead of silently rounding it up
        // and allowing the persisted pools to exceed the account allocation.
        guard effective >= 0.1 else {
            throw ATKError.unavailable("USDT 资产已被其他策略占用，无法分配策略资金池")
        }
        var normalized = config
        normalized.capitalPoolPercent = NSDecimalNumber(decimal: effective).doubleValue
        return normalized
    }

    public func deleteStrategy(_ id: UUID) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        guard let config = await paper.allStrategies().first(where: { $0.id == id }) else {
            throw PaperTradingStore.StoreError.notFound
        }
        guard !config.enabled else { throw PaperTradingStore.StoreError.running }
        strategyEntryClosureEpochs[id, default: 0] &+= 1
        strategyEntryClosures.insert(id)
        defer { strategyEntryClosures.remove(id) }
        // Remove pending entries first, then verify that no attributed remote
        // position remains. Deletion is refused while exposure exists so the
        // service cannot lose ownership of a live position.
        try await cancelRemoteEntryOrders(for: id)
        // An entry whose submit outcome is still unknown may own a position
        // that has not been reported yet.
        await resolveUnconfirmedSubmissions()
        // The resolver cancels an entry that turns out to be resting and keeps
        // it unresolved until the exchange confirms the cancel, so a live
        // entry can never pass this guard.
        guard !remoteReservations.contains(where: { Self.isUnresolvedReservation($0.key) && $0.value.strategyID == id }) else {
            throw ATKError.unavailable("策略仍有结果未知或待撤销的下单，请等待后台按 clOrdId 核对完成后再删除")
        }
        // Final sweep, matching the pause path: it catches an entry the
        // resolver re-keyed during the pass above.
        try await cancelRemoteEntryOrders(for: id)
        let positions = try await strategyRemotePositions(id)
        guard positions.isEmpty else {
            throw ATKError.unavailable("策略仍有远端持仓，请先平仓并确认持仓归零后再删除")
        }
        let removed = try await paper.delete(id)
        await riskEngine.removeStrategy(id)
        // Persist the pool removal so a later service restart cannot recreate
        // an orphaned capital card from the previous risk snapshot.
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("策略已删除：\(removed.name)")
        return removed
    }

    public func startStrategy(_ id: UUID) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        let riskSnapshot = await riskEngine.snapshot()
        guard !riskSnapshot.killSwitch else {
            appendLog("策略启动被拒绝：账户风控已熔断，需新日手动复位后才能启动", level: "warning")
            throw ATKError.unavailable("账户风控已熔断，请在新日复位后再启动策略")
        }
        let config = try await paper.setState(id, running: true)
        _ = await riskEngine.registerStrategy(config.id, allocationPercent: Decimal(config.capitalPoolPercent))
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("策略已启动：\(config.name)")
        return config
    }

    public func pauseStrategy(_ id: UUID) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        let config = try await paper.setState(id, running: false)
        strategyEntryClosureEpochs[id, default: 0] &+= 1
        strategyEntryClosures.insert(id)
        defer { strategyEntryClosures.remove(id) }
        // The cancel sweep below only sees order ids the service already
        // knows. An entry still unresolved during it is cancelled by the
        // resolver, and a concurrent reconciliation can re-key an entry to a
        // live order id after that sweep has looked; the final sweep after
        // the resolver is the serialized confirmation that no resting entry
        // of this strategy is left.
        try await cancelRemoteEntryOrders(for: id)
        // Resolve first, so an entry that turns out to be resting is settled
        // (or cancelled) before the final sweep.
        await resolveUnconfirmedSubmissions()
        // Final sweep: catches anything the resolver re-keyed during the pass
        // above, and any entry that only became a known order id in between.
        try await cancelRemoteEntryOrders(for: id)
        // The resolver may have left an entry unresolved on purpose; make one
        // more attempt so a pause does not return with a live entry order.
        await resolveUnconfirmedSubmissions()
        appendLog("策略已停止：\(config.name)")
        return config
    }

    public func strategyCapital() async -> [StrategyCapitalSnapshot] {
        await restoreRiskIfNeeded()
        return await riskEngine.strategyCapitals()
    }

    public func resetRisk() async -> RiskSnapshot {
        await restoreRiskIfNeeded()
        let snapshot = await riskEngine.resetKillSwitch()
        if !snapshot.killSwitch {
            globalRiskTripHandled = false
            globalRiskRemoteCleanupComplete = false
        }
        await paper.setRisk(snapshot)
        appendLog(snapshot.killSwitch ? "账户风控仍锁存：只能在 UTC 新日历日手动复位" : "账户风控已手动复位", level: snapshot.killSwitch ? "warning" : "risk")
        return snapshot
    }

    public func recordLog(_ log: RuntimeLog) {
        appendLog(log)
    }

    private func logSignals(_ statuses: [StrategyStatus], configs: [StrategyConfig], instrumentID: String) {
        for status in statuses {
            guard let signal = status.lastSignal, loggedSignalIDs.insert(signal.id).inserted,
                  let config = configs.first(where: { $0.id == status.id }) else { continue }
            appendLog("策略信号：\(config.name) / \(instrumentID) / \(signal.type) / \(signal.reason)", level: "signal")
        }
    }

    private func logNewFills() async {
        for fill in await broker.allFills() where loggedFillIDs.insert(fill.id).inserted {
            appendLog("成交：订单 \(fill.orderID.uuidString.prefix(8))，数量 \(fill.quantity)，价格 \(fill.price)，手续费 \(fill.fee)", level: "fill")
        }
    }

    /// Executes the account-level circuit breaker exactly once per backend
    /// lifetime. Strategy rules remain responsible for ordinary exits; this
    /// path is reserved for the daily mark-to-market loss limit.
    private func enforceGlobalRiskIfNeeded(now: Date = .now) async {
        let snapshot = await riskEngine.snapshot(now: now)
        guard snapshot.killSwitch else { return }

        // Local strategy stop/flatten is idempotent and only needs to be
        // announced once. Remote cleanup has its own latch below because a
        // transient REST/CLI failure must be retried on the next heartbeat.
        if !globalRiskTripHandled {
            globalRiskTripHandled = true
            let stopped = await paper.stopAllStrategies()
            appendLog("账户日损熔断：当日亏损 \(snapshot.dailyPnLPercent)%，已停止 \(stopped.count) 个策略", level: "risk")
            await broker.cancelPendingOrders()
            let localClosed = await broker.flattenAll(at: now)
            if !localClosed.isEmpty {
                appendLog("账户日损熔断：本地模拟持仓已平仓 \(localClosed.count) 个", level: "risk")
            }
        }
        guard !globalRiskRemoteCleanupComplete else { return }
        guard !globalRiskRemoteCleanupInFlight else { return }
        globalRiskRemoteCleanupInFlight = true
        defer { globalRiskRemoteCleanupInFlight = false }

        guard let account = try? await market.account() else {
            appendLog("账户日损熔断：无法读取交易账户，远端处置将在下次心跳重试", level: "warning")
            await paper.setRisk(snapshot)
            return
        }
        guard account.mode != .readOnly else {
            // A read-only profile cannot have remotely actionable orders.
            globalRiskRemoteCleanupComplete = true
            await paper.setRisk(snapshot)
            return
        }

        var cleanupSucceeded = true
        do {
            let orders = try await market.privateOrders()
            for order in orders where !order.isTerminal {
                do {
                    if account.mode == .paper {
                        try await market.cancelDemoOrder(instrumentID: order.instrumentID, orderID: order.id)
                    } else {
                        try await market.cancelLiveOrder(instrumentID: order.instrumentID, orderID: order.id)
                    }
                    appendLog("账户日损熔断：已撤销开仓挂单 \(order.id)", level: "risk")
                } catch {
                    cleanupSucceeded = false
                    appendLog("账户日损熔断：撤单失败 \(order.id)：\(error.localizedDescription)", level: "warning")
                }
            }
        } catch {
            cleanupSucceeded = false
            appendLog("账户日损熔断：读取远端挂单失败，下一次心跳重试：\(error.localizedDescription)", level: "warning")
        }

        do {
            let positions = try await market.privatePositions()
            for position in positions where abs(position.quantity) > 0 {
                do {
                    if account.mode == .paper {
                        try await market.closeDemoPosition(instrumentID: position.instrumentID, positionSide: position.side, marginMode: position.marginMode)
                    } else {
                        try await market.closeLivePosition(instrumentID: position.instrumentID, positionSide: position.side, marginMode: position.marginMode)
                    }
                    appendLog("账户日损熔断：已提交平仓 \(position.instrumentID) \(position.side)", level: "risk")
                } catch {
                    cleanupSucceeded = false
                    appendLog("账户日损熔断：平仓失败 \(position.instrumentID)：\(error.localizedDescription)", level: "warning")
                }
            }
        } catch {
            cleanupSucceeded = false
            appendLog("账户日损熔断：读取远端持仓失败，下一次心跳重试：\(error.localizedDescription)", level: "warning")
        }

        // Exchange close/cancel acceptance is asynchronous. Keep the cleanup
        // retryable until a fresh authenticated snapshot actually shows flat
        // positions and no outstanding orders.
        await market.invalidateAccountState()
        do {
            let remainingOrders = try await market.privateOrders()
            let remainingPositions = try await market.privatePositions()
            if remainingOrders.contains(where: { !$0.isTerminal }) ||
                remainingPositions.contains(where: { abs($0.quantity) > 0 }) {
                cleanupSucceeded = false
            }
        } catch {
            cleanupSucceeded = false
            appendLog("账户风控熔断：远端处置尚未确认，下次心跳继续核对", level: "warning")
        }
        globalRiskRemoteCleanupComplete = cleanupSucceeded
        await paper.setRisk(await riskEngine.snapshot(now: now))
    }

    /// Strategy-level protection is evaluated independently of the account
    /// circuit breaker. Entry signals attach exchange-native SL/TP orders; this
    /// monitor supplies the time exit and retries protection when a remote
    /// position is visible without an attached exit.
    private func enforceStrategyExits(at timestamp: Date, fallbackPrice: Decimal? = nil, candle: Candle? = nil, candleInstrumentID: String? = nil) async {
        guard !globalRiskTripHandled, !strategyExitMonitorInFlight else { return }
        strategyExitMonitorInFlight = true
        defer { strategyExitMonitorInFlight = false }
        let configs = await paper.allStrategies()
        guard !configs.isEmpty, let positions = try? await market.privatePositions() else { return }
        await settleCompletedRemoteExits(positions: positions, timestamp: timestamp)
        guard !positions.isEmpty else { return }
        let orders = await paper.allOrders()
        // The exit state machine keeps the remote order id so a canceled or
        // rejected reduce-only leg can be released for retry. If this read
        // fails, keep the pending claim until quantity confirms the fill or
        // an authenticated order snapshot confirms failure.
        let remoteOrders = try? await market.privateOrders()
        guard let account = try? await market.account(), account.mode != .readOnly else { return }
        for position in positions where abs(position.quantity) > 0 {
            let normalizedPositionSide = position.side.lowercased()
            let positionIsShort = normalizedPositionSide == "short" || (normalizedPositionSide == "net" && position.quantity < 0)
            guard let order = orders.reversed().first(where: { order in
                order.instrumentID == position.instrumentID &&
                    ["submitted", "filled", "pending"].contains(order.status.lowercased()) &&
                    (positionIsShort ? order.side.lowercased() == "short" : order.side.lowercased() == "long")
            }),
                  let config = configs.first(where: { $0.id == order.strategyID }) else { continue }
            let status = await paper.status(for: config.id, instrumentID: position.instrumentID)
            let signal = order.signal ?? status?.lastSignal
            let positionFallback = candleInstrumentID == position.instrumentID ? fallbackPrice : nil
            let price = position.markPrice ?? positionFallback ?? order.fillPrice
            guard let price else { continue }
            // OKX net-mode rows use a signed `pos` value and report side as
            // "net". Infer direction from quantity before applying SL/TP.
            let isShort = positionIsShort
            let stopHit: Bool
            let takeHit: Bool
            if isShort {
                stopHit = signal?.stopPrice.map { price >= $0 } ?? false
                takeHit = signal?.takePrice.map { price <= $0 } ?? false
            } else {
                stopHit = signal?.stopPrice.map { price <= $0 } ?? false
                takeHit = signal?.takePrice.map { price >= $0 } ?? false
            }
            let maxHold = Self.strategyMaxHoldSeconds(config)
            let timedOut = timestamp.timeIntervalSince(order.requestedAt) >= maxHold
            guard stopHit || takeHit || timedOut else { continue }
            let positionKey = "\(position.id):\(position.instrumentID)"
            if let pending = pendingRemoteExits[positionKey],
               let remoteOrderID = pending.remoteOrderID,
               let remoteOrder = remoteOrders?.first(where: { $0.id == remoteOrderID }),
               remoteOrder.isTerminal {
                // A reduce-only order may be terminal while a partial
                // remainder is still visible. Release the retry latch so the
                // next pass can submit only the current residual quantity.
                pendingRemoteExits.removeValue(forKey: positionKey)
                submittedExitPositionIDs.remove(positionKey)
                savePendingRemoteExits()
            }
            guard submittedExitPositionIDs.insert(positionKey).inserted else { continue }
            let side = isShort ? "buy" : "sell"
            let request = LiveOrderRequest(instrumentID: position.instrumentID, side: side, orderType: "market", quantity: abs(position.quantity), marginMode: position.marginMode ?? "cross", reduceOnly: true)
            do {
                let spec = try await market.instrumentSpec(instrumentID: position.instrumentID)
                guard spec.isLiveUSDTLinearSwap,
                      spec.accepts(contractQuantity: abs(position.quantity)) else {
                    throw ATKError.invalidOrder("远端持仓数量不符合 live USDT linear swap 合约规格")
                }
                let result: LiveOrderCommandResult
                if account.mode == .paper {
                    result = try await market.placeDemoOrder(request)
                } else {
                    result = try await market.placeLiveOrder(request)
                }
                // Keep the pool reservation until settleCompletedRemoteExits
                // confirms that the exchange position has disappeared.
                let reservedNotional = abs(spec.notional(forContracts: position.quantity, price: position.entryPrice))
                let exitRisk = signal?.stopPrice.map {
                    abs(position.entryPrice - $0) * abs(position.quantity) * spec.contractValue
                } ?? 0
                pendingRemoteExits[positionKey] = PendingRemoteExit(
                    strategyID: config.id,
                    entryOrderID: order.remoteOrderID,
                    positionID: position.id,
                    instrumentID: position.instrumentID,
                    quantity: abs(position.quantity),
                    entryPrice: position.entryPrice,
                    exitPrice: price,
                    side: isShort ? "short" : "long",
                    contractValue: spec.contractValue,
                    reservedNotional: reservedNotional,
                    exitRisk: exitRisk,
                    remoteOrderID: result.orderID
                )
                savePendingRemoteExits()
                appendLog("策略平仓已提交，等待成交确认：\(config.name) / \(position.instrumentID) / \(result.orderID) / \(stopHit ? "止损" : takeHit ? "止盈" : "持仓超时离场")", level: "exit")
                await market.invalidateAccountState()
            } catch {
                submittedExitPositionIDs.remove(positionKey)
                appendLog("策略平仓失败：\(config.name) / \(position.instrumentID)：\(error.localizedDescription)", level: "warning")
            }
        }
    }

    /// Time exit measured from the entry order's signal bar. Mirrors each
    /// lab's `max_hold_bars` on its entry timeframe: double pump holds at
    /// most 24 × 15m after the signal bar closes, the 1h sweep rule 96 hours.
    static func strategyMaxHoldSeconds(_ config: StrategyConfig) -> TimeInterval {
        switch config.type {
        case .doublePumpExhaustionShort:
            let bars = max(1, config.parameters["maxHoldBars"] ?? 24)
            return (bars + 1) * 15 * 60
        default:
            return 96 * 3600
        }
    }

    /// Reduce-only exits are asynchronous. Keep the strategy pool reserved
    /// until a later position snapshot confirms that the exit completed.
    private func settleCompletedRemoteExits(positions: [PositionSnapshot], timestamp: Date) async {
        // A native stop/take-profit can win the race with a service-submitted
        // reduce-only exit. Fetch the exchange's closed-position result before
        // mutating the pending record so that the final settlement uses the
        // actual realized PnL when that race occurs.
        var closedHistoryByInstrument: [String: [ClosedSwapPositionSnapshot]] = [:]
        let pendingSnapshot = pendingRemoteExits.values
        let historyInstruments = Set(pendingSnapshot.compactMap { pending -> String? in
            guard !positions.contains(where: { $0.id == pending.positionID && abs($0.quantity) > 0 }) else { return nil }
            return pending.instrumentID
        })
        for instrumentID in historyInstruments {
            if let history = try? await market.closedSwapPositions(instrumentID: instrumentID) {
                closedHistoryByInstrument[instrumentID] = history
            }
        }
        await withRiskReservationMutation {
            // Claim completed exits before the first RiskEngine await. The
            // mutation gate keeps the claim and release atomic with generic
            // reconciliation and new order authorization.
            var completed: [(String, PendingRemoteExit, Decimal, Decimal, Decimal)] = []
            for (key, pending) in pendingRemoteExits {
                let hasOpenOriginalPosition = positions.contains {
                    $0.id == pending.positionID &&
                        $0.instrumentID == pending.instrumentID &&
                        abs($0.quantity) > 0
                }
                guard !hasOpenOriginalPosition else { continue }
                pendingRemoteExits.removeValue(forKey: key)
                submittedExitPositionIDs.remove(key)
                let reservationsToRelease = remoteReservations.compactMap { reservationKey, reservation -> RemoteReservation? in
                    reservation.strategyID == pending.strategyID &&
                        reservation.instrumentID == pending.instrumentID &&
                        (pending.entryOrderID == nil || reservationKey == pending.entryOrderID) &&
                        !reservation.inFlight ? reservation : nil
                }
                let reservedNotional = reservationsToRelease.map(\.notional).reduce(0, +)
                let reservedMargin = reservationsToRelease.map(\.margin).reduce(0, +)
                let reservedRisk = reservationsToRelease.map(\.riskAmount).reduce(0, +)
                let reservationKeys = remoteReservations.compactMap { reservationKey, reservation in
                    reservation.strategyID == pending.strategyID &&
                        reservation.instrumentID == pending.instrumentID &&
                        (pending.entryOrderID == nil || reservationKey == pending.entryOrderID) &&
                        !reservation.inFlight ? reservationKey : nil
                }
                // An entry whose order id never resolved is closed through
                // its local ledger id instead.
                let unresolvedEntryIDs = reservationKeys.compactMap { remoteReservations[$0]?.localOrderID }
                for reservationKey in reservationKeys { remoteReservations.removeValue(forKey: reservationKey) }
                if !reservationKeys.isEmpty { saveRemoteReservations() }
                let completedEntryIDs = Set(reservationKeys + [pending.entryOrderID].compactMap { $0 })
                for entryID in completedEntryIDs {
                    await paper.markRemoteOrderTerminal(entryID)
                }
                for localOrderID in unresolvedEntryIDs {
                    await paper.markLocalOrderTerminal(localOrderID, status: "closed")
                }
                completed.append((key, pending,
                                  reservedNotional > 0 ? reservedNotional : pending.reservedNotional,
                                  reservedMargin > 0 ? reservedMargin : pending.reservedNotional,
                                  reservedRisk > 0 ? reservedRisk : pending.exitRisk))
            }
            if !completed.isEmpty {
                savePendingRemoteExits()
            }
            for (_, pending, reservedNotional, reservedMargin, reservedRisk) in completed {
                let direction: Decimal = pending.side == "short" ? -1 : 1
                let fallbackRealized = (pending.exitPrice - pending.entryPrice) * pending.quantity * pending.contractValue * direction
                    - abs(pending.exitPrice * pending.quantity * pending.contractValue) * broker.feeRate
                let historyRealized = closedHistoryByInstrument[pending.instrumentID]?
                    .first(where: { $0.positionID == pending.positionID })?.realizedPnL
                let realized = historyRealized ?? pending.realizedPnL ?? fallbackRealized
                await riskEngine.recordStrategyRealized(realized, strategyID: pending.strategyID, now: timestamp)
                await riskEngine.release(instrumentID: pending.instrumentID, notional: reservedNotional, strategyID: pending.strategyID, margin: reservedMargin, riskAmount: reservedRisk, closedPosition: true)
                // Both 15m short rules define their cooldown from the exit,
                // not from the signal (double pump STRATEGY.md §5.6).
                if let config = await paper.allStrategies().first(where: { $0.id == pending.strategyID }),
                   config.type == .doublePumpExhaustionShort {
                    await paper.setCooldown(strategyID: pending.strategyID, instrumentID: pending.instrumentID, bars: config.type.defaultCooldownBars)
                }
                appendLog("策略平仓已成交并结算：\(pending.instrumentID)，已实现盈亏 \(realized)", level: "fill")
            }
            await paper.setRisk(await riskEngine.snapshot(now: timestamp))
        }
    }

    /// Reconciles global risk reservations made by the manual order APIs.
    /// Strategy reservations are settled by their strategy-specific exit
    /// path. If the exchange no longer reports an order and no position for
    /// its instrument, the reservation is safe to release; otherwise it is
    /// retained conservatively until the position disappears.
    private func reconcileRemoteReservations() async throws {
        // Settle deferred submissions first so the steps below see them under
        // their exchange order id (or not at all).
        await resolveUnconfirmedSubmissions()
        // Serialize the complete read/merge transaction logically. Actor
        // reentrancy still permits the network reads to overlap, so a slower,
        // older snapshot is discarded using this generation token.
        reconciliationGeneration &+= 1
        let generation = reconciliationGeneration
        // An incomplete authenticated snapshot cannot authorize new exposure.
        // Propagate read failures instead of silently proceeding from an empty
        // or stale local ledger.
        let orders = try await market.privateOrders()
        let positions = try await market.privatePositions()
        guard generation == reconciliationGeneration else { return }
        // Recreate confirmed strategy/manual reservations from the durable
        // paper ledger when the process was restarted between remote
        // acceptance and the in-memory reservation update.
        var recoveredReservations: [String: RemoteReservation] = [:]
        let strategyIDs = Set((await paper.allStrategies()).map(\.id))
        // An entry whose claim is still keyed by its client order id is
        // already reserved; recreating it under the order id would count the
        // same order twice.
        let unresolvedClientOrderIDs = Set(remoteReservations.compactMap { key, reservation in
            Self.isUnresolvedReservation(key) ? reservation.clientOrderID : nil
        })
        for paperOrder in await paper.allOrders() {
            guard let remoteOrderID = paperOrder.remoteOrderID,
                  remoteReservations[remoteOrderID] == nil,
                  !(paperOrder.clientOrderID.map(unresolvedClientOrderIDs.contains) ?? false),
                  !OrderLifecycle.isTerminal(paperOrder.status) else { continue }
            let remoteOrder = orders.first(where: { $0.id == remoteOrderID })
            let expectedSide = paperOrder.side.lowercased()
            let position = positions.first(where: {
                guard $0.instrumentID == paperOrder.instrumentID, abs($0.quantity) > 0 else { return false }
                let side = $0.side.lowercased()
                let isShort = side == "short" || (side == "net" && $0.quantity < 0)
                return (isShort ? "short" : "long") == expectedSide
            })
            let spec = try await market.instrumentSpec(instrumentID: paperOrder.instrumentID)
            guard spec.isLiveUSDTLinearSwap else { continue }
            let price = remoteOrder?.price.flatMap { $0 > 0 ? $0 : nil }
                ?? position?.entryPrice
                ?? paperOrder.fillPrice
                ?? paperOrder.signal?.price
            guard let price, price.isFinite, price > 0 else { continue }
            let notional = abs(spec.notional(forContracts: paperOrder.quantity, price: price))
            guard notional.isFinite, notional > 0 else { continue }
            // Ownership comes from the durable instance registry. Manual order
            // APIs use a fresh UUID which is not present in this set.
            let strategyID = strategyIDs.contains(paperOrder.strategyID) ? paperOrder.strategyID : nil
            let riskAmount: Decimal
            if let stop = paperOrder.signal?.stopPrice,
               stop.isFinite, stop > 0 {
                // A market entry can fill far from the signal candle. Risk
                // recovery must use the authenticated fill/position price so
                // a restart cannot understate the open stop risk and bypass
                // the strategy cap. The signal price is only a last resort
                // for an order that has not exposed a remote fill yet.
                let entryPrice = position?.entryPrice ?? remoteOrder?.price ?? paperOrder.signal?.price
                if let entryPrice, entryPrice.isFinite, entryPrice > 0 {
                    riskAmount = abs(entryPrice - stop) * paperOrder.quantity * spec.contractValue
                } else {
                    riskAmount = 0
                }
            } else {
                riskAmount = 0
            }
            recoveredReservations[remoteOrderID] = RemoteReservation(
                instrumentID: paperOrder.instrumentID,
                notional: notional,
                createdAt: paperOrder.requestedAt,
                strategyID: strategyID,
                margin: notional,
                riskAmount: riskAmount,
                closedPosition: strategyID != nil,
                inFlight: false,
                positionID: position?.id
            )
        }
        // Rebuild the global exposure baseline from the authenticated remote
        // view. This also repairs state after a process restart, when the
        // in-memory reservation dictionary no longer exists.
        var exposures: [String: Decimal] = [:]
        for position in positions where abs(position.quantity) > 0 {
            let spec = try await market.instrumentSpec(instrumentID: position.instrumentID)
            guard spec.isLiveUSDTLinearSwap else {
                throw ATKError.invalidOrder("远端持仓不是可交易的 live USDT linear swap：\(position.instrumentID)")
            }
            let notional = abs(spec.notional(forContracts: position.quantity, price: position.entryPrice))
            guard notional.isFinite, notional > 0 else { continue }
            exposures[position.instrumentID, default: 0] += notional
        }
        for order in orders where !OrderLifecycle.isTerminal(order.status) {
            let notional: Decimal?
            if let reserved = (remoteReservations[order.id] ?? recoveredReservations[order.id])?.notional, reserved.isFinite, reserved > 0 {
                notional = reserved
            } else if let price = order.price, price.isFinite, price > 0 {
                let spec = try await market.instrumentSpec(instrumentID: order.instrumentID)
                guard spec.isLiveUSDTLinearSwap else {
                    throw ATKError.invalidOrder("远端挂单不是可交易的 live USDT linear swap：\(order.instrumentID)")
                }
                notional = abs(spec.notional(forContracts: order.quantity, price: price))
            } else {
                // OKX market orders expose px=0/nil. Use a fresh ticker to
                // conservatively account for externally submitted exposure
                // instead of silently dropping it from the global cap.
                let spec = try await market.instrumentSpec(instrumentID: order.instrumentID)
                let ticker = try await market.ticker(instrumentID: order.instrumentID)
                guard spec.isLiveUSDTLinearSwap, ticker.last.isFinite, ticker.last > 0 else {
                    throw ATKError.invalidOrder("远端市价挂单缺少可用于风控的合约规格或价格")
                }
                notional = abs(spec.notional(forContracts: order.quantity, price: ticker.last))
            }
            guard let notional, notional.isFinite, notional > 0 else { continue }
            exposures[order.instrumentID, default: 0] += notional
        }
        // The remote reads and spec lookups above may take time. Claim and
        // mutate the local ledger only after them, under the short gate. This
        // keeps a concurrent authorization/release from being lost, and the
        // second generation check prevents an older snapshot from winning.
        guard generation == reconciliationGeneration else { return }
        // Closed-position history is the only authenticated source that can
        // attribute native SL/TP realization to a strategy entry. Fetch it
        // before taking the short mutation gate; an unavailable history read
        // keeps a filled/no-position reservation conservatively reserved.
        var closedHistoryByInstrument: [String: [ClosedSwapPositionSnapshot]] = [:]
        let historyInstruments = Set((Array(remoteReservations) + Array(recoveredReservations))
            .compactMap { orderID, reservation -> String? in
                guard reservation.strategyID != nil, let positionID = reservation.positionID,
                      !positions.contains(where: { $0.id == positionID && $0.instrumentID == reservation.instrumentID && abs($0.quantity) > 0 }) else { return nil }
                if let order = orders.first(where: { $0.id == orderID }), order.endedUnfilled {
                    return nil
                }
                return reservation.instrumentID
            })
        for instrumentID in historyInstruments {
            if let history = try? await market.closedSwapPositions(instrumentID: instrumentID) {
                closedHistoryByInstrument[instrumentID] = history
            }
        }
        await withRiskReservationMutation {
            guard generation == reconciliationGeneration else { return }
            for (orderID, reservation) in recoveredReservations where remoteReservations[orderID] == nil {
                remoteReservations[orderID] = reservation
            }
            if !recoveredReservations.isEmpty { saveRemoteReservations() }
            // Attach the current position identity to reservations recovered
            // before the first position snapshot. This makes later native
            // close settlement exact even when another position uses the same
            // instrument.
            let allOrders = await paper.allOrders()
            let paperOrdersByRemoteID = Dictionary(uniqueKeysWithValues: allOrders.compactMap { order in
                order.remoteOrderID.map { ($0, order) }
            })
            let ordersByLocalID = Dictionary(uniqueKeysWithValues: allOrders.map { ($0.id, $0) })
            for orderID in remoteReservations.keys {
                guard var reservation = remoteReservations[orderID], reservation.positionID == nil else { continue }
                // An entry whose outcome is still unknown is settled by its
                // clOrdId lookup, so its local record is not keyed by order id.
                let paperOrder = paperOrdersByRemoteID[orderID]
                    ?? reservation.localOrderID.flatMap { ordersByLocalID[$0] }
                guard let paperOrder else { continue }
                let expectedSide = paperOrder.side.lowercased()
                if let position = positions.first(where: {
                    guard $0.instrumentID == reservation.instrumentID, abs($0.quantity) > 0 else { return false }
                    let side = $0.side.lowercased()
                    let isShort = side == "short" || (side == "net" && $0.quantity < 0)
                    return (isShort ? "short" : "long") == expectedSide
                }) {
                    reservation.positionID = position.id
                    remoteReservations[orderID] = reservation
                }
            }
            var releases: [(RemoteReservation, Decimal?)] = []
            for (orderID, reservation) in remoteReservations {
                // Only a clOrdId lookup may settle an unresolved submission.
                guard !reservation.inFlight, !Self.isUnresolvedReservation(orderID) else { continue }
                if let strategyID = reservation.strategyID,
                   pendingRemoteExits.values.contains(where: { $0.strategyID == strategyID && $0.instrumentID == reservation.instrumentID }) {
                    continue
                }
                let order = orders.first(where: { $0.id == orderID })
                let hasPosition = positions.contains {
                    guard $0.instrumentID == reservation.instrumentID, abs($0.quantity) > 0 else { return false }
                    if let positionID = reservation.positionID { return $0.id == positionID }
                    return true
                }
                let shouldRelease: Bool
                var realizedPnL: Decimal?
                if let order {
                    if OrderLifecycle.isTerminal(order.status) && !hasPosition {
                        if !order.endedUnfilled {
                            let cancellationSettled = reservation.cancelRequestedAt.map {
                                Date().timeIntervalSince($0) >= Self.cancelSettlementGrace
                            } ?? false
                            if let positionID = reservation.positionID,
                               let history = closedHistoryByInstrument[reservation.instrumentID],
                               let closed = history.first(where: { $0.positionID == positionID }) {
                                realizedPnL = closed.realizedPnL
                            } else if !cancellationSettled {
                                terminalRemoteOrderObservations.removeValue(forKey: orderID)
                                continue
                            }
                        }
                        let observations = (terminalRemoteOrderObservations[orderID] ?? 0) + 1
                        terminalRemoteOrderObservations[orderID] = observations
                        // OKX can publish a terminal order before the matching
                        // position snapshot. Require two consecutive
                        // authenticated observations before releasing it.
                        shouldRelease = observations >= 2
                    } else {
                        terminalRemoteOrderObservations.removeValue(forKey: orderID)
                        shouldRelease = false
                    }
                } else {
                    terminalRemoteOrderObservations.removeValue(forKey: orderID)
                    let cancellationSettled = reservation.cancelRequestedAt.map {
                        Date().timeIntervalSince($0) >= Self.cancelSettlementGrace
                    } ?? false
                    if reservation.strategyID != nil && !cancellationSettled {
                        // `swap orders` defaults to open orders, so a filled
                        // entry commonly disappears from this response. Do
                        // not release a strategy reservation in that case
                        // until closed-position history confirms its stable
                        // position ID and realized result.
                        if let positionID = reservation.positionID,
                           let history = closedHistoryByInstrument[reservation.instrumentID],
                           let closed = history.first(where: { $0.positionID == positionID }) {
                            realizedPnL = closed.realizedPnL
                        } else {
                            continue
                        }
                    } else {
                        if reservation.strategyID != nil,
                           let positionID = reservation.positionID,
                           let history = closedHistoryByInstrument[reservation.instrumentID],
                           let closed = history.first(where: { $0.positionID == positionID }) {
                            realizedPnL = closed.realizedPnL
                        } else {
                            realizedPnL = nil
                        }
                    }
                    shouldRelease = !hasPosition && Date().timeIntervalSince(reservation.createdAt) >= 30
                }
                guard shouldRelease else { continue }
                remoteReservations.removeValue(forKey: orderID)
                terminalRemoteOrderObservations.removeValue(forKey: orderID)
                saveRemoteReservations()
                // Persist the terminal claim before yielding to the risk
                // engine. Recovery must never resurrect this entry and
                // release another position's pool reservation a second time.
                await paper.markRemoteOrderTerminal(orderID)
                releases.append((reservation, realizedPnL))
            }
            var strategyConfigs: [StrategyConfig] = []
            if !releases.isEmpty { strategyConfigs = await paper.allStrategies() }
            for (reservation, realizedPnL) in releases {
                await riskEngine.release(
                    instrumentID: reservation.instrumentID,
                    notional: reservation.notional,
                    strategyID: reservation.strategyID,
                    margin: reservation.margin,
                    riskAmount: reservation.riskAmount,
                    closedPosition: reservation.closedPosition
                )
                if let strategyID = reservation.strategyID, let realizedPnL {
                    await riskEngine.recordStrategyRealized(realizedPnL, strategyID: strategyID)
                    // A native SL/TP close is still an exit; apply the same
                    // post-exit cooldown as a service-submitted exit.
                    if let config = strategyConfigs.first(where: { $0.id == strategyID }),
                       config.type == .doublePumpExhaustionShort {
                        await paper.setCooldown(strategyID: strategyID, instrumentID: reservation.instrumentID, bars: config.type.defaultCooldownBars)
                    }
                }
            }

            // Include reservations created after the remote snapshot. The
            // exchange view may lag a just-submitted order, but local risk may
            // never be reset below that in-flight authorization. A filled
            // entry is already counted through its position above, so a
            // strategy reservation whose order left the open-order list is
            // not added again once the publication and cancel-grace windows
            // have passed; adding it would double the instrument's exposure
            // for the life of every strategy position.
            for (orderID, reservation) in remoteReservations {
                let orderMissing = orders.allSatisfy { $0.id != orderID }
                if reservation.inFlight || orderMissing {
                    let age = Date().timeIntervalSince(reservation.createdAt)
                    let cancelAge = reservation.cancelRequestedAt.map {
                        Date().timeIntervalSince($0)
                    }
                    let retainMissing = reservation.inFlight ||
                        Self.isUnresolvedReservation(orderID) ||
                        age < 30 ||
                        (cancelAge.map { $0 < Self.cancelSettlementGrace } ?? false)
                    guard retainMissing else { continue }
                    exposures[reservation.instrumentID, default: 0] += reservation.notional
                }
            }
            await riskEngine.reconcileGlobalNotionals(exposures)
            await paper.setRisk(await riskEngine.snapshot())
        }
    }

    public func contracts(forceRefresh: Bool = false) async throws -> [ContractMarket] {
        let all = try await market.contracts(forceRefresh: forceRefresh)
        let byVolume = Set(all.filter(StrategyUniverseRules.isEligibleHotAltcoin)
            .sorted { $0.volume24h > $1.volume24h }
            .prefix(20)
            .map(\.id))
        let gainers = Set(all.sorted { $0.changePercent > $1.changePercent }.prefix(12).map(\.id))
        let losers = Set(all.sorted { $0.changePercent < $1.changePercent }.prefix(12).map(\.id))
        let enriched = all.map { item in
            let category = byVolume.contains(item.id) ? "热门" : gainers.contains(item.id) ? "涨幅" : losers.contains(item.id) ? "跌幅" : "全部"
            return ContractMarket(id: item.id, name: item.name, baseCurrency: item.baseCurrency, quoteCurrency: item.quoteCurrency, last: item.last, changePercent: item.changePercent, rollingChangePercent: item.rollingChangePercent, volume24h: item.volume24h, category: category, updatedAt: item.updatedAt)
        }.sorted { $0.volume24h > $1.volume24h }
        contractUniverse = enriched
        await paper.refreshStrategyUniverse(enriched)
        return enriched
    }

    public func cachedContracts() -> [ContractMarket] { contractUniverse }

    /// Returns the concrete targets currently used by the scanner. `fresh=true`
    /// is intended for operator verification and performs one contract refresh;
    /// normal runtime callers reuse the backend's last contract snapshot.
    public func strategyUniverseTargets(fresh: Bool = false) async throws -> [StrategyUniverseSnapshot] {
        if fresh { _ = try await contracts(forceRefresh: true) }
        return await paper.strategyUniverseTargets()
    }

    public func account() async throws -> AccountOverview {
        await restoreRiskIfNeeded()
        let value = try await market.account()
        if let equity = value.equityUSD { await riskEngine.synchronizeEquity(equity) }
        var reconciliationError: Error?
        do {
            try await reconcileRemoteReservations()
        } catch {
            reconciliationError = error
            appendLog("远端风险占用对账失败，本次禁止基于旧账本开仓：\(error.localizedDescription)", level: "warning")
        }
        // Account-level risk remains based on total equity, while strategy
        // sizing is strictly based on the authenticated USDT asset balance.
        // Apply the USDT base after settlement so a just-realized native exit
        // is reflected once, rather than shrinking the pool and then adding
        // the same realized PnL a second time.
        if let usdtEquity = value.usdtEquity {
            await riskEngine.synchronizeStrategyCapital(usdtEquity)
        }
        await enforceGlobalRiskIfNeeded()
        // Persist the latest daily baseline/equity even when the loss limit
        // has not tripped, so a service restart cannot silently forget it.
        await paper.setRisk(await riskEngine.snapshot())
        if let reconciliationError { throw reconciliationError }
        return value
    }

    public func liveStatus() async -> LiveTradingStatus {
        do {
            let account = try await account()
            if account.mode == .live {
                return LiveTradingStatus(mode: .live, profile: account.profile, enabled: liveTradingEnabled, available: true, message: liveTradingEnabled ? "实盘账户已连接，策略按账号类型自动交易；手动下单已启用" : "实盘账户已连接，策略按账号类型自动交易；手动下单需启用")
            }
            return LiveTradingStatus(mode: account.mode, profile: account.profile, enabled: false, available: false, message: account.mode == .paper ? "当前是模拟账户，策略按账号类型自动交易" : "未连接可下单的实盘 profile")
        } catch {
            return LiveTradingStatus(message: error.localizedDescription)
        }
    }

    public func enableLiveTrading() async throws -> LiveTradingStatus {
        let account = try await account()
        guard account.mode == .live else { throw ATKError.demoProfile(profile: account.profile ?? "unknown") }
        liveTradingEnabled = true
        appendLog("手动实盘交易已启用", level: "warning")
        return await liveStatus()
    }

    public func disableLiveTrading() async -> LiveTradingStatus {
        liveTradingEnabled = false
        appendLog("手动实盘交易已关闭")
        return await liveStatus()
    }

    private func validatedRemoteOrder(_ request: LiveOrderRequest, ticker: MarketTicker) async throws -> (SwapInstrumentSpec, Decimal) {
        guard request.quantity.isFinite, request.quantity > 0 else {
            throw ATKError.invalidOrder("数量必须是有限的正数")
        }
        // The client order id keys the durable risk claim and is the only way
        // to resolve an interrupted submit; an id OKX would reject could
        // never be looked up, so its claim would never settle.
        if let clientOrderID = request.clientOrderID, !ATKClient.isValidClientOrderID(clientOrderID) {
            throw ATKError.invalidOrder("客户端订单号必须是 1 到 32 位字母或数字")
        }
        guard ticker.last.isFinite, ticker.last > 0 else {
            throw ATKError.invalidOrder("市场价格必须是有限的正数")
        }
        let spec = try await market.instrumentSpec(instrumentID: request.instrumentID)
        guard spec.isLiveUSDTLinearSwap else {
            throw ATKError.invalidOrder("仅支持 live USDT linear swap：\(request.instrumentID)")
        }
        guard spec.accepts(contractQuantity: request.quantity) else {
            throw ATKError.invalidOrder("数量必须按合约 lotSz 对齐且不小于 minSz（当前数量为合约张数）")
        }
        guard ["cross", "isolated"].contains(request.marginMode.lowercased()) else {
            throw ATKError.invalidOrder("保证金模式必须是 cross 或 isolated")
        }
        for price in [request.price, request.takeProfitTriggerPrice, request.stopLossTriggerPrice].compactMap({ $0 }) {
            guard spec.accepts(price: price) else {
                throw ATKError.invalidOrder("价格必须按合约 tickSz 对齐")
            }
        }
        // A buy limit above the current quote may fill at its limit price;
        // reserve the larger price so a valid limit cannot bypass the cap.
        let exposurePrice = max(ticker.last, request.price ?? ticker.last)
        let notional = abs(spec.notional(forContracts: request.quantity, price: exposurePrice))
        guard notional.isFinite, notional > 0 else {
            throw ATKError.invalidOrder("无法根据合约规格计算有效名义价值")
        }
        return (spec, notional)
    }

    public func placeLiveOrder(_ incoming: LiveOrderRequest) async throws -> LiveOrderResult {
        // This endpoint is for manual orders. Strategy submissions use the
        // account-selected route directly and intentionally do not require
        // this operator switch.
        guard liveTradingEnabled else { throw ATKError.liveTradingDisabled }
        let clientOrderID = incoming.clientOrderID ?? Self.strategyClientOrderID(UUID())
        let request = Self.request(incoming, clientOrderID: clientOrderID)
        let account = try await account()
        guard account.mode == .live else { throw ATKError.demoProfile(profile: account.profile ?? "unknown") }
        let ticker = try await market.ticker(instrumentID: request.instrumentID)
        let (_, notional) = try await validatedRemoteOrder(request, ticker: ticker)
        let (decision, submissionToken) = await authorizeRemoteSubmission(instrumentID: request.instrumentID, notional: notional, reduceOnly: request.reduceOnly, clientOrderID: clientOrderID, demo: false)
        guard decision.allowed else { throw ATKError.unavailable(decision.reason ?? "风控拒绝订单") }
        await paper.setRisk(await riskEngine.snapshot())
        let result: LiveOrderCommandResult
        do {
            result = try await market.placeLiveOrder(request)
        } catch {
            guard let orderID = try await recoverManualSubmission(error, token: submissionToken, instrumentID: request.instrumentID, clientOrderID: clientOrderID, demo: false) else { throw error }
            result = LiveOrderCommandResult(orderID: orderID, clientOrderID: clientOrderID, message: "响应中断，已按 clOrdId 确认订单")
        }
        await confirmRemoteSubmission(submissionToken, orderID: result.orderID)
        await market.invalidateAccountState()
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("实盘订单已提交 \(request.instrumentID) \(request.side) \(request.quantity)", level: "warning")
        return LiveOrderResult(orderID: result.orderID, clientOrderID: result.clientOrderID, instrumentID: request.instrumentID, side: request.side, orderType: request.orderType, quantity: request.quantity, message: result.message)
    }

    private static func request(_ request: LiveOrderRequest, clientOrderID: String) -> LiveOrderRequest {
        LiveOrderRequest(instrumentID: request.instrumentID, side: request.side, orderType: request.orderType, quantity: request.quantity, positionSide: request.positionSide, marginMode: request.marginMode, reduceOnly: request.reduceOnly, price: request.price, takeProfitTriggerPrice: request.takeProfitTriggerPrice, stopLossTriggerPrice: request.stopLossTriggerPrice, leverage: request.leverage, clientOrderID: clientOrderID)
    }

    /// Returns the exchange order id when a failed manual submit was in fact
    /// accepted, or nil (reservation released) when OKX confirms the order
    /// does not exist. An unverifiable result throws a distinct error so the
    /// operator checks the exchange instead of blindly re-submitting.
    private func recoverManualSubmission(_ error: Error, token: String?, instrumentID: String, clientOrderID: String, demo: Bool) async throws -> String? {
        switch await resolveFailedSubmission(instrumentID: instrumentID, clientOrderID: clientOrderID, demo: demo) {
        case let .accepted(orderID, _):
            appendLog("订单响应失败但 OKX 已接受 \(instrumentID) 订单 \(orderID)：\(error.localizedDescription)", level: "warning")
            return orderID
        case .notPlaced:
            await failRemoteSubmission(token)
            await paper.setRisk(await riskEngine.snapshot())
            return nil
        case let .terminal(orderID, status):
            // OKX has the order and already ended it without a fill: the
            // attempt opened no exposure and left no resting order.
            await failRemoteSubmission(token)
            await paper.setRisk(await riskEngine.snapshot())
            appendLog("订单响应失败，OKX 已终结订单 \(orderID)（\(status)），未产生持仓", level: "warning")
            return nil
        case let .unknown(lookupError):
            // The order may exist: keep its reservation until a later
            // clOrdId lookup settles the outcome.
            await markSubmissionUnresolved(token)
            await market.invalidateAccountState()
            await paper.setRisk(await riskEngine.snapshot())
            let message = "下单结果未知（clOrdId \(clientOrderID)），风控占用已保留并将在后台按 clOrdId 核对；请先在 OKX 核对 \(instrumentID) 再重试：\(error.localizedDescription)；查询失败：\(lookupError.localizedDescription)"
            appendLog(message, level: "warning")
            throw ATKError.unavailable(message)
        }
    }

    public func placePaperOrder(_ request: PaperOrderRequest) async throws -> PaperOrder {
        let account = try await account()
        guard account.mode == .paper else { throw ATKError.unavailable("当前不是 OKX 模拟账户") }
        let ticker = try await market.ticker(instrumentID: request.instrumentID)
        let clientOrderID = Self.strategyClientOrderID(UUID())
        let liveRequest = LiveOrderRequest(instrumentID: request.instrumentID, side: request.side, orderType: "market", quantity: request.quantity, marginMode: request.marginMode ?? "cross", reduceOnly: request.reduceOnly, clientOrderID: clientOrderID)
        let (_, notional) = try await validatedRemoteOrder(liveRequest, ticker: ticker)
        let (decision, submissionToken) = await authorizeRemoteSubmission(instrumentID: request.instrumentID, notional: notional, reduceOnly: request.reduceOnly, clientOrderID: clientOrderID, demo: true)
        guard decision.allowed else { throw ATKError.unavailable(decision.reason ?? "风控拒绝模拟订单") }
        await paper.setRisk(await riskEngine.snapshot())
        let remoteOrderID: String
        do {
            remoteOrderID = try await market.placeDemoOrder(liveRequest).orderID
        } catch {
            guard let orderID = try await recoverManualSubmission(error, token: submissionToken, instrumentID: request.instrumentID, clientOrderID: clientOrderID, demo: true) else { throw error }
            remoteOrderID = orderID
        }
        await confirmRemoteSubmission(submissionToken, orderID: remoteOrderID)
        await market.invalidateAccountState()
        await paper.setRisk(await riskEngine.snapshot())
        let order = PaperOrder(strategyID: UUID(), instrumentID: request.instrumentID, side: request.side.lowercased() == "sell" ? "short" : "long", quantity: request.quantity, status: "submitted", remoteOrderID: remoteOrderID)
        await paper.record(order)
        appendLog("挂单：OKX 模拟盘 \(request.instrumentID) \(request.side) \(request.quantity)，订单 \(remoteOrderID)", level: "order")
        return order
    }

    public func privatePositions() async throws -> [PositionSnapshot] { try await market.privatePositions() }
    public func privateOrders() async throws -> [OrderSnapshot] { try await market.privateOrders() }

    public func strategies() async -> [StrategyConfig] { await paper.allStrategies() }
    public func statuses() async -> [StrategyStatus] { await paper.allStatuses() }
    public func status(for id: UUID) async -> StrategyStatus? { await paper.status(for: id) }
    /// Loads the historical window needed by a background strategy monitor
    /// without evaluating old bars or submitting historical signals.
    /// `forceRefresh` bypasses the snapshot TTL. The stream uses it after a
    /// WSS reconnect: bars that closed while the socket was down are never
    /// replayed by OKX, so only a REST reload can fill that gap.
    public func prewarmStrategyMarket(instrumentID: String, interval: KlineInterval, forceRefresh: Bool = false) async throws {
        let snapshot = try await market.snapshot(instrumentID: instrumentID, interval: interval, forceRefresh: forceRefresh)
        await candles.ingest(snapshot.candles, instrumentID: instrumentID, interval: interval)
        await paper.prewarm(snapshot)
    }
    public func marketSnapshot(instrumentID: String, interval: KlineInterval) async throws -> MarketSnapshot {
        await restoreRiskIfNeeded()
        let snapshot = try await market.snapshot(instrumentID: instrumentID, interval: interval)
        await candles.ingest(snapshot.candles, instrumentID: instrumentID, interval: interval)
        let statuses = await paper.evaluate(snapshot, contracts: contractUniverse)
        let latestTimestamp = snapshot.candles.last(where: { $0.confirmed })?.timestamp
        if let latest = snapshot.candles.last(where: { $0.confirmed }) {
            await broker.processNextOpen(instrumentID: instrumentID, candle: latest)
            // Orders fill at this bar's open. Mark the resulting position at
            // the snapshot close so REST refreshes expose current unrealized PnL
            // just like the realtime candle path.
        await broker.mark(instrumentID: instrumentID, price: latest.close, at: latest.timestamp)
        await logNewFills()
        }
        await enforceGlobalRiskIfNeeded(now: latestTimestamp ?? .now)
        let exitCandle = snapshot.interval == .fifteenMinutes ? snapshot.candles.last(where: { $0.confirmed }) : nil
        await enforceStrategyExits(at: .now, fallbackPrice: snapshot.candles.last(where: { $0.confirmed })?.close, candle: exitCandle, candleInstrumentID: instrumentID)
        let configs = await paper.allStrategies()
        logSignals(statuses, configs: configs, instrumentID: instrumentID)
        let latestConfirmedTimestamp = snapshot.candles.last(where: { $0.confirmed })?.timestamp
        for status in statuses {
            guard status.state == .running, let signal = status.lastSignal, signal.type.hasPrefix("entry_"), signal.timestamp == latestConfirmedTimestamp, !submittedSignals.contains(signal.id), let config = configs.first(where: { $0.id == status.id }), config.enabled else { continue }
            // 防御：信号必须就是本标的当前记录的那一个，跨标的信号一律不下单。
            guard await paper.status(for: config.id, instrumentID: instrumentID)?.lastSignal?.id == signal.id else { continue }
            if await submitStrategyOrder(config: config, signal: signal, instrumentID: instrumentID) {
                recordSubmittedSignal(signal.id)
            }
        }
        await paper.setRisk(await broker.riskSnapshot())
        return snapshot
    }

    /// 统计某个策略实例当前在远端持有的仓位数量。
    /// 远端持仓不带策略归属，因此用"该标的最近一笔入场订单"反查（与
    /// `enforceStrategyExits` 的归属判断保持同一口径）。
    private func openPositionCount(strategyID: UUID) async -> Int {
        guard let positions = try? await market.privatePositions(), !positions.isEmpty else { return 0 }
        let orders = await paper.allOrders()
        var count = 0
        for position in positions where abs(position.quantity) > 0 {
            let normalizedPositionSide = position.side.lowercased()
            let positionIsShort = normalizedPositionSide == "short" || (normalizedPositionSide == "net" && position.quantity < 0)
            guard let order = orders.reversed().first(where: { order in
                order.instrumentID == position.instrumentID &&
                    ["submitted", "filled", "pending"].contains(order.status.lowercased()) &&
                    (positionIsShort ? order.side.lowercased() == "short" : order.side.lowercased() == "long")
            }) else { continue }
            if order.strategyID == strategyID { count += 1 }
        }
        return count
    }

    /// Submit a strategy entry through the account selected in the connected
    /// OKX profile.  Strategy definitions describe signals and risk only;
    /// they must not decide whether the account is paper or live.
    private func submitStrategyOrder(config: StrategyConfig, signal: StrategySignal, instrumentID: String) async -> Bool {
        guard config.enabled else { return false }
        guard strategyEntrySubmissionsInFlight.insert(config.id).inserted else { return false }
        guard strategyEntryInFlightInstruments.insert(instrumentID).inserted else {
            strategyEntrySubmissionsInFlight.remove(config.id)
            return false
        }
        defer {
            strategyEntrySubmissionsInFlight.remove(config.id)
            strategyEntryInFlightInstruments.remove(instrumentID)
        }
        // Callers pass a config captured before several suspension points. A
        // pause that ran in between saw no in-flight submission and returned
        // without anything to cancel, so re-read the persisted state only after
        // claiming the in-flight slot (later pauses wait for this submission).
        guard let live = await paper.allStrategies().first(where: { $0.id == config.id }), live.enabled else {
            appendLog("策略 \(config.name) 未发送：策略已停止或已删除", level: "warning")
            return false
        }
        // 同一币种单仓：策略实例对动态扫描池中的每个币种都只允许一笔。
        // 读取失败时不能当作"无持仓"放行。
        let existingPositions: [PositionSnapshot]
        do {
            existingPositions = try await market.privatePositions()
        } catch {
            appendLog("策略 \(config.name) 未发送：无法读取远端持仓（\(error.localizedDescription)）", level: "warning")
            return false
        }
        if existingPositions.contains(where: { $0.instrumentID == instrumentID && abs($0.quantity) > 0 }) {
            appendLog("策略 \(config.name) 未发送：\(instrumentID) 已有持仓，同一标的只允许一笔", level: "warning")
            return false
        }
        // 组合上限：动态扫描可以覆盖多个币种，但一个策略实例只允许
        // 一个活动币种；每笔订单仍按策略池权益和动态止损距离计算。
        let maxConcurrent = Int(config.parameters["maxConcurrentPositions"] ?? 1)
        guard await openPositionCount(strategyID: config.id) < maxConcurrent else {
            appendLog("策略 \(config.name) 未发送：并发持仓已达上限 \(maxConcurrent)", level: "warning")
            return false
        }
        guard let account = try? await account() else {
            appendLog("策略 \(config.name) 未发送：无法读取交易账户", level: "warning")
            return false
        }
        let demo: Bool
        switch account.mode {
        case .paper:
            demo = true
        case .live:
            demo = false
        case .readOnly:
            appendLog("策略 \(config.name) 未发送：当前账户为只读模式", level: "warning")
            return false
        }
        guard !remoteReservations.values.contains(where: { $0.instrumentID == instrumentID }) else {
            appendLog("策略 \(config.name) 未发送：\(instrumentID) 已有待确认的挂单或持仓", level: "warning")
            return false
        }
        guard let ticker = try? await market.ticker(instrumentID: instrumentID),
              let spec = try? await market.instrumentSpec(instrumentID: instrumentID),
              spec.isLiveUSDTLinearSwap else {
            appendLog("策略 \(config.name) 未发送：无法读取可交易的 live USDT linear swap 合约规格", level: "warning")
            return false
        }
        let pool = await riskEngine.strategyCapital(config.id, allocationPercent: Decimal(config.capitalPoolPercent))
        guard pool.openPositions < maxConcurrent else {
            appendLog("策略 \(config.name) 未发送：已有其他币种的挂单或持仓，只允许一个币种下单", level: "warning")
            return false
        }
        // Size against the price that will actually be sent to the exchange.
        // The signal close can be stale by the time the dynamic-universe
        // monitor submits it; using that old price understates stop risk after
        // a gap and can exceed the strategy's risk budget.
        let entry = ticker.last
        let isLong = signal.type == "entry_long"
        // DME 规则：入场价相对信号收盘价偏离超过滑点上限时放弃该笔交易，
        // 不追价（double_pump_exhaustion_short STRATEGY.md §4，max_entry_slippage_pct）。
        if config.type == .doublePumpExhaustionShort, signal.price > 0 {
            let limit = Decimal(config.parameters["maxEntrySlippage"] ?? 0.003)
            let deviation = abs(entry - signal.price) / signal.price
            guard deviation <= limit else {
                appendLog("策略 \(config.name) 未发送：\(instrumentID) 当前价 \(entry) 偏离信号收盘价 \(signal.price) 超过 \(limit * 100)%，不追价", level: "warning")
                return false
            }
        }
        // Move protection towards the entry when rounding. This preserves the
        // risk budget and prevents ATR-derived off-tick prices being rejected.
        let stopPrice = signal.stopPrice.flatMap { spec.alignedPrice($0, roundingUp: isLong) }
        let takePrice = signal.takePrice.flatMap { spec.alignedPrice($0, roundingUp: !isLong) }
        guard signal.stopPrice == nil || stopPrice != nil,
              signal.takePrice == nil || takePrice != nil else { return false }
        if let stop = stopPrice {
            let stopIsOnProtectiveSide = isLong ? stop < entry : stop > entry
            guard stopIsOnProtectiveSide else {
                appendLog("策略 \(config.name) 未发送：当前价格已越过保护止损价", level: "warning")
                return false
            }
        }
        if let take = takePrice {
            let takeIsOnProfitSide = isLong ? take > entry : take < entry
            guard takeIsOnProfitSide else {
                appendLog("策略 \(config.name) 未发送：当前价格已越过止盈价", level: "warning")
                return false
            }
        }
        let riskDistance = stopPrice.map { abs($0 - entry) } ?? 0
        // The stop-loss budget is a percentage of this strategy's own pool
        // equity at authorization (realized results compound into it,
        // unrealized ones do not). Sizing off the whole account would let a
        // 1% pool carry a position sized for the full balance.
        let riskBudget = pool.equity * Decimal(config.riskPercent) / 100
        let targetNotional: Decimal
        if riskDistance > 0, entry > 0 {
            targetNotional = min(pool.availableCapital, riskBudget * entry / riskDistance)
        } else {
            targetNotional = pool.availableCapital
        }
        guard targetNotional > 0, ticker.last > 0 else {
            appendLog("策略 \(config.name) 未发送：资金池无可用余额", level: "warning")
            return false
        }
        // OKX `sz` is a contract count.  Round down to lotSz so the order's
        // actual notional and stop risk remain below the strategy budget.
        guard let quantity = spec.contracts(forTargetNotional: targetNotional, price: ticker.last) else {
            appendLog("策略 \(config.name) 未发送：目标资金不足以覆盖合约最小张数", level: "warning")
            return false
        }
        let notional = abs(spec.notional(forContracts: quantity, price: ticker.last))
        // 本单的止损风险，用于执行策略资金池的"开放风险 ≤ 池权益比例"上限
        // 风险金额用于累计开放止损风险的授权检查。
        let orderRisk = riskDistance > 0 ? quantity * riskDistance * spec.contractValue : 0
        let maxOpenRiskPercent: Decimal? = Decimal(config.parameters["maxOpenRiskPercent"] ?? 0)
        // A pause or update may have arrived during account/ticker reads.
        // Never submit a signal using a stale strategy definition.
        guard await paper.allStrategies().contains(config) else { return false }
        let side = signal.type == "entry_short" ? "sell" : "buy"
        // Derived from the signal so every retry of the same signal carries
        // the same exchange identity and can be looked up after a timeout.
        let clientOrderID = Self.strategyClientOrderID(signal.id)
        // OKX can accept an order whose response never arrives, and a process
        // that exits during that call must still restart knowing the entry.
        // Everything is therefore persisted before the submit, in this order:
        // the reservation and risk snapshot (inside authorization), then the
        // entry record. A crash between them leaves only a reservation that
        // references an unwritten record; its clOrdId lookup finds no order
        // and releases it. The reverse order could strand an entry record
        // with no claim, attributing unrelated positions to the strategy.
        let order = PaperOrder(strategyID: config.id, instrumentID: instrumentID, side: side == "sell" ? "short" : "long", quantity: quantity, requestedAt: signal.timestamp, status: "submitted", clientOrderID: clientOrderID, signal: signal)
        let (decision, submissionToken) = await authorizeRemoteSubmission(
            instrumentID: instrumentID, notional: notional,
            strategyID: config.id, poolAllocationPercent: Decimal(config.capitalPoolPercent),
            riskAmount: orderRisk, maxOpenRiskPercent: maxOpenRiskPercent,
            maxConcurrentPositions: maxConcurrent,
            clientOrderID: clientOrderID, demo: demo, localOrderID: order.id
        )
        guard decision.allowed else {
            appendLog("策略 \(config.name) 被风控拒绝：\(decision.reason ?? "未知原因")", level: "warning")
            return false
        }
        await paper.record(order)
        let request = LiveOrderRequest(instrumentID: instrumentID, side: side, orderType: "market", quantity: quantity, marginMode: "cross", takeProfitTriggerPrice: takePrice, stopLossTriggerPrice: stopPrice, leverage: Decimal(config.leverage), clientOrderID: clientOrderID)
        let remoteOrderID: String
        do {
            if demo {
                remoteOrderID = try await market.placeDemoOrder(request).orderID
            } else {
                remoteOrderID = try await market.placeLiveOrder(request).orderID
            }
        } catch {
            switch await resolveFailedSubmission(instrumentID: instrumentID, clientOrderID: clientOrderID, demo: demo) {
            case let .accepted(orderID, _):
                appendLog("策略 \(config.name) 下单响应失败但 OKX 已接受订单 \(orderID)：\(error.localizedDescription)", level: "warning")
                remoteOrderID = orderID
            case .notPlaced:
                await failRemoteSubmission(submissionToken)
                await paper.markLocalOrderTerminal(order.id, status: "rejected")
                await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
                appendLog("策略 \(config.name) OKX \(demo ? "模拟" : "实盘")下单失败：\(error.localizedDescription)", level: "warning")
                return false
            case let .terminal(orderID, status):
                await failRemoteSubmission(submissionToken)
                await paper.markLocalOrderTerminal(order.id, status: status)
                await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
                appendLog("策略 \(config.name) 下单响应失败，OKX 已终结订单 \(orderID)（\(status)），未产生持仓：\(error.localizedDescription)", level: "warning")
                return false
            case let .unknown(lookupError):
                // The entry record and the durable claim already exist from
                // before the submit, so only the outcome is unknown;
                // reconciliation resolves it by clOrdId. The signal is
                // reported as consumed so it is never resent.
                await markSubmissionUnresolved(submissionToken)
                await market.invalidateAccountState()
                await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
                appendLog("策略 \(config.name) 下单结果未知，已停止重发该信号并保留风控占用，后台将按 clOrdId \(clientOrderID) 核对 \(instrumentID)：\(error.localizedDescription)；查询失败：\(lookupError.localizedDescription)", level: "warning")
                return true
            }
        }
        // Give the durable entry record its exchange order id now that the
        // exchange accepted the order; restart recovery uses it to protect
        // the remote position.
        await paper.attachRemoteOrderID(remoteOrderID, toLocalOrder: order.id)
        // Keep the reservation under the authenticated order ID until a
        // position disappears.  This covers exchange-native SL/TP exits,
        // which never pass through enforceStrategyExits.
        await confirmRemoteSubmission(submissionToken, orderID: remoteOrderID)
        await market.invalidateAccountState()
        await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
        appendLog("挂单：策略 \(config.name) / OKX \(demo ? "模拟" : "实盘") / \(instrumentID) / \(side) \(quantity)，订单 \(remoteOrderID)", level: "order")
        if signal.stopPrice != nil || signal.takePrice != nil || signal.takePrices != nil { appendLog("策略 \(config.name) 已附带保护规则；分批止盈/保本/跟踪由策略监控处理", level: "info") }
        return true
    }

    /// Cancels an entry that is resting on the exchange. Returns the failure
    /// instead of throwing so a caller mid-reconciliation can log it and let
    /// the next pass retry. A read-only profile cannot place orders, so an
    /// unreadable mode is reported as a failure rather than assumed safe.
    private func cancelRestingOrder(orderID: String, instrumentID: String) async -> Error? {
        do {
            let account = try await market.account()
            guard account.mode != .readOnly else {
                return ATKError.unavailable("当前账户为只读模式，无法撤销挂单")
            }
            if account.mode == .paper {
                try await market.cancelDemoOrder(instrumentID: instrumentID, orderID: orderID)
            } else {
                try await market.cancelLiveOrder(instrumentID: instrumentID, orderID: orderID)
            }
            await market.invalidateAccountState()
            return nil
        } catch {
            return error
        }
    }

    private enum FailedSubmissionOutcome {
        case accepted(orderID: String, status: String)
        /// OKX knows the order but already ended it without leaving a
        /// position, so it must not become a live claim on the pool.
        case terminal(orderID: String, status: String)
        case notPlaced
        case unknown(Error)
    }



    /// A timed-out or truncated CLI response does not mean OKX rejected the
    /// order. Look up the exact client order id before treating the attempt
    /// as unsent; only OKX's explicit "order does not exist" counts as absent,
    /// and an order OKX already ended must not be re-registered as a resting
    /// claim on the pool.
    private func resolveFailedSubmission(instrumentID: String, clientOrderID: String, demo: Bool) async -> FailedSubmissionOutcome {
        do {
            if let order = try await market.privateOrder(instrumentID: instrumentID, clientOrderID: clientOrderID, demo: demo) {
                let status = OrderLifecycle.normalized(order.status)
                // A cancelled or expired order may still have filled partly;
                // only a reported zero fill proves it opened no position.
                if order.endedUnfilled {
                    return .terminal(orderID: order.id, status: status)
                }
                return .accepted(orderID: order.id, status: status)
            }
            return .notPlaced
        } catch {
            return .unknown(error)
        }
    }

    public func orders() async -> [PaperOrder] { await paper.allOrders() }
    public func fills() async -> [PaperFill] { await paper.allFills() }
    public func logs() -> [RuntimeLog] { runtimeLogs }

    @discardableResult
    public func ingestRealtimeCandle(_ candle: Candle, instrumentID: String, interval: KlineInterval) async -> [StrategyStatus] {
        await restoreRiskIfNeeded()
        await market.cacheRealtimeCandle(candle, instrumentID: instrumentID, interval: interval)
        await candles.ingest(candle, instrumentID: instrumentID, interval: interval)
        // A pending order is filled at the next bar's open. WSS publishes an
        // unconfirmed update as soon as that bar starts, so waiting for the
        // close would introduce a full-bar execution delay in paper trading.
        await broker.processNextOpen(instrumentID: instrumentID, candle: candle)
        await broker.mark(instrumentID: instrumentID, price: candle.close, at: candle.timestamp)
        await logNewFills()
        await enforceGlobalRiskIfNeeded(now: candle.timestamp)
        let exitCandle = interval == .fifteenMinutes ? candle : nil
        await enforceStrategyExits(at: .now, fallbackPrice: candle.close, candle: exitCandle, candleInstrumentID: instrumentID)
        await paper.setRisk(await broker.riskSnapshot())
        let configs = await paper.allStrategies()
        let shouldEvaluate = candle.confirmed
        if shouldEvaluate {
            let current = await candles.values(instrumentID: instrumentID, interval: interval)
            let snapshot = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: current)
            let statuses = await paper.evaluate(snapshot, contracts: contractUniverse)
            logSignals(statuses, configs: configs, instrumentID: instrumentID)
            for status in statuses {
                guard status.state == .running, let signal = status.lastSignal, signal.type.hasPrefix("entry_"), signal.timestamp == candle.timestamp, !submittedSignals.contains(signal.id), let config = configs.first(where: { $0.id == status.id }), config.enabled else { continue }
                // 防御：信号必须就是本标的当前记录的那一个，跨标的信号一律不下单。
                guard await paper.status(for: config.id, instrumentID: instrumentID)?.lastSignal?.id == signal.id else { continue }
                if await submitStrategyOrder(config: config, signal: signal, instrumentID: instrumentID) { recordSubmittedSignal(signal.id) }
            }
            return statuses
        }
        return await paper.allStatuses()
    }

    public func appendLog(_ message: String, level: String = "info") {
        appendLog(RuntimeLog(level: level, message: message))
    }

    /// `runtime-log.jsonl` is append-only between compactions. Once it holds
    /// twice the in-memory window it is rewritten to that window, so the file
    /// stays bounded however long the daemon runs.
    private func appendLog(_ log: RuntimeLog) {
        runtimeLogs.append(log)
        if runtimeLogs.count > Self.runtimeLogMemoryLimit {
            runtimeLogs.removeFirst(runtimeLogs.count - Self.runtimeLogMemoryLimit)
        }
        runtimeLogFileLines += 1
        if runtimeLogFileLines > 2 * Self.runtimeLogMemoryLimit {
            Self.writeRuntimeLogs(runtimeLogs, to: runtimeLogURL)
            runtimeLogFileLines = runtimeLogs.count
        } else {
            Self.appendRuntimeLog(log, to: runtimeLogURL)
        }
    }

    private static func runtimeLogEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    /// Returns the retained log window and the number of lines in the file.
    private static func loadRuntimeLogs(from url: URL) -> (logs: [RuntimeLog], fileLines: Int) {
        guard let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .utf8) else { return ([], 0) }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let lines = text.split(separator: "\n")
        let logs = lines.suffix(runtimeLogMemoryLimit).compactMap { line -> RuntimeLog? in
            try? decoder.decode(RuntimeLog.self, from: Data(line.utf8))
        }
        return (logs, lines.count)
    }

    private static func writeRuntimeLogs(_ logs: [RuntimeLog], to url: URL) {
        let encoder = runtimeLogEncoder()
        var data = Data()
        for log in logs {
            guard let line = try? encoder.encode(log) else { continue }
            data.append(line)
            data.append(0x0A)
        }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
    }

    private static func appendRuntimeLog(_ log: RuntimeLog, to url: URL) {
        guard var line = try? runtimeLogEncoder().encode(log) else { return }
        line.append(0x0A)
        // Runtime logging must never interrupt market-data or order handling.
        guard let handle = try? FileHandle(forWritingTo: url) else {
            writeRuntimeLogs([log], to: url)
            return
        }
        defer { try? handle.close() }
        _ = try? handle.seekToEnd()
        try? handle.write(contentsOf: line)
    }
}

/// Rejects browser-originated and DNS-rebound requests to the loopback API.
/// The daemon has no credentials of its own, so without this any web page
/// could POST a `text/plain` body (no CORS preflight) to enable live trading
/// and place orders, or open the stream and read account data. Native
/// clients (URLSession, curl) send no `Origin`; browsers always send one on
/// cross-origin POST and WebSocket upgrades.
public struct LoopbackOriginGuardMiddleware: HBMiddleware {
    public init() {}

    public func apply(to request: HBRequest, next: HBResponder) -> EventLoopFuture<HBResponse> {
        guard Self.isAllowed(host: request.headers.first(name: "host"), origin: request.headers.first(name: "origin")) else {
            return request.failure(HBHTTPError(.forbidden))
        }
        return next.respond(to: request)
    }

    public static func isAllowed(host: String?, origin: String?) -> Bool {
        guard let host, isLoopbackAuthority(host) else { return false }
        guard let origin else { return true }
        guard let url = URL(string: origin), url.scheme == "http" || url.scheme == "https",
              let originHost = url.host else { return false }
        let authority = url.port.map { "\(originHost):\($0)" } ?? originHost
        return isLoopbackAuthority(authority) && authority.lowercased() == host.lowercased()
    }

    private static func isLoopbackAuthority(_ authority: String) -> Bool {
        let value = authority.lowercased()
        let hostPart: String
        if value.hasPrefix("[") {
            guard let end = value.firstIndex(of: "]") else { return false }
            hostPart = String(value[value.index(after: value.startIndex)..<end])
        } else {
            hostPart = value.split(separator: ":", maxSplits: 1).first.map(String.init) ?? value
        }
        return ["127.0.0.1", "localhost", "::1"].contains(hostPart)
    }
}

public struct TradingHTTPServer {
    public let backend: TradingBackend

    public init(backend: TradingBackend = TradingBackend()) { self.backend = backend }

    public func configure(_ app: HBApplication) throws {
        app.middleware.add(LoopbackOriginGuardMiddleware())
        let jsonEncoder = JSONEncoder()
        jsonEncoder.dateEncodingStrategy = .iso8601
        app.encoder = jsonEncoder
        let jsonDecoder = JSONDecoder()
        jsonDecoder.dateDecodingStrategy = .iso8601
        app.decoder = jsonDecoder
        app.router.get("health") { [backend] request in try request.application.encoder.encode(await backend.health(), from: request) }
        app.router.get("api/v1/contracts") { [backend] request in
            let forceRefresh = request.uri.queryParameters.get("fresh") == "true"
            return try request.application.encoder.encode(await backend.contracts(forceRefresh: forceRefresh), from: request)
        }
        app.router.get("api/v1/account") { [backend] request in
            try request.application.encoder.encode(try await backend.account(), from: request)
        }
        app.router.get("api/v1/live/trading-status") { [backend] request in
            try request.application.encoder.encode(await backend.liveStatus(), from: request)
        }
        app.router.post("api/v1/live/trading/enable") { [backend] request in
            try request.application.encoder.encode(try await backend.enableLiveTrading(), from: request)
        }
        app.router.post("api/v1/live/trading/disable") { [backend] request in
            try request.application.encoder.encode(await backend.disableLiveTrading(), from: request)
        }
        app.router.post("api/v1/live/orders") { [backend] request in
            let order = try request.decode(as: LiveOrderRequest.self)
            return try request.application.encoder.encode(try await backend.placeLiveOrder(order), from: request)
        }
        app.router.post("api/v1/paper/orders") { [backend] request in
            let order = try request.decode(as: PaperOrderRequest.self)
            return try request.application.encoder.encode(try await backend.placePaperOrder(order), from: request)
        }
        app.router.get("api/v1/market/candles") { [backend] request in
            let params = request.uri.queryParameters
            guard let instrument = params.get("instId"), !instrument.isEmpty,
                  let interval = params.get("bar").flatMap(KlineInterval.init(exchangeBar:)) else { throw HBHTTPError(.badRequest) }
            return try request.application.encoder.encode(await backend.marketSnapshot(instrumentID: instrument, interval: interval), from: request)
        }
        app.router.get("api/v1/strategies") { [backend] request in try request.application.encoder.encode(await backend.strategies(), from: request) }
        app.router.get("api/v1/strategies/targets") { [backend] request in
            let fresh = request.uri.queryParameters.get("fresh") == "true"
            return try request.application.encoder.encode(await backend.strategyUniverseTargets(fresh: fresh), from: request)
        }
        app.router.get("api/v1/strategy-packages") { [backend] request in
            try request.application.encoder.encode(await backend.strategyPackageManifests(), from: request)
        }
        app.router.post("api/v1/strategy-packages") { [backend] request in
            let install = try request.decode(as: StrategyPackageInstallRequest.self)
            return try request.application.encoder.encode(try await backend.installStrategyPackage(install), from: request)
        }
        app.router.delete("api/v1/strategy-packages/:id") { [backend] request in
            guard let identifier = request.parameters.get("id") else { throw HBHTTPError(.badRequest) }
            return try request.application.encoder.encode(try await backend.uninstallStrategyPackage(identifier: identifier), from: request)
        }
        app.router.post("api/v1/strategy-packages/:id/instances") { [backend] request in
            guard let identifier = request.parameters.get("id") else { throw HBHTTPError(.badRequest) }
            // An empty body asks for the manifest defaults; a malformed one is rejected.
            let hasBody = (request.body.buffer?.readableBytes ?? 0) > 0
            let instance = hasBody ? try request.decode(as: StrategyPackageInstanceRequest.self) : StrategyPackageInstanceRequest()
            return try request.application.encoder.encode(try await backend.createStrategyFromPackage(identifier: identifier, request: instance), from: request)
        }
        app.router.get("api/v1/strategies/status") { [backend] request in try request.application.encoder.encode(await backend.statuses(), from: request) }
        app.router.get("api/v1/strategies/capital") { [backend] request in try request.application.encoder.encode(await backend.strategyCapital(), from: request) }
        app.router.get("api/v1/paper/orders") { [backend] request in try request.application.encoder.encode(await backend.orders(), from: request) }
        app.router.get("api/v1/paper/fills") { [backend] request in try request.application.encoder.encode(await backend.fills(), from: request) }
        app.router.get("api/v1/logs") { [backend] request in try request.application.encoder.encode(await backend.logs(), from: request) }
        app.router.post("api/v1/logs") { [backend] request in
            let log = try request.decode(as: RuntimeLog.self)
            await backend.recordLog(log)
            return try request.application.encoder.encode(log, from: request)
        }
        app.router.post("api/v1/strategies") { [backend] request in
            let config = try request.decode(as: StrategyConfig.self)
            return try request.application.encoder.encode(try await backend.createStrategy(config), from: request)
        }
        app.router.patch("api/v1/strategies/:id") { [backend] request in
            guard let idString = request.parameters.get("id"), let id = UUID(uuidString: idString) else { throw HBHTTPError(.badRequest) }
            let config = try request.decode(as: StrategyConfig.self)
            guard config.id == id else { throw HBHTTPError(.badRequest) }
            return try request.application.encoder.encode(try await backend.updateStrategy(config), from: request)
        }
        app.router.delete("api/v1/strategies/:id") { [backend] request in
            guard let idString = request.parameters.get("id"), let id = UUID(uuidString: idString) else { throw HBHTTPError(.badRequest) }
            return try request.application.encoder.encode(try await backend.deleteStrategy(id), from: request)
        }
        app.router.post("api/v1/strategies/:id/start") { [backend] request in
            guard let idString = request.parameters.get("id"), let id = UUID(uuidString: idString) else { throw HBHTTPError(.badRequest) }
            return try request.application.encoder.encode(try await backend.startStrategy(id), from: request)
        }
        app.router.post("api/v1/strategies/:id/pause") { [backend] request in
            guard let idString = request.parameters.get("id"), let id = UUID(uuidString: idString) else { throw HBHTTPError(.badRequest) }
            return try request.application.encoder.encode(try await backend.pauseStrategy(id), from: request)
        }
        app.router.get("api/v1/positions") { [backend] request in
            try request.application.encoder.encode(try await backend.privatePositions(), from: request)
        }
        app.router.get("api/v1/orders") { [backend] request in
            try request.application.encoder.encode(try await backend.privateOrders(), from: request)
        }
        app.router.get("api/v1/risk") { [backend] request in try request.application.encoder.encode(await backend.broker.riskSnapshot(), from: request) }
        app.router.post("api/v1/risk/reset") { [backend] request in try request.application.encoder.encode(await backend.resetRisk(), from: request) }
    }
}
