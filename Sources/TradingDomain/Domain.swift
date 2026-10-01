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

    /// The amount of USDT available as the strategy sizing base. A missing
    /// USDT row is a known zero balance once an authenticated account has
    /// loaded; before authentication/account loading, keep it unknown.
    public var usdtEquity: Decimal? {
        guard authenticated else { return nil }
        let usdtBalances = assets
            .filter { $0.currency.trimmingCharacters(in: .whitespacesAndNewlines).uppercased() == "USDT" }
            .map(\.equity)
        guard !usdtBalances.isEmpty else { return 0 }
        let total = usdtBalances.reduce(0, +)
        guard total.isFinite else { return nil }
        return max(0, total)
    }

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

/// The exchange contract specification needed to translate OKX swap contract
/// counts (`sz`) into a quote-currency notional.  OKX reports quantity in
/// contracts, while `ctVal`/`ctMult` describe the base amount represented by
/// one contract.  Keeping this value in the domain prevents the UI, strategy
/// sizing and risk engine from silently assuming `contracts × price`.
public struct SwapInstrumentSpec: Codable, Equatable, Sendable, Identifiable {
    public let instrumentID: String
    public var id: String { instrumentID }
    public let ctVal: Decimal
    public let ctMult: Decimal
    public let lotSize: Decimal
    public let minSize: Decimal
    public let tickSize: Decimal
    public let state: String
    public let ctType: String
    public let settleCurrency: String

    public init(instrumentID: String, ctVal: Decimal, ctMult: Decimal = 1, lotSize: Decimal, minSize: Decimal, tickSize: Decimal, state: String, ctType: String, settleCurrency: String) {
        self.instrumentID = instrumentID
        self.ctVal = ctVal
        self.ctMult = ctMult
        self.lotSize = lotSize
        self.minSize = minSize
        self.tickSize = tickSize
        self.state = state
        self.ctType = ctType
        self.settleCurrency = settleCurrency
    }

    /// True only for contracts this service is allowed to trade.  Inverse,
    /// expired, option and non-USDT instruments must never enter the sizing
    /// path by accident.
    public var isLiveUSDTLinearSwap: Bool {
        instrumentID.uppercased().hasSuffix("-USDT-SWAP") &&
            state.lowercased() == "live" &&
            ctType.lowercased() == "linear" &&
            settleCurrency.uppercased() == "USDT" &&
            ctVal.isFinite && ctVal > 0 &&
            ctMult.isFinite && ctMult > 0 &&
            contractValue.isFinite && contractValue > 0 &&
            lotSize.isFinite && lotSize > 0 &&
            minSize.isFinite && minSize > 0 &&
            tickSize.isFinite && tickSize > 0
    }

    public var contractValue: Decimal { ctVal * ctMult }

    public func notional(forContracts contracts: Decimal, price: Decimal) -> Decimal {
        contracts * contractValue * price
    }

    /// Converts a quote-currency target into whole exchange lots.  Rounding
    /// is always down so the actual order cannot exceed the risk budget.
    public func contracts(forTargetNotional target: Decimal, price: Decimal) -> Decimal? {
        guard target.isFinite, target > 0, price.isFinite, price > 0,
              contractValue.isFinite, contractValue > 0,
              lotSize.isFinite, lotSize > 0,
              minSize.isFinite, minSize > 0 else { return nil }
        let raw = target / (price * contractValue)
        guard raw.isFinite, raw > 0 else { return nil }
        var units = raw / lotSize
        var whole = Decimal.zero
        NSDecimalRound(&whole, &units, 0, .down)
        var contracts = whole * lotSize
        // Decimal division may round a value just below a lot boundary up to
        // that boundary. Verify the resulting cost before returning it.
        if notional(forContracts: contracts, price: price) > target {
            whole -= 1
            contracts = whole * lotSize
        }
        guard contracts.isFinite, contracts >= minSize, contracts > 0 else { return nil }
        return contracts
    }

    public func accepts(contractQuantity quantity: Decimal) -> Bool {
        guard quantity.isFinite, quantity > 0,
              lotSize.isFinite, lotSize > 0,
              minSize.isFinite, minSize > 0 else { return false }
        var units = quantity / lotSize
        var whole = Decimal.zero
        NSDecimalRound(&whole, &units, 0, .down)
        return whole * lotSize == quantity && quantity >= minSize
    }

    public func accepts(price: Decimal) -> Bool {
        guard price.isFinite, price > 0, tickSize.isFinite, tickSize > 0 else { return false }
        var units = price / tickSize
        var whole = Decimal.zero
        NSDecimalRound(&whole, &units, 0, .down)
        return whole * tickSize == price
    }

    public func alignedPrice(_ price: Decimal, roundingUp: Bool) -> Decimal? {
        guard price.isFinite, price > 0, tickSize.isFinite, tickSize > 0 else { return nil }
        var units = price / tickSize
        var whole = Decimal.zero
        NSDecimalRound(&whole, &units, 0, roundingUp ? .up : .down)
        let aligned = whole * tickSize
        return aligned.isFinite && aligned > 0 ? aligned : nil
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
    /// OKX `volCcyQuote` (row 7), in quote currency.  HLSR's 24h liquidity
    /// gate must use this field rather than contract/base volume.  It is
    /// Optional at the API boundary for backwards compatibility with candles
    /// persisted before quote volume was collected. HLSR fails closed when it
    /// encounters nil rather than substituting contract volume.
    public let quoteVolume: Decimal?
    public let confirmed: Bool

    public init(timestamp: Date, open: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal = 0, quoteVolume: Decimal? = nil, confirmed: Bool = true) {
        self.id = Int64(timestamp.timeIntervalSince1970)
        self.timestamp = timestamp; self.open = open; self.high = high; self.low = low; self.close = close
        self.volume = volume; self.quoteVolume = quoteVolume; self.confirmed = confirmed
    }

    private enum CodingKeys: String, CodingKey { case id, timestamp, open, high, low, close, volume, quoteVolume, confirmed }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let timestamp = try values.decode(Date.self, forKey: .timestamp)
        self.id = try values.decodeIfPresent(Int64.self, forKey: .id) ?? Int64(timestamp.timeIntervalSince1970)
        self.timestamp = timestamp
        self.open = try values.decode(Decimal.self, forKey: .open)
        self.high = try values.decode(Decimal.self, forKey: .high)
        self.low = try values.decode(Decimal.self, forKey: .low)
        self.close = try values.decode(Decimal.self, forKey: .close)
        self.volume = try values.decodeIfPresent(Decimal.self, forKey: .volume) ?? 0
        self.quoteVolume = try values.decodeIfPresent(Decimal.self, forKey: .quoteVolume)
        self.confirmed = try values.decodeIfPresent(Bool.self, forKey: .confirmed) ?? true
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(timestamp, forKey: .timestamp)
        try values.encode(open, forKey: .open)
        try values.encode(high, forKey: .high)
        try values.encode(low, forKey: .low)
        try values.encode(close, forKey: .close)
        try values.encode(volume, forKey: .volume)
        try values.encodeIfPresent(quoteVolume, forKey: .quoteVolume)
        try values.encode(confirmed, forKey: .confirmed)
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
/// the first live-trading surface. `quantity` is an OKX swap contract count
/// (`sz`), already aligned to the instrument's `lotSz`; callers that size by
/// quote notional must convert through `SwapInstrumentSpec` first. Secrets and
/// auth tokens never cross this boundary.
public struct LiveOrderRequest: Codable, Equatable, Sendable {
    public let instrumentID: String
    public let side: String
    public let orderType: String
    public let quantity: Decimal
    public let positionSide: String?
    public let marginMode: String
    public let reduceOnly: Bool
    public let price: Decimal?
    public let takeProfitTriggerPrice: Decimal?
    public let stopLossTriggerPrice: Decimal?
    /// Optional contract leverage. `nil` preserves the exchange/account
    /// default for callers such as manual reduce-only orders.
    public let leverage: Decimal?
    /// Stable exchange client identifier used to recover an interrupted
    /// submission. Optional preserves requests persisted by older versions.
    public let clientOrderID: String?

    public init(instrumentID: String, side: String, orderType: String = "market", quantity: Decimal, positionSide: String? = nil, marginMode: String = "cross", reduceOnly: Bool = false, price: Decimal? = nil, takeProfitTriggerPrice: Decimal? = nil, stopLossTriggerPrice: Decimal? = nil, leverage: Decimal? = nil, clientOrderID: String? = nil) {
        self.instrumentID = instrumentID
        self.side = side
        self.orderType = orderType
        self.quantity = quantity
        self.positionSide = positionSide
        self.marginMode = marginMode
        self.reduceOnly = reduceOnly
        self.price = price
        self.takeProfitTriggerPrice = takeProfitTriggerPrice
        self.stopLossTriggerPrice = stopLossTriggerPrice
        self.leverage = leverage
        self.clientOrderID = clientOrderID
    }

    private enum CodingKeys: String, CodingKey {
        case instrumentID, side, orderType, quantity, positionSide, marginMode, reduceOnly, price
        case takeProfitTriggerPrice, stopLossTriggerPrice, leverage, clientOrderID
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        instrumentID = try container.decode(String.self, forKey: .instrumentID)
        side = try container.decode(String.self, forKey: .side)
        orderType = try container.decodeIfPresent(String.self, forKey: .orderType) ?? "market"
        quantity = try container.decode(Decimal.self, forKey: .quantity)
        positionSide = try container.decodeIfPresent(String.self, forKey: .positionSide)
        marginMode = try container.decodeIfPresent(String.self, forKey: .marginMode) ?? "cross"
        reduceOnly = try container.decodeIfPresent(Bool.self, forKey: .reduceOnly) ?? false
        price = try container.decodeIfPresent(Decimal.self, forKey: .price)
        takeProfitTriggerPrice = try container.decodeIfPresent(Decimal.self, forKey: .takeProfitTriggerPrice)
        stopLossTriggerPrice = try container.decodeIfPresent(Decimal.self, forKey: .stopLossTriggerPrice)
        leverage = try container.decodeIfPresent(Decimal.self, forKey: .leverage)
        clientOrderID = try container.decodeIfPresent(String.self, forKey: .clientOrderID)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(instrumentID, forKey: .instrumentID)
        try container.encode(side, forKey: .side)
        try container.encode(orderType, forKey: .orderType)
        try container.encode(quantity, forKey: .quantity)
        try container.encodeIfPresent(positionSide, forKey: .positionSide)
        try container.encode(marginMode, forKey: .marginMode)
        try container.encode(reduceOnly, forKey: .reduceOnly)
        try container.encodeIfPresent(price, forKey: .price)
        try container.encodeIfPresent(takeProfitTriggerPrice, forKey: .takeProfitTriggerPrice)
        try container.encodeIfPresent(stopLossTriggerPrice, forKey: .stopLossTriggerPrice)
        try container.encodeIfPresent(leverage, forKey: .leverage)
        try container.encodeIfPresent(clientOrderID, forKey: .clientOrderID)
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

public enum StrategyType: RawRepresentable, Codable, CaseIterable, Hashable, Sendable {
    case sweepReversalShort
    case emaAltcoinLong
    /// High-Level Liquidity Sweep Reversal.  Keep the short identifier stable
    /// because it is persisted in strategy files and used by the service
    /// router; UI copy belongs in ``displayName``.
    case hlsr
    /// Preserve a package identifier when its runtime handler is unavailable.
    /// Removing a package must never make the entire trading ledger undecodable.
    case external(String)

    public init?(rawValue: String) {
        switch rawValue {
        case "sweepReversalShort": self = .sweepReversalShort
        case "emaAltcoinLong": self = .emaAltcoinLong
        case "hlsr": self = .hlsr
        default: self = .external(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .sweepReversalShort: return "sweepReversalShort"
        case .emaAltcoinLong: return "emaAltcoinLong"
        case .hlsr: return "hlsr"
        case .external(let identifier): return identifier
        }
    }

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self = StrategyType(rawValue: value) ?? .external(value)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    public var hasRuntimeHandler: Bool {
        if case .external = self { return false }
        return true
    }

    public static var allCases: [StrategyType] { availableCases }

    public static var availableCases: [StrategyType] { [.sweepReversalShort, .emaAltcoinLong, .hlsr] }

    /// Stable machine-readable identifier. Keep this independent from UI copy.
    public var identifier: String { rawValue }

    /// User-editable leverage bounds shared by the strategy editor and the
    /// persistence layer.  Individual OKX contracts can impose a lower
    /// exchange-side ceiling; the order gateway remains responsible for
    /// rejecting a value the selected instrument cannot support.
    public var leverageRange: ClosedRange<Double> { 1.0...100.0 }

    /// Default leverage shown when creating a new instance.
    public var defaultLeverage: Double { defaultParameters["leverage"] ?? 2.0 }

    public var displayName: String {
        switch self {
        case .sweepReversalShort: return "山寨币二次扫顶做空"
        case .emaAltcoinLong: return "双均线交易山寨币做多"
        case .hlsr: return "高位扫顶反转做空"
        case .external(let identifier): return identifier
        }
    }

    /// Stable English name used by exports and operator documentation.
    public var englishName: String {
        switch self {
        case .sweepReversalShort: return "Sweep Reversal Short"
        case .emaAltcoinLong: return "EMA Altcoin Long"
        case .hlsr: return "High-Level Liquidity Sweep Reversal"
        case .external(let identifier): return identifier
        }
    }

    /// 运行时参数默认值，逐项复制自各自实验室 `config/strategy.json` 的
    /// `signal_parameters`（机器参数真源）。领域层持有它是为了让 App 与
    /// TradingService 共用同一份默认值，避免两处漂移；
    /// `scripts/validate_strategy_sync.py` 会比对这里的值与实验室文件。
    public var defaultParameters: [String: Double] {
        switch self {
        case .sweepReversalShort:
            return [
                "L": 10, "R": 5, "majorWindow": 288, "sweepWait": 96, "rejectWait": 5,
                "resweepWait": 12, "rsiMin": 62, "volMult": 1.5, "rsDeep": 0.2,
                "atrPeriod": 14, "bufATR": 0.5, "tpMult": 2.2, "minATRPct": 0.5,
                "maxRiskATR": 5.0, "btcGateEnabled": 1,
                "entryTimeframeMinutes": 60, "confirmationTimeframeMinutes": 15,
                "confirmationWindowMinutes": 60,
                "leverage": 2,
                "maxConcurrentPositions": 1,
                "maxOpenRiskPercent": 5.0,
            ]
        case .emaAltcoinLong:
            return [
                "emaFast": 20, "emaSlow": 60, "emaTrend": 120, "atrPeriod": 14,
                "clusterATR": 0.75, "breakoutBars": 4, "pullbackBars": 6, "pullbackATR": 0.35,
                "minATRPct": 0.4, "minBreakoutATR": 0.3, "minSpreadATR": 0.5,
                "stopATR": 1.25, "targetR": 2.5, "maxHoldBars": 96,
                "minimumHistoryBars": 120, "gateSlopeBars": 6,
                "leverage": 2,
                "maxConcurrentPositions": 1,
                "maxOpenRiskPercent": 0.5,
            ]
        case .hlsr:
            // Values mirror strategies/hlsr/config/strategy.json.  Boolean
            // settings are represented as 0/1 because StrategyConfig's
            // parameter bag is intentionally numeric for persistence and
            // service-side overrides.
            return [
                "entryTimeframeMinutes": 15,
                "structureTimeframeMinutes": 240,
                "gain24hGt": 0.4,
                "quoteVolume24hGt": 30_000_000,
                "swingLookback": 6,
                "wickRatio": 0.6,
                "volumeMultiple": 1.0,
                "minimumRejectionScore": 2,
                "rejectDepthATR": 0.1,
                "confirmationWindow": 4,
                "stopATR": 0.25,
                "trailBars": 2,
                "allowRange": 1,
                // `zone_required: "any"` has no numeric restriction; zero
                // is the stable code understood by the runtime router.
                "zoneRequiredCode": 0,
                "leverage": 2,
                "partialTarget1": 0.3,
                "partialTarget2": 0.3,
                "partialTarget3": 0.4,
                "moveStopToEntryAfterTP1": 1,
                "cooldownBars": 16,
                "feeRateOneWay": 0.0006,
                "slippage": 0.0002,
                "fundingRate": 0,
                "maxConcurrentPositions": 1,
                "maxOpenRiskPercent": 5.0,
            ]
        case .external: return [:]
        }
    }

    /// 实验室给出的单笔风险上限（%），运行时会按此值收敛用户输入。
    public var maxRiskPercent: Double {
        switch self {
        case .sweepReversalShort: return 5.0
        case .emaAltcoinLong: return 0.5
        case .hlsr: return 5.0
        case .external: return 0
        }
    }

    /// 实验室给出的单笔风险默认值（%）。
    public var defaultRiskPercent: Double {
        switch self {
        case .sweepReversalShort: return 1.0
        case .emaAltcoinLong: return 0.5
        case .hlsr: return 0.5
        case .external: return 0
        }
    }

    /// This rule supplies its own ATR-based protective stop. It is deliberately
    /// descriptive because the stop distance is calculated per signal, not a
    /// fixed percentage that a strategy instance can override.
    public var stopLossDescription: String {
        switch self {
        case .sweepReversalShort:
            return "动态：扫顶期间最高价 + 0.5 × ATR14（按信号计算止损比例）"
        case .emaAltcoinLong:
            return "动态：入场价下方 1.25 × ATR14，止盈 2.5R"
        case .hlsr:
            return "动态：扫顶高点 + 0.25 × ATR14；TP1 后保本，TP2 后跟踪最近 2 根 15 分钟高点"
        case .external:
            return "运行时处理器不可用，策略已暂停"
        }
    }

    /// Entry bars consumed by the runtime monitor.  HLSR evaluates its
    /// structure on a separate completed 4H series, but all signal and order
    /// events are driven by confirmed 15m bars.
    public var entryInterval: KlineInterval {
        switch self {
        case .hlsr: return .fifteenMinutes
        case .sweepReversalShort, .emaAltcoinLong: return .oneHour
        case .external: return .oneHour
        }
    }

    /// Number of entry bars a strategy remains in cooldown after a complete
    /// exit.  This is surfaced to the UI and also used when migrating old
    /// configs that predate the strategy-specific cooldown.
    public var defaultCooldownBars: Int {
        switch self {
        case .hlsr: return 16
        case .sweepReversalShort, .emaAltcoinLong: return 96
        case .external: return 0
        }
    }
}
public enum StrategyState: String, Codable, Sendable { case draft, running, paused, error }

public enum StrategyScopeMode: String, Codable, CaseIterable, Sendable {
    case single
    case multiple
    case dynamicCategory
}

public enum StrategyUniverseCategory: String, Codable, CaseIterable, Sendable {
    case mainstream
    case hotAltcoins
    case emaAltcoinCandidates
    case highGain60
    case highGain100

    public var displayName: String {
        switch self {
        case .mainstream: return "主流币"
        case .hotAltcoins: return "热门榜前 20 个山寨币"
        case .emaAltcoinCandidates: return "双均线候选前 50 个山寨币"
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

    public var displayName: String {
        switch mode {
        case .single: return instrumentIDs.first ?? "未选择合约"
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
            let limit = category == .emaAltcoinCandidates ? 50 : 20
            return Self.contracts(for: category, in: contracts).prefix(limit).map(\.id)
        }
    }

    public func matches(_ instrumentID: String, contracts: [ContractMarket]) -> Bool {
        resolvedInstrumentIDs(from: contracts).contains(instrumentID)
    }

    private static func contracts(for category: StrategyUniverseCategory, in contracts: [ContractMarket]) -> [ContractMarket] {
        switch category {
        case .mainstream:
            return contracts.filter { StrategyUniverseRules.mainstreamSymbols.contains($0.baseCurrency.uppercased()) }
        case .hotAltcoins:
            let altcoins = contracts.filter(StrategyUniverseRules.isEligibleHotAltcoin)
            return altcoins.sorted { $0.volume24h > $1.volume24h }
        case .emaAltcoinCandidates:
            let altcoins = contracts.filter(StrategyUniverseRules.isEligibleHotAltcoin)
            return altcoins.sorted { $0.volume24h > $1.volume24h }
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
    /// Percentage of account equity assigned to this strategy instance's
    /// isolated capital pool. This is separate from the per-trade risk cap.
    public var capitalPoolPercent: Double
    public var cooldownBars: Int
    public var trailingStopPercent: Double
    public var scope: StrategyScope

    /// Configured contract leverage for this strategy instance.
    ///
    /// Leverage is kept in ``parameters`` so strategy packages can continue
    /// to add numeric knobs without changing the persisted ``StrategyConfig``
    /// schema.  The typed accessor gives the UI and runtime a stable field,
    /// while still preserving user overrides through Codable persistence.
    /// Older strategy files that predate this parameter read the strategy's
    /// built-in default (currently 2x).
    public var leverage: Double {
        get {
            let fallback = type.defaultParameters["leverage"] ?? 2.0
            guard let value = parameters["leverage"], value.isFinite, value > 0 else {
                return fallback
            }
            return value
        }
        set { parameters["leverage"] = newValue }
    }

    /// Stable strategy type identifier for routing and persistence.
    public var strategyIdentifier: String { type.identifier }

    /// User-facing strategy name. This may change without changing `type`.
    public var displayName: String { name }

    public init(id: UUID = UUID(), name: String, instrumentID: String, interval: KlineInterval, type: StrategyType, parameters: [String: Double] = [:], enabled: Bool = false, stopLossPercent: Double = 1.5, takeProfitPercent: Double = 3, riskPercent: Double = 1, capitalPoolPercent: Double = 100, cooldownBars: Int = 3, trailingStopPercent: Double = 1) {
        self.id = id; self.name = name; self.instrumentID = instrumentID; self.interval = interval; self.type = type; self.parameters = parameters; self.enabled = enabled
        self.stopLossPercent = stopLossPercent; self.takeProfitPercent = takeProfitPercent; self.riskPercent = riskPercent; self.capitalPoolPercent = capitalPoolPercent; self.cooldownBars = cooldownBars; self.trailingStopPercent = trailingStopPercent; self.scope = .single(instrumentID)
    }

    public init(id: UUID = UUID(), name: String, scope: StrategyScope, interval: KlineInterval, type: StrategyType, parameters: [String: Double] = [:], enabled: Bool = false, stopLossPercent: Double = 1.5, takeProfitPercent: Double = 3, riskPercent: Double = 1, capitalPoolPercent: Double = 100, cooldownBars: Int = 3, trailingStopPercent: Double = 1) {
        self.id = id; self.name = name; self.instrumentID = scope.instrumentIDs.first ?? ""; self.interval = interval; self.type = type; self.parameters = parameters; self.enabled = enabled
        self.stopLossPercent = stopLossPercent; self.takeProfitPercent = takeProfitPercent; self.riskPercent = riskPercent; self.capitalPoolPercent = capitalPoolPercent; self.cooldownBars = cooldownBars; self.trailingStopPercent = trailingStopPercent; self.scope = scope
    }

    private enum CodingKeys: String, CodingKey {
        case id, name, instrumentID, interval, type, parameters, enabled
        case stopLossPercent, takeProfitPercent, riskPercent, capitalPoolPercent
        case cooldownBars, trailingStopPercent, scope
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        instrumentID = try container.decodeIfPresent(String.self, forKey: .instrumentID) ?? ""
        interval = try container.decode(KlineInterval.self, forKey: .interval)
        type = try container.decode(StrategyType.self, forKey: .type)
        parameters = try container.decodeIfPresent([String: Double].self, forKey: .parameters) ?? [:]
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? false
        stopLossPercent = try container.decodeIfPresent(Double.self, forKey: .stopLossPercent) ?? 1.5
        takeProfitPercent = try container.decodeIfPresent(Double.self, forKey: .takeProfitPercent) ?? 3
        riskPercent = try container.decodeIfPresent(Double.self, forKey: .riskPercent) ?? 1
        capitalPoolPercent = try container.decodeIfPresent(Double.self, forKey: .capitalPoolPercent) ?? 100
        cooldownBars = try container.decodeIfPresent(Int.self, forKey: .cooldownBars) ?? 3
        trailingStopPercent = try container.decodeIfPresent(Double.self, forKey: .trailingStopPercent) ?? 1
        scope = try container.decodeIfPresent(StrategyScope.self, forKey: .scope) ?? .single(instrumentID)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(id, forKey: .id)
        try container.encode(name, forKey: .name)
        try container.encode(instrumentID, forKey: .instrumentID)
        try container.encode(interval, forKey: .interval)
        try container.encode(type, forKey: .type)
        try container.encode(parameters, forKey: .parameters)
        try container.encode(enabled, forKey: .enabled)
        try container.encode(stopLossPercent, forKey: .stopLossPercent)
        try container.encode(takeProfitPercent, forKey: .takeProfitPercent)
        try container.encode(riskPercent, forKey: .riskPercent)
        try container.encode(capitalPoolPercent, forKey: .capitalPoolPercent)
        try container.encode(cooldownBars, forKey: .cooldownBars)
        try container.encode(trailingStopPercent, forKey: .trailingStopPercent)
        try container.encode(scope, forKey: .scope)
    }
}

public struct StrategyCapitalSnapshot: Codable, Equatable, Sendable, Identifiable {
    public let strategyID: UUID
    public var id: UUID { strategyID }
    public let allocationPercent: Decimal
    public let initialCapital: Decimal
    public let equity: Decimal
    public let reservedCapital: Decimal
    public let availableCapital: Decimal
    public let realizedPnL: Decimal
    public let unrealizedPnL: Decimal
    public let rolloverCount: Int
    public let updatedAt: Date
    /// 未平仓订单的止损风险合计（|入场价 − 止损价| × 数量）。
    public let openRisk: Decimal
    /// 未平仓订单数。
    public let openPositions: Int

    public init(strategyID: UUID, allocationPercent: Decimal = 100, initialCapital: Decimal = 0, equity: Decimal = 0, reservedCapital: Decimal = 0, availableCapital: Decimal? = nil, realizedPnL: Decimal = 0, unrealizedPnL: Decimal = 0, rolloverCount: Int = 0, updatedAt: Date = .now, openRisk: Decimal = 0, openPositions: Int = 0) {
        self.strategyID = strategyID
        self.allocationPercent = allocationPercent
        self.initialCapital = initialCapital
        self.equity = equity
        self.reservedCapital = reservedCapital
        self.availableCapital = availableCapital ?? max(0, equity - reservedCapital)
        self.realizedPnL = realizedPnL
        self.unrealizedPnL = unrealizedPnL
        self.rolloverCount = rolloverCount
        self.updatedAt = updatedAt
        self.openRisk = openRisk
        self.openPositions = openPositions
    }
}

/// Shared sizing calculations for the USDT strategy capital base. The
/// snapshots include paused strategies and pools without live orders because
/// their allocated pool equity remains reserved for that strategy.
public struct StrategyCapitalAllocation: Equatable, Sendable {
    public let totalCapital: Decimal
    public let strategyCapitals: [StrategyCapitalSnapshot]

    public init(totalCapital: Decimal = 0, strategyCapitals: [StrategyCapitalSnapshot] = []) {
        self.totalCapital = totalCapital.isFinite ? max(0, totalCapital) : 0
        self.strategyCapitals = strategyCapitals
    }

    public var occupiedCapital: Decimal {
        strategyCapitals.reduce(0) { total, pool in
            total + (pool.equity.isFinite ? max(0, pool.equity) : 0)
        }
    }

    public var availableCapital: Decimal {
        max(0, totalCapital - occupiedCapital)
    }

    public var occupiedAllocationPercent: Decimal {
        guard totalCapital > 0 else { return 0 }
        return occupiedCapital / totalCapital * 100
    }

    public var availableAllocationPercent: Decimal {
        let configuredRemaining = 100 - strategyCapitals.reduce(0) { total, pool in
            total + (pool.allocationPercent.isFinite ? max(0, pool.allocationPercent) : 0)
        }
        guard totalCapital > 0 else { return 0 }
        return min(max(0, configuredRemaining), max(0, 100 - occupiedAllocationPercent))
    }

    public func capital(for allocationPercent: Decimal) -> Decimal {
        guard totalCapital > 0, allocationPercent.isFinite else { return 0 }
        return totalCapital * min(max(0, allocationPercent), 100) / 100
    }

    public func effectiveAllocationPercent(for requested: Decimal) -> Decimal {
        guard requested.isFinite else { return 0 }
        return min(max(0, requested), availableAllocationPercent)
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
    /// Full HLSR exit plan. `takePrice` remains TP1 for compatibility with
    /// existing dashboards and exchange order payloads.
    public let takePrices: [Decimal]?
    public let targetFractions: [Decimal]?
    public let moveStopToEntryAfterTP1: Bool
    public let trailBars: Int?
    public let invalidationPrice: Decimal?

    public init(id: UUID = UUID(), strategyID: UUID, type: String, price: Decimal, reason: String, timestamp: Date = .now, stopPrice: Decimal? = nil, takePrice: Decimal? = nil, takePrices: [Decimal]? = nil, targetFractions: [Decimal]? = nil, moveStopToEntryAfterTP1: Bool = false, trailBars: Int? = nil, invalidationPrice: Decimal? = nil) {
        self.id = id; self.strategyID = strategyID; self.type = type; self.price = price; self.reason = reason; self.timestamp = timestamp
        self.stopPrice = stopPrice; self.takePrice = takePrice ?? takePrices?.first
        self.takePrices = takePrices; self.targetFractions = targetFractions
        self.moveStopToEntryAfterTP1 = moveStopToEntryAfterTP1; self.trailBars = trailBars
        self.invalidationPrice = invalidationPrice
    }

    private enum CodingKeys: String, CodingKey {
        case id, strategyID, type, price, reason, timestamp
        case stopPrice, takePrice, takePrices, targetFractions
        case moveStopToEntryAfterTP1, trailBars, invalidationPrice
    }

    /// StrategySignal is persisted inside PaperTradingStore.  The HLSR exit
    /// plan added ``moveStopToEntryAfterTP1`` after older state files already
    /// existed; decoding it as a synthesized non-optional Bool would reject
    /// every pre-HLSR status on restart.  Missing optional legs remain nil and
    /// the new behavior defaults to false for legacy signals.
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        self.id = try values.decode(UUID.self, forKey: .id)
        self.strategyID = try values.decode(UUID.self, forKey: .strategyID)
        self.type = try values.decode(String.self, forKey: .type)
        self.price = try values.decode(Decimal.self, forKey: .price)
        self.reason = try values.decode(String.self, forKey: .reason)
        self.timestamp = try values.decode(Date.self, forKey: .timestamp)
        self.stopPrice = try values.decodeIfPresent(Decimal.self, forKey: .stopPrice)
        self.takePrices = try values.decodeIfPresent([Decimal].self, forKey: .takePrices)
        self.takePrice = try values.decodeIfPresent(Decimal.self, forKey: .takePrice) ?? self.takePrices?.first
        self.targetFractions = try values.decodeIfPresent([Decimal].self, forKey: .targetFractions)
        self.moveStopToEntryAfterTP1 = try values.decodeIfPresent(Bool.self, forKey: .moveStopToEntryAfterTP1) ?? false
        self.trailBars = try values.decodeIfPresent(Int.self, forKey: .trailBars)
        self.invalidationPrice = try values.decodeIfPresent(Decimal.self, forKey: .invalidationPrice)
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(strategyID, forKey: .strategyID)
        try values.encode(type, forKey: .type)
        try values.encode(price, forKey: .price)
        try values.encode(reason, forKey: .reason)
        try values.encode(timestamp, forKey: .timestamp)
        try values.encodeIfPresent(stopPrice, forKey: .stopPrice)
        try values.encodeIfPresent(takePrice, forKey: .takePrice)
        try values.encodeIfPresent(takePrices, forKey: .takePrices)
        try values.encodeIfPresent(targetFractions, forKey: .targetFractions)
        try values.encode(moveStopToEntryAfterTP1, forKey: .moveStopToEntryAfterTP1)
        try values.encodeIfPresent(trailBars, forKey: .trailBars)
        try values.encodeIfPresent(invalidationPrice, forKey: .invalidationPrice)
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
    /// 最近一次真正被处理的已确认 K 线时间戳。评估必须按 bar 幂等：同一根 K 线
    /// 被重复评估（REST 刷新图表、重复收盘事件）时不得再推进冷却或指标状态。
    public let lastEvaluatedBar: Date?

    public init(id: UUID, state: StrategyState, direction: String? = nil, cooldown: Int = 0, pnl: Decimal = 0, lastSignal: StrategySignal? = nil, indicators: [String: [Double]] = [:], lastEvaluatedBar: Date? = nil) {
        self.id = id; self.state = state; self.direction = direction; self.cooldown = cooldown; self.pnl = pnl; self.lastSignal = lastSignal; self.indicators = indicators; self.lastEvaluatedBar = lastEvaluatedBar
    }

    /// Returns the same status re-marked as evaluated at `bar`. Every status the
    /// engine returns goes through this, so the idempotency guard cannot be
    /// reset to "never evaluated" by accident.
    public func evaluated(at bar: Date?) -> StrategyStatus {
        StrategyStatus(id: id, state: state, direction: direction, cooldown: cooldown, pnl: pnl, lastSignal: lastSignal, indicators: indicators, lastEvaluatedBar: bar)
    }
}

public struct RiskSnapshot: Codable, Equatable, Sendable {
    public let equity: Decimal
    /// USDT balance used to size strategy pools; account equity remains the
    /// separate all-asset baseline for global risk limits.
    public let strategyCapitalBase: Decimal?
    public let equityPeak: Decimal
    public let dayStartEquity: Decimal
    /// UTC calendar-day boundary used for `dayStartEquity`. Optional for
    /// decoding snapshots written before the boundary was persisted.
    public let dayStartAt: Date?
    public let dailyPnLPercent: Decimal
    public let drawdownPercent: Decimal
    public let killSwitch: Bool
    public let reason: String?
    /// Persisted pool state lets a service restart continue compounding each
    /// strategy instance from its own realized equity.
    public let strategyCapitals: [StrategyCapitalSnapshot]
    /// Global notional reservations survive a service restart until the next
    /// authenticated account reconciliation removes completed exposure.
    public let globalNotionals: [String: Decimal]

    private enum CodingKeys: String, CodingKey {
        case equity, strategyCapitalBase, equityPeak, dayStartEquity, dayStartAt, dailyPnLPercent
        case drawdownPercent, killSwitch, reason, strategyCapitals, globalNotionals
    }

    public init(equity: Decimal = 0, equityPeak: Decimal = 0, dayStartEquity: Decimal = 0, dayStartAt: Date? = nil, dailyPnLPercent: Decimal = 0, drawdownPercent: Decimal = 0, killSwitch: Bool = false, reason: String? = nil, strategyCapitals: [StrategyCapitalSnapshot] = [], globalNotionals: [String: Decimal] = [:], strategyCapitalBase: Decimal? = nil) {
        self.equity = equity; self.strategyCapitalBase = strategyCapitalBase; self.equityPeak = equityPeak; self.dayStartEquity = dayStartEquity; self.dayStartAt = dayStartAt; self.dailyPnLPercent = dailyPnLPercent; self.drawdownPercent = drawdownPercent; self.killSwitch = killSwitch; self.reason = reason; self.strategyCapitals = strategyCapitals; self.globalNotionals = globalNotionals
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        equity = try container.decodeIfPresent(Decimal.self, forKey: .equity) ?? 0
        strategyCapitalBase = try container.decodeIfPresent(Decimal.self, forKey: .strategyCapitalBase)
        equityPeak = try container.decodeIfPresent(Decimal.self, forKey: .equityPeak) ?? 0
        dayStartEquity = try container.decodeIfPresent(Decimal.self, forKey: .dayStartEquity) ?? 0
        dayStartAt = try container.decodeIfPresent(Date.self, forKey: .dayStartAt)
        dailyPnLPercent = try container.decodeIfPresent(Decimal.self, forKey: .dailyPnLPercent) ?? 0
        drawdownPercent = try container.decodeIfPresent(Decimal.self, forKey: .drawdownPercent) ?? 0
        killSwitch = try container.decodeIfPresent(Bool.self, forKey: .killSwitch) ?? false
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        strategyCapitals = try container.decodeIfPresent([StrategyCapitalSnapshot].self, forKey: .strategyCapitals) ?? []
        globalNotionals = try container.decodeIfPresent([String: Decimal].self, forKey: .globalNotionals) ?? [:]
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(equity, forKey: .equity)
        try container.encodeIfPresent(strategyCapitalBase, forKey: .strategyCapitalBase)
        try container.encode(equityPeak, forKey: .equityPeak)
        try container.encode(dayStartEquity, forKey: .dayStartEquity)
        try container.encodeIfPresent(dayStartAt, forKey: .dayStartAt)
        try container.encode(dailyPnLPercent, forKey: .dailyPnLPercent)
        try container.encode(drawdownPercent, forKey: .drawdownPercent)
        try container.encode(killSwitch, forKey: .killSwitch)
        try container.encodeIfPresent(reason, forKey: .reason)
        try container.encode(strategyCapitals, forKey: .strategyCapitals)
        try container.encode(globalNotionals, forKey: .globalNotionals)
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
    /// Protection must keep the signal that authorized this entry even if
    /// the scanner later observes another setup or the service restarts.
    public let signal: StrategySignal?

    public init(id: UUID = UUID(), strategyID: UUID, instrumentID: String, side: String, quantity: Decimal, requestedAt: Date = .now, fillPrice: Decimal? = nil, status: String = "pending", remoteOrderID: String? = nil, signal: StrategySignal? = nil) {
        self.id = id; self.strategyID = strategyID; self.instrumentID = instrumentID; self.side = side; self.quantity = quantity
        self.requestedAt = requestedAt; self.fillPrice = fillPrice; self.status = status; self.remoteOrderID = remoteOrderID
        self.signal = signal
    }

    private enum CodingKeys: String, CodingKey {
        case id, strategyID, instrumentID, side, quantity, requestedAt, fillPrice, status, remoteOrderID, signal
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id)
        strategyID = try values.decode(UUID.self, forKey: .strategyID)
        instrumentID = try values.decode(String.self, forKey: .instrumentID)
        side = try values.decode(String.self, forKey: .side)
        quantity = try values.decode(Decimal.self, forKey: .quantity)
        requestedAt = try values.decodeIfPresent(Date.self, forKey: .requestedAt) ?? .now
        fillPrice = try values.decodeIfPresent(Decimal.self, forKey: .fillPrice)
        status = try values.decodeIfPresent(String.self, forKey: .status) ?? "pending"
        remoteOrderID = try values.decodeIfPresent(String.self, forKey: .remoteOrderID)
        signal = try values.decodeIfPresent(StrategySignal.self, forKey: .signal)
    }

    public func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id)
        try values.encode(strategyID, forKey: .strategyID)
        try values.encode(instrumentID, forKey: .instrumentID)
        try values.encode(side, forKey: .side)
        try values.encode(quantity, forKey: .quantity)
        try values.encode(requestedAt, forKey: .requestedAt)
        try values.encodeIfPresent(fillPrice, forKey: .fillPrice)
        try values.encode(status, forKey: .status)
        try values.encodeIfPresent(remoteOrderID, forKey: .remoteOrderID)
        try values.encodeIfPresent(signal, forKey: .signal)
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

    private enum CodingKeys: String, CodingKey {
        case maxInstrumentNotional, maxTotalNotional, maxMarginPercent
        case minOrderIntervalSeconds, maxOrdersPerHour, maxDailyLossPercent, maxDrawdownPercent
    }

    public init(maxInstrumentNotional: Decimal = 25_000, maxTotalNotional: Decimal = 100_000, maxMarginPercent: Decimal = 25, minOrderIntervalSeconds: Int = 15, maxOrdersPerHour: Int = 60, maxDailyLossPercent: Decimal = 5, maxDrawdownPercent: Decimal = 10) {
        // Limits are an input boundary.  A negative or non-finite ceiling can
        // otherwise make the comparison logic wrap into an unintended allow
        // path (or disable throttling entirely).  Invalid values fail closed
        // by becoming zero; callers can still choose an explicit zero limit.
        func nonNegative(_ value: Decimal) -> Decimal { value.isFinite && value >= 0 ? value : 0 }
        self.maxInstrumentNotional = nonNegative(maxInstrumentNotional)
        self.maxTotalNotional = nonNegative(maxTotalNotional)
        self.maxMarginPercent = nonNegative(maxMarginPercent)
        self.minOrderIntervalSeconds = max(0, minOrderIntervalSeconds)
        self.maxOrdersPerHour = max(0, maxOrdersPerHour)
        self.maxDailyLossPercent = nonNegative(maxDailyLossPercent)
        self.maxDrawdownPercent = nonNegative(maxDrawdownPercent)
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.init(
            maxInstrumentNotional: try container.decodeIfPresent(Decimal.self, forKey: .maxInstrumentNotional) ?? 25_000,
            maxTotalNotional: try container.decodeIfPresent(Decimal.self, forKey: .maxTotalNotional) ?? 100_000,
            maxMarginPercent: try container.decodeIfPresent(Decimal.self, forKey: .maxMarginPercent) ?? 25,
            minOrderIntervalSeconds: try container.decodeIfPresent(Int.self, forKey: .minOrderIntervalSeconds) ?? 15,
            maxOrdersPerHour: try container.decodeIfPresent(Int.self, forKey: .maxOrdersPerHour) ?? 60,
            maxDailyLossPercent: try container.decodeIfPresent(Decimal.self, forKey: .maxDailyLossPercent) ?? 5,
            maxDrawdownPercent: try container.decodeIfPresent(Decimal.self, forKey: .maxDrawdownPercent) ?? 10
        )
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
