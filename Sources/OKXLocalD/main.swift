import Foundation
import Hummingbird
import HummingbirdWebSocket
import TradingService
import TradingDomain

@main
struct OKXLocalD {
    static func main() throws {
        let port = ProcessInfo.processInfo.environment[LocalService.portEnvironmentKey].flatMap(Int.init) ?? LocalService.defaultPort
        let app = HBApplication(configuration: .init(address: .hostname("127.0.0.1", port: port), serverName: "NovaTrade"))
        let service = TradingHTTPServer()
        let backend = service.backend
        configureWebSockets(app, backend: backend)
        try service.configure(app)
        try app.start()
        Task.detached { await backend.appendLog("后台服务已启动，监听 127.0.0.1:\(port)") }
        app.wait()
    }

    // NIO invokes these callbacks on its event-loop thread. Registering them
    // directly inside Swift 6's MainActor-isolated main() causes a runtime
    // isolation trap (or a deadlock for an async callback while app.wait runs).
    private nonisolated static func configureWebSockets(_ app: HBApplication, backend: TradingBackend) {
        app.ws.addUpgrade()
        // Browsers do not apply same-origin policy to WebSocket upgrades.
        app.ws.add(middleware: LoopbackOriginGuardMiddleware())
        // One shared hub: a single upstream OKX socket and a single account/log
        // poll serve every client subscribed to the same instrument/interval
        // instead of opening one upstream connection per client.
        let hub = StreamHub(backend: backend)
        hub.startStrategyRuntime()
        app.ws.on("/api/v1/stream", onUpgrade: { _, ws in
            let writer = StreamWriter(webSocket: ws)
            let state = StreamState()
            writer.send(StreamEvent(type: "connection", timestamp: .now, payload: "connected"))
            ws.onRead { data, _ in
                guard case let .text(text) = data,
                      let body = text.data(using: .utf8),
                      let subscription = try? JSONDecoder().decode(StreamSubscription.self, from: body) else { return }
                writer.send(StreamEvent(type: "connection", timestamp: .now, payload: "subscribed:\(subscription.channels.joined(separator: ","))"))
                if let previous = state.active {
                    hub.unsubscribe(id: previous.id, instrumentID: previous.subscription.instrumentID, interval: previous.subscription.interval)
                }
                state.active = nil
                guard !subscription.channels.isEmpty else { return }
                let id = UUID()
                state.active = (id, subscription)
                hub.subscribe(StreamHub.Subscriber(id: id, writer: writer, channels: Set(subscription.channels)), instrumentID: subscription.instrumentID, interval: subscription.interval)
            }
            ws.onClose { _ in
                if let active = state.active {
                    hub.unsubscribe(id: active.id, instrumentID: active.subscription.instrumentID, interval: active.subscription.interval)
                }
            }
        })
    }

}

private final class StreamWriter: @unchecked Sendable {
    private let webSocket: HBWebSocket
    private let encoder: JSONEncoder
    private let lock = NSLock()
    init(webSocket: HBWebSocket) {
        self.webSocket = webSocket
        self.encoder = JSONEncoder()
        self.encoder.dateEncodingStrategy = .iso8601
    }
    func send(_ event: StreamEvent) {
        lock.lock()
        defer { lock.unlock() }
        write(event)
    }
    private func write(_ event: StreamEvent) {
        guard let data = try? encoder.encode(event), let text = String(data: data, encoding: .utf8) else { return }
        webSocket.write(.text(text), promise: nil)
    }
    func send<T: Encodable>(_ type: String, _ value: T, instrumentID: String) {
        lock.lock()
        defer { lock.unlock() }
        guard let data = try? encoder.encode(value), let payload = String(data: data, encoding: .utf8) else { return }
        write(StreamEvent(type: type, timestamp: .now, instrumentID: instrumentID, payload: payload))
    }
}

/// Per-socket subscription. NIO delivers a socket's read and close callbacks
/// on that socket's event loop, so they never race each other.
private final class StreamState: @unchecked Sendable {
    var active: (id: UUID, subscription: StreamSubscription)?
}

/// Bounds how many REST history prewarms run at once. Each prewarm spawns
/// `okx` CLI processes, and a 100-symbol strategy universe starts about 200
/// candle streams together; unbounded, that is hundreds of concurrent
/// processes and a burst against the OKX REST rate limit. Order calls do not
/// pass through this gate, so they never queue behind market data.
private actor PrewarmGate {
    private let limit: Int
    private var active = 0
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(limit: Int) { self.limit = limit }

    func run(_ body: @Sendable () async throws -> Void) async throws {
        if active < limit {
            active += 1
        } else {
            // `release` hands its slot straight to the next waiter.
            await withCheckedContinuation { waiters.append($0) }
        }
        defer { release() }
        try Task.checkCancellation()
        try await body()
    }

    private func release() {
        if waiters.isEmpty { active -= 1 } else { waiters.removeFirst().resume() }
    }
}

/// Shares one upstream OKX candle subscription and one account/log poll across
/// every WebSocket client watching the same instrument and interval. Fan-out
/// happens here; the backend ingests each realtime candle exactly once.
private final class StreamHub: @unchecked Sendable {
    struct Subscriber {
        let id: UUID
        let writer: StreamWriter
        let channels: Set<String>
    }

    struct StrategyTarget: Hashable {
        let instrumentID: String
        let interval: KlineInterval
        var key: String { "\(instrumentID):\(interval.rawValue)" }
    }

    private let lock = NSLock()
    private let backend: TradingBackend
    private let prewarmGate = PrewarmGate(limit: 4)
    private var subscribersByKey: [String: [Subscriber]] = [:]
    private var tasksByKey: [String: Task<Void, Never>] = [:]
    private var strategyTargets: Set<StrategyTarget> = []

    init(backend: TradingBackend) { self.backend = backend }

    func startStrategyRuntime() {
        Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let configs = await backend.strategies()
                do {
                    _ = try await backend.contracts(forceRefresh: true)
                } catch {
                    await backend.appendLog("策略币种范围刷新失败，继续使用上次合约列表：\(error.localizedDescription)", level: "warning")
                }
                let resolvedTargets = try? await backend.strategyUniverseTargets()
                let targetsByStrategy = Dictionary(uniqueKeysWithValues: (resolvedTargets ?? []).map { ($0.strategyID, $0.instrumentIDs) })
                var targets = Set<StrategyTarget>()
                for config in configs where config.enabled {
                    // Consume the same resolved cache exposed by
                    // /api/v1/strategies/targets and used by
                    // PaperTradingStore.evaluate. This avoids a second full
                    // universe filter and makes actual subscriptions auditable.
                    let instrumentIDs = targetsByStrategy[config.id] ?? []
                    for instrumentID in instrumentIDs {
                        targets.insert(StrategyTarget(instrumentID: instrumentID, interval: config.interval))
                        if config.type == .sweepReversalShort {
                            // 1h establishes the structure; 15m is the only
                            // execution confirmation stream for this rule.
                            targets.insert(StrategyTarget(instrumentID: instrumentID, interval: .fifteenMinutes))
                        }
                        if config.type == .hlsr {
                            // HLSR consumes confirmed 15m bars for entry and a
                            // separate completed 4H history for regime state.
                            targets.insert(StrategyTarget(instrumentID: instrumentID, interval: .fifteenMinutes))
                            targets.insert(StrategyTarget(instrumentID: instrumentID, interval: .fourHours))
                        }
                    }
                    if config.type == .sweepReversalShort && (config.parameters["btcGateEnabled"] ?? 1) >= 1 {
                        targets.insert(StrategyTarget(instrumentID: "BTC-USDT-SWAP", interval: .oneHour))
                    }
                }
                self.updateStrategyTargets(targets)
                do { try await Task.sleep(for: .seconds(30)) }
                catch { break }
            }
        }
    }

    private func updateStrategyTargets(_ targets: Set<StrategyTarget>) {
        lock.lock()
        let previous = strategyTargets
        strategyTargets = targets
        for target in targets where tasksByKey[target.key] == nil {
            tasksByKey[target.key] = supervisor(hubKey: target.key, instrumentID: target.instrumentID, interval: target.interval)
        }
        for target in previous.subtracting(targets) where (subscribersByKey[target.key] ?? []).isEmpty {
            tasksByKey[target.key]?.cancel()
            tasksByKey[target.key] = nil
        }
        lock.unlock()
    }

    private static func key(instrumentID: String, interval: KlineInterval) -> String { "\(instrumentID):\(interval.rawValue)" }

    func subscribe(_ subscriber: Subscriber, instrumentID: String, interval: KlineInterval) {
        lock.lock()
        let hubKey = Self.key(instrumentID: instrumentID, interval: interval)
        subscribersByKey[hubKey, default: []].append(subscriber)
        if tasksByKey[hubKey] == nil {
            tasksByKey[hubKey] = supervisor(hubKey: hubKey, instrumentID: instrumentID, interval: interval)
        }
        lock.unlock()
    }

    func unsubscribe(id: UUID, instrumentID: String, interval: KlineInterval) {
        lock.lock()
        let hubKey = Self.key(instrumentID: instrumentID, interval: interval)
        var remaining = subscribersByKey[hubKey] ?? []
        remaining.removeAll { $0.id == id }
        if remaining.isEmpty {
            subscribersByKey[hubKey] = nil
            if !strategyTargets.contains(where: { $0.key == hubKey }) {
                tasksByKey[hubKey]?.cancel()
                tasksByKey[hubKey] = nil
            }
        } else {
            subscribersByKey[hubKey] = remaining
        }
        lock.unlock()
    }

    // MARK: - Supervision

    /// Starts/stops the candle upstream and the account/log poll as channel
    /// demand changes. Both subtasks stop when the last subscriber leaves
    /// (the supervisor itself is cancelled by `unsubscribe`).
    private func supervisor(hubKey: String, instrumentID: String, interval: KlineInterval) -> Task<Void, Never> {
        Task { [weak self] in
            guard let self else { return }
            var upstream: Task<Void, Never>?
            var auxiliary: Task<Void, Never>?
            while !Task.isCancelled {
                let wants = self.wants(key: hubKey)
                if wants.candle {
                    if upstream == nil { upstream = self.candleUpstream(hubKey: hubKey, instrumentID: instrumentID, interval: interval) }
                } else {
                    upstream?.cancel()
                    upstream = nil
                }
                if wants.auxiliary {
                    if auxiliary == nil { auxiliary = self.auxiliaryPoll(hubKey: hubKey, instrumentID: instrumentID) }
                } else {
                    auxiliary?.cancel()
                    auxiliary = nil
                }
                do { try await Task.sleep(for: .seconds(1)) }
                catch { break }
            }
            upstream?.cancel()
            auxiliary?.cancel()
        }
    }

    private func wants(key: String) -> (candle: Bool, auxiliary: Bool) {
        lock.lock()
        defer { lock.unlock() }
        var candle = strategyTargets.contains { $0.key == key }
        var auxiliary = false
        for subscriber in subscribersByKey[key] ?? [] {
            if !subscriber.channels.isDisjoint(with: ["candle", "strategy", "risk"]) { candle = true }
            if !subscriber.channels.isDisjoint(with: ["account", "log"]) { auxiliary = true }
        }
        return (candle, auxiliary)
    }

    private func subscribers(of key: String) -> [Subscriber] {
        lock.lock()
        defer { lock.unlock() }
        return subscribersByKey[key] ?? []
    }

    // MARK: - Upstream candle stream

    private func prewarm(instrumentID: String, interval: KlineInterval, forceRefresh: Bool = false) async throws {
        let backend = backend
        try await prewarmGate.run {
            try await backend.prewarmStrategyMarket(instrumentID: instrumentID, interval: interval, forceRefresh: forceRefresh)
        }
    }

    private func candleUpstream(hubKey: String, instrumentID: String, interval: KlineInterval) -> Task<Void, Never> {
        Task { [weak self] in
            guard let self else { return }
            // Historical REST snapshots are never emitted as live candles.
            // Retry the history load so strategy indicators have enough
            // context before the socket begins delivering new bars.
            // REST history must be reloaded on the next subscription when the
            // prewarm failed, and after every reconnect thereafter.
            var needsHistoryReload = true
            for attempt in 0..<3 where !Task.isCancelled {
                do {
                    try await self.prewarm(instrumentID: instrumentID, interval: interval)
                    needsHistoryReload = false
                    break
                } catch {
                    if attempt < 2 {
                        try? await Task.sleep(for: .seconds(2))
                    } else {
                        await backend.appendLog("\(instrumentID) \(interval.rawValue) 历史 K 线预热失败：\(error.localizedDescription)", level: "warning")
                    }
                }
            }
            for await event in await backend.market.candleEvents(instrumentID: instrumentID, interval: interval) {
                guard !Task.isCancelled else { break }
                switch event {
                case .connecting:
                    self.sendConnection(hubKey: hubKey, state: "okx_wss_connecting", instrumentID: instrumentID)
                case .subscribed:
                    self.sendConnection(hubKey: hubKey, state: "okx_wss_subscribed", instrumentID: instrumentID)
                    // `.subscribed` is reported again after every reconnect.
                    // OKX does not replay bars that closed while the socket
                    // was down, so reload them over REST; otherwise indicators
                    // run over a silent gap and a dangling open bar blocks
                    // HLSR evaluation.
                    if needsHistoryReload {
                        do {
                            try await self.prewarm(instrumentID: instrumentID, interval: interval, forceRefresh: true)
                        } catch {
                            await backend.appendLog("\(instrumentID) \(interval.rawValue) 重连后补齐 K 线失败：\(error.localizedDescription)", level: "warning")
                        }
                    }
                    needsHistoryReload = true
                case let .reconnecting(reason, retryInSeconds):
                    self.sendConnection(hubKey: hubKey, state: "okx_wss_reconnecting", instrumentID: instrumentID)
                    await backend.appendLog("OKX WSS \(instrumentID) \(interval.rawValue) 断开：\(reason)，\(retryInSeconds) 秒后重连", level: "warning")
                    self.send(hubKey: hubKey, type: "log", value: await backend.logs(), instrumentID: instrumentID) { _ in true }
                case let .candle(candle):
                    let statuses = await backend.ingestRealtimeCandle(candle, instrumentID: instrumentID, interval: interval)
                    for subscriber in self.subscribers(of: hubKey) {
                        if subscriber.channels.contains("candle") { subscriber.writer.send("candle", candle, instrumentID: instrumentID) }
                        if subscriber.channels.contains("strategy") { subscriber.writer.send("strategy", statuses, instrumentID: instrumentID) }
                        if subscriber.channels.contains("risk") { subscriber.writer.send("risk", await backend.broker.riskSnapshot(), instrumentID: instrumentID) }
                    }
                }
            }
        }
    }

    // MARK: - Account and log polling

    private func auxiliaryPoll(hubKey: String, instrumentID: String) -> Task<Void, Never> {
        Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                if self.subscribers(of: hubKey).contains(where: { $0.channels.contains("account") }),
                   let account = try? await backend.account() {
                    self.send(hubKey: hubKey, type: "account", value: account, instrumentID: instrumentID) { $0.channels.contains("account") }
                }
                if self.subscribers(of: hubKey).contains(where: { $0.channels.contains("log") }) {
                    self.send(hubKey: hubKey, type: "log", value: await backend.logs(), instrumentID: instrumentID) { $0.channels.contains("log") }
                }
                do { try await Task.sleep(for: .seconds(5)) }
                catch { break }
            }
        }
    }

    // MARK: - Fan-out

    private func sendConnection(hubKey: String, state: String, instrumentID: String) {
        for subscriber in subscribers(of: hubKey) {
            subscriber.writer.send(StreamEvent(type: "connection", timestamp: .now, instrumentID: instrumentID, payload: state))
        }
    }

    private func send<T: Encodable>(hubKey: String, type: String, value: T, instrumentID: String, where filter: (Subscriber) -> Bool) {
        for subscriber in subscribers(of: hubKey) where filter(subscriber) {
            subscriber.writer.send(type, value, instrumentID: instrumentID)
        }
    }
}
