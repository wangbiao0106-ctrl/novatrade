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

#if os(macOS)
@Test
func addsATKExecutableDirectoryToChildPath() {
    let runner = LocalATKCommandRunner(
        executableURL: URL(fileURLWithPath: "/tmp/nova-atk/bin/okx"),
        environment: ["PATH": "/usr/bin:/bin"]
    )
    let path = runner.environment?["PATH"] ?? ""
    #expect(path.split(separator: ":").first == "/tmp/nova-atk/bin")
    #expect(path.contains("/usr/bin"))
}
#endif

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
func parsesClosedSwapPositionHistoryForNativeExitSettlement() async throws {
    let runner = StubATKRunner(outputs: [
        "account positions-history --instType SWAP --instId BTC-USDT-SWAP --limit 100 --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","posId":"position-7","realizedPnl":"-12.5","uTime":"1700000000000"}]"#)
    ])
    let history = try await ATKClient(runner: runner).closedSwapPositions(instrumentID: "BTC-USDT-SWAP")
    #expect(history.count == 1)
    #expect(history[0].positionID == "position-7")
    #expect(history[0].realizedPnL == Decimal(string: "-12.5"))
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
func resolvesSwapOrderByClientIDAndTreatsOnlyExplicitNotFoundAsAbsent() async throws {
    let runner = StubATKRunner(outputs: [
        "--demo swap get --instId BTC-USDT-SWAP --clOrdId hlsrleg1 --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ordId":"remote-1","clOrdId":"hlsrleg1","side":"buy","state":"live","sz":"1","cTime":"1700000000000"}]"#),
        "--demo swap get --instId BTC-USDT-SWAP --clOrdId missing --json": ATKCommandResult(stdout: #"{"code":"51603","msg":"Order does not exist"}"#)
    ])
    let client = ATKClient(runner: runner)
    let found = try await client.swapOrder(instrumentID: "BTC-USDT-SWAP", clientOrderID: "hlsrleg1", demo: true)
    #expect(found?.id == "remote-1")
    let absent = try await client.swapOrder(instrumentID: "BTC-USDT-SWAP", clientOrderID: "missing", demo: true)
    #expect(absent == nil)
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
func demoOrderPassesConfiguredLeverageToATK() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#),
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#),
        "--demo swap leverage --instId BTC-USDT-SWAP --lever 3 --mgnMode cross --json": ATKCommandResult(stdout: #"[{"lever":"3","mgnMode":"cross","instId":"BTC-USDT-SWAP"}]"#),
        "--demo swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz 1 --tdMode cross --json": ATKCommandResult(stdout: #"[{"ordId":"demo-lever-3","sCode":"0"}]"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "sell", quantity: 1, leverage: 3)
    let result = try await ATKClient(runner: runner).placeDemoSwapOrder(order)
    #expect(result.orderID == "demo-lever-3")
}

@Test
func rejectsEntryWhenConfiguredLeverageCannotBeApplied() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#),
        "market instruments --instType SWAP --instId BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#),
        "--demo swap leverage --instId BTC-USDT-SWAP --lever 3 --mgnMode cross --json": ATKCommandResult(stdout: #"{"code":"51000","msg":"leverage rejected"}"#),
        "--demo swap place --instId BTC-USDT-SWAP --side sell --ordType market --sz 1 --tdMode cross --json": ATKCommandResult(stdout: #"[{"ordId":"must-not-place","sCode":"0"}]"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "sell", quantity: 1, leverage: 3)
    await #expect(throws: ATKError.commandFailed(code: 51000, message: "leverage rejected")) {
        try await ATKClient(runner: runner).placeDemoSwapOrder(order)
    }
}

@Test
func liveOrderRequestKeepsLeverageCodableCompatibility() throws {
    let decoder = JSONDecoder()
    let legacy = try decoder.decode(LiveOrderRequest.self, from: Data(#"{"instrumentID":"BTC-USDT-SWAP","side":"buy","quantity":1}"#.utf8))
    #expect(legacy.leverage == nil)

    let request = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1, leverage: 2.5)
    let encoded = try JSONEncoder().encode(request)
    let object = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]
    #expect((object?["leverage"] as? NSNumber)?.doubleValue == 2.5)
}

@Test
func rejectsLeverageOutsideGatewayRange() async throws {
    for leverage in [Decimal.zero, Decimal(string: "100.5")!] {
        let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1, leverage: leverage)
        await #expect(throws: ATKError.invalidOrder("杠杆必须是 1 到 100 倍之间的有限值")) {
            try await ATKClient(runner: StubATKRunner(outputs: [:])).placeDemoSwapOrder(order)
        }
    }
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
func todayPnLUsesUTCDayBoundary() {
    var shanghai = Calendar(identifier: .gregorian)
    shanghai.timeZone = TimeZone(identifier: "Asia/Shanghai")!
    let now = shanghai.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 8, minute: 1))!
    let justBeforeUTCMidnight = shanghai.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 7, minute: 59))!
    let justAfterUTCMidnight = shanghai.date(from: DateComponents(year: 2026, month: 9, day: 2, hour: 8, minute: 2))!
    let root: [[String: Any]] = [
        ["ts": String(Int(justBeforeUTCMidnight.timeIntervalSince1970 * 1000)), "pnl": "-1"],
        ["ts": String(Int(justAfterUTCMidnight.timeIntervalSince1970 * 1000)), "pnl": "2"]
    ]

    #expect(ATKClient.todayPnL(from: root, now: now) == Decimal(2))
}

@Test
func marketContractsUseUTCDayOpenForChangePercent() async throws {
    let runner = StubATKRunner(outputs: [
        "market tickers SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","last":"110","open24h":"108","sodUtc0":"100","sodUtc8":"105","volCcy24h":"2500"}]"#)
    ])

    let markets = try await ATKClient(runner: runner).marketContracts()
    #expect(markets.count == 1)
    #expect(markets[0].changePercent == Decimal(10))
}

@Test
func marketContractsRankByQuoteTurnoverNotBaseCoinCount() async throws {
    // A cheap coin trades far more coins than an expensive one, but the
    // strategy universes rank by USDT turnover (volCcy24h × last). The
    // contract count in `vol24h` must never be used as a fallback.
    let runner = StubATKRunner(outputs: [
        "market tickers SWAP --json": ATKCommandResult(stdout: #"[{"instId":"CHEAP-USDT-SWAP","last":"0.001","volCcy24h":"1000000000","vol24h":"1"},{"instId":"PRICY-USDT-SWAP","last":"50","volCcy24h":"100000","vol24h":"1"},{"instId":"BARE-USDT-SWAP","last":"2","vol24h":"999999999"}]"#)
    ])

    let markets = try await ATKClient(runner: runner).marketContracts()
    let volumes = Dictionary(uniqueKeysWithValues: markets.map { ($0.id, $0.volume24h) })
    #expect(volumes["CHEAP-USDT-SWAP"] == Decimal(1_000_000))
    #expect(volumes["PRICY-USDT-SWAP"] == Decimal(5_000_000))
    #expect(volumes["BARE-USDT-SWAP"] == 0)
}

@Test
func marketContractsFallbackWhenUTCDayOpenIsZero() async throws {
    let runner = StubATKRunner(outputs: [
        "market tickers SWAP --json": ATKCommandResult(stdout: #"[{"instId":"NEW-USDT-SWAP","last":"110","open24h":"100","sodUtc0":"0","volCcy24h":"2500"}]"#)
    ])

    let markets = try await ATKClient(runner: runner).marketContracts()
    #expect(markets.count == 1)
    #expect(markets[0].changePercent == Decimal(10))
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
