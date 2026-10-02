import Foundation
import Testing
import ATKGateway
import TradingDomain
@testable import TradingService

/// Demo-account double whose order response is lost (the CLI call throws)
/// and whose clOrdId lookup can fail, report the order absent, or find it.
private actor UnresolvedSubmissionRunner: ATKCommandRunning {
    enum Lookup { case failing, absent, accepted(String) }
    private var lookup: Lookup = .failing
    private var hasPosition = false

    func setLookup(_ value: Lookup) { lookup = value }
    func setHasPosition(_ value: Bool) { hasPosition = value }

    func run(arguments: [String]) async throws -> ATKCommandResult {
        let command = arguments.joined(separator: " ")
        if command.contains("swap place") {
            throw ATKError.unavailable("ATK 命令超时")
        }
        if command.contains("swap get ") {
            switch lookup {
            case .failing:
                throw ATKError.unavailable("ATK 查询超时")
            case .absent:
                return ATKCommandResult(stdout: #"{"code":"51603","msg":"Order does not exist"}"#)
            case let .accepted(orderID):
                let clientOrderID = arguments.firstIndex(of: "--clOrdId").map { arguments[$0 + 1] } ?? ""
                return ATKCommandResult(stdout: "[{\"instId\":\"BTC-USDT-SWAP\",\"ordId\":\"\(orderID)\",\"clOrdId\":\"\(clientOrderID)\",\"side\":\"buy\",\"state\":\"filled\",\"sz\":\"1\",\"cTime\":\"1700000000000\"}]")
            }
        }
        if command == "config show --json" {
            return ATKCommandResult(stdout: #"{"default_profile":"demo","profiles":{"demo":{"site":"global","api_key":"key","demo":true}}}"#)
        }
        if command.hasPrefix("account balance-all") {
            return ATKCommandResult(stdout: #"{"trading":{"totalEq":"1000","adjEq":"1000","details":[{"ccy":"USDT","eq":"1000","availEq":"1000","eqUsd":"1000"}]},"valuation":{"totalBal":"1000"}}"#)
        }
        if command == "account config --json" { return ATKCommandResult(stdout: #"[{"label":"demo"}]"#) }
        if command.hasPrefix("account positions") || command == "swap positions --json" {
            return ATKCommandResult(stdout: hasPosition
                ? #"[{"instId":"BTC-USDT-SWAP","pos":"1","posId":"position-1","avgPx":"100","posSide":"net"}]"# : "[]")
        }
        if command == "swap orders --json" { return ATKCommandResult(stdout: "[]") }
        if command.hasPrefix("market ticker BTC-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","last":"100","ts":"1700000000000"}]"#)
        }
        if command.hasPrefix("market instruments --instType SWAP --instId BTC-USDT-SWAP") {
            return ATKCommandResult(stdout: #"[{"instId":"BTC-USDT-SWAP","ctVal":"1","ctMult":"1","lotSz":"1","minSz":"1","tickSz":"0.1","state":"live","ctType":"linear","settleCcy":"USDT"}]"#)
        }
        return ATKCommandResult(stdout: "[]")
    }
}

private func makeUnresolvedBackend(runner: UnresolvedSubmissionRunner, directory: URL, risk: RiskEngine) -> TradingBackend {
    let market = MarketDataService(client: ATKClient(runner: runner), ttl: MarketCacheTTL(ticker: 0, account: 0, positions: 0, orders: 0))
    return TradingBackend(market: market, paper: PaperTradingStore(directory: directory), riskEngine: risk)
}

private func persistedReservationKeys(_ directory: URL) throws -> Set<String> {
    let data = try Data(contentsOf: directory.appendingPathComponent("remote-reservations.json"))
    let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    return Set(object.keys)
}

/// Backdates every persisted reservation so the 30-second publication grace
/// period no longer explains why it is still counted.
private func backdatePersistedReservations(_ directory: URL) throws {
    let url = directory.appendingPathComponent("remote-reservations.json")
    var object = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: [String: Any]] ?? [:]
    for key in object.keys {
        object[key]?["createdAt"] = Date().addingTimeInterval(-3_600).timeIntervalSinceReferenceDate
    }
    try JSONSerialization.data(withJSONObject: object).write(to: url)
}

@Test("An unknown submit outcome keeps its reservation across restarts until OKX reports the order absent")
func unknownSubmitOutcomeKeepsReservationUntilLookupReportsAbsent() async throws {
    let runner = UnresolvedSubmissionRunner()
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("unresolved-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let backend = makeUnresolvedBackend(runner: runner, directory: directory, risk: RiskEngine(initialEquity: 1_000))

    do {
        _ = try await backend.placePaperOrder(PaperOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1))
        Issue.record("expected the unknown submit outcome to be reported")
    } catch {
        #expect(error.localizedDescription.contains("下单结果未知"))
    }
    #expect((await backend.riskEngine.snapshot()).globalNotionals["BTC-USDT-SWAP"] == 100)
    let keys = try persistedReservationKeys(directory)
    #expect(keys.count == 1)
    #expect(keys.first?.hasPrefix("unresolved-") == true)

    // Neither a restart nor an old timestamp may drop a reservation whose
    // order could exist while the lookup keeps failing.
    try backdatePersistedReservations(directory)
    let restartedRisk = RiskEngine(initialEquity: 1_000)
    let restarted = makeUnresolvedBackend(runner: runner, directory: directory, risk: restartedRisk)
    _ = try await restarted.account()
    #expect((await restartedRisk.snapshot()).globalNotionals["BTC-USDT-SWAP"] == 100)

    await runner.setLookup(.absent)
    _ = try await restarted.account()
    #expect((await restartedRisk.snapshot()).globalNotionals["BTC-USDT-SWAP"] == nil)
    #expect(try persistedReservationKeys(directory).isEmpty)
}

@Test("A later clOrdId lookup moves an unknown submission's reservation to the accepted order id")
func unknownSubmitOutcomeIsRekeyedWhenLookupFindsTheOrder() async throws {
    let runner = UnresolvedSubmissionRunner()
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("unresolved-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: directory) }
    let risk = RiskEngine(initialEquity: 1_000)
    let backend = makeUnresolvedBackend(runner: runner, directory: directory, risk: risk)

    _ = try? await backend.placePaperOrder(PaperOrderRequest(instrumentID: "BTC-USDT-SWAP", side: "buy", quantity: 1))
    let unresolvedKey = try #require(try persistedReservationKeys(directory).first)
    let clientOrderID = String(unresolvedKey.dropFirst("unresolved-".count))
    await runner.setLookup(.accepted("late-order"))
    await runner.setHasPosition(true)
    _ = try await backend.account()

    #expect(try persistedReservationKeys(directory) == ["late-order"])
    let reservations = try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("remote-reservations.json"))) as? [String: [String: Any]] ?? [:]
    #expect(reservations["late-order"]?["clientOrderID"] as? String == clientOrderID)

    // Within the 30-second publication window reconciliation counts both the
    // position and the just-submitted reservation. After a restart only the
    // reservation remains, so the exposure is back to the single order.
    try backdatePersistedReservations(directory)
    let restartedRisk = RiskEngine(initialEquity: 1_000)
    let restarted = makeUnresolvedBackend(runner: runner, directory: directory, risk: restartedRisk)
    _ = try await restarted.account()
    #expect((await restartedRisk.snapshot()).globalNotionals["BTC-USDT-SWAP"] == 100)
}
