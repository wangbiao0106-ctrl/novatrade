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

public enum KlineInterval: String, Codable, CaseIterable, Sendable {
    case oneMinute = "1m"
    case fiveMinutes = "5m"
    case fifteenMinutes = "15m"
    case oneHour = "1H"
    case fourHours = "4H"
    case oneDay = "1D"
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
    /// Rolling 24h quote turnover (USDT for linear swaps), not contracts or
    /// base-coin units; every universe ranking and volume floor uses it.
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
    /// gate must use this field rather than contract/base volume, and fails
    /// closed when it is nil rather than substituting contract volume.
    public let quoteVolume: Decimal?
    public let confirmed: Bool

    public init(timestamp: Date, open: Decimal, high: Decimal, low: Decimal, close: Decimal, volume: Decimal = 0, quoteVolume: Decimal? = nil, confirmed: Bool = true) {
        self.id = Int64(timestamp.timeIntervalSince1970)
        self.timestamp = timestamp; self.open = open; self.high = high; self.low = low; self.close = close
        self.volume = volume; self.quoteVolume = quoteVolume; self.confirmed = confirmed
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
    public let updatedAt: Date

    public init(instrumentID: String, interval: KlineInterval, candles: [Candle] = [], updatedAt: Date = .now) {
        self.instrumentID = instrumentID; self.interval = interval; self.candles = candles; self.updatedAt = updatedAt
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
    /// submission. Manual requests may omit it; the service assigns one.
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
    /// 日内翻倍后 15m 动能衰竭确认做空。
    case doublePumpExhaustionShort
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
        case "doublePumpExhaustionShort": self = .doublePumpExhaustionShort
        case "hlsr": self = .hlsr
        default: self = .external(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .sweepReversalShort: return "sweepReversalShort"
        case .doublePumpExhaustionShort: return "doublePumpExhaustionShort"
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

    public static var availableCases: [StrategyType] { [.sweepReversalShort, .hlsr, .doublePumpExhaustionShort] }

    /// Stable machine-readable identifier. Keep this independent from UI copy.
    public var identifier: String { rawValue }

    /// User-editable leverage bounds shared by the strategy editor and the
    /// persistence layer.  Individual OKX contracts can impose a lower
    /// exchange-side ceiling; the order gateway remains responsible for
    /// rejecting a value the selected instrument cannot support.
    public var leverageRange: ClosedRange<Double> { 1.0...100.0 }

    /// Default leverage shown when creating a new instance.
    public var defaultLeverage: Double { defaultParameters["leverage"] ?? 2.0 }

    /// Each runtime strategy owns its eligible-market definition. The scope
    /// is compiled from this rule and cannot silently fall back to the generic
    /// hot-altcoin ranking used by a different strategy.
    public var defaultUniverseCategory: StrategyUniverseCategory {
        switch self {
        case .sweepReversalShort: return .sweepCandidates
        case .hlsr: return .hlsrCandidates
        case .doublePumpExhaustionShort: return .doublePumpCandidates
        case .external: return .hotAltcoins
        }
    }

    public var defaultScope: StrategyScope { .dynamic(defaultUniverseCategory) }

    public var displayName: String {
        switch self {
        case .sweepReversalShort: return "山寨币二次扫顶做空"
        case .hlsr: return "高位扫顶反转做空"
        case .doublePumpExhaustionShort: return "日内翻倍动能衰竭确认做空"
        case .external(let identifier): return identifier
        }
    }

    /// Stable English name used by exports and operator documentation.
    public var englishName: String {
        switch self {
        case .sweepReversalShort: return "Sweep Reversal Short"
        case .hlsr: return "High-Level Liquidity Sweep Reversal"
        case .doublePumpExhaustionShort: return "Double Pump Exhaustion Short"
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
                "maxOpenRiskPercent": 1.0,
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
                "maxOpenRiskPercent": 1.0,
            ]
        case .doublePumpExhaustionShort:
            return [
                "atrPeriod": 14, "rsiPeriod": 14, "volumePeriod": 20,
                "gain24Gt": 1.0, "upperWickMin": 0.4, "closePositionMax": 0.5,
                "rsiMin": 50.0, "volumeMultiple": 0.5, "stopATR": 0.45,
                "targetR": 1.0, "minRiskATR": 0.5, "maxRiskATR": 3.0,
                "minimumHistoryBars": 97, "leverage": 2,
                "maxConcurrentPositions": 1, "maxOpenRiskPercent": 1.0,
            ]
        case .external: return [:]
        }
    }

    /// 实验室给出的单笔风险上限（%），运行时会按此值收敛用户输入。
    public var maxRiskPercent: Double {
        switch self {
        case .sweepReversalShort, .hlsr, .doublePumpExhaustionShort: return 1.0
        case .external: return 0
        }
    }

    /// 实验室给出的单笔风险默认值（%）。
    public var defaultRiskPercent: Double {
        switch self {
        case .sweepReversalShort, .hlsr, .doublePumpExhaustionShort: return 1.0
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
        case .hlsr:
            return "动态：扫顶高点 + 0.25 × ATR14；TP1 后保本，TP2 后跟踪最近 2 根 15 分钟高点"
        case .doublePumpExhaustionShort:
            return "动态：确认 K 线高点 + 0.45 × ATR14，止盈 1R"
        case .external:
            return "运行时处理器不可用，策略已暂停"
        }
    }

    /// Entry bars consumed by the runtime monitor.  HLSR evaluates its
    /// structure on a separate completed 4H series, but all signal and order
    /// events are driven by confirmed 15m bars.
    public var entryInterval: KlineInterval {
        switch self {
        case .hlsr, .doublePumpExhaustionShort: return .fifteenMinutes
        case .sweepReversalShort, .external: return .oneHour
        }
    }

    /// Number of entry bars a strategy remains in cooldown after a complete
    /// exit.
    public var defaultCooldownBars: Int {
        switch self {
        case .hlsr, .doublePumpExhaustionShort: return 16
        case .sweepReversalShort: return 96
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
    case hotAltcoins
    case sweepCandidates
    case hlsrCandidates
    case doublePumpCandidates

    public var displayName: String {
        switch self {
        case .hotAltcoins: return "热门榜前 20 个山寨币"
        case .sweepCandidates: return "扫顶候选（24h 成交额前 100、≥300 万 USDT）"
        case .hlsrCandidates: return "高位扫顶候选（24h 涨幅 >40%、成交额 >3000 万）"
        case .doublePumpCandidates: return "翻倍衰竭候选（24h 涨幅 >100%、成交额 ≥1000 万）"
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
            let ranked = Self.contracts(for: category, in: contracts)
            switch category {
            case .hotAltcoins:
                return ranked.prefix(20).map(\.id)
            case .sweepCandidates:
                return ranked.prefix(StrategyUniverseRules.sweepCandidateLimit).map(\.id)
            case .hlsrCandidates, .doublePumpCandidates:
                return ranked.map(\.id)
            }
        }
    }

    private static func contracts(for category: StrategyUniverseCategory, in contracts: [ContractMarket]) -> [ContractMarket] {
        switch category {
        case .hotAltcoins:
            let altcoins = contracts.filter(StrategyUniverseRules.isEligibleHotAltcoin)
            return altcoins.sorted { $0.volume24h > $1.volume24h }
        case .sweepCandidates:
            return contracts
                .filter(StrategyUniverseRules.isEligibleSweep)
                .sorted { $0.volume24h > $1.volume24h }
        case .hlsrCandidates:
            return contracts
                .filter(StrategyUniverseRules.isEligibleHLSR)
                .sorted { $0.changePercent > $1.changePercent }
        case .doublePumpCandidates:
            return contracts
                .filter(StrategyUniverseRules.isEligibleDoublePump)
                .sorted { $0.changePercent > $1.changePercent }
        }
    }
}

/// The resolved instrument set used by the running scanner for one strategy
/// instance. This read-only diagnostic model lets operators verify the actual
/// backend scan pool instead of inferring it from the scope label.
public struct StrategyUniverseSnapshot: Codable, Equatable, Sendable {
    public let strategyID: UUID
    public let strategyName: String
    public let strategyType: StrategyType
    public let enabled: Bool
    public let universeCategory: StrategyUniverseCategory?
    public let instrumentIDs: [String]
    public let targetCount: Int
    public let refreshedAt: Date

    public init(strategyID: UUID,
                strategyName: String,
                strategyType: StrategyType,
                enabled: Bool,
                universeCategory: StrategyUniverseCategory?,
                instrumentIDs: [String],
                refreshedAt: Date = .now) {
        self.strategyID = strategyID
        self.strategyName = strategyName
        self.strategyType = strategyType
        self.enabled = enabled
        self.universeCategory = universeCategory
        self.instrumentIDs = instrumentIDs
        self.targetCount = instrumentIDs.count
        self.refreshedAt = refreshedAt
    }
}

public struct StrategyConfig: Codable, Equatable, Sendable, Identifiable {
    public let id: UUID
    public var name: String
    public var interval: KlineInterval
    public var type: StrategyType
    public var parameters: [String: Double]
    public var enabled: Bool
    public var riskPercent: Double
    /// Percentage of account equity assigned to this strategy instance's
    /// isolated capital pool. This is separate from the per-trade risk cap.
    public var capitalPoolPercent: Double
    public var cooldownBars: Int
    public var scope: StrategyScope

    /// Configured contract leverage for this strategy instance. It lives in
    /// ``parameters`` so strategy packages can add numeric knobs without
    /// changing the persisted schema.
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

    public init(id: UUID = UUID(), name: String, scope: StrategyScope, interval: KlineInterval, type: StrategyType, parameters: [String: Double] = [:], enabled: Bool = false, riskPercent: Double = 1, capitalPoolPercent: Double = 100, cooldownBars: Int = 3) {
        self.id = id; self.name = name; self.interval = interval; self.type = type; self.parameters = parameters; self.enabled = enabled
        self.riskPercent = riskPercent; self.capitalPoolPercent = capitalPoolPercent; self.cooldownBars = cooldownBars; self.scope = scope
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
    /// Full HLSR exit plan. `takePrice` is TP1, used for the exchange
    /// take-profit order.
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
    /// UTC calendar-day boundary used for `dayStartEquity`.
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

    public init(equity: Decimal = 0, equityPeak: Decimal = 0, dayStartEquity: Decimal = 0, dayStartAt: Date? = nil, dailyPnLPercent: Decimal = 0, drawdownPercent: Decimal = 0, killSwitch: Bool = false, reason: String? = nil, strategyCapitals: [StrategyCapitalSnapshot] = [], globalNotionals: [String: Decimal] = [:], strategyCapitalBase: Decimal? = nil) {
        self.equity = equity; self.strategyCapitalBase = strategyCapitalBase; self.equityPeak = equityPeak; self.dayStartEquity = dayStartEquity; self.dayStartAt = dayStartAt; self.dailyPnLPercent = dailyPnLPercent; self.drawdownPercent = drawdownPercent; self.killSwitch = killSwitch; self.reason = reason; self.strategyCapitals = strategyCapitals; self.globalNotionals = globalNotionals
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

public struct RiskLimits: Equatable, Sendable {
    public var maxInstrumentNotional: Decimal
    public var maxTotalNotional: Decimal
    public var maxMarginPercent: Decimal
    public var minOrderIntervalSeconds: Int
    public var maxOrdersPerHour: Int
    public var maxDailyLossPercent: Decimal
    public var maxDrawdownPercent: Decimal

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

/// The loopback endpoint shared by `okx-locald` and the desktop client.
public enum LocalService {
    public static let defaultPort = 8787
    public static let portEnvironmentKey = "OKX_LOCALD_PORT"
    public static var defaultBaseURL: URL { URL(string: "http://127.0.0.1:\(defaultPort)")! }
}

/// One frame on the `/api/v1/stream` websocket. `payload` carries a
/// JSON-encoded domain value (candle, strategy status, risk, …) or a short
/// connection marker, depending on `type`.
public struct StreamEvent: Codable, Equatable, Sendable {
    public let type: String
    public let timestamp: Date?
    public let instrumentID: String?
    public let payload: String?

    public init(type: String, timestamp: Date? = nil, instrumentID: String? = nil, payload: String? = nil) {
        self.type = type; self.timestamp = timestamp; self.instrumentID = instrumentID; self.payload = payload
    }
}

/// The client → daemon message on `/api/v1/stream`. Each message replaces the
/// socket's previous subscription; an empty `channels` list only detaches it.
public struct StreamSubscription: Codable, Equatable, Sendable {
    public let channels: [String]
    public let instrumentID: String
    public let interval: KlineInterval

    public init(channels: [String], instrumentID: String, interval: KlineInterval) {
        self.channels = channels; self.instrumentID = instrumentID; self.interval = interval
    }
}
