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
    case emaAltcoinCandidates = "双均线候选山寨币（前 50）"
    case hlsrCandidates = "高位扫顶候选山寨币"
    case doublePumpCandidates = "翻倍衰竭候选山寨币"
    case highGain60 = "高涨幅币（24h +60%）"
    case highGain100 = "高涨幅币（24h +100%）"

    var id: String { rawValue }

    var domainValue: StrategyUniverseCategory {
        switch self {
        case .mainstream: return .mainstream
        case .hotAltcoins: return .hotAltcoins
        case .emaAltcoinCandidates: return .emaAltcoinCandidates
        case .hlsrCandidates: return .hlsrCandidates
        case .doublePumpCandidates: return .doublePumpCandidates
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
    var stopDescription: String
    var state: StrategyState
    var pnl: Double
    var allocation: Double
    var poolAllocation: Double
    var updatedAt: Date = .now
    init(serviceID: UUID? = nil, name: String, symbol: String, rule: String, stopDescription: String = "动态止损", state: StrategyState, pnl: Double, allocation: Double, poolAllocation: Double = 100) {
        self.serviceID = serviceID; self.name = name; self.symbol = symbol; self.rule = rule; self.stopDescription = stopDescription; self.state = state; self.pnl = pnl; self.allocation = allocation; self.poolAllocation = poolAllocation
    }
}

@MainActor
final class DashboardModel: ObservableObject {
    /// 策略类型 → 界面规则描述
    nonisolated static func ruleLabel(_ type: StrategyType) -> String {
        switch type {
        case .sweepReversalShort: return "山寨币二次扫顶做空"
        case .emaAltcoinLong: return "双均线交易山寨币做多"
        case .hlsr: return "高位扫顶反转做空"
        case .doublePumpExhaustionShort: return "日内翻倍动能衰竭确认做空"
        case .external(let identifier): return "\(identifier)（运行时处理器不可用）"
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
    @Published private(set) var isResettingRisk = false
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
    private let selectedContractStorageKey = "nova.trade.selectedContract"
    private static let defaultContractID = "BTC-USDT-SWAP"

    init() {
        if let saved = UserDefaults.standard.string(forKey: favoriteStorageKey), !saved.isEmpty {
            favoriteIDs = Set(saved.split(separator: ",").map(String.init))
        }
        if let saved = UserDefaults.standard.string(forKey: selectedContractStorageKey), !saved.isEmpty {
            selectedContract = saved
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
        switch category {
        case "主流": return source.filter(Self.isMainstream).sorted { Self.compactVolume($0.volume) > Self.compactVolume($1.volume) }
        case "热门": return source.sorted { Self.compactVolume($0.volume) > Self.compactVolume($1.volume) }.prefix(20).map { $0 }
        case "合约", "全部": return source
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
            return source.filter(Self.isMainstream).sorted { Self.compactVolume($0.volume) > Self.compactVolume($1.volume) }
        case .hotAltcoins:
            let altcoins = source.filter(Self.isEligibleHotAltcoin)
            return Array(altcoins.sorted { Self.compactVolume($0.volume) > Self.compactVolume($1.volume) }.prefix(20))
        case .emaAltcoinCandidates:
            let altcoins = source.filter(Self.isEligibleHotAltcoin)
            return Array(altcoins.sorted { Self.compactVolume($0.volume) > Self.compactVolume($1.volume) }.prefix(50))
        case .hlsrCandidates:
            return source
                .filter { StrategyUniverseRules.isEligibleHLSR(Self.domainContract(from: $0)) }
                .sorted { $0.change > $1.change }
        case .doublePumpCandidates:
            return source
                .filter { StrategyUniverseRules.isEligibleDoublePump(Self.domainContract(from: $0)) }
                .sorted { $0.change > $1.change }
        case .highGain60:
            return source.filter { Self.isEligibleHotAltcoin($0) && $0.change >= 60 }.sorted { $0.change > $1.change }
        case .highGain100:
            return source.filter { Self.isEligibleHotAltcoin($0) && $0.change >= 100 }.sorted { $0.change > $1.change }
        }
    }

    private static func isMainstream(_ contract: PerpetualContract) -> Bool {
        StrategyUniverseRules.mainstreamSymbols.contains(contract.shortName.uppercased())
    }

    private static func isEligibleHotAltcoin(_ contract: PerpetualContract) -> Bool {
        let parts = contract.id.split(separator: "-")
        let quote = parts.count > 1 ? String(parts[1]) : ""
        return StrategyUniverseRules.isEligibleHotAltcoin(instrumentID: contract.id, baseCurrency: contract.shortName, quoteCurrency: quote)
    }

    private static func domainContract(from contract: PerpetualContract) -> ContractMarket {
        ContractMarket(id: contract.id,
                       name: contract.name,
                       baseCurrency: contract.shortName,
                       quoteCurrency: contract.id.split(separator: "-").dropFirst().first.map(String.init) ?? "USDT",
                       last: Decimal(contract.price),
                       changePercent: Decimal(contract.change),
                       volume24h: Decimal(Self.compactVolume(contract.volume)),
                       category: contract.category)
    }

    private static func compactVolume(_ value: String) -> Double {
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
        UserDefaults.standard.set(selectedContract, forKey: selectedContractStorageKey)
        snapshot = SystemSnapshot(mode: snapshot.mode, connection: snapshot.connection, ticker: nil, account: snapshot.account)
        requestMarket()
    }

    private func restoreSelectedContract(from availableContracts: [PerpetualContract]) {
        guard !availableContracts.isEmpty else { return }
        let availableIDs = Set(availableContracts.map(\.id))
        let restoredID = availableIDs.contains(selectedContract)
            ? selectedContract
            : (availableIDs.contains(Self.defaultContractID) ? Self.defaultContractID : availableContracts[0].id)
        selectedContract = restoredID
        UserDefaults.standard.set(restoredID, forKey: selectedContractStorageKey)
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
            let remoteContracts = try await client.contracts()
            guard generation == lifecycleGeneration, autoStartBackend else {
                isRefreshing = false
                return
            }
            if !remoteContracts.isEmpty { contracts = remoteContracts.map(PerpetualContract.init(remote:)) }
            restoreSelectedContract(from: contracts)
            requestMarket()
            startContractsRefresh()
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
                    TradingStrategy(serviceID: config.id, name: config.displayName, symbol: config.scope.displayName, rule: DashboardModel.ruleLabel(config.type), stopDescription: config.type.stopLossDescription, state: config.enabled ? .running : .paused, pnl: 0, allocation: config.riskPercent, poolAllocation: config.capitalPoolPercent)
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

    /// Requests a manual risk reset from the local service. The service keeps
    /// the switch latched until the next UTC calendar day, so a successful request
    /// may still return a snapshot with `killSwitch == true`.
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
        strategies.insert(TradingStrategy(serviceID: created.id, name: created.displayName, symbol: created.scope.displayName, rule: DashboardModel.ruleLabel(created.type), stopDescription: created.type.stopLossDescription, state: .paused, pnl: 0, allocation: created.riskPercent, poolAllocation: created.capitalPoolPercent), at: 0)
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
            case .emaAltcoinCandidates: uiCategory = .emaAltcoinCandidates
            case .hlsrCandidates: uiCategory = .hlsrCandidates
            case .doublePumpCandidates: uiCategory = .doublePumpCandidates
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
                let positionsToConfirm = positionsToClose
                if closePositions {
                    for position in positionsToConfirm {
                        let quantity = abs(position.quantity)
                        guard quantity > 0 else { continue }
                        if accountOverview.mode == .live {
                            _ = try await client.placeLiveOrder(LiveOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true))
                        } else {
                            _ = try await client.placePaperOrder(PaperOrderRequest(instrumentID: position.instrumentID, side: closeSide(for: position), quantity: quantity, reduceOnly: true))
                        }
                    }
                    // A reduce-only command is asynchronous. Poll until every
                    // position selected for closure is flat before allowing
                    // the backend to delete its owner.
                    let targetKeys = Set(positionsToConfirm.map { "\($0.instrumentID)|\($0.side.lowercased())" })
                    var remaining = positionsToConfirm
                    for attempt in 0..<20 {
                        let latest = try await client.privatePositions()
                        livePositions = latest
                        remaining = latest.filter { position in
                            targetKeys.contains("\(position.instrumentID)|\(position.side.lowercased())") && abs(position.quantity) > 0
                        }
                        if remaining.isEmpty { break }
                        if attempt < 19 { try await Task.sleep(for: .milliseconds(250)) }
                    }
                    guard remaining.isEmpty else {
                        errorMessage = "平仓尚未完成，请确认远端持仓归零后再删除策略"
                        return
                    }
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

    var body: some View {
        NavigationSplitView {
            MarketSidebar(model: model)
        } detail: {
            VStack(spacing: 0) {
                DashboardWindowHeader(model: model)
                AccountPanel(model: model)
                HStack(alignment: .top, spacing: 0) {
                    GeometryReader { geometry in
                        ScrollView {
                            VStack(alignment: .leading, spacing: 12) {
                                MarketHeader(model: model)
                                ChartPanel(model: model, chartHeight: max(280, geometry.size.height - 180))
                                MarketInsightStrip(model: model)
                            }.padding(16)
                        }
                    }
                    Divider().overlay(Color.white.opacity(0.07))
                    RightRail(model: model, showingNewStrategy: $showingNewStrategy)
                }
            }
            .background(Color.appBackground)
            .ignoresSafeArea(.container, edges: .top)
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
                    Label("策略状态", systemImage: "brain.head.profile")
                        .font(.headline)
                    Spacer()
                    Button { showingNewStrategy = true } label: { Image(systemName: "plus") }
                        .buttonStyle(.bordered).controlSize(.small).help("新建策略")
                }
                .padding(.vertical, 4)
                if model.riskSnapshot.killSwitch {
                    RiskAlertModule(model: model)
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

/// The account level kill switch is separate from each strategy's paused or
/// running state. Keep it visible above the strategy cards so a rejected start
/// has an actionable explanation and a reset entry point.
struct RiskAlertModule: View {
    @ObservedObject var model: DashboardModel
    @State private var resetFeedback: String?
    @State private var resetFeedbackIsError = false

    private var risk: RiskSnapshot { model.riskSnapshot }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Label("账户风控已熔断", systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.orange)
                Spacer(minLength: 0)
                Text(risk.reason ?? "风险熔断")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(.orange)
                    .lineLimit(1)
            }

            HStack(spacing: 12) {
                riskMetric("单日损益", value: String(format: "%+.2f%%", decimalDouble(risk.dailyPnLPercent)), color: decimalDouble(risk.dailyPnLPercent) < 0 ? .red : .green)
                riskMetric("累计回撤", value: String(format: "%.2f%%", decimalDouble(risk.drawdownPercent)), color: .orange)
                riskMetric("账户权益", value: formatUSD(risk.equity), color: .primary)
            }

            Text("策略启动已暂停。复位仅在 UTC 新自然日后生效。")
                .font(.caption2)
                .foregroundStyle(.secondary)

            if let resetFeedback {
                Text(resetFeedback)
                    .font(.caption2)
                    .foregroundStyle(resetFeedbackIsError ? .red : .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Button {
                resetFeedback = nil
                resetFeedbackIsError = false
                Task {
                    do {
                        let result = try await model.resetRisk()
                        if result.killSwitch {
                            resetFeedback = "风控仍锁存：只能在 UTC 新自然日后复位。"
                        } else {
                            resetFeedback = "账户风控已复位。"
                        }
                    } catch {
                        resetFeedbackIsError = true
                        resetFeedback = "复位失败：\(error.localizedDescription)"
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    if model.isResettingRisk {
                        ProgressView().controlSize(.small)
                        Text("复位中…")
                    } else {
                        Image(systemName: "arrow.clockwise")
                        Text("复位风控")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(.orange)
            .disabled(model.isResettingRisk)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.10), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.orange.opacity(0.35)))
    }

    private func riskMetric(_ title: String, value: String, color: Color) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.caption.monospacedDigit().weight(.semibold)).foregroundStyle(color)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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

    private var stopLossText: String {
        guard let signal = status?.lastSignal,
              let stop = signal.stopPrice,
              signal.price != 0 else {
            return "策略止损：\(strategy.stopDescription)"
        }
        let distance = abs(NSDecimalNumber(decimal: stop).doubleValue - NSDecimalNumber(decimal: signal.price).doubleValue)
        let entry = abs(NSDecimalNumber(decimal: signal.price).doubleValue)
        return String(format: "策略止损 %.2f%%（动态）", distance / entry * 100)
    }

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
            Text(stopLossText).font(.caption2).foregroundStyle(.secondary)
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
            Button("取消", role: .cancel) {}
        } message: {
            Text("检测到 (openPositions.count) 个未平仓位。必须先平仓并确认远端持仓归零后才能删除策略。")
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
    @State private var searchText = ""
    @State private var debouncedSearchText = ""
    @State private var category = "主流"
    @FocusState private var searchFocused: Bool

    private var searchKey: String {
        normalizeSearchText(debouncedSearchText)
    }

    private var isSearching: Bool { !searchKey.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
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
            .padding(.horizontal, 14).padding(.top, 14).padding(.bottom, 12)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 3) {
                    if isSearching {
                        searchResults
                    } else {
                        favoritesSection
                        categoryPicker
                        ForEach(model.contracts(for: category).filter { !model.favoriteIDs.contains($0.id) }) { contract in
                            contractRow(contract)
                        }
                    }
                }
                .padding(.horizontal, 8)
            }
            Spacer(minLength: 12)
        }.background(Color.sidebarBackground).navigationSplitViewColumnWidth(min: 245, ideal: 270, max: 310)
            .task(id: searchText) {
                let value = searchText
                guard !normalizeSearchText(value).isEmpty else {
                    debouncedSearchText = ""
                    return
                }
                do { try await Task.sleep(for: .milliseconds(150)) }
                catch { return }
                guard !Task.isCancelled else { return }
                debouncedSearchText = value
            }
    }

    @ViewBuilder
    private var favoritesSection: some View {
        HStack {
            Text("自选").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            Text("\(model.favoriteContracts.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 5)
        ForEach(model.favoriteContracts) { contract in
            contractRow(contract)
        }
    }

    private var categoryPicker: some View {
        Picker("合约分组", selection: $category) {
            Text("主流").tag("主流")
            Text("热门").tag("热门")
            Text("涨幅").tag("涨幅")
            Text("跌幅").tag("跌幅")
        }
        .labelsHidden()
        .pickerStyle(.segmented)
        .controlSize(.small)
        .padding(.top, 12)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var searchResults: some View {
        let results = model.contracts.filter { matches($0, query: searchKey) }
        HStack {
            Text("搜索结果").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            Spacer()
            Text("\(results.count)").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.bottom, 5)
        if results.isEmpty {
            Text("未找到匹配合约")
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 44)
        } else {
            ForEach(results) { contract in
                contractRow(contract)
            }
        }
    }

    private func contractRow(_ contract: PerpetualContract) -> some View {
        ContractRow(contract: contract, selected: model.selectedContract == contract.id, isFavorite: model.favoriteIDs.contains(contract.id)) {
            model.select(contract)
        } toggleFavorite: {
            model.toggleFavorite(contract)
        }
    }

    private func matches(_ contract: PerpetualContract, query: String) -> Bool {
        [contract.id, contract.name, contract.shortName, contract.pairLabel, contract.category]
            .contains { normalizeSearchText($0).contains(query) }
    }

    private func normalizeSearchText(_ value: String) -> String {
        value.lowercased().filter { $0.isLetter || $0.isNumber }
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

    private var primaryAssets: [AccountAsset] {
        model.accountOverview.assets
            .sorted { left, right in
                NSDecimalNumber(decimal: valuation(for: left) ?? 0).compare(NSDecimalNumber(decimal: valuation(for: right) ?? 0)) == .orderedDescending
            }
            .prefix(4)
            .map { $0 }
    }

    /// OKX does not always include `eqUsd` for demo balances. Keep the
    /// overview useful by valuing those balances against the latest contract
    /// ticker already loaded by the market sidebar. Stablecoins are valued at
    /// one USD and remain available even while the contract list is warming.
    private func valuation(for asset: AccountAsset) -> Decimal? {
        if let usdValue = asset.usdValue, usdValue >= 0 { return usdValue }
        let currency = asset.currency.uppercased()
        let unitPrice: Double
        if ["USD", "USDT", "USDC", "DAI"].contains(currency) {
            unitPrice = 1
        } else if let contract = model.contracts.first(where: { $0.shortName.uppercased() == currency }) {
            unitPrice = contract.price
        } else {
            return nil
        }
        guard unitPrice.isFinite, unitPrice > 0 else { return nil }
        let quantity = NSDecimalNumber(decimal: asset.equity).doubleValue
        guard quantity.isFinite, quantity >= 0 else { return nil }
        return Decimal(quantity * unitPrice)
    }

    var body: some View {
        VStack(spacing: 0) {
            Divider().overlay(Color.white.opacity(0.08))
            HStack(spacing: 0) {
                accountMetric("估计总资产", value: model.accountOverview.totalAssetValueUSD.map(formatUSD) ?? "--")
                    .frame(width: 126, alignment: .leading)
                metricDivider
                accountMetric("今日收益", value: model.accountOverview.todayPnLUSD.map(signedUSD) ?? "--", color: pnlColor)
                    .frame(width: 112, alignment: .leading)
                metricDivider
                HStack(spacing: 12) {
                    if primaryAssets.isEmpty {
                        Text("暂无资产明细")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else {
                        ForEach(primaryAssets, content: assetMetric)
                    }
                }
                .layoutPriority(1)
                Spacer(minLength: 10)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            Divider().overlay(Color.white.opacity(0.08))
        }
    }

    private var metricDivider: some View {
        Divider()
            .frame(height: 30)
            .overlay(Color.white.opacity(0.1))
            .padding(.horizontal, 14)
    }

    private var pnlColor: Color {
        (model.accountOverview.todayPnLUSD ?? 0) >= 0 ? .green : .red
    }

    private func accountMetric(_ title: String, value: String, color: Color = .primary) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.weight(.semibold).monospacedDigit()).foregroundStyle(color)
        }
        .lineLimit(1)
    }

    private func assetMetric(_ asset: AccountAsset) -> some View {
        let usdValue = valuation(for: asset)
        return VStack(alignment: .leading, spacing: 4) {
            Text(asset.currency)
                .font(.caption.weight(.bold).monospaced())
            Text(usdValue.map(formatUSD) ?? "估值待同步")
                .font(.subheadline.weight(.semibold).monospacedDigit())
                .foregroundStyle(usdValue == nil ? Color.secondary : Color.mint)
        }
        .frame(minWidth: 82, alignment: .leading)
        .lineLimit(1)
    }

    private func signedUSD(_ value: Decimal) -> String {
        let number = NSDecimalNumber(decimal: value).doubleValue
        return String(format: "%@$%.2f", number >= 0 ? "+" : "-", abs(number))
    }
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
                    Text(String(format: "风险预算 %.1f%%", strategy.allocation)).font(.caption2).foregroundStyle(.secondary)
                    Text("策略止损：\(strategy.stopDescription)").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
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
            Button("取消", role: .cancel) {}
        } message: {
            Text("检测到 (openPositions.count) 个未平仓位。必须先平仓并确认远端持仓归零后才能删除策略。")
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
    @State private var capitalPoolPercent = 100.0
    @State private var leverage = StrategyType.sweepReversalShort.defaultLeverage
    @State private var selectedRule: StrategyType = .sweepReversalShort
    @State private var step: NewStrategyStep = .choose
    @State private var isCreating = false
    @State private var creationErrorMessage: String?

    private enum NewStrategyStep {
        case choose
        case configure
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                if step == .configure {
                    Button {
                        step = .choose
                    } label: {
                        Image(systemName: "chevron.left")
                            .font(.headline)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("返回选择策略")
                    .help("返回选择策略")
                }
                Image(systemName: step == .choose ? "square.grid.3x3" : "slider.horizontal.3")
                    .font(.headline.weight(.semibold))
                    .foregroundStyle(.mint)
                    .frame(width: 30, height: 30)
                    .background(Color.mint.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 2) {
                    Text(step == .choose ? "选择策略" : "配置策略")
                        .font(.title2.weight(.semibold))
                    if step == .configure {
                        Text("调整下单参数")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    } else {
                        Text("选择一个策略开始配置")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
            }

            if step == .choose {
                strategySelection
            } else {
                configurationForm
            }

            Divider()
                .overlay(Color.white.opacity(0.08))
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Label("仅提交至当前 OKX 模拟账户", systemImage: "shield.checkered")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    if step == .configure, model.riskSnapshot.killSwitch {
                        Label("账户风控已熔断，保存后仍不能启动；请在新日复位后启动", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    }
                }
                Spacer()
                if step == .configure {
                    Button {
                        create()
                    } label: {
                        if isCreating {
                            ProgressView()
                                .controlSize(.small)
                            Text("保存中…")
                        } else {
                            Text("创建并保存")
                        }
                    }
                        .buttonStyle(.borderedProminent)
                        .tint(.mint)
                        .keyboardShortcut(.defaultAction)
                        .frame(minWidth: 104)
                        .disabled(!canCreate || isCreating)
                }
            }
        }
        .padding(22)
        .frame(width: 760, height: step == .choose ? 500 : 650)
        .preferredColorScheme(.dark)
        .onAppear {
            // Every tap on 添加 starts at the catalog, even if SwiftUI keeps
            // the sheet's state storage alive between presentations.
            step = .choose
            selectedRule = .sweepReversalShort
            leverage = selectedRule.defaultLeverage
            capitalPoolPercent = min(100, max(1, maxCapitalPoolPercent))
            isCreating = false
            creationErrorMessage = nil
        }
        .onChange(of: capitalPoolPercent) { _, value in
            let clamped = value.isFinite ? min(max(value, 1), max(1, maxCapitalPoolPercent)) : min(100, max(1, maxCapitalPoolPercent))
            if clamped != value {
                capitalPoolPercent = clamped
            }
        }
        .onChange(of: model.strategyCapitals) { _, _ in clampCapitalPoolPercent() }
        .onChange(of: model.accountOverview) { _, _ in clampCapitalPoolPercent() }
        .alert("创建策略失败", isPresented: Binding(
            get: { creationErrorMessage != nil },
            set: { isPresented in
                if !isPresented {
                    creationErrorMessage = nil
                    model.errorMessage = nil
                }
            }
        )) {
            Button("确定", role: .cancel) {
                creationErrorMessage = nil
                model.errorMessage = nil
            }
        } message: {
            Text(creationErrorMessage ?? "未知错误")
        }
    }

    private var strategySelection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("选择一个策略开始配置")
                .font(.headline)
            Text("卡片会告诉你它做什么、多久检查一次，以及如何下单。资金池只使用 USDT。")
                .font(.caption)
                .foregroundStyle(.secondary)

            // Keep this as three columns: the strategy catalog currently has
            // exactly three runnable strategies, so there is no placeholder
            // card to imply an unavailable strategy.
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: 3), spacing: 12) {
                ForEach(StrategyType.availableCases, id: \.self) { strategyType in
                    Button {
                        selectedRule = strategyType
                        leverage = strategyType.defaultLeverage
                        step = .configure
                    } label: {
                        strategySelectionCard(strategyType)
                    }
                    .buttonStyle(.plain)
                    .disabled(model.hasStrategyType(strategyType))
                    .opacity(model.hasStrategyType(strategyType) ? 0.62 : 1)
                }
            }
            Spacer(minLength: 0)
        }
    }

    private func strategySelectionCard(_ strategyType: StrategyType) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: strategyIcon(strategyType))
                    .font(.title3)
                    .foregroundStyle(.mint)
                VStack(alignment: .leading, spacing: 3) {
                    Text(strategyType.displayName)
                        .font(.subheadline.weight(.semibold))
                        .multilineTextAlignment(.leading)
                    if model.hasStrategyType(strategyType) {
                        Text("已有实例 · 无法重复创建")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(.orange)
                    }
                }
            }
            Text(strategySummary(strategyType))
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.leading)
                .lineLimit(3)
                .frame(maxWidth: .infinity, alignment: .leading)
            Divider().overlay(Color.white.opacity(0.08))
            HStack {
                Label(orderTypeDescription(strategyType), systemImage: orderTypeIcon(strategyType))
                Spacer()
                Text("默认 \(formattedLeverage(strategyType.defaultLeverage)) 倍")
            }
            .font(.caption2.weight(.medium))
            .foregroundStyle(.secondary)
            Text("资金池：USDT · \(signalIntervalDescription(for: strategyType))")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .padding(14)
        .frame(maxWidth: .infinity, minHeight: 188, alignment: .topLeading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.white.opacity(0.09)))
        .contentShape(RoundedRectangle(cornerRadius: 10))
    }

    private var configurationForm: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 14) {
                configurationSection("策略配置", subtitle: "策略运行范围与信号规则") {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: strategyIcon(selectedRule))
                            .font(.title3.weight(.semibold))
                            .foregroundStyle(.mint)
                            .frame(width: 34, height: 34)
                            .background(Color.mint.opacity(0.14), in: RoundedRectangle(cornerRadius: 9))
                        VStack(alignment: .leading, spacing: 5) {
                            Text(selectedRule.displayName)
                                .font(.headline.weight(.semibold))
                            Text(strategySummary(selectedRule))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer(minLength: 0)
                        strategyTag(orderTypeDescription(selectedRule), systemImage: orderTypeIcon(selectedRule))
                    }
                    Divider().overlay(Color.white.opacity(0.08))
                    VStack(alignment: .leading, spacing: 8) {
                        infoRow("扫描范围", value: scopeDescription)
                        infoRow("信号周期", value: signalIntervalDescription)
                        infoRow("保护规则", value: selectedRule.stopLossDescription)
                    }
                }

                configurationSection("下单") {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("资金池比例")
                                    .font(.body.weight(.medium))
                                Text("仅使用 USDT")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text("\(String(format: "%.0f", capitalPoolPercent))%")
                                .font(.title3.monospacedDigit().weight(.semibold))
                                .foregroundStyle(.mint)
                        }
                        HStack(spacing: 12) {
                            Slider(value: $capitalPoolPercent, in: capitalPoolRange, step: 1)
                                .tint(.mint)
                                .accessibilityLabel("策略资金池比例")
                                .accessibilityValue("\(String(format: "%.0f", capitalPoolPercent))%")
                            TextField("", value: $capitalPoolPercent, format: .number.precision(.fractionLength(0)))
                                .textFieldStyle(.roundedBorder)
                                .multilineTextAlignment(.trailing)
                                .frame(width: 58)
                                .accessibilityLabel("策略资金池比例")
                                .accessibilityValue("\(String(format: "%.0f", capitalPoolPercent))%")
                            Text("%")
                                .foregroundStyle(.secondary)
                        }
                        Divider().overlay(Color.white.opacity(0.08))
                        VStack(alignment: .leading, spacing: 8) {
                            infoRow("USDT 总资产", value: formatted(usdtTotalAssets), valueColor: .primary)
                            infoRow("其他策略占用", value: formatted(otherStrategyCapital))
                            infoRow("当前策略占用", value: formatted(currentStrategyCapital), valueColor: .mint)
                        }
                        Divider().overlay(Color.white.opacity(0.08))
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("杠杆")
                                    .font(.body.weight(.medium))
                                Text("默认 \(formattedLeverage(defaultLeverage)) 倍")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            Stepper(value: $leverage, in: selectedRule.leverageRange, step: 0.5) {
                                Text("\(formattedLeverage(leverage)) 倍")
                                    .font(.body.monospacedDigit().weight(.semibold))
                                    .frame(minWidth: 58, alignment: .trailing)
                            }
                            .controlSize(.small)
                            .accessibilityLabel("杠杆倍数")
                            .accessibilityValue("\(formattedLeverage(leverage)) 倍")
                            .accessibilityHint("使用加号或减号调整杠杆")
                        }
                    }
                }
            }
            .padding(.bottom, 4)
        }
        .scrollIndicators(.automatic)
    }

    @ViewBuilder
    private func configurationSection<Content: View>(_ title: String, subtitle: String? = nil, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.headline.weight(.semibold))
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.panelBackground, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.white.opacity(0.08)))
    }

    @ViewBuilder
    private func infoRow(_ title: String, value: String, valueColor: Color = .secondary) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(title)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.callout)
                .foregroundStyle(valueColor)
                .multilineTextAlignment(.trailing)
                .fixedSize(horizontal: false, vertical: true)
                .layoutPriority(1)
        }
    }

    private func strategyTag(_ title: String, systemImage: String) -> some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.medium))
            .foregroundStyle(.mint)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(Color.mint.opacity(0.12), in: Capsule())
    }

    private var canCreate: Bool {
        !model.hasStrategyType(selectedRule) && usdtTotalAssets != nil && effectiveCapitalPoolPercent > 0
    }

    private func strategyIcon(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort: return "arrow.down.right.and.arrow.up.left"
        case .emaAltcoinLong: return "chart.line.uptrend.xyaxis"
        case .hlsr: return "waveform.path.ecg"
        case .doublePumpExhaustionShort: return "chart.bar.xaxis"
        case .external: return "questionmark"
        }
    }

    private func strategySummary(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort:
            return "找出冲高后第二次扫顶的币，确认转弱后做空。"
        case .emaAltcoinLong:
            return "用快慢均线找上涨趋势，回踩确认后做多，达到目标止盈。"
        case .hlsr:
            return "观察高位流动性扫顶，反转确认后分批做空并逐步止盈。"
        case .doublePumpExhaustionShort:
            return "观察滚动 24 小时翻倍后的 15 分钟冲高衰竭，确认收盘后做空。"
        case .external:
            return "策略包暂不可用。"
        }
    }

    private func orderTypeDescription(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort, .emaAltcoinLong, .hlsr, .doublePumpExhaustionShort:
            return "市价下单"
        case .external:
            return "不可用"
        }
    }

    private func orderTypeIcon(_ strategyType: StrategyType) -> String {
        switch strategyType {
        case .sweepReversalShort, .emaAltcoinLong, .hlsr, .doublePumpExhaustionShort: return "bolt.fill"
        case .external: return "questionmark"
        }
    }

    private func signalIntervalDescription(for strategyType: StrategyType) -> String {
        switch strategyType {
        case .hlsr: return "15 分钟检查"
        case .doublePumpExhaustionShort: return "15 分钟检查"
        case .sweepReversalShort, .emaAltcoinLong: return "1 小时检查"
        case .external: return "不可用"
        }
    }

    /// Risk is supplied by the selected strategy's laboratory defaults. It is
    /// shown in the risk summary but is intentionally not editable here.
    private var effectiveRisk: Double { min(selectedRule.defaultRiskPercent, maxRiskForRule) }

    private var defaultLeverage: Double { selectedRule.defaultLeverage }

    private func formattedLeverage(_ value: Double) -> String {
        value.rounded() == value ? String(format: "%.0f", value) : String(format: "%.1f", value)
    }

    /// 实验室规则给出的风险预算硬上限。
    private var maxRiskForRule: Double { selectedRule.maxRiskPercent }

    private var scopeDescription: String {
        switch selectedRule {
        case .hlsr:
            return "动态扫描 24h 涨幅 >40%、报价成交额 >3,000 万 USDT 的山寨币"
        case .doublePumpExhaustionShort:
            return "动态扫描 24h 涨幅 >100%、报价成交额 ≥1,000 万 USDT 的山寨币"
        case .emaAltcoinLong:
            return "动态扫描合规山寨币成交额前 50"
        case .sweepReversalShort:
            return "动态扫描合规热门榜前 20 个山寨币"
        case .external:
            return "策略包运行时不可用（暂停）"
        }
    }

    private var signalIntervalDescription: String {
        switch selectedRule {
        case .hlsr:
            return "15 分钟入场；4 小时市场状态"
        case .doublePumpExhaustionShort:
            return "15 分钟收盘确认；翻倍后衰竭做空"
        case .sweepReversalShort, .emaAltcoinLong:
            return "1 小时收盘确认"
        case .external:
            return "运行时处理器不可用"
        }
    }

    private var usdtTotalAssets: Decimal? { model.accountOverview.usdtEquity }

    private var capitalAllocation: StrategyCapitalAllocation {
        StrategyCapitalAllocation(totalCapital: usdtTotalAssets ?? 0, strategyCapitals: model.strategyCapitals)
    }

    private var otherStrategyCapital: Decimal? {
        usdtTotalAssets.map { _ in capitalAllocation.occupiedCapital }
    }

    private var maxCapitalPoolPercent: Double {
        guard usdtTotalAssets != nil else { return 100 }
        return decimalDouble(capitalAllocation.availableAllocationPercent)
    }

    private var effectiveCapitalPoolPercent: Double {
        let requested = Decimal(capitalPoolPercent)
        return decimalDouble(capitalAllocation.effectiveAllocationPercent(for: requested))
    }

    private var currentStrategyCapital: Decimal? {
        guard usdtTotalAssets != nil else { return nil }
        return capitalAllocation.capital(for: Decimal(effectiveCapitalPoolPercent))
    }

    private var capitalPoolRange: ClosedRange<Double> {
        1...max(1, maxCapitalPoolPercent)
    }

    private func clampCapitalPoolPercent() {
        let upperBound = max(1, maxCapitalPoolPercent)
        let clamped = min(max(capitalPoolPercent, 1), upperBound)
        if clamped != capitalPoolPercent { capitalPoolPercent = clamped }
    }

    private func formatted(_ value: Decimal?) -> String {
        value.map(formatUSD) ?? "--"
    }

    private func create() {
        guard !isCreating else { return }
        // 参数默认值来自领域层（与实验室 config 对齐），不再在界面里重复一份。
        var parameters = selectedRule.defaultParameters
        parameters["leverage"] = leverage
        let config = StrategyConfig(name: selectedRule.displayName, scope: selectedRule.defaultScope, interval: selectedRule.entryInterval, type: selectedRule, parameters: parameters, enabled: false, riskPercent: effectiveRisk, capitalPoolPercent: effectiveCapitalPoolPercent, cooldownBars: selectedRule.defaultCooldownBars)
        isCreating = true
        model.errorMessage = nil
        Task { @MainActor in
            defer { isCreating = false }
            do {
                _ = try await model.createStrategy(config)
                dismiss()
            } catch {
                let message = error.localizedDescription
                model.errorMessage = message
                creationErrorMessage = message
            }
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

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // The development launcher assembles the app bundle after SwiftPM
        // builds the executable. Set the icon explicitly at runtime as well
        // as in Info.plist so the Dock does not retain a generic executable
        // icon while LaunchServices refreshes the bundle metadata.
        if let iconURL = Bundle.main.url(forResource: "NovaTrade", withExtension: "icns"),
           let icon = NSImage(contentsOf: iconURL) {
            NSApplication.shared.applicationIconImage = icon
        }
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

    var body: some Scene {
        WindowGroup("NovaTrade") {
            DashboardView()
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1480, height: 900)
    }
}
