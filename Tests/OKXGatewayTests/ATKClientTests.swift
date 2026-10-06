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
func addsATKExecutableDirectoryToChildPath() {
    let runner = LocalATKCommandRunner(
        executableURL: URL(fileURLWithPath: "/tmp/nova-atk/bin/okx"),
        environment: ["PATH": "/usr/bin:/bin"]
    )
    let path = runner.environment?["PATH"] ?? ""
    #expect(path.split(separator: ":").first == "/tmp/nova-atk/bin")
    #expect(path.contains("/usr/bin"))
}

@Test
func readsTickerThroughATK() async throws {
    let runner = StubATKRunner(outputs: [
        "market ticker BTC-USDT-SWAP --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","last":"100.5","bidPx":"100.4","askPx":"100.6","ts":"1700000000000"}]"#)
    ])
    let ticker = try await ATKClient(runner: runner).marketTicker(instrumentID: "BTC-USDT-SWAP")
    #expect(ticker.last == Decimal(string: "100.5"))
}

@Test
func surfacesATKCommandFailures() async throws {
    let client = ATKClient(runner: StubATKRunner(outputs: [:]))
    await #expect(throws: ATKError.self) {
        try await client.marketTicker(instrumentID: "BTC-USDT-SWAP")
    }
}

@Test
func parsesClosedSwapPositionHistoryForNativeExitSettlement() async throws {
    let runner = StubATKRunner(outputs: [
        "account positions-history --instType SWAP --instId BTC-USDT-SWAP --limit 100 --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","posId":"position-7","realizedPnl":"-12.5","uTime":"1700000000000","openAvgPx":"100","closeAvgPx":"95","closeTotalPos":"2","closeType":"tp"}]"#)
    ])
    let history = try await ATKClient(runner: runner).closedSwapPositions(instrumentID: "BTC-USDT-SWAP")
    #expect(history.count == 1)
    #expect(history[0].positionID == "position-7")
    #expect(history[0].realizedPnL == Decimal(string: "-12.5"))
    #expect(history[0].entryPrice == 100)
    #expect(history[0].exitPrice == 95)
    #expect(history[0].closedQuantity == 2)
    #expect(history[0].closeType == "tp")
}

@Test
func parsesNativeProtectionExitHistoryAndChildOrder() async throws {
    let runner = StubATKRunner(outputs: [
        "--demo swap algo orders --history --ordType oco --instId FIL-USDT-SWAP --limit 100 --json": ATKCommandResult(stdout: #"[{"algoId":"algo-7","instId":"FIL-USDT-SWAP","state":"effective","actualSide":"tp","actualSz":"12987","tpTriggerPx":"1.1899","slTriggerPx":"1.1439","tpTriggerPxType":"mark","triggerTime":"1791307820000","attachAlgoClOrdId":"entry-client-7","posId":"position-7","ordIdList":["child-7"]}]"#)
    ])

    let exits = try await ATKClient(runner: runner).nativeProtectionExitHistory(instrumentID: "FIL-USDT-SWAP", demo: true)
    let exit = try #require(exits.first)
    #expect(exit.algorithmID == "algo-7")
    #expect(exit.instrumentID == "FIL-USDT-SWAP")
    #expect(exit.state == "effective")
    #expect(exit.actualSide == "tp")
    #expect(exit.actualQuantity == 12987)
    #expect(exit.triggerPrice == Decimal(string: "1.1899"))
    #expect(exit.triggerPriceType == "mark")
    #expect(exit.triggerTime == Date(timeIntervalSince1970: 1_791_307_820))
    #expect(exit.attachedClientOrderID == "entry-client-7")
    #expect(exit.positionID == "position-7")
    #expect(exit.closingOrderIDs == ["child-7"])
    #expect(exit.stopLossTriggerPrice == Decimal(string: "1.1439"))
}

@Test
func nativeProtectionHistoryRejectsMismatchedInstrumentOrMissingAlgorithmID() async throws {
    let wrongInstrument = StubATKRunner(outputs: [
        "--live swap algo orders --history --ordType oco --instId FIL-USDT-SWAP --limit 100 --json": ATKCommandResult(stdout: #"[{"algoId":"algo-7","instId":"BTC-USDT-SWAP","state":"effective","actualSide":"tp"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("原生保护单历史缺少有效合约、算法单标识或状态")) {
        _ = try await ATKClient(runner: wrongInstrument).nativeProtectionExitHistory(instrumentID: "FIL-USDT-SWAP", demo: false)
    }

    let missingAlgorithm = StubATKRunner(outputs: [
        "--live swap algo orders --history --ordType oco --instId FIL-USDT-SWAP --limit 100 --json": ATKCommandResult(stdout: #"[{"instId":"FIL-USDT-SWAP","state":"effective","actualSide":"sl"}]"#)
    ])
    await #expect(throws: ATKError.invalidJSON("原生保护单历史缺少有效合约、算法单标识或状态")) {
        _ = try await ATKClient(runner: missingAlgorithm).nativeProtectionExitHistory(instrumentID: "FIL-USDT-SWAP", demo: false)
    }
}

@Test
func redactsConfigToAPIKeyPresence() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"secret-value","secret_key":"do-not-retain","passphrase":"hidden"}}}"#)
    ])
    let config = try await ATKClient(runner: runner).configSummary()
    #expect(config.profiles == [ATKProfileSummary(id: "live", site: "global", hasAPIKey: true)])
}

@Test
func refusesOrderWhenDefaultProfileHasNoAPIKey() async throws {
    let runner = StubATKRunner(outputs: [
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"oauth","profiles":{"oauth":{"site":"global"},"live":{"site":"global","api_key":"key"}}}"#)
    ])
    let order = LiveOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1)
    await #expect(throws: ATKError.apiKeyNotConfigured) {
        try await ATKClient(runner: runner).placeSwapOrder(order)
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
        "--demo swap get --instId BTC-USDT-SWAP --clOrdId clientleg1 --json": ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ordId":"remote-1","clOrdId":"clientleg1","side":"buy","state":"live","sz":"1","cTime":"1700000000000"}]"#),
        "--demo swap get --instId BTC-USDT-SWAP --clOrdId missing --json": ATKCommandResult(stdout: #"{"code":"51603","msg":"Order does not exist"}"#)
    ])
    let client = ATKClient(runner: runner)
    let found = try await client.swapOrder(instrumentID: "BTC-USDT-SWAP", clientOrderID: "clientleg1", demo: true)
    #expect(found?.id == "remote-1")
    let absent = try await client.swapOrder(instrumentID: "BTC-USDT-SWAP", clientOrderID: "missing", demo: true)
    #expect(absent == nil)
}

@Test
func resolvesSwapOrderByRemoteOrderIDAndRejectsMismatchedResponse() async throws {
    let runner = StubATKRunner(outputs: [
        "--demo swap get --instId FIL-USDT-SWAP --ordId child-7 --json": ATKCommandResult(stdout: #"[{"instId":"FIL-USDT-SWAP","ordId":"child-7","side":"buy","state":"filled","sz":"12987","avgPx":"1.19517","accFillSz":"12987","cTime":"1791307820000"}]"#),
        "--demo swap get --instId FIL-USDT-SWAP --ordId missing --json": ATKCommandResult(stdout: #"{"code":"51603","msg":"Order does not exist"}"#),
        "--demo swap get --instId FIL-USDT-SWAP --ordId wrong --json": ATKCommandResult(stdout: #"[{"instId":"FIL-USDT-SWAP","ordId":"other","side":"buy","state":"filled","sz":"1","cTime":"1791307820000"}]"#)
    ])
    let client = ATKClient(runner: runner)
    let found = try await client.swapOrder(instrumentID: "FIL-USDT-SWAP", orderID: "child-7", demo: true)
    #expect(found?.id == "child-7")
    #expect(found?.averageFillPrice == Decimal(string: "1.19517"))
    #expect(found?.filledQuantity == 12987)
    #expect(try await client.swapOrder(instrumentID: "FIL-USDT-SWAP", orderID: "missing", demo: true) == nil)
    await #expect(throws: ATKError.invalidJSON("订单查询未返回匹配的订单号")) {
        _ = try await client.swapOrder(instrumentID: "FIL-USDT-SWAP", orderID: "wrong", demo: true)
    }
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
func liveOrderRequestEncodesLeverage() throws {
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
func requestsUTCAlignedDailyBarsForOneDayInterval() async throws {
    let runner = StubATKRunner(outputs: [
        "market candles BTC-USDT-SWAP --bar 1Dutc --limit 300 --json": ATKCommandResult(stdout: #"[["1760054400000","100","110","95","105","12","12","1248","1"]]"#)
    ])

    let candles = try await ATKClient(runner: runner).marketCandles(instrumentID: "BTC-USDT-SWAP", interval: .oneDay)

    #expect(candles.count == 1)
    // 2025-10-10 00:00:00 UTC must remain that instant; display formatting
    // can apply the computer's local time zone later without changing OHLC.
    #expect(candles[0].timestamp == Date(timeIntervalSince1970: 1_760_054_400))
    #expect(candles[0].open == 100)
    #expect(candles[0].close == 105)
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
    #expect(markets[0].rollingChangePercent > 1.85)
    #expect(markets[0].rollingChangePercent < 1.86)
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

@Test
func positionCarriesMarginModeAndCloseRepeatsIt() async throws {
    let runner = StubATKRunner(outputs: [
        "swap positions --json": ATKCommandResult(stdout: #"[{"posId":"p1","instId":"BTC-USDT-SWAP","pos":"2","avgPx":"100","markPx":"105","upl":"10","posSide":"net","mgnMode":"isolated","imr":"40","lever":"5","tpTriggerPx":"110","slTriggerPx":"95"},{"posId":"p2","instId":"ETH-USDT-SWAP","pos":"1","avgPx":"10","posSide":"net","mgnMode":"weird"}]"#),
        "config show --json": ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"key","demo":false}}}"#),
        "--live swap close --instId BTC-USDT-SWAP --mgnMode isolated --autoCxl --json": ATKCommandResult(stdout: #"{"code":"0","data":[{"instId":"BTC-USDT-SWAP"}]}"#)
    ])
    let client = ATKClient(runner: runner)
    let positions = try await client.swapPositions()
    #expect(positions.first(where: { $0.id == "p1" })?.marginMode == "isolated")
    #expect(positions.first(where: { $0.id == "p1" })?.margin == 40)
    #expect(positions.first(where: { $0.id == "p1" })?.leverage == 5)
    #expect(positions.first(where: { $0.id == "p1" })?.takeProfitPrice == 110)
    #expect(positions.first(where: { $0.id == "p1" })?.stopLossPrice == 95)
    // An unrecognized mode keeps the position visible and falls back to cross.
    #expect(positions.first(where: { $0.id == "p2" })?.marginMode == nil)
    try await client.closeLiveSwapPosition(instrumentID: "BTC-USDT-SWAP", positionSide: "net", marginMode: "isolated")
    await #expect(throws: ATKError.invalidOrder("保证金模式必须是 cross 或 isolated")) {
        try await client.closeLiveSwapPosition(instrumentID: "BTC-USDT-SWAP", marginMode: "portfolio")
    }
}

@Test
func orderCarriesPricesMarginAndProtectionLevels() async throws {
    let runner = StubATKRunner(outputs: [
        "swap orders --json": ATKCommandResult(stdout: #"[{"ordId":"o1","instId":"BTC-USDT-SWAP","side":"buy","state":"live","sz":"2","px":"100","cTime":"1700000000000","accFillSz":"1","avgPx":"99","imr":"40","lever":"5","tdMode":"isolated","attachAlgoOrds":[{"tpTriggerPx":"110","slTriggerPx":"95"}]}]"#)
    ])
    let order = try #require(try await ATKClient(runner: runner).swapOrders().first)
    #expect(order.price == 100)
    #expect(order.averageFillPrice == 99)
    #expect(order.margin == 40)
    #expect(order.leverage == 5)
    #expect(order.marginMode == "isolated")
    #expect(order.takeProfitPrice == 110)
    #expect(order.stopLossPrice == 95)
}
