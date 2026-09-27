import Foundation
import Testing
import TradingDomain
@testable import TradingService
@testable import ATKGateway

private actor CountingRunner: ATKCommandRunning {
    private var calls: [String: Int] = [:]
    private let outputs: [String: String]
    private let delay: Duration?

    init(outputs: [String: String], delay: Duration? = nil) {
        self.outputs = outputs
        self.delay = delay
    }

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let key = arguments.joined(separator: " ")
        calls[key, default: 0] += 1
        if let delay {
            do { try await Task.sleep(for: delay) } catch { }
        }
        guard let stdout = outputs[key] else {
            return ATKCommandResult(stdout: "", stderr: "missing \(key)", exitCode: 1)
        }
        return ATKCommandResult(stdout: stdout)
    }

    func total() -> Int { calls.values.reduce(0, +) }
}

@Test
func marketDataServiceCachesTickerAndCollapsesConcurrentRequests() async throws {
    let tickerOutput = #"[{"instId":"BTC-USDT-SWAP","last":"100.5","ts":"1700000000000"}]"#
    let runner = CountingRunner(outputs: ["market ticker BTC-USDT-SWAP --json": tickerOutput], delay: .milliseconds(80))
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 10))
    async let first: MarketTicker = service.ticker(instrumentID: "BTC-USDT-SWAP")
    async let second: MarketTicker = service.ticker(instrumentID: "BTC-USDT-SWAP")
    let results = try await [first, second]
    #expect(results[0].last == Decimal(string: "100.5"))
    #expect(results[1] == results[0])
    // Concurrent misses collapse into one CLI invocation.
    #expect(await runner.total() == 1)
    // The fresh cache serves the next read without another invocation.
    _ = try await service.ticker(instrumentID: "BTC-USDT-SWAP")
    #expect(await runner.total() == 1)
}

@Test
func marketDataServiceRefetchesAfterTickerTTLExpires() async throws {
    let tickerOutput = #"[{"instId":"BTC-USDT-SWAP","last":"100.5","ts":"1700000000000"}]"#
    let runner = CountingRunner(outputs: ["market ticker BTC-USDT-SWAP --json": tickerOutput])
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: -1))
    _ = try await service.ticker(instrumentID: "BTC-USDT-SWAP")
    _ = try await service.ticker(instrumentID: "BTC-USDT-SWAP")
    #expect(await runner.total() == 2)
}

@Test
func marketDataServiceForceRefreshesContractsBeyondCacheTTL() async throws {
    let contractsOutput = #"[{"instId":"BTC-USDT-SWAP","last":"100.5","open24h":"100","volCcy24h":"2500"}]"#
    let runner = CountingRunner(outputs: ["market tickers SWAP --json": contractsOutput])
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(contracts: 60))

    _ = try await service.contracts()
    _ = try await service.contracts(forceRefresh: true)

    #expect(await runner.total() == 2)
}

@Test
func marketDataServiceCachesHistoricalSnapshotWithinTTL() async throws {
    let candleOutput = #"[[1700000000000,"100","110","95","104","12","12","1248","1"]]"#
    let tickerOutput = #"[{"instId":"BTC-USDT-SWAP","last":"104","ts":"1700000000000"}]"#
    let runner = CountingRunner(outputs: [
        "market candles BTC-USDT-SWAP --bar 1H --limit 300 --json": candleOutput,
        "market ticker BTC-USDT-SWAP --json": tickerOutput
    ])
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(snapshot: 10))
    let first = try await service.snapshot(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    #expect(first.candles.count == 1)
    let second = try await service.snapshot(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    #expect(second.candles.count == 1)
    #expect(await runner.total() == 2) // one candles call + one ticker call
    // Realtime updates keep the cached snapshot live without a REST reload.
    await service.cacheRealtimeCandle(Candle(timestamp: Date(timeIntervalSince1970: 1_700_003_600), open: 104, high: 108, low: 103, close: 107, confirmed: true), instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    let updated = try await service.snapshot(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    #expect(updated.candles.count == 2)
    #expect(await runner.total() == 2)
}

@Test
func marketDataServiceMergesRealtimeCandlesWhenHistoricalTTLExpires() async throws {
    let candleOutput = #"[[1700000000000,"100","110","95","104","12","12","1248","1"]]"#
    let tickerOutput = #"[{"instId":"BTC-USDT-SWAP","last":"104","ts":"1700000000000"}]"#
    let runner = CountingRunner(outputs: [
        "market candles BTC-USDT-SWAP --bar 1H --limit 300 --json": candleOutput,
        "market ticker BTC-USDT-SWAP --json": tickerOutput
    ])
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(snapshot: -1))
    _ = try await service.snapshot(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    await service.cacheRealtimeCandle(Candle(timestamp: Date(timeIntervalSince1970: 1_700_003_600), open: 104, high: 108, low: 103, close: 107, confirmed: true), instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    let refreshed = try await service.snapshot(instrumentID: "BTC-USDT-SWAP", interval: .oneHour)
    #expect(refreshed.candles.count == 2)
    #expect(refreshed.candles.last?.close == 107)
}

@Test
func marketDataServiceInvalidatesAccountStateAfterOrders() async throws {
    let runner = CountingRunner(outputs: [
        "config show --json": #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#,
        "account balance-all --valuationCcy USD --json": #"{"trading":{"totalEq":"1000","adjEq":"900","details":[{"ccy":"USDT","eq":"1000","availEq":"900","eqUsd":"1000"}]},"valuation":{"totalBal":"1000"}}"#,
        "account config --json": #"[{"label":"demo"}]"#,
        "account positions --instType SWAP --json": #"[]"#
    ])
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(account: 60))
    let first = try await service.account()
    #expect(first.authenticated)
    #expect(first.equityUSD == Decimal(string: "1000"))
    // Fresh cache: the second read spawns no new CLI processes.
    _ = try await service.account()
    let baseline = await runner.total()
    await service.invalidateAccountState()
    _ = try await service.account()
    #expect(await runner.total() > baseline)
}

@Test
func marketDataServiceDoesNotReinsertInFlightAccountAfterInvalidation() async throws {
    let runner = CountingRunner(outputs: [
        "config show --json": #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#,
        "account balance-all --valuationCcy USD --json": #"{"trading":{"totalEq":"1000","adjEq":"900","details":[]},"valuation":{"totalBal":"1000"}}"#,
        "account config --json": #"[{"label":"demo"}]"#,
        "account positions --instType SWAP --json": #"[]"#
    ], delay: .milliseconds(100))
    let service = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(account: 60))
    let inFlight = Task { try await service.account() }
    try await Task.sleep(for: .milliseconds(10))
    await service.invalidateAccountState()
    _ = try await inFlight.value
    let beforeRefresh = await runner.total()
    _ = try await service.account()
    #expect(await runner.total() > beforeRefresh)
}
