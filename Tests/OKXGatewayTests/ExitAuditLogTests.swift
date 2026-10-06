import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

private actor ExitAuditRunner: ATKCommandRunning {
    private var positionVisible = true
    private var positionSide = "long"
    private var markPrice: Decimal = 100
    private var placeCount = 0
    private var closedHistory: String = "[]"
    private var nativeHistory: String = "[]"

    init(positionSide: String = "long", markPrice: Decimal = 100) {
        self.positionSide = positionSide
        self.markPrice = markPrice
    }

    func setPositionVisible(_ visible: Bool) { positionVisible = visible }
    func setMarkPrice(_ price: Decimal) { markPrice = price }
    func setClosedHistory(_ value: String) { closedHistory = value }
    func setNativeHistory(_ value: String) { nativeHistory = value }
    func placedCount() -> Int { placeCount }

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        if command.contains("swap place") {
            placeCount += 1
            return ATKCommandResult(stdout: #"[{"ordId":"exit-order-1","clOrdId":"exit-client-1","sCode":"0"}]"#)
        }
        if command.contains("algo orders") && command.contains("--history") {
            return ATKCommandResult(stdout: nativeHistory)
        }
        if command.hasPrefix("account positions-history") {
            return ATKCommandResult(stdout: closedHistory)
        }
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#)
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"demo"}]"#) }
        if command.hasPrefix("account balance-all") {
            return ATKCommandResult(stdout: #"{"trading":{"totalEq":"1000","adjEq":"1000","details":[{"ccy":"USDT","eq":"1000","availEq":"1000","eqUsd":"1000"}]},"valuation":{"totalBal":"1000"}}"#)
        }
        if command == "swap positions --json" {
            guard positionVisible else { return ATKCommandResult(stdout: "[]") }
            return ATKCommandResult(stdout: "[{\"instId\":\"FIL-USDT-SWAP\",\"pos\":\"1\",\"posId\":\"position-1\",\"avgPx\":\"100\",\"markPx\":\"\(markPrice)\",\"posSide\":\"\(positionSide)\",\"mgnMode\":\"cross\"}]")
        }
        if command == "swap orders --json" { return ATKCommandResult(stdout: "[]") }
        if command.hasPrefix("market instruments --instType SWAP --instId FIL-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"FIL-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
        }
        if command.hasPrefix("market ticker FIL-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"FIL-USDT-SWAP","last":"100","ts":"1791307820000"}]"#)
        }
        return ATKCommandResult(stdout: "[]")
    }
}

private func exitCandle(at timestamp: Date = .now) -> Candle {
    Candle(timestamp: timestamp, open: 100, high: 100, low: 100, close: 100, confirmed: false)
}

private struct ExitFixture {
    let backend: TradingBackend
    let runner: ExitAuditRunner
    let directory: URL
}

private func makeExitFixture(
    side: String = "long", markPrice: Decimal = 100,
    requestedAt: Date = .now.addingTimeInterval(-60),
    stopPrice: Decimal? = 95, takePrice: Decimal? = 110
) async throws -> ExitFixture {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("exit-audit-\(UUID().uuidString)", isDirectory: true)
    let paper = PaperTradingStore(directory: directory)
    let config = try await paper.create(StrategyConfig(name: "退出审计", scope: .dynamic(.hotAltcoins), interval: .oneHour, type: .sweepReversalShort))
    let signal = StrategySignal(strategyID: config.id, type: side == "short" ? "entry_short" : "entry_long", price: 100, reason: "fixture", timestamp: requestedAt, stopPrice: stopPrice, takePrice: takePrice)
    let order = PaperOrder(strategyID: config.id, instrumentID: "FIL-USDT-SWAP", side: side, quantity: 1, requestedAt: requestedAt, status: "submitted", remoteOrderID: "entry-order-1", clientOrderID: "entry-client-1", signal: signal)
    await paper.record(order)
    let runner = ExitAuditRunner(positionSide: side, markPrice: markPrice)
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: paper, riskEngine: RiskEngine(initialEquity: 1_000))
    return ExitFixture(backend: backend, runner: runner, directory: directory)
}

private func messages(_ backend: TradingBackend) async -> [String] {
    let logs = await backend.logs()
    return logs.map(\.message)
}

@Test
func serviceExitLogsStopReasonAndSettlesOnce() async throws {
    let fixture = try await makeExitFixture(markPrice: 90)
    defer { try? FileManager.default.removeItem(at: fixture.directory) }

    _ = await fixture.backend.ingestRealtimeCandle(exitCandle(), instrumentID: "FIL-USDT-SWAP", interval: .fifteenMinutes)
    #expect(await fixture.runner.placedCount() == 1)
    await fixture.runner.setPositionVisible(false)
    _ = await fixture.backend.ingestRealtimeCandle(exitCandle(at: .now.addingTimeInterval(60)), instrumentID: "FIL-USDT-SWAP", interval: .fifteenMinutes)

    let logs = await messages(fixture.backend)
    #expect(logs.filter { $0.contains("event=exit_submitted") }.count == 1)
    #expect(logs.contains { $0.contains("event=exit_submitted") && $0.contains("source=service") && $0.contains("reason=stop_loss") && $0.contains("instrument=FIL-USDT-SWAP") && $0.contains("positionID=position-1") && $0.contains("observedPrice=90") && $0.contains("stopPrice=95") })
    #expect(logs.filter { $0.contains("event=exit_settled") }.count == 1)
    #expect(logs.contains { $0.contains("event=exit_settled") && $0.contains("source=service_reduce_only") && $0.contains("reason=stop_loss") && $0.contains("realizedPnL=") })
}

@Test
func serviceExitLogsTakeProfitAndTimeoutReasons() async throws {
    let take = try await makeExitFixture(markPrice: 115)
    defer { try? FileManager.default.removeItem(at: take.directory) }
    _ = await take.backend.ingestRealtimeCandle(exitCandle(), instrumentID: "FIL-USDT-SWAP", interval: .fifteenMinutes)
    #expect(await take.runner.placedCount() == 1)
    let takeLogs = await messages(take.backend)
    #expect(takeLogs.contains { $0.contains("event=exit_submitted") && $0.contains("reason=take_profit") && $0.contains("takePrice=110") })

    let timeout = try await makeExitFixture(markPrice: 100, requestedAt: .now.addingTimeInterval(-96 * 3600 - 10), stopPrice: 95, takePrice: 110)
    defer { try? FileManager.default.removeItem(at: timeout.directory) }
    _ = await timeout.backend.ingestRealtimeCandle(exitCandle(), instrumentID: "FIL-USDT-SWAP", interval: .fifteenMinutes)
    #expect(await timeout.runner.placedCount() == 1)
    let timeoutLogs = await messages(timeout.backend)
    #expect(timeoutLogs.contains { $0.contains("event=exit_submitted") && $0.contains("reason=timeout") && $0.contains("observedPrice=100") })
}

@Test
func nativeProtectionWinsServiceExitRaceAndIsNotLoggedAgainAfterRestart() async throws {
    let fixture = try await makeExitFixture(markPrice: 115)
    defer { try? FileManager.default.removeItem(at: fixture.directory) }
    _ = await fixture.backend.ingestRealtimeCandle(exitCandle(), instrumentID: "FIL-USDT-SWAP", interval: .fifteenMinutes)
    await fixture.runner.setClosedHistory(#"[{"instId":"FIL-USDT-SWAP","posId":"position-1","realizedPnl":"52.7","uTime":"1791307820000","openAvgPx":"100","closeAvgPx":"1.19517","closeTotalPos":"1"}]"#)
    await fixture.runner.setNativeHistory(#"[{"algoId":"algo-1","instId":"FIL-USDT-SWAP","state":"effective","actualSide":"tp","actualSz":"1","tpTriggerPx":"110","tpTriggerPxType":"mark","triggerTime":"1791307820000","attachAlgoClOrdId":"entry-client-1","posId":"position-1","ordIdList":["child-1"]}]"#)
    await fixture.runner.setPositionVisible(false)
    _ = await fixture.backend.ingestRealtimeCandle(exitCandle(at: .now.addingTimeInterval(60)), instrumentID: "FIL-USDT-SWAP", interval: .fifteenMinutes)
    #expect(await fixture.runner.placedCount() == 1)

    let firstLogs = await messages(fixture.backend)
    #expect(firstLogs.filter { $0.contains("event=exit_settled") }.count == 1)
    #expect(firstLogs.contains { $0.contains("event=exit_settled") && $0.contains("source=exchange_native_oco") && $0.contains("reason=take_profit") && $0.contains("actualSide=tp") && $0.contains("algoID=algo-1") && $0.contains("realizedPnL=52.7") })

    let restarted = TradingBackend(market: MarketDataService(client: ATKClient(runner: fixture.runner), ttl: MarketCacheTTL(ticker: 0, account: 0, positions: 0, orders: 0)), paper: PaperTradingStore(directory: fixture.directory), riskEngine: RiskEngine(initialEquity: 1_000))
    _ = try? await restarted.account()
    let restartedLogs = await messages(restarted)
    #expect(restartedLogs.filter { $0.contains("event=exit_settled") }.count == 1)
}
