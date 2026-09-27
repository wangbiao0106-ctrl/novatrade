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
