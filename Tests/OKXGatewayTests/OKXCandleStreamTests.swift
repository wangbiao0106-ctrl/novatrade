import Foundation
import Testing
import TradingDomain
@testable import OKXGateway

@Test
func candleStreamDecodesLiveAndClosedBarsInTimeOrder() throws {
    let frame = #"{"arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"data":[["1700003600000","100","110","95","104","12","12","1248","0"],["1700000000000","90","101","88","100","20","20","2000","1"]]}"#
    let event = try OKXPublicClient.candleFrame(frame, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
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
func candleStreamSeparatesAcknowledgementsAndHeartbeatFromPrices() throws {
    let subscribed = #"{"event":"subscribe","arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"connId":"abc"}"#
    #expect(try OKXPublicClient.candleFrame(subscribed, instrumentID: "BTC-USDT-SWAP", interval: .oneHour) == .subscribed)
    #expect(try OKXPublicClient.candleFrame("pong", instrumentID: "BTC-USDT-SWAP", interval: .oneHour) == .pong)
    #expect(try OKXPublicClient.candleFrame(subscribed, instrumentID: "ETH-USDT-SWAP", interval: .oneHour) == .ignored)
    #expect(try OKXPublicClient.candleFrame(subscribed, instrumentID: "BTC-USDT-SWAP", interval: .oneMinute) == .ignored)
}

@Test
func candleStreamRejectsSubscriptionFailuresAndTruncatedBars() {
    let error = #"{"event":"error","code":"60012","msg":"Invalid request","connId":"abc"}"#
    #expect(throws: OKXGatewayError.self) {
        try OKXPublicClient.candleFrame(error, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
    let truncated = #"{"arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"data":[["1700000000000","100","110","95","104","12"]]}"#
    #expect(throws: OKXGatewayError.self) {
        try OKXPublicClient.candleFrame(truncated, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test
func candleStreamRejectsNonFiniteTimestamps() {
    let invalid = #"{"arg":{"channel":"candle1H","instId":"BTC-USDT-SWAP"},"data":[["NaN","100","110","95","104","12","12","1248","1"]]}"#
    #expect(throws: OKXGatewayError.self) {
        try OKXPublicClient.candleFrame(invalid, instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
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
