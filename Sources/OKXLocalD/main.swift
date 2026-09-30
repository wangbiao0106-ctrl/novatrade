import Foundation
import Hummingbird
import HummingbirdWebSocket
import TradingService
import TradingDomain

@main
struct OKXLocalD {
    static func main() throws {
        let port = Int(ProcessInfo.processInfo.environment["OKX_LOCALD_PORT"] ?? "8787") ?? 8787
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
        // One shared hub: a single upstream OKX socket and a single auxiliary
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
                if let previousID = state.subscriptionID, let previousInstrument = state.instrumentID, let previousInterval = state.interval {
                    hub.unsubscribe(id: previousID, instrumentID: previousInstrument, interval: previousInterval)
                }
                let instrumentID = subscription.instrumentID ?? "BTC-USDT-SWAP"
                let interval = subscription.interval ?? .fifteenMinutes
                let subscriptionID = UUID()
                state.subscriptionID = subscriptionID
                state.instrumentID = instrumentID
                state.interval = interval
                guard !subscription.channels.isEmpty else { return }
                hub.subscribe(StreamHub.Subscriber(id: subscriptionID, writer: writer, channels: Set(subscription.channels)), instrumentID: instrumentID, interval: interval)
            }
            ws.onClose { _ in
                if let id = state.subscriptionID, let instrumentID = state.instrumentID, let interval = state.interval {
                    hub.unsubscribe(id: id, instrumentID: instrumentID, interval: interval)
                }
            }
        })
    }

}

private struct StreamSubscription: Decodable {
    let channels: [String]
    let instrumentID: String?
    let interval: KlineInterval?
    enum CodingKeys: String, CodingKey { case channels, instrumentID = "instrumentID", instrument = "instId", interval, bar }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        channels = try values.decodeIfPresent([String].self, forKey: .channels) ?? []
        instrumentID = try values.decodeIfPresent(String.self, forKey: .instrumentID) ?? values.decodeIfPresent(String.self, forKey: .instrument)
        interval = try values.decodeIfPresent(KlineInterval.self, forKey: .interval) ?? values.decodeIfPresent(KlineInterval.self, forKey: .bar)
    }
}

private struct StreamEvent: Codable {
    let type: String
    let timestamp: Date?
    let instrumentID: String?
    let payload: String?
    init(type: String, timestamp: Date? = nil, instrumentID: String? = nil, payload: String? = nil) {
        self.type = type; self.timestamp = timestamp; self.instrumentID = instrumentID; self.payload = payload
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

private final class StreamState: @unchecked Sendable {
    var subscriptionID: UUID?
    var instrumentID: String?
    var interval: KlineInterval?
}

/// Shares one upstream OKX candle subscription and one auxiliary poll across
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
    private var subscribersByKey: [String: [Subscriber]] = [:]
    private var tasksByKey: [String: Task<Void, Never>] = [:]
    private var strategyTargets: Set<StrategyTarget> = []

    init(backend: TradingBackend) { self.backend = backend }

    func startStrategyRuntime() {
        Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let configs = await backend.strategies()
                let contracts: [ContractMarket]
                do {
                    contracts = try await backend.contracts(forceRefresh: true)
                } catch {
                    contracts = await backend.cachedContracts()
                    await backend.appendLog("策略币种范围刷新失败，继续使用上次合约列表：\(error.localizedDescription)", level: "warning")
                }
                var targets = Set<StrategyTarget>()
                for config in configs where config.enabled {
                    for instrumentID in config.scope.resolvedInstrumentIDs(from: contracts) {
                        targets.insert(StrategyTarget(instrumentID: instrumentID, interval: config.interval))
                        if config.type == .sweepReversalShort {
                            // 1h establishes the structure; 15m is the only
                            // execution confirmation stream for this rule.
                            targets.insert(StrategyTarget(instrumentID: instrumentID, interval: .fifteenMinutes))
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

    /// Starts/stops the candle upstream and the auxiliary poll as channel
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
            if !subscriber.channels.isDisjoint(with: ["ticker", "orderbook", "trade", "account", "log"]) { auxiliary = true }
        }
        return (candle, auxiliary)
    }

    private func subscribers(of key: String) -> [Subscriber] {
        lock.lock()
        defer { lock.unlock() }
        return subscribersByKey[key] ?? []
    }

    // MARK: - Upstream candle stream

    private func candleUpstream(hubKey: String, instrumentID: String, interval: KlineInterval) -> Task<Void, Never> {
        Task { [weak self] in
            guard let self else { return }
            // Historical REST snapshots are never emitted as live candles.
            // Retry the history load so strategy indicators have enough
            // context before the socket begins delivering new bars.
            for attempt in 0..<3 where !Task.isCancelled {
                do {
                    try await backend.prewarmStrategyMarket(instrumentID: instrumentID, interval: interval)
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

    // MARK: - Auxiliary polling

    private func auxiliaryPoll(hubKey: String, instrumentID: String) -> Task<Void, Never> {
        Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                if self.subscribers(of: hubKey).contains(where: { $0.channels.contains("ticker") }),
                   let ticker = try? await backend.market.ticker(instrumentID: instrumentID) {
                    self.send(hubKey: hubKey, type: "ticker", value: ticker, instrumentID: instrumentID) { $0.channels.contains("ticker") }
                }
                if self.subscribers(of: hubKey).contains(where: { $0.channels.contains("orderbook") }),
                   let book = try? await backend.market.orderBook(instrumentID: instrumentID) {
                    self.send(hubKey: hubKey, type: "orderbook", value: book, instrumentID: instrumentID) { $0.channels.contains("orderbook") }
                }
                if self.subscribers(of: hubKey).contains(where: { $0.channels.contains("trade") }),
                   let trades = try? await backend.market.trades(instrumentID: instrumentID) {
                    self.send(hubKey: hubKey, type: "trade", value: trades, instrumentID: instrumentID) { $0.channels.contains("trade") }
                }
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
