import Foundation
import TradingDomain
#if os(macOS)
import Darwin
#endif

public enum TradingServiceClientError: LocalizedError, Sendable {
    case invalidResponse
    case server(status: Int)
    case disconnected

    public var errorDescription: String? {
        switch self {
        case .invalidResponse: return "本地交易服务返回了无法识别的响应"
        case let .server(status): return "本地交易服务错误（HTTP \(status)）"
        case .disconnected: return "本地交易服务连接已断开"
        }
    }
}

public struct ServiceStreamEvent: Codable, Equatable, Sendable {
    public let type: String
    public let timestamp: Date?
    public let instrumentID: String?
    public let payload: String?

    public init(type: String, timestamp: Date? = nil, instrumentID: String? = nil, payload: String? = nil) {
        self.type = type; self.timestamp = timestamp; self.instrumentID = instrumentID; self.payload = payload
    }
}

public struct PaperLedgerSnapshot: Codable, Sendable {
    public let orders: [PaperOrder]
    public let fills: [PaperFill]
}

public actor TradingServiceClient {
    public let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(baseURL: URL = URL(string: "http://127.0.0.1:8787")!, session: URLSession = .shared) {
        self.baseURL = baseURL; self.session = session; self.decoder = JSONDecoder(); self.encoder = JSONEncoder()
        decoder.dateDecodingStrategy = .iso8601
        encoder.dateEncodingStrategy = .iso8601
    }

    public func health() async throws -> ServiceHealth { try await get(path: "/health") }
    /// Loads the complete swap ticker list. A fresh read is used by the
    /// sidebar poller so contracts that are not currently selected do not sit
    /// behind the backend's longer contracts cache window.
    public func contracts(forceRefresh: Bool = false) async throws -> [ContractMarket] {
        try await get(path: "/api/v1/contracts", query: forceRefresh ? ["fresh": "true"] : [:])
    }
    public func account() async throws -> AccountOverview { try await get(path: "/api/v1/account") }
    public func liveTradingStatus() async throws -> LiveTradingStatus { try await get(path: "/api/v1/live/trading-status") }
    public func enableLiveTrading() async throws -> LiveTradingStatus { try await request(path: "/api/v1/live/trading/enable", method: "POST") }
    public func disableLiveTrading() async throws -> LiveTradingStatus { try await request(path: "/api/v1/live/trading/disable", method: "POST") }
    public func placeLiveOrder(_ order: LiveOrderRequest) async throws -> LiveOrderResult { try await post("/api/v1/live/orders", body: order) }
    public func privatePositions() async throws -> [PositionSnapshot] { try await get(path: "/api/v1/positions") }
    public func privateOrders() async throws -> [OrderSnapshot] { try await get(path: "/api/v1/orders") }
    public func market(instrumentID: String, interval: KlineInterval) async throws -> MarketSnapshot {
        try await get(path: "/api/v1/market/candles", query: ["instId": instrumentID, "bar": interval.rawValue])
    }
    public func ticker(instrumentID: String) async throws -> MarketTicker { try await get(path: "/api/v1/market/ticker", query: ["instId": instrumentID]) }
    public func orderBook(instrumentID: String) async throws -> OrderBookSnapshot { try await get(path: "/api/v1/market/orderbook", query: ["instId": instrumentID]) }
    public func trades(instrumentID: String) async throws -> [TradeTick] { try await get(path: "/api/v1/market/trades", query: ["instId": instrumentID]) }
    public func paperLedger() async throws -> PaperLedgerSnapshot { try await get(path: "/api/v1/paper/ledger") }
    public func placePaperOrder(_ order: PaperOrderRequest) async throws -> PaperOrder { try await post("/api/v1/paper/orders", body: order) }
    public func paperPositions() async throws -> [PaperPosition] { try await get(path: "/api/v1/paper/positions") }
    public func paperOrders() async throws -> [PaperOrder] { try await get(path: "/api/v1/paper/orders") }
    public func paperFills() async throws -> [PaperFill] { try await get(path: "/api/v1/paper/fills") }
    public func runtimeLogs() async throws -> [RuntimeLog] { try await get(path: "/api/v1/logs") }
    public func appendLog(_ log: RuntimeLog) async throws -> RuntimeLog { try await post("/api/v1/logs", body: log) }
    public func strategies() async throws -> [StrategyConfig] { try await get(path: "/api/v1/strategies") }
    public func strategyStatuses() async throws -> [StrategyStatus] { try await get(path: "/api/v1/strategies/status") }
    public func risk() async throws -> RiskSnapshot { try await get(path: "/api/v1/risk") }
    public func createStrategy(_ config: StrategyConfig) async throws -> StrategyConfig { try await post("/api/v1/strategies", body: config) }
    public func updateStrategy(_ config: StrategyConfig) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(config.id.uuidString)", method: "PATCH", body: config) }
    public func deleteStrategy(_ id: UUID) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(id.uuidString)", method: "DELETE") }
    public func startStrategy(_ id: UUID) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(id.uuidString)/start", method: "POST") }
    public func pauseStrategy(_ id: UUID) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(id.uuidString)/pause", method: "POST") }

    public func stream(instrumentID: String = "BTC-USDT-SWAP", interval: KlineInterval = .oneHour) -> AsyncThrowingStream<ServiceStreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var delay: UInt64 = 500_000_000
                while !Task.isCancelled {
                    var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
                    components.scheme = baseURL.scheme == "https" ? "wss" : "ws"
                    components.path = "/api/v1/stream"
                    var request = URLRequest(url: components.url!)
                    request.timeoutInterval = 15
                    let socket = session.webSocketTask(with: request)
                    defer { socket.cancel(with: .goingAway, reason: nil) }
                    do {
                        continuation.yield(ServiceStreamEvent(type: "connection", timestamp: .now, instrumentID: instrumentID, payload: "local_connecting"))
                        socket.resume()
                        try await withTaskCancellationHandler {
                        let subscription: [String: Any] = ["type": "subscribe", "instrumentID": instrumentID, "interval": interval.rawValue, "channels": ["ticker", "candle", "strategy", "risk", "account", "log"]]
                        let body = try JSONSerialization.data(withJSONObject: subscription)
                        try await socket.send(.string(String(decoding: body, as: UTF8.self)))
                        delay = 500_000_000
                        while !Task.isCancelled {
                            let message = try await socket.receive()
                            guard !Task.isCancelled else { break }
                            guard case let .string(value) = message else { continue }
                            if let data = value.data(using: .utf8) {
                                continuation.yield((try? decoder.decode(ServiceStreamEvent.self, from: data)) ?? ServiceStreamEvent(type: "raw", payload: value))
                            }
                        }
                        } onCancel: {
                            socket.cancel(with: .goingAway, reason: nil)
                        }
                    } catch {
                        if Task.isCancelled { break }
                        socket.cancel(with: .goingAway, reason: nil)
                        continuation.yield(ServiceStreamEvent(type: "connection", timestamp: .now, instrumentID: instrumentID, payload: "local_reconnecting"))
                        try? await Task.sleep(nanoseconds: delay)
                        delay = min(delay * 2, 8_000_000_000)
                    }
                }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func get<T: Decodable>(path: String, query: [String: String] = [:]) async throws -> T {
        try await request(path: path, method: "GET", body: nil, query: query)
    }

    private func request<T: Decodable>(path: String, method: String, body: (any Encodable)? = nil, query: [String: String] = [:]) async throws -> T {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.path = path
        components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        if let body { request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try encoder.encode(body) }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw TradingServiceClientError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else { throw TradingServiceClientError.server(status: http.statusCode) }
        return try decoder.decode(T.self, from: data)
    }

    private func post<T: Encodable, R: Decodable>(_ path: String, body: T) async throws -> R {
        try await request(path: path, method: "POST", body: body)
    }
}

#if os(macOS)
public actor LocalServiceProcess {
    private let executableURL: URL
    private var detachedPID: Int32?

    public init(executableURL: URL? = nil) {
        if let executableURL { self.executableURL = executableURL }
        else if let configured = ProcessInfo.processInfo.environment["OKX_LOCALD_PATH"] { self.executableURL = URL(fileURLWithPath: configured) }
        else {
            let currentDirectory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            let executableDirectory = Bundle.main.executableURL?.deletingLastPathComponent()
            let sourcePackageRoot = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent() // MacTraderApp
                .deletingLastPathComponent() // Sources
                .deletingLastPathComponent() // package root
            let candidates = [
                currentDirectory.appendingPathComponent(".build/debug/okx-locald"),
                currentDirectory.appendingPathComponent(".build/out/Products/Debug/okx-locald"),
                currentDirectory.appendingPathComponent(".build/arm64-apple-macosx/debug/okx-locald"),
                executableDirectory?.appendingPathComponent("okx-locald"),
                sourcePackageRoot.appendingPathComponent(".build/debug/okx-locald"),
                sourcePackageRoot.appendingPathComponent(".build/out/Products/Debug/okx-locald"),
                URL(fileURLWithPath: "/usr/local/bin/okx-locald")
            ].compactMap { $0 }
            self.executableURL = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0.path) }) ?? candidates[0]
        }
    }

    public func start() {
        guard !isRunning() else { return }
        let launcher = Process()
        launcher.executableURL = URL(fileURLWithPath: "/bin/sh")
        let quotedPath = "'" + executableURL.path.replacingOccurrences(of: "'", with: "'\\''") + "'"
        launcher.arguments = ["-c", "nohup \(quotedPath) >/dev/null 2>&1 </dev/null & printf '%s' $!"]
        var environment = ProcessInfo.processInfo.environment
        environment["OKX_LOCALD_PORT"] = "8787"
        launcher.environment = environment
        let output = Pipe()
        launcher.standardOutput = output
        launcher.standardError = try? FileHandle(forWritingTo: URL(fileURLWithPath: "/dev/null"))
        do {
            try launcher.run()
            launcher.waitUntilExit()
            let pidText = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
            detachedPID = pidText.flatMap(Int32.init)
        } catch {
            detachedPID = nil
        }
    }

    public func isRunning() -> Bool {
        if detachedPID == nil { detachedPID = discoverPID() }
        guard let pid = detachedPID, pid > 0 else { return false }
        return kill(pid, 0) == 0
    }

    public func stop() {
        let pid = detachedPID ?? discoverPID()
        if let pid, pid > 0 { _ = kill(pid, SIGTERM) }
        detachedPID = nil
    }

    private func discoverPID() -> Int32? {
        let lookup = Process()
        lookup.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        lookup.arguments = ["-f", executableURL.path]
        let output = Pipe()
        lookup.standardOutput = output
        lookup.standardError = try? FileHandle(forWritingTo: URL(fileURLWithPath: "/dev/null"))
        do {
            try lookup.run()
            lookup.waitUntilExit()
            let values = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                .split(whereSeparator: { $0.isNewline })
                .compactMap { Int32($0) }
            return values?.first
        } catch {
            return nil
        }
    }
}
#endif
