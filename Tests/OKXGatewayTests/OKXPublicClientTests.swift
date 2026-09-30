import Foundation
import Testing
@testable import OKXGateway

private struct StubSession: HTTPSession {
    let body: Data

    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
    }
}

@Test
func decodesTicker() async throws {
    let json = #"{"code":"0","msg":"","data":[{"instId":"BTC-USDT-SWAP","last":"100.50","bidPx":"100.40","askPx":"100.60","ts":"1700000000000"}]}"#.data(using: .utf8)!
    let ticker = try await OKXPublicClient(session: StubSession(body: json)).ticker(instrumentID: "BTC-USDT-SWAP")
    #expect(ticker.instrumentID == "BTC-USDT-SWAP")
    #expect(ticker.last == Decimal(string: "100.50"))
    #expect(ticker.bid == Decimal(string: "100.40"))
}

@Test
func surfacesOKXErrors() async throws {
    let json = #"{"code":"51000","msg":"bad instrument","data":[]}"#.data(using: .utf8)!
    await #expect(throws: OKXGatewayError.self) {
        try await OKXPublicClient(session: StubSession(body: json)).ticker(instrumentID: "BAD")
    }
}

@Test
func rejectsTickerForUnexpectedInstrument() async throws {
    let json = #"{"code":"0","msg":"","data":[{"instId":"ETH-USDT-SWAP","last":"100.50","ts":"1700000000000"}]}"#.data(using: .utf8)!
    await #expect(throws: OKXGatewayError.invalidResponse) {
        try await OKXPublicClient(session: StubSession(body: json)).ticker(instrumentID: "BTC-USDT-SWAP")
    }
}

@Test
func rejectsMalformedHistoricalCandleInsteadOfDroppingIt() async throws {
    let json = #"{"code":"0","msg":"","data":[["1700000000000","100","101","99","100","1","0","0","unknown"]]}"#.data(using: .utf8)!
    await #expect(throws: OKXGatewayError.invalidResponse) {
        try await OKXPublicClient(session: StubSession(body: json)).candles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test
func decodesHistoricalCandleQuoteVolumeFromOKXRESTRow() async throws {
    let json = #"{"code":"0","msg":"","data":[["1700000000000","100","101","99","100","12","12","1248","1"]]}"#.data(using: .utf8)!
    let candles = try await OKXPublicClient(session: StubSession(body: json)).candles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    #expect(candles.count == 1)
    #expect(candles[0].quoteVolume == 1248)
}

@Test
func rejectsEmptyOrderBookResponse() async throws {
    let json = #"{"code":"0","msg":"","data":[]}"#.data(using: .utf8)!
    await #expect(throws: OKXGatewayError.invalidResponse) {
        try await OKXPublicClient(session: StubSession(body: json)).orderBook(instrumentID: "BTC-USDT-SWAP")
    }
}

@Test
func rejectsHistoricalCandleWithoutConfirmationFlag() async throws {
    let json = #"{"code":"0","msg":"","data":[["1700000000000","100","101","99","100","1"]]}"#.data(using: .utf8)!
    await #expect(throws: OKXGatewayError.invalidResponse) {
        try await OKXPublicClient(session: StubSession(body: json)).candles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test
func rejectsTickerNumericPrefixInsteadOfTradingAtTruncatedPrice() async throws {
    let json = #"{"code":"0","msg":"","data":[{"instId":"BTC-USDT-SWAP","last":"100oops","ts":"1700000000000"}]}"#.data(using: .utf8)!
    await #expect(throws: OKXGatewayError.invalidNumber("100oops")) {
        try await OKXPublicClient(session: StubSession(body: json)).ticker(instrumentID: "BTC-USDT-SWAP")
    }
}

private actor RequestCapturingSession: HTTPSession {
    private(set) var requests: [URLRequest] = []
    func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        requests.append(request)
        let data = #"{"code":"0","msg":"","data":[]}"#.data(using: .utf8)!
        return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
    }
}

@Test
func marketRequestUsesBoundedTimeout() async throws {
    let session = RequestCapturingSession()
    let client = OKXPublicClient(session: session)
    _ = try await client.candles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    let requests = await session.requests
    #expect(requests.count == 1)
    #expect(requests[0].timeoutInterval == 15)
}
