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
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#),
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
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#),
        "--demo swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz 1 --tdMode cross --json": ATKCommandResult(stdout: #"[{"ordId":"demo-123","sCode":"0"}]"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "sell", quantity: 1)
    let result = try await ATKClient(runner: runner).placeDemoSwapOrder(order)
    #expect(result.orderID == "demo-123")
}

@Test
func rejectsAPILevelFailureReturnedInsideSuccessfulCLIResponse() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"key","demo":false}}}"#),
        "--live swap cancel BTC-USDT-SWAP --ordId 123 --json": ATKCommandResult(stdout: #"{"code":"0","msg":"","data":[{"sCode":"51008","sMsg":"order already canceled"}]}"#)
    ])
    await #expect(throws: ATKError.commandFailed(code: 51008, message: "order already canceled")) {
        try await ATKClient(runner: runner).cancelLiveSwapOrder(instrumentID: "BTC-USDT-SWAP", orderID: "123")
    }
}

@Test
func rejectsNonFiniteOrderQuantityBeforeInvokingCLI() async throws {
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: .nan)
    await #expect(throws: ATKError.invalidOrder("数量必须是有限的正数")) {
        try await ATKClient(runner: StubATKRunner(outputs: [:])).placeSwapOrder(order)
    }
}

@Test
func rejectsMalformedCandleRowsInsteadOfSilentlyDroppingThem() async throws {
    let runner = StubATKRunner(outputs: [
        "market candles BTC-USDT-SWAP --bar 1H --limit 300 --json": ATKCommandResult(stdout: #"[["1700000000000","100","101","99","100","1","0","0","x"]]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("K 线第 0 行格式无效")) {
        try await ATKClient(runner: runner).marketCandles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test
func decodesCandleQuoteVolumeFromATKRow() async throws {
    let runner = StubATKRunner(outputs: [
        "market candles BTC-USDT-SWAP --bar 1H --limit 300 --json": ATKCommandResult(stdout: #"[["1700000000000","100","101","99","100","12","12","1248","1"]]"#)
    ])
    let candles = try await ATKClient(runner: runner).marketCandles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    #expect(candles.count == 1)
    #expect(candles[0].quoteVolume == 1248)
}

@Test
func rejectsBooleanTickerTimestampInsteadOfTreatingItAsEpochMilliseconds() async throws {
    let runner = StubATKRunner(outputs: [
        "market ticker BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","last":"100.5","ts":true}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("ticker 时间戳无效")) {
        try await ATKClient(runner: runner).marketTicker(instrumentID: "BTC-USDT-SWAP")
    }
}

@Test
func rejectsMalformedPositionEnvelopeInsteadOfTreatingItAsNoPositions() async throws {
    let runner = StubATKRunner(outputs: [
        "swap positions --json": ATKCommandResult(stdout: #"{"unexpected":[]}"#)
    ])
    await #expect(throws: ATKError.invalidJSON("持仓响应格式无效")) {
        try await ATKClient(runner: runner).swapPositions()
    }
}

@Test
func rejectsMalformedSwapPositionUsingTheSameRulesAsAccountSnapshot() async throws {
    let runner = StubATKRunner(outputs: [
        "swap positions --json": ATKCommandResult(stdout: #"[{"posId":"0","instId":"BTC-USDT-SWAP","pos":"1","avgPx":"100oops","markPx":"101","posSide":"net"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("持仓缺少有效开仓价")) {
        try await ATKClient(runner: runner).swapPositions()
    }
}

@Test
func rejectsCandleWithoutConfirmationFlag() async throws {
    let runner = StubATKRunner(outputs: [
        "market candles BTC-USDT-SWAP --bar 1H --limit 300 --json": ATKCommandResult(stdout: #"[["1700000000000","100","101","99","100","1"]]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("K 线第 0 行格式无效")) {
        try await ATKClient(runner: runner).marketCandles(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    }
}

@Test
func doesNotLetZeroSubCodeHideNonZeroTopLevelCode() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"key","demo":false}}}"#),
        "--live swap cancel BTC-USDT-SWAP --ordId 123 --json": ATKCommandResult(stdout: #"{"code":"51008","sCode":"0","msg":"outer failure"}"#)
    ])
    await #expect(throws: ATKError.commandFailed(code: 51008, message: "outer failure")) {
        try await ATKClient(runner: runner).cancelLiveSwapOrder(instrumentID: "BTC-USDT-SWAP", orderID: "123")
    }
}

@Test
func rejectsMalformedAccountBalanceInsteadOfReturningAuthenticatedEmptyAccount() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"key","demo":false}}}"#),
        "account balance-all --valuationCcy USD --json": ATKCommandResult(stdout: #"{"trading":{"details":[]}}"#),
        "account config --json": ATKCommandResult(stdout: #"[]"#),
        "account positions --instType SWAP --json": ATKCommandResult(stdout: #"[]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("账户余额缺少有效权益")) {
        try await ATKClient(runner: runner).accountOverview()
    }
}

@Test
func parsesStrictUSDTLinearSwapInstrumentSpecification() async throws {
    let runner = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"0.01","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
    ])
    let spec = try await ATKClient(runner: runner).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    #expect(spec.instrumentID == "BTC-USDT-SWAP")
    #expect(spec.ctVal == Decimal(string: "0.01"))
    #expect(spec.ctMult == 1)
    #expect(spec.lotSize == 1)
    #expect(spec.minSize == 1)
    #expect(spec.tickSize == Decimal(string: "0.1"))
    #expect(spec.isLiveUSDTLinearSwap)
}

@Test
func rejectsNonLiveOrNonUSDTLinearInstrumentSpecification() async throws {
    let suspended = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"0.01","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"suspend","ctType":"linear","settleCcy":"USDT"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("合约规格状态不是 live")) {
        try await ATKClient(runner: suspended).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    }

    let inverse = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"100","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"1","state":"live","ctType":"inverse","settleCcy":"USD"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("仅支持 linear 合约")) {
        try await ATKClient(runner: inverse).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    }
}

@Test
func rejectsMalformedInstrumentNumbersAndWrongInstrumentID() async throws {
    let malformed = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"0.01oops","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("合约规格 ctVal 无效")) {
        try await ATKClient(runner: malformed).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    }

    let wrongID = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"ETH-USDT-SWAP","ctVal":"0.01","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("合约规格返回了错误的合约")) {
        try await ATKClient(runner: wrongID).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    }
}

@Test
func rejectsEmptyOrFailedInstrumentSpecificationEnvelope() async throws {
    let empty = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"{"code":"0","data":[]}"#)
    ])
    await #expect(throws: ATKError.invalidJSON("合约规格响应必须包含唯一数据行")) {
        try await ATKClient(runner: empty).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    }

    let failed = StubATKRunner(outputs: [
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"{"code":"51000","msg":"bad request","data":[]}"#)
    ])
    await #expect(throws: ATKError.commandFailed(code: 51000, message: "bad request")) {
        try await ATKClient(runner: failed).marketInstrumentSpec(instrumentID: "BTC-USDT-SWAP")
    }
}
