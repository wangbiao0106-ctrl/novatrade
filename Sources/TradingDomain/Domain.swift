import Foundation

public enum TradingMode: String, Codable, Sendable {
    case readOnly
    case paper
    case live
}

public struct MarketTicker: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let instrumentID: String
    public let last: Decimal
    public let bid: Decimal?
    public let ask: Decimal?
    public let timestamp: Date

    public init(instrumentID: String, last: Decimal, bid: Decimal? = nil, ask: Decimal? = nil, timestamp: Date = .now) {
        self.id = instrumentID
        self.instrumentID = instrumentID
        self.last = last
        self.bid = bid
        self.ask = ask
        self.timestamp = timestamp
    }
}

public struct AccountSnapshot: Codable, Equatable, Sendable {
    public let equity: Decimal?
    public let availableBalance: Decimal?
    public let updatedAt: Date

    public init(equity: Decimal? = nil, availableBalance: Decimal? = nil, updatedAt: Date = .now) {
        self.equity = equity
        self.availableBalance = availableBalance
        self.updatedAt = updatedAt
    }
}

public struct AccountAsset: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let currency: String
    public let equity: Decimal
    public let available: Decimal
    public let usdValue: Decimal?

    public init(currency: String, equity: Decimal, available: Decimal = 0, usdValue: Decimal? = nil) {
        self.id = currency
        self.currency = currency
        self.equity = equity
        self.available = available
        self.usdValue = usdValue
    }
}

public struct AccountOverview: Codable, Equatable, Sendable {
    public let mode: TradingMode
    public let profile: String?
    public let site: String?
    public let label: String?
    public let authenticated: Bool
    public let equityUSD: Decimal?
    public let availableEquityUSD: Decimal?
    public let totalAssetValueUSD: Decimal?
    public let todayPnLUSD: Decimal?
    public let assets: [AccountAsset]
    public let positions: [PositionSnapshot]
    public let updatedAt: Date

    public init(mode: TradingMode = .readOnly, profile: String? = nil, site: String? = nil, label: String? = nil, authenticated: Bool = false, equityUSD: Decimal? = nil, availableEquityUSD: Decimal? = nil, totalAssetValueUSD: Decimal? = nil, todayPnLUSD: Decimal? = nil, assets: [AccountAsset] = [], positions: [PositionSnapshot] = [], updatedAt: Date = .now) {
        self.mode = mode; self.profile = profile; self.site = site; self.label = label; self.authenticated = authenticated
        self.equityUSD = equityUSD; self.availableEquityUSD = availableEquityUSD; self.totalAssetValueUSD = totalAssetValueUSD ?? equityUSD; self.todayPnLUSD = todayPnLUSD; self.assets = assets; self.positions = positions; self.updatedAt = updatedAt
    }
}

public struct SystemSnapshot: Codable, Equatable, Sendable {
    public let mode: TradingMode
    public let connection: String
    public let ticker: MarketTicker?
    public let account: AccountSnapshot?

    public init(mode: TradingMode = .readOnly, connection: String = "未连接", ticker: MarketTicker? = nil, account: AccountSnapshot? = nil) {
        self.mode = mode
        self.connection = connection
        self.ticker = ticker
        self.account = account
    }
}

public enum KlineInterval: String, Codable, CaseIterable, Sendable {
    case oneMinute = "1m"
    case fiveMinutes = "5m"
    case fifteenMinutes = "15m"
    case oneHour = "1H"
    case fourHours = "4H"
    case oneDay = "1D"

    public var okxBar: String {
        switch self {
        case .oneMinute: return "1m"
        case .fiveMinutes: return "5m"
        case .fifteenMinutes: return "15m"
        case .oneHour: return "1H"
        case .fourHours: return "4H"
        case .oneDay: return "1D"
        }
    }
}

public struct Instrument: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let baseCurrency: String
    public let quoteCurrency: String
    public let tickSize: Decimal?
    public let lotSize: Decimal?

    public init(id: String, name: String, baseCurrency: String, quoteCurrency: String, tickSize: Decimal? = nil, lotSize: Decimal? = nil) {
        self.id = id; self.name = name; self.baseCurrency = baseCurrency; self.quoteCurrency = quoteCurrency
        self.tickSize = tickSize; self.lotSize = lotSize
    }
}

public struct ContractMarket: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let baseCurrency: String
    public let quoteCurrency: String
    public let last: Decimal
    public let changePercent: Decimal
    public let volume24h: Decimal
    public let category: String
    public let updatedAt: Date

    public init(id: String, name: String, baseCurrency: String, quoteCurrency: String, last: Decimal, changePercent: Decimal = 0, volume24h: Decimal = 0, category: String = "全部", updatedAt: Date = .now) {
        self.id = id; self.name = name; self.baseCurrency = baseCurrency; self.quoteCurrency = quoteCurrency
        self.last = last; self.changePercent = changePercent; self.volume24h = volume24h; self.category = category; self.updatedAt = updatedAt
    }
}

public struct Candle: Codable, Equatable, Sendable, Identifiable {
    public let id: Int64
    public let timestamp: Date
    public let open: Decimal
    public let high: Decimal
    public let low: Decimal
    public let close: Decimal
    public let volume: Decimal
    public let confirmed: Bool

    public init(timestamp: Date, open: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal = 0, confirmed: Bool = true) {
        self.id = Int64(timestamp.timeIntervalSince1970)
        self.timestamp = timestamp; self.open = open; self.high = high; self.low = low; self.close = close
        self.volume = volume; self.confirmed = confirmed
    }
}

extension Array where Element == Candle {
    /// Inserts or replaces a candle while keeping the array ordered by
    /// timestamp. The steady-state WSS case (a newer bar) appends in O(1)
    /// instead of re-sorting the whole window on every frame.
    public mutating func upsert(_ candle: Candle) {
        if let index = firstIndex(where: { $0.timestamp == candle.timestamp }) {
            self[index] = candle
            return
        }
        if let lastTimestamp = last?.timestamp, candle.timestamp > lastTimestamp {
            append(candle)
            return
        }
        let position = firstIndex(where: { $0.timestamp > candle.timestamp }) ?? count
        insert(candle, at: position)
    }
}

public struct MarketSnapshot: Codable, Equatable, Sendable {
    public let instrumentID: String
    public let interval: KlineInterval
    public let candles: [Candle]
    public let ticker: MarketTicker?
    public let updatedAt: Date

    public init(instrumentID: String, interval: KlineInterval, candles: [Candle] = [], ticker: MarketTicker? = nil, updatedAt: Date = .now) {
        self.instrumentID = instrumentID; self.interval = interval; self.candles = candles; self.ticker = ticker; self.updatedAt = updatedAt
    }
}

public struct OrderBookSnapshot: Codable, Equatable, Sendable {
    public let instrumentID: String
    public let bids: [[Decimal]]
    public let asks: [[Decimal]]
    public let timestamp: Date

    public init(instrumentID: String, bids: [[Decimal]] = [], asks: [[Decimal]] = [], timestamp: Date = .now) {
        self.instrumentID = instrumentID; self.bids = bids; self.asks = asks; self.timestamp = timestamp
    }
}

public struct TradeTick: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let instrumentID: String
    public let price: Decimal
    public let size: Decimal
    public let side: String?
    public let timestamp: Date

    public init(id: String = UUID().uuidString, instrumentID: String, price: Decimal, size: Decimal, side: String? = nil, timestamp: Date = .now) {
        self.id = id; self.instrumentID = instrumentID; self.price = price; self.size = size; self.side = side; self.timestamp = timestamp
    }
}

public struct PositionSnapshot: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let instrumentID: String
    public let side: String
    public let quantity: Decimal
    public let entryPrice: Decimal
    public let markPrice: Decimal?
    public let unrealizedPnL: Decimal?

    public init(id: String = UUID().uuidString, instrumentID: String, side: String, quantity: Decimal, entryPrice: Decimal, markPrice: Decimal? = nil, unrealizedPnL: Decimal? = nil) {
        self.id = id; self.instrumentID = instrumentID; self.side = side; self.quantity = quantity; self.entryPrice = entryPrice
        self.markPrice = markPrice; self.unrealizedPnL = unrealizedPnL
    }
}

public struct OrderSnapshot: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let instrumentID: String
    public let side: String
    public let status: String
    public let quantity: Decimal
    public let price: Decimal?
    public let createdAt: Date

    public init(id: String = UUID().uuidString, instrumentID: String, side: String, status: String, quantity: Decimal, price: Decimal? = nil, createdAt: Date = .now) {
        self.id = id; self.instrumentID = instrumentID; self.side = side; self.status = status; self.quantity = quantity; self.price = price; self.createdAt = createdAt
    }
}

/// A live order request is intentionally limited to the order fields needed by
/// the first live-trading surface. Secrets and auth tokens never cross this
/// boundary.
public struct LiveOrderRequest: Codable, Equatable, Sendable {
    public let instrumentID: String
    public let side: String
    public let orderType: String
    public let quantity: Decimal
    public let positionSide: String?
    public let marginMode: String
    public let reduceOnly: Bool
    public let price: Decimal?

    public init(instrumentID: String, side: String, orderType: String = "market", quantity: Decimal, positionSide: String? = nil, marginMode: String = "cross", reduceOnly: Bool = false, price: Decimal? = nil) {
        self.instrumentID = instrumentID
        self.side = side
        self.orderType = orderType
        self.quantity = quantity
        self.positionSide = positionSide
        self.marginMode = marginMode
        self.reduceOnly = reduceOnly
        self.price = price
    }
}

public struct LiveOrderResult: Codable, Equatable, Sendable {
    public let orderID: String
    public let clientOrderID: String?
    public let instrumentID: String
    public let side: String
    public let orderType: String
    public let quantity: Decimal
    public let status: String
    public let message: String?
    public let submittedAt: Date

    public init(orderID: String, clientOrderID: String? = nil, instrumentID: String, side: String, orderType: String, quantity: Decimal, status: String = "submitted", message: String? = nil, submittedAt: Date = .now) {
        self.orderID = orderID; self.clientOrderID = clientOrderID; self.instrumentID = instrumentID; self.side = side; self.orderType = orderType; self.quantity = quantity; self.status = status; self.message = message; self.submittedAt = submittedAt
    }
}

public struct LiveTradingStatus: Codable, Equatable, Sendable {
    public let mode: TradingMode
    public let profile: String?
    public let enabled: Bool
    public let available: Bool
    public let message: String
    public let updatedAt: Date

    public init(mode: TradingMode = .readOnly, profile: String? = nil, enabled: Bool = false, available: Bool = false, message: String = "未连接 OKX", updatedAt: Date = .now) {
        self.mode = mode; self.profile = profile; self.enabled = enabled; self.available = available; self.message = message; self.updatedAt = updatedAt
    }
}

public enum StrategyType: String, Codable, CaseIterable, Sendable { case trendFollowing, rsiReversal, sweepReversalShort }
public enum StrategyState: String, Codable, Sendable { case draft, running, paused, error }

public enum StrategyScopeMode: String, Codable, CaseIterable, Sendable {
    case single
    case multiple
    case dynamicCategory
}

public enum StrategyUniverseCategory: String, Codable, CaseIterable, Sendable {
    case mainstream
    case hotAltcoins
    case highGain60
    case highGain100

    public var displayName: String {
        switch self {
        case .mainstream: return "主流币"
        case .hotAltcoins: return "热门山寨币"
        case .highGain60: return "高涨幅币（24h +60%）"
        case .highGain100: return "高涨幅币（24h +100%）"
        }
    }
}

public struct StrategyScope: Codable, Equatable, Sendable {
    public var mode: StrategyScopeMode
    public var instrumentIDs: [String]
    public var category: StrategyUniverseCategory?

    public init(mode: StrategyScopeMode, instrumentIDs: [String] = [], category: StrategyUniverseCategory? = nil) {
        self.mode = mode
        self.instrumentIDs = Array(NSOrderedSet(array: instrumentIDs)) as? [String] ?? instrumentIDs
        self.category = category
    }

    public static func single(_ instrumentID: String) -> StrategyScope { StrategyScope(mode: .single, instrumentIDs: [instrumentID]) }
    public static func multiple(_ instrumentIDs: [String]) -> StrategyScope { StrategyScope(mode: .multiple, instrumentIDs: instrumentIDs) }
    public static func dynamic(_ category: StrategyUniverseCategory) -> StrategyScope { StrategyScope(mode: .dynamicCategory, category: category) }

    public var legacyInstrumentID: String { instrumentIDs.first ?? "" }

    public var displayName: String {
        switch mode {
        case .single: return legacyInstrumentID.isEmpty ? "未选择合约" : legacyInstrumentID
        case .multiple: return "\(instrumentIDs.count) 个币种"
        case .dynamicCategory: return "动态 · \(category?.displayName ?? "未分类")"
        }
    }

    public func resolvedInstrumentIDs(from contracts: [ContractMarket]) -> [String] {
        switch mode {
        case .single:
            return instrumentIDs.prefix(1).map { $0 }
        case .multiple:
            let available = Set(contracts.map(\.id))
            return instrumentIDs.filter { available.isEmpty || available.contains($0) }
        case .dynamicCategory:
            guard let category else { return [] }
            return Self.contracts(for: category, in: contracts).prefix(50).map(\.id)
        }
    }

    public func matches(_ instrumentID: String, contracts: [ContractMarket]) -> Bool {
        resolvedInstrumentIDs(from: contracts).contains(instrumentID)
    }

    private static let mainstreamSymbols: Set<String> = [
        "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
        "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI"
    ]

    private static func contracts(for category: StrategyUniverseCategory, in contracts: [ContractMarket]) -> [ContractMarket] {
        switch category {
        case .mainstream:
            return contracts.filter { mainstreamSymbols.contains($0.baseCurrency.uppercased()) }
        case .hotAltcoins:
            let altcoins = contracts.filter { !mainstreamSymbols.contains($0.baseCurrency.uppercased()) }
            let tagged = altcoins.filter { $0.category == "热门" }
            let candidates = tagged.count >= 3 ? tagged : altcoins
            return Array(candidates.sorted { $0.volume24h > $1.volume24h }.prefix(30))
        case .highGain60:
            return contracts.filter { $0.changePercent >= 60 }.sorted { $0.changePercent > $1.changePercent }
        case .highGain100:
            return contracts.filter { $0.changePercent >= 100 }.sorted { $0.changePercent > $1.changePercent }
        }
    }
}

public struct StrategyConfig: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public var name: String
    public var instrumentID: String
    public var interval: KlineInterval
    public var type: StrategyType
    public var parameters: [String: Double]
    public var enabled: Bool
    public var stopLossPercent: Double
    public var takeProfitPercent: Double
    public var riskPercent: Double
    public var cooldownBars: Int
    public var trailingStopPercent: Double
    /// Optional for backwards compatibility. Missing scope fields in older
    /// persisted configs continue to behave as a single instrument.
    public var scope: StrategyScope?

    public var effectiveScope: StrategyScope { scope ?? .single(instrumentID) }

    public init(id: UUID = UUID(), name: String, instrumentID: String, interval: KlineInterval, type: StrategyType, parameters: [String: Double] = [:], enabled: Bool = false, stopLossPercent: Double = 1.5, takeProfitPercent: Double = 3, riskPercent: Double = 1, cooldownBars: Int = 3, trailingStopPercent: Double = 1, scope: StrategyScope? = nil) {
        self.id = id; self.name = name; self.instrumentID = instrumentID; self.interval = interval; self.type = type; self.parameters = parameters; self.enabled = enabled
        self.stopLossPercent = stopLossPercent; self.takeProfitPercent = takeProfitPercent; self.riskPercent = riskPercent; self.cooldownBars = cooldownBars; self.trailingStopPercent = trailingStopPercent; self.scope = scope
    }

    public init(id: UUID = UUID(), name: String, scope: StrategyScope, interval: KlineInterval, type: StrategyType, parameters: [String: Double] = [:], enabled: Bool = false, stopLossPercent: Double = 1.5, takeProfitPercent: Double = 3, riskPercent: Double = 1, cooldownBars: Int = 3, trailingStopPercent: Double = 1) {
        self.init(id: id, name: name, instrumentID: scope.legacyInstrumentID, interval: interval, type: type, parameters: parameters, enabled: enabled, stopLossPercent: stopLossPercent, takeProfitPercent: takeProfitPercent, riskPercent: riskPercent, cooldownBars: cooldownBars, trailingStopPercent: trailingStopPercent, scope: scope)
    }
}

public struct StrategySignal: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let strategyID: UUID
    public let type: String
    public let price: Decimal
    public let reason: String
    public let timestamp: Date
    /// 信号附带的止损/止盈价位（ATR 标定策略使用；固定百分比策略为 nil）
    public let stopPrice: Decimal?
    public let takePrice: Decimal?

    public init(id: UUID = UUID(), strategyID: UUID, type: String, price: Decimal, reason: String, timestamp: Date = .now, stopPrice: Decimal? = nil, takePrice: Decimal? = nil) {
        self.id = id; self.strategyID = strategyID; self.type = type; self.price = price; self.reason = reason; self.timestamp = timestamp
        self.stopPrice = stopPrice; self.takePrice = takePrice
    }
}

public struct IndicatorPoint: Codable, Equatable, Sendable {
    public let timestamp: Date
    public let value: Decimal

    public init(timestamp: Date, value: Decimal) { self.timestamp = timestamp; self.value = value }
}

public struct IndicatorSeries: Codable, Equatable, Sendable {
    public let name: String
    public let points: [IndicatorPoint]

    public init(name: String, points: [IndicatorPoint]) { self.name = name; self.points = points }
}

public struct StrategyStatus: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let state: StrategyState
    public let direction: String?
    public let cooldown: Int
    public let pnl: Decimal
    public let lastSignal: StrategySignal?
    public let indicators: [String: [Double]]

    public init(id: UUID, state: StrategyState, direction: String? = nil, cooldown: Int = 0, pnl: Decimal = 0, lastSignal: StrategySignal? = nil, indicators: [String: [Double]] = [:]) {
        self.id = id; self.state = state; self.direction = direction; self.cooldown = cooldown; self.pnl = pnl; self.lastSignal = lastSignal; self.indicators = indicators
    }
}

public struct RiskSnapshot: Codable, Equatable, Sendable {
    public let equity: Decimal
    public let equityPeak: Decimal
    public let dayStartEquity: Decimal
    public let dailyPnLPercent: Decimal
    public let drawdownPercent: Decimal
    public let killSwitch: Bool
    public let reason: String?

    public init(equity: Decimal = 0, equityPeak: Decimal = 0, dayStartEquity: Decimal = 0, dailyPnLPercent: Decimal = 0, drawdownPercent: Decimal = 0, killSwitch: Bool = false, reason: String? = nil) {
        self.equity = equity; self.equityPeak = equityPeak; self.dayStartEquity = dayStartEquity; self.dailyPnLPercent = dailyPnLPercent; self.drawdownPercent = drawdownPercent; self.killSwitch = killSwitch; self.reason = reason
    }
}

public struct PaperOrder: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let strategyID: UUID
    public let instrumentID: String
    public let side: String
    public let quantity: Decimal
    public let requestedAt: Date
    public let fillPrice: Decimal?
    public let status: String
    /// OKX order id when this record was submitted to the demo account.
    public let remoteOrderID: String?

    public init(id: UUID = UUID(), strategyID: UUID, instrumentID: String, side: String, quantity: Decimal, requestedAt: Date = .now, fillPrice: Decimal? = nil, status: String = "pending", remoteOrderID: String? = nil) {
        self.id = id; self.strategyID = strategyID; self.instrumentID = instrumentID; self.side = side; self.quantity = quantity
        self.requestedAt = requestedAt; self.fillPrice = fillPrice; self.status = status; self.remoteOrderID = remoteOrderID
    }
}

public struct PaperOrderRequest: Codable, Equatable, Sendable {
    public let instrumentID: String
    public let side: String
    public let quantity: Decimal
    public let reduceOnly: Bool

    public init(instrumentID: String, side: String, quantity: Decimal, reduceOnly: Bool = false) {
        self.instrumentID = instrumentID
        self.side = side
        self.quantity = quantity
        self.reduceOnly = reduceOnly
    }
}

public struct PaperFill: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let orderID: UUID
    public let price: Decimal
    public let quantity: Decimal
    public let fee: Decimal
    public let timestamp: Date

    public init(id: UUID = UUID(), orderID: UUID, price: Decimal, quantity: Decimal, fee: Decimal, timestamp: Date = .now) {
        self.id = id; self.orderID = orderID; self.price = price; self.quantity = quantity; self.fee = fee; self.timestamp = timestamp
    }
}

public struct RuntimeLog: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public let timestamp: Date
    public let level: String
    public let message: String

    public init(id: UUID = UUID(), timestamp: Date = .now, level: String = "info", message: String) {
        self.id = id; self.timestamp = timestamp; self.level = level; self.message = message
    }
}

public struct PaperPosition: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let instrumentID: String
    public let side: String
    public let quantity: Decimal
    public let entryPrice: Decimal
    public let markPrice: Decimal?
    public let unrealizedPnL: Decimal
    public let updatedAt: Date

    public init(id: String = UUID().uuidString, instrumentID: String, side: String, quantity: Decimal, entryPrice: Decimal, markPrice: Decimal? = nil, unrealizedPnL: Decimal = 0, updatedAt: Date = .now) {
        self.id = id
        self.instrumentID = instrumentID
        self.side = side
        self.quantity = quantity
        self.entryPrice = entryPrice
        self.markPrice = markPrice
        self.unrealizedPnL = unrealizedPnL
        self.updatedAt = updatedAt
    }
}

public struct RiskLimits: Codable, Equatable, Sendable {
    public var maxInstrumentNotional: Decimal
    public var maxTotalNotional: Decimal
    public var maxMarginPercent: Decimal
    public var minOrderIntervalSeconds: Int
    public var maxOrdersPerHour: Int
    public var maxDailyLossPercent: Decimal
    public var maxDrawdownPercent: Decimal

    public init(maxInstrumentNotional: Decimal = 25_000, maxTotalNotional: Decimal = 100_000, maxMarginPercent: Decimal = 25, minOrderIntervalSeconds: Int = 15, maxOrdersPerHour: Int = 60, maxDailyLossPercent: Decimal = 5, maxDrawdownPercent: Decimal = 10) {
        self.maxInstrumentNotional = maxInstrumentNotional
        self.maxTotalNotional = maxTotalNotional
        self.maxMarginPercent = maxMarginPercent
        self.minOrderIntervalSeconds = minOrderIntervalSeconds
        self.maxOrdersPerHour = maxOrdersPerHour
        self.maxDailyLossPercent = maxDailyLossPercent
        self.maxDrawdownPercent = maxDrawdownPercent
    }
}

public struct RiskDecision: Codable, Equatable, Sendable {
    public let allowed: Bool
    public let reason: String?
    public init(allowed: Bool, reason: String? = nil) { self.allowed = allowed; self.reason = reason }
}

public struct ServiceHealth: Codable, Equatable, Sendable {
    public let status: String
    public let version: String
    public let mode: TradingMode
    public let updatedAt: Date

    public init(status: String = "ok", version: String = "1", mode: TradingMode = .paper, updatedAt: Date = .now) {
        self.status = status; self.version = version; self.mode = mode; self.updatedAt = updatedAt
    }
}

public struct CapabilityStatus: Codable, Equatable, Sendable {
    public let name: String
    public let available: Bool
    public let message: String?

    public init(name: String, available: Bool, message: String? = nil) {
        self.name = name; self.available = available; self.message = message
    }
}

public struct UnavailableCapability: Codable, Equatable, Sendable {
    public let capability: CapabilityStatus
    public init(name: String, message: String) {
        self.capability = CapabilityStatus(name: name, available: false, message: message)
    }
}
