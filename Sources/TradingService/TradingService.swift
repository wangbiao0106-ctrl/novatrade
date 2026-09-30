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
    /// HLSR keeps a dedicated completed 4H history. It must not be inferred
    /// from the bounded 15m cache because 55 4H bars require 880 children.
    private var hlsrFourHourCandlesByInstrument: [String: [Candle]] = [:]

    public nonisolated var stateDirectory: URL { directory }

    private static func canonicalized(_ config: StrategyConfig) -> StrategyConfig {
        var normalized = config
        // 参数默认值只有一份（StrategyType.defaultParameters），App 与后端共用；
        // 调用方显式传入的键优先。
        for (key, value) in config.type.defaultParameters where normalized.parameters[key] == nil {
            normalized.parameters[key] = value
        }
        switch config.type {
        case .sweepReversalShort, .emaAltcoinLong:
            normalized.name = config.type.displayName
            normalized.interval = .oneHour
            // Strategy instances scan the live universe. Fixed-symbol inputs
            // are migrated to the dynamic scope when legacy state is loaded.
            normalized.instrumentID = ""
            normalized.scope = .dynamic(.hotAltcoins)
            if config.type == .emaAltcoinLong {
                normalized.riskPercent = config.type.maxRiskPercent
                normalized.cooldownBars = 96
            } else {
                normalized.riskPercent = min(max(normalized.riskPercent, 0.1), config.type.maxRiskPercent)
            }
        case .hlsr:
            normalized.name = config.type.displayName
            normalized.interval = .fifteenMinutes
            normalized.instrumentID = ""
            normalized.scope = .dynamic(.hotAltcoins)
            normalized.riskPercent = min(max(normalized.riskPercent, 0.1), config.type.maxRiskPercent)
            normalized.cooldownBars = config.type.defaultCooldownBars
        case .external:
            // Keep an orphaned package instance visible so its ledger and
            // protective history survive an uninstall. It can never be
            // started until the package's runtime handler is installed.
            normalized.enabled = false
        }
        // A strategy may scan many symbols, but its instance-level execution
        // policy allows only one active symbol at a time. Override stale
        // persisted concurrency values during migration.
        normalized.parameters["maxConcurrentPositions"] = 1
        normalized.parameters["maxOpenRiskPercent"] = config.type == .emaAltcoinLong ? 0.5 : 5.0
        normalized.capitalPoolPercent = min(max(normalized.capitalPoolPercent, 0.1), 100.0)
        return normalized
    }

    private static func supports(_ config: StrategyConfig, allowLegacyDynamic: Bool = false) -> Bool {
        if case .external = config.type { return true }
        let validInterval = config.type == .hlsr ? config.interval == .fifteenMinutes : config.interval == .oneHour
        guard validInterval else { return false }
        let validDynamic = config.scope == .dynamic(.hotAltcoins)
        let legacySingle = allowLegacyDynamic && Self.singleInstrumentID(in: config.scope) != nil
        switch config.type {
        case .sweepReversalShort:
            return validDynamic || legacySingle
        case .emaAltcoinLong:
            return validDynamic || legacySingle
        case .hlsr:
            return validDynamic || legacySingle
        case .external:
            return true
        }
    }

    /// Returns a fixed symbol only for migrating old persisted configurations.
    /// New instances always use a dynamic scan scope.
    private static func singleInstrumentID(in scope: StrategyScope) -> String? {
        guard scope.mode == .single, scope.instrumentIDs.count == 1 else { return nil }
        let instrumentID = scope.instrumentIDs[0].trimmingCharacters(in: .whitespacesAndNewlines)
        return instrumentID.isEmpty ? nil : instrumentID
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
                .filter { Self.supports($0, allowLegacyDynamic: true) }
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
        if self.strategies.isEmpty {
            // A new installation starts with no strategy instances. Strategies
            // are created explicitly by the user in the strategy center.
            Self.persistInitial(directory: directory, encoder: encoder, strategies: self.strategies, statuses: self.statuses, orders: self.orders, fills: self.fills, risk: self.risk)
        } else {
            // A service restart is an explicit safety boundary: strategies must be
            // started manually after the backend becomes available.
            self.strategies = self.strategies.map { config in
                var paused = config
                paused.enabled = false
                return paused
            }
            self.statuses = Dictionary(uniqueKeysWithValues: self.strategies.map { ($0.id, StrategyStatus(id: $0.id, state: .paused)) })
            Self.persistInitial(directory: directory, encoder: encoder, strategies: self.strategies, statuses: self.statuses, orders: self.orders, fills: self.fills, risk: self.risk)
        }
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
    public func allStatuses() -> [StrategyStatus] { strategies.compactMap { statuses[$0.id] } }
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
        case .fourHours:
            hlsrFourHourCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
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
              config.capitalPoolPercent <= 100 else { throw StoreError.unsupported }
        let normalized = Self.canonicalized(config)
        guard !strategies.contains(where: { $0.id == normalized.id }) else { throw StoreError.conflict }
        guard !strategies.contains(where: { $0.type == normalized.type }) else { throw StoreError.conflict }
        strategies.append(normalized)
        statuses[normalized.id] = StrategyStatus(id: normalized.id, state: .paused)
        save()
        return normalized
    }

    public func update(_ config: StrategyConfig) throws -> StrategyConfig {
        guard config.type.hasRuntimeHandler,
              Self.supports(config),
              config.riskPercent > 0,
              config.riskPercent <= config.type.maxRiskPercent,
              config.capitalPoolPercent > 0,
              config.capitalPoolPercent <= 100 else { throw StoreError.unsupported }
        let normalized = Self.canonicalized(config)
        guard let index = strategies.firstIndex(where: { $0.id == normalized.id }) else { throw StoreError.notFound }
        guard !strategies.contains(where: { $0.id != normalized.id && $0.type == normalized.type }) else { throw StoreError.conflict }
        let wasEnabled = strategies[index].enabled
        strategies[index] = normalized
        statusesByInstrument[normalized.id] = [:]
        if let previous = statuses[normalized.id] {
            let lastSignal = !wasEnabled && normalized.enabled ? nil : previous.lastSignal
            statuses[normalized.id] = StrategyStatus(id: previous.id, state: normalized.enabled ? .running : .paused, direction: previous.direction, cooldown: previous.cooldown, pnl: previous.pnl, lastSignal: lastSignal, indicators: previous.indicators)
        } else {
            statuses[normalized.id] = StrategyStatus(id: normalized.id, state: normalized.enabled ? .running : .paused)
        }
        save()
        return normalized
    }

    public func delete(_ id: UUID) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { throw StoreError.notFound }
        guard !strategies[index].enabled else { throw StoreError.running }
        let removed = strategies.remove(at: index)
        statuses.removeValue(forKey: id)
        statusesByInstrument.removeValue(forKey: id)
        // Deleting an instance must not leave a live pending entry that can
        // fill after its strategy/package has been removed. Keep the order in
        // the audit ledger, but make it ineligible for PaperBroker execution.
        orders = orders.map { order in
            guard order.strategyID == id, order.status == "pending" else { return order }
            return PaperOrder(id: order.id, strategyID: order.strategyID,
                              instrumentID: order.instrumentID, side: order.side,
                              quantity: order.quantity, requestedAt: order.requestedAt,
                              fillPrice: order.fillPrice, status: "cancelled",
                              remoteOrderID: order.remoteOrderID, signal: order.signal)
        }
        save()
        return removed
    }

    public func setState(_ id: UUID, running: Bool) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { throw StoreError.notFound }
        guard strategies[index].type.hasRuntimeHandler else { throw StoreError.unsupported }
        if running, strategies[index].scope != .dynamic(.hotAltcoins) {
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
        // 缓存 BTC 1 小时 K 线，供扫顶反转策略的 BTC<SMA200 门控使用
        if snapshot.instrumentID == "BTC-USDT-SWAP", snapshot.interval == .oneHour {
            btcHourlyCandles = snapshot.candles
        }
        switch snapshot.interval {
        case .oneHour:
            structureCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        case .fifteenMinutes:
            confirmationCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        case .fourHours:
            hlsrFourHourCandlesByInstrument[snapshot.instrumentID] = snapshot.candles
        default:
            break
        }
        var changed = false
        var evaluatedStatuses: [StrategyStatus] = []
        for config in strategies where config.scope.matches(snapshot.instrumentID, contracts: contracts) {
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
            if config.type == .hlsr {
                guard snapshot.interval == .fifteenMinutes else {
                    let current = statusesByInstrument[config.id]?[snapshot.instrumentID]
                        ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                    evaluatedStatuses.append(current)
                    continue
                }
                let previous = statusesByInstrument[config.id]?[snapshot.instrumentID]
                    ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                let next = engine.evaluateHLSR(config: config,
                                               candles15m: snapshot.candles,
                                               fourHourCandles: hlsrFourHourCandlesByInstrument[snapshot.instrumentID] ?? [],
                                               previous: previous)
                statusesByInstrument[config.id, default: [:]][snapshot.instrumentID] = next
                evaluatedStatuses.append(next)
                if statuses[config.id] != next {
                    statuses[config.id] = next
                    changed = true
                }
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
            if config.type == .emaAltcoinLong {
                guard snapshot.interval == .oneHour else {
                    let current = statusesByInstrument[config.id]?[snapshot.instrumentID]
                        ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                    evaluatedStatuses.append(current)
                    continue
                }
                let previous = statusesByInstrument[config.id]?[snapshot.instrumentID]
                    ?? StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
                let next = engine.evaluateEmaAltcoinLong(config: config, candles: snapshot.candles, previous: previous, btcCandles: btcHourlyCandles)
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

    private struct StoreFile: Codable {
        let schemaVersion: Int
        let strategies: [StrategyConfig]
        let statuses: [String: StrategyStatus]
        let orders: [PaperOrder]
        let fills: [PaperFill]
        let risk: RiskSnapshot
    }

    private struct StrategyFile: Codable {
        let schemaVersion: Int
        let strategies: [StrategyConfig]
        let statuses: [String: StrategyStatus]
    }

    private struct LedgerFile: Codable {
        let schemaVersion: Int
        let orders: [PaperOrder]
        let fills: [PaperFill]
        let risk: RiskSnapshot
    }

    private static func loadFile(directory: URL, decoder: JSONDecoder) -> StoreFile? {
        let url = directory.appendingPathComponent("paper-state.json")
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? decoder.decode(StoreFile.self, from: data)
    }

    private func save() {
        let file = StoreFile(schemaVersion: 1, strategies: strategies, statuses: Dictionary(uniqueKeysWithValues: statuses.map { ($0.key.uuidString, $0.value) }), orders: orders, fills: fills, risk: risk)
        guard let data = try? encoder.encode(file) else { return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try Self.writeAtomic(data, to: directory.appendingPathComponent("paper-state.json"))
            let strategyData = try encoder.encode(StrategyFile(schemaVersion: 1, strategies: strategies, statuses: Dictionary(uniqueKeysWithValues: statuses.map { ($0.key.uuidString, $0.value) })))
            try Self.writeAtomic(strategyData, to: directory.appendingPathComponent("strategies.json"))
            let ledgerData = try encoder.encode(LedgerFile(schemaVersion: 1, orders: orders, fills: fills, risk: risk))
            try Self.writeAtomic(ledgerData, to: directory.appendingPathComponent("paper-ledger.json"))
            let audit = directory.appendingPathComponent("audit.jsonl")
            let line = "{\"timestamp\":\"\(ISO8601DateFormatter().string(from: .now))\",\"event\":\"state_saved\"}\n"
            if let handle = try? FileHandle(forWritingTo: audit) { try handle.seekToEnd(); try handle.write(contentsOf: Data(line.utf8)); try handle.close() }
            else { try Data(line.utf8).write(to: audit, options: .atomic) }
        } catch { }
    }

    private static func writeAtomic(_ data: Data, to url: URL) throws {
        let tmp = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).tmp")
        try data.write(to: tmp, options: .atomic)
        if FileManager.default.fileExists(atPath: url.path) { try FileManager.default.removeItem(at: url) }
        try FileManager.default.moveItem(at: tmp, to: url)
    }

    private static func persistInitial(directory: URL, encoder: JSONEncoder, strategies: [StrategyConfig], statuses: [UUID: StrategyStatus], orders: [PaperOrder], fills: [PaperFill], risk: RiskSnapshot) {
        let file = StoreFile(schemaVersion: 1, strategies: strategies, statuses: Dictionary(uniqueKeysWithValues: statuses.map { ($0.key.uuidString, $0.value) }), orders: orders, fills: fills, risk: risk)
        guard let data = try? encoder.encode(file) else { return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try writeAtomic(data, to: directory.appendingPathComponent("paper-state.json"))
            try writeAtomic(try encoder.encode(StrategyFile(schemaVersion: 1, strategies: strategies, statuses: file.statuses)), to: directory.appendingPathComponent("strategies.json"))
            try writeAtomic(try encoder.encode(LedgerFile(schemaVersion: 1, orders: orders, fills: fills, risk: risk)), to: directory.appendingPathComponent("paper-ledger.json"))
        } catch { }
    }

    public enum StoreError: LocalizedError {
        case conflict, notFound, running, unsupported

        public var errorDescription: String? {
            switch self {
            case .conflict: return "每种策略规则只允许创建一个实例"
            case .notFound: return "策略实例不存在"
            case .running: return "策略运行中，请先停止策略后再删除"
            case .unsupported: return "当前仅支持 1 小时山寨币策略；运行时扫描动态合规币种池，每次只允许一个币种下单或持仓，单笔风险不得超过规则上限"
        }
        }
    }
}

/// Cache TTLs for `MarketDataService`. Every uncached read spawns an ATK CLI
/// process (or hits OKX REST), so short windows collapse bursts from several
/// stream clients and the auxiliary poller into one call per window.
public struct MarketCacheTTL: Sendable {
    public var ticker: TimeInterval
    public var snapshot: TimeInterval
    public var contracts: TimeInterval
    public var account: TimeInterval
    public var positions: TimeInterval
    public var orders: TimeInterval
    public var orderBook: TimeInterval
    public var trades: TimeInterval

    public init(ticker: TimeInterval = 2, snapshot: TimeInterval = 60, contracts: TimeInterval = 60, account: TimeInterval = 5, positions: TimeInterval = 5, orders: TimeInterval = 5, orderBook: TimeInterval = 3, trades: TimeInterval = 3) {
        self.ticker = ticker; self.snapshot = snapshot; self.contracts = contracts; self.account = account
        self.positions = positions; self.orders = orders; self.orderBook = orderBook; self.trades = trades
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
    private let marketSocket: OKXPublicClient
    /// REST client for order book and public trade polling.
    private let rest: OKXPublicClient
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

    private var orderBookCache: [String: Fresh<OrderBookSnapshot>] = [:]
    private var orderBookTasks: [String: Task<OrderBookSnapshot, Error>] = [:]

    private var tradesCache: [String: Fresh<[TradeTick]>] = [:]
    private var tradesTasks: [String: Task<[TradeTick], Error>] = [:]

    public init(client: ATKClient = ATKClient(), marketSocket: OKXPublicClient = OKXPublicClient(), rest: OKXPublicClient = OKXPublicClient(), ttl: MarketCacheTTL = MarketCacheTTL()) {
        self.client = client
        self.marketSocket = marketSocket
        self.rest = rest
        self.ttl = ttl
    }

    public func snapshot(instrumentID: String, interval: KlineInterval) async throws -> MarketSnapshot {
        let cacheKey = key(instrumentID, interval)
        // Historical candles are a prewarm only. Once a subscription has
        // loaded them, the WSS stream owns all subsequent candle updates; the
        // TTL re-arms a REST reload only to heal gaps after a stream outage.
        if let cached = cache[cacheKey], let fetchedAt = cacheFetchedAt[cacheKey], fetchedAt.addingTimeInterval(ttl.snapshot) > Date() {
            return cached
        }
        if let task = cacheTasks[cacheKey] { return try await task.value }
        let task = Task { [client] in
            let candles = try await client.marketCandles(instrumentID: instrumentID, interval: interval)
            let ticker = try? await client.marketTicker(instrumentID: instrumentID)
            return MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: candles, ticker: ticker)
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
            let merged = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: mergedCandles, ticker: value.ticker ?? cache[cacheKey]?.ticker)
            cache[cacheKey] = merged
            cacheFetchedAt[cacheKey] = Date()
            cacheTasks[cacheKey] = nil
            if let ticker = merged.ticker { tickerCache[instrumentID] = Fresh(value: ticker, fetchedAt: Date()) }
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

    public func orderBook(instrumentID: String) async throws -> OrderBookSnapshot {
        if let cached = orderBookCache[instrumentID], cached.isValid(ttl: ttl.orderBook) { return cached.value }
        if let task = orderBookTasks[instrumentID] { return try await task.value }
        let task = Task { [rest] in try await rest.orderBook(instrumentID: instrumentID) }
        orderBookTasks[instrumentID] = task
        do {
            let value = try await task.value
            orderBookCache[instrumentID] = Fresh(value: value, fetchedAt: Date())
            orderBookTasks[instrumentID] = nil
            return value
        } catch {
            orderBookTasks[instrumentID] = nil
            throw error
        }
    }

    public func trades(instrumentID: String) async throws -> [TradeTick] {
        if let cached = tradesCache[instrumentID], cached.isValid(ttl: ttl.trades) { return cached.value }
        if let task = tradesTasks[instrumentID] { return try await task.value }
        let task = Task { [rest] in try await rest.trades(instrumentID: instrumentID) }
        tradesTasks[instrumentID] = task
        do {
            let value = try await task.value
            tradesCache[instrumentID] = Fresh(value: value, fetchedAt: Date())
            tradesTasks[instrumentID] = nil
            return value
        } catch {
            tradesTasks[instrumentID] = nil
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
    public func cancelLiveOrder(instrumentID: String, orderID: String) async throws { try await client.cancelLiveSwapOrder(instrumentID: instrumentID, orderID: orderID); invalidateAccountState() }
    public func cancelDemoOrder(instrumentID: String, orderID: String) async throws { try await client.cancelDemoSwapOrder(instrumentID: instrumentID, orderID: orderID); invalidateAccountState() }
    public func closeLivePosition(instrumentID: String, positionSide: String?) async throws { try await client.closeLiveSwapPosition(instrumentID: instrumentID, positionSide: positionSide); invalidateAccountState() }
    public func closeDemoPosition(instrumentID: String, positionSide: String?) async throws { try await client.closeDemoSwapPosition(instrumentID: instrumentID, positionSide: positionSide); invalidateAccountState() }

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

    public func candleUpdates(instrumentID: String, interval: KlineInterval) -> AsyncStream<Candle> {
        // OKXPublicClient owns the socket lifecycle and reconnects with
        // exponential backoff after a disconnect. Keeping this as a direct
        // pass-through is intentional: no timer or REST fallback should run
        // in the real-time candle path.
        marketSocket.candleUpdates(instrumentID: instrumentID, interval: interval)
    }
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
        cache[cacheKey] = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: values, ticker: previous.ticker, updatedAt: .now)
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
    private var runtimeLogs: [RuntimeLog] = []
    private let runtimeLogURL: URL
    private var loggedSignalIDs: Set<UUID> = []
    private var loggedFillIDs: Set<UUID> = []
    private var liveTradingEnabled = false
    private var contractUniverse: [ContractMarket] = []
    private var globalRiskTripHandled = false
    private var globalRiskRemoteCleanupComplete = false
    private var submittedExitPositionIDs: Set<String> = []
    /// The HLSR exit state machine is persisted independently from the paper
    /// ledger.  A manager is only advanced after a position snapshot confirms
    /// the reduce-only fill, so a rejected or partially filled order cannot
    /// skip a target or silently lose the breakeven/trailing stop.
    private struct HLSRExitRuntimeState: Codable, Sendable {
        var manager: HLSRPositionManager
        var remoteOrderID: String?
        var submittedAt: Date?
        var lastExitPrice: Decimal?
        var realizedExitPnL: Decimal?
    }
    private struct HLSRExitStateFile: Codable {
        let schemaVersion: Int
        let states: [String: HLSRExitRuntimeState]
    }
    private var hlsrExitStates: [String: HLSRExitRuntimeState]
    private let hlsrExitStateURL: URL
    /// Serialize exit monitors across awaits to keep an older position read
    /// from claiming another leg while the first submit is still suspended.
    private var strategyExitMonitorInFlight = false
    private struct PendingRemoteExit {
        let strategyID: UUID
        let instrumentID: String
        let quantity: Decimal
        let entryPrice: Decimal
        let exitPrice: Decimal
        let side: String
        let contractValue: Decimal
        let reservedNotional: Decimal
        let exitRisk: Decimal
        var realizedPnL: Decimal? = nil
    }
    private var pendingRemoteExits: [String: PendingRemoteExit] = [:]

    /// Computes the net result of one confirmed HLSR reduce-only fill. The
    /// current ATK order snapshot does not expose per-fill trade reports, so
    /// the manager's claimed execution price is the deterministic price used
    /// for the strategy leg. Accumulating by the observed quantity keeps
    /// TP1/TP2/TP3 accounting correct across partial position snapshots.
    private func hlsrLegRealizedPnL(quantity: Decimal, entryPrice: Decimal,
                                    exitPrice: Decimal, side: String,
                                    contractValue: Decimal,
                                    feeRate: Decimal? = nil) -> Decimal {
        guard quantity.isFinite, quantity > 0,
              entryPrice.isFinite, entryPrice > 0,
              exitPrice.isFinite, exitPrice > 0,
              contractValue.isFinite, contractValue > 0 else { return 0 }
        let direction: Decimal = side.lowercased() == "short" ? -1 : 1
        let gross = (exitPrice - entryPrice) * quantity * contractValue * direction
        let effectiveFeeRate = feeRate ?? broker.feeRate
        let fee = abs(exitPrice * quantity * contractValue) * effectiveFeeRate
        return gross - fee
    }
    /// Reservations created by the manual live/demo order endpoints. Strategy
    /// orders have their own position lifecycle; manual orders need a small
    /// reconciliation ledger so a successful submit cannot permanently consume
    /// the global notional limit, while a still-open remote position remains
    /// protected by the reservation.
    private struct RemoteReservation {
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
        /// Set while the remote command is suspended.  Reconciliation must
        /// carry this amount forward even when the exchange has not published
        /// the newly submitted order yet.
        let inFlight: Bool

        init(instrumentID: String, notional: Decimal, createdAt: Date = .now,
             strategyID: UUID? = nil, margin: Decimal? = nil,
             riskAmount: Decimal = 0, closedPosition: Bool = false,
             inFlight: Bool = false) {
            self.instrumentID = instrumentID
            self.notional = notional
            self.createdAt = createdAt
            self.strategyID = strategyID
            self.margin = margin ?? notional
            self.riskAmount = riskAmount
            self.closedPosition = closedPosition
            self.inFlight = inFlight
        }
    }
    private var remoteReservations: [String: RemoteReservation] = [:]
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
                                            maxConcurrentPositions: Int? = nil) async -> (RiskDecision, String?) {
        return await withRiskReservationMutation {
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
                strategyID: strategyID, margin: notional,
                riskAmount: riskAmount, closedPosition: strategyID != nil
            )
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
        }
    }

    /// Registers exposure before awaiting a remote order command.  Actors may
    /// re-enter at that await, so account reconciliation must see this token
    /// even if the exchange has not returned an order ID yet.
    private func beginRemoteSubmission(instrumentID: String, notional: Decimal,
                                       strategyID: UUID? = nil, margin: Decimal? = nil,
                                       riskAmount: Decimal = 0,
                                       closedPosition: Bool = false) -> String {
        let token = "pending-\(UUID().uuidString)"
        remoteReservations[token] = RemoteReservation(
            instrumentID: instrumentID,
            notional: notional,
            strategyID: strategyID,
            margin: margin,
            riskAmount: riskAmount,
            closedPosition: closedPosition,
            inFlight: true
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
                inFlight: false
            )
        }
    }

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
        self.runtimeLogs = Self.loadRuntimeLogs(from: self.runtimeLogURL)
        self.hlsrExitStateURL = directory.appendingPathComponent("hlsr-exit-state.json")
        self.hlsrExitStates = Self.loadHLSRExitStates(from: self.hlsrExitStateURL)
    }

    private static func loadHLSRExitStates(from url: URL) -> [String: HLSRExitRuntimeState] {
        guard let data = try? Data(contentsOf: url),
              let file = try? JSONDecoder().decode(HLSRExitStateFile.self, from: data),
              file.schemaVersion == 1 else { return [:] }
        return file.states
    }

    private func saveHLSRExitStates() {
        let file = HLSRExitStateFile(schemaVersion: 1, states: hlsrExitStates)
        guard let data = try? JSONEncoder().encode(file) else { return }
        do {
            try FileManager.default.createDirectory(at: hlsrExitStateURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            let tmp = hlsrExitStateURL.deletingLastPathComponent().appendingPathComponent(".hlsr-exit-state.json.tmp")
            try data.write(to: tmp, options: .atomic)
            if FileManager.default.fileExists(atPath: hlsrExitStateURL.path) { try FileManager.default.removeItem(at: hlsrExitStateURL) }
            try FileManager.default.moveItem(at: tmp, to: hlsrExitStateURL)
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
        await riskEngine.restore(await paper.riskSnapshot())
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
            return config.strategyIdentifier == normalized
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

    public func createStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        let created = try await paper.create(config)
        _ = await riskEngine.registerStrategy(created.id, allocationPercent: Decimal(created.capitalPoolPercent))
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("策略已创建：\(created.name)，规则 \(Self.strategyRuleName(created.type))")
        return created
    }

    public func updateStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        let updated = try await paper.update(config)
        _ = await riskEngine.registerStrategy(updated.id, allocationPercent: Decimal(updated.capitalPoolPercent))
        await paper.setRisk(await riskEngine.snapshot())
        appendLog("策略已更新：\(updated.name)")
        return updated
    }

    public func deleteStrategy(_ id: UUID) async throws -> StrategyConfig {
        await restoreRiskIfNeeded()
        // Remove the broker's in-memory pending entry and release its
        // reservation before deleting the persisted strategy instance.
        await broker.cancelPendingOrders(strategyID: id)
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
        appendLog(snapshot.killSwitch ? "账户风控仍锁存：只能在新日历日手动复位" : "账户风控已手动复位", level: snapshot.killSwitch ? "warning" : "risk")
        return snapshot
    }

    public func recordLog(_ log: RuntimeLog) {
        appendLog(log)
    }

    private static func strategyRuleName(_ type: StrategyType) -> String {
        switch type {
        case .sweepReversalShort: return "山寨币二次扫顶做空"
        case .emaAltcoinLong: return "双均线交易山寨币做多"
        case .hlsr: return "高位扫顶反转做空"
        case .external(let identifier): return identifier
        }
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
            for order in orders where !["filled", "canceled", "cancelled", "closed"].contains(order.status.lowercased()) {
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
                        try await market.closeDemoPosition(instrumentID: position.instrumentID, positionSide: position.side)
                    } else {
                        try await market.closeLivePosition(instrumentID: position.instrumentID, positionSide: position.side)
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
        // A position can disappear outside this service (manual close,
        // exchange liquidation, or a process restart before the final
        // settlement poll). Once pending exits have been settled, remove any
        // persisted HLSR manager whose position key is no longer live.
        let liveHLSRKeys = Set(positions.filter { abs($0.quantity) > 0 }
            .map { "\($0.id):\($0.instrumentID)" })
        let orphanHLSRKeys = hlsrExitStates.keys.filter {
            !liveHLSRKeys.contains($0) && pendingRemoteExits[$0] == nil
        }
        if !orphanHLSRKeys.isEmpty {
            for key in orphanHLSRKeys { hlsrExitStates.removeValue(forKey: key) }
            saveHLSRExitStates()
        }
        guard !positions.isEmpty else { return }
        let orders = await paper.allOrders()
        // The exit state machine keeps the remote order id so a canceled or
        // rejected reduce-only leg can be released for retry.  If this read
        // fails, the state machine falls back to its bounded stale-order
        // timeout rather than assuming the order filled.
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
            let hlsrKey = "\(position.id):\(position.instrumentID)"
            // After a restart PaperTradingStore intentionally pauses strategy
            // instances and clears aggregate status. A persisted HLSR manager
            // still owns the live protective signal and must keep running
            // until the remote position is flat.
            let signal = order.signal ?? hlsrExitStates[hlsrKey]?.manager.signal ?? status?.lastSignal
            let positionCandle = candleInstrumentID == position.instrumentID ? candle : nil
            let positionFallback = candleInstrumentID == position.instrumentID ? fallbackPrice : nil
            let price = position.markPrice ?? positionFallback ?? order.fillPrice
            guard let price else { continue }
            if config.type == .hlsr, let signal {
                await enforceHLSRExit(position: position, config: config, signal: signal,
                                      order: order, orders: orders, price: price,
                                      timestamp: timestamp, account: account, candle: positionCandle,
                                      remoteOrders: remoteOrders)
                continue
            }
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
            let timedOut = timestamp.timeIntervalSince(order.requestedAt) >= 96 * 3600
            guard stopHit || takeHit || timedOut else { continue }
            let positionKey = "\(position.id):\(position.instrumentID)"
            guard submittedExitPositionIDs.insert(positionKey).inserted else { continue }
            let side = isShort ? "buy" : "sell"
            let request = LiveOrderRequest(instrumentID: position.instrumentID, side: side, orderType: "market", quantity: abs(position.quantity), marginMode: "cross", reduceOnly: true)
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
                    instrumentID: position.instrumentID,
                    quantity: abs(position.quantity),
                    entryPrice: position.entryPrice,
                    exitPrice: price,
                    side: isShort ? "short" : "long",
                    contractValue: spec.contractValue,
                    reservedNotional: reservedNotional,
                    exitRisk: exitRisk
                )
                appendLog("策略平仓已提交，等待成交确认：\(config.name) / \(position.instrumentID) / \(result.orderID) / \(stopHit ? "止损" : takeHit ? "止盈" : "96根时间离场")", level: "exit")
                await market.invalidateAccountState()
            } catch {
                submittedExitPositionIDs.remove(positionKey)
                appendLog("策略平仓失败：\(config.name) / \(position.instrumentID)：\(error.localizedDescription)", level: "warning")
            }
        }
    }

    /// Reduce-only exits are asynchronous. Keep the strategy pool reserved
    /// HLSR's three exits are submitted as separate reduce-only orders. The
    /// first target never closes the full position, TP1 moves the stop to
    /// entry, and TP2 enables a two-bar high trailing stop. This path is also
    /// used for demo orders because OKX's single native TP field cannot
    /// represent the strategy's partial plan.
    private func enforceHLSRExit(position: PositionSnapshot, config: StrategyConfig,
                                 signal: StrategySignal, order: PaperOrder,
                                 orders: [PaperOrder], price: Decimal,
                                 timestamp: Date, account: AccountOverview,
                                 candle: Candle? = nil,
                                 remoteOrders: [OrderSnapshot]? = nil) async {
        let key = "\(position.id):\(position.instrumentID)"
        let spec: SwapInstrumentSpec
        do {
            spec = try await market.instrumentSpec(instrumentID: position.instrumentID)
            guard spec.isLiveUSDTLinearSwap,
                  spec.accepts(contractQuantity: abs(position.quantity)) else {
                throw ATKError.invalidOrder("HLSR 持仓数量不符合合约规格")
            }
        } catch {
            appendLog("HLSR 退出规格读取失败：\(position.instrumentID)：\(error.localizedDescription)", level: "warning")
            return
        }

        var runtime = hlsrExitStates[key]
        if runtime == nil {
            let requestedQuantity = orders.reversed().first {
                $0.instrumentID == position.instrumentID &&
                    $0.strategyID == config.id &&
                    $0.side.lowercased() == "short" &&
                    $0.quantity > 0
            }?.quantity ?? abs(position.quantity)
            guard let manager = try? HLSRPositionManager(
                strategyID: config.id,
                instrumentID: position.instrumentID,
                signal: signal,
                spec: spec,
                requestedQuantity: requestedQuantity,
                actualFilledQuantity: abs(position.quantity),
                actualFillPrice: position.entryPrice,
                openedAt: order.requestedAt
            ) else { return }
            runtime = HLSRExitRuntimeState(
                manager: manager, remoteOrderID: nil,
                submittedAt: nil, lastExitPrice: nil
            )
            hlsrExitStates[key] = runtime!
            saveHLSRExitStates()
        }
        guard var runtime else { return }
        var manager = runtime.manager
        let hlsrFeeRate = Decimal(config.parameters["feeRateOneWay"] ?? 0.0005)

        // A submitted order is only an intent. Target progression,
        // breakeven and trailing are advanced after the position snapshot
        // confirms that the reduce-only quantity was actually filled.
        if let pending = manager.pendingLeg {
            let quantityBeforeConfirmation = manager.remainingQuantity
            let confirmed = manager.confirm(pending.id, position: position)
            let observedReduction = max(0, quantityBeforeConfirmation - manager.remainingQuantity)
            // Accumulate every observed reduction, including partial fills of
            // the final leg. The pending remote record below is recomputed
            // from this running total so a later flat snapshot cannot double
            // count a partial fill.
            if observedReduction > 0 {
                let legPnL = hlsrLegRealizedPnL(
                    quantity: observedReduction,
                    entryPrice: manager.entryPrice,
                    exitPrice: pending.price,
                    side: "short",
                    contractValue: spec.contractValue,
                    feeRate: hlsrFeeRate
                )
                runtime.realizedExitPnL = (runtime.realizedExitPnL ?? 0) + legPnL
            }
            if var pendingRemote = pendingRemoteExits[key] {
                pendingRemote.realizedPnL = (runtime.realizedExitPnL ?? 0) + hlsrLegRealizedPnL(
                    quantity: manager.remainingQuantity,
                    entryPrice: manager.entryPrice,
                    exitPrice: pendingRemote.exitPrice,
                    side: pendingRemote.side,
                    contractValue: pendingRemote.contractValue,
                    feeRate: hlsrFeeRate
                )
                pendingRemoteExits[key] = pendingRemote
            }
            runtime.manager = manager
            if confirmed {
                runtime.remoteOrderID = nil
                runtime.submittedAt = nil
                hlsrExitStates[key] = runtime
                saveHLSRExitStates()
                if manager.isFlat {
                    pendingRemoteExits[key] = PendingRemoteExit(
                        strategyID: config.id,
                        instrumentID: position.instrumentID,
                        quantity: manager.initialQuantity,
                        entryPrice: manager.entryPrice,
                        exitPrice: runtime.lastExitPrice ?? price,
                        side: "short",
                        contractValue: spec.contractValue,
                        reservedNotional: abs(spec.notional(forContracts: manager.initialQuantity, price: manager.entryPrice)),
                        exitRisk: abs(manager.initialStop - manager.entryPrice) *
                            manager.initialQuantity * spec.contractValue,
                        realizedPnL: runtime.realizedExitPnL
                    )
                }
            } else {
                hlsrExitStates[key] = runtime
                saveHLSRExitStates()
                // Market reduce-only orders normally settle immediately. A
                // stale/rejected order must eventually be released so a
                // later snapshot can retry the same intent.
                let failedTerminalStates: Set<String> = ["canceled", "cancelled", "rejected", "expired", "failed"]
                let remoteOrder = runtime.remoteOrderID.flatMap { id in
                    remoteOrders?.first { $0.id == id }
                }
                let failedRemoteOrder = remoteOrder.map {
                    failedTerminalStates.contains($0.status.lowercased())
                } ?? false
                // An absent order row is ambiguous on OKX: the order may be
                // filled, still propagating, or omitted by a paginated
                // snapshot. Retry only on an explicit terminal rejection or
                // cancellation; this fail-closed rule prevents duplicate
                // reduce-only orders after a transient REST omission.
                if failedRemoteOrder {
                    _ = manager.fail(pending.id)
                    pendingRemoteExits.removeValue(forKey: key)
                    runtime.manager = manager
                    runtime.remoteOrderID = nil
                    runtime.submittedAt = nil
                    hlsrExitStates[key] = runtime
                    saveHLSRExitStates()
                } else {
                    return
                }
            }
        }
        guard !manager.isFlat, manager.pendingLeg == nil else { return }

        // Stops and targets may react to an opening update or intrabar
        // high/low; the manager only permits invalidation on a confirmed close.
        guard let intent = manager.evaluate(
            candle: candle, price: price, position: position, now: timestamp
        ), let leg = manager.claim(intent, now: timestamp) else { return }
        runtime.manager = manager
        runtime.lastExitPrice = intent.price
        runtime.submittedAt = .now
        hlsrExitStates[key] = runtime
        saveHLSRExitStates()

        let request = LiveOrderRequest(
            instrumentID: position.instrumentID,
            side: "buy",
            orderType: "market",
            quantity: leg.quantity,
            marginMode: "cross",
            reduceOnly: true
        )
        do {
            let result: LiveOrderCommandResult
            if account.mode == .paper {
                result = try await market.placeDemoOrder(request)
            } else {
                result = try await market.placeLiveOrder(request)
            }
            runtime.remoteOrderID = result.orderID
            runtime.manager = manager
            hlsrExitStates[key] = runtime
            saveHLSRExitStates()
            // Keep the reservation/fill settlement record before the remote
            // snapshot can publish the now-flat position.  A final leg may
            // be acknowledged before the next position poll, so waiting for
            // manager.confirm(flat) here would lose the settlement event.
            if leg.expectedRemainingQuantity <= 0 {
                pendingRemoteExits[key] = PendingRemoteExit(
                    strategyID: config.id,
                    instrumentID: position.instrumentID,
                    quantity: manager.initialQuantity,
                    entryPrice: manager.entryPrice,
                    exitPrice: intent.price,
                    side: "short",
                    contractValue: spec.contractValue,
                    reservedNotional: abs(spec.notional(forContracts: manager.initialQuantity, price: manager.entryPrice)),
                    exitRisk: abs(manager.initialStop - manager.entryPrice) *
                        manager.initialQuantity * spec.contractValue,
                    realizedPnL: (runtime.realizedExitPnL ?? 0) + hlsrLegRealizedPnL(
                        quantity: leg.quantity,
                        entryPrice: manager.entryPrice,
                        exitPrice: intent.price,
                        side: "short",
                        contractValue: spec.contractValue,
                        feeRate: hlsrFeeRate
                    )
                )
            }
            await market.invalidateAccountState()
            appendLog(
                "HLSR \(config.name) \(position.instrumentID) 已提交 \(intent.reason.rawValue)，数量 \(leg.quantity)，订单 \(result.orderID)",
                level: "exit"
            )
        } catch {
            _ = manager.fail(leg.id)
            // The final-leg pre-settlement record is valid only after the
            // reduce-only order was accepted. A rejection must remain
            // retryable and must not settle a phantom flat position later.
            pendingRemoteExits.removeValue(forKey: key)
            runtime.manager = manager
            runtime.remoteOrderID = nil
            runtime.submittedAt = nil
            hlsrExitStates[key] = runtime
            saveHLSRExitStates()
            appendLog(
                "HLSR 分批退出失败：\(config.name) / \(position.instrumentID)：\(error.localizedDescription)",
                level: "warning"
            )
        }
    }

    /// until a later position snapshot confirms that the exit completed.
    private func settleCompletedRemoteExits(positions: [PositionSnapshot], timestamp: Date) async {
        await withRiskReservationMutation {
            // Claim completed exits before the first RiskEngine await. The
            // mutation gate keeps the claim and release atomic with generic
            // reconciliation and new order authorization.
            var completed: [(String, PendingRemoteExit)] = []
            for (key, pending) in pendingRemoteExits {
                guard !positions.contains(where: { $0.instrumentID == pending.instrumentID && abs($0.quantity) > 0 }) else { continue }
                pendingRemoteExits.removeValue(forKey: key)
                submittedExitPositionIDs.remove(key)
                hlsrExitStates.removeValue(forKey: key)
                let reservationKeys = remoteReservations.compactMap { reservationKey, reservation in
                    reservation.strategyID == pending.strategyID &&
                        reservation.instrumentID == pending.instrumentID &&
                        !reservation.inFlight ? reservationKey : nil
                }
                for reservationKey in reservationKeys { remoteReservations.removeValue(forKey: reservationKey) }
                completed.append((key, pending))
            }
            for (_, pending) in completed {
                let direction: Decimal = pending.side == "short" ? -1 : 1
                let fallbackRealized = (pending.exitPrice - pending.entryPrice) * pending.quantity * pending.contractValue * direction
                    - abs(pending.exitPrice * pending.quantity * pending.contractValue) * broker.feeRate
                let realized = pending.realizedPnL ?? fallbackRealized
                await riskEngine.recordStrategyRealized(realized, strategyID: pending.strategyID, now: timestamp)
                await riskEngine.release(instrumentID: pending.instrumentID, notional: pending.reservedNotional, strategyID: pending.strategyID, margin: pending.reservedNotional, riskAmount: pending.exitRisk, closedPosition: true)
                if let config = await paper.allStrategies().first(where: { $0.id == pending.strategyID }), config.type == .hlsr {
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
        let terminal: Set<String> = ["filled", "canceled", "cancelled", "rejected", "expired", "failed", "closed"]
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
        for order in orders where !terminal.contains(order.status.lowercased()) {
            let notional: Decimal?
            if let reserved = remoteReservations[order.id]?.notional, reserved.isFinite, reserved > 0 {
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
        await withRiskReservationMutation {
            guard generation == reconciliationGeneration else { return }
            var releases: [RemoteReservation] = []
            for (orderID, reservation) in remoteReservations {
                guard !reservation.inFlight else { continue }
                if let strategyID = reservation.strategyID,
                   pendingRemoteExits.values.contains(where: { $0.strategyID == strategyID && $0.instrumentID == reservation.instrumentID }) {
                    continue
                }
                let order = orders.first(where: { $0.id == orderID })
                let hasPosition = positions.contains {
                    $0.instrumentID == reservation.instrumentID && abs($0.quantity) > 0
                }
                let shouldRelease: Bool
                if let order {
                    shouldRelease = terminal.contains(order.status.lowercased()) && !hasPosition
                } else {
                    shouldRelease = !hasPosition && Date().timeIntervalSince(reservation.createdAt) >= 30
                }
                guard shouldRelease else { continue }
                remoteReservations.removeValue(forKey: orderID)
                releases.append(reservation)
            }
            for reservation in releases {
                await riskEngine.release(
                    instrumentID: reservation.instrumentID,
                    notional: reservation.notional,
                    strategyID: reservation.strategyID,
                    margin: reservation.margin,
                    riskAmount: reservation.riskAmount,
                    closedPosition: reservation.closedPosition
                )
            }

            // Include reservations created after the remote snapshot. The
            // exchange view may lag a just-submitted order, but local risk may
            // never be reset below that in-flight authorization.
            for (orderID, reservation) in remoteReservations {
                if reservation.inFlight || orders.allSatisfy({ $0.id != orderID }) {
                    guard reservation.inFlight || Date().timeIntervalSince(reservation.createdAt) < 30 else { continue }
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
            return ContractMarket(id: item.id, name: item.name, baseCurrency: item.baseCurrency, quoteCurrency: item.quoteCurrency, last: item.last, changePercent: item.changePercent, volume24h: item.volume24h, category: category, updatedAt: item.updatedAt)
        }.sorted { $0.volume24h > $1.volume24h }
        contractUniverse = enriched
        return enriched
    }

    public func cachedContracts() -> [ContractMarket] { contractUniverse }

    public func account() async throws -> AccountOverview {
        await restoreRiskIfNeeded()
        let value = try await market.account()
        if let equity = value.equityUSD { await riskEngine.synchronizeEquity(equity) }
        try await reconcileRemoteReservations()
        await enforceGlobalRiskIfNeeded()
        // Persist the latest daily baseline/equity even when the loss limit
        // has not tripped, so a service restart cannot silently forget it.
        await paper.setRisk(await riskEngine.snapshot())
        return value
    }

    public func liveStatus() async -> LiveTradingStatus {
        do {
            let account = try await account()
            if account.mode == .live {
                return LiveTradingStatus(mode: .live, profile: account.profile, enabled: liveTradingEnabled, available: true, message: liveTradingEnabled ? "实盘交易已启用" : "实盘账户已连接，等待手动启用")
            }
            return LiveTradingStatus(mode: account.mode, profile: account.profile, enabled: false, available: false, message: account.mode == .paper ? "当前是模拟账户，不能真实下单" : "未连接可下单的实盘 profile")
        } catch {
            return LiveTradingStatus(message: error.localizedDescription)
        }
    }

    public func enableLiveTrading() async throws -> LiveTradingStatus {
        let account = try await account()
        guard account.mode == .live else { throw ATKError.demoProfile(profile: account.profile ?? "unknown") }
        liveTradingEnabled = true
        appendLog("实盘交易已手动启用", level: "warning")
        return await liveStatus()
    }

    public func disableLiveTrading() async -> LiveTradingStatus {
        liveTradingEnabled = false
        appendLog("实盘交易已关闭")
        return await liveStatus()
    }

    private func validatedRemoteOrder(_ request: LiveOrderRequest, ticker: MarketTicker) async throws -> (SwapInstrumentSpec, Decimal) {
        guard request.quantity.isFinite, request.quantity > 0 else {
            throw ATKError.invalidOrder("数量必须是有限的正数")
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

    public func placeLiveOrder(_ request: LiveOrderRequest) async throws -> LiveOrderResult {
        guard liveTradingEnabled else { throw ATKError.liveTradingDisabled }
        let account = try await account()
        guard account.mode == .live else { throw ATKError.demoProfile(profile: account.profile ?? "unknown") }
        let ticker = try await market.ticker(instrumentID: request.instrumentID)
        let (_, notional) = try await validatedRemoteOrder(request, ticker: ticker)
        let (decision, submissionToken) = await authorizeRemoteSubmission(instrumentID: request.instrumentID, notional: notional, reduceOnly: request.reduceOnly)
        guard decision.allowed else { throw ATKError.unavailable(decision.reason ?? "风控拒绝订单") }
        await paper.setRisk(await riskEngine.snapshot())
        do {
            let result = try await market.placeLiveOrder(request)
            await confirmRemoteSubmission(submissionToken, orderID: result.orderID)
            await market.invalidateAccountState()
            await paper.setRisk(await riskEngine.snapshot())
            appendLog("实盘订单已提交 \(request.instrumentID) \(request.side) \(request.quantity)", level: "warning")
            return LiveOrderResult(orderID: result.orderID, clientOrderID: result.clientOrderID, instrumentID: request.instrumentID, side: request.side, orderType: request.orderType, quantity: request.quantity, message: result.message)
        } catch {
            await failRemoteSubmission(submissionToken)
            await paper.setRisk(await riskEngine.snapshot())
            throw error
        }
    }

    public func placePaperOrder(_ request: PaperOrderRequest) async throws -> PaperOrder {
        let account = try await account()
        guard account.mode == .paper else { throw ATKError.unavailable("当前不是 OKX 模拟账户") }
        let ticker = try await market.ticker(instrumentID: request.instrumentID)
        let liveRequest = LiveOrderRequest(instrumentID: request.instrumentID, side: request.side, orderType: "market", quantity: request.quantity, marginMode: "cross", reduceOnly: request.reduceOnly)
        let (_, notional) = try await validatedRemoteOrder(liveRequest, ticker: ticker)
        let (decision, submissionToken) = await authorizeRemoteSubmission(instrumentID: request.instrumentID, notional: notional, reduceOnly: request.reduceOnly)
        guard decision.allowed else { throw ATKError.unavailable(decision.reason ?? "风控拒绝模拟订单") }
        await paper.setRisk(await riskEngine.snapshot())
        do {
            let result = try await market.placeDemoOrder(liveRequest)
            await confirmRemoteSubmission(submissionToken, orderID: result.orderID)
            await market.invalidateAccountState()
            await paper.setRisk(await riskEngine.snapshot())
            let order = PaperOrder(strategyID: UUID(), instrumentID: request.instrumentID, side: request.side.lowercased() == "sell" ? "short" : "long", quantity: request.quantity, status: "submitted", remoteOrderID: result.orderID)
            await paper.record(order)
            appendLog("挂单：OKX 模拟盘 \(request.instrumentID) \(request.side) \(request.quantity)，订单 \(result.orderID)", level: "order")
            return order
        } catch {
            await failRemoteSubmission(submissionToken)
            await paper.setRisk(await riskEngine.snapshot())
            throw error
        }
    }

    public func privatePositions() async throws -> [PositionSnapshot] { try await market.privatePositions() }
    public func privateOrders() async throws -> [OrderSnapshot] { try await market.privateOrders() }

    public func strategies() async -> [StrategyConfig] { await paper.allStrategies() }
    public func statuses() async -> [StrategyStatus] { await paper.allStatuses() }
    public func status(for id: UUID) async -> StrategyStatus? { await paper.status(for: id) }
    /// Loads the historical window needed by a background strategy monitor
    /// without evaluating old bars or submitting historical signals.
    public func prewarmStrategyMarket(instrumentID: String, interval: KlineInterval) async throws {
        let snapshot = try await market.snapshot(instrumentID: instrumentID, interval: interval)
        await candles.ingest(snapshot.candles, instrumentID: instrumentID, interval: interval)
        await paper.prewarm(snapshot)
        if interval == .fifteenMinutes {
            // HLSR always has a separately fetched 4H warm-up. Fetching this
            // series explicitly avoids pretending 500 cached 15m bars are
            // enough to supply the required 55 completed 4H bars.
            let fourHour = try await market.snapshot(instrumentID: instrumentID, interval: .fourHours)
            await candles.ingest(fourHour.candles, instrumentID: instrumentID, interval: .fourHours)
            await paper.prewarm(fourHour)
        }
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
            if await submitDemoStrategyOrder(config: config, signal: signal, instrumentID: instrumentID) {
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

    private func submitDemoStrategyOrder(config: StrategyConfig, signal: StrategySignal, instrumentID: String) async -> Bool {
        guard config.enabled else { return false }
        // 同一币种单仓：策略实例对动态扫描池中的每个币种都只允许一笔。
        if let positions = try? await market.privatePositions(),
           positions.contains(where: { $0.instrumentID == instrumentID && abs($0.quantity) > 0 }) {
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
        guard let account = try? await account(), account.mode == .paper else {
            appendLog("策略 \(config.name) 未发送：当前不是 OKX 模拟账户", level: "warning")
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
        // Move protection towards the entry when rounding. This preserves the
        // risk budget and prevents ATR-derived off-tick prices being rejected.
        let stopPrice = signal.stopPrice.flatMap { spec.alignedPrice($0, roundingUp: isLong) }
        // HLSR targets are deliberately managed as three reduce-only legs;
        // attaching only TP1 as an exchange-native full-position TP would
        // silently violate the strategy's 30/30/40 exit plan.
        let takePrice = config.type == .hlsr ? nil : signal.takePrice.flatMap { spec.alignedPrice($0, roundingUp: !isLong) }
        // HLSR deliberately leaves the exchange-native TP field empty: its
        // three reduce-only legs are submitted only after the position
        // snapshot confirms the entry.  Requiring a rounded `takePrice` here
        // would reject every valid HLSR signal before it ever reaches OKX.
        guard signal.stopPrice == nil || stopPrice != nil,
              config.type == .hlsr || signal.takePrice == nil || takePrice != nil else { return false }
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
        let (decision, submissionToken) = await authorizeRemoteSubmission(
            instrumentID: instrumentID, notional: notional,
            strategyID: config.id, poolAllocationPercent: Decimal(config.capitalPoolPercent),
            riskAmount: orderRisk, maxOpenRiskPercent: maxOpenRiskPercent,
            maxConcurrentPositions: maxConcurrent
        )
        guard decision.allowed else {
            appendLog("策略 \(config.name) 被风控拒绝：\(decision.reason ?? "未知原因")", level: "warning")
            return false
        }
        // Write the reservation before the remote submission so a process
        // crash cannot forget the notional cap after OKX has accepted it.
        await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
        let side = signal.type == "entry_short" ? "sell" : "buy"
        let request = LiveOrderRequest(instrumentID: instrumentID, side: side, orderType: "market", quantity: quantity, marginMode: "cross", takeProfitTriggerPrice: takePrice, stopLossTriggerPrice: stopPrice)
        do {
            let result = try await market.placeDemoOrder(request)
            // Keep the reservation under the authenticated order ID until a
            // position disappears.  This covers exchange-native SL/TP exits,
            // which never pass through enforceStrategyExits.
            await confirmRemoteSubmission(submissionToken, orderID: result.orderID)
            await market.invalidateAccountState()
            let order = PaperOrder(strategyID: config.id, instrumentID: instrumentID, side: side == "sell" ? "short" : "long", quantity: quantity, requestedAt: signal.timestamp, status: "submitted", remoteOrderID: result.orderID, signal: signal)
            await paper.record(order)
            await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
            appendLog("挂单：策略 \(config.name) / \(instrumentID) / \(side) \(quantity)，订单 \(result.orderID)", level: "order")
            if signal.stopPrice != nil || signal.takePrice != nil || signal.takePrices != nil { appendLog("策略 \(config.name) 已附带保护规则；分批止盈/保本/跟踪由策略监控处理", level: "info") }
            return true
        } catch {
            await failRemoteSubmission(submissionToken)
            await paper.setRisk(await riskEngine.snapshot(now: signal.timestamp))
            appendLog("策略 \(config.name) OKX 模拟下单失败：\(error.localizedDescription)", level: "warning")
            return false
        }
    }

    public func positions() async -> [PaperPosition] { await broker.allPositions() }
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
        let shouldEvaluate = candle.confirmed || (interval == .fifteenMinutes && configs.contains { $0.enabled && $0.type == .hlsr })
        if shouldEvaluate {
            let current = await candles.values(instrumentID: instrumentID, interval: interval)
            let snapshot = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: current)
            let statuses = await paper.evaluate(snapshot, contracts: contractUniverse)
            logSignals(statuses, configs: configs, instrumentID: instrumentID)
            for status in statuses {
            guard status.state == .running, let signal = status.lastSignal, signal.type.hasPrefix("entry_"), signal.timestamp == candle.timestamp, !submittedSignals.contains(signal.id), let config = configs.first(where: { $0.id == status.id }), config.enabled else { continue }
            // 防御：信号必须就是本标的当前记录的那一个，跨标的信号一律不下单。
            guard await paper.status(for: config.id, instrumentID: instrumentID)?.lastSignal?.id == signal.id else { continue }
                if await submitDemoStrategyOrder(config: config, signal: signal, instrumentID: instrumentID) { recordSubmittedSignal(signal.id) }
            }
            appendLog("\(instrumentID) \(interval.rawValue) K 线收盘，策略状态已更新")
            return statuses
        }
        return await paper.allStatuses()
    }

    public func appendLog(_ message: String, level: String = "info") {
        appendLog(RuntimeLog(level: level, message: message))
    }

    private func appendLog(_ log: RuntimeLog) {
        runtimeLogs.append(log)
        if runtimeLogs.count > Self.runtimeLogMemoryLimit {
            runtimeLogs.removeFirst(runtimeLogs.count - Self.runtimeLogMemoryLimit)
        }
        Self.persistRuntimeLog(log, to: runtimeLogURL)
    }

    private static func loadRuntimeLogs(from url: URL) -> [RuntimeLog] {
        guard let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .utf8) else { return [] }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let logs = text.split(separator: "\n").compactMap { line -> RuntimeLog? in
            guard let lineData = line.data(using: .utf8) else { return nil }
            return try? decoder.decode(RuntimeLog.self, from: lineData)
        }
        return logs.count > runtimeLogMemoryLimit ? Array(logs.suffix(runtimeLogMemoryLimit)) : logs
    }

    private static func persistRuntimeLog(_ log: RuntimeLog, to url: URL) {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(log) else { return }
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: url.path) {
                guard let handle = try? FileHandle(forWritingTo: url) else { return }
                try handle.seekToEnd()
                try handle.write(contentsOf: data)
                try handle.write(contentsOf: Data([0x0A]))
                try handle.close()
            } else {
                var line = Data()
                line.append(data)
                line.append(0x0A)
                try line.write(to: url, options: .atomic)
            }
        } catch {
            // Runtime logging must never interrupt market-data or order handling.
        }
    }
}

public struct TradingHTTPServer {
    public let backend: TradingBackend

    public init(backend: TradingBackend = TradingBackend()) { self.backend = backend }

    public func configure(_ app: HBApplication) throws {
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
            let instrument = params.get("instId") ?? "BTC-USDT-SWAP"
            let interval = KlineInterval(rawValue: params.get("bar") ?? "15m") ?? .fifteenMinutes
            return try request.application.encoder.encode(await backend.marketSnapshot(instrumentID: instrument, interval: interval), from: request)
        }
        app.router.get("api/v1/market/ticker") { [backend] request in
            let instrument = request.uri.queryParameters.get("instId") ?? "BTC-USDT-SWAP"
            return try request.application.encoder.encode(await backend.market.ticker(instrumentID: instrument), from: request)
        }
        app.router.get("api/v1/market/orderbook") { [backend] request in
            let instrument = request.uri.queryParameters.get("instId") ?? "BTC-USDT-SWAP"
            return try request.application.encoder.encode(await backend.market.orderBook(instrumentID: instrument), from: request)
        }
        app.router.get("api/v1/market/trades") { [backend] request in
            let instrument = request.uri.queryParameters.get("instId") ?? "BTC-USDT-SWAP"
            return try request.application.encoder.encode(await backend.market.trades(instrumentID: instrument), from: request)
        }
        app.router.get("api/v1/strategies") { [backend] request in try request.application.encoder.encode(await backend.strategies(), from: request) }
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
            let instance = (try? request.decode(as: StrategyPackageInstanceRequest.self)) ?? StrategyPackageInstanceRequest()
            return try request.application.encoder.encode(try await backend.createStrategyFromPackage(identifier: identifier, request: instance), from: request)
        }
        app.router.get("api/v1/strategies/status") { [backend] request in try request.application.encoder.encode(await backend.statuses(), from: request) }
        app.router.get("api/v1/strategies/capital") { [backend] request in try request.application.encoder.encode(await backend.strategyCapital(), from: request) }
        app.router.get("api/v1/paper/positions") { [backend] request in try request.application.encoder.encode(await backend.positions(), from: request) }
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
        app.router.get("api/v1/paper/ledger") { [backend] _ in
            PaperLedger(orders: await backend.paper.allOrders(), fills: await backend.paper.allFills())
        }
    }
}

public struct PaperLedger: Codable, HBResponseEncodable {
    public let orders: [PaperOrder]
    public let fills: [PaperFill]
    public init(orders: [PaperOrder], fills: [PaperFill]) { self.orders = orders; self.fills = fills }
}
