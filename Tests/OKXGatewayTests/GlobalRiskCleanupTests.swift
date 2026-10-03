import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

private actor RiskCleanupRunner: ATKCommandRunning {
    private var equity: Decimal = 1_000
    private var hasPosition = true
    private var closeCount = 0
    private var closeCommands: [String] = []
    private let marginMode: String

    init(marginMode: String = "cross") { self.marginMode = marginMode }

    func setEquity(_ value: Decimal) { equity = value }
    func confirmFlat() { hasPosition = false }
    func closes() -> Int { closeCount }
    func closeArguments() -> [String] { closeCommands }

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        if command.contains("swap close") {
            closeCount += 1
            closeCommands.append(command)
            // An accepted close stays visible until confirmFlat, just as an
            // asynchronous exchange operation can precede position updates.
            return ATKCommandResult(stdout: #"{"code":"0","data":[{"instId":"BTC-USDT-SWAP"}]}"#)
        }
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#)
        }
        if command.hasPrefix("account balance-all") {
            return ATKCommandResult(stdout: "{\"trading\":{\"totalEq\":\"\(equity)\",\"adjEq\":\"\(equity)\",\"details\":[{\"ccy\":\"USDT\",\"eq\":\"\(equity)\",\"availEq\":\"\(equity)\",\"eqUsd\":\"\(equity)\"}]},\"valuation\":{\"totalBal\":\"\(equity)\"}}")
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"demo"}]"#) }
        if command.hasPrefix("account positions") || command == "swap positions --json" {
            return ATKCommandResult(stdout: hasPosition
                ? #"[{"instId":"BTC-USDT-SWAP","pos":"1","posId":"risk-position","avgPx":"100","posSide":"long","mgnMode":"\#(marginMode)"}]"# : "[]")
        }
        if command.hasPrefix("market instruments") {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
        }
        return ATKCommandResult(stdout: "[]")
    }
}

@Test
func globalCircuitBreakerRetriesAcceptedCloseUntilRemotePositionIsFlat() async throws {
    let runner = RiskCleanupRunner()
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("risk-cleanup-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory))
    _ = try await backend.account()

    await runner.setEquity(700)
    _ = try await backend.account()
    #expect((await backend.riskEngine.snapshot()).killSwitch)
    #expect(await runner.closes() == 1)

    _ = try await backend.account()
    #expect(await runner.closes() == 2)
    await runner.confirmFlat()
    _ = try await backend.account()
    _ = try await backend.account()
    #expect(await runner.closes() == 2)
}

@Test("Global circuit breaker closes an isolated position with its own margin mode")
func globalCircuitBreakerClosesIsolatedPositionInIsolatedMode() async throws {
    let runner = RiskCleanupRunner(marginMode: "isolated")
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("risk-cleanup-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(account: 0, positions: 0, orders: 0))
    let backend = TradingBackend(market: market, paper: PaperTradingStore(directory: directory))
    _ = try await backend.account()

    await runner.setEquity(700)
    _ = try await backend.account()
    let closes = await runner.closeArguments()
    #expect(closes.count == 1)
    #expect(closes.first?.contains("--mgnMode isolated") == true)
}
