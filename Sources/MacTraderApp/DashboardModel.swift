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
    /// Resolved scan pools per strategy, refreshed with the other account
    /// activity so the card can show how many symbols are actually scanned.
    @Published private(set) var strategyUniverses: [StrategyUniverseSnapshot] = []
    /// Strategy instances are owned by the local service; this stays empty
    /// until its persisted state has been loaded.
    @Published private(set) var strategies: [StrategyConfig] = []
    @Published private(set) var accountOverview = AccountOverview()
    @Published private(set) var contracts: [PerpetualContract] = []
    @Published private(set) var favoriteIDs: Set<String> = []
    @Published private(set) var aiConfig = AIConfig()
    @Published private(set) var aiStatus = AIStatus()
    @Published private(set) var aiDecisions: [AIAuditRecord] = []
    @Published private(set) var aiAudit: [AIAuditRecord] = []
    /// Configurations and runtime streams keyed by their server strategy ID.
    /// The singular Codex properties above remain as source compatibility for
    /// existing views and older local services.
    @Published private(set) var aiConfigs: [AIStrategyID: AIConfig] = [:]
    @Published private(set) var aiStatuses: [AIStrategyID: AIStatus] = [:]
    @Published private(set) var aiDecisionsByStrategy: [AIStrategyID: [AIAuditRecord]] = [:]
    @Published private(set) var aiAuditByStrategy: [AIStrategyID: [AIAuditRecord]] = [:]
    @Published private(set) var isUpdatingAI = false

    /// Contracts actually included in the latest AI market snapshot. The
    /// backend reports this set so the settings UI can verify the fixed
    /// allowlist that was sent to the worker.
    var aiObservedInstruments: [String] { aiStatus.observedInstruments }
    var aiObservationUpdatedAt: Date? { aiStatus.observationUpdatedAt }

    func aiConfig(for strategy: AIStrategyID) -> AIConfig {
        if let config = aiConfigs[strategy] { return config }
        if strategy == .codex { return aiConfig }
        return AIConfig(strategyID: strategy, provider: strategy == .deepseek ? .deepseekHarness : .codex)
    }

    func aiStatus(for strategy: AIStrategyID) -> AIStatus {
        if let status = aiStatuses[strategy] { return status }
        return strategy == .codex ? aiStatus : AIStatus(strategyID: strategy, provider: .deepseekHarness)
    }

    func aiDecisions(for strategy: AIStrategyID) -> [AIAuditRecord] {
        strategy == .codex ? aiDecisions : (aiDecisionsByStrategy[strategy] ?? [])
    }

    func aiAudit(for strategy: AIStrategyID) -> [AIAuditRecord] {
        strategy == .codex ? aiAudit : (aiAuditByStrategy[strategy] ?? [])
    }

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
    private var startupRetryTask: Task<Void, Never>?
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
                .sorted(by: Self.volumeRank)
        case .hot: return Array(contracts.sorted(by: Self.volumeRank).prefix(20))
        case .gainers: return Array(contracts.sorted {
            $0.change == $1.change
                ? $0.id < $1.id
                : $0.change > $1.change
        }.prefix(20))
        case .losers: return Array(contracts.sorted {
            $0.change == $1.change
                ? $0.id < $1.id
                : $0.change < $1.change
        }.prefix(20))
        }
    }

    /// Keep the OKX turnover ranking deterministic when two contracts have the
    /// same rounded 24h quote volume.
    private static func volumeRank(_ lhs: PerpetualContract, _ rhs: PerpetualContract) -> Bool {
        lhs.volume24h == rhs.volume24h ? lhs.id < rhs.id : lhs.volume24h > rhs.volume24h
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
            if state == "okx_wss_subscribed" {
                // The subscription is live; a candle may not arrive for
                // minutes, which must not read as "still connecting".
                if candleDataSource != .websocket { candleDataSource = .subscribed }
            } else if ["local_connecting", "okx_wss_connecting"].contains(state) {
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
                await serviceProcess.restart()
                // A freshly spawned daemon can take a few seconds to bind its
                // socket, especially on the first launch after a build or
                // after replacing a stale daemon.
                var healthy = false
                for attempt in 0..<40 {
                    if (try? await client.health()) != nil { healthy = true; break }
                    if attempt < 39 { try? await Task.sleep(for: .milliseconds(250)) }
                }
                guard healthy else { throw TradingServiceClientError.disconnected }
            }
            guard isCurrent() else { return }
            serviceState = .running
            let connectedLog = RuntimeLog(level: "service", message: "前台已连接后台服务")
            runtimeLogs.append(connectedLog)
            _ = try? await client.appendLog(connectedLog)
            // Start the saved/default contract immediately. Loading the
            // sidebar's full ticker list can take a network round trip; it
            // should not delay the selected chart's WSS and history fetch.
            let startedContract = selectedContract
            requestMarket()
            let remoteContracts = try await client.contracts()
            guard isCurrent() else { return }
            if !remoteContracts.isEmpty { contracts = remoteContracts.map(PerpetualContract.init(remote:)) }
            restoreSelectedContract()
            // A persisted contract may have expired. Re-request only when
            // restoring the list changed the selected instrument.
            if selectedContract != startedContract {
                requestMarket()
            }
            startPolling()
            let account = try await client.account()
            guard isCurrent() else { return }
            accountOverview = account
            riskSnapshot = (try? await client.risk()) ?? riskSnapshot
            await refreshTradingActivity()
            runtimeLogs = (try? await client.runtimeLogs()) ?? runtimeLogs
            strategyStatuses = (try? await client.strategyStatuses()) ?? strategyStatuses
            strategyCapitals = (try? await client.strategyCapital()) ?? strategyCapitals
            strategyUniverses = (try? await client.strategyTargets()) ?? strategyUniverses
            strategies = (try? await client.strategies()) ?? strategies
            await refreshAI()
        } catch {
            guard generation == lifecycleGeneration else { return }
            if autoStartBackend {
                serviceState = .unavailable
                scheduleStartupRetry()
            } else {
                if serviceState != .running { serviceState = .stopped }
                errorMessage = "未能读取实时行情。\(error.localizedDescription)"
            }
        }
    }

    /// Positions, orders and fills live on the OKX account (demo or live), so
    /// they change without any action from this app and must be re-read.
    private func refreshTradingActivity() async {
        livePositions = (try? await client.privatePositions()) ?? livePositions
        liveOrders = (try? await client.privateOrders()) ?? liveOrders
        orders = (try? await client.paperOrders()) ?? orders
        fills = (try? await client.paperFills()) ?? fills
        strategyStatuses = (try? await client.strategyStatuses()) ?? strategyStatuses
        strategyCapitals = (try? await client.strategyCapital()) ?? strategyCapitals
        riskSnapshot = (try? await client.risk()) ?? riskSnapshot
        strategyUniverses = (try? await client.strategyTargets()) ?? strategyUniverses
    }

    /// Refreshes the AI worker's control-plane state.  AI failures are kept
    /// separate from market/account refreshes so a missing Codex CLI cannot
    /// make the trading dashboard look disconnected.
    func refreshAI() async {
        guard serviceState == .running else { return }
        if let config = try? await client.aiConfig() {
            aiConfig = config
            aiConfigs[.codex] = config
        }
        if let status = try? await client.aiStatus() {
            aiStatus = status
            aiStatuses[.codex] = status
        }
        if let decisions = try? await client.aiDecisions() {
            aiDecisions = decisions
            aiDecisionsByStrategy[.codex] = decisions
        }
        if let audit = try? await client.aiAudit() {
            aiAudit = audit
            aiAuditByStrategy[.codex] = audit
        }
        // A service predating multi-strategy support simply returns an error
        // for these calls; preserve the Codex dashboard in that case.
        if let config = try? await client.aiConfig(strategy: .deepseek) {
            aiConfigs[.deepseek] = config
        }
        if let status = try? await client.aiStatus(strategy: .deepseek) {
            aiStatuses[.deepseek] = status
        }
        if let decisions = try? await client.aiDecisions(strategy: .deepseek) {
            aiDecisionsByStrategy[.deepseek] = decisions
        }
        if let audit = try? await client.aiAudit(strategy: .deepseek) {
            aiAuditByStrategy[.deepseek] = audit
        }
    }

    func updateAI(_ patch: AIPatch) async throws {
        try await updateAI(patch, strategy: .codex)
    }

    func updateAI(_ patch: AIPatch, strategy: AIStrategyID) async throws {
        guard !isUpdatingAI else { return }
        isUpdatingAI = true
        defer { isUpdatingAI = false }
        let config = try await client.updateAIConfig(patch, strategy: strategy)
        aiConfigs[strategy] = config
        if strategy == .codex { aiConfig = config }
        if let status = try? await client.aiStatus(strategy: strategy) {
            aiStatuses[strategy] = status
            if strategy == .codex { aiStatus = status }
        }
    }

    func enableAI() async throws {
        try await enableAI(strategy: .codex)
    }

    func enableAI(strategy: AIStrategyID) async throws {
        guard !isUpdatingAI else { return }
        isUpdatingAI = true
        defer { isUpdatingAI = false }
        let status = try await client.enableAI(strategy: strategy)
        aiStatuses[strategy] = status
        if strategy == .codex { aiStatus = status }
        if let config = try? await client.aiConfig(strategy: strategy) {
            aiConfigs[strategy] = config
            if strategy == .codex { aiConfig = config }
        }
    }

    func disableAI() async throws {
        try await disableAI(strategy: .codex)
    }

    func disableAI(strategy: AIStrategyID) async throws {
        guard !isUpdatingAI else { return }
        isUpdatingAI = true
        defer { isUpdatingAI = false }
        let status = try await client.disableAI(strategy: strategy)
        aiStatuses[strategy] = status
        if strategy == .codex { aiStatus = status }
        if let config = try? await client.aiConfig(strategy: strategy) {
            aiConfigs[strategy] = config
            if strategy == .codex { aiConfig = config }
        }
    }

    func flattenAI() async throws {
        try await flattenAI(strategy: .codex)
    }

    func flattenAI(strategy: AIStrategyID) async throws {
        guard !isUpdatingAI else { return }
        isUpdatingAI = true
        defer { isUpdatingAI = false }
        _ = try await client.flattenAI(strategy: strategy)
        if let status = try? await client.aiStatus(strategy: strategy) {
            aiStatuses[strategy] = status
            if strategy == .codex { aiStatus = status }
        }
        await refreshTradingActivity()
    }

    func chatAI(message: String, apply: Bool = false, suggestion: AIPatch? = nil) async throws -> AIChatResponse {
        try await chatAI(strategy: .codex, message: message, apply: apply, suggestion: suggestion)
    }

    func chatAI(strategy: AIStrategyID, message: String, apply: Bool = false, suggestion: AIPatch? = nil) async throws -> AIChatResponse {
        let response = try await client.chatAI(strategy: strategy, message: message, apply: apply, suggestion: suggestion)
        if let config = response.config {
            aiConfigs[strategy] = config
            if strategy == .codex { aiConfig = config }
        }
        if response.applied, let status = try? await client.aiStatus(strategy: strategy) {
            aiStatuses[strategy] = status
            if strategy == .codex { aiStatus = status }
        }
        return response
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
                await refreshAI()
            }
        }
    }

    private func scheduleStartupRetry() {
        guard autoStartBackend, startupRetryTask == nil else { return }
        let generation = lifecycleGeneration
        startupRetryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard !Task.isCancelled, let self else { return }
            startupRetryTask = nil
            guard generation == lifecycleGeneration, autoStartBackend, serviceState != .running else { return }
            await refresh()
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
        startupRetryTask?.cancel()
        startupRetryTask = nil
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

    func strategyCapital(for config: StrategyConfig) -> StrategyCapitalSnapshot? {
        strategyCapitals.first { $0.strategyID == config.id }
    }

    func strategyStatus(for config: StrategyConfig) -> StrategyStatus? {
        strategyStatuses.first { $0.id == config.id }
    }

    func strategyUniverse(for config: StrategyConfig) -> StrategyUniverseSnapshot? {
        strategyUniverses.first { $0.strategyID == config.id }
    }

    /// The entry order a signal produced, if it was submitted. Signals do not
    /// carry an instrument; the order record is the only link to the symbol.
    func strategyOrder(for signal: StrategySignal) -> PaperOrder? {
        orders.first { $0.signal?.id == signal.id }
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
        let owned = orders.filter { $0.strategyID == config.id && !OrderLifecycle.isTerminal($0.status) }
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
            // A reduce-only order only reduces the position of its own margin mode.
            let marginMode = position.marginMode ?? "cross"
            if accountOverview.mode == .live {
                _ = try await client.placeLiveOrder(LiveOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, marginMode: marginMode, reduceOnly: true))
            } else {
                _ = try await client.placePaperOrder(PaperOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true, marginMode: marginMode))
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
