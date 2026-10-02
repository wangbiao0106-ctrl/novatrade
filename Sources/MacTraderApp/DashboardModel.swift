import Foundation
import TradingDomain
import TradingServiceClient

@MainActor
final class DashboardModel: ObservableObject {
    @Published private(set) var selectedContract = DashboardModel.defaultContractID
    @Published private(set) var selectedInterval: KlineInterval = .fifteenMinutes
    @Published var errorMessage: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var serviceState: BackendServiceState = .stopped
    @Published private(set) var marketSnapshot: MarketSnapshot?
    @Published private(set) var candleDataSource: CandleDataSource = .unavailable
    @Published private(set) var candleLastUpdatedAt: Date?
    @Published private(set) var riskSnapshot = RiskSnapshot()
    @Published private(set) var isResettingRisk = false
    @Published private(set) var orders: [PaperOrder] = []
    @Published private(set) var livePositions: [PositionSnapshot] = []
    @Published private(set) var liveOrders: [OrderSnapshot] = []
    @Published private(set) var fills: [PaperFill] = []
    @Published private(set) var runtimeLogs: [RuntimeLog] = []
    @Published private(set) var strategyStatuses: [StrategyStatus] = []
    @Published private(set) var strategyCapitals: [StrategyCapitalSnapshot] = []
    /// Strategy instances are owned by the local service; this stays empty
    /// until its persisted state has been loaded.
    @Published private(set) var strategies: [StrategyConfig] = []
    @Published private(set) var accountOverview = AccountOverview()
    @Published private(set) var contracts: [PerpetualContract] = []
    @Published private(set) var favoriteIDs: Set<String> = []

    private let client = TradingServiceClient()
    private let serviceProcess = LocalServiceProcess()
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
    private var streamTask: Task<Void, Never>?
    private var marketTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var marketSubscriptionID = UUID()
    private var lifecycleGeneration = UUID()
    private var autoStartBackend = true

    private static let favoriteStorageKey = "nova.trade.favoriteContracts"
    private static let intervalStorageKey = "nova.trade.chartInterval"
    private static let selectedContractStorageKey = "nova.trade.selectedContract"
    private static let defaultContractID = "BTC-USDT-SWAP"
    private static let maxChartCandles = 500

    init() {
        let defaults = UserDefaults.standard
        if let saved = defaults.string(forKey: Self.favoriteStorageKey), !saved.isEmpty {
            favoriteIDs = Set(saved.split(separator: ",").map(String.init))
        }
        if let saved = defaults.string(forKey: Self.selectedContractStorageKey), !saved.isEmpty {
            selectedContract = saved
        }
        if let saved = defaults.string(forKey: Self.intervalStorageKey), let interval = KlineInterval(rawValue: saved) {
            selectedInterval = interval
        }
    }

    // MARK: - Market

    var selected: PerpetualContract? { contracts.first { $0.id == selectedContract } }
    var favoriteContracts: [PerpetualContract] { contracts.filter { favoriteIDs.contains($0.id) } }
    var currentPrice: Double? { marketSnapshot?.candles.last?.close.doubleValue ?? selected?.price }
    var currentChange: Double? { selected?.change }

    func contracts(for category: MarketCategory) -> [PerpetualContract] {
        switch category {
        case .mainstream:
            return contracts
                .filter { StrategyUniverseRules.mainstreamSymbols.contains($0.shortName.uppercased()) }
                .sorted { $0.volume24h > $1.volume24h }
        case .hot: return Array(contracts.sorted { $0.volume24h > $1.volume24h }.prefix(20))
        case .gainers: return Array(contracts.sorted { $0.change > $1.change }.prefix(20))
        case .losers: return Array(contracts.sorted { $0.change < $1.change }.prefix(20))
        }
    }

    func toggleFavorite(_ contract: PerpetualContract) {
        if favoriteIDs.contains(contract.id) { favoriteIDs.remove(contract.id) } else { favoriteIDs.insert(contract.id) }
        UserDefaults.standard.set(favoriteIDs.sorted().joined(separator: ","), forKey: Self.favoriteStorageKey)
    }

    func select(_ contract: PerpetualContract) {
        guard selectedContract != contract.id else { return }
        selectedContract = contract.id
        UserDefaults.standard.set(selectedContract, forKey: Self.selectedContractStorageKey)
        requestMarket()
    }

    func selectInterval(_ interval: KlineInterval) {
        guard selectedInterval != interval else { return }
        selectedInterval = interval
        UserDefaults.standard.set(interval.rawValue, forKey: Self.intervalStorageKey)
        requestMarket()
    }

    private func restoreSelectedContract() {
        guard let first = contracts.first else { return }
        let availableIDs = Set(contracts.map(\.id))
        let restoredID = availableIDs.contains(selectedContract)
            ? selectedContract
            : (availableIDs.contains(Self.defaultContractID) ? Self.defaultContractID : first.id)
        selectedContract = restoredID
        UserDefaults.standard.set(restoredID, forKey: Self.selectedContractStorageKey)
    }

    private func requestMarket() {
        marketTask?.cancel()
        streamTask?.cancel()
        marketSubscriptionID = UUID()
        let subscriptionID = marketSubscriptionID
        let instrument = selectedContract
        let interval = selectedInterval
        marketSnapshot = nil
        candleLastUpdatedAt = nil
        candleDataSource = serviceState == .running ? .connecting : .unavailable
        guard serviceState == .running else { return }
        // Connect immediately; historical loading must not delay real-time
        // candles or serialize rapid instrument selection.
        startStream()
        marketTask = Task { [weak self] in
            guard let self else { return }
            do {
                let history = try await client.market(instrumentID: instrument, interval: interval)
                guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { return }
                var candles = Dictionary(uniqueKeysWithValues: history.candles.map { ($0.id, $0) })
                // Preserve any WSS candles received while REST was warming up.
                for candle in marketSnapshot?.candles ?? [] { candles[candle.id] = candle }
                marketSnapshot = MarketSnapshot(instrumentID: instrument, interval: interval, candles: candles.values.sorted { $0.timestamp < $1.timestamp }, updatedAt: candleLastUpdatedAt ?? history.updatedAt)
            } catch {
                guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { return }
                errorMessage = "历史 K 线加载失败：\(error.localizedDescription)。实时数据仍等待 WSS 推送。"
            }
        }
    }

    private func startStream() {
        streamTask?.cancel()
        let instrument = selectedContract
        let interval = selectedInterval
        let subscriptionID = marketSubscriptionID
        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await event in await client.stream(instrumentID: instrument, interval: interval) {
                    guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { break }
                    guard event.instrumentID == nil || event.instrumentID == instrument else { continue }
                    handle(event)
                }
            } catch {
                guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { return }
                candleDataSource = .reconnecting
            }
        }
    }

    private func handle(_ event: StreamEvent) {
        switch event.type {
        case "connection":
            let state = event.payload ?? ""
            if ["local_connecting", "okx_wss_connecting", "okx_wss_subscribed"].contains(state) {
                candleDataSource = .connecting
            } else if state == "local_reconnecting" || state.hasPrefix("okx_wss_reconnecting") {
                candleDataSource = .reconnecting
            }
        case "candle":
            guard let candle = decode(Candle.self, from: event) else { return }
            merge(candle)
            candleDataSource = .websocket
            candleLastUpdatedAt = .now
        case "strategy":
            if let statuses = decode([StrategyStatus].self, from: event) { strategyStatuses = statuses }
        case "risk":
            // The risk stream is the authoritative, atomic view of account
            // and per-strategy pool state.
            guard let risk = decode(RiskSnapshot.self, from: event) else { return }
            riskSnapshot = risk
            strategyCapitals = risk.strategyCapitals
        case "log":
            if let logs = decode([RuntimeLog].self, from: event) { runtimeLogs = logs }
        case "account":
            if let account = decode(AccountOverview.self, from: event) { accountOverview = account }
        default:
            break
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from event: StreamEvent) -> T? {
        guard let data = event.payload?.data(using: .utf8) else { return nil }
        return try? decoder.decode(type, from: data)
    }

    private func merge(_ candle: Candle) {
        let current = marketSnapshot ?? MarketSnapshot(instrumentID: selectedContract, interval: selectedInterval)
        var values = current.candles
        if let existing = values.first(where: { $0.timestamp == candle.timestamp }), existing.confirmed, !candle.confirmed { return }
        values.upsert(candle)
        if values.count > Self.maxChartCandles { values.removeFirst(values.count - Self.maxChartCandles) }
        marketSnapshot = MarketSnapshot(instrumentID: current.instrumentID, interval: current.interval, candles: values, updatedAt: .now)
    }

    // MARK: - Backend lifecycle

    func refresh() async {
        guard !isRefreshing else { return }
        let generation = lifecycleGeneration
        // A manual stop during any await below supersedes this refresh.
        func isCurrent() -> Bool { generation == lifecycleGeneration && autoStartBackend }
        isRefreshing = true
        defer { isRefreshing = false }
        errorMessage = nil
        serviceState = .starting
        do {
            if (try? await client.health()) == nil {
                await serviceProcess.start()
                // A freshly spawned daemon can take a few seconds to bind its
                // socket, especially on the first launch after a build.
                var healthy = false
                for attempt in 0..<20 {
                    if (try? await client.health()) != nil { healthy = true; break }
                    if attempt < 19 { try? await Task.sleep(for: .milliseconds(250)) }
                }
                guard healthy else { throw TradingServiceClientError.disconnected }
            }
            guard isCurrent() else { return }
            serviceState = .running
            let connectedLog = RuntimeLog(level: "service", message: "前台已连接后台服务")
            runtimeLogs.append(connectedLog)
            _ = try? await client.appendLog(connectedLog)
            let remoteContracts = try await client.contracts()
            guard isCurrent() else { return }
            if !remoteContracts.isEmpty { contracts = remoteContracts.map(PerpetualContract.init(remote:)) }
            restoreSelectedContract()
            requestMarket()
            startPolling()
            let account = try await client.account()
            guard isCurrent() else { return }
            accountOverview = account
            riskSnapshot = (try? await client.risk()) ?? riskSnapshot
            await refreshTradingActivity()
            runtimeLogs = (try? await client.runtimeLogs()) ?? runtimeLogs
            strategyStatuses = (try? await client.strategyStatuses()) ?? strategyStatuses
            strategyCapitals = (try? await client.strategyCapital()) ?? strategyCapitals
            strategies = (try? await client.strategies()) ?? strategies
        } catch {
            guard generation == lifecycleGeneration else { return }
            if serviceState != .running { serviceState = autoStartBackend ? .unavailable : .stopped }
            errorMessage = "未能读取实时行情。\(error.localizedDescription)"
        }
    }

    /// Positions, orders and fills live on the OKX account (demo or live), so
    /// they change without any action from this app and must be re-read.
    private func refreshTradingActivity() async {
        livePositions = (try? await client.privatePositions()) ?? livePositions
        liveOrders = (try? await client.privateOrders()) ?? liveOrders
        orders = (try? await client.paperOrders()) ?? orders
        fills = (try? await client.paperFills()) ?? fills
    }

    /// The chart stream only covers the selected contract. The sidebar needs
    /// prices for every contract and the rail needs current account activity,
    /// so both are polled independently of chart selection.
    private func startPolling() {
        pollTask?.cancel()
        let generation = lifecycleGeneration
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
                guard let self, generation == lifecycleGeneration, serviceState == .running else { break }
                if let remote = try? await client.contracts(forceRefresh: true), !remote.isEmpty,
                   generation == lifecycleGeneration {
                    contracts = remote.map(PerpetualContract.init(remote:))
                }
                guard generation == lifecycleGeneration else { break }
                await refreshTradingActivity()
            }
        }
    }

    func toggleBackend() {
        if serviceState == .running || serviceState == .starting { stopBackend() } else { startBackend() }
    }

    private func startBackend() {
        autoStartBackend = true
        Task { await refresh() }
    }

    private func stopBackend() {
        autoStartBackend = false
        lifecycleGeneration = UUID()
        let generation = lifecycleGeneration
        isRefreshing = false
        serviceState = .stopping
        marketSubscriptionID = UUID()
        marketTask?.cancel(); marketTask = nil
        streamTask?.cancel(); streamTask = nil
        pollTask?.cancel(); pollTask = nil
        candleDataSource = .stopped
        Task {
            let stoppingLog = RuntimeLog(level: "service", message: "后台服务正在停止（手动操作）")
            runtimeLogs.append(stoppingLog)
            _ = try? await client.appendLog(stoppingLog)
            await serviceProcess.stop()
            guard generation == lifecycleGeneration else { return }
            serviceState = .stopped
        }
    }

    // MARK: - Risk

    /// Requests a manual risk reset. The service keeps the switch latched
    /// until the next UTC calendar day, so a successful request may still
    /// return a snapshot with `killSwitch == true`.
    func resetRisk() async throws -> RiskSnapshot {
        guard !isResettingRisk else { return riskSnapshot }
        isResettingRisk = true
        defer { isResettingRisk = false }
        let risk = try await client.resetRisk()
        riskSnapshot = risk
        strategyCapitals = risk.strategyCapitals
        runtimeLogs = (try? await client.runtimeLogs()) ?? runtimeLogs
        return risk
    }

    // MARK: - Strategies

    func hasStrategyType(_ type: StrategyType) -> Bool {
        strategies.contains { $0.type == type }
    }

    func createStrategy(_ config: StrategyConfig) async throws {
        let created = try await client.createStrategy(config)
        strategies.append(created)
    }

    func toggleStrategy(_ id: UUID) {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { return }
        let nextEnabled = !strategies[index].enabled
        strategies[index].enabled = nextEnabled
        Task {
            do {
                _ = nextEnabled ? try await client.startStrategy(id) : try await client.pauseStrategy(id)
            } catch {
                errorMessage = error.localizedDescription
                // Roll back only if no other toggle was issued meanwhile.
                if let current = strategies.firstIndex(where: { $0.id == id }), strategies[current].enabled == nextEnabled {
                    strategies[current].enabled = !nextEnabled
                }
            }
        }
    }

    /// Positions attributable to a strategy. Ownership mirrors the backend's
    /// `strategyRemotePositions`: only the instruments and directions of this
    /// strategy's non-terminal entry orders. The strategy's universe must not
    /// be used here, or "close and delete" would flatten manual positions and
    /// other strategies' positions on the same instruments.
    func strategyOpenPositions(for config: StrategyConfig) -> [PositionSnapshot] {
        let terminal: Set<String> = ["cancelled", "canceled", "rejected", "expired", "failed", "closed"]
        let owned = orders.filter { $0.strategyID == config.id && !terminal.contains($0.status.lowercased()) }
        let directionsByInstrument = Dictionary(grouping: owned, by: \.instrumentID).mapValues { values in
            Set(values.map { $0.side.lowercased() })
        }
        return livePositions.filter { position in
            guard position.quantity != 0, let directions = directionsByInstrument[position.instrumentID] else { return false }
            let side = position.side.lowercased()
            let isShort = side == "short" || (side == "net" && position.quantity < 0)
            return directions.contains(isShort ? "short" : "long")
        }
    }

    func deleteStrategy(_ config: StrategyConfig, closePositions: Bool) {
        guard !config.enabled else {
            errorMessage = "策略运行中，请先停止策略后再删除"
            return
        }
        let positionsToClose = closePositions ? strategyOpenPositions(for: config) : []
        Task {
            do {
                if !positionsToClose.isEmpty {
                    try await close(positionsToClose)
                    guard try await waitUntilFlat(positionsToClose) else {
                        errorMessage = "平仓尚未完成，请确认远端持仓归零后再删除策略"
                        return
                    }
                }
                _ = try await client.deleteStrategy(config.id)
                strategies.removeAll { $0.id == config.id }
            } catch {
                errorMessage = "删除策略 \(config.name) 失败：\(error.localizedDescription)"
            }
        }
    }

    private func close(_ positions: [PositionSnapshot]) async throws {
        for position in positions {
            let quantity = abs(position.quantity)
            guard quantity > 0 else { continue }
            if accountOverview.mode == .live {
                _ = try await client.placeLiveOrder(LiveOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true))
            } else {
                _ = try await client.placePaperOrder(PaperOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true))
            }
        }
    }

    /// Reduce-only orders fill asynchronously. Poll until every closed
    /// position is flat before allowing the backend to delete its owner.
    private func waitUntilFlat(_ positions: [PositionSnapshot]) async throws -> Bool {
        let targetKeys = Set(positions.map { "\($0.instrumentID)|\($0.side.lowercased())" })
        for attempt in 0..<20 {
            let latest = try await client.privatePositions()
            livePositions = latest
            let remaining = latest.filter { targetKeys.contains("\($0.instrumentID)|\($0.side.lowercased())") && $0.quantity != 0 }
            if remaining.isEmpty { return true }
            if attempt < 19 { try await Task.sleep(for: .milliseconds(250)) }
        }
        return false
    }

    private func closeSide(for position: PositionSnapshot) -> String {
        let side = position.side.lowercased()
        let isLong = side == "long" || side == "buy" || (side == "net" && position.quantity >= 0)
        return isLong ? "sell" : "buy"
    }
}
