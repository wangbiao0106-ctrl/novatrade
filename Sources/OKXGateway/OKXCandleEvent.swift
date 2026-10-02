import Foundation
import TradingDomain

public enum OKXCandleEvent: Sendable, Equatable {
    case connecting(attempt: Int)
    case subscribed
    case candle(Candle)
    case reconnecting(reason: String, retryInSeconds: Double)
}

enum OKXCandleFrame: Sendable, Equatable {
    case subscribed
    case candles([Candle])
    case pong
    case ignored
}

/// Uses monotonic uptime so a system clock change cannot hide a dead socket.
struct OKXCandleHeartbeat: Sendable {
    enum Action: Equatable { case none, ping, disconnect }
    let startedAt: TimeInterval
    var lastReceivedAt: TimeInterval
    var pingSentAt: TimeInterval?
    var subscribed = false
    var failureReason: String?

    init(now: TimeInterval) {
        startedAt = now
        lastReceivedAt = now
    }

    mutating func received(at now: TimeInterval, subscribed: Bool) {
        lastReceivedAt = now
        pingSentAt = nil
        self.subscribed = self.subscribed || subscribed
    }

    mutating func action(at now: TimeInterval) -> Action {
        if !subscribed, now - startedAt >= 15 {
            failureReason = "OKX WSS 订阅超时"
            return .disconnect
        }
        if let pingSentAt, now - pingSentAt >= 10 {
            failureReason = "OKX WSS 心跳超时，连接已断开"
            return .disconnect
        }
        if pingSentAt == nil, now - lastReceivedAt >= 20 {
            pingSentAt = now
            return .ping
        }
        return .none
    }
}

actor OKXCandleConnectionHealth {
    private var heartbeat = OKXCandleHeartbeat(now: ProcessInfo.processInfo.systemUptime)

    var failureReason: String? { heartbeat.failureReason }

    func action() -> OKXCandleHeartbeat.Action {
        heartbeat.action(at: ProcessInfo.processInfo.systemUptime)
    }

    func received(subscribed: Bool) {
        heartbeat.received(at: ProcessInfo.processInfo.systemUptime, subscribed: subscribed)
    }

    func fail(_ reason: String) { heartbeat.failureReason = reason }
}

/// OKX accepts at most 3 WebSocket connection requests per second per IP.
/// Every candle stream opens its own socket, and a 100-symbol strategy
/// universe needs about 200 of them, so all connection attempts in this
/// process (first connects and reconnects alike) take a slot from one shared
/// pacer instead of opening in the same instant and being refused.
actor OKXConnectionPacer {
    static let shared = OKXConnectionPacer(spacing: .milliseconds(400))

    private let spacing: Duration
    private var nextSlot: ContinuousClock.Instant?

    init(spacing: Duration) { self.spacing = spacing }

    /// Reserves the next free slot and sleeps until it arrives. The slot is
    /// reserved before suspending, so concurrent callers queue in order.
    func waitForSlot() async throws {
        let now = ContinuousClock.now
        let slot = max(now, nextSlot ?? now)
        nextSlot = slot.advanced(by: spacing)
        if slot > now { try await Task.sleep(until: slot, clock: .continuous) }
    }
}
