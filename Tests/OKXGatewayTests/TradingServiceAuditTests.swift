import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

/// A deterministic CLI double for the backend risk/reconciliation tests.  It
/// returns authenticated demo-account snapshots and can hold an order command
/// long enough for a concurrent account refresh to re-enter TradingBackend.
private actor TradingAuditRunner: ATKCommandRunning {
    private let delayedPlace: Bool
    private let rejectPlace: Bool
    private let positionOutput: String
    private let contractValue: Int
    private let lotSize: Int
    private let totalEquity: Decimal
    private let usdtEquity: Decimal?

    init(delayedPlace: Bool = false, rejectPlace: Bool = false, hasPosition: Bool = false,
         contractValue: Int = 1, lotSize: Int = 1,
         totalEquity: Decimal = 1_000, usdtEquity: Decimal? = 1_000) {
        self.delayedPlace = delayedPlace
        self.rejectPlace = rejectPlace
        self.contractValue = contractValue
        self.lotSize = lotSize
        self.totalEquity = totalEquity
        self.usdtEquity = usdtEquity
        self.positionOutput = hasPosition
            ? #"[{"instId":"BTC-USDT-SWAP","pos":"1","posId":"position-1","avgPx":"100","posSide":"long"}]"#
            : "[]"
    }

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        if command.contains("swap place") {
            if delayedPlace { try? await Task.sleep(for: .milliseconds(180)) }
            if rejectPlace { return ATKCommandResult(stdout: #"{"code":"51000","msg":"rejected"}"#) }
            return ATKCommandResult(stdout: #"{"data":[{"ordId":"order-1"}]}"#)
        }
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#)
        }
        if command.hasPrefix("account balance-all") {
            let details = usdtEquity.map { "[{\"ccy\":\"USDT\",\"eq\":\"\($0)\",\"availEq\":\"\($0)\",\"eqUsd\":\"\($0)\"}]" } ?? "[]"
            return ATKCommandResult(stdout: "{\"trading\":{\"totalEq\":\"\(totalEquity)\",\"adjEq\":\"\(totalEquity)\",\"details\":\(details)},\"valuation\":{\"totalBal\":\"\(totalEquity)\"}}")
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"demo"}]"#) }
        if command.hasPrefix("account positions") { return ATKCommandResult(stdout: positionOutput) }
        if command.hasPrefix("account bills") { return ATKCommandResult(stdout: "[]") }
        if command == "swap positions --json" { return ATKCommandResult(stdout: positionOutput) }
        if command == "swap orders --json" { return ATKCommandResult(stdout: "[]") }
        if command.hasPrefix("market ticker BTC-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","last":"100","ts":"1700000000000"}]"#)
        }
        if command.hasPrefix("market instruments --instType SWAP --instId BTC-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"\#(contractValue)","ctMult":"1","lotSz":"\#(lotSize)","minSz":"\#(lotSize)","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
        }
        return ATKCommandResult(stdout: "[]")
    }
}

@Test
func accountSyncUsesUSDTOnlyForStrategyPoolWhileKeepingTotalEquityForRisk() async throws {
    let runner = TradingAuditRunner(totalEquity: 10_000, usdtEquity: 1_000)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, contracts: 60, account: 0, positions: 0, orders: 0))
    let risk = RiskEngine(initialEquity: 100_000)
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("trading-usdt-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: risk)

    let account = try await backend.account()
    #expect(account.equityUSD == Decimal(10_000))
    #expect(account.usdtEquity == Decimal(1_000))

    let strategyID = UUID()
    let pool = await risk.registerStrategy(strategyID, allocationPercent: 50)
    #expect(pool.equity == Decimal(500))
    #expect((await risk.snapshot()).equity == Decimal(10_000))
}

@Test
func authenticatedAccountWithoutUSDTLeavesStrategyPoolAtZero() async throws {
    let runner = TradingAuditRunner(totalEquity: 10_000, usdtEquity: nil)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, contracts: 60, account: 0, positions: 0, orders: 0))
    let risk = RiskEngine(initialEquity: 100_000)
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("trading-no-usdt-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: risk)

    _ = try await backend.account()
    let pool = await risk.registerStrategy(UUID(), allocationPercent: 50)
    #expect(pool.equity == 0)
    #expect((await risk.snapshot()).equity == Decimal(10_000))
}

@Test
func strategyCreationPersistsOnlyRemainingUSDTAllocation() async throws {
    let runner = TradingAuditRunner(totalEquity: 10_000, usdtEquity: 1_000)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, contracts: 60, account: 0, positions: 0, orders: 0))
    let risk = RiskEngine(initialEquity: 100_000)
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("trading-usdt-create-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: risk)
    _ = try await backend.account()

    let first = try await backend.createStrategy(StrategyConfig(name: "第一策略", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort, capitalPoolPercent: 75))
    let second = try await backend.createStrategy(StrategyConfig(name: "第二策略", scope: .dynamic(.hotAltcoins), interval: .fifteenMinutes, type: .doublePumpExhaustionShort, riskPercent: 0.5, capitalPoolPercent: 75))
    #expect(first.capitalPoolPercent == 75)
    #expect(second.capitalPoolPercent == 25)
    let pools = await risk.strategyCapitals()
    #expect(pools.first(where: { $0.strategyID == first.id })?.equity == 750)
    #expect(pools.first(where: { $0.strategyID == second.id })?.equity == 250)
}

@Test
func strategyCreationRejectsSubMinimumRemainingUSDTAllocation() async throws {
    let runner = TradingAuditRunner(totalEquity: 10_000, usdtEquity: 1_000)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, contracts: 60, account: 0, positions: 0, orders: 0))
    let risk = RiskEngine(initialEquity: 100_000)
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("trading-usdt-minimum-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: risk)
    _ = try await backend.account()

    _ = try await backend.createStrategy(StrategyConfig(name: "主策略", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort, capitalPoolPercent: 99.95))
    await #expect(throws: ATKError.unavailable("USDT 资产已被其他策略占用，无法分配策略资金池")) {
        _ = try await backend.createStrategy(StrategyConfig(name: "余量策略", scope: .dynamic(.hotAltcoins), interval: .fifteenMinutes, type: .doublePumpExhaustionShort, capitalPoolPercent: 1))
    }
}

@Test
func strategyCreationRejectsKnownZeroUSDTBalance() async throws {
    let runner = TradingAuditRunner(totalEquity: 10_000, usdtEquity: nil)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, contracts: 60, account: 0, positions: 0, orders: 0))
    let risk = RiskEngine(initialEquity: 100_000)
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("trading-no-usdt-create-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: risk)
    _ = try await backend.account()

    let config = StrategyConfig(name: "无 USDT", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort)
    await #expect(throws: ATKError.unavailable("USDT 资产为 0，无法分配策略资金池")) {
        _ = try await backend.createStrategy(config)
    }
}

@Test
func remoteRiskUsesContractValueInsteadOfTreatingSizeAsBaseCoins() async throws {
    let runner = TradingAuditRunner(contractValue: 10)
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100), initialEquity: 100_000)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, contracts: 60, account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: FileManager.default.temporaryDirectory.appendingPathComponent("trading-audit-\(UUID().uuidString)")), riskEngine: risk)

    _ = try await backend.placePaperOrder(PaperOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1))
    let notionals = await risk.snapshot().globalNotionals
    #expect(notionals.values.first == 1_000)
}

@Test
func remoteOrderRejectsQuantityBelowContractLotBeforeSubmission() async throws {
    let runner = TradingAuditRunner(lotSize: 10)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: FileManager.default.temporaryDirectory.appendingPathComponent("trading-audit-\(UUID().uuidString)")), riskEngine: RiskEngine(initialEquity: 100_000))

    await #expect(throws: ATKError.invalidOrder("数量必须按合约 lotSz 对齐且不小于 minSz（当前数量为合约张数）")) {
        _ = try await backend.placePaperOrder(PaperOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1))
    }
}

@Test
func failedReduceOnlyRemoteOrderDoesNotReleaseAnotherPosition() async throws {
    let runner = TradingAuditRunner(rejectPlace: true, hasPosition: true)
    let risk = RiskEngine(initialEquity: 1_000)
    let client = ATKClient(runner: runner)
    let market = MarketDataService(client: client, ttl: MarketCacheTTL(ticker: 0, account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: FileManager.default.temporaryDirectory.appendingPathComponent("trading-audit-\(UUID().uuidString)")), riskEngine: risk)
    let request = PaperOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "sell", quantity: 1, reduceOnly: true)
    do {
        _ = try await backend.placePaperOrder(request)
        Issue.record("expected the simulated exchange rejection")
    } catch {
        // Expected: the exchange command failed after authorization.
    }
    let notionals = await risk.snapshot().globalNotionals
    #expect(notionals.first?.key == "BTC-USDT-SWAP")
    #expect(notionals.first?.value == 100)
}

@Test
func accountReconciliationKeepsInFlightOrderReservation() async throws {
    let runner = TradingAuditRunner(delayedPlace: true)
    let risk = RiskEngine(initialEquity: 1_000)
    let client = ATKClient(runner: runner)
    let market = MarketDataService(client: client, ttl: MarketCacheTTL(ticker: 0, account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: FileManager.default.temporaryDirectory.appendingPathComponent("trading-audit-\(UUID().uuidString)")), riskEngine: risk)
    let placing = Task { try await backend.placePaperOrder(PaperOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1)) }
    // The remote command sleeps after local authorization. This refresh runs
    // against an empty exchange snapshot while that command is still pending.
    try await Task.sleep(for: .milliseconds(35))
    _ = try await backend.account()
    let duringSubmit = await risk.snapshot().globalNotionals
    #expect(duringSubmit.first?.key == "BTC-USDT-SWAP")
    #expect(duringSubmit.first?.value == 100)
    _ = try await placing.value
}
