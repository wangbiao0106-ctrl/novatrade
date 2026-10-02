import Foundation
import TradingDomain

public struct ATKCommandResult: Sendable, Equatable {
    public let stdout: String
    public let stderr: String
    public let exitCode: Int32

    public init(stdout: String, stderr: String = "", exitCode: Int32 = 0) {
        self.stdout = stdout
        self.stderr = stderr
        self.exitCode = exitCode
    }
}

public protocol ATKCommandRunning: Sendable {
    func run(arguments: [String]) async throws -> ATKCommandResult
}

public enum ATKError: LocalizedError, Sendable, Equatable {
    case unavailable(String)
    case commandFailed(code: Int32, message: String)
    case invalidJSON(String)
    case invalidInput(String)
    case apiKeyNotConfigured
    case timedOut
    case demoProfile(profile: String)
    case liveTradingDisabled
    case invalidOrder(String)

    public var errorDescription: String? {
        switch self {
        case let .unavailable(message): return message
        case let .commandFailed(code, message): return "ATK 命令失败（退出码 \(code)）：\(message)"
        case let .invalidJSON(message): return "ATK 返回了无法解析的 JSON：\(message)"
        case let .invalidInput(message): return "ATK 输入无效：\(message)"
        case .apiKeyNotConfigured: return "ATK 未检测到 API Key profile，请先运行 okx config init"
        case .timedOut: return "ATK 命令超时，已终止进程"
        case let .demoProfile(profile): return "当前 OKX profile（" + profile + "）是模拟盘，已拒绝真实下单"
        case .liveTradingDisabled: return "实盘交易尚未在本地服务中启用"
        case let .invalidOrder(message): return "实盘订单无效：" + message
        }
    }
}

/// CLI profile metadata with every secret dropped at decode time.
struct ATKProfileSummary: Equatable, Sendable {
    let id: String
    var site: String?
    var hasAPIKey = false
    var demo = false
}

struct ATKConfigSummary: Equatable, Sendable {
    let defaultProfile: String?
    let profiles: [ATKProfileSummary]
}

public struct LiveOrderCommandResult: Codable, Equatable, Sendable {
    public let orderID: String
    public let clientOrderID: String?
    public let code: String?
    public let message: String?

    public init(orderID: String, clientOrderID: String? = nil, code: String? = nil, message: String? = nil) {
        self.orderID = orderID; self.clientOrderID = clientOrderID; self.code = code; self.message = message
    }
}

/// The exchange's net result for one closed position, including its trading
/// fees and funding. Matching the stable position ID avoids attributing an
/// unrelated trade on the same instrument to a strategy.
public struct ClosedSwapPositionSnapshot: Equatable, Sendable {
    public let positionID: String
    public let instrumentID: String
    public let realizedPnL: Decimal
    public let closedAt: Date

    public init(positionID: String, instrumentID: String, realizedPnL: Decimal, closedAt: Date) {
        self.positionID = positionID
        self.instrumentID = instrumentID
        self.realizedPnL = realizedPnL
        self.closedAt = closedAt
    }
}

public struct ATKClient: Sendable {
    private static let utcCalendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }()

    private let runner: any ATKCommandRunning
    private let decoder: JSONDecoder

    public init(runner: any ATKCommandRunning = LocalATKCommandRunner()) {
        self.runner = runner
        self.decoder = JSONDecoder()
    }

    /// Returns only profile names, sites, and whether an API key exists. Secrets are never decoded or retained.
    func configSummary() async throws -> ATKConfigSummary {
        let result = try await run(["config", "show", "--json"])
        guard let data = result.stdout.data(using: .utf8) else { throw ATKError.invalidJSON("不是 UTF-8") }
        do {
            guard let root = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw ATKError.invalidJSON("配置根节点不是对象")
            }
            let defaultProfile = root["default_profile"] as? String ?? root["defaultProfile"] as? String
            let profileObject = root["profiles"] as? [String: Any] ?? [:]
            let profiles = profileObject.keys.sorted().map { name -> ATKProfileSummary in
                let raw = profileObject[name] as? [String: Any] ?? [:]
                let site = raw["site"] as? String
                let hasAPIKey = ((raw["api_key"] as? String)?.isEmpty == false) || ((raw["apiKey"] as? String)?.isEmpty == false)
                let demo = raw["demo"] as? Bool ?? false
                return ATKProfileSummary(id: name, site: site, hasAPIKey: hasAPIKey, demo: demo)
            }
            return ATKConfigSummary(defaultProfile: defaultProfile, profiles: profiles)
        } catch let error as ATKError {
            throw error
        } catch {
            throw ATKError.invalidJSON(error.localizedDescription)
        }
    }

    public func marketTicker(instrumentID: String) async throws -> MarketTicker {
        try Self.validateInstrumentID(instrumentID)
        let result = try await run(["market", "ticker", instrumentID, "--json"])
        return try decodeTicker(result.stdout, fallbackInstrumentID: instrumentID)
    }

    public func marketCandles(instrumentID: String, interval: KlineInterval, limit: Int = 300) async throws -> [Candle] {
        try Self.validateInstrumentID(instrumentID)
        let root = try await runJSON(["market", "candles", instrumentID, "--bar", interval.rawValue, "--limit", String(min(max(limit, 1), 300))])
        guard let rows = root as? [[Any]] else { throw ATKError.invalidJSON("K 线数据格式无效") }
        return try rows.enumerated().map { index, row in
            guard let candle = Self.decodeCandle(row) else {
                throw ATKError.invalidJSON("K 线第 \(index) 行格式无效")
            }
            return candle
        }.sorted { $0.timestamp < $1.timestamp }
    }

    public func marketContracts() async throws -> [ContractMarket] {
        let root = try await runJSON(["market", "tickers", "SWAP"])
        let rows: [[String: Any]]
        if let array = root as? [[String: Any]] {
            rows = array
        } else if let object = root as? [String: Any], let data = object["data"] as? [[String: Any]] {
            rows = data
        } else {
            throw ATKError.invalidJSON("合约行情格式无效")
        }
        let markets = rows.compactMap { row -> ContractMarket? in
            guard let id = row["instId"] as? String,
                  let last = Self.decimal(row["last"]), last > 0 else { return nil }
            // OKX exposes both UTC and UTC+8 day-open prices.  The market
            // sidebar's daily move must use the UTC day boundary.  Contracts
            // listed during the current UTC day report `sodUtc0` as 0, so they
            // fall back to the rolling 24h open.
            let utcDayOpen = Self.decimal(row["sodUtc0"]).flatMap { $0 > 0 ? $0 : nil }
            let rollingOpen = Self.decimal(row["open24h"]).flatMap { $0 > 0 ? $0 : nil }
            let open = utcDayOpen ?? rollingOpen ?? last
            let change = open == 0 ? 0 : (last - open) / open * 100
            let volume = Self.quoteVolume24h(row, last: last)
            let base = id.split(separator: "-").first.map(String.init) ?? id
            let quote = id.split(separator: "-").dropFirst().first.map(String.init) ?? "USDT"
            return ContractMarket(id: id, name: base, baseCurrency: base, quoteCurrency: quote, last: last, changePercent: change, volume24h: volume, category: "全部", updatedAt: .now)
        }
        // Keep the complete list while tagging ranking buckets for the sidebar.
        // A contract may belong to one visible bucket; the "全部" view always contains every item.
        let volumeIDs = Set(markets.sorted { $0.volume24h > $1.volume24h }.prefix(20).map(\.id))
        let gainIDs = Set(markets.sorted { $0.changePercent > $1.changePercent }.prefix(20).map(\.id))
        let lossIDs = Set(markets.sorted { $0.changePercent < $1.changePercent }.prefix(20).map(\.id))
        return markets.map { market in
            let category: String
            if volumeIDs.contains(market.id) { category = "热门" }
            else if gainIDs.contains(market.id) { category = "涨幅" }
            else if lossIDs.contains(market.id) { category = "跌幅" }
            else { category = "全部" }
            return ContractMarket(id: market.id, name: market.name, baseCurrency: market.baseCurrency, quoteCurrency: market.quoteCurrency, last: market.last, changePercent: market.changePercent, volume24h: market.volume24h, category: category, updatedAt: market.updatedAt)
        }
    }

    /// 24h quote turnover in the quote currency (USDT for linear swaps).
    ///
    /// For SWAP tickers OKX reports `vol24h` in contracts and `volCcy24h` in
    /// base-coin units; neither is quote turnover and there is no quote-volume
    /// field, so turnover is approximated as `volCcy24h × last`.  Ranking by the
    /// raw fields would favour cheap coins with huge unit counts.  A missing or
    /// invalid `volCcy24h` yields 0 so the contract fails every volume floor
    /// instead of falling back to a contract count.
    static func quoteVolume24h(_ row: [String: Any], last: Decimal) -> Decimal {
        guard let baseVolume = decimal(row["volCcy24h"]), baseVolume > 0, last > 0 else { return 0 }
        return baseVolume * last
    }

    /// Loads the exchange's contract specification used to translate swap
    /// contract counts into quote-currency notional.  The `--instId` filter is
    /// expected to return exactly one row; accepting a different or ambiguous
    /// row would allow a caller to size an order with the wrong contract.
    public func marketInstrumentSpec(instrumentID: String) async throws -> SwapInstrumentSpec {
        try Self.validateInstrumentID(instrumentID)
        let root = try await runJSON(["market", "instruments", "--instType", "SWAP", "--instId", instrumentID])
        guard let rows = Self.objectRows(root), rows.count == 1,
              let row = rows.first else {
            throw ATKError.invalidJSON("合约规格响应必须包含唯一数据行")
        }

        guard let returnedID = Self.requiredText(row["instId"]), returnedID == instrumentID else {
            throw ATKError.invalidJSON("合约规格返回了错误的合约")
        }
        guard let ctVal = Self.decimal(row["ctVal"]), ctVal.isFinite, ctVal > 0 else {
            throw ATKError.invalidJSON("合约规格 ctVal 无效")
        }
        guard let ctMult = Self.decimal(row["ctMult"]), ctMult.isFinite, ctMult > 0 else {
            throw ATKError.invalidJSON("合约规格 ctMult 无效")
        }
        guard let lotSize = Self.decimal(row["lotSz"]), lotSize.isFinite, lotSize > 0 else {
            throw ATKError.invalidJSON("合约规格 lotSz 无效")
        }
        guard let minSize = Self.decimal(row["minSz"]), minSize.isFinite, minSize > 0 else {
            throw ATKError.invalidJSON("合约规格 minSz 无效")
        }
        guard let tickSize = Self.decimal(row["tickSz"]), tickSize.isFinite, tickSize > 0 else {
            throw ATKError.invalidJSON("合约规格 tickSz 无效")
        }
        guard let state = Self.requiredText(row["state"]), state.lowercased() == "live" else {
            throw ATKError.invalidJSON("合约规格状态不是 live")
        }
        guard let contractType = Self.requiredText(row["ctType"]), contractType.lowercased() == "linear" else {
            throw ATKError.invalidJSON("仅支持 linear 合约")
        }
        guard let settleCurrency = Self.requiredText(row["settleCcy"]), settleCurrency.uppercased() == "USDT" else {
            throw ATKError.invalidJSON("仅支持 USDT 结算合约")
        }
        let spec = SwapInstrumentSpec(
            instrumentID: returnedID,
            ctVal: ctVal,
            ctMult: ctMult,
            lotSize: lotSize,
            minSize: minSize,
            tickSize: tickSize,
            state: state,
            ctType: contractType,
            settleCurrency: settleCurrency
        )
        guard spec.isLiveUSDTLinearSwap else {
            throw ATKError.invalidJSON("合约规格不是 live USDT linear swap")
        }
        return spec
    }

    public func accountOverview() async throws -> AccountOverview {
        let profileSummary = try await configSummary()
        let profile = profileSummary.defaultProfile.flatMap { name in profileSummary.profiles.first(where: { $0.id == name }) }
        guard let profile, profile.hasAPIKey else { throw ATKError.apiKeyNotConfigured }
        let balance = try await runJSON(["account", "balance-all", "--valuationCcy", "USD"])
        let config = try await runJSON(["account", "config"])
        let positions = try await runJSON(["account", "positions", "--instType", "SWAP"])
        let bills = (try? await runJSON(["account", "bills", "--instType", "SWAP", "--limit", "100"])) ?? []
        guard let balanceObject = balance as? [String: Any],
              let trading = balanceObject["trading"] as? [String: Any] else {
            throw ATKError.invalidJSON("账户余额缺少 trading 数据")
        }
        let valuation = balanceObject["valuation"] as? [String: Any]
        let totalEq = Self.decimal(valuation?["totalBal"]) ?? Self.decimal(trading["totalEq"])
        guard let totalEq, totalEq.isFinite, totalEq >= 0 else {
            throw ATKError.invalidJSON("账户余额缺少有效权益")
        }
        let availableEq = Self.decimal(trading["adjEq"]) ?? totalEq
        let todayPnL = Self.todayPnL(from: bills, now: .now)
        let assets = ((trading["details"] as? [[String: Any]]) ?? []).compactMap { row -> AccountAsset? in
            guard let currency = row["ccy"] as? String, let equity = Self.decimal(row["eq"]) else { return nil }
            let frozen = Self.decimal(row["frozenBal"]) ?? 0
            let available = Self.decimal(row["availEq"]) ?? Self.decimal(row["availBal"]) ?? max(0, equity - frozen)
            return AccountAsset(currency: currency, equity: equity, available: available, usdValue: Self.decimal(row["eqUsd"]))
        }
        let accountConfig = (config as? [[String: Any]])?.first ?? (config as? [String: Any]) ?? [:]
        let label = accountConfig["label"] as? String
        guard let positionRows = Self.objectRows(positions) else {
            throw ATKError.invalidJSON("账户持仓格式无效")
        }
        let accountPositions = try positionRows.compactMap(Self.decodePosition)
        return AccountOverview(mode: profile.demo ? .paper : .live, profile: profile.id, site: profile.site, label: label, authenticated: true, equityUSD: totalEq, availableEquityUSD: availableEq, totalAssetValueUSD: totalEq, todayPnLUSD: todayPnL, assets: assets, positions: accountPositions)
    }

    /// Places a single swap order through the logged-in CLI profile. Live
    /// orders require an explicitly non-demo profile.
    public func placeSwapOrder(_ request: LiveOrderRequest) async throws -> LiveOrderCommandResult {
        try await placeSwapOrder(request, mode: .live)
    }

    /// Places an order through OKX's simulated trading environment. The
    /// `--demo` flag makes OKX record the order and fill it using its own
    /// matching, margin and fee rules instead of the local paper broker.
    public func placeDemoSwapOrder(_ request: LiveOrderRequest) async throws -> LiveOrderCommandResult {
        try await placeSwapOrder(request, mode: .demo)
    }

    public func cancelDemoSwapOrder(instrumentID: String, orderID: String) async throws {
        try Self.validateInstrumentID(instrumentID)
        try Self.validateIdentifier(orderID, field: "订单 ID")
        try await mutateSwap(["--demo", "swap", "cancel", instrumentID, "--ordId", orderID])
    }

    public func cancelLiveSwapOrder(instrumentID: String, orderID: String) async throws {
        try Self.validateInstrumentID(instrumentID)
        try Self.validateIdentifier(orderID, field: "订单 ID")
        try await mutateSwap(["--live", "swap", "cancel", instrumentID, "--ordId", orderID])
    }

    public func closeDemoSwapPosition(instrumentID: String, positionSide: String? = nil) async throws {
        try await closeSwapPosition(instrumentID: instrumentID, positionSide: positionSide, demo: true)
    }

    public func closeLiveSwapPosition(instrumentID: String, positionSide: String? = nil) async throws {
        try await closeSwapPosition(instrumentID: instrumentID, positionSide: positionSide, demo: false)
    }

    private func closeSwapPosition(instrumentID: String, positionSide: String?, demo: Bool) async throws {
        try Self.validateInstrumentID(instrumentID)
        if let positionSide, !positionSide.isEmpty, !["net", "long", "short"].contains(positionSide.lowercased()) {
            throw ATKError.invalidOrder("持仓方向必须是 net、long 或 short")
        }
        var arguments = [demo ? "--demo" : "--live", "swap", "close", "--instId", instrumentID, "--mgnMode", "cross", "--autoCxl"]
        if let positionSide, !positionSide.isEmpty, positionSide.lowercased() != "net" { arguments += ["--posSide", positionSide.lowercased()] }
        _ = try await mutateSwap(arguments)
    }

    @discardableResult
    private func mutateSwap(_ arguments: [String]) async throws -> Any {
        let root = try await runJSON(arguments)
        if let failure = Self.responseFailure(root, defaultMessage: "OKX 拒绝风控处置") {
            throw ATKError.commandFailed(code: failure.code, message: failure.message)
        }
        return root
    }

    private enum SwapOrderMode { case live, demo }

    private func placeSwapOrder(_ request: LiveOrderRequest, mode: SwapOrderMode) async throws -> LiveOrderCommandResult {
        try Self.validateInstrumentID(request.instrumentID)
        if let clientOrderID = request.clientOrderID { try Self.validateClientOrderID(clientOrderID) }
        guard ["buy", "sell"].contains(request.side.lowercased()) else { throw ATKError.invalidOrder("方向必须是 buy 或 sell") }
        guard ["market", "limit"].contains(request.orderType.lowercased()) else { throw ATKError.invalidOrder("只支持 market 或 limit") }
        guard request.quantity > 0, request.quantity.isFinite else { throw ATKError.invalidOrder("数量必须是有限的正数") }
        if let leverage = request.leverage,
           !(leverage.isFinite && leverage >= 1 && leverage <= 100) {
            throw ATKError.invalidOrder("杠杆必须是 1 到 100 倍之间的有限值")
        }
        guard ["cross", "isolated"].contains(request.marginMode.lowercased()) else { throw ATKError.invalidOrder("保证金模式必须是 cross 或 isolated") }
        if let positionSide = request.positionSide, !positionSide.isEmpty, !["net", "long", "short"].contains(positionSide.lowercased()) {
            throw ATKError.invalidOrder("持仓方向必须是 net、long 或 short")
        }
        if request.orderType.lowercased() == "limit", !(request.price ?? 0 > 0 && (request.price ?? 0).isFinite) { throw ATKError.invalidOrder("限价单必须提供有限的正价格") }
        if let price = request.price, !(price > 0 && price.isFinite) { throw ATKError.invalidOrder("价格必须是有限的正数") }
        if let takeProfit = request.takeProfitTriggerPrice, !(takeProfit > 0 && takeProfit.isFinite) { throw ATKError.invalidOrder("止盈触发价必须是有限的正数") }
        if let stopLoss = request.stopLossTriggerPrice, !(stopLoss > 0 && stopLoss.isFinite) { throw ATKError.invalidOrder("止损触发价必须是有限的正数") }

        let summary = try await configSummary()
        guard let profileName = summary.defaultProfile,
              let profile = summary.profiles.first(where: { $0.id == profileName }) else {
            throw ATKError.apiKeyNotConfigured
        }
        guard profile.hasAPIKey else { throw ATKError.apiKeyNotConfigured }
        switch mode {
        case .live:
            guard !profile.demo else { throw ATKError.demoProfile(profile: profile.id) }
        case .demo:
            guard profile.demo else { throw ATKError.unavailable("当前 profile 不是 OKX 模拟盘") }
        }

        // `sz` is a contract count.  Validate the live instrument metadata
        // at the final gateway boundary as well as in TradingService so a
        // caller using ATKClient directly cannot bypass lot/minimum rules.
        let spec = try await marketInstrumentSpec(instrumentID: request.instrumentID)
        guard spec.accepts(contractQuantity: request.quantity) else {
            throw ATKError.invalidOrder("数量必须按合约 lotSz 对齐且不小于 minSz（当前数量为合约张数）")
        }
        for price in [request.price, request.takeProfitTriggerPrice, request.stopLossTriggerPrice].compactMap({ $0 }) {
            guard spec.accepts(price: price) else {
                throw ATKError.invalidOrder("价格必须按合约 tickSz 对齐")
            }
        }

        let modeArgument = mode == .live ? "--live" : "--demo"
        // The CLI ignores --lever on swap place. Apply leverage through its
        // dedicated account command and abort the entry if OKX rejects it.
        if let leverage = request.leverage, !request.reduceOnly {
            var leverageArguments = [modeArgument, "swap", "leverage", "--instId", request.instrumentID, "--lever", Self.decimalText(leverage), "--mgnMode", request.marginMode.lowercased()]
            if let positionSide = request.positionSide, ["long", "short"].contains(positionSide.lowercased()) {
                leverageArguments += ["--posSide", positionSide.lowercased()]
            }
            _ = try await mutateSwap(leverageArguments)
        }
        var arguments = [modeArgument, "swap", "place", "--instId", request.instrumentID, "--side", request.side.lowercased(), "--ordType", request.orderType.lowercased(), "--sz", Self.decimalText(request.quantity), "--tdMode", request.marginMode.lowercased()]
        if let positionSide = request.positionSide, !positionSide.isEmpty { arguments += ["--posSide", positionSide.lowercased()] }
        if request.reduceOnly { arguments.append("--reduceOnly") }
        if let clientOrderID = request.clientOrderID { arguments += ["--clOrdId", clientOrderID] }
        if let price = request.price { arguments += ["--px", Self.decimalText(price)] }
        if let takeProfit = request.takeProfitTriggerPrice {
            arguments += ["--tpTriggerPx", Self.decimalText(takeProfit), "--tpOrdPx", "-1", "--tpOrdKind", "condition", "--tpTriggerPxType", "mark"]
        }
        if let stopLoss = request.stopLossTriggerPrice {
            arguments += ["--slTriggerPx", Self.decimalText(stopLoss), "--slOrdPx", "-1", "--slTriggerPxType", "mark"]
        }
        let root = try await runJSON(arguments)
        if let failure = Self.responseFailure(root, defaultMessage: "OKX 拒绝订单") {
            throw ATKError.commandFailed(code: failure.code, message: failure.message)
        }
        let row = Self.firstJSONObject(root) ?? [:]
        let code = row["sCode"] as? String ?? row["code"] as? String
        let orderID = (row["ordId"] as? String) ?? (row["orderId"] as? String)
        guard let orderID, !orderID.isEmpty else { throw ATKError.invalidJSON("下单响应缺少 ordId") }
        return LiveOrderCommandResult(orderID: orderID, clientOrderID: row["clOrdId"] as? String ?? row["clientOrderId"] as? String, code: code, message: row["sMsg"] as? String ?? row["msg"] as? String)
    }

    public func swapPositions() async throws -> [PositionSnapshot] {
        let root = try await runJSON(["swap", "positions"])
        guard let rows = Self.objectRows(root) else { throw ATKError.invalidJSON("持仓响应格式无效") }
        return try rows.compactMap(Self.decodePosition)
    }

    public func swapOrders() async throws -> [OrderSnapshot] {
        let root = try await runJSON(["swap", "orders"])
        guard let rows = Self.objectRows(root) else { throw ATKError.invalidJSON("订单响应格式无效") }
        return try rows.compactMap(Self.decodeOrder)
    }

    /// Looks up one exact client order id. Only OKX's explicit 51603
    /// (order does not exist) is an absent order; network/auth failures and
    /// malformed/empty success responses remain errors so recovery cannot
    /// interpret them as permission to submit another order.
    public func swapOrder(instrumentID: String, clientOrderID: String, demo: Bool) async throws -> OrderSnapshot? {
        try Self.validateInstrumentID(instrumentID)
        try Self.validateClientOrderID(clientOrderID)
        let arguments = [demo ? "--demo" : "--live", "swap", "get", "--instId", instrumentID, "--clOrdId", clientOrderID, "--json"]
        let result = try await runner.run(arguments: arguments)
        let root = try? JSONSerialization.jsonObject(with: Data(result.stdout.utf8))
        if let root, let failure = Self.responseFailure(root, defaultMessage: "OKX 订单查询失败") {
            if failure.code == 51603 { return nil }
            throw ATKError.commandFailed(code: failure.code, message: failure.message)
        }
        guard result.exitCode == 0 else {
            // The installed CLI prints API failures to stderr as separate
            // `Code: ...` lines even with --json. Match that exact line rather
            // than finding a code in an unrelated error/hint sentence.
            let codeLines = result.stderr.split(whereSeparator: \.isNewline)
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            if codeLines.contains("Code: 51603") { return nil }
            let message = result.stderr.isEmpty ? result.stdout : result.stderr
            throw ATKError.commandFailed(code: result.exitCode, message: message.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        guard let root, let rows = Self.objectRows(root), rows.count == 1,
              let order = try Self.decodeOrder(rows[0]), order.instrumentID == instrumentID,
              (rows[0]["clOrdId"] as? String ?? rows[0]["clientOrderId"] as? String) == clientOrderID else {
            throw ATKError.invalidJSON("订单查询未返回匹配的客户端订单号")
        }
        return order
    }

    private static func validateClientOrderID(_ value: String) throws {
        guard (1...32).contains(value.utf8.count),
              value.utf8.allSatisfy({ byte in
                  (48...57).contains(byte) || (65...90).contains(byte) || (97...122).contains(byte)
              }) else {
            throw ATKError.invalidOrder("客户端订单号必须是 1 到 32 位字母或数字")
        }
    }

    public func closedSwapPositions(instrumentID: String) async throws -> [ClosedSwapPositionSnapshot] {
        try Self.validateInstrumentID(instrumentID)
        let root = try await runJSON(["account", "positions-history", "--instType", "SWAP", "--instId", instrumentID, "--limit", "100"])
        guard let rows = Self.objectRows(root) else { throw ATKError.invalidJSON("历史持仓响应格式无效") }
        return try rows.map { row in
            guard let returnedID = Self.requiredText(row["instId"]), returnedID == instrumentID,
                  let positionID = Self.requiredText(row["posId"]), Self.isSafeIdentifier(positionID),
                  let realized = Self.decimal(row["realizedPnl"]), realized.isFinite,
                  let closedAt = Self.date(row["uTime"]) else {
                throw ATKError.invalidJSON("历史持仓缺少有效合约、持仓标识、净盈亏或结算时间")
            }
            return ClosedSwapPositionSnapshot(positionID: positionID, instrumentID: returnedID,
                                              realizedPnL: realized, closedAt: closedAt)
        }
    }

    static func todayPnL(from root: Any, now: Date) -> Decimal {
        let rows: [[String: Any] ]
        if let array = root as? [[String: Any]] { rows = array }
        else if let object = root as? [String: Any], let data = object["data"] as? [[String: Any]] { rows = data }
        else { return 0 }
        return rows.reduce(Decimal.zero) { total, row in
            let timestamp: TimeInterval?
            if let value = row["ts"] as? String { timestamp = Double(value).map { $0 / 1000 } }
            else if let value = row["ts"] as? NSNumber { timestamp = value.doubleValue / 1000 }
            else { timestamp = nil }
            guard let timestamp, Self.utcCalendar.isDate(Date(timeIntervalSince1970: timestamp), inSameDayAs: now), let pnl = decimal(row["pnl"]) else { return total }
            return total + pnl
        }
    }

    private func run(_ arguments: [String]) async throws -> ATKCommandResult {
        let result = try await runner.run(arguments: arguments)
        guard result.exitCode == 0 else {
            let message = result.stderr.isEmpty ? result.stdout : result.stderr
            throw ATKError.commandFailed(code: result.exitCode, message: message.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return result
    }

    private func runJSON(_ arguments: [String]) async throws -> Any {
        let result = try await run(arguments + ["--json"])
        guard let data = result.stdout.data(using: .utf8) else { throw ATKError.invalidJSON("不是 UTF-8") }
        do {
            let root = try JSONSerialization.jsonObject(with: data)
            if let failure = Self.responseFailure(root, defaultMessage: "OKX API 请求失败") {
                throw ATKError.commandFailed(code: failure.code, message: failure.message)
            }
            return root
        }
        catch let error as ATKError { throw error }
        catch { throw ATKError.invalidJSON(error.localizedDescription) }
    }

    private static func decodePosition(_ row: [String: Any]) throws -> PositionSnapshot? {
        guard let quantity = Self.decimal(row["pos"]), quantity.isFinite else {
            throw ATKError.invalidJSON("持仓缺少有效数量")
        }
        if quantity == 0 { return nil }
        guard let instrument = row["instId"] as? String, !instrument.isEmpty else {
            throw ATKError.invalidJSON("持仓缺少合约")
        }
        guard Self.isSafeIdentifier(instrument) else {
            throw ATKError.invalidJSON("持仓合约无效")
        }
        guard let positionID = row["posId"] as? String, !positionID.isEmpty,
              Self.isSafeIdentifier(positionID) else {
            throw ATKError.invalidJSON("持仓缺少有效标识")
        }
        guard let entryPrice = Self.decimal(row["avgPx"]), entryPrice.isFinite, entryPrice > 0 else {
            throw ATKError.invalidJSON("持仓缺少有效开仓价")
        }
        let side = row["posSide"] as? String ?? "net"
        guard ["net", "long", "short"].contains(side.lowercased()) else {
            throw ATKError.invalidJSON("持仓方向无效")
        }
        let markPrice: Decimal?
        if row["markPx"] != nil {
            guard let value = Self.decimal(row["markPx"]), value.isFinite, value > 0 else {
                throw ATKError.invalidJSON("持仓标记价无效")
            }
            markPrice = value
        } else { markPrice = nil }
        let unrealizedPnL: Decimal?
        if row["upl"] != nil {
            guard let value = Self.decimal(row["upl"]), value.isFinite else {
                throw ATKError.invalidJSON("持仓未实现盈亏无效")
            }
            unrealizedPnL = value
        } else { unrealizedPnL = nil }
        return PositionSnapshot(id: positionID, instrumentID: instrument, side: side.lowercased(), quantity: quantity, entryPrice: entryPrice, markPrice: markPrice, unrealizedPnL: unrealizedPnL)
    }

    private static func decodeOrder(_ row: [String: Any]) throws -> OrderSnapshot? {
        guard let instrument = row["instId"] as? String, !instrument.isEmpty,
              Self.isSafeIdentifier(instrument),
              let quantity = Self.decimal(row["sz"]), quantity.isFinite, quantity > 0 else {
            throw ATKError.invalidJSON("订单缺少有效合约或数量")
        }
        guard let orderID = row["ordId"] as? String, !orderID.isEmpty,
              Self.isSafeIdentifier(orderID),
              let side = row["side"] as? String,
              ["buy", "sell"].contains(side.lowercased()),
              let state = row["state"] as? String, !state.isEmpty else {
            throw ATKError.invalidJSON("订单缺少有效标识、方向或状态")
        }
        guard let created = Self.date(row["cTime"] ?? row["uTime"]) else {
            throw ATKError.invalidJSON("订单缺少有效时间")
        }
        let price: Decimal?
        if let rawPrice = row["px"] {
            guard let parsed = Self.decimal(rawPrice), parsed.isFinite, parsed >= 0 else {
                throw ATKError.invalidJSON("订单价格无效")
            }
            price = parsed > 0 ? parsed : nil
        } else { price = nil }
        return OrderSnapshot(id: orderID, instrumentID: instrument, side: side.lowercased(), status: state.lowercased(), quantity: quantity, price: price, createdAt: created)
    }

    private static func decodeCandle(_ row: [Any]) -> Candle? {
        guard row.count >= 9,
              let milliseconds = decimal(row[0]).map({ NSDecimalNumber(decimal: $0).doubleValue }),
              let open = decimal(row[1]), let high = decimal(row[2]), let low = decimal(row[3]), let close = decimal(row[4]), let volume = decimal(row[5]), let quoteVolume = decimal(row[7]) else { return nil }
        guard milliseconds.isFinite, milliseconds > 0,
              open.isFinite, high.isFinite, low.isFinite, close.isFinite, volume.isFinite,
              open > 0, high > 0, low > 0, close > 0, volume >= 0, quoteVolume >= 0,
              high >= low, high >= open, high >= close, low <= open, low <= close else { return nil }
        let confirmed: Bool
        guard let flag = row[8] as? String, flag == "0" || flag == "1" else { return nil }
        confirmed = flag == "1"
        return Candle(timestamp: Date(timeIntervalSince1970: milliseconds / 1000), open: open, high: high, low: low, close: close, volume: volume, quoteVolume: quoteVolume, confirmed: confirmed)
    }

    private func decodeTicker(_ text: String, fallbackInstrumentID: String) throws -> MarketTicker {
        guard let data = text.data(using: .utf8) else { throw ATKError.invalidJSON("不是 UTF-8") }
        do {
            let root = try JSONSerialization.jsonObject(with: data)
            let row: [String: Any]?
            if let array = root as? [[String: Any]] {
                row = array.first
            } else if let object = root as? [String: Any], let rows = object["data"] as? [[String: Any]] {
                row = rows.first
            } else if let object = root as? [String: Any] {
                row = object
            } else {
                row = nil
            }
            guard let row else { throw ATKError.invalidJSON("ticker 数据为空") }
            let instrumentID = (row["instId"] as? String) ?? (row["instrumentId"] as? String) ?? fallbackInstrumentID
            guard instrumentID == fallbackInstrumentID,
                  let last = Self.decimal(row["last"]), last > 0 else {
                throw ATKError.invalidJSON("ticker 缺少有效的 last")
            }
            let bid: Decimal?
            if let raw = row["bidPx"] ?? row["bid"] {
                guard let parsed = Self.decimal(raw), parsed > 0 else { throw ATKError.invalidJSON("ticker 的 bid 无效") }
                bid = parsed
            } else {
                bid = nil
            }
            let ask: Decimal?
            if let raw = row["askPx"] ?? row["ask"] {
                guard let parsed = Self.decimal(raw), parsed > 0 else { throw ATKError.invalidJSON("ticker 的 ask 无效") }
                ask = parsed
            } else {
                ask = nil
            }
            guard let rawTimestamp = row["ts"],
                  let timestamp = Self.date(rawTimestamp) else {
                throw ATKError.invalidJSON("ticker 时间戳无效")
            }
            return MarketTicker(instrumentID: instrumentID, last: last, bid: bid, ask: ask, timestamp: timestamp)
        } catch let error as ATKError {
            throw error
        } catch {
            throw ATKError.invalidJSON(error.localizedDescription)
        }
    }

    private static func decimal(_ value: Any?) -> Decimal? {
        if value is Bool { return nil }
        if let text = value as? String {
            let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty else { return nil }
            guard normalized.range(of: #"^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"#, options: .regularExpression) != nil else { return nil }
            guard let value = Decimal(string: normalized, locale: Locale(identifier: "en_US_POSIX")), value.isFinite else { return nil }
            return value
        }
        if let number = value as? NSNumber {
            guard number.doubleValue.isFinite else { return nil }
            return decimal(number.stringValue)
        }
        return nil
    }

    private static func decimalText(_ value: Decimal) -> String { NSDecimalNumber(decimal: value).stringValue }

    private static func requiredText(_ value: Any?) -> String? {
        guard let text = value as? String,
              !text.isEmpty,
              text.trimmingCharacters(in: .whitespacesAndNewlines) == text else { return nil }
        return text
    }

    private static func objectRows(_ root: Any) -> [[String: Any]]? {
        if let rows = root as? [[String: Any]] { return rows }
        if let object = root as? [String: Any], let rows = object["data"] as? [[String: Any]] { return rows }
        return nil
    }

    private static func firstJSONObject(_ root: Any) -> [String: Any]? {
        if let object = root as? [String: Any] {
            if let rows = object["data"] as? [[String: Any]] { return rows.first }
            return object
        }
        return (root as? [[String: Any]])?.first
    }

    private static func date(_ value: Any?) -> Date? {
        if value is Bool { return nil }
        let milliseconds: Double?
        if let text = value as? String {
            milliseconds = Double(text)
        } else if let number = value as? NSNumber {
            milliseconds = number.doubleValue
        } else {
            milliseconds = nil
        }
        guard let milliseconds, milliseconds.isFinite, milliseconds > 0 else { return nil }
        return Date(timeIntervalSince1970: milliseconds / 1000)
    }

    private static func validateInstrumentID(_ value: String) throws {
        try validateIdentifier(value, field: "合约")
    }

    private static func validateIdentifier(_ value: String, field: String) throws {
        guard isSafeIdentifier(value) else {
            throw ATKError.invalidInput("\(field)包含空值、控制字符或非法前缀")
        }
    }

    /// Values are passed to the CLI as separate arguments; rejecting a
    /// leading dash keeps an exchange-supplied value from becoming a flag.
    private static func isSafeIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 100 && !value.hasPrefix("-")
            && value.unicodeScalars.allSatisfy { !$0.properties.isWhitespace && $0.value >= 0x20 && $0.value != 0x7F }
    }

    private static func responseFailure(_ root: Any, defaultMessage: String) -> (code: Int32, message: String)? {
        func stringValue(_ value: Any?) -> String? {
            if let value = value as? String { return value }
            if let value = value as? NSNumber { return value.stringValue }
            return nil
        }
        func codeAndMessage(_ object: [String: Any]) -> (Int32, String)? {
            if let rawCode = stringValue(object["sCode"]), !rawCode.isEmpty, rawCode != "0" {
                return (Int32(rawCode) ?? -1, stringValue(object["sMsg"]) ?? defaultMessage)
            }
            if let rawCode = stringValue(object["code"]), !rawCode.isEmpty, rawCode != "0" {
                return (Int32(rawCode) ?? -1, stringValue(object["msg"]) ?? defaultMessage)
            }
            return nil
        }
        if let object = root as? [String: Any] {
            if let failure = codeAndMessage(object) { return failure }
            if let rows = object["data"] as? [[String: Any]] {
                for row in rows {
                    if let failure = codeAndMessage(row) { return failure }
                }
            }
        } else if let rows = root as? [[String: Any]] {
            for row in rows {
                if let failure = codeAndMessage(row) { return failure }
            }
        }
        return nil
    }
}

private final class ATKProcessCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var completed = false
    private var timer: DispatchSourceTimer?
    private let continuation: CheckedContinuation<ATKCommandResult, Error>

    init(_ continuation: CheckedContinuation<ATKCommandResult, Error>) {
        self.continuation = continuation
    }

    func setTimer(_ timer: DispatchSourceTimer) {
        lock.lock()
        if completed {
            lock.unlock()
            timer.cancel()
        } else {
            self.timer = timer
            lock.unlock()
        }
    }

    func finish(_ result: Result<ATKCommandResult, Error>) {
        lock.lock()
        guard !completed else {
            lock.unlock()
            return
        }
        completed = true
        let timer = self.timer
        self.timer = nil
        lock.unlock()
        timer?.cancel()
        continuation.resume(with: result)
    }
}

public struct LocalATKCommandRunner: ATKCommandRunning {
    public let executableURL: URL
    public let environment: [String: String]?
    public let timeout: Duration

    public init(executableURL: URL? = nil, environment: [String: String]? = nil, timeout: Duration = .seconds(15)) {
        let resolvedExecutable = executableURL ?? Self.discoverExecutable()
        self.executableURL = resolvedExecutable
        self.timeout = timeout
        var inherited = ProcessInfo.processInfo.environment
        inherited.removeValue(forKey: "OKX_API_KEY")
        inherited.removeValue(forKey: "OKX_SECRET_KEY")
        inherited.removeValue(forKey: "OKX_PASSPHRASE")
        if let environment { inherited.merge(environment) { _, new in new } }
        if resolvedExecutable.path != "/usr/bin/env" {
            // Finder-launched apps do not inherit the shell PATH. The ATK CLI
            // is commonly a Node script whose shebang invokes `env node`, so
            // expose the CLI's bin directory to both the script and its child.
            let executableDirectory = resolvedExecutable.deletingLastPathComponent().path
            let currentPath = inherited["PATH"] ?? ""
            let pathEntries = currentPath.split(separator: ":").map(String.init)
            if !pathEntries.contains(executableDirectory) {
                inherited["PATH"] = ([executableDirectory] + pathEntries).joined(separator: ":")
            }
        }
        self.environment = inherited
    }

    private static func discoverExecutable() -> URL {
        let environment = ProcessInfo.processInfo.environment
        let fileManager = FileManager.default
        let home = NSHomeDirectory()
        var candidatePaths = [environment["OKX_CLI_PATH"]]
        candidatePaths += (environment["PATH"] ?? "")
            .split(separator: ":")
            .map { String($0) + "/okx" }
        candidatePaths += [
            "/opt/homebrew/bin/okx",
            "/usr/local/bin/okx",
            "\(home)/.npm-global/bin/okx",
            "\(home)/.local/bin/okx"
        ]
        // Node version managers often install npm global binaries below a
        // versioned directory that is only present in an interactive shell's
        // PATH (for example ~/.local/node-v24.*/bin/okx).
        let localNodeRoot = URL(fileURLWithPath: home).appendingPathComponent(".local", isDirectory: true)
        if let entries = try? fileManager.contentsOfDirectory(at: localNodeRoot, includingPropertiesForKeys: nil) {
            candidatePaths += entries
                .filter { $0.lastPathComponent.hasPrefix("node-") }
                .sorted { $0.lastPathComponent > $1.lastPathComponent }
                .map { $0.appendingPathComponent("bin/okx").path }
        }
        let candidates = candidatePaths.compactMap { $0 }.map(URL.init(fileURLWithPath:))
        if let executable = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0.path) }) {
            return executable
        }
        return URL(fileURLWithPath: "/usr/bin/env")
    }

    public func run(arguments: [String]) async throws -> ATKCommandResult {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            // Ticker lists can exceed the OS pipe buffer. Writing them to temporary
            // files prevents the CLI child from blocking while the parent waits.
            let outputURL = FileManager.default.temporaryDirectory.appendingPathComponent("nova-atk-\(UUID().uuidString).out")
            let errorURL = FileManager.default.temporaryDirectory.appendingPathComponent("nova-atk-\(UUID().uuidString).err")
            FileManager.default.createFile(atPath: outputURL.path, contents: nil)
            FileManager.default.createFile(atPath: errorURL.path, contents: nil)
            guard let stdout = try? FileHandle(forWritingTo: outputURL), let stderr = try? FileHandle(forWritingTo: errorURL) else {
                try? FileManager.default.removeItem(at: outputURL)
                try? FileManager.default.removeItem(at: errorURL)
                continuation.resume(throwing: ATKError.unavailable("无法创建 ATK 输出缓冲区"))
                return
            }
            let completion = ATKProcessCompletion(continuation)

            process.executableURL = executableURL
            process.arguments = executableURL.path == "/usr/bin/env" ? ["okx"] + arguments : arguments
            process.standardOutput = stdout
            process.standardError = stderr
            if let environment { process.environment = environment }
            process.terminationHandler = { process in
                try? stdout.close()
                try? stderr.close()
                let output = String(data: (try? Data(contentsOf: outputURL)) ?? Data(), encoding: .utf8) ?? ""
                let error = String(data: (try? Data(contentsOf: errorURL)) ?? Data(), encoding: .utf8) ?? ""
                try? FileManager.default.removeItem(at: outputURL)
                try? FileManager.default.removeItem(at: errorURL)
                completion.finish(.success(ATKCommandResult(stdout: output, stderr: error, exitCode: process.terminationStatus)))
            }
            do {
                try process.run()
                let timer = DispatchSource.makeTimerSource(queue: .global(qos: .userInitiated))
                let timeoutInterval = Double(timeout.components.seconds) + Double(timeout.components.attoseconds) / 1_000_000_000_000_000_000
                timer.schedule(deadline: .now() + timeoutInterval)
                timer.setEventHandler {
                    if process.isRunning {
                        process.terminate()
                        completion.finish(.failure(ATKError.timedOut))
                        // A CLI can ignore SIGTERM (or leave a child process
                        // behind). Force-stop the direct child so callers
                        // never wait forever after the timeout has fired.
                        let pid = process.processIdentifier
                        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 1) {
                            if process.isRunning { _ = kill(pid, SIGKILL) }
                        }
                    }
                }
                completion.setTimer(timer)
                timer.resume()
            } catch {
                try? stdout.close()
                try? stderr.close()
                try? FileManager.default.removeItem(at: outputURL)
                try? FileManager.default.removeItem(at: errorURL)
                completion.finish(.failure(ATKError.unavailable("找不到 okx CLI，请安装 @okx_ai/okx-trade-cli")))
            }
        }
    }
}
