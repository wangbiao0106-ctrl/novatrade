import Foundation
import Hummingbird
import HummingbirdFoundation
import HummingbirdWebSocket
import ATKGateway
import OKXGateway
import TradingDomain

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

    public init(directory: URL = PaperTradingStore.defaultDirectory()) {
        self.directory = directory
        self.encoder = JSONEncoder()
        self.decoder = JSONDecoder()
        self.strategies = []
        self.statuses = [:]
        self.orders = []
        self.fills = []
        if let file = Self.loadFile(directory: directory, decoder: decoder) {
            self.strategies = file.strategies
            self.statuses = Dictionary(uniqueKeysWithValues: file.statuses.compactMap { entry in
                guard let uuid = UUID(uuidString: entry.key) else { return nil }
                return (uuid, entry.value)
            })
            self.orders = file.orders
            self.fills = file.fills
            self.risk = file.risk
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

    public func prewarm(_ snapshot: MarketSnapshot) {
        if snapshot.instrumentID == "BTC-USDT-SWAP", snapshot.interval == .oneHour {
            btcHourlyCandles = snapshot.candles
        }
    }
    public func riskSnapshot() -> RiskSnapshot { risk }

    public func create(_ config: StrategyConfig) throws -> StrategyConfig {
        guard !strategies.contains(where: { $0.id == config.id }) else { throw StoreError.conflict }
        guard !strategies.contains(where: { $0.type == config.type }) else { throw StoreError.conflict }
        strategies.append(config)
        statuses[config.id] = StrategyStatus(id: config.id, state: .paused)
        save()
        return config
    }

    public func update(_ config: StrategyConfig) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == config.id }) else { throw StoreError.notFound }
        guard !strategies.contains(where: { $0.id != config.id && $0.type == config.type }) else { throw StoreError.conflict }
        let wasEnabled = strategies[index].enabled
        strategies[index] = config
        statusesByInstrument[config.id] = [:]
        if let previous = statuses[config.id] {
            let lastSignal = !wasEnabled && config.enabled ? nil : previous.lastSignal
            statuses[config.id] = StrategyStatus(id: previous.id, state: config.enabled ? .running : .paused, direction: previous.direction, cooldown: previous.cooldown, pnl: previous.pnl, lastSignal: lastSignal, indicators: previous.indicators)
        } else {
            statuses[config.id] = StrategyStatus(id: config.id, state: config.enabled ? .running : .paused)
        }
        save()
        return config
    }

    public func delete(_ id: UUID) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { throw StoreError.notFound }
        guard !strategies[index].enabled else { throw StoreError.running }
        let removed = strategies.remove(at: index)
        statuses.removeValue(forKey: id)
        statusesByInstrument.removeValue(forKey: id)
        save()
        return removed
    }

    public func setState(_ id: UUID, running: Bool) throws -> StrategyConfig {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { throw StoreError.notFound }
        strategies[index].enabled = running
        statusesByInstrument[id] = [:]
        statuses[id] = StrategyStatus(id: id, state: running ? .running : .paused)
        save()
        return strategies[index]
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
        var changed = false
        var evaluatedStatuses: [StrategyStatus] = []
        for config in strategies where config.effectiveScope.matches(snapshot.instrumentID, contracts: contracts) && config.interval == snapshot.interval {
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
        case conflict, notFound, running

        public var errorDescription: String? {
            switch self {
            case .conflict: return "每种策略规则只允许创建一个实例"
            case .notFound: return "策略实例不存在"
            case .running: return "策略运行中，请先停止策略后再删除"
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
    public let candles = CandleStore()
    public let riskEngine: RiskEngine
    public let broker: PaperBroker
    private var submittedSignals: Set<UUID> = []
    /// Insertion order of `submittedSignals`, used to evict the oldest entries
    /// so the dedupe set cannot grow without bound over long sessions.
    private var submittedSignalOrder: [UUID] = []
    private var runtimeLogs: [RuntimeLog] = []
    private var loggedSignalIDs: Set<UUID> = []
    private var loggedFillIDs: Set<UUID> = []
    private var liveTradingEnabled = false
    private var contractUniverse: [ContractMarket] = []

    private static let submittedSignalLimit = 500

    public init(market: MarketDataService = MarketDataService(), paper: PaperTradingStore = PaperTradingStore(), riskEngine: RiskEngine = RiskEngine()) {
        self.market = market
        self.paper = paper
        self.riskEngine = riskEngine
        self.broker = PaperBroker(risk: riskEngine)
    }

    private func recordSubmittedSignal(_ id: UUID) {
        guard !submittedSignals.contains(id) else { return }
        submittedSignals.insert(id)
        submittedSignalOrder.append(id)
        if submittedSignalOrder.count > Self.submittedSignalLimit {
            submittedSignals.remove(submittedSignalOrder.removeFirst())
        }
    }

    public func health() -> ServiceHealth { ServiceHealth() }

    public func createStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        let created = try await paper.create(config)
        appendLog("策略已创建：\(created.name)，规则 \(Self.strategyRuleName(created.type))")
        return created
    }

    public func updateStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        let updated = try await paper.update(config)
        appendLog("策略已更新：\(updated.name)")
        return updated
    }

    public func deleteStrategy(_ id: UUID) async throws -> StrategyConfig {
        let removed = try await paper.delete(id)
        appendLog("策略已删除：\(removed.name)")
        return removed
    }

    public func startStrategy(_ id: UUID) async throws -> StrategyConfig {
        let config = try await paper.setState(id, running: true)
        appendLog("策略已启动：\(config.name)")
        return config
    }

    public func pauseStrategy(_ id: UUID) async throws -> StrategyConfig {
        let config = try await paper.setState(id, running: false)
        appendLog("策略已停止：\(config.name)")
        return config
    }

    public func recordLog(_ log: RuntimeLog) {
        appendLog(log.message, level: log.level)
    }

    private static func strategyRuleName(_ type: StrategyType) -> String {
        switch type {
        case .trendFollowing: return "EMA / 唐奇安趋势过滤"
        case .rsiReversal: return "RSI 超买超卖回穿"
        case .sweepReversalShort: return "高位二次扫顶做空（山寨币）"
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

    public func contracts(forceRefresh: Bool = false) async throws -> [ContractMarket] {
        let all = try await market.contracts(forceRefresh: forceRefresh)
        let byVolume = Set(all.sorted { $0.volume24h > $1.volume24h }.prefix(12).map(\.id))
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
        let value = try await market.account()
        if let equity = value.equityUSD { await riskEngine.synchronizeEquity(equity) }
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

    public func placeLiveOrder(_ request: LiveOrderRequest) async throws -> LiveOrderResult {
        guard liveTradingEnabled else { throw ATKError.liveTradingDisabled }
        guard request.quantity > 0 else { throw ATKError.invalidOrder("数量必须大于 0") }
        let account = try await account()
        guard account.mode == .live else { throw ATKError.demoProfile(profile: account.profile ?? "unknown") }
        let ticker = try await market.ticker(instrumentID: request.instrumentID)
        let notional = abs(request.quantity * ticker.last)
        let decision = await riskEngine.authorize(instrumentID: request.instrumentID, notional: notional, margin: notional, reduceOnly: request.reduceOnly)
        guard decision.allowed else { throw ATKError.unavailable(decision.reason ?? "风控拒绝订单") }
        do {
            let result = try await market.placeLiveOrder(request)
            if request.reduceOnly { await riskEngine.release(instrumentID: request.instrumentID, notional: notional) }
            await market.invalidateAccountState()
            appendLog("实盘订单已提交 \(request.instrumentID) \(request.side) \(request.quantity)", level: "warning")
            return LiveOrderResult(orderID: result.orderID, clientOrderID: result.clientOrderID, instrumentID: request.instrumentID, side: request.side, orderType: request.orderType, quantity: request.quantity, message: result.message)
        } catch {
            await riskEngine.release(instrumentID: request.instrumentID, notional: notional)
            throw error
        }
    }

    public func placePaperOrder(_ request: PaperOrderRequest) async throws -> PaperOrder {
        guard request.quantity > 0 else { throw ATKError.invalidOrder("数量必须大于 0") }
        let account = try await account()
        guard account.mode == .paper else { throw ATKError.unavailable("当前不是 OKX 模拟账户") }
        let ticker = try await market.ticker(instrumentID: request.instrumentID)
        let notional = abs(request.quantity * ticker.last)
        let decision = await riskEngine.authorize(instrumentID: request.instrumentID, notional: notional, margin: notional, reduceOnly: request.reduceOnly)
        guard decision.allowed else { throw ATKError.unavailable(decision.reason ?? "风控拒绝模拟订单") }
        let liveRequest = LiveOrderRequest(instrumentID: request.instrumentID, side: request.side, orderType: "market", quantity: request.quantity, marginMode: "cross", reduceOnly: request.reduceOnly)
        do {
            let result = try await market.placeDemoOrder(liveRequest)
            if request.reduceOnly { await riskEngine.release(instrumentID: request.instrumentID, notional: notional) }
            await market.invalidateAccountState()
            let order = PaperOrder(strategyID: UUID(), instrumentID: request.instrumentID, side: request.side.lowercased() == "sell" ? "short" : "long", quantity: request.quantity, status: "submitted", remoteOrderID: result.orderID)
            await paper.record(order)
            appendLog("挂单：OKX 模拟盘 \(request.instrumentID) \(request.side) \(request.quantity)，订单 \(result.orderID)", level: "order")
            return order
        } catch {
            await riskEngine.release(instrumentID: request.instrumentID, notional: notional)
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
    }
    public func marketSnapshot(instrumentID: String, interval: KlineInterval) async throws -> MarketSnapshot {
        let snapshot = try await market.snapshot(instrumentID: instrumentID, interval: interval)
        await candles.ingest(snapshot.candles, instrumentID: instrumentID, interval: interval)
        let statuses = await paper.evaluate(snapshot, contracts: contractUniverse)
        if let latest = snapshot.candles.last(where: { $0.confirmed }) {
            await broker.processNextOpen(instrumentID: instrumentID, candle: latest)
            // Orders fill at this bar's open. Mark the resulting position at
            // the snapshot close so REST refreshes expose current unrealized PnL
            // just like the realtime candle path.
            await broker.mark(instrumentID: instrumentID, price: latest.close, at: latest.timestamp)
            await logNewFills()
        }
        let configs = await paper.allStrategies()
        logSignals(statuses, configs: configs, instrumentID: instrumentID)
        let latestConfirmedTimestamp = snapshot.candles.last(where: { $0.confirmed })?.timestamp
        for status in statuses {
            guard status.state == .running, let signal = status.lastSignal, signal.type.hasPrefix("entry_"), signal.timestamp == latestConfirmedTimestamp, !submittedSignals.contains(signal.id), let config = configs.first(where: { $0.id == status.id }), config.enabled else { continue }
            let quantity = max(Decimal(string: "0.001") ?? 0, Decimal(config.riskPercent) / 100)
            if await submitDemoStrategyOrder(config: config, signal: signal, instrumentID: instrumentID, quantity: quantity) {
                recordSubmittedSignal(signal.id)
            }
        }
        await paper.setRisk(await broker.riskSnapshot())
        return snapshot
    }

    private func submitDemoStrategyOrder(config: StrategyConfig, signal: StrategySignal, instrumentID: String, quantity: Decimal) async -> Bool {
        guard config.enabled, quantity > 0 else { return false }
        guard let account = try? await account(), account.mode == .paper else {
            appendLog("策略 \(config.name) 未发送：当前不是 OKX 模拟账户", level: "warning")
            return false
        }
        guard let ticker = try? await market.ticker(instrumentID: instrumentID) else { return false }
        let notional = abs(quantity * ticker.last)
        let decision = await riskEngine.authorize(instrumentID: instrumentID, notional: notional, margin: notional)
        guard decision.allowed else {
            appendLog("策略 \(config.name) 被风控拒绝：\(decision.reason ?? "未知原因")", level: "warning")
            return false
        }
        let side = signal.type == "entry_short" ? "sell" : "buy"
        let request = LiveOrderRequest(instrumentID: instrumentID, side: side, orderType: "market", quantity: quantity, marginMode: "cross")
        do {
            let result = try await market.placeDemoOrder(request)
            await market.invalidateAccountState()
            let order = PaperOrder(strategyID: config.id, instrumentID: instrumentID, side: side == "sell" ? "short" : "long", quantity: quantity, requestedAt: signal.timestamp, status: "submitted", remoteOrderID: result.orderID)
            await paper.record(order)
            appendLog("挂单：策略 \(config.name) / \(instrumentID) / \(side) \(quantity)，订单 \(result.orderID)", level: "order")
            return true
        } catch {
            await riskEngine.release(instrumentID: instrumentID, notional: notional)
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
        await market.cacheRealtimeCandle(candle, instrumentID: instrumentID, interval: interval)
        await candles.ingest(candle, instrumentID: instrumentID, interval: interval)
        // A pending order is filled at the next bar's open. WSS publishes an
        // unconfirmed update as soon as that bar starts, so waiting for the
        // close would introduce a full-bar execution delay in paper trading.
        await broker.processNextOpen(instrumentID: instrumentID, candle: candle)
        await broker.mark(instrumentID: instrumentID, price: candle.close, at: candle.timestamp)
        await logNewFills()
        if candle.confirmed {
            let current = await candles.values(instrumentID: instrumentID, interval: interval)
            let snapshot = MarketSnapshot(instrumentID: instrumentID, interval: interval, candles: current)
            let statuses = await paper.evaluate(snapshot, contracts: contractUniverse)
            let configs = await paper.allStrategies()
            logSignals(statuses, configs: configs, instrumentID: instrumentID)
            for status in statuses {
            guard status.state == .running, let signal = status.lastSignal, signal.type.hasPrefix("entry_"), signal.timestamp == candle.timestamp, !submittedSignals.contains(signal.id), let config = configs.first(where: { $0.id == status.id }), config.enabled else { continue }
                let quantity = max(Decimal(string: "0.001") ?? 0, Decimal(config.riskPercent) / 100)
                if await submitDemoStrategyOrder(config: config, signal: signal, instrumentID: instrumentID, quantity: quantity) { recordSubmittedSignal(signal.id) }
            }
            appendLog("\(instrumentID) \(interval.rawValue) K 线收盘，策略状态已更新")
            return statuses
        }
        return await paper.allStatuses()
    }

    public func appendLog(_ message: String, level: String = "info") {
        runtimeLogs.append(RuntimeLog(level: level, message: message))
        if runtimeLogs.count > 300 { runtimeLogs.removeFirst(runtimeLogs.count - 300) }
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
        app.router.get("api/v1/strategies/status") { [backend] request in try request.application.encoder.encode(await backend.statuses(), from: request) }
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
