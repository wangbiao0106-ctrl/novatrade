import SwiftUI
import AppKit
import TradingDomain
import TradingServiceClient

private extension JSONDecoder {
    static var iso8601: JSONDecoder { let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601; return decoder }
}

struct PerpetualContract: Identifiable, Hashable {
    let id: String
    let name: String
    let shortName: String
    let price: Double
    let change: Double
    let volume: String
    let category: String
    let accent: Color

    var pairLabel: String { shortName + " / " + (id.split(separator: "-").dropFirst().first.map(String.init) ?? "USDT") }

    init(id: String, name: String, shortName: String, price: Double, change: Double, volume: String, category: String, accent: Color) {
        self.id = id; self.name = name; self.shortName = shortName; self.price = price; self.change = change; self.volume = volume; self.category = category; self.accent = accent
    }

    init(remote: ContractMarket) {
        self.init(id: remote.id, name: remote.name, shortName: remote.baseCurrency, price: NSDecimalNumber(decimal: remote.last).doubleValue, change: NSDecimalNumber(decimal: remote.changePercent).doubleValue, volume: formatCompact(NSDecimalNumber(decimal: remote.volume24h).doubleValue), category: remote.category, accent: accentFor(remote.baseCurrency))
    }
}

private func accentFor(_ symbol: String) -> Color {
    let palette: [Color] = [.orange, .indigo, .purple, .blue, .yellow, .teal, .pink, .cyan]
    return palette[abs(symbol.hashValue) % palette.count]
}

private func formatCompact(_ value: Double) -> String {
    if value >= 1_000_000_000 { return String(format: "%.2fB", value / 1_000_000_000) }
    if value >= 1_000_000 { return String(format: "%.2fM", value / 1_000_000) }
    if value >= 1_000 { return String(format: "%.1fK", value / 1_000) }
    return String(format: "%.2f", value)
}

private let marketRiseColor = Color(red: 0.94, green: 0.27, blue: 0.31)
private let marketFallColor = Color(red: 0.18, green: 0.76, blue: 0.56)

enum ChartInterval: String, CaseIterable, Identifiable {
    case oneMinute = "1m", fiveMinutes = "5m", fifteenMinutes = "15m", oneHour = "1H", fourHours = "4H", oneDay = "1D"
    var id: String { rawValue }
}

extension ChartInterval {
    var domainValue: KlineInterval { KlineInterval(rawValue: rawValue) ?? .oneHour }
}

enum StrategyState: String, CaseIterable, Identifiable {
    case running = "运行中", paused = "已暂停", draft = "草稿"
    var id: String { rawValue }
    var color: Color {
        switch self { case .running: return .green; case .paused: return .orange; case .draft: return .secondary }
    }
}

/// The symbol buckets available when a strategy is created. The high-gain
/// buckets are derived from the live 24h percentage in `PerpetualContract`.
enum StrategySymbolCategory: String, CaseIterable, Identifiable {
    case mainstream = "主流币"
    case hotAltcoins = "热门山寨币"
    case highGain60 = "高涨幅币（24h +60%）"
    case highGain100 = "高涨幅币（24h +100%）"

    var id: String { rawValue }

    var domainValue: StrategyUniverseCategory {
        switch self {
        case .mainstream: return .mainstream
        case .hotAltcoins: return .hotAltcoins
        case .highGain60: return .highGain60
        case .highGain100: return .highGain100
        }
    }
}

enum BackendServiceState: String {
    case stopped, starting, running, stopping, unavailable

    var title: String {
        switch self {
        case .stopped: return "后台服务已停止"
        case .starting: return "后台服务启动中"
        case .running: return "后台服务已连接"
        case .stopping: return "后台服务停止中"
        case .unavailable: return "后台服务不可用"
        }
    }

    var color: Color {
        switch self {
        case .running: return .green
        case .starting, .stopping: return .orange
        case .stopped: return .secondary
        case .unavailable: return .red
        }
    }
}

enum CandleDataSource: Equatable {
    case connecting
    case websocket
    case reconnecting
    case stopped
    case unavailable

    var label: String {
        switch self {
        case .connecting: return "正在连接 OKX WSS"
        case .websocket: return "OKX WSS 长连接 · 实时推送"
        case .reconnecting: return "WSS 已断开 · 自动重连中"
        case .stopped: return "后台服务已停止"
        case .unavailable: return "等待行情连接"
        }
    }

    var icon: String {
        switch self {
        case .connecting: return "antenna.radiowaves.left.and.right"
        case .websocket: return "bolt.horizontal.circle.fill"
        case .reconnecting: return "arrow.triangle.2.circlepath"
        case .stopped: return "pause.circle"
        case .unavailable: return "clock.arrow.circlepath"
        }
    }
}

struct TradingStrategy: Identifiable {
    let id = UUID()
    var serviceID: UUID?
    var name: String
    var symbol: String
    var rule: String
    var state: StrategyState
    var pnl: Double
    var allocation: Double
    var poolAllocation: Double
    var updatedAt: Date = .now
    init(serviceID: UUID? = nil, name: String, symbol: String, rule: String, state: StrategyState, pnl: Double, allocation: Double, poolAllocation: Double = 100) {
        self.serviceID = serviceID; self.name = name; self.symbol = symbol; self.rule = rule; self.state = state; self.pnl = pnl; self.allocation = allocation; self.poolAllocation = poolAllocation
    }
}

@MainActor
final class DashboardModel: ObservableObject {
    /// 策略类型 → 界面规则描述
    nonisolated static func ruleLabel(_ type: StrategyType) -> String {
        switch type {
        case .sweepReversalShort: return "山寨币二次扫顶做空（1h）"
        case .emaAltcoinLong: return "双均线交易山寨币多（1h）"
        }
    }

    @Published private(set) var snapshot = SystemSnapshot()
    @Published var selectedContract = "BTC-USDT-SWAP"
    @Published var selectedInterval: ChartInterval = .fifteenMinutes
    @Published var errorMessage: String?
    @Published var isRefreshing = false
    @Published private(set) var serviceState: BackendServiceState = .stopped
    @Published private(set) var marketSnapshot: MarketSnapshot?
    @Published private(set) var candleDataSource: CandleDataSource = .unavailable
    @Published private(set) var candleLastUpdatedAt: Date?
    @Published private(set) var riskSnapshot = RiskSnapshot()
    @Published private(set) var positions: [PaperPosition] = []
    @Published private(set) var orders: [PaperOrder] = []
    @Published private(set) var livePositions: [PositionSnapshot] = []
    @Published private(set) var liveOrders: [OrderSnapshot] = []
    @Published private(set) var fills: [PaperFill] = []
    @Published private(set) var runtimeLogs: [RuntimeLog] = []
    @Published private(set) var strategyStatuses: [StrategyStatus] = []
    @Published private(set) var strategyCapitals: [StrategyCapitalSnapshot] = []
    @Published private(set) var strategyConfigs: [StrategyConfig] = []
    @Published private(set) var accountOverview = AccountOverview()
    @Published private(set) var liveTradingStatus = LiveTradingStatus()
    @Published private(set) var lastLiveOrder: LiveOrderResult?
    @Published private(set) var contracts: [PerpetualContract] = []
    @Published var favoriteIDs: Set<String> = []
    // Strategy instances are owned by the local service. Keep this empty
    // until the persisted service state has been loaded so stale demo cards
    // cannot appear during application startup.
    @Published var strategies: [TradingStrategy] = []

    private let client = TradingServiceClient()
    private let serviceProcess = LocalServiceProcess()
    private var streamTask: Task<Void, Never>?
    private var marketTask: Task<Void, Never>?
    private var contractsTask: Task<Void, Never>?
    private var marketSubscriptionID = UUID()
    private var lifecycleGeneration = UUID()
    private var autoStartBackend = true
    private let favoriteStorageKey = "nova.trade.favoriteContracts"
    private let intervalStorageKey = "nova.trade.chartInterval"

    init() {
        if let saved = UserDefaults.standard.string(forKey: favoriteStorageKey), !saved.isEmpty {
            favoriteIDs = Set(saved.split(separator: ",").map(String.init))
        }
        if let saved = UserDefaults.standard.string(forKey: intervalStorageKey), let interval = ChartInterval(rawValue: saved) {
            selectedInterval = interval
        }
    }

    var selected: PerpetualContract? { contracts.first(where: { $0.id == selectedContract }) }
    private var displayContracts: [PerpetualContract] { contracts }
    var favoriteContracts: [PerpetualContract] { displayContracts.filter { favoriteIDs.contains($0.id) } }
    var otherContracts: [PerpetualContract] { displayContracts.filter { !favoriteIDs.contains($0.id) } }
    var runningStrategies: [TradingStrategy] { strategies.filter { $0.state == .running } }
    var currentPrice: Double? { snapshot.ticker.map { NSDecimalNumber(decimal: $0.last).doubleValue } ?? selected?.price }
    var currentChange: Double? { selected?.change }

    func contracts(for category: String) -> [PerpetualContract] {
        let source = contracts
        guard category != "全部" else { return source }
        switch category {
        case "热门": return source.sorted { compactVolume($0.volume) > compactVolume($1.volume) }.prefix(20).map { $0 }
        case "涨幅": return source.sorted { $0.change > $1.change }.prefix(20).map { $0 }
        case "跌幅": return source.sorted { $0.change < $1.change }.prefix(20).map { $0 }
        default: return source
        }
    }

    /// Returns the live contract choices used by the strategy form. The
    /// backend's "热门" tag is a volume ranking bucket, so the hot-altcoin
    /// group falls back to the highest-volume non-major contracts when that
    /// tag is sparse or unavailable.
    func strategyContracts(for category: StrategySymbolCategory) -> [PerpetualContract] {
        let source = displayContracts
        switch category {
        case .mainstream:
            return source.filter(Self.isMainstream).sorted { compactVolume($0.volume) > compactVolume($1.volume) }
        case .hotAltcoins:
            let altcoins = source.filter(Self.isEligibleHotAltcoin)
            return Array(altcoins.sorted { compactVolume($0.volume) > compactVolume($1.volume) }.prefix(20))
        case .highGain60:
            return source.filter { $0.change >= 60 }.sorted { $0.change > $1.change }
        case .highGain100:
            return source.filter { $0.change >= 100 }.sorted { $0.change > $1.change }
        }
    }

    private static let mainstreamSymbols: Set<String> = [
        "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
        "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI"
    ]

    private static func isMainstream(_ contract: PerpetualContract) -> Bool {
        mainstreamSymbols.contains(contract.shortName.uppercased())
    }

    private static func isEligibleHotAltcoin(_ contract: PerpetualContract) -> Bool {
        let parts = contract.id.split(separator: "-")
        let quote = parts.count > 1 ? String(parts[1]) : ""
        return StrategyUniverseRules.isEligibleHotAltcoin(instrumentID: contract.id, baseCurrency: contract.shortName, quoteCurrency: quote)
    }

    private func compactVolume(_ value: String) -> Double {
        let suffix = value.last
        let multiplier: Double = suffix == "B" ? 1_000_000_000 : suffix == "M" ? 1_000_000 : suffix == "K" ? 1_000 : 1
        let number = Double(value.dropLast(suffix == "B" || suffix == "M" || suffix == "K" ? 1 : 0)) ?? 0
        return number * multiplier
    }

    func toggleFavorite(_ contract: PerpetualContract) {
        if favoriteIDs.contains(contract.id) { favoriteIDs.remove(contract.id) } else { favoriteIDs.insert(contract.id) }
        UserDefaults.standard.set(favoriteIDs.sorted().joined(separator: ","), forKey: favoriteStorageKey)
    }

    func select(_ contract: PerpetualContract) {
        guard selectedContract != contract.id else { return }
        selectedContract = contract.id
        snapshot = SystemSnapshot(mode: snapshot.mode, connection: snapshot.connection, ticker: nil, account: snapshot.account)
        requestMarket()
    }

    func selectInterval(_ interval: ChartInterval) {
        guard selectedInterval != interval else { return }
        selectedInterval = interval
        UserDefaults.standard.set(interval.rawValue, forKey: intervalStorageKey)
        requestMarket()
    }

    private func requestMarket() {
        marketTask?.cancel()
        streamTask?.cancel()
        marketSubscriptionID = UUID()
        let subscriptionID = marketSubscriptionID
        let instrument = selectedContract
        let interval = selectedInterval.domainValue
        marketSnapshot = nil
        candleLastUpdatedAt = nil
        candleDataSource = serviceState == .running ? .connecting : .unavailable
        guard serviceState == .running else { return }
        // Connect immediately; account refresh and historical loading must not
        // delay real-time candles or serialize rapid instrument selection.
        startStream()
        marketTask = Task { [weak self] in
            guard let self else { return }
            do {
                let history = try await client.market(instrumentID: instrument, interval: interval)
                guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { return }
                var candles = Dictionary(uniqueKeysWithValues: history.candles.map { ($0.id, $0) })
                // Preserve any WSS candles received while REST was warming up.
                for candle in marketSnapshot?.candles ?? [] { candles[candle.id] = candle }
                marketSnapshot = MarketSnapshot(instrumentID: instrument, interval: interval, candles: candles.values.sorted { $0.timestamp < $1.timestamp }, ticker: history.ticker, updatedAt: candleLastUpdatedAt ?? history.updatedAt)
            } catch {
                guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { return }
                errorMessage = "历史 K 线加载失败：\(error.localizedDescription)。实时数据仍等待 WSS 推送。"
            }
        }
    }

    func refresh() async {
        guard !isRefreshing else { return }
        let generation = lifecycleGeneration
        isRefreshing = true
        errorMessage = nil
        let previousTicker = snapshot.ticker
        serviceState = .starting
        snapshot = SystemSnapshot(mode: .paper, connection: "本地服务连接中…", ticker: previousTicker, account: snapshot.account)
        do {
            var health: ServiceHealth?
            if let result = try? await client.health() {
                health = result
            } else if autoStartBackend {
                await serviceProcess.start()
            }
            // A freshly spawned Hummingbird process can take a few seconds to
            // bind its socket, especially on the first launch after a build.
            if health == nil {
                for attempt in 0..<20 {
                    if let result = try? await client.health() { health = result; break }
                    if attempt < 19 { try? await Task.sleep(for: .milliseconds(250)) }
                }
            }
            guard generation == lifecycleGeneration, autoStartBackend else {
                isRefreshing = false
                return
            }
            guard let health else { throw TradingServiceClientError.disconnected }
            _ = health
            serviceState = .running
            let connectedLog = RuntimeLog(level: "service", message: "前台已连接后台服务")
            runtimeLogs.append(connectedLog)
            _ = try? await client.appendLog(connectedLog)
            requestMarket()
            startContractsRefresh()
            let remoteContracts = try await client.contracts()
            guard generation == lifecycleGeneration, autoStartBackend else {
                isRefreshing = false
                return
            }
            if !remoteContracts.isEmpty { contracts = remoteContracts.map(PerpetualContract.init(remote:)) }
            let account = try await client.account()
            guard generation == lifecycleGeneration, autoStartBackend else {
                isRefreshing = false
                return
            }
            accountOverview = account
            liveTradingStatus = (try? await client.liveTradingStatus()) ?? liveTradingStatus
            riskSnapshot = (try? await client.risk()) ?? riskSnapshot
            positions = (try? await client.paperPositions()) ?? positions
            orders = (try? await client.paperOrders()) ?? orders
            // The demo profile is a real OKX account boundary too. Read its
            // positions and orders from OKX so strategy activity is visible
            // after the exchange accepts the simulated order.
            livePositions = (try? await client.privatePositions()) ?? livePositions
            liveOrders = (try? await client.privateOrders()) ?? liveOrders
            fills = (try? await client.paperFills()) ?? fills
            runtimeLogs = (try? await client.runtimeLogs()) ?? runtimeLogs
            strategyStatuses = (try? await client.strategyStatuses()) ?? strategyStatuses
            strategyCapitals = (try? await client.strategyCapital()) ?? strategyCapitals
            if let configs = try? await client.strategies() {
                strategyConfigs = configs
                strategies = configs.map { config in
                    TradingStrategy(serviceID: config.id, name: config.displayName, symbol: config.scope.displayName, rule: DashboardModel.ruleLabel(config.type), state: config.enabled ? .running : .paused, pnl: 0, allocation: config.riskPercent, poolAllocation: config.capitalPoolPercent)
                }
            }
            guard generation == lifecycleGeneration, autoStartBackend else {
                isRefreshing = false
                return
            }
            snapshot = SystemSnapshot(mode: account.mode, connection: "已连接 · OKX CLI \(account.mode == .paper ? "模拟盘" : "实盘")", ticker: snapshot.ticker, account: AccountSnapshot(equity: account.equityUSD, availableBalance: account.availableEquityUSD))
        } catch {
            guard generation == lifecycleGeneration else {
                isRefreshing = false
                return
            }
            if serviceState != .running { serviceState = autoStartBackend ? .unavailable : .stopped }
            snapshot = SystemSnapshot(mode: accountOverview.mode, connection: "OKX CLI 不可用", ticker: previousTicker, account: snapshot.account)
            errorMessage = "未能读取实时行情。\(error.localizedDescription)"
        }
        isRefreshing = false
    }

    /// The selected contract has a dedicated candle/ticker stream. The
    /// sidebar still needs prices for every other contract, so refresh the
    /// exchange's bulk ticker response independently of chart selection.
    private func startContractsRefresh() {
        contractsTask?.cancel()
        let generation = lifecycleGeneration
        contractsTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self,
                      generation == self.lifecycleGeneration,
                      self.serviceState == .running else { break }
                if let remote = try? await self.client.contracts(forceRefresh: true), !remote.isEmpty,
                   generation == self.lifecycleGeneration,
                   self.serviceState == .running {
                    self.contracts = remote.map(PerpetualContract.init(remote:))
                }
                do { try await Task.sleep(for: .seconds(5)) }
                catch { break }
            }
        }
    }

    private func startStream() {
        streamTask?.cancel()
        let instrument = selectedContract
        let interval = selectedInterval.domainValue
        let subscriptionID = marketSubscriptionID
        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await event in await client.stream(instrumentID: instrument, interval: interval) {
                    guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { break }
                    guard event.instrumentID == nil || event.instrumentID == instrument else { continue }
                    if event.type == "connection", ["okx_wss_connecting", "local_connecting", "okx_wss_subscribed"].contains(event.payload ?? "") {
                        await MainActor.run { self.candleDataSource = .connecting }
                    }
                    if event.type == "connection", event.payload?.hasPrefix("okx_wss_reconnecting") == true || event.type == "connection" && event.payload == "local_reconnecting" {
                        await MainActor.run { self.candleDataSource = .reconnecting }
                    }
                    if event.type == "ticker", let payload = event.payload, let data = payload.data(using: .utf8), let ticker = try? JSONDecoder.iso8601.decode(MarketTicker.self, from: data) {
                        await MainActor.run {
                            self.snapshot = SystemSnapshot(mode: self.snapshot.mode, connection: "已连接 · OKX CLI \(self.accountOverview.mode == .paper ? "模拟盘" : "实盘")", ticker: ticker, account: self.snapshot.account)
                            self.updateContract(ticker)
                        }
                    }
                    if event.type == "candle", let payload = event.payload, let data = payload.data(using: .utf8), let candle = try? JSONDecoder.iso8601.decode(Candle.self, from: data) {
                        await MainActor.run {
                            self.merge(candle)
                            self.candleDataSource = .websocket
                            self.candleLastUpdatedAt = .now
                        }
                    }
                    if event.type == "strategy", let payload = event.payload, let data = payload.data(using: .utf8), let statuses = try? JSONDecoder.iso8601.decode([StrategyStatus].self, from: data) {
                        await MainActor.run { self.strategyStatuses = statuses }
                    }
                    if event.type == "risk", let payload = event.payload, let data = payload.data(using: .utf8), let risk = try? JSONDecoder.iso8601.decode(RiskSnapshot.self, from: data) {
                        await MainActor.run {
                            // The risk stream is the authoritative, atomic view
                            // of account and per-strategy pool state. Keep the
                            // card values in lockstep with the kill-switch and
                            // daily PnL fields instead of waiting for a REST
                            // refresh.
                            self.riskSnapshot = risk
                            self.strategyCapitals = risk.strategyCapitals
                        }
                    }
                    if event.type == "log", let payload = event.payload, let data = payload.data(using: .utf8), let logs = try? JSONDecoder.iso8601.decode([RuntimeLog].self, from: data) {
                        await MainActor.run { self.runtimeLogs = logs }
                    }
                    if event.type == "account", let payload = event.payload, let data = payload.data(using: .utf8), let account = try? JSONDecoder.iso8601.decode(AccountOverview.self, from: data) {
                        await MainActor.run { self.accountOverview = account }
                    }
                }
            } catch {
                guard !Task.isCancelled, subscriptionID == marketSubscriptionID else { return }
                await MainActor.run { self.candleDataSource = .reconnecting }
            }
        }
    }

    private func merge(_ candle: Candle) {
        var current = marketSnapshot ?? MarketSnapshot(instrumentID: selectedContract, interval: selectedInterval.domainValue)
        var values = current.candles
        if let index = values.firstIndex(where: { $0.timestamp == candle.timestamp }) {
            if values[index].confirmed && !candle.confirmed { return }
        }
        values.upsert(candle)
        if values.count > 500 { values.removeFirst(values.count - 500) }
        current = MarketSnapshot(instrumentID: current.instrumentID, interval: current.interval, candles: values, ticker: current.ticker, updatedAt: .now)
        marketSnapshot = current
    }

    private func updateContract(_ ticker: MarketTicker) {
        guard let index = contracts.firstIndex(where: { $0.id == ticker.instrumentID }) else { return }
        let item = contracts[index]
        let price = NSDecimalNumber(decimal: ticker.last).doubleValue
        contracts[index] = PerpetualContract(id: item.id, name: item.name, shortName: item.shortName, price: price, change: item.change, volume: item.volume, category: item.category, accent: item.accent)
    }

    func stopService() async {
        lifecycleGeneration = UUID()
        isRefreshing = false
        marketSubscriptionID = UUID()
        marketTask?.cancel(); marketTask = nil
        streamTask?.cancel(); streamTask = nil
        contractsTask?.cancel(); contractsTask = nil
        candleDataSource = .stopped
        await serviceProcess.stop(); serviceState = .stopped
    }

    func startBackend() {
        autoStartBackend = true
        Task { await refresh() }
    }

    func stopBackend() {
        autoStartBackend = false
        lifecycleGeneration = UUID()
        let generation = lifecycleGeneration
        isRefreshing = false
        serviceState = .stopping
        marketSubscriptionID = UUID()
        marketTask?.cancel(); marketTask = nil
        streamTask?.cancel(); streamTask = nil
        contractsTask?.cancel(); contractsTask = nil
        candleDataSource = .stopped
        Task {
            let stoppingLog = RuntimeLog(level: "service", message: "后台服务正在停止（手动操作）")
            runtimeLogs.append(stoppingLog)
            _ = try? await client.appendLog(stoppingLog)
            await serviceProcess.stop()
            guard generation == lifecycleGeneration else { return }
            serviceState = .stopped
            snapshot = SystemSnapshot(mode: snapshot.mode, connection: "后台服务已停止", ticker: snapshot.ticker, account: snapshot.account)
        }
    }

    func toggleBackend() {
        if serviceState == .running || serviceState == .starting { stopBackend() } else { startBackend() }
    }

    func toggleLiveTrading() {
        Task {
            do {
                liveTradingStatus = liveTradingStatus.enabled ? try await client.disableLiveTrading() : try await client.enableLiveTrading()
            } catch { errorMessage = error.localizedDescription; liveTradingStatus = (try? await client.liveTradingStatus()) ?? liveTradingStatus }
        }
    }

    func placeLiveOrder(side: String, quantityText: String, reduceOnly: Bool) {
        guard let quantity = Decimal(string: quantityText.trimmingCharacters(in: .whitespacesAndNewlines)), quantity > 0 else {
            errorMessage = "请输入有效的下单数量"
            return
        }
        Task {
            do {
                let request = LiveOrderRequest(instrumentID: selectedContract, side: side, quantity: quantity, reduceOnly: reduceOnly)
                lastLiveOrder = try await client.placeLiveOrder(request)
                errorMessage = nil
                accountOverview = (try? await client.account()) ?? accountOverview
                livePositions = (try? await client.privatePositions()) ?? livePositions
                liveOrders = (try? await client.privateOrders()) ?? liveOrders
            } catch { errorMessage = error.localizedDescription }
        }
    }

    func createStrategy(_ config: StrategyConfig) async throws -> StrategyConfig {
        let created = try await client.createStrategy(config)
        strategyConfigs.append(created)
        strategies.insert(TradingStrategy(serviceID: created.id, name: created.displayName, symbol: created.scope.displayName, rule: DashboardModel.ruleLabel(created.type), state: .paused, pnl: 0, allocation: created.riskPercent, poolAllocation: created.capitalPoolPercent), at: 0)
        return created
    }

    func strategyOpenPositions(for strategy: TradingStrategy) -> [PositionSnapshot] {
        guard let serviceID = strategy.serviceID else { return [] }
        let instrumentIDs = strategyInstrumentIDs(for: serviceID)
        return livePositions.filter { instrumentIDs.contains($0.instrumentID) && $0.quantity != 0 }
    }

    private func strategyInstrumentIDs(for serviceID: UUID) -> Set<String> {
        var ids = Set(orders.filter { $0.strategyID == serviceID }.map(\.instrumentID))
        guard let config = strategyConfigs.first(where: { $0.id == serviceID }) else { return ids }
        ids.formUnion(config.scope.instrumentIDs)
        if config.scope.mode == .dynamicCategory, let category = config.scope.category {
            let uiCategory: StrategySymbolCategory
            switch category {
            case .mainstream: uiCategory = .mainstream
            case .hotAltcoins: uiCategory = .hotAltcoins
            case .highGain60: uiCategory = .highGain60
            case .highGain100: uiCategory = .highGain100
            }
            ids.formUnion(strategyContracts(for: uiCategory).map(\.id))
        }
        return ids
    }

    private func closeSide(for position: PositionSnapshot) -> String {
        let side = position.side.lowercased()
        let isLong = side == "long" || side == "buy" || (side == "net" && position.quantity >= 0)
        return isLong ? "sell" : "buy"
    }

    func deleteStrategy(_ id: UUID, closePositions: Bool = false) {
        guard let index = strategies.firstIndex(where: { $0.serviceID == id }) else { return }
        let previous = strategies[index]
        guard previous.state != .running else {
            errorMessage = "策略运行中，请先停止策略后再删除"
            return
        }
        let positionsToClose = strategyOpenPositions(for: previous)
        Task {
            do {
                if closePositions {
                    for position in positionsToClose {
                        let quantity = abs(position.quantity)
                        guard quantity > 0 else { continue }
                        if accountOverview.mode == .live {
                            _ = try await client.placeLiveOrder(LiveOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true))
                        } else {
                            _ = try await client.placePaperOrder(PaperOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true))
                        }
                    }
                    livePositions = (try? await client.privatePositions()) ?? livePositions
                }
                _ = try await client.deleteStrategy(id)
                strategies.removeAll { $0.serviceID == id }
                strategyConfigs.removeAll { $0.id == id }
            } catch {
                errorMessage = "删除策略 \(previous.name) 失败：\(error.localizedDescription)"
            }
        }
    }

    func occupiedFunds(for strategy: TradingStrategy) -> Decimal? {
        guard let equity = accountOverview.equityUSD else { return nil }
        if let serviceID = strategy.serviceID, let pool = strategyCapitals.first(where: { $0.strategyID == serviceID }) {
            return pool.reservedCapital
        }
        return equity * Decimal(strategy.poolAllocation) / 100
    }

    func strategyPool(for strategy: TradingStrategy) -> StrategyCapitalSnapshot? {
        guard let serviceID = strategy.serviceID else { return nil }
        return strategyCapitals.first(where: { $0.strategyID == serviceID })
    }

    func hasStrategyType(_ type: StrategyType) -> Bool {
        strategyConfigs.contains { $0.type == type }
    }

    func toggleStrategy(_ id: UUID) {
        guard let index = strategies.firstIndex(where: { $0.id == id }) else { return }
        let previousState = strategies[index].state
        let nextRunning = strategies[index].state != .running
        strategies[index].state = nextRunning ? .running : .paused
        strategies[index].updatedAt = .now
        if let serviceID = strategies[index].serviceID {
            Task {
                do {
                    _ = nextRunning ? try await client.startStrategy(serviceID) : try await client.pauseStrategy(serviceID)
                } catch {
                    errorMessage = error.localizedDescription
                    // Roll back only if the user has not issued another toggle
                    // while this request was in flight.
                    if let currentIndex = strategies.firstIndex(where: { $0.serviceID == serviceID }),
                       strategies[currentIndex].state == (nextRunning ? .running : .paused) {
                        strategies[currentIndex].state = previousState
                        strategies[currentIndex].updatedAt = .now
                    }
                }
            }
        }
    }
}

struct DashboardView: View {
    @StateObject private var model = DashboardModel()
    @State private var showingNewStrategy = false
    @State private var searchText = ""

    var body: some View {
        NavigationSplitView {
            MarketSidebar(model: model, searchText: $searchText)
        } detail: {
            VStack(spacing: 0) {
                TopBar(model: model, showingNewStrategy: $showingNewStrategy)
                Divider().overlay(Color.white.opacity(0.06))
                HStack(alignment: .top, spacing: 0) {
                    GeometryReader { geometry in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 12) {
                                MarketHeader(model: model)
                                ChartPanel(model: model, chartHeight: max(280, geometry.size.height - 300))
                                MarketInsightStrip(model: model)
                            }.padding(16)
                        }
                    }
                    Divider().overlay(Color.white.opacity(0.07))
                    RightRail(model: model, showingNewStrategy: $showingNewStrategy)
                }
            }.background(Color.appBackground)
        }
        .sheet(isPresented: $showingNewStrategy) { NewStrategySheet(model: model) }
        .task { await model.refresh() }
        .preferredColorScheme(.dark)
        .frame(minWidth: 1180, minHeight: 760)
    }
}

struct RightRail: View {
    @ObservedObject var model: DashboardModel
    @Binding var showingNewStrategy: Bool

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Label("策略状态", systemImage: "brain.head.profile").font(.headline)
                    Spacer()
                    Button { showingNewStrategy = true } label: { Image(systemName: "plus") }
                        .buttonStyle(.bordered).controlSize(.small).help("新建策略")
                }
                ForEach(model.strategies) { strategy in
                    StrategyStatusModule(strategy: strategy, status: model.strategyStatuses.first(where: { $0.id == strategy.serviceID }), model: model, toggle: { model.toggleStrategy(strategy.id) }, delete: { closePositions in
                        if let serviceID = strategy.serviceID { model.deleteStrategy(serviceID, closePositions: closePositions) }
                    })
                }
                RailModule(title: "持仓", icon: "chart.bar.xaxis") {
                    if model.livePositions.isEmpty { RailEmpty(model.accountOverview.mode == .paper ? "暂无 OKX 模拟持仓" : "暂无实盘持仓") }
                    ForEach(model.livePositions) { position in
                        RailRow {
                            Text(position.instrumentID).font(.caption.monospaced())
                            Spacer()
                            Text(decimalText(position.quantity)).font(.caption.monospacedDigit())
                            Text(position.side.uppercased()).foregroundStyle(.secondary)
                            Text(String(format: "%+.2f", decimalDouble(position.unrealizedPnL ?? 0))).foregroundStyle(decimalDouble(position.unrealizedPnL ?? 0) >= 0 ? .green : .red)
                        }
                    }
                }
                RailModule(title: "挂单", icon: "list.bullet.rectangle") {
                    if model.liveOrders.isEmpty { RailEmpty(model.accountOverview.mode == .paper ? "暂无 OKX 模拟挂单" : "暂无实盘挂单") }
                    ForEach(model.liveOrders) { order in
                        RailRow {
                            Text(order.instrumentID).font(.caption.monospaced())
                            Spacer()
                            Text(order.side.uppercased()).font(.caption2.weight(.semibold))
                            Text(order.status).foregroundStyle(.secondary)
                        }
                    }
                }
            RailModule(title: "交易流水", icon: "arrow.left.arrow.right") {
                if model.fills.isEmpty { RailEmpty("暂无 OKX 模拟成交记录") }
                ForEach(model.fills.prefix(8)) { fill in
                    RailRow {
                        Text(fill.timestamp.formatted(date: .omitted, time: .shortened)).foregroundStyle(.secondary)
                        Spacer()
                        Text(decimalText(fill.quantity)).monospacedDigit()
                        Text("费 \(decimalText(fill.fee))").foregroundStyle(.secondary)
                    }
                }
                }
                RailModule(title: "运行日志", icon: "text.alignleft") {
                    if model.runtimeLogs.isEmpty { RailEmpty("等待服务事件…") }
                    ForEach(model.runtimeLogs.suffix(12).reversed()) { log in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(log.message).font(.caption).lineLimit(2)
                            Text(log.timestamp.formatted(date: .omitted, time: .shortened)).font(.caption2).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
                    }
                }
            }.padding(16)
        }
        .frame(width: 350)
        .background(Color.sidebarBackground.opacity(0.7))
    }
}

struct StrategyStatusModule: View {
    let strategy: TradingStrategy
    let status: StrategyStatus?
    @ObservedObject var model: DashboardModel
    let toggle: () -> Void
    let delete: (Bool) -> Void
    @State private var showingDeleteConfirmation = false
    @State private var showingPositionDeleteConfirmation = false

    private var openPositions: [PositionSnapshot] { model.strategyOpenPositions(for: strategy) }

    private func requestDelete() {
        guard strategy.state != .running else {
            model.errorMessage = "策略运行中，请先停止策略后再删除"
            return
        }
        if openPositions.isEmpty {
            showingDeleteConfirmation = true
        } else {
            showingPositionDeleteConfirmation = true
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                Text(strategy.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer()
                if strategy.serviceID != nil {
                    Button { requestDelete() } label: { Image(systemName: "trash") }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .opacity(strategy.state == .running ? 0.45 : 1)
                        .disabled(strategy.state == .running)
                        .help(strategy.state == .running ? "请先停止策略" : "删除策略实例")
                }
                Text(status?.direction?.uppercased() ?? "空仓").font(.caption2.weight(.semibold)).padding(.horizontal, 7).padding(.vertical, 4).background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 5))
            }
            HStack(spacing: 6) {
                Text(strategy.symbol).font(.caption.monospaced()).foregroundStyle(.secondary)
                Spacer()
                Circle().fill(strategy.state.color).frame(width: 7, height: 7)
                Text(strategy.state.rawValue).font(.caption2).foregroundStyle(strategy.state.color)
            }
            if let status, let signal = status.lastSignal {
                Text("最近信号 \(signal.type) @ \(formatPrice(NSDecimalNumber(decimal: signal.price).doubleValue))").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            } else {
                Text("暂无最近信号").font(.caption2).foregroundStyle(.secondary)
            }
            HStack {
                Text("收益 \(formatPnL(status?.pnl ?? 0)) USDT").font(.caption2.monospacedDigit()).foregroundStyle(decimalDouble(status?.pnl ?? 0) >= 0 ? .green : .red)
                Spacer()
                Button(strategy.state == .running ? "暂停" : "启动", action: toggle).buttonStyle(.bordered).controlSize(.mini)
            }
        }
        .padding(12)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(strategy.state == .running ? Color.mint.opacity(0.25) : Color.white.opacity(0.07)))
        .confirmationDialog("删除策略实例？", isPresented: $showingDeleteConfirmation, titleVisibility: .visible) {
            Button("删除", role: .destructive) { delete(false) }
            Button("取消", role: .cancel) {}
        } message: {
            Text("删除后不会再自动运行该规则，但历史挂单和成交记录会保留。")
        }
        .confirmationDialog("策略存在未平仓位", isPresented: $showingPositionDeleteConfirmation, titleVisibility: .visible) {
            Button("平仓并删除", role: .destructive) { delete(true) }
            Button("保留持仓并删除") { delete(false) }
            Button("取消", role: .cancel) {}
        } message: {
            Text("检测到 (openPositions.count) 个未平仓位。删除策略不会自动停止或管理这些仓位，请选择是否先平仓。")
        }
    }
}

struct RailModule<Content: View>: View {
    let title: String
    let icon: String
    let content: () -> Content

    init(title: String, icon: String, @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.icon = icon
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: icon).font(.headline)
            content()
        }.padding(12).frame(maxWidth: .infinity, alignment: .leading).background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 8))
    }
}

struct RailRow<Content: View>: View {
    let content: () -> Content

    init(@ViewBuilder content: @escaping () -> Content) {
        self.content = content
    }

    var body: some View { HStack(spacing: 6) { content() }.font(.caption2).padding(.vertical, 3) }
}

struct RailEmpty: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 34) }
}

struct MarketSidebar: View {
    @ObservedObject var model: DashboardModel
    @Binding var searchText: String
    @State private var category = "全部"
    @FocusState private var searchFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "waveform.path.ecg.rectangle.fill").font(.title2.weight(.bold)).foregroundStyle(.mint)
                VStack(alignment: .leading, spacing: 2) { Text("NOVA TRADE").font(.headline.weight(.bold)).tracking(1.2); Text("永续合约工作台").font(.caption).foregroundStyle(.secondary) }
            }.padding(.horizontal, 18).padding(.top, 20).padding(.bottom, 18)
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(searchFocused ? .mint : .secondary)
                TextField("搜索合约", text: $searchText)
                    .textFieldStyle(.plain)
                    .focused($searchFocused)
                    .onSubmit { searchFocused = false }
                if !searchText.isEmpty { Button { searchText = ""; searchFocused = true } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.plain).foregroundStyle(.secondary) }
            }
            .padding(9)
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture().onEnded { searchFocused = true })
            .background(Color.white.opacity(searchFocused ? 0.1 : 0.06), in: RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7).stroke(searchFocused ? Color.mint.opacity(0.55) : .clear))
            .padding(.horizontal, 14).padding(.bottom, 12)
            Picker("合约分组", selection: $category) { Text("全部").tag("全部"); Text("热门").tag("热门"); Text("涨幅").tag("涨幅"); Text("跌幅").tag("跌幅") }
                .pickerStyle(.segmented).controlSize(.small).padding(.horizontal, 14).padding(.bottom, 12)
            HStack { Text("自选").font(.caption.weight(.semibold)).foregroundStyle(.secondary); Spacer(); Text("\(model.favoriteContracts.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
                .padding(.horizontal, 18).padding(.bottom, 8)
            ScrollView {
                VStack(spacing: 3) {
                    ForEach(filtered(model.favoriteContracts)) { contract in
                        ContractRow(contract: contract, selected: model.selectedContract == contract.id, isFavorite: true) { model.select(contract) } toggleFavorite: { model.toggleFavorite(contract) }
                    }
                    if !model.otherContracts.isEmpty { Text("全部合约").font(.caption.weight(.semibold)).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 18).padding(.horizontal, 18).padding(.bottom, 6) }
                    ForEach(filtered(model.contracts(for: category).filter { !model.favoriteIDs.contains($0.id) })) { contract in
                        ContractRow(contract: contract, selected: model.selectedContract == contract.id, isFavorite: false) { model.select(contract) } toggleFavorite: { model.toggleFavorite(contract) }
                    }
                }.padding(.horizontal, 8)
            }
            Spacer(minLength: 12)
        }.background(Color.sidebarBackground).navigationSplitViewColumnWidth(min: 245, ideal: 270, max: 310)
    }
    private func filtered(_ input: [PerpetualContract]) -> [PerpetualContract] {
        guard !searchText.isEmpty else { return input }
        return input.filter { $0.id.localizedCaseInsensitiveContains(searchText) || $0.name.localizedCaseInsensitiveContains(searchText) || $0.shortName.localizedCaseInsensitiveContains(searchText) }
    }
}

struct ContractRow: View {
    let contract: PerpetualContract
    let selected: Bool
    let isFavorite: Bool
    let action: () -> Void
    let toggleFavorite: () -> Void
    var body: some View {
        HStack(spacing: 6) {
            Button(action: action) {
                HStack(spacing: 6) {
                    RoundedRectangle(cornerRadius: 5).fill(contract.accent.opacity(0.2)).frame(width: 24, height: 24).overlay(Text(contract.shortName.prefix(1)).font(.caption.weight(.bold)).foregroundStyle(contract.accent))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(contract.pairLabel)
                            .font(.system(size: 12, weight: .semibold))
                            .minimumScaleFactor(0.8)
                        Text(contract.category + " · 永续").font(.caption2).foregroundStyle(.secondary)
                    }
                    .lineLimit(1)
                    .layoutPriority(1)
                    Spacer(minLength: 2)
                    VStack(alignment: .trailing, spacing: 3) {
                        Text(formatPrice(contract.price))
                            .font(.system(size: 11, design: .monospaced))
                            .minimumScaleFactor(0.8)
                        Text(String(format: "%+.2f%%", contract.change))
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(contract.change >= 0 ? marketRiseColor : marketFallColor)
                    }
                    .lineLimit(1)
                    .layoutPriority(1)
                    .help(formatPrice(contract.price))
                }
            }.buttonStyle(.plain).frame(maxWidth: .infinity, alignment: .leading)
            Button(action: toggleFavorite) { Image(systemName: isFavorite ? "star.fill" : "star").font(.caption).foregroundStyle(isFavorite ? .yellow : .secondary) }.buttonStyle(.plain).help(isFavorite ? "取消收藏" : "收藏合约")
        }
        .padding(.vertical, 9).padding(.horizontal, 9)
        .background(selected ? Color.white.opacity(0.1) : .clear, in: RoundedRectangle(cornerRadius: 7))
        .overlay(alignment: .leading) { if selected { Capsule().fill(.mint).frame(width: 3, height: 26) } }
    }
}

struct TopBar: View {
    @ObservedObject var model: DashboardModel
    @Binding var showingNewStrategy: Bool
    var body: some View {
        HStack(spacing: 18) {
            HStack(spacing: 10) {
                Image(systemName: model.accountOverview.mode == .live ? "bolt.shield.fill" : "flask.fill")
                    .font(.title3).foregroundStyle(model.accountOverview.mode == .live ? .orange : .mint)
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.accountOverview.mode == .live ? "实盘账户" : "模拟账户").font(.headline)
                    HStack(spacing: 5) {
                        Text(accountLabel).lineLimit(1)
                        Image(systemName: "link").font(.caption2)
                        Text(model.accountOverview.authenticated ? "OKX 已连接" : "OKX 未连接")
                    }.font(.caption).foregroundStyle(model.accountOverview.authenticated ? .mint : .orange)
                }
            }
            Divider().frame(height: 30)
            topMetric("总资产估值", model.accountOverview.totalAssetValueUSD.map(formatUSD) ?? "--")
            topMetric("今日收益", model.accountOverview.todayPnLUSD.map(formatUSD) ?? "--", color: (model.accountOverview.todayPnLUSD ?? 0) >= 0 ? .green : .red)
            Spacer(minLength: 8)
            HStack(spacing: 8) {
                Circle().fill(model.serviceState.color).frame(width: 8, height: 8)
                Text(model.serviceState.title).font(.caption.weight(.semibold)).foregroundStyle(model.serviceState.color)
                Button(model.serviceState == .running || model.serviceState == .starting ? "停止服务" : "启动服务") {
                    model.toggleBackend()
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(model.serviceState == .stopping)
            }
        }
        .padding(.horizontal, 24).padding(.vertical, 12)
        .background(Color.appBackground)
    }

    private var accountLabel: String {
        let parts = [model.accountOverview.profile, model.accountOverview.label, model.accountOverview.site].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? "等待 OKX CLI 登录信息" : parts.joined(separator: " · ")
    }

    private func topMetric(_ title: String, _ value: String, color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit()).foregroundStyle(color)
        }
    }
}

struct MarketHeader: View {
    @ObservedObject var model: DashboardModel

    private var loadedCandles: [Candle] { model.marketSnapshot?.candles ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 24) {
                    contractIdentity
                    Spacer(minLength: 8)
                    priceSummary
                }
                .fixedSize(horizontal: true, vertical: false)
                VStack(alignment: .leading, spacing: 7) {
                    contractIdentity
                    priceSummary
                }
            }
            HStack(spacing: 18) {
                rangeMetric("区间最高", value: loadedCandles.map(\.high).max().map(chartPriceText) ?? "--")
                rangeMetric("区间最低", value: loadedCandles.map(\.low).min().map(chartPriceText) ?? "--")
                rangeMetric("24h 成交量", value: model.selected?.volume ?? "--")
                Spacer(minLength: 0)
            }
            .help("区间高低取当前周期已加载的 \(loadedCandles.count) 根真实 K 线")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var contractIdentity: some View {
        Group {
            if let selected = model.selected {
                HStack(spacing: 8) {
                    RoundedRectangle(cornerRadius: 7)
                        .fill(selected.accent.opacity(0.18)).frame(width: 30, height: 30)
                        .overlay(Text(selected.shortName.prefix(1)).font(.headline.weight(.bold)).foregroundStyle(selected.accent))
                    Text(selected.pairLabel).font(.title3.weight(.bold))
                    Text("永续").font(.caption2.weight(.semibold)).foregroundStyle(.mint)
                        .padding(.horizontal, 6).padding(.vertical, 3).background(.mint.opacity(0.12), in: Capsule())
                }
            } else {
                Label("等待实时合约数据", systemImage: "clock.arrow.circlepath")
                    .font(.title3.weight(.semibold)).foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private var priceSummary: some View {
        let change = model.currentChange ?? 0
        return HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(model.currentPrice.map(formatPrice) ?? "--")
                .font(.system(size: 27, weight: .bold, design: .rounded)).monospacedDigit()
                .foregroundStyle(model.currentChange == nil ? .secondary : (change >= 0 ? marketRiseColor : marketFallColor))
            Text(model.currentChange.map { String(format: "%+.2f%%", $0) } ?? "--")
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundStyle(model.currentChange == nil ? .secondary : (change >= 0 ? marketRiseColor : marketFallColor))
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private func rangeMetric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit())
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }
}

struct AccountPanel: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(spacing: 18) {
                HStack(spacing: 10) {
                    Image(systemName: model.accountOverview.mode == .live ? "bolt.shield.fill" : "flask.fill")
                        .font(.title3).foregroundStyle(model.accountOverview.mode == .live ? .orange : .mint)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(model.accountOverview.mode == .live ? "实盘账户" : "模拟账户").font(.headline)
                        Text([model.accountOverview.profile, model.accountOverview.label, model.accountOverview.site].compactMap { $0 }.joined(separator: " · ").isEmpty ? "等待 OKX CLI 登录信息" : [model.accountOverview.profile, model.accountOverview.label, model.accountOverview.site].compactMap { $0 }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Divider().frame(height: 30)
                accountMetric("账户权益", value: model.accountOverview.equityUSD.map { formatUSD($0) } ?? "--")
                accountMetric("可用权益", value: model.accountOverview.availableEquityUSD.map { formatUSD($0) } ?? "--")
                accountMetric("资产币种", value: "\(model.accountOverview.assets.count)")
                Spacer()
                Label(model.accountOverview.authenticated ? "CLI 已认证" : "未认证", systemImage: model.accountOverview.authenticated ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                    .font(.caption.weight(.semibold)).foregroundStyle(model.accountOverview.authenticated ? .mint : .orange)
            }
            Divider().overlay(Color.white.opacity(0.06))
            if model.accountOverview.assets.isEmpty {
                Text("暂无资产明细").font(.caption).foregroundStyle(.secondary)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 9) {
                        ForEach(model.accountOverview.assets) { asset in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(asset.currency).font(.caption.weight(.bold).monospaced())
                                Text("总余额 \(formatAsset(asset.equity))").font(.caption2.monospacedDigit())
                                Text("可用 \(formatAsset(asset.available))").font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                                if let usdValue = asset.usdValue {
                                    Text(formatUSD(usdValue)).font(.caption2.monospacedDigit()).foregroundStyle(.mint)
                                }
                            }
                            .frame(minWidth: 118, alignment: .leading)
                            .padding(.horizontal, 10).padding(.vertical, 8)
                            .background(Color.white.opacity(0.045), in: RoundedRectangle(cornerRadius: 7))
                        }
                    }
                }
            }
        }
        .padding(15)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).stroke(model.accountOverview.mode == .live ? Color.orange.opacity(0.35) : Color.mint.opacity(0.22)))
    }

    private func accountMetric(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 3) { Text(title).font(.caption2).foregroundStyle(.secondary); Text(value).font(.subheadline.weight(.semibold).monospacedDigit()) }
    }
}

private func formatAsset(_ value: Decimal) -> String {
    let number = NSDecimalNumber(decimal: value).doubleValue
    if abs(number) >= 1000 { return String(format: "%,.2f", number) }
    if abs(number) >= 1 { return String(format: "%.6f", number).replacingOccurrences(of: #"0+$"#, with: "", options: .regularExpression).replacingOccurrences(of: #"\.$"#, with: "", options: .regularExpression) }
    return String(format: "%.8f", number).replacingOccurrences(of: #"0+$"#, with: "", options: .regularExpression).replacingOccurrences(of: #"\.$"#, with: "", options: .regularExpression)
}

private func formatPnL(_ value: Decimal) -> String {
    String(format: "%+.2f", NSDecimalNumber(decimal: value).doubleValue)
}

private func decimalDouble(_ value: Decimal) -> Double {
    NSDecimalNumber(decimal: value).doubleValue
}

private func decimalText(_ value: Decimal) -> String {
    String(format: "%.4f", decimalDouble(value))
}

private func formatUSD(_ value: Decimal) -> String {
    let number = NSDecimalNumber(decimal: value).doubleValue
    let formatter = NumberFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.numberStyle = .currency
    formatter.currencyCode = "USD"
    formatter.currencySymbol = "$"
    formatter.minimumFractionDigits = 2
    formatter.maximumFractionDigits = 2
    return formatter.string(from: NSNumber(value: number)) ?? String(format: "$%.2f", number)
}

private func chartPriceText(_ value: Decimal) -> String {
    formatPrice(NSDecimalNumber(decimal: value).doubleValue)
}

struct Metric: View {
    let label: String; let value: String; let color: Color
    var body: some View { VStack(alignment: .trailing, spacing: 5) { Text(label).font(.caption).foregroundStyle(.secondary); Text(value).font(.subheadline.weight(.semibold).monospacedDigit()).foregroundStyle(color) } }
}

struct ChartPanel: View {
    @ObservedObject var model: DashboardModel
    var chartHeight: CGFloat = 438
    @State private var showVolume = true
    @State private var showEMA = true
    @State private var showGrid = true
    @State private var chartResetToken = UUID()
    var body: some View {
        VStack(spacing: 0) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) {
                    intervalControls
                    Spacer(minLength: 0)
                    indicatorControls
                }.fixedSize(horizontal: true, vertical: false)
                VStack(alignment: .leading, spacing: 8) {
                    intervalControls
                    HStack {
                        indicatorControls
                        Spacer(minLength: 0)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12).padding(.vertical, 9)
            Divider().overlay(Color.white.opacity(0.07))
            NativeCandleChart(snapshot: model.marketSnapshot, showVolume: showVolume, showEMA: showEMA, showGrid: showGrid)
                .id(chartResetToken)
                .frame(height: chartHeight)
                .padding(.horizontal, 10)
                .padding(.top, 10)
            TimelineView(.periodic(from: .now, by: 1)) { timeline in
                let stale = model.candleDataSource == .websocket && model.candleLastUpdatedAt.map { timeline.date.timeIntervalSince($0) > 30 } == true
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) {
                        candleStatus(stale: stale)
                        Spacer(minLength: 6)
                        Text("拖拽平移 · 双指缩放 · 悬停查看 OHLC").foregroundStyle(.secondary)
                    }
                    candleStatus(stale: stale)
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 17)
            .padding(.bottom, 11)
        }.background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10)).overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.07)))
    }

    private var intervalControls: some View {
        HStack(spacing: 4) {
            Label("K线", systemImage: "chart.bar.xaxis")
                .font(.caption.weight(.semibold)).foregroundStyle(.primary)
                .padding(.trailing, 3)
            Divider().frame(height: 17).padding(.trailing, 3)
            ForEach(ChartInterval.allCases) { interval in
                Button(interval.rawValue) {
                    model.selectInterval(interval)
                    chartResetToken = UUID()
                }
                .buttonStyle(.plain)
                .font(.caption.weight(.medium).monospacedDigit())
                .foregroundStyle(model.selectedInterval == interval ? .white : .secondary)
                .padding(.horizontal, 7).padding(.vertical, 5)
                .background(model.selectedInterval == interval ? Color.white.opacity(0.14) : .clear, in: RoundedRectangle(cornerRadius: 4))
                .fixedSize(horizontal: true, vertical: false)
            }
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private var indicatorControls: some View {
        HStack(spacing: 10) {
            Toggle(isOn: $showEMA) { Label("EMA", systemImage: "chart.line.uptrend.xyaxis") }
                .toggleStyle(.button).font(.caption2).tint(.orange)
            Toggle(isOn: $showVolume) { Label("量", systemImage: "chart.bar.xaxis") }
                .toggleStyle(.button).font(.caption2).tint(.mint)
            Toggle(isOn: $showGrid) { Image(systemName: "square.grid.3x3") }
                .toggleStyle(.button).help("网格")
            Button { chartResetToken = UUID() } label: {
                Label("自动", systemImage: "arrow.up.left.and.arrow.down.right")
            }
            .buttonStyle(.plain).font(.caption2).foregroundStyle(.secondary)
            .help("自动缩放到最新行情")
        }
        .lineLimit(1)
        .fixedSize(horizontal: true, vertical: false)
    }

    private func candleStatus(stale: Bool) -> some View {
        HStack(spacing: 8) {
            Label(stale ? "OKX WSS · 行情暂未更新" : model.candleDataSource.label, systemImage: model.candleDataSource.icon)
                .foregroundStyle(model.candleDataSource == .websocket && !stale ? .mint : .orange)
                .help("历史 K 线首次通过 API 加载；实时 K 柱仅由 OKX WSS candle 频道更新")
            if let updated = model.candleLastUpdatedAt {
                Text(updated.formatted(.dateTime.hour(.twoDigits(amPM: .omitted)).minute().second()))
                    .monospacedDigit().foregroundStyle(.secondary)
            }
        }
        .lineLimit(1)
    }
}

struct MarketInsightStrip: View {
    @ObservedObject var model: DashboardModel

    private var candles: [Candle] { Array((model.marketSnapshot?.candles ?? []).suffix(24)) }
    private var latest: Candle? { candles.last }

    var body: some View {
        HStack(spacing: 0) {
            insight("最新收盘", value: latest.map { chartPriceText($0.close) } ?? "--", detail: latest?.confirmed == true ? "已收盘" : "当前 K 线")
            insight("区间高低", value: rangeText, detail: "最近 \(candles.count) 根")
            insight("成交量", value: volumeText, detail: "K 线累计")
            insight("K 线状态", value: latest == nil ? "等待数据" : latest?.confirmed == true ? "已确认" : model.candleDataSource == .websocket ? "实时更新" : "等待推送", detail: model.candleDataSource.label)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 14)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.07)))
    }

    private var rangeText: String {
        guard let low = candles.map(\.low).min(), let high = candles.map(\.high).max() else { return "--" }
        return "\(chartPriceText(low)) – \(chartPriceText(high))"
    }

    private var volumeText: String {
        let total = candles.reduce(Decimal.zero) { $0 + $1.volume }
        return formatCompact(decimalDouble(total))
    }

    private func insight(_ title: String, value: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit())
            Text(detail).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .overlay(alignment: .trailing) { Rectangle().fill(Color.white.opacity(0.08)).frame(width: 1, height: 38) }
    }
}

struct StrategySection: View {
    @ObservedObject var model: DashboardModel
    @Binding var showingNewStrategy: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) { Text("策略中心").font(.title3.weight(.bold)); Text("管理已配置策略与实时运行状态").font(.caption).foregroundStyle(.secondary) }
                Spacer(); Text("\(model.runningStrategies.count) 个策略运行中").font(.caption.weight(.medium)).foregroundStyle(.mint); Button { showingNewStrategy = true } label: { Label("添加", systemImage: "plus") }.buttonStyle(.bordered).controlSize(.small)
            }
            HStack(spacing: 12) {
                ForEach(model.strategies) { strategy in
                    StrategyCard(strategy: strategy, model: model, toggle: { model.toggleStrategy(strategy.id) }, delete: { closePositions in
                        if let serviceID = strategy.serviceID { model.deleteStrategy(serviceID, closePositions: closePositions) }
                    })
                }
            }
        }
    }
}

struct StrategyCard: View {
    let strategy: TradingStrategy
    @ObservedObject var model: DashboardModel
    let toggle: () -> Void
    let delete: (Bool) -> Void
    @State private var showingDeleteConfirmation = false
    @State private var showingPositionDeleteConfirmation = false

    private var poolEquityText: String {
        guard let pool = model.strategyPool(for: strategy) else { return "--" }
        return formatUSD(pool.equity)
    }

    private var occupiedFundsText: String {
        model.occupiedFunds(for: strategy).map(formatUSD) ?? "--"
    }

    private var openPositions: [PositionSnapshot] { model.strategyOpenPositions(for: strategy) }

    private func requestDelete() {
        guard strategy.state != .running else {
            model.errorMessage = "策略运行中，请先停止策略后再删除"
            return
        }
        if openPositions.isEmpty {
            showingDeleteConfirmation = true
        } else {
            showingPositionDeleteConfirmation = true
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Image(systemName: strategy.state == .running ? "bolt.fill" : "pause.fill").foregroundStyle(strategy.state.color)
                Text(strategy.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                Spacer()
                Circle().fill(strategy.state.color).frame(width: 7, height: 7)
                Text(strategy.state.rawValue).font(.caption2.weight(.medium)).foregroundStyle(strategy.state.color)
                if strategy.serviceID != nil {
                    Button { requestDelete() } label: { Image(systemName: "trash") }
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .opacity(strategy.state == .running ? 0.45 : 1)
                        .disabled(strategy.state == .running)
                        .help(strategy.state == .running ? "请先停止策略" : "删除策略实例")
                    }
            }
            Text(strategy.symbol).font(.caption.monospaced()).foregroundStyle(.secondary)
            Divider().overlay(Color.white.opacity(0.08))
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 4) { Text("累计收益").font(.caption2).foregroundStyle(.secondary); Text(String(format: "%+.2f USDT", strategy.pnl)).font(.headline.monospacedDigit()).foregroundStyle(strategy.pnl >= 0 ? .green : .red) }
                Spacer()
                VStack(alignment: .trailing, spacing: 4) {
                    Text("资金池 \(poolEquityText)").font(.caption2).foregroundStyle(.secondary)
                    Text("已占用 \(occupiedFundsText)").font(.caption2).foregroundStyle(.secondary)
                    Text(String(format: "单笔风险 %.1f%%", strategy.allocation)).font(.caption2).foregroundStyle(.secondary)
                    Button(strategy.state == .running ? "暂停" : "启动", action: toggle).buttonStyle(.bordered).controlSize(.mini)
                }
            }
        }
        .padding(15)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).stroke(strategy.state == .running ? Color.mint.opacity(0.25) : Color.white.opacity(0.07)))
        .confirmationDialog("删除策略实例？", isPresented: $showingDeleteConfirmation, titleVisibility: .visible) {
            Button("删除", role: .destructive) { delete(false) }
            Button("取消", role: .cancel) {}
        } message: {
            Text("删除后不会再自动运行该规则，但历史挂单和成交记录会保留。")
        }
        .confirmationDialog("策略存在未平仓位", isPresented: $showingPositionDeleteConfirmation, titleVisibility: .visible) {
            Button("平仓并删除", role: .destructive) { delete(true) }
            Button("保留持仓并删除") { delete(false) }
            Button("取消", role: .cancel) {}
        } message: {
            Text("检测到 (openPositions.count) 个未平仓位。删除策略不会自动停止或管理这些仓位，请选择是否先平仓。")
        }
    }
}

struct OperationsSection: View {
    @ObservedObject var model: DashboardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("交易运营").font(.title3.weight(.bold))
                Spacer()
                Text("OKX 模拟盘 · 订单由交易所记录").font(.caption.weight(.medium)).foregroundStyle(.secondary)
            }
            HStack(alignment: .top, spacing: 12) {
                operationPanel("持仓", systemImage: "chart.bar.xaxis", empty: "暂无 OKX 模拟持仓") {
                    ForEach(model.positions) { position in
                        HStack {
                            Text(position.instrumentID).font(.caption.monospaced())
                            Text(position.side.uppercased()).font(.caption2.weight(.semibold)).foregroundStyle(position.side.lowercased() == "buy" || position.side.lowercased() == "long" ? .green : .red)
                            Spacer()
                            Text(decimalText(position.quantity)).font(.caption.monospacedDigit())
                            Text(String(format: "%+.2f", decimalDouble(position.unrealizedPnL))).font(.caption.monospacedDigit()).foregroundStyle(decimalDouble(position.unrealizedPnL) >= 0 ? .green : .red)
                        }
                    }
                }
                operationPanel("挂单", systemImage: "list.bullet.rectangle", empty: "暂无挂单") {
                    ForEach(model.orders) { order in
                        HStack {
                            Text(order.instrumentID).font(.caption.monospaced())
                            Text(order.side.uppercased()).font(.caption2.weight(.semibold))
                            Spacer()
                            Text(decimalText(order.quantity)).font(.caption.monospacedDigit())
                            Text(order.status).font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
                operationPanel("交易流水", systemImage: "arrow.left.arrow.right", empty: "暂无 OKX 模拟成交") {
                    ForEach(model.fills.prefix(5)) { fill in
                        HStack {
                            Text(fill.timestamp.formatted(date: .omitted, time: .shortened)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                            Spacer()
                            Text(decimalText(fill.quantity)).font(.caption.monospacedDigit())
                            Text("费 \(decimalText(fill.fee))").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Label("运行日志", systemImage: "text.alignleft").font(.headline)
                    Spacer()
                    Text("\(model.runtimeLogs.count) 条").font(.caption2).foregroundStyle(.secondary)
                }
                if model.runtimeLogs.isEmpty {
                    Text("等待服务事件…").font(.caption).foregroundStyle(.secondary).padding(.vertical, 8)
                } else {
                    ForEach(model.runtimeLogs.prefix(6)) { log in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(log.timestamp.formatted(date: .omitted, time: .standard)).font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                            Text(log.level.uppercased()).font(.caption2.weight(.bold)).foregroundStyle(log.level.lowercased() == "error" ? .red : .mint)
                            Text(log.message).font(.caption).lineLimit(1)
                        }
                    }
                }
            }
            .padding(15)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 9))
            .overlay(RoundedRectangle(cornerRadius: 9).stroke(Color.white.opacity(0.07)))
        }
    }

    @ViewBuilder
    private func operationPanel<Content: View>(_ title: String, systemImage: String, empty: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            Label(title, systemImage: systemImage).font(.headline)
            if title == "持仓" && model.positions.isEmpty || title == "挂单" && model.orders.isEmpty || title == "交易流水" && model.fills.isEmpty {
                Text(empty).font(.caption).foregroundStyle(.secondary).padding(.vertical, 10)
            } else {
                content()
            }
        }
        .padding(15)
        .frame(maxWidth: .infinity, minHeight: 104, alignment: .topLeading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 9))
        .overlay(RoundedRectangle(cornerRadius: 9).stroke(Color.white.opacity(0.07)))
    }

    private func decimalDouble(_ value: Decimal) -> Double { NSDecimalNumber(decimal: value).doubleValue }
    private func decimalText(_ value: Decimal) -> String { String(format: "%.4f", decimalDouble(value)) }
}

struct NewStrategySheet: View {
    @ObservedObject var model: DashboardModel
    @Environment(\.dismiss) private var dismiss
    @State private var allocation = 1.0
    @State private var capitalPoolPercent = 100.0
    @State private var selectedRule: StrategyType = .sweepReversalShort

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack { Text("新建策略").font(.title2.weight(.bold)); Spacer(); Button("取消") { dismiss() }.buttonStyle(.plain).foregroundStyle(.secondary) }
            Text("启动后按信号向当前 OKX 模拟账户提交入场单，并按策略规则执行保护止损、止盈和时间离场。").font(.caption).foregroundStyle(.secondary)
            Form {
                Picker("策略规则", selection: $selectedRule) {
                    ForEach(StrategyType.availableCases, id: \.self) { strategyType in
                        Text("\(strategyType.displayName)\(model.hasStrategyType(strategyType) ? "（已有实例）" : "")")
                            .tag(strategyType)
                    }
                }
                LabeledContent("扫描范围") {
                    Text(scopeDescription)
                        .foregroundStyle(.secondary)
                }
                Text("策略运行期间会定期刷新合规山寨币成交额榜，自动跟踪命中的合约，不需要手动指定币种。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                VStack(alignment: .leading) {
                    Text("单笔风险 \(String(format: "%.1f", effectiveRisk))%")
                    Slider(value: $allocation, in: 0.1...maxRiskForRule, step: 0.1)
                }
                VStack(alignment: .leading) {
                    Text("策略资金池 \(String(format: "%.0f", capitalPoolPercent))%")
                    Slider(value: $capitalPoolPercent, in: 1...100, step: 1)
                }
            }.formStyle(.grouped)
        .onChange(of: selectedRule) { _, _ in allocation = min(allocation, maxRiskForRule) }
            Spacer(); HStack { Spacer(); Button("创建并保存") { create() }.buttonStyle(.borderedProminent).tint(.mint).disabled(!canCreate) }
        }
        .padding(24)
        .frame(width: 560, height: 480)
        .preferredColorScheme(.dark)
    }

    private var canCreate: Bool {
        !model.hasStrategyType(selectedRule)
    }

    private var effectiveRisk: Double { min(allocation, maxRiskForRule) }

    /// 实验室规则给出的单笔风险上限：扫顶 1%，双均线多头 0.5%。
    private var maxRiskForRule: Double { selectedRule == .emaAltcoinLong ? 0.5 : 1.0 }

    private var scopeDescription: String { "动态扫描热门榜前 20 个山寨币" }

    private func create() {
        // 参数默认值来自领域层（与实验室 config 对齐），不再在界面里重复一份。
        let config = StrategyConfig(name: selectedRule.displayName, scope: .dynamic(.hotAltcoins), interval: .oneHour, type: selectedRule, parameters: selectedRule.defaultParameters, enabled: false, riskPercent: effectiveRisk, capitalPoolPercent: capitalPoolPercent, cooldownBars: 96)
        Task {
            do { _ = try await model.createStrategy(config); dismiss() }
            catch { model.errorMessage = error.localizedDescription }
        }
    }
}

private func formatPrice(_ value: Double) -> String {
    let formatter = NumberFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.numberStyle = .decimal
    formatter.usesGroupingSeparator = true
    if abs(value) > 0 && abs(value) < 1 {
        formatter.usesSignificantDigits = true
        formatter.minimumSignificantDigits = 3
        formatter.maximumSignificantDigits = 6
    } else {
        formatter.minimumFractionDigits = abs(value) < 10 ? 3 : 2
        formatter.maximumFractionDigits = formatter.minimumFractionDigits
    }
    return formatter.string(from: NSNumber(value: value)) ?? String(format: "%.2f", value)
}

extension Color {
    static let appBackground = Color(red: 0.055, green: 0.065, blue: 0.08)
    static let sidebarBackground = Color(red: 0.065, green: 0.075, blue: 0.09)
    static let panelBackground = Color(red: 0.09, green: 0.102, blue: 0.12)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.unhide(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            NSApplication.shared.setActivationPolicy(.regular)
            NSApplication.shared.unhide(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
            NSApplication.shared.windows.first?.makeKeyAndOrderFront(nil)
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.unhide(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct MacTraderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene { WindowGroup("NovaTrade") { DashboardView() }.defaultSize(width: 1480, height: 900) }
}
