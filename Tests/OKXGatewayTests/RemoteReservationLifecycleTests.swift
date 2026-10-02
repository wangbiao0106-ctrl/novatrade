import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

private actor RemoteLifecycleRunner: ATKCommandRunning {
    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"api_key":"key","demo":true}}}"#)
        }
        if command.hasPrefix("account balance-all") {
            return ATKCommandResult(stdout: #"{"trading":{"totalEq":"1000","adjEq":"1000","details":[{"ccy":"USDT","eq":"1000","availEq":"1000"}]},"valuation":{"totalBal":"1000"}}"#)
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"demo"}]"#) }
        if command == "swap orders --json" {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ordId":"previous-entry","side":"sell","state":"rejected","sz":"1","accFillSz":"0","cTime":"1700000000000"}]"#)
        }
        if command.hasPrefix("account positions-history") { return ATKCommandResult(stdout: "[]") }
        if command.hasPrefix("market instruments") {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
        }
        return ATKCommandResult(stdout: "[]")
    }
}

@Test("Completed remote entries never release a later strategy reservation")
func closedRemoteEntryIsNotRecoveredOnLaterReconciliationOrRestart() async throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("remote-terminal-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let paper = PaperTradingStore(directory: directory)
    let config = try await paper.create(StrategyConfig(name: "生命周期", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort))
    let risk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100), initialEquity: 1_000)
    await risk.synchronizeStrategyCapital(1_000)
    _ = await risk.registerStrategy(config.id)
    let previous = await risk.authorize(instrumentID: "BTC-USDT-SWAP", notional: 100, margin: 100, strategyID: config.id, riskAmount: 10)
    #expect(previous.allowed)
    let previousTime = Date().addingTimeInterval(-60)
    let previousSignal = StrategySignal(strategyID: config.id, type: "entry_short", price: 100, reason: "previous entry", timestamp: previousTime, stopPrice: 110)
    let previousOrder = PaperOrder(strategyID: config.id, instrumentID: "BTC-USDT-SWAP", side: "short", quantity: 1, requestedAt: previousTime, status: "submitted", remoteOrderID: "previous-entry", signal: previousSignal)
    await paper.record(previousOrder)
    await paper.setRisk(await risk.snapshot())
    let market = MarketDataService(client: ATKClient(runner: RemoteLifecycleRunner()), ttl: MarketCacheTTL(account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: paper, riskEngine: risk)

    _ = try await backend.account()
    _ = try await backend.account()
    #expect((await risk.strategyCapital(config.id)).reservedCapital == 0)
    #expect((await paper.allOrders()).first(where: { $0.id == previousOrder.id })?.status == "closed")

    let restartedRisk = RiskEngine(limits: RiskLimits(maxMarginPercent: 100), initialEquity: 1_000)
    let restarted = TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: restartedRisk)
    _ = try await restarted.account()
    let restartedPool = await restartedRisk.strategyCapital(config.id)
    #expect(restartedPool.reservedCapital == 0)
    #expect(restartedPool.openRisk == 0)
    #expect(restartedPool.openPositions == 0)
}
