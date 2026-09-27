import Foundation
import Testing
@testable import ATKGateway
import TradingDomain

private struct StubATKRunner: ATKCommandRunning {
    let outputs: [String: ATKCommandResult]

    func run(arguments: [String]) async throws -> ATKCommandResult {
        outputs[arguments.joined(separator: " ")] ?? ATKCommandResult(stdout: "", stderr: "missing", exitCode: 1)
    }
}

@Test
func readsOAuthStatusAndTickerThroughATK() async throws {
    let runner = StubATKRunner(outputs: [
        "auth status --json": ATKCommandResult(stdout: #"{"profile":"oauth","site":"global","status":"logged_in","scopes":["live:read"]}"#),
        "market ticker BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","last":"100.5","bidPx":"100.4","askPx":"100.6","ts":"1700000000000"}]"#),
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"oauth","profiles":{"oauth":{"site":"global"}}}"#)
    ])
    let client = ATKClient(runner: runner)
    let auth = try await client.authStatus()
    let ticker = try await client.marketTicker(instrumentID: "BTC-USDT-SWAP")
    let config = try await client.configSummary()
    #expect(auth.isLoggedIn)
    #expect(auth.site == "global")
    #expect(ticker.last == Decimal(string: "100.5"))
    #expect(config.hasAPIKeyProfile == false)
}

@Test
func surfacesATKCommandFailures() async throws {
    let client = ATKClient(runner: StubATKRunner(outputs: [:]))
    await #expect(throws: ATKError.self) {
        try await client.authStatus()
    }
}

@Test
func redactsConfigToAPIKeyPresenceAndNormalizesUnauthenticatedSite() async throws {
    let runner = StubATKRunner(outputs: [
        "auth status --json": ATKCommandResult(stdout: #"{"profile":"oauth","site":"global","status":"not_logged_in"}"#, exitCode: 2),
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"secret-value","secret_key":"do-not-retain","passphrase":"hidden"}}}"#)
    ])
    let client = ATKClient(runner: runner)
    let status = try await client.authStatus()
    let config = try await client.configSummary()
    #expect(status.site == nil)
    #expect(config.hasAPIKeyProfile)
    #expect(config.profiles.first?.id == "live")
}

@Test
func rejectsOAuthLoginWhenAPIKeyProfileExists() async throws {
    let runner = StubATKRunner(outputs: [
        "auth login --manual --site global": ATKCommandResult(stdout: #"{"status":"skipped","reason":"api_key_configured","profile":"live"}"#, exitCode: 2)
    ])
    await #expect(throws: ATKError.apiKeyConfigured(profile: "live")) {
        try await ATKClient(runner: runner).authLoginManual(site: "global")
    }
}

@Test
func requiresAPIKeyProfileForTradingPath() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"oauth","profiles":{"oauth":{"site":"global"}}}"#)
    ])
    await #expect(throws: ATKError.apiKeyNotConfigured) {
        try await ATKClient(runner: runner).requireAPIKeyProfile()
    }
}

@Test
func requiresDefaultProfileToContainAPIKey() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"oauth","profiles":{"oauth":{"site":"global"},"live":{"site":"global","api_key":"key"}}}"#)
    ])
    await #expect(throws: ATKError.apiKeyNotConfigured) {
        try await ATKClient(runner: runner).requireAPIKeyProfile()
    }
}

@Test
func refusesLiveOrderForDemoProfile() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1)
    await #expect(throws: ATKError.demoProfile(profile: "demo")) {
        try await ATKClient(runner: runner).placeSwapOrder(order)
    }
}

@Test
func liveOrderPassesExplicitLiveFlagAndParsesResult() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"key","demo":false}}}"#),
        "--live swap place --instId BTC-USDT-SWAP --side buy --ordType market --sz 1 --tdMode cross --reduceOnly --json": ATKCommandResult(stdout: #"[{"ordId":"12345","clOrdId":"client-1","sCode":"0"}]"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1, reduceOnly: true)
    let result = try await ATKClient(runner: runner).placeSwapOrder(order)
    #expect(result.orderID == "12345")
    #expect(result.clientOrderID == "client-1")
}

@Test
func demoOrderPassesExplicitDemoFlagAndParsesResult() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#),
        "--demo swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz 1 --tdMode cross --json": ATKCommandResult(stdout: #"[{"ordId":"demo-123","sCode":"0"}]"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "sell", quantity: 1)
    let result = try await ATKClient(runner: runner).placeDemoSwapOrder(order)
    #expect(result.orderID == "demo-123")
}
