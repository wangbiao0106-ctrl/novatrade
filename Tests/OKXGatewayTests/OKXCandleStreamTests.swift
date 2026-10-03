import Foundation
import Testing
import TradingDomain
@testable import OKXGateway

@Test
func candleStreamDecodesLiveAndClosedBarsInTimeOrder() throws {
    let frame = #"{"arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"data":[["1700003600000","100","110","95","104","12","12","1248","0"],["1700000000000","90","101","88","100","20","20","2000","1"]]}"#
    let event = try OKXCandleSocket.candleFrame(frame, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    guard case let .candles(candles) = event else {
        Issue.record("Expected candle rows")
        return
    }
    #expect(candles.count == 2)
    #expect(candles[0].confirmed)
    #expect(!candles[1].confirmed)
    #expect(candles[1].close == 104)
    #expect(candles[1].volume == 12)
    #expect(candles[1].quoteVolume == 1248)
    #expect(candles[1].timestamp == Date(timeIntervalSince1970: 1_700_003_600))
}

@Test
func candleStreamUsesUTCAlignedChannelForOneDayInterval() throws {
    let frame = #"{"arg":{"channel":"candle1Dutc","instId":"BTC-USDT-SWAP"},"data":[["1760054400000","100","110","95","105","12","12","1248","1"]]}"#
    let event = try OKXCandleSocket.candleFrame(frame, instrumentID: "BTC-USDT-SWAP", interval: .oneDay)

    guard case let .candles(candles) = event else {
        Issue.record("Expected UTC daily candle row")
        return
    }
    #expect(candles.count == 1)
    #expect(candles[0].timestamp == Date(timeIntervalSince1970: 1_760_054_400))
}

@Test
func candleStreamSeparatesAcknowledgementsAndHeartbeatFromPrices() throws {
    let subscribed = #"{"event":"subscribe","arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"connId":"abc"}"#
    #expect(try OKXCandleSocket.candleFrame(subscribed, instrumentID: "BTC-USDT-SWAP", interval: .oneHour) == .subscribed)
    #expect(try OKXCandleSocket.candleFrame("pong", instrumentID: "BTC-USDT-SWAP", interval: .oneHour) == .pong)
    #expect(try OKXCandleSocket.candleFrame(subscribed, instrumentID: "ETH-USDT-SWAP", interval: .oneHour) == .ignored)
    #expect(try OKXCandleSocket.candleFrame(subscribed, instrumentID: "BTC-USDT-SWAP", interval: .oneMinute) == .ignored)
}

@Test
func candleStreamRejectsSubscriptionFailuresAndTruncatedBars() {
    let error = #"{"event":"error","code":"60012","msg":"Invalid request","connId":"abc"}"#
    #expect(throws: OKXGatewayError.self) {
        try OKXCandleSocket.candleFrame(error, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
    let truncated = #"{"arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"data":[["1700000000000","100","110","95","104","12"]]}"#
    #expect(throws: OKXGatewayError.self) {
        try OKXCandleSocket.candleFrame(truncated, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test(arguments: [
    #"["NaN","100","110","95","104","12","12","1248","1"]"#,
    // A numeric prefix must not be read as a truncated price.
    #"["1700000000000","100","110","95","104oops","12","12","1248","1"]"#,
    // Only "0" or "1" may decide whether a bar is closed.
    #"["1700000000000","100","110","95","104","12","12","1248","unknown"]"#
])
func candleStreamRejectsMalformedRows(row: String) {
    let frame = #"{"arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"data":["# + row + "]}"
    #expect(throws: OKXGatewayError.invalidResponse) {
        try OKXCandleSocket.candleFrame(frame, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test
func candleHeartbeatTimesOutMissingAcknowledgement() {
    var heartbeat = OKXCandleHeartbeat(now: 100)
    // A pong proves transport liveness but cannot acknowledge the subscription.
    heartbeat.received(at: 114, subscribed: false)
    #expect(heartbeat.action(at: 114) == .none)
    #expect(heartbeat.action(at: 115) == .disconnect)
    #expect(heartbeat.failureReason == "OKX WSS 订阅超时")
}

@Test
func candleHeartbeatSendsTextPingThenTimesOutMissingPong() {
    var heartbeat = OKXCandleHeartbeat(now: 100)
    heartbeat.received(at: 101, subscribed: true)
    #expect(heartbeat.action(at: 120) == .none)
    #expect(heartbeat.action(at: 121) == .ping)
    #expect(heartbeat.action(at: 129) == .none)
    #expect(heartbeat.action(at: 131) == .disconnect)
    #expect(heartbeat.failureReason?.contains("心跳超时") == true)
}

@Test
func candleHeartbeatPongAndCandleDataKeepIdleSubscriptionsAlive() {
    var heartbeat = OKXCandleHeartbeat(now: 100)
    heartbeat.received(at: 101, subscribed: true)
    #expect(heartbeat.action(at: 121) == .ping)
    heartbeat.received(at: 122, subscribed: false)
    #expect(heartbeat.action(at: 131) == .none)
    #expect(heartbeat.action(at: 142) == .ping)
    heartbeat.received(at: 143, subscribed: true)
    #expect(heartbeat.action(at: 152) == .none)
    #expect(heartbeat.action(at: 163) == .ping)
}

@Test
func connectionPacerSpacesConcurrentConnectionAttempts() async throws {
    let pacer = OKXConnectionPacer(spacing: .milliseconds(60))
    let clock = ContinuousClock()
    let start = clock.now
    try await withThrowingTaskGroup(of: Void.self) { group in
        for _ in 0..<4 { group.addTask { try await pacer.waitForSlot() } }
        try await group.waitForAll()
    }
    // Four attempts need three gaps; none may share a slot.
    #expect(clock.now - start >= .milliseconds(180))
}
