import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

/// Minimal authenticated live-profile double used to verify the strategy
/// execution boundary. It records the exact CLI command so the test can prove
/// that the strategy selected the live route without calling the manual live
/// enable endpoint.
private actor StrategyExecutionModeRunner: ATKCommandRunning {
    private var commands: [String] = []

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        commands.append(command)
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"live","profiles":{"live":{"site":"global","api_key":"key","demo":false}}}"#)
        }
        if command.hasPrefix("account balance-all") {
            return ATKCommandResult(stdout: #"{"trading":{"totalEq":"1000","adjEq":"1000","details":[{"ccy":"USDT","eq":"1000","availEq":"1000","eqUsd":"1000"}]},"valuation":{"totalBal":"1000"}}"#)
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"live"}]"#) }
        if command.hasPrefix("account positions") || command == "swap positions --json" { return ATKCommandResult(stdout: "[]") }
        if command.hasPrefix("account bills") || command == "swap orders --json" { return ATKCommandResult(stdout: "[]") }
        if command.hasPrefix("market tickers SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"ALT-USDT-SWAP","last":"205.8","open24h":"100","sodUtc0":"100","volCcy24h":"100000000"}]"#)
        }
        if command.hasPrefix("market ticker ALT-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"ALT-USDT-SWAP","last":"205.8","ts":"1700000000000"}]"#)
        }
        if command.hasPrefix("market instruments --instType SWAP --instId ALT-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"ALT-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
        }
        if command.contains("swap place") { return ATKCommandResult(stdout: #"{"data":[{"ordId":"live-order"}]}"#) }
        if command.contains("swap leverage") { return ATKCommandResult(stdout: #"{"data":[{}]}"#) }
        return ATKCommandResult(stdout: "[]")
    }

    func sawLiveStrategyOrder() -> Bool {
        commands.contains { $0.contains("--live swap place") }
    }

    func placeCommands() -> [String] {
        commands.filter { $0.contains("swap place") }
    }
}

/// `halfRange` widens every bar so the signal's ATR-based stop can sit more
/// than 15% above the entry while still inside the rule's 3×ATR window.
private func strategyModeCandles(halfRange: Double = 1, signalHigh: Double = 210) -> [Candle] {
    let start = Date(timeIntervalSince1970: 1_700_000_000)
    return (0..<97).map { index in
        let timestamp = start.addingTimeInterval(Double(index) * 15 * 60)
        if index < 76 {
            let close = 100.0 + Double(index) * 0.8
            return Candle(timestamp: timestamp, open: Decimal(close - 0.2), high: Decimal(close + halfRange), low: Decimal(close - halfRange), close: Decimal(close), quoteVolume: 100)
        }
        if index < 96 {
            let close = 160.0 + Double(index - 76) * 2.45
            return Candle(timestamp: timestamp, open: Decimal(close - 0.2), high: Decimal(close + halfRange), low: Decimal(close - halfRange), close: Decimal(close), quoteVolume: 100)
        }
        return Candle(timestamp: timestamp, open: 206.5, high: Decimal(signalHigh), low: 202, close: 205.8, quoteVolume: 50)
    }
}

@Test("Strategy entry uses the whole pool as notional and ignores account margin ceilings")
func strategyEntrySizesOrderToFullPool() async throws {
    let runner = StrategyExecutionModeRunner()
    let market = MarketDataService(
        client: ATKClient(runner: runner),
        ttl: MarketCacheTTL(ticker: 0, contracts: 0, account: 0, positions: 0, orders: 0)
    )
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("strategy-full-pool-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    // Default limits: the 25% account margin ceiling must not apply to a
    // pool-sized strategy order.
    let backend = TradingBackend(
        market: market,
        paper: PaperTradingStore(directory: directory),
        riskEngine: RiskEngine(initialEquity: 1_000)
    )
    let created = try await backend.createStrategy(StrategyConfig(
        name: StrategyType.doublePumpExhaustionShort.displayName,
        scope: StrategyType.doublePumpExhaustionShort.defaultScope,
        interval: .fifteenMinutes,
        type: .doublePumpExhaustionShort,
        enabled: true,
        capitalPoolPercent: 100
    ))
    _ = try await backend.startStrategy(created.id)
    _ = try await backend.contracts(forceRefresh: true)
    for candle in strategyModeCandles() {
        _ = await backend.ingestRealtimeCandle(candle, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    }
    // 1,000 USDT pool at 205.8 per 1-coin contract → 4 contracts (823 USDT),
    // the largest lot that fits the pool; nothing else scales it.
    let placed = await runner.placeCommands()
    #expect(placed.count == 1)
    #expect(placed.first?.contains("--sz 4") == true)
}

@Test("Strategy entry is skipped when the stop sits more than 15% from the entry")
func strategyEntrySkipsSignalsBeyondStopDistanceCap() async throws {
    let runner = StrategyExecutionModeRunner()
    let market = MarketDataService(
        client: ATKClient(runner: runner),
        ttl: MarketCacheTTL(ticker: 0, contracts: 0, account: 0, positions: 0, orders: 0)
    )
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("strategy-stop-cap-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(
        market: market,
        paper: PaperTradingStore(directory: directory),
        riskEngine: RiskEngine(initialEquity: 1_000)
    )
    let created = try await backend.createStrategy(StrategyConfig(
        name: StrategyType.doublePumpExhaustionShort.displayName,
        scope: StrategyType.doublePumpExhaustionShort.defaultScope,
        interval: .fifteenMinutes,
        type: .doublePumpExhaustionShort,
        enabled: true
    ))
    _ = try await backend.startStrategy(created.id)
    _ = try await backend.contracts(forceRefresh: true)
    // ATR ≈ 13.6, stop ≈ 236 + 0.45 × ATR ≈ 242 → 17.6% above the 205.8
    // entry: inside the rule's 3×ATR window, outside the 15% loss cap.
    for candle in strategyModeCandles(halfRange: 6, signalHigh: 236) {
        _ = await backend.ingestRealtimeCandle(candle, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    }
    #expect(await runner.placeCommands().isEmpty)
    let logs = await backend.logs().map(\.message)
    #expect(logs.contains { $0.contains("止损距离") && $0.contains("超过 15") })
}

@Test("Strategy execution mode follows a live account without manual enablement")
func strategyUsesLiveAccountModeWithoutManualSwitch() async throws {
    let runner = StrategyExecutionModeRunner()
    let market = MarketDataService(
        client: ATKClient(runner: runner),
        ttl: MarketCacheTTL(ticker: 0, contracts: 0, account: 0, positions: 0, orders: 0)
    )
    let directory = FileManager.default.temporaryDirectory
        .appendingPathComponent("strategy-live-mode-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(
        market: market,
        paper: PaperTradingStore(directory: directory),
        riskEngine: RiskEngine(limits: RiskLimits(maxMarginPercent: 100), initialEquity: 1_000)
    )

    let config = StrategyConfig(
        name: StrategyType.doublePumpExhaustionShort.displayName,
        scope: StrategyType.doublePumpExhaustionShort.defaultScope,
        interval: .fifteenMinutes,
        type: .doublePumpExhaustionShort,
        enabled: true
    )
    let created = try await backend.createStrategy(config)
    _ = try await backend.startStrategy(created.id)
    _ = try await backend.contracts(forceRefresh: true)
    for candle in strategyModeCandles() {
        _ = await backend.ingestRealtimeCandle(candle, instrumentID: "ALT-USDT-SWAP", interval: .fifteenMinutes)
    }

    #expect(await runner.sawLiveStrategyOrder())
}
