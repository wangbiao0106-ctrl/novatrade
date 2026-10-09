import Foundation
import TradingDomain

public enum TradingServiceClientError: LocalizedError, Sendable {
    case invalidResponse
    case server(status: Int)
    case disconnected
    case timedOut
    /// Flattening first waits for any in-flight AI evaluation to stop and can
    /// then perform several private exchange requests. It has a separate
    /// message so a slow emergency action is not reported as a model timeout.
    case flattenTimedOut

    public var errorDescription: String? {
        switch self {
        case .invalidResponse: return "本地交易服务返回了无法识别的响应"
        case let .server(status): return "本地交易服务错误（HTTP \(status)）"
        case .disconnected: return "本地交易服务连接已断开"
        case .timedOut: return "AI 响应超时，请缩小标的池或稍后重试"
        case .flattenTimedOut: return "平仓请求超时，后台可能仍在处理，请刷新持仓状态"
        }
    }
}

/// REST + websocket client for the loopback FastAPI backend.
public actor TradingServiceClient {
    public let baseURL: URL
    private let session: URLSession
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(baseURL: URL = LocalService.defaultBaseURL, session: URLSession = .shared) {
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
    public func placeLiveOrder(_ order: LiveOrderRequest) async throws -> LiveOrderResult { try await post("/api/v1/live/orders", body: order) }
    public func privatePositions() async throws -> [PositionSnapshot] { try await get(path: "/api/v1/positions") }
    public func privateOrders() async throws -> [OrderSnapshot] { try await get(path: "/api/v1/orders") }
    public func market(instrumentID: String, interval: KlineInterval) async throws -> MarketSnapshot {
        try await get(path: "/api/v1/market/candles", query: ["instId": instrumentID, "bar": interval.chartExchangeBar])
    }
    public func placePaperOrder(_ order: PaperOrderRequest) async throws -> PaperOrder { try await post("/api/v1/paper/orders", body: order) }
    public func paperOrders() async throws -> [PaperOrder] { try await get(path: "/api/v1/paper/orders") }
    public func paperFills() async throws -> [PaperFill] { try await get(path: "/api/v1/paper/fills") }
    public func runtimeLogs() async throws -> [RuntimeLog] { try await get(path: "/api/v1/logs") }
    public func appendLog(_ log: RuntimeLog) async throws -> RuntimeLog { try await post("/api/v1/logs", body: log) }
    public func strategies() async throws -> [StrategyConfig] { try await get(path: "/api/v1/strategies") }
    public func strategyStatuses() async throws -> [StrategyStatus] { try await get(path: "/api/v1/strategies/status") }
    public func strategyCapital() async throws -> [StrategyCapitalSnapshot] { try await get(path: "/api/v1/strategies/capital") }
    /// The concrete instrument set each strategy is scanning right now, as
    /// resolved by the daemon's own universe cache.
    public func strategyTargets() async throws -> [StrategyUniverseSnapshot] { try await get(path: "/api/v1/strategies/targets") }
    public func risk() async throws -> RiskSnapshot { try await get(path: "/api/v1/risk") }
    public func resetRisk() async throws -> RiskSnapshot { try await request(path: "/api/v1/risk/reset", method: "POST") }
    // MARK: - AI decision worker

    public func aiStatus() async throws -> AIStatus { try await get(path: "/api/v1/ai/status") }
    public func aiConfig() async throws -> AIConfig { try await get(path: "/api/v1/ai/config") }
    public func updateAIConfig(_ patch: AIPatch) async throws -> AIConfig {
        try await request(path: "/api/v1/ai/config", method: "PATCH", body: patch)
    }
    public func enableAI() async throws -> AIStatus {
        try await request(path: "/api/v1/ai/enable", method: "POST")
    }
    public func disableAI() async throws -> AIStatus {
        try await request(path: "/api/v1/ai/disable", method: "POST")
    }
    /// Requests the server-side emergency action.  The gateway owns the
    /// actual cancellation and reduce-only flattening; the client only
    /// receives the resulting worker status.
    public func flattenAI() async throws -> AIFlattenResult {
        do {
            // The service stops an in-flight model evaluation before it starts
            // cancelling orders and submitting reduce-only exits. A normal
            // 15-second REST deadline can expire while that safe stop is still
            // in progress, even though the request itself is healthy.
            return try await request(path: "/api/v1/ai/flatten", method: "POST", timeout: 120)
        } catch TradingServiceClientError.timedOut {
            throw TradingServiceClientError.flattenTimedOut
        }
    }
    /// Sends a conversational request to the AI control plane. The server may
    /// return a configuration patch for review; only an explicit apply request
    /// can persist it, and this endpoint never submits an order.
    public func chatAI(message: String, apply: Bool = false, suggestion: AIPatch? = nil) async throws -> AIChatResponse {
        // Model-backed conversations can take longer than the local REST
        // calls, especially when the worker is assembling a fresh market
        // snapshot. Keep the longer bound local to chat requests so a stalled
        // daemon still cannot block the rest of the UI indefinitely.
        try await post(
            "/api/v1/ai/chat",
            body: AIChatRequest(message: message, apply: apply, suggestion: suggestion),
            timeout: 120
        )
    }
    /// The endpoint returns the append-only decision event rows, including
    /// rejected decisions and worker errors, rather than bare decisions.
    public func aiDecisions() async throws -> [AIAuditRecord] {
        try await get(path: "/api/v1/ai/decisions")
    }
    public func aiAudit() async throws -> [AIAuditRecord] {
        try await get(path: "/api/v1/ai/audit")
    }

    public func createStrategy(_ config: StrategyConfig) async throws -> StrategyConfig { try await post("/api/v1/strategies", body: config) }
    public func deleteStrategy(_ id: UUID) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(id.uuidString)", method: "DELETE") }
    public func startStrategy(_ id: UUID) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(id.uuidString)/start", method: "POST") }
    public func pauseStrategy(_ id: UUID) async throws -> StrategyConfig { try await request(path: "/api/v1/strategies/\(id.uuidString)/pause", method: "POST") }

    /// Subscribes to the chart's candles plus the account-wide strategy,
    /// risk, account and log pushes. Reconnects with exponential backoff and
    /// reports each attempt as a `connection` event.
    public func stream(instrumentID: String, interval: KlineInterval) -> AsyncThrowingStream<StreamEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                var delay: UInt64 = 500_000_000
                while !Task.isCancelled {
                    var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
                    components.scheme = "ws"
                    components.path = "/api/v1/stream"
                    var request = URLRequest(url: components.url!)
                    request.timeoutInterval = 15
                    request.setValue("Bearer \(LocalService.tokenFromEnvironmentOrFile())", forHTTPHeaderField: "Authorization")
                    let socket = session.webSocketTask(with: request)
                    defer { socket.cancel(with: .goingAway, reason: nil) }
                    do {
                        continuation.yield(StreamEvent(type: "connection", timestamp: .now, instrumentID: instrumentID, payload: "local_connecting"))
                        socket.resume()
                        try await withTaskCancellationHandler {
                            let subscription = StreamSubscription(
                                channels: ["candle", "strategy", "risk", "account", "log"],
                                instrumentID: instrumentID,
                                interval: interval
                            )
                            try await socket.send(.string(String(decoding: try encoder.encode(subscription), as: UTF8.self)))
                            delay = 500_000_000
                            while !Task.isCancelled {
                                let message = try await socket.receive()
                                guard !Task.isCancelled else { break }
                                guard case let .string(value) = message,
                                      let event = try? decoder.decode(StreamEvent.self, from: Data(value.utf8)) else { continue }
                                continuation.yield(event)
                            }
                        } onCancel: {
                            socket.cancel(with: .goingAway, reason: nil)
                        }
                    } catch {
                        if Task.isCancelled { break }
                        socket.cancel(with: .goingAway, reason: nil)
                        continuation.yield(StreamEvent(type: "connection", timestamp: .now, instrumentID: instrumentID, payload: "local_reconnecting"))
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

    private func request<T: Decodable>(
        path: String,
        method: String,
        body: (any Encodable)? = nil,
        query: [String: String] = [:],
        timeout: TimeInterval = 15
    ) async throws -> T {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.path = path
        components.queryItems = query.isEmpty ? nil : query.map { URLQueryItem(name: $0.key, value: $0.value) }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        // Every REST call is bounded so a stalled local daemon cannot block
        // the UI actor indefinitely.
        request.timeoutInterval = timeout.isFinite && timeout > 0 ? timeout : 15
        request.setValue("Bearer \(LocalService.tokenFromEnvironmentOrFile())", forHTTPHeaderField: "Authorization")
        if let body { request.setValue("application/json", forHTTPHeaderField: "Content-Type"); request.httpBody = try encoder.encode(body) }
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError where error.code == .timedOut {
            throw TradingServiceClientError.timedOut
        }
        guard let http = response as? HTTPURLResponse else { throw TradingServiceClientError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else { throw TradingServiceClientError.server(status: http.statusCode) }
        return try decoder.decode(T.self, from: data)
    }

    private func post<T: Encodable, R: Decodable>(_ path: String, body: T, timeout: TimeInterval = 15) async throws -> R {
        try await request(path: path, method: "POST", body: body, timeout: timeout)
    }
}

/// Starts and stops the direct FastAPI backend.
public actor LocalServiceProcess {
    private let gatewayURL: URL
    private let pythonURL: URL
    private var detachedPID: Int32?

    /// `NOVATRADE_FASTAPI_PATH` overrides the gateway source path. Bundles
    /// place it beside the app executable under `backend/main.py`.
    public init() {
        if let configured = ProcessInfo.processInfo.environment["NOVATRADE_FASTAPI_PATH"] {
            gatewayURL = URL(fileURLWithPath: configured)
        } else {
            let executableCandidates = [
                Bundle.main.executableURL,
                CommandLine.arguments.first.map { URL(fileURLWithPath: $0).standardizedFileURL }
            ].compactMap { $0 }
            let bundledCandidates = executableCandidates.map {
                $0.deletingLastPathComponent().appendingPathComponent("backend/main.py")
            } + [
                Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/backend/main.py")
            ]
            let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("backend/main.py")
            gatewayURL = bundledCandidates.first { FileManager.default.fileExists(atPath: $0.path) } ?? source
        }
        pythonURL = URL(fileURLWithPath: ProcessInfo.processInfo.environment["NOVATRADE_PYTHON"] ?? "/usr/bin/python3")
    }

    public func start() {
        guard !isRunning() else { return }
        let launcher = Process()
        launcher.executableURL = pythonURL
        launcher.arguments = [gatewayURL.path]
        var environment = ProcessInfo.processInfo.environment
        environment["NOVATRADE_FASTAPI_PORT"] = String(LocalService.defaultPort)
        let token = LocalService.tokenFromEnvironmentOrFile()
        environment[LocalService.tokenEnvironmentKey] = token
        launcher.environment = environment
        launcher.standardOutput = FileHandle.nullDevice
        launcher.standardError = FileHandle.nullDevice
        do {
            try launcher.run()
            detachedPID = launcher.processIdentifier
        } catch {
            detachedPID = nil
        }
    }

    /// Replaces a stale daemon that is still alive but no longer serving the
    /// current client. This is used after a failed health check so an old
    /// process cannot block startup forever.
    public func restart() async {
        stop()
        for _ in 0..<20 {
            if !isRunning() { break }
            try? await Task.sleep(for: .milliseconds(50))
        }
        start()
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
        lookup.arguments = ["-f", gatewayURL.path]
        let output = Pipe()
        lookup.standardOutput = output
        lookup.standardError = FileHandle.nullDevice
        do {
            try lookup.run()
            lookup.waitUntilExit()
            return String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                .split(whereSeparator: \.isNewline)
                .compactMap { Int32($0) }
                .first
        } catch {
            return nil
        }
    }
}
