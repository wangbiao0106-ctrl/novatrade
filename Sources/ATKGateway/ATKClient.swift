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
    case notAuthenticated
    case apiKeyConfigured(profile: String)
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
        case .notAuthenticated: return "ATK 尚未完成 OAuth 登录，请先运行 okx auth login"
        case let .apiKeyConfigured(profile): return "ATK 检测到 API Key profile（\(profile)），OAuth 登录被跳过"
        case .apiKeyNotConfigured: return "ATK 未检测到 API Key profile，请先运行 okx config init"
        case .timedOut: return "ATK 命令超时，已终止进程"
        case let .demoProfile(profile): return "当前 OKX profile（" + profile + "）是模拟盘，已拒绝真实下单"
        case .liveTradingDisabled: return "实盘交易尚未在本地服务中启用"
        case let .invalidOrder(message): return "实盘订单无效：" + message
        }
    }
}

public struct ATKAuthStatus: Codable, Equatable, Sendable {
    public let profile: String?
    public let site: String?
    public let status: String
    public let scopes: [String]?
    public let apiKey: Bool?

    public var isLoggedIn: Bool { status == "logged_in" }

    public init(profile: String? = nil, site: String? = nil, status: String, scopes: [String]? = nil, apiKey: Bool? = nil) {
        self.profile = profile
        self.site = site
        self.status = status
        self.scopes = scopes
        self.apiKey = apiKey
    }

    enum CodingKeys: String, CodingKey {
        case profile, site, status, scopes
        case apiKey
    }
}

public struct ATKLoginChallenge: Codable, Equatable, Sendable {
    public let verificationURI: URL
    public let userCode: String
    public let expiresIn: Int

    enum CodingKeys: String, CodingKey {
        case verificationURI = "verificationUri"
        case userCode
        case expiresIn
    }
}

public struct ATKProfileSummary: Codable, Equatable, Sendable, Identifiable {
    public let id: String
    public let site: String?
    public let hasAPIKey: Bool
    public let demo: Bool

    public init(id: String, site: String? = nil, hasAPIKey: Bool = false, demo: Bool = false) {
        self.id = id
        self.site = site
        self.hasAPIKey = hasAPIKey
        self.demo = demo
    }
}

public struct ATKConfigSummary: Codable, Equatable, Sendable {
    public let defaultProfile: String?
    public let profiles: [ATKProfileSummary]

    public var hasAPIKeyProfile: Bool { profiles.contains(where: \.hasAPIKey) }

    public init(defaultProfile: String? = nil, profiles: [ATKProfileSummary] = []) {
        self.defaultProfile = defaultProfile
        self.profiles = profiles
    }
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

public struct ATKClient: Sendable {
    private let runner: any ATKCommandRunning
    private let decoder: JSONDecoder

    public init(runner: any ATKCommandRunning = LocalATKCommandRunner()) {
        self.runner = runner
        self.decoder = JSONDecoder()
    }

    public func authStatus() async throws -> ATKAuthStatus {
        let result = try await runner.run(arguments: ["auth", "status", "--json"])
        do {
            var status = try decoder.decode(ATKAuthStatus.self, from: Data(result.stdout.utf8))
            if status.status == "not_logged_in" {
                status = ATKAuthStatus(profile: status.profile, site: nil, status: status.status, scopes: status.scopes, apiKey: status.apiKey)
            }
            return status
        } catch {
            guard result.exitCode == 0 else {
                let message = result.stderr.isEmpty ? result.stdout : result.stderr
                throw ATKError.commandFailed(code: result.exitCode, message: message.trimmingCharacters(in: .whitespacesAndNewlines))
            }
            throw ATKError.invalidJSON(error.localizedDescription)
        }
    }

    public func authLoginManual(site: String) async throws -> ATKLoginChallenge {
        let result = try await runner.run(arguments: ["auth", "login", "--manual", "--site", site])
        if let data = result.stdout.data(using: .utf8),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           object["reason"] as? String == "api_key_configured" {
            throw ATKError.apiKeyConfigured(profile: object["profile"] as? String ?? "unknown")
        }
        guard result.exitCode == 0 else {
            let message = result.stderr.isEmpty ? result.stdout : result.stderr
            throw ATKError.commandFailed(code: result.exitCode, message: message.trimmingCharacters(in: .whitespacesAndNewlines))
        }
        do {
            return try decoder.decode(ATKLoginChallenge.self, from: Data(result.stdout.utf8))
        } catch {
            throw ATKError.invalidJSON(error.localizedDescription)
        }
    }

    /// Returns only profile names, sites, and whether an API key exists. Secrets are never decoded or retained.
    public func configSummary() async throws -> ATKConfigSummary {
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

    public func requireAPIKeyProfile() async throws -> ATKConfigSummary {
        let summary = try await configSummary()
        // All authenticated commands use the CLI's default profile. An API
        // key in a different profile cannot authorize those commands and must
        // not make this preflight check report a false positive.
        guard let defaultProfile = summary.defaultProfile,
              summary.profiles.first(where: { $0.id == defaultProfile })?.hasAPIKey == true else {
            throw ATKError.apiKeyNotConfigured
        }
        return summary
    }

    public func marketTicker(instrumentID: String) async throws -> MarketTicker {
        let result = try await run(["market", "ticker", instrumentID, "--json"])
        return try decodeTicker(result.stdout, fallbackInstrumentID: instrumentID)
    }

    public func marketCandles(instrumentID: String, interval: KlineInterval, limit: Int = 300) async throws -> [Candle] {
        let root = try await runJSON(["market", "candles", instrumentID, "--bar", interval.okxBar, "--limit", String(min(max(limit, 1), 300))])
        guard let rows = root as? [[Any]] else { throw ATKError.invalidJSON("K 线数据格式无效") }
        return rows.compactMap(Self.decodeCandle).sorted { $0.timestamp < $1.timestamp }
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
            let open = Self.decimal(row["open24h"]) ?? last
            let change = open == 0 ? 0 : (last - open) / open * 100
            let volume = Self.decimal(row["volCcy24h"]) ?? Self.decimal(row["vol24h"]) ?? 0
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

    public func accountOverview() async throws -> AccountOverview {
        let profileSummary = try await configSummary()
        let profile = profileSummary.defaultProfile.flatMap { name in profileSummary.profiles.first(where: { $0.id == name }) }
        guard let profile, profile.hasAPIKey else { throw ATKError.apiKeyNotConfigured }
        let balance = try await runJSON(["account", "balance-all", "--valuationCcy", "USD"])
        let config = try await runJSON(["account", "config"])
        let positions = try await runJSON(["account", "positions", "--instType", "SWAP"])
        let bills = (try? await runJSON(["account", "bills", "--instType", "SWAP", "--limit", "100"])) ?? []
        let trading = (balance as? [String: Any])?["trading"] as? [String: Any] ?? [:]
        let valuation = (balance as? [String: Any])?["valuation"] as? [String: Any]
        let totalEq = Self.decimal(valuation?["totalBal"]) ?? Self.decimal(trading["totalEq"])
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
        let accountPositions = Self.objectRows(positions).compactMap { row -> PositionSnapshot? in
            guard let instrument = row["instId"] as? String, let quantity = Self.decimal(row["pos"]), quantity != 0 else { return nil }
            return PositionSnapshot(id: row["posId"] as? String ?? UUID().uuidString, instrumentID: instrument, side: row["posSide"] as? String ?? "net", quantity: quantity, entryPrice: Self.decimal(row["avgPx"]) ?? 0, markPrice: Self.decimal(row["markPx"]), unrealizedPnL: Self.decimal(row["upl"]))
        }
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

    private enum SwapOrderMode { case live, demo }

    private func placeSwapOrder(_ request: LiveOrderRequest, mode: SwapOrderMode) async throws -> LiveOrderCommandResult {
        guard !request.instrumentID.isEmpty else { throw ATKError.invalidOrder("合约不能为空") }
        guard ["buy", "sell"].contains(request.side.lowercased()) else { throw ATKError.invalidOrder("方向必须是 buy 或 sell") }
        guard ["market", "limit"].contains(request.orderType.lowercased()) else { throw ATKError.invalidOrder("只支持 market 或 limit") }
        guard request.quantity > 0 else { throw ATKError.invalidOrder("数量必须大于 0") }
        if request.orderType.lowercased() == "limit", (request.price ?? 0) <= 0 { throw ATKError.invalidOrder("限价单必须提供有效价格") }

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

        var arguments = [mode == .live ? "--live" : "--demo", "swap", "place", "--instId", request.instrumentID, "--side", request.side.lowercased(), "--ordType", request.orderType.lowercased(), "--sz", Self.decimalText(request.quantity), "--tdMode", request.marginMode]
        if let positionSide = request.positionSide, !positionSide.isEmpty { arguments += ["--posSide", positionSide] }
        if request.reduceOnly { arguments.append("--reduceOnly") }
        if let price = request.price { arguments += ["--px", Self.decimalText(price)] }
        let root = try await runJSON(arguments)
        let row = Self.firstJSONObject(root) ?? [:]
        let code = row["sCode"] as? String ?? row["code"] as? String
        if let code, !code.isEmpty, code != "0" {
            throw ATKError.commandFailed(code: Int32(code) ?? -1, message: (row["sMsg"] as? String) ?? (row["msg"] as? String) ?? "OKX 拒绝订单")
        }
        let orderID = (row["ordId"] as? String) ?? (row["orderId"] as? String)
        guard let orderID, !orderID.isEmpty else { throw ATKError.invalidJSON("下单响应缺少 ordId") }
        return LiveOrderCommandResult(orderID: orderID, clientOrderID: row["clOrdId"] as? String ?? row["clientOrderId"] as? String, code: code, message: row["sMsg"] as? String ?? row["msg"] as? String)
    }

    public func swapPositions() async throws -> [PositionSnapshot] {
        let root = try await runJSON(["swap", "positions"])
        return Self.objectRows(root).compactMap { row in
            guard let instrument = row["instId"] as? String, let quantity = Self.decimal(row["pos"]), quantity != 0 else { return nil }
            return PositionSnapshot(id: row["posId"] as? String ?? UUID().uuidString, instrumentID: instrument, side: row["posSide"] as? String ?? "net", quantity: quantity, entryPrice: Self.decimal(row["avgPx"]) ?? 0, markPrice: Self.decimal(row["markPx"]), unrealizedPnL: Self.decimal(row["upl"]))
        }
    }

    public func swapOrders() async throws -> [OrderSnapshot] {
        let root = try await runJSON(["swap", "orders"])
        return Self.objectRows(root).compactMap { row in
            guard let instrument = row["instId"] as? String, let quantity = Self.decimal(row["sz"]) else { return nil }
            let created = Self.date(row["cTime"] ?? row["uTime"]) ?? .now
            return OrderSnapshot(id: row["ordId"] as? String ?? UUID().uuidString, instrumentID: instrument, side: row["side"] as? String ?? "", status: row["state"] as? String ?? "", quantity: quantity, price: Self.decimal(row["px"]), createdAt: created)
        }
    }

    private static func todayPnL(from root: Any, now: Date) -> Decimal {
        let rows: [[String: Any] ]
        if let array = root as? [[String: Any]] { rows = array }
        else if let object = root as? [String: Any], let data = object["data"] as? [[String: Any]] { rows = data }
        else { return 0 }
        let calendar = Calendar.current
        return rows.reduce(Decimal.zero) { total, row in
            let timestamp: TimeInterval?
            if let value = row["ts"] as? String { timestamp = Double(value).map { $0 / 1000 } }
            else if let value = row["ts"] as? NSNumber { timestamp = value.doubleValue / 1000 }
            else { timestamp = nil }
            guard let timestamp, calendar.isDate(Date(timeIntervalSince1970: timestamp), inSameDayAs: now), let pnl = decimal(row["pnl"]) else { return total }
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
        do { return try JSONSerialization.jsonObject(with: data) }
        catch { throw ATKError.invalidJSON(error.localizedDescription) }
    }

    private static func decodeCandle(_ row: [Any]) -> Candle? {
        guard row.count >= 6,
              let milliseconds = decimal(row[0]).map({ NSDecimalNumber(decimal: $0).doubleValue }),
              let open = decimal(row[1]), let high = decimal(row[2]), let low = decimal(row[3]), let close = decimal(row[4]), let volume = decimal(row[5]) else { return nil }
        let confirmed = row.count < 9 || (row[8] as? String) != "0"
        guard milliseconds.isFinite, milliseconds > 0 else { return nil }
        return Candle(timestamp: Date(timeIntervalSince1970: milliseconds / 1000), open: open, high: high, low: low, close: close, volume: volume, confirmed: confirmed)
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
            guard let lastText = row["last"] as? String,
                  let last = Decimal(string: lastText, locale: Locale(identifier: "en_US_POSIX")) else {
                throw ATKError.invalidJSON("ticker 缺少有效的 last")
            }
            let bid = Self.decimal(row["bidPx"] ?? row["bid"])
            let ask = Self.decimal(row["askPx"] ?? row["ask"])
            let timestamp = (row["ts"] as? String).flatMap(Double.init).flatMap { value in
                value.isFinite && value > 0 ? Date(timeIntervalSince1970: value / 1000) : nil
            } ?? .now
            return MarketTicker(instrumentID: instrumentID, last: last, bid: bid, ask: ask, timestamp: timestamp)
        } catch let error as ATKError {
            throw error
        } catch {
            throw ATKError.invalidJSON(error.localizedDescription)
        }
    }

    private static func decimal(_ value: Any?) -> Decimal? {
        if let text = value as? String {
            let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !normalized.isEmpty else { return nil }
            return Decimal(string: normalized, locale: Locale(identifier: "en_US_POSIX"))
        }
        if let number = value as? NSNumber { return Decimal(string: number.stringValue, locale: Locale(identifier: "en_US_POSIX")) }
        return nil
    }

    private static func decimalText(_ value: Decimal) -> String { NSDecimalNumber(decimal: value).stringValue }

    private static func objectRows(_ root: Any) -> [[String: Any]] {
        if let rows = root as? [[String: Any]] { return rows }
        if let object = root as? [String: Any], let rows = object["data"] as? [[String: Any]] { return rows }
        return []
    }

    private static func firstJSONObject(_ root: Any) -> [String: Any]? {
        if let object = root as? [String: Any] {
            if let rows = object["data"] as? [[String: Any]] { return rows.first }
            return object
        }
        return (root as? [[String: Any]])?.first
    }

    private static func date(_ value: Any?) -> Date? {
        guard let text = value as? String, let milliseconds = Double(text), milliseconds.isFinite, milliseconds > 0 else { return nil }
        return Date(timeIntervalSince1970: milliseconds / 1000)
    }
}

#if os(macOS)
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
        self.executableURL = executableURL ?? Self.discoverExecutable()
        self.timeout = timeout
        var inherited = ProcessInfo.processInfo.environment
        inherited.removeValue(forKey: "OKX_API_KEY")
        inherited.removeValue(forKey: "OKX_SECRET_KEY")
        inherited.removeValue(forKey: "OKX_PASSPHRASE")
        if let environment { inherited.merge(environment) { _, new in new } }
        self.environment = inherited
    }

    private static func discoverExecutable() -> URL {
        let environment = ProcessInfo.processInfo.environment
        let candidates = [
            environment["OKX_CLI_PATH"],
            "/opt/homebrew/bin/okx",
            "/usr/local/bin/okx",
            "\(NSHomeDirectory())/.npm-global/bin/okx"
        ].compactMap { $0 }.map(URL.init(fileURLWithPath:))
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
#else
public struct LocalATKCommandRunner: ATKCommandRunning {
    public init(executableURL: URL? = nil, environment: [String: String]? = nil) {}

    public func run(arguments: [String]) async throws -> ATKCommandResult {
        throw ATKError.unavailable("ATK CLI 仅在本地 Mac 交易服务上运行")
    }
}
#endif
